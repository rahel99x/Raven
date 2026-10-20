"""Bounded pane scrollbars over cached geometry; gestures never fetch source data."""

from __future__ import annotations
from dataclasses import replace
from .ui.geometry import (
    Pane as Pane,
    pane_state as initialize,
    pane_rect as _rect,
    clip_pane_rect as _clip,
    take_panes as take_since,
    put_panes as put_records,
    map_panes as map_records,
)
from .ui.geometry import Rect
from . import layout as L, scrolling

from .pointer_routing import capture_handler

MAX_PANES = 128
MAX_MANUAL = 128


from .ui.scrollbar_state import begin_frame as begin_frame


from .ui.scrollbar_state import register as register


from .ui.scrollbar_state import mark as mark


def place_since(app, first, *, dy=0, dx=0, row_map=None, clip=None):
    put_records(
        app,
        map_records(take_since(app, first), dy=dy, dx=dx, row_map=row_map, clip=clip),
    )


from .ui.scrollbar_state import manual as manual


from .ui.scrollbar_state import set_manual as set_manual


from .ui.scrollbar_state import resume as resume


def commit_selection_gesture(app):
    """A fresh press ends an earlier range capture without discarding its text."""
    getattr(app, "job_selection_state", {})["capture"] = None
    text = getattr(app, "text_selection_state", {})
    if text.get("capture"):
        text["capture"] = None
        text["discard_release"] = True


def _claim(app):
    # A rail/header press owns the new gesture, including a release missing
    # from an older graph, range, divider or dock gesture.
    commit_selection_gesture(app)
    from .pointer_routing import cancel_capture

    cancel_capture(app, reason="scrollbar-claim")
    getattr(app, "history_browser_state", {})["drag"] = None


def _token(app):
    panel = getattr(app, "job_panel_state", {})
    layout = getattr(app, "layout_state", None)
    return (
        getattr(app, "mode", "main"),
        getattr(app, "tab", ""),
        getattr(app, "width", 0),
        getattr(app, "height", 0),
        getattr(app, "selected_id", None),
        getattr(app, "analytics_job", None),
        getattr(app, "analytics_view", None),
        getattr(app, "research_job_id", None),
        getattr(app, "research_view", None),
        getattr(app, "log_job", None),
        getattr(getattr(app, "logs", None), "path", None),
        panel.get("mode"),
        panel.get("analytics_view"),
        panel.get("research_view"),
        getattr(layout, "ratio", None),
        getattr(layout, "maximized", None),
        getattr(layout, "density", None),
    )


def publish(app, width, height, *, overlays=()):
    state = initialize(app)
    screen = Rect(0, 0, max(0, height or 0), max(0, width))
    panes = []
    for pane in state["staged"]:
        rect = _clip(pane.rect, screen)
        if rect is not None:
            panes.append(replace(pane, rect=rect))
    state["panes"] = tuple(panes)
    for pane in panes:
        entry = state["manual"].get(pane.key)
        if entry is not None and entry["context"] == pane.context:
            entry["target"] = min(pane.limit, entry["target"])
    state["token"] = _token(app)
    state["modal"] = getattr(app, "mode", "main") != "main"
    state["menu_rect"] = _rect(getattr(app, "toolbar_state", {}).get("menu_rect"))
    state["blocked"] = tuple(
        Rect(y, x, y + 1, x + L.vlen(L.row_text(row))) for y, x, row in overlays
    )
    capture = state["capture"]
    if capture and (
        capture["token"] != state["token"]
        or not any(pane.signature == capture["signature"] for pane in panes)
    ):
        cancel(app)
    return state["panes"]


def _visible(state, pane, y, x):
    menu = state.get("menu_rect")
    if menu and menu.contains(y, x):
        return False
    if state.get("modal") and pane.layer == 0:
        return False
    if pane.layer > 0:
        return True
    return not any(rect.contains(y, x) for rect in state["blocked"])


def _current(app):
    state = initialize(app)
    if state["token"] != _token(app):
        if state["capture"]:
            cancel(app)
        return ()
    return state["panes"]


def _thumb(pane):
    height = pane.rect.bottom - pane.rect.top
    size = min(height, max(1, round(height * pane.page / max(1, pane.count))))
    travel = max(0, height - size)
    start = pane.rect.top + (
        round(travel * pane.painted / pane.limit) if pane.limit else 0
    )
    return start, size, travel


def _rail_hit(state, pane, y, x):
    return (
        x == pane.rect.right - 1
        and pane.rect.top <= y < pane.rect.bottom
        and _visible(state, pane, y, x)
    )


def _seek_target(pane, y, grab):
    _, size, travel = _thumb(pane)
    grab = min(grab, max(0, size - 1))
    fraction = (y - pane.rect.top - grab) / max(1, travel)
    return round(max(0, min(1, fraction)) * pane.limit)


def _set(app, pane, target):
    target = max(0, min(pane.limit, int(target)))
    current = manual(app, pane.key, context=pane.context)
    if current is not None and target == current:
        # Many terminal reports land in the same thumb cell. They must not
        # mark the entire document dirty or restart smoothing needlessly.
        return
    pane.setter(target)
    set_manual(app, pane.key, target, context=pane.context)
    scrolling.note_input(app, "wheel")
    focus = getattr(app, "interaction_state", None)
    if isinstance(focus, dict):
        focus["frame_required"] = True


def activate(app, key, direction):
    if direction not in ("top", "bottom"):
        return False
    pane = next((p for p in _current(app) if p.key == key), None)
    if pane is None:
        return False
    _set(app, pane, 0 if direction == "top" else pane.limit)
    return True


def keyboard_scroll(app, key, action):
    """Scroll an already published document without rebuilding its source."""
    pane = next((p for p in _current(app) if p.key == key), None)
    if pane is None:
        return False
    current = manual(app, key, context=pane.context)
    current = pane.target if current is None else current
    targets = {
        "up": current - 1,
        "down": current + 1,
        "page_up": current - pane.page,
        "page_down": current + pane.page,
        "home": 0,
        "end": pane.limit,
    }
    if action not in targets:
        return False
    _set(app, pane, targets[action])
    scrolling.cancel(app)
    return True


def cancel(app):
    state = initialize(app)
    if state["capture"]:
        state["discard_release"] = True
    state["capture"] = None


def handle_key(app, key):
    state = initialize(app)
    if key == "esc" and state["capture"]:
        capture = state["capture"]
        pane = next(
            (p for p in _current(app) if p.signature == capture["signature"]), None
        )
        if pane:
            _set(app, pane, capture["start"])
        cancel(app)
        return True
    return False


@capture_handler("scrollbars", cancel=cancel)
def handle_mouse(app, y, x, button="left", shift=False, *, rail_only=False):
    if any(not isinstance(value, int) or isinstance(value, bool) for value in (y, x)):
        return False
    state = initialize(app)
    panes = _current(app)
    capture = state["capture"]
    if button == "right":
        cancel(app)
        return False
    if capture:
        pane = next((p for p in panes if p.signature == capture["signature"]), None)
        if pane is None:
            cancel(app)
        elif button in ("motion", "drag", "release"):
            # Capture owns the release, not every cell under the pointer. A
            # missing release may leave native motion reports arriving over
            # Details; only the visible rail is allowed to change this pane.
            if _rail_hit(state, pane, y, x):
                _set(app, pane, _seek_target(pane, y, capture["grab"]))
            if button == "release":
                state["capture"] = None
            return True
        else:
            cancel(app)
    if button in ("press", "left"):
        state["discard_release"] = False
    if button == "release" and state["discard_release"]:
        state["discard_release"] = False
        return True
    if button not in ("press", "left", "wheel-up", "wheel-down") or shift:
        return False
    for pane in sorted(panes, key=lambda item: item.layer, reverse=True):
        if not pane.limit:
            continue
        if not _visible(state, pane, y, x):
            continue
        if (
            button in ("press", "left")
            and pane.header
            and y == pane.header[0]
            and pane.header[1] <= x < pane.header[1] + 4
        ):
            if button in ("press", "left"):
                _claim(app)
            _set(app, pane, 0 if x < pane.header[1] + 2 else pane.limit)
            return True
        rail = _rail_hit(state, pane, y, x)
        body = not rail_only and pane.rect.contains(y, x)
        if button.startswith("wheel-") and (rail or body):
            # Native row navigation owns semantic table/log-body wheel input.
            if not rail and pane.key in (
                "jobs",
                "recent",
                "history",
                "group",
                "sources",
                "deps",
                "logs:document",
                "inline-log",
            ):
                return False
            target = manual(app, pane.key, context=pane.context)
            _set(
                app,
                pane,
                (pane.target if target is None else target)
                + (3 if button == "wheel-down" else -3),
            )
            return True
        if not rail:
            continue
        _claim(app)
        start, size, _ = _thumb(pane)
        on_thumb = start <= y < start + size
        grab = y - start if on_thumb else size // 2
        if button == "press":
            target = manual(app, pane.key, context=pane.context)
            state["capture"] = {
                "signature": pane.signature,
                "token": state["token"],
                "start": pane.target if target is None else target,
                "grab": grab,
            }
            state["discard_release"] = False
        if not on_thumb:
            _set(app, pane, _seek_target(pane, y, grab))
        return True
    return False


def descriptors(app):
    state = initialize(app)
    output = []
    for pane in _current(app):
        if not pane.header or not pane.limit:
            continue
        y, left, _ = pane.header
        for offset, direction, label in (
            (0, "top", "Scroll to top"),
            (2, "bottom", "Scroll to bottom"),
        ):
            if _visible(state, pane, y, left + offset):
                output.append(
                    {
                        "id": "scroll:" + pane.key + ":" + direction,
                        "label": label,
                        "rect": Rect(y, left + offset, y + 1, left + offset + 2),
                        "action": ("scrollbar", pane.key, direction),
                        "group": "scroll:" + pane.key,
                        "layer": pane.layer,
                        "button": True,
                    }
                )
    return tuple(output)


def feedback(app, *, ascii_=False):
    state = initialize(app)
    output = []
    pointer = getattr(app, "interaction_state", {}).get("pointer")
    for pane in _current(app):
        if not pane.limit:
            continue
        start, size, _ = _thumb(pane)
        x = pane.rect.right - 1
        for y in range(pane.rect.top, pane.rect.bottom):
            if not _visible(state, pane, y, x):
                continue
            thumb = start <= y < start + size
            hover = pointer == (y, x)
            glyph = (
                "|" if ascii_ and thumb else ":" if ascii_ else "┃" if thumb else "│"
            )
            style = "accent+bold" if thumb or hover else "dim"
            output.append((y, x, [(glyph, style)]))
        if pane.header:
            y, left, _ = pane.header
            for offset, glyph in (
                (0, "^" if ascii_ else "↑"),
                (2, "v" if ascii_ else "↓"),
            ):
                if _visible(state, pane, y, left + offset):
                    output.append((y, left + offset, [(glyph + " ", "accent+bold")]))
    return output


def command_names():
    return []


def run_command(app, args):
    return False


def overlay(views, snap, app, width, height):
    return None
