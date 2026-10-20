"""Reports stay responsive, preserve request identity, and publish atomically."""
import copy
from pathlib import Path
import threading

import pytest

from raven import report
from raven.actions import Actions
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs
from raven.model import Job, Store
from raven.remote import LocalFiles
from raven.research import ResearchHub
from raven.views import Views

@pytest.fixture(autouse=True)
def durable_export_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_EXPORT_DIR", str(tmp_path / "state" / "exports"))



@pytest.fixture
def dashboard(tmp_path):
    cfg = Config({"log_lines": 0, "clipboard": {"tools": False, "osc52": False}})
    store = Store(str(tmp_path / "state"), persist=True)
    store.apply_jobs([Job("1", "captured-job", "main", "RUNNING")])
    app = App(store, None, None, cfg, "reporter")
    app.research = ResearchHub(cfg)
    app.files = app.logs.files = app.research.files
    views = Views(Glyphs(True), cfg, files=app.files)
    app.views_ref = views
    views.compose(store.snapshot(), app, 100, 24)
    try:
        yield app, views, store
    finally:
        app.research.close()


def test_prepare_performs_no_file_or_scheduler_read(dashboard, monkeypatch):
    app, views, store = dashboard
    def forbidden(*args, **kwargs):
        pytest.fail("export preparation touched files or scheduler")
    monkeypatch.setattr(LocalFiles, "stat", forbidden)
    monkeypatch.setattr(LocalFiles, "read", forbidden)
    monkeypatch.setattr(LocalFiles, "tail", forbidden)
    monkeypatch.setattr(store, "series_of", forbidden)
    monkeypatch.setattr(store, "series_jobs", forbidden)
    monkeypatch.setattr(ResearchHub, "request", forbidden)
    prepared = report.prepare_export(store.snapshot(), app, views)
    assert prepared.snap["jobs"][0].id == "1"
    assert prepared.app.research.pool is None


def test_export_inside_single_research_worker_does_not_submit_nested_reads(dashboard, monkeypatch, tmp_path):
    app, views, store = dashboard
    entered, release = threading.Event(), threading.Event()
    threads = []
    original = ResearchHub._read
    def read(hub, context):
        threads.append(threading.get_ident())
        if not entered.is_set():
            entered.set()
            assert release.wait(5), "test report worker was never released"
        return original(hub, context)
    monkeypatch.setattr(ResearchHub, "_read", read)
    prepared = report.prepare_export(store.snapshot(), app, views)
    delivered = []
    try:
        assert app.research.start_task(lambda: prepared.write(str(tmp_path / "exports-state"), "captured.txt"), delivered.append)
        assert entered.wait(5)
        # Keyboard handling remains immediate while a real reader is blocked.
        app.handle("3")
        assert app.tab == "history"
        assert not delivered
        release.set()
        app.research.future.result(timeout=5)
        assert not delivered
        app.tick()
        assert len(delivered) == 1
        assert "End of report" in Path(delivered[0]).read_text()
        assert threads and set(threads) != {threading.get_ident()}
    finally:
        release.set()


def test_prepared_report_keeps_job_log_settings_passport_and_memory_series(dashboard, tmp_path):
    app, views, store = dashboard
    path = tmp_path / "captured.log"
    path.write_text("captured actual output\n")
    store.details["1"] = {"StdOut": str(path)}
    app.log_job, app.log_record = "1", store.job("1")
    app.logs.entry = {"path": str(path), "label": "captured stream"}
    store.record("1", {"t": 1000, "k": "live", "cpu": .5, "rss": 1024})
    app.research.configure(workdir="/captured/workdir")
    app.research.passport = {"id": "captured-passport", "parameters": {"seed": 7}}
    prepared = report.prepare_export(store.snapshot(), app, views)
    store.job("1").name = "changed-live-job"
    store.details["1"]["StdOut"] = "/changed/live.log"
    store.record("1", {"t": prepared.at + 1, "k": "live", "cpu": .9, "rss": 2048})
    app.log_job = "different"
    app.logs.entry["path"] = "/different/file.log"
    app.research.configure(workdir="/changed/workdir")
    app.research.passport["parameters"]["seed"] = 99
    text = prepared.render()
    assert "captured-job" in text and "changed-live-job" not in text
    assert "captured actual output" in text and "/changed/live.log" not in text
    assert prepared.app.research.settings["workdir"] == "/captured/workdir"
    assert prepared.app.research.passport["parameters"]["seed"] == 7
    assert [sample["cpu"] for sample in prepared.app.store.series_of("1")] == [.5]
    assert store.job("1").name == "changed-live-job"
    assert app.research.passport["parameters"]["seed"] == 99


def test_report_actions_marks_are_private_and_cannot_submit(dashboard):
    app, views, store = dashboard
    class NoSubmit:
        def submit(self, *args, **kwargs):
            pytest.fail("report must not submit jobs")
    actions = Actions(NoSubmit(), store)
    actions.marks["1"] = ("holding", 0)
    before = copy.deepcopy(actions.marks)
    prepared = report.prepare_export(store.snapshot(), app, views, actions)
    prepared.render()
    assert actions.marks == before
    assert prepared.actions.store is prepared.app.store


def test_cancellation_before_render_creates_no_export(dashboard, tmp_path):
    app, views, store = dashboard
    prepared = report.prepare_export(store.snapshot(), app, views)
    target = tmp_path / "cancelled-state"
    with pytest.raises(report.ReportCancelled, match="cancelled"):
        prepared.write(str(target), "complete.txt", cancel=lambda: True)
    assert not target.exists()


def test_cancellation_during_page_render_preserves_existing_export(dashboard, tmp_path, monkeypatch):
    app, views, store = dashboard
    prepared = report.prepare_export(store.snapshot(), app, views)
    target = tmp_path / "output" / "exports"
    target.mkdir(parents=True)
    (target / "complete.txt").write_text("previous complete report")
    monkeypatch.setenv("RAVEN_EXPORT_DIR", str(target))
    cancelled = threading.Event()
    original = prepared.views.compose
    def render(*args, **kwargs):
        result = original(*args, **kwargs)
        cancelled.set()
        return result
    monkeypatch.setattr(prepared.views, "compose", render)
    with pytest.raises(report.ReportCancelled):
        prepared.write(str(target.parent), "complete.txt", cancel=cancelled.is_set)
    assert (target / "complete.txt").read_text() == "previous complete report"
    assert [path.name for path in target.iterdir()] == ["complete.txt"]


def test_cancellation_during_write_removes_temporary_file(dashboard, tmp_path, monkeypatch):
    app, views, store = dashboard
    prepared = report.prepare_export(store.snapshot(), app, views)
    monkeypatch.setattr(prepared, "render", lambda **kwargs: "captured report\n" * 10000)
    target = tmp_path / "cancel-writing"
    monkeypatch.setenv("RAVEN_EXPORT_DIR", str(target / "exports"))
    checks = []
    def cancel():
        checks.append(True)
        return len(checks) >= 3
    with pytest.raises(report.ReportCancelled):
        prepared.write(str(target), "complete.txt", cancel=cancel)
    assert not list((target / "exports").iterdir())


def test_published_report_is_complete_ascii_and_private(dashboard, tmp_path):
    app, views, store = dashboard
    prepared = report.prepare_export(store.snapshot(), app, views)
    path = Path(prepared.write(str(tmp_path / "published"), "complete.txt"))
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text().isascii()
    assert path.read_text().endswith("End of report | Raven | Plain ASCII, no external resources.\n")
    assert not list(path.parent.glob(".report-*.tmp"))


@pytest.mark.parametrize("name", ["../outside.txt", "/outside.txt", "", ".", ".."])
def test_report_name_cannot_escape_export_directory(dashboard, tmp_path, name):
    app, views, store = dashboard
    prepared = report.prepare_export(store.snapshot(), app, views)
    with pytest.raises(ValueError, match="plain file name"):
        prepared.write(str(tmp_path / "protected"), name)


def test_app_export_is_responsive_and_publishes_captured_inputs_to_activity(dashboard, monkeypatch):
    app, _, store = dashboard
    entered, release = threading.Event(), threading.Event()
    original = ResearchHub._read
    def read(hub, context):
        if not entered.is_set():
            entered.set()
            assert release.wait(5), "test reader release timed out"
        return original(hub, context)
    monkeypatch.setattr(ResearchHub, "_read", read)
    try:
        app.export("report")
        assert entered.wait(5)
        assert app.activity.snapshot()[1]["status"] == "running"
        app.handle("3")
        assert app.tab == "history"
        store.job("1").name = "changed-after-export-request"
        app.research.configure(workdir="/changed-after-export-request")
        assert not list((Path(app.state_dir) / "exports").glob("report-*.txt"))
        release.set()
        app.research.future.result(timeout=5)
        # Completion state is applied exclusively through App.tick.
        assert app.activity.snapshot()[1]["status"] == "running"
        app.tick()
        notices, task = app.activity.snapshot()
        assert task["status"] == "ready"
        path = Path(next(item["path"] for item in reversed(notices) if item["path"]))
        text = path.read_text()
        assert "captured-job" in text and "changed-after-export-request" not in text
        assert any(event["kind"] == "export" for event in store.events)
        assert app.tab == "history"
    finally:
        release.set()


def test_app_export_rejects_busy_worker_without_reading_or_replacing_activity(dashboard, monkeypatch):
    app, _, _ = dashboard
    entered, release = threading.Event(), threading.Event()
    def busy():
        entered.set()
        assert release.wait(5)
    def forbidden(*args, **kwargs):
        pytest.fail("busy report request prepared work despite rejection")
    monkeypatch.setattr(report, "prepare_export", forbidden)
    try:
        assert app.research.start_task(busy, lambda value: None)
        assert entered.wait(5)
        app.export("report")
        assert not app.command_ok
        assert "background command" in app.message
        assert app.activity.snapshot()[1] is None
    finally:
        release.set()
        app.research.future.result(timeout=5)
        app.tick()


def test_app_activity_cancels_report_without_publishing_incomplete_output(dashboard, monkeypatch):
    app, _, store = dashboard
    entered, release = threading.Event(), threading.Event()
    original = ResearchHub._read
    def read(hub, context):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(hub, context)
    monkeypatch.setattr(ResearchHub, "_read", read)
    try:
        app.export("report")
        assert entered.wait(5)
        app.run_command("activity")
        app.handle("c")
        assert app.activity.snapshot()[1]["cancel"].is_set()
        release.set()
        with pytest.raises(report.ReportCancelled):
            app.research.future.result(timeout=5)
        app.tick()
        assert app.activity.snapshot()[1]["status"] == "cancelled"
        assert "cancelled" in app.message
        assert not any(event["kind"] == "export" for event in store.events)
        export_dir = Path(app.state_dir) / "exports"
        assert not export_dir.exists() or not list(export_dir.iterdir())
    finally:
        release.set()


def test_app_background_report_errors_are_retained_without_a_success_event(dashboard, monkeypatch):
    app, _, store = dashboard
    def broken(*args, **kwargs):
        raise OSError("report destination unavailable")
    monkeypatch.setattr(report.PreparedReport, "write", broken)
    app.export("report")
    with pytest.raises(OSError, match="destination unavailable"):
        app.research.future.result(timeout=5)
    app.tick()
    notices, task = app.activity.snapshot()
    assert task["status"] == "error"
    assert notices[-1]["level"] == "error"
    assert "destination unavailable" in notices[-1]["text"]
    assert not any(event["kind"] == "export" for event in store.events)


def test_noninteractive_report_export_retains_synchronous_cli_behavior(dashboard, monkeypatch):
    app, _, store = dashboard
    app.interactive = False
    def forbidden(*args, **kwargs):
        pytest.fail("noninteractive export unexpectedly dispatched a background task")
    monkeypatch.setattr(report, "prepare_export", forbidden)
    app.export("report")
    assert app.command_ok
    assert app.research.pending is None
    assert any(event["kind"] == "export" for event in store.events)
    assert len(list((Path(app.state_dir) / "exports").glob("report-*.txt"))) == 1
