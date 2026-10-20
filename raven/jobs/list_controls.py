"""Persistent presentation choices for lists of actual job records.

Scientific tables and dependency trees keep their own ordering. These controls
operate only on the records already admitted to a job listing.
"""

from __future__ import annotations
from raven.ui.recent_state import initialize

from raven.ui.list_preferences import (
    CONTEXTS as CONTEXTS,
    MODES as MODES,
    mode as mode,
    batch_mode as batch_mode,
    fingerprint as fingerprint,
    painted_group as painted_group,
    restore as restore,
    invalidate,
)
from raven.ui.list_controls import controls as controls


def run_command(app, args, *, preserve_view=False):
    if not args or args[0] != "joblist":
        return False
    if (
        len(args) != 3
        or args[1] not in CONTEXTS
        or args[2] not in (*MODES, "sort-min", "sort-max", "column-order")
    ):
        app.say(
            "Use joblist <jobs|recent|history|group|history:PAGE> <grouped|sequential|sort-min|sort-max|column-order>"
        )
        return True
    context, action = args[1:]
    if context == "recent" and action in MODES:
        from raven.recents_ui import run_command as recent_command

        return recent_command(app, ["recents", action])
    from raven import table_sort

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
        state = initialize(app)
        selected = getattr(app, "selected_id", None)
        if selected and any(record.id == selected for record in state.loaded):
            state.transition_anchor = selected
    invalidate(app)
    callback = getattr(app, "table_sort_changed", None)
    if callable(callback) and context in ("jobs", "recent", "history", "group"):
        callback(context, persist=False)
    if context.startswith("history:"):
        browser = getattr(app, "history_browser_state", {})
        browser["last_selected"] = None
        browser["frame"] = None
    if context == "analytics:advisor":
        getattr(app, "analytics_scroll_offsets", {}).pop("advisor", None)
    detail = (
        "fewer active jobs, then fewer total jobs"
        if action == "sort-min"
        else "more active jobs, then fewer total jobs"
        if action == "sort-max"
        else "column order (completion time by default)"
        if action == "column-order"
        else "contiguous batches"
        if action == "grouped"
        else "individual job rows; group assignments kept"
    )
    app.say(
        context.replace("history:", "")
        + ": "
        + detail
        + "; preferences saved for this listing"
    )
    app.save()
    return True
