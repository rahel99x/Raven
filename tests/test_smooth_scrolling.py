"""Viewport motion remains bounded, exact and independent of scheduler/file IO."""
from collections import deque
import curses
import io
import json
import math
from types import SimpleNamespace

import pytest

from raven import screen, scrolling
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs
from raven.model import Job, Store
from raven.views import Views


def small_app(**values):
    fields = dict(cfg={}, theme="default", animations_enabled=True)
    fields.update(values)
    return SimpleNamespace(**fields)


@pytest.mark.parametrize("target", [0, 1, 2, 12, 40, 100, 500, 50000])
@pytest.mark.parametrize("rate", [30, 60, 120, 1000])
def test_pid_monotonic_bounded_and_exact_at_deadline(target, rate):
    pid = scrolling.ScrollPID(upper=50000)
    pid.snap(0, 10)
    pid.set_target(target, 10, upper=50000)
    positions = [pid.step(10 + tick / rate) for tick in range(1, math.ceil(rate * .25) + 1)]
    assert positions == sorted(positions)
    assert all(0 <= position <= target for position in positions)
    assert positions[-1] == target
    assert not pid.active and pid.velocity == pid.integral == 0


@pytest.mark.parametrize("start,target", [(0, 100), (100, 0), (10000, 9999), (1, 0)])
def test_pid_reversal_drops_stored_momentum_and_never_overshoots(start, target):
    pid = scrolling.ScrollPID(upper=10000)
    pid.snap(start, 0)
    pid.set_target(target, 0, upper=10000)
    pid.step(.05)
    before = pid.position
    opposite = start
    pid.set_target(opposite, .05, upper=10000)
    assert pid.integral == pid.velocity == 0
    positions = [pid.step(.05 + tick / 60) for tick in range(1, 17)]
    assert all(min(before, opposite) <= position <= max(before, opposite) for position in positions)
    assert positions[-1] == opposite


def test_pid_long_pause_backward_clock_nonfinite_and_changed_bounds():
    pid = scrolling.ScrollPID(upper=200)
    pid.snap(0, 1)
    pid.set_target(100, 1, upper=200)
    assert pid.step(0) == pid.step(float("nan")) == 0
    assert pid.step(1000) == 100
    pid.set_target(200, 1000, upper=5)
    assert pid.target == pid.position == 5
    assert pid.step(1000.01) == 5


def test_pid_saturation_does_not_wind_up_or_exceed_bounded_rate():
    pid = scrolling.ScrollPID(upper=1000000)
    pid.snap(0, 0)
    pid.set_target(1000000, 0, upper=1000000)
    previous = 0
    for tick in range(1, 10):
        now = tick / 60
        position = pid.step(now)
        assert position - previous <= scrolling.MAX_SPEED / 60 + 1e-8
        assert abs(pid.integral) <= .25
        previous = position
    pid.set_target(0, .15, upper=1000000)
    assert pid.integral == pid.velocity == 0
    assert pid.step(.17) < previous


def test_viewport_retains_logical_target_and_seeds_new_documents_immediately():
    app = small_app()
    assert scrolling.viewport(app, "details", 0, 100, 10, context=("7",), now=10) == 0
    scrolling.note_input(app, "wheel", now=10)
    scrolling.begin_frame(app)
    painted = scrolling.viewport(app, "details", 40, 100, 10, context=("7",), now=10.016)
    assert 0 < painted < 40
    assert app.scrolling_state["controllers"]["details"]["pid"].target == 40
    assert scrolling.finish_frame(app) and scrolling.timeout_ms(app) == 17
    assert scrolling.viewport(app, "details", 20, 100, 10, context=("8",), now=10.02) == 20
    assert scrolling.viewport(app, "details", 1000, 3, 20, context=("8",), now=10.03) == 0


def test_virtualized_renderer_peeks_painted_window_without_advancing_or_resetting_frame():
    app = small_app()
    scrolling.viewport(app, "workspace:jobs:details", 0, 100, 10, context=("metrics",), now=0)
    scrolling.note_input(app, "wheel", now=0)
    first = scrolling.viewport(app, "workspace:jobs:details", 40, 100, 10, context=("metrics",), now=.01)
    scrolling.begin_frame(app)
    pid = app.scrolling_state["controllers"]["workspace:jobs:details"]["pid"]
    before = (pid.position, pid.updated, pid.velocity)
    assert scrolling.published_position(app, "workspace:jobs:details", 40, context=("metrics",), now=.02) == first
    assert (pid.position, pid.updated, pid.velocity) == before
    assert scrolling.published_position(app, "workspace:jobs:details", 40, context=("workflow",), now=.02) == 40
    scrolling.note_input(app, "key")
    assert scrolling.published_position(app, "workspace:jobs:details", 80, now=.02) == 80


def test_virtualized_window_prepares_exact_deadline_target_before_pid_endpoint_snap():
    app = small_app()
    scrolling.viewport(app, "details", 0, 100, 10, now=0)
    scrolling.note_input(app, "wheel", now=0)
    scrolling.viewport(app, "details", 40, 100, 10, now=.01)
    # A fresh wheel report at a boundary extends the gesture without changing
    # its target. The controller's endpoint deadline still owns the next paint.
    scrolling.note_input(app, "wheel", now=.24)
    assert scrolling.published_position(app, "details", 40, now=.26) == 40
    assert scrolling.viewport(app, "details", 40, 100, 10, now=.26) == 40


@pytest.mark.parametrize("operation", ["key", "paste", "press", "drag", "release", "copy"])
def test_nonwheel_input_snaps_painted_position_before_exact_selection(operation):
    app = small_app()
    scrolling.viewport(app, "log", 0, 1000, 10, now=0)
    scrolling.note_input(app, "wheel", now=0)
    assert scrolling.viewport(app, "log", 100, 1000, 10, now=.01) < 100
    scrolling.note_input(app, operation, now=.01)
    assert scrolling.viewport(app, "log", 100, 1000, 10, now=.02) == 100
    assert not scrolling.active(app) and scrolling.timeout_ms(app) == 200


@pytest.mark.parametrize("fields", [{"animations_enabled": False}, {"theme": "reader"}, {"cfg": {"smooth_scrolling": False}}])
def test_reduced_motion_and_disabled_preference_keep_direct_wheel_updates(fields):
    app = small_app(**fields)
    scrolling.viewport(app, "research", 0, 100, 5, now=0)
    scrolling.note_input(app, "wheel", now=0)
    assert scrolling.viewport(app, "research", 50, 100, 5, now=.01) == 50
    assert not scrolling.active(app)


def test_no_input_queue_bounded_memory_and_untouched_views_do_not_animate():
    app = small_app()
    for index in range(1000):
        scrolling.viewport(app, str(index), index, 10000, 5, now=0)
    assert len(app.scrolling_state["controllers"]) == scrolling.MAX_CONTROLLERS
    scrolling.note_input(app, "wheel", now=0)
    for target in range(1000):
        scrolling.viewport(app, "999", target, 10000, 5, now=.001)
    entry = app.scrolling_state["controllers"]["999"]
    assert entry["pid"].target == 999 and set(entry) == {"pid", "context"}
    scrolling.begin_frame(app)
    assert not scrolling.finish_frame(app) and scrolling.timeout_ms(app) == 200


def test_preference_round_trip_and_invalid_commands_preserve_state():
    events = []
    app = small_app(say=events.append, fail=events.append, save=lambda: events.append("saved"))
    assert scrolling.enabled(app)
    assert scrolling.run_command(app, ["smoothscroll", "off"])
    assert not scrolling.enabled(app) and app.cfg["smooth_scrolling"] is False
    assert json.loads(json.dumps(scrolling.save(app))) == {"enabled": False}
    scrolling.restore(app, {"enabled": "true"})
    assert not scrolling.enabled(app)
    scrolling.restore(app, {"enabled": True})
    assert scrolling.enabled(app)
    assert scrolling.run_command(app, ["smoothscroll", "bad"])
    assert scrolling.enabled(app) and events[-1] == "smoothscroll [on|off|toggle]"
    assert scrolling.run_command(app, ["smoothscroll"])
    assert not scrolling.enabled(app)
    assert not scrolling.run_command(app, ["unrelated"])


class InputWindow:
    def __init__(self, values, mouse):
        self.values, self.mouse = deque(values), mouse

    def timeout(self, value):
        pass

    def get_wch(self):
        if not self.values:
            raise curses.error("empty")
        value = self.values.popleft()
        if isinstance(value, tuple):
            self.mouse.append(value)
            return curses.KEY_MOUSE
        return value


def input_app(**fields):
    values = dict(mode="main", tab="jobs", quit=False, keymap={"up": "up", "down": "down"},
                  handle=lambda key: None, click=lambda *args, **kwargs: None)
    values.update(fields)
    app = SimpleNamespace(**values)
    from raven.workbench import bind_actions
    bind_actions(app)
    return app


def test_motion_burst_coalesces_endpoint_and_preserves_release_click_and_key(monkeypatch):
    mouse, applied = deque(), []
    monkeypatch.setattr(curses, "getmouse", mouse.popleft)
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    monkeypatch.setattr(screen, "_apply_input", lambda app, event, hits, module: applied.append(event))
    motion = lambda x: (0, x, 9, 0, curses.REPORT_MOUSE_POSITION | curses.BUTTON1_PRESSED)
    release = (0, 150, 9, 0, curses.BUTTON1_RELEASED)
    click = (0, 10, 9, 0, curses.BUTTON1_CLICKED)
    app = input_app()
    window = InputWindow([motion(index) for index in range(1, 150)] + [release, click, "q"], mouse)
    pending = screen._consume_input_batch(app, window, curses, (), ("mouse", motion(0)))
    assert applied == [("mouse", motion(149))]
    assert pending == ("mouse", release)
    assert list(window.values) == [click, "q"]
    screen._consume_input_batch(app, window, curses, (), pending)
    assert applied[-1] == ("mouse", release) and list(window.values) == [click, "q"]


def test_pointer_wheel_budget_processes_every_count_without_dropping_opposite_direction(monkeypatch):
    mouse, applied = deque(), []
    monkeypatch.setattr(curses, "getmouse", mouse.popleft)
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    app = input_app(handle=applied.append)
    up = (0, 10, 9, 0, curses.BUTTON4_PRESSED)
    down = (0, 10, 9, 0, curses.BUTTON5_PRESSED)
    window = InputWindow([up] * 190 + [down] * 12 + ["q"], mouse)
    pending = screen._consume_input_batch(app, window, curses, (), ("mouse", up))
    assert applied == ["up"] * 191 + ["down"] * 12
    assert pending == ("q", None) and not window.values


def test_mouse_protocol_enables_hover_and_disables_all_modes_even_after_failure(monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    terminal = Terminal()
    monkeypatch.setattr(screen.sys, "stdout", terminal)
    monkeypatch.setattr(curses, "wrapper", lambda main: (_ for _ in ()).throw(RuntimeError("paint failed")))
    screen._mouse_reporting(True)
    with pytest.raises(RuntimeError, match="paint failed"):
        screen.run_curses(None, None, None, None, None, {})
    text = terminal.getvalue()
    formats_off = "\x1b[?1005l\x1b[?1015l\x1b[?1016l"
    assert text.startswith(formats_off)
    assert text.count(formats_off) == 2  # Start and exceptional shutdown.
    assert "\x1b[?1002h\x1b[?1003h\x1b[?1006h" in text
    assert "\x1b[?1003l\x1b[?1002l\x1b[?1000l\x1b[?1006l" in text
    assert text.endswith("\x1b[?2004l")


@pytest.mark.parametrize("code,ending,flag", [(0, "M", "BUTTON1_PRESSED"),
                                            (0, "m", "BUTTON1_RELEASED"),
                                            (32, "M", "BUTTON1_PRESSED"),
                                            (35, "M", "REPORT_MOUSE_POSITION"),
                                            (64, "M", "BUTTON4_PRESSED"),
                                            (65, "M", "BUTTON5_PRESSED"),
                                            (2, "M", "BUTTON3_PRESSED")])
def test_raw_sgr_reports_decode_on_legacy_screen_termcap_without_executing_bytes(code, ending, flag, monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    window = InputWindow(list(f"\x1b[<{code};350;120{ending}") + ["q"], deque())
    reader = screen._InputReader(window)
    event = reader.read(curses)
    assert event[0] == "mouse" and event[1][:4] == (0, 349, 119, 0)
    assert event[1][4] & getattr(curses, flag)
    if code == 32:
        assert event[1][4] & curses.REPORT_MOUSE_POSITION
    assert reader.read(curses) == ("q", None)


def test_raw_sgr_mouse_fragments_and_modifiers_preserve_following_command(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: clock[0])
    window = InputWindow(list("\x1b[<"), deque())
    reader = screen._InputReader(window)
    assert reader.read(curses) is None
    window.values.extend("36;31;9M")
    assert reader.read(curses) == ("mouse", (0, 30, 8, 0, curses.BUTTON1_PRESSED | curses.REPORT_MOUSE_POSITION | curses.BUTTON_SHIFT))
    window.values.extend("\x1b[<0;123")
    assert reader.read(curses) is None
    clock[0] = .1
    assert reader.read(curses) == (None, None)
    window.values.extend("q")
    assert reader.read(curses) == ("q", None)


@pytest.mark.parametrize("report", ["\x1b[<0;0;1M", "\x1b[<256;1;1M", "\x1b[<0;1;0m", "\x1b[<0;99999999999999999999999999999999999"])
def test_invalid_mouse_reports_never_replay_digits_as_commands(report, monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    window = InputWindow(list(report), deque())
    reader = screen._InputReader(window)
    result = reader.read(curses)
    assert result in ((None, None), ("mouse", None))
    assert not reader.queue
    if reader.discard_mouse:
        window.values.extend("Mq")
        assert reader.read(curses) == ("q", None)


def test_research_wheel_scrolls_content_without_changing_selected_job():
    app = input_app(tab="research", research_rows=200, research_scroll=50, research_job_id="7")
    assert scrolling.handle_wheel(app, 20, 30, -1)
    assert app.research_scroll == 47 and app.research_job_id == "7"
    app.research_scroll = 0
    for _ in range(500):
        assert scrolling.handle_wheel(app, 20, 30, -1)
    assert app.research_scroll == 0


def test_log_wheel_uses_only_published_buffer_and_exact_original_selection(tmp_path, monkeypatch):
    cfg = Config({"log_lines": 0, "animations": False})
    store = Store(persist=False)
    store.apply_jobs([Job("7", "training", "gpu", "RUNNING")])
    path = tmp_path / "log.txt"
    raw = [f"original_{index:04}\tpayload 界\r\n".encode() for index in range(300)]
    path.write_bytes(b"".join(raw))
    store.details["7"] = {"StdOut": str(path)}
    app = App(store, None, None, cfg, "test", interactive=False)
    views = Views(Glyphs(False), cfg)
    app.views_ref = views
    views.compose(store.snapshot(), app, 100, 24)
    app.open_log("7")
    views.compose(store.snapshot(), app, 100, 24)
    app.handle("home")
    app.handle("v")
    def unexpected(*args, **kwargs):
        raise AssertionError("wheel must not perform file IO or prepare a snapshot")
    monkeypatch.setattr(app, "prepare_log", unexpected)
    monkeypatch.setattr(views.files, "read", unexpected)
    monkeypatch.setattr(views.files, "stat", unexpected)
    for _ in range(10):
        assert scrolling.handle_wheel(app, 20, 30, 1)
    assert app.logs.cursor == app.logs.selection_end == 30
    assert app.logs.selection_bytes(app.logs.buffers[str(path)]) == b"".join(raw[:31])
    for _ in range(100):
        assert scrolling.handle_wheel(app, 20, 30, -1)
    assert app.logs.cursor == app.logs.selection_end == 0


def test_dispatch_distinguishes_passive_hover_held_drag_release_and_press(monkeypatch):
    from raven import toolbar
    from raven.ui import selection_state
    calls = []
    app = input_app(toolbar_state={}, job_selection_state={"capture": None},
                    click=lambda *args, **kwargs: calls.append(("click", args, kwargs)))
    monkeypatch.setattr(toolbar, "handle_mouse", lambda app, y, x, **kwargs: calls.append(("toolbar", y, x, kwargs)) or False)
    motion = (0, 30, 12, 0, curses.REPORT_MOUSE_POSITION)
    screen._apply_input(app, ("mouse", motion), (), curses)
    assert calls == [("toolbar", 12, 30, {"button": "motion", "shift": False})]
    monkeypatch.setattr(selection_state, "active", lambda app: True)
    for state, button in ((curses.BUTTON1_PRESSED, "press"),
                          (curses.REPORT_MOUSE_POSITION | curses.BUTTON1_PRESSED, "drag"),
                          (curses.BUTTON1_RELEASED, "release")):
        screen._apply_input(app, ("mouse", (0, 30, 12, 0, state)), (), curses)
        assert calls[-1] == ("click", (12, 30, ()), {"button": button, "shift": False})


def test_curses_redraw_budget_only_accelerates_while_viewport_motion_is_active(monkeypatch):
    cfg = Config({"log_lines": 0, "startup_animation": False})
    store = Store(persist=False)
    store.apply_jobs([Job(str(index), "training", "gpu", "RUNNING") for index in range(30)])
    app = App(store, None, None, cfg, "test")
    views = Views(Glyphs(False), cfg)
    mouse = deque()
    clock = [10.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(curses, "getmouse", mouse.popleft)
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    for name in ("raw", "curs_set", "mousemask", "mouseinterval", "doupdate", "set_escdelay"):
        monkeypatch.setattr(curses, name, lambda *args: None)
    wheel = (0, 10, 10, 0, curses.BUTTON5_PRESSED)

    class Window(InputWindow):
        def __init__(self):
            super().__init__([wheel, "q"], mouse)
            self.timeouts = []
        def timeout(self, value):
            self.timeouts.append(value)
        def getmaxyx(self):
            return 24, 100
        def keypad(self, value):
            pass
        def erase(self):
            pass
        def addstr(self, *args):
            pass
        def noutrefresh(self):
            pass
    window = Window()
    original = views.compose
    def compose(*args, **kwargs):
        clock[0] += .016
        scrolling.viewport(app, "budget-test", app.cursor["jobs"], 100, 10, now=clock[0])
        return original(*args, **kwargs)
    views.compose = compose
    monkeypatch.setattr(curses, "wrapper", lambda main: main(window))
    screen.run_curses(app, views, None, store, None, cfg)
    assert 17 in window.timeouts
    assert window.timeouts[0] == 200 and app.quit
    assert not scrolling.active(app)  # q is an immediate input, never delayed.
