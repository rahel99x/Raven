"""Published text-selection state and explicit in-memory reset operations."""

from __future__ import annotations
from collections import OrderedDict


def initialize(app):
    state = getattr(app, "text_selection_state", None)
    if not isinstance(state, dict):
        state = {
            "panes": (),
            "rows": {},
            "visible": {},
            "cache": OrderedDict(),
            "cache_bytes": 0,
            "selection": None,
            "capture": None,
            "cursor": None,
            "explicit": False,
            "token": None,
            "frame_required": False,
            "discard_release": False,
            "invalidated": False,
        }
        app.text_selection_state = state
    return state


def _context(app):
    panels = getattr(app, "job_panel_state", {})
    project = getattr(app, "project_state", {})
    mode, tab = getattr(app, "mode", "main"), getattr(app, "tab", "")
    # Automatic project discovery can finish during a Cluster copy gesture.
    # Its scheduler counters do not change source when that unrelated root is
    # published. Project-dependent pages and modals retain the root guard.
    project_root = None if (mode, tab) == ("main", "cluster") else project.get("root")
    return (
        mode,
        tab,
        getattr(app, "analytics_view", None),
        getattr(app, "research_view", None),
        getattr(app, "nodes_view", None),
        getattr(app, "analytics_job", None),
        getattr(app, "research_job_id", None),
        getattr(app, "selected_id", None),
        panels.get("mode"),
        panels.get("view"),
        panels.get("analytics_view"),
        panels.get("research_view"),
        project_root,
    )


def _token(app):
    toolbar = getattr(app, "toolbar_state", {})
    return (
        _context(app),
        getattr(app, "width", 0),
        getattr(app, "height", 0),
        toolbar.get("menu"),
        toolbar.get("panel"),
    )


def clear(app):
    state = initialize(app)
    if state["capture"]:
        state["discard_release"] = True
    state.update(
        selection=None, capture=None, explicit=False, cursor=None, invalidated=False
    )


def active(app):
    return bool(initialize(app)["capture"])


def selected(app):
    return bool(initialize(app)["selection"])
