"""Render cached control feedback without handler or source dependencies."""

from __future__ import annotations
from .interaction_state import initialize, _current
from .control_graph import POINTER_STYLE, FOCUS_STYLE, _integer, _decorate_row


def _targets(app):
    state = initialize(app)
    graph = _current(app)
    if graph is None:
        return ()
    targets = []
    hovered = graph.get(state["hovered"])
    focused = graph.get(state["focused"]) if state["active"] else None
    if hovered:
        targets.append((hovered, POINTER_STYLE))
    if focused:
        targets.append((focused, FOCUS_STYLE))
    return targets


def feedback_rows(app):
    """Visible physical rows affected by the current pointer or button focus.

    A painter can repaint the union of the previous and new sets to erase old
    feedback and draw its replacement without rebuilding the document.
    """
    return frozenset(
        y
        for control, _ in _targets(app)
        for y in range(control.rect.top, control.rect.bottom)
    )


def decorate(app, rows, origin=(0, 0)):
    """Style controls while preserving the explicit screen-line yank range."""
    targets = _targets(app)
    if not targets:
        return rows
    y0, x0 = origin
    output = list(rows)
    indices = {
        y - y0
        for control, _ in targets
        for y in range(
            max(y0, control.rect.top), min(y0 + len(rows), control.rect.bottom)
        )
    }
    for index in sorted(indices):
        y = index + y0
        anchor, end = getattr(app, "sel_anchor", None), getattr(app, "sel_end", None)
        if (
            _integer(anchor)
            and _integer(end)
            and min(anchor, end) <= y <= max(anchor, end)
        ):
            # Explicit yank selection has stronger visual precedence than a
            # passive pointer or arrow-focus background. Native table cursors
            # still receive normal feedback outside this selected line range.
            # Menus retain their own feedback in decorate_overlays below.
            continue
        ranges = [
            (control.rect.left - x0, control.rect.right - x0, style)
            for control, style in targets
            if control.rect.top <= y < control.rect.bottom
        ]
        if ranges:
            output[index] = _decorate_row(rows[index], ranges)
    return output


def decorate_overlays(app, overlays):
    """Apply identical feedback to absolute overlay triples."""
    if overlays is None:
        return None
    targets = _targets(app)
    if not targets:
        return overlays
    output = []
    for y, x, row in overlays:
        ranges = [
            (control.rect.left - x, control.rect.right - x, style)
            for control, style in targets
            if control.rect.top <= y < control.rect.bottom
        ]
        output.append((y, x, _decorate_row(row, ranges) if ranges else row))
    return output
