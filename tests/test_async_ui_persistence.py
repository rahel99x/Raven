"""Settings publication never makes interactive input wait for a slow disk."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from raven import ui_persistence as P
from raven.model import Store
from raven.worker_scheduler import WorkerScheduler


def test_coalesces_latest_settings_and_flushes_after_live_mode_changes(tmp_path, monkeypatch):
    store, scheduler = Store(str(tmp_path)), WorkerScheduler(mode="single")
    app = SimpleNamespace(store=store, interactive=True)
    writer = P.bind(app, scheduler)
    entered, release = Event(), Event()
    seen, threads = [], []
    original = store.save_ui
    def slow(value):
        threads.append(get_ident())
        seen.append(value)
        if len(seen) == 1:
            entered.set()
            assert release.wait(3)
        return original(value)
    monkeypatch.setattr(store, "save_ui", slow)
    try:
        P.save(app, {"value": 0})
        assert entered.wait(1)
        for index in range(1, 100):
            value = {"value": index, "nested": [index]}
            P.save(app, value)
            value["nested"].append("changed after publication")
        scheduler.request_mode("multi")
        scheduler.request_mode("single")
        assert writer.latest[1] == {"value": 99, "nested": [99]}
        assert len(seen) == 1
        release.set()
        P.close(app)
        assert seen == [{"value": 0}, {"value": 99, "nested": [99]}]
        assert store.load_ui() == {"value": 99, "nested": [99]}
        assert get_ident() not in threads
        assert writer.completed == writer.sequence == 100
    finally:
        release.set()
        P.close(app)
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_queue_admission_retry_and_shutdown_do_not_lose_latest_snapshot(tmp_path):
    store, scheduler = Store(str(tmp_path)), WorkerScheduler(mode="single", max_queue=1)
    app = SimpleNamespace(store=store, interactive=True)
    P.bind(app, scheduler)
    lane = scheduler.lane("source")
    entered, release = Event(), Event()
    def block():
        entered.set()
        assert release.wait(3)
    try:
        first = lane.submit(block)
        assert entered.wait(1)
        queued = lane.submit(lambda: None)
        P.save(app, {"counter": 1})
        P.save(app, {"counter": 2})
        assert app.ui_writer.future is None
        assert store.ui_state_status["state"] == "pending"
        # This path has never been admitted; teardown must still save it.
        P.close(app)
        assert store.load_ui() == {"counter": 2}
        assert not app.ui_writer.publish({"counter": 3})
        release.set()
        first.result(timeout=2)
        queued.result(timeout=2)
    finally:
        release.set()
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_retry_admits_deferred_settings_without_json_on_ui(tmp_path, monkeypatch):
    store, scheduler = Store(str(tmp_path)), WorkerScheduler(mode="single", max_queue=1)
    app = SimpleNamespace(store=store, interactive=True)
    writer = P.bind(app, scheduler)
    submit = writer.lane.submit
    def full(*args, **kwargs):
        raise RuntimeError("full")
    monkeypatch.setattr(writer.lane, "submit", full)
    P.save(app, {"theme": "darcula"})
    assert writer.latest and not writer.scheduled
    monkeypatch.setattr(writer.lane, "submit", submit)
    try:
        P.tick(app)
        P.close(app)
        assert store.load_ui() == {"theme": "darcula"}
    finally:
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_failed_write_does_not_poison_next_preference_or_hide_error(tmp_path, monkeypatch):
    store, scheduler = Store(str(tmp_path)), WorkerScheduler(mode="single")
    app = SimpleNamespace(store=store, interactive=True)
    writer = P.bind(app, scheduler)
    original = store.save_ui
    def failure(data):
        raise OSError("disk disappeared")
    monkeypatch.setattr(store, "save_ui", failure)
    try:
        P.save(app, {"tab": "jobs"})
        writer.future.result(timeout=1)
        assert store.ui_state_status["state"] == "error"
        monkeypatch.setattr(store, "save_ui", original)
        P.save(app, {"tab": "history"})
        P.close(app)
        assert store.load_ui() == {"tab": "history"}
    finally:
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_explicit_roundtrip_barrier_does_not_close_publication_lane(tmp_path):
    store, scheduler = Store(str(tmp_path)), WorkerScheduler(mode="single")
    app = SimpleNamespace(store=store, interactive=True)
    P.bind(app, scheduler)
    try:
        P.save(app, {"sequence": 1})
        P.flush(app)
        assert store.load_ui() == {"sequence": 1}
        P.save(app, {"sequence": 2})
        P.flush(app)
        assert store.load_ui() == {"sequence": 2}
        assert not app.ui_writer.closed
    finally:
        P.close(app)
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_cancelled_queued_writer_is_reconciled_by_explicit_flush(tmp_path):
    store, scheduler = Store(str(tmp_path)), WorkerScheduler(mode="single")
    app = SimpleNamespace(store=store, interactive=True)
    writer = P.bind(app, scheduler)
    entered, release = Event(), Event()
    blocker = scheduler.lane("source")
    def block():
        entered.set()
        assert release.wait(3)
    try:
        task = blocker.submit(block)
        assert entered.wait(1)
        P.save(app, {"sequence": 1})
        assert writer.future.cancel()
        P.save(app, {"sequence": 2})
        P.flush(app)
        assert store.load_ui() == {"sequence": 2}
        release.set()
        task.result(timeout=2)
        P.save(app, {"sequence": 3})
        P.flush(app)
        assert store.load_ui() == {"sequence": 3}
    finally:
        release.set()
        P.close(app)
        blocker.shutdown(wait=True)
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_concurrent_barriers_do_not_overlap_disk_publications(tmp_path, monkeypatch):
    store, scheduler = Store(str(tmp_path)), WorkerScheduler(mode="single")
    app = SimpleNamespace(store=store, interactive=True)
    writer = P.bind(app, scheduler)
    entered, release = Event(), Event()
    original = store.save_ui
    seen = []
    def disk(value):
        seen.append(value["sequence"])
        if value["sequence"] == 1:
            entered.set()
            assert release.wait(3)
        return original(value)
    monkeypatch.setattr(store, "save_ui", disk)
    try:
        P.save(app, {"sequence": 1})
        assert entered.wait(1)
        P.save(app, {"sequence": 2})
        with ThreadPoolExecutor(max_workers=2) as pool:
            barriers = [pool.submit(P.flush, app) for _ in range(2)]
            assert seen == [1]
            release.set()
            for barrier in barriers:
                barrier.result(timeout=2)
        assert seen == [1, 2]
        assert store.load_ui() == {"sequence": 2}
        assert writer.completed == writer.sequence == 2
    finally:
        release.set()
        P.close(app)
        scheduler.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("interactive,persist", [(False, True), (True, False), (False, False)])
def test_headless_and_no_state_preserve_existing_contract(tmp_path, interactive, persist):
    app = SimpleNamespace(store=Store(str(tmp_path), persist=persist), interactive=interactive)
    assert P.bind(app, None) is None
    P.save(app, {"tab": "jobs"})
    assert app.store.load_ui() == ({"tab": "jobs"} if persist else {})
