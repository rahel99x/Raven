"""A bounded, cell-accurate interaction graph for the painted terminal frame.

Renderers publish controls as data. Graph construction and pointer feedback
never read files or request samples. Explicit keyboard row focus binds only a
published real job ID; activation uses the controller and its confirmations.
"""

from __future__ import annotations
from .ui.selection_effects import resume
from .ui.workspace_actions import focus_main

from .ui.control_feedback import (
    _targets as _targets,
    feedback_rows as feedback_rows,
    decorate as decorate,
    decorate_overlays as decorate_overlays,
)

from .ui.interaction_state import (
    initialize as initialize,
    _state as _state,
    hit_token as hit_token,
    _scope as _scope,
    _viewport as _viewport,
    _context as _context,
    _notification_context as _notification_context,
    _guided_context as _guided_context,
    _current as _current,
    controls as controls,
    _pane_rect as _pane_rect,
    _regions as _regions,
    _recover_focus as _recover_focus,
    needs_frame as needs_frame,
)

from .ui.control_graph import (
    Control as Control,
    _SpanIndex as _SpanIndex,
    Graph as Graph,
    nearest as nearest,
    _integer as _integer,
    _rect as _rect,
    _action as _action,
    _descriptor as _descriptor,
    _overlay_spans as _overlay_spans,
    _row_rect as _row_rect,
    _feedback_style as _feedback_style,
    _decorate_row as _decorate_row,
    MAX_CONTROLS as MAX_CONTROLS,
    MAX_OVERLAY_ROWS as MAX_OVERLAY_ROWS,
    MAX_HIT_ITEMS as MAX_HIT_ITEMS,
    POINTER_STYLE as POINTER_STYLE,
    FOCUS_STYLE as FOCUS_STYLE,
    ROW_KINDS as ROW_KINDS,
    BOUNDED_KINDS as BOUNDED_KINDS,
)

from itertools import islice
from .ui.geometry import Rect as Rect
from .ui.owner_ports import interaction as sources


def command_names():
    return ["focusbuttons"]


def overlay(views, snap, app, width, height):
    return None


def _hit_controls(app, rows, hits, width, height, *, layer=0, spans=None):
    for hit in islice(hits or (), MAX_CONTROLS):
        if not isinstance(hit, (tuple, list)) or len(hit) != 3:
            continue
        y, kind, value = hit
        if not _integer(y) or not 0 <= y < height:
            continue
        if kind == "control" and isinstance(value, dict):
            descriptor = dict(value)
            descriptor["rect"] = (y, value.get("left"), y + 1, value.get("right"))
            control = _descriptor(descriptor, layer=layer)
            if control:
                yield control
        elif (
            kind == "sort_header"
            and isinstance(value, (tuple, list))
            and len(value) == 4
        ):
            table, column, left, right = value
            rect = _rect((y, left, y + 1, right))
            if rect:
                yield Control(
                    f"sort:{table}:{column}",
                    str(column),
                    rect,
                    ("click", y, left),
                    f"sort:{table}",
                    layer=layer,
                )
        elif (
            kind in BOUNDED_KINDS
            and isinstance(value, (tuple, list))
            and len(value) == 3
        ):
            target, left, right = value
            rect = _rect((y, left, y + 1, right))
            if rect:
                identity = str(target)
                if (
                    kind == "job_panel_action"
                    and isinstance(target, (tuple, list))
                    and len(target) == 2
                ):
                    action_kind, payload = target
                    identity = (
                        str(action_kind)
                        + ":"
                        + str(
                            payload.get("id", "")
                            if isinstance(payload, dict)
                            else payload
                        )
                    )
                yield Control(
                    f"{kind}:{identity}",
                    identity,
                    rect,
                    ("click", y, left),
                    kind,
                    button=kind != "node_cell",
                    layer=layer,
                )
        elif kind in ROW_KINDS:
            right = None
            if getattr(app, "tab", "") in ("jobs", "history"):
                panel = getattr(app, "job_panel_rect", None)
                if (
                    panel is not None
                    and panel.y <= y < panel.y + panel.height
                    and panel.x
                ):
                    right = panel.x - 1
            rect = _row_rect(rows, y, width, height, overlays=spans, main_right=right)
            if (
                rect
                and getattr(app, "tab", "") in ("jobs", "history")
                and kind in ("job", "recent", "fin")
            ):
                main = _pane_rect(
                    getattr(app, "workspace_main_rect", None), width, height
                )
                if main and main.top <= y < main.bottom:
                    # A table row remains selectable in its leading cells.
                    # Progress text can start farther right (" wait " / --);
                    # that padding must not become the divider's drag buffer.
                    # Keep the outer structural margin free for resizing.
                    rect = Rect(
                        rect.top,
                        min(rect.left, main.left + 1),
                        rect.bottom,
                        min(rect.right, main.right),
                    )
            content = getattr(app, "history_browser_content_rect", None)
            if (
                rect
                and getattr(app, "tab", "") in ("analytics", "deps", "log", "research")
                and content is not None
            ):
                rect = _rect(
                    (
                        max(rect.top, content.y),
                        max(rect.left, content.x),
                        min(rect.bottom, content.y + content.height),
                        min(rect.right, content.x + content.width),
                    )
                )
            if rect:
                yield Control(
                    f"{kind}:{value}",
                    str(value),
                    rect,
                    ("row", y, rect.left),
                    kind,
                    button=False,
                    layer=layer,
                )


def _toolbar_controls(app):
    state = _state(app, "toolbar_state")
    if not state:
        return
    try:
        labels = {
            tuple(item["action"]): item["label"]
            for item in sources(app).toolbar_descriptors(app)
        }
    except (TypeError, ValueError, IndexError, KeyError):
        labels = {}
    tracks = []
    for hit in islice(state.get("hits", ()), MAX_CONTROLS):
        if not isinstance(hit, (tuple, list)) or len(hit) != 5:
            continue
        y, left, right, kind, key = hit
        rect = _rect((y, left, y + 1, right))
        if not rect:
            continue
        if kind == "track":
            tracks.append(rect)
        else:
            yield Control(
                f"toolbar:{kind}:{key}",
                str(labels.get((kind, key), key if kind == "menu" else kind)),
                rect,
                ("click", y, left),
                "toolbar",
                layer=3,
            )
    if tracks:
        rect = Rect(
            tracks[0].top,
            min(r.left for r in tracks),
            tracks[-1].bottom,
            max(r.right for r in tracks),
        )
        yield Control(
            "toolbar:track",
            "Update rate slider",
            rect,
            ("click", rect.top, round(rect.center[1])),
            "toolbar",
            layer=3,
        )
    if state.get("menu") is not None:
        disabled = state.get("menu_disabled", {})
        for hit in islice(state.get("menu_hits", ()), MAX_CONTROLS):
            if not isinstance(hit, (tuple, list)) or len(hit) != 4:
                continue
            y, left, right, key = hit
            rect = _rect((y, left, y + 1, right))
            if rect:
                reason = disabled.get(key, "")
                yield Control(
                    f"toolbar:menu:{state['menu']}:{key}",
                    str(labels.get(("item", key), key)),
                    rect,
                    ("click", y, left),
                    "toolbar-menu",
                    not bool(reason),
                    str(reason),
                    layer=4,
                )


def _modal_controls(app, rows, width, height, spans):
    mode = getattr(app, "mode", "main")
    if sources(app).group_menu_active(app):
        yield from sources(app).group_menu_controls(app)
        return
    if sources(app).list_menu_active(app):
        yield from sources(app).list_menu_controls(app)
        return
    if sources(app).history_export_active(app):
        yield from sources(app).history_export_controls(app)
        return
    state_names = {
        "analysis": "analysis_state",
        "project_runs": "project_state",
        "project_outputs": "project_state",
        "project_preview": "project_state",
        "log_compare": "log_workbench_state",
        "log_diff": "log_workbench_state",
        "execution": "execution_state",
        "columns": "table_state",
        "telemetry": "telemetry_state",
        "shell_checks": "shell_checks_state",
        "arraymap": "array_manifest_state",
        "gpu_provider": "gpu_provider_state",
        "operations": "ops_state",
        "raven": "raven_ui_state",
        "setup": "setup_ui_state",
        "notifications": "notification_ui_state",
        "data_export": "data_export_state",
        "telemetry_settings": "telemetry_settings_ui_state",
        "onboarding": "onboarding_ui_state",
        "launch_job": "launch_wizard_state",
        "launch_setup": "data_export_state",
        "connect_data": "connect_data_state",
        "run_home": "run_home_state",
    }
    # The log workbench uses additional mode names; only current modal data may
    # contribute controls, never a previous hidden panel's registries.
    state_name = state_names.get(mode)
    if state_name:
        yield from _hit_controls(
            app,
            rows,
            _state(app, state_name).get("control_hits", ()),
            width,
            height,
            layer=2,
            spans=spans,
        )
    if mode == "main" and getattr(app, "tab", "") == "log":
        yield from _hit_controls(
            app,
            rows,
            _state(app, "log_workbench_state").get("control_hits", ()),
            width,
            height,
            layer=2,
            spans=spans,
        )
    if mode == "table_tools":
        state = _state(app, "table_tools_state")
        for hit in islice(state.get("hits", ()), MAX_CONTROLS):
            if isinstance(hit, (tuple, list)) and len(hit) == 4:
                y, left, right, index = hit
                rect = _rect((y, left, y + 1, right))
                if rect:
                    yield Control(
                        f"table-tools:{state.get('modal')}:{index}",
                        str(index),
                        rect,
                        ("click", y, left),
                        "table-tools",
                        layer=2,
                    )
    if mode == "confirm":
        for hit in islice(
            _state(app, "command_state").get("confirm_hits", ()), MAX_CONTROLS
        ):
            if isinstance(hit, (tuple, list)) and len(hit) == 4:
                y, left, right, choice = hit
                rect = _rect((y, left, y + 1, right))
                if rect:
                    yield Control(
                        f"confirm:{choice}",
                        str(choice),
                        rect,
                        ("click", y, left),
                        "confirmation",
                        layer=2,
                    )
    row_registries = []
    if mode == "palette":
        row_registries.append(
            ("palette", _state(app, "command_state").get("result_hits", ()))
        )
    navigation = _state(app, "navigation_tools_state")
    if mode in (
        "jump_picker",
        "locations_picker",
        "bindings_editor",
        "settings_editor",
    ):
        row_registries.append(("navigation:" + mode, navigation.get("hits", ())))
    for prefix, registry in row_registries:
        for hit in islice(registry, MAX_CONTROLS):
            if isinstance(hit, (tuple, list)) and len(hit) == 2:
                y, index = hit
                rect = _row_rect(rows, y, width, height, overlays=spans)
                if rect:
                    yield Control(
                        f"{prefix}:{index}",
                        str(index),
                        rect,
                        ("row", y, rect.left),
                        prefix,
                        button=False,
                        layer=2,
                    )
    if mode == "session_inbox":
        registry = _state(app, "session_tools_state").get("mouse_rows", {})
        prefix = "inbox"
    elif mode == "exports":
        registry = getattr(getattr(app, "activity", None), "mouse_rows", {})
        prefix = "export"
    elif mode in ("log_tools_results", "log_tools_marks"):
        registry = _state(app, "log_tools_state").get("mouse_rows", {})
        prefix = mode
    else:
        registry, prefix = {}, ""
    for y, value in islice(registry.items(), MAX_CONTROLS):
        if isinstance(value, (tuple, list)) and len(value) == 3:
            identity, left, right = value
            rect = _rect((y, left, y + 1, right))
            if rect:
                yield Control(
                    f"{prefix}:{identity}",
                    str(identity),
                    rect,
                    ("row", y, left),
                    prefix,
                    button=False,
                    layer=2,
                )


def publish(app, rows, hits, width, height, overlays=None, extra_controls=()):
    """Publish one immutable graph from the final visible frame's metadata.

    ``rows`` contain normal display rows; ``overlays`` contain absolute
    ``(y, x, row)`` triples. Explicit controls may be ``Control`` instances or
    dictionaries with ``id, rect, action, group, label``. Existing three-item
    hits and modal registries are adapted without invoking any renderer.
    """
    state = initialize(app)
    hits = tuple(islice(hits or (), MAX_CONTROLS))
    state["published_hits"] = hits
    state["published_hit_token"] = hit_token(hits)
    width = max(0, width) if _integer(width) else 0
    height = max(0, height) if _integer(height) else len(rows)
    spans = _overlay_spans(overlays, width, height)
    toolbar_state = _state(app, "toolbar_state")
    menu_rect = (
        _rect(toolbar_state.get("menu_rect"))
        if toolbar_state.get("menu") is not None
        else None
    )
    candidates = []
    if getattr(app, "mode", "main") == "main":
        candidates.extend(_hit_controls(app, rows, hits, width, height))
        candidates.extend(
            _hit_controls(
                app,
                rows,
                _state(app, "notification_ui_state").get("status_hits", ()),
                width,
                height,
            )
        )
        for hit in islice(getattr(app, "tab_hits", ()), MAX_CONTROLS):
            if isinstance(hit, (tuple, list)) and len(hit) == 4:
                y, left, right, key = hit
                rect = _rect((y, left, y + 1, right))
                if rect:
                    candidates.append(
                        Control(
                            f"tab:{key}", str(key), rect, ("click", y, left), "tabs"
                        )
                    )
        chip_y = getattr(app, "table_chip_y", None)
        chips = getattr(app, "table_chip_cells", ("", ()))
        if _integer(chip_y) and isinstance(chips, (tuple, list)) and len(chips) == 2:
            for chip in islice(chips[1], MAX_CONTROLS):
                if isinstance(chip, (tuple, list)) and len(chip) == 3:
                    key, left, right = chip
                    rect = _rect((chip_y, left, chip_y + 1, right))
                    if rect:
                        candidates.append(
                            Control(
                                f"filter-chip:{chips[0]}:{key}",
                                str(key),
                                rect,
                                ("click", chip_y, left),
                                "filter-chips",
                            )
                        )
    candidates.extend(_modal_controls(app, rows, width, height, spans))
    candidates.extend(_toolbar_controls(app))
    for descriptor in islice(extra_controls or (), MAX_CONTROLS):
        control = _descriptor(descriptor, layer=2 if spans else 0)
        if control:
            candidates.append(control)
    visible, identities, primary_rects = [], set(), {}
    for control in candidates:
        if len(visible) >= MAX_CONTROLS:
            break
        rect = control.rect.clip(width, height)
        if not rect:
            continue
        if menu_rect is not None and control.layer < 4 and rect.intersects(menu_rect):
            continue
        identity = control.id
        if identity in identities:
            # Inline cited-file links intentionally repeat their native action
            # on each wrapped line. Keep each painted fragment selectable;
            # ordinary duplicate semantic metadata still contributes one node.
            if (
                identity.startswith("job_panel_action:")
                and primary_rects.get(identity) != rect
            ):
                identity += f":fragment:{rect.top}:{rect.left}:{rect.right}"
            if identity in identities:
                continue
        # A dropdown masks controls directly underneath its painted rectangle.
        # Generic modals omit all underlying controls even beyond their border.
        if control.layer == 0 and any(
            rect.intersects(span)
            for y in range(rect.top, rect.bottom)
            for span, _ in spans.get(y, ())
        ):
            continue
        action = control.action
        if action[0] in ("click", "row") and len(action) == 3:
            action = (
                action[0],
                max(rect.top, min(rect.bottom - 1, action[1])),
                max(rect.left, min(rect.right - 1, action[2])),
            )
        visible.append(
            Control(
                identity,
                control.label,
                rect,
                action,
                control.group,
                control.enabled,
                control.reason,
                control.button,
                control.layer,
            )
        )
        identities.add(identity)
        primary_rects.setdefault(control.id, rect)
    token = _context(app)
    geometry = (getattr(app, "width", None), getattr(app, "height", None))
    visible = tuple(visible)
    graph = previous = state["graph"]
    previous_focus = previous.get(state["focused"]) if previous else None
    regions, scope = _regions(app, width, height), _scope(app)
    if (
        graph is None
        or graph.token != token
        or graph.width != width
        or graph.height != height
        or graph.observed_geometry != geometry
        or graph.controls != visible
        or graph.regions != regions
    ):
        state["generation"] += 1
        graph = Graph(
            visible, token, width, height, state["generation"], geometry, regions, scope
        )
    state["graph"] = graph
    state["frame_required"] = False
    focused = graph.get(state["focused"])
    if focused is None or not focused.enabled:
        recovered = (
            _recover_focus(app, previous, graph, previous_focus)
            if state["active"]
            else None
        )
        state["focused"] = recovered.id if recovered else None
        state["active"] = bool(recovered)
        if recovered:
            _bind_row(app, recovered)
    elif previous and previous.scope != scope:
        # The same incidental ID on another page/modal is not the same focus.
        state["focused"], state["active"] = None, False
    pointer = state["pointer"]
    hovered = graph.at(*pointer) if pointer else None
    state["hovered"] = hovered.id if hovered else None
    if state.get("pending_focus"):
        state["pending_focus"] = None
        _focus(app)
    return graph


def _bind_row(app, control):
    """Bind explicit job-row focus using existing published real identities."""
    if (
        getattr(app, "mode", "main") != "main"
        or getattr(app, "tab", "") not in ("jobs", "history")
        or control.button
        or control.group not in ("job", "recent", "fin")
    ):
        return False
    tab = getattr(app, "tab", "jobs")
    ids = (
        (getattr(app, "last_history_ids", ()) or ())
        if tab == "history"
        else getattr(app, "visible_ids", ())
        if control.group == "job"
        else getattr(app, "recent_ids", ())
    )
    if control.label not in ids or not isinstance(getattr(app, "cursor", None), dict):
        return False
    index = ids.index(control.label) + (
        len(getattr(app, "visible_ids", ())) if control.group == "recent" else 0
    )
    changed = (
        getattr(app, "selected_id", None) != control.label
        or app.cursor.get(tab) != index
    )
    resume(app, tab)
    app.cursor[tab], app.selected_id = index, control.label
    layout = getattr(app, "layout_state", None)
    if layout:
        layout.focus = "main"
    panel = _state(app, "job_panel_state")
    panel["focus"] = ""
    if changed:
        initialize(app)["frame_required"] = True
    return changed


def _advance_row(app, graph, current, direction):
    """At a table viewport edge, admit the next actual scheduler row."""
    if (
        direction not in ("up", "down")
        or current.button
        or current.group not in ("job", "recent", "fin")
        or getattr(app, "mode", "main") != "main"
        or getattr(app, "tab", "") not in ("jobs", "history")
        or not callable(getattr(app, "move", None))
    ):
        return False
    cy = current.rect.center[0]
    sign = 1 if direction == "down" else -1
    if any(
        control.enabled
        and control.group == current.group
        and graph.region(control) == graph.region(current)
        and (control.rect.center[0] - cy) * sign > 0
        for control in graph.controls
    ):
        return False
    _bind_row(app, current)
    old = getattr(app, "selected_id", None)
    from .recent_history import navigate

    tab = getattr(app, "tab", "jobs")
    if tab != "jobs" or not navigate(app, direction):
        app.move(direction)
        ids = (
            (getattr(app, "last_history_ids", ()) or ())
            if tab == "history"
            else list(getattr(app, "visible_ids", ()))
            + list(getattr(app, "recent_ids", ()))
        )
        cursor = getattr(app, "cursor", {}).get(tab, 0)
        if 0 <= cursor < len(ids):
            app.selected_id = ids[cursor]
    selected = getattr(app, "selected_id", None)
    if selected == old or not selected:
        return False
    kind = (
        "fin"
        if tab == "history"
        else "job"
        if selected in getattr(app, "visible_ids", ())
        else "recent"
    )
    state = initialize(app)
    state["focused"], state["active"], state["frame_required"] = (
        kind + ":" + selected,
        True,
        True,
    )
    return True


def _focus(app, enabled=True):
    state = initialize(app)
    graph = _current(app)
    if not enabled:
        state["active"] = False
        state["pending_focus"] = None
        return True
    if graph is None:
        state["pending_focus"] = True
        return True
    target = graph.get(state["hovered"]) or graph.get(state["focused"])
    if target is None or not target.enabled:
        target = next(
            (
                control
                for control in graph.controls
                if control.enabled and control.button
            ),
            next((control for control in graph.controls if control.enabled), None),
        )
    if target is None:
        return False
    state["focused"], state["active"] = target.id, True
    # F8 explicitly transfers keyboard ownership from a clicked job browser.
    # Its native arrow handler runs before this graph in the shared workbench.
    browser = _state(app, "history_browser_state")
    browser["focused"] = False
    _bind_row(app, target)
    return True


def run_command(app, args):
    if not args or args[0] != "focusbuttons":
        return False
    if len(args) > 2 or len(args) == 2 and args[1] not in ("on", "off"):
        if hasattr(app, "fail"):
            app.fail("focusbuttons [on|off]")
        return True
    enabled = args[1] != "off" if len(args) == 2 else not initialize(app)["active"]
    if not _focus(app, enabled) and hasattr(app, "say"):
        app.say("No visible controls yet; draw this page and press F8.")
    elif hasattr(app, "say"):
        app.say(
            "Buttons: arrows move; Enter activates; Esc returns to content."
            if enabled
            else "Content keys restored."
        )
    return True


def _activate(app, control, *, keyboard=False):
    state = initialize(app)
    graph = _current(app)
    if graph is None or graph.get(control.id) != control or state["routing"]:
        return False
    if not control.enabled:
        if hasattr(app, "say") and control.reason:
            app.say(control.reason)
        return True
    action = control.action
    previous_active = state["active"]
    state["routing"], state["active"] = True, False
    try:
        if action[0] in ("click", "row") and len(action) == 3:
            app.click(action[1], action[2], getattr(app, "last_hits", ()))
            if (
                keyboard
                and action[0] == "row"
                and getattr(app, "mode", "main") == graph.token[0]
            ):
                app.handle("enter")
        elif action[0] == "command" and len(action) == 2 and isinstance(action[1], str):
            app.run_command(action[1])
        elif action[0] == "key" and len(action) == 2 and isinstance(action[1], str):
            app.handle(action[1])
        elif (
            action[0] == "set"
            and len(action) == 3
            and action[1] in ("nodes_view", "analytics_view", "research_view")
        ):
            setattr(app, action[1], action[2])
        elif action[0] == "scrollbar" and len(action) == 3:
            from .scrollbars import activate

            return activate(app, action[1], action[2])
        else:
            return False
    finally:
        state["routing"] = False
        state["active"] = previous_active and graph.token == _context(app)
        if (
            previous_active
            and control.button
            and graph.token != _context(app)
            and getattr(app, "mode", "main") == graph.token[0]
        ):
            state["pending_focus"] = True
    return True


def handle_key(app, key):
    state = initialize(app)
    if state["routing"]:
        return False
    if (
        getattr(app, "mode", "main") == "operations"
        and (
            _state(app, "ops_state").get("editing") is not None
            or _state(app, "ops_state").get("catalog_search")
        )
    ) or (
        getattr(app, "mode", "main") == "connect_data"
        and _state(app, "connect_data_state").get("editing") is not None
    ):
        # Fields and catalog search own their text keys. Returning focus to
        # the graph here would turn Home/Enter or typed shortcuts into actions.
        state["active"] = False
        state["pending_focus"] = None
        return False
    if key == "f8":
        _focus(app, not state["active"])
        return True
    if not state["active"]:
        return False
    if key == "esc":
        state["active"] = False
        state["pending_focus"] = None
        if getattr(app, "mode", "main") == "main" and getattr(app, "tab", "") in (
            "jobs",
            "history",
        ):
            # Clicking a Details button starts graph traversal and native tab
            # focus together. One Escape releases both owners, so Jobs arrows
            # do not unexpectedly continue cycling Details after graph exit.
            focus_main(app)
        return True
    graph = _current(app)
    if graph is None:
        if (
            state["frame_required"]
            and state.get("graph")
            and state["graph"].scope == _scope(app)
        ):
            # The terminal loop paints before the next queued event. Small
            # embedders that call again sooner still cannot activate old data.
            if key in (
                "up",
                "down",
                "left",
                "right",
                "home",
                "end",
                "tab",
                "btab",
                "enter",
                "space",
            ):
                return True
        state["active"] = False
        return False
    current = graph.get(state["focused"])
    if current is None:
        state["active"] = False
        return False
    target = None
    if key in ("up", "down", "left", "right"):
        if _advance_row(app, graph, current, key):
            return True
        target = nearest(graph, current, key)
    elif key in ("home", "end", "tab", "btab"):
        ordered = graph._ordered
        if ordered:
            index = graph._order_indices.get(current.id, 0)
            index = (
                0
                if key == "home"
                else len(ordered) - 1
                if key == "end"
                else (index + (1 if key == "tab" else -1)) % len(ordered)
            )
            target = ordered[index]
    elif key in ("enter", "space"):
        return _activate(app, current, keyboard=True)
    elif key == "p" and getattr(app, "mode", "main") == "operations":
        return sources(app).pin_focused(app, current)
    else:
        # Typing a content shortcut exits graph mode and reaches its existing
        # handler. No hidden keymap or chart/log selection mode is replaced.
        state["active"] = False
        return False
    if target:
        state["focused"] = target.id
        _bind_row(app, target)
    return True


def handle_mouse(app, y, x, button="left", shift=False, **kwargs):
    state = initialize(app)
    if state["routing"]:
        return False
    if not _integer(y) or not _integer(x):
        return False
    state["pointer"] = (y, x)
    graph = _current(app)
    target = graph.at(y, x) if graph else None
    state["hovered"] = target.id if target else None
    if button in ("motion", "drag", "release", "press"):
        # Captured slider / job selection motion still reaches its owner.
        return False
    if button in ("wheel-up", "wheel-down", "wheel_up", "wheel_down"):
        state["active"] = False
        return False
    if button != "left" or shift:
        return False
    if target is None or not target.button:
        state["active"] = False
        return False
    state["focused"], state["active"] = target.id, True
    return _activate(app, target)
