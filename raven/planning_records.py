"""Bounded plain planning records, independent of analysis and dispatch."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass


def record(value):
    if isinstance(value, Mapping):
        return dict(value) if len(value) <= 256 else {}
    if is_dataclass(value) and not isinstance(value, type):
        names = fields(value)
        return (
            {field.name: getattr(value, field.name) for field in names}
            if len(names) <= 256
            else {}
        )
    return {}
