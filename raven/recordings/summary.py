"""Exact extrema index for continuous native envelope rendering.

Building is linear after checking chronological input. A width-W window uses
O(W log N) bounded range queries. Gaps, unknown samples, duplicate timestamps,
unsafe ranges and interpolated window edges use the established full renderer;
no summary is substituted for unavailable observations or raw export data.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
import math
from itertools import chain
import sys

from raven import charts

MAX_POINTS = 64000


class EnvelopeIndex:
    def __init__(self, values, timestamps):
        self.valid = False
        if len(values) != len(timestamps) or not 1 <= len(values) <= MAX_POINTS:
            return
        try:
            if any(
                type(value) not in (int, float) or not math.isfinite(value)
                for value in chain(values, timestamps)
            ):
                return
        except OverflowError:
            return
        if any(b <= a for a, b in zip(timestamps, timestamps[1:])):
            return
        self.values = tuple(values)
        self.times = tuple(timestamps)
        deltas = [b - a for a, b in zip(timestamps, timestamps[1:])]
        if any(not math.isfinite(delta) for delta in deltas):
            return
        self.max_gap = max(deltas, default=0.0)
        self._deltas = tuple(deltas)
        self.cadence = None
        size = 1
        while size < len(values):
            size *= 2
        self.size = size
        self.minimum = [-1] * (2 * size)
        self.maximum = [-1] * (2 * size)
        for index in range(len(values)):
            self.minimum[size + index] = self.maximum[size + index] = index
        for node in range(size - 1, 0, -1):
            self.minimum[node] = self._pick(
                self.minimum[node * 2], self.minimum[node * 2 + 1], False
            )
            self.maximum[node] = self._pick(
                self.maximum[node * 2], self.maximum[node * 2 + 1], True
            )
        self.turns = [0] * (len(values) + 1)
        self.changed = [0] * len(values)
        self.nonzero = []
        direction = 0
        for index in range(1, len(values)):
            current = (values[index] > values[index - 1]) - (
                values[index] < values[index - 1]
            )
            if current:
                self.nonzero.append(index)
                self.changed[index] = int(bool(direction and current != direction))
                direction = current
            self.turns[index + 1] = self.turns[index] + self.changed[index]
        self.valid = True

    def _pick(self, a, b, maximum):
        if a < 0:
            return b
        if b < 0:
            return a
        if self.values[a] == self.values[b]:
            return min(a, b)
        return a if (self.values[a] > self.values[b]) == maximum else b

    def extrema(self, left, right):
        low = high = -1
        a, b = left + self.size, right + self.size
        while a < b:
            if a & 1:
                low = self._pick(low, self.minimum[a], False)
                high = self._pick(high, self.maximum[a], True)
                a += 1
            if b & 1:
                b -= 1
                low = self._pick(low, self.minimum[b], False)
                high = self._pick(high, self.maximum[b], True)
            a //= 2
            b //= 2
        return low, high

    def points(self, width, bounds, interval, *, details=None):
        if not self.valid or type(width) is not int or width <= 0:
            return None
        bounds = bounds or (self.times[0], self.times[-1])
        if len(bounds) != 2 or any(
            type(t) not in (int, float) or not math.isfinite(t) for t in bounds
        ):
            return None
        start, end = bounds
        span = end - start
        if span <= 0 or not math.isfinite(span):
            return None
        if charts._finite(interval) is not None and interval > 0:
            cadence = interval
        else:
            if self.cadence is None:
                ordered = sorted(self._deltas)
                self.cadence = ordered[(len(ordered) - 1) // 2] if ordered else 0.0
            cadence = self.cadence
        gap = min(sys.float_info.max, cadence * 2.5)
        if self.max_gap > gap:
            return None
        left, right = bisect_left(self.times, start), bisect_right(self.times, end)
        if (
            left
            and left < len(self.times)
            and self.times[left] != start
            or right
            and right < len(self.times)
            and self.times[right - 1] != end
        ):
            return None  # Genuine edge interpolation remains with the renderer.
        if left >= right:
            return None
        index = self

        class Columns:
            def __len__(self):
                return len(index.times)

            def __getitem__(self, item):
                return round(((index.times[item] - start) / span) * (width - 1))

        columns = Columns()
        points = []
        previous = None
        for column in range(width):
            stop = bisect_right(columns, column, left, right)
            if stop <= left:
                continue
            minimum, maximum = self.extrema(left, stop)
            vertices = sorted({left, stop - 1, minimum, maximum})
            points.extend(
                (column, self.values[item], previous is not None or offset > 0)
                for offset, item in enumerate(vertices)
            )
            if details is not None:
                turns = self.turns[stop] - self.turns[left + 1]
                first = bisect_left(self.nonzero, left + 1)
                if first < len(self.nonzero) and self.nonzero[first] < stop:
                    turns -= self.changed[self.nonzero[first]]
                details[column] = {
                    "count": stop - left,
                    "turns": min(2, max(0, turns)),
                    "step": False,
                }
            previous = stop - 1
            left = stop
        return points, bounds
