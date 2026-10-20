"""Bounded, accessible list presentation menus; no scheduler work on paint.

View and order are local preferences. Group membership and disclosure remain
shared between listings; deleting assignments therefore has its own review.
"""
from __future__ import annotations

import time

from . import layout as L

MODE = "job_list_menu"
SECTIONS = ("view", "order", "groups", "manage", "remove")


def initialize(app):
    state = getattr(app, "job_list_menu_state", None)
    if not isinstance(state, dict):
        state = app.job_list_menu_state = dict(token=0, context=None, section=None,
            items=(), cursor=0, top=0, page=1, hits=(), paint_token=None, pressed=None, hover=None, aggregate_guard=None)
    return state


def active(app):
    return getattr(app, "mode", "main") == MODE


def _invalidate(app):
    getattr(app, "interaction_state", {}).update(active=False, focused=None, pressed=None, frame_required=True)


def close(app):
    state = initialize(app)
    state.update(hits=(), paint_token=None, pressed=None, hover=None, items=(), aggregate_guard=None)
    if active(app):
        app.mode = "main"
    _invalidate(app)


def _choices(app, context, section):
    from .job_list_controls import mode, batch_mode
    view, order = mode(app, context), batch_mode(app, context)
    if section == "view":
        return (("grouped", "Grouped — contiguous batches", view == "grouped"),
                ("sequential", "Sequential — individual jobs", view == "sequential"))
    if section == "order":
        choices = (("sort-min", "Fewest running first", order == "min"),
                   ("sort-max", "Most running first", order == "max"))
        return choices if context == "jobs" else choices + (("column-order", "Use column order / completion time", order is None),)
    if section == "groups":
        return (("expand", "Expand all groups", False), ("collapse", "Collapse all groups", False),
                ("manage", "Manage group assignments…", False))
    if section == "manage":
        return (("auto", "Detect automatic groups", False), ("undo", "Undo automatic grouping", False),
                ("remove", "Remove all group assignments…", False),
                ("restore", "Restore removed assignments", False), ("groups", "Back to Groups", False))
    return (("manage", "Keep assignments / go back", False), ("clear", "Remove assignments from every list", False))


def open_menu(app, context, section):
    from .job_list_controls import CONTEXTS
    if context not in CONTEXTS or section not in SECTIONS:
        return False
    if getattr(app, "mode", "main") not in ("main", MODE):
        return False
    state = initialize(app)
    state.update(token=state["token"] + 1, context=context, section=section,
        items=_choices(app, context, section), cursor=0, top=0, hits=(), pressed=None, hover=None,
        paint_token=None, aggregate_guard=None, tab=getattr(app, "tab", ""),
        revision=getattr(app, "manual_job_groups_revision", 0))
    selected = next((index for index, item in enumerate(state["items"]) if item[2]), 0)
    state["cursor"] = selected
    # A menu owns subsequent input, including releases from the opening click.
    for name in ("job_selection_state", "job_group_drag_state", "group_queue_drag_state"):
        gesture = getattr(app, name, {})
        if gesture.get("capture"):
            gesture["discard_release"] = True
        gesture.update(capture=None, pending=None, armed=None)
    toolbar = getattr(app, "toolbar_state", {})
    toolbar.update(menu=None, panel=None, focus="", dragging=False, menu_hits=[], menu_token=None)
    app.mode = MODE
    _invalidate(app)
    return True


def _activate(app, index, *, pointer=False):
    state = initialize(app)
    if not active(app) or type(index) is not int or not 0 <= index < len(state["items"]):
        return
    action = state["items"][index][0]
    if action in SECTIONS:
        previous_hit = next(((row, left, right) for row, left, right, actual in state["hits"]
                             if actual == index), None) if pointer else None
        open_menu(app, state["context"], action)
        if previous_hit is not None:
            # Native adapters can append an aggregate click after release. It
            # must not activate a different action under the new submenu.
            state["aggregate_guard"] = (time.monotonic() + 0.5, *previous_hit)
        return
    context = state["context"]
    if action == "clear" and state["revision"] != getattr(app, "manual_job_groups_revision", 0):
        open_menu(app, context, "remove")
        app.say("Group assignments changed; review the removal again")
        return
    close(app)
    if action in ("grouped", "sequential", "sort-min", "sort-max", "column-order"):
        from .job_list_controls import run_command
        run_command(app, ["joblist", context, action], preserve_view=True)
    else:
        from .grouping_toolbar import run_command
        run_command(app, ["grouping", action])


def run_command(app, args):
    if not args or args[0] != "joblistmenu":
        return False
    state = initialize(app)
    if args[1:] == ["cancel"]:
        close(app)
    elif len(args) == 3:
        open_menu(app, args[1], args[2])
    elif (len(args) == 4 and args[1] == "choose" and args[2] == str(state["token"])
          and args[3].isdigit() and _fresh(app)):
        _activate(app, int(args[3]))
    return True


def handle_key(app, key):
    if not active(app):
        return False
    state = initialize(app)
    if key == "f8" or getattr(app, "interaction_state", {}).get("active") and key not in ("esc", "ctrl-c", "q"):
        from .interaction import handle_key as focus_key
        if focus_key(app, key):
            return True
    if key in ("esc", "ctrl-c", "q"):
        close(app)
    elif key in ("up", "down", "left", "right", "tab", "btab", "pgup", "pgdn", "home", "end"):
        count, page = len(state["items"]), state["page"]
        delta = {"up": -1, "down": 1, "left": -1, "right": 1, "tab": 1, "btab": -1,
                 "pgup": -page, "pgdn": page, "home": -count, "end": count}[key]
        cursor = state["cursor"] + delta
        state["cursor"] = cursor % count if key in ("tab", "btab") else max(0, min(count - 1, cursor))
        state.update(pressed=None, hover=None)
    elif key in ("enter", "space"):
        _activate(app, state["cursor"])
    return True


def tick(app):
    if active(app) and initialize(app).get("tab") != getattr(app, "tab", ""):
        close(app)


def _paint_context(app, width=None, height=None):
    state = initialize(app)
    return (state["token"], getattr(app, "mode", "main"), getattr(app, "tab", ""), state["top"],
            width if width is not None else getattr(app, "width", None),
            height if height is not None else getattr(app, "height", None))


def _fresh(app):
    state = initialize(app)
    return bool(active(app) and state.get("paint_token") == _paint_context(app))


def overlay(views, snap, app, width, height):
    state = initialize(app)
    state.update(hits=(), paint_token=None)
    if not active(app):
        return None
    from .research_text import clean
    room, available = max(1, min(72, width - 8)), max(1, height - 4)
    section, context = state["section"], state["context"]
    title = {"view": "View", "order": "Order", "groups": "Groups", "manage": "Manage assignments", "remove": "Remove assignments?"}[section]
    titles = {"recent": "Recents", "jobs": "Jobs", "group": "Account jobs"}
    scope = titles.get(context, context.replace("history:", "").replace(":", " / ").title())
    descriptions = {
        "view": ("Changes this list only. Group assignments and folds are kept.",),
        "order": ("Grouped batches: ties use fewer total jobs. Raven Heavy stays first.",
                  "Sequential view keeps individual row order; this choice is remembered."),
        "groups": ("Expand/collapse is shared by lists. Assignments stay unchanged.",),
        "manage": ("Assignments are shared across Jobs, Recents and History.",
                   "To hide groups only, choose View > Sequential."),
        "remove": ("This removes ALL shared assignments and turns automatic grouping off.",
                   "Jobs and recordings remain. View > Sequential keeps assignments.",
                   "You can restore removal until another membership edit.")}
    # Text has a fixed bound; rendering does not inspect jobs or copy snapshots.
    import textwrap
    prefix = [[(" " + clean(line, views.g.ascii), "dim")]
              for text in descriptions[section] for line in textwrap.wrap(text, max(1, room - 1))]
    if available < len(prefix) + 3:
        prefix = []
    page = max(1, available - len(prefix) - (1 if available >= 4 else 0))
    count = len(state["items"])
    top = max(0, min(state["top"], max(0, count - page)))
    cursor = state["cursor"]
    if cursor < top:
        top = cursor
    elif cursor >= top + page:
        top = cursor - page + 1
    state.update(top=top, page=page)
    rows, indices = list(prefix), []
    for index in range(top, min(count, top + page)):
        _, label, selected = state["items"][index]
        marker = "*" if selected else " "
        style = "sel+bold" if cursor == index else "accent+under" if state["hover"] == index else "text"
        indices.append((len(rows), index))
        rows.append([(L.cut(f" {marker} {clean(label, views.g.ascii)} ", room, views.g.ascii), style)])
    if len(rows) < available:
        rows.append([(L.cut(" Arrows choose | Enter select | Esc close", room, views.g.ascii), "dim")])
    rendered = L.box(views.g, rows, width, height, f"{scope} / {title}", min_width=1)
    hits = []
    for offset, index in indices:
        if offset + 1 < len(rendered) - 1:
            y, x, segments = rendered[offset + 1]
            right = x + L.vlen(L.row_text(segments)) - 1
            if x + 1 < right:
                hits.append((y, x + 1, right, index))
    state.update(hits=tuple(hits), paint_token=_paint_context(app, width, height))
    return rendered


def controls(app):
    if not _fresh(app):
        return ()
    from .interaction import Control, Rect
    state = initialize(app)
    token = str(state["token"])
    return tuple(Control(f"job-list-menu:{token}:{index}", state["items"][index][1],
                         Rect(y, left, y + 1, right), ("command", f"joblistmenu choose {token} {index}"),
                         "job-list-menu", layer=2)
                 for y, left, right, index in state["hits"])


def handle_mouse(app, y, x, button="left", shift=False):
    if not active(app):
        return False
    state = initialize(app)
    guard = state.get("aggregate_guard")
    if guard is not None:
        deadline, row, left, right = guard
        inside = y == row and left <= x < right
        if (time.monotonic() >= deadline or button == "press"
                or button in ("motion", "drag") and not inside):
            state["aggregate_guard"] = None
        elif button in ("left", "double") and inside:
            return True
    if not _fresh(app):
        state.update(pressed=None, hover=None)
        return True
    hit = next((index for row, left, right, index in state["hits"] if row == y and left <= x < right), None)
    if button in ("motion", "drag"):
        state["hover"] = hit
    elif button in ("wheel-up", "wheel-down", "wheel_up", "wheel_down"):
        handle_key(app, "up" if button in ("wheel-up", "wheel_up") else "down")
    elif button == "press":
        state["pressed"] = (state["paint_token"], hit) if hit is not None else None
        if hit is not None:
            state["cursor"] = hit
    elif button == "release":
        pressed, state["pressed"] = state["pressed"], None
        if hit is not None and pressed == (state["paint_token"], hit):
            _activate(app, hit, pointer=True)
    elif button in ("left", "double") and hit is not None:
        _activate(app, hit, pointer=True)
    return True
