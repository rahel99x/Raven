"""Live telemetry changes preserve ownership, retained data and recovery clocks."""
from __future__ import annotations

from threading import Event
from types import SimpleNamespace

import pytest

from raven.model import Store
from raven.raven_collection import Deferred, Ledger, classify, settings, wrap_files
from raven.remote import LocalFiles
from raven.research import ResearchHub
from raven.sampler import Sampler
from raven.shared_collection import SharedQueryCache
from raven.slurm import CommandError


class Timer:
    value = 1000.

    def __call__(self):
        return self.value


def make_sampler(monkeypatch):
    timer = Timer()
    monkeypatch.setattr("raven.sampler.time.time", timer)
    sampler = Sampler(SimpleNamespace(), Store(persist=False), {"jobs": 60.}, [],
                      collection={"jitter_fraction": 0.})
    sampler.sources["jobs"] = lambda: None
    return sampler, timer


def test_live_cadence_recomputes_success_deadlines_without_forgetting_last_sample(monkeypatch):
    sampler, timer = make_sampler(monkeypatch)
    try:
        sampler.last_run["jobs"] = timer.value
        sampler.run_source("jobs")
        assert sampler._collection_not_before["jobs"] == 1060.
        timer.value = 1002.
        sampler.set_collection_settings({"queue_seconds": 1., "jitter_fraction": 0.}, {"jobs": 1.})
        assert sampler.effective_interval("jobs") == 1.
        assert sampler._collection_not_before["jobs"] == 1001.
        assert sampler.due("jobs", timer.value)
        assert sampler.health("jobs").last_ok == sampler.last_run["jobs"] == 1000.
        assert sampler.health("jobs").calls == 1
        # Returning to Light delays the next read using the same evidence clock.
        sampler.set_collection_settings({"queue_seconds": 60., "jitter_fraction": 0.}, {"jobs": 60.})
        assert not sampler.due("jobs", timer.value)
        assert sampler._collection_not_before["jobs"] == 1060.
    finally:
        sampler.shutdown()


def test_sampler_wakes_for_fast_queue_even_when_trace_cadence_is_slower(monkeypatch):
    sampler, timer = make_sampler(monkeypatch)
    try:
        assert sampler.wake_interval() == .5
        sampler.set_collection_settings({"queue_seconds": .1, "trace_seconds": 1., "jitter_fraction": 0.},
                                        {"jobs": .1, "trace": 1.})
        assert sampler.wake_interval() == .1
        sampler.health("jobs").enabled = False
        assert sampler.wake_interval() == .5
    finally:
        sampler.shutdown()


@pytest.mark.parametrize("error", [CommandError("scheduler down"), Deferred("busy", retry_after=17.)])
def test_live_cadence_retains_backoff_and_admission_deferrals(monkeypatch, error):
    sampler, timer = make_sampler(monkeypatch)
    def fail():
        raise error
    sampler.sources["jobs"] = fail
    try:
        sampler.last_run["jobs"] = timer.value
        sampler.run_source("jobs")
        before = (sampler._collection_not_before["jobs"], sampler.health("jobs").backoff,
                  dict(sampler._collection_failures), set(sampler._collection_deferred))
        timer.value += 1.
        sampler.set_collection_settings({"queue_seconds": .5, "jitter_fraction": 0.}, {"jobs": .5})
        assert before == (sampler._collection_not_before["jobs"], sampler.health("jobs").backoff,
                          sampler._collection_failures, sampler._collection_deferred)
        assert not sampler.due("jobs", timer.value)
    finally:
        sampler.shutdown()


def test_policy_change_during_a_sample_does_not_overlap_or_lose_its_result(monkeypatch):
    sampler, timer = make_sampler(monkeypatch)
    entered, release = Event(), Event()
    calls = []
    def read():
        calls.append(1)
        entered.set()
        assert release.wait(3.)
    sampler.sources["jobs"] = read
    for name in sampler.sources:
        sampler.health(name).enabled = name == "jobs"
    try:
        sampler.prime_collection_snapshot()
        work = sampler.round()[0]
        assert entered.wait(1.)
        sampler.set_collection_settings({"queue_seconds": 1., "jitter_fraction": 0.}, {"jobs": 1.})
        assert sampler.round() == []
        assert sampler.health("jobs").inflight
        release.set()
        work.result(timeout=3.)
        assert calls == [1]
        assert sampler._collection_not_before["jobs"] == timer.value + 1.
        assert not sampler.health("jobs").inflight
        assert sampler.health("jobs").calls == 1
    finally:
        release.set()
        sampler.shutdown()


def test_ledger_reconfigure_preserves_spent_budgets_and_live_reservations(tmp_path):
    timer = Timer()
    ledger = Ledger(SharedQueryCache(tmp_path / "cache"), {"user": "researcher"},
                    {"queue_seconds": .5, "controller_requests_per_minute": 3}, now=timer)
    argv = ["squeue", "-u", "me"]
    with ledger.admit(classify(argv), argv):
        before = ledger.report()
        ledger.reconfigure({"queue_seconds": .5, "controller_requests_per_minute": 1})
        assert ledger.report() == before
        with pytest.raises(Deferred, match="lane is occupied"):
            with ledger.admit(classify(argv), argv):
                pytest.fail("an active collector must retain its reservation")
    timer.value += 2.
    with pytest.raises(Deferred, match="budget exhausted"):
        with ledger.admit(classify(argv), argv):
            pytest.fail("switching settings must not mint a fresh allowance")
    ledger.reconfigure({"queue_seconds": .5, "controller_requests_per_minute": 3})
    with ledger.admit(classify(argv), argv):
        pass
    assert ledger.report()["rates_last_minute"]["controller"] == 2


def test_ledger_uses_new_floor_but_keeps_failed_source_recovery(tmp_path):
    timer = Timer()
    ledger = Ledger(SharedQueryCache(tmp_path / "cache"), {"user": "researcher"}, {}, now=timer)
    argv = ["squeue", "-u", "me"]
    with ledger.admit(classify(argv), argv):
        pass
    timer.value += 1.
    ledger.reconfigure({"queue_seconds": .5})
    with pytest.raises(CommandError, match="offline"):
        with ledger.admit(classify(argv), argv):
            raise CommandError("offline")
    timer.value += .6
    ledger.reconfigure({"queue_seconds": .5, "controller_requests_per_minute": 100})
    with pytest.raises(Deferred, match="recovery"):
        with ledger.admit(classify(argv), argv):
            pytest.fail("a faster preset must not erase scheduler failure recovery")


def test_runtime_updates_shared_file_policy_and_keeps_research_reader(tmp_path):
    from raven.log_catalog import LogCatalog
    from raven.logs import LogSession
    from raven.telemetry_runtime import apply
    ledger = Ledger(SharedQueryCache(tmp_path / "cache"), {"user": "researcher"}, {})
    backend = SimpleNamespace(ledger=ledger)
    files = wrap_files(LocalFiles(), backend)
    sampler = Sampler(SimpleNamespace(b=backend), Store(persist=False), {}, [], collection={}, files=files)
    hub = ResearchHub({}, files)
    logs = LogSession(files=files)
    logs.catalog = LogCatalog(files)
    app = SimpleNamespace(sampler=sampler, research=hub, logs=logs, files=files)
    entered, release = Event(), Event()
    def read(context):
        entered.set()
        assert release.wait(3.)
        return {"status": "ok", "evidence": [1, 2, 3]}
    hub._read = read
    context = {"view": "experiment", "jid": "7", "job": None, "snap": {},
               "settings": dict(hub.settings), "generation": hub.generation}
    try:
        hub.request(context)
        assert entered.wait(1.)
        future, generation = hub.future, hub.generation
        apply(app, {"queue_seconds": 1., "trace_seconds": .5, "log_follow_seconds": 1.},
              {"jobs": 1., "trace": .5}, .5)
        assert hub.future is future and hub.generation == generation
        assert hub.refresh_interval() == .5
        assert files.min_refresh == logs.catalog.ttl == 1.
        assert sampler.effective_interval("trace") == .5
        assert ledger.config["queue_seconds"] == sampler.effective_interval("jobs") == 1.
        release.set()
        future.result(timeout=3.)
        assert hub.current(context)["evidence"] == [1, 2, 3]
        assert len(hub.cache) == 1
    finally:
        release.set()
        sampler.shutdown()
        hub.close()


def test_trace_interval_is_independent_but_old_collection_files_remain_compatible():
    assert settings({"log_follow_seconds": 13.})["trace_seconds"] == 13.
    assert settings({"log_follow_seconds": 13., "trace_seconds": 2.})["trace_seconds"] == 2.


def test_remote_research_retains_transport_floor_with_fast_local_preset():
    hub = ResearchHub({}, SimpleNamespace(remote=True))
    try:
        hub.set_refresh_interval(.5)
        assert hub.refresh_interval() == 1.5
    finally:
        hub.close()


def test_per_metric_slider_cannot_bypass_resolved_managed_research_interval():
    hub = ResearchHub({})
    try:
        hub.set_refresh_interval(10., managed=True)
        assert hub._refresh_interval(100) == 10.
        hub.set_polling_multiplier(50)
        assert hub._refresh_interval(100) == 10.
        hub.set_refresh_interval(.5, managed=True)
        assert hub._refresh_interval(100) == .5
    finally:
        hub.close()


def test_managed_research_applies_physical_floor_without_rejecting_legacy_intervals():
    hub = ResearchHub({})
    try:
        hub.set_refresh_interval(.1, managed=True)
        assert hub.refresh_interval() == .5
        hub.set_refresh_interval(31536000., managed=True)
        assert hub.refresh_interval() == 31536000.
    finally:
        hub.close()


def test_bound_retained_cadence_caps_legacy_interval_but_never_bypasses_site_floor(monkeypatch):
    sampler, timer = make_sampler(monkeypatch)
    try:
        sampler.set_collection_settings({"trace_seconds": 600.}, {"trace": 600.})
        assert sampler.effective_interval("trace") == 600.
        sampler.bind_telemetry(retained_binding())
        assert sampler.effective_interval("trace") == 2.
        sampler.set_collection_settings({"trace_seconds": 600.}, {"trace": 600.}, retained_trace_floor=9.)
        assert sampler.effective_interval("trace") == 9.
        sampler.bind_telemetry(None)
        assert sampler.effective_interval("trace") == 600.
    finally:
        sampler.shutdown()


def retained_binding(jid="7"):
    return {"job_id": jid, "run_id": "run-" + jid, "run_root": "/recorded/" + jid,
            "telemetry_index": "/recorded/" + jid + "/telemetry/index.json"}


@pytest.mark.parametrize("tab", ["jobs", "history", "research", "analytics"])
def test_retained_cpu_only_selection_is_immediate_without_scheduler_snapshot(monkeypatch, tab):
    sampler, timer = make_sampler(monkeypatch)
    reads = []
    value = {"index": {"sources": []}, "cpu-memory": [{"t": 1.}], "gpu": [], "errors": {}}
    monkeypatch.setattr("raven.projects.read_bound_telemetry", lambda binding, **kwargs: reads.append(binding["job_id"]) or value)
    try:
        sampler.set_collection_settings({"trace_seconds": 1., "gpu": "off", "jitter_fraction": 0.},
                                        {"jobs": 60., "trace": 1.})
        sampler._collection_not_before["trace"] = timer.value + 300.
        sampler.bind_telemetry(retained_binding())
        sampler.set_collection_demand(tab, "7")
        assert sampler.health("jobs").calls == 0
        assert sampler.due("trace", timer.value)
        sampler.run_source("trace")
        assert reads == ["7"]
        assert sampler.trace_status["7"]["state"] == "ready"
        assert sampler.trace_status["7"]["family_rows"] == {"cpu-memory": 1, "gpu": 0}
        assert sampler.retained_telemetry["7"] is value
        # Reading an unchanged selection respects its file cadence even if the
        # controller is unavailable or the user stops interacting with the UI.
        sampler._collection_activity -= 600.
        assert sampler.effective_interval("trace") == 1.
        assert not sampler.due("trace", timer.value + .5)
        assert sampler.due("trace", timer.value + 1.)
        sampler.bind_telemetry(retained_binding("8"))
        sampler.set_collection_demand(tab, "8")
        assert sampler.due("trace", timer.value + .5)
    finally:
        sampler.shutdown()


def test_retained_read_runs_while_scheduler_read_is_still_blocked(monkeypatch):
    sampler, timer = make_sampler(monkeypatch)
    entered, release, recorded = Event(), Event(), Event()
    def blocked_queue():
        entered.set()
        assert release.wait(3.)
    sampler.sources["jobs"] = blocked_queue
    sampler.sources["trace"] = recorded.set
    for name in sampler.sources:
        sampler.health(name).enabled = name in ("jobs", "trace")
    try:
        sampler.prime_collection_snapshot()
        queue = sampler.round()[0]
        assert entered.wait(1.)
        sampler.bind_telemetry(retained_binding())
        sampler.set_collection_demand("history", "7")
        reads = sampler.round()
        assert recorded.wait(1.)
        assert not queue.done()
        assert len(reads) == 1
        reads[0].result(timeout=1.)
    finally:
        release.set()
        sampler.shutdown()


def test_retained_retry_later_keeps_rows_and_does_not_count_as_a_failure(monkeypatch):
    sampler, timer = make_sampler(monkeypatch)
    value = {"index": {"sources": []}, "cpu-memory": [{"t": 1.}], "gpu": [], "errors": {}}
    def read(binding, **kwargs):
        if sampler.retained_telemetry:
            raise Deferred("Reader busy; retry later", retry_after=1.)
        return value
    monkeypatch.setattr("raven.projects.read_bound_telemetry", read)
    try:
        sampler.set_collection_settings({"trace_seconds": 1., "jitter_fraction": 0.},
                                        {"jobs": 60., "trace": 1.})
        sampler.bind_telemetry(retained_binding())
        sampler.set_collection_demand("jobs", "7")
        sampler.run_source("trace")
        timer.value += 1.
        sampler.run_source("trace")
        assert sampler.retained_telemetry["7"] is value
        assert sampler.trace_status["7"]["state"] == "deferred"
        assert "retry later" in sampler.trace_status["7"]["reason"]
        assert sampler.health("trace").backoff == sampler.health("trace").errors == 0
        assert sampler._collection_failures["trace"] == 0
        assert sampler._collection_not_before["trace"] == timer.value + 1.
        assert sampler.due("trace", timer.value + 1.)
    finally:
        sampler.shutdown()


@pytest.mark.parametrize("error", [CommandError("old file failed"), Deferred("old reader busy", retry_after=17.)])
def test_new_retained_binding_does_not_inherit_previous_file_recovery(monkeypatch, error):
    sampler, timer = make_sampler(monkeypatch)
    def fail():
        raise error
    sampler.sources["trace"] = fail
    try:
        sampler.bind_telemetry(retained_binding())
        sampler.set_collection_demand("history", "7")
        sampler.run_source("trace")
        assert not sampler.due("trace", timer.value)
        sampler.bind_telemetry(retained_binding())
        assert not sampler.due("trace", timer.value)  # same identity retains recovery
        sampler.bind_telemetry(retained_binding("8"))
        sampler.set_collection_demand("history", "8")
        assert sampler.due("trace", timer.value)
        assert sampler.health("trace").backoff == 0.
        assert "trace" not in sampler._collection_deferred
        assert "trace" not in sampler._collection_failures
    finally:
        sampler.shutdown()


def test_old_inflight_file_failure_cannot_delay_new_retained_binding(monkeypatch):
    sampler, timer = make_sampler(monkeypatch)
    entered, release = Event(), Event()
    def fail():
        entered.set()
        assert release.wait(3.)
        raise CommandError("old run failed")
    sampler.sources["trace"] = fail
    for name in sampler.sources:
        sampler.health(name).enabled = name == "trace"
    try:
        sampler.bind_telemetry(retained_binding())
        sampler.set_collection_demand("history", "7")
        work = sampler.round()[0]
        assert entered.wait(1.)
        sampler.bind_telemetry(retained_binding("8"))
        sampler.set_collection_demand("history", "8")
        assert sampler.round() == []
        release.set()
        work.result(timeout=3.)
        assert sampler.health("trace").backoff == sampler.health("trace").errors == 0
        assert sampler.due("trace", timer.value)
    finally:
        release.set()
        sampler.shutdown()


def test_retained_sampling_does_not_add_jitter_above_its_two_second_cadence(monkeypatch):
    sampler, timer = make_sampler(monkeypatch)
    sampler.sources["trace"] = lambda: None
    try:
        sampler.set_collection_settings({"trace_seconds": 600., "jitter_fraction": 1.}, {"trace": 600.})
        sampler.bind_telemetry(retained_binding())
        sampler.run_source("trace")
        assert sampler._collection_not_before["trace"] == timer.value + 2.
    finally:
        sampler.shutdown()
