"""Virtualized prose wrapping without inbox state or navigation effects."""

from __future__ import annotations
from array import array
from dataclasses import dataclass
from .. import layout as L


@dataclass(frozen=True)
class Wrapped:
    text: str
    offsets: array

    def __len__(self):
        return len(self.offsets) // 2

    def line(self, index):
        return self.text[self.offsets[2 * index] : self.offsets[2 * index + 1]]


def _wrap(text, width, ascii_=False):
    """One linear scan, compact offsets, no copied offscreen line strings."""
    width = max(1, width)
    if ascii_ and not text.isascii():
        text = text.encode("ascii", "replace").decode("ascii")
    offsets = array("I")
    if text.isascii():
        # Scheduler diagnostics are mostly ASCII. Scan paragraphs in C and
        # retain word boundaries without walking every character in Python.
        start = 0
        while True:
            newline = text.find("\n", start)
            end = len(text) if newline < 0 else newline
            if start == end:
                offsets.extend((start, end))
            while start < end:
                stop = min(start + width, end)
                if stop < end:
                    space = text.rfind(" ", start + 1, stop)
                    if space >= 0:
                        stop = space + 1
                offsets.extend((start, stop))
                start = stop
            if newline < 0:
                break
            start = newline + 1
        return Wrapped(text, offsets)
    start = used = 0
    for index, char in enumerate(text):
        if char == "\n":
            offsets.extend((start, index))
            start, used = index + 1, 0
            continue
        size = 1 if char.isascii() else L.vlen(char)
        if used and used + size > width:
            offsets.extend((start, index))
            start, used = index, 0
        used += size
    if start < len(text) or not offsets or text.endswith("\n"):
        offsets.extend((start, len(text)))
    return Wrapped(text, offsets)
