"""Published docked history-list state and exact geometry validation."""

from __future__ import annotations
from dataclasses import dataclass

TABS = ("analytics", "deps", "log", "research")

DOCKS = ("auto", "right", "bottom", "left", "top")


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def contains(self, y, x):
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height


def initialize(app):
    state = getattr(app, "history_browser_state", None)
    if not isinstance(state, dict):
        state = {
            "views": {},
            "frame": None,
            "drag": None,
            "focused": False,
            "discard_release": False,
            "records": (),
            "record_key": None,
            "items": (),
            "index": 0,
        }
        app.history_browser_state = state
    state.setdefault("discard_release", False)
    return state


def _view(app):
    return initialize(app)["views"].setdefault(
        getattr(app, "tab", ""),
        {
            "dock": "auto",
            "enabled": True,
            "ratio": 25,
            "top": 0,
            "selected": None,
            "explicit": False,
        },
    )


def _current(app):
    frame = initialize(app).get("frame")
    toolbar = getattr(app, "toolbar_state", {})
    if (
        not frame
        or frame["tab"] != getattr(app, "tab", "")
        or frame["mode"] != getattr(app, "mode", "main")
        or frame["mode"] != "main"
        or frame["geometry"]
        != (getattr(app, "width", None), getattr(app, "height", None))
        or isinstance(toolbar, dict)
        and (toolbar.get("menu") is not None or toolbar.get("panel"))
    ):
        return None
    view = _view(app)
    if frame.get("preference", (view["dock"], view["enabled"], view["ratio"])) != (
        view["dock"],
        view["enabled"],
        view["ratio"],
    ):
        return None
    return frame
