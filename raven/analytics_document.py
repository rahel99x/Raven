"""Virtualized native Analytics Job Series document, with no source IO."""
from __future__ import annotations

from . import layout as L, scrollbars as S
from .pane_document import PaneDocument, DocumentTransform


def initialize(app):
    state = getattr(app, "analytics_document_state", None)
    if not isinstance(state, dict):
        state = {"identity": None, "top": 0, "frame": None}
        app.analytics_document_state = state
    return state


def eligible(app, height=None):
    if (getattr(app, "analytics_document_mode", False)
            or getattr(app, "tab", "") != "analytics"
            or getattr(app, "analytics_view", "") != "job"
            or getattr(app, "mode", "main") != "main" or height is None):
        return False
    from .ui.workspace_state import enabled
    return not enabled(app)


def _context(app):
    return (getattr(app, "mode", "main"), getattr(app, "tab", ""),
            getattr(app, "analytics_view", ""), getattr(app, "analytics_job", None),
            getattr(app, "width", None), getattr(app, "height", None))


def begin_render(app):
    # An embedded pane reads published services from its owning application.
    # Its outer Details viewport owns this document, so do not alter the
    # native Analytics workspace's frame or offset from the proxy renderer.
    # A palette or modal is drawn over the current document. Retain its last
    # Main viewport so a command entered there can run after returning to Main;
    # _current rejects it while blocked or after a job/page/size change.
    if (not getattr(app, "analytics_document_mode", False)
            and getattr(app, "mode", "main") == "main"):
        initialize(app)["frame"] = None


def prepare(app, job_id, attempt, width, height, sticky, count):
    """Resolve the painted offset before rasterizing any metric band."""
    state = initialize(app)
    identity = (str(job_id), attempt)
    if state["identity"] != identity:
        state["identity"], state["top"] = identity, 0
    page = max(0, int(height) - int(sticky) - 1)
    count = max(0, int(count))
    state["top"] = max(0, min(max(0, count - page), state["top"]))
    motion_context = (identity, width, page)
    from .scrolling import viewport
    painted = viewport(app, "analytics:series-document", state["top"], count, page,
                       context=motion_context)
    browser = getattr(app, "history_browser_state", {}).get("frame")
    content = getattr(app, "history_browser_content_rect", None)
    if (isinstance(browser, dict) and browser.get("tab") == "analytics"
            and browser.get("mode") == "main" and browser.get("geometry") ==
            (getattr(app, "width", None), getattr(app, "height", None))
            and content is not None):
        x, y = content.x, content.y
    else:
        x, y = 0, getattr(app, "body_origin", 0)
    # Navigation buttons and the accounting-window row precede this body.
    origin = y + getattr(app, "analytics_nav_rows", 0) + 1
    state["frame"] = {"context": _context(app), "page": page, "count": count,
                      "painted": painted, "width": width, "sticky": sticky,
                      "rect": (origin + sticky, x, origin + sticky + page, x + width)}
    return painted, page


def finish(app, rows, sticky, painted, page, width, chart_mark, glyphs):
    """Slice the exact staged rows, then translate and clip graph controls."""
    state = initialize(app)
    count = max(0, len(rows) - sticky)
    frame = state["frame"]
    frame["count"] = count
    visible = rows[sticky + painted:sticky + painted + page]
    first = painted + 1 if count and page else 0
    last = min(count, painted + page) if page else 0
    note = f" Series rows {first}-{last}/{count} {glyphs.dot} PgUp/PgDn or wheel; :series-scroll home/end"
    output = rows[:sticky] + visible + [L.clip_row([(note, "dim")], width)]
    # The standalone and embedded document paths use the same geometry
    # contract. Hidden cards contribute source rows without publishing any
    # interactive targets outside the visible metric viewport.
    document = PaneDocument.capture(app, rows, chart_mark=chart_mark).transform(
        output, DocumentTransform(-painted, (sticky, 0, sticky + page, width)))
    output, _ = document.publish(app)
    header = sticky - 1 if sticky else len(output) - 1
    if page > 0 and width >= 6:
        # The jump controls share the pane heading; no table column is added.
        if count > page:
            output[header] = L.clip_row([("    ", "")] + output[header], width)
        S.register(app, "analytics:series-document", (sticky, 0, sticky + page, width),
                   count, page, state["top"], painted,
                   lambda value: state.__setitem__("top", value),
                   context=(state["identity"], width, page), header=(header, 0, width))
    return output


def _current(app):
    state = initialize(app)
    frame = state["frame"]
    if not isinstance(frame, dict) or frame["context"] != _context(app):
        return None
    if not eligible(app, frame["page"]):
        return None
    toolbar = getattr(app, "toolbar_state", {})
    if toolbar.get("menu") is not None or toolbar.get("panel"):
        return None
    return frame


def scroll(app, action, *, wheel=False):
    frame = _current(app)
    if frame is None:
        return False
    from .scrolling import note_input
    note_input(app, "wheel" if wheel else "key")
    state = initialize(app)
    page = max(1, frame["page"])
    top, limit = state["top"], max(0, frame["count"] - frame["page"])
    state["top"] = max(0, min(limit, {
        "up": top - 3, "down": top + 3, "page-up": top - page,
        "page-down": top + page, "home": 0, "end": limit,
    }[action]))
    return True


def handle_key(app, key):
    if key not in ("pgup", "pgdn"):
        return False
    if getattr(app, "interaction_state", {}).get("active"):
        return False
    return scroll(app, "page-up" if key == "pgup" else "page-down")


def handle_mouse(app, y, x, button="left", shift=False):
    if button not in ("wheel-up", "wheel-down", "wheel_up", "wheel_down"):
        return False
    frame = _current(app)
    if frame is None or not isinstance(y, int) or not isinstance(x, int):
        return False
    top, left, bottom, right = frame["rect"]
    if not top <= y < bottom or not left <= x < right:
        return False
    return scroll(app, "up" if "up" in button else "down", wheel=True)


def command_names():
    return ["series-scroll"]


def run_command(app, args):
    if not args or args[0] != "series-scroll":
        return False
    actions = ("up", "down", "page-up", "page-down", "home", "end")
    if len(args) != 2 or args[1] not in actions:
        app.fail("series-scroll <up|down|page-up|page-down|home|end>")
        return True
    if not scroll(app, args[1]):
        app.fail("Draw native Analytics Job Series before scrolling its metrics")
    return True


def overlay(views, snap, app, width, height):
    return None
