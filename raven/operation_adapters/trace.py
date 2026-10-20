"""Worker-only recording inspection, causal paths and collective validation."""

from __future__ import annotations

from pathlib import Path
import shlex
import re

from .. import operation_support as O
from ..tracing import protocol as T
from ..tracing import analysis as A


def _field(key, label, default="", required=False):
    return {"key": key, "label": label, "default": default, "required": required}


_TRACE_FIELDS = [
    _field("path", "Trace manifest or project directory", required=True),
    _field("job_id", "Expected job ID"),
    _field("attempt", "Expected attempt identity"),
]


class MissingTraceAttempt(ValueError):
    """A valid project index contains no currently available matching stream."""


SPECIFICATIONS = [
    {
        "key": "trace-inspect",
        "title": "Trace evidence",
        "group": "Trace",
        "proposal": "V6-T01",
        "summary": "Read a checked journal prefix and reconstruct recorded Python/cooperative stacks.",
        "fields": _TRACE_FIELDS,
    },
    {
        "key": "causal-delay",
        "title": "Causal delay explorer",
        "group": "Trace",
        "proposal": "V6-T02",
        "summary": "Explain measured span dependencies; missing edges and clocks remain unknown.",
        "fields": _TRACE_FIELDS,
    },
    {
        "key": "collective-audit",
        "title": "Collective protocol auditor",
        "group": "Trace",
        "proposal": "V6-T03",
        "summary": "Compare declared collective operations across ranks; missing evidence is not a deadlock.",
        "fields": _TRACE_FIELDS,
    },
    {
        "key": "trace-capture",
        "title": "Prepare trace launch",
        "group": "Trace",
        "proposal": "V6-T01",
        "summary": "Build an opt-in launch command; never attach, stop, or modify a running job.",
        "fields": [
            _field("project", "Existing project directory", required=True),
            _field("script", "Python script or native executable", required=True),
            _field("adapter", "Adapter: python, native or mpi", "python"),
            _field("job_id", "Job ID", "local"),
            _field("attempt", "Exact submit/start or run identity", required=True),
            _field("max_mib", "Storage budget MiB", "64"),
            _field("mpi_library", "Compiled PMPI library (mpi adapter)"),
            _field("mpi_generation", "Shared communicator generation (mpi adapter)"),
        ],
    },
    {
        "key": "trace-snapshot",
        "title": "Capture current Python stacks",
        "group": "Trace",
        "proposal": "V6-T01",
        "summary": "Review a nonblocking py-spy snapshot of an allocation-verified local process.",
        "fields": [
            _field("job_id", "Running job ID", required=True),
            _field("pid", "Local job process PID", required=True),
            _field("project", "Existing project directory", required=True),
        ],
    },
]


def _value(params, key, required=True):
    value = params.get(key, "")
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError(f"Invalid {key}")
    if required and not value.strip():
        raise ValueError(f"Set {key}")
    return value.strip()


def run(feature, params, ctx):
    O.checkpoint(ctx)
    O.local_only(ctx)
    if feature == "trace-snapshot":
        from ..tracing.snapshot import prepare

        return prepare(params, ctx)
    if feature == "trace-capture":
        project, script = (
            Path(_value(params, "project")).absolute(),
            Path(_value(params, "script")).absolute(),
        )
        if not project.is_dir() or not script.is_file():
            raise ValueError(
                "Capture preparation requires an existing project and program"
            )
        adapter = _value(params, "adapter", False) or "python"
        if adapter not in {"python", "native", "mpi"}:
            raise ValueError("Supported launch adapters are python, native and mpi")
        budget = int(_value(params, "max_mib", False) or "64")
        if not 1 <= budget <= 65536:
            raise ValueError("Storage budget must be 1–65536 MiB")
        attempt = _value(params, "attempt")
        argv = [
            "raven",
            "trace",
            "run" if adapter == "python" else "native",
            "--project",
            str(project),
            "--job-id",
            _value(params, "job_id", False) or str(ctx.selected or "local"),
            "--attempt",
            attempt,
            "--max-mib",
            str(budget),
            "--",
        ]
        if adapter == "mpi":
            library = Path(_value(params, "mpi_library")).absolute()
            generation = _value(params, "mpi_generation")
            if not library.is_file() or not re.fullmatch(
                r"[A-Za-z0-9_.-]{1,128}", generation
            ):
                raise ValueError(
                    "MPI capture needs a compiled library and a shared safe communicator generation"
                )
            argv += [
                "env",
                "LD_PRELOAD=" + str(library),
                "RAVEN_MPI_COMM_GENERATION=" + generation,
            ]
        argv.append(str(script))
        rows = [
            "Launch inside the existing job allocation:",
            shlex.join(argv),
            "No command was executed. Copy this into the batch launch script.",
            "Trace recording continues while the Raven UI is closed.",
            "Recorders run within the allocation; account for their CPU, memory and storage.",
        ]
        warnings = [
            "No universal lossless guarantee: only declared instrumented events are covered; uncommitted crash tails remain unknown.",
            "The native adapter requires explicit SDK calls. Python monitoring requires CPython 3.12+; native extensions and GPU instructions are not covered.",
            "For multiple nodes launch a recorder with each application process; an inherited native socket does not cross srun/SSH automatically.",
        ]
        if adapter == "mpi":
            warnings.append(
                "PMPI coverage: C WORLD collectives, at most 256 ranks, Barrier/Bcast/scalar Allreduce MPI_SUM. No MPI_THREAD_MULTIPLE, Fortran, nonblocking or device coverage. Compile against the application's exact MPI runtime."
            )
        return O.report(
            feature,
            "Prepared cooperative trace launch",
            rows=rows,
            warnings=warnings,
            data={
                "argv": argv,
                "adapter": adapter,
                "attempt": attempt,
                "max_mib": budget,
            },
        )
    if feature not in {"trace-inspect", "causal-delay", "collective-audit"}:
        raise ValueError("Unknown trace operation")
    return analysis_report(feature, collect_trace(params, ctx), ctx)


def collect_trace(params, ctx):
    """Acquire one bounded checked prefix for reuse by specialized analyzers."""
    O.checkpoint(ctx)
    O.local_only(ctx)
    path = Path(_value(params, "path")).absolute()
    expected_job = _value(params, "job_id", False) or str(ctx.selected or "")
    expected_attempt = _value(params, "attempt", False)
    is_project = path.is_dir() and (path / ".raven/traces/index.json").exists()
    if is_project:
        if not expected_job or not expected_attempt:
            raise ValueError(
                "Multi-stream analysis requires an explicit job ID and exact attempt"
            )
        paths = T.discover_trace(path, expected_job, expected_attempt)
        if not paths:
            raise MissingTraceAttempt(
                "No registered trace streams are available for this exact attempt"
            )
        if len(paths) > 256:
            raise ValueError("Choose an attempt with 1–256 registered streams")
    else:
        paths = [path]
    snapshots, events, guards, remaining_events, remaining_bytes = (
        [],
        [],
        [],
        20000,
        8 << 20,
    )
    for source in paths:
        O.checkpoint(ctx)
        if remaining_events < 1 or remaining_bytes < 1024:
            break
        current = T.read_trace(
            source,
            max_events=remaining_events,
            max_bytes=remaining_bytes,
            cancel=ctx.cancel,
        )
        snapshots.append(current)
        manifest_path = (
            Path(source) / "manifest.json" if Path(source).is_dir() else Path(source)
        )
        guards.append(
            {
                "format": "trace-prefix",
                "path": str(manifest_path),
                "identity": current["identity"],
                "sha256": current["sha256"],
                "read_seq": current["read_seq"],
                "complete": current["complete"],
                "coverage": current["covered"],
                "dropped": current["manifest"].get("dropped", 0),
                "producer_warnings": current["manifest"].get("warnings", []),
            }
        )
        events += current["events"]
        remaining_events -= len(current["events"])
        remaining_bytes -= sum(len(T.encoded(event)) + 1 for event in current["events"])
    snapshot = snapshots[0]
    O.checkpoint(ctx)
    _identity = snapshot["identity"]
    if expected_job and any(
        item["identity"]["job_id"] != expected_job for item in snapshots
    ):
        raise ValueError("Trace job identity does not match the selected job")
    if expected_attempt and any(
        item["identity"]["attempt"] != expected_attempt for item in snapshots
    ):
        raise ValueError("Trace attempt identity does not match")
    known_cluster = ctx.scope.get("cluster")
    if known_cluster and any(
        item["identity"]["cluster"] != known_cluster for item in snapshots
    ):
        raise ValueError("Trace cluster does not match the current connection")
    return {
        "snapshots": snapshots,
        "events": events,
        "guards": guards,
        "paths": list(map(str, paths)),
        "expected_job": expected_job,
        "expected_attempt": expected_attempt,
        "project": str(path) if is_project else None,
    }


def verify_prefixes(guards, ctx):
    """Check the recorded prefix; appending beyond an open prefix is allowed.

    No journal is called complete because its sequence hash matches. Coverage,
    reported losses and closed-state completeness retain their own checks.
    """
    O.local_only(ctx)
    remaining_bytes = 8 << 20
    for guard in guards:
        O.checkpoint(ctx)
        if remaining_bytes < 1024:
            raise ValueError("Trace verification exceeded its bounded prefix budget")
        current = T.read_trace(
            guard["path"],
            max_events=max(1, guard["read_seq"]),
            max_bytes=remaining_bytes,
            cancel=ctx.cancel,
        )
        remaining_bytes -= sum(len(T.encoded(event)) + 1 for event in current["events"])
        if (
            current["identity"] != guard["identity"]
            or current["read_seq"] != guard["read_seq"]
            or current["sha256"] != guard["sha256"]
            or current["covered"] != guard["coverage"]
            or current["manifest"].get("dropped", 0) != guard["dropped"]
            or current["manifest"].get("warnings", []) != guard["producer_warnings"]
            or guard["complete"]
            and not current["complete"]
        ):
            raise ValueError(
                "Trace evidence changed within the checked prefix; inspect again"
            )
    O.checkpoint(ctx)


def analysis_report(feature, collection, ctx):
    """Render a captured prefix using one cached pure specialized analysis."""
    snapshots, events, paths = (
        collection["snapshots"],
        collection["events"],
        collection["paths"],
    )
    snapshot, identity = snapshots[0], snapshots[0]["identity"]
    expected_attempt = collection["expected_attempt"]
    O.checkpoint(ctx)
    warnings = list(dict.fromkeys(w for item in snapshots for w in item["warnings"]))
    if len(snapshots) < len(paths):
        warnings.append(
            "Multi-stream byte/event budget reached; some registered streams were not read"
        )
    if not expected_attempt:
        warnings.append(
            "Attempt was not independently matched; this is explicitly opened evidence, not an automatic job association"
        )
    if feature == "trace-inspect":
        return O.report(
            feature,
            "Recorded stack evidence",
            status="ok" if snapshot["complete"] else "warning",
            rows=snapshot["rows"],
            warnings=warnings,
            data={
                key: snapshot[key]
                for key in (
                    "identity",
                    "stacks",
                    "status",
                    "complete",
                    "covered",
                    "read_seq",
                    "sha256",
                )
            },
        )
    if feature not in {"causal-delay", "collective-audit"}:
        raise ValueError("Choose a supported trace analysis")
    from ..derived_results import RESULTS, ResultKey

    revisions = [
        {key: source[key] for key in ("identity", "sha256", "read_seq")}
        for source in collection["guards"]
    ]
    key = ResultKey.create(
        scope=ctx.scope,
        identity={"streams": [source["identity"] for source in revisions]},
        revision=O.digest(revisions),
        algorithm=feature,
        version="trace-analysis-1",
        settings={"events": 20000, "bytes": 8 << 20},
    )
    result = RESULTS.get_or_compute(
        key,
        lambda: (
            A.analyze_causality(events)
            if feature == "causal-delay"
            else A.audit_collectives(events)
        ),
        cancel=ctx.cancel,
    )
    warnings += result["warnings"]
    if any(not item["caught_up"] or item["evicted"] for item in snapshots):
        warnings.append(
            "Analysis uses a bounded prefix/view; do not infer absent spans or ranks from it"
        )
    return O.report(
        feature,
        result["title"],
        status="warning"
        if warnings or result["status"] not in {"COMPLETE", "MATCH"}
        else "ok",
        rows=result["rows"],
        warnings=warnings,
        data={
            "identity": identity,
            "analysis": result["data"],
            "analysis_status": result["status"],
            "source_complete": len(snapshots) == len(paths)
            and all(item["complete"] for item in snapshots),
            "sources": [
                {"identity": item["identity"], "sha256": item["sha256"]}
                for item in snapshots
            ],
            "checked_prefixes": collection["guards"],
        },
    )


def apply(feature, plan, ctx):
    if feature == "trace-snapshot":
        from ..tracing.snapshot import apply as apply_snapshot

        return apply_snapshot(plan, ctx)
    raise ValueError(
        "Trace operations are read-only; run a prepared command explicitly inside the allocation"
    )


# Managed Raven entry points also protect direct adapter callers.
from ..operation_support import (
    managed_run as _managed_run,
    managed_apply as _managed_apply,
)

run = _managed_run(run)
apply = _managed_apply(apply)
