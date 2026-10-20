"""Named peer-form and project-binding effects supplied by UI composition."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Callable


def _validate(record):
    for field in fields(record):
        if not callable(getattr(record, field.name)):
            raise TypeError("Navigation effect must be callable: " + field.name)


@dataclass(frozen=True)
class FormNavigation:
    telemetry: Callable[[Any], Any]
    run_home: Callable[[Any, dict | None], Any]
    relaunch: Callable[[Any, dict], Any]
    advanced_launch: Callable[[Any, str], Any]
    launch_receipt: Callable[[Any, dict], Any]
    outputs: Callable[[Any], Any]
    export_run: Callable[[Any], Any]
    historical: Callable[[Any], Any]
    connections: Callable[[Any], Any]
    allocation: Callable[[Any], Any]
    recordings: Callable[[Any], Any]
    diagnostics: Callable[[Any], Any]

    def __post_init__(self):
        _validate(self)


@dataclass(frozen=True)
class ProjectBindingEffects:
    before_log_source_change: Callable[[Any], Any]
    detach_array: Callable[[Any], Any]
    apply_array: Callable[[Any, dict], Any]
    bind_run: Callable[[Any, str, dict], Any]
    clear_binding: Callable[..., Any]

    def __post_init__(self):
        _validate(self)


@dataclass(frozen=True)
class ResearchNavigation:
    record: Callable[..., Any]
    resume: Callable[[Any, str], Any]

    def __post_init__(self):
        _validate(self)


@dataclass(frozen=True)
class ExportBackground:
    queue: Callable[[Any, str, Callable, Callable], Any]

    def __post_init__(self):
        _validate(self)


def exports(app):
    value = getattr(app, "export_background", None)
    if not isinstance(value, ExportBackground):
        raise ValueError("Export background work has not been bound by the application")
    return value


def forms(app):
    value = getattr(app, "form_navigation", None)
    if not isinstance(value, FormNavigation):
        raise ValueError("Form navigation has not been bound by the application")
    return value


def projects(app):
    value = getattr(app, "project_binding_effects", None)
    if not isinstance(value, ProjectBindingEffects):
        raise ValueError("Project effects have not been bound by the application")
    return value


def research(app):
    value = getattr(app, "research_navigation", None)
    if not isinstance(value, ResearchNavigation):
        raise ValueError("Research navigation has not been bound by the application")
    return value
