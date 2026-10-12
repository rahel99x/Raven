"""Cheap viewport and final geometry adapters for boxed scrollable dialogs.

These helpers operate only on rows and counts the dialog already published.
They never enumerate a source, change a selected item, or start background work.
"""
from __future__ import annotations

from . import layout as L


def window(app, key, target, count, page, *, context=(), focus=None):
    """Retain explicit scrolling until the dialog's selected item changes."""
    from . import scrollbars as S
    from .scrolling import viewport
    if focus is not None:
        anchors = getattr(app, "modal_scrollbar_focus", None)
        if not isinstance(anchors, dict):
            anchors = app.modal_scrollbar_focus = {}
        identity = (context, focus)
        if key in anchors and anchors[key] != identity:
            S.resume(app, key)
        anchors[key] = identity
    override = S.manual(app, key, context=context)
    if override is not None:
        target = override
    target = max(0, min(max(0, count - page), int(target)))
    painted = viewport(app, key, target, count, page, context=context)
    return target, painted


def boxed(app, key, rendered, *, start, count, page, target, painted,
          setter, context=(), header=0, rows=None):
    """Register visible boxed rows, reserving header buttons and one rail cell.

    ``start`` and ``header`` are logical interior-row indices, excluding the
    border. The returned rows retain the box's dimensions and exact styles.
    """
    from . import scrollbars as S
    if len(rendered) < 3 or page <= 0:
        return rendered
    output = list(rendered)
    first = start + 1
    if first < 1 or first >= len(output) - 1:
        return rendered
    span = max(0, min(page, count - painted)) if rows is None else max(0, rows)
    end = min(len(output) - 1, first + span)
    if end <= first:
        return rendered
    y, x, row = output[first]
    width = L.vlen(L.row_text(row))
    if width < 7:
        return rendered
    left, right = x + 1, x + width - 1
    # Keep the right border intact and reserve the last interior cell. The
    # rail paints on top of this blank, so a full-width label cannot overlap it.
    for index in range(first, end):
        ry, rx, segments = output[index]
        inside = L.clip_row(segments[1:-1], right - left - 1)
        inside = L.fill_row(inside, right - left, "text+bg:surface")
        output[index] = (ry, rx, segments[:1] + inside + segments[-1:])
    header_index = header + 1
    header_rect = None
    if header_index == 0:
        hy, hx, segments = output[0]
        text = L.row_text(segments)
        if L.vlen(text) >= 8:
            style = segments[0][1] if segments else "accent+bg:surface"
            shifted = text[:1] + "    " + L.cut(text[1:-1], width - 6, True) + text[-1:]
            output[0] = (hy, hx, [(shifted, style)])
            header_rect = (hy, hx + 1, hx + width - 1)
    elif 0 < header_index < len(output) - 1:
        hy, hx, segments = output[header_index]
        hwidth = L.vlen(L.row_text(segments)) - 2
        if hwidth >= 6:
            inside = [("    ", "text+bg:surface")] + L.clip_row(segments[1:-1], hwidth - 4)
            inside = L.fill_row(inside, hwidth, "text+bg:surface")
            output[header_index] = (hy, hx, segments[:1] + inside + segments[-1:])
            header_rect = (hy, hx + 1, hx + width - 1)
    S.register(app, key, (y, left, output[end - 1][0] + 1, right),
               count, page, target, painted, setter, context=context,
               header=header_rect, layer=1, absolute=True)
    return output
