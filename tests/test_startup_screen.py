"""Actual curses-loop welcome, input, menu layers, and painted-frame contracts."""
from __future__ import annotations

from collections import deque
import curses
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven import layout as L
from raven import screen, startup, toolbar
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views


class PaintedWindow:
    """Record final display cells and painter order without a real terminal."""

    def __init__(self, height=38, width=160):
        self.height, self.width = height, width
        self.rows = []
        self.frames = []
        self.paint_order = []
        self.timeouts = []

    def getmaxyx(self):
        return self.height, self.width

    def timeout(self, value):
        self.timeouts.append(value)

    def keypad(self, enabled):
        pass

    def erase(self):
        self.rows = [[" "] * self.width for _ in range(self.height)]
        self.paint_order = []

    def addstr(self, y, x, text, attribute):
        self.paint_order.append((y, x, text))
        for character in text:
            cells = L.vlen(character)
            if cells and 0 <= y < self.height and 0 <= x < self.width:
                self.rows[y][x] = character
            x += cells

    def noutrefresh(self):
        self.frames.append({"rows": tuple("".join(row) for row in self.rows),
                            "paint_order": tuple(self.paint_order)})


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    screen._INPUT_READERS.clear()
    clock = [100.0]
    monkeypatch.setattr(startup.time, "monotonic", lambda: clock[0])
    for name in ("raw", "curs_set", "mousemask", "mouseinterval", "doupdate", "set_escdelay"):
        monkeypatch.setattr(curses, name, lambda *args: None)
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    cfg = Config({"clipboard": {"osc52": False, "tools": False}, "gpu_sampling": False})
    store = Store(state_dir=str(tmp_path / "state"), persist=False)
    store.jobs = [Job(id="700", name="SCREEN_VISIBLE_JOB", state="RUNNING", partition="main", cpus=4)]
    app = App(store, None, None, cfg, "tester", interactive=True)
    app.state_dir = str(tmp_path / "clipboard")
    painter = Views(L.Glyphs(False), cfg)
    window = PaintedWindow()
    monkeypatch.setattr(curses, "wrapper", lambda callback: callback(window))
    monkeypatch.setattr(screen, "_mouse_reporting", lambda enabled: None)
    queue = deque()

    def read(stdscr, curses_module):
        assert stdscr is window
        assert queue, "Curses loop exhausted the scripted input without quitting."
        # None represents one idle input wait, not a queued terminal event.
        # A nonblocking prepaint poll cannot consume that wait or advance time.
        if queue[0] is None and window.timeouts[-1] == 0:
            return None
        value = queue.popleft()
        if window.timeouts[-1] != 0:
            clock[0] += .1
        return value() if callable(value) else value

    monkeypatch.setattr(screen, "_read_input", read)

    def run(events):
        queue.extend(events)
        screen.run_curses(app, painter, None, store, None, cfg)
        assert not queue

    yield SimpleNamespace(app=app, painter=painter, store=store, cfg=cfg,
                          window=window, clock=clock, run=run)
    if app.research is not None:
        app.research.shutdown()
    screen._INPUT_READERS.clear()


def frame_text(frame):
    return "\n".join(frame["rows"])


def test_actual_loop_starts_once_draws_welcome_and_keeps_toolbar(terminal, monkeypatch):
    calls = []
    original = startup.begin

    def observe_begin(app, *args, **kwargs):
        calls.append((app, kwargs))
        return original(app, *args, **kwargs)

    monkeypatch.setattr(startup, "begin", observe_begin)
    terminal.run([None, None, ("2", None), ("ctrl-c", None)])
    assert len(calls) == 1
    assert terminal.app.startup_state["begun"]
    assert not startup.active(terminal.app)
    assert terminal.app.tab == "cluster"
    assert any("Any key continues" in frame_text(frame) for frame in terminal.window.frames)
    assert 40 in terminal.window.timeouts
    for frame in terminal.window.frames:
        assert frame["rows"][0].lstrip().startswith("x")
        assert "File" in frame["rows"][0] and "View" in frame["rows"][0]
        assert "5s" in frame["rows"][0] and "1x" not in frame["rows"][0]


@pytest.mark.parametrize("reason", ["noninteractive", "disabled", "reader", "reduced-motion"])
def test_actual_loop_skips_welcome_for_noninteractive_or_preferences(terminal, reason):
    if reason == "noninteractive":
        terminal.app.interactive = False
    elif reason == "disabled":
        startup.restore(terminal.app, {"enabled": False})
    elif reason == "reader":
        terminal.app.set_theme("reader")
    else:
        terminal.cfg.set("animations", False)
    terminal.run([None, ("ctrl-c", None)])
    assert terminal.app.startup_state["begun"]
    assert not terminal.app.startup_state["running"]
    assert all("Any key continues" not in frame_text(frame) for frame in terminal.window.frames)
    assert 40 not in terminal.window.timeouts


def test_first_mouse_click_dismisses_and_opens_the_clicked_page(terminal):
    active_at_click = []

    def click_cluster():
        active_at_click.append(startup.active(terminal.app))
        y, left, right, _ = next(hit for hit in terminal.app.tab_hits if hit[3] == "cluster")
        return "mouse", (0, left, y, 0, curses.BUTTON1_CLICKED)

    terminal.run([click_cluster, ("ctrl-c", None)])
    assert active_at_click == [True]
    assert terminal.app.tab == "cluster"
    assert not startup.active(terminal.app)
    assert "Any key continues" in frame_text(terminal.window.frames[0])
    assert "Any key continues" not in frame_text(terminal.window.frames[-1])


def test_welcome_expires_during_normal_frames_without_input_or_restart(terminal):
    terminal.run([None] * 11 + [("ctrl-c", None)])
    assert "Any key continues" in frame_text(terminal.window.frames[0])
    assert "Any key continues" not in frame_text(terminal.window.frames[-1])
    assert not startup.active(terminal.app)
    assert terminal.app.startup_state["start"] == 100
    # The last idle read consumed half of the fixed maintenance interval.
    assert terminal.window.timeouts[-1] == 100


def test_preview_paints_below_open_dropdown_without_closing_it(terminal, monkeypatch):
    startup.restore(terminal.app, {"enabled": False})
    items = toolbar.menu_items(terminal.app, "View")
    cursor = next(index for index, item in enumerate(items) if item.key == "startup-preview")
    toolbar.initialize(terminal.app).update(menu=2, cursor=cursor)
    overlays = []
    original = startup.overlay

    def observe_overlay(*args, **kwargs):
        value = original(*args, **kwargs)
        overlays.append(value)
        return value

    monkeypatch.setattr(startup, "overlay", observe_overlay)
    terminal.run([("enter", None), None, ("ctrl-c", None)])
    assert startup.save(terminal.app) == {"enabled": False}
    assert terminal.app.toolbar_state["menu"] == 2
    assert overlays[0] is None
    welcome_frame = next(frame for frame, welcome in zip(terminal.window.frames, overlays) if welcome)
    assert "Any key continues" in frame_text(welcome_frame)
    assert "Preview startup animation" in frame_text(welcome_frame)
    # The welcome occupies the center, and the menu wins wherever they overlap.
    welcome = next(value for value in overlays if value)
    menu = toolbar.overlay(terminal.painter, terminal.store.snapshot(), terminal.app,
                           terminal.window.width, terminal.window.height)
    intersection = next((y, x, L.row_text(row)) for y, x, row in menu
                        if any(y == wy and x < wx + L.vlen(L.row_text(wrow))
                               and x + L.vlen(L.row_text(row)) > wx for wy, wx, wrow in welcome))
    y, x, row = intersection
    assert welcome_frame["rows"][y][x:x + L.vlen(row)] == row


def test_actual_screen_retains_footer_selection_and_pristine_copy(terminal):
    terminal.store.jobs.append(Job('2', 'second selectable job', 'cpu', 'RUNNING'))
    startup.restore(terminal.app, {"enabled": False})
    terminal.app.say("FOOTER_ONLY_NOTICE")
    terminal.run([("v", None), ("down", None), ("y", None), ("ctrl-c", None)])
    frames = terminal.window.frames
    assert "FOOTER_ONLY_NOTICE" in frames[0]["rows"][-1]
    assert any(any("▏" in row for row in frame["rows"]) for frame in frames)
    assert any("copied 2 lines" in frame["rows"][-1] for frame in frames)
    clipboard = Path(terminal.app.state_dir) / "clipboard.txt"
    content = clipboard.read_text()
    assert content.count("\n") == 2
    assert "◆" not in content
    assert "▏" not in content
    assert "FOOTER_ONLY_NOTICE" not in content
    # Pristine rows retain the structural splitter diamond and exclude the
    # pane-local text selection feedback.
    divider = terminal.app.pane_drag_state["dividers"]["workspace:jobs"]
    structural_y = divider.y + (divider.height - 1) // 2
    for y, row in enumerate(terminal.app.last_rows):
        text = L.row_text(row)
        assert text.count("◆") == (1 if y == structural_y else 0)
        assert not text.endswith("◆")


def test_ui_trace_covers_actual_curses_phases(terminal):
    from raven.ui_trace import UITrace
    terminal.app.ui_trace = UITrace()
    terminal.cfg.set("animations", False)
    terminal.run([None, ("2", None), ("ctrl-c", None)])
    report = terminal.app.ui_trace.report()
    assert {"maintenance", "snapshot", "compose", "document", "feedback", "paint",
            "terminal_flush", "prepaint_input", "input_wait", "input_batch", "input_dispatch"} <= set(report["phases"])
    assert report["inputs"]["keyboard"] == 2
    assert any(event["after"]["tab"] == "cluster" for event in report["transitions"])
