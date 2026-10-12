"""One byte decoder owns mouse framing, keyboard mappings, and UTF-8."""
from collections import deque
import curses

import pytest

from raven import screen


class Window:
    def __init__(self, data=b""):
        self.values = deque(data)
        self.keypad_calls = []
        self.timeouts = []

    def getch(self):
        return self.values.popleft() if self.values else -1

    def keypad(self, enabled):
        self.keypad_calls.append(enabled)

    def timeout(self, value):
        self.timeouts.append(value)


def events(reader, limit=100):
    found = []
    for _ in range(limit):
        event = reader.read(curses)
        if event is not None and event[0] is not None:
            found.append(event)
        if not reader.window.values and not reader.raw_values and not reader.queue:
            break
    return found


@pytest.fixture(autouse=True)
def stable_clock(monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0.)


@pytest.mark.parametrize("encoding", ["utf-8", "latin1"])
@pytest.mark.parametrize("x,y", [(120,75), (81,120), (190,24), (200,200), (24,81)])
def test_extended_x10_coordinates_never_leak_log_or_page_keys(encoding, x, y):
    data = "\x1b[M" + "".join(chr(value) for value in (67, x + 33, y + 33))
    reader = screen._InputReader(Window(data.encode(encoding) + b"q"))
    assert events(reader) == [("mouse", (0, x, y, 0, curses.REPORT_MOUSE_POSITION)), ("q", None)]
    assert reader.window.keypad_calls == [False]


@pytest.mark.parametrize("coordinate", [(120,75), (190,24), (200,200)])
def test_utf8_mouse_report_survives_every_byte_boundary(coordinate):
    x, y = coordinate
    data = ("\x1b[M" + "".join(chr(value) for value in (67, x + 33, y + 33))).encode()
    for split in range(1, len(data)):
        window = Window(data[:split])
        reader = screen._InputReader(window)
        assert not events(reader)
        window.values.extend(data[split:] + b"q")
        assert events(reader) == [("mouse", (0, x, y, 0, curses.REPORT_MOUSE_POSITION)), ("q", None)]


@pytest.mark.parametrize("text", ["界", "é", "🚀", "alpha界omega"])
def test_keyboard_utf8_survives_every_byte_boundary(text):
    data = text.encode()
    for split in range(1, len(data)):
        window = Window(data[:split])
        reader = screen._InputReader(window)
        found = events(reader)
        window.values.extend(data[split:])
        found.extend(events(reader))
        assert found == [(character, None) for character in text]
        assert window.keypad_calls == [False]


@pytest.mark.parametrize("sequence,name", sorted(screen._ESCAPE_KEYS.items()))
def test_raw_keyboard_controls_and_modifiers_preserve_existing_sequences(sequence, name):
    reader = screen._InputReader(Window(sequence.encode() + b"q"))
    assert events(reader) == [(name, None), ("q", None)]


def test_terminfo_capabilities_preserve_function_and_nonstandard_keyboard_keys(monkeypatch):
    bindings = {"kf24": b"\x1b[123~", "kcuu1": b"\x1b[99A", "kLFT5": b"\x1b[42D",
                "kbs": b"\x7f", "kmous": b"\x1b[M"}
    monkeypatch.setattr(curses, "tigetstr", bindings.get)
    reader = screen._InputReader(Window(b"\x1b[123~\x1b[99A\x1b[42D\x7fq"))
    assert events(reader) == [("f24", None), ("up", None), ("ctrl-left", None),
                              ("backspace", None), ("q", None)]
    assert "\x1b[M" not in reader.escape_keys


@pytest.mark.parametrize("prefix", [b"\x1b[M", b"\x1b[<35;", b"\x1b[200~a", b"\xe7"])
def test_resize_preserves_partial_mouse_paste_and_unicode_prefix(prefix):
    window = Window(prefix)
    reader = screen._InputReader(window)
    before = events(reader)
    window.values.append(curses.KEY_RESIZE)
    assert reader.read(curses) == ("resize", None)
    if prefix == b"\x1b[M":
        window.values.extend(b"Cl9q")
        expected = [("mouse", (0,75,24,0,curses.REPORT_MOUSE_POSITION)), ("q", None)]
    elif prefix.startswith(b"\x1b[<"):
        window.values.extend(b"121;76Mq")
        expected = [("mouse", (0,120,75,0,curses.REPORT_MOUSE_POSITION)), ("q", None)]
    elif prefix.startswith(b"\x1b[200"):
        window.values.extend("界\x1b[201~q".encode())
        expected = [("paste", "a界"), ("q", None)]
    else:
        window.values.extend(b"\x95\x8cq")
        expected = [("界", None), ("q", None)]
    assert before == []
    assert events(reader) == expected
    assert window.keypad_calls == [False]


def test_raw_c1_report_and_unicode_paste_remain_indivisible():
    reader = screen._InputReader(Window(b"\x9b<35;121;76M" + "\x1b[200~log\n界\x1b[201~q".encode()))
    assert events(reader) == [("mouse", (0,120,75,0,curses.REPORT_MOUSE_POSITION)),
                              ("paste", "log\n界"), ("q", None)]


def test_delayed_utf8_coordinate_never_expires_into_a_log_shortcut(monkeypatch):
    now = [0.]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    window = Window(b"\x1b[MC\xc2")
    reader = screen._InputReader(window)
    assert not events(reader)
    for gap in (.12, .3, 5.):
        now[0] = gap
        assert not events(reader)
        assert reader.pending
    window.values.extend(b"\x99lq")
    assert events(reader) == [("mouse", (0,120,75,0,curses.REPORT_MOUSE_POSITION)), ("q", None)]
    assert not reader.pending


def test_raw_eight_bit_final_coordinate_completes_before_the_following_key():
    window = Window(b"\x1b[MCx\xe9")
    reader = screen._InputReader(window)
    assert not events(reader)
    window.values.append(ord("q"))
    assert events(reader) == [("mouse", (0,87,200,0,curses.REPORT_MOUSE_POSITION)), ("q", None)]


@pytest.mark.parametrize("split", range(1, 7))
def test_every_slow_utf8_mouse_byte_boundary_preserves_the_following_log_key(split, monkeypatch):
    now = [0.]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    data = b"\x1b[MC\xc2\x99l"
    window = Window(data[:split])
    reader = screen._InputReader(window)
    assert not events(reader)
    now[0] = .3
    found = events(reader)
    assert found == ([("esc", None)] if split == 1 else [])
    window.values.extend(data[split:] + b"lq")
    assert events(reader) == [("mouse", (0,120,75,0,curses.REPORT_MOUSE_POSITION)),
                              ("l", None), ("q", None)]


@pytest.mark.parametrize("prefix", [b"\x1b", b"\x1bO", b"\x1bPX", b"\x1b[", b"\x1b[<35;121;", b"\x1b[MC",
                                     b"\x1b[MC\xc2", b"\xe7"])
def test_global_interrupt_escapes_every_partial_protocol_and_utf8_state(prefix, monkeypatch):
    monkeypatch.setattr(curses, "tigetstr", lambda capability: b"\x1bPXY" if capability == "kf10" else None)
    window = Window(prefix)
    reader = screen._InputReader(window)
    assert not events(reader)
    window.values.extend(b"\x03q")
    assert events(reader) == [("ctrl-c", None), ("q", None)]
    assert not reader.pending
    assert not reader.queue and not reader.raw_values


@pytest.mark.parametrize("state", ["discard_mouse", "discard_csi"])
def test_global_interrupt_escapes_malformed_report_quarantine(state):
    window = Window(b"\x03q")
    reader = screen._InputReader(window)
    setattr(reader, state, True)
    assert events(reader) == [("ctrl-c", None), ("q", None)]
    assert not reader.discard_mouse and not reader.discard_csi


def test_pasted_interrupt_stays_payload_even_beside_malformed_utf8():
    reader = screen._InputReader(Window(b"\x1b[200~log\xe7\x03\x1b[201~q"))
    assert events(reader) == [("paste", "log\udce7\x03"), ("q", None)]


@pytest.mark.parametrize("sequence,name", [(b"\x1bOA", "up"), (b"\x1bOP", "f1"), (b"\x1bPXY", "f10")])
@pytest.mark.parametrize("delay", [.12, .3])
def test_slow_ss3_and_custom_terminfo_keys_keep_their_prefix(sequence, name, delay, monkeypatch):
    now = [0.]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(curses, "tigetstr", lambda capability: b"\x1bPXY" if capability == "kf10" else None)
    window = Window(sequence[:-1])
    reader = screen._InputReader(window)
    assert not events(reader)
    now[0] = delay
    assert not events(reader)
    window.values.extend(sequence[-1:] + b"q")
    assert events(reader) == [(name, None), ("q", None)]


@pytest.mark.parametrize("prefix,invalid", [(b"\x1bO", b"l"), (b"\x1bPX", b"l")])
def test_unknown_suffix_after_a_known_key_prefix_never_replays_log_shortcuts(prefix, invalid, monkeypatch):
    monkeypatch.setattr(curses, "tigetstr", lambda capability: b"\x1bPXY" if capability == "kf10" else None)
    window = Window(prefix)
    reader = screen._InputReader(window)
    assert not events(reader)
    window.values.extend(invalid + b"q")
    assert events(reader) == [("q", None)]


def test_expired_keyboard_escape_preserves_the_real_log_files_key(monkeypatch):
    now = [0.]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    window = Window(b"\x1b")
    reader = screen._InputReader(window)
    assert not events(reader)
    now[0] = .04
    assert events(reader) == [("esc", None)]
    window.values.extend(b"Oq")
    assert events(reader) == [("O", None), ("q", None)]


@pytest.mark.parametrize("sequence", [b"\x1bO1;5A", b"\x1bO999;2l", b"\x1bO?7M"])
def test_unknown_parameterized_ss3_is_one_control_without_numeric_page_shortcuts(sequence):
    reader = screen._InputReader(Window(sequence + b"q"))
    assert events(reader) == [("q", None)]
    assert not reader.escape and not reader.discard_csi


def test_slow_parameterized_ss3_retains_bounded_prefix_and_resynchronizes_escape(monkeypatch):
    now = [0.]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    window = Window(b"\x1bO1;5")
    reader = screen._InputReader(window)
    assert not events(reader)
    now[0] = .3
    assert not events(reader)
    window.values.extend(b"A\x1bO1;\x1b[Aq")
    assert events(reader) == [("up", None), ("q", None)]
    window.values.extend(b"\x1bO" + b"1" * 100 + b";5Aq")
    assert events(reader) == [("q", None)]
    assert not reader.escape and not reader.discard_csi


def test_known_parameterized_terminfo_mapping_has_priority_over_ss3_quarantine(monkeypatch):
    monkeypatch.setattr(curses, "tigetstr", lambda capability: b"\x1bO1;5A" if capability == "kUP5" else None)
    reader = screen._InputReader(Window(b"\x1bO1;5Aq"))
    assert events(reader) == [("ctrl-up", None), ("q", None)]
