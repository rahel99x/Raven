"""Apply already resolved telemetry policies to existing session collectors.

No new worker, connection, cache identity or allocation is created. Histories,
in-flight readers and the shared admission ledger survive every policy change.
"""
from __future__ import annotations

from contextlib import nullcontext
import math


def apply(app, collection, intervals, research_seconds, *, retained_trace_floor=0.):
    """Retune a live session after configuration and site limits are resolved."""
    from .raven_collection import settings

    policy = settings(collection)
    intervals = dict(intervals)
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
           for value in intervals.values()):
        raise ValueError("Source intervals must be finite nonnegative seconds")
    if (type(research_seconds) not in (int, float) or not math.isfinite(research_seconds)
            or not .1 <= research_seconds <= 31536000):
        raise ValueError("Research refresh must be finite and between .1 and 31536000 seconds")
    if type(retained_trace_floor) not in (int, float) or not math.isfinite(retained_trace_floor) or retained_trace_floor < 0:
        raise ValueError("Retained telemetry floor must be finite nonnegative seconds")

    sampler = getattr(app, "sampler", None)
    research = getattr(app, "research", None)
    logs = getattr(app, "logs", None)
    manager = getattr(app, "raven_allocation_manager", None)
    candidates = (getattr(getattr(sampler, "slurm", None), "b", None),
                  getattr(manager, "collection_backend", None),
                  getattr(app, "files", None), getattr(sampler, "files", None),
                  getattr(research, "files", None), getattr(logs, "files", None))
    owners = {}
    for candidate in candidates:
        ledger = getattr(candidate, "ledger", None)
        if ledger is not None:
            owners[id(ledger)] = ledger
    with getattr(sampler, "_schedule_lock", nullcontext()):
        # Existing permits and failure deadlines remain owned by the ledger.
        # Updating it first makes every newly scheduled command see the policy.
        for ledger in owners.values():
            ledger.reconfigure(policy)
        if research is not None:
            research.set_refresh_interval(research_seconds, managed=True)
        catalog = getattr(logs, "catalog", None)
        if catalog is not None:
            with catalog.lock:
                catalog.ttl = float(policy["log_follow_seconds"])
        if sampler is not None:
            sampler.set_collection_settings(policy, intervals, retained_trace_floor=retained_trace_floor)
    return policy
