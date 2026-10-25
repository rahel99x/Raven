"""Small, strict native-batch helpers; no scheduler processes run on import."""

from __future__ import annotations

import math
import os
import re

from .. import evidence as E
from .core import stdin_command


def text(value, label="native field", *, empty=False):
    return E.text(value, label, 4096, empty=empty)


def integer(value, label="native count", *, optional=False):
    if value in (None, "", "-") and optional:
        return None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= (1 << 63) - 1:
        raise ValueError(f"{label} must be a nonnegative native integer")
    return value


def timestamp(value, label="native timestamp"):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{label} is missing or invalid")
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{label} must be a positive finite timestamp")
        return str(value)
    return text(value, label)


def records(value, limit, label="native job records"):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"{label} exceed the record bound or are not a list")
    if any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{label} must contain objects")
    return value


def checked_id(value, pattern, label="native job ID"):
    if type(value) is int:
        value = str(value)
    if (
        not isinstance(value, str)
        or len(value) > 512
        or not re.fullmatch(pattern, value)
    ):
        raise ValueError(
            f"Invalid exact {label}; ranges, switches and wildcards are unavailable"
        )
    return value


def memory_bytes(value):
    """PBS size units are binary, with no guessing of site-specific LSF units."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("PBS memory must retain an explicit native unit")
    match = re.fullmatch(r"([0-9]{1,18})([kmgtpe]?b)", value.lower())
    if not match:
        raise ValueError(
            "Unsupported PBS memory quantity; use explicit b/kb/mb/gb/tb/pb/eb"
        )
    power = {"b": 0, "kb": 1, "mb": 2, "gb": 3, "tb": 4, "pb": 5, "eb": 6}[match[2]]
    result = int(match[1]) * 1024**power
    if result > (1 << 63) - 1:
        raise ValueError("PBS memory quantity exceeds the supported bound")
    return result


def quantity(value, label="native resource quantity"):
    """Preserve native fractions instead of rounding resource requests."""
    if value in (None, "", "-"):
        return None
    if isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        value = float(value) if "." in value else int(value)
    return E.finite(value, label, 0)


def optional_count(value, label):
    return integer(value, label, optional=True)


def memory_quantity(value, label):
    return memory_bytes(value)


def metrics(mapping, fields, *, source):
    """Keep optional resources independent from action-authorizing identity.

    Native expressions and unsupported units remain raw evidence and a read
    diagnostic. They never become executable configuration or invented numbers.
    """
    values, diagnostics = {}, []
    if not isinstance(mapping, dict):
        return {field: None for field in fields}, [
            {
                "field": source,
                "native_value": mapping,
                "error": "Native optional resource fields are not an object",
            }
        ]
    for field, parser in fields.items():
        raw = mapping.get(field)
        try:
            values[field] = parser(raw, f"{source} {field}")
        except ValueError as exc:
            values[field] = None
            diagnostics.append(
                {"field": f"{source} {field}", "native_value": raw, "error": str(exc)}
            )
    return values, diagnostics


def submission(spec, *, unsupported=()):
    if not isinstance(spec, dict):
        raise ValueError("Submission settings must be an object")
    allowed = {
        "script",
        "account",
        "partition",
        "cpus",
        "memory_mib",
        "wall_seconds",
        "name",
        "nodes",
        "gpus",
        "native_options",
    }
    if set(spec) - allowed:
        raise ValueError(
            "Unknown submission settings: " + ", ".join(sorted(set(spec) - allowed))
        )
    script = text(spec.get("script"), "absolute submission file")
    if not script.startswith("/") or script != os.path.normpath(script):
        raise ValueError(
            "Submission file must be absolute and normalized on the scheduler host"
        )
    if spec.get("native_options") not in (None, []):
        raise ValueError(
            "Unreviewed native_options are unavailable; use the native submission file"
        )
    for key in unsupported:
        if spec.get(key) is not None:
            raise ValueError(
                f"{key} has no portable equivalent for this scheduler; specify its native semantics in the submission file"
            )
    for key in ("account", "partition", "name"):
        if spec.get(key) is not None:
            value = text(spec[key], key)
            if value.startswith("-") or not re.fullmatch(
                r"[\w.@/+:-]{1,256}", value, re.UNICODE
            ):
                raise ValueError(f"{key} must be an exact native token")
    for key in ("cpus", "memory_mib", "wall_seconds", "nodes", "gpus"):
        if spec.get(key) is not None:
            value = integer(spec[key], key)
            if value < (0 if key == "gpus" else 1) or value > (1 << 31) - 1:
                raise ValueError(
                    f"{key} must be a bounded integer with a valid native minimum"
                )
    return script


def stdin_submission(argv, script_text):
    """Submit reviewed bytes, not a path that can change after verification.

    The shell program is fixed. Script text and every native argument are
    positional arguments, so names, quotes and substitutions stay literal.
    The shared core enforces a per-argument bound below common exec limits.
    """
    return stdin_command(argv, script_text)


def action_owner(scheduler, record):
    expected = scheduler.scope.get("user")
    if not expected or record.get("user") != expected:
        raise ValueError("A reviewed action requires the exact configured login owner")
    if record.get("native", {}).get("identity_verified") is False:
        raise ValueError(
            "Native submission identity lacks authoritative precision; actions remain unavailable"
        )


def one(records_, identity):
    matches = [record for record in records_ if record["identity"] == identity]
    if len(matches) != 1:
        raise ValueError("Exact native job attempt is missing, stale or ambiguous")
    return matches[0]


def observed(records_, native_id, scheduler):
    """One exact native selection owned by the explicitly configured login."""
    if len(records_) != 1 or records_[0]["identity"]["native_id"] != native_id:
        raise ValueError(
            "Native job selection is absent, ambiguous or identifies another job"
        )
    record = records_[0]
    if record["user"] != scheduler.scope["user"]:
        raise ValueError("Native job selection is not owned by the selected login")
    return record


def hms(seconds):
    hours, remainder = divmod(seconds, 3600)
    minutes, second = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{second:02}"
