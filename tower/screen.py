"""Painters: the curses screen (interactive, with mouse and resize), the ANSI animated screen (--watch) and one
frame of text (--once) or JSON (--json)."""
from __future__ import annotations

import codecs
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from typing import Optional

from . import layout as L
from . import palette as P
from .model import to_plain
from .views import stdout_path
from .ui_trace import timed as _timed_ui

KEYNAMES = {}
CB_MAP = {"green": "blue", "red": "yellow", "yellow": "magenta"}       # colour-blind safe: blue / orange(yellow) / magenta instead of green / red / yellow
# A readable palette on modern terminals; basic terminals retain their native eight colours.
PALETTE_256 = {"green": 114, "yellow": 221, "red": 203, "cyan": 81, "magenta": 183, "blue": 75, "white": 252}
INPUT_BATCH_LIMIT = 32
INPUT_BATCH_SECONDS = .008
POINTER_BATCH_LIMIT = 256
POINTER_BATCH_SECONDS = .012
MAINTENANCE_SECONDS = .2
QUIET_DOCUMENT_SECONDS = 1.0
_UNREAD_PUBLICATION = object()
_NAVIGATION_ACTIONS = frozenset(("up", "down", "page_up", "page_down", "home", "end"))
_INPUT_READERS = {}
_ESCAPE_KEYS = {"\x1b[1;5D": "ctrl-left", "\x1b[1;5C": "ctrl-right",
                "\x1b[1;3D": "alt-left", "\x1b[1;3C": "alt-right",
                "\x1bb": "alt-b", "\x1bf": "alt-f", "\x1bd": "alt-d",
                "\x1bu": "alt-u", "\x1br": "alt-r", "\x1b\x7f": "alt-backspace",
                "\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left"}
_ESCAPE_KEYS.update({"\x1b[H": "home", "\x1b[F": "end", "\x1b[Z": "btab",
                     "\x1b[1~": "home", "\x1b[4~": "end", "\x1b[7~": "home", "\x1b[8~": "end",
                     "\x1b[3~": "delete", "\x1b[5~": "pgup", "\x1b[6~": "pgdn",
                     "\x1bOA": "up", "\x1bOB": "down", "\x1bOC": "right", "\x1bOD": "left",
                     "\x1bOH": "home", "\x1bOF": "end", "\x1bOP": "f1", "\x1bOQ": "f2",
                     "\x1bOR": "f3", "\x1bOS": "f4"})
_ESCAPE_KEYS.update({f"\x1b[{number}~": f"f{index}" for index, number in
                     enumerate((15, 17, 18, 19, 20, 21, 23, 24), 5)})
_PASTE_START, _PASTE_END = "\x1b[200~", "\x1b[201~"
_SGR_MOUSE = re.compile(r"\x1b\[<(\d{1,5});(\d{1,5});(\d{1,5})([Mm])\Z", re.ASCII)
_URXVT_MOUSE = re.compile(r"\x1b\[(\d{1,5});(\d{1,5});(\d{1,5})M\Z", re.ASCII)
_SGR_PREFIX = re.compile(r"\x1b\[<[0-9;]*\Z", re.ASCII)
_CSI_PREFIX = re.compile(r"\x1b\[[0-?]*[ -/]*\Z", re.ASCII)
_CSI_COMPLETE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]\Z", re.ASCII)
_SS3_PREFIX = re.compile(r"\x1bO[0-?]*[ -/]*\Z", re.ASCII)
_SS3_COMPLETE = re.compile(r"\x1bO[0-?]*[ -/]*[@-~]\Z", re.ASCII)
_ESCAPE_KEYS.update({"\x1b" + chr(code): "alt-" + chr(code) for code in range(ord("a"), ord("z") + 1)})
for _modifier, _number in (("alt", 3), ("ctrl", 5)):
    for _suffix, _key in (("A", "up"), ("B", "down"), ("C", "right"), ("D", "left"), ("H", "home"), ("F", "end")):
        _ESCAPE_KEYS[f"\x1b[1;{_number}{_suffix}"] = f"{_modifier}-{_key}"
    for _code, _key in ((3, "delete"), (5, "pgup"), (6, "pgdn")):
        _ESCAPE_KEYS[f"\x1b[{_code};{_number}~"] = f"{_modifier}-{_key}"
_ESCAPE_KEYS["\x1b[127;5u"] = "ctrl-backspace"
del _modifier, _number, _suffix, _key, _code
_ESCAPE_PREFIXES = frozenset(sequence[:end] for sequence in (*_ESCAPE_KEYS, _PASTE_START)
                             for end in range(1, len(sequence)))
_ESCAPE_SECONDS = .03
_POINTER_ESCAPE_SECONDS = .2
_RECENT_POINTER_SECONDS = .5
_TERMINAL_PASTE_LIMIT = 1024 * 1024
_CONTROL_STRING_LIMIT = 16384


class InputEvent(tuple):
    """An input frame bound to the focus owner when its first byte arrived.

    Keep the historical two-item interface for trace tools and compatibility
    controllers. ``origin=None`` is an explicit Tower owner, not permission to
    send a queued frame to whichever shell happens to be focused later.
    """

    def __new__(cls, values, *, origin=None, geometry=None):
        result = super().__new__(cls, values)
        result.origin = origin
        result.geometry = geometry
        return result


def _terminal_keys(curses):
    """Read keyboard capabilities without handing mouse framing to ncurses."""
    keys = dict(_ESCAPE_KEYS)
    capabilities = {"kcuu1": "up", "kcud1": "down", "kcub1": "left", "kcuf1": "right",
                    "khome": "home", "kend": "end", "kpp": "pgup", "knp": "pgdn",
                    "kdch1": "delete", "kich1": "insert", "kcbt": "btab", "kent": "enter",
                    "kbs": "backspace"}
    capabilities.update({f"kf{number}": f"f{number}" for number in range(1, 25)})
    for capability, name in (("kUP", "up"), ("kDN", "down"), ("kLFT", "left"),
                             ("kRIT", "right"), ("kHOM", "home"), ("kEND", "end"),
                             ("kPRV", "pgup"), ("kNXT", "pgdn"), ("kDC", "delete")):
        for number, modifier in ((3, "alt"), (5, "ctrl")):
            capabilities[capability + str(number)] = modifier + "-" + name
    for capability, name in capabilities.items():
        try:
            sequence = curses.tigetstr(capability)
        except (AttributeError, curses.error):
            continue
        if sequence:
            # Ignore mouse capabilities and ordinary printable characters.
            # Bracketed paste and reports retain their dedicated parsers.
            try:
                text = sequence.decode("ascii")
            except UnicodeDecodeError:
                continue
            if text.startswith("\x1b") and len(text) <= 64:
                keys[text] = name
    return keys


class _InputReader:
    """Own byte framing, including UTF-8/legacy mouse and terminal keyboards.

    Ncurses mouse decoding consumes three bytes of a legacy report even when
    a terminal sends UTF-8 coordinates. The remaining coordinate byte can be
    a page shortcut. Raw getch with keypad disabled prevents that split owner;
    UTF-8 decoding here is incremental and never waits inside get_wch.
    """
    def __init__(self, window):
        self.window = window
        self.raw = callable(getattr(window, "getch", None))
        self.raw_values = deque()
        self.utf8 = codecs.getincrementaldecoder("utf-8")("surrogateescape")
        self.escape_keys = _ESCAPE_KEYS
        self.escape_prefixes = _ESCAPE_PREFIXES
        self.configured = False
        if self.raw:
            window.keypad(False)
        self.escape = ""
        self.escape_time = 0.0
        self.pasting = False
        self.end = ""
        self.text = []
        self.queue = deque()
        self.discard_mouse = False
        self.discard_csi = False
        self.after_escape = False
        self.recovered_escape = False
        self.protect_escape = False
        self.mouse_time = None
        self.escape_grace = _ESCAPE_SECONDS
        self.timed_out_mouse = False
        self.input_owner = None
        self.geometry_owner = None
        self.frame_origin = None
        self.frame_geometry = None
        self.frame_active = False
        self.control_string = ""
        self.control_discard = False
        self.control_escape = False
        self.paste_overflow = False
        self.paste_bytes = 0

    def _value(self, curses):
        if not self.raw:
            return self.window.get_wch()
        if not self.configured:
            self.escape_keys = _terminal_keys(curses)
            self.escape_prefixes = frozenset(sequence[:end] for sequence in (*self.escape_keys, _PASTE_START)
                                             for end in range(1, len(sequence)))
            self.configured = True
        deadline = time.monotonic() + INPUT_BATCH_SECONDS
        for _ in range(256):
            if self.raw_values:
                char = self.raw_values.popleft()
                # Surrogateescape keeps malformed UTF-8 bytes intact. Legacy
                # X10 coordinates may legitimately be arbitrary 8-bit values.
                if isinstance(char, str) and 0xdc80 <= ord(char) <= 0xdcff and self.escape.startswith("\x1b[M"):
                    return chr(ord(char) - 0xdc00)
                return char
            value = self.window.getch()
            if value == -1:
                # A UTF-8 coordinate prefix has at most three pending bytes.
                # Keep it until more input arrives: flushing it on a timer
                # could release an ASCII coordinate suffix as a page shortcut.
                raise curses.error("no input")
            if value == 3 and not self.pasting:
                # Interrupt is not a valid byte in any mouse report. Deliver
                # it before UTF-8 error recovery can queue an older high byte.
                self.utf8.reset()
                self.raw_values.clear()
                return "\x03"
            if value > 255:
                # Resize is still delivered by ncurses with keypad disabled.
                # Do not let it consume a partial multibyte keyboard character.
                return value
            pending_utf8 = self.utf8.getstate()[0]
            if value < 128 and not pending_utf8:
                return chr(value)
            if value == 0x9b and not pending_utf8 and not self.escape.startswith("\x1b[M"):
                return "\x9b"
            self.raw_values.extend(self.utf8.decode(bytes((value,))))
            if self.utf8.getstate()[0]:
                # Only the first byte read may use the frame's idle timeout.
                # A fragmented UTF-8 character must not block a whole frame.
                self.window.timeout(0)
            if time.monotonic() >= deadline and not self.raw_values:
                raise curses.error("pending UTF-8")
        raise curses.error("input budget")

    @property
    def pending(self):
        return bool(self.escape or self.pasting or self.control_string or self.control_discard or self.utf8.getstate()[0])

    def _mouse_result(self, report):
        if report is not None:
            self.mouse_time = time.monotonic()
        self.timed_out_mouse = False
        return "mouse", report

    def read(self, curses):
        if not self.frame_active:
            self.frame_origin = self.input_owner() if callable(self.input_owner) else None
            self.frame_geometry = self.geometry_owner() if callable(self.geometry_owner) else None
            self.frame_active = True
        result = self._read(curses)
        if result is not None:
            framed = InputEvent(result, origin=self.frame_origin, geometry=self.frame_geometry)
            # Quarantined/partial controls return an inert tuple. Keep their
            # original owner until the complete sequence has been consumed.
            if not self.pending and not self.queue:
                self.frame_active = False
            return framed
        if not self.pending and not self.queue:
            self.frame_active = False
        return None

    def _read(self, curses):
        if self.queue:
            return self.queue.popleft()
        deadline = time.monotonic() + INPUT_BATCH_SECONDS if self.pending or self.discard_mouse or self.discard_csi else None
        for _ in range(4096 if self.pasting else 256):
            try:
                value = self._value(curses)
            except curses.error:
                elapsed = time.monotonic() - self.escape_time if self.escape else 0.0
                if self.escape and elapsed >= _ESCAPE_SECONDS:
                    pending = self.escape
                    if pending == "\x1b" and elapsed < self.escape_grace:
                        return None
                    if pending.startswith("\x1b[<"):
                        # A fragmented report remains bounded and may resume
                        # after a slow terminal/SSH packet. Dropping the prefix
                        # here loses pointer updates and makes hover lag.
                        self.timed_out_mouse = True
                        return None, None
                    if (pending.startswith(("\x1b[", "\x1bO")) or
                            len(pending) > 1 and pending in self.escape_prefixes):
                        # Retain bounded report and terminfo key prefixes.
                        # Replaying an SS3 prefix would turn its 'O' into the
                        # log-files shortcut before a delayed arrow arrives.
                        return None, None
                    self.escape = ""
                    self.after_escape = pending == "\x1b"
                    self.recovered_escape = False
                    self.timed_out_mouse = False
                    self.queue.extend((key_name(ch, curses), None) for ch in pending[1:])
                    return "esc", None
                return None
            if value == "\x03" and not self.pasting:
                # A corrupt report must not trap the global interrupt behind
                # its quarantine. Pasted control bytes remain inert payload.
                self.escape = ""
                self.after_escape = self.recovered_escape = False
                self.discard_mouse = self.discard_csi = False
                self.timed_out_mouse = False
                self.control_string, self.control_discard, self.control_escape = "", False, False
                self.utf8.reset()
                self.raw_values.clear()
                self.queue.clear()
                return "ctrl-c", None
            if self.raw and value == getattr(curses, "KEY_RESIZE", None):
                # A signal can interrupt any byte position, including paste
                # or a UTF-8 coordinate. Preserve the prefix across resize.
                return "resize", None
            if self.control_string or self.control_discard:
                # OSC/DCS/APC/PM are one opaque frame, including printable
                # payloads. No payload character may become a Tower shortcut.
                char = value if isinstance(value, str) else ""
                complete = char == "\x07" and self.control_string.startswith("\x1b]") or self.control_escape and char == "\\"
                if not self.control_discard:
                    self.control_string += char
                    if len(self.control_string) > _CONTROL_STRING_LIMIT:
                        self.control_discard = True
                self.control_escape = char == "\x1b"
                if complete:
                    sequence, discarded = self.control_string, self.control_discard
                    self.control_string, self.control_discard, self.control_escape = "", False, False
                    return ("terminal-raw", sequence) if self.frame_origin is not None and not discarded else (None, None)
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                continue
            if isinstance(value, int) and not self.pasting:
                # Ncurses can resume decoding between fragments. A complete
                # native key/mouse event resolves an older partial raw prefix
                # instead of being swallowed into that prefix as empty text.
                name = key_name(value, curses)
                if name is not None:
                    self.escape = ""
                    self.after_escape = self.recovered_escape = False
                    self.discard_mouse = self.discard_csi = False
                    self.timed_out_mouse = False
                    if name == "mouse":
                        try:
                            return self._mouse_result(curses.getmouse())
                        except curses.error:
                            return None, None
                    return name, None
            if value == "\x9b" and not self.pasting and not self.escape.startswith("\x1b[M"):
                # UTF-8 terminals may deliver the C1 CSI introducer as one
                # character rather than the equivalent two bytes Escape '['.
                self.escape, self.escape_time = "\x1b[", time.monotonic()
                self.after_escape = self.recovered_escape = False
                self.discard_mouse = self.discard_csi = False
                self.timed_out_mouse = False
                deadline = self.escape_time + INPUT_BATCH_SECONDS
                self.window.timeout(0)
                continue
            if self.after_escape:
                # Bare Escape remains responsive, but it may have been the
                # first packet of a delayed mouse report. Recover its framing
                # before '[' can become the previous-tab shortcut.
                self.after_escape = False
                if value == "[":
                    self.escape, self.escape_time = "\x1b[", time.monotonic()
                    self.recovered_escape = True
                    deadline = self.escape_time + INPUT_BATCH_SECONDS
                    self.window.timeout(0)
                    continue
            if self.discard_csi:
                if value == "\x1b":
                    self.discard_csi = False
                    self.escape, self.escape_time = value, time.monotonic()
                elif isinstance(value, str) and "@" <= value <= "~":
                    self.discard_csi = False
                continue
            if self.discard_mouse:
                if value in ("M", "m"):
                    self.discard_mouse = False
                elif value == "\x1b":
                    self.discard_mouse = False
                    self.escape, self.escape_time = "\x1b", time.monotonic()
            elif self.pasting:
                # keypad is disabled so the end delimiter and pasted control
                # characters reach this parser in their original order.
                char = value if isinstance(value, str) else ""
                self.end += char
                while self.end and not _PASTE_END.startswith(self.end):
                    char = self.end[0]
                    if self.frame_origin is not None:
                        self.paste_bytes += len(char.encode("utf-8", "surrogateescape"))
                        if self.paste_bytes <= _TERMINAL_PASTE_LIMIT:
                            self.text.append(char)
                        else:
                            self.paste_overflow = True
                            self.text.clear()
                    elif len(self.text) < 4097:
                        self.text.append(char)
                    self.end = self.end[1:]
                if self.end == _PASTE_END:
                    self.pasting, self.end = False, ""
                    if not self.raw:
                        self.window.keypad(True)
                    text, self.text = "".join(self.text), []
                    if self.paste_overflow:
                        self.paste_overflow = False
                        return "terminal-overflow", self.paste_bytes
                    return "paste", text
            elif self.escape:
                char = value if isinstance(value, str) else ""
                candidate = self.escape + char
                if candidate.startswith("\x1b[") and len(candidate) > 2 and candidate[2].isdigit():
                    self.recovered_escape = False
                if char == "\x1b":
                    self.escape, self.escape_time = char, time.monotonic()
                    self.recovered_escape = False
                    self.timed_out_mouse = False
                elif (candidate in ("\x1b]", "\x1bP", "\x1b_", "\x1b^", "\x1bX")
                      and candidate not in self.escape_prefixes and candidate not in self.escape_keys):
                    self.escape, self.control_string = "", candidate
                    self.control_escape, self.control_discard = False, False
                elif candidate.startswith("\x1b[M"):
                    self.recovered_escape = False
                    # Legacy X10 reports encode three raw characters. A
                    # coordinate such as 'r' is data, never a keyboard shortcut.
                    if len(candidate) < 6:
                        self.escape = candidate
                    else:
                        self.escape = ""
                        return self._mouse_result(_x10_mouse(candidate, curses))
                elif candidate.startswith("\x1b[<"):
                    self.recovered_escape = False
                    # Some tmux/screen terminfo entries advertise legacy X10
                    # input even though the terminal supports requested SGR.
                    # Decode fragmented reports without executing their bytes.
                    match = _SGR_MOUSE.fullmatch(candidate)
                    if match:
                        self.escape = ""
                        return self._mouse_result(_sgr_mouse(match, curses))
                    if len(candidate) <= 23 and _SGR_PREFIX.fullmatch(candidate):
                        self.escape = candidate
                    else:
                        self.escape = ""
                        if self.timed_out_mouse and char and char not in "0123456789;Mm":
                            # A nonreport key after an abandoned, timed-out
                            # numeric prefix is still usable (especially Quit).
                            self.timed_out_mouse = False
                            return key_name(value, curses), None
                        self.timed_out_mouse = False
                        self.discard_mouse = not candidate.endswith(("M", "m"))
                        return None, None
                elif candidate == _PASTE_START:
                    self.escape, self.pasting = "", True
                    self.text, self.end = [], ""
                    self.paste_bytes, self.paste_overflow = 0, False
                    if not self.raw:
                        self.window.keypad(False)
                elif candidate in self.escape_keys:
                    self.escape = ""
                    self.recovered_escape = False
                    return self.escape_keys[candidate], None
                elif candidate in self.escape_prefixes:
                    self.escape = candidate
                elif _URXVT_MOUSE.fullmatch(candidate):
                    self.escape = ""
                    self.recovered_escape = False
                    match = _URXVT_MOUSE.fullmatch(candidate)
                    code, x, y = (int(match.group(index)) for index in (1, 2, 3))
                    return self._mouse_result(_mouse_report(code - 32, x, y, "M", curses))
                elif _CSI_COMPLETE.fullmatch(candidate):
                    # Unknown terminal control sequences are indivisible;
                    # replaying their bytes can activate unrelated shortcuts.
                    self.escape = ""
                    if self.frame_origin is not None:
                        self.recovered_escape = False
                        return "terminal-raw", candidate
                    if self.recovered_escape:
                        # Escape followed by literal '[' and an ordinary key
                        # has no mouse framing. Preserve both user inputs.
                        self.recovered_escape = False
                        self.queue.extend((key_name(ch, curses), None) for ch in candidate[2:])
                        return "[", None
                    return None, None
                elif _SS3_COMPLETE.fullmatch(candidate):
                    self.escape = ""
                    return ("terminal-raw", candidate) if self.frame_origin is not None else (None, None)
                elif len(candidate) <= 64 and (_CSI_PREFIX.fullmatch(candidate) or _SS3_PREFIX.fullmatch(candidate)):
                    self.escape = candidate
                    if char.isdigit():
                        # A numeric CSI may be a urxvt mouse report; its bytes
                        # must never be replayed as tab-number shortcuts.
                        self.recovered_escape = False
                elif candidate.startswith(("\x1b[", "\x1bO")):
                    self.escape = ""
                    self.discard_csi = not (char and "@" <= char <= "~")
                    return None, None
                elif len(self.escape) > 1 and self.escape in self.escape_prefixes:
                    # An unsupported suffix after a framed SS3/custom key is
                    # one invalid control, not a series of keyboard shortcuts.
                    self.escape = ""
                    return None, None
                else:
                    self.escape = ""
                    if self.frame_origin is not None:
                        return "terminal-raw", candidate
                    self.queue.extend((key_name(ch, curses), None) for ch in candidate[1:])
                    return "esc", None
            else:
                name = key_name(value, curses)
                if name == "esc":
                    self.escape, self.escape_time = "\x1b", time.monotonic()
                    self.timed_out_mouse = False
                    pointer_recent = self.mouse_time is not None and self.escape_time - self.mouse_time <= _RECENT_POINTER_SECONDS
                    self.escape_grace = (_POINTER_ESCAPE_SECONDS if self.protect_escape or pointer_recent
                                         else _ESCAPE_SECONDS)
                    deadline = self.escape_time + INPUT_BATCH_SECONDS
                    self.window.timeout(0)
                elif name == "mouse":
                    try:
                        return self._mouse_result(curses.getmouse())
                    except curses.error:
                        return None, None
                else:
                    if name is None and self.frame_origin is not None and isinstance(value, str):
                        if value == "\x00":
                            return "ctrl-space", None
                        return "terminal-raw", value
                    return name, None
            if deadline is not None and time.monotonic() >= deadline:
                return None
        return None


class MouseReport(tuple):
    """Curses-compatible report with explicit held-button evidence when known.

    Native curses tuples cannot distinguish omitted button bits from a lost
    release. Decoded terminal protocols can, without changing the tuple API.
    """

    def __new__(cls, values, *, held=None):
        result = super().__new__(cls, values)
        result.held = held
        return result


def _sgr_mouse(match, curses):
    code, x, y = (int(match.group(index)) for index in (1, 2, 3))
    return _mouse_report(code, x, y, match.group(4), curses)


def _x10_mouse(report, curses):
    if len(report) != 6 or any(ord(char) < 32 for char in report[3:]):
        return None
    code, x, y = (ord(char) - 32 for char in report[3:])
    return _mouse_report(code, x, y, "M", curses)


def _mouse_report(code, x, y, ending, curses):
    if code < 0 or code > 127 or x < 1 or y < 1:
        return None
    button = code & 3
    modifiers = sum(getattr(curses, flag, 0) for bit, flag in
                    ((4, "BUTTON_SHIFT"), (8, "BUTTON_ALT"), (16, "BUTTON_CTRL")) if code & bit)
    if code & 64:
        state = getattr(curses, "BUTTON4_PRESSED" if button == 0 else "BUTTON5_PRESSED", 0) if button < 2 else 0
    elif ending == "m" or button == 3 and not code & 32:
        state = getattr(curses, f"BUTTON{button + 1 if button < 3 else 1}_RELEASED", 0)
    else:
        state = getattr(curses, f"BUTTON{button + 1}_PRESSED", 0) if button < 3 else 0
        if code & 32:
            state |= getattr(curses, "REPORT_MOUSE_POSITION", 0)
    held = None if code & 64 else ending != "m" and button < 3
    return MouseReport((0, x - 1, y - 1, 0, state | modifiers), held=held)


def style_attr(style: str, theme: str, base: dict, colors: dict, bold: int) -> int:
    """A '+'-joined style -> a curses attribute under the theme.  mono and reader use no colour (red / yellow become
    bold); high adds bold to every colour; cb remaps green / red / yellow to blue / yellow / magenta."""
    a = 0
    plain = theme in ("mono", "reader")
    for s in style.split("+"):
        if theme == "cb" and s in CB_MAP:
            s = CB_MAP[s]
        if s in base and (s != "sel" or plain or "sel" not in colors):
            a |= base[s]
        elif not plain and s in colors:
            a |= colors[s] | (bold if theme == "high" else 0)
        elif plain and s in ("red", "yellow", "magenta"):
            a |= bold
    return a


class CursesPalette:
    """Lazy, bounded colour pairs; a theme's initialized pairs stay stable.

    Reusing pair numbers for new gradients changes already painted cells. When
    a small terminal exhausts its pair table, approximate with the nearest
    existing pair instead. Styles and quantised colours are cached across frames.
    A theme change reseeds a new instance and invalidates the complete screen.
    """

    def __init__(self, curses, enabled: bool = True, theme: str = "default"):
        self.curses = curses
        self.theme = P.canonical_theme(theme)
        self.enabled = enabled and not P.colors_disabled() and curses.has_colors()
        self.background = -1
        self.count = self.limit = 0
        self.pairs: dict[tuple[int, int], int] = {}
        self.approximations: dict[tuple[int, int], int] = {}
        self.attributes: dict[tuple[str, str], int] = {}
        if not self.enabled:
            return
        try:
            curses.start_color()
            self.count = (1 << 24) if curses.COLORS >= (1 << 24) else (256 if curses.COLORS >= 256 else min(8, curses.COLORS))
            self.limit = max(0, min(curses.COLOR_PAIRS - 1, 1024))
            try:
                curses.use_default_colors()
            except curses.error:
                self.background = curses.COLOR_BLACK
            if self.count < 8 or not self.limit:
                self.enabled = False
                return
            # Reserve essential text and selection tones before gradient charts.
            for style in ("white", "sel", "cursor", "cyan", "green", "yellow", "red", "magenta", "blue"):
                parsed = P.resolve(P.cell_style(style, self.theme), self.theme)
                self._pair(self._index(parsed.foreground), self._index(parsed.background, background=True))
            if self.limit >= 15:
                # Preserve opaque menus, info bars and slider surfaces before
                # charts can saturate a small terminal's remaining pair table.
                for style in ("text+bg:surface", "text+bg:surface-raised",
                              "accent+bg:surface-raised", "muted+bg:surface",
                              "border+bg:surface", "accent+bg:surface-sunken",
                              "track+bg:surface-sunken"):
                    parsed = P.resolve(P.cell_style(style, self.theme), self.theme)
                    self._pair(self._index(parsed.foreground), self._index(parsed.background, background=True))
            if not self.pairs:
                self.enabled = False
        except curses.error:
            self.enabled = False

    def _index(self, color, background=False):
        if color is None:
            if background or self.background == -1:
                return self.background
            color = P.rgb(P.theme_tokens(self.theme)["white"])
        if self.count >= (1 << 24):
            return (color[0] << 16) | (color[1] << 8) | color[2]
        return P.color_index(color, self.count, background=background)

    def _rgb(self, index, default):
        if index < 0:
            return P.rgb(P.theme_tokens(self.theme)[default])
        if self.count >= (1 << 24):
            return ((index >> 16) & 255, (index >> 8) & 255, index & 255)
        return P.INDEXED[index]

    def _pair(self, foreground: int, background: int) -> int:
        key = (foreground, background)
        if key in self.pairs:
            return self.pairs[key]
        if key in self.approximations:
            return self.approximations[key]
        c = self.curses
        if len(self.pairs) < self.limit:
            number = len(self.pairs) + 1
            try:
                c.init_pair(number, foreground, background)
                result = c.color_pair(number)
                self.pairs[key] = result
                return result
            except (c.error, OverflowError, ValueError):
                # Some curses builds expose more pairs than init_pair supports.
                self.limit = len(self.pairs)
        if not self.pairs:
            return 0

        def distance(existing):
            fg, bg = existing
            a, b = self._rgb(foreground, "white"), self._rgb(fg, "white")
            x, y = self._rgb(background, "black"), self._rgb(bg, "black")
            return sum((v - w) ** 2 for v, w in zip(a, b)) + 2 * sum((v - w) ** 2 for v, w in zip(x, y))

        result = self.pairs[min(self.pairs, key=distance)]
        if len(self.approximations) >= 4096:
            self.approximations.clear()
        self.approximations[key] = result
        return result

    def attr(self, style: str, theme: str = "default") -> int:
        key = (style, theme)
        if key in self.attributes:
            return self.attributes[key]
        c = self.curses
        parsed = P.resolve(style, theme if self.enabled else "mono")
        result = 0
        flags = {"bold": c.A_BOLD, "dim": c.A_DIM, "rev": c.A_REVERSE, "under": c.A_UNDERLINE}
        for flag in parsed.flags:
            result |= flags[flag]
        if self.enabled and (parsed.foreground is not None or parsed.background is not None):
            result |= self._pair(self._index(parsed.foreground), self._index(parsed.background, background=True))
            if "sel" in style.split("+"):
                canvas = P.resolve(P.cell_style("", theme), theme)
                indistinct = (parsed.background is not None and canvas.background is not None and
                               self._index(parsed.background, background=True) ==
                               self._index(canvas.background, background=True))
                if self.limit < 2 or indistinct:
                    result |= c.A_REVERSE
        # Keep plugin-generated styles from growing memory indefinitely.
        if len(self.attributes) >= 4096:
            self.attributes.clear()
        self.attributes[key] = result
        return result


def key_name(ch, curses) -> Optional[str]:
    """A wide character or curses key code -> the controller's key name."""
    if isinstance(ch, str):
        if len(ch) != 1:
            return None
        if ord(ch) > 127:
            return ch if ch.isprintable() else None
        ch = ord(ch)
    if ch == -1:
        return None
    if ch == curses.KEY_RESIZE:
        return "resize"
    if ch == curses.KEY_MOUSE:
        return "mouse"
    table = {curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right", curses.KEY_NPAGE: "pgdn", curses.KEY_PPAGE: "pgup",
             curses.KEY_HOME: "home", curses.KEY_END: "end", curses.KEY_BTAB: "btab", curses.KEY_ENTER: "enter", curses.KEY_BACKSPACE: "backspace",
             9: "tab", 10: "enter", 13: "enter", 27: "esc", 32: "space", 127: "backspace", 8: "backspace"}
    if ch in table:
        return table[ch]
    if ch in (28, 29, 30, 31):
        return {28: "ctrl-\\", 29: "ctrl-]", 30: "ctrl-^", 31: "ctrl-_"}[ch]
    if ch in (1, 2, 5, 7, 11, 16, 21, 23, 25, 26):
        return {1: "ctrl-a", 2: "ctrl-b", 5: "ctrl-e", 7: "ctrl-g", 11: "ctrl-k",
                16: "ctrl-p", 21: "ctrl-u", 23: "ctrl-w", 25: "ctrl-y", 26: "ctrl-z"}[ch]
    if 1 <= ch <= 26:
        return "ctrl-" + chr(ord("a") + ch - 1)
    if getattr(curses, "KEY_F0", 100000) < ch <= getattr(curses, "KEY_F0", 100000) + 24:
        return f"f{ch - curses.KEY_F0}"
    if hasattr(curses, "KEY_DC") and ch == curses.KEY_DC:
        return "delete"
    if 33 <= ch < 127:
        return chr(ch)
    if ch > 127:
        try:
            name = {b"kLFT3": "alt-left", b"kRIT3": "alt-right", b"kLFT5": "ctrl-left", b"kRIT5": "ctrl-right"}.get(curses.keyname(ch))
            if name is None:
                encoded = curses.keyname(ch).decode("ascii", "replace")
                for prefix, key in (("kUP", "up"), ("kDN", "down"), ("kHOM", "home"), ("kEND", "end"),
                                    ("kPRV", "pgup"), ("kNXT", "pgdn"), ("kDC", "delete")):
                    if encoded in (prefix + "3", prefix + "5"):
                        name = ("alt-" if encoded.endswith("3") else "ctrl-") + key
                        break
            if name:
                return name
        except (curses.error, ValueError):
            pass
    return None


def _read_input(stdscr, curses):
    reader = _INPUT_READERS.get(id(stdscr))
    if reader is None or reader.window is not stdscr:
        if len(_INPUT_READERS) >= 128:
            _INPUT_READERS.pop(next(iter(_INPUT_READERS)))
        reader = _INPUT_READERS[id(stdscr)] = _InputReader(stdscr)
    return reader.read(curses)


def _bind_input(stdscr, app):
    """Attach live ownership without changing the public reader test API."""
    reader = _INPUT_READERS.get(id(stdscr))
    if reader is None or reader.window is not stdscr:
        reader = _INPUT_READERS[id(stdscr)] = _InputReader(stdscr)
    from .terminal_ui import input_identity
    reader.input_owner = lambda: input_identity(app)
    reader.geometry_owner = lambda: _terminal_geometry(app)
    return reader


def _terminal_geometry(app):
    state = getattr(app, "terminal_state", None)
    rect = getattr(state, "body_rect", None)
    if rect is None:
        return None
    return (getattr(app, "width", None), getattr(app, "height", None), rect)


def _terminal_pointer(app, event):
    if event[0] != "mouse" or event[1] is None:
        return False
    from .pointer_routing import active
    if active(app) and not getattr(getattr(app, "terminal_state", None), "capture", None):
        return False
    from .terminal_ui import owns_mouse
    return owns_mouse(app, event[1][2], event[1][1])


def _protect_pointer_escape(app):
    """Allow a short report-prefix grace while a pointer gesture owns input."""
    from .pointer_routing import active
    return active(app)


def _navigation_context(app):
    """A changed document needs a new frame before another navigation event."""
    logs = getattr(app, "logs", None)
    analysis = getattr(app, "analysis_state", {})
    table = getattr(app, "table_tools_state", {})
    page = getattr(app, "log_tools_state", {}).get("page") or {}
    toolbar = getattr(app, "toolbar_state", {}) or {}
    panels = getattr(app, "job_panel_state", {}) or {}
    from .terminal_ui import input_identity
    return (app.mode, app.tab, analysis.get("modal"), analysis.get("chart_job"), analysis.get("metric"),
            table.get("modal"), table.get("tab"), table.get("node"), table.get("action_job"),
            page.get("path"), page.get("start"), page.get("end"), page.get("snapshot", {}).get("ident"),
            getattr(app, "research_job_id", None),
            getattr(app, "research_view", None), getattr(app, "analytics_job", None),
            getattr(app, "log_job", None), getattr(logs, "path", None),
            getattr(logs, "browser", None),
            toolbar.get("menu"), toolbar.get("focus"), toolbar.get("cursor"), toolbar.get("panel"),
            panels.get("mode"), panels.get("focus"), input_identity(app))


def _motion_report(buttons, curses):
    """Held-left reports are positions; completed clicks remain actions."""
    actions = 0
    for flag in ("BUTTON1_CLICKED", "BUTTON1_DOUBLE_CLICKED", "BUTTON1_RELEASED",
                 "BUTTON3_CLICKED", "BUTTON3_PRESSED", "BUTTON4_PRESSED", "BUTTON5_PRESSED"):
        actions |= getattr(curses, flag, 0)
    return bool(buttons & getattr(curses, "REPORT_MOUSE_POSITION", 0) and not buttons & actions)


def _finish_unheld_pointer(app, mouse, curses):
    """A decoded no-button report ends gestures whose release was lost.

    Bare native curses position tuples are deliberately inconclusive: some
    drivers omit held bits during a valid drag. Preserve their capture path.
    """
    if getattr(mouse, "held", None) is not False or not _motion_report(mouse[4], curses):
        return
    from .pointer_routing import cancel_capture
    cancel_capture(app, reason="unheld")


def _batchable_input(app, event, curses):
    name, mouse = event
    # Child applications own the meaning of every drag sample. Tower's graph
    # endpoint coalescing must never change an application's mouse protocol.
    if _terminal_pointer(app, event):
        return False
    if isinstance(event, InputEvent) and event.origin is not None:
        return name != "mouse"
    # Passive movement has no document action, including over a modal. Keeping
    # it outside the navigation whitelist avoids one modal render per report.
    if name == "mouse" and mouse is not None and _motion_report(mouse[4], curses):
        return True
    mode = app.mode
    traversal = (mode in ("session_inbox", "log_tools_page", "log_tools_results", "log_tools_marks",
                          "jump_picker", "locations_picker", "value_peek", "field_explanation")
                 or mode == "analysis" and getattr(app, "analysis_state", {}).get("modal") in ("chart", "chart_events", "timeline", "diff", "inspect")
                 or mode == "table_tools" and getattr(app, "table_tools_state", {}).get("modal") in ("headers", "marks", "node", "actions"))
    if mode != "main" and not traversal:
        return False
    if name is None:
        return True  # An unsupported key or mouse motion has no controller action.
    if name != "mouse":
        if traversal:
            arrows = ("up", "down", "home", "end", "pgup", "pgdn")
            horizontal = mode == "log_tools_page" or mode == "analysis" and getattr(app, "analysis_state", {}).get("modal") == "chart"
            return name in arrows or horizontal and name in ("left", "right")
        return app.keymap.get(name) in _NAVIGATION_ACTIONS
    if mouse is None:
        return True
    buttons = mouse[4]
    click_mask = 0
    for button in ("BUTTON1_CLICKED", "BUTTON1_PRESSED", "BUTTON1_DOUBLE_CLICKED",
                   "BUTTON3_CLICKED", "BUTTON3_PRESSED"):
        click_mask |= getattr(curses, button, 0)
    # Captured motion is never a click. Initial presses and releases must use
    # the current frame; a release cannot disappear into a hover burst.
    if buttons & getattr(curses, "BUTTON1_RELEASED", 0):
        return False
    if _motion_report(buttons, curses):
        return True
    # Clicks use the hit map of the freshly painted frame. Wheels and passive
    # position reports can share a redraw without changing event order.
    if buttons & click_mask:
        return False
    if buttons & getattr(curses, "BUTTON4_PRESSED", 0):
        return traversal or app.keymap.get("up") in _NAVIGATION_ACTIONS
    if buttons & getattr(curses, "BUTTON5_PRESSED", 0):
        return traversal or app.keymap.get("down") in _NAVIGATION_ACTIONS
    return True


def _apply_input(app, event, hits, curses):
    name, mouse = event
    if name is None or name == "resize":
        return
    demand = getattr(getattr(app, "sampler", None), "set_collection_demand", None)
    if demand is not None and (name != "mouse" or mouse is not None and not _motion_report(mouse[4], curses)):
        panel = getattr(app, "job_panel_state", {})
        inline = app.tab in ("jobs", "history") and panel.get("mode") == "analytics"
        demand(app.tab, getattr(app, "selected_id", None), active=True,
               sources=("live", "trace") if inline else ())
    from . import terminal_ui
    identity = terminal_ui.input_identity(app)
    if name != "mouse":
        origin = event.origin if isinstance(event, InputEvent) else identity
        if origin is not None:
            # Input queued for a hidden/replaced session must never be sent to
            # its replacement, nor reinterpreted as Tower navigation.
            if identity != origin:
                return
            if name == "terminal-overflow":
                app.fail(f"Paste exceeds {_TERMINAL_PASTE_LIMIT // 1024} KiB; no paste bytes were sent.")
            elif name == "terminal-raw":
                terminal_ui.handle_event(app, {"type": "raw", "data": mouse})
            elif name == "paste":
                terminal_ui.handle_event(app, {"type": "paste", "text": mouse})
            else:
                terminal_ui.handle_key(app, name)
            return
        if identity is not None and isinstance(event, InputEvent):
            # An earlier Tower event cannot become a command in a newly
            # focused shell simply because painting happened in between.
            return
    if name == "paste":
        from .scrolling import note_input
        note_input(app, "paste")
        from .startup import dismiss
        dismiss(app)
        if app.mode == "terminal_probe":
            app.handle(f"paste ({len(mouse)} characters)")
        else:
            from .history_log_export import active as export_active, paste as export_paste
            if export_active(app):
                export_paste(app, mouse)
                return
            if app.mode == "operations":
                from .ops_ui import paste as operation_paste
                operation_paste(app, mouse)
                return
            from .command_ui import paste
            paste(app, mouse)
        return
    if name != "mouse":
        from .scrolling import note_input
        note_input(app, "key")
        app.handle(name)
        return
    if mouse is None:
        return
    if isinstance(event, InputEvent) and event.origin != identity:
        # A delayed terminal release/press cannot activate a graph or row after
        # a session switch. Conversely, a queued Tower report cannot leak into
        # a newly focused full-screen child application.
        return
    if isinstance(event, InputEvent) and event.geometry is not None and event.geometry != _terminal_geometry(app):
        # Resize or divider movement changes coordinate meaning, not keyboard
        # destination. Drop only obsolete pointer frames; retain shell keys.
        return
    if not getattr(app, "cfg", {}).get("mouse", True) and app.mode != "terminal_probe":
        return
    _, mx, my, _, bstate = mouse
    _finish_unheld_pointer(app, mouse, curses)
    shift = bool(bstate & getattr(curses, "BUTTON_SHIFT", 0))
    alt = bool(bstate & getattr(curses, "BUTTON_ALT", 0))
    def click(*args, **kwargs):
        # Preserve the legacy adapter API when no modifier was reported.
        if alt:
            kwargs["alt"] = True
        return app.click(*args, **kwargs)
    from .toolbar import handle_mouse as toolbar_mouse
    button = ("wheel-up" if bstate & getattr(curses, "BUTTON4_PRESSED", 0) else
              "wheel-down" if bstate & getattr(curses, "BUTTON5_PRESSED", 0) else
              "release" if bstate & getattr(curses, "BUTTON1_RELEASED", 0) else
              "right" if bstate & (getattr(curses, "BUTTON3_CLICKED", 0) | getattr(curses, "BUTTON3_PRESSED", 0)) else
              "left" if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED) else
              "drag" if bstate & getattr(curses, "REPORT_MOUSE_POSITION", 0) and bstate & getattr(curses, "BUTTON1_PRESSED", 0) else
              "motion" if bstate & getattr(curses, "REPORT_MOUSE_POSITION", 0) else
              "press" if bstate & curses.BUTTON1_PRESSED else "motion")
    terminal_button = button
    if bstate & getattr(curses, "BUTTON2_RELEASED", 0):
        terminal_button = "middle-release"
    elif bstate & getattr(curses, "BUTTON3_RELEASED", 0):
        terminal_button = "right-release"
    elif bstate & (getattr(curses, "BUTTON2_PRESSED", 0) | getattr(curses, "BUTTON2_CLICKED", 0)):
        terminal_button = "drag-middle" if bstate & getattr(curses, "REPORT_MOUSE_POSITION", 0) else "middle"
    elif bstate & getattr(curses, "BUTTON3_PRESSED", 0) and bstate & getattr(curses, "REPORT_MOUSE_POSITION", 0):
        terminal_button = "drag-right"
    terminal_modifiers = {"shift": shift, "alt": alt}
    if bstate & getattr(curses, "BUTTON_CTRL", 0):
        terminal_modifiers["ctrl"] = True
    if terminal_ui.handle_mouse(app, my, mx, button=terminal_button, **terminal_modifiers):
        return
    from .scrolling import note_input, handle_wheel
    note_input(app, "wheel" if button.startswith("wheel-") else button)
    if button in ("press", "left"):
        from .pointer_routing import begin_gesture
        if (type(mx) is int and type(my) is int and
                0 <= mx < getattr(app, "width", 100000) and
                0 <= my < getattr(app, "height", 100000)):
            begin_gesture(app)
        from .scrollbars import commit_selection_gesture
        commit_selection_gesture(app)
    from .startup import handle_mouse as startup_mouse
    startup_mouse(app, my, mx, button=button, shift=shift)
    from .workbench import INSPECTION_MODES
    if app.mode in INSPECTION_MODES:
        # Inspection dialogs own motion, release and wheel events as well as
        # clicks. Do not run underlying pane/browser pre-dispatch handlers.
        click(my, mx, hits, button=button, shift=shift)
        return
    from .history_log_export import active as export_active, handle_mouse as export_mouse
    from .scrollbars import handle_mouse as scrollbar_mouse
    from .column_resize import active as column_active, handle_mouse as column_mouse
    from .group_queue_drag import active as queue_active
    from .job_group_drag import pending as group_drag_pending
    from .pointer_routing import PointerEvent, route_capture
    if route_capture(app, PointerEvent(my, mx, button, shift, alt)):
        return
    from .job_group_menu import active as group_menu_active, handle_mouse as group_menu_mouse
    if group_menu_active(app):
        if scrollbar_mouse(app, my, mx, button=button, shift=shift):
            return
        group_menu_mouse(app, my, mx, button=button, shift=shift)
        return
    if not button.startswith("wheel-") and scrollbar_mouse(app, my, mx, button=button, shift=shift):
        return
    if export_active(app):
        export_mouse(app, my, mx, button=button, shift=shift)
        return
    if button == "right":
        from .toolbar import handle_interval_reset as toolbar_reset
        from .metric_live import handle_mouse as live_mouse
        if toolbar_reset(app, my, mx):
            return
        if live_mouse(app, my, mx, button=button, shift=shift):
            return
        if column_mouse(app, my, mx, button=button, shift=shift):
            return
        if group_menu_mouse(app, my, mx, button=button, shift=shift):
            return
    from .job_selection import context_click
    if button == "right":
        from .text_selection import clear as clear_text
        clear_text(app)
    if context_click(app, my, mx, button=button):
        return
    if button in ("press", "left"):
        # The toolbar can consume a fresh gesture before App.click runs.
        # Commit an earlier marked range here too, so a lost row release
        # cannot remain captured behind a new menu or update-slider gesture.
        getattr(app, "job_selection_state", {})["capture"] = None
    # Every final pointer position survives global capture. Wheels change the
    # document or menu and publish fresh hover geometry immediately afterward;
    # avoid resolving the displaced graph for every report in a wheel burst.
    wheel = button in ("wheel-up", "wheel-down")
    if isinstance(getattr(app, "interaction_state", None), dict):
        if wheel:
            app.interaction_state["pointer"] = (my, mx)
        else:
            from .interaction import handle_mouse as interaction_mouse
            interaction_mouse(app, my, mx, button="motion", shift=shift)
    from .chart_interaction import hover as chart_hover, cancel as cancel_chart
    charts = getattr(app, "chart_interaction_state", None)
    if wheel and isinstance(charts, dict):
        charts["pointer"] = (my, mx)
    else:
        chart_hover(app, my, mx)
    if isinstance(getattr(app, "toolbar_state", None), dict) and toolbar_mouse(app, my, mx, button=button, shift=shift):
        cancel_chart(app)
        from .metric_live import cancel as cancel_live
        cancel_live(app)
        return
    if button in ("wheel-up", "wheel-down"):
        from .pane_drag import blur
        blur(app)
        cancel_chart(app)
        from .metric_live import cancel as cancel_live
        cancel_live(app)
        # A wheel gesture belongs to content, even after clicking a button.
        # Keyboard focus must not turn its direction into button traversal.
        focus = getattr(app, "interaction_state", None)
        if isinstance(focus, dict):
            focus["active"], focus["focused"] = False, None
        if scrollbar_mouse(app, my, mx, button=button, shift=shift, rail_only=True):
            return
    if button in ("motion", "drag", "release"):
        from .job_selection import active as selection_active
        if (selection_active(app) or group_drag_pending(app) or column_active(app) or queue_active(app) or getattr(app, "pane_drag_state", {}).get("capture") or
                getattr(app, "history_browser_state", {}).get("drag") or
                getattr(app, "chart_interaction_state", {}).get("capture") or
                getattr(app, "text_selection_state", {}).get("capture") or
                getattr(app, "metric_live_state", {}).get("capture")):
            click(my, mx, hits, button=button, shift=shift)
        elif app.mode == "terminal_probe":
            click(my, mx, hits, button=button, shift=shift)
        return
    if button in ("wheel-up", "wheel-down"):
        from .history_browser import handle_mouse as history_mouse
        from .recent_history import handle_mouse as recent_mouse
        if history_mouse(app, my, mx, button=button, shift=shift) or recent_mouse(app, my, mx, button=button, shift=shift):
            return
        from .analytics_document import handle_mouse as series_mouse
        if series_mouse(app, my, mx, button=button, shift=shift):
            return
        if app.tab == "deps" and getattr(app, "history_browser_state", {}).get("views", {}).get("deps", {}).get("explicit"):
            app.move("up" if button == "wheel-up" else "down")
            return
        from .job_panels import contains as in_job_panel, handle_mouse as panel_mouse
        if in_job_panel(app, my, mx):
            if app.mode == "main":
                # Toolbar, browser, chart capture and pane ownership were
                # already handled above. Route to the published Details pane
                # once, retaining its sticky header and content wheel rules.
                app.last_hits = hits
                panel_mouse(app, my, mx, button=button, shift=shift)
            else:
                click(my, mx, hits, button=button, shift=shift)
            return
    if app.mode == "terminal_probe":
        click(my, mx, hits, button=button, shift=shift)
        return
    if button.startswith("wheel-") and scrollbar_mouse(app, my, mx, button=button, shift=shift):
        return
    if button.startswith("wheel-") and handle_wheel(app, my, mx, -1 if button == "wheel-up" else 1):
        return
    if bstate & (getattr(curses, "BUTTON3_CLICKED", 0) | getattr(curses, "BUTTON3_PRESSED", 0)):
        click(my, mx, hits, button="right")
    elif bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_DOUBLE_CLICKED):
        origin = app.tab
        from .job_panels import contains as in_job_panel
        double_target = None
        if (bstate & curses.BUTTON1_DOUBLE_CLICKED and not shift and not alt and app.mode == "main"
                and type(mx) is int and type(my) is int
                and 0 <= mx < getattr(app, "width", 100000)
                and 0 <= my < getattr(app, "height", 100000)
                and not in_job_panel(app, my, mx)):
            double_target = next(((kind, key) for y, kind, key in hits
                                  if y == my and ((origin == "jobs" and kind in ("job", "recent"))
                                  or (origin == "history" and kind == "fin")
                                  or (origin == "log" and app.logs.browser and kind == "log_file"))), None)
            pointer = getattr(app, "interaction_state", {}) or {}
            if double_target is not None and pointer.get("graph") is not None:
                from .interaction import _current, hit_token
                graph = _current(app)
                kind, key = double_target
                control = graph.get(f"{kind}:{key}") if graph is not None else None
                pointed = graph.at(my, mx) if graph is not None else None
                # Leading row-marker padding still belongs to the row, while
                # another published button or empty space past its end does not.
                if (control is None or control.rect.top != my or mx >= control.rect.right
                        or (pointed is not None and pointed.id != control.id)
                        or pointer.get("published_hit_token") is None
                        or hit_token(hits) != pointer.get("published_hit_token")):
                    double_target = None
        # Actual applications distinguish a held press from a completed click.
        # Small compatibility controllers without drag state retain left-click.
        pressed = button == "press" and hasattr(app, "job_selection_state")
        click(my, mx, hits, button="press" if pressed else "left", shift=shift)
        selected_target = (double_target is not None and
                           (origin == "log" or getattr(app, "selected_id", None) == double_target[1]))
        if bstate & curses.BUTTON1_DOUBLE_CLICKED and selected_target and app.tab == origin and app.mode == "main":
            app.handle("enter")
    elif bstate & getattr(curses, "BUTTON4_PRESSED", 0):
        for _ in range(3 if app.tab == "log" else 1):
            app.handle("up")
    elif bstate & getattr(curses, "BUTTON5_PRESSED", 0):
        for _ in range(3 if app.tab == "log" else 1):
            app.handle("down")


def _consume_input_batch(app, stdscr, curses, hits, first, *, effects=None):
    """Preserve every event while sharing a bounded redraw for queued scrolling.

    Return the first nonnavigation event for the next, freshly painted frame.
    Keeping this single captured event avoids pushing escape sequences or mouse
    reports back through curses, where their identity or order can change.
    """
    event = first
    context = _navigation_context(app)
    pointer = first[0] == "mouse"
    limit = POINTER_BATCH_LIMIT if pointer else INPUT_BATCH_LIMIT
    deadline = time.monotonic() + (POINTER_BATCH_SECONDS if pointer else INPUT_BATCH_SECONDS)
    count = 0
    motion = None
    from .interaction import needs_frame

    def apply(value):
        if effects is not None:
            effects.record(app, value, curses)
        trace = getattr(app, "ui_trace", None)
        if trace is not None:
            trace.input(app, value)
        _timed_ui(app, "input_dispatch", _apply_input, app, value, hits, curses)

    def is_motion(value):
        if value[0] != "mouse" or value[1] is None:
            return False
        return _motion_report(value[1][4], curses)

    while event is not None:
        batchable = _batchable_input(app, event, curses)
        if batchable and is_motion(event):
            # Keep the latest position only. Drag selection and slider position
            # are functions of the endpoint, not of the number of reports.
            if (motion is not None and getattr(event[1], "held", None) is False
                    and (getattr(motion[1], "held", None) is True
                         or motion[1][4] & getattr(curses, "BUTTON1_PRESSED", 0))):
                # Apply the final held endpoint before a report proving the
                # gesture ended; cancellation must retain its selected range.
                apply(motion)
                if app.quit or _navigation_context(app) != context or needs_frame(app):
                    return event
            motion = event
        else:
            if motion is not None:
                apply(motion)
                motion = None
                if app.quit or _navigation_context(app) != context or needs_frame(app):
                    return event
            apply(event)
        count += 1
        if (not batchable or app.quit or _navigation_context(app) != context or needs_frame(app) or
                count >= limit or time.monotonic() >= deadline):
            if motion is not None:
                apply(motion)
            return None
        stdscr.timeout(0)
        event = _read_input(stdscr, curses)
        if event is not None and not _batchable_input(app, event, curses):
            if motion is not None:
                apply(motion)
            return event
    if motion is not None:
        apply(motion)
    return None


class _InputEffects:
    """Separate cosmetic position updates from document-changing gestures."""

    def __init__(self):
        self.document = False

    def record(self, app, event, curses):
        name, mouse = event
        if name is None or name == "mouse" and mouse is None:
            return
        if (isinstance(event, InputEvent) and event.origin is not None and name != "mouse") or _terminal_pointer(app, event):
            # PTY readiness paints only the published terminal rectangle.
            # Keystrokes and child mouse traffic do not recompute job graphs.
            return
        state = mouse[4] if name == "mouse" and mouse is not None else 0
        hover = name == "mouse" and _motion_report(state, curses)
        chart = getattr(app, "chart_interaction_state", {}) or {}
        live = getattr(app, "metric_live_state", {}) or {}
        if ((chart.get("capture") or live.get("capture") or getattr(app, "text_selection_state", {}).get("capture")
             or getattr(app, "job_group_drag_state", {}).get("capture")
             or getattr(app, "group_queue_drag_state", {}).get("capture")) and hover):
            # Pointer feedback uses the published raster. Live slider motion
            # redraws its control immediately; its curve keeps a 10 Hz limit.
            return
        toolbar = getattr(app, "toolbar_state", {}) or {}
        selection = getattr(app, "job_selection_state", {}) or {}
        # Some terminal drivers omit the held-button bit on position reports.
        # A captured gesture must still rebuild its slider or selected rows.
        capture = (toolbar.get("dragging") or selection.get("capture") or
                   getattr(app, "column_resize_state", {}).get("capture") or
                   getattr(app, "scrollbar_state", {}).get("capture") or
                   getattr(app, "pane_drag_state", {}).get("capture") or
                   getattr(app, "history_browser_state", {}).get("drag"))
        if not hover or capture or app.mode == "terminal_probe":
            self.document = True


class _DifferentialPainter:
    """Paint changed cells after composing overlays in their original order."""

    def __init__(self, window, paint):
        self.window, self.paint = window, paint
        self.previous = None
        self.pixels = None
        self.base_pixels = None
        self.geometry = None

    def invalidate(self):
        self.previous = None
        self.pixels = None
        self.base_pixels = None

    @staticmethod
    def _raster(layer, width, *, base=None):
        cells = list(base) if base is not None else [(" ", "text+bg:canvas", 1)] * max(0, width)

        def clear(x):
            head = x
            while head > 0 and cells[head][2] == 0:
                head -= 1
            _, style, size = cells[head]
            for column in range(head, min(width, head + max(1, size))):
                cells[column] = (" ", style, 1)

        for left, row in layer:
            x = left
            for text, style in row:
                for char in text:
                    size = L.vlen(char)
                    if not size:
                        if 0 < x <= width:
                            head = x - 1
                            while head > 0 and cells[head][2] == 0:
                                head -= 1
                            prior, prior_style, prior_size = cells[head]
                            cells[head] = (prior + char, prior_style, prior_size)
                        continue
                    if x >= width:
                        break
                    if x < 0 or x + size > width:
                        x += size
                        continue
                    # Replacing one ordinary cell cannot leave half a wide
                    # glyph behind. Most graph and text cells take this path;
                    # only wide heads/tails need the clearing walk.
                    if size != 1 or cells[x][2] != 1:
                        for column in range(x, x + size):
                            clear(column)
                    cells[x] = (char, style, size)
                    for column in range(x + 1, x + size):
                        cells[column] = ("", style, 0)
                    x += size
        return tuple(cells)

    @staticmethod
    def _changed_runs(before, after):
        width = len(after)
        dirty = [before is None or before[x] != after[x] for x in range(width)]
        # A terminal cell in the middle of a wide glyph is not writable on
        # its own. Include both halves of old and new glyphs before painting.
        for x in range(width):
            if not dirty[x]:
                continue
            for row in (before, after):
                if row is None:
                    continue
                head = x
                while head > 0 and row[head][2] == 0:
                    head -= 1
                for column in range(head, min(width, head + max(1, row[head][2]))):
                    dirty[column] = True
        left = 0
        while left < width:
            if not dirty[left]:
                left += 1
                continue
            right = left + 1
            while right < width and dirty[right]:
                right += 1
            segments = []
            for text, style, size in after[left:right]:
                if not size:
                    continue
                if segments and segments[-1][1] == style:
                    segments[-1] = (segments[-1][0] + text, style)
                else:
                    segments.append((text, style))
            yield left, segments
            left = right

    def draw(self, rows, overlays, width, height, *, bar=None):
        layers = [[(0, tuple(rows[y]) if y < len(rows) else ())] for y in range(max(0, height))]
        for y, x, row in overlays:
            if 0 <= y < height and x < width:
                layers[y].append((x, tuple(row)))
        if height > 0 and bar is not None:
            # The complete toolbar masks any modal placed on row zero.
            layers[0] = [(0, tuple(bar))]
        current = tuple(tuple(layer) for layer in layers)
        reset = self.previous is None or self.geometry != (width, height)
        if reset:
            self.window.erase()
        changed = []
        pixels, base_pixels = [], []
        for y, layer in enumerate(current):
            if not reset and layer == self.previous[y]:
                pixels.append(self.pixels[y])
                base_pixels.append(self.base_pixels[y])
                continue
            # Pointer overlays usually move over an unchanged report. Reuse
            # its base raster instead of re-reading every chart glyph.
            base = (self.base_pixels[y] if not reset and layer[0] == self.previous[y][0]
                    else self._raster(layer[:1], width))
            base_pixels.append(base)
            raster = self._raster(layer[1:], width, base=base) if len(layer) > 1 else base
            pixels.append(raster)
            runs = list(self._changed_runs(None if reset else self.pixels[y], raster))
            if runs:
                changed.append(y)
            for x, row in runs:
                self.paint(y, x, row, width, height)
        self.previous, self.geometry = current, (width, height)
        self.pixels = tuple(pixels)
        self.base_pixels = tuple(base_pixels)
        return tuple(changed)


def _toolbar_feedback_token(app):
    state = getattr(app, "toolbar_state", {}) or {}
    from .worker_ui import token as worker_token
    from .raven_ui import token as allocation_token
    return tuple(state.get(key) for key in ("menu", "cursor", "top", "panel", "panel_scroll", "focus")) + (
        getattr(app, 'cfg', {}).get('clipboard', {}).get('destination', 'copy'),
        worker_token(app), allocation_token(app))


def _group_menu_feedback_token(app):
    state = getattr(app, "job_group_menu_state", {}) or {}
    return ((state.get("token"), state.get("cursor"), state.get("top"), state.get("hover"))
            if getattr(app, "mode", "main") == "job_group_menu" else None)


def _drain_prepaint_motion(app, stdscr, curses, hits, pending_input=None):
    """Paint the newest queued passive pointer after a document rebuild.

    A rebuild may outlast several position reports. Keep their newest endpoint
    before painting, while leaving every deliberate event for normal dispatch.
    Captures and partial key/paste/report framing retain their ordinary owner.
    """
    if pending_input is not None or app.mode != "main" or _protect_pointer_escape(app):
        return pending_input
    reader = _INPUT_READERS.get(id(stdscr))
    if reader is not None and reader.pending:
        return pending_input
    deliberate = 0
    for button in range(1, 6):
        for action in ("CLICKED", "PRESSED", "RELEASED", "DOUBLE_CLICKED", "TRIPLE_CLICKED"):
            deliberate |= getattr(curses, f"BUTTON{button}_{action}", 0)
    stdscr.timeout(0)
    deadline = time.monotonic() + POINTER_BATCH_SECONDS
    motion = None
    for _ in range(POINTER_BATCH_LIMIT):
        if time.monotonic() >= deadline:
            break
        event = _read_input(stdscr, curses)
        if event is None:
            break
        mouse = event[1] if event[0] == "mouse" else None
        if (mouse is None or _terminal_pointer(app, event) or not _motion_report(mouse[4], curses)
                or mouse[4] & deliberate or getattr(mouse, "held", None) is True):
            pending_input = event
            break
        motion = event
    if motion is not None:
        trace = getattr(app, "ui_trace", None)
        if trace is not None:
            trace.input(app, motion)
        _timed_ui(app, "input_dispatch", _apply_input, app, motion, hits, curses)
    return pending_input


def _quiet_jobs_token(app, *, publication=_UNREAD_PUBLICATION):
    """Permit slower composition only for a known, quiescent managed Jobs view.

    Housekeeping and feedback keep their original cadence. Unknown producers,
    pending readers, active animations/gestures, and other pages fall back to
    the ordinary document deadline rather than guessing at their publications.
    """
    config=getattr(app,'cfg',{}).get('raven')
    if (not isinstance(config,dict) or not config.get('managed') or
            getattr(app,'tab',None)!='jobs' or getattr(app,'mode',None)!='main' or
            getattr(app,'plugins',None) is not None or getattr(app,'replay',None) is not None or
            getattr(app,'job_panel_state',{}).get('mode') not in ('inspector','off')):
        return None
    from .model import Store
    store=getattr(app,'store',None)
    if type(store) is not Store or store.raven_limits is None:
        return None
    from . import startup,scrolling,metric_live,pointer_routing,terminal_ui,raven_ui
    if (startup.active(app) or getattr(getattr(app,'completion',None),'active',False) or
            scrolling.active(app) or pointer_routing.active(app) or terminal_ui.showing(app) or
            metric_live.document_interval(app) is not None or
            getattr(app,'toolbar_state',{}).get('panel') or
            getattr(app,'ops_state',{}).get('running')):
        return None
    views=getattr(app,'views_ref',None)
    if getattr(views,'_preview_pending',None) or store._series_archive.worker:
        return None
    hub=getattr(app,'research',None)
    research=None
    if hub is not None:
        from .research import ResearchHub
        if type(hub) is not ResearchHub:
            return None
        with hub.lock:
            if hub.pending is not None or hub.future is not None and not hub.future.done():
                return None
            research=(hub.generation,id(hub.future),hub.closed)
    activity=getattr(app,'activity',None)
    if activity is not None:
        with activity.lock:
            if activity.task and activity.task.get('status')=='running':
                return None
    published=store.ui_publication_token() if publication is _UNREAD_PUBLICATION else publication
    if published is None:
        return None
    return (published,getattr(app,'ui_publication_revision',0),research,
            getattr(app,'message',''),getattr(app,'message_t',0),getattr(app,'selected_id',None),
            getattr(app,'inbox_unread',0),getattr(app,'freeze_label',''),raven_ui.token(app))


class _FrameCache:
    """Retain a published document while cosmetic pointer feedback changes.

    Maintenance and animation deadlines are independent of pointer traffic.
    Every deliberate input still requests a complete, current document frame.
    """

    def __init__(self):
        self.dirty = True
        self.snapshot = None
        self.rows, self.hits = [], []
        self.welcome, self.content, self.toolbar = [], [], []
        self.bar = []
        self.geometry = None
        self.next_maintenance = self.next_animation = 0.0
        self.next_document = float('inf')
        self.document_token = None
        self.next_live = float("inf")
        self.next_selector = float("inf")
        self.live_revision = 0
        self.preview_ready = 0.0
        self.next_preview = float("inf")
        self.toolbar_token = None
        self.group_menu_token = None

    def due(self, app, width, height, now=None):
        now = time.monotonic() if now is None else now
        from .interaction import needs_frame
        from . import metric_live
        revision_changed = metric_live.document_revision(app) != self.live_revision
        preview = revision_changed and metric_live.active(app)
        self.next_preview = self.preview_ready if preview else float("inf")
        live_changed = revision_changed and (not preview or now >= self.preview_ready)
        return (self.dirty or live_changed or needs_frame(app) or getattr(app, "text_selection_state", {}).get("frame_required") or self.geometry != (width, height) or
                now >= self.next_maintenance or now >= self.next_animation or now >= self.next_live or now >= self.next_document)

    def maintain(self, app, width, height, now=None):
        """Run the usual housekeeping without rebuilding unchanged Jobs rows."""
        now=time.monotonic() if now is None else now
        if (self.dirty or self.geometry!=(width,height) or now<self.next_maintenance or
                self.document_token is None or _quiet_jobs_token(app) is None):
            return False
        _timed_ui(app,'maintenance',app.tick)
        self.next_maintenance=now+MAINTENANCE_SECONDS
        current=_quiet_jobs_token(app)
        if current is None or current!=self.document_token:
            self.dirty=True
        return True

    def rebuild(self, app, views, store, actions, width, height, *, maintained=False):
        from . import startup, toolbar
        from .scrolling import begin_frame, finish_frame, timeout_ms
        from . import scrollbars
        from .interaction import publish
        if not maintained:
            _timed_ui(app, "maintenance", app.tick)
        snap = _timed_ui(app, "snapshot", getattr(store, "render_snapshot", store.snapshot))
        app.width = width
        begin_frame(app)
        options = {"feedback": False} if getattr(views, "feedback_options", False) else {}
        rows, hits = _timed_ui(app, "compose", views.compose, snap, app, width, height, actions, **options)
        welcome = startup.overlay(views, snap, app, width, height) or []
        overlays = _timed_ui(app, "overlay", views.overlay, snap, app, width, height, **options) or []
        finish_frame(app)
        self.rows = getattr(app, "frame_rows", rows)
        self.hits, self.welcome = hits, welcome
        from .job_progress import publish_animation
        publish_animation(app, self.rows, hits,
                          ascii_=bool(getattr(getattr(views, "g", None), "ascii", False)))
        if options:
            self.content = getattr(app, "content_overlay_rows", []) or []
            self.toolbar = getattr(app, "toolbar_overlay_rows", []) or []
        else:
            self.content, self.toolbar = overlays, []
        self.bar = toolbar.render_bar(views, app, width) if height > 0 else []
        app.last_hits = hits
        from .chart_interaction import publish as publish_charts
        from . import metric_live
        publish_charts(app, width, height)
        scrollbars.publish(app, width, height, overlays=welcome + overlays)
        from .text_selection import publish as publish_text
        publish_text(app, self.rows, width, height, overlays=welcome + overlays)
        from .column_resize import descriptors as column_descriptors
        _timed_ui(app, "publish_controls", publish, app, self.rows, hits, width, height, overlays=welcome + overlays,
                  extra_controls=metric_live.descriptors(app) + scrollbars.descriptors(app) + tuple(column_descriptors(app)))
        self.snapshot, self.geometry = snap, (width, height)
        self.toolbar_token = _toolbar_feedback_token(app)
        self.group_menu_token = _group_menu_feedback_token(app)
        now = time.monotonic()
        self.next_maintenance = now + MAINTENANCE_SECONDS
        self.document_token=_quiet_jobs_token(app,publication=snap.get('_ui_publication_token'))
        # Visible elapsed/clock labels use whole seconds. Keep their refresh
        # boundary even when no source or asynchronous callback publishes.
        from . import clock
        self.next_document=(now+max(.001,min(QUIET_DOCUMENT_SECONDS,1.0-clock.now()%1.0))
                            if self.document_token is not None else float('inf'))
        idle = 100 if app.animations_enabled and app.completion.active else 200
        if startup.active(app):
            idle = min(idle, int(startup.FRAME_INTERVAL * 1000))
        interval = timeout_ms(app, idle)
        self.next_animation = now + interval / 1000 if interval < 200 else float("inf")
        live_interval = metric_live.document_interval(app)
        self.next_live = now + live_interval if live_interval is not None else float("inf")
        self.live_revision = metric_live.document_revision(app)
        # Slider feedback follows every input, while expensive graph previews
        # use the latest accepted value at most twenty times per second.
        # Release commits immediately through the ordinary revision path.
        self.preview_ready = now + .05
        self.next_preview = float("inf")
        self.dirty = False

    def feedback(self, app, views):
        from .interaction import publish, decorate, decorate_overlays
        from . import toolbar
        width, height = self.geometry
        from . import job_group_menu
        group_menu_token = _group_menu_feedback_token(app)
        if job_group_menu.active(app) and group_menu_token != self.group_menu_token:
            # Menu hover changes only a bounded cached action list, never the
            # underlying job tables, source snapshot, or graph rasters.
            self.content = job_group_menu.overlay(views, self.snapshot, app, width, height) or []
            from . import scrollbars, metric_live
            scrollbars.publish(app, width, height, overlays=self.welcome + self.content + self.toolbar)
            from .column_resize import descriptors as column_descriptors
            publish(app, self.rows, self.hits, width, height,
                    overlays=self.welcome + self.content + self.toolbar,
                    extra_controls=metric_live.descriptors(app) + scrollbars.descriptors(app) + tuple(column_descriptors(app)))
            self.group_menu_token = _group_menu_feedback_token(app)
        token = _toolbar_feedback_token(app)
        if token != self.toolbar_token:
            # A menu cursor, switch, or pointer dismissal only changes this
            # overlay. The underlying chart/report remains the same document.
            self.toolbar = toolbar.overlay(views, self.snapshot, app, width, height) or []
            self.bar = toolbar.render_bar(views, app, width) if height > 0 else []
            from .chart_interaction import publish as publish_charts
            from . import metric_live
            from . import scrollbars
            publish_charts(app, width, height)
            scrollbars.publish(app, width, height, overlays=self.welcome + self.content + self.toolbar)
            from .text_selection import publish as publish_text
            publish_text(app, self.rows, width, height, overlays=self.welcome + self.content + self.toolbar)
            from .column_resize import descriptors as column_descriptors
            publish(app, self.rows, self.hits, width, height,
                    overlays=self.welcome + self.content + self.toolbar,
                    extra_controls=metric_live.descriptors(app) + scrollbars.descriptors(app) + tuple(column_descriptors(app)))
            self.toolbar_token = _toolbar_feedback_token(app)
        rows = decorate(app, self.rows)
        from .job_progress import animate_rows
        rows = animate_rows(app, rows)
        from .terminal_ui import feedback as terminal_feedback
        rows = terminal_feedback(app, rows)
        from .job_group_drag import feedback as group_drag_feedback
        rows = group_drag_feedback(app, rows)
        from .group_queue_drag import feedback as queue_drag_feedback
        rows = queue_drag_feedback(app, rows)
        overlays = self.welcome + decorate_overlays(app, self.content + self.toolbar)
        from .chart_interaction import feedback as chart_feedback
        overlays += chart_feedback(app, ascii_=bool(getattr(getattr(views, "g", None), "ascii", False)),
                                   rows=rows, overlays=overlays, compact=True)
        from .chart_interaction import next_deadline
        self.next_selector = next_deadline(app)
        from .metric_live import feedback as live_feedback
        glyphs = getattr(views, "g", None) or L.Glyphs(bool(getattr(app, "ascii", False)))
        overlays += live_feedback(app, glyphs)
        from .scrollbars import feedback as scrollbar_feedback
        overlays += scrollbar_feedback(app, ascii_=glyphs.ascii)
        from .text_selection import feedback as text_feedback
        overlays += text_feedback(app, ascii_=glyphs.ascii)
        bar = decorate(app, [self.bar])[0] if height > 0 else None
        return rows, overlays, bar

    def wait_ms(self, now=None):
        now = time.monotonic() if now is None else now
        return max(1, min(200, round(1000 * (min(self.next_maintenance, self.next_animation,
                                              self.next_live, self.next_selector, self.next_preview, self.next_document) - now))))


def _mouse_reporting(enabled):
    """Request SGR passive movement and drag reports; always restore terminal.

    Older terminals that do not support any-event mode retain button-event
    mode. Both are disabled explicitly on exit, including exceptional exits.
    Clear inherited extended/pixel encodings before requesting cell-based SGR.
    Tower's byte decoder owns the complete report, including fallback formats.
    """
    if not sys.stdout.isatty():
        return
    formats_off = "\033[?1005l\033[?1015l\033[?1016l"
    sys.stdout.write(formats_off + ("\033[?1002h\033[?1003h\033[?1006h" if enabled else
                                  "\033[?1003l\033[?1002l\033[?1000l\033[?1006l"))
    sys.stdout.flush()


def run_curses(app, views, sampler, store, actions, cfg):
    import curses
    import locale
    locale.setlocale(locale.LC_ALL, "")

    def main(stdscr):
        try:
            curses.raw()  # Word-edit undo/redo must receive Ctrl-Z/Ctrl-S intact.
        except curses.error:
            pass
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.timeout(200)
        # Tower owns both mouse and keyboard escape framing. Ncurses must not
        # consume a legacy mouse header and leak UTF-8 coordinate tail bytes.
        stdscr.keypad(False)
        if hasattr(curses, "set_escdelay") and "ESCDELAY" not in os.environ:
            curses.set_escdelay(25)
        mouse_enabled = cfg.get("mouse", True) or app.mode == "terminal_probe"
        try:
            curses.mousemask((curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION) if mouse_enabled else 0)
            curses.mouseinterval(0)
        except curses.error:
            pass
        _mouse_reporting(mouse_enabled)
        palette = CursesPalette(curses, cfg["color"], app.theme)
        settings_generation = getattr(app, "terminal_settings_generation", 0)
        painted_theme = app.theme

        def paint(y, x0, segs, width, height):
            x = x0
            for text, style in segs:
                if x >= width or y >= height:
                    break
                text = L.cut(text, width - x, True) if L.vlen(text) > width - x else text
                try:
                    stdscr.addstr(y, x, text, palette.attr(P.cell_style(style, app.theme), app.theme))
                except curses.error:
                    pass
                x += L.vlen(text)

        hits = []
        rung = 0
        pending_input = None
        cache = _FrameCache()
        painter = _DifferentialPainter(stdscr, paint)
        app.views_ref = views
        from . import startup
        from .ops_ui import defer_quit as defer_operation_quit
        startup.begin(app)
        from . import terminal_ui, raven_ui, raven_diagnostics
        _bind_input(stdscr, app)
        while not app.quit or defer_operation_quit(app):
            terminal_ui.tick(app)
            diagnostics_changed = raven_diagnostics.tick(app)
            if (raven_ui.tick(app) or diagnostics_changed) and app.mode == "raven":
                cache.dirty = True
            if painted_theme != app.theme:
                painted_theme = app.theme
                # The whole screen is invalidated before pairs are reseeded.
                # New theme tones must not approximate saturated old gradients.
                palette = CursesPalette(curses, cfg["color"], app.theme)
                cache.dirty = True
                painter.invalidate()
            current_settings = getattr(app, "terminal_settings_generation", 0)
            if settings_generation != current_settings:
                palette = CursesPalette(curses, cfg["color"], app.theme)
                settings_generation = current_settings
                cache.dirty = True
                painter.invalidate()
            current_mouse = cfg.get("mouse", True) or app.mode == "terminal_probe"
            if current_mouse != mouse_enabled:
                try:
                    curses.mousemask((curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION) if current_mouse else 0)
                except curses.error:
                    pass
                mouse_enabled = current_mouse
                _mouse_reporting(mouse_enabled)
            height, width = stdscr.getmaxyx()
            maintained=cache.maintain(app,width,height)
            if cache.due(app, width, height):
                _timed_ui(app, "document", cache.rebuild, app, views, store, actions, width, height,maintained=maintained)
                pending_input = _timed_ui(app, "prepaint_input", _drain_prepaint_motion,
                                         app, stdscr, curses, cache.hits, pending_input)
            if _operation_terminal(app, stdscr, curses, mouse_enabled):
                pending_input = None
                painter.invalidate()
                cache.dirty = True
                continue
            rows, overlays, bar = _timed_ui(app, "feedback", cache.feedback, app, views)
            hits, snap = cache.hits, cache.snapshot
            # A PTY is independent of the Slurm polling interval. A short,
            # bounded readiness check keeps interactive echo responsive while
            # preserving the cached underlying graph/document.
            stdscr.timeout(min(cache.wait_ms(), 16) if terminal_ui.showing(app) else cache.wait_ms())
            reader = _INPUT_READERS.get(id(stdscr))
            if reader:
                reader.protect_escape = _protect_pointer_escape(app)
                if reader.pending:
                    stdscr.timeout(5)
            _timed_ui(app, "paint", painter.draw, rows, overlays, width, height, bar=bar)
            started = sum(1 for e in snap["events"] if e.get("kind") == "started" and not e.get("old"))
            if app.bell and started > rung:
                curses.beep()
            rung = started
            stdscr.noutrefresh()
            _timed_ui(app, "terminal_flush", curses.doupdate)
            try:
                event = pending_input if pending_input is not None else _timed_ui(app, "input_wait", _read_input, stdscr, curses)
            except KeyboardInterrupt:
                if terminal_ui.focused(app):
                    terminal_ui.handle_key(app, "ctrl-c")
                    continue
                raise
            pending_input = None
            if event is None:
                continue
            effects = _InputEffects()
            before = (app.mode, app.tab, getattr(app, "selected_id", None))
            pending_input = _timed_ui(app, "input_batch", _consume_input_batch,
                                      app, stdscr, curses, hits, event, effects=effects)
            cache.dirty = (effects.document or pending_input is not None
                           or before != (app.mode, app.tab, getattr(app, "selected_id", None))
                           or not getattr(views, "feedback_options", False))
            if getattr(app, "want_less", False):
                app.want_less = False
                files = views.files
                path = views.pager_path(store.snapshot(), app)
                if path and files.exists(path):
                    curses.endwin()
                    _mouse_reporting(False)
                    try:
                        subprocess.call(files.less_argv(path))
                    finally:
                        _mouse_reporting(mouse_enabled)
                        stdscr.touchwin()
                        stdscr.refresh()
                        painter.invalidate()
                        cache.dirty = True
                else:
                    app.say("no stdout file yet for this job")

    bracketed = sys.stdout.isatty()
    if bracketed:
        sys.stdout.write("\033[?2004h")
        sys.stdout.flush()
    try:
        curses.wrapper(main)
    finally:
        _INPUT_READERS.clear()
        _mouse_reporting(False)
        if bracketed:
            sys.stdout.write("\033[?2004l")
            sys.stdout.flush()


def _operation_terminal(app, stdscr, curses, mouse_enabled):
    """Hand an explicitly reviewed allocation to the terminal, then restore it."""
    from . import ops_ui
    argv = ops_ui.take_terminal(app)
    if argv is None:
        return False
    bracketed = sys.stdout.isatty()
    curses.endwin()
    _mouse_reporting(False)
    if bracketed:
        sys.stdout.write("\033[?2004l")
        sys.stdout.flush()
    try:
        result = subprocess.call(argv)
        app.say("Allocation terminal closed" + (f" (exit {result})." if result else "."))
    except (OSError, KeyboardInterrupt) as exc:
        app.fail("Allocation terminal: " + str(exc))
    finally:
        # Shell input must never become Tower shortcuts after returning.
        _INPUT_READERS.pop(id(stdscr), None)
        try:
            curses.flushinp()
        except curses.error:
            pass
        _mouse_reporting(mouse_enabled)
        if bracketed:
            sys.stdout.write("\033[?2004h")
            sys.stdout.flush()
        stdscr.touchwin()
        stdscr.refresh()
    return True


def run_watch(app, views, sampler, store, actions, cfg, interval: float, color: bool, width_hint: int = 120):
    """The non-interactive animated screen (Ctrl-C exits)."""
    import shutil
    interactive = sys.stdout.isatty() and os.environ.get("TERM", "").lower() != "dumb"
    if interactive:
        sys.stdout.write("\033[?25l\033[2J")
    rung = 0
    previous: list[str] = []
    previous_size = None
    try:
        while True:
            size = shutil.get_terminal_size((width_hint, 40))
            width, height = max(1, size.columns), max(1, size.lines)
            app.width = width
            app.tick()
            snap = store.snapshot()
            rows, _ = views.compose(snap, app, width, height if interactive else None, actions)
            if interactive and len(rows) == height:
                rows[-1] = L.clip_row([(" Ctrl-C exit", "bold"), (f"  |  watch every {interval:g}s", "dim")], width)
            started = sum(1 for e in snap["events"] if e.get("kind") == "started" and not e.get("old"))
            bell = "\a" if app.bell and started > rung else ""
            rung = started
            frame = L.to_text(rows, width, color, theme=app.theme, canvas=True).split("\n")
            if interactive:
                if previous_size is not None and previous_size != (width, height):
                    sys.stdout.write("\033[2J")
                    previous = []
                # Update changed lines only, retaining a stable screen on SSH.
                for y, line in enumerate(frame):
                    if y >= len(previous) or line != previous[y]:
                        sys.stdout.write(f"\033[{y + 1};1H{line}\033[K")
                if len(previous) > len(frame) and len(frame) < height:
                    sys.stdout.write(f"\033[{len(frame) + 1};1H\033[J")
                sys.stdout.write(bell)
            else:
                sys.stdout.write("\n".join(frame) + "\n" + bell)
            previous = frame
            previous_size = (width, height)
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\n" if interactive else "")
        sys.stdout.flush()


def once_text(app, views, store, actions, width: int, color: bool, tab: Optional[str] = None) -> str:
    if tab:
        app.tab = tab
    app.width = width
    app.tick()
    snap = store.snapshot()
    rows, _ = views.compose(snap, app, width, None, actions)
    return L.to_text(rows, width, color, theme=app.theme, canvas=True)


def once_json(store) -> str:
    snap = store.snapshot()
    return json.dumps(to_plain({k: v for k, v in snap.items() if k not in ("hist_cpu", "hist_gpu")}), indent=1, default=str)
