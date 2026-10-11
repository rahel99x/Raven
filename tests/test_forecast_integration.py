"""Queue calibration persistence stays scoped; observers reuse existing samples."""
from concurrent.futures import ThreadPoolExecutor
import json
import socket
import threading
import time

import pytest

from tower import cli, clock, ui_persistence
from tower.config import Config
from tower.forecast import ForecastTracker
from tower.model import Job, Store
from tower.record import RecordingBackend
from tower.remote import LocalFiles, RemoteFiles, SshBackend
from tower.sampler import Sampler
from tower.slurm import FakeBackend, Slurm


def pending(jid="1", **changes):
    values = dict(id=jid, name="simulation", partition="main", state="PENDING", submit=1000,
                  est_start=2000, cpus=4, nodes=1, gpus=0, mem_req="16G", limit="01:00:00")
    values.update(changes)
    return Job(**values)


def collect_outcome(tracker):
    tracker.observe([pending()], now=1500)
    tracker.observe([pending(state="RUNNING", start=2050)], now=2100)
    assert len(tracker.observations()) == 1


@pytest.fixture
def dashboard_builder(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "state_dir", lambda: str(tmp_path / "state"))
    monkeypatch.setattr(socket, "gethostname", lambda: "login.carc.example")
    monkeypatch.setattr(cli.os, "getuid", lambda: 1234)
    monkeypatch.setattr(clock, "now", lambda: 100000.)
    sessions = []

    def build(*, host="", user="alice", profile="", ssh_user="", ssh_opts=(), flags=(), recorded=False):
        def forbidden(*args, **kwargs):
            pytest.fail("constructing or restoring a dashboard queried a backend")
        cfg = Config({"profiles": {profile: {}}}) if profile else Config()
        if profile:
            cfg = cfg.profile(profile)
        if host:
            actual = SshBackend(host, user=ssh_user, opts=ssh_opts, runner=forbidden)
            files = RemoteFiles(actual)
        else:
            actual, files = FakeBackend("alice"), LocalFiles()
        backend = RecordingBackend(actual, str(tmp_path / "recording.jsonl")) if recorded else actual
        monkeypatch.setattr(cli, "make_backend", lambda args, cfg, selected: (backend, files, None, selected))
        session = cli.build(cli.parse(["--no-plugins", "--user", user, *flags]), cfg)
        sessions.append(session)
        return session

    yield build
    for session in sessions:
        session.close()


def ui(session):
    ui_persistence.flush(session.app)
    return session.store.load_ui()


def save_ui(session, value):
    ui_persistence.flush(session.app)
    session.store.save_ui(value)


def publish_ui(session):
    session.app.save()
    ui_persistence.flush(session.app)


def test_scoped_outcomes_roundtrip_without_backend_queries(dashboard_builder):
    initial = dashboard_builder()
    collect_outcome(initial.app.research.forecasts)
    publish_ui(initial)
    stored = ui(initial)
    assert "forecast_observations" not in stored
    state = stored["forecast_state"]
    assert state["version"] == 1
    assert state["scope"] == initial.app.forecast_scope
    assert state["scope"]["host"] == "login.carc.example"
    assert state["scope"]["uid"] == 1234
    restored = dashboard_builder()
    assert restored.app.research.forecasts.observations() == initial.app.research.forecasts.observations()
    assert restored.backend.calls == initial.backend.calls == []
    assert not restored.app.research.forecast_restore_warning


@pytest.mark.parametrize("changed", ["host", "profile", "uid", "user", "ssh_user", "ssh_options_sha256", "backend"])
def test_other_saved_connection_scope_never_restores(dashboard_builder, changed):
    initial = dashboard_builder()
    collect_outcome(initial.app.research.forecasts)
    publish_ui(initial)
    stored = ui(initial)
    stored["forecast_state"]["scope"][changed] = "different" if changed != "uid" else 4321
    save_ui(initial, stored)
    restored = dashboard_builder()
    assert restored.app.research.forecasts.observations() == []
    assert "another connection" in restored.app.research.forecast_restore_warning


def test_boolean_uid_is_not_integer_scope_match(dashboard_builder, monkeypatch):
    monkeypatch.setattr(cli.os, "getuid", lambda: 0)
    initial = dashboard_builder()
    collect_outcome(initial.app.research.forecasts)
    publish_ui(initial)
    stored = ui(initial)
    stored["forecast_state"]["scope"]["uid"] = False
    save_ui(initial, stored)
    assert dashboard_builder().app.research.forecasts.observations() == []


def test_local_hostname_and_monitoring_user_change_scope(dashboard_builder, monkeypatch):
    initial = dashboard_builder(user="alice")
    collect_outcome(initial.app.research.forecasts)
    publish_ui(initial)
    restored = dashboard_builder(user="bob")
    assert restored.app.research.forecasts.observations() == []
    assert restored.app.forecast_scope["user"] == "bob"
    monkeypatch.setattr(socket, "gethostname", lambda: "other.carc.example")
    assert dashboard_builder(user="alice").app.research.forecasts.observations() == []


def test_profiles_with_same_connection_do_not_share_calibration(dashboard_builder):
    initial = dashboard_builder(profile="discovery")
    collect_outcome(initial.app.research.forecasts)
    publish_ui(initial)
    restored = dashboard_builder(profile="endeavour")
    assert restored.app.research.forecasts.observations() == []
    assert restored.app.forecast_scope["profile"] == "endeavour"


def test_remote_host_login_and_options_define_separate_scopes(dashboard_builder):
    initial = dashboard_builder(host="login.example", ssh_user="alice", ssh_opts=["-p", "2222"])
    collect_outcome(initial.app.research.forecasts)
    publish_ui(initial)
    stored = ui(initial)["forecast_state"]
    assert stored["scope"]["backend"] == "ssh"
    assert stored["scope"]["host"] == "login.example"
    assert stored["scope"]["ssh_user"] == "alice"
    assert len(stored["scope"]["ssh_options_sha256"]) == 64
    assert "2222" not in json.dumps(stored)
    matched = dashboard_builder(host="login.example", ssh_user="alice", ssh_opts=["-p", "2222"])
    assert len(matched.app.research.forecasts.observations()) == 1
    assert dashboard_builder(host="login.example", ssh_user="bob", ssh_opts=["-p", "2222"]).app.research.forecasts.observations() == []
    assert dashboard_builder(host="login.example", ssh_user="alice", ssh_opts=["-p", "2223"]).app.research.forecasts.observations() == []


def test_recording_preserves_actual_backend_scope(dashboard_builder):
    plain = dashboard_builder(host="login.example", ssh_user="alice")
    collect_outcome(plain.app.research.forecasts)
    publish_ui(plain)
    recording = dashboard_builder(host="login.example", ssh_user="alice", recorded=True)
    assert recording.app.forecast_scope == plain.app.forecast_scope
    assert recording.app.research.forecasts.observations() == plain.app.research.forecasts.observations()


def test_legacy_unscoped_forecasts_are_ignored(dashboard_builder):
    initial = dashboard_builder()
    collect_outcome(initial.app.research.forecasts)
    save_ui(initial, {"forecast_observations": initial.app.research.forecasts.observations()})
    restored = dashboard_builder()
    assert restored.app.research.forecasts.observations() == []
    assert "unscoped" in restored.app.research.forecast_restore_warning


@pytest.mark.parametrize("state", [None, [], 1, "corrupt", True, {"version": True}, {"version": 2}])
def test_malformed_unknown_envelopes_do_not_stop_startup(dashboard_builder, state):
    initial = dashboard_builder()
    save_ui(initial, {"forecast_state": state})
    restored = dashboard_builder()
    assert restored.app.research.forecasts.observations() == []
    assert restored.backend.calls == []


@pytest.mark.parametrize("records", [None, {}, "corrupt", 1, [None], [{"job_id": "incomplete"}]])
def test_corrupt_scoped_observations_ignored_gracefully(dashboard_builder, records):
    initial = dashboard_builder()
    save_ui(initial, {"forecast_state": {"version": 1, "scope": initial.app.forecast_scope, "observations": records}})
    restored = dashboard_builder()
    assert restored.app.research.forecasts.observations() == []
    assert "Malformed" in restored.app.research.forecast_restore_warning


@pytest.mark.parametrize("nonobject", [None, [], 1, "not an object", True])
def test_nonobject_ui_state_does_not_crash_before_restore(dashboard_builder, nonobject):
    initial = dashboard_builder()
    save_ui(initial, nonobject)
    assert dashboard_builder().app.research.forecasts.observations() == []


def test_saved_future_observations_cannot_leak(dashboard_builder):
    initial = dashboard_builder()
    collect_outcome(initial.app.research.forecasts)
    row = initial.app.research.forecasts.observations()[0]
    row["actual_start"] = row["last_observed_at"] = 100001
    save_ui(initial, {"forecast_state": {"version": 1, "scope": initial.app.forecast_scope, "observations": [row]}})
    restored = dashboard_builder()
    assert restored.app.research.forecasts.observations() == []
    assert "future" in restored.app.research.forecast_restore_warning


@pytest.mark.parametrize("flag", ["--no-state", "--fake"])
def test_disabled_state_never_reads_writes_evidence(dashboard_builder, flag):
    session = dashboard_builder(flags=[flag])
    assert session.app.forecast_scope is None and not session.store.persist
    collect_outcome(session.app.research.forecasts)
    session.app.save()
    assert session.store.load_ui() == {}


def sampler(backend=None):
    backend = backend or FakeBackend(t0=100000, speed=0)
    store = Store(persist=False)
    worker = Sampler(Slurm(backend, "alice"), store, {}, [], gpu_sampling=False, weather=False, budget=False)
    return worker, store, backend


def test_forecast_observer_adds_zero_scheduler_queries(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: 100000.)
    baseline, _, plain_backend = sampler()
    tracked, _, observed_backend = sampler()
    tracker = ForecastTracker()
    tracked.job_observers.append(tracker.observe)
    try:
        baseline.src_jobs()
        baseline.src_starts()
        tracked.src_jobs()
        tracked.src_starts()
        assert observed_backend.calls == plain_backend.calls
        assert sum(command[0] == "squeue" for command in observed_backend.calls) == 2
        assert tracker.observations()
    finally:
        baseline.shutdown()
        tracked.shutdown()


def test_faulty_observer_does_not_fail_source_or_skip_other_callbacks():
    worker, store, backend = sampler()
    received = []
    def faulty(jobs):
        raise ValueError("observer-specific error")
    worker.job_observers[:] = [faulty, lambda jobs: received.extend(jobs)]
    try:
        worker.run_source("jobs")
        assert store.health["jobs"].errors == 0
        assert store.health["jobs"].calls == 1
        assert received and worker.observer_errors == {0: "observer-specific error"}
        assert sum(command[0] == "squeue" for command in backend.calls) == 1
        worker.job_observers[0] = lambda jobs: None
        worker.observe_jobs()
        assert worker.observer_errors == {}
    finally:
        worker.shutdown()


def test_observers_cannot_mutate_store_or_other_observer_snapshot():
    worker, store, _ = sampler()
    store.jobs = [pending(hosts=["n1"])]
    tracker, received = ForecastTracker(), []
    def mutate(jobs):
        jobs[0]["state"] = "FAILED"
        jobs[0]["hosts"].append("injected")
        jobs.clear()
    worker.job_observers[:] = [mutate, lambda jobs: received.extend(jobs), tracker.observe]
    try:
        clock.set_source(lambda: 1500.)
        worker.observe_jobs()
        assert received[0]["state"] == "PENDING" and received[0]["hosts"] == ["n1"]
        assert store.jobs[0].hosts == ["n1"]
        assert len(tracker.observations()) == 1
    finally:
        clock.reset()
        worker.shutdown()


def test_observer_dispatch_serializes_concurrent_sources():
    worker, store, _ = sampler()
    store.jobs = [pending()]
    guard = threading.Lock()
    active = maximum = calls = 0
    def consumer(jobs):
        nonlocal active, maximum, calls
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(.002)
        with guard:
            active -= 1
            calls += 1
    worker.job_observers.append(consumer)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: worker.observe_jobs(), range(40)))
        assert maximum == 1 and calls == 40
        assert worker.observer_errors == {}
    finally:
        worker.shutdown()


def test_observer_count_and_snapshot_size_are_bounded():
    worker, store, _ = sampler()
    store.jobs = [pending(str(i)) for i in range(10001)]
    calls = []
    worker.job_observers[:] = [lambda jobs: calls.append(len(jobs)) for _ in range(9)]
    try:
        worker.observe_jobs()
        assert calls == [10000] * 8
    finally:
        worker.shutdown()


def test_observer_error_text_is_bounded():
    worker, store, _ = sampler()
    store.jobs = [pending()]
    def faulty(jobs):
        raise ValueError("x" * 100000)
    worker.job_observers.append(faulty)
    try:
        worker.observe_jobs()
        assert len(worker.observer_errors[0]) == 512
    finally:
        worker.shutdown()


def test_existing_sources_capture_revision_and_actual_start(monkeypatch):
    worker, store, backend = sampler()
    current = [pending()]
    monkeypatch.setattr(worker.slurm, "jobs", lambda: current)
    monkeypatch.setattr(worker.slurm, "starts", lambda: {"1": 2200})
    tracker = ForecastTracker()
    worker.job_observers.append(tracker.observe)
    try:
        clock.set_source(lambda: 1500.)
        worker.src_jobs()
        clock.set_source(lambda: 1600.)
        worker.src_starts()
        rows = tracker.observations()
        assert rows[0]["predicted_start"] == 2200 and rows[0]["revision_count"] == 1
        current[:] = [pending(state="RUNNING", start=2250)]
        clock.set_source(lambda: 2300.)
        worker.src_jobs()
        rows = tracker.observations()
        assert rows[0]["actual_start"] == 2250
        assert rows[0]["issued_at"] == 1600
        assert backend.calls == []
    finally:
        clock.reset()
        worker.shutdown()


def test_ignored_scope_warning_surfaces_in_forecast_result(dashboard_builder):
    initial = dashboard_builder()
    save_ui(initial, {"forecast_observations": []})
    session = dashboard_builder()
    session.store.jobs = [pending(est_start=100100)]
    app = session.app
    app.research_view = "forecast"
    result = app.research.request(app.research.context(session.store.snapshot(), app), wait=True)
    assert any("unscoped" in text for text in result["limitations"])
