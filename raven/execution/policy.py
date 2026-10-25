"""Execution policy contracts and worker-client port, independent of operation adapters.

This module is deliberately cheap to import in the worker handshake. Scientific
implementations load only inside an admitted task child. Legacy pure libraries
remain usable outside a configured Raven runtime; this is an application policy,
not a sandbox for arbitrary programs run by the user.
"""

from __future__ import annotations

import hashlib
import json
import math
import time


SCHEMA = "raven.execution-contract/v1"
RESULT_SCHEMA = "raven.execution-result/v1"
MAX_INPUT = 1 << 20
MAX_RESULT = 512 << 10
DEFAULT_BUDGET = {
    "cpus": 1,
    "memory_mib": 256,
    "scratch_mib": 64,
    "input_bytes": MAX_INPUT,
    "output_bytes": MAX_RESULT,
    "wall_seconds": 60,
    "cpu_seconds": 30,
    "max_records": 100000,
}

# Explicit, fixed imports; an operation never names a Python module or callable.
_PURE = {
    "statistics": ("science_stats", "compare", ("document",)),
    "acceptance": ("science_stats", "acceptance", ("document", "results")),
    "numerical-stability": ("science_diagnostics", "numerical", ("document",)),
    "factorial-design": ("science_diagnostics", "factorial_plan", ("document",)),
    "factorial-analysis": ("science_diagnostics", "factorial", ("document",)),
    "regression-investigation": ("science_diagnostics", "regression", ("document",)),
    "recommendation": ("science_contracts", "evaluate", ("document",)),
    "investigation-plan": ("investigation_planner", "plan", ("document",)),
    "scientific-impact": ("scientific_lineage", "analyze", ("document",)),
    "causal-delay": ("tracing.analysis", "analyze_causality", ("events",)),
    "collective-audit": ("tracing.analysis", "audit_collectives", ("events",)),
    "execution-simulation": (
        "execution_simulator",
        "simulate",
        ("document", "scenario"),
    ),
    "diagnostic-recipe": (
        "diagnostic_recipes",
        "compile_recipe",
        ("document", "capabilities"),
    ),
    "information-query": (
        "diagnostic_recipes",
        "summarize_query",
        ("document", "query"),
    ),
    "scientific-field": ("science_fields", "inspect_field", ("document",)),
    "mcp-evidence-read": ("mcp_evidence", "read", ("document",)),
}


def _contract(
    operation,
    *,
    handler=True,
    target="allocated-compute-or-desktop",
    side_effects="none",
):
    return {
        "schema": SCHEMA,
        "operation": operation,
        "version": 1,
        "profile": "heavy",
        "targets": ["desktop", "allocated-compute"],
        "target": target,
        "budget": dict(DEFAULT_BUDGET),
        "network_bytes": 0,
        "concurrency": 1,
        "additional_job": False,
        "additional_gpu": False,
        "cancellation": "cooperative checkpoints and owned task-process termination",
        "checkpoint": "none; completed immutable results only",
        "retry": "explicit pure retry",
        "side_effects": side_effects,
        "result_schema": RESULT_SCHEMA,
        "coverage": "input-declared coverage; no missing records inferred complete",
        "acceptance": "finite bounded result with input/context digests and declared limits",
        "implemented_worker_adapter": handler,
    }


from ..distribution_profile import absent as _extension_absent

_PURE = {
    key: value
    for key, value in _PURE.items()
    if not _extension_absent("raven." + value[0])
}
OPERATIONS = {key: _contract(key) for key in _PURE}
OPERATIONS["mcp-evidence-read"]["budget"].update(
    input_bytes=128 << 20,
    output_bytes=128 << 10,
    wall_seconds=10,
    cpu_seconds=5,
    max_records=10000,
)
for _key in (
    "planning",
    "planning-command",
    "trace-file",
    "log-search",
    "log-search-files",
    "research-command",
    "scientific-inspection",
):
    OPERATIONS[_key] = _contract(_key)
OPERATIONS["trace-file"]["budget"]["input_bytes"] = 8 << 20
OPERATIONS["trace-file"]["budget"]["max_records"] = 20000
OPERATIONS["log-search-files"]["budget"]["input_bytes"] = 8 << 20
OPERATIONS["scientific-inspection"]["budget"]["input_bytes"] = 8 << 20
OPERATIONS["engineering-analysis"] = _contract("engineering-analysis")
OPERATIONS["engineering-analysis"]["budget"].update(
    input_bytes=2 << 20, output_bytes=4 << 20, wall_seconds=15, cpu_seconds=10
)

# These existing named inspections run unchanged in a resource-limited task
# child. They may read explicit artifacts, but cannot acquire a Slurm transport.
FILE_OPERATIONS = frozenset(
    {
        "environment",
        "reuse",
        "bottlenecks",
        "energy",
        "evidence",
        "research-portable",
        "qualification",
        "support-bundle",
        "investigation-workflow",
        "diagnostic-campaign",
        "minimize-reproducer",
        "regression-sweep",
        "workflow-engine",
        "staging",
        "adapter",
        "wasi-analysis",
        "checkpoint",
        "search",
        "packing",
        "dask",
        "heterogeneous",
        "incidents",
        "investigation-timeline",
        "evidence-query",
        "allocation-health",
        "process-profile",
        "telemetry-anomalies",
        "phase-alignment",
        "queue-model",
        "campaign-policy",
        "engineering-performance",
    }
)
from ..distribution_profile import missing_operation as _extension_operation_absent

FILE_OPERATIONS = frozenset(
    key for key in FILE_OPERATIONS if not _extension_operation_absent(key)
)
for _key in FILE_OPERATIONS:
    OPERATIONS[_key] = _contract(_key)
    OPERATIONS[_key]["budget"]["input_bytes"] = 8 << 20
    OPERATIONS[_key]["coverage"] = (
        "Explicit stable artifact snapshots; unavailable host mappings remain unavailable"
    )
PUBLISH_OPERATIONS = frozenset(
    {
        "research-portable",
        "support-bundle",
        "reuse",
        "execution-simulation",
        "log-export",
    }
)
DEFERRED_PATHS = {
    "clusters": "Live multi-cluster collection needs a governed shared source adapter.",
    "supervisor:start": "A detached collector needs its own governed source owner.",
    "site-telemetry:query": "Network telemetry needs its own request/response ledger.",
    "environment:probe_job_id": "Compute-host probes need a governed target adapter; artifact-only inspection works.",
    "environment:apply": "Environment publication needs a reviewed bounded worker artifact adapter.",
    "checkpoint/search/packing/dask/heterogeneous:apply": "Campaign submissions need a reviewed worker-to-scheduler handoff.",
    "diagnostic-campaign/minimize-reproducer/regression-sweep/investigation-workflow:apply": "External drivers need an owned, bounded execution adapter.",
    "diagnostic-campaign/minimize-reproducer/regression-sweep:status/cancel": "Driver control belongs to the original execution host.",
    "staging:apply": "Transfers need a reviewed worker I/O budget and destination handoff.",
    "adapter:apply": "Plugin execution needs an explicit declared adapter.",
    "trace recorder outside a verified job-local target": "Python/native capture needs fresh owned job/step and current process membership. Inspect reads a bounded prefix; register and SDK output remain Light.",
    "diagnostic/diagnostic-trials CLI": "External experiments need a reviewed owned execution target.",
    "report exec/passport/aggregate outside a verified job-local target": "These run inside a verified owned Slurm job/step. Report submit uses the governed scheduler ledger without requiring Heavy; bounded metadata reports remain Light.",
    "passport capture outside a verified job-local target": "Repository/input capture needs an explicit owned job-local execution target.",
    "terminal generated helpers": "Arbitrary generated shell commands cannot substitute for named worker operations; user input and local hooks remain available.",
    "large remote artifacts": "No implicit mapping between interface, SSH, and worker paths.",
    "absolute log line/time seek above 2 MiB": "Use byte/percent windows or admitted Heavy search.",
    "direct whole-file copy above 2 MiB": "Use the admitted Heavy log-bundle export.",
}
OPERATIONS["artifact-publish"] = _contract(
    "artifact-publish", side_effects="reviewed no-clobber artifact publication"
)
OPERATIONS["artifact-publish"]["retry"] = (
    "never automatic; reconcile receipt before a new review"
)
OPERATIONS["artifact-publish"]["budget"]["input_bytes"] = 8 << 20
OPERATIONS["evidence-query"]["budget"].update(input_bytes=128 << 20, scratch_mib=128)
OPERATIONS["evidence-index-update"] = _contract(
    "evidence-index-update",
    side_effects="reviewed transactional evidence index update or consistent no-clobber backup",
)
OPERATIONS["evidence-index-update"]["budget"].update(
    input_bytes=128 << 20, scratch_mib=128, max_records=200000
)
OPERATIONS["evidence-index-update"]["retry"] = (
    "never automatic; reconcile immutable capture or publication receipt before retry"
)
OPERATIONS["allocation-health-probe"] = _contract(
    "allocation-health-probe",
    side_effects="reviewed bounded memory/scratch probes in verified owned allocation and no-clobber receipt",
)
OPERATIONS["allocation-health-probe"]["budget"].update(
    wall_seconds=35, cpu_seconds=30, scratch_mib=16
)
OPERATIONS["allocation-health-probe"]["retry"] = (
    "never automatic; inspect retained receipt before preparing a new review"
)
for _health in ("allocation-health", "allocation-health-probe"):
    OPERATIONS[_health]["budget"].update(cpus=2, memory_mib=1024)
OPERATIONS["wasi-analysis-execute"] = _contract(
    "wasi-analysis-execute",
    side_effects="reviewed bounded pure-analysis WASI module and optional no-clobber result receipt",
)
OPERATIONS["wasi-analysis-execute"]["budget"].update(
    input_bytes=8 << 20, wall_seconds=35, cpu_seconds=30, scratch_mib=16
)
OPERATIONS["wasi-analysis-execute"]["retry"] = (
    "never automatic; inspect retained receipt before preparing a new review"
)
OPERATIONS["process-profile-capture"] = _contract(
    "process-profile-capture",
    side_effects="reviewed read-only owned local process sampling and no-clobber receipt",
)
OPERATIONS["process-profile-capture"]["budget"].update(
    wall_seconds=65, cpu_seconds=10, scratch_mib=4
)
OPERATIONS["process-profile-capture"]["retry"] = (
    "never automatic; inspect receipt and revalidate exact PID/start-time identity"
)
OPERATIONS["simulation-review"] = _contract("simulation-review")

# These operations retain their existing explicit review/identity path in Light.
# No computational entitlement is gained by submitting a job or changing mode.
LIGHT_OPERATIONS = frozenset(
    {
        "storage",
        "pending-edit",
        "array-throttle",
        "reservations",
        "licenses",
        "slurm-doctor",
        "batch-script",
        "rerun-job",
        "allocation-shell",
        "placement",
        "dependency-repair",
        "queue-order",
        "project-guide",
        "site-policy",
        "runtime-steering",
        "io-coordination",
        "trace-capture",
        "trace-snapshot",
        "supervisor",
        "clusters",
        "site-telemetry",
        "native-scheduler",
        "engineering-experiments",
        "engineering-qualification",
        "engineering-capsules",
        "engineering-labs",
        "engineering-provenance",
        "engineering-incidents",
        "engineering-optimization",
        "engineering-recovery",
        "engineering-diagnosis",
        "engineering-reliability",
    }
)
HEAVY_OPERATIONS = frozenset(
    {
        "environment",
        "acceptance",
        "statistics",
        "reuse",
        "checkpoint",
        "search",
        "packing",
        "dask",
        "heterogeneous",
        "supervisor",
        "staging",
        "workflow-engine",
        "bottlenecks",
        "clusters",
        "incidents",
        "energy",
        "investigation-timeline",
        "investigation-workflow",
        "recommendation",
        "investigation-plan",
        "scientific-impact",
        "numerical-stability",
        "factorial-design",
        "factorial-analysis",
        "regression-investigation",
        "diagnostic-campaign",
        "minimize-reproducer",
        "regression-sweep",
        "site-telemetry",
        "evidence",
        "adapter",
        "wasi-analysis",
        "research-portable",
        "qualification",
        "support-bundle",
        "execution-simulation",
        "diagnostic-recipe",
        "information-query",
        "scientific-field",
        "trace-inspect",
        "causal-delay",
        "collective-audit",
        "evidence-query",
        "allocation-health",
        "process-profile",
        "telemetry-anomalies",
        "phase-alignment",
    }
)

# Only bounded existing source readers and centrally gated dispatchers may use
# the Light background lane. Unknown plugins/callables are not admitted.
LIGHT_BACKGROUND_MODULES = frozenset(
    {
        "activity_ui",
        "job_logs.buffer",
        "controller",
        "job_logs.catalog",
        "execution_ui",
        "project_ui",
        "session_tools",
        "research_commands",
        "ui.operations",
        "terminal.workflows",
        "shell_checks_ui",
        "terminal.fusion",
        "telemetry_ui",
        "jobs.progress",
        "views",
        "array_manifest_ui",
        "planning_commands",
        "tracing.ui",
        "tracing.browser",
        "quick_advisor",
        "analysis_ui",
        "job_logs.tools",
        "job_logs.workbench",
        "job_logs.history_export",
        "data_export_ui",
        "auto_export_ui",
        "setup_ui",
        "recordings.settings",
        "onboarding_ui",
        "launching.wizard_ui",
        "launching.wizard",
        "connect_data_ui",
        "connect_data",
        "run_home_ui",
        "run_home",
    }
)
HEAVY_BACKGROUND_MODULES = frozenset(
    {
        "job_logs.tools",
        "job_logs.workbench",
        "quick_advisor",
        "analysis_ui",
        "job_logs.history_export",
    }
)
_PROCESS_ROUTER = None


from ..source_budget import (
    AdmissionError,
    source_limit as source_limit,
    source_read as source_read,
    _SOURCE_BUDGET,
    _TASK_ROUTER,
)


def _json(value, limit, label, max_records=None):
    """Bound depth and element count before serializing possibly nested data."""
    from ..raven_protocol import PAYLOAD_DEPTH, PAYLOAD_RECORDS

    max_records = PAYLOAD_RECORDS if max_records is None else max_records
    pending, count = [(value, 0)], 0
    while pending:
        current, depth = pending.pop()
        count += 1
        if depth > PAYLOAD_DEPTH or count > max_records:
            raise ValueError(label + " exceeds the depth or record budget")
        if isinstance(current, dict):
            if any(not isinstance(key, str) or len(key) > 4096 for key in current):
                raise ValueError(label + " needs bounded string keys")
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend((item, depth + 1) for item in current)
        elif not (current is None or isinstance(current, (str, int, float, bool))):
            raise ValueError(label + " must be JSON data")
        if isinstance(current, str) and len(current) > limit:
            raise ValueError(label + " exceeds its byte budget")
    try:
        raw = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ValueError(label + " must be finite JSON") from exc
    if len(raw) > limit:
        raise ValueError(label + " exceeds its byte budget")
    return raw


def budget_for(operation, supplied=None):
    if operation not in OPERATIONS:
        raise ValueError("Unknown worker operation: " + str(operation)[:80])
    defaults = OPERATIONS[operation]["budget"]
    supplied = {} if supplied is None else supplied
    if not isinstance(supplied, dict) or set(supplied) - set(defaults):
        raise ValueError("Unknown execution budget field")
    result = dict(defaults, **supplied)
    for key, value in result.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            or value > defaults[key]
            or key not in {"wall_seconds", "cpu_seconds"}
            and not isinstance(value, int)
        ):
            raise ValueError("Invalid or excessive execution budget: " + key)
    return result


class _Cancellation:
    def __init__(self, callback, deadline):
        self.callback, self.deadline = callback, deadline

    def is_set(self):
        return time.monotonic() >= self.deadline or bool(
            self.callback and self.callback()
        )

    def check(self):
        if self.is_set():
            raise AdmissionError("Execution cancelled or its deadline expired")


def deferred(capability, reason, *, supported=True):
    return {
        "schema": "raven.operation/v1",
        "feature": capability,
        "status": "deferred",
        "summary": reason,
        "rows": ["Request compute explicitly; no analysis ran on the interface host."],
        "warnings": [],
        "data": {
            "required_profile": "heavy",
            "worker_adapter": supported,
            "executed": False,
            "retry": "explicit request after compute is ready",
        },
    }


def policy_for(value=None):
    cfg = getattr(value, "cfg", value)
    cfg = getattr(cfg, "data", cfg)
    policy = cfg.get("raven", {}) if hasattr(cfg, "get") else {}
    return policy if isinstance(policy, dict) else {}


class ExecutionRouter:
    def __init__(self, policy=None, worker_provider=None):
        self.policy = policy or {}
        self.worker_provider = worker_provider
        self.last_deferred = None
        self.source_backend = None
        self.job_local_receipt = None

    @property
    def managed(self):
        return self.policy.get("managed") is True

    def worker(self):
        worker = self.worker_provider() if self.worker_provider else None
        return (
            worker if worker is not None and getattr(worker, "ready", False) else None
        )

    def require(self, capability, *, worker_operation=None):
        operation = worker_operation or capability
        if not self.managed:
            return None
        if operation not in OPERATIONS:
            self.last_deferred = deferred(
                capability,
                "This workflow needs a declared execution adapter; local fallback is disabled.",
                supported=False,
            )
        elif self.worker() is None:
            self.last_deferred = deferred(
                capability, "Heavy compute is not ready; the task is prepared/deferred."
            )
        else:
            return None
        return self.last_deferred

    def dispatch(self, operation, inputs, context=None, *, cancel=None, budget=None):
        unavailable = self.require(operation)
        if unavailable is not None:
            return unavailable
        if not self.managed:
            raise AdmissionError("Worker dispatch needs a managed execution context")
        worker = self.worker()
        if worker is None:
            return deferred(
                operation, "Compute became unavailable before task admission"
            )
        limits = budget_for(operation, budget)
        resources = getattr(worker, "resources", None)
        if resources:
            from ..raven_resources import task_limits

            share = task_limits({}, resources)
            limits = {
                key: min(value, share.get(key, value)) for key, value in limits.items()
            }
        inputs = json.loads(
            _json(inputs, limits["input_bytes"], "Task inputs", limits["max_records"])
        )
        context = json.loads(_json(context or {}, 64 << 10, "Task context"))
        identity = _json(getattr(worker, "identity", {}), 64 << 10, "Worker identity")
        task_id = worker.submit(operation, inputs, context or {}, budget=limits)
        deadline = time.monotonic() + limits["wall_seconds"] + 2
        while True:
            cancelled = (
                bool(cancel() if callable(cancel) else cancel.is_set())
                if cancel is not None
                else False
            )
            if cancelled or time.monotonic() >= deadline:
                worker.cancel(task_id)
                raise AdmissionError(
                    "Worker task cancelled or timed out; no local retry was started"
                )
            worker.poll()
            if (
                not getattr(worker, "ready", False)
                or _json(getattr(worker, "identity", {}), 64 << 10, "Worker identity")
                != identity
            ):
                raise AdmissionError(
                    "Execution placement changed; no local fallback was started"
                )
            state = getattr(worker, "tasks", {}).get(task_id, {})
            status = state.get("status")
            if status in {"completed", "complete", "done", "succeeded", "committed"}:
                result = state.get("result")
                ack = getattr(worker, "ack", None)
                try:
                    _json(result, limits["output_bytes"], "Worker result")
                    if (
                        not isinstance(result, dict)
                        or result.get("schema") != RESULT_SCHEMA
                        or result.get("operation") != operation
                    ):
                        raise AdmissionError(
                            "Worker result has the wrong execution contract"
                        )
                    if (
                        result.get("context_sha256")
                        != hashlib.sha256(
                            _json(context or {}, 64 << 10, "Task context")
                        ).hexdigest()
                    ):
                        raise AdmissionError(
                            "Worker result belongs to another execution identity"
                        )
                finally:
                    # A rejected committed result still occupies retention and
                    # admission until explicitly released by its sole consumer.
                    if ack is not None:
                        ack(task_id)
                return result
            if status in {
                "error",
                "failed",
                "cancelled",
                "expired",
                "rejected",
                "uncertain",
            }:
                raise AdmissionError(
                    "Worker task "
                    + status
                    + ": "
                    + str(state.get("error", "no result"))[:400]
                )
            manager = getattr(worker, "manager", None)
            wait = getattr(manager, "wait_for_activity", None)
            if callable(wait):
                wait(min(0.25, max(0.0, deadline - time.monotonic())))
            else:
                # Direct clients own a readiness selector; test/extension
                # adapters without one retain a finite cancellation check.
                poll = getattr(worker, "poll")
                try:
                    poll(timeout=min(0.25, max(0.0, deadline - time.monotonic())))
                except TypeError:
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def router_for(value=None):
    explicit = getattr(value, "execution", None) or getattr(
        value, "raven_execution", None
    )
    if isinstance(explicit, ExecutionRouter):
        return explicit
    policy = policy_for(value)
    task_router = _TASK_ROUTER.get()
    if policy.get("managed") is True:
        if task_router is not None and task_router.policy == policy:
            return task_router
        if _PROCESS_ROUTER is not None and _PROCESS_ROUTER.policy == policy:
            return _PROCESS_ROUTER
        return ExecutionRouter(policy)
    return task_router or _PROCESS_ROUTER or ExecutionRouter()


def configure_process(policy, manager=None):
    """Public launchers establish policy before invoking old CLI dispatchers."""
    global _PROCESS_ROUTER
    _PROCESS_ROUTER = ExecutionRouter(
        policy, (lambda: manager.worker) if manager else None
    )
    return _PROCESS_ROUTER


def process_router():
    return _PROCESS_ROUTER


def reset_process(previous=None):
    global _PROCESS_ROUTER
    _PROCESS_ROUTER = previous


def bind(app, manager=None):
    router = ExecutionRouter(
        policy_for(app), (lambda: manager.worker) if manager else None
    )
    router.source_backend = (
        getattr(manager, "collection_backend", None)
        if manager
        else getattr(getattr(app, "slurm", None), "b", None)
    )
    app.raven_execution = router
    if getattr(app, "research", None) is not None:
        app.research.execution = router
    return router


def background_allowed(router, function):
    if not router.managed:
        return True
    module = getattr(function, "__module__", "").removeprefix("raven.")
    if module in LIGHT_BACKGROUND_MODULES:
        return True
    router.last_deferred = deferred(
        "background:" + module,
        "This analysis needs an explicit worker task; arbitrary local callbacks are disabled.",
        supported=False,
    )
    return False


def bounded_background(router, function):
    if not router.managed:
        return function

    def run():
        token = _SOURCE_BUDGET.set({"remaining": 2 << 20, "files": 128})
        router_token = _TASK_ROUTER.set(router)
        try:
            return function()
        finally:
            _SOURCE_BUDGET.reset(token)
            _TASK_ROUTER.reset(router_token)

    return run


def context_data(ctx):
    selected = str(getattr(ctx, "selected", "") or "")
    records = [
        row
        for row in getattr(ctx, "jobs", ()) + getattr(ctx, "finished", ())
        if str(row.get("id", "")) == selected
    ]
    result = {
        "selected": selected,
        "scope": getattr(ctx, "scope", {}),
        "attempts": records[:2],
        "state_dir": str(getattr(ctx, "state_dir", "") or ""),
    }
    _json(result, 64 << 10, "Execution identity")
    return result
