"""Recents-local presentation modes over published observations only."""
from __future__ import annotations

from . import layout as L

MODES = ("grouped", "sequential")


def mode(app):
    value = getattr(app, "table_state", {}).get("recents_mode", "grouped")
    return value if value in MODES else "grouped"


def order(records, index):
    """Pack each batch at its first sorted member, retaining member order.

    The source is the already admitted and sorted Recents prefix. This linear
    projection never fetches siblings outside that prefix or queries Slurm.
    """
    if index is None:
        return list(records)
    blocks = {}
    for record in records:
        key = ("group", index.by_job[record.id]) if record.id in index.by_job else ("job", record.id)
        blocks.setdefault(key, []).append(record)
    return [record for block in blocks.values() for record in block]


def divider(app, g, row, width):
    """Fit the three list menus on the divider, leaving a drag handle."""
    from .job_list_controls import controls
    from .pane_drag import _replace
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
    from . import grouping_toolbar, recent_history, table_sort
    selected = getattr(app, "selected_id", None)
    state = recent_history.initialize(app)
    if selected and any(record.id == selected for record in state.loaded):
        state.transition_anchor = selected
    app.table_state["recents_mode"] = args[1]
    table_sort.reset_sort(app, "recent")
    grouping_toolbar._invalidate(app)
    # The normal sorting callback preserves exact identity. A folded Recents
    # batch retains its selected member as representative, without unfolding
    # the same batch in Jobs, History, or any other shared view.
    callback = getattr(app, "table_sort_changed", None)
    if callable(callback):
        callback("recent", persist=False)
    app.say(("Grouped Recents: complete batches, newest completion first" if args[1] == "grouped" else
             "Sequential Recents: individual jobs, newest completion first") +
            "; column headings can change the sort")
    app.save()
    return True
