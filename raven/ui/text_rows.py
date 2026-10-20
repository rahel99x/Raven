"""Bounded semantic row wrapping independent of workspace composition."""

from __future__ import annotations
from .. import layout as L
from typing import Optional

MAX_REFLOW_ROWS = 2048


def _join_characters(characters) -> L.Row:
    segments = []
    text, style = [], None
    for character, character_style in characters:
        if style is not None and character_style != style:
            segments.append(("".join(text), style))
            text = []
        text.append(character)
        style = character_style
    if text:
        segments.append(("".join(text), style or ""))
    return segments


def _wrap_row(row: L.Row, width: int) -> list[L.Row]:
    """Wrap metadata at spaces while preserving its exact semantic cell styles."""
    if width <= 0:
        return [[]]
    if L.vlen(L.row_text(row)) <= width:
        return [list(row)]
    lines, current, used, last_space = [], [], 0, -1
    for text, style in row:
        for character in text[:131072]:
            cell_width = 1 if character.isascii() else L.vlen(character)
            if cell_width > width:
                character, cell_width = "?", 1
            if character == "\n":
                lines.append(_join_characters(current))
                current, used, last_space = [], 0, -1
                continue
            if used + cell_width > width:
                if last_space >= 0:
                    lines.append(_join_characters(current[:last_space]))
                    current = current[last_space + 1 :]
                else:
                    lines.append(_join_characters(current))
                    current = []
                used = sum(L.vlen(char) for char, _ in current)
                last_space = max(
                    (n for n, (char, _) in enumerate(current) if char.isspace()),
                    default=-1,
                )
            current.append((character, style))
            used += cell_width
            if character.isspace():
                last_space = len(current) - 1
            if len(lines) >= MAX_REFLOW_ROWS:
                return lines
    if current or not lines:
        lines.append(_join_characters(current))
    return lines


def _section_title(row: L.Row) -> Optional[str]:
    text = L.row_text(row).lstrip()
    if not text.startswith(("──", "--")):
        return None
    title = text.lstrip("─- ").rstrip("─- ").strip()
    return title.lower() if title else None


def _replace(row, start, text, style, width, *, fitted=False):
    """Replace display cells without splitting a wide glyph or copying styles."""
    before = L.clip_row(row, start)
    used = L.vlen(L.row_text(before))
    if used < start:
        before.append((" " * (start - used), "bg:canvas"))
    end = start + L.vlen(text)
    after, position = [], 0
    for offset, (segment, segment_style) in enumerate(row):
        if position >= end:
            if position == end and not L.vlen(segment):
                continue
            # The suffix is already fitted to the canvas. Its graph cells do
            # not need another width scan merely to paint a divider before it.
            after.extend(row[offset:])
            break
        length = L.vlen(segment)
        if position + length <= end:
            position += length
            continue
        skipped, index = 0, 0
        for index, character in enumerate(segment):
            cells = L._character_width(character)
            if position + skipped >= end:
                break
            skipped += cells
        else:
            index = len(segment)
        if position + skipped > end:
            after.append((" " * (position + skipped - end), segment_style))
        if index < len(segment):
            after.append((segment[index:], segment_style))
        position += length
    result = before + [(text, style)] + after
    if fitted:
        # clip_row stops at the exact edge before trailing empty/combining
        # segments; preserve that rule without rescanning the full suffix.
        while result and not L.vlen(result[-1][0]):
            result.pop()
        return result
    return L.clip_row(result, width)
