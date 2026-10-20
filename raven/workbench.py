"""Shared terminal-workbench extension points, without a second event loop."""

from __future__ import annotations

import importlib
from functools import lru_cache
from types import MappingProxyType

from . import column_resize
from .ui.focus import FocusStack, Frame, Owner

INSPECTION_MODES = {
    "historical": "historical_ui",
    "telemetry": "telemetry_ui",
    "shell_checks": "shell_checks_ui",
    "arraymap": "array_manifest_ui",
    "gpu_provider": "gpu_provider_ui",
    "operations": "ops_ui",
    "raven": "raven_ui",
    "setup": "setup_ui",
    "notifications": "notification_ui",
    "data_export": "data_export_ui",
    "telemetry_settings": "telemetry_settings_ui",
    "onboarding": "onboarding_ui",
    "launch_job": "launch_wizard_ui",
    "launch_setup": "data_export_ui",
    "job_list_menu": "grouping_toolbar",
    "connect_data": "connect_data_ui",
    "run_home": "run_home_ui",
}

FEATURES = (
    "historical_ui",
    "startup",
    "terminal_ui",
    "job_group_menu",
    "job_group_drag",
    "group_queue_drag",
    "grouping_toolbar",
    "history_log_export",
    "data_export_ui",
    "refresh_rate",
    "worker_ui",
    "toolbar",
    "onboarding_ui",
    "launch_wizard_ui",
    "setup_ui",
    "telemetry_settings_ui",
    "notification_ui",
    "raven_ui",
    "scrollbars",
    "pane_drag",
    "metric_live",
    "chart_interaction",
    "history_browser",
    "job_selection",
    "interaction",
    "scrolling",
    "recent_history",
    "job_panels",
    "analytics_document",
    "workspace_layout",
    "navigation_ui",
    "command_ui",
    "navigation_tools",
    "table_ui",
    "table_tools",
    "activity_ui",
    "session_tools",
    "project_ui",
    "connect_data_ui",
    "run_home_ui",
    "log_workbench",
    "log_tools",
    "analysis_ui",
    "execution_ui",
    "job_progress",
    "telemetry_ui",
    "shell_checks_ui",
    "array_manifest_ui",
    "gpu_provider_ui",
    "ops_ui",
    "trace_ui",
)


@lru_cache(maxsize=1)
def modules():
    return tuple(importlib.import_module("raven." + name) for name in FEATURES)


# Mode-to-owner is an immutable composition contract, not handler probing.
# Feature-local states still own their validation, cancellation and persistence.
MODE_FEATURES = MappingProxyType(
    {
        **INSPECTION_MODES,
        "job_group_menu": "job_group_menu",
        "analysis": "analysis_ui",
        "workspace_picker": "navigation_ui",
        "activity": "activity_ui",
        "exports": "activity_ui",
        "export_preview": "activity_ui",
        "session_inbox": "session_tools",
        "session_alerts": "session_tools",
        "terminal_diagnostics": "session_tools",
        "terminal_probe": "session_tools",
        "jump_picker": "navigation_tools",
        "settings_editor": "navigation_tools",
        "bindings_editor": "navigation_tools",
        "field_explanation": "navigation_tools",
        "value_peek": "navigation_tools",
        "locations_picker": "navigation_tools",
        "table_tools": "table_tools",
        "layout": "workspace_layout",
        "columns": "table_ui",
        "project_runs": "project_ui",
        "project_outputs": "project_ui",
        "project_preview": "project_ui",
        "execution": "execution_ui",
        "log_tools_page": "log_tools",
        "log_tools_results": "log_tools",
        "log_tools_marks": "log_tools",
        "help": "command_ui",
        "palette": "command_ui",
        "confirm": "command_ui",
        **{
            "history_log_" + stage: "history_log_export"
            for stage in (
                "menu",
                "picker",
                "mkdir",
                "busy",
                "missing",
                "receipt",
                "error",
            )
        },
    }
)


@lru_cache(maxsize=1)
def module_map():
    return MappingProxyType(
        {module.__name__.rsplit(".", 1)[-1]: module for module in modules()}
    )


@lru_cache(maxsize=1)
def owners():
    registered = {
        feature.__name__.rsplit(".", 1)[-1]: Owner(
            feature.__name__.rsplit(".", 1)[-1],
            getattr(feature, "handle_key", None),
            getattr(feature, "handle_mouse", None),
            getattr(feature, "paste", None),
        )
        for feature in modules()
    }
    # Column resizing is an input-only owner, with no command/overlay feature.
    registered["column_resize"] = Owner(
        "column_resize", column_resize.handle_key, column_resize.handle_mouse
    )
    return registered


def _focus(app, *, keyboard=False):
    stack = getattr(app, "focus_stack", None)
    if not isinstance(stack, FocusStack):
        stack = app.focus_stack = FocusStack()
    mode = getattr(app, "mode", "main")
    context = (mode, getattr(app, "tab", ""))
    name = MODE_FEATURES.get(mode)
    registered = owners()
    frames = [Frame(registered[name], context)] if name else []
    if not keyboard and mode != "main" and not frames:
        frames.append(Frame(Owner("controller-dialog"), context))
    toolbar = getattr(app, "toolbar_state", {})
    if toolbar.get("menu") is not None or toolbar.get("panel"):
        frames.append(
            Frame(
                registered["toolbar"],
                context + (toolbar.get("menu"), toolbar.get("panel")),
            )
        )
    if keyboard:
        # Pointer captures consume their keys only while the captured gesture is
        # live. This is a fixed small state lookup, never a walk of UI handlers.
        for feature, state_name in (
            ("scrollbars", "scrollbar_state"),
            ("job_group_drag", "job_group_drag_state"),
            ("group_queue_drag", "group_queue_drag_state"),
            ("column_resize", "column_resize_state"),
        ):
            state = getattr(app, state_name, {})
            capture = state.get("capture")
            if capture:
                frames.append(Frame(registered[feature], context + (id(capture),)))
                break
        terminal = registered["terminal_ui"]
        feature = module_map()["terminal_ui"]
        if feature.focused(app):
            frames.append(Frame(terminal, context + feature.input_identity(app)))
    stack.publish(frames)
    return stack


def route_modal_key(app, key):
    stack = _focus(app, keyboard=True)
    frame = stack.top
    protected_interrupt = {
        "historical_ui",
        "terminal_ui",
        "column_resize",
        "group_queue_drag",
        "job_group_drag",
        "scrollbars",
        "job_group_menu",
        "history_log_export",
        "data_export_ui",
    }
    binding_test = getattr(app, "mode", "main") == "bindings_editor" and getattr(
        app, "navigation_tools_state", {}
    ).get("test")
    if (
        key == "ctrl-c"
        and frame is not None
        and frame.owner.name not in protected_interrupt
        and app.mode != "terminal_probe"
        and not binding_test
        and key not in app.keymap
    ):
        app.quit = True
        return True
    if key == "f10" and (frame is None or frame.owner.name != "terminal_ui"):
        owners()["toolbar"].key(app, key)
        return True
    if frame is not None and frame.owner.name == MODE_FEATURES.get(
        getattr(app, "mode", "main")
    ):
        # Selection tools are part of the foreground document, not a covered
        # owner. Text entry modes reject them through their existing contract.
        from .text_selection import handle_key as document_key

        if document_key(app, key):
            return True
        # Charts, polling controls and accessible controls are tools of this
        # foreground document. Each validates its published frame and attempt;
        # none receives a covered dialog's input through handler fallthrough.
        for tool in ("metric_live", "chart_interaction"):
            callback = owners()[tool].key
            if callback is not None and callback(app, key):
                return True
        if owners()["interaction"].key(app, key):
            return True
        palette_modes = {
            "analysis",
            "session_alerts",
            "terminal_diagnostics",
            "log_tools_page",
            "log_tools_results",
            "log_tools_marks",
            "project_preview",
            "export_preview",
            "locations_picker",
            "value_peek",
            "field_explanation",
            "layout",
            "columns",
        }
        list_mode = (
            app.mode == "session_inbox"
            and not app.session_tools_state.get("filtering")
            or app.mode in ("activity", "exports")
            and not app.activity.filtering
            or app.mode == "table_tools"
            and app.table_tools_state.get("edit") is None
        )
        if key == ":" and (app.mode in palette_modes or list_mode):
            from .command_ui import open_palette

            open_palette(app)
            return True
    return stack.key(app, key)


def route_modal_mouse(app, y, x, *, button="left", shift=False, current=True):
    stack = _focus(app)
    frame = stack.top
    if frame is None:
        return False
    if not current and button in ("left", "press"):
        return True
    # A modal scrollbar belongs to the same foreground frame. No background
    # list, graph or job action receives input after the frame claims it.
    scrollbar = owners()["scrollbars"].mouse
    if scrollbar is not None and scrollbar(app, y, x, button=button, shift=shift):
        return True
    # Only a document that actually exposes chart controls owns these tools.
    # Other overlays cover the plots, including passive pointer motion; do not
    # even dispatch hover to the hidden document and rely on callback refusal.
    if not module_map()["chart_interaction"]._blocked(app):
        for tool in ("metric_live", "chart_interaction"):
            callback = owners()[tool].mouse
            if callback is not None and callback(app, y, x, button=button, shift=shift):
                return True
    return stack.mouse(app, y, x, button=button, shift=shift)


def paste(app, text):
    """One owned paste destination; fallback only edits the command palette."""
    name = MODE_FEATURES.get(getattr(app, "mode", "main"), "command_ui")
    feature = module_map().get(name)
    callback = getattr(feature, "paste", None)
    if callback is None:
        callback = getattr(module_map()["command_ui"], "paste", None)
    return callback(app, text)


def initialize(app):
    from .ui.research_binding import INTERACTION

    app.research_interaction = INTERACTION
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
    return {
        feature.__name__.rsplit(".", 1)[-1]: feature.save(app)
        for feature in modules()
        if hasattr(feature, "save")
    }


def command_names():
    return sorted({name for feature in modules() for name in feature.command_names()})


def handle_key(app, key, *, modal=True):
    if modal and route_modal_key(app, key):
        return True
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
    if not args:
        return False
    from .command_registry import command_owner

    owner = command_owner(args[0], args[1:])
    feature = module_map().get(owner)
    return bool(feature and feature.run_command(app, args))


def overlay(views, snap, app, width, height):
    toolbar_rows = None
    app.content_overlay_rows, app.toolbar_overlay_rows = [], []
    for feature in modules():
        if feature.__name__ == "raven.startup":
            # Welcome animation is painted beneath ordinary modals by curses.
            continue
        rows = feature.overlay(views, snap, app, width, height)
        if feature.__name__ == "raven.toolbar":
            toolbar_rows = rows
            app.toolbar_overlay_rows = rows or []
            continue
        if rows is not None:
            # A persistent menu keeps an underlying prompt/review visible.
            # The toolbar paints last and owns pointer input until dismissed.
            app.content_overlay_rows = rows
            return rows + (toolbar_rows or [])
    return toolbar_rows
