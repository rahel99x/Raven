"""Explicit reviewed-plan navigation supplied by the application composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ExecutionNavigation:
    open_shell_checks: Callable[[Any, dict | None], Any]

    def __post_init__(self):
        if not callable(self.open_shell_checks):
            raise TypeError("open_shell_checks must be callable")


def navigation(app):
    value = app.execution_navigation
    if not isinstance(value, ExecutionNavigation):
        raise TypeError("Execution navigation was not composed for this application")
    return value
