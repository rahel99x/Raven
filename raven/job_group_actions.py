"""Frozen, exact-job presentation actions shared by pointer menus and drops."""
from __future__ import annotations


def _origin(app, scope):
    browser = getattr(app, "history_browser_state", {})
    frame = browser.get("frame") or {}
    table = getattr(app, "table_state", {})
    tab = getattr(app, "tab", "")
    return (tab, getattr(app, "analytics_view", None) if tab == "analytics" else None,
            getattr(app, "research_view", None) if tab == "research" else None,
            (frame.get("dock"), frame.get("preference")) if scope.startswith("history:") else None,
            bool(table.get("groups")), tuple(table.get("collapsed", ())),
            getattr(app, "manual_job_groups_revision", 0))


def freeze(app, context, job_ids=None):
    """Capture source intent without querying a scheduler or copying its store."""
    if not context:
        return None
    ids = context.get("ids", ())
    available = set(ids)
    if job_ids is None:
        marks = getattr(app, "marks", ())
        targets = tuple(dict.fromkeys(jid for jid in ids if jid in marks))
        if not targets:
            chosen = context.get("selected")
            targets = (chosen,) if chosen in available else ()
    else:
        targets = tuple(dict.fromkeys(job_ids))
    from .manual_job_groups import MAX_IDENTITIES
    if (not targets or len(targets) > MAX_IDENTITIES
            or any(not isinstance(jid, str) or jid not in available for jid in targets)):
        return None
    tokens = context.get("tokens", {})
    scope = context.get("scope", "")
    frozen = {"scope": scope, "ids": context.get("pane_ids", ids), "selected": context.get("selected"),
            "targets": targets, "tokens": {jid: tokens.get(jid) for jid in targets},
            "origin": _origin(app, scope)}
    if context.get("source_group") is not None:
        frozen["source_group"] = context["source_group"]
    return frozen


def validate(app, context):
    """Revalidate frozen source attempts once at an explicit action boundary."""
    if (not context or getattr(app, "mode", "main") != "main"
            or context.get("origin") != _origin(app, context.get("scope", ""))):
        app.say("The job view changed; select the current jobs again")
        return None
    from .table_tools import snapshot
    from .manual_job_groups import selection_tokens
    snap = snapshot(app, app.store.snapshot())
    ids = context.get("targets", ())
    observed = selection_tokens(snap, ids)
    published = context.get("tokens", {})
    if not ids or any(published.get(jid) is None or observed.get(jid) != published[jid] for jid in ids):
        app.say("Selected jobs changed; select the current rows again before grouping")
        return None
    expected = context.get("source_group")
    if expected is not None:
        from .job_groups import registry
        if registry(app).ensure(snap).groups.get(expected.id) != expected:
            app.say("The selected group changed; select its current header again")
            return None
    return snap


def execute(app, context, action, group_id=None, expected=None):
    """Apply one atomic membership change; stale gestures never choose new jobs."""
    snap = validate(app, context)
    if snap is None:
        return False
    from . import job_groups, manual_job_groups as manual
    registry = job_groups.registry(app)
    index = registry.ensure(snap)
    targets = context["targets"]
    if action == "create":
        result = manual.create(app, snap, targets)
    elif action == "add":
        if expected is None:
            app.say("The destination group changed; select its current row again")
            return False
        result = manual.add(app, snap, group_id, targets, expected=expected)
    elif action == "ungroup":
        groups, members = [], []
        for jid in targets:
            group = index.for_job(jid)
            if group is None:
                continue
            if (not context.get("source_group") and app.table_state.get("groups")
                    and registry.is_collapsed(group)):
                groups.append(group.id)
            else:
                members.append(jid)
        if groups:
            groups = list(dict.fromkeys(groups))
            current = manual.target_evidences(app, snap, groups, index=index, all_groups=True)
            frozen = context.get("ungroup_groups", {})
            if any(frozen.get(gid) is None or current.get(gid) != frozen[gid] for gid in groups):
                app.say("The selected group changed; select its current row again")
                return False
        result = manual.detach(app, snap, members, group_ids=groups)
    else:
        return False
    if not result.changed:
        app.say(result.message)
        return False
    from .manual_group_ui import _finish
    _finish(app, context, result, targets, created=action == "create")
    return True
