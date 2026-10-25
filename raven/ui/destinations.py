"""Shared, bounded navigation identities; never selected jobs or source paths."""

from __future__ import annotations

from .catalog import ANALYTICS_VIEWS, NODES_VIEWS, RESEARCH_VIEWS, TABS

MAX_FAVORITES = 24


def destinations():
    return {
        **{"tab:" + key: (title, ("tab " + key,)) for key, title in TABS},
        **{
            "research:" + key: ("Research: " + title, ("workspace " + key,))
            for key, title in RESEARCH_VIEWS
        },
        **{
            "analytics:" + key: (
                "Analytics: " + title.title(),
                ("tab analytics", "view " + key),
            )
            for key, title in ANALYTICS_VIEWS
        },
        **{
            "nodes:" + key: ("Nodes: " + title.title(), ("tab nodes", "view " + key))
            for key, title in NODES_VIEWS
        },
        "trace:live": ("Traces: Live", ("traces", "trace workspace live")),
        "trace:fit": ("Traces: Fit", ("traces", "trace workspace fit")),
    }


def valid_favorites(values):
    known = destinations()
    return (
        list(
            dict.fromkeys(
                value
                for value in values[:MAX_FAVORITES]
                if isinstance(value, str) and value in known
            )
        )
        if isinstance(values, (list, tuple))
        else []
    )


def current(app):
    tab = getattr(app, "tab", "jobs")
    if tab == "research":
        return "research:" + getattr(app, "research_view", "experiment")
    if tab == "analytics":
        return "analytics:" + getattr(app, "analytics_view", "job")
    if tab == "nodes":
        return "nodes:" + getattr(app, "nodes_view", "mine")
    if tab == "traces":
        view = getattr(app, "trace_panel_state", {}).get("workspace_view", {})
        if view.get("mode") == "live":
            return "trace:live"
    return "tab:" + tab


def children(tab):
    if tab == "research":
        return tuple(("research:" + key, title) for key, title in RESEARCH_VIEWS)
    if tab == "analytics":
        return tuple(
            ("analytics:" + key, title.title()) for key, title in ANALYTICS_VIEWS
        )
    if tab == "nodes":
        return tuple(("nodes:" + key, title.title()) for key, title in NODES_VIEWS)
    if tab == "traces":
        return (("trace:live", "Live"), ("trace:fit", "Fit"))
    return ()
