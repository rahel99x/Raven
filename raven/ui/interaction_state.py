"""Current painted control publication and bounded stale-context query tokens."""

from __future__ import annotations
from itertools import islice
from .control_graph import Rect, MAX_HIT_ITEMS, MAX_CONTROLS, _integer


def initialize(app):
    state = getattr(app, "interaction_state", None)
    if not isinstance(state, dict):
        state = {}
        app.interaction_state = state
    for key, value in (
        ("graph", None),
        ("pointer", None),
        ("hovered", None),
        ("focused", None),
        ("active", False),
        ("routing", False),
        ("generation", 0),
        ("pending_focus", None),
        ("frame_required", False),
    ):
        state.setdefault(key, value)
    return state


def _state(app, name):
    value = getattr(app, name, {})
    return value if isinstance(value, dict) else {}


def hit_token(hits):
    """Copy native hit payloads into a bounded, immutable frame identity.

    Controller activation must compare the registry that was painted, rather
    than a mutable ``last_hits`` alias. Unsupported or oversized payloads fail
    closed; this work runs at publication and explicit clicks, never hover.
    """
    remaining = MAX_HIT_ITEMS

    def freeze(value, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 8:
            raise ValueError("oversized hit payload")
        if isinstance(value, (str, int, float, bool, type(None))):
            return (type(value).__name__, value)
        if isinstance(value, (tuple, list)) and len(value) <= 128:
            return tuple(freeze(item, depth + 1) for item in value)
        if isinstance(value, dict) and len(value) <= 128:
            entries = tuple(
                (freeze(key, depth + 1), freeze(item, depth + 1))
                for key, item in value.items()
            )
            return ("mapping", tuple(sorted(entries, key=lambda item: item[0])))
        raise ValueError("unsupported hit payload")

    try:
        token = tuple(freeze(hit) for hit in islice(hits or (), MAX_CONTROLS + 1))
        return token if len(token) <= MAX_CONTROLS else None
    except (TypeError, ValueError, RecursionError):
        return None


def _scope(app):
    """Overlay identities are distinct from ordinary viewport/row changes."""
    toolbar = _state(app, "toolbar_state")
    analysis = _state(app, "analysis_state")
    mode = getattr(app, "mode", "main")
    return (
        mode,
        getattr(app, "tab", ""),
        toolbar.get("menu"),
        toolbar.get("panel"),
        analysis.get("modal"),
        _state(app, "table_tools_state").get("modal"),
        id(getattr(app, "confirm", None)) if mode == "confirm" else None,
        id(_state(app, "execution_state").get("review"))
        if mode == "execution"
        else None,
        tuple(
            _state(app, "ops_state").get(key)
            for key in ("generation", "view", "editing")
        )
        if mode == "operations"
        else None,
        tuple(
            _state(app, "setup_ui_state").get(key)
            for key in ("page", "generation", "revision", "editing")
        )
        if mode == "setup"
        else None,
        tuple(
            _state(app, "telemetry_settings_ui_state").get(key)
            for key in ("generation", "revision", "page", "editing")
        )
        if mode == "telemetry_settings"
        else None,
        tuple(
            _state(app, "onboarding_ui_state").get(key)
            for key in ("generation", "revision", "page", "editing")
        )
        if mode == "onboarding"
        else None,
        tuple(
            _state(app, "launch_wizard_state").get(key)
            for key in ("generation", "revision", "stage", "editing")
        )
        if mode == "launch_job"
        else None,
        _guided_context(app),
        tuple(
            _state(app, "notification_ui_state").get(key)
            for key in ("severity", "selected", "details", "revision")
        )
        if mode == "notifications"
        else None,
        tuple(
            _state(app, "data_export_state").get(key)
            for key in ("generation", "revision", "stage", "editing")
        )
        if mode in ("data_export", "launch_setup")
        else None,
    )


def _viewport(app):
    """Bounded preference/offset identity; never inspect jobs or chart data."""
    tab = getattr(app, "tab", "")
    top, cursor = _state(app, "top"), _state(app, "cursor")
    table = _state(app, "table_state")
    layout = getattr(app, "layout_state", None)
    scroll = getattr(layout, "scroll", {})
    browser = _state(app, "history_browser_state").get("views", {}).get(tab, {})
    if not isinstance(browser, dict):
        browser = {}
    logs = getattr(app, "logs", None)
    recent = getattr(app, "recent_history_state", None)
    panel = _state(app, "job_panel_state")
    session = panel.get("session")
    analysis = _state(app, "analysis_state")
    project = _state(app, "project_state")
    from ..table_query import fingerprint

    tables = (tab, "recent") if tab == "jobs" else (tab,)
    return (
        tuple(
            (
                name,
                top.get(name),
                cursor.get(name),
                _state(app, "sort").get(name),
                _state(app, "reverse").get(name),
                fingerprint(app, name),
                tuple(islice(table.get("hidden", {}).get(name, ()), 64)),
                tuple(islice(table.get("order", {}).get(name, ()), 64)),
                tuple(
                    sorted(islice(table.get("widths", {}).get(name, {}).items(), 64))
                ),
            )
            for name in tables
        ),
        getattr(app, "filter", ""),
        table.get("groups"),
        tuple(islice(table.get("collapsed", ()), 256)),
        getattr(app, "manual_job_groups_revision", 0),
        getattr(layout, "density", None),
        getattr(layout, "ratio", None),
        getattr(layout, "maximized", None),
        scroll.get(tab + ":main"),
        scroll.get(tab + ":details"),
        browser.get("top"),
        browser.get("dock"),
        browser.get("ratio"),
        browser.get("enabled"),
        browser.get("selected"),
        browser.get("explicit"),
        getattr(recent, "ratio", None),
        getattr(recent, "manual_split", None),
        getattr(app, "research_scroll", None),
        getattr(app, "research_task_offset", None),
        getattr(app, "deps_scope_top", None),
        getattr(logs, "top", None),
        getattr(logs, "browser_top", None),
        _state(app, "analytics_document_state").get("top"),
        panel.get("file_id"),
        getattr(session, "path", None),
        getattr(session, "top", None),
        tuple(
            analysis.get(name)
            for name in (
                "scroll",
                "cursor",
                "section",
                "evidence_cursor",
                "zoom",
                "pan",
                "window",
                "preset",
                "chart_box",
                "chart_live_window",
            )
        ),
        tuple(
            project.get(name)
            for name in (
                "generation",
                "run_cursor",
                "run_top",
                "output_cursor",
                "output_top",
                "preview_scroll",
                "preview_page",
                "preview_column",
                "filter",
                "notices_open",
                "notices_scroll",
            )
        ),
    )


def _context(app):
    """Reject actions after a page, overlay, or selected-job identity changes."""
    toolbar = _state(app, "toolbar_state")
    analysis = _state(app, "analysis_state")
    panel = _state(app, "job_panel_state")
    project = _state(app, "project_state")
    table_tools = _state(app, "table_tools_state")
    execution = _state(app, "execution_state")
    binding = project.get("binding")
    binding = binding if isinstance(binding, dict) else {}
    logs = getattr(app, "logs", None)
    return (
        getattr(app, "mode", "main"),
        getattr(app, "tab", ""),
        getattr(app, "selected_id", None),
        getattr(app, "nodes_view", None),
        getattr(app, "analytics_view", None),
        getattr(app, "research_view", None),
        analysis.get("modal"),
        analysis.get("job"),
        analysis.get("chart_job"),
        analysis.get("metric"),
        panel.get("mode"),
        panel.get("view"),
        panel.get("research_view"),
        panel.get("analytics_view"),
        project.get("root"),
        getattr(logs, "browser", None),
        getattr(logs, "path", None),
        toolbar.get("menu"),
        toolbar.get("panel"),
        table_tools.get("modal"),
        table_tools.get("tab"),
        _state(app, "table_state").get("tab")
        if getattr(app, "mode", "main") == "columns"
        else None,
        id(getattr(app, "confirm", None))
        if getattr(app, "mode", "main") == "confirm"
        else None,
        id(execution.get("review"))
        if getattr(app, "mode", "main") == "execution"
        else None,
        repr(execution.get("pending_action"))[:512]
        if getattr(app, "mode", "main") == "execution"
        else None,
        _viewport(app),
        getattr(app, "analytics_job", None),
        getattr(app, "research_job_id", None),
        getattr(app, "log_job", None),
        getattr(app, "content_origin_x", 0),
        tuple(_state(app, "toolbar_state").get("favorites", ())),
        tuple(
            _state(app, "sidebar_state").get(key)
            for key in ("revision", "top", "order")
        ),
        tuple(
            binding.get(name)
            for name in ("project_root", "run_id", "job_id", "attempt")
        ),
        tuple(
            _state(app, "ops_state").get(key)
            for key in ("generation", "view", "editing")
        )
        if getattr(app, "mode", "main") == "operations"
        else None,
        tuple(
            _state(app, "setup_ui_state").get(key)
            for key in ("page", "generation", "revision", "editing")
        )
        if getattr(app, "mode", "main") == "setup"
        else None,
        tuple(
            _state(app, "telemetry_settings_ui_state").get(key)
            for key in ("generation", "revision", "page", "editing")
        )
        if getattr(app, "mode", "main") == "telemetry_settings"
        else None,
        tuple(
            _state(app, "onboarding_ui_state").get(key)
            for key in ("generation", "revision", "page", "editing")
        )
        if getattr(app, "mode", "main") == "onboarding"
        else None,
        tuple(
            _state(app, "launch_wizard_state").get(key)
            for key in ("generation", "revision", "stage", "editing")
        )
        if getattr(app, "mode", "main") == "launch_job"
        else None,
        _guided_context(app),
        _notification_context(app),
        tuple(
            _state(app, "data_export_state").get(key)
            for key in ("generation", "revision", "stage", "editing")
        )
        if getattr(app, "mode", "main") in ("data_export", "launch_setup")
        else None,
    )


def _notification_context(app):
    from .notification_state import context

    return context(app)


def _guided_context(app):
    """Scope new workflow controls to the exact painted page and revision."""
    mode = getattr(app, "mode", "")
    if mode == "workspace_picker":
        state = _state(app, "navigation_state")
        return tuple(state.get(key) for key in ("query", "picker_top"))
    if mode in (
        "jump_picker",
        "locations_picker",
        "settings_editor",
        "bindings_editor",
        "value_peek",
        "field_explanation",
    ):
        state = _state(app, "navigation_tools_state")
        return tuple(
            state.get(key)
            for key in ("query", "top", "editing", "test", "field", "index_at")
        )
    if mode in ("sidebar", "sidebar_menu"):
        state = _state(app, "sidebar_state")
        return state.get("revision"), state.get("top"), state.get("menu")
    name = {
        "connect_data": "connect_data_state",
        "run_home": "run_home_state",
        "job_list_menu": "job_list_menu_state",
    }.get(getattr(app, "mode", ""))
    if name is None:
        return None
    state = _state(app, name)
    return tuple(
        state.get(key)
        for key in (
            "generation",
            "revision",
            "token",
            "page",
            "view",
            "editing",
            "context",
            "section",
        )
    )


def _current(app):
    state = initialize(app)
    graph = state["graph"]
    if graph is None or graph.token != _context(app):
        return None
    # Screen size is optional for small embedders and headless renderers.
    for name, expected in zip(("width", "height"), graph.observed_geometry):
        actual = getattr(app, name, None)
        if actual != expected:
            return None
    return graph


def controls(app):
    graph = _current(app)
    return graph.controls if graph is not None else ()


def _pane_rect(value, width, height):
    if value is None:
        return None
    parts = tuple(getattr(value, name, None) for name in ("y", "x", "height", "width"))
    if not all(_integer(item) for item in parts):
        return None
    y, x, h, w = parts
    return Rect(y, x, y + h, x + w).clip(width, height)


def _regions(app, width, height):
    """Use painted pane bounds, rather than the center of a wide table row."""
    if getattr(app, "mode", "main") != "main":
        return (("modal", Rect(0, 0, height, width)),)
    tab = getattr(app, "tab", "")
    browser = _state(app, "history_browser_state").get("frame")
    browsing = (
        tab in ("analytics", "deps", "log", "research")
        and isinstance(browser, dict)
        and browser.get("tab") == tab
        and browser.get("mode") == "main"
        and browser.get("geometry")
        == (getattr(app, "width", None), getattr(app, "height", None))
    )
    body = (
        _pane_rect(getattr(app, "history_browser_content_rect", None), width, height)
        if browsing
        else None
    )
    body = body or Rect(
        max(0, getattr(app, "body_origin", 0)),
        max(0, getattr(app, "content_origin_x", 0)),
        max(0, height - 1),
        width,
    )
    regions = []
    if browsing:
        rect = _pane_rect(getattr(app, "history_browser_rect", None), width, height)
        if rect:
            regions.append(("page:history", rect))
    dividers = _state(app, "pane_drag_state").get("dividers", {})
    # Research and Logs render their own native documents. A Main rectangle
    # retained from Jobs must not invent a hidden Details pane on those pages.
    workspace = tab in ("jobs", "history") or "workspace:" + tab in dividers
    main = (
        _pane_rect(getattr(app, "workspace_main_rect", None), width, height)
        if workspace
        else None
    )
    panel = (
        _pane_rect(getattr(app, "job_panel_rect", None), width, height)
        if tab in ("jobs", "history")
        else None
    )
    layout = getattr(app, "layout_state", None)
    has_details = "details" in getattr(layout, "available", ())
    if (
        main
        and body.top <= main.top
        and main.bottom <= body.bottom
        and body.left <= main.left
        and main.right <= body.right
    ):
        if panel:
            regions.append(("page:details", panel))
        elif has_details and main.right < body.right:
            regions.append(
                (
                    "page:details",
                    Rect(body.top, main.right + 1, body.bottom, body.right),
                )
            )
        elif has_details and main.bottom < body.bottom:
            regions.append(
                (
                    "page:details",
                    Rect(main.bottom + 1, body.left, body.bottom, body.right),
                )
            )
        regions.append(("page:main", main))
    elif panel:
        regions.append(("page:details", panel))
    # The broader content rectangle is deliberately last; native split bounds
    # win, and pages without a workspace still have a stable content domain.
    regions.append(("page:main", body))
    return tuple(
        (name, rect)
        for name, rect in regions
        if rect.top < rect.bottom and rect.left < rect.right
    )


def _recover_focus(app, previous, graph, current):
    """Reanchor a removed row within its current table, never activate it."""
    if not previous or not current or previous.scope != graph.scope:
        return None
    if not current.button and current.group in ("job", "recent", "fin"):
        migrated = next(
            (
                control
                for control in graph.controls
                if control.enabled
                and not control.button
                and control.group in ("job", "recent", "fin")
                and control.label == current.label
                and graph.region(control) == previous.region(current)
            ),
            None,
        )
        if migrated:
            return migrated
    candidates = [
        control
        for control in graph.controls
        if control.enabled
        and control.group == current.group
        and graph.region(control) == previous.region(current)
    ]
    if not candidates:
        return None
    selected = str(getattr(app, "selected_id", ""))
    return next(
        (
            control
            for control in candidates
            if not control.button and control.label == selected
        ),
        min(
            candidates,
            key=lambda control: (
                abs(control.rect.center[0] - current.rect.center[0]),
                abs(control.rect.center[1] - current.rect.center[1]),
                control.rect.top,
                control.rect.left,
                control.id,
            ),
        ),
    )


def needs_frame(app):
    """Stop a key burst before another event can use displaced row geometry."""
    return bool(initialize(app)["frame_required"])
