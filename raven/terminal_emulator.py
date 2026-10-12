"""Bounded VT adapter for embedded PTYs; never writes to the outer terminal.

The optional ``pyte`` engine owns VT cursor, erase, rendition and scroll parsing.
This adapter frames control strings before that parser, supplies separate normal
and alternate screens, and encodes semantic input using the *current* child
modes. OSC 52 writes are bounded requests for explicit UI mediation; reads are
never answered. OSC 8 links expose destinations without opening them. Graphics,
extended keyboard negotiation and complex emoji shaping are unsupported. Resize
clips normal-buffer columns rather than pretending to provide lossless reflow.
Cells retain at most eight combining marks; geometry is bounded at 160 by 400.
"""
from __future__ import annotations

import base64
import binascii
import codecs
from collections import deque, namedtuple
from functools import lru_cache
import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit

try:
    import pyte
    from wcwidth import wcwidth
except ImportError:  # The ordinary Raven installation must remain usable.
    pyte = None  # type: ignore[assignment]

MAX_ROWS = 160
MAX_COLS = 400
MAX_CONTROL = 4096
MAX_CSI = 256
MAX_REPLY = 16384
MAX_HISTORY_BYTES = 4 * 1024 * 1024
MAX_INPUT = 1024 * 1024
_FRAMES = re.compile(r"[\x1b\x90\x98\x9b\x9d\x9e\x9f]")
_RGB = re.compile(r"^[0-9a-fA-F]{6}$")
_ANSI = {
    "black": "000000", "red": "cd0000", "green": "00cd00",
    "brown": "cdcd00", "yellow": "cdcd00", "blue": "0000ee",
    "magenta": "cd00cd", "cyan": "00cdcd", "white": "e5e5e5",
    "brightblack": "7f7f7f", "brightred": "ff0000",
    "brightgreen": "00ff00", "brightbrown": "ffff00",
    "brightyellow": "ffff00", "brightblue": "5c5cff",
    "brightmagenta": "ff00ff", "brightcyan": "00ffff", "brightwhite": "ffffff",
}


@lru_cache(maxsize=2048)
def _style(fg: str, bg: str, bold: bool, under: bool, reverse: bool) -> str:
    parts = ["terminal"]
    for prefix, value in (("fg", fg), ("bg", bg)):
        color = _ANSI.get(value, value)
        if _RGB.fullmatch(color):
            parts.append(prefix + ":#" + color.lower())
    if bold:
        parts.append("bold")
    if under:
        parts.append("under")
    if reverse:
        parts.append("rev")
    return "+".join(parts)


def _geometry(rows: int, cols: int) -> tuple[int, int]:
    if isinstance(rows, bool) or isinstance(cols, bool):
        raise ValueError("terminal dimensions must be integers")
    rows, cols = int(rows), int(cols)
    if not 1 <= rows <= MAX_ROWS or not 1 <= cols <= MAX_COLS:
        raise ValueError("terminal dimensions outside 1..160 rows / 1..400 columns")
    return rows, cols


if pyte is not None:
    _LinkChar = namedtuple("_LinkChar", (*pyte.screens.Char._fields, "link"))

    class _Screen(pyte.Screen):
        def __init__(self, owner: TerminalEngine, cols: int, rows: int, normal: bool):
            self.owner = owner
            self.normal = normal
            self._inside_draw = False
            super().__init__(cols, rows)

        @property
        def default_char(self):
            return _LinkChar(*super().default_char, 0)

        def reset(self) -> None:
            super().reset()
            self.cursor.attrs = self.default_char

        def select_graphic_rendition(self, *attrs: int) -> None:
            super().select_graphic_rendition(*attrs)
            self.cursor.attrs = self.cursor.attrs._replace(link=self.owner._active_link)

        def write_process_input(self, data: str) -> None:
            self.owner._reply(data.encode("utf-8"))

        def index(self) -> None:
            # Scroll regions inside editors are not shell scrollback.
            margins = self.margins
            full = margins is None or (margins.top == 0 and margins.bottom == self.lines - 1)
            if self.normal and full and self.cursor.y == self.lines - 1:
                line = self.buffer[self.cursor.y - (self.lines - 1)]
                wrapped = bool(getattr(line, "wrapped", False))
                value = "".join(line[x].data for x in range(self.columns))
                self.owner._remember(value if wrapped else value.rstrip(), wrapped)
            super().index()

        def carriage_return(self) -> None:
            if self._inside_draw and self.cursor.x >= self.columns:
                self.buffer[self.cursor.y].wrapped = True
            super().carriage_return()

        def erase_in_line(self, how: int = 0, *args: Any, **kwargs: Any) -> None:
            super().erase_in_line(how, *args, **kwargs)
            if how == 2 or (how == 0 and self.cursor.x == 0):
                self.buffer[self.cursor.y].wrapped = False

        def save_cursor(self) -> None:
            super().save_cursor()
            # Malicious repeated ESC 7 must not grow an unbounded stack.
            del self.savepoints[:-16]

        def erase_in_display(self, how: int = 0, *args: Any, **kwargs: Any) -> None:
            super().erase_in_display(how, *args, **kwargs)
            if how in (2, 3):
                for line in self.buffer.values():
                    line.wrapped = False
            if how == 3 and self.normal:
                self.owner.clear_history()

        def _native_draw(self, data: str) -> None:
            self._inside_draw = True
            try:
                super().draw(data)
            finally:
                self._inside_draw = False

        def draw(self, data: str) -> None:
            self.cursor.attrs = self.cursor.attrs._replace(link=self.owner._active_link)
            # pyte's per-character NFC normalizer needs a cell limit: cursor
            # movement can otherwise append combining marks indefinitely.
            if self.cursor.x < self.columns and self.cursor.x > 0:
                line = self.buffer[self.cursor.y]
                if not line[self.cursor.x].data and wcwidth(line[self.cursor.x - 1].data[:1]) == 2:
                    line[self.cursor.x - 1] = line[self.cursor.x - 1]._replace(data=" ")
                    self.dirty.add(self.cursor.y)
            if data.isascii():
                self.owner._combining = 0
                self._native_draw(data)
                return
            pending: list[str] = []

            def flush() -> None:
                if pending:
                    self._native_draw("".join(pending))
                    pending.clear()

            for char in data:
                width = wcwidth(char)
                if unicodedata.combining(char):
                    flush()
                    self.owner._combining += 1
                    row, col = self.cursor.y, self.cursor.x - 1
                    if col < 0:
                        row, col = row - 1, self.columns - 1
                    if row < 0:
                        continue
                    line = self.buffer[row]
                    if col > 0 and not line[col].data:
                        col -= 1  # Attach accents to the wide glyph, not its stub.
                    last = line[col]
                    if len(last.data) >= 9 or self.owner._combining > 8:
                        self.owner.unsupported += int(self.owner._combining == 9)
                        continue
                    line[col] = last._replace(data=unicodedata.normalize("NFC", last.data + char))
                    self.dirty.add(row)
                elif width <= 0:
                    # Complex joined emoji shaping is outside pyte's contract.
                    # Consume unsupported joiners without losing later text.
                    flush()
                    self.owner.unsupported += 1
                elif width == 2:
                    flush()
                    self.owner._combining = 0
                    if self.cursor.x == self.columns - 1:
                        if (7 << 5) in self.mode and self.columns > 1:
                            self.buffer[self.cursor.y].wrapped = True
                            self.carriage_return()
                            self.linefeed()
                        else:
                            self._native_draw("\ufffd")
                            self.owner.unsupported += 1
                            continue
                    self._native_draw(char)
                else:
                    self.owner._combining = 0
                    pending.append(char)
            flush()


class _Dispatch:
    """Bound dynamic dispatch: pyte caches handlers when attaching its parser."""
    def __init__(self, owner: TerminalEngine):
        self.owner = owner

    def __getattr__(self, name: str):
        if pyte is None or name not in pyte.Stream.events:
            raise AttributeError(name)

        def event(*args: Any, **kwargs: Any) -> None:
            owner = self.owner
            if name in ("set_mode", "reset_mode"):
                owner._mode(name == "set_mode", args, bool(kwargs.get("private")))
            elif name == "reset":
                owner.reset()
            elif name == "debug":
                owner.unsupported += 1
            elif name == "bell":
                owner.bells = min(owner.bells + 1, 2**31 - 1)
            elif name == "report_device_status" and kwargs.get("private"):
                if args and args[0] == 6:
                    cursor = owner.screen.cursor
                    owner._reply(f"\x1b[?{cursor.y + 1};{min(cursor.x, owner.cols - 1) + 1}R".encode())
            else:
                try:
                    getattr(owner.screen, name)(*args, **kwargs)
                except (TypeError, ValueError, IndexError):
                    # Unsupported parameter forms are child data, not broker failures.
                    owner.unsupported += 1
        return event


class TerminalEngine:
    """VT state for one owned PTY, with JSON-safe snapshots and no I/O.

    Input events use ``type``: text/paste (``text``), key (``key``), focus
    (``focused``), or mouse (zero-based ``x/y``, button 0/1/2, action
    press/release/move/wheel, optional ``delta``). Modifiers are a sequence of
    shift/alt/ctrl names or corresponding booleans. SGR mouse is used only when
    the child enabled it. Unsupported input protocols are never advertised.
    """
    def __init__(self, rows: int = 24, cols: int = 80, history_limit: int = 2000,
                 history_bytes: int = MAX_HISTORY_BYTES):
        if pyte is None:
            raise RuntimeError("Embedded terminal requires the terminal extra. From the Raven checkout, run: pipx install --force '.[terminal]'")
        self.rows, self.cols = _geometry(rows, cols)
        self.history_limit = max(0, min(int(history_limit), 10000))
        self.history_bytes_limit = max(0, min(int(history_bytes), MAX_HISTORY_BYTES))
        self._history: deque[tuple[str, int, bool]] = deque()
        self._history_bytes = 0
        self.history_start = 0
        self.unsupported = 0
        self.bells = 0
        self.title = ""
        self._replies = bytearray()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._state = "normal"
        self._control = ""
        self._string_kind = ""
        self._overflow = False
        self._combining = 0
        self._private: set[int] = set()
        self._keypad = False
        self._cursor_shape = 0
        self._active_link = 0
        self._links: dict[int, str] = {}
        self._next_link = 0
        self._clipboard_requests: deque[dict[str, Any]] = deque(maxlen=8)
        self._next_clipboard = 0
        self.clipboard_denied = 0
        self._normal = _Screen(self, self.cols, self.rows, True)
        self._alternate = _Screen(self, self.cols, self.rows, False)
        self.screen = self._normal
        self._stream = pyte.Stream(_Dispatch(self))
        self._stream.use_utf8 = False
        self._cache: dict[int, tuple[list[list[str]], str, list[dict[str, Any]]]] = {}
        self._pending_damage = set(range(self.rows))

    @property
    def alternate(self) -> bool:
        return self.screen is self._alternate

    def _reply(self, data: bytes) -> None:
        if len(self._replies) + len(data) <= MAX_REPLY:
            self._replies.extend(data)
        else:
            self.unsupported += 1

    def take_replies(self) -> bytes:
        value = bytes(self._replies)
        self._replies.clear()
        return value

    def _remember(self, text: str, wrapped: bool = False) -> None:
        size = len(text.encode("utf-8"))
        self._history.append((text, size, wrapped))
        self._history_bytes += size
        while self._history and (len(self._history) > self.history_limit or self._history_bytes > self.history_bytes_limit):
            _, removed, _ = self._history.popleft()
            self._history_bytes -= removed
            self.history_start += 1

    def clear_history(self) -> None:
        self.history_start += len(self._history)
        self._history.clear()
        self._history_bytes = 0

    def history_text(self) -> list[str]:
        return [line for line, _, _ in self._history]

    def history_wrapped(self) -> list[bool]:
        return [wrapped for _, _, wrapped in self._history]

    def reset(self) -> None:
        self._private.clear()
        self._keypad = False
        self._cursor_shape = 0
        self._active_link = 0
        self._links: dict[int, str] = {}
        self._next_link = 0
        self._clipboard_requests: deque[dict[str, Any]] = deque(maxlen=8)
        self._next_clipboard = 0
        self.clipboard_denied = 0
        self._normal.reset()
        self._alternate.reset()
        self.screen = self._normal
        self.clear_history()
        self._cache.clear()
        self._pending_damage.update(range(self.rows))

    def _mode(self, enabled: bool, modes: tuple[Any, ...], private: bool) -> None:
        for raw in modes:
            mode = int(raw)
            if private:
                if mode in (47, 1047, 1049):
                    if enabled and not self.alternate:
                        if mode == 1049:
                            self._normal.save_cursor()
                        if mode in (1047, 1049):
                            self._alternate.reset()
                        self.screen = self._alternate
                    elif not enabled and self.alternate:
                        self.screen = self._normal
                        if 1049 in self._private:
                            self._normal.restore_cursor()
                        self._private.difference_update((47, 1047, 1049))
                    self._cache.clear()
                    self._pending_damage.update(range(self.rows))
                elif mode == 1048:
                    (self.screen.save_cursor if enabled else self.screen.restore_cursor)()
                elif mode not in (1, 5, 6, 7, 25, 1000, 1002, 1003, 1004, 1006, 2004):
                    # DECCOLM may not resize beyond the PTY's negotiated geometry.
                    self.unsupported += 1
                    continue
                if enabled:
                    self._private.add(mode)
                else:
                    self._private.discard(mode)
                if mode not in (5, 6, 7, 25):
                    continue
            elif mode not in (4, 20):
                self.unsupported += 1
                continue
            method = self.screen.set_mode if enabled else self.screen.reset_mode
            method(mode, private=private)

    def resize(self, rows: int, cols: int) -> None:
        rows, cols = _geometry(rows, cols)
        if (rows, cols) == (self.rows, self.cols):
            return
        self.rows, self.cols = rows, cols
        for screen in (self._normal, self._alternate):
            screen.resize(lines=rows, columns=cols)
            screen.cursor.x = min(screen.cursor.x, cols - 1)
            screen.cursor.y = min(screen.cursor.y, rows - 1)
        self._cache.clear()
        self._pending_damage = set(range(rows))

    def _hyperlink(self, url: str) -> None:
        self._active_link = 0
        if not url:
            return
        try:
            allowed = urlsplit(url).scheme.lower() in ("https", "http", "file", "mailto")
        except ValueError:
            allowed = False
        if not allowed or len(url) > 2048 or any(not char.isprintable() for char in url):
            self.unsupported += 1
            return
        self._next_link += 1
        self._active_link = self._next_link
        self._links[self._active_link] = url
        while len(self._links) > 128:
            del self._links[next(iter(self._links))]

    def _clipboard_request(self, value: str) -> None:
        selection, separator, payload = value.partition(";")
        # Read requests are never answered. Writes require an explicit UI action;
        # the emulator only exposes bounded, session-scoped pending requests.
        if not separator or payload == "?" or selection not in ("", "c", "p", "pc", "cp"):
            self.clipboard_denied += 1
            return
        try:
            raw = base64.b64decode(payload, validate=True)
            text = raw.decode("utf-8")
        except (binascii.Error, UnicodeError, ValueError):
            self.clipboard_denied += 1
            return
        if len(raw) > 16384:
            self.clipboard_denied += 1
            return
        self._next_clipboard += 1
        self._clipboard_requests.append({"id": self._next_clipboard, "selection": selection or "c", "text": text})

    def dismiss_clipboard(self, request_id: int) -> None:
        self._clipboard_requests = deque((item for item in self._clipboard_requests if item["id"] != request_id), maxlen=8)

    def _finish_string(self) -> None:
        if self._string_kind == "osc" and not self._overflow:
            code, sep, value = self._control.partition(";")
            if sep and code == "8":
                _params, separator, url = value.partition(";")
                if separator:
                    self._hyperlink(url)
                else:
                    self.unsupported += 1
            elif sep and code == "52":
                self._clipboard_request(value)
            elif sep and code in ("0", "1", "2"):
                self.title = "".join(c for c in value[:512] if c.isprintable())
            else:
                self.unsupported += 1
        elif not self._overflow:
            self.unsupported += 1
        self._control = ""
        self._state = "normal"
        self._overflow = False

    def _csi(self, sequence: str) -> None:
        # Cursor shape is terminal chrome; it must not leak into the glyph grid.
        match = re.fullmatch(r"\x1b\[([0-6]?) q", sequence)
        if match:
            self._cursor_shape = int(match.group(1) or "0")
            return
        # xterm colon-separated direct/indexed colors, normalized for pyte 0.8.
        if sequence.endswith("m") and ":" in sequence:
            sequence = sequence.replace("38:2::", "38;2;").replace("48:2::", "48;2;").replace(":", ";")
        # Secondary device attributes: avoid falsely advertising unsupported DA.
        if sequence.startswith("\x1b[>") and sequence.endswith("c"):
            self._reply(b"\x1b[>0;0;0c")
            return
        self._parse(sequence)

    def _parse(self, text: str) -> None:
        try:
            self._stream.feed(text)
        except (ValueError, TypeError, IndexError, OverflowError):
            # pyte accepts several historical parameter forms. Malformed child
            # bytes must not terminate its coroutine and poison later output.
            self.unsupported += 1
            self._stream = pyte.Stream(_Dispatch(self))
            self._stream.use_utf8 = False

    def feed(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("PTY output must be bytes")
        # Split before decoding so a caller's giant chunk cannot grow parser strings.
        for offset in range(0, len(data), 65536):
            self._feed_text(self._decoder.decode(data[offset:offset + 65536]))
        self._pending_damage.update(self.screen.dirty)
        for row in self.screen.dirty:
            self._cache.pop(row, None)

    def _feed_text(self, text: str) -> None:
        index = 0
        while index < len(text):
            if self._state == "normal":
                match = _FRAMES.search(text, index)
                end = match.start() if match else len(text)
                if end > index:
                    self._parse(text[index:end])
                if not match:
                    break
                char = text[end]
                index = end + 1
                if char == "\x1b":
                    self._state = "escape"
                elif char == "\x9b":
                    self._state, self._control = "csi", "\x1b["
                else:
                    self._state = "string"
                    self._string_kind = "osc" if char == "\x9d" else "discard"
                    self._control = ""
                continue
            char = text[index]
            index += 1
            if self._state == "escape":
                if char == "[":
                    self._state, self._control = "csi", "\x1b["
                elif char in "]PX^_":
                    self._state, self._control = "string", ""
                    self._string_kind = "osc" if char == "]" else "discard"
                elif char == "=":
                    self._keypad, self._state = True, "normal"
                elif char == ">":
                    self._keypad, self._state = False, "normal"
                elif char == "\x1b":
                    continue
                else:
                    self._parse("\x1b" + char)
                    self._state = "normal"
            elif self._state == "csi":
                if char == "\x1b":
                    self._state, self._control, self._overflow = "escape", "", False
                elif char in "\x18\x1a":
                    self._state, self._control, self._overflow = "normal", "", False
                elif "@" <= char <= "~":
                    if not self._overflow:
                        self._csi(self._control + char)
                    self._state, self._control, self._overflow = "normal", "", False
                elif not self._overflow:
                    self._control += char
                    if len(self._control) > MAX_CSI:
                        self._control, self._overflow = "", True
                        self.unsupported += 1
            elif self._state in ("string", "string_escape"):
                if char in "\x18\x1a" or char == "\x9c" or (char == "\x07" and self._string_kind == "osc"):
                    self._finish_string()
                elif self._state == "string_escape" and char == "\\":
                    self._finish_string()
                elif char == "\x1b":
                    self._state = "string_escape"
                else:
                    if not self._overflow:
                        if self._state == "string_escape":
                            self._control += "\x1b"
                        self._control += char
                        limit = 24576 if self._string_kind == "osc" and self._control.startswith("52;") else MAX_CONTROL
                        if len(self._control) > limit:
                            self._control, self._overflow = "", True
                            self.unsupported += 1
                    self._state = "string"

    def consume_damage(self) -> list[int]:
        damaged = sorted(self._pending_damage | self.screen.dirty)
        self._pending_damage.clear()
        # Only unrendered damage invalidates cached rows; already published
        # pending damage remains reportable without rebuilding those cells.
        for row in self.screen.dirty:
            self._cache.pop(row, None)
        self.screen.dirty.clear()
        return [row for row in damaged if 0 <= row < self.rows]

    def snapshot(self, include_history: bool = True) -> dict[str, Any]:
        self._pending_damage.update(self.screen.dirty)
        for row in self.screen.dirty:
            self._cache.pop(row, None)
        lines = []
        links = []
        for row in range(self.rows):
            if row not in self._cache:
                cells = self.screen.buffer[row]
                segments: list[list[str]] = []
                plain = []
                row_links = []
                for col in range(self.cols):
                    cell = cells[col]
                    style = _style(cell.fg, cell.bg, cell.bold, cell.underscore, cell.reverse)
                    value = cell.data
                    if not value:
                        if col == 0 or wcwidth(cells[col - 1].data[:1]) != 2:
                            value = " "
                    elif wcwidth(value[:1]) == 2 and (col + 1 >= self.cols or cells[col + 1].data):
                        # A partial overwrite must not paint an orphan wide glyph.
                        value = " "
                    plain.append(value)
                    link_id = getattr(cell, "link", 0)
                    url = self._links.get(link_id)
                    if url:
                        if row_links and row_links[-1]["id"] == link_id and row_links[-1]["end"] == col:
                            row_links[-1]["end"] = col + 1
                        elif len(row_links) < 256:
                            row_links.append({"id": link_id, "row": row, "start": col, "end": col + 1, "url": url})
                    if segments and segments[-1][1] == style:
                        segments[-1][0] += value
                    else:
                        segments.append([value, style])
                self._cache[row] = (segments, "".join(plain), row_links)
            lines.append([segment[:] for segment in self._cache[row][0]])
            links.extend(dict(link) for link in self._cache[row][2][:max(0, 256 - len(links))] if link["id"] in self._links)
        self.screen.dirty.clear()
        cursor = self.screen.cursor
        mouse = next((mode for mode in (1003, 1002, 1000) if mode in self._private), 0)
        return {"rows": self.rows, "cols": self.cols, "lines": lines,
                "cursor": {"x": min(cursor.x, self.cols - 1), "y": cursor.y,
                           "visible": not cursor.hidden, "shape": self._cursor_shape, "blink": self._cursor_shape in (0, 1, 3, 5)},
                "modes": {"alternate": self.alternate, "application_cursor": 1 in self._private,
                          "application_keypad": self._keypad, "bracketed_paste": 2004 in self._private,
                          "mouse": mouse, "mouse_sgr": 1006 in self._private, "focus": 1004 in self._private},
                "title": self.title, "links": links, "clipboard_requests": [dict(item) for item in self._clipboard_requests],
                "clipboard_denied": self.clipboard_denied, "history": self.history_text() if include_history else [],
                "history_count": len(self._history), "history_start": self.history_start,
                "history_bytes": self._history_bytes, "history_bytes_limit": self.history_bytes_limit,
                "line_wrapped": [bool(getattr(self.screen.buffer[row], "wrapped", False)) for row in range(self.rows)],
                "history_wrapped": self.history_wrapped() if include_history else [],
                "unsupported": self.unsupported, "bells": self.bells,
                "engine": "pyte", "reflow": False}

    def encode(self, event: dict[str, Any]) -> bytes:
        kind = event.get("type", "key")
        mods = {str(m).lower() for m in event.get("modifiers", ())}
        mods.update(name for name in ("shift", "alt", "ctrl") if event.get(name))
        if kind == "raw":
            value = event.get("data", "")
            if (not isinstance(value, str) or len(value) > MAX_CSI or
                    not re.fullmatch(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|O[ -~]|[ -~])", value)):
                raise ValueError("raw terminal input must be one bounded CSI or SS3 frame")
            return value.encode("utf-8", "surrogateescape")
        if kind in ("text", "paste"):
            text = event.get("text", "")
            if not isinstance(text, str):
                raise ValueError("terminal text must be a string")
            data = text.encode("utf-8")
            if len(data) > MAX_INPUT:
                raise ValueError("terminal input exceeds 1 MiB transaction limit")
            if kind == "paste" and 2004 in self._private:
                # An embedded terminator would turn paste data into executable keys.
                if "\x1b[201~" in text or "\x9b201~" in text:
                    raise ValueError("paste contains a bracketed-paste terminator")
                return b"\x1b[200~" + data + b"\x1b[201~"
            return data
        if kind == "focus":
            return (b"\x1b[I" if event.get("focused") else b"\x1b[O") if 1004 in self._private else b""
        if kind == "mouse":
            return self._mouse(event, mods)
        if kind != "key":
            raise ValueError("unsupported terminal input event")
        key = str(event.get("key", ""))
        # Accept standard semantic labels, including modifier prefixes.
        while "-" in key:
            prefix, rest = key.split("-", 1)
            prefix = prefix.lower()
            prefix = {"control": "ctrl"}.get(prefix, prefix)
            if prefix not in ("ctrl", "alt", "shift"):
                break
            mods.add(prefix)
            key = rest
        label = key.lower().replace("key_", "")
        aliases = {"space": " ", "btab": "backtab", "pgup": "pageup", "pgdn": "pagedown"}
        if label in aliases:
            key = label = aliases[label]
        if label.startswith("f") and label[1:].isdigit() and 13 <= int(label[1:]) <= 24:
            label = "f" + str(int(label[1:]) - 12)
            mods.add("shift")
        if len(key) == 1:
            if "ctrl" in mods:
                char = key.upper()
                controls = {"?": 127, " ": 0, "2": 0, "3": 27, "4": 28, "5": 29, "6": 30, "7": 31, "8": 127}
                value = controls.get(char)
                if value is None and "@" <= char <= "_":
                    value = ord(char) & 31
                if value is None:
                    return b""
                result = bytes([value])
            else:
                result = key.encode("utf-8")
            return (b"\x1b" if "alt" in mods else b"") + result
        simple = {"enter": b"\r", "return": b"\r", "backspace": b"\x7f", "bs": b"\x7f",
                  "tab": b"\t", "escape": b"\x1b", "esc": b"\x1b", "backtab": b"\x1b[Z"}
        if label in simple:
            if label in ("backspace", "bs") and "ctrl" in mods:
                return (b"\x1b" if "alt" in mods else b"") + b"\x08"
            if label == "tab" and "shift" in mods:
                return b"\x1b[Z"
            return (b"\x1b" if "alt" in mods else b"") + simple[label]
        modifier = 1 + int("shift" in mods) + 2 * int("alt" in mods) + 4 * int("ctrl" in mods)
        arrows = {"up": "A", "down": "B", "right": "C", "left": "D", "home": "H", "end": "F"}
        if label in arrows:
            final = arrows[label]
            if modifier > 1:
                return f"\x1b[1;{modifier}{final}".encode()
            return ("\x1b" + ("O" if 1 in self._private else "[") + final).encode()
        tilde = {"insert": 2, "ic": 2, "delete": 3, "dc": 3, "pageup": 5, "ppage": 5,
                 "pagedown": 6, "npage": 6, "f5": 15, "f6": 17, "f7": 18, "f8": 19,
                 "f9": 20, "f10": 21, "f11": 23, "f12": 24}
        if label in tilde:
            suffix = f";{modifier}" if modifier > 1 else ""
            return f"\x1b[{tilde[label]}{suffix}~".encode()
        if label in ("f1", "f2", "f3", "f4"):
            final = "PQRS"[int(label[1:]) - 1]
            return (f"\x1b[1;{modifier}{final}" if modifier > 1 else "\x1bO" + final).encode()
        if label.startswith("kp_"):
            value = label[3:]
            keypad = {**{str(n): chr(ord("p") + n) for n in range(10)}, "decimal": "n", "enter": "M", "add": "k", "subtract": "m", "multiply": "j", "divide": "o"}
            if self._keypad and value in keypad:
                return ("\x1bO" + keypad[value]).encode()
            return {"decimal": b".", "enter": b"\r", "add": b"+", "subtract": b"-", "multiply": b"*", "divide": b"/"}.get(value, value.encode() if value.isdigit() else b"")
        return b""

    def _mouse(self, event: dict[str, Any], mods: set[str]) -> bytes:
        mode = next((value for value in (1003, 1002, 1000) if value in self._private), 0)
        if not mode:
            return b""
        action = event.get("action", "press")
        x, y = int(event.get("x", 0)), int(event.get("y", 0))
        if not 0 <= x < self.cols or not 0 <= y < self.rows:
            if action != "release":
                return b""
            x, y = min(max(0, x), self.cols - 1), min(max(0, y), self.rows - 1)
        button = int(event.get("button", 0))
        if action == "move" and (mode == 1000 or (mode == 1002 and button not in (0, 1, 2))):
            return b""
        if action not in ("press", "release", "move", "wheel"):
            return b""
        code = button if button in (0, 1, 2) else 3
        if action == "wheel":
            code = 64 if int(event.get("delta", 1)) > 0 else 65
        elif action == "move":
            code |= 32
        code |= 4 * int("shift" in mods) | 8 * int("alt" in mods) | 16 * int("ctrl" in mods)
        if 1006 in self._private:
            final = "m" if action == "release" else "M"
            return f"\x1b[<{code};{x + 1};{y + 1}{final}".encode()
        if x > 222 or y > 222:
            return b""  # Legacy X10 cannot represent these coordinates safely.
        if action == "release":
            code = (code & ~3) | 3
        return b"\x1b[M" + bytes((code + 32, x + 33, y + 33))
