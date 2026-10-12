"""Native collection remains bounded and complete under the governed worker budget."""
from concurrent.futures import FIRST_COMPLETED, wait
from threading import Event, current_thread, get_ident
from types import SimpleNamespace

from raven.model import GpuSample, Job, Store
from raven.sampler import Sampler
from raven.worker_scheduler import WorkerScheduler


def test_full_root_queue_reserves_bounded_gpu_children_without_losing_jobs():
    scheduler = WorkerScheduler(mode="single", max_queue=4)
    store = Store(persist=False)
    jobs = [Job(str(index), "worker", "gpu", "RUNNING", gpus=1) for index in range(40)]
    store.apply_jobs(jobs)
    calls, queued_calls, sampled_queue = [], [], []
    def sample(job):
        calls.append(job.id)
        sampled_queue.append(scheduler.status()["queued"])
        return [GpuSample("node", 0, 75., 100., 1000.)]
    sampler = Sampler(SimpleNamespace(gpu=sample), store, {}, [], worker_scheduler=scheduler)
    started, release = Event(), Event()
    source = scheduler.lane("source")
    def cycle():
        started.set()
        assert release.wait(3)
        sampler.run_source("gpu")
    try:
        root = source.submit(cycle)
        assert started.wait(1)
        accepted = [source.submit(queued_calls.append, index) for index in range(4)]
        assert scheduler.status()["queued"] == 4
        release.set()
        root.result(timeout=3)
        assert wait(accepted, timeout=2).done == set(accepted)
        assert calls == [job.id for job in jobs]
        assert set(store.gpu) == {job.id for job in jobs}
        assert sampled_queue and max(sampled_queue) <= 4 + sampler.gpu_workers
        assert queued_calls == list(range(4))
        assert store.health["gpu"].calls == 1 and not store.health["gpu"].errors
    finally:
        release.set()
        sampler.shutdown()
        source.shutdown(wait=True)
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_cooperative_nested_names_and_callback_context_restore_parent_lane():
    scheduler = WorkerScheduler(mode="single")
    source, gpu = scheduler.lane("source"), scheduler.lane("gpu")
    started, release, callback_done = Event(), Event(), Event()
    observed = []
    def child():
        observed.append(("child", current_thread().name, get_ident()))
        return 7
    def parent():
        observed.append(("parent-before", current_thread().name, get_ident()))
        started.set()
        assert release.wait(3)
        pending = gpu.submit(child)
        done, remaining = scheduler.gather_children([pending], return_when=FIRST_COMPLETED)
        assert done == {pending} and not remaining and pending.result() == 7
        observed.append(("parent-after", current_thread().name, get_ident()))
        return "done"
    def completed(future):
        assert future.result() == "done"
        observed.append(("callback", current_thread().name, get_ident()))
        callback_done.set()
    try:
        future = source.submit(parent)
        assert started.wait(1)
        future.add_done_callback(completed)
        release.set()
        assert future.result(timeout=3) == "done" and callback_done.wait(1)
        assert [label for label, _, _ in observed] == ["parent-before", "child", "parent-after", "callback"]
        assert [name for _, name, _ in observed] == ["raven-src", "raven-gpu", "raven-src", "raven-src"]
        assert len({tid for _, _, tid in observed}) == 1
    finally:
        release.set()
        scheduler.shutdown(wait=True, cancel_futures=True)
