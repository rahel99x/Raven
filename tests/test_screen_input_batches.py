"""Queued scrolling preserves commands, hit maps, and source-line selection."""
from collections import deque
import curses
from types import SimpleNamespace

import pytest

from raven import clipboard, screen
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs
from raven.model import Job, Store
from raven.views import Views


class InputWindow:
    def __init__(self, values=(), mouse=None):
        self.values = deque(values)
        self.mouse = mouse
        self.timeouts = []

    def timeout(self, value):
        self.timeouts.append(value)

    def get_wch(self):
        if not self.values:
            raise curses.error("no queued input")
        value = self.values.popleft()
        if isinstance(value, tuple):
            self.mouse.append(value)
            return curses.KEY_MOUSE
        return value


@pytest.fixture
def dashboard():
    cfg = Config({"log_lines": 0, "animations": False, "gpu_sampling": False})
    store = Store(persist=False)
    store.apply_jobs([Job(str(i + 1), f"training-{i:03}", "gpu", "RUNNING") for i in range(100)])
    app = App(store, None, None, cfg, "test")
    views = Views(Glyphs(False), cfg)
    app.views_ref = views
    views.compose(store.snapshot(), app, 100, 24)
    return app, views, store


def consume(app, window, first, hits=()):
    return screen._consume_input_batch(app, window, curses, hits, (first, None))


def test_burst_keeps_every_cursor_move_and_stops_before_action(dashboard, monkeypatch):
    app, views, store = dashboard
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    keys = [curses.KEY_DOWN] * 15 + [curses.KEY_UP] * 4 + [" ", curses.KEY_DOWN]
    window = InputWindow(keys)
    pending = consume(app, window, "down")
    assert app.cursor["jobs"] == 12
    assert app.selected_id == app.visible_ids[12]
    assert not app.marks
    assert pending == ("space", None)
    assert list(window.values) == [curses.KEY_DOWN]
    # The following action targets the final selected job after a fresh frame.
    views.compose(store.snapshot(), app, 100, 24)
    selected = app.selected_id
    screen._consume_input_batch(app, window, curses, app.last_hits, pending)
    assert app.marks == {selected}
    assert list(window.values) == [curses.KEY_DOWN]


def test_boundary_scroll_does_not_lose_opposite_direction(dashboard, monkeypatch):
    app, _, _ = dashboard
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    window = InputWindow([curses.KEY_UP] * 30 + [curses.KEY_DOWN])
    assert consume(app, window, "up") is None
    assert app.cursor["jobs"] == 1
    assert not window.values


def test_batch_limit_leaves_remaining_events_in_order(dashboard, monkeypatch):
    app, _, _ = dashboard
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    window = InputWindow([curses.KEY_DOWN] * 80 + [curses.KEY_UP, "q"])
    assert consume(app, window, "down") is None
    assert app.cursor["jobs"] == screen.INPUT_BATCH_LIMIT
    assert len(window.values) == 51
    assert list(window.values)[-2:] == [curses.KEY_UP, "q"]
    assert not app.quit


def test_elapsed_budget_returns_before_reading_another_input(monkeypatch):
    applied = []
    clock = iter((0, .003, .006, .009))
    monkeypatch.setattr(screen.time, "monotonic", lambda: next(clock))
    app = SimpleNamespace(mode="main", tab="jobs", quit=False,
                          keymap={"down": "down"}, handle=applied.append)
    window = InputWindow([curses.KEY_DOWN] * 10)
    assert consume(app, window, "down") is None
    assert applied == ["down"] * 3
    assert len(window.values) == 8


@pytest.mark.parametrize("mode", ["palette", "help", "confirm", "execution", "analysis", "project_runs"])
def test_modal_navigation_is_not_batched(mode, monkeypatch):
    applied = []
    app = SimpleNamespace(mode=mode, tab="jobs", quit=False,
                          keymap={"down": "down"}, handle=applied.append)
    window = InputWindow([curses.KEY_DOWN, "y"])
    assert consume(app, window, "down") is None
    assert applied == ["down"]
    assert list(window.values) == [curses.KEY_DOWN, "y"]


@pytest.mark.parametrize("changed", ["mode", "tab", "research_job_id", "research_view", "analytics_job", "log_job"])
def test_document_or_mode_change_is_a_batch_barrier(changed):
    app = SimpleNamespace(mode="main", tab="jobs", quit=False, keymap={"down": "down"})
    applied = []

    def handle(key):
        applied.append(key)
        setattr(app, changed, "changed")

    app.handle = handle
    window = InputWindow([curses.KEY_DOWN, "y"])
    assert consume(app, window, "down") is None
    assert applied == ["down"]
    assert list(window.values) == [curses.KEY_DOWN, "y"]


def test_custom_navigation_binding_is_batched_and_remapped_arrow_is_barrier(dashboard, monkeypatch):
    app, _, _ = dashboard
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    app.keymap["x"] = "down"
    app.keymap["down"] = "mark"
    window = InputWindow(["x", curses.KEY_DOWN, "q"])
    pending = consume(app, window, "x")
    assert app.cursor["jobs"] == 2
    assert pending == ("down", None)
    assert not app.marks
    assert list(window.values) == ["q"]


def test_wheel_counts_and_mouse_click_identity_survive_batch(monkeypatch):
    mouse = deque()
    monkeypatch.setattr(curses, "getmouse", mouse.popleft)
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    calls = []
    app = SimpleNamespace(mode="main", tab="log", quit=False,
                          logs=SimpleNamespace(path="same.log", browser=False),
                          keymap={"up": "up"}, handle=lambda key: calls.append(("key", key)),
                          click=lambda *args, **kw: calls.append(("click", args, kw)))
    wheel = (0, 20, 12, 0, curses.BUTTON4_PRESSED)
    click = (0, 10, 7, 0, curses.BUTTON1_PRESSED | curses.BUTTON_SHIFT)
    window = InputWindow([wheel, wheel, click, curses.KEY_DOWN], mouse)
    pending = consume(app, window, "up")
    assert calls == [("key", "up")] * 7
    assert pending == ("mouse", click)
    fresh_hits = [(7, "log_line", "123")]
    screen._consume_input_batch(app, window, curses, fresh_hits, pending)
    assert calls[-1] == ("click", (7, 10, fresh_hits), {"button": "left", "shift": True})
    assert list(window.values) == [curses.KEY_DOWN]


def test_mouse_motion_and_unknown_keys_do_not_displace_pending_resize(dashboard, monkeypatch):
    app, _, _ = dashboard
    mouse = deque()
    monkeypatch.setattr(curses, "getmouse", mouse.popleft)
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    window = InputWindow([(0, 10, 9, 0, curses.REPORT_MOUSE_POSITION), 0, curses.KEY_RESIZE, "q"], mouse)
    assert consume(app, window, "down") == ("resize", None)
    assert app.cursor["jobs"] == 1
    assert list(window.values) == ["q"]


def test_remapped_wheel_action_waits_for_a_fresh_frame(dashboard, monkeypatch):
    app, _, _ = dashboard
    mouse = deque()
    monkeypatch.setattr(curses, "getmouse", mouse.popleft)
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    app.keymap["up"] = "mark"
    wheel = (0, 10, 9, 0, curses.BUTTON4_PRESSED)
    window = InputWindow([wheel, curses.KEY_DOWN], mouse)
    assert consume(app, window, "down") == ("mouse", wheel)
    assert app.cursor["jobs"] == 1 and not app.marks
    assert list(window.values) == [curses.KEY_DOWN]


def test_log_burst_then_visual_yank_preserves_original_selected_bytes(dashboard, tmp_path, monkeypatch):
    app, views, store = dashboard
    raw = [f"prefix_{i:03}\tpayload 界\r\n".encode() for i in range(120)]
    path = tmp_path / "stdout.log"
    path.write_bytes(b"".join(raw))
    store.details["1"] = {"StdOut": str(path)}
    app.open_log("1")
    views.compose(store.snapshot(), app, 100, 24)
    app.handle("home")
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    window = InputWindow([curses.KEY_DOWN] * 19 + ["v"])
    pending = consume(app, window, "down")
    assert app.logs.cursor == 20
    assert pending == ("v", None)
    views.compose(store.snapshot(), app, 100, 24)
    screen._consume_input_batch(app, window, curses, app.last_hits, pending)
    assert app.logs.selection_anchor == 20
    window = InputWindow([curses.KEY_DOWN] * 3 + ["y"])
    pending = consume(app, window, "down")
    assert app.logs.selection_end == 24
    copied = []
    monkeypatch.setattr(clipboard, "copy", lambda text, state_dir=None, **kw: copied.append(text.encode()) or "captured")
    views.compose(store.snapshot(), app, 100, 24)
    screen._consume_input_batch(app, window, curses, app.last_hits, pending)
    assert copied == [b"".join(raw[20:25])]


def test_curses_loop_repaints_before_queued_click_and_following_action(dashboard, monkeypatch):
    app, views, store = dashboard
    mouse = deque()
    monkeypatch.setattr(curses, "getmouse", mouse.popleft)
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    for name in ("curs_set", "mousemask", "mouseinterval", "doupdate", "set_escdelay"):
        monkeypatch.setattr(curses, name, lambda *args: None)
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    row = next(y for y, kind, _ in app.last_hits if kind == "job")

    class PaintedWindow(InputWindow):
        def getmaxyx(self):
            return 24, 100

        def keypad(self, enabled):
            pass

        def erase(self):
            pass

        def addstr(self, *args):
            pass

        def noutrefresh(self):
            pass

    click = (0, 5, row, 0, curses.BUTTON1_PRESSED)
    window = PaintedWindow([curses.KEY_DOWN] * 15 + [click, " ", "q"], mouse)
    monkeypatch.setattr(curses, "wrapper", lambda callback: callback(window))
    frames = []
    clicks = []
    compose, click_handler = views.compose, app.click

    def paint(*args, **kwargs):
        result = compose(*args, **kwargs)
        frames.append((app.selected_id, list(result[1])))
        return result

    def select(y, x, hits, **kwargs):
        # A stale pre-burst frame would still name the first job. This must be
        # the frame drawn after all 15 down events, before the queued click.
        assert frames[-1][0] == app.selected_id == app.visible_ids[15]
        assert list(hits) == frames[-1][1]
        target = next(key for hy, kind, key in hits if hy == y and kind == "job")
        clicks.append(target)
        return click_handler(y, x, hits, **kwargs)

    views.compose, app.click = paint, select
    screen.run_curses(app, views, None, store, None, app.cfg)
    assert app.quit and not window.values
    assert app.marks == set(clicks) and len(clicks) == 1
    assert len(frames) == 4  # initial, scrolled, clicked, marked
    assert window.timeouts[0] == 200 and window.timeouts[-1] == 200


def test_discarded_mouse_report_keeps_the_published_document(dashboard):
    app, _, _ = dashboard
    before = app.tab, app.selected_id, set(app.marks)
    effects = screen._InputEffects()
    screen._consume_input_batch(app, InputWindow(), curses, app.last_hits,
                                ("mouse", None), effects=effects)
    assert not effects.document
    assert (app.tab, app.selected_id, set(app.marks)) == before
