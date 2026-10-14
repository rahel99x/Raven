"""Persistent presentation choices for lists of actual job records.

Scientific tables and dependency trees keep their own ordering. These controls
operate only on the records already admitted to a job listing.
"""
from __future__ import annotations

CONTEXTS = ("jobs", "recent", "history", "group", "analytics:advisor", "history:analytics",
            "history:research", "history:log", "history:deps")
MODES = ("grouped", "sequential")


def mode(app, context):
    if context == "recent":
        from .recents_ui import mode as recent_mode
        return recent_mode(app)
    state = getattr(app, "table_state", {})
    value = state.get("job_list_modes", {}).get(context)
    if value in MODES:
        return value
    return "grouped" if state.get("groups", False) else "sequential"


def batch_mode(app, context):
    if context == "jobs":
        from .job_batch_sort import mode as jobs_mode
        return jobs_mode(app)
    value = getattr(app, "table_state", {}).get("job_list_sorts", {}).get(context)
    return value if value in ("min", "max") else None


def fingerprint(app, context):
    return mode(app, context), batch_mode(app, context)


def painted_group(app, context, job_id, group):
    """A hidden fold never turns an individual-row action into a batch action."""
    from .job_groups import metadata_for_record
    if context == "jobs" and job_id in getattr(app, "recent_ids", ()):
        context = "recent"
    meta = metadata_for_record(app, context, job_id)
    return bool(meta and meta.header and meta.collapsed and meta.group.id == group.id)


def restore(state, data):
    for key, allowed in (("job_list_modes", MODES), ("job_list_sorts", ("min", "max"))):
        source = data.get(key, {})
        state[key] = {context: value for context, value in source.items()
                      if context in CONTEXTS and isinstance(value, str) and value in allowed} if isinstance(source, dict) else {}


def controls(app, g, width, context):
    from .control_rows import buttons
    from .layout import vlen
    view = mode(app, context).title()
    order = {"min": "Fewest running first", "max": "Most running first"}.get(batch_mode(app, context), "Column order")
    # Three bounded menus replace nine competing actions. At narrow split-pane
    # widths the full current value stays available in the semantic label/menu.
    labels = [("view", "View: " + view), ("order", "Order: " + order), ("groups", "Groups")]
    if sum(vlen(label) + 2 for _, label in labels) + 2 > width:
        labels = [("view", "View: " + view), ("order", "Order"), ("groups", "Groups")]
    if sum(vlen(label) + 2 for _, label in labels) + 2 > width:
        labels = [("view", "View"), ("order", "Order"), ("groups", "Groups")]
    rows, hits = buttons(g, width, [(key, label, ("command", f"joblistmenu {context} {key}"))
                                   for key, label in labels],
                         group="job-list-controls", prefix=f"joblist:{context}:", gap=0)
    full = {"view": "View: " + view + "; change presentation without removing assignments",
            "order": "Order: " + order + "; batch order applies in Grouped view",
            "groups": "Groups: expand, collapse, or manage shared assignments"}
    for _, _, value in hits:
        value["label"] = full[value["id"].rsplit(":", 1)[-1]]
    return rows, hits


def run_command(app, args, *, preserve_view=False):
    if not args or args[0] != "joblist":
        return False
    if len(args) != 3 or args[1] not in CONTEXTS or args[2] not in (*MODES, "sort-min", "sort-max", "column-order"):
        app.say("Use joblist <jobs|recent|history|group|history:PAGE> <grouped|sequential|sort-min|sort-max|column-order>")
        return True
    context, action = args[1:]
    if context == "recent" and action in MODES:
        from .recents_ui import run_command as recent_command
        return recent_command(app, ["recents", action])
    from . import grouping_toolbar, table_sort
    table = app.table_state
    if action == "column-order":
        if context == "jobs":
            app.say("Jobs use Fewest or Most running first; choose either order")
            return True
        table.get("job_list_sorts", {}).pop(context, None)
    elif action.startswith("sort-"):
        choice = action[5:]
        if context == "jobs":
            table["jobs_batch_sort"] = choice
        else:
            table.setdefault("job_list_sorts", {})[context] = choice
        if not preserve_view:
            if context == "recent":
                table["recents_mode"] = "grouped"
            else:
                table.setdefault("job_list_modes", {})[context] = "grouped"
    else:
        table.setdefault("job_list_modes", {})[context] = action
        if context == "history":
            table_sort.reset_sort(app, context)
    # Keep an older admitted recent record across a preference-cache reset.
    if context == "recent":
        from .recent_history import initialize
        state = initialize(app)
        selected = getattr(app, "selected_id", None)
        if selected and any(record.id == selected for record in state.loaded):
            state.transition_anchor = selected
    grouping_toolbar._invalidate(app)
    callback = getattr(app, "table_sort_changed", None)
    if callable(callback) and context in ("jobs", "recent", "history", "group"):
        callback(context, persist=False)
    if context.startswith("history:"):
        browser = getattr(app, "history_browser_state", {})
        browser["last_selected"] = None
        browser["frame"] = None
    if context == "analytics:advisor":
        getattr(app, "analytics_scroll_offsets", {}).pop("advisor", None)
    detail = ("fewer active jobs, then fewer total jobs" if action == "sort-min" else
              "more active jobs, then fewer total jobs" if action == "sort-max" else
              "column order (completion time by default)" if action == "column-order" else
              "contiguous batches" if action == "grouped" else "individual job rows; group assignments kept")
    app.say(context.replace("history:", "") + ": " + detail + "; preferences saved for this listing")
    app.save()
    return True
