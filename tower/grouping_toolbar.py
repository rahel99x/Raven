"""Explicit, reversible launch presentation controls; no scheduler I/O."""
from __future__ import annotations

from . import job_groups as G, manual_job_groups as M


def initialize(app):
    pass


def handle_key(app, key):
    return False


def overlay(views, snap, app, width, height):
    return None


def command_names():
    return ["grouping"]


def controls(app, g, width):
    from .control_rows import buttons
    return buttons(g, width, [(key, label, ("command", "grouping " + key)) for key, label in
        (("auto", "Auto Group"), ("undo", "Undo Auto"), ("clear", "Ungroup All"), ("expand", "Expand All"))],
        group="grouping-toolbar", prefix="grouping:")


def _invalidate(app):
    app.job_groups_frame_index = None
    getattr(app, "interaction_state", {}).update(active=False, focused=None, pressed=None, frame_required=True)
    getattr(app, "history_browser_state", {}).pop("item_cache", None)
    # Membership and selection gestures must not survive a projection change.
    for name in ("job_selection_state", "job_group_drag_state", "group_queue_drag_state"):
        value = getattr(app, name, None)
        if isinstance(value, dict):
            if value.get("capture"):
                value["discard_release"] = True
            value["capture"] = None
            if name == "job_group_drag_state":
                value.update(pending=None, armed=None)


def execute(app, action, snap=None):
    """All listings share these choices; undo never overwrites manual edits."""
    if snap is None:
        from .table_tools import snapshot
        snap = snapshot(app, app.store.snapshot())
    table = app.table_state
    registry = G.registry(app)
    index = registry.ensure(snap)
    if action == "auto":
        before = dict(enabled=table.get("groups", True), auto=table.get("auto_groups", True),
                      collapsed=set(registry.collapsed), revision=getattr(app, "manual_job_groups_revision", 0))
        table.update(groups=True, auto_groups=True)
        registry = G.registry(app)
        index = registry.ensure(snap)
        count = 0
        for group in index.groups.values():
            if group.kind != "manual":
                # Never evict a user's existing folded group to fit another
                # automatic fold into the bounded preference list.
                if registry.is_collapsed(group) or len(registry.collapsed) < G.MAX_COLLAPSED:
                    registry.fold(group.id, True)
                count += 1
        table["collapsed"] = sorted(registry.collapsed)
        before["added"] = registry.collapsed - before["collapsed"]
        # Repeated presses are idempotent and retain the useful undo record.
        if not before["enabled"] or not before["auto"] or before["added"]:
            app.grouping_auto_undo = before
        message = f"Automatic groups enabled ({count} detected); manual groups and exclusions kept"
    elif action == "undo":
        before = getattr(app, "grouping_auto_undo", None)
        if not before:
            return M.Result(False, "There is no Auto Group change to undo")
        table["auto_groups"] = before["auto"]
        # A group created since Auto remains visible when the old view was off.
        if table.get("groups", True):
            table["groups"] = before["enabled"] or (bool(registry.manual.groups()) and
                before["revision"] != getattr(app, "manual_job_groups_revision", 0))
        registry.collapsed.difference_update(before["added"])
        table["collapsed"] = sorted(registry.collapsed)
        app.grouping_auto_undo = None
        message = "Auto Group changes undone; manual groups kept"
    elif action == "clear":
        if not index.groups and not registry.manual.groups() and not table.get("auto_groups", True):
            return M.Result(False, "All groups are already ungrouped")
        app.grouping_all_undo = dict(groups=registry.manual.groups(), detached=registry.manual.detached(),
            labels=dict(registry.manual._labels), enabled=table.get("groups", True),
            auto=table.get("auto_groups", True), collapsed=list(table.get("collapsed", ())))
        M._save(app, registry, {}, set())
        table.update(groups=True, auto_groups=False, collapsed=[])
        app.grouping_all_undo["revision"] = getattr(app, "manual_job_groups_revision", 0)
        app.grouping_auto_undo = None
        message = "All shared groups removed; Auto Group stays off. Use :grouping restore to undo"
    elif action == "restore":
        before = getattr(app, "grouping_all_undo", None)
        if not before:
            return M.Result(False, "There is no Ungroup All change to restore")
        if before["revision"] != getattr(app, "manual_job_groups_revision", 0) or table.get("auto_groups", True):
            return M.Result(False, "Grouping changed after Ungroup All; restore would overwrite newer choices")
        M._save(app, registry, before["groups"], before["detached"], labels=before["labels"])
        table.update(groups=before["enabled"], auto_groups=before["auto"], collapsed=before["collapsed"])
        app.grouping_all_undo = None
        message = "Grouping restored to before Ungroup All"
    elif action == "expand":
        if not table.get("collapsed"):
            return M.Result(False, "All groups are already expanded")
        table["collapsed"] = []
        message = "All shared groups expanded"
    else:
        return M.Result(False, "Use grouping auto, undo, clear, expand, or restore")
    _invalidate(app)
    G.registry(app).ensure(snap)
    return M.Result(True, message)


def run_command(app, args):
    if not args or args[0] != "grouping":
        return False
    if len(args) != 2:
        app.say("Use grouping auto, undo, clear, expand, or restore")
        return True
    result = execute(app, args[1])
    app.say(result.message)
    if result.changed:
        app.save()
    return True
