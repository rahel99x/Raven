"""Persistent destination rail with bounded, paint-bound mouse interactions.

Favorites retain their custom sequence when displayed alphabetically. Only
explicit add/remove, sort, disclosure and completed drag actions write state;
rendering, hover and selection never request scheduler data or read files.
"""

from __future__ import annotations

import time

from . import layout as L
from .pointer_routing import capture_handler
from .ui.catalog import TABS
from .ui.control_graph import Control, Rect
from .ui.destinations import (
    MAX_FAVORITES,
    children,
    current,
    destinations,
    valid_favorites,
)

MODES = frozenset(("main", "sidebar", "sidebar_menu"))
STYLE = "text+bg:surface-sunken"


def initialize(app):
    state = getattr(app, "sidebar_state", None)
    if not isinstance(state, dict):
        state = app.sidebar_state = {
            "order": "custom",
            "expanded": [],
            "top": 0,
            "cursor": 0,
            "page": 1,
            "hits": (),
            "rows": (),
            "token": None,
            "revision": 0,
            "menu": None,
            "menu_hits": (),
            "menu_token": None,
            "capture": None,
            "hover": None,
            "discard_release": False,
            "aggregate": None,
            "geometry": None,
            "menu_cursor": 0,
            "last_destination": None,
        }
    return state


def restore(app, data):
    data = data if isinstance(data, dict) else {}
    expanded = data.get("expanded", [])
    allowed = {key for key, _ in TABS if children(key)}
    initialize(app).update(
        order="alpha" if data.get("favorite_order") == "alpha" else "custom",
        expanded=list(
            dict.fromkeys(
                key for key in expanded[:32] if isinstance(key, str) and key in allowed
            )
        )
        if isinstance(expanded, list)
        else [],
    )


def save(app):
    state = initialize(app)
    return {"favorite_order": state["order"], "expanded": list(state["expanded"])}


def width(app, total_width):
    """Keep a visible rail while reserving useful content in narrow terminals."""
    total_width = max(0, int(total_width))
    desired = 17 if total_width >= 80 else 14 if total_width >= 60 else 7
    return min(desired, max(0, total_width - 1))


def _pins(app):
    return valid_favorites(getattr(app, "toolbar_state", {}).get("favorites", []))


def _display_pins(app):
    pins = _pins(app)
    if initialize(app)["order"] == "alpha":
        known = destinations()
        pins.sort(key=lambda key: (known[key][0].casefold(), key))
    return pins


def _changed(app, *, persist=False):
    state = initialize(app)
    state.update(
        revision=state["revision"] + 1,
        token=None,
        hits=(),
        menu_hits=(),
        menu_token=None,
        capture=None,
        hover=None,
    )
    getattr(app, "interaction_state", {}).update(frame_required=True, pressed=None)
    if persist and callable(getattr(app, "save", None)):
        app.save()


def _context(app):
    state = initialize(app)
    toolbar = getattr(app, "toolbar_state", {})
    return (
        getattr(app, "mode", "main"),
        getattr(app, "tab", "jobs"),
        getattr(app, "width", None),
        getattr(app, "height", None),
        current(app),
        tuple(_pins(app)),
        state["order"],
        tuple(state["expanded"]),
        state["top"],
        state["revision"],
        toolbar.get("menu"),
        toolbar.get("panel"),
        state["geometry"],
    )


def _gesture_context(app):
    state = initialize(app)
    toolbar = getattr(app, "toolbar_state", {})
    return (
        getattr(app, "mode", "main"),
        getattr(app, "tab", "jobs"),
        getattr(app, "width", None),
        getattr(app, "height", None),
        current(app),
        tuple(_pins(app)),
        state["order"],
        tuple(state["expanded"]),
        toolbar.get("menu"),
        toolbar.get("panel"),
    )


def _toolbar_open(app):
    toolbar = getattr(app, "toolbar_state", {})
    return toolbar.get("menu") is not None or bool(toolbar.get("panel"))


def token(app):
    """Small invalidation key for frame caches; never copies scheduler state."""
    state = initialize(app)
    return (
        _context(app),
        state["cursor"],
        state["menu"],
        state["menu_cursor"],
        (state["capture"].get("drop"), state["capture"].get("dragged"))
        if isinstance(state["capture"], dict)
        else None,
    )


def _rows(app):
    """Small scalar rows; favorites never remove entries from the full list."""
    state, known = initialize(app), destinations()
    rows = [("favorite", key, known[key][0], False) for key in _display_pins(app)]
    if not rows:
        rows.append(("hint", "", "Right-click to pin", False))
    rows.append(("section", "", "Workspace", False))
    for key, title in TABS:
        child_rows = children(key)
        rows.append(("destination", "tab:" + key, title, bool(child_rows)))
        if key in state["expanded"]:
            rows.extend(
                ("child", identity, label, False) for identity, label in child_rows
            )
    return tuple(rows)


def _pad(text, columns, ascii_=False):
    return L.pad(L.cut(text, columns, ascii_), columns)


def render(views, app, total_width, height):
    """Absolute triples below the global toolbar, above the final status row."""
    state = initialize(app)
    columns, capacity = width(app, total_width), max(0, int(height) - 3)
    geometry = (total_width, height, columns)
    if state["geometry"] != geometry:
        state.update(
            capture=None,
            discard_release=bool(state["capture"]),
            menu=None,
            menu_hits=(),
            geometry=geometry,
        )
        if getattr(app, "mode", "main") == "sidebar_menu":
            app.mode = "main"
    rows = _rows(app)
    active = current(app)
    if state["last_destination"] != active and not state["capture"]:
        # External navigation reveals the active leaf or its collapsed parent.
        # Wheel scrolling remains independent until the destination changes.
        preferred = next(
            (
                index
                for index, row in enumerate(rows)
                if row[0] == "child" and row[1] == active
            ),
            None,
        )
        if preferred is None:
            preferred = next(
                (
                    index
                    for index, row in enumerate(rows)
                    if row[0] == "favorite" and row[1] == active
                ),
                None,
            )
        if preferred is None:
            preferred = next(
                (
                    index
                    for index, row in enumerate(rows)
                    if row[0] == "destination"
                    and row[1] == "tab:" + getattr(app, "tab", "jobs")
                ),
                None,
            )
        if preferred is not None:
            if preferred < state["top"]:
                state["top"] = preferred
            elif preferred >= state["top"] + capacity:
                state["top"] = preferred - capacity + 1
        state["last_destination"] = active
    state["cursor"] = max(0, min(state["cursor"], len(rows) - 1))
    if getattr(app, "mode", "main") == "sidebar":
        if state["cursor"] < state["top"]:
            state["top"] = state["cursor"]
        elif state["cursor"] >= state["top"] + capacity:
            state["top"] = state["cursor"] - capacity + 1
    state["top"] = max(0, min(state["top"], max(0, len(rows) - capacity)))
    state.update(rows=rows, page=max(1, capacity), hits=())
    if state["token"] is not None and state["token"] != _context(app):
        state["revision"] += 1
    state["token"] = _context(app)
    if not columns or height < 3:
        return []
    ascii_ = bool(views.g.ascii)
    divider = "|" if ascii_ else "│"
    inner = max(0, columns - 1)
    order_label = "A-Z" if state["order"] == "alpha" else "Own"
    header = _pad("Favorites", max(0, inner - 5), ascii_) + _pad(
        "[" + order_label + "]", min(5, inner), ascii_
    )
    header = _pad(header, inner, ascii_) + divider
    result = [(1, 0, [(header, STYLE + "+bold")])]
    hits = [(1, max(0, inner - 5), inner, "order", "")]
    for offset in range(capacity):
        index, y = state["top"] + offset, 2 + offset
        if index >= len(rows):
            result.append((y, 0, [(" " * inner + divider, STYLE)]))
            continue
        kind, identity, label, expandable = rows[index]
        expanded = identity.split(":", 1)[-1] in state["expanded"]
        arrow = ("v" if ascii_ else "▾") if expanded else (">" if ascii_ else "▸")
        prefix = (
            "* "
            if kind == "favorite"
            else "  "
            if kind == "child"
            else (arrow + " " if expandable else "  ")
        )
        if kind in ("hint", "section"):
            prefix = ""
        style = STYLE
        if kind == "section":
            style += "+accent+bold"
        elif kind == "hint":
            style += "+dim"
        elif identity == active:
            style = "sel+bold"
        elif kind == "destination" and identity == "tab:" + getattr(app, "tab", "jobs"):
            style += "+accent+bold"
        elif getattr(app, "mode", "main") == "sidebar" and index == state["cursor"]:
            style += "+under+bold"
        capture = state["capture"]
        if isinstance(capture, dict) and capture.get("dragged") and kind == "favorite":
            if capture.get("drop") == identity:
                style = "sel+bold+under"
                prefix = "> "
            elif capture["hit"][4] == identity:
                style += "+dim"
        visible_label = label.split(": ", 1)[-1] if kind == "favorite" else label
        text = _pad(prefix + visible_label, inner, ascii_)
        edge, scroll = divider, None
        if offset == 0 and state["top"] > 0:
            edge, scroll = ("^" if ascii_ else "▴"), "scroll-up"
        elif offset == capacity - 1 and state["top"] + capacity < len(rows):
            edge, scroll = ("v" if ascii_ else "▾"), "scroll-down"
        result.append((y, 0, [(text, style), (edge, STYLE + "+border")]))
        if scroll:
            hits.append((y, inner, columns, scroll, ""))
        if kind in ("favorite", "destination", "child") and inner:
            left = min(2, inner) if expandable else 0
            if expandable:
                hits.append((y, 0, left, "expand", identity))
            if left < inner:
                hits.append((y, left, inner, kind, identity))
    state["hits"] = tuple(hit for hit in hits if hit[1] < hit[2])
    return result


def _fresh(app):
    state = initialize(app)
    return state["token"] is not None and state["token"] == _context(app)


def _hit(app, y, x):
    return next(
        (
            hit
            for hit in initialize(app)["hits"]
            if hit[0] == y and hit[1] <= x < hit[2]
        ),
        None,
    )


def _toggle_order(app):
    state = initialize(app)
    state["order"] = "alpha" if state["order"] == "custom" else "custom"
    _changed(app, persist=True)


def _toggle_expanded(app, identity):
    tab = identity.removeprefix("tab:")
    if not children(tab):
        return
    expanded = initialize(app)["expanded"]
    expanded.remove(tab) if tab in expanded else expanded.append(tab)
    _changed(app, persist=True)


def _navigate(app, identity):
    entry = destinations().get(identity)
    if entry is None:
        return
    close(app)
    for command in entry[1]:
        app.run_command(command)


def _toggle_pin(app, identity):
    if identity not in destinations():
        return
    pins = _pins(app)
    if identity in pins:
        pins.remove(identity)
        message = "Removed from Favorites."
    elif len(pins) >= MAX_FAVORITES:
        app.say("Favorites is full; remove an entry first.")
        return
    else:
        pins.append(identity)
        message = "Added to Favorites."
    getattr(app, "toolbar_state", {}).update(favorites=pins)
    _changed(app, persist=True)
    app.say(message)


def close(app):
    state = initialize(app)
    state.update(menu=None, menu_hits=(), menu_token=None, capture=None)
    if getattr(app, "mode", "main") in ("sidebar", "sidebar_menu"):
        app.mode = "main"
    _changed(app)


def cancel(app):
    """Cancel only the gesture; a reorder commits exclusively on release."""
    state = initialize(app)
    state.update(
        discard_release=bool(state["capture"]) or state["discard_release"],
        capture=None,
        hover=None,
    )


def _activate(app, hit):
    if getattr(app, "mode", "main") not in ("main", "sidebar") or _toolbar_open(app):
        return
    state = initialize(app)
    kind, identity = hit[3:]
    if kind == "order":
        _toggle_order(app)
    elif kind in ("scroll-up", "scroll-down"):
        _scroll(app, -state["page"] if kind == "scroll-up" else state["page"])
    elif kind == "expand":
        _toggle_expanded(app, identity)
    else:
        _navigate(app, identity)


def menu_active(app):
    return getattr(app, "mode", "main") == "sidebar_menu"


def _open_menu(app, identity, y):
    state = initialize(app)
    state.update(
        menu=(identity, y), menu_cursor=0, capture=None, menu_hits=(), menu_token=None
    )
    app.mode = "sidebar_menu"
    _changed(app)


def overlay(views, snap, app, total_width, height):
    state = initialize(app)
    state.update(menu_hits=(), menu_token=None)
    if (
        _toolbar_open(app)
        or not menu_active(app)
        or not state["menu"]
        or total_width < 1
        or height < 3
    ):
        return None
    identity, anchor = state["menu"]
    columns = min(total_width, 24)
    y = max(1, min(anchor, height - 3))
    label = "Remove favorite" if identity in _pins(app) else "Add favorite"
    lines = (label, "Close")
    result, hits = [], []
    for index, title in enumerate(lines):
        style = (
            "sel+bold" if index == state["menu_cursor"] else "text+bg:surface-raised"
        )
        result.append(
            (y + index, 0, [(_pad(" " + title, columns, views.g.ascii), style)])
        )
        hits.append((y + index, 0, columns, index))
    state.update(menu_hits=tuple(hits), menu_token=_context(app))
    return result


def _menu_action(app, index):
    state = initialize(app)
    target = state["menu"][0] if state["menu"] else None
    close(app)
    if index == 0 and target:
        _toggle_pin(app, target)


def _guarded(app, y, x):
    guard = initialize(app)["aggregate"]
    return bool(guard and guard[0] >= time.monotonic() and guard[1:] == (y, x))


def _scroll(app, delta, *, capture=False):
    state = initialize(app)
    next_top = max(
        0, min(max(0, len(_rows(app)) - state["page"]), state["top"] + delta)
    )
    if next_top == state["top"]:
        return
    state.update(top=next_top, token=None, hits=(), revision=state["revision"] + 1)
    if not capture:
        cancel(app)
    getattr(app, "interaction_state", {}).update(frame_required=True, pressed=None)


@capture_handler("sidebar", cancel=cancel)
def handle_mouse(app, y, x, button="left", shift=False):
    if type(y) is not int or type(x) is not int:
        return False
    state = initialize(app)
    if state["geometry"] is None or getattr(app, "content_origin_x", 1) == 0:
        # Headless adapters and streamed documents have no painted rail. Their
        # content keeps its original coordinates and pointer ownership.
        return False
    if _toolbar_open(app):
        cancel(app)
        return False
    inside = 1 <= y < getattr(app, "height", 0) - 1 and 0 <= x < width(
        app, getattr(app, "width", 0)
    )
    if getattr(app, "mode", "main") not in MODES:
        state.update(capture=None, menu=None)
        return False
    if button == "left" and _guarded(app, y, x):
        return True
    if menu_active(app):
        if button == "right":
            return True
        if state["menu_token"] != _context(app):
            return True
        hit = next(
            (hit for hit in state["menu_hits"] if hit[0] == y and hit[1] <= x < hit[2]),
            None,
        )
        if button in ("motion", "drag") and hit:
            state["menu_cursor"] = hit[3]
        if button == "press":
            state["capture"] = ("menu", hit, state["menu_token"])
        elif button == "release":
            capture = state.pop("capture", None)
            state["capture"] = None
            if capture and capture == ("menu", hit, state["menu_token"]) and hit:
                _menu_action(app, hit[3])
                state["aggregate"] = (time.monotonic() + 0.5, y, x)
            elif capture and capture == ("menu", None, state["menu_token"]):
                close(app)
                state["aggregate"] = (time.monotonic() + 0.5, y, x)
        elif button == "left":
            _menu_action(app, hit[3]) if hit else close(app)
        return True
    capture = state["capture"]
    if capture is not None and button in ("motion", "drag", "release"):
        if not _fresh(app) or capture["context"] != _gesture_context(app):
            state.update(capture=None, discard_release=button != "release")
            return True
        target = _hit(app, y, x)
        if button in ("motion", "drag"):
            if capture["hit"][3] == "favorite" and (
                y != capture["hit"][0] or button == "drag"
            ):
                capture["dragged"] = True
                capture["drop"] = (
                    target[4] if target and target[3] == "favorite" else None
                )
                if inside and y in (2, getattr(app, "height", 0) - 2):
                    _scroll(app, -1 if y == 2 else 1, capture=True)
            return True
        state["capture"] = None
        if capture["dragged"]:
            # A release can arrive without a final motion report. Its fresh
            # painted identity decides the drop, including after autoscroll.
            capture["drop"] = target[4] if target and target[3] == "favorite" else None
            _finish_drag(app, capture)
        elif target == capture["hit"]:
            _activate(app, target)
        state["aggregate"] = (time.monotonic() + 0.5, y, x)
        return True
    if button == "release":
        consumed = state["discard_release"]
        state["discard_release"] = False
        return bool(consumed or inside)
    if not inside:
        state["hover"] = None
        return False
    if not _fresh(app):
        state.update(capture=None, hover=None)
        return True
    hit = _hit(app, y, x)
    if button in ("wheel-up", "wheel-down"):
        _scroll(app, -3 if button == "wheel-up" else 3)
    elif (
        button == "right"
        and hit
        and hit[3] in ("favorite", "destination", "child", "expand")
    ):
        _open_menu(app, hit[4], y)
    elif button == "press" and hit:
        state["capture"] = {
            "hit": hit,
            "context": _gesture_context(app),
            "dragged": False,
            "drop": None,
        }
    elif button == "left" and hit:
        _activate(app, hit)
    return True


def _finish_drag(app, capture):
    source, target = capture["hit"][4], capture["drop"]
    pins = _display_pins(app)
    if source == target or source not in pins or target not in pins:
        return
    source_index, target_index = pins.index(source), pins.index(target)
    pins.pop(source_index)
    pins.insert(target_index, source)
    getattr(app, "toolbar_state", {}).update(favorites=pins)
    initialize(app)["order"] = "custom"
    _changed(app, persist=True)


def handle_key(app, key):
    state = initialize(app)
    if _toolbar_open(app) or key == "f10":
        return False
    if getattr(app, "mode", "main") in ("sidebar", "sidebar_menu") and (
        key == "f8"
        or getattr(app, "interaction_state", {}).get("active")
        and key not in ("esc", "q", "ctrl-c")
    ):
        from .interaction import handle_key as focus_key

        if focus_key(app, key):
            return True
    if menu_active(app):
        if key in ("esc", "q", "ctrl-c"):
            close(app)
        elif key in ("up", "down", "tab", "btab", "home", "end"):
            state["menu_cursor"] = 0 if key in ("up", "btab", "home") else 1
        elif key in ("enter", "space", "f"):
            _menu_action(app, state["menu_cursor"])
        return True
    if getattr(app, "mode", "main") != "sidebar":
        return False
    rows = _rows(app)
    state["cursor"] = max(0, min(state["cursor"], len(rows) - 1))
    kind, identity, _, expandable = rows[state["cursor"]]
    if key in ("esc", "q", "ctrl-c"):
        close(app)
    elif key in ("up", "down", "tab", "btab", "home", "end", "pgup", "pgdn"):
        delta = {
            "up": -1,
            "down": 1,
            "tab": 1,
            "btab": -1,
            "home": -len(rows),
            "end": len(rows),
            "pgup": -state["page"],
            "pgdn": state["page"],
        }[key]
        state["cursor"] = max(0, min(len(rows) - 1, state["cursor"] + delta))
    elif key == "o":
        _toggle_order(app)
    elif key == "f" and identity:
        _toggle_pin(app, identity)
    elif key in ("left", "right") and expandable:
        tab = identity.removeprefix("tab:")
        if (key == "right") != (tab in state["expanded"]):
            _toggle_expanded(app, identity)
    elif key in ("enter", "space") and kind in ("favorite", "destination", "child"):
        _navigate(app, identity)
    return True


def command_names():
    return ["sidebar"]


def run_command(app, args):
    if not args or args[0] != "sidebar":
        return False
    state = initialize(app)
    if args[1:] in ([], ["focus"]) and getattr(app, "mode", "main") in MODES:
        app.mode = "sidebar"
        state["menu"] = None
        state["cursor"] = next(
            (index for index, row in enumerate(_rows(app)) if row[1] == current(app)), 0
        )
        _changed(app)
    elif args[1:] == ["order"] and getattr(app, "mode", "main") in MODES:
        _toggle_order(app)
    elif args[1:] == ["close"]:
        close(app)
    elif (
        len(args) == 4
        and args[1] == "choose"
        and args[2] == str(state["revision"])
        and args[3].isdigit()
        and _fresh(app)
    ):
        index = int(args[3])
        if 0 <= index < len(state["hits"]):
            _activate(app, state["hits"][index])
    elif (
        len(args) == 4
        and args[1] == "menu"
        and args[2] == str(state["revision"])
        and args[3] in ("0", "1")
        and menu_active(app)
        and state["menu_token"] == _context(app)
    ):
        _menu_action(app, int(args[3]))
    return True


def controls(app):
    state = initialize(app)
    if _toolbar_open(app) or getattr(app, "mode", "main") not in MODES:
        return ()
    if menu_active(app):
        if state["menu_token"] != _context(app):
            return ()
        return tuple(
            Control(
                f"sidebar:menu:{index}",
                "Toggle favorite" if index == 0 else "Close Favorites menu",
                Rect(y, left, y + 1, right),
                ("command", f"sidebar menu {state['revision']} {index}"),
                "sidebar-menu",
                layer=4,
            )
            for y, left, right, index in state["menu_hits"]
        )
    if not _fresh(app):
        return ()
    known = destinations()
    labels = {
        "order": "Favorites order: " + state["order"],
        "scroll-up": "Scroll navigation up",
        "scroll-down": "Scroll navigation down",
    }
    return tuple(
        Control(
            f"sidebar:{kind}:{identity}",
            labels[kind]
            if kind in labels
            else ("Expand or collapse " if kind == "expand" else "Open ")
            + known[identity][0],
            Rect(y, left, y + 1, right),
            ("command", f"sidebar choose {state['revision']} {index}"),
            "sidebar",
            layer=3,
        )
        for index, (y, left, right, kind, identity) in enumerate(state["hits"])
    )
