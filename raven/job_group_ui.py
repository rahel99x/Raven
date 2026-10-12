"""Theme-aware group presentation; allocation identities remain untouched."""
from __future__ import annotations

from . import job_groups, layout as L


def fold_icon(collapsed, *, ascii_=False):
    """Show the action: right folds an open batch; down reopens its summary."""
    return ("v" if collapsed else ">") if ascii_ else ("▾" if collapsed else "▸")


def fold_command(group_id, collapsed):
    """Use the painted state so a repeated click before repaint is idempotent."""
    import shlex
    return "jobgroup " + ("open " if collapsed else "close ") + shlex.quote(group_id)


def has_collapsed(app, context):
    if not getattr(app, "table_state", {}).get("collapsed"):
        return False
    return any(meta.header and meta.collapsed for meta in
               getattr(app, "job_group_metadata", {}).get(context, {}).values())


def finished_row(app, context, row):
    """Decorate one representative without reporting its metrics as a batch."""
    meta = job_groups.metadata_for_record(app, context, row["id"])
    if meta is None or not meta.header or not meta.collapsed:
        return row
    row = dict(row)
    for key in ("part", "elapsed", "cpus", "gpus", "ce", "me", "rss", "start", "end", "exit", "nodes", "tags"):
        row[key] = ""
    row.update(name=f"{meta.group.label} / {meta.visible_count} records", state="BATCH",
               info=job_groups.summary(meta.stats or meta.records, compact=True), _group=meta)
    row["_styles"] = dict(row.get("_styles", {}), name="accent+bold", state="accent", info="accent")
    return row


def summary_row(records, width, *, ascii_=False, selected=False):
    """Fit status badges to cell boundaries, retaining the current theme."""
    segments = job_groups.summary_segments(records, ascii_=ascii_, compact=True)
    if L.vlen(L.row_text(segments)) > width:
        # Narrow docks still show all observed states, instead of truncating
        # the critical blocked-dependency badge at the right edge.
        counts = job_groups.status_counts(records)
        badges = (("running", "R", "green"), ("pending", "P", "yellow"),
                  ("dependent", "D", "cyan"), ("blocked", "!", "red"),
                  ("completed", "C", "green"), ("failed", "F", "red"),
                  ("cancelled", "X", "yellow"), ("other", "?", "dim"))
        segments = []
        for key, label, style in badges:
            if counts[key]:
                if segments:
                    segments.append((" ", "dim"))
                segments.append((f"{label}{counts[key]}", style))
    if selected:
        segments = [(text, style + "+sel") for text, style in segments]
    row = L.clip_row(segments, max(0, width))
    remaining = max(0, width - L.vlen(L.row_text(row)))
    return row + ([(" " * remaining, "sel" if selected else "")] if remaining else [])


def paint_info(row, meta, bounds, width, *, ascii_=False):
    """Replace only the INFO cell, preserving marks and exact hit geometry."""
    left, right = bounds
    if not meta.collapsed or right <= left:
        return row
    from .pane_drag import _replace
    selected = any("rev" in style.split("+") or "sel" in style.split("+") for _, style in row)
    badges = summary_row(getattr(meta, "stats", None) or meta.records, right - left, ascii_=ascii_, selected=selected)
    # Slice the original table row exactly once. Replacing each badge and
    # separator independently scans wide table rows repeatedly on every frame.
    # A private sentinel preserves _replace's wide/combining boundary rules;
    # it is removed before the row can reach the terminal renderer.
    marker = object()
    result = _replace(row, left, L.row_text(badges), marker, width)
    return [segment for text, style in result
            for segment in (badges if style is marker else [(text, style)])]
