"""Workspace-owned Details preferences and published panel geometry queries."""

from __future__ import annotations
from .catalog import RESEARCH_VIEWS, ANALYTICS_VIEWS

TABS = (
    ("inspector", "Inspector"),
    ("logs", "Logs"),
    ("investigate", "Investigate"),
    ("research", "Research"),
    ("analytics", "Analytics"),
    ("quick", "Quick Advisor"),
    ("trace", "Trace"),
    ("off", "Off"),
)

MODES = tuple(name for name, _ in TABS)

DETAIL_TABS = ("jobs", "history")


def _preferences(data):
    result = {
        "mode": "inspector",
        "research_view": "experiment",
        "analytics_view": "job",
    }
    if isinstance(data, dict):
        if data.get("mode") in MODES:
            result["mode"] = data["mode"]
        for group in ("research", "analytics"):
            if data.get(group + "_view") in dict(_choices(group)):
                result[group + "_view"] = data[group + "_view"]
    return result


def initialize(app):
    state = getattr(app, "job_panel_state", None)
    if not isinstance(state, dict):
        state = dict(
            mode="inspector",
            focus="",
            job=None,
            file_id=None,
            entries=[],
            files=None,
            session=None,
            catalog=None,
        )
        app.job_panel_state = state
    state.setdefault("research_view", "experiment")
    state.setdefault("analytics_view", "job")
    state.setdefault("scrolls", {})
    state.setdefault("view_states", {})
    state.setdefault("document_windows", {})
    state.setdefault("document_maps", {})
    state.setdefault("document_headers", {})
    state.setdefault("workspace", "jobs")
    workspaces = state.setdefault("workspaces", {})
    tab = getattr(app, "tab", "jobs")
    if tab in DETAIL_TABS and tab != state["workspace"]:
        # Mode choices belong to a workspace. Readers remain session-owned;
        # changing the page clears their exact-job attachment before use.
        workspaces[state["workspace"]] = _preferences(state)
        state.update(_preferences(workspaces.get(tab)))
        state.update(workspace=tab, focus="", job=None, file_id=None, entries=[])
        for key in (
            "scrolls",
            "view_states",
            "document_windows",
            "document_maps",
            "document_headers",
        ):
            state[key].clear()
    return state


def scroll_key(app):
    """Use the owning workspace for all inline document scroll coordinates."""
    return ("history" if getattr(app, "tab", "") == "history" else "jobs") + ":details"


def _view_key(state):
    mode = state["mode"]
    return (
        mode + ":" + state[mode + "_view"]
        if mode in ("research", "analytics")
        else mode
    )


def _choices(group):
    if group == "research":
        return RESEARCH_VIEWS
    if group == "analytics":
        return [(key, label.title()) for key, label in ANALYTICS_VIEWS]
    return ()


def context(app):
    state = getattr(app, "job_panel_state", {})
    selected = getattr(app, "selected_id", None)
    attempt = getattr(getattr(app, "store", None), "job_attempt", None)
    return (
        getattr(app, "tab", ""),
        getattr(app, "mode", "main"),
        getattr(app, "width", None),
        getattr(app, "height", None),
        selected,
        state.get("mode"),
        state.get("research_view"),
        state.get("analytics_view"),
        attempt(str(selected)) if selected is not None and callable(attempt) else None,
    )


def contains(app, y, x):
    published = getattr(app, "job_panel_context", None)
    if published is not None:
        # Geometry still blocks table hits during selection changes and routes
        # modal wheel input through the controller. Only controls require an
        # exact mode/job/view match; page and terminal-size changes hide bounds.
        current_geometry = (
            getattr(app, "tab", ""),
            (getattr(app, "width", None), getattr(app, "height", None)),
        )
        if (published[0], published[2:4]) != current_geometry:
            return False
    rect = getattr(app, "job_panel_rect", None)
    return bool(
        getattr(app, "tab", "") in DETAIL_TABS
        and rect
        and rect.x <= x < rect.x + rect.width
        and rect.y <= y < rect.y + rect.height
    )
