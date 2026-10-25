"""Compose existing trace investigations without inventing execution evidence.

This module consumes the same bounded workspace queries as both trace viewers.
It neither samples processes nor interprets absent events as waiting or idleness.
"""

from __future__ import annotations

import copy
import hashlib
import time

from .. import evidence as E
from ..tracing import workspace_analysis as investigation
from ..tracing import workspace_model as model
from ..tracing.workspace_analysis_common import partial, validate_rows


def trace_identity(value):
    """A recording identity is a binding, never authorization for native actions."""
    E.keys(value, {"cluster", "job_id", "attempt"}, {"run_id"}, "recording identity")
    return {key: E.text(item, key, 256) for key, item in value.items()}


def _cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Performance investigation cancelled")


def _bind(query, identity):
    declared = trace_identity(query.get("identity", {}))
    expected = trace_identity(identity) if identity is not None else declared
    if any(declared.get(key) != value for key, value in expected.items()):
        raise ValueError("Investigation query identifies a different recording attempt")
    rows, sources = validate_rows(query)
    for source in sources.values():
        supplied = source.get("identity", {})
        if not isinstance(supplied, dict) or any(
            supplied.get(key) != value for key, value in expected.items()
        ):
            raise ValueError("Investigation source identifies a different recording attempt")
    if any(row["source_id"] not in sources for row in rows.values()):
        raise ValueError("Investigation event has no matching source descriptor")
    for row in rows.values():
        E.text(row.get("kind", "unknown"), "recorded activity kind", 128)
        if row.get("kind") == "scheduling" and not isinstance(
            row.get("data", {}).get("state"), str
        ):
            raise ValueError("Recorded scheduling states must be descriptive text")
    return expected, rows, sources


def analyze(query, *, identity=None, selected_id=None, capacity=None, cancel=None):
    """Return evidence-linked timing and waiting explanations for one attempt.

    Analysis is bounded by the workspace's 20,000 returned records. Durations
    use existing union/statistics implementations. Source clocks stay separate;
    the causal view alone applies declared, uncertainty-preserving calibration.
    """
    _cancel(cancel)
    query_digest = hashlib.sha256(E.canonical(query)).hexdigest()
    expected, rows, sources = _bind(query, identity)
    resources = model.resource_metrics(query, capacity=capacity)
    _cancel(cancel)
    functions = model.function_statistics(query, limit=100)
    hints = investigation.bottleneck_hints(query)
    counters = investigation.counter_lens(query)
    flows = model.explicit_flows(query, limit=2000)
    elapsed = {}
    counts = {}
    for row in rows.values():
        _cancel(cancel)
        clock = row["clock_id"]
        first = row["start_ns"]
        last = row.get("end_ns", first)
        bound = elapsed.setdefault(clock, [first, last])
        bound[0], bound[1] = min(bound[0], first), max(bound[1], last)
        kind = E.text(row.get("kind", "unknown"), "recorded activity kind", 128)
        counts[kind] = counts.get(kind, 0) + 1
    measured_wait = [
        value
        for value in resources["metrics"]
        if value.get("state") in {"blocked", "runnable", "waiting", "off-cpu"}
    ]
    cpu = [value for value in resources["metrics"] if value["name"] == "logical_cpu_on_cpu_ns"]
    devices = [value for value in resources["metrics"] if value["name"] == "gpu_activity_busy_ns"]
    limitations = [
        "Call intervals measure elapsed time, including unmeasured waits and instrumentation; they do not measure CPU execution.",
        "Blocked/runnable durations require explicit scheduling state evidence. Samples and time between calls do not establish waiting.",
        "Per-thread scheduling states may overlap in malformed producer evidence; state unions are separate observations and must not be added as a partition.",
        "Device activity describes reported kernel/copy intervals, not achieved throughput or occupancy.",
        "Each clock envelope covers returned observations only. It is not a whole-run makespan or a sum across nodes.",
    ]
    result = {
        "schema": "raven.performance-investigation/v1",
        "identity": expected,
        "partial": partial(query),
        "coverage": {
            "returned_events": len(rows),
            "sources": len(sources),
            "caught_up": query.get("caught_up"),
            "truncated": bool(query.get("truncated")),
            "read_errors": copy.deepcopy(query.get("discovery_errors", [])),
            "kinds": counts,
        },
        "clock_envelopes": [
            {
                "clock_id": clock,
                "start_ns": bound[0],
                "end_ns": bound[1],
                "elapsed_ns": bound[1] - bound[0],
            }
            for clock, bound in sorted(elapsed.items())
        ],
        "execution": {
            "available": bool(cpu),
            "observations": cpu,
            "missing_reason": None
            if cpu
            else "No recorded on-CPU scheduling slices are available.",
        },
        "waiting": {
            "available": bool(measured_wait),
            "observations": measured_wait,
            "missing_reason": None
            if measured_wait
            else "No explicit scheduling wait-state evidence is available; elapsed gaps remain unknown.",
        },
        "device_activity": {
            "available": bool(devices),
            "observations": devices,
            "missing_reason": None
            if devices
            else "No device activity was recorded; host API calls do not substitute for device execution.",
        },
        "resources": resources,
        "functions": functions,
        "counters": counters,
        "flows": flows,
        "hints": hints,
        "limitations": limitations,
        "evidence_digest": query_digest,
    }
    if selected_id is not None:
        E.text(selected_id, "selected event ID", 1024)
        if selected_id not in rows:
            raise ValueError("Selected event is outside this bounded recording query")
        _cancel(cancel)
        result["causal_slice"] = investigation.causal_slice(query, selected_id)
    _cancel(cancel)
    return result


def read_recordings(paths, *, identity, options=None, cancel=None, max_seconds=1.0):
    """Read retained recordings through the existing verified indexed reader.

    This bounded initial read can remain partial. It never waits for Slurm and
    never changes recording state. Further refreshes can reuse the cached index.
    """
    from ..tracing.workspace_session import WorkspaceSession

    expected = trace_identity(identity)
    E.finite(max_seconds, "initial recording read seconds", 0.001)
    if max_seconds > 5:
        raise ValueError("Initial recording read is limited to five seconds")
    if options is not None and not isinstance(options, dict):
        raise ValueError("Recording query filters must be an object")
    options = dict(options or {})
    allowed = {
        "start_ns",
        "end_ns",
        "clock_id",
        "source_ids",
        "thread",
        "node",
        "process",
        "logical_cpu",
        "device_id",
        "kind",
        "kinds",
        "task",
        "rank",
        "search",
        "limit",
    }
    if options.keys() - allowed:
        raise ValueError("Only bounded trace query filters are accepted")
    if "clock_id" in options:
        E.text(options["clock_id"], "selected recording clock", 1024)
    if {"start_ns", "end_ns"}.intersection(options) and not options.get("clock_id"):
        raise ValueError(
            "Time filters require one explicit recording clock; independent clocks cannot share an unqualified range"
        )
    if "limit" in options:
        E.integer(options["limit"], "recording query limit", 10000)
        if options["limit"] < 1:
            raise ValueError("Recording query limit must be positive")
    _cancel(cancel)
    session = WorkspaceSession(paths, expected=expected, light=True)
    try:
        deadline = time.monotonic() + max_seconds
        for _ in range(128):
            _cancel(cancel)
            result = session.refresh(
                {**options, "all_clocks": not bool(options.get("clock_id")), "mode": "explore"},
                light=True,
                cancel=cancel,
            )
            if result.get("caught_up") or time.monotonic() >= deadline:
                break
        # The shared all-clock query has its own aggregate bound. Preserve a
        # smaller caller output limit as well; independent clock IDs are ordered
        # lexically rather than pretending their timestamps share a timeline.
        limit = options.get("limit")
        events = [*result.get("intervals", []), *result.get("markers", [])]
        if limit is not None and len(events) > limit:
            selected = {
                row["id"]
                for row in sorted(
                    events, key=lambda row: (row["clock_id"], row["start_ns"], row["id"])
                )[:limit]
            }
            result["intervals"] = [
                row for row in result.get("intervals", []) if row["id"] in selected
            ]
            result["markers"] = [row for row in result.get("markers", []) if row["id"] in selected]
            result["truncated"] = True
            result["warnings"] = [
                *result.get("warnings", []),
                "Caller recording-query event limit reached; narrow the clock or range for more events.",
            ]
            # This projection was computed before enforcing the smaller limit.
            # Rebuild it from the returned evidence, preserving its semantics.
            result["workspace_projection"] = model.project(result, "execution")
        return result
    finally:
        session.close()
