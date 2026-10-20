"""Explicit, reversible launch presentation controls; no scheduler I/O."""

from __future__ import annotations
from .table_query import snapshot
from .ui.list_preferences import mode as list_mode

from raven.jobs import group_domain as G
from raven.jobs import manual_groups as M
from .ui.list_preferences import invalidate as _invalidate


def initialize(app):
    from raven.jobs.list_menu import initialize as menu_initialize

    menu_initialize(app)


def handle_key(app, key):
    from raven.jobs.list_menu import handle_key as menu_key

    return menu_key(app, key)


def handle_mouse(app, y, x, button="left", shift=False):
    from raven.jobs.list_menu import handle_mouse as menu_mouse

    return menu_mouse(app, y, x, button=button, shift=shift)


def tick(app):
    from raven.jobs.list_menu import tick as menu_tick

    menu_tick(app)


def overlay(views, snap, app, width, height):
    from raven.jobs.list_menu import overlay as menu_overlay

    return menu_overlay(views, snap, app, width, height)


def command_names():
    return ["grouping", "joblist", "joblistmenu"]


def controls(app, g, width):
    from .ui.list_controls import controls as list_controls

    return list_controls(app, g, width, "jobs")


def execute(app, action, snap=None):
    """All listings share these choices; undo never overwrites manual edits."""
    if snap is None:
        snap = snapshot(app, app.store.snapshot())
    table = app.table_state
    registry = G.registry(app)
    index = registry.ensure(snap)
    if action in ("sort-min", "sort-max"):
        from raven.jobs.batch_sort import mode

        chosen = action[5:]

        changed = mode(app) != chosen or list_mode(app, "jobs") != "grouped"
        table["jobs_batch_sort"] = chosen
        table.setdefault("job_list_modes", {})["jobs"] = "grouped"
        _invalidate(app)
        # Synchronize the actual selected job before another input can act on
        # old row positions; the existing sort path preserves exact identity.
        callback = getattr(app, "table_sort_changed", None)
        if callable(callback):
            callback("jobs", persist=False)
        message = (
            "Fewest running first: fewer active jobs, then fewer total queue jobs per batch"
            if chosen == "min"
            else "Most running first: more active jobs, then fewer total queue jobs per batch"
        )
        return M.Result(changed, message + "; Raven Heavy first")
    elif action == "auto":
        before = dict(
            enabled=table.get("groups", True),
            auto=table.get("auto_groups", True),
            collapsed=set(registry.collapsed),
            revision=getattr(app, "manual_job_groups_revision", 0),
            jobs_mode=table.get("job_list_modes", {}).get("jobs"),
        )
        table.update(groups=True, auto_groups=True)
        table.setdefault("job_list_modes", {})["jobs"] = "grouped"
        registry = G.registry(app)
        index = registry.ensure(snap)
        count = 0
        for group in index.groups.values():
            if group.kind != "manual":
                # Never evict a user's existing folded group to fit another
                # automatic fold into the bounded preference list.
                if (
                    registry.is_collapsed(group)
                    or len(registry.collapsed) < G.MAX_COLLAPSED
                ):
                    registry.fold(group.id, True)
                count += 1
        table["collapsed"] = sorted(registry.collapsed)
        before["added"] = registry.collapsed - before["collapsed"]
        # Repeated presses are idempotent and retain the useful undo record.
        if (
            not before["enabled"]
            or not before["auto"]
            or before["added"]
            or before["jobs_mode"] == "sequential"
        ):
            app.grouping_auto_undo = before
        message = f"Automatic groups enabled ({count} detected); manual groups and exclusions kept"
    elif action == "undo":
        before = getattr(app, "grouping_auto_undo", None)
        if not before:
            return M.Result(False, "There is no Auto Group change to undo")
        table["auto_groups"] = before["auto"]
        if before["revision"] == getattr(app, "manual_job_groups_revision", 0):
            modes = table.setdefault("job_list_modes", {})
            if before.get("jobs_mode") is None:
                modes.pop("jobs", None)
            else:
                modes["jobs"] = before["jobs_mode"]
        # A group created since Auto remains visible when the old view was off.
        if table.get("groups", True):
            table["groups"] = before["enabled"] or (
                bool(registry.manual.groups())
                and before["revision"] != getattr(app, "manual_job_groups_revision", 0)
            )
        registry.collapsed.difference_update(before["added"])
        table["collapsed"] = sorted(registry.collapsed)
        app.grouping_auto_undo = None
        message = "Auto Group changes undone; manual groups kept"
    elif action == "clear":
        if (
            not index.groups
            and not registry.manual.groups()
            and not table.get("auto_groups", True)
        ):
            return M.Result(False, "All groups are already ungrouped")
        app.grouping_all_undo = dict(
            groups=registry.manual.groups(),
            detached=registry.manual.detached(),
            labels=dict(registry.manual._labels),
            enabled=table.get("groups", True),
            auto=table.get("auto_groups", True),
            collapsed=list(table.get("collapsed", ())),
        )
        M._save(app, registry, {}, set())
        table.update(groups=True, auto_groups=False, collapsed=[])
        app.grouping_all_undo["revision"] = getattr(
            app, "manual_job_groups_revision", 0
        )
        app.grouping_auto_undo = None
        message = "All shared groups removed; Auto Group stays off. Use :grouping restore to undo"
    elif action == "restore":
        before = getattr(app, "grouping_all_undo", None)
        if not before:
            return M.Result(False, "There is no Ungroup All change to restore")
        if before["revision"] != getattr(
            app, "manual_job_groups_revision", 0
        ) or table.get("auto_groups", True):
            return M.Result(
                False,
                "Grouping changed after Ungroup All; restore would overwrite newer choices",
            )
        M._save(
            app, registry, before["groups"], before["detached"], labels=before["labels"]
        )
        table.update(
            groups=before["enabled"],
            auto_groups=before["auto"],
            collapsed=before["collapsed"],
        )
        app.grouping_all_undo = None
        message = "Grouping restored to before Ungroup All"
    elif action == "collapse":
        if not index.groups:
            return M.Result(False, "There are no groups to collapse")
        if len(index.groups) > G.MAX_COLLAPSED:
            return M.Result(
                False,
                f"Collapse All needs at most {G.MAX_COLLAPSED} known groups; no folds changed",
            )
        previous = set(registry.collapsed)
        # Fold the complete current index before retaining older preferences.
        # This avoids bounded storage evicting a group we have just folded.
        registry.collapsed.clear()
        for group in index.groups.values():
            registry.fold(group.id, True)
        remaining = G.MAX_COLLAPSED - len(registry.collapsed)
        registry.collapsed.update(sorted(previous - registry.collapsed)[:remaining])
        table["collapsed"] = sorted(registry.collapsed)
        selected_group = index.for_job(getattr(app, "selected_id", None))
        if selected_group is not None:
            G._reanchor(app, selected_group.id)
        undo = getattr(app, "grouping_auto_undo", None)
        if isinstance(undo, dict):
            # Like an explicit disclosure click, this action owns its folds.
            undo.get("added", set()).difference_update(registry.collapsed)
        message = "All shared groups collapsed; membership unchanged"
        if previous == registry.collapsed:
            return M.Result(False, message)
    elif action == "expand":
        if not table.get("collapsed"):
            return M.Result(False, "All groups are already expanded")
        table["collapsed"] = []
        message = "All shared groups expanded"
    else:
        return M.Result(
            False,
            "Use grouping auto, undo, clear, expand, collapse, sort-min, sort-max, or restore",
        )
    _invalidate(app)
    G.registry(app).ensure(snap)
    return M.Result(True, message)


def run_command(app, args):
    from raven.jobs.list_menu import run_command as menu_command

    if args and args[0] == "joblistmenu":
        return menu_command(app, args)
    from raven.jobs.list_controls import run_command as job_list_command

    if args and args[0] == "joblist":
        return job_list_command(app, args)
    if not args or args[0] != "grouping":
        return False
    if len(args) != 2:
        app.say(
            "Use grouping auto, undo, clear, expand, collapse, sort-min, sort-max, or restore"
        )
        return True
    result = execute(app, args[1])
    app.say(result.message)
    if result.changed:
        app.save()
    return True
