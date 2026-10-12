"""Source-line selection and faithful copying through keyboard and mouse actions."""
from __future__ import annotations

from pathlib import Path
import threading

import pytest

from raven import clipboard
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text, vlen
from raven.model import Finished, Store
from raven.remote import LocalFiles
from raven.research import ResearchHub
from raven.views import Views


class SelectionDashboard:
    def __init__(self, root, monkeypatch, *, ascii_=True, count=160):
        self.root = root
        self.raw_lines = {
            "out": [f"out {index:03d}\tpayload 界 {index}\r\n".encode("utf-8") for index in range(count)],
            "err": [f"err {index:03d}\terror detail {index}\r\n".encode("utf-8") for index in range(count)],
        }
        self.paths = {"out": root / "stdout-77.log", "err": root / "stderr-77.log"}
        for which, path in self.paths.items():
            path.write_bytes(b"".join(self.raw_lines[which]))
        self.copies = []

        def copy(text, state_dir=None, **kwargs):
            self.copies.append(text.encode("utf-8"))
            return "captured selection"

        def copy_file(path, **kwargs):
            data = Path(path).read_bytes()
            self.copies.append(data)
            return {"methods": ["test clipboard"], "warnings": [], "bytes": len(data), "text": True}

        self.copy_file = copy_file
        monkeypatch.setattr(clipboard, "copy", copy)
        monkeypatch.setattr(clipboard, "copy_file", copy_file)
        self.cfg = Config({"log_lines": 0, "clipboard": {"osc52": False, "tools": True}})
        self.store = Store(state_dir=str(root / "private state"))
        self.store.finished = [Finished("77", "failed experiment", "FAILED", workdir=str(root))]
        self.store.details["77"] = {"StdOut": str(self.paths["out"]), "StdErr": str(self.paths["err"]), "WorkDir": str(root)}
        self.files = LocalFiles()
        # These cases exercise source-line and byte-copy semantics with current
        # file contents; interactive snapshot responsiveness has its own suite.
        self.app = App(self.store, None, None, self.cfg, "reader", ascii_=ascii_, interactive=False)
        self.views = Views(Glyphs(ascii_), self.cfg, files=self.files)
        self.app.files = self.files
        self.app.logs.files = self.files
        self.app.views_ref = self.views
        self.app.research = ResearchHub(self.cfg, self.files)
        self.app.handle("3")
        self.render()
        self.app.handle("l")
        self.render()

    def render(self, *, width=100, height=24):
        rows, hits = self.views.compose(self.store.snapshot(), self.app, width, height)
        self.app.last_hits = hits
        return "\n".join(row_text(row) for row in rows), rows, hits

    def drain(self):
        for _ in range(5):
            pending = self.app.research.pending
            if pending is None:
                return
            pending[0].result(timeout=3)
            self.app.research.poll_task()
        raise AssertionError("copy task did not finish on its single worker")

    def close(self):
        self.drain()
        if self.app.logs.catalog:
            self.app.logs.catalog.close()
        self.app.research.close()

    def select(self, start, end):
        self.app.handle("home")
        for _ in range(start):
            self.app.handle("down")
        self.app.handle("v")
        for _ in range(end - start):
            self.app.handle("down")


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    dashboard = SelectionDashboard(tmp_path, monkeypatch)
    yield dashboard
    dashboard.close()


def test_visual_selection_starts_at_keyboard_cursor_without_mouse_click(dashboard):
    dashboard.select(5, 8)
    logs = dashboard.app.logs
    assert logs.cursor == 8
    assert logs.selection_anchor == 5 and logs.selection_end == 8
    dashboard.app.handle("y")
    dashboard.drain()
    assert dashboard.copies == [b"".join(dashboard.raw_lines["out"][5:9])]


def test_selection_can_extend_across_pages_and_copy_source_lines_without_display_gutters(dashboard):
    dashboard.select(2, 2)
    dashboard.app.handle("pgdn")
    dashboard.app.handle("pgdn")
    last = dashboard.app.logs.cursor
    assert last > dashboard.app.logs.page
    dashboard.render()
    dashboard.app.handle("y")
    dashboard.drain()
    assert dashboard.copies == [b"".join(dashboard.raw_lines["out"][2:last + 1])]


def test_reverse_selection_copies_lines_in_file_order(dashboard):
    dashboard.select(10, 10)
    for _ in range(4):
        dashboard.app.handle("up")
    dashboard.app.handle("y")
    dashboard.drain()
    assert dashboard.copies == [b"".join(dashboard.raw_lines["out"][6:11])]


@pytest.mark.parametrize("ascii_,marker", [(True, "*"), (False, "◆")])
def test_selected_line_has_rightmost_visible_marker_and_cursor_keeps_line_visible(tmp_path, monkeypatch, ascii_, marker):
    dashboard = SelectionDashboard(tmp_path, monkeypatch, ascii_=ascii_)
    try:
        dashboard.select(3, 4)
        _, rows, hits = dashboard.render(width=80, height=20)
        selected = [(y, int(index)) for y, kind, index in hits if kind == "log_line" and int(index) in (3, 4)]
        assert selected
        assert any(index == 4 for _, index in selected)
        for y, _ in selected:
            text = row_text(rows[y])
            assert text.endswith(marker)
            assert vlen(text) == 80
            assert rows[y][-1][0].endswith(marker)
    finally:
        dashboard.close()


def test_plain_cursor_appears_without_a_selection(dashboard):
    dashboard.app.handle("home")
    dashboard.app.handle("down")
    _, rows, hits = dashboard.render()
    cursor_row = next(y for y, kind, index in hits if kind == "log_line" and index == "1")
    assert row_text(rows[cursor_row]).endswith(">")
    assert not dashboard.app.logs.selection_active


def test_wrapped_mouse_line_selection_maps_continuation_to_one_source_line(dashboard):
    long_line = ("out 003\t" + "long 界 text " * 80 + "\r\n").encode("utf-8")
    dashboard.raw_lines["out"][3] = long_line
    replacement = dashboard.paths["out"].with_suffix(".replacement")
    replacement.write_bytes(b"".join(dashboard.raw_lines["out"]))
    replacement.replace(dashboard.paths["out"])
    dashboard.app.handle("home")
    dashboard.app.handle("w")
    _, rows, hits = dashboard.render(width=80, height=20)
    fragments = [y for y, kind, index in hits if kind == "log_line" and index == "3"]
    assert len(fragments) >= 2
    dashboard.app.click(fragments[-1], 10, hits)
    dashboard.app.handle("v")
    assert dashboard.app.logs.selection_anchor == 3
    dashboard.app.handle("down")
    _, _, hits = dashboard.render(width=80, height=20)
    assert any(kind == "log_line" and index == "4" for _, kind, index in hits)
    dashboard.app.handle("y")
    dashboard.drain()
    assert dashboard.copies == [b"".join(dashboard.raw_lines["out"][3:5])]


def test_shift_click_extends_selection_using_source_lines(dashboard):
    dashboard.app.handle("home")
    _, _, hits = dashboard.render()
    first = next(y for y, kind, index in hits if kind == "log_line" and index == "2")
    last = next(y for y, kind, index in hits if kind == "log_line" and index == "5")
    dashboard.app.click(first, 10, hits)
    dashboard.app.click(last, 10, hits, shift=True)
    dashboard.app.handle("y")
    dashboard.drain()
    assert dashboard.copies == [b"".join(dashboard.raw_lines["out"][2:6])]


def test_clear_selection_returns_to_cursor_navigation(dashboard):
    dashboard.select(3, 6)
    dashboard.app.handle("esc")
    assert not dashboard.app.logs.selection_active
    dashboard.app.handle("down")
    assert dashboard.app.logs.cursor == 7


def test_stderr_selection_immediately_after_switch_never_copies_previous_stdout(dashboard):
    dashboard.select(2, 4)
    dashboard.app.handle("e")
    # Do not render between the file switch and the next keyboard actions.
    dashboard.select(0, 2)
    dashboard.app.handle("y")
    dashboard.drain()
    assert dashboard.app.logs.path == str(dashboard.paths["err"])
    assert dashboard.copies == [b"".join(dashboard.raw_lines["err"][:3])]


def test_selection_clears_when_switching_files_without_reusing_old_bounds(dashboard):
    dashboard.select(5, 12)
    dashboard.app.handle("e")
    dashboard.render()
    assert not dashboard.app.logs.selection_active
    dashboard.app.handle("home")
    dashboard.app.handle("v")
    dashboard.app.handle("y")
    dashboard.drain()
    assert dashboard.copies == [dashboard.raw_lines["err"][0]]


@pytest.mark.parametrize("key", ["y", "Y", "v", "V"])
def test_log_browser_requires_opening_file_before_selecting_or_copying(dashboard, key):
    dashboard.app.handle("O")
    dashboard.render(height=None)
    dashboard.drain()
    dashboard.render()
    assert dashboard.app.logs.browser
    dashboard.app.handle(key)
    dashboard.drain()
    assert dashboard.app.logs.browser
    assert dashboard.copies == []


def test_browser_enter_then_selection_without_render_uses_new_file(dashboard):
    dashboard.app.handle("O")
    dashboard.render(height=None)
    dashboard.drain()
    dashboard.render()
    entries = dashboard.app.log_entries()
    err_index = next(index for index, entry in enumerate(entries) if entry["path"] == str(dashboard.paths["err"]))
    dashboard.app.handle("home")
    for _ in range(err_index):
        dashboard.app.handle("down")
    dashboard.app.handle("enter")
    dashboard.select(1, 3)
    dashboard.app.handle("y")
    dashboard.drain()
    assert dashboard.copies == [b"".join(dashboard.raw_lines["err"][1:4])]


@pytest.mark.parametrize("action", ["visual_all", "copy_all_key", "copy_all_command"])
def test_copy_all_means_whole_current_file_even_when_log_buffer_is_small(dashboard, action):
    dashboard.app.logs.max_bytes = 256
    dashboard.app.logs.buffers.clear()
    dashboard.render()
    if action == "visual_all":
        dashboard.app.handle("V")
        dashboard.app.handle("y")
    elif action == "copy_all_key":
        dashboard.app.handle("Y")
    else:
        dashboard.app.run_command("copy all")
    dashboard.drain()
    assert dashboard.copies == [b"".join(dashboard.raw_lines["out"])]


@pytest.mark.parametrize("key", ["v", "V", "y", "Y"])
def test_empty_log_selection_or_copy_is_safe_and_never_copies_view_labels(dashboard, key):
    dashboard.paths["out"].write_bytes(b"")
    dashboard.render()
    dashboard.app.handle(key)
    dashboard.drain()
    assert not dashboard.app.logs.selection_active
    assert all(data == b"" for data in dashboard.copies)


def test_full_copy_finishing_after_file_switch_does_not_mark_new_file(dashboard, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def blocked_copy(path, **kwargs):
        entered.set()
        assert release.wait(3), "copy worker was not released"
        return dashboard.copy_file(path, **kwargs)

    monkeypatch.setattr(clipboard, "copy_file", blocked_copy)
    try:
        dashboard.app.handle("Y")
        assert entered.wait(3)
        dashboard.app.handle("e")
        dashboard.render()
        assert dashboard.app.logs.path == str(dashboard.paths["err"])
        release.set()
        dashboard.drain()
        assert dashboard.copies == [b"".join(dashboard.raw_lines["out"])]
        assert dashboard.app.logs.path == str(dashboard.paths["err"])
        assert not dashboard.app.logs.selection_active
    finally:
        release.set()


def test_full_copy_finishing_on_same_file_preserves_a_newer_selection(dashboard, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def blocked_copy(path, **kwargs):
        entered.set()
        assert release.wait(3), "copy worker was not released"
        return dashboard.copy_file(path, **kwargs)

    monkeypatch.setattr(clipboard, "copy_file", blocked_copy)
    try:
        dashboard.app.handle("Y")
        assert entered.wait(3)
        dashboard.select(4, 6)
        assert dashboard.app.logs.selection_active
        bounds = (dashboard.app.logs.selection_anchor, dashboard.app.logs.selection_end)
        release.set()
        dashboard.drain()
        assert dashboard.copies == [b"".join(dashboard.raw_lines["out"])]
        assert dashboard.app.logs.selection_active
        assert (dashboard.app.logs.selection_anchor, dashboard.app.logs.selection_end) == bounds
    finally:
        release.set()
