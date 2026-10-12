"""Real input decoding preserves paste boundaries and leaves actions inert."""
from collections import deque
import curses

import pytest

from raven import screen
from raven.config import Config
from raven.controller import App
from raven.model import Store


class Window:
    def __init__(self, text=""):
        self.values = deque(text)
        self.keypad_calls = []
        self.timeouts = []

    def get_wch(self):
        if not self.values:
            raise curses.error("empty")
        return self.values.popleft()

    def keypad(self, enabled):
        self.keypad_calls.append(enabled)

    def timeout(self, value):
        self.timeouts.append(value)


@pytest.fixture(autouse=True)
def reset_readers():
    screen._INPUT_READERS.clear()
    yield
    screen._INPUT_READERS.clear()


def drain(window, count=100):
    for _ in range(count):
        event = screen._read_input(window, curses)
        if event is not None:
            return event
    raise AssertionError("No completed event")


def test_fragmented_multiline_paste_never_dispatches_payload_keys(monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    window = Window("\x1b[20")
    assert screen._read_input(window, curses) is None
    window.values.extend("0~log 123\nquit\n界\x1b[20")
    assert screen._read_input(window, curses) is None
    window.values.extend("1~")
    event = drain(window)
    assert event == ("paste", "log 123\nquit\n界")
    app = App(Store(persist=False), None, None, Config(), "test", interactive=False)
    screen._apply_input(app, event, [], curses)
    assert app.mode == "palette"
    assert app.palette_edit == "log 123\nquit\n界"
    assert not app.quit and app.tab == "jobs" and app.log_job is None
    assert window.keypad_calls == [False, True]


def test_oversized_paste_is_bounded_and_next_key_is_preserved(monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    window = Window("\x1b[200~" + "x" * 20000 + "\x1b[201~q")
    event = drain(window)
    assert event == ("paste", "x" * 4097)
    app = App(Store(persist=False), None, None, Config(), "test", interactive=False)
    screen._apply_input(app, event, [], curses)
    assert len(app.palette_edit) == 4096
    assert "truncated" in app.command_state["paste_notice"]
    assert drain(window) == ("q", None)


def test_control_characters_inside_paste_are_rejected_as_one_event(monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    window = Window("\x1b[200~quit\x1b[31m\n\x1b[201~")
    app = App(Store(persist=False), None, None, Config(), "test", interactive=False)
    screen._apply_input(app, drain(window), [], curses)
    assert app.mode == "main" and not app.quit
    assert not app.command_ok and "control characters" in app.message


@pytest.mark.parametrize("sequence,name", sorted(screen._ESCAPE_KEYS.items()))
def test_word_and_navigation_sequences_survive_fragmentation(sequence, name, monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    window = Window(sequence[:-1])
    assert screen._read_input(window, curses) is None
    window.values.append(sequence[-1])
    assert drain(window) == (name, None)


def test_bare_escape_expires_without_losing_the_following_key(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    window = Window("\x1b")
    assert screen._read_input(window, curses) is None
    now[0] = .04
    assert screen._read_input(window, curses) == ("esc", None)
    window.values.append("界")
    assert screen._read_input(window, curses) == ("界", None)


def test_mouse_setting_blocks_actions_but_keeps_terminal_probe_usable():
    app = App(Store(persist=False), None, None, Config({"mouse": False}), "test", interactive=False)
    calls = []
    app.click = lambda *args, **kwargs: calls.append((args, kwargs))
    event = ("mouse", (0, 3, 4, 0, curses.BUTTON1_PRESSED))
    screen._apply_input(app, event, [], curses)
    assert not calls
    app.mode = "terminal_probe"
    screen._apply_input(app, event, [], curses)
    assert calls


def test_probe_records_paste_size_without_executing_or_storing_payload():
    app = App(Store(persist=False), None, None, Config(), "test", interactive=False)
    app.run_command("terminaltest")
    screen._apply_input(app, ("paste", "quit\nlog 123"), [], curses)
    assert app.mode == "terminal_probe" and not app.quit
    assert app.session_tools_state["probe_events"][-1].endswith("paste (12 characters)")


@pytest.mark.parametrize("mode", ["main", "palette", "settings_editor", "confirm"])
def test_raw_terminal_interrupt_exits_without_running_a_hidden_action(mode):
    app = App(Store(persist=False), None, None, Config(), "test", interactive=False)
    app.mode = mode
    app.handle(screen.key_name("\x03", curses))
    assert app.quit and app.log_job is None


def test_terminal_probe_keeps_raw_interrupt_inert():
    app = App(Store(persist=False), None, None, Config(), "test", interactive=False)
    app.run_command("terminaltest")
    app.handle("ctrl-c")
    assert not app.quit and app.mode == "terminal_probe"


def test_keybinding_tester_keeps_raw_interrupt_inert():
    app = App(Store(persist=False), None, None, Config(), "test", interactive=False)
    app.run_command("keybindings")
    app.handle("t")
    app.handle("ctrl-c")
    assert not app.quit and app.mode == "bindings_editor"
    assert app.navigation_tools_state["test"] is False
    assert "ctrl-c" in app.navigation_tools_state["test_message"]


@pytest.mark.parametrize("mode", ["analysis", "session_alerts", "terminal_diagnostics", "session_inbox", "activity", "exports",
                                 "log_tools_page", "log_tools_results", "log_tools_marks", "project_preview", "export_preview",
                                 "table_tools", "layout", "columns"])
def test_readonly_modal_colon_opens_palette_and_restores_its_origin(mode):
    app = App(Store(persist=False), None, None, Config(), "test", interactive=False)
    app.mode = mode
    app.handle(":")
    assert app.mode == "palette" and app.command_state["origin_mode"] == mode
    app.handle("esc")
    assert app.mode == mode


def test_table_filter_editor_keeps_colon_as_literal_input():
    app = App(Store(persist=False), None, None, Config(), "test", interactive=False)
    app.run_command("filters jobs")
    app.table_tools_state["edit"] = {"field": "name", "text": "run"}
    app.handle(":")
    assert app.mode == "table_tools" and app.table_tools_state["edit"]["text"] == "run:"


def test_focused_header_explanation_is_reachable_through_palette():
    from raven.views import Views
    from raven.layout import Glyphs
    app = App(Store(persist=False), None, None, Config(), "test", interactive=False)
    app.run_command("headers jobs")
    views = Views(Glyphs(False), app.cfg)
    views.overlay(app.store.snapshot(), app, 120, 40)
    column = app.table_tools_state["header"]["column"]
    app.handle(":")
    for char in "explain":
        app.handle(char)
    app.handle("enter")
    assert app.command_ok and app.mode == "field_explanation"
    assert app.table_tools_state["header"]["column"] == column
    assert "Source: jobs" in app.navigation_tools_state["value"]


def test_terminal_probe_records_wheel_coordinates_without_navigation():
    app = App(Store(persist=False), None, None, Config({"mouse": False}), "test", interactive=False)
    app.run_command("terminaltest")
    screen._apply_input(app, ("mouse", (0, 17, 8, 0, curses.BUTTON4_PRESSED)), [], curses)
    assert app.session_tools_state["probe_events"][-1] == "Mouse: wheel-up x=17, y=8"
    assert app.cursor["jobs"] == 0 and not app.quit
