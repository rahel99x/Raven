"""Shared terminal-workbench extension points, without a second event loop."""

from __future__ import annotations

from functools import lru_cache
from types import MappingProxyType

from . import column_resize

# This input-only feature registers its capture callback during composition.
from . import text_selection as text_selection
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
    "sidebar",
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
    """Finite UI composition: every core target is visible to the layer gate."""
    from . import historical_ui
    from . import startup
    from .terminal import ui as terminal_ui
    from .jobs import group_menu as job_group_menu
    from .jobs import group_drag as job_group_drag
    from . import group_queue_drag
    from . import grouping_toolbar
    from .job_logs import history_export as history_log_export
    from . import data_export_ui
    from . import refresh_rate
    from . import worker_ui
    from . import toolbar
    from . import sidebar
    from . import onboarding_ui
    from .launching import wizard_ui as launch_wizard_ui
    from . import setup_ui
    from . import telemetry_settings_ui
    from . import notification_ui
    from .ui import allocation as raven_ui
    from . import scrollbars
    from . import pane_drag
    from . import metric_live
    from . import chart_interaction
    from . import history_browser
    from .jobs import selection as job_selection
    from . import interaction
    from . import scrolling
    from . import recent_history
    from .jobs import panels as job_panels
    from . import analytics_document
    from . import workspace_layout
    from . import navigation_ui
    from . import command_ui
    from . import navigation_tools
    from . import table_ui
    from . import table_tools
    from . import activity_ui
    from . import session_tools
    from . import project_ui
    from . import connect_data_ui
    from . import run_home_ui
    from .job_logs import workbench as log_workbench
    from .job_logs import tools as log_tools
    from . import analysis_ui
    from . import execution_ui
    from .jobs import progress as job_progress
    from . import telemetry_ui
    from . import shell_checks_ui
    from . import array_manifest_ui
    from . import gpu_provider_ui
    from .ui import operations as ops_ui
    from .tracing import ui as trace_ui

    return (
        historical_ui,
        startup,
        terminal_ui,
        job_group_menu,
        job_group_drag,
        group_queue_drag,
        grouping_toolbar,
        history_log_export,
        data_export_ui,
        refresh_rate,
        worker_ui,
        toolbar,
        sidebar,
        onboarding_ui,
        launch_wizard_ui,
        setup_ui,
        telemetry_settings_ui,
        notification_ui,
        raven_ui,
        scrollbars,
        pane_drag,
        metric_live,
        chart_interaction,
        history_browser,
        job_selection,
        interaction,
        scrolling,
        recent_history,
        job_panels,
        analytics_document,
        workspace_layout,
        navigation_ui,
        command_ui,
        navigation_tools,
        table_ui,
        table_tools,
        activity_ui,
        session_tools,
        project_ui,
        connect_data_ui,
        run_home_ui,
        log_workbench,
        log_tools,
        analysis_ui,
        execution_ui,
        job_progress,
        telemetry_ui,
        shell_checks_ui,
        array_manifest_ui,
        gpu_provider_ui,
        ops_ui,
        trace_ui,
    )


# Mode-to-owner is an immutable composition contract, not handler probing.
# Feature-local states still own their validation, cancellation and persistence.
MODE_FEATURES = MappingProxyType(
    {
        **INSPECTION_MODES,
        "job_group_menu": "job_group_menu",
        "sidebar": "sidebar",
        "sidebar_menu": "sidebar",
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
    features = modules()
    if len(features) != len(FEATURES) or len(set(FEATURES)) != len(FEATURES):
        raise RuntimeError(
            "Feature composition needs one module for each stable feature key"
        )
    return MappingProxyType(dict(zip(FEATURES, features)))


@lru_cache(maxsize=1)
def owners():
    registered = {
        name: Owner(
            name,
            getattr(feature, "handle_key", None),
            getattr(feature, "handle_mouse", None),
            getattr(feature, "paste", None),
        )
        for name, feature in module_map().items()
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
    from .ui import modal_pointer

    if modal_pointer.continuation(app, button) and modal_pointer.route(
        app, frame, y, x, button=button, shift=shift, current=current
    ):
        return True
    if frame is None:
        return False
    if not current and button in ("left", "press"):
        if frame.owner.name == "job_group_menu":
            module_map()["job_group_menu"].close(app)
            app.say("Job view changed; reopen actions to review the current targets")
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
    if button == "right" and frame.owner.name == MODE_FEATURES.get(
        getattr(app, "mode", "main")
    ):
        # Foreground documents retain their local clear/reset semantics. A
        # toolbar above them still owns input through its separate focus frame.
        from .jobs.selection import context_click

        modal_pointer.initialize(app)["capture"] = None
        if context_click(app, y, x, button=button, shift=shift):
            return True
    if modal_pointer.route(
        app, frame, y, x, button=button, shift=shift, current=current
    ):
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
    text_selection.initialize(app)
    bind_pointer(app)
    bind_terminal(app)
    bind_actions(app)
    bind_navigation(app)
    bind_execution(app)
    bind_trace_browser(app)


def bind_terminal(app):
    """Compose concrete terminal navigation and presentation capabilities."""
    from .terminal.ports import TerminalNavigation, TerminalPresentation
    from .ui import operations as ops_ui
    from . import project_ui
    from . import workspace_layout
    from . import interaction
    from .pointer_routing import active, begin_gesture
    from .ui.control_feedback import decorate

    def allocation(target, feature, values):
        ops_ui._open(target, feature)
        ops_ui.initialize(target)["values"].update(values)
        ops_ui._inspect(target)

    def acceptance(target, values):
        ops_ui.run_command(target, ["ops", "open", "acceptance"])
        ops_ui.initialize(target)["values"].update(values)

    app.terminal_navigation = TerminalNavigation(
        maximize=lambda target: workspace_layout.run_command(target, ["maximize"]),
        open_allocation=allocation,
        open_acceptance=acceptance,
        open_project=lambda target, path: project_ui.run_command(
            target, ["project", path]
        ),
        preview_artifact=lambda target, node: project_ui._preview(target, node),
    )
    app.terminal_presentation = TerminalPresentation(
        decorate=decorate,
        pointer_active=active,
        begin_gesture=begin_gesture,
        pointer_motion=interaction.handle_mouse,
        wrap_workspace=workspace_layout.wrap_terminal_workspace,
    )


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
    for name, feature in module_map().items():
        callback = getattr(feature, "restore", None)
        if callback:
            callback(app, data.get(name, {}))


def save(app):
    return {
        name: feature.save(app)
        for name, feature in module_map().items()
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
    for name, feature in module_map().items():
        if name == "startup":
            # Welcome animation is painted beneath ordinary modals by curses.
            continue
        rows = feature.overlay(views, snap, app, width, height)
        if name == "toolbar":
            toolbar_rows = rows
            app.toolbar_overlay_rows = rows or []
            continue
        if rows is not None:
            # A persistent menu keeps an underlying prompt/review visible.
            # The toolbar paints last and owns pointer input until dismissed.
            app.content_overlay_rows = rows
            return rows + (toolbar_rows or [])
    return toolbar_rows


def bind_navigation(app):
    """Compose peer-form effects above the independent feature implementations."""
    from . import array_manifest_ui
    from . import data_export_ui
    from . import historical_ui
    from .jobs import selection as job_selection
    from .launching import setup_ui as launch_setup_ui
    from .launching import wizard_ui as launch_wizard_ui
    from .job_logs import tools as log_tools
    from . import navigation_ui
    from . import onboarding_ui
    from . import project_ui
    from . import run_home_ui
    from . import setup_ui
    from . import telemetry_settings_ui
    from .ui.navigation_ports import (
        ExportBackground,
        FormNavigation,
        ProjectBindingEffects,
        ResearchNavigation,
    )

    def bind_run(target, root, selected):
        project_ui.initialize(target).update(root=root, registered_root=root)
        return project_ui._apply_run(target, selected)

    app.form_navigation = FormNavigation(
        telemetry=lambda target: telemetry_settings_ui.open_settings(target),
        run_home=lambda target, binding: run_home_ui.open_run(target, binding=binding),
        relaunch=lambda target, draft: launch_wizard_ui.open_from_run(target, draft),
        advanced_launch=lambda target, root: launch_setup_ui.open_setup(
            target, root=root
        ),
        launch_receipt=lambda target, receipt: run_home_ui.open_receipt(
            target, receipt
        ),
        outputs=lambda target: project_ui.run_command(target, ["outputs"]),
        export_run=lambda target: data_export_ui.open_export(target),
        historical=lambda target: historical_ui.run_command(target, ["asof"]),
        connections=lambda target: onboarding_ui.run_command(target, ["profiles"]),
        allocation=lambda target: setup_ui.run_command(target, ["setup", "allocation"]),
        recordings=lambda target: setup_ui.run_command(target, ["setup", "telemetry"]),
        diagnostics=lambda target: setup_ui.run_command(
            target, ["setup", "installation"]
        ),
    )
    app.project_binding_effects = ProjectBindingEffects(
        before_log_source_change=lambda target: log_tools.before_source_change(target),
        detach_array=lambda target: array_manifest_ui.detach_automatic(target),
        apply_array=lambda target, selected: array_manifest_ui.apply_automatic(
            target, selected
        ),
        bind_run=bind_run,
        clear_binding=lambda target, **kwargs: project_ui.clear_binding(
            target, **kwargs
        ),
    )
    app.research_navigation = ResearchNavigation(
        record=lambda target, view, **kwargs: navigation_ui.record(
            target, view, **kwargs
        ),
        resume=lambda target, view: job_selection.resume(target, view),
    )
    app.export_background = ExportBackground(
        queue=lambda target, label, work, complete: data_export_ui._queue(
            target, label, work, complete
        )
    )


def bind_actions(app):
    """Bind neighboring feature actions once, retaining native owner checks."""
    from .ui.owner_ports import (
        SelectionActions,
        InteractionSources,
        PanelActions,
        ChartActions,
        ListActions,
    )
    from .jobs import selection as job_selection
    from . import history_browser
    from . import chart_interaction
    from .job_logs import tools as log_tools
    from . import analysis_ui
    from .job_logs import history_export as history_log_export
    from . import toolbar
    from . import sidebar
    from .jobs import group_menu as job_group_menu
    from .jobs import list_menu as job_list_menu
    from .ui import operations as ops_ui
    from .jobs import panels as job_panels
    from . import data_export_ui
    from . import grouping_toolbar
    from . import table_ui

    # Resolve these explicit capabilities through their owner at invocation so
    # embedders can replace a handler without rebuilding unrelated UI state.
    app.selection_actions = SelectionActions(
        selection_mouse=lambda *args, **kwargs: job_selection.handle_mouse(
            *args, **kwargs
        ),
        selection_key=lambda *args, **kwargs: job_selection.handle_key(*args, **kwargs),
        history_activate=lambda *args, **kwargs: history_browser.activate(
            *args, **kwargs
        ),
        graph_mouse=lambda *args, **kwargs: chart_interaction.handle_mouse(
            *args, **kwargs
        ),
        graph_hover=lambda *args, **kwargs: chart_interaction.hover(*args, **kwargs),
        log_mouse=lambda *args, **kwargs: log_tools.handle_mouse(*args, **kwargs),
        analysis_context=lambda *args, **kwargs: analysis_ui.context_click(
            *args, **kwargs
        ),
        history_export_mouse=lambda *args, **kwargs: history_log_export.handle_mouse(
            *args, **kwargs
        ),
    )
    app.interaction_sources = InteractionSources(
        toolbar_descriptors=lambda target: toolbar.control_descriptors(target),
        sidebar_controls=lambda target: sidebar.controls(target),
        group_menu_active=lambda target: job_group_menu.active(target),
        group_menu_controls=lambda target: job_group_menu.controls(target),
        list_menu_active=lambda target: job_list_menu.active(target),
        list_menu_controls=lambda target: job_list_menu.controls(target),
        history_export_active=lambda target: history_log_export.active(target),
        history_export_controls=lambda target: history_log_export.controls(target),
        pin_focused=lambda target, control: ops_ui.pin_focused(target, control),
    )
    app.panel_actions = PanelActions(
        activate=lambda *args, **kwargs: job_panels._activate(*args, **kwargs)
    )
    app.chart_actions = ChartActions(
        open_export=lambda *args, **kwargs: data_export_ui.open_export(*args, **kwargs)
    )
    app.list_actions = ListActions(
        grouping_command=lambda target, args: grouping_toolbar.run_command(
            target, args
        ),
        table_command=lambda target, args: table_ui.run_command(target, args),
    )


def bind_execution(app):
    """Compose reviewed-plan navigation without peer feature imports below it."""
    from . import shell_checks_ui
    from .ui.execution_ports import ExecutionNavigation

    app.execution_navigation = ExecutionNavigation(
        open_shell_checks=lambda target, plan: shell_checks_ui.open_for_plan(
            target, plan
        )
    )


def bind_trace_browser(app):
    """Bind exact-attempt browser completion checks above its request handler."""
    from .tracing import ui as trace_ui
    from .ui.trace_ports import TraceBrowserEffects

    app.trace_browser_effects = TraceBrowserEffects(
        current_context=lambda target: trace_ui._context(target),
        cancel_reader=lambda target: trace_ui.cancel(target),
    )


def bind_pointer(app):
    """Compose the finite pointer owners without process-global registration."""
    from .pointer_routing import bind, binding_for

    features = module_map()
    handlers = (
        features["sidebar"].handle_mouse,
        column_resize.handle_mouse,
        features["group_queue_drag"].handle_mouse,
        features["job_group_drag"].handle_mouse,
        features["scrollbars"].handle_mouse,
        features["toolbar"].handle_mouse,
        text_selection.handle_mouse,
        features["job_selection"].handle_mouse,
        features["pane_drag"].handle_mouse,
        features["history_browser"].handle_mouse,
        features["metric_live"].handle_mouse,
        features["chart_interaction"].handle_mouse,
    )
    bind(
        app,
        tuple(binding_for(handler) for handler in handlers),
        pointer_motion=features["interaction"].handle_mouse,
    )
