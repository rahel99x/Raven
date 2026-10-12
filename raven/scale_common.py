"""Strict, bounded input primitives for the scale diagnostics.

These modules collect evidence only. They never run in the terminal renderer.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math


def text(value, label, *, limit=256, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not value and not empty):
        raise ValueError(f"{label} must be a {'nonempty ' if not empty else ''}string of at most {limit} characters")
    if any(not c.isprintable() for c in value):
        raise ValueError(f"{label} contains control characters")
    return value


def number(value, label, *, optional=False, minimum=0):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum or value > 1e100 or not math.isfinite(value) or (value != 0 and abs(value) < 1e-100):
        raise ValueError(f"{label} must be a finite number >= {minimum}, <= 1e100, and zero or >= 1e-100")
    return float(value)


def timestamp(value, label="timestamp"):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = number(value, label)
        if result > 253402300799:
            raise ValueError(f"{label} exceeds the supported calendar")
        return result
    text(value, label, limit=80)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO timestamp with a time zone or Unix seconds") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a time zone")
    result = parsed.timestamp()
    if not 0 <= result <= 253402300799:
        raise ValueError(f"{label} lies outside the supported calendar")
    return result


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


def items(value, label, *, limit=20000, empty=True):
    if not isinstance(value, list) or len(value) > limit or (not empty and not value):
        raise ValueError(f"{label} must be a list with {'1' if not empty else '0'}..{limit} items")
    if any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{label} items must be objects")
    return value


def document(value, schema):
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise ValueError(f"Expected schema {schema}")
    return value


def identity(value):
    return tuple(text(value.get(key), key) for key in ("cluster", "job_id", "attempt"))


def display(value, unit="", digits=3):
    return "unknown" if value is None else f"{value:.{digits}g}{unit}"
