"""Independent regressions for changes arriving between log/history key actions."""
from __future__ import annotations

from pathlib import Path
import threading

import pytest

from raven import clipboard, layout
from raven import views as views_module
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs
from raven.model import Finished, Store
from raven.remote import LocalFiles
from raven.research import ResearchHub
from raven.views import Views


class ReviewDashboard:
    def __init__(self, root, monkeypatch):
        monkeypatch.setenv("RAVEN_EXPORT_DIR", str(root / "state" / "exports"))
        self.path = root / "failed-77.log"
        self.path.write_bytes(b"line 0\nline 1\nline 2\nline 3\n")
        self.copies = []
        self.cfg = Config({"log_lines": 0, "clipboard": {"osc52": False, "tools": True}})
        self.store = Store(state_dir=str(root / "state"))
        self.store.finished = [Finished("77", "failed", "FAILED", end="2026-10-05T12:00:00")]
        self.store.details["77"] = {"StdOut": str(self.path), "StdErr": str(self.path)}
        # Read deterministic current snapshots here; expensive copies still use
        # the shared worker, including the blocked-copy responsiveness cases.
        self.app = App(self.store, None, None, self.cfg, "reader", ascii_=True, interactive=False)
        self.views = Views(Glyphs(True), self.cfg, files=LocalFiles())
        self.app.logs.files = self.views.files
        self.app.views_ref = self.views
        self.app.research = ResearchHub(self.cfg, self.views.files)

        def copy(text, *_args, **_kwargs):
            self.copies.append(text.encode("utf-8"))
            return "captured text"

        def copy_file(path, **_kwargs):
            data = Path(path).read_bytes()
            self.copies.append(data)
            return {"methods": ["test clipboard"], "warnings": [], "bytes": len(data), "text": True}

        monkeypatch.setattr(clipboard, "copy", copy)
        monkeypatch.setattr(clipboard, "copy_file", copy_file)
        self.app.handle("3")
        self.render()
        self.app.handle("l")
        self.render()

    def render(self):
        return self.views.compose(self.store.snapshot(), self.app, 100, 20)

    def drain(self):
        pending = self.app.research.pending
        if pending is not None:
            pending[0].result(timeout=3)
            self.app.research.poll_task()

    def replace_log(self, payload):
        replacement = self.path.with_suffix(".replacement")
        replacement.write_bytes(payload)
        replacement.replace(self.path)
        self.app.handle("home")

    def exports(self):
        return list((Path(self.app.state_dir) / "exports").glob("log-selected-*.log"))

    def close(self):
        self.drain()
        self.app.research.close()


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    dashboard = ReviewDashboard(tmp_path, monkeypatch)
    yield dashboard
    dashboard.close()


@pytest.mark.parametrize("change", ["eviction", "truncation", "rotation"])
@pytest.mark.parametrize("render_before_yank", [False, True])
def test_invalidated_range_yank_never_becomes_an_unrequested_whole_file_copy(dashboard, change, render_before_yank):
    dashboard.app.logs.max_bytes = 64
    dashboard.app.logs.buffers.clear()
    dashboard.render()
    dashboard.app.handle("home")
    dashboard.app.handle("down")
    dashboard.app.handle("v")
    dashboard.app.handle("down")
    assert dashboard.app.logs.selection_active
    if change == "eviction":
        with dashboard.path.open("ab") as output:
            output.write(b"new line\n" * 100)
    elif change == "truncation":
        dashboard.path.write_bytes(b"replacement\n")
    else:
        replacement = dashboard.path.with_suffix(".replacement")
        replacement.write_bytes(b"different file\n")
        replacement.replace(dashboard.path)
    if render_before_yank:
        dashboard.render()
        assert not dashboard.app.logs.selection_active
    dashboard.app.handle("y")
    dashboard.drain()
    assert dashboard.copies == []
    assert dashboard.app.research.pending is None
    assert not dashboard.app.logs.selection_active


def test_history_log_key_preserves_displayed_job_when_new_accounting_arrives_between_frames(dashboard, tmp_path):
    second = Finished("78", "older failure", "FAILED", end="2026-10-05T11:00:00")
    newest = Finished("79", "new failure", "FAILED", end="2026-10-05T13:00:00")
    second_path = tmp_path / "failed-78.log"
    second_path.write_text("EXPECTED_HISTORY_JOB_78\n", encoding="utf-8")
    dashboard.store.details["78"] = {"StdOut": str(second_path), "StdErr": str(second_path)}
    original = list(dashboard.store.finished)
    dashboard.store.apply_finished(original + [second])
    dashboard.app.enter_tab("history")
    dashboard.render()
    dashboard.app.handle("down")
    dashboard.render()
    assert dashboard.app.selected_id == "78"
    dashboard.store.apply_finished(original + [second, newest])
    # No composition between an asynchronous inventory update and the log key.
    dashboard.app.handle("l")
    dashboard.render()
    assert dashboard.app.log_job == "78"
    assert dashboard.app.logs.path == str(second_path)


@pytest.mark.parametrize("wrapped", [False, True])
def test_log_rendering_inspects_only_viewport_sized_text_but_copy_keeps_the_full_line(dashboard, monkeypatch, wrapped):
    payload = b"header " + b"x" * 32000 + b"\r\n"
    replacement = dashboard.path.with_suffix(".replacement")
    replacement.write_bytes(payload)
    replacement.replace(dashboard.path)
    dashboard.app.handle("home")
    if wrapped:
        dashboard.app.handle("w")
    measured, classified = [], []
    original_vlen = layout.vlen

    def measured_vlen(text):
        measured.append(len(text))
        return original_vlen(text)

    class MeasuredPattern:
        def __init__(self, pattern):
            self.pattern = pattern

        def search(self, text):
            classified.append(len(text))
            return self.pattern.search(text)

    monkeypatch.setattr(layout, "vlen", measured_vlen)
    for name in ("LOG_ERROR", "LOG_WARNING", "LOG_SUCCESS"):
        monkeypatch.setattr(views_module, name, MeasuredPattern(getattr(views_module, name)))
    dashboard.render()
    budget = 100 * max(1, dashboard.app.logs.page) * 2
    assert measured and classified
    assert max(measured) <= budget
    assert max(classified) <= budget
    dashboard.app.handle("v")
    dashboard.app.handle("y")
    dashboard.drain()
    assert dashboard.copies == [payload]


@pytest.mark.parametrize("change", ["eviction", "truncation", "rotation"])
def test_stale_log_mouse_hits_cannot_select_or_copy_new_file_contents(dashboard, change):
    dashboard.app.logs.max_bytes = 64
    dashboard.app.logs.buffers.clear()
    dashboard.app.handle("home")
    _, hits = dashboard.render()
    clicked_row = next(y for y, kind, index in hits if kind == "log_line" and index == "1")
    if change == "eviction":
        with dashboard.path.open("ab") as output:
            output.write(b"new line\n" * 100)
    elif change == "truncation":
        dashboard.path.write_bytes(b"replacement\n")
    else:
        replacement = dashboard.path.with_suffix(".replacement")
        replacement.write_bytes(b"different file\n")
        replacement.replace(dashboard.path)
    dashboard.app.click(clicked_row, 10, hits)
    assert not dashboard.app.logs.selection_active
    dashboard.app.handle("y")
    dashboard.drain()
    assert dashboard.copies == []


def test_selected_invalid_utf8_is_exported_as_exact_raw_bytes_on_the_worker(dashboard):
    payload = b"invalid\tUTF-8: \xff\xfe\x80\r\n"
    dashboard.replace_log(payload)
    dashboard.app.handle("v")
    dashboard.app.handle("y")
    assert dashboard.app.research.pending is not None
    dashboard.drain()
    assert dashboard.copies == [payload]
    exported, = dashboard.exports()
    assert exported.read_bytes() == payload
    assert b"\xef\xbf\xbd" not in exported.read_bytes()
    assert not dashboard.app.logs.selection_active


@pytest.mark.parametrize("ending", [b"\r\n", b""])
def test_large_selected_line_copy_keeps_frontend_responsive_and_exact_including_partial_line(dashboard, monkeypatch, ending):
    payload = b"large\t" + b"x" * (1 << 20) + ending
    dashboard.replace_log(payload)
    dashboard.app.handle("v")
    entered, release = threading.Event(), threading.Event()

    def blocked_copy_file(path, **_kwargs):
        entered.set()
        assert release.wait(3), "copy worker was not released"
        data = Path(path).read_bytes()
        dashboard.copies.append(data)
        return {"methods": ["test clipboard"], "warnings": [], "bytes": len(data), "text": True}

    monkeypatch.setattr(clipboard, "copy_file", blocked_copy_file)
    try:
        dashboard.app.handle("y")
        assert entered.wait(3)
        future, _ = dashboard.app.research.pending
        assert not future.done()
        assert dashboard.copies == []
        # Keyboard handling and drawing continue while delivery is still blocked.
        dashboard.app.handle("e")
        dashboard.render()
        assert dashboard.app.logs.which == "err"
        assert not future.done()
        release.set()
        dashboard.drain()
        assert dashboard.copies == [payload]
        exported, = dashboard.exports()
        assert exported.read_bytes() == payload
    finally:
        release.set()


def test_selected_copy_completion_cannot_clear_identical_bounds_in_a_rotated_file(dashboard, monkeypatch):
    original = b"first selected file\n" + b"x" * (1 << 20) + b"\n"
    dashboard.replace_log(original)
    dashboard.app.handle("v")
    dashboard.app.handle("down")
    entered, release = threading.Event(), threading.Event()

    def blocked_copy_file(path, **_kwargs):
        entered.set()
        assert release.wait(3), "copy worker was not released"
        data = Path(path).read_bytes()
        dashboard.copies.append(data)
        return {"methods": ["test clipboard"], "warnings": [], "bytes": len(data), "text": True}

    monkeypatch.setattr(clipboard, "copy_file", blocked_copy_file)
    try:
        dashboard.app.handle("y")
        assert entered.wait(3)
        dashboard.replace_log(b"new first line\nnew second line\n")
        dashboard.app.handle("v")
        dashboard.app.handle("down")
        assert dashboard.app.logs.selection_active
        assert (dashboard.app.logs.selection_anchor, dashboard.app.logs.selection_end) == (0, 1)
        release.set()
        dashboard.drain()
        assert dashboard.copies == [original]
        assert dashboard.app.logs.selection_active
        assert (dashboard.app.logs.selection_anchor, dashboard.app.logs.selection_end) == (0, 1)
    finally:
        release.set()
