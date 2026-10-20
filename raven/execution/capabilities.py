"""Read-only published capability catalog assembled above adapter contracts."""

from .policy import (
    DEFERRED_PATHS,
    HEAVY_BACKGROUND_MODULES,
    LIGHT_BACKGROUND_MODULES,
    LIGHT_OPERATIONS,
    MAX_RESULT,
    OPERATIONS,
    PUBLISH_OPERATIONS,
    _PURE,
    _contract,
)


def capability_ledger():
    """Every installed proposal and operation receives a versioned contract."""
    from ..capabilities import PROPOSALS
    from ..operations import catalog

    entries = []
    for specification in catalog():
        key = specification["key"]
        adapter = (
            "trace-file"
            if key in {"trace-inspect", "causal-delay", "collective-audit"}
            else "scientific-inspection"
            if key in _PURE
            else key
        )
        entry = _contract(key, handler=adapter in OPERATIONS)
        entry["worker_operation"] = adapter if adapter in OPERATIONS else None
        if key in LIGHT_OPERATIONS:
            entry.update(
                profile="light",
                target="bounded-source-or-reviewed-target",
                targets=["interface", "reviewed-job-target"],
                retry="never replay a mutation",
                side_effects="adapter review and exact-attempt revalidation required for apply",
                budget={
                    "input_bytes": 256 << 10,
                    "output_bytes": MAX_RESULT,
                    "wall_seconds": 120,
                    "concurrency": 2,
                },
            )
        entry["proposal_ids"] = [
            proposal.id for proposal in PROPOSALS if key in proposal.operations
        ]
        entry["availability"] = (
            "light-reviewed"
            if key in LIGHT_OPERATIONS
            else "worker-adapter"
            if adapter in OPERATIONS
            else "prepared/deferred; adapter unavailable"
        )
        if key == "clusters":
            entry["availability"] = (
                "prepared/deferred; governed live collector unavailable"
            )
        entry["apply"] = (
            "existing exact-attempt reviewed target"
            if key in LIGHT_OPERATIONS
            else "reviewed verified-allocation probes and receipt"
            if key == "allocation-health"
            else "reviewed transactional index update or export"
            if key == "evidence-query"
            else "reviewed worker publication"
            if key in PUBLISH_OPERATIONS
            else "read-only or deferred until target adapter is registered"
        )
        entries.append(entry)
    return {
        "schema": "raven.capability-ledger/v1",
        "operations": entries,
        "proposals": [
            {
                "id": proposal.id,
                "operations": list(proposal.operations),
                "area": proposal.area,
                "boundary": proposal.boundary,
            }
            for proposal in PROPOSALS
        ],
        "background": {
            "bounded_read_or_gated_dispatch": sorted(LIGHT_BACKGROUND_MODULES),
            "worker_required": sorted(HEAVY_BACKGROUND_MODULES),
            "unknown": "rejected",
        },
        "deferred_paths": dict(DEFERRED_PATHS),
        "worker_operations": [OPERATIONS[key] for key in sorted(OPERATIONS)],
        "limits": [
            "A registered contract is not site qualification.",
            "Legacy pure libraries outside a managed Raven runtime are not sandboxed.",
            "A Heavy worker never grants additional scheduler query or mutation entitlement.",
        ],
    }
