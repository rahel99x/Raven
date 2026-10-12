"""Pure, bounded chart preferences and observation statistics.

Values remain in their reported units. Display preferences never rewrite a
sample, infer a unit, or make an unobserved interval look measured.
"""
from __future__ import annotations

import math
import statistics

from . import charts

PRESETS = (("5m", 300), ("30m", 1800), ("2h", 7200), ("all", None))
MAX_PREFERENCES = 64
RESOURCE_UNITS = {"CPU per core (%)": "%", "GPU utilization (%)": "%", "Memory (GB)": "GiB"}


def finite(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except (ValueError, TypeError, OverflowError):
        return False


def text(value, limit=96):
    return isinstance(value, str) and 0 < len(value) <= limit and value.isprintable()


def restore(saved):
    """Accept only bounded, printable display declarations and valid axes."""
    result = {"metric_display": {}, "axes": {}, "shared_scale": True, "chart_events": True}
    if not isinstance(saved, dict):
        return result
    for name, preference in list(saved.get("metric_display", {}).items())[:MAX_PREFERENCES] if isinstance(saved.get("metric_display"), dict) else []:
        if not text(name) or not isinstance(preference, dict):
            continue
        accepted = {key: preference[key] for key in ("label", "unit") if text(preference.get(key), 96)}
        precision = preference.get("precision")
        if isinstance(precision, int) and not isinstance(precision, bool) and 0 <= precision <= 12:
            accepted["precision"] = precision
        if accepted:
            result["metric_display"][name] = accepted
    for name, axis in list(saved.get("axes", {}).items())[:MAX_PREFERENCES] if isinstance(saved.get("axes"), dict) else []:
        if text(name) and valid_axis(axis):
            result["axes"][name] = {key: axis[key] for key in ("mode", "low", "high") if key in axis}
    for key in ("shared_scale", "chart_events"):
        if isinstance(saved.get(key), bool):
            result[key] = saved[key]
    return result


def valid_axis(axis):
    if not isinstance(axis, dict) or axis.get("mode") not in ("auto", "fixed", "log"):
        return False
    low, high = axis.get("low"), axis.get("high")
    if low is None and high is None:
        return axis["mode"] != "fixed"
    return finite(low) and finite(high) and low < high and (axis["mode"] != "log" or low > 0)


def display(state, name):
    options = preference(state, name)
    return options.get("label", name), options.get("unit", ""), options.get("precision")


def preference(state, name):
    options = {"unit": RESOURCE_UNITS[name]} if name in RESOURCE_UNITS else {}
    return {**options, **state.get("metric_display", {}).get(name, {})}


def format_value(value, preference):
    if not finite(value):
        return "unavailable"
    unit, precision = preference.get("unit", ""), preference.get("precision")
    if precision is None:
        return charts.fmt_num(value, unit)
    # Decimal formatting of a huge value is bounded by the finite float range.
    number = f"{value:.{precision}f}" if abs(value) < 1e12 else f"{value:.{max(1, precision)}g}"
    return number + ((" " + unit) if unit and unit != "%" else unit)


def format_axis(value, preference, lo, hi):
    """Honor declared precision, otherwise resolve ticks at the selected scale."""
    if preference.get("precision") is not None:
        return format_value(value, preference)
    return charts.axis_num(value, lo, hi, preference.get("unit", ""))


def axis_values(values, axis):
    """Return plotted coordinates, bounds, and count undefined on a log axis."""
    mode = axis.get("mode", "auto")
    known = [value for value in values if finite(value) and (mode != "log" or value > 0)]
    low, high = axis.get("low"), axis.get("high")
    if low is None:
        low = min(known, default=1.0) if mode == "log" else min([0.0] + known)
    if mode == "log":
        plotted = [math.log10(value) if finite(value) and value > 0 else None for value in values]
        lo = math.log10(low)
        hi = math.log10(high) if high is not None else max((value for value in plotted if value is not None), default=lo) + .05
        if hi <= lo:
            hi = lo + 1
        return plotted, lo, hi, sum(finite(value) and value <= 0 for value in values)
    return list(values), low, high, 0


def percentile(sorted_values, fraction):
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return charts._between(sorted_values[low], sorted_values[high], position - low)


def statistics_for(points, full_points=None):
    """Sample statistics and observed-interval coverage, without gap filling.

    Coverage uses the median positive cadence from the complete retained
    series. Only adjacent known samples within 2.5 cadences cover time. A
    singleton has sample coverage but no measurable interval coverage.
    """
    values = sorted(point["value"] for point in points if finite(point.get("value")))
    cadence_points = full_points if full_points is not None else points
    deltas = [b["t"] - a["t"] for a, b in zip(cadence_points, cadence_points[1:])
              if finite(a.get("t")) and finite(b.get("t")) and 0 < b["t"] - a["t"] < math.inf]
    cadence = statistics.median(deltas) if deltas else None
    covered = [b["t"] - a["t"] for a, b in zip(points, points[1:])
               if finite(a.get("value")) and finite(b.get("value")) and cadence
               and 0 <= b["t"] - a["t"] <= cadence * 2.5]
    span = points[-1]["t"] - points[0]["t"] if len(points) > 1 else 0
    # Ratios avoid overflow if the timestamp span crosses extreme finite ends.
    coverage = None if span <= 0 or not math.isfinite(span) else min(1.0, math.fsum(delta / span for delta in covered))
    return {"count": len(values), "samples": len(points), "missing": len(points) - len(values),
            "minimum": values[0] if values else None, "maximum": values[-1] if values else None,
            "median": percentile(values, .5), "mean": charts._mean(values) if values else None,
            "p05": percentile(values, .05), "p95": percentile(values, .95), "p99": percentile(values, .99),
            "sample_coverage": len(values) / len(points) if points else None, "time_coverage": coverage,
            "cadence": cadence}


def interval(points, state, name):
    selected = state.get("chart_range", {})
    if selected.get("metric") != name or not finite(selected.get("start")):
        return []
    end = selected.get("end", selected["start"])
    if not finite(end):
        end = selected["start"]
    low, high = sorted((selected["start"], end))
    return [point for point in points if low <= point["t"] <= high]
