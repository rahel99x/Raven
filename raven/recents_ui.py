"""Recents-local presentation modes over published observations only."""

from __future__ import annotations

from . import layout as L

from .ui.list_preferences import MODES as MODES, recent_mode as mode, invalidate
from raven.jobs.group_records import contiguous_order as order

__all__ = ["MODES", "mode", "order", "divider", "run_command"]


def divider(app, g, row, width):
    """Fit the three list menus on the divider, leaving a drag handle."""
    from .ui.list_controls import controls
    from .ui.text_rows import _replace

    # The rightmost six cells belong to the existing header scroll controls.
    available = max(0, width - 6)
    # Keep the section title intact: workspace partitioning and the resize
    # handle both rely on this being the Recents divider, even in split panes.
    rows, hits = controls(app, g, max(0, available - 16), "recent")
    if len(rows) != 1 or len(hits) != 3:
        return row, [], width
    required = L.vlen(L.row_text(rows[0]))
    left = max(0, available - required)
    if left < 2:
        return row, [], width
    for text, style in rows[0]:
        row = _replace(row, left, text, style, width)
        left += L.vlen(text)
    offset = available - required
    result = []
    for y, kind, value in hits:
        value = dict(value, left=value["left"] + offset, right=value["right"] + offset)
        result.append((y, kind, value))
    return row, result, max(1, offset - 1)


def run_command(app, args):
    if not args or args[0] != "recents":
        return False
    if len(args) != 2 or args[1] not in MODES:
        app.say("Use recents grouped or recents sequential")
        return True
    from . import table_sort
    from .ui.recent_state import initialize as recent_initialize

    selected = getattr(app, "selected_id", None)
    state = recent_initialize(app)
    if selected and any(record.id == selected for record in state.loaded):
        state.transition_anchor = selected
    app.table_state["recents_mode"] = args[1]
    table_sort.reset_sort(app, "recent")
    invalidate(app)
    # The normal sorting callback preserves exact identity. A folded Recents
    # batch retains its selected member as representative, without unfolding
    # the same batch in Jobs, History, or any other shared view.
    callback = getattr(app, "table_sort_changed", None)
    if callable(callback):
        callback("recent", persist=False)
    app.say(
        (
            "Grouped Recents: complete batches, newest completion first"
            if args[1] == "grouped"
            else "Sequential Recents: individual jobs, newest completion first"
        )
        + "; column headings can change the sort"
    )
    app.save()
    return True
