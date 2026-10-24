"""Bounded protocol-provable dependencies from existing recorded observations.

MPI signatures reuse the existing collective auditor. Links express the
standard's supported completion semantics, not synchronized timestamps, a
deadlock diagnosis or a complete global critical path. Bare profiler numeric
correlations have no universal launch semantics and remain unresolved.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import product

from .workspace_analysis_common import (
    LOSS_KINDS,
    _data,
    issue,
    validate_rows,
)

MAX_MPI_EVENTS = 8192
MAX_MPI_MEMBERS = 256  # The existing selected PMPI adapter's supported bound.
MAX_LINKS = 8192


def _key(identity, data):
    def scalar(value):
        return str(value) if type(value) in (str, int) else None

    return (
        scalar(identity.get("job_id")),
        scalar(identity.get("attempt")),
        scalar(identity.get("cluster")),
        scalar(data.get("communicator")),
        scalar(data.get("generation")),
        data.get("sequence"),
    )


def _envelopes(rows, sources, unknown):
    events, groups, invalid = [], defaultdict(list), False
    for row in rows.values():
        if row.get("kind") != "collective" and row.get("kind") not in LOSS_KINDS:
            continue
        if len(events) == MAX_MPI_EVENTS:
            issue(
                unknown,
                "MPI audit event budget reached; narrow the selected range",
                kind="collective-budget",
            )
            return [], {}, True
        identity = sources.get(row["source_id"], {}).get("identity", {})
        data = _data(row)
        if not isinstance(identity, dict):
            issue(
                unknown,
                "Collective source identity must be an object",
                kind="malformed-collective-identity",
            )
            invalid = True
            continue
        if row.get("kind") == "collective":
            if (
                data.get("communicator") != "MPI_COMM_WORLD"
                or data.get("intercommunicator", False) is not False
            ):
                issue(
                    unknown,
                    "The selected PMPI dependency bridge supports declared intracommunicator MPI_COMM_WORLD only",
                    kind="unsupported-collective-communicator",
                    evidence_ids=[row["id"]],
                )
                invalid = True
                continue
            members = data.get("members")
            if (
                not isinstance(members, (list, tuple))
                or not 1 <= len(members) <= MAX_MPI_MEMBERS
            ):
                issue(
                    unknown,
                    "Collective membership is missing or exceeds the supported 256-rank PMPI scope",
                    kind="unsupported-collective-membership",
                    evidence_ids=[row["id"]],
                )
                invalid = True
                continue
            rank = identity.get("rank")
            if rank is not None and str(rank) != str(data.get("rank")):
                issue(
                    unknown,
                    "Collective rank disagrees with its recorded process identity",
                    kind="conflicting-rank",
                    evidence_ids=[row["id"]],
                )
                invalid = True
                continue
            if any(
                identity.get(field) is None
                for field in ("job_id", "attempt", "cluster", "stream")
            ):
                issue(
                    unknown,
                    "Collective source lacks an exact job/attempt/cluster/stream identity",
                    kind="missing-collective-identity",
                    evidence_ids=[row["id"]],
                )
                invalid = True
                continue
            # Untrusted nested sequence values must not become dictionary keys.
            sequence = data.get("sequence")
            if type(sequence) is not int or not 0 <= sequence < 1 << 63:
                issue(
                    unknown,
                    "Collective sequence must be a nonnegative integer",
                    kind="malformed-collective",
                    evidence_ids=[row["id"]],
                )
                invalid = True
                continue
            groups[_key(identity, data)].append(row)
        events.append(
            dict(
                kind=row["kind"],
                data=data,
                thread=row.get("thread"),
                seq=row.get("first_seq"),
                mono_ns=row.get("raw_start_ns", row["start_ns"]),
                **{
                    field: identity.get(field)
                    for field in (
                        "stream",
                        "job_id",
                        "attempt",
                        "cluster",
                        "node",
                        "boot_id",
                        "pid",
                        "process_start",
                    )
                },
            )
        )
    return events, groups, invalid


def _phases(finding, observed, unknown):
    grouped = defaultdict(list)
    for row in observed:
        data = _data(row)
        rank, phase = data.get("rank"), data.get("phase")
        if type(rank) is not int or phase not in {"enter", "complete"}:
            return None
        grouped[rank, phase].append(row)
    entries, completions = [], []
    for rank in finding["members"]:
        start, end = grouped[rank, "enter"], grouped[rank, "complete"]
        if len(start) != 1 or len(end) != 1:
            issue(
                unknown,
                "Collective phase lifetime is missing or repeated; no ambiguous call pairing",
                kind="ambiguous-collective-phase",
                evidence_ids=[row["id"] for row in (start + end)[:16]],
            )
            return None
        a, b = start[0], end[0]
        if (
            a["source_id"] != b["source_id"]
            or a.get("thread") != b.get("thread")
            or type(a.get("first_seq")) is not int
            or type(b.get("first_seq")) is not int
            or a["first_seq"] >= b["first_seq"]
            or a.get("raw_start_ns", a["start_ns"])
            > b.get("raw_start_ns", b["start_ns"])
        ):
            issue(
                unknown,
                "Collective completion does not match one ordered process/thread call lifetime",
                kind="conflicting-collective-lifetime",
                evidence_ids=[a["id"], b["id"]],
            )
            return None
        entries.append(a)
        completions.append(b)
    return entries, completions


def _pairs(finding, entries, completions, unknown):
    operation = finding["operation"]
    rank_evidence = finding["rank_evidence"]
    if operation == "BARRIER":
        return (
            product(entries, completions),
            len(entries) * len(completions),
            (
                "MPI Barrier: every declared member enters before any successful return; matched communicator/generation/sequence audited"
            ),
        )
    positive = all(
        type(row.get("count")) is int and row["count"] > 0 for row in rank_evidence
    )
    if (
        operation == "ALLREDUCE"
        and positive
        and all(row.get("reduction") == "sum" for row in rank_evidence)
    ):
        return (
            product(entries, completions),
            len(entries) * len(completions),
            (
                "MPI Allreduce SUM with positive supported scalar count: each declared input precedes each successful result return; not a barrier-after-return"
            ),
        )
    if operation == "BCAST" and positive:
        root = rank_evidence[0].get("root")
        root_entry = next(
            (row for row in entries if _data(row).get("rank") == root), None
        )
        if root_entry is not None:
            # Receiver entry does not precede the root's return in general.
            own = list(zip(entries, completions))
            receiver = [
                (root_entry, end)
                for end in completions
                if _data(end).get("rank") != root
            ]
            return (
                iter(own + receiver),
                len(own) + len(receiver),
                (
                    "MPI Bcast with positive supported scalar count: root input entry precedes receiver result return; each local entry precedes its own return; no implicit barrier"
                ),
            )
    issue(
        unknown,
        "Only local entry/return ordering is established for this zero-count or unsupported collective data-dependency case",
        kind="unsupported-collective-data-dependency",
        evidence_ids=[row["id"] for row in entries[:16]],
    )
    return (
        iter(zip(entries, completions)),
        len(entries),
        (
            "Matched declared MPI local call entry precedes its own successful return; no cross-rank data dependency inferred"
        ),
    )


def _collectives(query, rows, sources, unknown, limit):
    from .analysis import audit_collectives

    events, groups, invalid = _envelopes(rows, sources, unknown)
    if not events:
        return [], 0, {"status": "unavailable" if invalid else "empty", "groups": 0}
    audited = audit_collectives(events)
    summary = {
        "status": audited["status"],
        "groups": len(audited["data"]["collectives"]),
        "qualified_groups": 0,
        "warnings": audited["warnings"][:16],
        "scope": "Audited declared collective signatures in this bounded query; sequence gaps can reflect filtered non-collective events",
    }
    if (
        invalid
        or query.get("truncated")
        or any(row.get("kind") in LOSS_KINDS for row in rows.values())
    ):
        issue(
            unknown,
            "Incomplete/lost/malformed selected evidence prevents certifying collective dependency groups; narrow the query or recover the missing source",
            kind="partial-collective-evidence",
        )
        return [], 0, summary
    links, omitted = [], 0
    for finding in audited["data"]["collectives"]:
        key = tuple(
            finding[field]
            for field in (
                "job_id",
                "attempt",
                "cluster",
                "communicator",
                "generation",
                "sequence",
            )
        )
        observed = groups.get(key, [])
        if finding["status"] != "MATCH":
            issue(
                unknown,
                "Collective audit is "
                + finding["status"]
                + ": "
                + (
                    "; ".join(finding["reasons"])
                    or "member entry/completion evidence is incomplete"
                ),
                kind="collective-audit",
                evidence_ids=[row["id"] for row in observed[:16]],
            )
            continue
        phases = _phases(finding, observed, unknown)
        if phases is None:
            continue
        pairs, count, provenance = _pairs(finding, *phases, unknown)
        summary["qualified_groups"] += 1
        available, emitted = max(0, limit - len(links)), 0
        for first, last in pairs:
            if emitted == available:
                break
            links.append(
                dict(
                    source_id=first["id"],
                    target_id=last["id"],
                    relation="precedes",
                    provenance=provenance,
                    evidence_ids=[first["id"], last["id"]],
                )
            )
            emitted += 1
        omitted += count - emitted
    return links, omitted, summary


def correlations(query, *, limit=4096, qualified_activity_ids=()):
    """Return only scoped, audited dependencies with existing actual endpoints."""
    if type(limit) is not int or not 1 <= limit <= MAX_LINKS:
        raise ValueError("Correlation link budget must be 1–8192")
    if (
        not isinstance(qualified_activity_ids, (set, frozenset, tuple, list))
        or len(qualified_activity_ids) > MAX_LINKS * 2
        or any(
            not isinstance(value, str) or not 0 < len(value) <= 1024
            for value in qualified_activity_ids
        )
    ):
        raise ValueError("Qualified activity endpoints must be a bounded ID collection")
    qualified_activity_ids = set(qualified_activity_ids)
    rows, sources = validate_rows(query)
    unknown = []
    links, omitted, audit = _collectives(query, rows, sources, unknown, limit)
    # Different profiler versions reuse bare numeric IDs for different domains,
    # API calls and lifetimes. A number alone is not a typed launch declaration.
    unqualified = []
    for row in rows.values():
        if (
            row["id"] not in qualified_activity_ids
            and row.get("kind")
            in {"host", "gpu_kernel", "gpu_copy", "gpu_sync", "gpu_synchronization"}
            and _data(row).get("correlation_id") is not None
        ):
            if len(unqualified) < 16:
                unqualified.append(row["id"])
    if unqualified:
        issue(
            unknown,
            "Producer correlation numbers are retained, but no versioned typed host/device launch mapping is supplied; use explicit flow endpoints or a validated producer-specific declaration. A matching number does not prove a launch or GPU queue delay.",
            kind="unqualified-activity-correlation",
            evidence_ids=unqualified,
        )
    if omitted:
        issue(
            unknown,
            "Collective dependency edge budget omitted "
            + str(omitted)
            + " links; narrow the group/range",
            kind="correlation-edge-budget",
        )
    return dict(
        schema="raven.trace-correlations/v1",
        links=links,
        unknown=unknown,
        omitted=omitted,
        audit=audit,
        scope="Supported protocol semantics over recorded observations only; no timestamp proximity, implicit synchronization or deadlock inference",
    )
