"""Negotiated frame boundaries preserve the existing input and painter owners."""
from collections import deque
import curses
import io

import pytest

from raven import screen
from raven.terminal_sync import BEGIN, END, QUERY, TerminalSync


class Terminal(io.StringIO):
    def isatty(self):
        return True


class Window:
    def __init__(self):
        self.values = deque()

    def getch(self):
        return self.values.popleft() if self.values else -1

    def keypad(self, enabled):
        pass

    def timeout(self, value):
        pass


def drain(reader):
    found = []
    for _ in range(100):
        item = reader.read(curses)
        if item is not None and item[0] is not None:
            found.append(item)
        if not reader.window.values and not reader.raw_values and not reader.queue:
            break
    return found


@pytest.mark.parametrize("response,enabled", [("0", False), ("1", True), ("2", True), ("3", False), ("4", False)])
def test_only_switchable_reported_capability_enables_frames(response, enabled):
    terminal = Terminal()
    sync = TerminalSync(terminal)
    sync.start()
    assert terminal.getvalue() == QUERY and not sync.enabled
    assert sync.receive("\x1b[?2026;" + response + "$y")
    assert sync.enabled is enabled
    with sync.frame():
        terminal.write("frame")
    assert terminal.getvalue() == QUERY + (BEGIN + "frame" + END if enabled else "frame")


@pytest.mark.parametrize("split", range(1, 12))
def test_fragmented_capability_reply_is_not_a_key_or_embedded_terminal_input(split, monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0.)
    terminal, window = Terminal(), Window()
    sync = TerminalSync(terminal)
    sync.start()
    reader = screen._InputReader(window)
    reader.control_response = sync.receive
    reader.input_owner = lambda: "embedded-shell"
    reply = b"\x1b[?2026;2$y"
    window.values.extend(reply[:split])
    events = drain(reader)
    window.values.extend(reply[split:] + b"q")
    events += drain(reader)
    assert events == [("q", None)] and sync.enabled
    assert events[0].origin == "embedded-shell"


def test_timeout_unknown_reply_and_off_keep_the_existing_painter(monkeypatch):
    now = [0.]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    terminal = Terminal()
    sync = TerminalSync(terminal)
    sync.start()
    assert not sync.receive("\x1b[?2004;2$y")
    now[0] = 2.
    with sync.frame():
        terminal.write("frame")
    assert terminal.getvalue() == QUERY + "frame"
    assert "no capability response" in sync.snapshot()["reason"]
    assert sync.receive("\x1b[?2026;2$y")
    assert not sync.enabled
    disabled = TerminalSync(terminal, "off")
    disabled.start()
    with disabled.frame():
        pass
    assert terminal.getvalue() == QUERY + "frame"


@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt, curses.error])
def test_frame_reset_on_error_and_close_does_not_duplicate_boundaries(exception):
    terminal = Terminal()
    sync = TerminalSync(terminal, "on")
    sync.start()
    with pytest.raises(exception):
        with sync.frame():
            terminal.write("partial")
            raise exception()
    sync.close()
    assert terminal.getvalue() == BEGIN + "partial" + END
    assert sync.frames == 1 and not sync.active


def test_nested_flush_and_unchanged_frames_do_not_add_control_traffic():
    terminal = Terminal()
    sync = TerminalSync(terminal, "on")
    sync.start()
    with sync.frame():
        with sync.frame():
            terminal.write("frame")
    for _ in range(100):
        with sync.frame(changed=False):
            pass
    assert terminal.getvalue() == BEGIN + "frame" + END
    assert sync.bytes == len(BEGIN + END) and sync.frames == 1


@pytest.mark.parametrize("mode", ["auto", "on", "off", "invalid"])
def test_noninteractive_output_never_receives_terminal_controls(mode):
    terminal = io.StringIO()
    sync = TerminalSync(terminal, mode)
    sync.start()
    with sync.frame():
        terminal.write("plain")
    sync.close()
    assert terminal.getvalue() == "plain"


def test_broken_terminal_falls_back_and_frame_callback_still_runs():
    class Broken(Terminal):
        def write(self, text):
            raise BrokenPipeError()
    sync = TerminalSync(Broken(), "on")
    sync.start()
    reached = []
    with sync.frame():
        reached.append(True)
    assert reached and not sync.enabled and not sync.active
    assert "unavailable" in sync.snapshot()["reason"]


@pytest.mark.parametrize("late", [False, True])
def test_duplicate_or_late_reply_stays_owned_under_embedded_shell(monkeypatch, late):
    now = [0.]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    sync, window = TerminalSync(Terminal()), Window()
    sync.start()
    if late:
        now[0] = 2.
    else:
        assert sync.receive("\x1b[?2026;2$y")
    reader = screen._InputReader(window)
    reader.control_response = sync.receive
    reader.input_owner = lambda: "embedded-shell"
    window.values.extend(b"\x1b[?2026;2$yq")
    assert drain(reader) == [("q", None)]
    assert sync.enabled is not late
