"""Mouse range selection of exact job IDs for existing marked-job actions."""

from __future__ import annotations
from raven.ui.chart_state import cancel as cancel_chart
from raven.ui.workspace_actions import focus_main
from raven.ui.chart_state import cancel
from raven.ui.history_state import _current

from raven.ui.selection_effects import (
    resume as resume,
    resume_lines as resume_lines,
    clear_lines as clear_lines,
)

from raven.ui.selection_state import (
    initialize as initialize,
    selected as selected,
    cleared as cleared,
    lines_cleared as lines_cleared,
    publish as publish,
    _order as _order,
    _browser_order as _browser_order,
    _browser_hit as _browser_hit,
    _browser_column as _browser_column,
    advisor_pointer as advisor_pointer,
    context as context,
    _context_result as _context_result,
    _mark_scope as _mark_scope,
    _bind_marks as _bind_marks,
    _range_scope as _range_scope,
    _hit as _hit,
    active as active,
    _valid as _valid,
    _browser_valid as _browser_valid,
    MAX_SELECTION as MAX_SELECTION,
    ROW_KINDS as ROW_KINDS,
    JOB_SCOPES as JOB_SCOPES,
)

from raven.pointer_routing import capture_handler
from raven.ui.owner_ports import selection as actions


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
        focus_main(app)
    from raven.pointer_routing import cancel_capture

    cancel_chart(app)
    cancel_capture(app, reason="selection-clear")
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
    if not (
        0 <= x < getattr(app, "width", 120) and 0 <= y < getattr(app, "height", 100000)
    ):
        return False
    mode = getattr(app, "mode", "main")
    chart_options = {"button": button}
    if shift:
        chart_options["shift"] = True
    if mode in ("log_tools_page", "log_tools_results", "log_tools_marks"):
        # The modal keeps its own source and control ownership. Its clear must
        # precede the global toolbar so even a top-row right-click is inert.
        return actions(app).log_mouse(app, y, x, button=button)
    if mode in ("help", "details", "analysis"):
        if mode == "analysis" and actions(app).graph_mouse(app, y, x, **chart_options):
            return True
        clear_lines(app)

        cancel(app)
        if mode == "analysis":
            if actions(app).analysis_context(app, y, x, button=button):
                return True
        app.say("Text selection cleared; dialog remains open")
        return True
    if mode != "main":
        return False
    tab = getattr(app, "tab", "")
    if actions(app).graph_mouse(app, y, x, **chart_options):
        return True
    if tab == "history":
        rect = getattr(app, "history_jobs_rect", None)
        if rect is not None and rect.contains(y, x):
            return actions(app).history_export_mouse(app, y, x, button=button)
    clear(app)
    return True


def command_names():
    return ["advisor-job"]


def run_command(app, args):
    if not args or args[0] != "advisor-job":
        return False
    if (
        len(args) != 2
        or getattr(app, "mode", "main") != "main"
        or getattr(app, "tab", "") != "analytics"
        or getattr(app, "analytics_view", "") != "advisor"
        or args[1] not in getattr(app, "analytics_advisor_ids", ())
    ):
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


def pointer_focus(app, y, x):
    """Revoke stale list focus before a fresh press can reach another owner."""
    state = initialize(app)
    actions(app).graph_hover(app, y, x)
    if not advisor_pointer(app, y, x):
        state["advisor_focus"] = False
    browser = getattr(app, "history_browser_state", {})
    rect = getattr(app, "history_browser_rect", None)
    if browser.get("focused") and (rect is None or not rect.contains(y, x)):
        browser["focused"] = False


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
    if (
        state.get("advisor_focus")
        and context(app)
        and key in ("up", "down", "home", "end", "esc")
    ):
        if key == "esc":
            state["advisor_focus"] = False
            return True
        ids = _order(app)
        if ids:
            current = getattr(app, "analytics_job", None)
            index = ids.index(current) if current in ids else 0
            index = (
                0
                if key == "home"
                else len(ids) - 1
                if key == "end"
                else max(0, min(len(ids) - 1, index + (1 if key == "down" else -1)))
            )
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
    first, last = (
        sorted((ids.index(capture["anchor"]), ids.index(identifier)))
        if indices is None
        else sorted((indices[capture["anchor"]], indices[identifier]))
    )
    base = capture.get("selection_base", capture["base"])
    app.marks = (set(base) if capture["extend"] else set()) | set(ids[first : last + 1])
    anchor_state = capture.get("anchor_state")
    if anchor_state is not None:
        anchor_state["base"] = set(base) if capture["extend"] else set()
        anchor_state["last_range"] = ids[first : last + 1]
    _bind_marks(app, ids[first : last + 1])
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
            identifier = (
                _browser_hit(app, y, x) if capture.get("browser") else _hit(app, y, x)
            )
            # Runtime status or auto-link headers may move the table while the
            # pointer stays still. Only vertical pointer movement chooses a
            # new endpoint; release retains the last deliberately marked ID.
            moved_cell = (
                y != capture["point"][0]
                or capture.get("browser")
                and _browser_column(app, x) != _browser_column(app, capture["point"][1])
            )
            if (
                identifier
                and moved_cell
                and (identifier != capture["anchor"] or capture["moved"])
            ):
                _range(app, capture, identifier)
            if identifier:
                capture["point"] = (y, x)
            if button == "release":
                state["capture"] = None
                if capture["moved"]:
                    if capture.get("browser"):
                        actions(app).history_activate(app, capture["endpoint"])
                    app.say(
                        f"{len(app.marks)} jobs marked; use the existing job action and review the group"
                    )
            return True
        if button in ("left", "press"):
            state["capture"] = None
        else:
            return False
    if (
        button not in ("press", "left")
        or getattr(app, "mode", "main") != "main"
        or getattr(app, "toolbar_state", {}).get("menu") is not None
        or getattr(app, "toolbar_state", {}).get("panel")
        or getattr(app, "sel_anchor", None) is not None
        or getattr(app, "text_selection_state", {}).get("explicit")
    ):
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
    anchor = (
        old_anchor
        if (
            shift
            and old_anchor
            and old_anchor["ids"] == range_ids
            and old_anchor["id"] in range_ids
            and old_anchor["token"] == state["published_tokens"].get(old_anchor["id"])
        )
        else None
    )
    if anchor is None:
        anchor = {
            "id": identifier,
            "ids": range_ids,
            "token": state["published_tokens"].get(identifier),
            "base": set(app.marks),
            "last_range": (),
        }
    else:
        # Preserve explicit marks made between Shift-clicks, but never restore
        # marks cleared by another action or an ordinary replacement drag.
        base = (set(app.marks) - set(anchor.get("last_range", ()))) | (
            anchor["base"] & app.marks
        )
        anchor = dict(anchor, base=base)
    state["anchors"][scope] = anchor
    # Retain the established drag across Main and Recents. Shift-click has no
    # physical path between panes, so its anchor and range remain pane-local.
    drag_ids = ids if not shift else range_ids
    state["capture"] = {
        "tab": app.tab,
        "anchor": anchor["id"],
        "ids": ids,
        "anchor_state": anchor,
        "previous_anchor": old_anchor,
        "scope": scope,
        "range_ids": drag_ids,
        "indices": {jid: index for index, jid in enumerate(drag_ids)},
        "selection_base": anchor["base"],
        "base": set(app.marks),
        "extend": bool(shift),
        "moved": False,
        "base_tokens": dict(state["mark_tokens"]),
        "base_scopes": dict(state["mark_scopes"]),
        "browser": browser,
        "endpoint": identifier,
        "view": getattr(app, "analytics_view", None)
        if app.tab == "analytics"
        else None,
        "point": (y, x),
        "size": (getattr(app, "width", None), getattr(app, "height", None)),
    }
    # A press retains ordinary row selection. Marks change only on a range drag.
    resume(app)
    if app.tab in ("jobs", "history"):
        focus_main(app)
    if browser:
        frame = _current(app)
        state["capture"]["browser_geometry"] = (frame["dock"], frame["preference"])
        actions(app).history_activate(app, identifier)
        state["capture"]["view"] = (
            getattr(app, "analytics_view", None) if app.tab == "analytics" else None
        )
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
