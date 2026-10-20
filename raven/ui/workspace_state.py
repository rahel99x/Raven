"""Workspace preference ownership and geometry value records, without actions."""

from __future__ import annotations
from dataclasses import dataclass, field

DENSITIES = ("comfortable", "compact", "focused")

PANELS = ("main", "details")


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


@dataclass
class LayoutState:
    density: str = "compact"
    focus: str = "main"
    maximized: bool = False
    ratio: int = 60
    named: dict = field(default_factory=dict)
    scroll: dict = field(default_factory=dict)
    sizes: dict = field(default_factory=dict)
    available: tuple = ("main",)
    selected: dict = field(default_factory=dict)
    overlay_top: int = 0
    overlay_count: int = 0
    overlay_page: int = 1
    interactive_panels: dict = field(default_factory=dict)
    reflow_cache: dict = field(default_factory=dict, repr=False)


def initialize(app) -> LayoutState:
    state = getattr(app, "layout_state", None)
    if not isinstance(state, LayoutState):
        state = LayoutState()
        cfg = getattr(app, "cfg", {})
        settings = cfg.get("workspace", {}) if hasattr(cfg, "get") else {}
        if isinstance(settings, dict):
            if settings.get("density") in DENSITIES:
                state.density = settings["density"]
            if isinstance(settings.get("split"), int) and not isinstance(
                settings["split"], bool
            ):
                state.ratio = max(20, min(80, settings["split"]))
        app.layout_state = state
    return state


def enabled(app) -> bool:
    state = initialize(app)
    return state.density != "compact" or state.focus != "main" or state.maximized
