"""Screen-bounded local trends and exact observed ranges for terminal plots.

This module operates on drawing columns, never on retained job history. A trend
is a model of the displayed bucket centres; it is not a new observation. Local
quadratics use at most seven neighbours and never extrapolate or cross a break.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from typing import Iterable, Optional, Sequence


MAX_COLUMNS = 4096
NEIGHBOURS = 7
MIN_FIT_COLUMNS = 5


def _midpoint(low: float, high: float) -> float:
    span = high - low
    return low + span * .5 if math.isfinite(span) else low * .5 + high * .5


def _between(low: float, high: float, fraction: float) -> float:
    span = high - low
    result = low + span * fraction if math.isfinite(span) else low * (1 - fraction) + high * fraction
    return max(low, min(high, result))


def _fraction(value: float, low: float, high: float) -> float:
    if low == high:
        return .5
    span = high - low
    if math.isfinite(span):
        return max(0., min(1., (value - low) / span))
    scale = max(abs(low), abs(high))
    return max(0., min(1., (value / scale - low / scale) / (high / scale - low / scale)))


@dataclass(frozen=True, slots=True)
class Column:
    x: int
    low: float
    high: float
    first: float
    last: float
    count: int
    bridge: bool
    broken: bool = False
    turns: int = 0
    step: bool = False

    @property
    def centre(self) -> float:
        return _midpoint(self.low, self.high)


def columns(points: Iterable[tuple[int, Optional[float], bool]], width: int,
            details: Optional[dict] = None) -> tuple[Column, ...]:
    """Summarize already bounded first/min/max/last drawing vertices.

    ``details`` optionally supplies original bucket counts, reversal counts and
    equal-time jumps. No sample arrays are retained. A missing value or internal
    continuity break makes the entire column ineligible for modelling.
    """
    width = max(0, min(MAX_COLUMNS, width))
    result = []
    current = None
    pending_break = False

    def finish():
        nonlocal pending_break
        if current is None or current[1] is None:
            pending_break = True
            return
        x, low, high, first, last, count, bridge, broken, turns, direction = current
        detail = details.get(x, {}) if details else {}
        result.append(Column(x, low, high, first, last, detail.get("count", count),
                             bridge and not pending_break, broken, detail.get("turns", turns), detail.get("step", False)))
        pending_break = False

    for x, value, bridge in points:
        if not isinstance(x, int) or not 0 <= x < width:
            continue
        if current is not None and x < current[0]:
            # Drawing vertices are ordered. A malformed extension cannot make
            # output memory grow with repeated backwards column cycles.
            continue
        if current is None or x != current[0]:
            finish()
            current = [x, None, None, None, None, 0, bool(bridge), False, 0, 0]
        try:
            finite = value is not None and not isinstance(value, bool) and math.isfinite(value)
        except (TypeError, ValueError, OverflowError):
            finite = False
        if not finite:
            current[7] = True
            current[6] = False
            continue
        if current[1] is None:
            current[1:5] = [value, value, value, value]
        else:
            direction = (value > current[4]) - (value < current[4])
            if direction:
                current[8] += bool(current[9] and current[9] != direction)
                current[9] = direction
            current[1] = min(current[1], value)
            current[2] = max(current[2], value)
            current[4] = value
            current[7] |= not bridge
        current[5] += 1
    finish()
    return tuple(result)


@dataclass(frozen=True, slots=True)
class Polynomial:
    origin: int
    scale: float
    low: float
    high: float
    coefficients: tuple[float, float, float]

    def at(self, x: int) -> float:
        u = (x - self.origin) / self.scale
        a, b, c = self.coefficients
        fraction = max(0., min(1., a + u * (b + u * c)))
        return _between(self.low, self.high, fraction)


def _solve(moment: list[float], rhs: list[float]) -> Optional[tuple[float, float, float]]:
    """Pivoted three-by-three solve with a scale-relative rank guard."""
    rows = [[moment[i + j] for j in range(3)] + [rhs[i]] for i in range(3)]
    threshold = max(abs(value) for row in rows for value in row[:3]) * 1e-10
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(rows[row][column]))
        if abs(rows[pivot][column]) <= threshold:
            return None
        rows[pivot], rows[column] = rows[column], rows[pivot]
        scale = rows[column][column]
        for j in range(column, 4):
            rows[column][j] /= scale
        for i in range(3):
            if i != column:
                factor = rows[i][column]
                for j in range(column, 4):
                    rows[i][j] -= factor * rows[column][j]
    result = tuple(row[3] for row in rows)
    return result if all(math.isfinite(value) for value in result) else None


@lru_cache(maxsize=128)
def _projection(offsets: tuple[int, ...]):
    """Cache geometry alone, never observations or display windows.

    Regular screen columns share the same seven-point geometry. Its small
    weighted least-squares projection is reusable across every metric and
    update; coefficients then cost three short dot products per column.
    """
    scale = max(1., max(abs(offset) for offset in offsets))
    moments, design = [0.] * 5, []
    for offset in offsets:
        u = offset / scale
        # A nonzero shoulder avoids ill-conditioned end fits. Weight each
        # visible column once, so a dense burst cannot dominate its neighbours.
        weight = 1. / (1. + 4. * u * u)
        power = weight
        for i in range(5):
            moments[i] += power
            power *= u
        design.append((weight, weight * u, weight * u * u))
    inverse = [_solve(moments, [float(i == column) for i in range(3)]) for column in range(3)]
    if any(column is None for column in inverse):
        return scale, None
    weights = tuple(tuple(sum(inverse[j][i] * row[j] for j in range(3)) for row in design) for i in range(3))
    return scale, weights


def _fit(neighbours: Sequence[Column], origin: int) -> Optional[Polynomial]:
    low, high = min(item.low for item in neighbours), max(item.high for item in neighbours)
    scale, weights = _projection(tuple(item.x - origin for item in neighbours))
    if low == high:
        return Polynomial(origin, scale, low, high, (.5, 0., 0.))
    if weights is None:
        return None
    fractions = [_fraction(item.centre, low, high) for item in neighbours]
    coefficients = tuple(math.fsum(weight * y for weight, y in zip(row, fractions)) for row in weights)
    return Polynomial(origin, scale, low, high, coefficients) if coefficients is not None else None


def local_trend(source: Sequence[Column], width: int) -> tuple[tuple[int, float, bool], ...]:
    """Fit only visible continuous runs, with bounded work and memory.

    Each observed screen column is fitted once. Its model anchor is constrained
    to that column's actual range. Between columns, adjacent local polynomials
    are blended with smoothstep, then clamped to the pair's actual low/high
    range. Constrained shared anchors keep interval joins continuous. Every
    raster x is evaluated at most once per run.
    Runs shorter than five columns, equal-time steps and unknown intervals have
    no model. Singular fits fall back to the original bucket centre.
    """
    width = max(0, min(MAX_COLUMNS, width))
    runs, current = [], []
    last_x = -1
    for item in source:
        if item.broken or item.step or not last_x < item.x < width:
            if current:
                runs.append(current)
                current = []
            continue
        last_x = item.x
        if not item.bridge or current and item.x <= current[-1].x:
            if current:
                runs.append(current)
            current = []
        current.append(item)
    if current:
        runs.append(current)
    result = []
    for run in runs:
        if len(run) < MIN_FIT_COLUMNS:
            continue
        centre = run[0].centre
        if all(item.centre == centre for item in run[1:]):
            # A constant response has the exact quadratic (centre, 0, 0).
            # Dense alternating bands often share this midrange. Avoid equal
            # fits in every column, and keep roundoff from making a perfectly
            # flat model wobble around a half-pixel rounding boundary.
            first, last = run[0].x, run[-1].x
            result.extend((x, centre, x != first) for x in range(first, last + 1))
            continue
        models = []
        for i, item in enumerate(run):
            start = min(max(0, i - NEIGHBOURS // 2), max(0, len(run) - NEIGHBOURS))
            model = _fit(run[start:start + NEIGHBOURS], item.x)
            if model is not None:
                anchor = max(item.low, min(item.high, model.at(item.x)))
                model = Polynomial(model.origin, model.scale, model.low, model.high,
                                   (_fraction(anchor, model.low, model.high), *model.coefficients[1:]))
            models.append(model)
        for i, item in enumerate(run[:-1]):
            after = run[i + 1]
            low, high = min(item.low, after.low), max(item.high, after.high)
            for x in range(item.x, after.x):
                fraction = (x - item.x) / (after.x - item.x)
                blend = fraction * fraction * (3. - 2. * fraction)
                a = models[i].at(x) if models[i] else item.centre
                b = models[i + 1].at(x) if models[i + 1] else after.centre
                value = _between(min(a, b), max(a, b), blend if a <= b else 1. - blend)
                result.append((x, max(low, min(high, value)), bool(i or x != item.x)))
        last = run[-1]
        value = models[-1].at(last.x) if models[-1] else last.centre
        low, high = min(run[-2].low, last.low), max(run[-2].high, last.high)
        result.append((last.x, max(low, min(high, value)), True))
    return tuple(result)


def band_columns(source: Sequence[Column], low: float, high: float,
                 pixels_high: int) -> tuple[Column, ...]:
    """Select genuinely dense oscillations, never a monotone slope or step.

    Require four observations per column and two reversals, either inside one
    column or shared by neighbouring dense columns. This avoids a bin-boundary
    flicker when the same peak moves between pixels. Monotone slopes and single
    equal-time steps do not qualify. Ranges span at least two raster pixels.
    """
    if pixels_high < 3 or high <= low:
        return ()
    candidates = {item.x: item for item in source if item.count >= 4 and item.turns >= 1
                  and not item.broken and not item.step and
                  (_fraction(item.high, low, high) - _fraction(item.low, low, high)) * (pixels_high - 1) >= 2.}
    result = []
    for item in candidates.values():
        before, after = candidates.get(item.x - 1), candidates.get(item.x + 1)
        if item.turns >= 2 or (before is not None and item.bridge) or (after is not None and after.bridge):
            result.append(item)
    return tuple(result)
