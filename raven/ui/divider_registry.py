"""Bounded divider geometry registration and presentation, without gestures."""

from dataclasses import dataclass
from typing import Callable
from .. import layout as L
from .divider_state import initialize
from .text_rows import _replace

MAX_DIVIDERS = 24
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
            return (
                self.y <= y < self.y + self.height
                and self.x - BUFFER <= x < self.x + self.width + BUFFER
            )
        return (
            self.x <= x < self.x + self.width
            and self.y - BUFFER <= y < self.y + self.height + BUFFER
        )

    @property
    def position(self):
        return (self.x if self.axis == "vertical" else self.y) - self.origin


def register(
    app,
    key,
    axis,
    x,
    y,
    width,
    height,
    origin,
    extent,
    value,
    setter,
    *,
    minimum=0,
    maximum=100,
    full_vertical=False,
    label="Resize panes",
):
    """Register one painted divider in absolute screen-cell coordinates.

    ``origin`` and ``extent`` describe the entire movable axis excluding the
    divider itself. ``setter`` accepts the first pane's integer percentage.
    Registration is bounded and deliberately does not save session state.
    """
    state = initialize(app)
    if (
        not isinstance(key, str)
        or not key
        or len(key) > 128
        or axis not in ("vertical", "horizontal")
        or not callable(setter)
        or any(
            not isinstance(item, int) or isinstance(item, bool)
            for item in (x, y, width, height, origin, extent, value, minimum, maximum)
        )
        or x < 0
        or y < 0
        or width <= 0
        or height <= 0
        or extent <= 0
        or not 0 <= minimum <= maximum <= 100
        or (key not in state["dividers"] and len(state["dividers"]) >= MAX_DIVIDERS)
    ):
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
    divider = Divider(
        key,
        axis,
        x,
        y,
        width,
        height,
        origin,
        extent,
        value,
        setter,
        minimum,
        maximum,
        full_vertical,
        str(label)[:128],
    )
    state["dividers"][key] = divider
    return divider


def control_hit(app, key, *, origin_y=0):
    """Return one semantic F8 control at the divider's visual center."""
    divider = initialize(app)["dividers"].get(key)
    if divider is None:
        return None
    y = divider.y + (divider.height - 1) // 2
    return (
        y - origin_y,
        "control",
        {
            "id": "pane:" + key,
            "label": divider.label,
            "left": divider.x,
            "right": divider.x + divider.width,
            "action": ("command", "pane-focus " + key),
            "group": "pane-dividers",
        },
    )


def paint(canvas, app, key, *, origin_x=0, origin_y=0, ascii_=False):
    """Paint an already-reserved divider gap; all canvas dimensions stay fixed."""
    state = initialize(app)
    divider = state["dividers"].get(key)
    if not divider or not canvas:
        return canvas
    focused = state["focus"] == key or (
        state["capture"] and state["capture"]["key"] == key
    )
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
