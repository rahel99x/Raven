"""Explicit terminal navigation and presentation effects bound by composition."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Callable


def _validate(record):
    for field in fields(record):
        if not callable(getattr(record, field.name)):
            raise TypeError("Terminal effect must be callable: " + field.name)


@dataclass(frozen=True)
class TerminalNavigation:
    maximize: Callable[[Any], Any]
    open_allocation: Callable[[Any, str, dict], Any]
    open_acceptance: Callable[[Any, dict], Any]
    open_project: Callable[[Any, str], Any]
    preview_artifact: Callable[[Any, dict], Any]

    def __post_init__(self):
        _validate(self)


@dataclass(frozen=True)
class TerminalPresentation:
    decorate: Callable[..., Any]
    pointer_active: Callable[[Any], bool]
    begin_gesture: Callable[[Any], Any]
    pointer_motion: Callable[..., bool]
    wrap_workspace: Callable[..., Any]

    def __post_init__(self):
        _validate(self)


def navigation(app):
    value = getattr(app, "terminal_navigation", None)
    if not isinstance(value, TerminalNavigation):
        raise ValueError("Terminal navigation has not been bound by the application")
    return value


def presentation(app):
    value = getattr(app, "terminal_presentation", None)
    if not isinstance(value, TerminalPresentation):
        raise ValueError("Terminal presentation has not been bound by the application")
    return value
