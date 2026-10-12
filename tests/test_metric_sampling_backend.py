"""Per-job metric requests isolate expensive probes and retain scheduler limits."""
from dataclasses import replace
from types import SimpleNamespace
import threading
import concurrent.futures

import pytest

from raven.model import GpuSample, Job, Live, Store
from raven.sampler import Sampler
from raven.slurm import CommandError


def identity(jid="1", metric="cpu:rate", attempt="submit|start", scope="resource-series"):
    return (scope, jid, metric, "%", attempt, None, None, None)


class Clock:
    value = 100.0


@pytest.fixture
def worker(monkeypatch):
    timer = Clock()
    monkeypatch.setattr("raven.sampler.time.monotonic", lambda: timer.value)
    store = Store(persist=False)
    store.apply_jobs([Job(jid, "job", "local", "RUNNING", submit="submit", start="start", gpus=1,
                          workdir="/project") for jid in ("1", "2")])
    calls = {"live": [], "gpu": [], "allocations": [], "trace": []}

    def live(job, previous, now):
        calls["live"].append((job.id, timer.value))
        return Live(rate=.5), (now, 1), []

    def gpu(job):
        calls["gpu"].append((job.id, timer.value))
        return [GpuSample(0, 50, 100, 200, "GPU")]

    def allocations():
        calls["allocations"].append(timer.value)
        return {"1": ("gpu", 1), "2": ("gpu", 1)}

    def trace(path, files):
        calls["trace"].append((path, timer.value))
        return [(timer.value, 0, 50, 100)]

    slurm = SimpleNamespace(live=live, gpu=gpu, gpu_allocations=allocations, gpu_trace=trace,
                            jobs=lambda: list(store.jobs))
    # This fixture explicitly exercises the fast five-second slider domain.
    # Slow configured cadences now remain slow instead of being discarded.
    sampler = Sampler(slurm, store, {"live": 5, "gpu": 5, "trace": 5}, [], workers=1,
                      files=SimpleNamespace(remote=False, exists=lambda path: True))
    for health in store.health.values():
        health.enabled = False
    yield sampler, store, calls, timer
    sampler.shutdown()


@pytest.mark.parametrize("rate,expected", [(1, 5), (2, 4.885049786496126), (20, 3.21403655864216),
                                         (40, 2.0185086292982772), (100, .5), (100.0, .5)])
def test_exact_job_rate_uses_complete_polling_domain(worker, rate, expected):
    sampler, _, _, _ = worker
    sampler.set_metric_sampling({identity(): rate})
    assert sampler.sampling_interval("live", "1", "submit|start") == pytest.approx(expected)
    assert sampler.sampling_interval("live", "2", "submit|start") == 5
    assert sampler.effective_interval("live") == pytest.approx(expected)
    assert sampler.intervals["live"] == 5


@pytest.mark.parametrize("source,metric", [("live", "cpu:rate"), ("gpu", "gpu:util"),
                                         ("trace", "gpu-trace:util")])
def test_every_local_metric_slider_position_changes_actual_collector_interval(worker, source, metric):
    sampler, _, _, _ = worker
    intervals = []
    for position in range(1, 101):
        sampler.set_metric_sampling({identity(metric=metric): position})
        intervals.append(sampler.sampling_interval(source, "1", "submit|start"))
        assert sampler.sampling_interval(source, "2") == 5
    assert intervals[0] == 5 and intervals[-1] == .5
    assert all(left > right for left, right in zip(intervals, intervals[1:]))


def test_middle_poll_slider_position_gates_real_probe_completion(worker):
    sampler, _, calls, timer = worker
    sampler.set_metric_sampling({identity(): 50})
    sampler.run_source("live")
    timer.value = 101.5996
    sampler.run_source("live")
    assert calls["live"] == [("1", 100), ("2", 100)]
    timer.value = 101.5997
    sampler.run_source("live")
    assert calls["live"] == [("1", 100), ("2", 100), ("1", 101.5997)]


def test_global_poll_slider_does_not_bypass_remote_trace_floor(worker):
    sampler, _, _, _ = worker
    sampler.files.remote = True
    sampler.set_polling_multiplier(50)
    assert sampler.sampling_interval("trace", "1") == 1.5
    assert sampler.effective_interval("trace") == 1.5
    assert sampler._baseline_interval("trace") == 1.5


def test_trace_cadence_fallback_keeps_remote_floor_at_default_metric_position():
    from raven.metric_sampling import cadence
    model = SimpleNamespace(cfg={"polling_multiplier": 50},
                            sampler=SimpleNamespace(intervals={"trace": 5}, files=SimpleNamespace(remote=True)))
    assert cadence(model, identity(metric="gpu-trace:util"), rate=1) == 1.5


@pytest.mark.parametrize("bad", [0, -1, 101, 1.5, True, False, "100", None, float("nan"), float("inf"), 10**1000])
def test_invalid_rate_is_atomic_and_does_not_wake(worker, bad):
    sampler, _, _, _ = worker
    sampler.set_metric_sampling({identity(): 20})
    sampler.kick.clear()
    with pytest.raises(ValueError):
        sampler.set_metric_sampling({identity(): bad})
    assert sampler.effective_interval("live") == pytest.approx(3.21403655864216)
    assert not sampler.kick.is_set()


@pytest.mark.parametrize("bad", [None, [], {identity(str(index)): 2 for index in range(129)}])
def test_invalid_request_container_is_atomic(worker, bad):
    sampler, _, _, _ = worker
    sampler.set_metric_sampling({identity(): 20})
    with pytest.raises(ValueError):
        sampler.set_metric_sampling(bad)
    assert sampler.effective_interval("live") == pytest.approx(3.21403655864216)


def test_shared_collector_uses_fastest_metric_and_reset_restores_global_request(worker):
    sampler, _, _, _ = worker
    cpu, memory = identity(), identity(metric="mem:peak")
    sampler.set_metric_sampling({cpu: 20, memory: 40})
    assert sampler.effective_interval("live") == pytest.approx(2.0185086292982772)
    assert sampler.sampling_interval("live", "1") == pytest.approx(2.0185086292982772)
    assert sampler.sampling_interval("live", "2") == 5
    sampler.set_metric_sampling({cpu: 20})
    assert sampler.effective_interval("live") == pytest.approx(3.21403655864216)
    sampler.set_metric_sampling({cpu: 1})
    assert sampler.effective_interval("live") == 5
    sampler.set_polling_multiplier(50)
    sampler.set_metric_sampling({cpu: 40})
    assert sampler.effective_interval("live") == .5
    assert sampler.sampling_interval("live", "2") == .5
    sampler.set_polling_multiplier(1)
    assert sampler.effective_interval("live") == pytest.approx(2.0185086292982772)
    sampler.set_metric_sampling({})
    assert sampler.effective_interval("live") == 5
    assert sampler.intervals["live"] == 5


def test_demand_updates_wake_once_without_force_refresh_or_backoff_reset(worker):
    sampler, _, _, _ = worker
    sampler.last_run["live"] = 100
    sampler.health("live").backoff = 40
    sampler.set_metric_sampling({identity(): 100})
    assert sampler.kick.is_set()
    sampler.kick.clear()
    sampler.set_metric_sampling({identity(): 100})
    assert not sampler.kick.is_set()
    assert sampler.last_run["live"] == 100 and sampler.health("live").backoff == 40
    sampler.set_metric_sampling({})
    assert sampler.kick.is_set()
    assert sampler.last_run["live"] == 100 and sampler.health("live").backoff == 40


def test_fresh_queue_rows_preserve_demand_without_waking_for_elapsed_changes(worker):
    sampler, store, _, _ = worker
    sampler.set_metric_sampling({identity(): 100})
    sampler.kick.clear()
    sampler.slurm.jobs = lambda: [replace(job, elapsed="1:00") for job in store.jobs]
    sampler.src_jobs()
    assert sampler.effective_interval("live") == .5
    assert not sampler.kick.is_set()


def test_fast_live_ticks_do_not_probe_other_jobs(worker):
    sampler, _, calls, timer = worker
    sampler.set_metric_sampling({identity(): 100})
    sampler.run_source("live")
    for tick in (100.5, 101, 104.5):
        timer.value = tick
        sampler.run_source("live")
    assert [jid for jid, _ in calls["live"]].count("2") == 1
    timer.value = 105
    sampler.run_source("live")
    assert [time for jid, time in calls["live"] if jid == "2"] == [100, 105]
    assert [time for jid, time in calls["live"] if jid == "1"] == [100, 100.5, 101, 104.5, 105]
    assert len(sampler._metric_completed) == 1


def test_unaffected_jobs_follow_global_rate_without_local_acceleration(worker):
    sampler, _, calls, timer = worker
    sampler.set_polling_multiplier(25)
    sampler.set_metric_sampling({identity(): 100})
    sampler.run_source("live")
    timer.value = 101.6187
    sampler.run_source("live")
    timer.value = 101.6188
    sampler.run_source("live")
    assert [time for jid, time in calls["live"] if jid == "2"] == [100, 101.6188]


def test_direct_source_calls_remain_compatible_with_explicit_refresh(worker):
    sampler, _, calls, timer = worker
    sampler.set_metric_sampling({identity(): 100})
    sampler.src_live()
    timer.value += .01
    sampler.src_live()
    assert [jid for jid, _ in calls["live"]] == ["1", "2", "1", "2"]


def test_probe_duration_starts_next_deadline_at_completion(worker):
    sampler, _, calls, timer = worker
    original = sampler.slurm.live

    def slow(job, previous, now):
        result = original(job, previous, now)
        if job.id == "1":
            timer.value += 2
        return result

    sampler.slurm.live = slow
    sampler.set_metric_sampling({identity(): 100})
    sampler.run_source("live")
    assert sampler._metric_completed[("live", "1", "submit|start", 0)] == 102
    timer.value = 102.49
    sampler.run_source("live")
    assert len(calls["live"]) == 2
    timer.value = 102.5
    sampler.run_source("live")
    assert [jid for jid, _ in calls["live"]] == ["1", "2", "1"]


def test_gpu_floor_and_allocation_discovery_preserve_other_job_cadence(worker):
    sampler, _, calls, timer = worker
    sampler.set_metric_sampling({identity(metric="gpu:util"): 100})
    assert sampler.effective_interval("gpu") == .5
    assert sampler.sampling_interval("gpu", "2") == 5
    for tick in (100, 100.5, 101, 105):
        timer.value = tick
        sampler.run_source("gpu")
    assert [time for jid, time in calls["gpu"] if jid == "1"] == [100, 100.5, 101, 105]
    assert [time for jid, time in calls["gpu"] if jid == "2"] == [100, 105]
    assert calls["allocations"] == [100, 100.5, 101, 105]
    sampler.set_polling_multiplier(50)
    sampler.set_metric_sampling({})
    assert sampler.effective_interval("gpu") == .5


@pytest.mark.parametrize("remote,expected", [(False, .5), (True, 1.5)])
def test_trace_file_floor_and_untouched_cache_retention(worker, remote, expected):
    sampler, store, calls, timer = worker
    sampler.files.remote = remote
    sampler.set_metric_sampling({identity(metric="gpu-trace:util"): 100})
    assert sampler.effective_interval("trace") == expected
    sampler.run_source("trace")
    original = store.trace["2"]
    timer.value += expected
    sampler.run_source("trace")
    assert store.trace["2"] is original
    assert sampler.trace_status["2"]["state"] == "ready"
    assert [path for path, _ in calls["trace"]].count("/project/logs/gpu-util-2.csv") == 1
    assert [path for path, _ in calls["trace"]].count("/project/logs/gpu-util-1.csv") == 2


@pytest.mark.parametrize("state", ["PENDING", "COMPLETING", "SUSPENDED", "CONFIGURING", "COMPLETED"])
def test_only_exact_running_attempt_is_accelerated(worker, state):
    sampler, store, _, _ = worker
    store.jobs[0].state = state
    assert sampler.set_metric_sampling({identity(): 100}) == 0
    assert sampler.effective_interval("live") == 5


@pytest.mark.parametrize("key", [identity(attempt="other|attempt"), identity("9"), identity("1_[1-4]"),
                                 ("resource-series",), identity(metric="disk:unknown")])
def test_missing_wrong_or_unsupported_identity_is_ignored(worker, key):
    sampler, _, _, _ = worker
    assert sampler.set_metric_sampling({key: 100}) == 0
    assert sampler.effective_interval("live") == 5


def test_scheduler_reported_metric_alias_can_request_same_source(worker):
    sampler, _, _, _ = worker
    key = ("reported-metric", "1", "CPU per core (%)", "Raven session resource samples",
           "scheduler:submit|start", None, None, None)
    sampler.set_metric_sampling({key: 100})
    assert sampler.effective_interval("live") == .5
    assert sampler.sampling_interval("live", "1", "scheduler:submit|start") == .5
    assert sampler.sampling_interval("live", "1", "other|attempt") == 5


def test_queue_departure_and_reused_id_drop_demand_and_completion_marks(worker):
    sampler, store, _, _ = worker
    sampler.set_metric_sampling({identity(): 100})
    sampler.run_source("live")
    sampler.slurm.jobs = lambda: [store.jobs[1]]
    sampler.src_jobs()
    assert not sampler._metric_requests and not sampler._metric_completed
    assert sampler.effective_interval("live") == 5
    sampler.slurm.jobs = lambda: [Job("1", "new", "local", "RUNNING", submit="new", start="new")]
    sampler.src_jobs()
    assert sampler.set_metric_sampling({identity(): 100}) == 0
    assert sampler.effective_interval("live") == 5


def test_list_replacement_and_in_place_completion_invalidate_rates_immediately(worker):
    sampler, store, _, _ = worker
    sampler.set_metric_sampling({identity(): 100})
    store.jobs[0].state = "COMPLETED"
    assert sampler.effective_interval("live") == 5
    store.jobs[0].state = "RUNNING"
    assert sampler.effective_interval("live") == .5
    store.apply_jobs([store.jobs[1]])
    assert sampler.effective_interval("live") == 5


@pytest.mark.parametrize("change", ["depart", "reuse", "mutate", "same_attempt"])
def test_stale_live_response_cannot_publish_or_replace_cpu_baseline(worker, change):
    sampler, store, _, _ = worker
    original = sampler.slurm.live

    def racing(job, previous, now):
        if job.id == "1":
            if change == "depart":
                store.apply_jobs([store.jobs[1]])
            elif change == "reuse":
                store.apply_jobs([replace(job, submit="different", start="different"), store.jobs[1]])
            elif change == "mutate":
                job.state = "COMPLETED"
            else:
                store.apply_jobs([replace(job, elapsed="1:00"), store.jobs[1]])
        return original(job, previous, now)

    sampler.slurm.live = racing
    sampler.src_live()
    assert ("1" in store.prev_cpu) == (change == "same_attempt")
    assert ("1" in store.steps) == (change == "same_attempt")
    assert ("1" in store.live) == (change == "same_attempt")


def test_failure_preserves_source_backoff_and_completion_deadline(worker):
    sampler, _, _, _ = worker
    sampler.health("live").enabled = True
    sampler.health("jobs").calls = 1
    sampler.set_metric_sampling({identity(): 100})

    def unavailable(job, previous, now):
        raise CommandError("scheduler unavailable")

    sampler.slurm.live = unavailable
    sampler.round(now=100, wait=True)
    assert sampler.health("live").backoff == 5
    assert sampler._metric_completed[("live", "1", "submit|start", 0)] == 100
    sampler.set_metric_sampling({identity(): 50})
    assert sampler.health("live").backoff == 5
    assert not sampler.due("live", 106.59)
    assert sampler.due("live", 106.6)


def test_inflight_and_disabled_sources_do_not_overlap_or_enable(worker):
    sampler, _, calls, _ = worker
    entered, release = threading.Event(), threading.Event()
    original = sampler.slurm.live

    def blocked(job, previous, now):
        entered.set()
        assert release.wait(2)
        return original(job, previous, now)

    sampler.slurm.live = blocked
    sampler.health("live").enabled = True
    sampler.health("jobs").calls = 1
    sampler.set_metric_sampling({identity(): 100})
    futures = sampler.round(now=100)
    try:
        assert entered.wait(1)
        for tick in (101, 200, 1000):
            assert not sampler.round(now=tick)
        assert sampler.health("live").inflight
        sampler.set_metric_sampling({identity(): 2})
        assert sampler.health("live").inflight
    finally:
        release.set()
        concurrent.futures.wait(futures, timeout=2)
    assert [jid for jid, _ in calls["live"]] == ["1", "2"]
    sampler.health("live").enabled = False
    assert not sampler.due("live", 1000)


def test_manual_refresh_clears_completion_gates(worker):
    sampler, _, calls, timer = worker
    sampler.set_metric_sampling({identity(): 100})
    sampler.run_source("live")
    timer.value += .01
    sampler.refresh_all()
    sampler.run_source("live")
    assert [jid for jid, _ in calls["live"]] == ["1", "2", "1", "2"]


def test_reset_keeps_other_jobs_original_next_deadline(worker):
    sampler, _, calls, timer = worker
    sampler.health("live").enabled = True
    sampler.health("jobs").calls = 1
    sampler.set_metric_sampling({identity(): 100})
    sampler.round(now=100, wait=True)
    timer.value = 104.5
    sampler.round(now=104.5, wait=True)
    sampler.set_metric_sampling({})
    timer.value = 105
    assert sampler.due("live", 105)
    sampler.round(now=105, wait=True)
    assert [time for jid, time in calls["live"] if jid == "2"] == [100, 105]


def test_scheduling_does_not_admit_empty_ticks_before_probe_completion_deadline(worker):
    sampler, _, calls, timer = worker
    sampler.health("live").enabled = True
    sampler.health("jobs").calls = 1
    original = sampler.slurm.live

    def delayed(job, previous, now):
        result = original(job, previous, now)
        if job.id == "1":
            timer.value += .2
        return result

    sampler.slurm.live = delayed
    sampler.set_metric_sampling({identity(): 100})
    sampler.round(now=100, wait=True)
    timer.value = 100.5
    assert not sampler.due("live", 100.5)
    timer.value = 100.701
    assert sampler.due("live", 100.701)
    sampler.round(now=100.701, wait=True)
    assert [jid for jid, _ in calls["live"]] == ["1", "2", "1"]


@pytest.mark.parametrize("timestamps", [("", ""), ("submit", "start")])
def test_reused_id_never_inherits_retained_request_with_same_timestamp_strings(worker, timestamps):
    sampler, store, _, _ = worker
    original = store.jobs[0]
    original.submit, original.start = timestamps
    key = identity(attempt="|".join(timestamps))
    sampler.set_metric_sampling({key: 100})
    store.apply_jobs([])
    replacement = replace(original, name="new", workdir="/other")
    store.apply_jobs([replacement])
    sampler.slurm.jobs = lambda: [replacement]
    sampler.src_jobs()
    assert not sampler._metric_requests
    assert sampler.effective_interval("live") == 5


@pytest.mark.parametrize("change", ["reuse", "mutate", "path", "same_attempt"])
def test_trace_publication_requires_captured_attempt_and_path(worker, change):
    sampler, store, _, _ = worker
    original = sampler.slurm.gpu_trace

    def racing(path, files):
        if path.endswith("gpu-util-1.csv"):
            current = store.jobs[0]
            if change == "reuse":
                store.apply_jobs([])
                store.apply_jobs([replace(current, name="new", workdir="/new")])
            elif change == "mutate":
                current.state = "COMPLETED"
            elif change == "path":
                current.workdir = "/new"
            else:
                store.apply_jobs([replace(current, elapsed="1:00"), store.jobs[1]])
        return original(path, files)

    sampler.slurm.gpu_trace = racing
    sampler.src_trace()
    assert ("1" in store.trace) == (change == "same_attempt")
    assert ("1" in sampler.trace_status) == (change == "same_attempt")
