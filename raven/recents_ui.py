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
    """Put both view choices on the existing divider, leaving a drag handle."""
    from .control_rows import buttons
    from .pane_drag import _replace
    labels = (("grouped", "Grouped"), ("sequential", "Sequential"))
    if width < 38:
        labels = (("grouped", "Group"), ("sequential", "Seq"))
    required = sum(L.vlen(label) + 2 for _, label in labels) + 1
    # The rightmost six cells belong to the existing header scroll controls.
    available = max(0, width - 6)
    left = max(0, available - required)
    rows, hits = buttons(g, available - left,
        [(key, label, ("command", "recents " + key)) for key, label in labels],
        selected=mode(app), group="recents-modes", prefix="recents-mode:")
    if len(rows) != 1 or len(hits) != 2 or left < 2:
        return row, [], width
    for text, style in rows[0]:
        row = _replace(row, left, text, style, width)
        left += L.vlen(text)
    offset = available - required
    hits = [(y, kind, dict(value, left=value["left"] + offset, right=value["right"] + offset,
                          label=("Grouped Recents" if value["id"].endswith("grouped") else "Sequential Recents")))
            for y, kind, value in hits]
    return row, hits, max(1, offset - 1)


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
