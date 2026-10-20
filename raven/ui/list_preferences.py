"""Persisted job-list preferences and in-memory fold state, without UI handlers."""

from __future__ import annotations
from raven.jobs.group_records import MAX_COLLAPSED, array_parent, valid_group_id

CONTEXTS = (
    "jobs",
    "recent",
    "history",
    "group",
    "analytics:advisor",
    "history:analytics",
    "history:research",
    "history:log",
    "history:deps",
)
MODES = ("grouped", "sequential")
BATCH_MODES = ("min", "max")


def recent_mode(app):
    value = getattr(app, "table_state", {}).get("recents_mode", "grouped")
    return value if value in MODES else "grouped"


def jobs_batch_mode(app):
    value = getattr(app, "table_state", {}).get("jobs_batch_sort", "min")
    return value if value in BATCH_MODES else "min"


def mode(app, context):
    if context == "recent":
        return recent_mode(app)
    state = getattr(app, "table_state", {})
    value = state.get("job_list_modes", {}).get(context)
    return (
        value
        if value in MODES
        else "grouped"
        if state.get("groups", False)
        else "sequential"
    )


def batch_mode(app, context):
    if context == "jobs":
        return jobs_batch_mode(app)
    value = getattr(app, "table_state", {}).get("job_list_sorts", {}).get(context)
    return value if value in BATCH_MODES else None


def fingerprint(app, context):
    return mode(app, context), batch_mode(app, context)


def metadata_for_record(app, tab, job_id):
    return getattr(app, "job_group_metadata", {}).get(tab, {}).get(job_id)


def painted_group(app, context, job_id, group):
    """A hidden fold never turns an individual-row action into a batch action."""
    if context == "jobs" and job_id in getattr(app, "recent_ids", ()):
        context = "recent"
    meta = metadata_for_record(app, context, job_id)
    return bool(meta and meta.header and meta.collapsed and meta.group.id == group.id)


def restore(state, data):
    for key, allowed in (("job_list_modes", MODES), ("job_list_sorts", BATCH_MODES)):
        source = data.get(key, {})
        state[key] = (
            {
                context: value
                for context, value in source.items()
                if context in CONTEXTS and isinstance(value, str) and value in allowed
            }
            if isinstance(source, dict)
            else {}
        )


def is_collapsed(registry, group):
    parent = array_parent(group)
    return (
        group.id in registry.collapsed
        or group.kind == "array"
        and group.id == "array:" + parent
        and parent in registry.collapsed
    )


def fold_registry(registry, group_id, collapsed=None, *, maximum=MAX_COLLAPSED):
    if not valid_group_id(group_id):
        return False
    group = registry.index.groups.get(group_id)
    if group is None:
        return False
    value = not is_collapsed(registry, group) if collapsed is None else bool(collapsed)
    registry.collapsed.discard(group.id)
    parent = array_parent(group)
    if group.kind == "array":
        registry.collapsed.discard(parent)
    stored = (
        parent if group.kind == "array" and group.id == "array:" + parent else group.id
    )
    if value:
        registry.collapsed.add(stored)
    if len(registry.collapsed) > maximum:
        registry.collapsed = (
            set(sorted(registry.collapsed - {stored})[-(maximum - 1) :]) | {stored}
            if value
            else set(sorted(registry.collapsed)[-maximum:])
        )
    return True


def reanchor(app, group_id):
    selected = getattr(app, "selected_id", None)
    tab = getattr(app, "tab", "")
    contexts = (
        ("jobs", "recent")
        if tab == "jobs"
        else (tab, "history:" + tab, "history_dock:" + tab, "history_dock")
    )
    meta = next(
        (
            metadata_for_record(app, context, selected)
            for context in contexts
            if metadata_for_record(app, context, selected) is not None
        ),
        None,
    )
    if meta is None or meta.group.id != group_id or meta.representative_id == selected:
        return
    app.selected_id = meta.representative_id
    cursor = getattr(app, "cursor", {})
    ids = {
        "jobs": getattr(app, "last_jobs_ids", ()),
        "history": getattr(app, "last_history_ids", ()),
        "group": getattr(app, "group_ids", ()),
        "deps": getattr(app, "dep_ids", ()),
    }.get(tab, ()) or ()
    if tab in cursor and meta.representative_id in ids:
        cursor[tab] = ids.index(meta.representative_id)


def after_fold(app, registry, group_id):
    undo = getattr(app, "grouping_auto_undo", None)
    if isinstance(undo, dict):
        group = registry.index.groups[group_id]
        undo.get("added", set()).discard(group_id)
        if group.kind == "array":
            undo.get("added", set()).discard(array_parent(group))
    app.table_state["collapsed"] = sorted(registry.collapsed)
    if registry.is_collapsed(registry.index.groups[group_id]):
        reanchor(app, group_id)


def invalidate(app):
    app.job_groups_frame_index = None
    getattr(app, "interaction_state", {}).update(
        active=False, focused=None, pressed=None, frame_required=True
    )
    getattr(app, "history_browser_state", {}).pop("item_cache", None)
    for name in (
        "job_selection_state",
        "job_group_drag_state",
        "group_queue_drag_state",
    ):
        value = getattr(app, name, None)
        if isinstance(value, dict):
            if value.get("capture"):
                value["discard_release"] = True
            value["capture"] = None
            if name == "job_group_drag_state":
                value.update(pending=None, armed=None)
