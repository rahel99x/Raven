"""Apply polling preferences to existing collectors without UI callbacks.

The refresh UI and telemetry preset owner call this one-way boundary, then
perform their own policy synchronization. No UI module is imported here.
"""

from __future__ import annotations

from .cadence import recordings_interval, validate_multiplier


def sync(app, value):
    cfg = getattr(app, "cfg", None)
    managed = bool(cfg and cfg.get("raven", {}).get("managed"))
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
        # Managed telemetry profiles own non-queue cadence. A queue slider
        # gesture must not silently accelerate research scans or log reads.
        target_value = 1 if managed and name != "sampler" else value
        setter = getattr(target, "set_polling_multiplier", None)
        if callable(setter):
            setter(target_value)
        else:
            target.polling_multiplier = target_value
    catalog = getattr(getattr(app, "logs", None), "catalog", None)
    if catalog is not None:
        catalog.polling_multiplier = 1 if managed else value
    if not managed:
        setter = getattr(getattr(app, "sampler", None), "set_recordings_interval", None)
        if callable(setter):
            setter(recordings_interval(app))


def initialize(app):
    if not isinstance(getattr(app, "refresh_rate_state", None), dict):
        cfg = getattr(app, "cfg", None)
        value = validate_multiplier(cfg.get("polling_multiplier", 1) if cfg else 1)
        app.refresh_rate_state = {"multiplier": value}
        sync(app, value)
    return app.refresh_rate_state


def set_multiplier(app, value):
    value = validate_multiplier(value)
    state = initialize(app)
    state["multiplier"] = value
    sync(app, value)
    return value
