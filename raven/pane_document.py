"""Rows and interaction geometry travel through one pane transformation.

Documents contain published data only: no collector, worker, or file reader is
part of this contract. A viewport maps rows, chart axes, bounded controls, and
scrollbars together before publication, including empty first publications.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Sequence

from .layout import Row

from .ui.geometry import (Plot, Pane, map_plots, map_panes, take_plots,
                          take_panes, put_plots, put_panes)


@dataclass(frozen=True)
class DocumentTransform:
    """Map source rows into terminal rows; bounds are half-open cells."""

    offset: int = 0
    clip: tuple[int, int, int, int] | None = None
    row_map: Callable[[int], int | None] | None = None

    def row(self, source):
        mapped = self.row_map(source) if self.row_map is not None else source
        if mapped is None:
            return None
        mapped += self.offset
        if self.clip is not None and not self.clip[0] <= mapped < self.clip[2]:
            return None
        return mapped

    def charts(self, records):
        return tuple(map_plots(records, self.row, clip=self.clip))

    def scrollbars(self, records):
        return tuple(map_panes(records, row_map=self.row, clip=self.clip))

    def hits(self, records):
        result = []
        for y, kind, value in records:
            mapped = self.row(y)
            if mapped is None:
                continue
            if self.clip is not None and isinstance(value, dict) and "left" in value and "right" in value:
                value = dict(value, left=max(self.clip[1], value["left"]),
                             right=min(self.clip[3], value["right"]))
                if value["left"] >= value["right"]:
                    continue
            result.append((mapped, kind, value))
        return tuple(result)


@dataclass(frozen=True)
class PaneDocument:
    """One staged source document with all of its pointer geometry."""

    rows: Sequence[Row]
    hits: tuple[tuple[int, str, object], ...] = ()
    charts: tuple[Plot, ...] = ()
    scrollbars: tuple[Pane, ...] = ()

    @classmethod
    def capture(cls, app, rows, hits=(), *, chart_mark, scroll_mark=None):
        return cls(rows, tuple(hits), tuple(take_plots(app, chart_mark)),
                   tuple(take_panes(app, scroll_mark)) if scroll_mark is not None else ())

    def transform(self, rows, mapping):
        return replace(self, rows=rows, hits=mapping.hits(self.hits),
                       charts=mapping.charts(self.charts),
                       scrollbars=mapping.scrollbars(self.scrollbars))

    def body(self, start, *, prefix=(), width, limit):
        """Replace a navigation prefix and clip data and controls identically."""
        rows = list(prefix) + list(self.rows[start:start + max(0, limit - len(prefix))])
        mapping = DocumentTransform(len(prefix) - start, (len(prefix), 0, len(rows), width))
        return self.transform(rows, mapping)

    def publish(self, app):
        put_plots(app, self.charts)
        put_panes(app, self.scrollbars)
        return self.rows, list(self.hits)


def source_window(previous, window, *, top, height, header, overscan, seed_full=False):
    """Estimate visible source rows from the last wrapped-row publication.

A growing document extends beyond its previous final row. A resized, scrolled
document without a map requests one complete bounded composition rather than
publishing blank placeholders for its first visible frame.
    """
    height = max(1, height)
    if not previous or not previous.get("mapping"):
        return None if seed_full and top > 0 else window or (0, height + overscan * 2)
    mapping = previous["mapping"]
    painted = top + previous["sticky"]
    lower, upper = painted - overscan, painted + height + overscan
    first = last_visible = last = last_position = None
    # One bounded pass, with no per-frame list of every visible source row.
    for index, position in mapping.items():
        if last is None or index > last:
            last, last_position = index, position
        if lower <= position < upper:
            first = index if first is None else min(first, index)
            last_visible = index if last_visible is None else max(last_visible, index)
    if first is None:
        return None if seed_full and top > 0 else window or (0, height + overscan * 2)
    missing = upper - last_position
    if missing > 0:
        last_visible = max(last_visible, last + missing)
    return (max(0, first - header - overscan),
            max(0, last_visible - header + overscan + 1))
