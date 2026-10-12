"""Bounded native chart rasters; controls, geometry and source age stay live."""
from __future__ import annotations

from collections import OrderedDict
from array import array
import copy
import os
import time

from . import charts, layout as L

MAX_ENTRIES = 64
MAX_SERIES_POINTS = 64000
MAX_POINTS = 512000
MAX_CELLS = 65536
MAX_PREPARED_ENTRIES = 32
MAX_PREPARED_POINTS = 512000
_UNCACHEABLE = object()


def coalesce_row(row):
    """Join adjacent equal styles without changing any glyph or style byte."""
    result, fragments, previous = [], [], None
    for text, style in row:
        if (fragments and type(style) is str and type(previous) is str
                and style == previous):
            fragments.append(text)
        else:
            if fragments:
                result.append(("".join(fragments), previous))
            fragments, previous = [text], style
    if fragments:
        result.append(("".join(fragments), previous))
    return result


def _freeze(value, depth=0):
    """Keep exact numeric representations and detect in-place corrections."""
    if depth > 4:
        return _UNCACHEABLE
    kind = type(value)
    if kind in (type(None), bool, int, str):
        return kind, value
    if kind is float:
        return kind, value.hex()
    if kind in (list, tuple) and len(value) <= MAX_SERIES_POINTS:
        # Native series normally contain plain floats. A C-level packed copy
        # retains every bit (including signed zero and NaN) without allocating
        # a Python type/value tuple for each point on every maintenance frame.
        if value and all(type(item) is float for item in value):
            return kind, float, array("d", value).tobytes()
        if value and all(type(item) is int for item in value):
            return kind, int, tuple(value)
        parts = tuple(_freeze(item, depth + 1) for item in value)
        return _UNCACHEABLE if any(item is _UNCACHEABLE for item in parts) else (kind, parts)
    if kind is dict and len(value) <= 64 and all(type(key) is str for key in value):
        parts = tuple((key, _freeze(item, depth + 1)) for key, item in sorted(value.items()))
        return _UNCACHEABLE if any(item is _UNCACHEABLE for _, item in parts) else (kind, parts)
    return _UNCACHEABLE


class MetricRasterCache:
    def __init__(self, *, budget=None):
        self.points = self.cells = 0
        from .raven_limits import BudgetCache
        self.entries = BudgetCache(budget,self._expired) if budget else OrderedDict()
        self.time_points = TimePointCache(budget=budget)

    def _expired(self, entry):
        self.points -= entry['points']
        self.cells -= entry['cells']

    def render(self, glyphs, values, width, height, *, filled=False, theme="", compact_rows=False, **options):
        painter = charts.vbar_chart if filled else charts.braille_chart
        metadata = {}
        # Unknown extension options use the original painter. The cache must
        # never impose a new limit on data that a renderer already supports.
        data = _freeze(values)
        settings = _freeze(options)
        zone = (os.environ.get("TZ"), time.tzname, time.timezone, time.daylight)
        key = (filled, compact_rows, width, height, glyphs.ascii, glyphs.spark, glyphs.box,
               glyphs.full, glyphs.dot, glyphs.rule, theme, zone, data, settings)
        cacheable = data is not _UNCACHEABLE and settings is not _UNCACHEABLE
        entry = self.entries.get(key) if cacheable else None
        if entry is not None:
            self.entries.move_to_end(key)
            return [list(row) for row in entry["rows"]], copy.deepcopy(entry["metadata"])
        # Callable colors/formatters correctly bypass complete raster reuse,
        # but cannot change measured time buckets. Reuse that bounded numeric
        # preparation independently of presentation. The painter supplies its
        # actual fitted-axis width, so resizing and zoom remain exact.
        time_cache = self.time_points.bind(values, options.get("sample_times"), data)
        rows = painter(glyphs, values, width, height, metadata=metadata,
                       _time_cache=time_cache, **options)
        if compact_rows:
            rows = [coalesce_row(row) for row in rows]
        if not cacheable:
            return rows, metadata
        count = len(values) + len(options.get("sample_times") or ())
        cells = sum(L.vlen(L.row_text(row)) for row in rows)
        if cacheable and count <= MAX_POINTS and cells <= MAX_CELLS:
            self.entries[key] = {"rows": tuple(tuple(row) for row in rows),
                                 "metadata": copy.deepcopy(metadata), "points": count, "cells": cells}
            self.points += count
            self.cells += cells
            while (len(self.entries) > MAX_ENTRIES or self.points > MAX_POINTS or self.cells > MAX_CELLS):
                _, old = self.entries.popitem(last=False)
                self.points -= old["points"]
                self.cells -= old["cells"]
        return rows, metadata


class TimePointCache:
    """Exact time-bucket reuse; no downsampling or source-identity guesses.

    Source keys are detached representations, never object IDs. In-place edits
    invalidate them. Entries contain immutable vertices and detail tuples; each
    painter receives fresh containers. Only the UI-owned raster cache uses this
    object, just like its existing complete-raster cache.
    """
    def __init__(self, *, budget=None):
        self.points = 0
        from .raven_limits import BudgetCache
        self.entries = BudgetCache(budget,self._expired) if budget else OrderedDict()

    def _expired(self, entry):
        self.points -= entry[3]

    def bind(self, values, sample_times, frozen_values=_UNCACHEABLE):
        if sample_times is None or frozen_values is _UNCACHEABLE:
            return None
        frozen_times = _freeze(sample_times)
        if frozen_times is _UNCACHEABLE:
            return None
        source = frozen_values, frozen_times
        source_points = len(values) + len(sample_times)

        def prepare(values, sample_times, width, times, interval, envelope=False, details=None):
            settings = _freeze((width, times, interval, envelope, details is not None))
            if settings is _UNCACHEABLE:
                return charts._time_points(values, sample_times, width, times, interval, envelope, details)
            key = source, settings
            cached = self.entries.get(key)
            if cached is not None:
                self.entries.move_to_end(key)
                if details is not None:
                    details.update((x, dict(value)) for x, value in cached[2])
                return list(cached[0]), copy.copy(cached[1])
            points, resolved = charts._time_points(values, sample_times, width, times, interval, envelope, details)
            # Count retained numeric fields as well as source-key elements;
            # small sources with wide output cannot escape the same budget.
            cost = source_points + len(points) * 4 + (len(details) * 8 if details is not None else 0)
            if cost <= MAX_PREPARED_POINTS:
                self.entries[key] = (tuple(points), copy.copy(resolved),
                                     tuple((x, tuple(value.items())) for x, value in details.items()) if details is not None else (), cost)
                self.points += cost
                while len(self.entries) > MAX_PREPARED_ENTRIES or self.points > MAX_PREPARED_POINTS:
                    _, old = self.entries.popitem(last=False)
                    self.points -= old[3]
            return points, resolved
        return prepare
