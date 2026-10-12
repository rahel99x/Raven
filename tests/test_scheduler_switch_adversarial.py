"""Mode changes preserve standard Future, callback and task-tree ownership."""
from collections import Counter
from concurrent.futures import (ALL_COMPLETED, FIRST_COMPLETED, CancelledError,
                                TimeoutError, as_completed, wait)
import threading
import time
from types import SimpleNamespace

import pytest

from raven.worker_scheduler import WorkerQueueFull, WorkerScheduler
from raven.model import GpuSample, Job, Store
from raven.sampler import Sampler


@pytest.fixture
def workers():
    scheduler = WorkerScheduler(workers=2)
    gates = []
    threads = []
    def gate():
        event = threading.Event()
        gates.append(event)
        return event
    def launch(fn):
        errors, done = [], threading.Event()
        def run():
            try:
                fn()
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()
        thread = threading.Thread(target=run, daemon=True)
        threads.append(thread)
        thread.start()
        return SimpleNamespace(thread=thread, done=done, errors=errors)
    yield SimpleNamespace(scheduler=scheduler, gate=gate, launch=launch,
                          source=scheduler.lane("source"), gpu=scheduler.lane("gpu"),
                          research=scheduler.lane("research"), notify=scheduler.lane("notification"))
    for event in gates:
        event.set()
    scheduler.shutdown(wait=True, cancel_futures=True)
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()


def blocking(started, released, value):
    started.set()
    if not released.wait(3):
        raise TimeoutError("test task was not released")
    return value


def settled(scheduler, mode=None):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = scheduler.status()
        if not status["running"] and not status["queued"] and (mode is None or status["mode"] == mode):
            return status
        threading.Event().wait(.002)
    raise AssertionError("worker state did not settle: " + str(scheduler.status()))


def test_running_roots_drain_and_accepted_queue_is_kept_once_under_single(workers):
    w = workers
    starts, releases = [w.gate() for _ in range(2)], [w.gate() for _ in range(2)]
    roots = [w.source.submit(blocking, starts[index], releases[index], index) for index in range(2)]
    assert all(event.wait(1) for event in starts)
    calls, modes = [], []
    def queued(value):
        calls.append(value)
        modes.append(w.scheduler.status()["mode"])
        return value
    accepted = w.source.submit(queued, 2)
    state = w.scheduler.request_mode("single")
    assert state["mode"] == "multi" and state["pending"]
    submitted_during_drain = w.research.submit(queued, 3)
    releases[0].set()
    assert roots[0].result(1) == 0
    assert not accepted.done() and not submitted_during_drain.done()
    releases[1].set()
    futures = roots + [accepted, submitted_during_drain]
    done, pending = wait(futures, timeout=2, return_when=ALL_COMPLETED)
    assert done == set(futures) and not pending
    assert [future.result() for future in futures] == [0, 1, 2, 3]
    assert Counter(calls) == {2: 1, 3: 1} and modes == ["single", "single"]
    assert settled(w.scheduler, "single")["limit"] == 1


def test_done_callback_children_remain_in_the_running_drain_tree(workers):
    w = workers
    entered, release, callback, child_started, child_release = [w.gate() for _ in range(5)]
    root = w.source.submit(blocking, entered, release, "published")
    assert entered.wait(1)
    children, callbacks = [], []
    def complete(future):
        assert future.result() == "published"
        w.scheduler.request_mode("single")
        children.append(w.gpu.submit(blocking, child_started, child_release, "child"))
        callback.set()
        callbacks.append(children[-1].result())
    root.add_done_callback(complete)
    release.set()
    assert callback.wait(1) and child_started.wait(1)
    assert root.done() and w.scheduler.status()["pending"]
    after = w.research.submit(lambda: "after")
    assert not after.done()
    child_release.set()
    assert after.result(2) == "after"
    assert callbacks == ["child"] and children[0].result() == "child"
    assert settled(w.scheduler, "single")["draining"] == 0


def test_rapid_target_flips_with_every_multi_slot_blocked_do_not_cancel_work(workers):
    w = workers
    lanes = [w.source, w.source, w.gpu, w.gpu, w.research, w.notify]
    starts, releases = [w.gate() for _ in lanes], [w.gate() for _ in lanes]
    futures = [lane.submit(blocking, starts[index], releases[index], index)
               for index, lane in enumerate(lanes)]
    assert all(event.wait(1) for event in starts)
    assert w.scheduler.status()["running"] == w.scheduler.multi_limit == len(lanes)
    for _ in range(8):
        assert w.scheduler.request_mode("single")["pending"]
        status = w.scheduler.request_mode("multi")
        assert status["mode"] == "multi" and not status["pending"]
    accepted = w.source.submit(lambda: "kept")
    releases[0].set()
    assert accepted.result(1) == "kept"
    for release in releases:
        release.set()
    assert [future.result(1) for future in futures] == list(range(len(lanes)))
    assert settled(w.scheduler, "multi")["generation"] == 16


def test_single_nested_gpu_collection_uses_the_existing_worker_permit(workers):
    w = workers
    w.scheduler.request_mode("single")
    identities, running = [], []
    def parent():
        owner = threading.get_ident()
        def child(value):
            identities.append(threading.get_ident())
            running.append(w.scheduler.status()["running"])
            return value
        futures = [w.gpu.submit(child, index) for index in range(5)]
        pending, values = set(futures), []
        while pending:
            done, pending = w.scheduler.gather_children(pending, timeout=.01, return_when=FIRST_COMPLETED)
            values.extend(future.result() for future in done)
        return owner, sorted(values)
    owner, values = w.source.submit(parent).result(2)
    assert values == list(range(5)) and set(identities) == {owner} and set(running) == {1}
    assert settled(w.scheduler, "single")["queued"] == 0


def test_timed_future_result_inside_single_never_starts_an_opaque_child(workers):
    w = workers
    w.scheduler.request_mode("single")
    started = w.gate()
    def parent():
        child = w.gpu.submit(lambda: started.set())
        before = time.monotonic()
        with pytest.raises(TimeoutError):
            child.result(timeout=.04)
        elapsed = time.monotonic() - before
        assert not started.is_set()
        return elapsed, child
    elapsed, child = w.source.submit(parent).result(1)
    assert .025 <= elapsed < .3
    assert child.result(1) is None and started.is_set()


def test_all_multi_permits_can_unblock_nested_gpu_without_a_second_source_pool(workers):
    w = workers
    ready, parent_release = [w.gate() for _ in range(2)], w.gate()
    gpu_starts, gpu_releases = [w.gate() for _ in range(2)], [w.gate() for _ in range(2)]
    def parent(index):
        ready[index].set()
        assert parent_release.wait(2)
        return w.gpu.submit(lambda: index + 10).result()
    parents = [w.source.submit(parent, index) for index in range(2)]
    fillers = [w.gpu.submit(blocking, gpu_starts[index], gpu_releases[index], index) for index in range(2)]
    extra_starts, extra_releases = [w.gate() for _ in range(2)], [w.gate() for _ in range(2)]
    extras = [lane.submit(blocking, extra_starts[index], extra_releases[index], index)
              for index, lane in enumerate((w.research, w.notify))]
    assert all(event.wait(1) for event in ready + gpu_starts + extra_starts)
    assert w.scheduler.status()["running"] == w.scheduler.multi_limit
    parent_release.set()
    for release in gpu_releases:
        release.set()
    assert [future.result(1) for future in parents] == [10, 11]
    for release in extra_releases:
        release.set()
    assert [future.result(1) for future in fillers + extras] == [0, 1, 0, 1]
    settled(w.scheduler)


def test_cancelled_queued_future_notifies_wait_and_as_completed(workers):
    w = workers
    w.scheduler.request_mode("single")
    started, release = w.gate(), w.gate()
    root = w.source.submit(blocking, started, release, "root")
    assert started.wait(1)
    child = w.research.submit(lambda: pytest.fail("cancelled work executed"))
    assert child.cancel() and not root.cancel()
    assert wait([child], timeout=.02) == ({child}, set())
    assert list(as_completed([child], timeout=.02)) == [child]
    with pytest.raises(CancelledError):
        child.result()
    release.set()
    assert root.result(1) == "root"
    assert settled(w.scheduler)["queued"] == 0


def test_exception_callback_can_switch_and_publish_an_untimed_followup(workers):
    w = workers
    started, release, callback_done = w.gate(), w.gate(), w.gate()
    followups, callback_errors = [], []
    def fail():
        blocking(started, release, None)
        raise ValueError("synthetic source failure")
    root = w.source.submit(fail)
    assert started.wait(1)
    def done(future):
        try:
            assert isinstance(future.exception(), ValueError)
            w.scheduler.request_mode("single")
            followups.append(w.source.submit(lambda: 42).result())
            w.scheduler.request_mode("multi")
        except BaseException as error:
            callback_errors.append(error)
        finally:
            callback_done.set()
    root.add_done_callback(done)
    release.set()
    with pytest.raises(ValueError, match="synthetic"):
        root.result(1)
    assert callback_done.wait(1) and not callback_errors and followups == [42]
    settled(w.scheduler, "multi")


def test_lane_shutdown_waits_for_callback_retirement_not_only_result_publication(workers):
    w = workers
    started, release, callback_started, callback_release = [w.gate() for _ in range(4)]
    root = w.source.submit(blocking, started, release, 1)
    assert started.wait(1)
    root.add_done_callback(lambda _: blocking(callback_started, callback_release, None))
    release.set()
    assert callback_started.wait(1) and root.result(1) == 1
    closer = w.launch(lambda: w.source.shutdown(wait=True))
    assert not closer.done.wait(.05), "lane shutdown returned while its callback still owns a task"
    callback_release.set()
    assert closer.done.wait(1) and not closer.errors
    assert w.gpu.submit(lambda: 2).result(1) == 2


def test_cancel_futures_is_owner_scoped_and_notifies_external_waiters(workers):
    w = workers
    w.scheduler.request_mode("single")
    started, release = w.gate(), w.gate()
    root = w.source.submit(blocking, started, release, 1)
    assert started.wait(1)
    own = w.source.submit(lambda: pytest.fail("closed owner's queue ran"))
    other = w.research.submit(lambda: 2)
    w.source.shutdown(wait=False, cancel_futures=True)
    assert wait([own], timeout=.02) == ({own}, set())
    assert own.cancelled() and not other.cancelled()
    with pytest.raises(RuntimeError):
        w.source.submit(lambda: 3)
    release.set()
    assert root.result(1) == 1 and other.result(1) == 2


@pytest.mark.parametrize("cancel", [False, True])
def test_global_nonblocking_shutdown_finishes_callbacks_and_closes_executor(workers, cancel):
    w = workers
    w.scheduler.request_mode("single")
    started, release, callback_done = w.gate(), w.gate(), w.gate()
    root = w.source.submit(blocking, started, release, 1)
    assert started.wait(1)
    root.add_done_callback(lambda _: callback_done.set())
    queued = w.research.submit(lambda: 2)
    before = time.monotonic()
    w.scheduler.shutdown(wait=False, cancel_futures=cancel)
    assert time.monotonic() - before < .3
    with pytest.raises(RuntimeError):
        w.gpu.submit(lambda: 3)
    if cancel:
        assert wait([queued], timeout=.02) == ({queued}, set()) and queued.cancelled()
    release.set()
    assert root.result(1) == 1 and callback_done.wait(1)
    if not cancel:
        assert queued.result(1) == 2
    settled(w.scheduler)
    # Retirement publishes an empty task set immediately before asynchronous
    # executor close. Wait for physical workers to exit, not for Future.result.
    for thread in list(w.scheduler._executor._threads):
        thread.join(1)
        assert not thread.is_alive(), "nonblocking shutdown left a physical worker running"
    assert w.scheduler._executor._shutdown, "last retired task did not close the underlying executor"


def test_queue_rejection_does_not_cancel_previously_accepted_work():
    scheduler = WorkerScheduler(mode="single", max_queue=1)
    started, release = threading.Event(), threading.Event()
    lane = scheduler.lane("source")
    try:
        first = lane.submit(blocking, started, release, 1)
        assert started.wait(1)
        second = lane.submit(lambda: 2)
        with pytest.raises(WorkerQueueFull):
            lane.submit(lambda: 3)
        assert scheduler.status()["queued"] == 1 and scheduler.status()["rejected"] == 1
        scheduler.request_mode("multi")
        assert second.result(1) == 2 and not first.cancelled()
        release.set()
        assert first.result(1) == 1
    finally:
        release.set()
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_cooperative_cross_task_cycle_reports_an_error_and_retires_both_tasks(workers):
    w = workers
    w.scheduler.request_mode("single")
    armed, roots = w.gate(), {}
    def parent():
        assert armed.wait(1)
        child = w.gpu.submit(lambda: roots["parent"].result())
        return child.result()
    roots["parent"] = w.source.submit(parent)
    armed.set()
    with pytest.raises(RuntimeError, match="cyclic"):
        roots["parent"].result(1)
    assert settled(w.scheduler)["running"] == 0


def test_real_sampler_gpu_source_publishes_all_jobs_in_single_without_a_child_pool(workers):
    w = workers
    w.scheduler.request_mode("single")
    store = Store(persist=False)
    store.apply_jobs([Job(str(index), "fixture", "gpu", "RUNNING", gpus=1,
                          submit="submit", start="start") for index in range(4)])
    identities, calls = [], []
    def gpu(job):
        identities.append(threading.get_ident())
        calls.append(job.id)
        assert w.scheduler.status()["running"] == 1
        return [GpuSample(0, 50 + int(job.id), 100, 200, "fixture")]
    slurm = SimpleNamespace(gpu=gpu, gpu_allocations=lambda: {str(index): ("fixture", 1) for index in range(4)})
    sampler = Sampler(slurm, store, {"gpu": 5}, [], worker_scheduler=w.scheduler)
    try:
        def collect():
            owner = threading.get_ident()
            sampler.run_source("gpu")
            return owner
        owner = sampler.pool.submit(collect).result(2)
        assert set(identities) == {owner} and Counter(calls) == {str(index): 1 for index in range(4)}
        assert set(store.gpu) == set(calls)
        assert store.health["gpu"].calls == 1 and not store.health["gpu"].error
        assert not store.health["gpu"].inflight
    finally:
        sampler.shutdown()
    assert w.notify.submit(lambda: "independent owner survives").result(1)


def test_cancel_is_idempotent_inside_its_own_done_callback(workers):
    w = workers
    w.scheduler.request_mode("single")
    entered, release = w.gate(), w.gate()
    running = w.source.submit(blocking, entered, release, 1)
    assert entered.wait(1)
    queued = w.research.submit(lambda: 2)
    repeated = []
    queued.add_done_callback(lambda future: repeated.append(future.cancel()))
    assert queued.cancel()
    assert repeated == [True], "cancelled Future.cancel() must remain idempotent during callbacks"
    assert queued.cancel() and queued.cancelled()
    release.set()
    assert running.result(1) == 1


@pytest.mark.parametrize("scope", ["lane", "session"])
def test_waiting_shutdown_from_its_own_worker_fails_before_closing_live_work(workers, scope):
    w = workers
    w.scheduler.request_mode("single")
    def parent():
        closer = w.source if scope == "lane" else w.scheduler
        with pytest.raises(RuntimeError, match="own"):
            closer.shutdown(wait=True)
        return w.gpu.submit(lambda: 7).result()
    assert w.source.submit(parent).result(1) == 7
    assert w.source.submit(lambda: 8).result(1) == 8
    assert not settled(w.scheduler)["closed"]


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_independent_research_root_can_await_source_child_without_a_lane_cycle(mode):
    scheduler = WorkerScheduler(workers=1, mode=mode)
    source, research = scheduler.lane("source"), scheduler.lane("research")
    entered, armed = threading.Event(), threading.Event()
    futures, samples = {}, []
    def observe():
        state = scheduler.status()
        samples.append(state)
        assert 0 <= state["running"] <= state["limit"]
        assert all(0 <= lane["running"] <= lane["limit"] for lane in state["lanes"].values())
    def source_parent():
        entered.set()
        assert armed.wait(2)
        value = futures["independent"].result()
        observe()
        assert scheduler.status()["lanes"]["source"]["running"] == 1
        return value + 1
    def research_parent():
        assert armed.wait(2)
        def source_child():
            observe()
            return 11
        value = source.submit(source_child).result()
        observe()
        assert scheduler.status()["lanes"]["research"]["running"] == 1
        return value
    try:
        parent = source.submit(source_parent)
        assert entered.wait(1)
        futures["independent"] = research.submit(research_parent)
        scheduler.request_mode("single")
        armed.set()
        assert parent.result(2) == 12 and futures["independent"].result(1) == 11
        assert len(samples) == 3
        assert settled(scheduler, "single")["limit"] == 1
    finally:
        armed.set()
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_native_all_completed_deadline_allows_one_opaque_overrun_not_every_child(workers, monkeypatch):
    w = workers
    w.scheduler.request_mode("single")
    timer = [10.]
    monkeypatch.setattr("raven.worker_scheduler.time.monotonic", lambda: timer[0])
    def parent():
        def child(value):
            timer[0] += 1.
            return value
        futures = [w.gpu.submit(child, index) for index in range(2)]
        done, pending = w.scheduler.gather_children(futures, timeout=.5, return_when=ALL_COMPLETED)
        assert len(done) == len(pending) == 1
        return futures
    futures = w.source.submit(parent).result(1)
    assert sorted(future.result(1) for future in futures) == [0, 1]
