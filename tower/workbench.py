"""Shared terminal-workbench extension points, without a second event loop."""
from __future__ import annotations

import importlib
from functools import lru_cache

INSPECTION_MODES = {"telemetry": "telemetry_ui", "shell_checks": "shell_checks_ui",
                    "arraymap": "array_manifest_ui", "gpu_provider": "gpu_provider_ui",
                    "operations": "ops_ui", "raven": "raven_ui"}

FEATURES = ("startup", "terminal_ui", "job_group_menu", "job_group_drag", "group_queue_drag", "grouping_toolbar", "history_log_export", "refresh_rate", "worker_ui", "toolbar", "raven_ui", "scrollbars", "pane_drag", "metric_live", "chart_interaction", "history_browser", "job_selection", "interaction", "scrolling", "recent_history", "job_panels", "analytics_document", "workspace_layout", "navigation_ui", "command_ui", "navigation_tools",
            "table_ui", "table_tools", "activity_ui", "session_tools", "project_ui",
            "log_workbench", "log_tools", "analysis_ui", "execution_ui", "job_progress",
            "telemetry_ui", "shell_checks_ui", "array_manifest_ui", "gpu_provider_ui", "ops_ui", "trace_ui")


@lru_cache(maxsize=1)
def modules():
    return tuple(importlib.import_module("tower." + name) for name in FEATURES)


def initialize(app):
    for feature in modules():
        feature.initialize(app)


def tick(app):
    """Publish and request bounded feature work from the existing UI loop."""
    from .ui_persistence import tick as tick_preferences
    tick_preferences(app)
    for feature in modules():
        callback = getattr(feature, "tick", None)
        if callback:
            callback(app)


def restore(app, data):
    data = data if isinstance(data, dict) else {}
    for feature in modules():
        callback = getattr(feature, "restore", None)
        if callback:
            callback(app, data.get(feature.__name__.rsplit(".", 1)[-1], {}))


def save(app):
    return {feature.__name__.rsplit(".", 1)[-1]: feature.save(app)
            for feature in modules() if hasattr(feature, "save")}


def command_names():
    return sorted({name for feature in modules() for name in feature.command_names()})


def handle_key(app, key):
    if app.mode == "terminal_probe":
        from .session_tools import handle_key as probe_key
        return probe_key(app, key)
    return any(feature.handle_key(app, key) for feature in modules())


def handle_mouse(app, y, x, button="left", shift=False):
    if app.mode == "terminal_probe":
        from .session_tools import handle_mouse as probe_mouse
        return probe_mouse(app, y, x, button=button, shift=shift)
    for feature in modules():
        callback = getattr(feature, "handle_mouse", None)
        if callback and callback(app, y, x, button=button, shift=shift):
            return True
    # Modal overlays never click through into a hidden table or job action.
    return app.mode not in ("main",)


def run_command(app, args):
    return any(feature.run_command(app, args) for feature in modules())


def overlay(views, snap, app, width, height):
    toolbar_rows = None
    app.content_overlay_rows, app.toolbar_overlay_rows = [], []
    for feature in modules():
        if feature.__name__ == "tower.startup":
            # Welcome animation is painted beneath ordinary modals by curses.
            continue
        rows = feature.overlay(views, snap, app, width, height)
        if feature.__name__ == "tower.toolbar":
            toolbar_rows = rows
            app.toolbar_overlay_rows = rows or []
            continue
        if rows is not None:
            # A persistent menu keeps an underlying prompt/review visible.
            # The toolbar paints last and owns pointer input until dismissed.
            app.content_overlay_rows = rows
            return rows + (toolbar_rows or [])
    return toolbar_rows
