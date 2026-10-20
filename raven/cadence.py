"""Pure refresh arithmetic, graph-source identity and read-only preferences.

This leaf imports no UI, scheduler, collector or configuration owner. Legacy
refresh_rate and metric_sampling names re-export these exact function objects.
"""

from __future__ import annotations

import math

MIN_MULTIPLIER = 1
MAX_MULTIPLIER = 50
MIN_POLL_INTERVAL = 0.5
MAX_POLL_INTERVAL = 5.0
NATIVE_SOURCES = frozenset(("jobs", "live", "gpu", "trace"))
SOURCE_FLOORS = {"weather": 30.0, "budget": 30.0}
SOURCE_FLOOR = 0.5
LOCAL_FILE_FLOOR = 0.5
REMOTE_FILE_FLOOR = 1.5


def validate_multiplier(value):
    """Accept finite whole numbers from 1 to 50; reject bools and text."""
    if (
        type(value) not in (int, float)
        or not MIN_MULTIPLIER <= value <= MAX_MULTIPLIER
        or not math.isfinite(value)
        or value != int(value)
    ):
        raise ValueError("polling_multiplier must be a whole number between 1 and 50")
    return int(value)


def poll_interval(value, maximum=MAX_MULTIPLIER):
    """Map a slider position to seconds, with exact reachable endpoints.

    Logarithmic spacing distributes useful changes across the complete track;
    there is no clamped high-speed region in a native collector's slider.
    """
    if type(maximum) is not int or maximum < 2:
        raise ValueError("Polling slider maximum must be a whole number of at least 2")
    if (
        type(value) not in (int, float)
        or not 1 <= value <= maximum
        or not math.isfinite(value)
        or value != int(value)
    ):
        raise ValueError(f"Polling position must be a whole number from 1 to {maximum}")
    if value == 1:
        return MAX_POLL_INTERVAL
    if value == maximum:
        return MIN_POLL_INTERVAL
    fraction = (value - 1) / (maximum - 1)
    return MAX_POLL_INTERVAL * (MIN_POLL_INTERVAL / MAX_POLL_INTERVAL) ** fraction


def poll_position(seconds, maximum=MAX_MULTIPLIER):
    """Convert a positive interval to the nearest bounded slider position."""
    if type(maximum) is not int or maximum < 2:
        raise ValueError("Polling slider maximum must be a whole number of at least 2")
    if type(seconds) not in (int, float) or seconds <= 0:
        raise ValueError("Polling interval must be finite and positive")
    try:
        seconds = float(seconds)
    except OverflowError as exc:
        raise ValueError("Polling interval must be finite and positive") from exc
    if not math.isfinite(seconds):
        raise ValueError("Polling interval must be finite and positive")
    if seconds >= MAX_POLL_INTERVAL:
        return 1
    if seconds <= MIN_POLL_INTERVAL:
        return maximum
    fraction = math.log(seconds / MAX_POLL_INTERVAL) / math.log(
        MIN_POLL_INTERVAL / MAX_POLL_INTERVAL
    )
    return max(1, min(maximum, round(1 + (maximum - 1) * fraction)))


def _base_interval(base):
    if type(base) not in (int, float) or base < 0:
        raise ValueError("polling interval must be finite and non-negative")
    try:
        base = float(base)
    except OverflowError as exc:
        raise ValueError("polling interval must be finite and non-negative") from exc
    if not math.isfinite(base):
        raise ValueError("polling interval must be finite and non-negative")
    return base


def _interval(base, value, floor):
    rate = validate_multiplier(value)
    base = _base_interval(base)
    # Never slow down an existing explicit cadence, including zero intervals
    # used by one-shot callers. Changing speed never edits the base interval.
    speed = MAX_POLL_INTERVAL / poll_interval(rate)
    return base if rate == 1 else max(min(base, floor), base / speed)


def source_interval(base, value, *, source=""):
    rate = validate_multiplier(value)
    base = _base_interval(base)
    if source in NATIVE_SOURCES:
        # Zero is an explicit one-shot/testing request, not a live slider.
        if base == 0:
            return 0.0
        # An explicit slow cadence is not a slider position. In particular,
        # --interval 60 must not round-trip through position 1 into five seconds.
        if base > MAX_POLL_INTERVAL:
            return _interval(base, rate, SOURCE_FLOOR)
        return poll_interval(rate)
    return _interval(base, rate, SOURCE_FLOORS.get(source, SOURCE_FLOOR))


def file_interval(base, value, *, remote=False):
    return _interval(base, value, REMOTE_FILE_FLOOR if remote else LOCAL_FILE_FLOOR)


MIN_RATE = 1
MAX_RATE = 100
MAX_REQUESTS = 128
GPU_FLOOR = 0.5


def validate_rate(value):
    if (
        type(value) not in (int, float)
        or not MIN_RATE <= value <= MAX_RATE
        or not math.isfinite(value)
        or value != int(value)
    ):
        raise ValueError("Metric sampling rate must be a whole number from 1 to 100")
    return int(value)


def interval(
    base, global_rate=1, metric_rate=1, *, source="", remote=False, file=False
):
    """Use the fastest requested interval, without multiplying slider indices."""
    global_rate, metric_rate = (
        validate_multiplier(global_rate),
        validate_rate(metric_rate),
    )
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
        requested = (
            baseline
            if metric_rate == 1
            else min(baseline, poll_interval(metric_rate, MAX_RATE))
        )
        floor = REMOTE_FILE_FLOOR if file and remote else SOURCE_FLOOR
        return max(floor, requested)
    baseline = (
        file_interval(base, global_rate, remote=remote)
        if file
        else source_interval(base, global_rate, source=source)
    )
    if metric_rate == 1:
        return baseline
    floor = (
        (REMOTE_FILE_FLOOR if remote else LOCAL_FILE_FLOOR)
        if file
        else SOURCE_FLOORS.get(source, SOURCE_FLOOR)
    )
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
    elif value >= 0.001:
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
        if name.startswith("gpu-trace:") or name in (
            "retained-cpu-seconds",
            "retained-rss",
        ):
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
    return "|".join(
        str(getattr(job, field, None) or "") for field in ("submit", "start")
    )


def matches(identity, job):
    """Scheduler collectors require an exact running job's attempt."""
    if not isinstance(identity, tuple) or len(identity) < 5 or job is None:
        return False
    if (
        str(identity[1]) != str(getattr(job, "id", ""))
        or getattr(job, "state", "") != "RUNNING"
    ):
        return False
    requested = identity[4]
    if isinstance(requested, str) and requested.startswith("scheduler:"):
        requested = requested[len("scheduler:") :]
    return requested == attempt(job)


def research_matches(identity, context):
    if (
        source(identity) != "research"
        or len(identity) != 8
        or not isinstance(context, dict)
    ):
        return False
    job = context.get("job")
    if (
        job is None
        or getattr(job, "state", "") != "RUNNING"
        or str(identity[1]) != str(context.get("jid", ""))
    ):
        return False
    if identity[7] != context.get("generation"):
        return False
    binding = context.get("binding") or {}
    if identity[5] != binding.get("project_root") or identity[6] != binding.get(
        "run_id"
    ):
        return False
    if binding.get("attempt") is not None:
        return identity[4] == binding["attempt"]
    return matches(identity, job)


def multiplier(app):
    """Read the current preference without work, IO, or admission of tasks."""
    state = getattr(app, "refresh_rate_state", None)
    cfg = getattr(app, "cfg", None)
    value = (
        state.get("multiplier", 1)
        if isinstance(state, dict)
        else (cfg.get("polling_multiplier", 1) if cfg else 1)
    )
    try:
        return validate_multiplier(value)
    except (TypeError, ValueError, OverflowError):
        return 1


def recordings_interval(app):
    """The independent retained-file preference; None follows the active preset."""
    state = getattr(app, "refresh_rate_state", None)
    value = state.get("recordings_seconds") if isinstance(state, dict) else None
    return value if type(value) in (int, float) and value in (1.0, 2.0) else None
