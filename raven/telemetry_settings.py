"""Live, mode-aware telemetry preferences with bounded configuration saving.

Rendering and typing do no I/O. Applying preferences reuses the existing
collectors and ledgers; saving is a separate background operation.
"""
from __future__ import annotations

import copy
import os
import time
from pathlib import Path

from .telemetry_profiles import DEFAULTS, FIELDS, PRESETS, VALUES, validate_custom
from .telemetry_profiles import value_fraction as value_fraction

_SOURCES = {"jobs": "queue_seconds", "starts": "starts_seconds", "finished": "history_seconds",
            "nodes": "inventory_seconds", "partitions": "inventory_seconds", "details": "details_seconds",
            "fin_details": "details_seconds", "live": "live_seconds", "gpu": "live_seconds",
            "trace": "trace_seconds", "account": "account_seconds", "share": "account_seconds",
            "budget": "account_seconds", "weather": "inventory_seconds"}


def _managed(app):
    value = getattr(app, "cfg", {}).get("raven", {})
    return value if isinstance(value, dict) and value.get("managed") else None


def _preference(value):
    value = value or {}
    preset = value.get("preset", "default")
    if preset not in PRESETS:
        raise ValueError("Choose Default, Medium, High, Ultra or Custom.")
    custom = validate_custom({**DEFAULTS["custom"], **value.get("custom", {})})
    return {"preset": preset, "custom": custom}


def initialize(app):
    raw = _managed(app)
    if raw is None:
        return None
    resolved = getattr(app, "raven_resolved", None) or getattr(getattr(app, "cfg", None), "raven_resolved", None)
    state = getattr(app, "telemetry_settings_state", None)
    if not isinstance(state, dict):
        from .configuration import policy as C
        baseline = C.merge(C.DEFAULTS, {key: value for key, value in raw.items() if key in C.DEFAULTS})
        preferences = _preference(baseline.get("telemetry"))
        restrictions = copy.deepcopy(getattr(resolved, "restrictions", raw.get("_raven_site_restrictions", ())))
        config_path = (getattr(resolved, "source", "") or getattr(app, "config_path", "")
                       or str(Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "raven/config.json"))
        state = {**preferences, "baseline": baseline, "restrictions": restrictions,
                 "config_path": config_path, "legacy": bool(getattr(resolved, "legacy_source", "")),
                 "saved_preferences": copy.deepcopy(getattr(resolved, "source_telemetry", None) or preferences), "signature": None,
                 "revision": 0, "error": "", "applied": None, "resolved": resolved}
        app.telemetry_settings_state = state
    elif (state.get("resolved") is not resolved
          or any(state["baseline"]["allocation"].get(key) != value
                 for key, value in raw.get("allocation", {}).items())):
        # Allocation Setup can change the active lab account before entering
        # Heavy. Reuse that admission context while preserving unsaved sampling
        # preferences and the original Light collection configuration.
        from .configuration import policy as C
        state["baseline"] = C.merge(C.DEFAULTS, {key: value for key, value in raw.items() if key in C.DEFAULTS})
        state["restrictions"] = copy.deepcopy(getattr(resolved, "restrictions", raw.get("_raven_site_restrictions", ())))
        state["resolved"] = resolved
        state["revision"] += 1
        state["signature"] = None
        state.pop("effective_cache", None)
    return state


def _heavy(app):
    snapshot = getattr(app, "raven_ui_state", {}).get("snapshot", {})
    return snapshot.get("state") == "heavy" and snapshot.get("mode") == "heavy" and not snapshot.get("cleanup_uncertain")


def _policies(state):
    from .configuration.policy import POLICY_SCHEMA
    return [("captured:" + str(index), {"schema": POLICY_SCHEMA, "restrictions": rule["rules"]})
            for index, rule in enumerate(state["restrictions"]) if isinstance(rule, dict) and "rules" in rule]


def _calculate(app, state, position):
    from .configuration import policy as C
    from .cadence import validate_multiplier
    value = copy.deepcopy(state["baseline"])
    heavy = _heavy(app)
    value["execution"]["startup"] = "heavy" if heavy else "light"
    if heavy:
        requested = state["custom"] if state["preset"] == "custom" else VALUES[state["preset"]]
        value["collection"].update(requested)
    from .cadence import recordings_interval
    recordings = recordings_interval(app)
    if recordings is not None:
        value["collection"]["trace_seconds"] = recordings
    policies = _policies(state)
    # Apply once to discover the queue's mandatory floor. Interpolation spans
    # the available range instead of leaving most of the slider clamped.
    limited, _ = C.enforce_site_restrictions(value, policies)
    nominal = float(value["collection"]["queue_seconds"])
    floor = .1
    for _, policy in policies:
        floor = max(floor, policy["restrictions"].get("floors", {}).get("collection.queue_seconds", .1))
    nominal = max(nominal, floor)
    fastest = max(floor, nominal / 10.)
    position = validate_multiplier(position)
    value["collection"]["queue_seconds"] = nominal * (fastest / nominal) ** ((position - 1) / 49.)
    effective, rules = C.enforce_site_restrictions(value, policies)
    collection = effective["collection"]
    collection["research_seconds"] = max(.5, collection["research_seconds"])
    limits = [f"{rule['path'].removeprefix('collection.')}: requested {rule['requested']:g}, site limit {rule['effective']:g}"
              for rule in rules if "path" in rule and rule["requested"] != rule["effective"]]
    if limited["collection"]["queue_seconds"] > (state["custom"] if state["preset"] == "custom" else VALUES[state["preset"]])["queue_seconds"] and heavy:
        limits.append(f"Queue interval is limited by site policy to at least {floor:g} s.")
    # Remote file reads have their own transport floor; show it before apply.
    if value["connection"]["transport"] == "ssh":
        for key in ("trace_seconds", "research_seconds", "log_follow_seconds"):
            if collection[key] < 1.5:
                limits.append(f"{key}: remote file reads use at least 1.5 s.")
                collection[key] = 1.5
    return collection, limits


def _effective(app, state, position=None):
    from .cadence import multiplier, recordings_interval, validate_multiplier
    position = validate_multiplier(multiplier(app) if position is None else position)
    key = (_heavy(app), state["revision"], state["preset"], tuple(state["custom"].items()), position,
           recordings_interval(app))
    cache = state.setdefault("effective_cache", {})
    if key not in cache:
        if len(cache) >= 128:
            cache.clear()
        cache[key] = _calculate(app, state, position)
    return cache[key]


def sync(app):
    """Apply once per selection, mode or attached collector change; no I/O."""
    state = initialize(app)
    if state is None:
        return False
    from .cadence import multiplier, recordings_interval
    signature = (_heavy(app), state["revision"], multiplier(app),
                 recordings_interval(app),
                 id(getattr(app, "sampler", None)), id(getattr(app, "research", None)),
                 id(getattr(app, "files", None)), id(getattr(app, "logs", None)))
    if state["signature"] == signature:
        return False
    collection, limits = _effective(app, state)
    from .telemetry_runtime import apply
    retained_floor = max((policy["restrictions"].get("floors", {}).get("collection.trace_seconds", 0.)
                          for _, policy in _policies(state)), default=0.)
    apply(app, collection, {source: collection[key] for source, key in _SOURCES.items()},
          collection["research_seconds"], retained_trace_floor=retained_floor)
    state.update(signature=signature, applied=collection, limits=limits, error="")
    return True


def slider_changed(app):
    return sync(app) if _managed(app) is not None else False


def candidate_cadence(app, source, position):
    state = initialize(app)
    if state is None or source not in _SOURCES:
        return None
    collection, _ = _effective(app, state, position)
    result = float(collection[_SOURCES[source]])
    sampler = getattr(app, "sampler", None)
    if source == "trace" and getattr(sampler, "telemetry_binding", None):
        site_floor = max((policy["restrictions"].get("floors", {}).get("collection.trace_seconds", 0.)
                          for _, policy in _policies(state)), default=0.)
        remote = state["baseline"]["connection"]["transport"] == "ssh"
        return max(1.5 if remote else .5, site_floor, min(2., result))
    activity = getattr(sampler, "_collection_activity", None)
    if source != "trace" and activity is not None and time.monotonic() - activity >= collection["idle_after_seconds"]:
        result *= max(1., collection["idle_interval_factor"])
    return result


def settings_snapshot(app):
    state = initialize(app)
    if state is None:
        raise ValueError("Telemetry presets require raven, ravenl or ravenh with a managed site profile.")
    collection, limits = _effective(app, state)
    effective = {field["key"]: collection[field["key"]] for field in FIELDS}
    from .cadence import multiplier
    position = multiplier(app)
    for source, key in _SOURCES.items():
        if key in effective:
            effective[key] = candidate_cadence(app, source, position)
    return {"preset": state["preset"], "custom": copy.deepcopy(state["custom"]),
            "effective": effective,
            "config_path": state["config_path"], "heavy": _heavy(app), "limits": limits,
            "message": "Changes apply live; source budgets and error backoff remain active." if _heavy(app)
            else "Light collection remains active. This telemetry preset takes effect when Heavy is active."}


def apply_settings(app, preset, custom=None, *, persist=False):
    if persist:
        raise ValueError("Save settings through the background configuration writer.")
    state = initialize(app)
    if state is None:
        raise ValueError("Launch raven, ravenl or ravenh with a managed site profile.")
    if getattr(app, "replay", None):
        raise ValueError("Live telemetry changes are unavailable during replay.")
    preferences = _preference({"preset": preset, "custom": state["custom"] if custom is None else custom})
    # Validate the complete draft before changing any running reader.
    draft = {**state, **preferences}
    _effective(app, draft, 1)
    before = {key: copy.deepcopy(state[key]) for key in ("preset", "custom", "revision", "signature")}
    from . import refresh_preferences
    from .cadence import multiplier
    old_position = multiplier(app)
    state.update(preferences)
    state["revision"] += 1
    try:
        refresh_preferences.set_multiplier(app, 1)  # A preset restores its stated intervals.
        sync(app)
    except (ValueError, RuntimeError, OSError):
        state.update(before)
        refresh_preferences.set_multiplier(app, old_position)
        sync(app)
        raise
    result = settings_snapshot(app)
    result["message"] = (preset.title() + " telemetry applied live." if result["heavy"] else
                         preset.title() + " telemetry selected; it will apply when Heavy is active.")
    return result


def prepare_save(app):
    """Capture plain values on the UI thread for an independent file writer."""
    state = initialize(app)
    if state is None or state["legacy"]:
        raise ValueError("Saving telemetry requires a versioned Raven configuration.")
    if getattr(app, "replay", None) or getattr(getattr(app, "research", None), "demo", False):
        raise ValueError("Configuration saving is unavailable in demo or replay sessions.")
    return {"path": state["config_path"], "profile": state["baseline"]["profile"],
            "preferences": {"preset": state["preset"], "custom": copy.deepcopy(state["custom"])},
            "expected": copy.deepcopy(state["saved_preferences"])}


def save_settings(request):
    """Merge only telemetry preferences; atomically preserve unrelated edits."""
    from .configuration import policy as C
    from .configuration.allocation import _atomic_write, _decode, _read_snapshot
    preferences = _preference(request["preferences"])
    path = Path(request["path"]).expanduser().absolute()
    raw, token = _read_snapshot(path)
    document = _decode(raw, path)
    profile = request["profile"]
    root_preference = document.get("telemetry", {})
    profile_preference = document.get("profiles", {}).get(profile, {}).get("telemetry", {})
    observed = _preference(C.merge(root_preference, profile_preference))
    if observed != request["expected"]:
        raise ValueError("Telemetry settings changed in the configuration file. Reopen Raven before saving; the file was not overwritten.")
    target = document.get("profiles", {}).get(profile)
    if target is None:
        if profile in ("default-slurm", "slurm") or document.get("profile") == profile:
            target = document
        else:
            target = document.setdefault("profiles", {}).setdefault(profile, {})
    target["telemetry"] = preferences
    C.validate(document, partial=True)
    _, warnings = _atomic_write(path, document, token)
    if path.suffix.lower() == ".toml":
        warnings.append("TOML values are preserved; comments and formatting are rewritten.")
    return {"message": "Telemetry settings saved for future launches.", "path": str(path), "warnings": warnings}


def saved(app, request, result):
    state = initialize(app)
    if state is not None and state["config_path"] == request["path"] and state["baseline"]["profile"] == request["profile"]:
        state["saved_preferences"] = copy.deepcopy(request["preferences"])
        cfg = getattr(app, "cfg", None)
        if callable(getattr(cfg, "set", None)):
            cfg.set("raven.telemetry", copy.deepcopy(request["preferences"]))
        elif isinstance(cfg, dict):
            cfg["raven"]["telemetry"] = copy.deepcopy(request["preferences"])
        resolved = getattr(app, "raven_resolved", None) or getattr(cfg, "raven_resolved", None)
        if resolved is not None:
            from dataclasses import replace
            data = resolved.as_dict()
            data["telemetry"] = copy.deepcopy(request["preferences"])
            updated = replace(resolved, data=data, source_telemetry=copy.deepcopy(request["preferences"]),
                              source=result["path"])
            app.raven_resolved = updated
            if hasattr(cfg, "__dict__"):
                cfg.raven_resolved = updated
    return result
