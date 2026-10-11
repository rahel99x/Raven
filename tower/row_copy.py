"""Copy an exact native non-job row from its published terminal cells.

Rendered ranges and job marks have their own owners. This adapter covers
native resource cursors that intentionally bypass rendered-line selection.
It never renders a replacement page or reads a source while copying.
"""
from __future__ import annotations

from . import clipboard, interaction, text_selection


def _at(values, index):
    return values[index] if isinstance(index, int) and 0 <= index < len(values) else None


def _target(app):
    """Return exact painted row identifiers, rather than a screen position."""
    tab = getattr(app, "tab", "")
    cursor = getattr(app, "cursor", {})
    if tab in ("sources", "nodes", "cluster"):
        resources = getattr(app, "table_tools_state", {}).get("resource_ids", {})
        names = getattr(app, "source_ids", ()) if tab == "sources" else resources.get(tab, ())
        name = _at(names, cursor.get(tab, 0))
        kinds = {"sources": ("source",), "nodes": ("node_row", "node_cell"),
                 "cluster": ("partition_row",)}[tab]
        return tuple(f"{kind}:{name}" for kind in kinds) if name is not None else ()
    if tab == "log" and getattr(getattr(app, "logs", None), "browser", False):
        # log_entries only filters the already published in-memory catalog.
        entry = _at(app.log_entries(), app.logs.browser_cursor)
        return ("log_file:" + str(entry["id"]),) if entry else ()
    if tab == "research":
        if getattr(app, "research_view", "") == "arrays":
            group = _at(getattr(app, "research_groups", ()), cursor.get(tab, 0))
            if group:
                key = (group.get("id"), group.get("cluster", ""))
                # Repeated array IDs across clusters use the exact cohort key.
                return (f"research_array:{key}", f"research_array:{group.get('id')}")
            return ()
        if getattr(app, "research_view", "") == "evidence":
            state = getattr(app, "analysis_state", {})
            identity = _at(state.get("evidence_ids", ()), state.get("evidence_cursor", 0))
            return (f"research_evidence:{identity}",) if identity is not None else ()
    return None


def copy_selection(app):
    """Return whether this native cursor owns y, including guarded failures."""
    if getattr(app, "mode", "main") != "main" or getattr(app, "sel_anchor", None) is not None:
        return False
    toolbar = getattr(app, "toolbar_state", {})
    if toolbar.get("menu") or toolbar.get("panel"):
        return False
    if (getattr(app, "history_browser_state", {}).get("focused")
            or getattr(getattr(app, "layout_state", None), "focus", "main") == "details"):
        return False
    from .job_selection import cleared, lines_cleared
    array_cursor = getattr(app, "tab", "") == "research" and getattr(app, "research_view", "") == "arrays"
    if cleared(app) or (lines_cleared(app) and not array_cursor):
        return False
    identities = _target(app)
    if identities is None:
        return False
    if not identities:
        app.fail("No row is selected. Select a row or use v to select text.")
        return True
    graph = interaction._current(app)
    frame = text_selection._current(app)
    if graph is None or frame is None:
        app.fail("The selected row changed since the display was drawn. Select it in the current display before copying.")
        return True
    # The navigation graph deduplicates semantic IDs. The immutable hit
    # publication also retains repeated native fragments, such as a log
    # catalog label followed by its path, and a node matrix above its table.
    # Reuse the same cell clipping adapter for those fragments, only on y.
    state = getattr(app, "interaction_state", {})
    rows = [frame["rows"].get(y, ((0, ()),))[0][1] for y in range(graph.height)]
    fragments = tuple(interaction._hit_controls(app, rows, state.get("published_hits", ()),
                                                graph.width, graph.height))
    controls = ()
    for identity in identities:
        visible = graph.get(identity)
        if visible is None or visible.button or visible.layer:
            continue
        controls = tuple(control for control in fragments if control.id == identity)
        if controls:
            break
    if not controls:
        app.fail("The selected row is outside the current display. Scroll to it or use v to select text before copying.")
        return True
    # Nodes can be represented both in a compact matrix and in a full table.
    # Prefer the full table rather than duplicate values from both views.
    if not identities[0].startswith("log_file:"):
        controls = (controls[-1],)
    lines = []
    seen = set()
    for control in sorted(controls, key=lambda item: (item.rect.top, item.rect.left)):
        rect = control.rect
        if rect in seen:
            continue
        seen.add(rect)
        pane = text_selection.Pane(control.id, rect, None)
        for y in range(rect.top, rect.bottom):
            line = text_selection._line(frame, pane, y)
            if line is None:
                app.fail("The selected row is covered by another view. Close that view before copying.")
                return True
            lines.append(line)
    text = "\n".join(lines).rstrip() + "\n"
    if not text.strip():
        app.fail("The selected row is empty.")
        return True
    if len(text.encode("utf-8")) > text_selection.MAX_BYTES:
        app.fail("The selected row is too large to copy. Export its source instead.")
        return True
    message = clipboard.copy(text, getattr(app, "state_dir", None), **clipboard.options(app))
    app.say(message)
    store = getattr(app, "store", None)
    if store is not None:
        store.event("copy", message)
    return True
