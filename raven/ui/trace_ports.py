"""Exact-attempt trace browser effects supplied by application composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class TraceBrowserEffects:
    current_context: Callable[[Any], dict | None]
    cancel_reader: Callable[[Any], Any]

    def __post_init__(self):
        if not callable(self.current_context) or not callable(self.cancel_reader):
            raise TypeError("Trace browser effects must be callable")


def browser_effects(app):
    value = app.trace_browser_effects
    if not isinstance(value, TraceBrowserEffects):
        raise TypeError("Trace browser effects were not composed for this application")
    return value
