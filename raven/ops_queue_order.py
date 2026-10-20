"""Reviewed, owner-scoped queue preference using Slurm's native top operation.

The controller implements this operation under its job lock. Raven neither
invents priorities nor rewrites dependencies. Resources and dependencies still
decide eligibility; a requested preference is never a promised start order.
"""
from __future__ import annotations

import re

from . import operations as O, ops_cluster as C

MAX_JOBS = 512
_JOB = re.compile(r"[1-9][0-9]{0,19}(?:_[0-9]{1,10})?\Z", re.ASCII)
SPECIFICATIONS = [dict(key="queue-order", title="Review own queue order", group="Cluster", proposal="QoL",
    summary="Move a launch before or after another launch within your own pending queue; review all affected jobs.",
    fields=[dict(key="source_jobs", label="Moved job IDs (comma separated)", required=True),
            dict(key="target_jobs", label="Destination job IDs (comma separated)", required=True),
            dict(key="placement", label="Placement", default="before", choices=["before", "after"])])]


def _ids(text):
    if not isinstance(text, str) or len(text) > 8192:
        raise ValueError("Use a bounded comma-separated list of exact job IDs")
    values = text.split(",")
    if not 1 <= len(values) <= MAX_JOBS or any(not _JOB.fullmatch(value) for value in values):
        raise ValueError("Queue ordering needs exact job IDs or numeric array tasks; ranges and heterogeneous jobs are unsupported")
    if len(set(values)) != len(values):
        raise ValueError("Duplicate job IDs cannot define a queue order")
    return tuple(values)


def _configuration(ctx):
    raw = O.command(ctx, ["scontrol", "show", "config"], limit=1 << 20)
    fields = {}
    for line in raw.splitlines():
        match = re.fullmatch(r"\s*(SchedulerParameters|PriorityType|ClusterName)\s*=\s*(.*?)\s*", line)
        if match:
            if match[1] in fields:
                raise ValueError("Ambiguous scheduler configuration")
            fields[match[1]] = match[2]
    flags = {part.strip().lower() for part in fields.get("SchedulerParameters", "").split(",")}
    if "enable_user_top" not in flags:
        raise ValueError("This site has not enabled SchedulerParameters=enable_user_top; no queue order was changed")
    return fields


def _key(record):
    if record.get("ArrayJobId"):
        task = record.get("ArrayTaskId", "")
        if not task.isascii() or not task.isdigit():
            raise ValueError("Slurm returned an unexpanded array; queue ordering needs exact numeric tasks")
        return record["ArrayJobId"] + "_" + task
    return record.get("JobId", "")


def _bucket(record):
    return tuple(record.get(key, "") for key in ("UserId", "Partition", "Account", "QOS"))


def _pending(record):
    return record.get("JobState") == "PENDING"


def _priority(record):
    value = record.get("Priority", "")
    if not value.isascii() or not value.isdigit() or len(value) > 10:
        raise ValueError("A pending job has no valid scheduler priority")
    return int(value)


def _evidence(record):
    keys = ("JobId", "ArrayJobId", "ArrayTaskId", "SubmitTime", "UserId", "JobState", "Partition", "Account", "QOS",
            "Priority", "Nice", "Dependency", "Restarts", "RestartCnt", "EligibleTime", "BeginTime", "Reservation")
    return {key: record.get(key, "") for key in keys}


def _read_jobs(ctx, ids):
    if not ids or len(ids) > MAX_JOBS:
        raise ValueError("Queue review is limited to 512 exact jobs")
    records = []
    for jid in ids:
        O.checkpoint(ctx)
        raw = O.command(ctx, ["scontrol", "show", "job", "-o", jid], limit=1 << 20)
        observed = C.parse_records(raw, "JobId")
        if len(observed) != 1 or _key(observed[0]) != jid:
            raise ValueError("The scheduler did not return exactly the requested job")
        records.extend(observed)
    by_id = {}
    for record in records:
        key = _key(record)
        if key in by_id or key not in ids:
            raise ValueError("The scheduler returned ambiguous or unrelated job records")
        C._identity(record)
        C._owned(ctx, record)
        by_id[key] = record
    if set(by_id) != set(ids):
        raise ValueError("Jobs changed while the queue was read; prepare a new review")
    return by_id


def _capture(ctx, source, target, *, expected=None):
    config = _configuration(ctx)
    user = str(getattr(ctx.slurm, "user", ""))
    if not user or len(user) > 128 or not re.fullmatch(r"[A-Za-z0-9_.@-]+", user):
        raise ValueError("Queue ordering requires an explicit scheduler user")
    # Read the two explicit launches first. Native queue filters then bound
    # discovery to their scheduling bucket, not this user's unrelated work.
    by_id = _read_jobs(ctx, tuple(dict.fromkeys((*source, *target))))
    if expected:
        for jid, token in expected.items():
            if not token or len(token) < 3 or not token[1] or by_id.get(jid, {}).get("SubmitTime") != token[1]:
                raise ValueError("A dragged job attempt changed or has no submission time; select its current group again")
            if token[2] and config.get("ClusterName") != token[2]:
                raise ValueError("The dragged group belongs to a different or unverified cluster")
    moving = tuple(jid for jid in source if _pending(by_id[jid]))
    anchor = tuple(jid for jid in target if _pending(by_id[jid]))
    if not moving or not anchor:
        raise ValueError("Both groups need at least one pending job; running and completed jobs keep their state")
    bucket = _bucket(by_id[moving[0]])
    if any(not value or value.lower() in C._NULL for value in bucket) or "," in bucket[1]:
        raise ValueError("Queue ordering requires one explicit owner, partition, account and QOS")
    if any(_bucket(by_id[jid]) != bucket for jid in (*moving, *anchor)):
        raise ValueError("Groups must share the same owner, single partition, account and QOS")
    if any(_priority(by_id[jid]) <= 1 for jid in (*moving, *anchor)):
        raise ValueError("Held or priority-one jobs cannot be reordered; their hold state is preserved")
    if any(not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", value) for value in bucket[1:]):
        raise ValueError("Queue ordering needs bounded unambiguous partition, account and QOS names")
    text = O.command(ctx, ["squeue", "--noheader", "--array", "--user=" + user,
                          "--partition=" + bucket[1], "--account=" + bucket[2], "--qos=" + bucket[3],
                          "--states=PENDING", "--format=%i"], limit=64 << 10)
    queued = tuple(line.strip() for line in text.splitlines() if line.strip())
    if len(queued) > MAX_JOBS or any(not _JOB.fullmatch(jid) for jid in queued):
        raise ValueError("This pending scheduling bucket exceeds 512 exact jobs or contains unsupported job IDs")
    if len(set(queued)) != len(queued) or not set((*moving, *anchor)) <= set(queued):
        raise ValueError("Jobs changed while the pending queue was read; prepare a new review")
    observed = _read_jobs(ctx, queued)
    for jid in (*moving, *anchor):
        if _evidence(observed[jid]) != _evidence(by_id[jid]):
            raise ValueError("A dragged job changed during inspection; prepare a new review")
    peers = {jid: record for jid, record in observed.items()
             if _pending(record) and _bucket(record) == bucket and _priority(record) > 1}
    from .table_sort import _natural
    current = tuple(sorted(peers, key=lambda jid: (-_priority(peers[jid]), peers[jid].get("SubmitTime", ""), _natural(jid))))
    return dict(configuration=config, bucket=list(bucket), current=list(current),
                # Pin every relevant field while keeping a 512-job review
                # small enough to render completely in the bounded workbench.
                records={jid: O.digest(_evidence(record)) for jid, record in peers.items()},
                source=list(moving), target=list(anchor),
                excluded=list(jid for jid in (*source, *target) if not _pending(by_id[jid])))


def inspect(params, ctx, *, expected=None):
    source, target = _ids(params["source_jobs"]), _ids(params["target_jobs"])
    placement = params.get("placement", "before")
    if placement not in ("before", "after") or set(source) & set(target):
        raise ValueError("Choose two distinct groups and a before/after placement")
    capture = _capture(ctx, source, target, expected=expected)
    selected = set(capture["source"])
    moving = [jid for jid in capture["current"] if jid in selected]
    remaining = [jid for jid in capture["current"] if jid not in selected]
    positions = [remaining.index(jid) for jid in capture["target"]]
    offset = min(positions) if placement == "before" else max(positions) + 1
    proposed = remaining[:offset] + moving + remaining[offset:]
    positions_by_id = {jid: index + 1 for index, jid in enumerate(capture["current"])}
    rows = [dict(job_id=jid, old_position=positions_by_id[jid], new_position=index + 1,
                 moved=jid in selected) for index, jid in enumerate(proposed)]
    warnings = ["This changes only your pending jobs in the same partition, account and QOS using Slurm's native nice adjustment.",
                "Dependencies, resources, reservations and site policy still control start order. A queue preference is not a start-time guarantee.",
                "A job may start or arrive between the final check and the controller action; Slurm owns that final decision."]
    if capture["excluded"]:
        warnings.append("Running or completed group members are excluded: " + ", ".join(capture["excluded"]))
    payload = dict(capture, proposed=proposed, argv=["scontrol", "top", ",".join(proposed)])
    plan = O.prepare_plan(ctx, "queue-order", params, payload) if proposed != capture["current"] else None
    return O.report("queue-order", f"Review preference for {len(proposed)} own pending jobs" if plan else "These groups already have that queue preference",
                    rows=rows, data={"scope": capture["bucket"], "order": proposed}, warnings=warnings, plan=plan)


def run(feature, params, ctx):
    return inspect(params, ctx)


def apply(feature, plan, ctx):
    payload = O.validate_plan(plan, ctx, "queue-order")
    params = O.parameters("queue-order", plan["params"])
    fresh = _capture(ctx, _ids(params["source_jobs"]), _ids(params["target_jobs"]))
    for key in ("configuration", "bucket", "current", "records", "source", "target", "excluded"):
        if payload.get(key) != fresh[key]:
            raise ValueError("The queue changed after review; inspect the current order before applying")
    proposed = payload.get("proposed")
    if (not isinstance(proposed, list) or len(proposed) != len(set(proposed))
            or set(proposed) != set(fresh["current"]) or payload.get("argv") != ["scontrol", "top", ",".join(proposed)]):
        raise ValueError("The reviewed queue order is invalid")
    output = O.command(ctx, payload["argv"], timeout=10)
    return O.report("queue-order", "Slurm accepted the own-job queue preference", data={"order": proposed, "output": output},
                    warnings=["Actual start order remains subject to dependencies, available resources and scheduler policy."])


# Managed Raven entry points also protect direct adapter callers.
from .operations import managed_run as _managed_run, managed_apply as _managed_apply
run = _managed_run(run)
apply = _managed_apply(apply)
