"""Embedded shell framing, destination identity, and cached-paint isolation."""
from collections import deque
import curses
from types import SimpleNamespace

import pytest

from tower import screen


class Window:
    def __init__(self, data=b""):
        self.values = deque(data)
        self.timeouts = []

    def getch(self):
        return self.values.popleft() if self.values else -1

    def keypad(self, enabled):
        pass

    def timeout(self, value):
        self.timeouts.append(value)


def drain(reader):
    result = []
    for _ in range(10000):
        value = reader.read(curses)
        if value is not None and value[0] is not None:
            result.append(value)
        if not reader.window.values and not reader.raw_values and not reader.queue:
            return result
    raise AssertionError("decoder did not drain its bounded input")


@pytest.fixture(autouse=True)
def stable_time(monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0.)


@pytest.mark.parametrize("sequence,expected", [
    (b"\x1b[97;5u", ("terminal-raw", "\x1b[97;5u")),
    (b"\x1bO1;7A", ("terminal-raw", "\x1bO1;7A")),
    (b"\x1b]52;c;cXlyZw==\x07", ("terminal-raw", "\x1b]52;c;cXlyZw==\x07")),
    (b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\", ("terminal-raw", "\x1b]10;rgb:ffff/ffff/ffff\x1b\\")),
    (b"\x1bP1$r0m\x1b\\", ("terminal-raw", "\x1bP1$r0m\x1b\\")),
    (b"\x1b_qrlygu\x1b\\", ("terminal-raw", "\x1b_qrlygu\x1b\\")),
    (b"\x1b[200~q\ny\x03\x1b[201~", ("paste", "q\ny\x03")),
    ("\x1b[200~a界é\x1b[201~".encode(), ("paste", "a界é")),
    (b"\x1b[1;5A", ("ctrl-up", None)),
    (b"\x1bQ", ("terminal-raw", "\x1bQ")),
])
def test_every_fragment_keeps_its_original_session(sequence, expected):
    for split in range(1, len(sequence)):
        owner = [("shell-a", 7)]
        window = Window(sequence[:split])
        reader = screen._InputReader(window)
        reader.input_owner = lambda: owner[0]
        first = drain(reader)
        assert first == []
        owner[0] = ("shell-b", 8)
        window.values.extend(sequence[split:] + b"q")
        found = drain(reader)
        assert found == [expected, ("q", None)]
        assert found[0].origin == ("shell-a", 7)
        assert found[1].origin == ("shell-b", 8)


@pytest.mark.parametrize("sequence", [b"\x1b]qyrg\x07", b"\x1bPqyrg\x1b\\", b"\x1b_qyrg\x1b\\",
                                      b"\x1b^qyrg\x1b\\", b"\x1bXqyrg\x1b\\"])
def test_control_string_payloads_never_become_tower_commands(sequence):
    assert drain(screen._InputReader(Window(sequence + b"x"))) == [("x", None)]


@pytest.mark.parametrize("prefix,suffix", [(b"\x1b]", b"\x07"), (b"\x1bP", b"\x1b\\")])
def test_oversized_control_string_is_quarantined_and_parser_recovers(prefix, suffix, monkeypatch):
    monkeypatch.setattr(screen, "_CONTROL_STRING_LIMIT", 32)
    reader = screen._InputReader(Window(prefix + b"qyrg" * 100 + suffix + b"x"))
    reader.input_owner = lambda: ("a", 1)
    assert drain(reader) == [("x", None)]
    assert not reader.control_string
    assert not reader.control_discard


def test_terminal_paste_exceeds_palette_limit_without_truncation():
    value = "a" * 12000 + "界\nq\ny"
    reader = screen._InputReader(Window(("\x1b[200~" + value + "\x1b[201~").encode()))
    reader.input_owner = lambda: ("a", 1)
    assert drain(reader) == [("paste", value)]


def test_oversized_terminal_paste_is_rejected_entirely_and_recovers(monkeypatch):
    monkeypatch.setattr(screen, "_TERMINAL_PASTE_LIMIT", 32)
    value = "界" * 12
    reader = screen._InputReader(Window(("\x1b[200~" + value + "\x1b[201~q").encode()))
    reader.input_owner = lambda: ("a", 1)
    assert drain(reader) == [("terminal-overflow", 36), ("q", None)]
    assert reader.text == []


@pytest.mark.parametrize("byte,key", [(0, "ctrl-space"), (28, "ctrl-\\"), (29, "ctrl-]"),
                                     (30, "ctrl-^"), (31, "ctrl-_")])
def test_remaining_control_keys_are_deliverable(byte, key):
    reader = screen._InputReader(Window(bytes([byte])))
    reader.input_owner = lambda: ("a", 1)
    assert drain(reader) == [(key, None)]


def test_partial_input_resize_preserves_session_ownership():
    owner = [("a", 1)]
    window = Window(b"\x1b[97;")
    reader = screen._InputReader(window)
    reader.input_owner = lambda: owner[0]
    assert drain(reader) == []
    owner[0] = ("b", 2)
    window.values.extend([curses.KEY_RESIZE, *b"5u"])
    found = drain(reader)
    assert found == [("resize", None), ("terminal-raw", "\x1b[97;5u")]
    assert all(event.origin == ("a", 1) for event in found)


def fake_terminal(monkeypatch, identity=("shell-a", 1)):
    from tower import terminal_ui
    app = SimpleNamespace(identity=identity, keys=[], events=[], errors=[], tower=[], mode="main", tab="jobs")
    app.fail = app.errors.append
    app.handle = app.tower.append
    monkeypatch.setattr(terminal_ui, "input_identity", lambda value: value.identity)
    monkeypatch.setattr(terminal_ui, "handle_key", lambda value, key: value.keys.append(key) or True)
    monkeypatch.setattr(terminal_ui, "handle_event", lambda value, event: value.events.append(event) or True)
    monkeypatch.setattr(terminal_ui, "owns_mouse", lambda *args: False)
    return app


@pytest.mark.parametrize("key", ["q", "y", "g", "u", ":", "esc", "ctrl-c", "up", "tab", "f10"])
def test_shell_owns_all_normal_tower_commands(monkeypatch, key):
    app = fake_terminal(monkeypatch)
    event = screen.InputEvent((key, None), origin=app.identity)
    screen._apply_input(app, event, [], curses)
    assert app.keys == [key]
    assert not app.tower


@pytest.mark.parametrize("value", [("q", None), ("paste", "dangerous\n"), ("terminal-raw", "\x1b[97u"),
                                    ("mouse", (0, 20, 10, 0, curses.BUTTON1_RELEASED))])
@pytest.mark.parametrize("old,current", [(("a", 1), ("b", 2)), (("a", 1), None), (None, ("a", 1)),
                                        (("a", 1), ("a", 2))])
def test_queued_events_are_never_redirected_after_focus_switch(monkeypatch, value, old, current):
    app = fake_terminal(monkeypatch, current)
    screen._apply_input(app, screen.InputEvent(value, origin=old), [], curses)
    assert not app.keys and not app.events and not app.tower


@pytest.mark.parametrize("kind,value,semantic", [
    ("paste", "a\n界", {"type": "paste", "text": "a\n界"}),
    ("terminal-raw", "\x1b[97u", {"type": "raw", "data": "\x1b[97u"}),
])
def test_framed_payload_reaches_semantic_broker_api(monkeypatch, kind, value, semantic):
    app = fake_terminal(monkeypatch)
    screen._apply_input(app, screen.InputEvent((kind, value), origin=app.identity), [], curses)
    assert app.events == [semantic]


def test_overflow_reports_no_partial_delivery(monkeypatch):
    app = fake_terminal(monkeypatch)
    screen._apply_input(app, screen.InputEvent(("terminal-overflow", 2000000), origin=app.identity), [], curses)
    assert "no paste bytes were sent" in app.errors[0]
    assert not app.events


def test_terminal_key_burst_does_not_invalidate_graph_document(monkeypatch):
    app = fake_terminal(monkeypatch)
    effects = screen._InputEffects()
    for key in ("q", "y", "g", "esc", "enter"):
        effects.record(app, screen.InputEvent((key, None), origin=app.identity), curses)
    assert not effects.document


@pytest.fixture
def pane_app(tmp_path):
    from tower import terminal_ui

    class Service:
        def __init__(self):
            self.events = []
            self.sessions = {"s": {"id": "s", "label": "local:test", "status": "running", "seq": 1,
                "lines": [], "modes": {"mouse": 1003}, "cursor": {"visible": False}}}
        def send(self, sid, event):
            self.events.append((sid, event))
            return True
        def snapshot(self, sid):
            return self.sessions[sid]
        def resize(self, *args):
            return True
        def request(self, *args, **kwargs):
            return True
        def poll(self):
            return False

    app = SimpleNamespace(tab="jobs", mode="main", width=120, height=40, cfg={"mouse": True},
                          state_dir=str(tmp_path), errors=[], messages=[])
    app.fail = app.errors.append
    app.say = app.messages.append
    state = terminal_ui.initialize(app)
    state.service = Service()
    state.active = "s"
    state.modes["jobs"] = "terminal"
    terminal_ui._focus(app, True)
    terminal_ui.render(app, 50, 20, x=65, y=6)
    state.service.events.clear()
    return app


@pytest.mark.parametrize("code,ending,action,button", [
    (0, "M", "press", 0), (1, "M", "press", 1), (2, "M", "press", 2),
    (0, "m", "release", 0), (1, "m", "release", 1), (2, "m", "release", 2),
    (32, "M", "move", 0), (33, "M", "move", 1), (34, "M", "move", 2),
])
def test_all_child_mouse_buttons_survive_screen_dispatch(pane_app, code, ending, action, button):
    from tower import terminal_ui
    app = pane_app
    rect = app.terminal_state.body_rect
    mouse = screen._mouse_report(code | 16, rect.x + 3, rect.y + 4, ending, curses)
    screen._apply_input(app, screen.InputEvent(("mouse", mouse), origin=terminal_ui.input_identity(app)), [], curses)
    sent = [event for _, event in app.terminal_state.service.events if event["type"] == "mouse"]
    assert len(sent) == 1
    assert sent[0]["action"] == action
    assert sent[0]["button"] == button
    assert sent[0]["x"] == 2 and sent[0]["y"] == 3
    assert sent[0]["ctrl"]


def test_terminal_mouse_samples_bypass_prepaint_endpoint_coalescing(pane_app):
    from tower import terminal_ui
    app = pane_app
    rect = app.terminal_state.body_rect
    report = f"\x1b[<35;{rect.x+2};{rect.y+2}M".encode()
    window = Window(report * 3)
    screen._bind_input(window, app)
    try:
        event = screen._drain_prepaint_motion(app, window, curses, [])
        assert event[0] == "mouse"
        assert not screen._batchable_input(app, event, curses)
        assert len(window.values) == 2 * len(report)
        assert not app.terminal_state.service.events
    finally:
        screen._INPUT_READERS.pop(id(window), None)


def test_terminal_capture_release_outside_cannot_activate_tower(pane_app):
    from tower import terminal_ui
    app = pane_app
    rect = app.terminal_state.body_rect
    press = screen._mouse_report(0, rect.x + 2, rect.y + 2, "M", curses)
    screen._apply_input(app, screen.InputEvent(("mouse", press), origin=terminal_ui.input_identity(app)), [], curses)
    assert app.terminal_state.capture
    release = screen._mouse_report(0, 2, 2, "m", curses)
    screen._apply_input(app, screen.InputEvent(("mouse", release), origin=terminal_ui.input_identity(app)), [], curses)
    assert app.terminal_state.capture is None
    assert app.tab == "jobs"
    assert app.terminal_state.service.events[-1][1]["action"] == "release"


def test_mouse_resize_rejects_old_coordinates_without_dropping_shell_keys(pane_app):
    from tower import terminal_ui
    app = pane_app
    rect = app.terminal_state.body_rect
    old_geometry = screen._terminal_geometry(app)
    origin = terminal_ui.input_identity(app)
    press = screen._mouse_report(0, rect.x + 2, rect.y + 2, "M", curses)
    app.width += 20
    terminal_ui.render(app, 70, 20, x=65, y=6)
    screen._apply_input(app, screen.InputEvent(("mouse", press), origin=origin, geometry=old_geometry), [], curses)
    assert not app.terminal_state.service.events
    screen._apply_input(app, screen.InputEvent(("q", None), origin=origin, geometry=old_geometry), [], curses)
    assert app.terminal_state.service.events == [("s", {"type": "key", "key": "q"})]


def test_pty_screen_updates_use_cached_document_and_leave_jobs_unchanged(pane_app, monkeypatch):
    from tower import terminal_ui, layout
    from tower.config import Config
    from tower.controller import App
    from tower.model import Store, Job
    from tower.views import Views

    cfg = Config({"animations": False, "startup_animation": False, "gpu_sampling": False})
    store = Store(persist=False)
    store.jobs = [Job("7", "research-job", "cpu", "RUNNING", cpus=4)]
    app = App(store, None, None, cfg, "test", interactive=False)
    service = pane_app.terminal_state.service
    state = app.terminal_state
    state.service, state.active = service, "s"
    state.modes["jobs"] = "terminal"
    terminal_ui._focus(app, True)
    views = Views(layout.Glyphs(False), cfg)
    app.views_ref = views
    cache = screen._FrameCache()
    cache.rebuild(app, views, store, None, 160, 45)
    original = [list(row) for row in cache.rows]
    service.sessions["s"] = {**service.sessions["s"], "seq": 2,
                            "lines": [[("terminal-only-output", "terminal")]]}
    assert terminal_ui.tick(app)
    def forbidden(*args, **kwargs):
        raise AssertionError("PTY damage rebuilt the Jobs/graph document")
    monkeypatch.setattr(views, "compose", forbidden)
    rows, _, _ = cache.feedback(app, views)
    assert "terminal-only-output" in "\n".join(layout.row_text(row) for row in rows)
    assert "research-job" in "\n".join(layout.row_text(row) for row in rows)
    assert cache.rows == original
