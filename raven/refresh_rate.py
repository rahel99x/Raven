"""Shared polling controls with a useful 5-second to 500-millisecond range.

Stored whole-number positions remain compatible with existing preferences.
Native job and resource collectors use absolute requested intervals; other
sources retain their configured cadence, cost limits, and error backoff.
"""
from __future__ import annotations

import math
import re

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
    if (type(value) not in (int, float) or not MIN_MULTIPLIER <= value <= MAX_MULTIPLIER
            or not math.isfinite(value) or value != int(value)):
        raise ValueError("polling_multiplier must be a whole number between 1 and 50")
    return int(value)


def poll_interval(value, maximum=MAX_MULTIPLIER):
    """Map a slider position to seconds, with exact reachable endpoints.

    Logarithmic spacing distributes useful changes across the complete track;
    there is no clamped high-speed region in a native collector's slider.
    """
    if type(maximum) is not int or maximum < 2:
        raise ValueError("Polling slider maximum must be a whole number of at least 2")
    if (type(value) not in (int, float) or not 1 <= value <= maximum
            or not math.isfinite(value) or value != int(value)):
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
    fraction = math.log(seconds / MAX_POLL_INTERVAL) / math.log(MIN_POLL_INTERVAL / MAX_POLL_INTERVAL)
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


def multiplier(app):
    """Read the current preference without work, IO, or admission of tasks."""
    state = getattr(app, "refresh_rate_state", None)
    cfg = getattr(app, "cfg", None)
    value = state.get("multiplier", 1) if isinstance(state, dict) else (cfg.get("polling_multiplier", 1) if cfg else 1)
    try:
        return validate_multiplier(value)
    except (TypeError, ValueError, OverflowError):
        return 1


def _sync(app, value):
    cfg = getattr(app, "cfg", None)
    if cfg is not None:
        setter = getattr(cfg, "set", None)
        if callable(setter):
            setter("polling_multiplier", value)
        elif isinstance(cfg, dict):
            cfg["polling_multiplier"] = value
    for name in ("sampler", "research", "logs"):
        target = getattr(app, name, None)
        if target is None:
            continue
        setter = getattr(target, "set_polling_multiplier", None)
        if callable(setter):
            setter(value)
        else:
            target.polling_multiplier = value
    catalog = getattr(getattr(app, "logs", None), "catalog", None)
    if catalog is not None:
        catalog.polling_multiplier = value


def initialize(app):
    if not isinstance(getattr(app, "refresh_rate_state", None), dict):
        cfg = getattr(app, "cfg", None)
        value = validate_multiplier(cfg.get("polling_multiplier", 1) if cfg else 1)
        app.refresh_rate_state = {"multiplier": value}
        _sync(app, value)
    return app.refresh_rate_state


def set_multiplier(app, value):
    value = validate_multiplier(value)
    state = initialize(app)
    state["multiplier"] = value
    _sync(app, value)
    return value


def adjust(app, delta):
    if type(delta) not in (int, float) or (type(delta) is float and not math.isfinite(delta)) or delta != int(delta):
        raise ValueError("rate adjustment must be a finite whole number")
    return set_multiplier(app, max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, multiplier(app) + int(delta))))


def restore(app, data):
    initialize(app)
    if not isinstance(data, dict) or "multiplier" not in data:
        return
    try:
        value = validate_multiplier(data["multiplier"])
    except (TypeError, ValueError, OverflowError):
        return
    set_multiplier(app, value)


def save(app):
    value = multiplier(app)
    settings = getattr(app, "navigation_tools_state", None)
    if isinstance(settings, dict) and isinstance(settings.get("preview_backup"), dict):
        try:
            value = validate_multiplier(settings["polling_preview_backup"])
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    return {"multiplier": value}


def cadence(app, source, base=None):
    """Return the source's current interval, including dynamic plugin settings."""
    sampler = getattr(app, "sampler", None)
    effective = getattr(sampler, "effective_interval", None)
    if callable(effective):
        return effective(source)
    intervals = getattr(sampler, "intervals", None)
    if isinstance(intervals, dict) and source in intervals:
        base = intervals[source]
    elif base is None:
        cfg = getattr(app, "cfg", None)
        configured = cfg.get("intervals", {}) if cfg else {}
        base = configured.get(source, 0)
    return source_interval(base, multiplier(app), source=source)


def cadence_summary(app):
    from .metric_sampling import format_interval
    return (f"Jobs poll every {format_interval(cadence(app, 'jobs'), ascii_=True)}; "
            "source floors and error backoff still apply")


def candidate_cadence(app, source, value):
    """Slider labels use the same configured cadence and policy as collection."""
    sampler = getattr(app, "sampler", None)
    cfg = getattr(app, "cfg", {}) or {}
    intervals = getattr(sampler, "intervals", cfg.get("intervals", {}))
    result = source_interval(intervals.get(source, 0.), value, source=source)
    enforced = getattr(sampler, "_collection_interval", None)
    if callable(enforced):
        return enforced(source, result)
    collection = cfg.get("raven", {}).get("collection")
    if collection is not None:
        from .raven_collection import interval
        result = interval(collection, source, result)
    return result


def command_names():
    return ["rate"]


def execute(app, cmd, args):
    if cmd != "rate":
        return False
    try:
        if not args:
            pass
        elif args == ["reset"]:
            set_multiplier(app, 1)
        elif len(args) == 1 and isinstance(args[0], str) and re.fullmatch(r"[0-9]{1,2}", args[0]):
            set_multiplier(app, int(args[0]))
        else:
            raise ValueError("rate [1..50|reset]")
        app.say(cadence_summary(app))
    except (TypeError, ValueError, OverflowError) as exc:
        app.fail(str(exc))
    return True


def run_command(app, args):
    return bool(args) and execute(app, args[0], args[1:])


def handle_key(app, key):
    return False


def handle_mouse(app, y, x, button="left", shift=False):
    return False


def overlay(views, snap, app, width, height):
    return None
