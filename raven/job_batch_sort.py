"""Stable queue batch ordering from cached observations; no scheduler I/O."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
import re

MODES = ("min", "max")


def mode(app):
    value = getattr(app, "table_state", {}).get("jobs_batch_sort", "min")
    return value if value in MODES else "min"


def heavy_publication(app):
    state = getattr(app, "raven_ui_state", {})
    return state.get("snapshot", {}) if isinstance(state, Mapping) else {}


def heavy_job_id(publication, snap):
    """Recognize the current verified worker allocation, never a job name.

    Cached owner/cluster/attempt information can disqualify a reused job ID.
    Missing optional queue fields do not invent an identity or run a query.
    """
    from .job_groups import _text, _value
    if not isinstance(publication, Mapping) or publication.get("state") not in ("pending", "starting", "heavy", "draining", "reconciling"):
        return None
    identity = publication.get("identity", {})
    if not isinstance(identity, Mapping) or identity.get("verified") is not True or identity.get("site_kind") != "slurm":
        # A live salloc child can identify its queued job before a worker has
        # placement proof. This is a presentation hint only, never admission.
        observed = publication.get("allocation_observation", {})
        if (publication.get("ownership") != "dedicated-allocation" or not isinstance(observed, Mapping)
                or observed.get("source") != "owned-salloc-stderr"
                or not publication.get("request_id") or type(publication.get("generation")) is not int
                or observed.get("request_id") != publication["request_id"]
                or observed.get("generation") != publication["generation"]):
            return None
        identity = {**observed, "uid": observed.get("owner_uid")}
    jid = identity.get("allocation_id") or identity.get("job_id")
    if not isinstance(jid, str) or not re.fullmatch(r"[0-9]{1,20}", jid) or publication.get("job_id") != jid:
        return None
    record = next((record for record in snap.get("jobs", ()) if _value(record, "id") == jid), None)
    if record is None:
        return None
    details = snap.get("details", {}).get(jid, {})
    details = details if isinstance(details, Mapping) else {}
    for expected, field, scheduler in (("cluster_id", "cluster", "Cluster"),
                                       ("submit_time", "submit", "SubmitTime"),
                                       ("start_time", "start", "StartTime")):
        # A queue snapshot may precede the worker's admission. Pending start
        # values can be estimates, and scheduler unknown tokens are not times.
        if field == "start" and str(_value(record, "state")).upper() in ("PENDING", "PD"):
            continue
        actual = _text(details.get(scheduler)) or _text(_value(record, field))
        if actual and _text(identity.get(expected)) and actual != identity[expected]:
            return None
    owner = str(details.get("UserId", ""))
    uid = re.search(r"\(([0-9]+)\)$", owner) or re.fullmatch(r"([0-9]+)", owner)
    if uid and identity.get("uid") is not None and int(uid[1]) != identity["uid"]:
        return None
    return jid


def order(app, snap, records, index=None):
    """Keep batches contiguous, retaining the caller's order within each one.

    Counts come from the unfiltered queue, before collapse. A compressed Slurm
    array row counts as one observed record. Ties keep the existing column sort.
    """
    from .job_groups import _value, status_counts
    membership = index.by_job if index is not None else {}
    heavy = heavy_job_id(heavy_publication(app), snap)

    def key(record):
        jid = _value(record, "id")
        return ("job", jid) if jid == heavy or jid not in membership else ("group", membership[jid])

    blocks = defaultdict(list)
    for record in records:
        blocks[key(record)].append(record)
    observed = defaultdict(list)
    seen = set()
    for record in snap.get("jobs", ()):
        jid = _value(record, "id")
        if jid not in seen:
            observed[key(record)].append(record)
            seen.add(jid)
    descending = mode(app) == "max"

    def rank(item):
        identifier, members = item
        counted = observed.get(identifier, members)
        active = status_counts(counted)["running"]
        return (0 if identifier == ("job", heavy) else 1,
                -active if descending else active, len(counted))

    return [record for _, members in sorted(blocks.items(), key=rank) for record in members]
