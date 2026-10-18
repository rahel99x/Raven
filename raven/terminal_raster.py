"""Opt-in Kitty transport prototype over existing, source-faithful Braille cells.

The regular text canvas remains authoritative for copy, export, coordinates and
unsupported terminals. This prototype does not increase scientific resolution.
"""
from __future__ import annotations

import base64
import os
import secrets
import time

from . import layout as L

MAX_COLUMNS = 80
MAX_ROWS = 24
MAX_ENCODED = 128 << 10
MIN_INTERVAL = .5
_DOTS = ((0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (0, 3), (1, 3))


def capability(option, stream, environ):
    if option != 'kitty':
        return False, 'disabled' if option == 'off' else 'Sixel/other protocols are not qualified'
    if not stream.isatty():
        return False, 'not an interactive terminal'
    if any(environ.get(key) for key in ('TMUX', 'STY', 'SSH_CONNECTION', 'SSH_TTY')):
        return False, 'multiplexer/SSH graphics forwarding is not qualified'
    if environ.get('TERM') != 'xterm-kitty' or not environ.get('KITTY_WINDOW_ID', '').isdigit():
        return False, 'explicit Kitty terminal capability is unavailable'
    return True, 'experimental Kitty rendering of retained Braille occupancy'


def _cells(row, left, right):
    found, column = [], 0
    for char in L.row_text(row):
        width = L.vlen(char)
        if left <= column < right:
            if width != 1 or char != ' ' and not 0x2800 <= ord(char) <= 0x28ff:
                return None
            found.append(char)
        column += width
        if column >= right:
            break
    return found if len(found) == right - left else None


def _uniform_style(rows, rect):
    """Monochrome output must not erase threshold/category distinctions."""
    styles = []
    for y in range(rect.top, rect.bottom):
        column = 0
        for text, style in rows[y]:
            for char in text:
                if rect.left <= column < rect.right and char not in {' ', '\u2800'}:
                    if styles and style != styles[0]:
                        return False
                    if not styles:
                        styles.append(style)
                column += L.vlen(char)
                if column >= rect.right:
                    break
            if column >= rect.right:
                break
    return True


def encode(cells, image_id, top, left):
    if (not cells or len(cells) > MAX_ROWS or not cells[0] or len(cells[0]) > MAX_COLUMNS
            or any(len(row) != len(cells[0]) for row in cells)
            or type(image_id) is not int or not 1 <= image_id < 1 << 32
            or any(char != ' ' and not 0x2800 <= ord(char) <= 0x28ff for row in cells for char in row)):
        raise ValueError('Invalid bounded terminal image')
    height, columns = len(cells), len(cells[0])
    width, pixels_high = columns * 2, height * 4
    rgba = bytearray(width * pixels_high * 4)
    for y, row in enumerate(cells):
        for x, char in enumerate(row):
            bits = ord(char) - 0x2800 if char != ' ' else 0
            for bit, (dx, dy) in enumerate(_DOTS):
                if bits & (1 << bit):
                    offset = ((y * 4 + dy) * width + x * 2 + dx) * 4
                    rgba[offset:offset + 4] = bytes((237, 139, 193, 255))
    payload = base64.b64encode(rgba).decode('ascii')
    first = f'a=T,f=32,s={width},v={pixels_high},c={columns},r={height},i={image_id},q=2,C=1,z=1'
    chunks = []
    for offset in range(0, len(payload), 4096):
        more = int(offset + 4096 < len(payload))
        options = first + f',m={more}' if offset == 0 else f'm={more},q=2'
        chunks.append('\x1b_G' + options + ';' + payload[offset:offset + 4096] + '\x1b\\')
    raw = f'\x1b7\x1b[{top + 1};{left + 1}H' + ''.join(chunks) + '\x1b8'
    if len(raw) > MAX_ENCODED:
        raise ValueError('Terminal image exceeds encoded byte budget')
    return raw


def _mask(rows, rect):
    result = list(rows)
    for y in range(rect.top, rect.bottom):
        parts, column = [], 0
        for text, style in rows[y]:
            chars = []
            for char in text:
                width = L.vlen(char)
                chars.append(' ' if rect.left <= column < rect.right else char)
                column += width
            parts.append((''.join(chars), style))
        result[y] = parts
    return result


class TerminalRaster:
    def __init__(self, stream, option='off', *, environ=None, clock=time.monotonic):
        self.stream, self.clock = stream, clock
        self.enabled, self.status = capability(option, stream, os.environ if environ is None else environ)
        self.image_id = secrets.randbelow((1 << 31) - 1) + 1
        self.active = self.planned = None
        self.encoded = ''
        self.last_draw = float('-inf')
        self.bytes_written = 0

    @property
    def changed(self):
        return self.active != self.planned

    def prepare(self, app, rows, overlays, width, height):
        self.planned, self.encoded = None, ''
        selection = getattr(app, 'text_selection_state', {}) or {}
        state = getattr(app, 'chart_interaction_state', {}) or {}
        if (not self.enabled or getattr(app, 'mode', 'main') != 'main'
                or selection.get('selection') or selection.get('capture')
                or state.get('capture') or getattr(app, 'ascii', False)):
            return rows
        for plot in state.get('plots', ()):
            rect = plot.rect
            if (plot.kind != 'metric' or rect != plot.visible
                    or not 0 <= rect.top < rect.bottom <= min(len(rows), height)
                    or not 0 <= rect.left < rect.right <= width
                    or rect.right - rect.left > MAX_COLUMNS or rect.bottom - rect.top > MAX_ROWS):
                continue
            if any(rect.top <= y < rect.bottom and x < rect.right and x + L.vlen(L.row_text(row)) > rect.left
                   for y, x, row in overlays):
                continue
            if not _uniform_style(rows, rect):
                continue
            cells = [_cells(rows[y], rect.left, rect.right) for y in range(rect.top, rect.bottom)]
            if any(row is None for row in cells):
                continue
            identity = (getattr(app, 'mode', None), getattr(app, 'tab', None),
                        getattr(app, 'selected_id', None), width, height, plot.key, rect,
                        tuple(tuple(row) for row in cells))
            if identity != self.active and self.clock() - self.last_draw < MIN_INTERVAL:
                return rows  # fresh text, never an old image under a new axis
            self.planned = identity
            if self.planned != self.active:
                self.encoded = encode(cells, self.image_id, rect.top, rect.left)
            return _mask(rows, rect)
        return rows

    def _write(self, raw):
        self.stream.write(raw)
        self.stream.flush()
        self.bytes_written += len(raw)

    def before_flush(self):
        """Delete our old placement before curses exposes the replacement canvas."""
        if self.active is not None and self.changed:
            try:
                self._write(f'\x1b_Ga=d,d=I,i={self.image_id},q=2;\x1b\\')
            except (OSError, ValueError):
                self.enabled = False
                self.status = 'terminal write failed; text fallback'
            self.active = None

    def after_flush(self):
        if not self.enabled:
            return self.planned is None
        if self.planned is not None and self.planned != self.active:
            try:
                self._write(self.encoded)
            except (OSError, ValueError):
                self.active = self.planned  # remove any partially transmitted image too
                self.enabled = False
                self.status = 'terminal write failed; text fallback'
                self.close()
                return False
            self.active = self.planned
            self.last_draw = self.clock()
        return True

    def close(self):
        self.planned = None
        self.before_flush()

