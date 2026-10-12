from concurrent.futures import Future
from types import SimpleNamespace
import threading
import time

from tower import raven_diagnostics as D
from tower.worker_scheduler import WorkerScheduler


def test_snapshot_never_observes_proc_or_starts_a_task(monkeypatch):
    monkeypatch.setattr(D, "collect", lambda app: (_ for _ in ()).throw(AssertionError("render started I/O")))
    app = SimpleNamespace(raven_diagnostic_state={"value": {"observed_at": 1}})
    assert D.snapshot(app) == {"observed_at": 1}


def test_slow_observation_does_not_block_ui_tick_or_duplicate(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def collect(app):
        calls.append(1)
        entered.set()
        release.wait(2)
        return {"observed_at": 123}
    monkeypatch.setattr(D, "collect", collect)
    governor = WorkerScheduler(workers=1)
    app = SimpleNamespace(cfg={"raven": {"managed": True}}, worker_scheduler=governor)
    D.bind(app)
    try:
        D.tick(app)
        assert entered.wait(1)
        begin = time.monotonic()
        for _ in range(100):
            assert D.tick(app) is False
        assert time.monotonic() - begin < .25
        assert calls == [1]
        assert D.snapshot(app) == {}
        release.set()
        app.raven_diagnostic_state["future"].result(timeout=2)
        assert D.tick(app) is True
        assert D.snapshot(app)["observed_at"] == 123
    finally:
        release.set()
        D.close(app)
        governor.shutdown(wait=True, cancel_futures=True)


def test_local_process_observation_has_no_environment_or_command_payloads():
    value = D.process_tree()
    assert value["processes"]
    assert value["rss_sum_bytes"] > 0
    for process in value["processes"]:
        assert set(process) == {"pid", "start_ticks", "rss_bytes", "pss_bytes", "cpu_seconds", "threads", "descriptors"}


def test_no_managed_policy_means_no_diagnostic_worker():
    app = SimpleNamespace(cfg={}, worker_scheduler=SimpleNamespace(lane=lambda _: 1/0))
    D.bind(app)
    assert not hasattr(app, "raven_diagnostic_state")
