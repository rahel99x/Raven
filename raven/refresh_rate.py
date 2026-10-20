"""Shared polling controls with a useful 5-second to 500-millisecond range.

Stored whole-number positions remain compatible with existing preferences.
Native job and resource collectors use absolute requested intervals; other
sources retain their configured cadence, cost limits, and error backoff.
"""

from __future__ import annotations

import math
import re

from .cadence import (
    LOCAL_FILE_FLOOR,
    MAX_MULTIPLIER,
    MAX_POLL_INTERVAL,
    MIN_MULTIPLIER,
    MIN_POLL_INTERVAL,
    NATIVE_SOURCES,
    REMOTE_FILE_FLOOR,
    SOURCE_FLOOR,
    SOURCE_FLOORS,
    _base_interval,
    _interval,
    file_interval,
    multiplier,
    poll_interval,
    poll_position,
    recordings_interval,
    source_interval,
    validate_multiplier,
)


def _sync(app, value):
    from . import telemetry_settings
    from .refresh_preferences import sync

    sync(app, value)
    telemetry_settings.slider_changed(app)


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


def set_recordings_interval(app, seconds):
    """Retune existing file readers without changing the job's recording writer."""
    if seconds is not None and (type(seconds) not in (int, float) or seconds not in (1.0, 2.0)):
        raise ValueError("Choose Automatic, 1 second or 2 seconds for reading recordings.")
    if getattr(app, "replay", None):
        raise ValueError("Live refresh changes are unavailable during replay.")
    state = initialize(app)
    from . import telemetry_settings

    managed = telemetry_settings.initialize(app) is not None
    previous = state.get("recordings_seconds")
    state["recordings_seconds"] = seconds
    try:
        if managed:
            telemetry_settings.sync(app)
        else:
            setter = getattr(getattr(app, "sampler", None), "set_recordings_interval", None)
            if callable(setter):
                setter(seconds)
    except (ValueError, RuntimeError, OSError):
        state["recordings_seconds"] = previous
        if managed:
            telemetry_settings.sync(app)
        raise
    return seconds


def adjust(app, delta):
    if (
        type(delta) not in (int, float)
        or (type(delta) is float and not math.isfinite(delta))
        or delta != int(delta)
    ):
        raise ValueError("rate adjustment must be a finite whole number")
    return set_multiplier(
        app, max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, multiplier(app) + int(delta)))
    )


def restore(app, data):
    initialize(app)
    if not isinstance(data, dict):
        return
    if "multiplier" in data:
        try:
            value = validate_multiplier(data["multiplier"])
        except (TypeError, ValueError, OverflowError):
            pass
        else:
            set_multiplier(app, value)
    if "recordings_seconds" in data:
        try:
            set_recordings_interval(app, data["recordings_seconds"])
        except (TypeError, ValueError, OverflowError):
            pass


def save(app):
    value = multiplier(app)
    settings = getattr(app, "navigation_tools_state", None)
    if isinstance(settings, dict) and isinstance(settings.get("preview_backup"), dict):
        try:
            value = validate_multiplier(settings["polling_preview_backup"])
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    result = {"multiplier": value}
    recordings = recordings_interval(app)
    if recordings is not None:
        result["recordings_seconds"] = recordings
    return result


def cadence(app, source, base=None):
    """Return the source's current interval, including dynamic plugin settings."""
    sampler = getattr(app, "sampler", None)
    effective = getattr(sampler, "effective_interval", None)
    if callable(effective):
        return effective(source)
    from . import telemetry_settings

    managed = telemetry_settings.candidate_cadence(app, source, multiplier(app))
    if managed is not None:
        return managed
    intervals = getattr(sampler, "intervals", None)
    if isinstance(intervals, dict) and source in intervals:
        base = intervals[source]
    elif base is None:
        cfg = getattr(app, "cfg", None)
        configured = cfg.get("intervals", {}) if cfg else {}
        base = configured.get(source, 0)
    return source_interval(base, multiplier(app), source=source)


def cadence_summary(app):
    from .cadence import format_interval

    return (
        f"Jobs poll every {format_interval(cadence(app, 'jobs'), ascii_=True)}; "
        "request limits, idle slowdown and error backoff still apply"
    )


def candidate_cadence(app, source, value):
    """Slider labels use the same configured cadence and policy as collection."""
    value = validate_multiplier(value)
    from . import telemetry_settings

    managed = telemetry_settings.candidate_cadence(app, source, value)
    if managed is not None:
        return managed
    sampler = getattr(app, "sampler", None)
    cfg = getattr(app, "cfg", {}) or {}
    intervals = getattr(sampler, "intervals", cfg.get("intervals", {}))
    result = source_interval(intervals.get(source, 0.0), value, source=source)
    enforced = getattr(sampler, "_collection_interval", None)
    if callable(enforced):
        return enforced(source, result)
    collection = cfg.get("raven", {}).get("collection")
    if collection is not None:
        from raven.collection.ledger import interval

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


# Explicit compatibility re-exports preserve the historical flat API.
__all__ = [
    "multiplier",
    "recordings_interval",
    "MIN_MULTIPLIER",
    "MAX_MULTIPLIER",
    "MIN_POLL_INTERVAL",
    "MAX_POLL_INTERVAL",
    "NATIVE_SOURCES",
    "SOURCE_FLOORS",
    "SOURCE_FLOOR",
    "LOCAL_FILE_FLOOR",
    "REMOTE_FILE_FLOOR",
    "validate_multiplier",
    "initialize", "set_multiplier", "set_recordings_interval", "adjust",
    "restore", "save", "cadence", "cadence_summary", "candidate_cadence",
    "command_names", "execute", "run_command", "handle_key", "handle_mouse", "overlay",
    "poll_interval",
    "poll_position",
    "_base_interval",
    "_interval",
    "source_interval",
    "file_interval",
]
