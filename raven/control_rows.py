"""Cell-accurate semantic buttons shared by page and modal renderers."""
from __future__ import annotations

from . import layout as L
from .log_text import display_text


def buttons(g, width, choices, *, selected=None, group="buttons", prefix="", gap=1):
    """Pack (id, label, action) controls without hiding later choices.

    Geometry is relative to the returned rows; compose/overlay translates it
    once. Actions are data understood by the interaction graph, never invoked
    here. A label is shortened only when one button exceeds the entire width.
    """
    width = max(0, width)
    if not width:
        return [], []
    separator = gap if isinstance(gap, str) else " " * max(0, gap)
    gap_width = L.vlen(separator)
    rows, hits, current, x = [], [], [], 0
    for identifier, label, action in choices:
        label = display_text(str(label))
        text = " " + label + " "
        if x and x + gap_width + L.vlen(text) > width:
            rows.append(current)
            current, x = [], 0
        if x and separator:
            current.append((separator, ""))
            x += gap_width
        text = L.cut(text, max(0, width - x), g.ascii)
        length = L.vlen(text)
        if not length:
            continue
        current.append((text, "sel+bold" if identifier == selected else "cyan+bg:surface"))
        hits.append((len(rows), "control", {"id": prefix + str(identifier), "label": str(label),
                     "left": x, "right": x + length, "action": action, "group": group}))
        x += length
    if current:
        rows.append(current)
    return rows, hits


def place_hits(hits, placements, *, offset=0, prefix=""):
    """Map document controls onto the exact visible box's painted cells."""
    result = []
    for row, kind, value in hits:
        index = row - offset
        if index < 0 or index >= len(placements):
            continue
        y, x, segments = placements[index]
        end = x + L.vlen(L.row_text(segments)) - 1
        payload = dict(value)
        payload.update(id=prefix + payload["id"], left=x + 1 + value["left"],
                       right=min(end, x + 1 + value["right"]))
        if payload["left"] < payload["right"]:
            result.append((y, kind, payload))
    return result
