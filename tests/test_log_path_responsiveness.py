"""Legacy filename resolution cannot stall interactive scrolling on CARC."""
from threading import current_thread
from types import SimpleNamespace

import pytest

from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text
from raven.model import Finished, Job, Store
from raven.remote import LocalFiles
from raven.research import ResearchHub
from raven.views import Views, stdout_path


class WorkerFiles(LocalFiles):
    def stat(self, path):
        assert current_thread().name.startswith("raven-research")
        return super().stat(path)

    def snapshot_stat(self, path):
        assert current_thread().name.startswith("raven-research")
        return super().snapshot_stat(path)

    def read(self, path, offset, length):
        assert current_thread().name.startswith("raven-research")
        return super().read(path, offset, length)


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    guessed = tmp_path / "logs" / "train-123.out"
    custom = tmp_path / "custom.log"
    guessed.write_bytes(b"".join(f"AB source {i:03}\tvalue\r\n".encode() for i in range(60)))
    custom.write_bytes(b"".join(f"CD custom {i:03}\tvalue\r\n".encode() for i in range(60)))
    store = Store(persist=False)
    store.apply_jobs([Job("123", "train", "gpu", "RUNNING")])
    cfg, files = Config({"log_lines": 3}), WorkerFiles()
    app = App(store, None, None, cfg, "test")
    app.files = app.logs.files = files
    app.research = ResearchHub(cfg, files)
    views = Views(Glyphs(False), cfg, files=files)
    app.views_ref = views

    def reject_ui_probe(path):
        assert current_thread().name.startswith("raven-research"), (
            f"Legacy path existence check reached the terminal thread: {path}")
        return original_exists(path)

    import os
    original_exists = os.path.exists
    monkeypatch.setattr("raven.views.os.path.exists", reject_ui_probe)
    yield app, views, guessed, custom
    if app.logs.catalog:
        app.logs.catalog.close()
    app.research.close()


def finish(app):
    app.research.future.result(timeout=3)
    app.research.poll_task()


@pytest.mark.parametrize("custom_source", [False, True])
@pytest.mark.parametrize("ascii_", [False, True])
def test_native_log_scroll_and_redraw_resolve_paths_without_filesystem_probes(dashboard, custom_source, ascii_):
    app, views, guessed, custom = dashboard
    views.set_ascii(ascii_)
    app.open_log("123")
    if custom_source:
        app.logs.entry = {"id": "custom", "path": str(custom), "label": "custom"}
    expected_path = str(custom) if custom_source else "logs/train-123.out"
    selected = custom if custom_source else guessed
    views.compose(app.store.snapshot(), app, 100, 24)
    finish(app)
    assert app.logs.path == expected_path
    assert app.logs.buffers[expected_path].raw_range(0, 59) == selected.read_bytes()
    app.handle("home")
    for _ in range(100):
        app.handle("up")
        rows, hits = views.compose(app.store.snapshot(), app, 100, 24)
    assert app.logs.cursor == 0
    content = [row_text(rows[y]) for y, kind, _ in hits if kind == "log_line"]
    assert ("CD custom 000" if custom_source else "AB source 000") in content[0]
    assert app.logs.path == expected_path


def test_selected_job_preview_defers_legacy_existence_check_to_worker(dashboard):
    app, views, guessed, custom = dashboard
    snap = app.store.snapshot()
    job = snap["jobs"][0]
    rows = views.selected_panel(snap, job, 80, 3, app)
    assert any("background log preview" in row_text(row) for row in rows)
    finish(app)
    for _ in range(30):
        rows = views.selected_panel(snap, job, 80, 3, app)
    assert any("AB source 059" in row_text(row) for row in rows)


def test_log_browser_scrolling_does_not_probe_undeclared_scheduler_streams(dashboard):
    app, views, guessed, custom = dashboard
    app.open_log("123")
    app.logs.browser = True
    views.compose(app.store.snapshot(), app, 100, 24)
    finish(app)
    for _ in range(30):
        app.handle("up")
        views.compose(app.store.snapshot(), app, 100, 24)
    assert any(entry["path"] == "logs/train-123.out" for entry in app.logs.entries)


def test_noninteractive_stdout_resolution_preserves_checked_legacy_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    job = Job("123", "train", "gpu", "RUNNING")
    assert stdout_path(job, {}) == ""
    (tmp_path / "logs").mkdir()
    path = tmp_path / "logs" / "train-123.out"
    path.write_bytes(b"AB source\n")
    assert stdout_path(job, {}) == "logs/train-123.out"
    path.unlink()
    assert stdout_path(job, {}) == ""
    assert stdout_path(job, {}, probe=False) == "logs/train-123.out"
    assert stdout_path(Finished("123", "train", "FAILED"), {}, probe=False) == ""
    assert stdout_path(job, {}, SimpleNamespace(remote=True), probe=False) == ""
