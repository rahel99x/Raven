"""Prepared, immutable recording publications and metric presentation values.

This reader layer does not import view renderers, input handlers, or UI roots.
"""

from __future__ import annotations
from collections import OrderedDict
from contextlib import nullcontext
from bisect import bisect_left, bisect_right
from typing import Sequence
import math
import sys
from raven import charts
from ..job_logs.text import display_text
from raven.ui.text_rows import _wrap_row


def _series_of(app, jid):
    """Interactive frames use published memory while old samples restore."""
    reader = (
        getattr(app.store, "series_view", None)
        if getattr(app, "interactive", False)
        else None
    )
    if not callable(reader):
        reader = getattr(app.store, "series_of", None)
    return (
        reader(jid)
        if callable(reader)
        else list(getattr(app.store, "series", {}).get(jid, ()))
    )


def _series_publication(app, jid, *, memory_only=False):
    if memory_only:
        reader = getattr(app.store, "series_publication", None)
        if callable(reader):
            return reader(jid, memory_only=True)
        with getattr(app.store, "lock", nullcontext()):
            return list(getattr(app.store, "series", {}).get(jid, ()))
    if not getattr(app, "interactive", False):
        # Headless reports and source adapters retain their explicit reader
        # contract (including report cutoffs and synchronous full restoration).
        return _series_of(app, jid)
    reader = getattr(app.store, "series_publication", None)
    return reader(jid, background=True) if callable(reader) else _series_of(app, jid)


def prepared_series(app, jid, key, build, *, memory_only=False):
    """Share source-derived values across full views and their Details panes.

    Presentation state never enters this cache. The source publication owns
    its records; foreign mutable sources retain exact content validation.
    """
    from raven.native_series_cache import NativeSeriesCache

    if getattr(app, "store", None) is None:
        return build(())
    owner = getattr(app, "_chart_owner", app)
    views = getattr(owner, "views_ref", None)
    cache = getattr(views, "_native_sources", None)
    if cache is None:
        cache = getattr(owner, "_series_preparations", None)
        if cache is None:
            cache = owner._series_preparations = NativeSeriesCache(
                budget=getattr(owner, "raven_cache_budget", None)
            )
    store = app.store
    attempt = (
        store.job_attempt(jid)
        if callable(getattr(store, "job_attempt", None))
        else None
    )
    scope = (
        getattr(store, "_series_scope", id(store)),
        getattr(getattr(app, "research", None), "generation", None),
    )
    return cache.remember(
        ("derived-series", scope, jid, attempt, key),
        _series_publication(app, jid, memory_only=memory_only),
        build,
    )


def normalized_series(app, jid):
    return prepared_series(app, jid, "normalized/v1", _native_series)


def _series_jobs(app):
    reader = (
        getattr(app.store, "series_jobs_view", None)
        if getattr(app, "interactive", False)
        else None
    )
    return reader() if callable(reader) else app.store.series_jobs()


def _native_series(samples):
    """Safe native observations without changing the saved archive objects.

    JSON objects from older versions or interrupted producers may be valid
    documents with invalid counters. Native summaries ignore invalid times
    and keep malformed counters as unknown readings, instead of multiplying
    strings or comparing containers with measured memory.
    """
    result = []
    for sample in samples:
        if (
            not isinstance(sample, dict)
            or sample.get("k") not in ("live", "gpu")
            or charts._finite(sample.get("t")) is None
        ):
            continue
        if sample["k"] == "live":
            changes = {
                field: None
                for field in ("cpu", "eff", "rss", "cpu_time")
                if sample.get(field) is not None
                and _gpu_value(sample[field], percentage=False) is None
            }
            if changes:
                sample = dict(sample, **changes)
        result.append(sample)
    return sorted(result, key=lambda sample: sample["t"])


def _series_notice(app, jid):
    """Explain background restores without accessing the archive on a frame."""
    reader = (
        getattr(app.store, "series_status", None)
        if getattr(app, "interactive", False)
        else None
    )
    info = reader(jid) if callable(reader) else None
    if not isinstance(info, dict):
        return None
    status = info.get("status")
    labels = {
        "loading": "Restoring recorded samples in the background",
        "busy": "Recorded samples are queued for background restoration",
        "limited": "Showing the most recent recorded samples; older observations remain on disk",
        "error": "Recorded samples could not be restored",
        "unavailable": "Recorded sample restoration is unavailable",
    }
    if status not in labels:
        return None
    message = display_text(str(info.get("message") or ""))[:400]
    return (
        labels[status] + (": " + message if message else ""),
        "yellow" if status in ("error", "unavailable") else "dim",
        status,
    )


class _MetricValues(Sequence):
    """Defer a native card's vectors until its band enters the viewport.

    Length is source geometry, available before reading individual counters.
    Materialization always returns plain lists for exact raster-cache identity;
    providers belong to one owned publication and never hide corrections.
    """

    def __init__(self, length, factory):
        self.length, self.factory, self.values = length, factory, None
        self.time_index = None

    def __len__(self):
        return self.length

    def materialize(self):
        if self.values is None:
            self.values = self.factory()
        return self.values

    def __iter__(self):
        return iter(self.materialize())

    def __getitem__(self, index):
        return self.materialize()[index]

    def index(self):
        if self.time_index is None:
            from raven.native_series_cache import ObservationIndex

            self.time_index = ObservationIndex(self.materialize())
        return self.time_index


def _metric_observations(values, timestamps, bounds, *, latest=None):
    """Keep visible observations and the exact two possible edge neighbours.

    Rasterizers sort observations and clip genuine measured line segments.
    Their nearest outside samples preserve those intersections, gaps, and
    duplicate-timestamp order without rescanning years of offscreen history.
    ``latest`` excludes replayed future records from both curves and source age.
    Callers only use this with an explicit positive sampling cadence, so
    clipping cannot change an inferred median cadence.
    """
    lower, upper = bounds
    selected, before, after, newest = [], None, None, None
    visible = []
    for index, (value, timestamp) in enumerate(zip(values, timestamps)):
        if (
            charts._finite(timestamp) is None
            or latest is not None
            and timestamp > latest
        ):
            continue
        newest = timestamp if newest is None or timestamp > newest else newest
        item = (index, value, timestamp)
        if lower <= timestamp <= upper:
            selected.append(item)
            visible.append(charts._finite(value))
        elif timestamp < lower:
            if before is None or timestamp >= before[2]:
                before = item
        elif after is None or timestamp < after[2]:
            after = item
    if before is not None:
        selected.append(before)
    if after is not None:
        selected.append(after)
    selected.sort(key=lambda item: item[0])
    return (
        [value for _, value, _ in selected],
        [timestamp for _, _, timestamp in selected],
        visible,
        newest,
    )


def _metric_source_times(timestamps, now, observation_index=None):
    """Find acquired time limits without reading every metric counter.

    Native sources retain a chronological index for moving frames. A generic
    source can arrive unsorted and include duplicate or replayed future times.
    Neither path treats a future observation as available telemetry.
    """
    if charts._finite(now) is None:
        return None, None, None
    if observation_index is not None:
        times = observation_index.sorted_times
        end = bisect_right(times, now)
        if not end:
            return None, None, None
        newest = times[end - 1]
        previous = bisect_left(times, newest, 0, end)
        return times[0], newest, times[previous - 1] if previous else None
    oldest, newest, previous = None, None, None
    for timestamp in timestamps or ():
        if charts._finite(timestamp) is None or timestamp > now:
            continue
        oldest = timestamp if oldest is None else min(oldest, timestamp)
        if newest is None or timestamp > newest:
            previous, newest = newest, timestamp
        elif timestamp < newest and (previous is None or timestamp > previous):
            previous = timestamp
    return oldest, newest, previous


def _metric_edge_values(values, timestamps, bounds, interval):
    """Actual clipped edge values, without letting bucketed gaps hide them."""
    samples = [
        (timestamp, charts._finite(value))
        for value, timestamp in zip(values, timestamps)
        if charts._finite(timestamp) is not None
    ]
    # Preserve source order at duplicate timestamps, including unknown values.
    samples.sort(key=lambda item: item[0])
    if interval is None or interval <= 0:
        deltas = sorted(
            b[0] - a[0]
            for a, b in zip(samples, samples[1:])
            if b[0] > a[0] and math.isfinite(b[0] - a[0])
        )
        interval = deltas[(len(deltas) - 1) // 2] if deltas else 0.0
    return [
        value
        for timestamp, value, _ in charts._iter_time_samples(
            samples, bounds, min(sys.float_info.max, interval * 2.5)
        )
        if value is not None and (timestamp == bounds[0] or timestamp == bounds[1])
    ]


def _metric_edge_signature(
    values, timestamps, end, now, interval, lo, hi, pixels, observation_index=None
):
    """Describe acquired edge brackets and their actual vertical raster cell.

    A new measured vertex, missing endpoint, or changed bracket is published
    immediately. Between them, a nearly level subpixel edge can reuse its
    painted domain. Indexed native sources read only the two edge counters.
    """
    before = after = None
    if observation_index is not None:
        index = observation_index
        available = bisect_right(index.sorted_times, now)
        right = min(available, bisect_right(index.sorted_times, end))
        if right:
            position = index.order[right - 1]
            before = (
                index.sorted_times[right - 1],
                position,
                charts._finite(values[position]),
            )
        if right < available:
            position = index.order[right]
            after = (
                index.sorted_times[right],
                position,
                charts._finite(values[position]),
            )
    else:
        for position, (value, timestamp) in enumerate(zip(values, timestamps)):
            if charts._finite(timestamp) is None or timestamp > now:
                continue
            if timestamp <= end and (before is None or timestamp >= before[0]):
                before = (timestamp, position, charts._finite(value))
            elif timestamp > end and (after is None or timestamp < after[0]):
                after = (timestamp, position, charts._finite(value))
    value, kind = None, "gap"
    if before is not None and before[0] == end:
        value, kind = before[2], "point"
    elif (
        before is not None
        and after is not None
        and before[2] is not None
        and after[2] is not None
    ):
        if after[0] - before[0] <= min(sys.float_info.max, interval * 2.5):
            fraction = charts._fraction(end, before[0], after[0])
            value = charts._between(
                min(before[2], after[2]),
                max(before[2], after[2]),
                fraction if before[2] <= after[2] else 1.0 - fraction,
            )
            kind = "segment"
    pixel = (
        None
        if value is None
        else "below"
        if value < lo
        else "above"
        if value > hi
        else round((1.0 - charts._fraction(value, lo, hi)) * max(0, pixels - 1))
    )
    return before, after, kind, pixel


def _gpu_value(value, *, percentage=True):
    """Unknown counters are gaps, never measured idle devices."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    value = charts._finite(value)
    return (
        value
        if value is not None and value >= 0 and (not percentage or value <= 100)
        else None
    )


def _gpu_mean(values):
    measured = [value for item in values if (value := _gpu_value(item)) is not None]
    return sum(measured) / len(measured) if measured else None


def _gpu_record_mean(point):
    devices = point.get("gpu")
    if not isinstance(devices, dict):
        return None
    return _gpu_mean(
        reading[0]
        for reading in devices.values()
        if isinstance(reading, (list, tuple)) and reading
    )


def _gpu_memory(sample):
    used = _gpu_value(sample.used, percentage=False)
    total = _gpu_value(sample.total, percentage=False)
    ratio = (
        used / total if used is not None and total is not None and total > 0 else None
    )
    text = f"{used / 1024:.1f}" if used is not None else "unknown"
    text += (
        "/"
        + (f"{total / 1024:.0f}" if total is not None and total > 0 else "unknown")
        + " GB"
    )
    return ratio, text


def _managed_gpu_mode(app):
    value = getattr(app, "cfg", {}).get("raven", {})
    return (
        value.get("collection", {}).get("gpu", "existing-telemetry")
        if value.get("managed")
        else None
    )


def _gpu_unavailable(snap, app, jid, record, *, detected=False):
    """Explain published collection state without scheduler or file access."""
    if not detected and not getattr(record, "gpus", 0):
        return []
    managed = _managed_gpu_mode(app)
    if managed is not None:
        reason = "GPU rate graphs use retained launch telemetry or this job's existing logs/gpu-util-JOBID.csv trace."
    elif detected:
        reason = "GPU devices were detected, but utilisation counters are unavailable. Missing readings remain graph gaps."
    else:
        reason = "GPU telemetry is unavailable. Enable GPU sampling or provide this job's nvidia-smi trace."
    rows = [[(" " + reason, "yellow")]]
    health = snap.get("health", {}).get("gpu")
    if managed is not None:
        rows.append(
            [
                (
                    " GPU collection is disabled by configuration."
                    if managed == "off"
                    else " Raven reads existing telemetry; automatic device probes are disabled.",
                    "dim",
                )
            ]
        )
    elif not getattr(app, "gpu", True):
        rows.append([(" GPU sampling is off.", "dim")])
    elif health is not None and not getattr(health, "enabled", True):
        rows.append([(" The GPU source is disabled. Enable it in Sources.", "dim")])
    elif health is not None and getattr(health, "error", ""):
        rows.append(
            [(" GPU source error: " + display_text(str(health.error))[:800], "dim")]
        )
    elif not detected and getattr(record, "state", "") == "PENDING":
        rows.append(
            [(" This job is waiting. GPU sampling starts when it runs.", "dim")]
        )
    trace_status = getattr(getattr(app, "sampler", None), "trace_status", {})
    status = trace_status.get(jid) if isinstance(trace_status, dict) else None
    if isinstance(status, dict) and status.get("state") in (
        "missing_workdir",
        "missing_file",
        "empty",
        "error",
    ):
        trace_reason = display_text(str(status.get("reason") or status["state"]))[:800]
        rows.append([(" GPU trace: " + trace_reason, "dim")])
    rows.append([(" Run raven --gpu-check " + str(jid) + " for details.", "cyan")])
    return rows


def _busy_mean(values):
    total, count, result = 0.0, 0, []
    for value in values:
        if value is None:
            result.append(None)
        else:
            total += value
            count += 1
            result.append(total / count)
    return result


def _owned_metric_record(record, *, gpu=False):
    """Keep lazy preparations independent of detached caller-owned records.

    Plain scalar counters are immutable; only GPU reading containers need an
    additional copy. Extension mappings retain their existing reader behavior
    and are excluded from persistent preparations by the signature cache.
    """
    if type(record) is not dict:
        return record
    result = dict(record)
    devices = result.get("gpu") if gpu else None
    if type(devices) is dict:
        result["gpu"] = {
            key: tuple(value) if type(value) is list else value
            for key, value in devices.items()
        }
    return result


def _prepare_native_metrics(samples, req, ascii_, intervals, *, owned=False):
    """Prepare one content publication; moving windows reuse its lazy vectors."""
    live, gpus, first, last = [], [], None, None
    for sample in samples:
        if (
            not isinstance(sample, dict)
            or sample.get("k") not in ("live", "gpu")
            or charts._finite(sample.get("t")) is None
        ):
            continue
        sample = sample if owned else _owned_metric_record(sample, gpu=True)
        timestamp = sample["t"]
        first = timestamp if first is None or timestamp < first else first
        last = timestamp if last is None or timestamp > last else last
        (live if sample["k"] == "live" else gpus).append(sample)
    plots = []
    if live:
        live.sort(key=lambda sample: sample["t"])

        def cpu_values():
            values = []
            for sample in live:
                value = _gpu_value(sample.get("cpu"), percentage=False)
                if value is None:
                    value = _gpu_value(sample.get("eff"), percentage=False)
                values.append(value)
            return [None if value is None else 100 * value for value in values]

        times = min(sample["t"] for sample in live), max(sample["t"] for sample in live)
        stamps = _MetricValues(len(live), lambda: [sample["t"] for sample in live])
        title = (
            "cpu per core (rate, efficiency where no rate)"
            if ascii_
            else "CPU per core · rate / efficiency"
        )
        plots.append(
            (
                title,
                _MetricValues(len(live), cpu_values),
                100.0,
                "%",
                times,
                stamps,
                intervals["live"],
                "cpu-rate",
            )
        )
        memory = _MetricValues(
            len(live),
            lambda: [
                None
                if (rss := _gpu_value(sample.get("rss"), percentage=False)) is None
                else 100 * rss / req
                if req
                else rss / 1024**3
                for sample in live
            ],
        )
        plots.append(
            (
                "memory of the request" if req else "resident memory (GB)",
                memory,
                100.0 if req else None,
                "%" if req else "G",
                times,
                stamps,
                intervals["live"],
                "memory-request" if req else "resident-memory",
            )
        )
    gpu_points = sorted(gpus, key=lambda sample: sample["t"])
    keys = []
    for sample in gpu_points:
        devices = sample.get("gpu", {})
        for key in devices if isinstance(devices, dict) else ():
            if isinstance(key, str) and key and key not in keys:
                keys.append(key)
                if len(keys) == 4:
                    break
        if len(keys) == 4:
            break

    def reading(point, device):
        devices = point.get("gpu", {})
        value = devices.get(device) if isinstance(devices, dict) else None
        return (
            _gpu_value(value[0]) if isinstance(value, (list, tuple)) and value else None
        )

    stamps = _MetricValues(
        len(gpu_points), lambda: [point["t"] for point in gpu_points]
    )
    for key in keys:
        values = _MetricValues(
            len(gpu_points),
            lambda key=key: [reading(point, key) for point in gpu_points],
        )
        means = _MetricValues(len(values), lambda values=values: _busy_mean(values))
        times = gpu_points[0]["t"], gpu_points[-1]["t"]
        plots.append(
            (
                f"GPU {key} rate / utilization",
                values,
                100.0,
                "%",
                times,
                stamps,
                intervals["gpu"],
                f"gpu:{key}:rate",
            )
        )
        plots.append(
            (
                f"GPU {key} observed busy mean / efficiency proxy",
                means,
                100.0,
                "%",
                times,
                stamps,
                intervals["gpu"],
                f"gpu:{key}:busy-mean",
            )
        )
    return dict(
        live_count=len(live),
        gpu_count=len(gpus),
        span=last - first if first is not None and len(live) + len(gpus) > 1 else None,
        plots=plots,
        keys=keys,
        available=any(
            reading(point, key) is not None for point in gpu_points for key in keys
        ),
        heatmaps=OrderedDict(),
    )


def _prepare_trace_metrics(trace, *, owned=False):
    groups, spans = {}, {}
    for point in trace:
        if (
            not isinstance(point, dict)
            or type(point.get("index")) is not int
            or point["index"] < 0
            or charts._finite(point.get("t")) is None
        ):
            continue
        point = point if owned else _owned_metric_record(point)
        device, timestamp = point.get("device_id") or point["index"], point["t"]
        if not isinstance(device, (int, str)):
            continue
        groups.setdefault(device, []).append(point)
        bounds = spans.get(device)
        spans[device] = (
            (min(bounds[0], timestamp), max(bounds[1], timestamp))
            if bounds
            else (timestamp, timestamp)
        )
    devices, plots = (
        sorted(groups, key=lambda value: (isinstance(value, str), value))[:4],
        [],
    )
    for device in devices:
        group = groups[device]
        points = _MetricValues(
            len(group), lambda group=group: sorted(group, key=lambda point: point["t"])
        )
        values = _MetricValues(
            len(points),
            lambda points=points: [_gpu_value(point.get("util")) for point in points],
        )
        stamps = _MetricValues(
            len(points), lambda points=points: [point["t"] for point in points]
        )
        means = _MetricValues(len(values), lambda values=values: _busy_mean(values))
        plots.append(
            (
                f"GPU trace {device} rate / utilization ({len(points)} samples)",
                values,
                100.0,
                "%",
                spans[device],
                stamps,
                group[0].get("sample_interval", 60.0),
                f"gpu-trace:{device}:rate",
            )
        )
        plots.append(
            (
                f"GPU trace {device} observed busy mean / efficiency proxy",
                means,
                100.0,
                "%",
                spans[device],
                stamps,
                group[0].get("sample_interval", 60.0),
                f"gpu-trace:{device}:busy-mean",
            )
        )
    # Availability stays lazy when a native GPU already supplies valid data.
    return dict(
        plots=plots,
        devices=devices,
        availability=lambda: any(
            _gpu_value(point.get("util")) is not None
            for device in devices
            for point in groups[device]
        ),
    )


def _prepare_retained_process_metrics(rows):
    """Observed process CPU seconds and RSS, separate from scheduler efficiency."""
    rows = sorted(
        (row for row in rows if charts._finite(row.get("timestamp")) is not None),
        key=lambda row: row["timestamp"],
    )
    if not rows:
        return []
    stamps = [row["timestamp"] for row in rows]
    times = stamps[0], stamps[-1]
    interval = _gpu_value(rows[0].get("interval_seconds"), percentage=False) or 5.0
    interval = min(300.0, max(1.0, interval))
    cpu = [_gpu_value(row.get("cpu_seconds"), percentage=False) for row in rows]
    memory = [_gpu_value(row.get("rss_bytes"), percentage=False) for row in rows]
    memory = [value / 1024**3 if value is not None else None for value in memory]
    return [
        (
            "Observed local process CPU time (departed processes excluded)",
            cpu,
            None,
            "s",
            times,
            stamps,
            interval,
            "retained-cpu-seconds",
        ),
        (
            "Observed local process RSS sum (shared pages may repeat)",
            memory,
            None,
            "G",
            times,
            stamps,
            interval,
            "retained-rss",
        ),
    ]


def _retained_status_rows(app, jid, retained, width):
    """Show retained evidence and complete read diagnostics, including CPU-only runs."""
    sampler = getattr(app, "sampler", None)
    binding = getattr(sampler, "telemetry_binding", None) or {}
    if not retained and binding.get("job_id") != jid:
        return []
    status = getattr(sampler, "trace_status", {}).get(jid, {})
    stale = status.get("state") in ("deferred", "error")
    label = "Retained samples loaded" + (
        " (last successful read)" if stale and retained else ""
    )
    notes = [
        (
            f" {label}: {len(retained.get('cpu-memory', []))} CPU/memory, {len(retained.get('gpu', []))} GPU.",
            "dim",
        )
    ]
    if status.get("state") in ("deferred", "error") and status.get("reason"):
        notes.append(
            (
                " Retained telemetry read: " + display_text(str(status["reason"])),
                "yellow",
            )
        )
    for family, error in retained.get("errors", {}).items():
        notes.append(
            (" Retained " + family + " read: " + display_text(str(error)), "yellow")
        )
    return [row for note in notes for row in _wrap_row([note], width)]
