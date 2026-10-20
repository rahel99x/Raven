"""Explicit cross-owner actions composed at the application boundary.

A published frame never imports a neighboring feature to find its handlers.
These records name each owner capability and preserve its native validation.
"""

from dataclasses import dataclass, fields
from typing import Any, Callable, TypeVar, cast

Callback = Callable[..., Any]
T = TypeVar("T")


class _Callbacks:
    def __post_init__(self) -> None:
        for field in fields(cast(Any, self)):
            if not callable(getattr(self, field.name)):
                raise TypeError(f"{type(self).__name__}.{field.name} must be callable")


@dataclass(frozen=True)
class SelectionActions(_Callbacks):
    selection_mouse: Callback
    selection_key: Callback
    history_activate: Callback
    graph_mouse: Callback
    graph_hover: Callback
    log_mouse: Callback
    analysis_context: Callback
    history_export_mouse: Callback


@dataclass(frozen=True)
class InteractionSources(_Callbacks):
    toolbar_descriptors: Callback
    group_menu_active: Callback
    group_menu_controls: Callback
    list_menu_active: Callback
    list_menu_controls: Callback
    history_export_active: Callback
    history_export_controls: Callback
    pin_focused: Callback


@dataclass(frozen=True)
class PanelActions(_Callbacks):
    activate: Callback


@dataclass(frozen=True)
class ChartActions(_Callbacks):
    open_export: Callback


def _bound(app: object, name: str, expected: type[T]) -> T:
    value = getattr(app, name, None)
    if not isinstance(value, expected):
        raise RuntimeError(f"{name} has not been composed by the application")
    return value


def selection(app: object) -> SelectionActions:
    return _bound(app, "selection_actions", SelectionActions)


def interaction(app: object) -> InteractionSources:
    return _bound(app, "interaction_sources", InteractionSources)


def panels(app: object) -> PanelActions:
    return _bound(app, "panel_actions", PanelActions)


def charts(app: object) -> ChartActions:
    return _bound(app, "chart_actions", ChartActions)


@dataclass(frozen=True)
class ListActions(_Callbacks):
    grouping_command: Callback
    table_command: Callback


def lists(app: object) -> ListActions:
    return _bound(app, "list_actions", ListActions)
