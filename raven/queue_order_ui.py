"""Turn a frozen header drop into a background, reviewed scheduler action."""
from __future__ import annotations


def prepare_drop(app, source, target, placement="before"):
    """source/target are TargetEvidence from the painted grouping frame."""
    from . import operations as O
    from .ui import operations as ops_ui
    from . import ops_queue_order
    from raven.jobs import group_domain as job_groups
    from raven.jobs import manual_groups as M
    from .table_tools import snapshot
    try:
        if (not isinstance(source, M.TargetEvidence) or not isinstance(target, M.TargetEvidence)
                or source.group_id == target.group_id or placement not in ("before", "after")
                or app.mode != "main" or app.tab != "jobs"):
            raise ValueError("Drop one group header before or after another group in Jobs Main")
        snap = snapshot(app, app.store.snapshot())
        index = job_groups.registry(app).ensure(snap)
        current = M.target_evidences(app, snap, (source.group_id, target.group_id), index=index, all_groups=True)
        if current.get(source.group_id) != source or current.get(target.group_id) != target:
            raise ValueError("The source or destination group changed; drag its current header again")
        # A launch can span live queue rows and accounting rows whose live
        # controller records were already purged. Only its queue members can
        # have a scheduler order; retain full group evidence above so a stale
        # membership gesture still fails before selecting those exact jobs.
        queued = {job_groups._value(job, "id") for job in snap.get("jobs", ())}
        source_ids = tuple(jid for jid in source.members if jid in queued)
        target_ids = tuple(jid for jid in target.members if jid in queued)
        if not source_ids or not target_ids:
            raise ValueError("Both groups need current queue members before their pending jobs can be reordered")
        expected = {jid: token for jid, token in (*source.tokens, *target.tokens) if jid in queued}
        if any(not token[1] for token in expected.values()):
            raise ValueError("Group jobs need known submission times before queue ordering")
        params = O.parameters("queue-order", dict(source_jobs=",".join(source_ids),
            target_jobs=",".join(target_ids), placement=placement))
        ops_ui._open(app, "queue-order")
        state = ops_ui.initialize(app)
        state["values"] = params
        ops_ui._reset(state, "report")
        archived = len(source.members) + len(target.members) - len(source_ids) - len(target_ids)
        def publish(result):
            if archived:
                result.setdefault("warnings", []).append(
                    f"{archived} archived or departed group members are outside the current scheduler queue and are unchanged.")
            state["result"] = result
            app.say(result["summary"])
        ops_ui._background(app, lambda ctx: ops_queue_order.inspect(params, ctx, expected=expected), publish)
    except (ValueError, OSError) as exc:
        app.say("Queue order: " + str(exc))
        return False
    return True
