"""Mouse range selection of exact job IDs for existing marked-job actions."""
from __future__ import annotations

from .pointer_routing import capture_handler

MAX_SELECTION = 50000
ROW_KINDS = {"jobs": {"job", "recent"}, "history": {"fin"},
             "group": {"group"}, "deps": {"dep"}, "analytics": {"advisor_job"}}
JOB_SCOPES = ("jobs", "history", "group", "deps", "analytics", "research", "log")


def initialize(app):
    if not isinstance(getattr(app, "job_selection_state", None), dict):
        app.job_selection_state = {"capture": None}
    app.job_selection_state.setdefault("deselected", {})
    app.job_selection_state.setdefault("lines_deselected", False)
    app.job_selection_state.setdefault("mark_tokens", {})
    app.job_selection_state.setdefault("mark_scopes", {})
    app.job_selection_state.setdefault("published_tokens", {})
    app.job_selection_state.setdefault("anchors", {})
    app.job_selection_state.setdefault("discard_release", False)
    return app.job_selection_state


def selected(app, tab, identifier):
    """Keep an explicitly cleared table clear across maintenance frames."""
    return None if initialize(app)["deselected"].get(tab, False) else identifier


def resume(app, tab=None):
    # An explicit native-row gesture owns subsequent copies. Do not leave a
    # prior rendered range (or its invalidation guard) in front of it.
    from .text_selection import clear as clear_text
    clear_text(app)
    initialize(app)["deselected"][tab or getattr(app, "tab", "")] = False


def cleared(app, tab=None):
    return bool(initialize(app)["deselected"].get(tab or getattr(app, "tab", ""), False))


def lines_cleared(app):
    return bool(initialize(app)["lines_deselected"])


def resume_lines(app):
    """A deliberate line gesture restores its cursor without selecting a job."""
    from .text_selection import clear as clear_text
    clear_text(app)
    initialize(app)["lines_deselected"] = False


def clear_lines(app):
    """Clear text and log cursors without changing job or dialog ownership."""
    from .text_selection import clear as clear_text
    clear_text(app)
    state = initialize(app)
    state["lines_deselected"] = True
    app.sel_anchor = app.click_row = None
    app.sel_end = 0
    app.log_selection_expected = False
    logs = getattr(app, "logs", None)
    if logs is not None:
        logs.clear_selection(reset_cursor=True)
        logs.match = None
    session = getattr(app, "job_panel_state", {}).get("session")
    if session is not None and session is not logs:
        session.clear_selection(reset_cursor=True)
        session.match = None
    log_workbench = getattr(app, "log_workbench_state", None)
    if isinstance(log_workbench, dict):
        log_workbench["citation"] = None
    log_tools = getattr(app, "log_tools_state", None)
    if isinstance(log_tools, dict):
        log_tools.update(selection=None, cursor_deselected=True)


def clear(app):
    state = initialize(app)
    state["capture"] = None
    state["anchors"].clear()
    state["mark_tokens"].clear()
    state["mark_scopes"].clear()
    state["deselected"].update(dict.fromkeys(JOB_SCOPES, True))
    app.marks.clear()
    app.selected_id = None
    clear_lines(app)
    pointer = getattr(app, "interaction_state", {})
    pointer.update(active=False, focused=None, frame_required=True)
    getattr(app, "job_panel_state", {})["focus"] = ""
    if app.tab in ("jobs", "history"):
        from .job_panels import focus_main
        focus_main(app)
    from . import chart_interaction, metric_live, pane_drag
    chart_interaction.cancel(app)
    metric_live.cancel(app)
    pane_drag.cancel(app)
    toolbar = getattr(app, "toolbar_state", None)
    if isinstance(toolbar, dict):
        toolbar.update(dragging=False, pressed=False, drag_width=None)
    history = getattr(app, "history_browser_state", None)
    if isinstance(history, dict):
        history.update(drag=None, focused=False, last_selected=None)
        for view in history.get("views", {}).values():
            if isinstance(view, dict):
                view.update(selected=None, explicit=False)
    app.say("Selections cleared; click a job or line to select again")


def context_click(app, y, x, button="left", shift=False):
    """A graph or History export owns right-click; all other pages clear."""
    if button != "right":
        return False
    if any(type(value) is not int for value in (y, x)):
        return False
    if not (0 <= x < getattr(app, "width", 120)
            and 0 <= y < getattr(app, "height", 100000)):
        return False
    mode = getattr(app, "mode", "main")
    if mode in ("log_tools_page", "log_tools_results", "log_tools_marks"):
        # The modal keeps its own source and control ownership. Its clear must
        # precede the global toolbar so even a top-row right-click is inert.
        from .log_tools import handle_mouse
        return handle_mouse(app, y, x, button=button)
    if mode in ("help", "details", "analysis"):
        from . import chart_interaction
        if mode == "analysis" and chart_interaction.handle_mouse(app, y, x, button=button, shift=shift):
            return True
        clear_lines(app)
        chart_interaction.cancel(app)
        if mode == "analysis":
            from .analysis_ui import context_click as analysis_context
            if analysis_context(app, y, x, button=button):
                return True
        app.say("Text selection cleared; dialog remains open")
        return True
    if mode != "main":
        return False
    tab = getattr(app, "tab", "")
    from .chart_interaction import handle_mouse as chart_mouse
    if chart_mouse(app, y, x, button=button, shift=shift):
        return True
    if tab == "history":
        rect = getattr(app, "history_jobs_rect", None)
        if rect is not None and rect.contains(y, x):
            from .history_log_export import handle_mouse
            return handle_mouse(app, y, x, button=button)
    clear(app)
    return True


def publish(app, rows, hits, width, height, snap=None):
    """Bind painted job identities, then publish History's clipped list."""
    if isinstance(snap, dict) and getattr(app, "tab", "") in JOB_SCOPES:
        from .manual_job_groups import SelectionTokenCache
        state = initialize(app)
        cache = state.get("token_cache")
        if cache is None:
            cache = state["token_cache"] = SelectionTokenCache()
        ids = _order(app)
        browser = getattr(app, "history_browser_state", {})
        if (browser.get("frame") or {}).get("tab") == getattr(app, "tab", ""):
            ids = tuple(dict.fromkeys((*ids, *_browser_order(app))))
        from .job_groups import registry
        frame = getattr(app, "job_groups_frame_index", None)
        checked = (bool(getattr(app, "table_state", {}).get("groups"))
                   and isinstance(frame, tuple) and len(frame) == 2 and frame[0] is snap)
        tokens = cache.update(snap, ids, registry=registry(app) if checked else None) if ids else {}
        previous = state["published_tokens"]
        capture = state.get("capture")
        if capture and any(previous.get(identifier) != tokens.get(identifier)
                           for identifier in capture["ids"]):
            state["capture"] = None
            state["discard_release"] = True
        state["published_tokens"] = tokens
        marks = getattr(app, "marks", ())
        bound = state["mark_tokens"]
        state["mark_tokens"] = {identifier: bound.get(identifier, tokens.get(identifier))
                                for identifier in marks if identifier in bound or identifier in tokens}
        scopes = state["mark_scopes"]
        scope = _mark_scope(app, state)
        state["mark_scopes"] = {identifier: scopes.get(identifier, scope)
                                for identifier in marks if identifier in scopes or identifier in tokens}
    app.history_jobs_rect = None
    if getattr(app, "tab", "") != "history" or height is None:
        return
    from .interaction import Rect
    from .workspace_layout import enabled, _section_title
    headers = [y for y, kind, value in hits if kind == "sort_header"
               and isinstance(value, (tuple, list)) and value[:1] == ("history",)]
    records = [y for y, kind, value in hits if kind == "fin"]
    if not headers and not records:
        return
    left, right, bottom = 0, width, max(0, height - 1)
    top = max(0, min(headers) - 1 if headers else min(records))
    native = isinstance(getattr(app, "job_panel_state", None), dict)
    main = getattr(app, "workspace_main_rect", None) if enabled(app) or native else None
    if main is not None:
        left, right = main.x, min(width, main.x + main.width)
        top, bottom = max(top, main.y), min(bottom, main.y + main.height)
    else:
        last = max(records) if records else max(headers)
        for y in range(last + 1, min(len(rows), bottom)):
            title = _section_title(rows[y])
            if title and title.startswith(("selected", "details")):
                bottom = y
                break
    if top < bottom and left < right:
        app.history_jobs_rect = Rect(top, left, bottom, right)


def command_names():
    return ["advisor-job"]


def run_command(app, args):
    if not args or args[0] != "advisor-job":
        return False
    if (len(args) != 2 or getattr(app, "mode", "main") != "main"
            or getattr(app, "tab", "") != "analytics"
            or getattr(app, "analytics_view", "") != "advisor"
            or args[1] not in getattr(app, "analytics_advisor_ids", ())):
        app.say("Select a running job from the current Advisor list")
        return True
    _select_advisor(app, args[1])
    return True


def _select_advisor(app, identifier):
    resume(app)
    app.analytics_job = app.selected_id = identifier
    initialize(app)["advisor_focus"] = True


def overlay(views, snap, app, width, height):
    return None


def _order(app):
    tab = getattr(app, "tab", "")
    if tab == "jobs":
        return tuple((list(getattr(app, "visible_ids", [])) + list(getattr(app, "recent_ids", [])))[:MAX_SELECTION])
    if tab == "history":
        cached = getattr(app, "last_history_ids", None)
        if isinstance(cached, (list, tuple)):
            return tuple(cached[:MAX_SELECTION])
        return tuple(record.id for record in app.history_jobs())[:MAX_SELECTION]
    if tab == "analytics" and getattr(app, "analytics_view", "") == "advisor":
        return tuple(getattr(app, "analytics_advisor_ids", ()))[:MAX_SELECTION]
    if tab in ("group", "deps"):
        return tuple(getattr(app, "group_ids" if tab == "group" else "dep_ids", []))[:MAX_SELECTION]
    return ()


def _browser_order(app):
    from .history_browser import initialize as browser_state
    state = browser_state(app)
    return state.get("selection_ids", ())[:MAX_SELECTION]


def _browser_hit(app, y, x):
    from .history_browser import _current
    frame = _current(app)
    rect = getattr(app, "history_browser_rect", None)
    if not frame or frame["dock"] == "off" or not rect or not rect.contains(y, x):
        return None
    # Disclosure and dock controls occur before overlapping whole-row hits.
    value = next((value for row, kind, value in frame["hits"]
                  if row == y - rect.y and kind == "control"
                  and value["left"] <= x - rect.x < value["right"]), None)
    prefix = "history:" + app.tab + ":job:"
    return value["id"][len(prefix):] if value and value["id"].startswith(prefix) else None


def _browser_column(app, x):
    state = getattr(app, "history_browser_state", {})
    frame = state.get("frame") or {}
    rect = getattr(app, "history_browser_rect", None)
    columns = max(1, frame.get("columns", 1))
    if rect is None:
        return 0
    return min(columns - 1, max(0, (x - rect.x) // max(1, (rect.width - 1) // columns)))


def advisor_pointer(app, y, x):
    """An exact published Advisor row, excluding its disclosure button."""
    return (getattr(app, "mode", "main") == "main"
            and getattr(app, "tab", "") == "analytics"
            and getattr(app, "analytics_view", "") == "advisor"
            and _hit(app, y, x) is not None)


def pointer_focus(app, y, x):
    """Revoke stale list focus before a fresh press can reach another owner."""
    state = initialize(app)
    from .chart_interaction import hover
    hover(app, y, x)
    if not advisor_pointer(app, y, x):
        state["advisor_focus"] = False
    browser = getattr(app, "history_browser_state", {})
    rect = getattr(app, "history_browser_rect", None)
    if browser.get("focused") and (rect is None or not rect.contains(y, x)):
        browser["focused"] = False


def context(app):
    """Return only the currently focused, published exact-job list.

    ``ids`` includes offscreen visible-order records but never hidden members
    of a collapsed summary. No source reads or inference runs on input.
    """
    state = initialize(app)
    toolbar = getattr(app, "toolbar_state", {})
    if (getattr(app, "mode", "main") != "main" or cleared(app)
            or toolbar.get("menu") is not None or toolbar.get("panel")
            or getattr(app, "text_selection_state", {}).get("explicit")
            or getattr(app, "sel_anchor", None) is not None
            or getattr(app, "text_selection_state", {}).get("capture")
            or getattr(app, "chart_interaction_state", {}).get("capture")
            or getattr(app, "metric_live_state", {}).get("capture")):
        return None
    chart = getattr(app, "chart_interaction_state", {})
    point = chart.get("pointer")
    if chart.get("hovered") and isinstance(point, tuple) and len(point) == 2:
        from .chart_interaction import _at
        if _at(app, *point) is not None:
            return None
    tab = getattr(app, "tab", "")
    browser = getattr(app, "history_browser_state", {})
    if browser.get("focused"):
        from .history_browser import _current
        frame = _current(app)
        if frame and frame["dock"] != "off":
            ids = _browser_order(app)
            index = min(max(0, browser.get("index", 0)), len(ids) - 1)
            return _context_result(app, "history:" + tab, ids, ids[index] if ids else None)
        return None
    if tab == "analytics":
        if getattr(app, "analytics_view", "") != "advisor" or not state.get("advisor_focus"):
            return None
        ids = _order(app)
        selected_id = getattr(app, "analytics_job", None)
        return _context_result(app, "analytics:advisor", ids, selected_id if selected_id in ids else None)
    if tab not in ("jobs", "history", "group", "deps"):
        return None
    if (getattr(getattr(app, "layout_state", None), "focus", "main") == "details"
            or tab in ("jobs", "history") and getattr(app, "job_panel_state", {}).get("focus")):
        return None
    ids = _order(app)
    index = getattr(app, "cursor", {}).get(tab, 0)
    chosen = ids[max(0, min(len(ids) - 1, index))] if ids else None
    scope = "recent" if tab == "jobs" and chosen in getattr(app, "recent_ids", ()) else tab
    return _context_result(app, scope, ids, chosen)


def _context_result(app, scope, ids, chosen):
    state = initialize(app)
    published, bound = state["published_tokens"], state["mark_tokens"]
    marks = getattr(app, "marks", ())
    tokens = {identifier: bound.get(identifier, published.get(identifier)) if identifier in marks
              else published.get(identifier) for identifier in ids}
    return {"scope": scope, "ids": ids, "selected": chosen, "tokens": tokens}


def _mark_scope(app, state):
    """Remember where a mark was made without reading records on pointer input."""
    capture = state.get("capture")
    if capture:
        return capture.get("scope", getattr(app, "tab", ""))
    tab = getattr(app, "tab", "")
    if getattr(app, "history_browser_state", {}).get("focused"):
        return "history:" + tab
    return "analytics:advisor" if tab == "analytics" else tab


def _bind_marks(app, identifiers):
    state = initialize(app)
    state["mark_tokens"] = {identifier: value for identifier, value in state["mark_tokens"].items()
                            if identifier in app.marks}
    state["mark_scopes"] = {identifier: value for identifier, value in state["mark_scopes"].items()
                            if identifier in app.marks}
    scope = _mark_scope(app, state)
    for identifier in identifiers:
        if identifier in app.marks:
            state["mark_tokens"][identifier] = state["published_tokens"].get(identifier)
            state["mark_scopes"][identifier] = scope


def _range_scope(app, identifier, browser, ids):
    """Shift-click anchors belong to one list; an explicit drag may cross it."""
    tab = getattr(app, "tab", "")
    if browser:
        return "history:" + tab, ids
    if tab == "jobs":
        recent = getattr(app, "recent_ids", ())
        if identifier in recent:
            return "recent", tuple(recent[:MAX_SELECTION])
        return "jobs", tuple(getattr(app, "visible_ids", ())[:MAX_SELECTION])
    return ("analytics:advisor" if tab == "analytics" else tab), ids


def _hit(app, y, x):
    if any(type(value) is not int for value in (y, x)):
        return None
    if not (0 <= x < getattr(app, "width", 120)
            and 0 <= y < getattr(app, "height", 100000)):
        return None
    if getattr(app, "tab", "") in ("jobs", "history"):
        from .job_panels import contains
        if contains(app, y, x):
            return None
    if getattr(app, "tab", "") == "history":
        rect = getattr(app, "history_jobs_rect", None)
        if rect is not None and not rect.contains(y, x):
            return None
    if getattr(app, "tab", "") in ("analytics", "deps"):
        rect = getattr(app, "history_browser_content_rect", None)
        if rect is not None and not rect.contains(y, x):
            return None
    if getattr(app, "tab", "") == "analytics":
        if getattr(app, "analytics_view", "") != "advisor":
            return None
        value = next((value for row, kind, value in getattr(app, "last_hits", ())
                      if row == y and kind == "control" and isinstance(value, dict)
                      and value.get("left", -1) <= x < value.get("right", -1)), None)
        prefix = "advisor-job:"
        return (value["id"][len(prefix):] if value and
                str(value.get("id", "")).startswith(prefix) else None)
    # Native rows span the whole pane; their overlaid disclosure/grip buttons
    # keep ownership, including synthesized clicks with no preceding press.
    if any(row == y and kind == "control" and isinstance(value, dict)
           and value.get("left", -1) <= x < value.get("right", -1)
           for row, kind, value in getattr(app, "last_hits", ())):
        return None
    kinds = ROW_KINDS.get(getattr(app, "tab", ""), set())
    return next((identifier for row, kind, identifier in getattr(app, "last_hits", [])
                 if row == y and kind in kinds and isinstance(identifier, str)), None)


def active(app):
    return bool(initialize(app)["capture"])


def _valid(app, capture):
    return (getattr(app, "mode", "main") == "main" and app.tab == capture["tab"]
            and getattr(app, "toolbar_state", {}).get("menu") is None
            and not getattr(app, "toolbar_state", {}).get("panel")
            and (getattr(app, "width", None), getattr(app, "height", None)) == capture["size"]
            and (_browser_order(app) if capture.get("browser") else _order(app)) == capture["ids"]
            and (not capture.get("browser") or _browser_valid(app, capture))
            and (capture.get("view") is None or capture["view"] == getattr(app, "analytics_view", None)))


def _browser_valid(app, capture):
    from .history_browser import _current
    frame = _current(app)
    return bool(frame and frame["dock"] != "off" and
                (frame["dock"], frame["preference"]) == capture["browser_geometry"])


def tick(app):
    state = initialize(app)
    if state["capture"] and not _valid(app, state["capture"]):
        state["capture"] = None
        state["discard_release"] = True


def handle_key(app, key):
    state = initialize(app)
    capture = state["capture"]
    if capture:
        state["capture"] = None
        state["discard_release"] = True
        if key == "esc":
            app.marks = set(capture["base"])
            state["mark_tokens"] = dict(capture.get("base_tokens", {}))
            state["mark_scopes"] = dict(capture.get("base_scopes", {}))
            scope = capture.get("scope")
            if scope is not None:
                previous_anchor = capture.get("previous_anchor")
                if previous_anchor is None:
                    state["anchors"].pop(scope, None)
                else:
                    state["anchors"][scope] = previous_anchor
            app.say("Drag selection cancelled")
            return True
    if key == "space":
        source = context(app)
        if source and source["selected"] is not None:
            identifier = source["selected"]
            if identifier in app.marks:
                app.marks.discard(identifier)
            else:
                app.marks.add(identifier)
            _bind_marks(app, (identifier,))
            if source["scope"] in ("jobs", "recent", "deps"):
                move = getattr(app, "move", None)
                if callable(move):
                    move("down")
                    app.sync_selection()
            app.say(f"{len(app.marks)} jobs marked; g groups, u ungroups")
            return True
    if (state.get("advisor_focus") and context(app)
            and key in ("up", "down", "home", "end", "esc")):
        if key == "esc":
            state["advisor_focus"] = False
            return True
        ids = _order(app)
        if ids:
            current = getattr(app, "analytics_job", None)
            index = ids.index(current) if current in ids else 0
            index = (0 if key == "home" else len(ids) - 1 if key == "end" else
                     max(0, min(len(ids) - 1, index + (1 if key == "down" else -1))))
            app.analytics_job = app.selected_id = ids[index]
            offsets = getattr(app, "analytics_scroll_offsets", {}).get("advisor")
            row = getattr(app, "analytics_advisor_positions", {}).get(ids[index])
            page = getattr(app, "analytics_advisor_page", 0)
            if isinstance(offsets, dict) and row is not None and page > 0:
                top = offsets["top"]
                if row < top + 1:
                    offsets["top"] = max(0, row - 1)
                elif row >= top + 1 + page:
                    offsets["top"] = max(0, row - page)
        return True
    return False


def _range(app, capture, identifier):
    ids = capture.get("range_ids", capture["ids"])
    indices = capture.get("indices")
    if identifier not in (ids if indices is None else indices):
        return
    if capture["moved"] and capture["endpoint"] == identifier:
        return
    first, last = sorted((ids.index(capture["anchor"]), ids.index(identifier))) if indices is None else sorted(
        (indices[capture["anchor"]], indices[identifier]))
    base = capture.get("selection_base", capture["base"])
    app.marks = (set(base) if capture["extend"] else set()) | set(ids[first:last + 1])
    anchor_state = capture.get("anchor_state")
    if anchor_state is not None:
        anchor_state["base"] = set(base) if capture["extend"] else set()
        anchor_state["last_range"] = ids[first:last + 1]
    _bind_marks(app, ids[first:last + 1])
    capture["moved"] = True
    resume(app)
    capture["endpoint"] = identifier
    if capture.get("browser"):
        app.say(f"{len(app.marks)} jobs marked; drag to adjust, release to finish")
        return
    if app.tab == "analytics":
        app.analytics_job = identifier
    else:
        app.cursor[app.tab] = capture["ids"].index(identifier)
    # The exact published order was validated above. Rebuilding accounting or
    # taking a Store snapshot for each pointer report adds no selection safety.
    app.selected_id = identifier
    app.say(f"{len(app.marks)} jobs marked; drag to adjust, release to finish")


@capture_handler("job_selection")
def handle_mouse(app, y, x, button="left", shift=False):
    state = initialize(app)
    capture = state["capture"]
    if any(type(value) is not int for value in (y, x)):
        state["capture"] = None
        return bool(capture)
    if button in ("press", "left"):
        state["discard_release"] = False
    elif button == "release" and capture is None and state["discard_release"]:
        state["discard_release"] = False
        return True
    if capture:
        if not _valid(app, capture):
            state["capture"] = None
            state["discard_release"] = button != "release"
            return button in ("release", "motion", "drag")
        if button in ("motion", "drag", "release"):
            identifier = _browser_hit(app, y, x) if capture.get("browser") else _hit(app, y, x)
            # Runtime status or auto-link headers may move the table while the
            # pointer stays still. Only vertical pointer movement chooses a
            # new endpoint; release retains the last deliberately marked ID.
            moved_cell = (y != capture["point"][0] or capture.get("browser") and
                          _browser_column(app, x) != _browser_column(app, capture["point"][1]))
            if (identifier and moved_cell
                    and (identifier != capture["anchor"] or capture["moved"])):
                _range(app, capture, identifier)
            if identifier:
                capture["point"] = (y, x)
            if button == "release":
                state["capture"] = None
                if capture["moved"]:
                    if capture.get("browser"):
                        from .history_browser import activate
                        activate(app, capture["endpoint"])
                    app.say(f"{len(app.marks)} jobs marked; use the existing job action and review the group")
            return True
        if button in ("left", "press"):
            state["capture"] = None
        else:
            return False
    if (button not in ("press", "left") or getattr(app, "mode", "main") != "main"
            or getattr(app, "toolbar_state", {}).get("menu") is not None
            or getattr(app, "toolbar_state", {}).get("panel")
            or getattr(app, "sel_anchor", None) is not None
            or getattr(app, "text_selection_state", {}).get("explicit")):
        return False
    identifier = _browser_hit(app, y, x)
    browser = identifier is not None
    if not browser and getattr(app, "tab", "") not in ROW_KINDS:
        return False
    identifier = identifier if browser else _hit(app, y, x)
    ids = _browser_order(app) if browser else _order(app)
    if identifier not in ids:
        return False
    scope, range_ids = _range_scope(app, identifier, browser, ids)
    old_anchor = state["anchors"].get(scope)
    anchor = old_anchor if (shift and old_anchor and old_anchor["ids"] == range_ids
                            and old_anchor["id"] in range_ids
                            and old_anchor["token"] == state["published_tokens"].get(old_anchor["id"])) else None
    if anchor is None:
        anchor = {"id": identifier, "ids": range_ids,
                  "token": state["published_tokens"].get(identifier), "base": set(app.marks), "last_range": ()}
    else:
        # Preserve explicit marks made between Shift-clicks, but never restore
        # marks cleared by another action or an ordinary replacement drag.
        base = (set(app.marks) - set(anchor.get("last_range", ()))) | (anchor["base"] & app.marks)
        anchor = dict(anchor, base=base)
    state["anchors"][scope] = anchor
    # Retain the established drag across Main and Recents. Shift-click has no
    # physical path between panes, so its anchor and range remain pane-local.
    drag_ids = ids if not shift else range_ids
    state["capture"] = {"tab": app.tab, "anchor": anchor["id"], "ids": ids,
                        "anchor_state": anchor, "previous_anchor": old_anchor,
                        "scope": scope, "range_ids": drag_ids,
                        "indices": {jid: index for index, jid in enumerate(drag_ids)},
                        "selection_base": anchor["base"],
                        "base": set(app.marks), "extend": bool(shift), "moved": False,
                        "base_tokens": dict(state["mark_tokens"]),
                        "base_scopes": dict(state["mark_scopes"]),
                        "browser": browser, "endpoint": identifier,
                        "view": getattr(app, "analytics_view", None) if app.tab == "analytics" else None,
                        "point": (y, x), "size": (getattr(app, "width", None), getattr(app, "height", None))}
    # A press retains ordinary row selection. Marks change only on a range drag.
    resume(app)
    if app.tab in ("jobs", "history"):
        from .job_panels import focus_main
        focus_main(app)
    if browser:
        from .history_browser import _current, activate
        frame = _current(app)
        state["capture"]["browser_geometry"] = (frame["dock"], frame["preference"])
        activate(app, identifier)
        state["capture"]["view"] = getattr(app, "analytics_view", None) if app.tab == "analytics" else None
    elif app.tab == "analytics":
        _select_advisor(app, identifier)
    else:
        app.cursor[app.tab] = ids.index(identifier)
    app.selected_id = identifier
    if shift:
        _range(app, state["capture"], identifier)
    if button == "left":
        state["capture"] = None
    return True
