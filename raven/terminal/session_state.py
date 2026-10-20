"""Terminal session and frame state, independent of feature actions and renderers."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

PAGES = frozenset(("jobs", "history", "analytics", "deps", "log", "research"))
MAX_SELECTION_BYTES = 2 << 20
MAX_REPORT_LINES = 10000
MAX_SESSIONS = 8


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def contains(self, y, x):
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height


@dataclass
class TerminalState:
    service: Any = None
    launches: dict = field(default_factory=dict)
    active: str | None = None
    modes: dict = field(default_factory=dict)
    keyboard_focus: bool = False
    focus_generation: int = 0
    page: str = ""
    rect: Rect | None = None
    body_rect: Rect | None = None
    rail_rect: Rect | None = None
    controls: list = field(default_factory=list)
    copy_mode: bool = False
    follow: bool = True
    top: int = 0
    selected: tuple | None = None
    selection_anchor: int | None = None
    selection_end: int | None = None
    selection_source: tuple = ()
    selection_wraps: tuple = ()
    selected_wraps: tuple = ()
    history_wraps: dict = field(default_factory=dict)
    history_wrap_pages: dict = field(default_factory=dict)
    clipboard_preview: dict | None = None
    capture: tuple | None = None
    discard_release: bool = False
    prefix: bool = False
    error: str = ""
    dialog: dict | None = None
    preview: dict | None = None
    report: dict | None = None
    menu: bool = False
    menu_top: int = 0
    menu_index: int = 0
    menu_items: list | None = None
    visibility: dict = field(default_factory=dict)
    unread: dict = field(default_factory=dict)
    sequences: dict = field(default_factory=dict)
    sizes: dict = field(default_factory=dict)
    snapshots: dict = field(default_factory=dict)
    generation: int = 0
    painted: list = field(default_factory=list)
    painted_key: tuple = ()
    history_pages: dict = field(default_factory=dict)
    history_seen: dict = field(default_factory=dict)
    history_requested: dict = field(default_factory=dict)
    pending_search: bool = False
    history_loading: dict = field(default_factory=dict)
    history_snapshot: dict = field(default_factory=dict)
    geometry: tuple = ()
    hover: tuple | None = None
    search: str = ""
    search_matches: list = field(default_factory=list)
    search_index: int = -1
    history: dict = field(default_factory=dict)
    history_results: dict = field(default_factory=dict)

    @property
    def sessions(self):
        return self.service.sessions if self.service is not None else {}


def initialize(app):
    state = getattr(app, "terminal_state", None)
    if not isinstance(state, TerminalState):
        state = TerminalState(page=str(getattr(app, "tab", "jobs")))
        app.terminal_state = state
    return state


def enabled(app):
    return (
        isinstance(getattr(app, "terminal_state", None), TerminalState)
        and getattr(app, "tab", "") in PAGES
    )


def showing(app):
    return (
        enabled(app)
        and initialize(app).modes.get(getattr(app, "tab", ""), "details") == "terminal"
    )


def _bump(state):
    state.generation += 1


def _snapshot(app):
    state = initialize(app)
    if not state.active or state.service is None:
        return {}
    try:
        snapshot = state.service.snapshot(state.active)
    except (OSError, ValueError, RuntimeError, KeyError):
        return state.snapshots.get(state.active, {})
    if isinstance(snapshot, dict):
        state.snapshots[state.active] = snapshot
        return snapshot
    return {}
