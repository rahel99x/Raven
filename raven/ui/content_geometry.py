"""Translate local document cells into a dashboard's reserved content area.

Only positional fields move; scheduler identities and action arguments retain
their original values. Renderers may also use these helpers without a sidebar.
"""

from __future__ import annotations

from .. import layout as L


def shift_hit(y, kind, value, *, dx=0, dy=0):
    """Move one published hit without mutating the renderer's source map."""
    if isinstance(value, dict) and "left" in value and "right" in value:
        value = dict(value, left=dx + value["left"], right=dx + value["right"])
    elif kind == "sort_header" and isinstance(value, (tuple, list)) and len(value) == 4:
        value = (*value[:2], dx + value[2], dx + value[3])
    elif (
        kind
        in (
            "node_cell",
            "job_panel_tab",
            "job_panel_file",
            "job_panel_view",
            "job_panel_action",
        )
        and isinstance(value, (tuple, list))
        and len(value) == 3
    ):
        value = (value[0], dx + value[1], dx + value[2])
    return y + dy, kind, value


def prefix_rows(rows, left, width):
    """Reserve left cells once, clipping each local row at its real width."""
    if not left:
        return rows
    return [[(" " * left, "bg:canvas")] + L.clip_row(row, width) for row in rows]


def shift_header_controls(app, dx):
    """Header chips and notices publish outside the document's normal hit map."""
    if not dx:
        return
    notice = getattr(app, "notification_ui_state", None)
    if isinstance(notice, dict):
        notice["status_hits"] = [
            shift_hit(y, kind, value, dx=dx)
            for y, kind, value in notice.get("status_hits", ())
        ]
    chips = getattr(app, "table_chip_cells", None)
    if isinstance(chips, (tuple, list)) and len(chips) == 2:
        tab, cells = chips
        app.table_chip_cells = (
            tab,
            [(key, left + dx, right + dx) for key, left, right in cells],
        )
