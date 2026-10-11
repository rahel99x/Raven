"""Small strict validators shared by optional runtime research adapters."""
from __future__ import annotations

import math
import re


def object_(value, required, optional=(), name="document"):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError(f"{name} requires {', '.join(sorted(required))}; unknown fields are not accepted")
    return value


def number(value, name, low=0, high=1e100, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < low or value > high or (positive and value == 0):
        raise ValueError(f"{name} must be finite and within {low}..{high}")
    return value


def integer(value, name, low=0, high=1000000):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer within {low}..{high}")
    return value


def text(value, name="identifier", limit=128):
    if not isinstance(value, str) or not value or len(value) > limit or any(not c.isprintable() for c in value):
        raise ValueError(f"{name} must be nonempty printable text of at most {limit} characters")
    return value


def identifier(value, name="identifier"):
    text(value, name)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,127}", value):
        raise ValueError(f"{name} contains unsupported characters")
    return value


def sequence(value, name, limit=10000, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= limit:
        raise ValueError(f"{name} must be a list containing {minimum}..{limit} entries")
    return value


def unique_names(value, name, limit=128):
    sequence(value, name, limit)
    for item in value:
        identifier(item, name)
    if len(set(value)) != len(value):
        raise ValueError(f"{name} contains duplicates")
    return value
