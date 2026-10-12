"""Focused-list manual grouping; scheduler actions retain exact job targets."""
from __future__ import annotations


def _finish(app, context, result, targets, *, created=False):
    from . import job_groups
    from .job_selection import initialize, resume

    initialize(app)["capture"] = None
    app.marks.difference_update(result.job_ids)
    resume(app)
    selected = context.get("selected")
    if created:
        app.table_state["groups"] = True
        job_groups.fold(app, result.group_id, True)
        selected = targets[0]
    elif selected not in context["ids"]:
        selected = targets[0] if targets else selected
    if selected:
        app.selected_id = selected
        scope = context["scope"]
        if scope.startswith("history:"):
            from .history_browser import activate
            activate(app, selected)
        elif scope == "analytics:advisor":
            app.analytics_job = selected
        elif selected in context["ids"]:
            app.cursor[app.tab] = context["ids"].index(selected)

    # A projection change invalidates every old hit map, including controls
    # held by a late terminal release. Never reroute the old row to a sibling.
    pointer = getattr(app, "interaction_state", {})
    pointer.update(active=False, focused=None, pressed=None, frame_required=True)
    app.job_groups_frame_index = None
    browser = getattr(app, "history_browser_state", {})
    browser.pop("item_cache", None)
    retained = len(app.marks)
    app.say(result.message + (f"; {retained} other job marks kept" if retained else ""))
    app.save()


def run_action(app, action, *, explicit=False):
    """Use marked rows in this list, or its cursor for member/group removal."""
    from .job_selection import context
    current = context(app)
    if current is None:
        if explicit:
            app.fail("Focus a job list before grouping or ungrouping jobs")
        return explicit
    ids = current["ids"]
    marks = getattr(app, "marks", set())
    targets = list(dict.fromkeys(jid for jid in ids if jid in marks))
    if action == "create" and len(targets) < 2:
        if explicit:
            app.fail("Mark at least two jobs in this list to create a group")
        return explicit
    if action == "ungroup" and not targets:
        selected = current.get("selected")
        targets = [selected] if selected in ids else []
    if not targets:
        if explicit:
            app.fail("Select a grouped job or a collapsed group first")
        return explicit

    # Keyboard ownership ends the drag even when source validation rejects the
    # command. A delayed release must not extend an obsolete range afterward.
    from .job_selection import initialize
    initialize(app)["capture"] = None

    from .table_tools import snapshot
    from . import job_groups, manual_job_groups
    snap = snapshot(app, app.store.snapshot())
    published = current.get("tokens", {})
    observed = manual_job_groups.selection_tokens(snap, targets)
    if any(published.get(jid) is None or observed.get(jid) != published[jid] for jid in targets):
        app.say("Selected jobs changed; select the current rows again before grouping")
        return True
    registry = job_groups.registry(app)
    index = registry.ensure(snap)
    if action == "create":
        result = manual_job_groups.create(app, snap, targets)
    else:
        groups, members = [], []
        for jid in targets:
            group = index.for_job(jid)
            if group is None:
                continue
            # Only a displayed collapsed row implies the whole group. An
            # expanded member never silently adds its siblings to the action.
            if app.table_state.get("groups") and registry.is_collapsed(group):
                groups.append(group.id)
            else:
                members.append(jid)
        if not groups and not members:
            if explicit:
                app.fail("The selected jobs are not grouped")
            return explicit
        result = manual_job_groups.detach(app, snap, members, group_ids=groups)
    if not result.changed:
        app.say(result.message)
        return True
    _finish(app, current, result, targets, created=action == "create")
    return True


def handle_key(app, key):
    # Preserve configured replacements. These contextual shortcuts extend the
    # existing Home/unmark defaults; text entry and graph undo retain ownership.
    defaults = {"g": "home", "u": "unmark_all"}
    if key not in defaults or getattr(app, "keymap", {}).get(key) != defaults[key]:
        return False
    return run_action(app, "create" if key == "g" else "ungroup")
