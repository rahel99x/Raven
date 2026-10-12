"""Bounded per-metric polling requests, shared by actual collection sources.

Display windows and polling are independent. Requests refer to exact published
jobs and attempts; changing a request never reads a file or starts a command.
"""
from __future__ import annotations

import math

from .refresh_rate import (NATIVE_SOURCES, SOURCE_FLOOR, SOURCE_FLOORS,
                           LOCAL_FILE_FLOOR, REMOTE_FILE_FLOOR,
                           poll_interval, source_interval, file_interval,
                           validate_multiplier)

MIN_RATE = 1
MAX_RATE = 100
MAX_REQUESTS = 128
GPU_FLOOR = 0.5


def validate_rate(value):
    if (type(value) not in (int, float) or not MIN_RATE <= value <= MAX_RATE
            or not math.isfinite(value) or value != int(value)):
        raise ValueError("Metric sampling rate must be a whole number from 1 to 100")
    return int(value)


def interval(base, global_rate=1, metric_rate=1, *, source="", remote=False, file=False):
    """Use the fastest requested interval, without multiplying slider indices."""
    global_rate, metric_rate = validate_multiplier(global_rate), validate_rate(metric_rate)
    if type(base) not in (int, float) or base < 0:
        raise ValueError("Polling interval must be finite and non-negative")
    try:
        base = float(base)
    except OverflowError as exc:
        raise ValueError("Polling interval must be finite and non-negative") from exc
    if not math.isfinite(base):
        raise ValueError("Polling interval must be finite and non-negative")
    if base == 0:
        return 0.0
    if source in NATIVE_SOURCES:
        baseline = source_interval(base, global_rate, source=source)
        requested = baseline if metric_rate == 1 else min(baseline, poll_interval(metric_rate, MAX_RATE))
        floor = REMOTE_FILE_FLOOR if file and remote else SOURCE_FLOOR
        return max(floor, requested)
    baseline = (file_interval(base, global_rate, remote=remote) if file
                else source_interval(base, global_rate, source=source))
    if metric_rate == 1:
        return baseline
    floor = ((REMOTE_FILE_FLOOR if remote else LOCAL_FILE_FLOOR) if file
             else SOURCE_FLOORS.get(source, SOURCE_FLOOR))
    return max(min(base, floor), min(baseline, poll_interval(metric_rate, MAX_RATE)))


def format_interval(value, *, ascii_=False):
    """Choose seconds, milliseconds, or microseconds without zero rounding."""
    if type(value) not in (int, float):
        return "?"
    try:
        if not math.isfinite(value) or value < 0:
            return "?"
    except OverflowError:
        return "?"
    if value == 0:
        return "0s"
    if value >= 1:
        number, unit = value, "s"
    elif value >= .001:
        number, unit = value * 1000, "ms"
    else:
        number, unit = value * 1000000, "us" if ascii_ else "µs"
    # Configured daily intervals must fit compact controls too. Scientific
    # notation would make 86400s longer than its complete integer spelling.
    label = f"{number:.0f}" if 1000 <= number < 100000 else f"{number:.3g}"
    return label + unit


def source(identity):
    """Map a graph to its collector rather than issue duplicate metric reads."""
    if not isinstance(identity, tuple) or len(identity) < 4:
        return None
    scope, _, name, origin = identity[:4]
    if not isinstance(name, str):
        return None
    if scope in ("resource-series", "resource-area"):
        if name.startswith("gpu-trace:") or name in ("retained-cpu-seconds", "retained-rss"):
            return "trace"
        if name.startswith("gpu:"):
            return "gpu"
        if name.lower().startswith(("cpu", "mem", "resident")):
            return "live"
    if scope == "reported-metric":
        if origin == "Raven session resource samples":
            if name == "GPU utilization (%)":
                return "gpu"
            if name in ("CPU per core (%)", "Memory (GB)"):
                return "live"
            return None
        return "research"
    return None


def attempt(job):
    return "|".join(str(getattr(job, field, None) or "") for field in ("submit", "start"))


def matches(identity, job):
    """Scheduler collectors require an exact running job's attempt."""
    if not isinstance(identity, tuple) or len(identity) < 5 or job is None:
        return False
    if str(identity[1]) != str(getattr(job, "id", "")) or getattr(job, "state", "") != "RUNNING":
        return False
    requested = identity[4]
    if isinstance(requested, str) and requested.startswith("scheduler:"):
        requested = requested[len("scheduler:"):]
    return requested == attempt(job)


def research_matches(identity, context):
    if source(identity) != "research" or len(identity) != 8 or not isinstance(context, dict):
        return False
    job = context.get("job")
    if job is None or getattr(job, "state", "") != "RUNNING" or str(identity[1]) != str(context.get("jid", "")):
        return False
    if identity[7] != context.get("generation"):
        return False
    binding = context.get("binding") or {}
    if identity[5] != binding.get("project_root") or identity[6] != binding.get("run_id"):
        return False
    if binding.get("attempt") is not None:
        return identity[4] == binding["attempt"]
    return matches(identity, job)


def sync(app):
    """Publish a bounded replacement snapshot only when demands change."""
    from . import metric_live
    owner = getattr(app, "_chart_owner", app)
    state = metric_live.initialize(owner)
    if state.get("sampling_sync"):
        return
    state["sampling_sync"] = True
    try:
        requests = {}
        for identity, entry in tuple(state["entries"].items()):
            rate = entry.get("rate", 1)
            if rate != 1 and metric_live._eligible(owner, identity) and source(identity):
                requests[identity] = validate_rate(rate)
        targets = tuple(getattr(owner, name, None) for name in ("sampler", "research"))
        signature = (tuple(requests.items()), tuple(id(target) for target in targets))
        if signature == state.get("sampling_signature"):
            return
        for target in targets:
            setter = getattr(target, "set_metric_sampling", None)
            if callable(setter):
                setter(requests)
        state["sampling_signature"] = signature
    finally:
        state["sampling_sync"] = False


def cadence(app, identity, rate=None):
    """Return the effective collection interval or a bounded config fallback.

    Candidate rates are used for slider endpoint labels. The current rate also
    includes faster requests from metrics sharing this job's collector.
    """
    owner = getattr(app, "_chart_owner", app)
    if not isinstance(identity, tuple) or len(identity) < 5:
        return None
    collector = source(identity)
    if collector is None:
        return None
    cfg = getattr(owner, "cfg", {}) or {}
    from .refresh_rate import multiplier
    global_rate = multiplier(owner)
    if rate is not None:
        try:
            rate = validate_rate(rate)
        except (ValueError, TypeError, OverflowError):
            return None
    sampler = getattr(owner, "sampler", None)
    if collector != "research":
        if rate is None:
            reader = getattr(sampler, "sampling_interval", None)
            if callable(reader):
                return reader(collector, str(identity[1]), identity[4] if len(identity) > 4 else None)
        defaults = {"live": 30.0, "gpu": 5.0, "trace": 5.0}
        base = getattr(sampler, "intervals", cfg.get("intervals", {})).get(collector, defaults[collector])
        if rate is None:
            from .metric_live import initialize
            state = initialize(owner)
            rate = max((entry.get("rate", 1) for key, entry in state["entries"].items()
                        if source(key) == collector and key[1] == identity[1] and key[4] == identity[4]
                        and entry.get("running")), default=1)
        value = interval(base, global_rate, rate, source=collector,
                         file=collector == "trace",
                         remote=bool(getattr(getattr(sampler, "files", None), "remote", False)))
        collection = getattr(sampler, "collection", None) or cfg.get("raven", {}).get("collection")
        if collection is not None:
            from .raven_collection import interval as collection_interval
            value = collection_interval(collection, collector, value)
        return value
    hub = getattr(owner, "research", None)
    if rate is None:
        reader = getattr(hub, "sampling_interval", None)
        if callable(reader):
            return reader(identity)
    settings = cfg.get("research", {}) or {}
    base = getattr(hub, "interval", settings.get("interval", 5.0))
    if rate is None:
        from .metric_live import initialize
        state = initialize(owner)
        rate = max((entry.get("rate", 1) for key, entry in state["entries"].items()
                    if source(key) == collector and key[1] == identity[1]
                    and key[4:] == identity[4:] and entry.get("running")), default=1)
    return interval(base, global_rate, rate, source="research", file=True,
                    remote=bool(getattr(getattr(hub, "files", None), "remote", False)))
