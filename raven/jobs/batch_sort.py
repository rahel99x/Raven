"""Stable queue batch ordering from cached observations; no scheduler I/O."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
import re

from raven.ui.list_preferences import BATCH_MODES as MODES, jobs_batch_mode as mode

__all__ = ["MODES", "mode", "heavy_publication", "heavy_job_id", "order"]


def heavy_publication(app):
    state = getattr(app, "raven_ui_state", {})
    return state.get("snapshot", {}) if isinstance(state, Mapping) else {}


def heavy_job_id(publication, snap):
    """Recognize the current verified worker allocation, never a job name.

    Cached owner/cluster/attempt information can disqualify a reused job ID.
    Missing optional queue fields do not invent an identity or run a query.
    """
    from raven.jobs.group_records import _text, _value

    if not isinstance(publication, Mapping) or publication.get("state") not in (
        "pending",
        "starting",
        "heavy",
        "draining",
        "reconciling",
    ):
        return None
    identity = publication.get("identity", {})
    if (
        not isinstance(identity, Mapping)
        or identity.get("verified") is not True
        or identity.get("site_kind") != "slurm"
    ):
        # A live salloc child can identify its queued job before a worker has
        # placement proof. This is a presentation hint only, never admission.
        observed = publication.get("allocation_observation", {})
        if (
            publication.get("ownership") != "dedicated-allocation"
            or not isinstance(observed, Mapping)
            or observed.get("source") != "owned-salloc-stderr"
            or not publication.get("request_id")
            or type(publication.get("generation")) is not int
            or observed.get("request_id") != publication["request_id"]
            or observed.get("generation") != publication["generation"]
        ):
            return None
        identity = {**observed, "uid": observed.get("owner_uid")}
    jid = identity.get("allocation_id") or identity.get("job_id")
    if (
        not isinstance(jid, str)
        or not re.fullmatch(r"[0-9]{1,20}", jid)
        or publication.get("job_id") != jid
    ):
        return None
    record = next(
        (record for record in snap.get("jobs", ()) if _value(record, "id") == jid), None
    )
    if record is None:
        return None
    details = snap.get("details", {}).get(jid, {})
    details = details if isinstance(details, Mapping) else {}
    for expected, field, scheduler in (
        ("cluster_id", "cluster", "Cluster"),
        ("submit_time", "submit", "SubmitTime"),
        ("start_time", "start", "StartTime"),
    ):
        # A queue snapshot may precede the worker's admission. Pending start
        # values can be estimates, and scheduler unknown tokens are not times.
        if field == "start" and str(_value(record, "state")).upper() in (
            "PENDING",
            "PD",
        ):
            continue
        actual = _text(details.get(scheduler)) or _text(_value(record, field))
        if actual and _text(identity.get(expected)) and actual != identity[expected]:
            return None
    owner = str(details.get("UserId", ""))
    uid = re.search(r"\(([0-9]+)\)$", owner) or re.fullmatch(r"([0-9]+)", owner)
    if uid and identity.get("uid") is not None and int(uid[1]) != identity["uid"]:
        return None
    return jid


def order(app, snap, records, index=None, *, observed=None, sort_mode=None):
    """Keep batches contiguous, retaining the caller's order within each one.

    Counts come from the unfiltered queue, before collapse. A compressed Slurm
    array row counts as one observed record. Ties keep the existing column sort.
    """
    from raven.jobs.group_records import _value

    membership = index.by_job if index is not None else {}
    heavy = heavy_job_id(heavy_publication(app), snap)

    def key(record):
        jid = _value(record, "id")
        return (
            ("job", jid)
            if jid == heavy or jid not in membership
            else ("group", membership[jid])
        )

    blocks = defaultdict(list)
    for record in records:
        blocks[key(record)].append(record)
    counts = {}
    seen = set()
    for record in snap.get("jobs", ()) if observed is None else observed:
        jid = _value(record, "id")
        if jid not in seen:
            count = counts.setdefault(key(record), [0, 0])
            state = str(_value(record, "state")).split(" ", 1)[0].rstrip("+").upper()
            count[0] += state in (
                "RUNNING",
                "R",
                "COMPLETING",
                "CG",
                "CONFIGURING",
                "CF",
                "RESIZING",
                "SIGNALING",
                "STAGE_OUT",
            )
            count[1] += 1
            seen.add(jid)
    descending = (mode(app) if sort_mode is None else sort_mode) == "max"

    def rank(item):
        identifier, members = item
        # The queue can be absent in an explicit standalone projection.
        # Only those fallback blocks need a local status count.
        counted = counts.get(identifier)
        if counted is None:
            from raven.jobs.group_records import status_counts

            counted = (status_counts(members)["running"], len(members))
        active, total = counted
        return (
            0 if identifier == ("job", heavy) else 1,
            -active if descending else active,
            total,
        )

    return [
        record for _, members in sorted(blocks.items(), key=rank) for record in members
    ]
