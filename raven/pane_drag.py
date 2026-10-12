"""Bounded, shared splitter capture for terminal workspaces.

Renderers register current screen geometry; dragging changes in-memory values
only. The existing session writer is called once when a changed drag finishes.
No pointer path reads job data, files, or the network.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from itertools import islice
from typing import Callable

from . import layout as L

from .pointer_routing import capture_handler

MAX_DIVIDERS = 24
MAX_BUTTON_HITS = 4096
BUFFER = 1


@dataclass(frozen=True)
class Divider:
    key: str
    axis: str
    x: int
    y: int
    width: int
    height: int
    origin: int
    extent: int
    value: int
    setter: Callable[[int], None]
    minimum: int = 0
    maximum: int = 100
    full_vertical: bool = False
    label: str = "Resize panes"

    def contains(self, y, x):
        if self.axis == "vertical":
            return self.y <= y < self.y + self.height and self.x - BUFFER <= x < self.x + self.width + BUFFER
        return self.x <= x < self.x + self.width and self.y - BUFFER <= y < self.y + self.height + BUFFER

    @property
    def position(self):
        return (self.x if self.axis == "vertical" else self.y) - self.origin


def initialize(app):
    state = getattr(app, "pane_drag_state", None)
    if not isinstance(state, dict):
        state = {"dividers": {}, "capture": None, "focus": None, "focus_context": None, "size": None,
                 "discard_release": False}
        app.pane_drag_state = state
    state.setdefault("discard_release", False)
    return state


def _context(app):
    return (getattr(app, "tab", ""), getattr(app, "mode", "main"),
            getattr(app, "width", None), getattr(app, "height", None))


def _blocked(app):
    toolbar = getattr(app, "toolbar_state", {})
    return (getattr(app, "mode", "main") != "main" or
            (isinstance(toolbar, dict) and (toolbar.get("menu") is not None or toolbar.get("panel"))))


def _restore_capture(state):
    capture = state["capture"]
    if capture:
        capture["setter"](capture["original"])
        state["capture"] = None
        state["discard_release"] = True


def begin_frame(app, width, height):
    """Clear hidden geometry once, before the outermost page renderer runs."""
    state = initialize(app)
    context = _context(app)
    if state["capture"] and state["capture"]["context"] != context:
        _restore_capture(state)
    if state["focus"] and state.get("focus_context", state.get("context")) != context:
        state["focus"] = None
    state["context"] = context
    state["size"] = (max(0, int(width)), None if height is None else max(0, int(height)))
    state["body_origin"] = max(0, int(getattr(app, "body_origin", 0)))
    state["body_height"] = (None if height is None else
                            max(0, int(height) - state["body_origin"] - 1))
    state["dividers"] = {}


def register(app, key, axis, x, y, width, height, origin, extent, value, setter,
             *, minimum=0, maximum=100, full_vertical=False, label="Resize panes"):
    """Register one painted divider in absolute screen-cell coordinates.

    ``origin`` and ``extent`` describe the entire movable axis excluding the
    divider itself. ``setter`` accepts the first pane's integer percentage.
    Registration is bounded and deliberately does not save session state.
    """
    state = initialize(app)
    if (not isinstance(key, str) or not key or len(key) > 128 or
            axis not in ("vertical", "horizontal") or not callable(setter) or
            any(not isinstance(item, int) or isinstance(item, bool)
                for item in (x, y, width, height, origin, extent, value, minimum, maximum)) or
            x < 0 or y < 0 or width <= 0 or height <= 0 or extent <= 0 or
            not 0 <= minimum <= maximum <= 100 or
            (key not in state["dividers"] and len(state["dividers"]) >= MAX_DIVIDERS)):
        return None
    size = state.get("size")
    if size:
        frame_width, frame_height = size
        if x >= frame_width or (frame_height is not None and y >= frame_height):
            return None
        width = min(width, frame_width - x)
        if frame_height is not None:
            height = min(height, frame_height - y)
    full_vertical = bool(full_vertical and axis == "vertical")
    if full_vertical and state.get("body_height") is not None:
        full_vertical = y == state["body_origin"] and height == state["body_height"]
    divider = Divider(key, axis, x, y, width, height, origin, extent,
                      value, setter, minimum, maximum, full_vertical, str(label)[:128])
    state["dividers"][key] = divider
    return divider


def _valid(app, capture):
    divider = initialize(app)["dividers"].get(capture["key"])
    return (not _blocked(app) and _context(app) == capture["context"] and
            divider is not None and divider.axis == capture["axis"] and
            divider.origin == capture["origin"] and divider.extent == capture["extent"])


def tick(app):
    state = initialize(app)
    capture = state["capture"]
    if capture and not _valid(app, capture):
        _restore_capture(state)
    if state["focus"] and (state["focus"] not in state["dividers"] or _blocked(app) or
                           state.get("focus_context", state.get("context", _context(app))) != _context(app)):
        state["focus"] = None


def active(app):
    return initialize(app)["capture"] is not None


def blur(app):
    """A content gesture leaves keyboard resizing; an active drag keeps capture."""
    state = initialize(app)
    if state["capture"] is None:
        state["focus"], state["focus_context"] = None, None


def cancel(app):
    """Restore any unfinished resize before shutdown or a global reset."""
    state = initialize(app)
    _restore_capture(state)
    blur(app)


@contextmanager
def committed(app):
    """Expose committed sizes to an unrelated session save during a drag.

    The screen retains its live preview. Copying persisted feature preferences
    inside this scope records the original size until the release commits it.
    """
    state = initialize(app)
    capture = state["capture"]
    if capture:
        capture["setter"](capture["original"])
    try:
        yield
    finally:
        if capture and state["capture"] is capture:
            capture["setter"](capture["current"])


def _save(app):
    callback = getattr(app, "save", None)
    if callable(callback):
        callback()


def _say(app, message):
    callback = getattr(app, "say", None)
    if callable(callback):
        callback(message)


def _bounded_button(app, y, x):
    """Nearby painted controls keep their area outside the separator line."""
    interaction = getattr(app, "interaction_state", None)
    if isinstance(interaction, dict) and interaction.get("graph") is not None:
        from .interaction import _current
        graph = _current(app)
        control = graph.at(y, x) if graph is not None else None
        if control is not None and control.group != "pane-dividers":
            return True
    for hit in islice(getattr(app, "last_hits", ()), MAX_BUTTON_HITS):
        if not isinstance(hit, (tuple, list)) or len(hit) != 3:
            continue
        row, kind, value = hit
        if row != y:
            continue
        if kind == "control" and isinstance(value, dict):
            left, right = value.get("left"), value.get("right")
        elif kind == "sort_header" and isinstance(value, (tuple, list)) and len(value) == 4:
            left, right = value[2:]
        elif kind in ("node_cell", "job_panel_tab", "job_panel_view", "job_panel_file", "job_panel_action") and isinstance(value, (tuple, list)) and len(value) == 3:
            left, right = value[1:]
        else:
            continue
        if isinstance(left, int) and isinstance(right, int) and left <= x < right:
            return True
    return False


@capture_handler("pane_drag")
def handle_mouse(app, y, x, button="left", shift=False):
    if any(type(value) is not int for value in (y, x)):
        return False
    state = initialize(app)
    capture = state["capture"]
    if button in ("press", "left"):
        state["discard_release"] = False
    if button == "right":
        # A context/deselect gesture ends an unfinished resize. Later hover
        # must not resume resizing after the user has cancelled the gesture.
        _restore_capture(state)
        blur(app)
        return False
    if button == "release" and not capture and state["discard_release"]:
        state["discard_release"] = False
        return True
    if button in ("wheel-up", "wheel-down") and not capture:
        blur(app)
        return False
    if capture:
        if not _valid(app, capture):
            _restore_capture(state)
            if button in ("release", "press", "left"):
                state["discard_release"] = False
            return button in ("motion", "drag", "release")
        if button in ("motion", "drag", "release"):
            coordinate = x if capture["axis"] == "vertical" else y
            delta = coordinate - capture["pointer"]
            target = round((capture["position"] + delta) * 100 / capture["extent"])
            divider = state["dividers"][capture["key"]]
            value = (capture["original"] if not delta else
                     max(divider.minimum, min(divider.maximum, target)))
            if value != capture["current"]:
                capture["setter"](value)
                capture["current"] = value
            if button == "release":
                state["capture"] = None
                state["discard_release"] = False
                if capture["current"] != capture["original"]:
                    _save(app)
            return True
        if button in ("press", "left"):
            _restore_capture(state)
            state["discard_release"] = False
        else:
            return False
    if (button not in ("press", "left") or _blocked(app) or
            state.get("context", _context(app)) != _context(app)):
        return False
    # Last-registered inner dividers win only where their actual line is nearer.
    candidates = [item for item in state["dividers"].values() if item.contains(y, x)]
    if not candidates:
        state["focus"] = None
        return False
    exact = [item for item in candidates if item.x <= x < item.x + item.width and
             item.y <= y < item.y + item.height]
    if not exact and _bounded_button(app, y, x):
        state["focus"] = None
        return False
    candidates = exact or candidates
    divider = min(reversed(candidates), key=lambda item: abs((x if item.axis == "vertical" else y) -
                                                           (item.x if item.axis == "vertical" else item.y)))
    state["focus"] = divider.key
    state["focus_context"] = _context(app)
    if button == "left":
        _say(app, divider.label + "; arrows resize, Esc returns to the page")
        return True
    state["capture"] = {"key": divider.key, "axis": divider.axis,
                        "context": _context(app), "origin": divider.origin,
                        "extent": divider.extent, "position": divider.position,
                        "pointer": x if divider.axis == "vertical" else y,
                        "original": divider.value, "current": divider.value,
                        "setter": divider.setter}
    state["discard_release"] = False
    return True


def _step(app, divider, delta):
    value = max(divider.minimum, min(divider.maximum, divider.value + delta))
    if (delta > 0 and value < divider.value) or (delta < 0 and value > divider.value):
        return True
    if value != divider.value:
        divider.setter(value)
        _save(app)
    return True


def handle_key(app, key):
    state = initialize(app)
    if state["capture"]:
        _restore_capture(state)
        state["focus"] = None
        if key == "esc":
            _say(app, "Pane resize cancelled")
            return True
        return False
    if (not state["focus"] or _blocked(app) or
            state.get("focus_context", state.get("context", _context(app))) != _context(app)):
        return False
    divider = state["dividers"].get(state["focus"])
    if divider is None:
        state["focus"] = None
        return False
    if key in ("esc", "enter", "tab", "f6", "ctrl-w", "ctrl_w", "f8"):
        state["focus"] = None
        if key in ("esc", "enter") and isinstance(getattr(app, "interaction_state", None), dict):
            app.interaction_state["active"] = False
        return key in ("esc", "enter")
    actions = ({"left": -2, "right": 2} if divider.axis == "vertical" else
               {"up": -2, "down": 2})
    actions.update({"pgup": -10, "pgdn": 10})
    return _step(app, divider, actions[key]) if key in actions else False


def command_names():
    return ["pane-focus", "pane-resize"]


def run_command(app, args):
    if not args or args[0] not in command_names():
        return False
    state = initialize(app)
    if len(args) < 2:
        _say(app, "pane-focus <key> | pane-resize <key> <smaller|larger>")
        return True
    divider = state["dividers"].get(args[1])
    if not divider or _blocked(app):
        _say(app, "That pane divider is not visible")
        return True
    if args[0] == "pane-focus" and len(args) == 2:
        state["focus"] = divider.key
        state["focus_context"] = _context(app)
        _say(app, divider.label + "; arrows resize, Esc returns to the page")
    elif args[0] == "pane-resize" and len(args) == 3 and args[2] in ("smaller", "larger"):
        _step(app, divider, -2 if args[2] == "smaller" else 2)
    else:
        _say(app, "pane-focus <key> | pane-resize <key> <smaller|larger>")
    return True


def overlay(views, snap, app, width, height):
    return None


def control_hit(app, key, *, origin_y=0):
    """Return one semantic F8 control at the divider's visual center."""
    divider = initialize(app)["dividers"].get(key)
    if divider is None:
        return None
    y = divider.y + (divider.height - 1) // 2
    return (y - origin_y, "control", {"id": "pane:" + key, "label": divider.label,
            "left": divider.x, "right": divider.x + divider.width,
            "action": ("command", "pane-focus " + key), "group": "pane-dividers"})


def _replace(row, start, text, style, width, *, fitted=False):
    """Replace display cells without splitting a wide glyph or copying styles."""
    before = L.clip_row(row, start)
    used = L.vlen(L.row_text(before))
    if used < start:
        before.append((" " * (start - used), "bg:canvas"))
    end = start + L.vlen(text)
    after, position = [], 0
    for offset, (segment, segment_style) in enumerate(row):
        if position >= end:
            if position == end and not L.vlen(segment):
                continue
            # The suffix is already fitted to the canvas. Its graph cells do
            # not need another width scan merely to paint a divider before it.
            after.extend(row[offset:])
            break
        length = L.vlen(segment)
        if position + length <= end:
            position += length
            continue
        skipped, index = 0, 0
        for index, character in enumerate(segment):
            cells = L._character_width(character)
            if position + skipped >= end:
                break
            skipped += cells
        else:
            index = len(segment)
        if position + skipped > end:
            after.append((" " * (position + skipped - end), segment_style))
        if index < len(segment):
            after.append((segment[index:], segment_style))
        position += length
    result = before + [(text, style)] + after
    if fitted:
        # clip_row stops at the exact edge before trailing empty/combining
        # segments; preserve that rule without rescanning the full suffix.
        while result and not L.vlen(result[-1][0]):
            result.pop()
        return result
    return L.clip_row(result, width)


def paint(canvas, app, key, *, origin_x=0, origin_y=0, ascii_=False):
    """Paint an already-reserved divider gap; all canvas dimensions stay fixed."""
    state = initialize(app)
    divider = state["dividers"].get(key)
    if not divider or not canvas:
        return canvas
    focused = state["focus"] == key or (state["capture"] and state["capture"]["key"] == key)
    line_style = "border+bg:canvas" + ("+bold" if focused else "")
    center = divider.y + (divider.height - 1) // 2
    for absolute_y in range(divider.y, divider.y + divider.height):
        y = absolute_y - origin_y
        if not 0 <= y < len(canvas):
            continue
        x = divider.x - origin_x
        if x < 0:
            continue
        width = L.vlen(L.row_text(canvas[y]))
        if x >= width:
            continue
        span = min(divider.width, width - x)
        diamond = divider.full_vertical and absolute_y == center
        if divider.axis == "vertical":
            text = ("*" if ascii_ else "◆") if diamond else ("|" if ascii_ else "│")
            style = "accent+bold+bg:canvas" if diamond else line_style
        else:
            text, style = ("-" if ascii_ else "─") * span, line_style
        canvas[y] = _replace(canvas[y], x, text, style, width, fitted=True)
    return canvas
