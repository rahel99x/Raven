"""Published exact-job selection identities and geometry-only queries.

Selection actions and cancellation remain in the effectful feature owner.
"""

from __future__ import annotations
from .geometry import Rect

MAX_SELECTION = 50000

ROW_KINDS = {
    "jobs": {"job", "recent"},
    "history": {"fin"},
    "group": {"group"},
    "deps": {"dep"},
    "analytics": {"advisor_job"},
}

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


def cleared(app, tab=None):
    return bool(
        initialize(app)["deselected"].get(tab or getattr(app, "tab", ""), False)
    )


def lines_cleared(app):
    return bool(initialize(app)["lines_deselected"])


def publish(app, rows, hits, width, height, snap=None):
    """Bind painted job identities, then publish History's clipped list."""
    if isinstance(snap, dict) and getattr(app, "tab", "") in JOB_SCOPES:
        from raven.jobs.group_membership import SelectionTokenCache

        state = initialize(app)
        cache = state.get("token_cache")
        if cache is None:
            cache = state["token_cache"] = SelectionTokenCache()
        ids = _order(app)
        browser = getattr(app, "history_browser_state", {})
        if (browser.get("frame") or {}).get("tab") == getattr(app, "tab", ""):
            ids = tuple(dict.fromkeys((*ids, *_browser_order(app))))
        from raven.jobs.group_domain import registry

        frame = getattr(app, "job_groups_frame_index", None)
        checked = (
            bool(getattr(app, "table_state", {}).get("groups"))
            and isinstance(frame, tuple)
            and len(frame) == 2
            and frame[0] is snap
        )
        tokens = (
            cache.update(snap, ids, registry=registry(app) if checked else None)
            if ids
            else {}
        )
        previous = state["published_tokens"]
        capture = state.get("capture")
        if capture and any(
            previous.get(identifier) != tokens.get(identifier)
            for identifier in capture["ids"]
        ):
            state["capture"] = None
            state["discard_release"] = True
        state["published_tokens"] = tokens
        marks = getattr(app, "marks", ())
        bound = state["mark_tokens"]
        state["mark_tokens"] = {
            identifier: bound.get(identifier, tokens.get(identifier))
            for identifier in marks
            if identifier in bound or identifier in tokens
        }
        scopes = state["mark_scopes"]
        scope = _mark_scope(app, state)
        state["mark_scopes"] = {
            identifier: scopes.get(identifier, scope)
            for identifier in marks
            if identifier in scopes or identifier in tokens
        }
    app.history_jobs_rect = None
    if getattr(app, "tab", "") != "history" or height is None:
        return
    from .workspace_state import enabled
    from .text_rows import _section_title

    headers = [
        y
        for y, kind, value in hits
        if kind == "sort_header"
        and isinstance(value, (tuple, list))
        and value[:1] == ("history",)
    ]
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


def _order(app):
    tab = getattr(app, "tab", "")
    if tab == "jobs":
        return tuple(
            (
                list(getattr(app, "visible_ids", []))
                + list(getattr(app, "recent_ids", []))
            )[:MAX_SELECTION]
        )
    if tab == "history":
        cached = getattr(app, "last_history_ids", None)
        if isinstance(cached, (list, tuple)):
            return tuple(cached[:MAX_SELECTION])
        return tuple(record.id for record in app.history_jobs())[:MAX_SELECTION]
    if tab == "analytics" and getattr(app, "analytics_view", "") == "advisor":
        return tuple(getattr(app, "analytics_advisor_ids", ()))[:MAX_SELECTION]
    if tab in ("group", "deps"):
        return tuple(getattr(app, "group_ids" if tab == "group" else "dep_ids", []))[
            :MAX_SELECTION
        ]
    return ()


def _browser_order(app):
    from .history_state import initialize as browser_state

    state = browser_state(app)
    return state.get("selection_ids", ())[:MAX_SELECTION]


def _browser_hit(app, y, x):
    from .history_state import _current

    frame = _current(app)
    rect = getattr(app, "history_browser_rect", None)
    if not frame or frame["dock"] == "off" or not rect or not rect.contains(y, x):
        return None
    # Disclosure and dock controls occur before overlapping whole-row hits.
    value = next(
        (
            value
            for row, kind, value in frame["hits"]
            if row == y - rect.y
            and kind == "control"
            and value["left"] <= x - rect.x < value["right"]
        ),
        None,
    )
    prefix = "history:" + app.tab + ":job:"
    return (
        value["id"][len(prefix) :] if value and value["id"].startswith(prefix) else None
    )


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
    return (
        getattr(app, "mode", "main") == "main"
        and getattr(app, "tab", "") == "analytics"
        and getattr(app, "analytics_view", "") == "advisor"
        and _hit(app, y, x) is not None
    )


def context(app):
    """Return only the currently focused, published exact-job list.

    ``ids`` includes offscreen visible-order records but never hidden members
    of a collapsed summary. No source reads or inference runs on input.
    """
    state = initialize(app)
    toolbar = getattr(app, "toolbar_state", {})
    if (
        getattr(app, "mode", "main") != "main"
        or cleared(app)
        or toolbar.get("menu") is not None
        or toolbar.get("panel")
        or getattr(app, "text_selection_state", {}).get("explicit")
        or getattr(app, "sel_anchor", None) is not None
        or getattr(app, "text_selection_state", {}).get("capture")
        or getattr(app, "chart_interaction_state", {}).get("capture")
        or getattr(app, "metric_live_state", {}).get("capture")
    ):
        return None
    chart = getattr(app, "chart_interaction_state", {})
    point = chart.get("pointer")
    if chart.get("hovered") and isinstance(point, tuple) and len(point) == 2:
        from .chart_state import _at

        if _at(app, *point) is not None:
            return None
    tab = getattr(app, "tab", "")
    browser = getattr(app, "history_browser_state", {})
    if browser.get("focused"):
        from .history_state import _current

        frame = _current(app)
        if frame and frame["dock"] != "off":
            ids = _browser_order(app)
            index = min(max(0, browser.get("index", 0)), len(ids) - 1)
            return _context_result(
                app, "history:" + tab, ids, ids[index] if ids else None
            )
        return None
    if tab == "analytics":
        if getattr(app, "analytics_view", "") != "advisor" or not state.get(
            "advisor_focus"
        ):
            return None
        ids = _order(app)
        selected_id = getattr(app, "analytics_job", None)
        return _context_result(
            app, "analytics:advisor", ids, selected_id if selected_id in ids else None
        )
    if tab not in ("jobs", "history", "group", "deps"):
        return None
    if (
        getattr(getattr(app, "layout_state", None), "focus", "main") == "details"
        or tab in ("jobs", "history")
        and getattr(app, "job_panel_state", {}).get("focus")
    ):
        return None
    ids = _order(app)
    index = getattr(app, "cursor", {}).get(tab, 0)
    chosen = ids[max(0, min(len(ids) - 1, index))] if ids else None
    scope = (
        "recent" if tab == "jobs" and chosen in getattr(app, "recent_ids", ()) else tab
    )
    return _context_result(app, scope, ids, chosen)


def _context_result(app, scope, ids, chosen):
    state = initialize(app)
    published, bound = state["published_tokens"], state["mark_tokens"]
    marks = getattr(app, "marks", ())
    tokens = {
        identifier: bound.get(identifier, published.get(identifier))
        if identifier in marks
        else published.get(identifier)
        for identifier in ids
    }
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
    state["mark_tokens"] = {
        identifier: value
        for identifier, value in state["mark_tokens"].items()
        if identifier in app.marks
    }
    state["mark_scopes"] = {
        identifier: value
        for identifier, value in state["mark_scopes"].items()
        if identifier in app.marks
    }
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
    if not (
        0 <= x < getattr(app, "width", 120) and 0 <= y < getattr(app, "height", 100000)
    ):
        return None
    if getattr(app, "tab", "") in ("jobs", "history"):
        from .panel_state import contains

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
        value = next(
            (
                value
                for row, kind, value in getattr(app, "last_hits", ())
                if row == y
                and kind == "control"
                and isinstance(value, dict)
                and value.get("left", -1) <= x < value.get("right", -1)
            ),
            None,
        )
        prefix = "advisor-job:"
        return (
            value["id"][len(prefix) :]
            if value and str(value.get("id", "")).startswith(prefix)
            else None
        )
    # Native rows span the whole pane; their overlaid disclosure/grip buttons
    # keep ownership, including synthesized clicks with no preceding press.
    if any(
        row == y
        and kind == "control"
        and isinstance(value, dict)
        and value.get("left", -1) <= x < value.get("right", -1)
        for row, kind, value in getattr(app, "last_hits", ())
    ):
        return None
    kinds = ROW_KINDS.get(getattr(app, "tab", ""), set())
    return next(
        (
            identifier
            for row, kind, identifier in getattr(app, "last_hits", [])
            if row == y and kind in kinds and isinstance(identifier, str)
        ),
        None,
    )


def active(app):
    return bool(initialize(app)["capture"])


def _valid(app, capture):
    return (
        getattr(app, "mode", "main") == "main"
        and app.tab == capture["tab"]
        and getattr(app, "toolbar_state", {}).get("menu") is None
        and not getattr(app, "toolbar_state", {}).get("panel")
        and (getattr(app, "width", None), getattr(app, "height", None))
        == capture["size"]
        and (_browser_order(app) if capture.get("browser") else _order(app))
        == capture["ids"]
        and (not capture.get("browser") or _browser_valid(app, capture))
        and (
            capture.get("view") is None
            or capture["view"] == getattr(app, "analytics_view", None)
        )
    )


def _browser_valid(app, capture):
    from .history_state import _current

    frame = _current(app)
    return bool(
        frame
        and frame["dock"] != "off"
        and (frame["dock"], frame["preference"]) == capture["browser_geometry"]
    )
