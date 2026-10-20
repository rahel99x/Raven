"""Published Recents viewport/cache state without navigation or rendering owners."""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class RecentState:
    enabled: bool = False
    ratio: int = 35
    manual_split: bool = False
    page: int = 5
    queue_count: int = 0
    target: int = 5
    exhausted: bool = False
    focus: bool = False
    rect: object = None
    loaded: list = field(default_factory=list)
    observation: dict = field(default_factory=dict, repr=False)
    options: object = None
    index_key: object = field(default=None, repr=False)
    index: list = field(default_factory=list, repr=False)
    index_generation: int = 0
    sorted_key: object = field(default=None, repr=False)
    sorted_rows: list = field(default_factory=list, repr=False)
    expiry: float | None = None
    projector: object = field(default=None, repr=False)
    transition_anchor: str | None = field(default=None, repr=False)


def initialize(app):
    state = getattr(app, "recent_history_state", None)
    if not isinstance(state, RecentState):
        state = RecentState()
        app.recent_history_state = state
    return state
