"""Pure terminal charts: fine continuous curves, measured areas, and timelines.

Every chart returns rows of text/style segments. Telemetry curves keep observed
extrema and leave unknown intervals empty. Labelled models remain separate from
the original records used for statistics, inspection and export.
"""
from __future__ import annotations

import math
import sys
import time
from typing import Callable, List, Optional, Sequence, Tuple

from . import clock
from .layout import Glyphs, Row, clip_row, cut, gradient_bar, level, pad, vlen

LEVELS = " ▁▂▃▄▅▆▇█"
LEVELS_ASCII = " ..:::##"
MAX_COLUMNS = 2048
MAX_HEIGHT = 128
QUADRANTS = " ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█"
BRAILLE = " " + "".join(chr(0x2800 + mask) for mask in range(1, 256))
BRAILLE_BITS = ((1, 8), (2, 16), (4, 32), (64, 128))


def _finite(v: Optional[float]) -> Optional[float]:
    try:
        return v if v is not None and not isinstance(v, bool) and math.isfinite(v) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _mean(values: Sequence[float]) -> float:
    """Stable finite mean, preserving tiny values and avoiding overflow sums."""
    if not values:
        return 0.0
    try:
        return math.fsum(values) / len(values)
    except OverflowError:
        # Scaling can erase tiny residuals after cancellation of huge terms.
        # Float denominators are powers of two; an exact bounded-size integer
        # accumulator preserves those residuals and rounds only the final mean.
        ratios = [float(value).as_integer_ratio() for value in values]
        denominator = max(pair[1] for pair in ratios)
        shift = denominator.bit_length()
        numerator = sum(number << (shift - divisor.bit_length()) for number, divisor in ratios)
        return numerator / (denominator * len(values))


def _fraction(value: float, lo: float, hi: float) -> float:
    """Clamped chart coordinate without overflowing signed finite ranges."""
    if value <= lo or hi <= lo:
        return 0.0
    if value >= hi:
        return 1.0
    span = hi - lo
    if math.isfinite(span):
        return max(0.0, min(1.0, (value - lo) / span))
    scale = max(abs(lo), abs(hi))
    lower, upper = lo / scale, hi / scale
    return max(0.0, min(1.0, (value / scale - lower) / (upper - lower)))


def _between(lo: float, hi: float, fraction: float) -> float:
    """Finite interpolation, including opposite-sign and subnormal endpoints."""
    if fraction <= 0:
        return lo
    if fraction >= 1:
        return hi
    span = hi - lo
    value = lo + span * fraction if math.isfinite(span) else lo * (1 - fraction) + hi * fraction
    return max(lo, min(hi, value))


def _bounds(values: Sequence[Optional[float]], lo: float, hi: Optional[float]) -> Tuple[float, float]:
    lo = lo if _finite(lo) is not None else 0.0
    if _finite(hi) is None:
        peak = max((v for v in values if v is not None), default=lo)
        if peak > lo:
            span = peak - lo
            padding = span * 0.05 if math.isfinite(span) else peak * 0.05 - lo * 0.05
            hi = peak + padding
            if not math.isfinite(hi):
                hi = sys.float_info.max
        else:
            hi = lo + 1.0
    if hi <= lo or not math.isfinite(hi):
        hi = lo + 1.0
        if not math.isfinite(hi) or hi <= lo:
            hi = math.nextafter(lo, math.inf)
            if not math.isfinite(hi):
                hi, lo = lo, math.nextafter(lo, -math.inf)
    return lo, hi


def _header(g: Glyphs, values: Sequence[Optional[float]], width: int, title: str, unit: str, indent: str) -> Row:
    known = [v for v in values if v is not None]
    # The newest sample can be unknown even when earlier samples exist.
    if known:
        tokens = [f"last {fmt_num(values[-1], unit)}", f"mean {fmt_num(_mean(known), unit)}",
                  f"max {fmt_num(max(known), unit)}", f"min {fmt_num(min(known), unit)}"]
    else:
        tokens = ["awaiting samples"]
    usable = max(0, width - vlen(indent))
    if not known and usable < vlen(tokens[0]):
        tokens = ["no data"]
    first = vlen(tokens[0])
    if usable < first:
        return clip_row([(indent + cut(title, usable, g.ascii), "cyan+bold")], width)
    title_budget = min(max(0, usable - first - 2), max(5, usable // 2))
    short_title = cut(title, title_budget, g.ascii)
    row: Row = [(indent + short_title, "cyan+bold")]
    used = vlen(indent) + vlen(short_title)
    for token in tokens:
        prefix = "  " if short_title or len(row) > 1 else ""
        if used + vlen(prefix + token) > width:
            break
        row.append((prefix + token, "dim"))
        used += vlen(prefix + token)
    return row


def chart_model_title(title: str, metadata: Optional[dict]) -> str:
    """Keep model/range identification visible when a long title is clipped."""
    if not metadata:
        return title
    labels = " + ".join(label for label, key in (("fit", "trend"), ("range", "band")) if metadata.get(key))
    return "[" + labels + "] " + title if labels else title


def _clip_time_samples(samples: Sequence[Tuple[float, Optional[float]]], times: Tuple[float, float],
                       gap_limit: float) -> List[Tuple[float, Optional[float], bool]]:
    """Clip the observed piecewise line at time boundaries for rasterization.

    An edge intersection is a position on a line between original observations,
    not a new measurement. The source arrays, headers, cursor and statistics
    remain original records. Missing endpoints or a cadence outage cannot yield
    an intersection. At most two additional drawing vertices are produced.
    """
    return list(_iter_time_samples(samples, times, gap_limit))


def _iter_time_samples(samples: Sequence[Tuple[float, Optional[float]]], times: Tuple[float, float],
                       gap_limit: float):
    """Yield drawing vertices without copying the whole retained time window."""
    t0, t1 = times
    previous = None

    def intersection(before: Tuple[float, float], after: Tuple[float, float], boundary: float) -> float:
        fraction = _fraction(boundary, before[0], after[0])
        return _between(min(before[1], after[1]), max(before[1], after[1]),
                        fraction if before[1] <= after[1] else 1 - fraction)

    for timestamp, value in samples:
        bridge = (previous is not None and value is not None and previous[1] is not None
                  and timestamp - previous[0] <= gap_limit)
        current = (timestamp, value)
        if timestamp < t0:
            previous = (timestamp, value)
            continue
        if bridge and previous[0] < t0 < timestamp:
            yield t0, intersection(previous, current, t0), False
        if timestamp > t1:
            if bridge and previous[0] < t1:
                yield t1, intersection(previous, current, t1), True
            break
        yield timestamp, value, bridge
        previous = (timestamp, value)


def _window_values(values: Sequence[Optional[float]], sample_times: Optional[Sequence[float]],
                   times: Optional[Tuple[float, float]]) -> Sequence[Optional[float]]:
    """Statistics describe actual records inside the time window, never edges."""
    if sample_times is None or times is None:
        return values
    return [value for timestamp, value in zip(sample_times, values)
            if _finite(timestamp) is not None and times[0] <= timestamp <= times[1]]


def fit_time_bounds(values: Sequence[Optional[float]], sample_times: Sequence[float],
                    times: Tuple[float, float], fallback: Tuple[float, float],
                    sample_interval: Optional[float] = None) -> Tuple[float, float]:
    """Fit the actual observed line in a selected time interval.

    Keep every measured extremum, including duplicate timestamps, and include
    genuine intersections with the interval edges. Unknown endpoints and
    cadence outages never provide an interpolated bound. No extrapolation is
    permitted. An empty interval retains finite display bounds without creating
    a sample. Fits are computed by renderers, never by pointer input handlers.
    """
    if (not isinstance(times, (list, tuple)) or len(times) != 2 or
            any(_finite(item) is None for item in times) or times[0] >= times[1]):
        return fallback
    samples = [(timestamp, _finite(value)) for timestamp, value in zip(sample_times, values)
               if _finite(timestamp) is not None]
    samples.sort(key=lambda item: item[0])
    if _finite(sample_interval) is not None and sample_interval > 0:
        cadence = sample_interval
    else:
        deltas = sorted(b[0] - a[0] for a, b in zip(samples, samples[1:])
                        if b[0] > a[0] and math.isfinite(b[0] - a[0]))
        cadence = deltas[(len(deltas) - 1) // 2] if deltas else 0.0
    vertices = [value for _, value, _ in _clip_time_samples(samples, times,
                                                          min(sys.float_info.max, cadence * 2.5))
                if value is not None]
    if not vertices:
        return fallback
    lower, upper = min(vertices), max(vertices)
    span = upper - lower
    if lower == upper:
        padding = abs(lower) * .025 if lower else .5
    else:
        padding = span * .025 if math.isfinite(span) else upper * .025 - lower * .025
    lo, hi = lower - padding, upper + padding
    lo = lo if math.isfinite(lo) else lower
    hi = hi if math.isfinite(hi) else upper
    if lo >= hi:
        lo, hi = math.nextafter(lower, -math.inf), math.nextafter(upper, math.inf)
        lo = lo if math.isfinite(lo) else lower
        hi = hi if math.isfinite(hi) else upper
    return (lo, hi) if lo < hi else fallback


def axis_num(value: float, lo: float, hi: float, unit: str = "") -> str:
    """Compact Y ticks with enough significant digits for the fitted span."""
    span = hi - lo
    magnitude = max(abs(lo), abs(hi))
    if not math.isfinite(span) or span <= 0 or magnitude == 0:
        return fmt_num(value, unit)
    digits = min(16, max(3, math.ceil(math.log10(magnitude) - math.log10(span)) + 2))
    return f"{value:.{digits}g}{unit}"


def time_scale(t0: float, t1: float) -> Tuple[float, str]:
    """Pick an offset unit from the selected duration, independent of its epoch."""
    span = t1 - t0
    return (1.0, "s") if span >= 1 or not math.isfinite(span) else (
           (1e3, "ms") if span >= 1e-3 else (1e6, "us"))


def time_selection_note(t0: float, t1: float, width: int, indent: str = "   ") -> Row:
    """Retain the exact absolute anchor when selected axes show relative offsets."""
    scale, unit = time_scale(t0, t1)
    span = (t1 - t0) * scale
    label = f"{span:.6g}{unit}" if math.isfinite(span) else ">1e308s"
    return clip_row([(indent + f"Time +offset from t_a={t0!r}s; span {label}", "dim")], width)


class _TimeBucket:
    """Four retained observed vertices and an exact bounded-size mean sum."""
    __slots__ = ("count", "first", "last", "minimum", "maximum", "unknown",
                 "breaks", "numerator", "shift", "envelope", "details", "turns", "direction", "step")

    def __init__(self, envelope, details=False):
        self.count, self.breaks, self.numerator, self.shift = 0, 0, 0, 0
        self.first = self.last = self.minimum = self.maximum = None
        self.unknown, self.envelope = False, envelope
        self.details, self.turns, self.direction, self.step = details, 0, 0, False

    def add(self, timestamp, value, bridge):
        if self.details and self.last is not None and value is not None and self.last[2] is not None:
            previous = self.last[2]
            if timestamp == self.last[1] and value != previous:
                self.step = True
            direction = (value > previous) - (value < previous)
            if direction and bridge:
                self.turns = min(2, self.turns + bool(self.direction and self.direction != direction))
                self.direction = direction
            elif not bridge:
                self.direction = 0
        self.breaks += not bridge
        point = (self.count, timestamp, value, self.breaks)
        self.count += 1
        if self.first is None:
            self.first = point
        self.last = point
        if value is None:
            self.unknown = True
            return
        if self.minimum is None or value < self.minimum[2]:
            self.minimum = point
        if self.maximum is None or value > self.maximum[2]:
            self.maximum = point
        if not self.envelope:
            # Float denominators are powers of two. An integer sum avoids
            # overflow and cancellation loss without retaining all samples.
            number, denominator = float(value).as_integer_ratio()
            shift = denominator.bit_length() - 1
            if shift > self.shift:
                self.numerator <<= shift - self.shift
                self.shift = shift
            self.numerator += number << (self.shift - shift)

    def mean(self):
        return self.numerator / ((1 << self.shift) * self.count)


def _time_points(values: Sequence[Optional[float]], sample_times: Sequence[float], width: int,
                 times: Optional[Tuple[float, float]], sample_interval: Optional[float],
                 envelope: bool = False, details: Optional[dict] = None) -> Tuple[List[Tuple[int, Optional[float], bool]], Tuple[float, float]]:
    """Bounded timestamp buckets with enough metadata to break curves across outages."""
    samples = [(timestamp, _finite(value)) for timestamp, value in zip(sample_times, values) if _finite(timestamp) is not None]
    if any(b[0] < a[0] for a, b in zip(samples, samples[1:])):
        samples.sort(key=lambda item: item[0])
    if times is None or len(times) != 2 or not all(_finite(t) is not None for t in times) or times[1] < times[0]:
        times = (samples[0][0], samples[-1][0]) if samples else (0.0, 0.0)
    if width <= 0:
        return [], times
    if _finite(sample_interval) is not None and sample_interval > 0:
        cadence = sample_interval
    else:
        deltas = sorted(b[0] - a[0] for a, b in zip(samples, samples[1:])
                        if b[0] > a[0] and math.isfinite(b[0] - a[0]))
        cadence = deltas[(len(deltas) - 1) // 2] if deltas else 0.0
    gap_limit = min(sys.float_info.max, cadence * 2.5)
    t0, t1 = times
    # Clipping below already guarantees native timestamps are inside [t0,t1].
    # For a finite positive span the ordinary division is exactly _fraction's
    # arithmetic, without its repeated endpoint/clamp/overflow checks. Keep
    # the original path for extension numerics and overflowing signed ranges.
    span = None
    if (type(width) is int and type(t0) in (int, float) and type(t1) in (int, float)
            and t1 > t0 and all(type(point[0]) in (int, float) for point in samples)):
        try:
            candidate = t1 - t0
            if math.isfinite(candidate):
                span = candidate
        except OverflowError:
            pass
    last_column = width - 1
    buckets: dict[int, _TimeBucket] = {}
    for timestamp, value, bridge in _iter_time_samples(samples, times, gap_limit):
        x = (round(((timestamp - t0) / span) * last_column) if span is not None else
             min(last_column, max(0, round(_fraction(timestamp, t0, t1) * last_column))) if t1 > t0 else last_column)
        bucket = buckets.get(x)
        if bucket is None:
            bucket = buckets[x] = _TimeBucket(envelope, details is not None)
        bucket.add(timestamp, value, bridge)
    points: List[Tuple[int, Optional[float], bool]] = []
    previous_last: Optional[float] = None
    for x, bucket in sorted(buckets.items()):
        if details is not None:
            details[x] = {"count": bucket.count, "turns": bucket.turns, "step": bucket.step}
        known = not bucket.unknown
        bridge = (known and bucket.first[3] == 0 and previous_last is not None
                  and bucket.first[1] - previous_last <= gap_limit)
        if envelope and known:
            # Keep the first, last, minimum and maximum samples in their source
            # order. Each raster column has at most four points. A narrow spike
            # cannot disappear into a mean, and a bucket containing a gap stays
            # unknown instead of manufacturing a continuous measurement.
            vertices = sorted({point[0]: point for point in (bucket.first, bucket.minimum,
                                                            bucket.maximum, bucket.last)}.values())
            previous_breaks = 0
            for i, point in enumerate(vertices):
                connected = bridge if i == 0 else point[3] == previous_breaks
                points.append((x, point[2], connected))
                previous_breaks = point[3]
        else:
            points.append((x, bucket.mean() if known else None, bridge))
        previous_last = bucket.last[1] if known else None
    return points, times


def envelope_points(values: Sequence[Optional[float]], width: int,
                    details: Optional[dict] = None) -> List[Tuple[int, Optional[float], bool]]:
    """Bounded first/minimum/maximum/last buckets for equally spaced samples."""
    vals = [_finite(value) for value in values]
    if width <= 0:
        return []
    if len(vals) <= width:
        return [(width - len(vals) + i, value, True) for i, value in enumerate(vals)]
    points = []
    for x in range(width):
        chunk = vals[x * len(vals) // width:(x + 1) * len(vals) // width]
        if any(value is None for value in chunk):
            points.append((x, None, False))
            continue
        if details is not None:
            previous, direction, turns = chunk[0], 0, 0
            for value in chunk[1:]:
                next_direction = (value > previous) - (value < previous)
                if next_direction:
                    turns += bool(direction and next_direction != direction)
                    direction = next_direction
                previous = value
                if turns >= 2:
                    break
            details[x] = {"count": len(chunk), "turns": turns, "step": False}
        indices = sorted({0, len(chunk) - 1, min(range(len(chunk)), key=chunk.__getitem__),
                          max(range(len(chunk)), key=chunk.__getitem__)})
        points.extend((x, chunk[index], True) for index in indices)
    return points


def resample(values: Sequence[Optional[float]], width: int, how: str = "mean") -> List[Optional[float]]:
    """``values`` -> ``width`` buckets (mean or max; None if any sample in a bucket is missing).
    Fewer values than columns are right-aligned without stretching."""
    vals = [_finite(v) for v in values]
    if width <= 0:
        return []
    if len(vals) <= width:
        return [None] * (width - len(vals)) + vals
    out: List[Optional[float]] = []
    n = len(vals)
    for i in range(width):
        a, b = (i * n) // width, max(((i + 1) * n) // width, (i * n) // width + 1)
        chunk = vals[a:b]
        # A bucket intersecting an unobserved interval remains a gap. This avoids
        # drawing continuous telemetry across a failed sample or stale connection.
        out.append(None if not chunk or any(v is None for v in chunk) else
                   (_mean(chunk) if how == "mean" else max(chunk)))
    return out


def fmt_num(v: Optional[float], unit: str = "") -> str:
    if v is None or not math.isfinite(v):
        return "?"
    a = abs(v)
    if a >= 1e15 or (0 < a < 0.01):
        return f"{v:.2g}{unit}"
    if unit == "%":
        return f"{v:.0f}%"
    if a >= 1e9:
        return f"{v / 1e9:.1f}G{unit}"
    if a >= 1e6:
        return f"{v / 1e6:.1f}M{unit}"
    if a >= 1e3:
        return f"{v / 1e3:.1f}k{unit}"
    if a >= 10 or v == int(v):
        return f"{v:.0f}{unit}"
    return f"{v:.2f}{unit}"


def time_axis(t0: float, t1: float, width: int, indent: str = "", elapsed: bool = False,
              units: bool = False) -> Row:
    """Spread time ticks over actual cells, or selected offsets in s/ms/us.

    Offset labels subtract the selected origin before scaling. Narrow intervals
    at large Unix epochs therefore retain useful precision. Tick labels never
    collide, and equal representable timestamps never get duplicate labels.
    """
    width = max(0, min(MAX_COLUMNS, width))
    if width <= 8 or _finite(t0) is None or _finite(t1) is None or t1 <= t0:
        return [(indent + " " * max(0, width), "dim")]
    span = t1 - t0
    fmt = "%H:%M" if span < 36 * 3600 else "%m-%d %H:%M"
    n = max(2, min(8, width // 14))
    if units and math.isfinite(span):
        scale, unit = time_scale(t0, t1)
        line, placed = [" "] * width, set()

        def offset_label(fraction):
            value = (_between(t0, t1, fraction) - t0) * scale
            return f"{value:.6g}{unit}"

        # Endpoints have priority. Intermediate ticks use only remaining space.
        for i in [0, n - 1] + list(range(1, n - 1)):
            fraction = i / (n - 1)
            label = offset_label(fraction)
            if vlen(label) > width or label in placed:
                continue
            x = max(0, min(width - len(label), round(fraction * (width - 1)) - len(label) // 2))
            if all(char == " " for char in line[max(0, x - 1):min(width, x + len(label) + 1)]):
                line[x:x + len(label)] = list(label)
                placed.add(label)
        return [(indent + "".join(line), "dim")]
    if span < 1:
        # Preserve the actual timestamp anchor and enough fractional digits to
        # distinguish small display windows. Rounding the fractional component
        # before formatting also carries correctly across a second or midnight.
        digits = min(6, max(1, math.ceil(-math.log10(span) + math.log10(n - 1))))
        scale = 10 ** digits

        def precise_label(value):
            if elapsed:
                return f"{value:.{digits}f}s"
            whole = math.floor(value)
            fraction = round((value - whole) * scale)
            if fraction == scale:
                whole, fraction = whole + 1, 0
            try:
                return time.strftime("%H:%M:%S", time.localtime(whole)) + f".{fraction:0{digits}d}"
            except (OverflowError, OSError, ValueError):
                return fmt_num(value, "s")

        first, last = precise_label(t0), precise_label(t1)
        if len(first) + len(last) + 1 > width or first == last:
            label = cut(last, width, True)
            return [(indent + " " * max(0, width - len(label)) + label, "dim")]
        line = [" "] * width
        line[:len(first)], line[-len(last):] = list(first), list(last)
        placed = {first, last}
        for i in range(1, n - 1):
            frac = i / (n - 1)
            label = precise_label(_between(t0, t1, frac))
            x = max(0, min(width - len(label), round(frac * (width - 1)) - len(label) // 2))
            if label not in placed and all(c == " " for c in line[max(0, x - 1):x + len(label) + 1]):
                line[x:x + len(label)] = list(label)
                placed.add(label)
        return [(indent + "".join(line), "dim")]
    if span < 120 and not elapsed:                          # too short for a scale: one label at the end
        try:
            label = time.strftime("%H:%M:%S", time.localtime(t1))
        except (OverflowError, OSError, ValueError):
            label = fmt_num(t1, "s")
        label = cut(label, width, True)
        return [(indent + " " * max(0, width - len(label)) + label, "dim")]
    line = [" "] * width
    for i in range(n):
        frac = i / (n - 1)
        value = _between(t0, t1, frac)
        label = fmt_num(value / 3600, "h") if elapsed and span >= 3600 else (
                fmt_num(value / 60, "m") if elapsed and span >= 120 else (
                fmt_num(value, "s") if elapsed else ""))
        if not elapsed:
            try:
                label = time.strftime(fmt, time.localtime(value))
            except (OverflowError, OSError, ValueError):
                label = fmt_num(value, "s")
        label = cut(label, width, True)
        x = int(round(frac * (width - 1)))
        x = max(0, min(width - len(label), x - len(label) // 2))
        if all(c == " " for c in line[x:x + len(label) + 1]):
            line[x:x + len(label)] = list(label)
    return [(indent + "".join(line), "dim")]


def _trace_axis(times: Tuple[float, float], samples: int, chart_w: int, indent: str, raster: int = 1, elapsed: bool = False) -> Row:
    """Label only the occupied trace extent when a short trace is right-aligned."""
    occupied = min(chart_w, (samples + raster - 1) // raster)
    return time_axis(times[0], times[1], occupied, indent + " " * (chart_w - occupied), elapsed)


def _plot_metadata(metadata: Optional[dict], width: int, chart_w: int, height: int,
                   indent: str, axis_w: int, title: str, lo: float, hi: float,
                   times: Optional[Tuple[float, float]], timestamped: bool,
                   has_data: bool, raster: Tuple[int, int]) -> None:
    """Publish exact local half-open geometry without retaining caller state.

    Timestamp-less short traces occupy only the right edge, so they deliberately
    publish no time transform. Interactive callers must provide sample times.
    Bounds are the plotted coordinate system, including log10 when supplied.
    """
    if metadata is None:
        return
    top = int(bool(title))
    left = min(max(0, width), vlen(indent) + axis_w)
    metadata.clear()
    metadata.update(plot_rect=(top, left, top + height, left + chart_w),
                    axis_rect=(top, min(max(0, width), vlen(indent)),
                               top + height + 1 + int(times is not None), left + chart_w),
                    x_bounds=times if timestamped else None,
                    y_bounds=(lo, hi), raster=raster,
                    valid=bool(chart_w and height), has_data=has_data)


def _clip_curve_segment(x0: int, value0: float, x1: int, value1: float,
                        lo: float, hi: float) -> Optional[Tuple[float, float, float, float]]:
    """Clip observed linear segments, rather than flattening clipped values.

    Intersections use the same overflow-safe fraction as the chart. Two samples
    above or below a fixed/zoomed range cannot manufacture a boundary plateau.
    """
    if min(value0, value1) > hi or max(value0, value1) < lo:
        return None
    lower, upper = min(value0, value1), max(value0, value1)

    def endpoint(x: int, value: float) -> Tuple[float, float]:
        if lo <= value <= hi:
            return float(x), value
        boundary = lo if value < lo else hi
        fraction = _fraction(boundary, lower, upper)
        if value1 < value0:
            fraction = 1 - fraction
        return x0 + (x1 - x0) * fraction, boundary

    start, end = endpoint(x0, value0), endpoint(x1, value1)
    return start[0], start[1], end[0], end[1]


def vbar_chart(g: Glyphs, values: Sequence[Optional[float]], width: int, height: int, lo: float = 0.0, hi: Optional[float] = None,
               unit: str = "", title: str = "", times: Optional[Tuple[float, float]] = None, color: Optional[Callable[[float], str]] = None,
               indent: str = "   ", axis_w: int = 7, sample_times: Optional[Sequence[float]] = None,
               sample_interval: Optional[float] = None, elapsed: bool = False, envelope: bool = True,
               axis_formatter: Optional[Callable[[float], str]] = None,
               metadata: Optional[dict] = None, fitted: bool = False, time_units: bool = False,
               _time_cache=None) -> List[Row]:
    """A vertical bar (area) chart ``height`` rows tall with eight sub-levels per row, a y axis on the left and a
    time axis below.  ``values`` are resampled to the chart width; None leaves a gap."""
    axis_w = max(1, axis_w)
    values = [_finite(v) for v in values]
    lo, hi = _bounds(values, lo, hi)
    if fitted:
        axis_formatter = axis_formatter or (lambda value: axis_num(value, lo, hi, unit))
        axis_w = min(max(axis_w, 1 + max(vlen(axis_formatter(value)) for value in (lo, hi, _mean((lo, hi))))),
                     max(1, width - vlen(indent) - 3))
    chart_w = max(0, min(MAX_COLUMNS, width - vlen(indent) - axis_w))
    height = max(0, min(MAX_HEIGHT, height))
    lows = [None] * chart_w
    if sample_times is None:
        vals = resample(values, chart_w, "max" if envelope else "mean")
        if envelope:
            for x, value, _ in envelope_points(values, chart_w):
                lows[x] = value if lows[x] is None else min(lows[x], value) if value is not None else None
    else:
        prepare = _time_cache or _time_points
        points, times = prepare(values, sample_times, chart_w, times, sample_interval, envelope)
        vals = [None] * chart_w
        for x, value, _ in points:
            vals[x] = value if vals[x] is None else max(vals[x], value) if value is not None else None
            lows[x] = value if lows[x] is None else min(lows[x], value) if value is not None else None
    # Summary figures describe the source samples, not the bucket averages used to draw the chart.
    lo, hi = _bounds(values, lo, hi)
    _plot_metadata(metadata, width, chart_w, height, indent, axis_w, title, lo, hi,
                   times, sample_times is not None, any(v is not None for v in vals),
                   (1, len(g.spark) if not g.ascii else len(LEVELS_ASCII[1:])))
    levels = g.spark if not g.ascii else LEVELS_ASCII[1:]
    nlev = len(levels)
    rows: List[Row] = []
    if title:
        rows.append(_header(g, _window_values(values, sample_times, times), width, title, unit, indent))
    for r in range(height):
        label = ""
        if r == 0:
            label = axis_formatter(hi) if axis_formatter else fmt_num(hi, unit)
        elif r == height - 1:
            label = axis_formatter(lo) if axis_formatter else fmt_num(lo, unit)
        elif height >= 5 and r == height // 2:
            label = axis_formatter(_mean((lo, hi))) if axis_formatter else fmt_num(_mean((lo, hi)), unit)
        segs: Row = [(indent + pad(cut(label, max(0, axis_w - 1), g.ascii), axis_w - 1, ">") + (g.box[5] if not g.ascii else "|"), "dim")]
        floor = (height - 1 - r) * nlev                      # sub-levels below this row
        row_style = ""
        if not g.ascii and not color:
            from .palette import gradient_style
            row_style = gradient_style("track", "cyan", (height - r) / max(1, height))
        for x, v in enumerate(vals):
            guide = g.dot if not g.ascii and r in (0, height // 2) and x % 8 == 0 else " "
            if v is None:
                segs.append((guide, "dim"))
                continue
            frac = _fraction(v, lo, hi)
            lv = int(round(frac * height * nlev))
            fill = min(nlev, max(0, lv - floor))
            if envelope and lows[x] is not None and lows[x] < v:
                bottom = int(round(_fraction(lows[x], lo, hi) * height * nlev))
                if lv >= floor and bottom <= floor + nlev and fill > 0:
                    # The envelope marks the observed range rather than hiding
                    # its minimum beneath the filled maximum area.
                    segs.append((":" if g.ascii else "│", color(frac) if color else "cyan+bold"))
                    continue
            if fill > 0:
                if color:
                    style = color(frac)
                elif g.ascii:
                    style = level(frac)
                else:
                    style = row_style
                segs.append((levels[fill - 1], style))
            else:
                # A measured zero has an anchor; an unobserved sample stays blank.
                segs.append((g.dot if r == height - 1 and v == lo else guide, "dim"))
        rows.append(segs)
    base = indent + " " * (axis_w - 1) + ("+" if g.ascii else "└") + g.rule * chart_w
    rows.append([(base, "dim")])
    if times:
        rows.append(_trace_axis(times, len(values), chart_w, indent + " " * axis_w, elapsed=elapsed) if sample_times is None else
                    time_axis(times[0], times[1], chart_w, indent + " " * axis_w, elapsed, units=time_units))
        if time_units:
            rows.append(time_selection_note(times[0], times[1], width, indent))
    return [clip_row(row, width) for row in rows]


def hbar_rows(g: Glyphs, items: Sequence[Tuple[str, float, str]], width: int, indent: str = "   ", label_w: int = 12, unit: str = "",
              hi: Optional[float] = None, color: Optional[Callable[[str, float], str]] = None) -> List[Row]:
    """One row per (label, value, style): ``label  ████░░░░  value``."""
    if not items:
        return []
    hi = hi if _finite(hi) is not None else max((v for _, v, _ in items if _finite(v) is not None), default=1.0)
    hi = hi if hi > 0 else 1.0
    label_w = min(label_w, max(1, (width - vlen(indent) - 10) // 2))
    bar_w = max(0, width - vlen(indent) - label_w - 12)
    rows: List[Row] = []
    for label, v, style in items:
        value = _finite(v)
        frac = None if value is None else _fraction(value, 0.0, hi)
        row: Row = [(indent + pad(cut(label, label_w, g.ascii), label_w) + " ", "")]
        if g.ascii or color:
            from .layout import bar
            text, _ = bar(g, frac, bar_w)
            filled = text.rstrip(g.empty)
            row += [(filled, style or (color(label, v) if color else "cyan")), (g.empty * (bar_w - vlen(filled)), "dim")]
        else:
            hue = style.split("+")[0]
            hue = hue if hue in ("green", "red", "yellow", "magenta", "blue", "cyan") else "cyan"
            row += gradient_bar(g, frac, bar_w, "track", hue)
        row += [(f" {fmt_num(v, unit)}", "bold")]
        rows.append(row)
    return [clip_row(row, width) for row in rows]


def braille_chart(g: Glyphs, values: Sequence[Optional[float]], width: int, height: int, lo: float = 0.0,
                  hi: Optional[float] = None, unit: str = "", title: str = "",
                  times: Optional[Tuple[float, float]] = None, color: Optional[Callable[[float], str]] = None,
                  indent: str = "   ", axis_w: int = 7, sample_times: Optional[Sequence[float]] = None,
                  sample_interval: Optional[float] = None, elapsed: bool = False, envelope: bool = True,
                  axis_formatter: Optional[Callable[[float], str]] = None,
                  metadata: Optional[dict] = None, fitted: bool = False, time_units: bool = False,
                  curve_style: str = "fine", trend: bool = True, bands: bool = True,
                  model_kind: str = "quadratic", reference: float | None = None,
                  _time_cache=None) -> List[Row]:
    """Connected telemetry with a fine 2 x 4 Braille raster per Unicode cell.

    Eight subcell positions retain small bends and steep transitions while
    leaving the graph readable beneath the pointer. ``curve_style="blocks"``
    retains the earlier opaque 2 x 2 raster. ASCII uses directional line strokes.
    Titled plots add a separately identified local quadratic trend. Dense
    oscillations use exact low/high ranges with dim fill and bright borders.
    Compression preserves extrema; unknown samples, cadence outages and steps
    break models. Source observations and summary statistics remain unchanged.
    """
    axis_w = max(1, axis_w)
    values = [_finite(v) for v in values]
    lo, hi = _bounds(values, lo, hi)
    if fitted:
        axis_formatter = axis_formatter or (lambda value: axis_num(value, lo, hi, unit))
        axis_w = min(max(axis_w, 1 + max(vlen(axis_formatter(value)) for value in (lo, hi, _mean((lo, hi))))),
                     max(1, width - vlen(indent) - 3))
    chart_w = max(0, min(MAX_COLUMNS, width - vlen(indent) - axis_w))
    height = max(0, min(MAX_HEIGHT, height))
    raster_x = 1 if g.ascii else 2
    raster_y = 1 if g.ascii else 2 if curve_style == "blocks" else 4
    enhanced = bool(title and envelope and chart_w and height and (trend or bands))
    details = {} if enhanced else None
    if sample_times is None:
        points = envelope_points(values, chart_w * raster_x, details) if envelope else [
            (x, value, True) for x, value in enumerate(resample(values, chart_w * raster_x))]
    else:
        prepare = _time_cache or _time_points
        points, times = prepare(values, sample_times, chart_w * raster_x, times, sample_interval, envelope, details)
    _plot_metadata(metadata, width, chart_w, height, indent, axis_w, title, lo, hi,
                   times, sample_times is not None, any(v is not None for _, v, _ in points),
                   (raster_x, raster_y))
    pixels_h, pixels_w = height * raster_y, chart_w * raster_x
    cells = [[0] * chart_w for _ in range(height)]
    trend_points, band_points = (), ()
    if enhanced:
        from . import metric_envelope as E
        profile = E.columns(points, pixels_w, details)
        trend_points = ((E.rolling_median(profile, pixels_w) if model_kind == "median" else
                         E.local_trend(profile, pixels_w)) if trend else ())
        if bands and model_kind == "drift":
            band_points = E.drift_columns(profile, reference) if reference is not None else ()
        elif bands:
            band_points = E.band_columns(profile, lo, hi, pixels_h)
    band_x = {point.x for point in band_points}
    borders = [[0] * chart_w for _ in range(height)] if band_points else []
    fills = [[0] * chart_w for _ in range(height)] if band_points else []
    models = [[0] * chart_w for _ in range(height)] if trend_points else []
    target_cells = cells
    if metadata is not None:
        metadata.update(trend=bool(trend_points), band=bool(band_points),
                        trend_label=("rolling median of displayed columns" if model_kind == "median" else "local quadratic fit") if trend_points else "",
                        band_label=("observed drift from baseline; no tolerance" if model_kind == "drift" else "observed low-high range") if band_points else "")

    def mark(x: int, y: int, direction: int = 16) -> None:
        if 0 <= x < pixels_w and 0 <= y < pixels_h:
            if g.ascii:
                target_cells[y][x] |= direction
            elif raster_y == 4:
                target_cells[y // 4][x // 2] |= BRAILLE_BITS[y % 4][x % 2]
            else:
                target_cells[y // 2][x // 2] |= 1 << ((y % 2) * 2 + x % 2)

    def stroke(px: int, py: int, ex: int, ey: int) -> None:
        dx, dy = abs(ex - px), abs(ey - py)
        sx, sy = (1 if px < ex else -1), (1 if py < ey else -1)
        # Directional ASCII strokes make level, rising, and falling segments
        # readable without colour; a same-column envelope uses a range marker.
        direction = (32 if dx == 0 and dy else 1 if dy == 0 else
                     4 if ey > py else 8)
        error = dx - dy
        while True:
            mark(px, py, direction)
            if px == ex and py == ey:
                break
            doubled = error * 2
            if doubled > -dy:
                error -= dy
                px += sx
            if doubled < dx:
                error += dx
                py += sy

    previous: Optional[Tuple[int, float, Optional[int]]] = None
    if pixels_h and pixels_w:
        for x, value, bridge in points:
            if value is None or x in band_x:
                previous = None
                continue
            y = round((1 - _fraction(value, lo, hi)) * (pixels_h - 1)) if lo <= value <= hi else None
            if y is not None:
                mark(x, y)
            if previous is not None and bridge:
                if previous[2] is not None and y is not None:
                    # The usual in-range path reuses integer pixel coordinates.
                    # Only actual clipping needs stable boundary interpolation.
                    stroke(previous[0], previous[2], x, y)
                else:
                    segment = _clip_curve_segment(previous[0], previous[1], x, value, lo, hi)
                    if segment is not None:
                        x0, v0, x1, v1 = segment
                        stroke(round(x0), round((1 - _fraction(v0, lo, hi)) * (pixels_h - 1)),
                               round(x1), round((1 - _fraction(v1, lo, hi)) * (pixels_h - 1)))
            previous = (x, value, y)

    if band_points:
        target_cells = borders
        previous_band = None
        for item in band_points:
            lower = round((1 - _fraction(item.low, lo, hi)) * (pixels_h - 1))
            upper = round((1 - _fraction(item.high, lo, hi)) * (pixels_h - 1))
            if lo <= item.low <= hi:
                mark(item.x, lower, 1)
            if lo <= item.high <= hi:
                mark(item.x, upper, 1)
            if previous_band is not None and item.bridge and item.x == previous_band.x + 1:
                for start, end in ((previous_band.low, item.low), (previous_band.high, item.high)):
                    segment = _clip_curve_segment(previous_band.x, start, item.x, end, lo, hi)
                    if segment is not None:
                        x0, v0, x1, v1 = segment
                        stroke(round(x0), round((1 - _fraction(v0, lo, hi)) * (pixels_h - 1)),
                               round(x1), round((1 - _fraction(v1, lo, hi)) * (pixels_h - 1)))
            target_cells = fills
            # Terminals do not support alpha. Sparse subcell stipple with a
            # themed dim foreground keeps range interiors faint and readable.
            for y in range(upper + 1, lower):
                if (item.x + y) % (2 if g.ascii else 4) == 0:
                    mark(item.x, y)
            target_cells = borders
            previous_band = item

    if trend_points:
        target_cells = models
        previous = None
        for x, value, bridge in trend_points:
            y = round((1 - _fraction(value, lo, hi)) * (pixels_h - 1)) if lo <= value <= hi else None
            if y is not None:
                mark(x, y)
            if previous is not None and bridge:
                if previous[2] is not None and y is not None:
                    stroke(previous[0], previous[2], x, y)
                else:
                    segment = _clip_curve_segment(previous[0], previous[1], x, value, lo, hi)
                    if segment is not None:
                        x0, v0, x1, v1 = segment
                        stroke(round(x0), round((1 - _fraction(v0, lo, hi)) * (pixels_h - 1)),
                               round(x1), round((1 - _fraction(v1, lo, hi)) * (pixels_h - 1)))
            previous = (x, value, y)

    def ascii_stroke(mask: int) -> str:
        directions = mask & 47
        if directions & 32:
            return ":"
        if directions in (1, 4, 8):
            return {1: "-", 4: "\\", 8: "/"}[directions]
        return "+" if directions else "."

    rows: List[Row] = []
    if title:
        caption = chart_model_title(title, {"trend": bool(trend_points), "band": bool(band_points)})
        rows.append(_header(g, _window_values(values, sample_times, times), width, caption, unit, indent))
    for r, masks in enumerate(cells):
        axis_value = hi if r == 0 else lo if r == height - 1 else _mean((lo, hi)) if height >= 5 and r == height // 2 else None
        label = (axis_formatter(axis_value) if axis_formatter else fmt_num(axis_value, unit)) if axis_value is not None else ""
        row: Row = [(indent + pad(cut(label, max(0, axis_w - 1), g.ascii), axis_w - 1, ">") + ("|" if g.ascii else "│"), "dim")]
        fraction = 1 - r / max(1, height - 1)
        style = (color(fraction) if color else "chart-1") + "+bold"
        for x, mask in enumerate(masks):
            # Sparse dim guides remain separate from the measured curve ink.
            guide = "·" if not g.ascii and r in (0, height // 2) and x % 8 == 0 else " "
            mask |= borders[r][x] if borders else 0
            model = models[r][x] if models else 0
            fill = fills[r][x] if fills else 0
            visible = mask or model or fill
            glyph = ascii_stroke(visible) if g.ascii else BRAILLE[visible] if raster_y == 4 else QUADRANTS[visible]
            # Measured geometry wins a shared terminal cell. Fits never cover
            # an observed stroke; selector composition likewise preserves ink.
            ink = style if mask else "chart-2" if model else "chart-fill+" + (color(fraction) if color else "chart-1") + "+dim"
            row.append((glyph, ink) if visible else (guide, "dim"))
        rows.append(row)
    rows.append([(indent + " " * max(0, axis_w - 1) + ("+" if g.ascii else "└") + g.rule * chart_w, "dim")])
    if times:
        rows.append(_trace_axis(times, len(values), chart_w, indent + " " * axis_w, raster_x, elapsed) if sample_times is None else
                    time_axis(times[0], times[1], chart_w, indent + " " * axis_w, elapsed, units=time_units))
        if time_units:
            rows.append(time_selection_note(times[0], times[1], width, indent))
    return [clip_row(row, width) for row in rows]


def heatmap(g: Glyphs, matrix: Sequence[Sequence[Optional[float]]], width: int, labels: Sequence[str] = (),
            title: str = "", lo: float = 0.0, hi: Optional[float] = None, unit: str = "", indent: str = " ",
            temporal: bool = True) -> List[Row]:
    """Measured matrix: opaque two-column cells, missing values marked explicitly.

    Rows share one scale. Each row also prints its newest measurement so the map
    remains useful with colour disabled. At most 128 matrix rows are rendered.
    """
    from .palette import gradient_style
    source = [[_finite(v) for v in values] for values in matrix[:MAX_HEIGHT]]
    lo, hi = _bounds([v for values in source for v in values], lo, hi)
    label_w = min(16, max(0, (width - vlen(indent) - 12) // 3)) if labels else 0
    cell_w = 1 if g.ascii else 2
    count = max(0, min(MAX_COLUMNS // cell_w, (width - vlen(indent) - label_w - 10) // cell_w))
    out: List[Row] = []
    if title:
        out.append(clip_row([(indent + title, "cyan+bold"), (f"  {fmt_num(lo, unit)} to {fmt_num(hi, unit)}", "dim")], width))
    if not source:
        out.append(clip_row([(indent + "awaiting samples", "dim")], width))
    for i, values in enumerate(source):
        label = labels[i] if i < len(labels) else ""
        row: Row = [(indent + (pad(cut(label, label_w, g.ascii), label_w) + " " if label_w else ""), "dim")]
        for value in resample(values, count):
            if value is None:
                row.append(("?" if g.ascii else "··", "dim"))
            else:
                fraction = _fraction(value, lo, hi)
                if g.ascii:
                    row.append((g.spark[min(7, int(fraction * 7.999))], level(fraction)))
                else:
                    shade = gradient_style("track", "cyan", fraction * 2) if fraction < 0.5 else gradient_style("cyan", "yellow", (fraction - 0.5) * 2)
                    row.append(("██", shade))
        latest = values[-1] if values else None
        row.append((" " + fmt_num(latest, unit), "bold"))
        out.append(clip_row(row, width))
    if source:
        out.append(clip_row([(indent + ("?" if g.ascii else "··") + " unobserved" +
                            ("  newest at right" if temporal else ""), "dim")], width))
    return out


def stacked_bar(g: Glyphs, items: Sequence[Tuple[str, Optional[float], str]], width: int,
                indent: str = " ", title: str = "") -> List[Row]:
    """A proportional composition bar with an explicit numeric colour legend.

    Each item's style is respected. Unknown values are labelled '?' and receive no
    inferred allocation; all-zero data is labelled 'total 0'. Returns two rows.
    """
    values = [(label, _finite(value), style) for label, value, style in items]
    scale = max((max(0.0, value) for _, value, _ in values if value is not None), default=0.0)
    proportions = [max(0.0, value) / scale if value is not None and scale else 0.0 for _, value, _ in values]
    total = math.fsum(proportions)
    span = max(0, min(MAX_COLUMNS, width - vlen(indent)))
    prefix = (title + "  ") if title else ""
    bar_w = max(0, span - vlen(prefix))
    row: Row = [(indent + prefix, "cyan+bold")]
    if total > 0:
        # Round cumulative boundaries, so the allocated cells always sum to bar_w.
        positive = sum(value is not None and value > 0 for _, value, _ in values)
        separators = max(0, positive - 1) if bar_w >= positive * 2 - 1 else 0
        fill_w = bar_w - separators
        cumulative, previous, used, groups = 0.0, 0, 0, 0
        for (_, value, style), proportion in zip(values, proportions):
            cumulative += proportion
            boundary = min(fill_w, int(round(cumulative / total * fill_w)))
            length = boundary - previous
            if length:
                if groups and separators:
                    row.append(("|" if g.ascii else "│", "dim"))
                    used += 1
                row.append((g.full * length, style or "cyan"))
                used += length
                groups += 1
            previous = boundary
        row.append((g.empty * max(0, bar_w - used), "dim"))
    else:
        row.append((g.empty * bar_w, "dim"))
    legend: Row = [(indent, "")]
    for i, (label, value, style) in enumerate(values):
        if i:
            legend.append(("  ", ""))
        legend.append(((g.full + " " if not g.ascii else "") + label + " " + fmt_num(value), style or "cyan"))
    if not values or not any(value is not None for _, value, _ in values):
        legend.append(("awaiting samples", "dim"))
    elif total == 0:
        legend.append(("  total 0", "dim"))
    return [clip_row(row, width), clip_row(legend, width)]


def histogram(g: Glyphs, values: Sequence[float], bins: Sequence[float], width: int, indent: str = "   ", unit: str = "",
              fmt: Optional[Callable[[float], str]] = None) -> List[Row]:
    """Counts of ``values`` in the intervals of ``bins`` (edges, ascending; the last interval is open) as hbar rows."""
    vals = [v for v in values if v is not None]
    if not vals or not bins:
        return []
    f = fmt or (lambda x: fmt_num(x, unit))
    counts = [0] * (len(bins))
    for v in vals:
        i = 0
        while i + 1 < len(bins) and v >= bins[i + 1]:
            i += 1
        counts[i] += 1
    items = []
    for i, c in enumerate(counts):
        label = f"{f(bins[i])}-{f(bins[i + 1])}" if i + 1 < len(bins) else f">= {f(bins[i])}"
        items.append((label, float(c), "cyan"))
    return hbar_rows(g, items, width, indent=indent, label_w=max(10, max(vlen(l) for l, _, _ in items)), unit="")


def gantt(g: Glyphs, jobs: Sequence[dict], t0: float, t1: float, width: int, indent: str = "   ", label_w: int = 26) -> List[Row]:
    """A timeline: one row per job dict(name, id, state, submit, start, end) with epoch seconds (None when unknown),
    the queue wait as dots and the run as a bar, coloured by state; a time axis above."""
    label_w = min(label_w, max(1, (width - vlen(indent) - 12) // 2))
    bar_w = max(1, width - vlen(indent) - label_w - 12)    # room for the state after the bar
    rows: List[Row] = [time_axis(t0, t1, bar_w, indent + " " * (label_w + 1))]
    span = max(1.0, t1 - t0)

    def col(t):
        fraction = _fraction(t, t0, t1) if span >= 1.0 else min(max(t, t0), t1) - t0
        return int(round(fraction * (bar_w - 1)))

    for j in jobs:
        state = j.get("state", "")
        style = {"COMPLETED": "green", "RUNNING": "cyan", "PENDING": "yellow"}.get(state, "yellow" if state.startswith("CANCEL") else "red")
        line = [" "] * bar_w
        sub, start, end = j.get("submit"), j.get("start"), j.get("end")
        now = clock.now()
        if sub is not None and (start is not None or state == "PENDING"):
            queue_end = start if start is not None else now
            if sub <= t1 and queue_end >= t0 and queue_end >= sub:
                a, b = col(sub), col(queue_end)
                for x in range(a, b + 1):
                    line[x] = g.dot
        if start is not None:
            run_end = end if end is not None else (now if state in ("RUNNING", "COMPLETING", "SUSPENDED", "CONFIGURING") else None)
            if run_end is not None and start <= t1 and run_end >= t0 and run_end >= start:
                a, b = col(start), col(run_end)
                for x in range(a, b + 1):
                    line[x] = g.full
            elif run_end is None and t0 <= start <= t1:
                line[col(start)] = "?"
        label = cut(f"{j.get('name', '')} {j.get('id', '')}", label_w, g.ascii)
        text = "".join(line)
        rows.append([(indent + pad(label, label_w) + " ", "" if state != "PENDING" else "dim"), (text, style), (f" {state.lower()[:9]}", style)])
    return [clip_row(row, width) for row in rows]


def summary_line(g: Glyphs, parts: Sequence[Tuple[str, str]], indent: str = " ") -> Row:
    row: Row = [(indent, "")]
    for i, (text, style) in enumerate(parts):
        row.append((text, style))
        if i < len(parts) - 1:
            row.append((f"  {g.dot}  ", "dim"))
    return row
