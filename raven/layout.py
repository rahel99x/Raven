"""Pure layout: columns fitted to a width, clipping with an ellipsis, bars, sparklines, rules and boxes.

Every panel is built as rows of ``(text, style)`` segments; painters (curses, ANSI, plain text) only draw them.
Styles are '+'-joined names: bold dim rev under green yellow red cyan magenta blue white."""

from __future__ import annotations

import unicodedata
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from .log_text import display_text

Seg = Tuple[str, str]
Row = List[Seg]

SPARK = "▁▂▃▄▅▆▇█"
SPARK_ASCII = ".:-=+*#@"
FRACTIONS = " ▏▎▍▌▋▊▉█"


@lru_cache(maxsize=4096)
def _character_width(char: str) -> int:
    if unicodedata.category(char) in {"Cf", "Mn", "Me"}:
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _unicode_width(s: str) -> int:
    n = 0
    for ch in s:
        n += 1 if ch.isascii() else _character_width(ch)
    return n


_short_unicode_width = lru_cache(maxsize=2048)(_unicode_width)


def vlen(s: str) -> int:
    """Display width, with bounded reuse for repeated terminal labels.

    Long log lines bypass the string cache; its retained keys are limited to
    2,048 strings of at most 512 characters. Wide and combining cells use the
    same Unicode rules regardless of whether a value is cached.
    """
    if not s.isprintable():
        s = display_text(s)
    if s.isascii():
        return len(s)
    if len(s) == 1:
        return _character_width(s)
    return _short_unicode_width(s) if len(s) <= 512 else _unicode_width(s)


def cut(text, width: int, ascii_: bool = False) -> str:
    """Clip to ``width`` display columns with an ellipsis."""
    text = display_text(str(text))
    if width <= 0:
        return ""
    # Inspect at most the visible prefix. Wide log lines and large tables must
    # not scan all offscreen Unicode characters just to draw one terminal row.
    prefix = truncate(text, width)
    if len(prefix) == len(text):
        return text
    e = "~" if ascii_ else "…"
    return truncate(prefix, width - 1) + e


def pad(text, width: int, align: str = "<") -> str:
    text = display_text(str(text))
    gap = max(0, width - vlen(text))
    if align == ">":
        return " " * gap + text
    if align == "^":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def row_text(row: Row) -> str:
    return "".join(t for t, _ in row)


@dataclass
class Column:
    key: str
    title: str
    lo: int
    hi: int
    align: str = "<"
    flex: bool = False
    style: str = ""  # style of the cells (a callable on the row may override)


def fit_columns(
    cols: Sequence[Column],
    rows: Iterable[dict],
    width: int,
    gap: int = 2,
    droppable: Sequence[str] = (),
    floor: int = 6,
) -> Tuple[Dict[str, int], List[Column]]:
    """Column widths that fit ``width``: the natural width of header and content within [lo, hi]; the widest flexible
    column shrinks first (down to its lo); then the ``droppable`` columns leave in order; as the last resort the
    flexible columns shrink to ``floor``.  Returns (widths, columns kept)."""
    rows = list(rows)
    kept = list(cols)
    natural = {}
    for column in kept:
        natural[id(column)] = max(
            vlen(column.title),
            max((vlen(str(row.get(column.key, ""))) for row in rows), default=0),
        )

    def shrink(lo_of):
        w = {}
        for c in kept:
            w[c.key] = max(lo_of(c), min(c.hi, natural[id(c)]))
        excess = sum(w.values()) + gap * (len(kept) - 1) - width
        while excess > 0:
            flex = [c for c in kept if c.flex and w[c.key] > lo_of(c)]
            if not flex:
                break
            c = max(flex, key=lambda c: w[c.key])
            w[c.key] -= 1
            excess -= 1
        return w, excess

    w, excess = shrink(lambda c: c.lo)
    for key in droppable:
        if excess <= 0:
            break
        if any(c.key == key for c in kept):
            kept = [c for c in kept if c.key != key]
            w, excess = shrink(lambda c: c.lo)
    if excess > 0:
        w, excess = shrink(lambda c: min(c.lo, floor) if c.flex else c.lo)
    return w, kept


def table(
    cols: Sequence[Column],
    rows: Sequence[dict],
    width: int,
    ascii_: bool = False,
    indent: str = " ",
    gap: int = 2,
    droppable: Sequence[str] = (),
    header_style: str = "cyan+bold",
    cursor: Optional[int] = None,
    marks: Iterable[int] = (),
    mark_char: Optional[str] = None,
    header_cells: Optional[list] = None,
) -> Tuple[List[Row], List[Column]]:
    """Header row plus one row per dict; ``rows[i]['_style']`` styles a row, ``rows[i]['_styles']`` a cell; the
    ``cursor`` row is reversed; marked rows carry ``mark_char`` in a leading one-character column.
    ``header_cells`` receives (key, left, right) visible, half-open display-column
    bounds from this exact fit, excluding gaps and the optional mark column."""
    cols = list(cols)
    marks = set(marks)
    if mark_char is not None:
        cols = [Column("_mark", "", 1, 1)] + cols
    w, kept = fit_columns(
        cols, rows, width - vlen(indent), gap=gap, droppable=droppable
    )
    if header_cells is not None:
        x = vlen(indent)
        for column in kept:
            right = min(width, x + w[column.key])
            if column.key != "_mark" and x < right:
                header_cells.append((column.key, x, right))
            x += w[column.key] + gap
    sep = " " * gap
    out: List[Row] = [
        [
            (indent, ""),
            (
                sep.join(
                    pad(cut(c.title, w[c.key], ascii_), w[c.key], c.align) for c in kept
                ),
                header_style,
            ),
        ]
    ]
    for i, r in enumerate(rows):
        segs: Row = [(indent, "")]
        for n, c in enumerate(kept):
            if c.key == "_mark":
                cell = mark_char if i in marks else (r.get("_mark") or " ")
            else:
                cell = r.get(c.key, "")
            style = (r.get("_styles") or {}).get(c.key, r.get("_style", c.style))
            segs.append(
                (
                    pad(cut(cell, w[c.key], ascii_), w[c.key], c.align)
                    + (sep if n < len(kept) - 1 else ""),
                    style,
                )
            )
        if cursor is not None and i == cursor:
            segs = [(t, "rev") for t, _ in segs]
        out.append(segs)
    return [clip_row(row, width) for row in out], kept


class Glyphs:
    def __init__(self, ascii_: bool):
        self.ascii = ascii_
        self.full, self.empty = ("#", "-") if ascii_ else ("█", "░")
        self.rule = "-" if ascii_ else "─"
        self.spark = SPARK_ASCII if ascii_ else SPARK
        self.cursor = ">" if ascii_ else "▸"
        self.mark = "*" if ascii_ else "●"
        self.dot = "-" if ascii_ else "·"
        self.box = (
            ("+", "+", "+", "+", "-", "|") if ascii_ else ("┌", "┐", "└", "┘", "─", "│")
        )
        self.arrow = "->" if ascii_ else "→"
        self.pin = "^" if ascii_ else "⚲"


def level(frac: float) -> str:
    return "green" if frac < 0.6 else ("yellow" if frac < 0.9 else "red")


def bar(g: Glyphs, frac: Optional[float], width: int, invert: bool = False) -> Seg:
    """A meter with eighth-cell Unicode precision and a distinct unknown state."""
    width = max(0, width)
    known = frac is not None and math.isfinite(frac)
    f = max(0.0, min(1.0, frac)) if known else 0.0
    if g.ascii:
        n = int(round(f * width))
        text = g.full * n + g.empty * (width - n)
    else:
        units = min(width * 8, int(round(f * width * 8)))
        full, partial = divmod(units, 8)
        text = g.full * full + (FRACTIONS[partial] if partial else "")
        text += g.empty * (width - full - bool(partial))
    return (text, "dim" if not known else level(1 - f if invert else f))


def gradient_bar(
    g: Glyphs,
    frac: Optional[float],
    width: int,
    start: str = "#22d3ee",
    end: str = "#a78bfa",
) -> Row:
    """A smooth opaque meter, with a dark track and an explicit unknown marker.

    Colours are style metadata; characters still form a useful meter when colour is disabled.
    """
    text, style = bar(g, frac, width)
    if g.ascii:
        return [(text, style)]
    if frac is None or not math.isfinite(frac):
        width = max(0, width)
        left = max(0, (width - 1) // 2)
        return [
            (
                g.empty * left
                + ("?" if width else "")
                + g.empty * max(0, width - left - 1),
                "dim",
            )
        ]
    from .palette import gradient_style

    rows: Row = []
    for i, ch in enumerate(text):
        style = (
            gradient_style(start, end, i / max(1, width - 1))
            if ch != g.empty
            else "track"
        )
        rows.append((ch, style))
    return rows


def spark(g: Glyphs, values: Sequence[Optional[float]], width: int = 12) -> str:
    """The most recent samples, preserving missing positions instead of shifting time."""
    if width <= 0:
        return ""
    vals = values[-width:]
    return "".join(
        " "
        if v is None or not math.isfinite(v)
        else g.spark[min(7, int(max(0.0, min(1.0, v)) * 7.999))]
        for v in vals
    ).rjust(width)


def rule(g: Glyphs, width: int, title: str = "", style: str = "dim") -> Row:
    if not title:
        return [(" " + g.rule * max(0, width - 1), style)] if width > 0 else []
    title = cut(f" {title} ", max(0, width - 3), g.ascii)
    return clip_row(
        [
            (" " + g.rule * 2, style),
            (title, "cyan+bold"),
            (g.rule * max(0, width - 3 - vlen(title)), style),
        ],
        width,
    )


def metrics(g: Glyphs, items: Sequence[Tuple[str, str, str]], width: int) -> Row:
    """A quiet metric ribbon: evenly spaced values with explicit labels, also legible without colour.

    Each item is (label, value, style). Narrow terminals keep complete leading metrics instead of
    cutting through the last value; the primary metric is always retained.
    """
    if not items or width <= 1:
        return []
    count = min(len(items), max(1, (width - 1) // 20))
    chosen = items[:count]
    cell = (width - 1) // count
    out: Row = [(" ", "")]
    for i, (label, value, style) in enumerate(chosen):
        usable = cell - (3 if i < count - 1 else 1)
        label = cut(label, max(0, usable // 2), g.ascii)
        value = cut(value, max(0, usable - vlen(label) - 1), g.ascii)
        out += [(label + " ", "dim"), (value, style + "+bold" if style else "bold")]
        if i < count - 1:
            out += [
                (" " * max(1, cell - vlen(label) - vlen(value) - 3), ""),
                (g.box[5] + " ", "dim"),
            ]
    return clip_row(out, width)


def box(
    g: Glyphs,
    lines: Sequence[Row],
    width: int,
    height: int,
    title: str,
    min_width: int = 40,
) -> List[Tuple[int, int, Row]]:
    """A bordered overlay centred on the screen: (y, x, row) triples."""
    if width <= 0 or height <= 0:
        return []
    if width < 4 or height < 3:
        return [(0, 0, fill_row([(title, "bold")], width, "text+bg:surface"))]
    inner = max((sum(vlen(t) for t, _ in l) for l in lines), default=0)
    w = min(width - 2, max(min_width, inner + 4))
    h = min(max(2, height - 2), len(lines) + 2)
    x0, y0 = max(0, (width - w) // 2), max(0, (height - h) // 2)
    tl, tr, bl, br, hz, vt = g.box
    t = cut(hz * 2 + f" {title} ", w - 2, g.ascii)
    rows = [(y0, x0, [(tl + t + hz * max(0, w - 2 - vlen(t)) + tr, "cyan+bold")])]
    for i in range(h - 2):
        segs: Row = [(vt, "bold")]
        if i < len(lines):
            used = 0
            for text, style in lines[i]:
                text = cut(text, w - 2 - used, g.ascii)
                segs.append((text, style))
                used += vlen(text)
            segs.append((" " * (w - 2 - used), ""))
        else:
            segs.append((" " * (w - 2), ""))
        segs.append((vt, "bold"))
        rows.append((y0 + 1 + i, x0, segs))
    rows.append((y0 + h - 1, x0, [((bl + hz * (w - 2) + br)[:w], "bold")]))
    return [(y, x, fill_row(row, w, "text+bg:surface")) for y, x, row in rows]


def truncate(text: str, width: int) -> str:
    """Hard cut to ``width`` display columns (no ellipsis: the screen edge)."""
    text = display_text(text)
    if text.isascii():
        return text[: max(0, width)]
    out, used = [], 0
    for ch in text:
        w = _character_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def _clip_row(row: Row, width: int):
    """Clip once and retain its measured width for callers that fill cells."""
    out, used = [], 0
    for text, style in row:
        text = display_text(str(text))
        if used >= width:
            break
        size = _character_width(text) if len(text) == 1 else vlen(text)
        if size > width - used:
            text = truncate(text, width - used)
            size = vlen(text)
        out.append((text, style))
        used += size
    return out, used


def clip_row(row: Row, width: int) -> Row:
    """Cut a row to ``width`` display columns."""
    return _clip_row(row, width)[0]


@lru_cache(maxsize=4096)
def _filled_style(style, cell_style):
    return "+".join(part for part in (style, cell_style) if part)


def fill_row(row: Row, width: int, style: str = "bg:surface") -> Row:
    """Fit a row and fill its entire canvas, preserving explicit cell colours."""
    width = max(0, width)
    fitted, used = _clip_row(row, width)
    out = [
        (
            text,
            _filled_style(style, cell_style)
            if len(style) <= 512 and len(cell_style) <= 512
            else "+".join(part for part in (style, cell_style) if part),
        )
        for text, cell_style in fitted
    ]
    gap = width - used
    if gap:
        out.append((" " * gap, style))
    return out


def panel_title(
    g: Glyphs,
    title: str,
    width: int,
    focused: bool = False,
    position: Optional[Tuple[int, int, int]] = None,
) -> Row:
    """A restrained panel heading with a visible focus and scrolling affordance."""
    if width <= 0:
        return []
    marker = (">" if g.ascii else "▸") if focused else " "
    style = "accent+bold+bg:surface-raised" if focused else "secondary+bold+bg:surface"
    title = " " + marker + " " + title
    suffix = ""
    if position is not None:
        start, end, count = position
        if count > max(0, end - start):
            suffix = f" {start + 1 if count else 0}-{end}/{count} "
    if vlen(suffix) >= width:
        suffix = ""
    return fill_row(
        [
            (
                pad(cut(title, width - vlen(suffix), g.ascii), width - vlen(suffix)),
                style,
            ),
            (suffix, "muted+bg:surface-raised" if focused else "muted+bg:surface"),
        ],
        width,
        "",
    )


def scroll_window(
    rows: Sequence[Row],
    hits: Sequence[Tuple[int, str, str]],
    width: int,
    height: int,
    top: int = 0,
) -> Tuple[List[Row], List[Tuple[int, str, str]], int]:
    """Slice a panel without changing hit identities or borrowing adjacent rows."""
    height, width = max(0, height), max(0, width)
    top = max(0, min(int(top), max(0, len(rows) - height)))
    window = [clip_row(row, width) for row in rows[top : top + height]]
    visible_hits = [
        (y - top, kind, key) for y, kind, key in hits if top <= y < top + height
    ]
    return window, visible_hits, top


ANSI = {
    "bold": "1",
    "dim": "2",
    "rev": "7",
    "under": "4",
    "green": "32",
    "yellow": "33",
    "red": "31",
    "cyan": "36",
    "magenta": "35",
    "blue": "34",
    "white": "37",
}


def to_text(
    rows: Sequence[Row],
    width: int,
    color: bool = False,
    color_depth: Optional[int] = None,
    theme: str = "default",
    canvas: bool = False,
) -> str:
    """Rows as lines of text, with optional complete themed ANSI canvas."""
    if color:
        from .palette import (
            ansi_codes,
            cell_style,
            colors_disabled,
            canonical_theme,
            color_depth as detect_depth,
        )

        color_depth = detect_depth() if color_depth is None else color_depth
        canvas = bool(
            canvas
            and color_depth
            and not colors_disabled()
            and canonical_theme(theme) not in ("terminal", "mono", "reader")
        )
    out = []
    for row in rows:
        parts = []
        row = [(display_text(str(text)), style) for text, style in row]
        fitted = fill_row(row, width, "") if color and canvas else clip_row(row, width)
        for text, style in fitted:
            if color and canvas:
                style = cell_style(style, theme)
            if color and style:
                codes = ansi_codes(style, color_depth=color_depth, theme=theme)
                parts.append(f"\033[{codes}m{text}\033[0m" if codes else text)
            else:
                parts.append(text)
        out.append("".join(parts).rstrip() if not color else "".join(parts))
    return "\n".join(out)
