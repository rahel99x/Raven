"""Interactive, bounded research dashboards and analysis overlays.

All frame rendering uses published snapshots or in-memory samples. Passport
files are read through the existing background worker, never while drawing.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from bisect import bisect_left, bisect_right
from contextlib import nullcontext
from itertools import islice
import math
import os
import statistics
import sys
import time

from . import charts, chart_tools, chart_interaction, clock, layout as L, scrollbars as S
from .model import human, short_duration
from .research_text import clean

MAX_METRICS = 64
MAX_POINTS = 10000
MAX_EVENTS = 512
MAX_CARD_CACHE = 8
MAX_CARD_CACHE_POINTS = MAX_POINTS * MAX_CARD_CACHE
MAX_SOURCE_CACHE = 8
COLORS = ("cyan", "magenta", "green", "yellow", "blue", "red")
SECTIONS = ("Overview", "Resources", "Steps", "Files", "Evidence")
_NEGATIVE_ZERO = ("negative-zero",)


def initialize(app):
    if not isinstance(getattr(app, "analysis_state", None), dict):
        app.analysis_state = {"pinned": [], "hidden": [], "order": [], "expanded": [], "colors": {},
                              "modal": "", "scroll": 0, "metric": "", "cursor": 0,
                              "zoom": 1.0, "pan": 0.0, "section": 0, "evidence_cursor": 0}
    for key, value in chart_tools.restore({}).items():
        app.analysis_state.setdefault(key, value)
    return app.analysis_state


def rows_selected(app, kind):
    """A cleared local row cursor stays hidden until deliberate selection."""
    if kind == "evidence":
        from .ui.selection_state import lines_cleared
        return not lines_cleared(app)
    cleared = initialize(app).get("rows_deselected", {})
    return not isinstance(cleared, dict) or not cleared.get(kind, False)


def resume_rows(app, kind):
    if kind == "evidence":
        from .ui.selection_effects import resume_lines
        resume_lines(app)
    elif kind in ("chart", "timeline", "chart_events"):
        state = initialize(app)
        cleared = state.get("rows_deselected", {})
        state["rows_deselected"] = {scope: bool(cleared.get(scope, False))
                                   for scope in ("chart", "timeline", "chart_events")} if isinstance(cleared, dict) else {}
        state["rows_deselected"][kind] = False


def context_click(app, y, x, button="left"):
    """Clear modal row selection without changing its job, source, or view."""
    if (button != "right" or getattr(app, "mode", "main") != "analysis"
            or any(not isinstance(value, int) or isinstance(value, bool) for value in (y, x))
            or not 0 <= x < getattr(app, "width", 120)
            or not 0 <= y < getattr(app, "height", 100000)):
        return False
    toolbar = getattr(app, "toolbar_state", {})
    if isinstance(toolbar, dict) and (toolbar.get("menu") is not None or toolbar.get("panel")):
        return False
    # This also makes direct handler callers preserve graph-reset precedence.
    if chart_interaction.handle_mouse(app, y, x, button="right"):
        return True
    # The foreground modal owns this gesture even when App.click dispatches
    # directly through the focus stack instead of the screen adapter.
    from .ui.selection_effects import clear_lines
    clear_lines(app)
    state = initialize(app)
    modal = state.get("modal")
    if modal in ("chart", "timeline", "chart_events"):
        chart_interaction.cancel(app)
        pointer = getattr(app, "interaction_state", {})
        graph = pointer.get("graph") if isinstance(pointer, dict) else None
        focused = graph.get(pointer.get("focused")) if graph is not None else None
        if focused is not None and focused.group in ("timeline_events", "chart_events"):
            pointer.update(active=False, focused=None, pending_focus=None)
        resume_rows(app, modal)
        state["rows_deselected"][modal] = True
        if modal == "chart":
            state.pop("chart_range", None)
        app.say("Row selection cleared; use the arrows or click a row to select")
    # Inspector and comparison controls express the current view. They keep
    # their state when there is no selected job, sample, or event row to clear.
    return True


def restore(app, ui):
    state = initialize(app)
    saved = ui.get("analysis", {}) if isinstance(ui, dict) else {}
    if not isinstance(saved, dict):
        return
    for key in ("pinned", "hidden", "order", "expanded"):
        value = saved.get(key, [])
        if isinstance(value, list):
            state[key] = list(dict.fromkeys(x for x in value[:MAX_METRICS]
                                           if isinstance(x, str) and 0 < len(x) <= 96 and x.isprintable()))
    colors = saved.get("colors", {})
    if isinstance(colors, dict):
        state["colors"] = {k: v for k, v in list(colors.items())[:MAX_METRICS]
                           if isinstance(k, str) and 0 < len(k) <= 96 and k.isprintable() and v in COLORS}
    state.update(chart_tools.restore(saved))


def save(app):
    state = initialize(app)
    return {"analysis": {**{k: state[k] for k in ("pinned", "hidden", "order", "expanded", "colors")},
                         **chart_tools.restore(state)}}


def command_names():
    return ["dashboard", "inspect", "chart", "timeline", "diff", "metricdisplay"]


def dashboard_names(app, series):
    state = initialize(app)
    available = list(series)[:MAX_METRICS]
    order = list(dict.fromkeys(state["pinned"] + state["order"] + available))
    query = state.get("metric_filter", "").casefold()
    return [name for name in order if name in series and name not in state["hidden"] and query in name.casefold()][:MAX_METRICS]


def observe_metrics(app, result, jid):
    """Record reported phase changes observed by Raven, with their source time."""
    state = initialize(app)
    phase = result.get("phase")
    if not phase:
        return
    identity = (jid, result.get("path", ""))
    previous = dict(state.get("phase_sources", {}))
    if previous.get(identity) == phase:
        return
    timestamp = result.get("last_t")
    if not _finite(timestamp):
        timestamp = max((point.get("t") for points in result.get("series", {}).values()
                         for point in points[-1:] if _finite(point.get("t"))), default=None)
    if not _finite(timestamp):
        return
    previous[identity] = phase
    while len(previous) > MAX_METRICS:
        previous.pop(next(iter(previous)))
    state["phase_sources"] = previous
    events = list(state.get("metric_events", []))
    events.append({"t": timestamp, "kind": "phase", "job": jid,
                   "text": f"Observed reported phase: {phase}", "source": result.get("path", ""),
                   "path": result.get("path", "") if result.get("path") != "simulated application metrics" else ""})
    del events[:-MAX_METRICS]
    state["metric_events"] = events


def _finite(value):
    try:
        return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)
    except (TypeError, ValueError, OverflowError):
        return False


def _fmt(value):
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:.9g}" if math.isfinite(value) else "unavailable"
    if isinstance(value, dict):
        return clean(", ".join(f"{k}={_fmt(v)}" for k, v in list(value.items())[:12]), limit=400)
    if isinstance(value, (list, tuple)):
        return clean(", ".join(_fmt(v) for v in value[:12]), limit=400) or "none"
    return clean(value, limit=400)


def _time(value):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
    except (ValueError, TypeError, OverflowError, OSError):
        return _fmt(value)


def _points(points):
    """Only malformed timestamps are discarded; unknown values remain gaps."""
    return sorted(({"t": p["t"], "value": p.get("value") if _finite(p.get("value")) else None,
                    "step": p.get("step")} for p in points[-MAX_POINTS:]
                   if isinstance(p, dict) and _finite(p.get("t"))), key=lambda p: p["t"])


def memory_series(app, jid):
    store = getattr(app, "store", None)
    if store is None:
        return []
    with getattr(store, "lock", nullcontext()):
        retained = list(islice(reversed(getattr(store, "series", {}).get(jid, ())), MAX_POINTS))
        retained.reverse()
        return retained


def _resource_series(app, jid):
    from raven.recordings.presentation import prepared_series
    return prepared_series(app, jid, ("resource-columns/v1", MAX_POINTS),
                           lambda records: _resource_columns(records[-MAX_POINTS:]),
                           memory_only=True)


def _resource_columns(records):
    values = {"CPU per core (%)": [], "Memory (GB)": [], "GPU utilization (%)": []}
    for sample in records:
        if not isinstance(sample, dict) or not _finite(sample.get("t")):
            continue
        t = sample["t"]
        if sample.get("k") == "live":
            cpu = sample.get("cpu") if sample.get("cpu") is not None else sample.get("eff")
            values["CPU per core (%)"].append({"t": t, "value": cpu * 100 if _finite(cpu) else None})
            rss = sample.get("rss")
            values["Memory (GB)"].append({"t": t, "value": rss / 1024 ** 3 if _finite(rss) else None})
        elif sample.get("k") == "gpu":
            samples = sample.get("gpu", {})
            if not isinstance(samples, dict):
                samples = {}
            util = [v[0] for v in samples.values() if isinstance(v, (list, tuple)) and v and _finite(v[0]) and 0 <= v[0] <= 100]
            values["GPU utilization (%)"].append({"t": t, "value": statistics.fmean(util) if util else None})
    return {name: points for name, points in values.items() if points}


def _analysis_jid(app):
    if getattr(app, "tab", "") == "research":
        return getattr(app, "research_job_id", None) or getattr(app, "selected_id", None)
    if getattr(app, "tab", "") == "analytics":
        return getattr(app, "analytics_job", None) or getattr(app, "selected_id", None)
    return getattr(app, "selected_id", None)


def publish_metric_metadata(app, result):
    """Publish only already-read semantics and rank choices for this view."""
    state = initialize(app)
    series = result.get("series", {})
    source = result.get("requested_path", result.get("path", ""))
    if state.get("metric_source") != source:
        state["rank_page"] = 0
    state["metric_source"] = source
    state["metric_metadata"] = {name: chart_tools.metric_metadata(result, name) for name in list(series)[:MAX_METRICS]}
    state["rank_streams"] = result.get("rank_streams", [])[:128]
    state["selected_stream"] = result.get("selected_stream", "")


def chart_data(app, snap):
    state = initialize(app)
    jid = state.get("chart_job") if state.get("modal") in ("chart", "chart_events") else _analysis_jid(app)
    result = getattr(app, "analysis_result", {}) or {}
    series = result.get("series", {}) if isinstance(result, dict) else {}
    hub = getattr(app, "research", None)
    generation = getattr(hub, "generation", None)
    if ((series or result.get("rank_streams")) and getattr(app, "analysis_result_job", None) == jid
            and getattr(app, "analysis_result_generation", None) == generation):
        publish_metric_metadata(app, result)
        return series, result.get("path", "application metrics"), jid
    if hub is not None and hasattr(hub, "current"):
        context = hub.context(snap, app)
        context["view"], context["jid"] = "experiment", jid
        cached = hub.current(context)
        if cached.get("series") or cached.get("rank_streams"):
            publish_metric_metadata(app, cached)
            return cached.get("series", {}), cached.get("path", "application metrics"), jid
    publish_metric_metadata(app, {})
    return _resource_series(app, jid), "Raven session resource samples", jid


def _chart_frame_series(app):
    """Reuse the published frame while processing its bounded input batch."""
    state = initialize(app)
    frame = state.get("chart_frame", {})
    if (frame.get("job") == state.get("chart_job") and "series" in frame
            and frame.get("generation") == getattr(getattr(app, "research", None), "generation", None)
            and frame.get("result") == id(getattr(app, "analysis_result", None))):
        return frame["series"]
    series, _, _ = chart_data(app, app.store.snapshot())
    return series


def _viewport_key(state, name, points):
    return (state.get("chart_job"), name, id(points), len(points),
            state.get("zoom", 1), state.get("pan", 0), state.get("window"), state.get("chart_box"),
            state.get("chart_live_window"), state.get("chart_display_window"))


def _chart_visible_points(app, series, name):
    state = initialize(app)
    points = series.get(name, [])
    source = state.get("chart_frame", {}).get("source", "Raven session resource samples")
    identity = chart_key(app, name, source, job=state.get("chart_frame", {}).get("record"))
    box = chart_interaction.bounds(app, identity,
                                   scale="log" if chart_tools.metric_axis(state, name, state.get("metric_metadata", {}).get(name, {})).get("mode") == "log" else "linear")
    from . import metric_live
    # Input acts on the published frame. Advancing a playback clock here would
    # change which sample an arrow selects before the replacement frame paints.
    current = state.get("chart_interaction_key") == identity
    live_window = state.get("chart_live_window") if current else metric_live.window(app, identity)
    display_window = state.get("chart_display_window") if current else None
    state["chart_box"] = (box["x"], box["y"]) if box else None
    state["chart_live_window"] = live_window
    state["chart_display_window"] = display_window
    cached = state.get("chart_visible", {})
    if cached.get("key") == _viewport_key(state, name, points):
        return cached["points"]
    normalized = _points(points)
    times = box["x"] if box else live_window or display_window
    visible = [point for point in normalized if times[0] <= point["t"] <= times[1]] if times else viewport(normalized, state, normalized=True)[0]
    state["chart_visible"] = {"key": _viewport_key(state, name, points), "points": visible}
    return visible


def viewport(points, state, *, normalized=False):
    points = points if normalized else _points(points)
    if not points:
        return [], (0.0, 0.0)
    t0, t1 = points[0]["t"], points[-1]["t"]
    zoom = max(1.0, min(1024.0, state.get("zoom", 1.0)))
    pan = max(0.0, min(1.0, state.get("pan", 0.0)))
    window = state.get("window")
    span = t1 - t0
    if _finite(window) and window > 0 and math.isfinite(span) and span > 0:
        zoom = max(1.0, span / window)
    # Convex interpolation avoids overflowing opposite-sign finite endpoints.
    start_fraction, end_fraction = pan * (1 - 1 / zoom), pan * (1 - 1 / zoom) + 1 / zoom
    a, b = t0 * (1 - start_fraction) + t1 * start_fraction, t0 * (1 - end_fraction) + t1 * end_fraction
    return [p for p in points if a <= p["t"] <= b], (a, b)


def _cadence(points):
    deltas = [b["t"] - a["t"] for a, b in zip(points, points[1:])
              if 0 < b["t"] - a["t"] < math.inf]
    return statistics.median(deltas) if deltas else None


def raster_viewport(points, times):
    """Keep original bracketing neighbors solely to clip the measured curve.

    The returned records remain genuine observations at their original times.
    Headers, sample counts, cursor inspection and interval statistics must use
    the strictly visible records instead. Unknown neighbors still break curves.
    """
    if not points or not all(_finite(value) for value in times) or times[0] > times[1]:
        return []
    first = bisect_left(points, times[0], key=lambda point: point["t"])
    last = bisect_right(points, times[1], key=lambda point: point["t"])
    return points[max(0, first - 1):min(len(points), last + 1)]


def _crosshair(rows, width, height, points, selected, times, ascii_, plot_rect=None):
    """Place a visible guide on the same timestamp buckets as the chart."""
    top, left, bottom, right = plot_rect or (1, 10, height + 1, width)
    plot_width = max(0, min(charts.MAX_COLUMNS, right - left))
    if not points or not plot_width or not selected or times[1] <= times[0]:
        return rows
    # Braille is a two-column raster, and its timestamp rounding happens there.
    raster = 1 if ascii_ else 2
    fraction = charts._fraction(selected["t"], *times)
    x = min(plot_width - 1, round(fraction * (plot_width * raster - 1)) // raster) + left
    guide = "|" if ascii_ else "│"
    out = list(rows)
    for y in range(top, min(len(out), bottom)):
        before, after, position, ink = [], [], 0, None
        for text, style in out[y]:
            for ch in text:
                size = L.vlen(ch)
                if position + size <= x:
                    before.append((ch, style))
                elif position >= x + 1:
                    after.append((ch, style))
                elif ch.strip():
                    # An inspection guide must not erase measured curve ink.
                    ink = (ch, style)
                position += size
        out[y] = before + [ink or (guide, "yellow+bold")] + after
    return out


def _highlight_interval(rows, width, height, selected, times, ascii_=False, plot_rect=None):
    """Show the selected source interval without adding or changing a value."""
    top, axis_left, bottom, axis_right = plot_rect or (1, 10, height + 1, width)
    plot_width = max(0, min(charts.MAX_COLUMNS, axis_right - axis_left))
    if not selected or not plot_width or times[1] <= times[0]:
        return rows
    first, last = selected[0]["t"], selected[-1]["t"]
    if last < times[0] or first > times[1]:
        return rows
    raster = 1 if ascii_ else 2
    left = axis_left + round(charts._fraction(first, *times) * (plot_width * raster - 1)) // raster
    right = axis_left + round(charts._fraction(last, *times) * (plot_width * raster - 1)) // raster
    output = list(rows)
    for y in range(top, min(len(rows), bottom)):
        line, position = [], 0
        for value, style in rows[y]:
            for char in value:
                size = L.vlen(char)
                line.append((char, style + "+rev" if left <= position <= right else style))
                position += size
        output[y] = line
    return output


def _source_row(g, source, metadata, *, app=None, identity=None):
    latest, visible, total, gaps, *extra = metadata
    age = clock.now() - latest if latest is not None else None
    stamp = "latest " + short_duration(abs(age) if _finite(age) else None) + (" ahead of clock" if _finite(age) and age < 0 else " ago")
    cadence = ""
    if extra:
        from .metric_live import format_delta
        cadence = " | observed cadence " + (format_delta(extra[0]) if _finite(extra[0]) else "unknown")
    polling = ""
    if app is not None and identity is not None:
        from . import metric_live
        from . import metric_sampling_runtime as metric_sampling
        if metric_live._eligible(app, metric_live.canonical(identity)):
            seconds = metric_sampling.cadence(app, identity)
            if seconds is not None:
                kind = "read every" if metric_sampling.source(identity) == "research" else "poll every"
                polling = " | " + kind + " " + metric_sampling.format_interval(seconds, ascii_=g.ascii)
        if len(extra) > 1 and extra[1]:
            playback = metric_live.playback_label(app, identity, ascii_=g.ascii,
                                                   displayed_end=extra[2] if len(extra) > 2 else None)
            if playback:
                polling += " | " + playback
    return [(clean(f" Source: {source} | {visible}/{total} samples | {stamp} | gaps {gaps}{cadence}{polling}", g.ascii), "dim")]


def _card_value(value):
    if not _finite(value):
        return None
    # Numeric equality equates signed zero, but the exact summary formatter
    # displays its sign. Keep that distinction in the cache's content key.
    return _NEGATIVE_ZERO if value == 0 and math.copysign(1, value) < 0 else value


def _prepared_card_source(app, content):
    """Reuse normalized observations after an exact bounded mutation scan.

    The signature is rebuilt from every retained timestamp/value before this
    lookup. Corrections within an existing list therefore invalidate it. Only
    immutable scalar records are retained; interactive step metadata keeps its
    original path and cannot be mistaken for a card's reduced observations.
    """
    from raven.collection.retention import cache as bounded_cache
    state = initialize(app)
    if 'chart_source_cache' not in state: state['chart_source_cache']=bounded_cache(app,'chart_source_cache')
    cache = state['chart_source_cache']
    source = cache.get(content)
    if source is None:
        points = sorted(({"t": timestamp, "value": -0.0 if value is _NEGATIVE_ZERO else value, "step": None}
                         for timestamp, value in content), key=lambda point: point["t"])
        state = initialize(app)
        state["chart_source_sequence"] = state.get("chart_source_sequence", 0) + 1
        source = {"points": points, "times": tuple(point["t"] for point in points),
                  "token": state["chart_source_sequence"]}
        cache[content] = source
    cache.move_to_end(content)
    while len(cache) > MAX_SOURCE_CACHE:
        cache.popitem(last=False)
    return source


def _source_cadence(app, times, limit):
    """Cache cadence by exact sorted timestamps and admitted prefix length."""
    from raven.collection.retention import cache as bounded_cache
    state = initialize(app)
    if 'chart_cadence_cache' not in state: state['chart_cadence_cache']=bounded_cache(app,'chart_cadence_cache')
    cache = state['chart_cadence_cache']
    key = (times, limit)
    if key not in cache:
        deltas = [b - a for a, b in zip(times[:max(0, limit - 1)], times[1:limit])
                  if 0 < b - a < math.inf]
        result = statistics.median(deltas) if deltas else None
        cache[key] = result
    else:
        result = cache[key]
    cache.move_to_end(key)
    while len(cache) > MAX_SOURCE_CACHE:
        cache.popitem(last=False)
    return result


def _published_chart_job(app, jid):
    """Reuse one exact current job without taking another full snapshot."""
    store = getattr(app, "store", None)
    if store is None or jid is None:
        return None
    jobs = getattr(store, "jobs", ())
    finished = getattr(store, "finished", ())
    if not isinstance(jobs, (list, tuple)) or not isinstance(finished, (list, tuple)):
        return None
    state = initialize(app)
    cached = state.get("chart_key_job", {})
    context = (id(store), id(jobs), len(jobs), id(finished), len(finished), str(jid))
    if cached.get("context") == context:
        record = cached.get("record")
        rows = jobs if cached.get("kind") == "jobs" else finished
        index = cached.get("index")
        if isinstance(index, int) and 0 <= index < len(rows) and rows[index] is record:
            return record
    for kind, rows in (("jobs", jobs), ("finished", finished)):
        for index, record in enumerate(rows):
            if str(getattr(record, "id", "")) == str(jid):
                state["chart_key_job"] = {"context": context, "record": record, "kind": kind, "index": index}
                return record
    state.pop("chart_key_job", None)
    departed = getattr(store, "departed_jobs", {})
    return departed.get(str(jid)) if isinstance(departed, dict) else None


def chart_key(app, name, source, *, interactive=True, jid=None, job=None):
    state = initialize(app)
    if jid is None:
        jid = state.get("chart_job") if interactive and state.get("modal") in ("chart", "chart_events") else _analysis_jid(app)
    project = getattr(app, "project_state", {})
    binding = project.get("binding") if isinstance(project, dict) else None
    bound_attempt = (isinstance(binding, dict) and str(binding.get("job_id", "")) == str(jid or "") and
                     binding.get("attempt") is not None)
    attempt = None
    # Native resource samples are collected for a scheduler attempt even when
    # the selected job is linked to a project's independently numbered run.
    # Using that run's attempt here makes CPU/GPU requests fail the sampler's
    # exact-attempt guard while their slider thumbs still move.
    if source == "Raven session resource samples" or not bound_attempt:
        job = job if job is not None and str(getattr(job, "id", "")) == str(jid or "") else _published_chart_job(app, jid)
        if job is not None:
            attempt = "scheduler:" + "|".join(str(getattr(job, field, None) or "") for field in ("submit", "start"))
    return chart_interaction.key(app, name, source, jid, scope="reported-metric", attempt=attempt)


def running_job(snapshot, jid):
    """Only a matching current scheduler record permits a moving Live window."""
    return bool(jid and isinstance(snapshot, dict) and any(
        str(getattr(job, "id", "")) == str(jid) and getattr(job, "state", "") == "RUNNING"
        for job in snapshot.get("jobs", ())))


def _playback_window(app, identity, timestamps, running):
    """Advance only from bounded, already published timestamp evidence.

    The source summary ignores duplicate and future timestamps. Values may be
    unknown: an observed missing value still advances its clock while remaining
    a gap in the measured curve. No input callback or file reader is involved.
    """
    from . import metric_live

    if not running or not metric_live._eligible(app, metric_live.canonical(identity)):
        return None, None, None, None
    now = clock.now()
    if not _finite(now):
        return None, None, None, None
    # Both callers supply sorted finite timestamps from their normalized
    # source. Admission and the latest distinct predecessor are binary reads;
    # a narrow Live frame never rescans thousands of old records here.
    limit = bisect_right(timestamps, now)
    newest = timestamps[limit - 1] if limit else None
    predecessor = bisect_left(timestamps, newest, 0, limit) - 1 if limit else -1
    previous = timestamps[predecessor] if predecessor >= 0 else None
    oldest = timestamps[0] if limit else None
    end = metric_live.display_end(app, identity, now=now, newest=newest,
                                  previous=previous, oldest=oldest)
    return metric_live.window(app, identity, now=now), now, end, newest


def _playback_resolution(app, identity, metadata, following):
    """Refresh long histories only when a timestamp can move a raster cell."""
    from . import metric_live

    times, rectangle = metadata.get("x_bounds"), metadata.get("plot_rect")
    if times and rectangle:
        metric_live.display_resolution(app, identity, times[1] - times[0],
                                       rectangle[3] - rectangle[1], following=following)


def _present_history(app, identity, *, interactive, g, width, height, times, low, high,
                     axis, cadence, acquired, last, now, source_token, following):
    """Keep an automatic history frame until its measured geometry can change."""
    from . import metric_live, metric_playback
    from . import metric_sampling_runtime as metric_sampling

    presentation = ("reported-modal" if interactive else "reported-card", identity)
    if not following:
        metric_playback.reset_presentation(app, presentation)
        return times
    candidate = metric_live.playback_status(app, identity)
    if candidate is None or source_token is None:
        metric_playback.reset_presentation(app, presentation)
        return times
    before = acquired[last - 1] if last else None
    after = acquired[last] if last < len(acquired) else None
    pairs = [point for point in (before, after) if point is not None]
    transformed, _, _, _ = chart_tools.axis_values([point["value"] for point in pairs], axis)
    left = transformed[0] if before is not None and transformed else None
    right = transformed[-1] if after is not None and transformed else None
    bridge = (before is not None and after is not None and left is not None and right is not None
              and cadence is not None and after["t"] - before["t"] <= cadence * 2.5)
    edge = left if before is not None and before["t"] == times[1] else None
    if bridge and before["t"] < times[1] < after["t"]:
        fraction = charts._fraction(times[1], before["t"], after["t"])
        edge = charts._between(min(left, right), max(left, right), fraction if left <= right else 1 - fraction)
    low, high = charts._bounds([], low, high)
    y_pixels = max(1, min(charts.MAX_HEIGHT, height) * (1 if g.ascii else 4))
    edge_pixel = round(charts._fraction(edge, low, high) * (y_pixels - 1)) if edge is not None else None
    plot_width = max(1, min(charts.MAX_COLUMNS, width - 10))
    span = times[1] - times[0]
    owner = getattr(app, "_chart_owner", app)
    geometry = (width, height, g.ascii, g.spark, g.box, g.rule,
                getattr(owner, "theme", ""), axis.get("mode"), low, high, cadence, times[0])
    signature = (last, before["t"] if before else None, after["t"] if after else None,
                 left is not None, right is not None, bridge, edge_pixel)
    entry = metric_live.initialize(app)["entries"].get(metric_live.canonical(identity), {})
    replay = getattr(getattr(owner, "replay", None), "clock", None)
    result = metric_playback.present(app, presentation, candidate, now=now, geometry=geometry,
                                     x_interval=span / (plot_width * (1 if g.ascii else 2)),
                                     edge_signature=signature, oldest=times[0],
                                     force_token=(source_token, entry.get("rate"), entry.get("delta"),
                                                  entry.get("token"), entry.get("generation"),
                                                  id(replay), getattr(replay, "generation", None),
                                                  metric_sampling.cadence(app, identity)))
    return (times[0], result.end) if result is not None else times


def chart_rows(g, app, points, width, height, name, source, *, interactive=True, snapshot=None,
               metadata=None, zoom_key=None, running=False, metric_meta=None):
    """Cache pure dashboard cards while preserving exact interactive charts.

    A content scan detects edits anywhere in the retained series, including
    corrections published into the same list or dictionary. The bounded cache
    skips normalization, sorting, cadence analysis, and rasterization only for
    unchanged noninteractive cards. Source identity and age are rendered fresh.
    """
    metric_meta = metric_meta if metric_meta is not None else initialize(app).get("metric_metadata", {}).get(name, {})
    zoom_key = chart_key(app, name, source, interactive=interactive) if zoom_key is None else zoom_key
    from . import metric_live
    # A card can be narrower than its optional controls. Its running source
    # still owns a playback clock and remains eligible for timed documents.
    metric_live.set_running(app, zoom_key, running)
    if interactive:
        full = _points(points)
        timestamps = tuple(point["t"] for point in full)
        live_window, live_now, display_end, _ = _playback_window(
            app, zoom_key, timestamps, running)
        source_token = None
        if live_now is not None and live_window is None:
            content = tuple((point["t"], _card_value(point["value"])) for point in full)
            source_token = _prepared_card_source(app, content)["token"]
        return _render_chart_rows(g, app, points, width, height, name, source,
                                  interactive=True, snapshot=snapshot, metadata=metadata, zoom_key=zoom_key, metric_meta=metric_meta,
                                  normalized_points=full, source_times=timestamps, live_window=live_window,
                                  live_now=live_now, display_end=display_end, source_token=source_token)
    state = initialize(app)
    content = tuple((point["t"], _card_value(point.get("value")))
                    for point in points[-MAX_POINTS:]
                    if isinstance(point, dict) and _finite(point.get("t")))
    prepared = _prepared_card_source(app, content)
    live_window, live_now, display_end, newest = _playback_window(
        app, zoom_key, prepared["times"], running)
    label, unit, precision = chart_tools.display(state, name)
    axis = chart_tools.metric_axis(state, name, metric_meta)
    color = state["colors"].get(name, COLORS[sum(ord(char) for char in name) % len(COLORS)])
    # The timestamp axis uses libc local time. A session timezone change must
    # invalidate the raster even when its source values remain unchanged.
    zone = (os.environ.get("TZ"), time.tzname, time.timezone, time.daylight)
    glyphs = (g.ascii, g.spark, g.box, g.dot, g.rule)
    scale = "log" if axis.get("mode") == "log" else "linear"
    box = chart_interaction.bounds(app, zoom_key, scale=scale)
    auto_fit = chart_interaction.autofit(app, zoom_key, scale=scale)
    captured = chart_interaction.captured_bounds(app, zoom_key, scale=scale)
    if live_window is not None or box or captured or live_now is None:
        from .metric_playback import reset_presentation
        reset_presentation(app, ("reported-card", zoom_key))
    key = (name, metric_meta.get("kind"), metric_meta.get("unit"), metric_meta.get("coalesced"),
           state.get("metric_modes", {}).get(name), metric_meta.get("baseline"), metric_meta.get("estimate"), width, height, glyphs, label, unit, (type(precision), precision), color,
           axis.get("mode"), repr(axis.get("low")), repr(axis.get("high")), zone, id(prepared),
           (box["x"], box["y"], auto_fit) if box else None, live_window,
           (live_now is not None, display_end if not box and not captured else None, newest),
           (captured["x"], captured["y"]) if captured else None)
    from raven.collection.retention import cache as bounded_cache
    if 'chart_card_cache' not in state: state['chart_card_cache']=bounded_cache(app,'chart_card_cache')
    cache = state['chart_card_cache']
    entry = cache.get(key)
    if entry is not None:
        cache.move_to_end(key)
        _playback_resolution(app, zoom_key, entry["plot_metadata"], entry["following"])
        if isinstance(metadata, dict):
            metadata.clear()
            metadata.update(entry["plot_metadata"])
            metadata["key"] = zoom_key
        return [list(row) for row in entry["rows"]] + [_source_row(g, source, entry["metadata"], app=app, identity=zoom_key)]
    cache_metadata, plot_metadata = {}, {}
    rows = _render_chart_rows(g, app, points, width, height, name, source, interactive=False,
                              snapshot=snapshot, normalized_points=prepared["points"], source_times=prepared["times"],
                              cache_metadata=cache_metadata,
                              metadata=plot_metadata, zoom_key=zoom_key, metric_meta=metric_meta, live_window=live_window,
                              live_now=live_now, display_end=display_end, source_token=prepared["token"])
    if cache_metadata.get("display_end") is not None:
        key = (*key[:-2], (live_now is not None, cache_metadata["display_end"], newest), key[-1])
    if isinstance(metadata, dict):
        metadata.clear()
        metadata.update(plot_metadata)
    cache[key] = {"rows": tuple(tuple(row) for row in rows[:-1]),
                  "metadata": cache_metadata["source"], "points": len(content),
                  "following": cache_metadata["following"],
                  # Keep the exact source alive while its identity is in a
                  # raster key; eviction must never permit object-ID reuse.
                  "source": prepared,
                  "plot_metadata": dict(plot_metadata)}
    # Each source is already capped at MAX_POINTS. Eight entries therefore
    # meet the point budget without rehashing every large source key on each
    # moving frame just to recompute an unchanged total.
    while len(cache) > MAX_CARD_CACHE:
        cache.popitem(last=False)
    return rows


def _render_chart_rows(g, app, points, width, height, name, source, *, interactive=True,
                       snapshot=None, normalized_points=None, cache_metadata=None, metadata=None, zoom_key=None,
                       live_window=None, live_now=None, display_end=None, source_times=None, source_token=None, metric_meta=None):
    state = initialize(app)
    metric_meta = metric_meta or {}
    kind = metric_meta.get("kind")
    trend, bands, model_kind = chart_tools.metric_layers(state, name, metric_meta)
    full = _points(points) if normalized_points is None else normalized_points
    source_times = tuple(point["t"] for point in full) if source_times is None else source_times
    axis = chart_tools.metric_axis(state, name, metric_meta)
    scale = "log" if axis.get("mode") == "log" else "linear"
    box = chart_interaction.bounds(app, zoom_key, scale=scale)
    captured = chart_interaction.captured_bounds(app, zoom_key, scale=scale)
    # The raster can use the first acquired successor beyond a delayed edge.
    # Its cutoff is the real clock, never the delayed display endpoint. Future
    # replay records remain excluded even before a source has enough history.
    acquired_count = bisect_right(source_times, live_now) if live_now is not None else len(full)
    acquired = full if acquired_count == len(full) else full[:acquired_count]
    following = (not interactive or (state.get("zoom", 1) == 1 and not state.get("pan", 0)
                                     and not state.get("window")))
    display_window = None
    if captured:
        times = captured["x"]
    elif box:
        times = box["x"]
    elif live_window:
        times = live_window
    elif display_end is not None and following and acquired:
        times = display_window = (min(acquired[0]["t"], display_end), display_end)
    else:
        _, times = viewport(acquired, state if interactive else {}, normalized=True)
    first = bisect_left(source_times, times[0], 0, acquired_count)
    last = bisect_right(source_times, times[1], first, acquired_count)
    visible = full[first:last]
    if interactive:
        state["chart_interaction_key"] = zoom_key
        state["chart_box"] = (box["x"], box["y"]) if box else None
        state["chart_live_window"] = live_window
        state["chart_display_window"] = display_window
        state["chart_visible"] = {"key": _viewport_key(state, name, points), "points": visible}
    values = [p["value"] for p in visible]
    color = state["colors"].get(name, COLORS[sum(ord(c) for c in name) % len(COLORS)])
    known = [v for v in values if v is not None]
    label, unit, precision = chart_tools.display(state, name)
    unit = unit or metric_meta.get("unit", "")
    preference = {"unit": unit, **chart_tools.preference(state, name)}
    axis = chart_tools.metric_axis(state, name, metric_meta)
    visible_plotted, low, high, undefined = chart_tools.axis_values(values, axis)
    baseline = metric_meta.get("baseline") if model_kind == "drift" else None
    declared_baseline = baseline is not None
    if model_kind == "drift" and baseline is None:
        baseline = next((p["value"] for p in acquired if p["value"] is not None), None)
    reference = baseline
    if baseline is not None and axis.get("mode") == "log":
        reference = math.log10(baseline) if baseline > 0 else None
    if reference is not None and axis.get("low") is None:
        low = min(low, reference)
    if reference is not None and axis.get("high") is None:
        high = max([reference] + [value for value in visible_plotted if value is not None])
    raster_points = full[max(0, first - 1):min(acquired_count, last + 1)]
    if len(raster_points) > len(visible):
        low, high = charts._bounds(visible_plotted, low, high)
    plotted, _, _, _ = chart_tools.axis_values([point["value"] for point in raster_points], axis)
    cadence = _source_cadence(app, source_times, acquired_count)
    buffered = live_now is not None and not box and not captured and (live_window is not None or following)
    if buffered:
        # A one-second window can lie between two five-second observations.
        # Fit their clipped measured segment rather than scaling empty header
        # statistics to 0..1 and hiding the only line. Unknown endpoints and
        # source outages still supply no interpolated vertices.
        # This normalized source is already sorted. Fit exact clipped vertices
        # directly: coarse buckets intentionally discard a bucket containing
        # any missing value, which can hide a valid edge or a large extremum.
        vertices = charts._iter_time_samples(zip((point["t"] for point in raster_points), plotted),
                                              times, min(sys.float_info.max, cadence * 2.5) if cadence else 0.)
        measured = [value for _, value, _ in vertices if value is not None]
        fitted_low, fitted_high = charts._bounds(measured, min([low] + measured), None)
        if axis.get("low") is None:
            low = fitted_low
        if axis.get("high") is None:
            high = fitted_high
    auto_fit = chart_interaction.autofit(app, zoom_key, scale="log" if axis.get("mode") == "log" else "linear")
    if box:
        low, high = box["y"]
        if auto_fit and not captured:
            low, high = charts.fit_time_bounds(plotted, [point["t"] for point in raster_points],
                                               times, box["y"], cadence)
    if captured:
        low, high = captured["y"]
    automatic_history = bool(buffered and live_window is None and display_window is not None)
    times = _present_history(app, zoom_key, interactive=interactive, g=g, width=width, height=height,
                              times=times, low=low, high=high, axis=axis, cadence=cadence,
                              acquired=acquired, last=last, now=live_now, source_token=source_token,
                              following=automatic_history)
    if automatic_history:
        display_window = times
        if interactive:
            state["chart_display_window"] = display_window
            state["chart_visible"] = {"key": _viewport_key(state, name, points), "points": visible}
    def axis_label(value):
        if axis["mode"] == "log":
            try:
                value = 10 ** value
            except OverflowError:
                return ">1e308"
        if auto_fit and axis["mode"] != "log":
            return chart_tools.format_axis(value, preference, low, high)
        return chart_tools.format_value(value, preference)
    plot_metadata = metadata if isinstance(metadata, dict) else {}
    # Axis/color callbacks are exact local closures, so the generic native
    # raster cache cannot key them. Keep this small pure-raster cache tied to
    # their scalar inputs, outside all headers, cursor and statistics work.
    raster_key = None
    raster_cache = state.setdefault("chart_history_rasters", OrderedDict())
    if automatic_history:
        raster_key = (source_token, times, low, high, scale, cadence, width, height, model_kind, trend, bands, baseline,
                      g.ascii, g.spark, g.box, g.dot, g.rule, color, label, unit, (type(precision), precision),
                      getattr(getattr(app, "_chart_owner", app), "theme", ""),
                      os.environ.get("TZ"), time.tzname, time.timezone, time.daylight)
    cached_raster = raster_cache.get(raster_key) if raster_key is not None else None
    if cached_raster is not None:
        raster_cache.move_to_end(raster_key)
        rows = [list(row) for row in cached_raster[0]]
        plot_metadata.clear()
        plot_metadata.update(cached_raster[1])
    else:
        rows = charts.braille_chart(g, plotted, width, height, lo=low, hi=high, title=clean(label, g.ascii),
                                   sample_times=[p["t"] for p in raster_points], times=times,
                                   sample_interval=cadence, color=lambda _: color, axis_formatter=axis_label,
                                   metadata=plot_metadata, fitted=auto_fit, time_units=bool(box),
                                   trend=trend, bands=bands, model_kind=model_kind, reference=reference)
        if raster_key is not None and sum(L.vlen(L.row_text(row)) for row in rows) <= 65536:
            raster_cache[raster_key] = (tuple(tuple(row) for row in rows), dict(plot_metadata))
            while len(raster_cache) > MAX_CARD_CACHE:
                raster_cache.popitem(last=False)
    plot_metadata["scale"] = "log" if axis.get("mode") == "log" else "linear"
    plot_metadata["key"] = zoom_key
    _playback_resolution(app, zoom_key, plot_metadata, buffered)
    selected_points = chart_tools.interval(full, state, name) if interactive else []
    rows = _highlight_interval(rows, width, height, selected_points, times, g.ascii, plot_metadata["plot_rect"])
    # Summary and exact inspection always describe the original measurements,
    # including values undefined on a logarithmic axis.
    if rows:
        plot_label = charts.chart_model_title(clean(label + (f" [{kind}]" if kind else ""), g.ascii), plot_metadata)
        rows[0] = charts._header(g, values, width, plot_label, unit, "   ")
        if buffered and plot_metadata.get("has_data") and not known:
            rows[0] = L.clip_row([("   " + plot_label, "cyan+bold"),
                                  ("  between acquired samples", "dim")], width)
        if precision is not None and known:
            summary = f" {plot_label}  last {chart_tools.format_value(values[-1], preference)}  mean {chart_tools.format_value(charts._mean(known), preference)}  max {chart_tools.format_value(max(known), preference)}"
            rows[0] = L.clip_row([(clean(summary, g.ascii), "cyan+bold")], width)
    if interactive and visible and rows_selected(app, "chart"):
        state["cursor"] = max(0, min(len(visible) - 1, int(state.get("cursor", 0))))
        selected = visible[state["cursor"]]
        state["cursor_t"] = selected["t"]
        rows = _crosshair(rows, width, height, visible, selected, times, g.ascii, plot_metadata["plot_rect"])
        step = f"  step {selected['step']}" if selected.get("step") is not None else ""
        exact = repr(selected["value"]) if selected["value"] is not None else "unavailable"
        rows.append([(clean(f" {_time(selected['t'])}  t={selected['t']!r}  value {exact}{step}", g.ascii), "yellow+bold")])
    description = f" Axis {axis['mode']}"
    if name in state.get("metric_modes", {}):
        description += " | display " + state["metric_modes"][name]
    if kind:
        description += f" | kind {kind}"
    coalesced = metric_meta.get("coalesced", 0)
    if coalesced:
        description += f" | coalesced {'x' if g.ascii else '×'}{coalesced}"
    if model_kind == "drift" and baseline is not None:
        description += f" | baseline {baseline:g} ({'producer first value' if declared_baseline else 'first retained'}); no tolerance declared"
    if metric_meta.get("estimate"):
        description += " | " + metric_meta["estimate"]
    layers = [plot_metadata[field] for field in ("trend_label", "band_label") if plot_metadata.get(field)]
    if layers:
        description += " | Draw: " + "; ".join(layers)
    if live_window:
        from .metric_live import format_delta
        description += f" | Live {format_delta(live_window[1] - live_window[0])}; no gap filling"
    if box:
        description += f" | zoomed Y [{axis_label(low)}, {axis_label(high)}]" + (" fits observed interval" if auto_fit else "")
    if axis.get("low") is not None:
        description += f" [{axis['low']!r}, {axis['high']!r}]"
        clipped = sum(value is not None and (value < axis["low"] or value > axis["high"]) for value in values)
        description += f" | {clipped} outside bounds"
    if axis["mode"] == "log":
        description += f" | {undefined} nonpositive samples undefined"
    description += f" | ID {name}" + (f" | declared unit {unit}" if unit else " | unit not declared")
    rows.append([(clean(description, g.ascii), "dim")])
    if selected_points:
        stats = chart_tools.statistics_for(selected_points, full)
        formatting = lambda value: chart_tools.format_value(value, preference)
        rows.append([(clean(f" Range t={selected_points[0]['t']!r} to {selected_points[-1]['t']!r} | {stats['count']}/{stats['samples']} known samples", g.ascii), "yellow+bold")])
        rows.append([(clean(f" Min {formatting(stats['minimum'])} | median {formatting(stats['median'])} | mean {formatting(stats['mean'])} | max {formatting(stats['maximum'])}", g.ascii), "cyan")])
        rows.append([(clean(f" P05 {formatting(stats['p05'])} | P95 {formatting(stats['p95'])} | P99 {formatting(stats['p99'])}", g.ascii), "cyan")])
        coverage = f"{stats['time_coverage']:.1%}" if stats['time_coverage'] is not None else "unavailable"
        rows.append([(clean(f" Coverage: samples {stats['sample_coverage']:.1%} | observed time {coverage} | missing {stats['missing']}; no gap filling", g.ascii), "dim")])
    gaps = sum(1 for p in visible if p["value"] is None)
    outages = sum(b["t"] - a["t"] > cadence * 2.5 for a, b in zip(visible, visible[1:])) if cadence else 0
    metadata = (acquired[-1]["t"] if acquired else None, len(visible), len(full), gaps + outages)
    if live_now is not None:
        metadata += (cadence, buffered, times[1] if automatic_history else None)
    rows.append(_source_row(g, source, metadata, app=app, identity=zoom_key))
    if cache_metadata is not None:
        cache_metadata["source"] = metadata
        cache_metadata["following"] = buffered
        cache_metadata["display_end"] = times[1] if automatic_history else None
    if interactive and state.get("chart_events") and getattr(app, "store", None):
        events = chart_events(app, snapshot if snapshot is not None else app.store.snapshot(), times)
        state["chart_event_items"] = events
        if events:
            rows.append(event_markers(g, events, times, width))
    return rows


def chart_events(app, snap, times=None):
    """Actual observed events for the pinned chart job, with original citations."""
    jid = initialize(app).get("chart_job") or _analysis_jid(app)
    events = [event for event in timeline_events(app, snap)
              if event.get("kind") not in ("resource", "recorded")
              and (not event.get("job") or event.get("job") == str(jid))]
    numbered = [dict(event, number=i + 1) for i, event in enumerate(events[-MAX_EVENTS:])]
    return [event for event in numbered if times is None or times[0] <= event["t"] <= times[1]]


def event_markers(g, events, times, width):
    """Timestamp aligned marker strip; collisions retain all entries in picker."""
    plot_width = max(0, min(charts.MAX_COLUMNS, width - 10))
    cells = [" "] * plot_width
    raster = 1 if g.ascii else 2
    for i, event in enumerate(events):
        x = round(charts._fraction(event["t"], *times) * (plot_width * raster - 1)) // raster if times[1] > times[0] else plot_width - 1
        if 0 <= x < plot_width:
            cells[x] = ("|" if g.ascii else "│") if cells[x] == " " else ("*" if g.ascii else "◆")
    return L.clip_row([(" Events e ", "yellow+bold"), ("".join(cells), "yellow+bold")], width)


def open_inspector(app, jid=None):
    state = initialize(app)
    jid = jid or getattr(app, "selected_id", None)
    record = app.job_record(jid) if jid and hasattr(app, "job_record") else None
    if record is None:
        app.fail("Select an active, recent, or historical job to inspect.")
        return False
    if getattr(app, "mode", "main") == "analysis" and state.get("modal") != "inspect":
        history = list(state.get("modal_back", []))
        history.append({key: state[key] for key in ("modal", "job", "scroll", "cursor", "section", "metric", "chart_job", "zoom", "pan", "window", "preset", "chart_range", "sample_cursor") if key in state})
        state["modal_back"] = history[-8:]
    else:
        state["modal_back"] = []
    state.update(modal="inspect", job=jid, section=0, scroll=0)
    app.detail_id, app.mode = jid, "analysis"
    if getattr(app, "sampler", None):
        app.sampler.select(jid)
        if not app.store.job(jid):
            app.sampler.select_fin(jid)
    return True


def timeline_events(app, snap):
    """A bounded observation timeline; missing historical phases stay unknown."""
    items = []
    for event in (snap.get("events", [])[-MAX_EVENTS:] + snap.get("job_transitions", [])[-MAX_EVENTS:] + initialize(app).get("metric_events", [])):
        if not isinstance(event, dict) or not _finite(event.get("t")):
            continue
        items.append({"t": event["t"], "kind": event.get("kind", "event"),
                      "text": event.get("text") or f"{event.get('name', '')} {event.get('state', '')}",
                      "job": str(event.get("job") or ""), "path": event.get("path", ""),
                      "source": event.get("source", ""),
                      "line": event.get("line"), "line_basis": event.get("line_basis", "original")})
    for citation in list(getattr(app, "research_evidence", {}).values())[:128]:
        if _finite(citation.get("t")):
            items.append(dict(citation, kind="log", text=citation.get("text", "")))
    replay = getattr(app, "replay", None)
    for record in getattr(replay, "entries", [])[-128:]:
        if isinstance(record, dict) and _finite(record.get("t")):
            command = record.get("cmd", [])
            if not isinstance(command, (list, tuple)):
                continue
            items.append({"t": record["t"], "kind": "recorded", "job": "",
                          "text": "Recorded scheduler observation: " + " ".join(map(str, command)) +
                                  (" / " + str(record["err"]) if record.get("err") else "")})
    jid = _analysis_jid(app)
    samples = memory_series(app, jid)
    # Resource observations are sampled rather than duplicating every frame.
    stride = max(1, (len(samples) + 95) // 96)
    for sample in samples[::stride]:
        if _finite(sample.get("t")):
            if sample.get("k") == "live":
                text = "Resource sample: CPU " + _fmt(sample.get("cpu", sample.get("eff"))) + " per core; RSS " + (human(sample["rss"]) if _finite(sample.get("rss")) else "unavailable")
            else:
                text = "GPU resource sample"
            items.append({"t": sample["t"], "kind": "resource", "text": text, "job": jid})
    unique = {}
    for item in items:
        unique[(item["t"], item.get("kind"), item.get("job"), item.get("text"))] = item
    return sorted(unique.values(), key=lambda item: item["t"])[-MAX_EVENTS:]


def _chart_control(app, args, series):
    """Validate chart controls before mutating preferences or opening a view."""
    if not args or args[0] not in ("preset", "axis", "mode", "rank", "rank-page", "range", "events", "event", "shared", "undo", "reset"):
        return False
    state = initialize(app)
    action, parameters = args[0], args[1:]
    name = state.get("metric") if state.get("metric") in series else next(iter(series), "")
    try:
        if action == "rank-page":
            if parameters not in (["previous"], ["next"]):
                raise ValueError("chart rank-page previous|next")
            count = len(state.get("rank_streams", []))
            state["rank_page"] = max(0, min(max(0, (count - 1) // 8),
                state.get("rank_page", 0) + (1 if parameters == ["next"] else -1)))
            return True
        if action == "rank":
            hub = getattr(app, "research", None)
            if len(parameters) != 1 or hub is None:
                raise ValueError("chart rank default|REGISTERED_STREAM")
            selected = parameters[0]
            if selected != "default" and (not state.get("metric_source") or selected not in [entry["path"] for entry in state.get("rank_streams", [])]):
                raise ValueError("Choose a registered rank stream from this run")
            hub.configure(metric_stream={} if selected == "default" else {"source": state["metric_source"], "name": selected})
            app.analysis_result = {}
            state.pop("chart_frame", None)
            app.say("Reading " + ("the default metric stream" if selected == "default" else selected) + "; ranks are displayed separately.")
            return True
        if action in ("undo", "reset"):
            if parameters:
                raise ValueError("chart undo|reset")
            (chart_interaction.undo if action == "undo" else chart_interaction.reset)(app, state.get("chart_interaction_key"))
        elif action == "preset":
            if len(parameters) != 1 or parameters[0] not in dict(chart_tools.PRESETS):
                raise ValueError("chart preset 5m|30m|2h|all")
            chart_interaction.reset(app, state.get("chart_interaction_key"))
            window = dict(chart_tools.PRESETS)[parameters[0]]
            state.update(preset=parameters[0], cursor=0, zoom=1.0, pan=1.0 if window else 0.0)
            if window:
                state["window"] = window
            else:
                state.pop("window", None)
        elif action == "mode":
            if len(parameters) != 1 or parameters[0] not in ("default", "raw", "trend", "range") or not name:
                raise ValueError("chart mode default|raw|trend|range")
            if parameters[0] == "default":
                state["metric_modes"].pop(name, None)
            else:
                state["metric_modes"][name] = parameters[0]
                state["metric_modes"] = dict(list(state["metric_modes"].items())[-MAX_METRICS:])
            app.say(f"{name}: {parameters[0]} display; original observations are unchanged.")
        elif action == "axis":
            axis = {"mode": parameters[0]} if parameters else {}
            if len(parameters) == 3:
                axis.update(low=float(parameters[1]), high=float(parameters[2]))
            if len(parameters) not in (1, 3) or not name or not chart_tools.valid_axis(axis) or axis.get("mode") == "auto" and len(parameters) != 1:
                raise ValueError("chart axis auto|fixed LOW HIGH|log [POSITIVE_LOW POSITIVE_HIGH]")
            chart_interaction.reset(app, state.get("chart_interaction_key"))
            state["axes"][name] = axis
            state["axes"] = dict(list(state["axes"].items())[-MAX_METRICS:])
            app.say(f"{name}: {axis['mode']} axis.")
        elif action == "range":
            if parameters == ["clear"]:
                state.pop("chart_range", None)
            else:
                points, _ = viewport(series.get(name, []), state)
                if len(parameters) != 2:
                    raise ValueError("chart range FIRST LAST|clear (visible sample numbers, starting at 1)")
                first, last = [int(value) - 1 for value in parameters]
                if not 0 <= first < len(points) or not 0 <= last < len(points):
                    raise ValueError("The selected range must use visible sample numbers, starting at 1.")
                state["chart_range"] = {"metric": name, "start": points[first]["t"], "end": points[last]["t"]}
        elif action == "shared":
            if parameters not in (["on"], ["off"]):
                raise ValueError("chart shared on|off")
            state["shared_scale"] = parameters == ["on"]
            app.say("Comparison axes: " + ("shared." if state["shared_scale"] else "independent."))
            app.save()
            return True
        elif action == "events":
            if parameters:
                if parameters not in (["on"], ["off"]):
                    raise ValueError("chart events [on|off]")
                state["chart_events"] = parameters == ["on"]
            else:
                resume_rows(app, "chart_events")
                state.update(modal="chart_events", cursor=0, scroll=0)
                app.mode = "analysis"
                return True
        elif action == "event":
            events = chart_events(app, app.store.snapshot())
            index = int(parameters[0]) - 1 if len(parameters) == 1 else -1
            if not 0 <= index < len(events):
                raise ValueError("chart event NUMBER (the 1-based number in the chart event picker)")
            _open_timeline_event(app, events[index])
            return True
    except (ValueError, OverflowError, TypeError) as exc:
        app.fail(str(exc) if str(exc) else "Invalid chart control.")
        return True
    state.update(modal="chart", scroll=0, metric=name)
    if action in ("axis", "mode", "events"):
        app.save()
    app.mode = "analysis"
    return True


def _open_timeline_event(app, event, *, seek=False):
    replay = getattr(app, "replay", None)
    if seek:
        if replay is None:
            app.fail("Timeline seeking is available in recorded sessions.")
            return
        replay.clock.seek(event["t"])
        if getattr(app, "sampler", None):
            app.sampler.refresh_all()
        app.say("Replay moved to " + _time(event["t"]))
        return
    if event.get("path"):
        from .job_logs import workbench as log_workbench
        from .navigation_ui import record
        state = initialize(app)
        previous = state.get("modal", "timeline")
        record(app, "log", force=True)
        app.mode, state["modal"] = "main", ""
        if not log_workbench.open_citation(app, event):
            app.mode, state["modal"] = "analysis", previous
    elif event.get("job"):
        open_inspector(app, event["job"])
    else:
        app.say("This event has no job or file evidence attached.")


def run_command(app, args):
    if not args or args[0] not in command_names():
        return False
    state = initialize(app)
    if getattr(app, "mode", "main") == "main":
        state["modal_back"] = []
    cmd, args = args[0], args[1:]
    if cmd == "inspect" and args[:1] == ["section"]:
        names = {name.casefold(): index for index, name in enumerate(SECTIONS)}
        if len(args) == 2 and args[1].casefold() in names and state.get("modal") == "inspect":
            state.update(section=names[args[1].casefold()], scroll=0)
        else:
            app.fail("Open Inspector, then use inspect section Overview|Resources|Steps|Files|Evidence")
        return True
    if cmd == "metricdisplay":
        try:
            if len(args) < 2 or not chart_tools.text(args[0]):
                raise ValueError
            name, action, values = args[0], args[1], args[2:]
            preference = dict(state["metric_display"].get(name, {}))
            if action == "reset" and not values:
                state["metric_display"].pop(name, None)
            elif action in ("label", "unit") and values and chart_tools.text(" ".join(values)):
                preference[action] = " ".join(values)
                state["metric_display"][name] = preference
            elif action == "precision" and len(values) == 1 and 0 <= int(values[0]) <= 12:
                preference[action] = int(values[0])
                state["metric_display"][name] = preference
            else:
                raise ValueError
            state["metric_display"] = dict(list(state["metric_display"].items())[-MAX_METRICS:])
            app.save()
            app.say(f"Display preference saved for {name}; original samples and ID are unchanged.")
        except (ValueError, TypeError, OverflowError):
            app.fail("metricdisplay METRIC label TEXT|unit DECLARED_UNIT|precision 0..12|reset")
        return True
    if cmd == "inspect":
        if len(args) > 1:
            app.fail("inspect [JOBID]")
        else:
            open_inspector(app, args[0] if args else None)
        return True
    if cmd == "dashboard":
        action = args[0] if args else "list"
        name = args[1] if len(args) > 1 else ""
        if action == "list" and len(args) == 1 or not args:
            state.update(modal="dashboard", scroll=0)
            app.mode = "analysis"
        elif action == "reset" and len(args) == 1:
            for key in ("pinned", "hidden", "order", "expanded"):
                state[key] = []
            state["colors"] = {}
            state["metric_filter"] = ""
            app.say("Dashboard arrangement reset.")
        elif action == "search" and len(args) <= 2:
            state["metric_filter"] = name[:96]
            app.say("Dashboard search: " + (name or "cleared"))
        elif action in ("pin", "unpin", "hide", "show", "expand", "collapse") and len(args) == 2 and 0 < len(name) <= 96 and name.isprintable():
            field = {"pin": "pinned", "unpin": "pinned", "hide": "hidden", "show": "hidden", "expand": "expanded", "collapse": "expanded"}[action]
            state[field] = [v for v in state[field] if v != name]
            if action in ("pin", "hide", "expand"):
                state[field] = (state[field] + [name])[-MAX_METRICS:]
            app.say(f"Dashboard {action}: {name}")
        elif action == "move" and len(args) == 3:
            try:
                position = int(args[2])
                if not 1 <= position <= MAX_METRICS:
                    raise ValueError
                available, _, _ = chart_data(app, app.store.snapshot())
                order = list(dict.fromkeys(state["order"] + list(available)))[:MAX_METRICS]
                if name not in available and name not in order:
                    raise ValueError
                order = [v for v in order if v != name]
                order.insert(min(position - 1, len(order)), name)
                state["order"] = order
                app.say(f"Dashboard moved {name} to {position}.")
            except (ValueError, TypeError):
                app.fail("dashboard move METRIC POSITION (1..64); use a loaded metric")
        elif action == "color" and len(args) == 3 and args[2] in COLORS and 0 < len(name) <= 96 and name.isprintable():
            state["colors"][name] = args[2]
            state["colors"] = dict(list(state["colors"].items())[-MAX_METRICS:])
            app.say(f"Dashboard {name}: {args[2]}")
        else:
            app.fail("dashboard [list|reset|search TEXT|pin|unpin|hide|show|expand|collapse METRIC|move METRIC POSITION|color METRIC COLOR]")
        return True
    if cmd == "chart":
        if state.get("modal") not in ("chart", "chart_events"):
            state["chart_job"] = _analysis_jid(app)
        series, _, _ = chart_data(app, app.store.snapshot())
        if _chart_control(app, args, series):
            return True
        if args and args[0] in ("zoom", "pan", "cursor", "window"):
            try:
                value = float(args[1]) if len(args) == 2 else float("nan")
                if not math.isfinite(value):
                    raise ValueError
                if args[0] != "cursor":
                    chart_interaction.reset(app, state.get("chart_interaction_key"))
                if args[0] == "zoom" and 1 <= value <= 1024:
                    state["zoom"] = value
                    state.pop("window", None)
                    state.pop("preset", None)
                elif args[0] == "pan" and 0 <= value <= 1:
                    state["pan"] = value
                elif args[0] == "cursor" and value >= 1 and value == int(value):
                    resume_rows(app, "chart")
                    state["cursor"] = min(MAX_POINTS - 1, int(value) - 1)
                elif args[0] == "window" and 0 < value <= 365 * 86400:
                    state.update(window=value, pan=1.0, cursor=0)
                    state.pop("preset", None)
                else:
                    raise ValueError
            except (ValueError, IndexError, OverflowError):
                app.fail("chart zoom 1..1024 | pan 0..1 | cursor SAMPLE (1-based) | window SECONDS (up to 365 days)")
                return True
        elif args:
            if len(args) != 1 or args[0] not in series:
                app.fail("chart METRIC (load metrics in Experiment first)")
                return True
            state.update(metric=args[0], cursor=0, zoom=1.0, pan=0.0)
            resume_rows(app, "chart")
            state.pop("window", None)
            state["preset"] = "all"
        state.update(modal="chart", scroll=0)
        app.mode = "analysis"
        return True
    if cmd == "timeline":
        if args and args[0] not in ("events", "seek", "open"):
            app.fail("timeline [events|open EVENT_NUMBER|seek EVENT_NUMBER]")
            return True
        events = timeline_events(app, app.store.snapshot())
        if args and args[0] in ("seek", "open"):
            try:
                index = int(args[1]) - 1 if len(args) == 2 else -1
                if not 0 <= index < len(events):
                    raise ValueError
                _open_timeline_event(app, events[index], seek=args[0] == "seek")
            except ValueError:
                app.fail("timeline " + args[0] + " EVENT_NUMBER (shown 1-based in Timeline)")
            return True
        if len(args) > 1:
            app.fail("timeline [events|open EVENT_NUMBER|seek EVENT_NUMBER]")
            return True
        resume_rows(app, "timeline")
        state.update(modal="timeline", cursor=max(0, len(events) - 1), scroll=0)
        app.mode = "analysis"
        return True
    if cmd == "diff":
        if args and args[0] == "passport":
            hub = getattr(app, "research", None)
            if len(args) != 3 or hub is None:
                app.fail("diff passport LEFT RIGHT")
                return True
            if getattr(hub.files, "remote", False):
                app.fail("Passport comparison needs Raven running on the cluster.")
                return True
            paths = args[1:]
            def work():
                from .provenance import load, diff
                left, right = load(paths[0]), load(paths[1])
                return {"left": left, "right": right, "differences": diff(left, right)}
            def completed(result):
                if isinstance(result, Exception):
                    app.fail("Passport comparison: " + clean(result))
                    return
                state.update(modal="diff", diff_kind="passport", comparison=result, scroll=0, unchanged=False)
                app.mode = "analysis"
                app.say("Passport comparison ready.")
            if hub.start_task(work, completed):
                app.say("Comparing passports in the background…")
            else:
                app.fail("Research worker is busy; try the comparison again when it completes.")
        else:
            ids = args or list(getattr(app, "compare_ids", [])) or sorted(getattr(app, "marks", []))
            ids = list(dict.fromkeys(ids))
            if not 2 <= len(ids) <= 6 or any(app.job_record(jid) is None for jid in ids):
                app.fail("diff JOBID JOBID [up to six jobs] or mark two jobs first")
            else:
                state.update(modal="diff", diff_kind="jobs", diff_ids=ids, scroll=0, unchanged=False)
                app.mode = "analysis"
        return True
    return False


def handle_key(app, key):
    state = initialize(app)
    action = getattr(app, "keymap", {}).get(key)
    if action in ("up", "down", "home", "end", "page_up", "page_down"):
        key = {"page_up": "pgup", "page_down": "pgdn"}.get(action, action)
    if getattr(app, "mode", "main") != "analysis":
        if getattr(app, "mode", "main") == "main" and getattr(app, "keymap", {}).get(key) == "inspector":
            open_inspector(app)
            return True
        if (getattr(app, "mode", "main") == "main" and getattr(app, "tab", "") == "research"
                and getattr(app, "research_view", "") == "experiment" and key in ("up", "down")):
            from .ui.research_binding import select_job
            snap = app.store.snapshot()
            ids = list(dict.fromkeys(job.id for job in snap.get("jobs", []) + snap.get("finished", [])))
            if ids:
                current = ids.index(app.research_job_id) if app.research_job_id in ids else 0
                select_job(app, ids[max(0, min(len(ids) - 1, current + (-1 if key == "up" else 1)))])
            return True
        if getattr(app, "mode", "main") == "main" and getattr(app, "tab", "") == "research" and getattr(app, "research_view", "") == "evidence":
            ids = state.get("evidence_ids", [])
            if key in ("up", "down", "home", "end") and ids:
                resume_rows(app, "evidence")
                current = state.get("evidence_cursor", 0)
                state["evidence_cursor"] = 0 if key == "home" else len(ids) - 1 if key == "end" else max(0, min(len(ids) - 1, current + (-1 if key == "up" else 1)))
                state["evidence_focus"] = True
                return True
            if key == "enter" and ids:
                if not rows_selected(app, "evidence"):
                    return True
                from .job_logs import workbench as log_workbench
                citation = getattr(app, "research_evidence", {}).get(ids[min(state.get("evidence_cursor", 0), len(ids) - 1)])
                if citation and citation.get("path"):
                    log_workbench.open_citation(app, citation)
                else:
                    app.say("This citation is a scheduler observation with no log file attached.")
                return True
        return False
    if key in ("esc", "q", "ctrl-b", "alt-left"):
        if state.get("modal") == "chart_events":
            state.update(modal="chart", scroll=0, cursor=state.get("sample_cursor", 0))
            return True
        history = state.get("modal_back", [])
        if history:
            state.update(history[-1])
            state["modal_back"] = history[:-1]
            app.mode = "analysis"
        else:
            app.mode, state["modal"] = "main", ""
        return True
    modal = state.get("modal")
    if key in ("up", "down", "pgup", "pgdn", "home", "end"):
        S.resume(app, "analysis:document")
    if modal == "chart":
        series = _chart_frame_series(app)
        names = list(series)
        name = state.get("metric") if state.get("metric") in names else names[0] if names else ""
        points = _chart_visible_points(app, series, name)
        if key in ("left", "right", "up", "down", "home", "end"):
            resume_rows(app, "chart")
            change = -1 if key in ("left", "up") else 1
            state["cursor"] = 0 if key == "home" else max(0, len(points) - 1) if key == "end" else max(0, min(max(0, len(points) - 1), state.get("cursor", 0) + change))
        elif key in ("+", "=", "-", "_"):
            chart_interaction.reset(app, state.get("chart_interaction_key"))
            state.pop("window", None)
            state.pop("preset", None)
            state["zoom"] = max(1.0, min(1024.0, state.get("zoom", 1.0) * (2 if key in ("+", "=") else .5)))
            state["cursor"] = 0
        elif key in ("[", "]"):
            chart_interaction.reset(app, state.get("chart_interaction_key"))
            state["pan"] = max(0.0, min(1.0, state.get("pan", 0.0) + (-.1 if key == "[" else .1)))
            state["cursor"] = 0
        elif key in ("pgup", "pgdn"):
            page = max(1, state.get("chart_page", 10))
            state["scroll"] = max(0, state.get("scroll", 0) + (-page if key == "pgup" else page))
        elif key in ("tab", "btab") and names:
            index = names.index(name)
            next_name = names[(index + (1 if key == "tab" else -1)) % len(names)]
            next_points = _chart_visible_points(app, series, next_name)
            timestamp = points[min(state.get("cursor", 0), len(points) - 1)]["t"] if points else None
            closest = min(range(len(next_points)), key=lambda i: abs(next_points[i]["t"] - timestamp)) if next_points and timestamp is not None else 0
            state.update(metric=next_name, cursor=closest)
        elif key == "t":
            presets = [item[0] for item in chart_tools.PRESETS]
            current = state.get("preset", "all")
            if current not in presets:
                current = "all"
            _chart_control(app, ["preset", presets[(presets.index(current) + 1) % len(presets)]], series)
        elif key in ("a", "g"):
            _chart_control(app, ["axis", "auto" if key == "a" else "log"], series)
        elif key == "r" and points:
            if not rows_selected(app, "chart"):
                app.say("Select a sample with the arrows before selecting a range")
                return True
            selected = points[min(state.get("cursor", 0), len(points) - 1)]
            existing = state.get("chart_range", {})
            if existing.get("selecting") and existing.get("metric") == name:
                existing.update(end=selected["t"], selecting=False)
            else:
                state["chart_range"] = {"metric": name, "start": selected["t"], "end": selected["t"], "selecting": True}
        elif key == "e":
            state["sample_cursor"] = state.get("cursor", 0)
            _chart_control(app, ["events"], series)
        elif key == "s":
            _chart_control(app, ["shared", "off" if state["shared_scale"] else "on"], series)
        selected_range = state.get("chart_range", {})
        if selected_range.get("selecting") and selected_range.get("metric") == name and points:
            selected_range["end"] = points[min(state.get("cursor", 0), len(points) - 1)]["t"]
    elif modal == "inspect" and key in ("tab", "btab", "left", "right"):
        state["section"] = (state.get("section", 0) + (1 if key in ("tab", "right") else -1)) % len(SECTIONS)
        state["scroll"] = 0
    elif modal == "inspect" and key in ("l", "enter"):
        from .navigation_ui import record
        record(app, "log", force=True)
        app.mode, state["modal"] = "main", ""
        app.open_log(state.get("job"))
    elif modal == "inspect" and key == "e":
        from .ui.research_binding import select_job
        from .navigation_ui import record
        record(app, "research", force=True)
        app.mode, state["modal"] = "main", ""
        app.enter_tab("research")
        select_job(app, state.get("job"), view="evidence", record_back=False)
    elif modal == "timeline":
        if key in ("enter", "s") and not rows_selected(app, "timeline"):
            return True
        events = timeline_events(app, app.store.snapshot())
        if key in ("up", "down", "home", "end", "pgup", "pgdn"):
            if events:
                resume_rows(app, "timeline")
            delta = -1 if key == "up" else 1 if key == "down" else -10 if key == "pgup" else 10
            state["cursor"] = 0 if key == "home" else max(0, len(events) - 1) if key == "end" else max(0, min(max(0, len(events) - 1), state.get("cursor", 0) + delta))
        elif key in ("enter", "s") and events:
            _open_timeline_event(app, events[min(state.get("cursor", 0), len(events) - 1)], seek=key == "s")
    elif modal == "chart_events":
        if key == "enter" and not rows_selected(app, "chart_events"):
            return True
        events = chart_events(app, app.store.snapshot())
        if key in ("up", "down", "home", "end", "pgup", "pgdn"):
            if events:
                resume_rows(app, "chart_events")
            delta = -1 if key == "up" else 1 if key == "down" else -10 if key == "pgup" else 10
            state["cursor"] = 0 if key == "home" else max(0, len(events) - 1) if key == "end" else max(0, min(max(0, len(events) - 1), state.get("cursor", 0) + delta))
        elif key == "enter" and events:
            _open_timeline_event(app, events[min(state.get("cursor", 0), len(events) - 1)])
    elif modal == "diff" and key == "s":
        state["shared_scale"] = not state["shared_scale"]
        app.say("Comparison axes: " + ("shared." if state["shared_scale"] else "independent."))
        app.save()
    elif modal == "diff" and key == "u":
        state["unchanged"] = not state.get("unchanged", False)
        state["scroll"] = 0
    if modal not in ("chart", "timeline", "chart_events"):
        if key in ("up", "down", "pgup", "pgdn", "home", "end"):
            delta = -1 if key == "up" else 1 if key == "down" else -10 if key == "pgup" else 10
            state["scroll"] = 0 if key == "home" else max(0, state.get("row_count", 0) - 1) if key == "end" else max(0, state.get("scroll", 0) + delta)
    return True


def _inspector_rows(g, snap, app, width):
    state = initialize(app)
    jid = state.get("job")
    job = app.job_record(jid, snap)
    from .control_rows import buttons
    rows, controls = buttons(g, width, [(name, name, ("command", "inspect section " + name)) for name in SECTIONS],
                              selected=SECTIONS[state.get("section", 0)], group="inspector_sections", prefix="inspect-section:")
    state["inspector_controls"], state["inspector_nav_rows"] = controls, len(rows)
    if job is None:
        return rows + [[(" This job is outside the current inventory. Its exact ID remains selected.", "yellow")]]
    row = lambda text, style="": [(clean(text, g.ascii), style)]
    rows.extend([row(f" Job {jid}  {job.name}", "cyan+bold"), row(f" {job.state}  | partition {job.partition or 'unavailable'}  | elapsed {job.elapsed or 'unavailable'}", "bold")])
    details = snap.get("details", {}).get(jid, {})
    section = SECTIONS[state.get("section", 0)]
    if section == "Overview":
        for label, value in (("Submitted", getattr(job, "submit", "")), ("Started", getattr(job, "start", "")), ("Ended / expected", getattr(job, "end", "")),
                             ("Time limit", getattr(job, "limit", "")), ("Exit", getattr(job, "exit", "")), ("Reason", getattr(job, "reason", "")),
                             ("Workdir", details.get("WorkDir") or getattr(job, "workdir", "")), ("Node list", getattr(job, "nodelist", ""))):
            rows.append(row(f" {label:18} {_fmt(value) if value not in ('', None) else 'unavailable'}"))
    elif section == "Resources":
        live = snap.get("live", {}).get(jid)
        rss = getattr(live, "rss", None) if live else getattr(job, "rss", None)
        if not live and not rss:
            # Finished.rss uses zero when accounting did not report MaxRSS.
            rss = None
        cpu = getattr(live, "rate", None) if live else getattr(job, "cpu_eff", None)
        cpu_basis = "interval rate" if live else "completed efficiency"
        if live and cpu is None:
            cpu = live.avg
            cpu_basis = "cumulative efficiency"
        requested = getattr(job, "mem_bytes", None) if hasattr(job, "mem_bytes") else getattr(job, "req_mem", None)
        rows.extend([row(f" Allocation  {job.cpus} CPUs  | {job.nodes} nodes  | {job.gpus} GPUs", "bold"),
                     row(" Requested memory  " + (human(requested) if _finite(requested) and requested > 0 else "unavailable")),
                     row(" Observed peak RSS " + (human(rss) if _finite(rss) else "unavailable")),
                     row(" CPU per core      " + (_fmt(cpu * 100) + "% (" + cpu_basis + ")" if _finite(cpu) else "unavailable"))])
        if _finite(cpu):
            rows.append(row(" CPU occupancy" if cpu_basis == "interval rate" else " CPU efficiency", "dim"))
            rows.append(L.gradient_bar(g, cpu, max(1, min(40, width - 8))))
        rows.append(row(" Live and accounting measurements retain their original source; missing samples stay unknown.", "dim"))
    elif section == "Steps":
        steps = snap.get("steps", {}).get(jid) or snap.get("fin_steps", {}).get(jid) or []
        if not steps:
            rows.append(row(" No step records published yet.", "dim"))
        for step in steps[:256]:
            rows.append(row(f" {step.id}  {step.name}  {step.state or 'live'}  elapsed {step.elapsed or 'unavailable'}", "bold"))
            rows.append(row(f"   CPU {short_duration(step.cpu_time)}  RSS {human(step.rss) if step.rss else 'unavailable'}  tasks {step.ntasks}  exit {step.exit or 'unavailable'}", "dim"))
    elif section == "Files":
        for field in ("WorkDir", "Command", "StdOut", "StdErr"):
            rows.append(row(f" {field:10} {details.get(field) or 'unavailable'}"))
        entries = getattr(getattr(app, "logs", None), "entries", []) if getattr(app, "log_job", None) == jid else []
        for entry in entries[:128]:
            rows.append(row(f" {entry.get('group', 'Logs')} / {entry.get('label', '')}: {entry.get('path', '')}", "cyan"))
        rows.append(row(" l / Enter opens this job's complete log browser.", "cyan"))
    else:
        citations = getattr(app, "research_evidence", {}) if state.get("evidence_job") == jid else {}
        for citation in list(citations.values())[:128]:
            rows.append(row(f" [{citation.get('id', '')}] {citation.get('source', '')}: {citation.get('text', '')}", "yellow"))
        if not citations:
            rows.append(row(" Open Evidence to collect bounded, cited observations for this job.", "dim"))
        rows.append(row(" e opens Evidence for this exact job.", "cyan"))
    return rows


def passport_rows(g, passport, differences=None, *, unchanged=False):
    row = lambda text, style="": [(clean(text, g.ascii), style)]
    rows = []
    if passport:
        rows.append(row(" Immutable run passport " + str(passport.get("id", "?")), "cyan+bold"))
        for key in ("captured_at", "workdir", "job_id", "script", "git", "resources", "parameters", "inputs", "environment", "modules", "container", "host"):
            rows.append(row(" " + key.replace("_", " ").title(), "bold"))
            value = passport.get(key)
            if isinstance(value, dict):
                for name, field in list(value.items())[:64]:
                    rows.append(row(f"   {name}: {_fmt(field)}", "dim" if field is None else ""))
            else:
                rows.append(row("   " + _fmt(value), "dim" if value is None else ""))
    if differences is not None:
        groups = defaultdict(list)
        for change in differences[:1024]:
            groups[change["path"].split("/")[1] if "/" in change["path"] else "run"].append(change)
        rows.append(row(f" {len(differences)} substantive changes", "yellow" if differences else "green"))
        for name, changes in groups.items():
            rows.append(row(f" {name.title()}  ({len(changes)} changed)", "cyan+bold"))
            for change in changes:
                rows.append(row(" " + change["path"], "bold"))
                left = _fmt(change.get("left")) if change.get("left_present", True) else "absent"
                right = _fmt(change.get("right")) if change.get("right_present", True) else "absent"
                rows.append(row("   Before  " + left, "red"))
                rows.append(row("   After   " + right, "green"))
        if not differences:
            rows.append(row(" No substantive differences between the two passports.", "green"))
    return rows


def _job_diff_rows(g, snap, app, width):
    state = initialize(app)
    ids = state.get("diff_ids", [])[:6]
    jobs = [app.job_record(jid, snap) for jid in ids]
    row = lambda text, style="": [(clean(text, g.ascii), style)]
    rows = [row(" Comparing " + ", ".join(ids), "cyan+bold"), row(" u shows / hides unchanged fields; sample curves align on first observed sample.", "dim"),
            row(" Axis scales " + ("shared per metric" if state["shared_scale"] else "independent per job") + " | s toggles the scale lock.", "cyan")]
    fields = [("Name", lambda j: j.name), ("State", lambda j: j.state), ("Partition", lambda j: j.partition),
              ("CPUs", lambda j: j.cpus), ("GPUs", lambda j: j.gpus), ("Nodes", lambda j: j.nodes),
              ("Memory requested", lambda j: human(j.mem_bytes if hasattr(j, "mem_bytes") else j.req_mem) if (j.mem_bytes if hasattr(j, "mem_bytes") else j.req_mem) else None),
              ("Elapsed", lambda j: j.elapsed), ("Limit", lambda j: j.limit), ("Exit", lambda j: getattr(j, "exit", None))]
    for label, fn in fields:
        values = [fn(job) if job else None for job in jobs]
        changed = len({_fmt(v) for v in values}) > 1
        if changed or state.get("unchanged"):
            rows.append(row(" " + label + (" / changed" if changed else " / unchanged"), "yellow+bold" if changed else "dim"))
            for jid, value in zip(ids, values):
                rows.append(row(f"   {jid}: {_fmt(value)}", "" if changed else "dim"))
    all_series = {jid: _resource_series(app, jid) for jid in ids}
    for metric in ("CPU per core (%)", "Memory (GB)", "GPU utilization (%)"):
        pointsets = {jid: _points(series.get(metric, [])) for jid, series in all_series.items()}
        pointsets = {jid: points for jid, points in pointsets.items() if points}
        if not pointsets:
            continue
        label, unit, _ = chart_tools.display(state, metric)
        preference = chart_tools.preference(state, metric)
        axis = state["axes"].get(metric, {"mode": "auto"})
        rows.append(row(" " + label + " / aligned measured curves / ID " + metric, "cyan+bold"))
        finite = [p["value"] for points in pointsets.values() for p in points if p["value"] is not None]
        _, shared_lo, shared_hi, _ = chart_tools.axis_values(finite, axis)
        if shared_hi is None:
            shared_hi = max(finite, default=1.0)
        span = max((points[-1]["t"] - points[0]["t"] for points in pointsets.values()), default=1.0)
        if not math.isfinite(span):
            span = 1.0
        for i, jid in enumerate(ids):
            points = pointsets.get(jid)
            if not points:
                rows.append(row(f" {jid}: no measured {metric} samples", "dim"))
                continue
            t0 = points[0]["t"]
            identity = chart_interaction.key(app, metric, "Raven session resource samples", jid,
                                              scope="comparison-elapsed", attempt=t0)
            scale = "log" if axis.get("mode") == "log" else "linear"
            box = chart_interaction.bounds(app, identity, scale=scale)
            captured = chart_interaction.captured_bounds(app, identity, scale=scale)
            elapsed_points = [dict(point, t=point["t"] - t0) for point in points]
            curve_times = captured["x"] if captured else box["x"] if box else (0, span)
            curve_points = [p for p in elapsed_points if curve_times[0] <= p["t"] <= curve_times[1]]
            values = [p["value"] for p in curve_points]
            visible_plotted, lo, hi, undefined = chart_tools.axis_values(values, axis)
            raster_points = raster_viewport(elapsed_points, curve_times)
            if len(raster_points) > len(curve_points):
                lo, hi = charts._bounds(visible_plotted, lo, hi)
            plotted, _, _, _ = chart_tools.axis_values([point["value"] for point in raster_points], axis)
            if state["shared_scale"]:
                lo, hi = shared_lo, shared_hi
            auto_fit = chart_interaction.autofit(app, identity, scale=scale)
            if box:
                lo, hi = box["y"]
                if auto_fit and not captured:
                    lo, hi = charts.fit_time_bounds(plotted, [point["t"] for point in raster_points],
                                                    curve_times, box["y"], _cadence(points))
            if captured:
                lo, hi = captured["y"]
            def axis_label(value):
                if axis["mode"] == "log":
                    try:
                        value = 10 ** value
                    except OverflowError:
                        return ">1e308"
                if auto_fit and axis["mode"] != "log":
                    return chart_tools.format_axis(value, preference, lo, hi)
                return chart_tools.format_value(value, preference)
            metadata = {}
            curve = charts.braille_chart(g, plotted, width, 3, lo=lo, hi=hi,
                                            title=jid, sample_times=[p["t"] for p in raster_points],
                                            times=curve_times, sample_interval=_cadence(points),
                                            elapsed=True, axis_formatter=axis_label,
                                            color=lambda _, i=i: COLORS[i % len(COLORS)], metadata=metadata,
                                            fitted=auto_fit, time_units=bool(box))
            chart_interaction.record(app, identity, metadata, row=len(rows), scale=scale, layer=1)
            if curve:
                curve[0] = charts._header(g, values, width, jid, unit, "   ")
            rows.extend(curve)
            if undefined:
                rows.append(row(f" {jid}: {undefined} nonpositive samples undefined on logarithmic axis.", "yellow"))
    if not any(all_series.values()):
        rows.append(row(" No resource samples observed for these jobs in this session. Accounting values remain visible above.", "dim"))
    return rows


def overlay(views, snap, app, width, height):
    state = initialize(app)
    state["control_hits"] = []
    if getattr(app, "mode", "main") != "analysis":
        return None
    g = views.g
    modal = state.get("modal", "")
    scroll_context = (modal, state.get("job"), state.get("chart_job"), state.get("section"),
                      state.get("metric"), state.get("diff_kind"), tuple(state.get("diff_ids", ())), width)
    inner = max(1, min(160, width - 8))
    page = max(1, height - 6)
    state["chart_page"] = page
    row = lambda text, style="": [(clean(text, g.ascii), style)]
    button_hits = []
    chart_mark = chart_interaction.mark(app)
    if modal == "inspect":
        rows = _inspector_rows(g, snap, app, inner)
        button_hits = list(state.get("inspector_controls", []))
        title = "Job inspector / " + str(state.get("job", ""))
        footer = row(" Tab / arrows: section   Up / Down: scroll   l: logs   e: evidence   Esc: back", "dim")
    elif modal == "chart":
        series, source, jid = chart_data(app, snap)
        chart_job = app.job_record(jid, snap) if jid and hasattr(app, "job_record") else None
        state["chart_frame"] = {"series": series, "job": jid, "source": source,
                                "generation": getattr(getattr(app, "research", None), "generation", None),
                                "result": id(getattr(app, "analysis_result", None)), "record": chart_job}
        names = list(series)
        name = state.get("metric") if state.get("metric") in names else names[0] if names else ""
        state["metric"] = name
        extra = 4 if state.get("chart_range", {}).get("metric") == name else 0
        plot_metadata = {}
        zoom_key = chart_key(app, name, source, jid=jid, job=chart_job)
        from . import metric_live
        live_running = running_job(snap, jid)
        rows, live_hits = metric_live.controls(g, app, zoom_key, inner, running=live_running, layer=1)
        button_hits.extend(live_hits)
        chart_offset = len(rows)
        curve = chart_rows(g, app, series.get(name, []), inner, max(1, min(20, page - 10 - extra - chart_offset)), name or "No metric samples", source, snapshot=snap,
                           metadata=plot_metadata, zoom_key=zoom_key, running=live_running)
        chart_interaction.record(app, zoom_key, plot_metadata, row=chart_offset,
                                 scale=plot_metadata.get("scale", "linear"), layer=1)
        rows.extend(curve)
        window = f" | window {state['window']:g}s" if state.get("window") else ""
        active_preset = state.get("preset", "all" if state.get("zoom", 1) == 1 and not state.get("window") else "custom")
        display_box = chart_interaction.bounds(app, zoom_key, scale=plot_metadata.get("scale", "linear"))
        if display_box:
            fit = chart_interaction.autofit(app, zoom_key, scale=plot_metadata.get("scale", "linear"))
            rows.append(row(f" {'Time' if fit else 'Box'} zoom t={display_box['x'][0]!r} to {display_box['x'][1]!r} | u: undo | 0: reset", "cyan"))
        else:
            rows.append(row(f" Zoom x{state.get('zoom', 1):g} | pan {state.get('pan', 0):.0%}{window}" +
                            (" | [custom]" if active_preset == "custom" else ""), "cyan"))
        from .control_rows import buttons
        choices = [(label, "[" + label + "]" if label == active_preset else label,
                    ("command", "chart preset " + label)) for label, _ in chart_tools.PRESETS]
        choices += [("undo", "Undo zoom", ("command", "chart undo")),
                    ("reset", "Reset zoom", ("command", "chart reset")),
                    ("auto", "Auto axis", ("command", "chart axis auto")),
                    ("log", "Log axis", ("command", "chart axis log")),
                    ("range", "Select range", ("key", "r")), ("events", "Events", ("key", "e"))]
        choices += [("mode-" + mode, "[" + label + "]" if state["metric_modes"].get(name, "default") == mode else label,
                     ("command", "chart mode " + mode)) for mode, label in
                    (("default", "Kind default"), ("raw", "Raw"), ("trend", "Trend"), ("range", "Range"))]
        controls, hits = buttons(g, inner, choices, selected=active_preset, group="chart_controls", prefix="chart-control:")
        button_hits.extend((y + len(rows), kind, value) for y, kind, value in hits)
        rows.extend(controls)
        title, footer = "Chart inspector", row(" Arrows: sample  +/-: zoom  [ ]: pan  Tab: metric  Home/End  Esc: back", "dim")
    elif modal == "chart_events":
        events = chart_events(app, snap)
        state["cursor"] = max(0, min(max(0, len(events) - 1), state.get("cursor", 0)))
        rows = []
        for i, event in enumerate(events):
            selected = rows_selected(app, "chart_events") and i == state["cursor"]
            first_row = len(rows)
            rows.append(row(f" {'>' if selected else ' '} {event['number']:3} {_time(event['t'])} {event.get('kind', '')} | job {event.get('job') or 'global'}", "rev+bold" if selected else "cyan"))
            rows.append(row("      " + str(event.get("text", "")), "bold" if selected else "dim"))
            rows.append(row("      Source: " + (event.get("path") or event.get("source") or "Raven observed event") +
                            (" | original line " + str(event["line"]) if event.get("line") is not None else " | original line unavailable"), "dim"))
            for line in range(first_row, len(rows)):
                button_hits.append((line, "control", {"id": f"chart-event:{i}:{line - first_row}",
                    "label": f"Open chart event {i + 1}", "left": 0,
                    "right": min(inner, L.vlen(L.row_text(rows[line]))),
                    "action": ("click", line, 0), "group": "chart_events",
                    "event_index": i,
                    "event": dict(event), "event_context": (modal, state.get("chart_job"))}))
        if not rows:
            rows = [row(" No timestamped events observed for this chart job. Historical phases remain unknown.", "dim")]
        if rows_selected(app, "chart_events") and S.manual(app, "analysis:document", context=scroll_context) is None:
            state["scroll"] = max(0, state["cursor"] * 3 - page // 2)
        title, footer = "Chart events / " + str(state.get("chart_job") or _analysis_jid(app)), row(" Arrows: event | Enter: exact job or cited file | Esc: chart", "dim")
    elif modal == "timeline":
        events = timeline_events(app, snap)
        state["cursor"] = max(0, min(max(0, len(events) - 1), state.get("cursor", 0)))
        rows = []
        for i, event in enumerate(events):
            selected = rows_selected(app, "timeline") and i == state["cursor"]
            first_row = len(rows)
            rows.append(row(f" {'>' if selected else ' '} {i + 1:3} {_time(event['t'])} {event.get('kind', '')} {event.get('job') or ''}", "rev+bold" if selected else "cyan"))
            rows.append(row("      " + str(event.get("text", "")), "bold" if selected else "dim"))
            for line in range(first_row, len(rows)):
                button_hits.append((line, "control", {"id": f"timeline-event:{i}:{line - first_row}",
                    "label": f"Open timeline event {i + 1}", "left": 0,
                    "right": min(inner, L.vlen(L.row_text(rows[line]))),
                    "action": ("click", line, 0), "group": "timeline_events",
                    "event_index": i,
                    "event": dict(event), "event_context": (modal, state.get("chart_job"))}))
        if not rows:
            rows = [row(" No timestamped events observed yet.", "dim")]
        if rows_selected(app, "timeline") and S.manual(app, "analysis:document", context=scroll_context) is None:
            state["scroll"] = max(0, state["cursor"] * 2 - page // 2)
        title, footer = "Observed event timeline", row(" Arrows: event  Enter: job / cited log  s: seek replay  Esc: back", "dim")
    elif modal == "diff":
        if state.get("diff_kind") == "passport":
            comparison = state.get("comparison", {})
            rows = passport_rows(g, comparison.get("left") if state.get("unchanged") else {}, comparison.get("differences", []))
            rows.insert(0, row(" Left " + str(comparison.get("left", {}).get("id", "?")) + " / Right " + str(comparison.get("right", {}).get("id", "?")), "cyan+bold"))
        else:
            rows = _job_diff_rows(g, snap, app, inner)
        title, footer = "Run comparison", row(" Up / Down: scroll   u: unchanged fields   s: shared scales   Esc: back", "dim")
    else:
        series, _, _ = chart_data(app, snap)
        rows = [row(" Metric dashboard arrangement", "cyan+bold")]
        for i, name in enumerate(list(dict.fromkeys(state["order"] + list(series)))[:MAX_METRICS]):
            flags = [label for field, label in (("pinned", "pinned"), ("hidden", "hidden"), ("expanded", "expanded")) if name in state[field]]
            rows.append(row(f" {i + 1:2}. {name}  {', '.join(flags) or 'visible'}  {state['colors'].get(name, 'automatic color')}", "dim" if name in state["hidden"] else "cyan"))
        rows += [row(" :dashboard pin / hide / show / expand METRIC", "dim"), row(" :dashboard move METRIC POSITION | color METRIC cyan", "dim"), row(" :chart METRIC opens exact-sample inspection.", "dim")]
        rows.append(row(" :dashboard search TEXT filters visible cards; :dashboard search clears it.", "dim"))
        title, footer = "Dashboard", row(" Up / Down: scroll   Esc: back", "dim")
    state["row_count"] = len(rows)
    offset = max(0, min(state.get("scroll", 0), max(0, len(rows) - page)))
    state["scroll"] = offset
    from .scrolling import viewport
    count = len(rows)
    painted = viewport(app, "analysis:document", offset, count, page, context=scroll_context)
    rows = [L.clip_row(line, inner) for line in rows[painted:painted + page]] + [L.clip_row(footer, inner)]
    rendered = L.box(g, rows, width, height, "   " + title, min_width=min(max(1, inner), 100))
    if modal in ("chart", "diff") and len(rendered) > 2:
        first_y, first_x, first_row = rendered[1]
        chart_interaction.place_since(app, chart_mark, dy=first_y - painted, dx=first_x + 1,
                                     clip=(first_y, first_x + 1, rendered[-1][0],
                                           first_x + L.vlen(L.row_text(first_row)) - 1))
    from .control_rows import place_hits
    state["control_hits"] = place_hits(button_hits, rendered[1:-1], offset=painted)
    for y, _, value in state["control_hits"]:
        if "event" in value:
            value["action"] = ("click", y, value["left"])
    if count > 0 and len(rendered) >= 3:
        top, left, first = rendered[1]
        right = left + L.vlen(L.row_text(first))
        bottom = min(rendered[-1][0], top + page)
        S.register(app, "analysis:document", (top, left + 1, bottom, right),
                   count, page, offset, painted,
                   lambda value: state.__setitem__("scroll", value),
                   context=scroll_context, header=(rendered[0][0], left + 1, right - 1),
                   layer=1, absolute=True)
    return rendered


def handle_mouse(app, y, x, button="left", shift=False):
    if getattr(app, "mode", "main") != "analysis":
        return False
    if button == "right":
        return context_click(app, y, x, button=button)
    if button != "left":
        return False
    state = initialize(app)
    for row, kind, value in state.get("control_hits", []):
        if row == y and value["left"] <= x < value["right"]:
            if "event" in value:
                if value["event_context"] == (state.get("modal"), state.get("chart_job")):
                    S.resume(app, "analysis:document")
                    resume_rows(app, state.get("modal"))
                    state["cursor"] = value.get("event_index", state.get("cursor", 0))
                    _open_timeline_event(app, value["event"])
                return True
            action, argument = value["action"]
            if action == "command":
                app.run_command(argument)
            elif action == "key":
                handle_key(app, argument)
            return True
    return False
