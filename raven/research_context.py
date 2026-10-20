"""Typed read inputs and UI callbacks for the research application service.

A reader consumes cached data; selecting or detaching belongs to the UI
composition root. No adapter here imports UI handlers or opens a file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class InteractionCallbacks:
    select: Callable[..., None]
    detach: Callable[[object], None]


def interaction_for(app: Any) -> InteractionCallbacks:
    value = getattr(app, "research_interaction", None)
    if not isinstance(value, InteractionCallbacks):
        raise ValueError(
            "This application has not attached its research interaction adapter"
        )
    return value


@dataclass(frozen=True)
class ResearchInputs:
    selection_cleared: bool = False
    project_logs: tuple[Any, ...] = ()


def capture_inputs(project: Any, selection: Any) -> ResearchInputs:
    """Freeze the already published inputs without mutating UI state."""
    project = project if isinstance(project, dict) else {}
    selection = selection if isinstance(selection, dict) else {}
    return ResearchInputs(
        bool(selection.get("deselected", {}).get("research", False)),
        tuple(project.get("logs", ())),
    )
