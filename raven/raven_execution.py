"""Raven's named execution contracts and managed-runtime admission.

This module is deliberately cheap to import in the worker handshake. Scientific
implementations load only inside an admitted task child. Legacy pure libraries
remain usable outside a configured Raven runtime; this is an application policy,
not a sandbox for arbitrary programs run by the user.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from contextvars import ContextVar
from contextlib import contextmanager
import hashlib
import importlib
import json
import math
import os
import threading
import time


SCHEMA = "raven.execution-contract/v1"
RESULT_SCHEMA = "raven.execution-result/v1"
MAX_INPUT = 1 << 20
MAX_RESULT = 512 << 10
DEFAULT_BUDGET = {"cpus": 1, "memory_mib": 256, "scratch_mib": 64,
                  "input_bytes": MAX_INPUT, "output_bytes": MAX_RESULT,
                  "wall_seconds": 60, "cpu_seconds": 30, "max_records": 100000}

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
    "causal-delay": ("trace_analysis", "analyze_causality", ("events",)),
    "collective-audit": ("trace_analysis", "audit_collectives", ("events",)),
    "execution-simulation": ("execution_simulator", "simulate", ("document", "scenario")),
    "diagnostic-recipe": ("diagnostic_recipes", "compile_recipe", ("document", "capabilities")),
    "information-query": ("diagnostic_recipes", "summarize_query", ("document", "query")),
    "scientific-field": ("science_fields", "inspect_field", ("document",)),
    "mcp-evidence-read": ("mcp_evidence", "read", ("document",)),
}


def _contract(operation, *, handler=True, target="allocated-compute-or-desktop", side_effects="none"):
    return {"schema": SCHEMA, "operation": operation, "version": 1,
            "profile": "heavy", "targets": ["desktop", "allocated-compute"],
            "target": target, "budget": dict(DEFAULT_BUDGET), "network_bytes": 0,
            "concurrency": 1, "additional_job": False, "additional_gpu": False,
            "cancellation": "cooperative checkpoints and owned task-process termination",
            "checkpoint": "none; completed immutable results only", "retry": "explicit pure retry",
            "side_effects": side_effects, "result_schema": RESULT_SCHEMA,
            "coverage": "input-declared coverage; no missing records inferred complete",
            "acceptance": "finite bounded result with input/context digests and declared limits",
            "implemented_worker_adapter": handler}


from .distribution_profile import absent as _extension_absent
_PURE = {key: value for key, value in _PURE.items() if not _extension_absent("raven." + value[0])}
OPERATIONS = {key: _contract(key) for key in _PURE}
OPERATIONS["mcp-evidence-read"]["budget"].update(input_bytes=128 << 20, output_bytes=128 << 10, wall_seconds=10, cpu_seconds=5, max_records=10000)
for _key in ("planning", "planning-command", "trace-file", "log-search", "log-search-files", "research-command", "scientific-inspection"):
    OPERATIONS[_key] = _contract(_key)
OPERATIONS["trace-file"]["budget"]["input_bytes"] = 8 << 20
OPERATIONS["trace-file"]["budget"]["max_records"] = 20000
OPERATIONS["log-search-files"]["budget"]["input_bytes"] = 8 << 20
OPERATIONS["scientific-inspection"]["budget"]["input_bytes"] = 8 << 20

# These existing named inspections run unchanged in a resource-limited task
# child. They may read explicit artifacts, but cannot acquire a Slurm transport.
FILE_OPERATIONS = frozenset({"environment", "reuse", "bottlenecks", "energy", "evidence",
    "research-portable", "qualification", "support-bundle", "investigation-workflow",
    "diagnostic-campaign", "minimize-reproducer", "regression-sweep", "workflow-engine",
    "staging", "adapter", "wasi-analysis", "checkpoint", "search", "packing", "dask", "heterogeneous",
    "incidents", "investigation-timeline", "evidence-query", "allocation-health", "process-profile", "telemetry-anomalies", "phase-alignment", "queue-model", "campaign-policy"})
from .distribution_profile import missing_operation as _extension_operation_absent
FILE_OPERATIONS = frozenset(key for key in FILE_OPERATIONS if not _extension_operation_absent(key))
for _key in FILE_OPERATIONS:
    OPERATIONS[_key] = _contract(_key)
    OPERATIONS[_key]["budget"]["input_bytes"] = 8 << 20
    OPERATIONS[_key]["coverage"] = "Explicit stable artifact snapshots; unavailable host mappings remain unavailable"
PUBLISH_OPERATIONS = frozenset({"research-portable", "support-bundle", "reuse", "execution-simulation", "log-export"})
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
OPERATIONS["artifact-publish"] = _contract("artifact-publish", side_effects="reviewed no-clobber artifact publication")
OPERATIONS["artifact-publish"]["retry"] = "never automatic; reconcile receipt before a new review"
OPERATIONS["artifact-publish"]["budget"]["input_bytes"] = 8 << 20
OPERATIONS["evidence-query"]["budget"].update(input_bytes=128 << 20, scratch_mib=128)
OPERATIONS["evidence-index-update"] = _contract("evidence-index-update", side_effects="reviewed transactional evidence index update or consistent no-clobber backup")
OPERATIONS["evidence-index-update"]["budget"].update(input_bytes=128 << 20, scratch_mib=128, max_records=200000)
OPERATIONS["evidence-index-update"]["retry"] = "never automatic; reconcile immutable capture or publication receipt before retry"
OPERATIONS["allocation-health-probe"] = _contract("allocation-health-probe", side_effects="reviewed bounded memory/scratch probes in verified owned allocation and no-clobber receipt")
OPERATIONS["allocation-health-probe"]["budget"].update(wall_seconds=35, cpu_seconds=30, scratch_mib=16)
OPERATIONS["allocation-health-probe"]["retry"] = "never automatic; inspect retained receipt before preparing a new review"
for _health in ("allocation-health", "allocation-health-probe"):
    OPERATIONS[_health]["budget"].update(cpus=2, memory_mib=1024)
OPERATIONS["wasi-analysis-execute"] = _contract("wasi-analysis-execute", side_effects="reviewed bounded pure-analysis WASI module and optional no-clobber result receipt")
OPERATIONS["wasi-analysis-execute"]["budget"].update(input_bytes=8 << 20, wall_seconds=35, cpu_seconds=30, scratch_mib=16)
OPERATIONS["wasi-analysis-execute"]["retry"] = "never automatic; inspect retained receipt before preparing a new review"
OPERATIONS["process-profile-capture"] = _contract("process-profile-capture", side_effects="reviewed read-only owned local process sampling and no-clobber receipt")
OPERATIONS["process-profile-capture"]["budget"].update(wall_seconds=65, cpu_seconds=10, scratch_mib=4)
OPERATIONS["process-profile-capture"]["retry"] = "never automatic; inspect receipt and revalidate exact PID/start-time identity"
OPERATIONS["simulation-review"] = _contract("simulation-review")

# These operations retain their existing explicit review/identity path in Light.
# No computational entitlement is gained by submitting a job or changing mode.
LIGHT_OPERATIONS = frozenset({
    "storage", "pending-edit", "array-throttle", "reservations", "licenses",
    "slurm-doctor", "batch-script", "rerun-job", "allocation-shell", "placement",
    "dependency-repair", "queue-order", "project-guide", "site-policy",
    "runtime-steering", "io-coordination", "trace-capture", "trace-snapshot",
    "supervisor", "clusters", "site-telemetry",
})
HEAVY_OPERATIONS = frozenset({
    "environment", "acceptance", "statistics", "reuse", "checkpoint", "search",
    "packing", "dask", "heterogeneous", "supervisor", "staging", "workflow-engine",
    "bottlenecks", "clusters", "incidents", "energy", "investigation-timeline",
    "investigation-workflow", "recommendation", "investigation-plan", "scientific-impact",
    "numerical-stability", "factorial-design", "factorial-analysis", "regression-investigation", "diagnostic-campaign",
    "minimize-reproducer", "regression-sweep", "site-telemetry", "evidence", "adapter", "wasi-analysis",
    "research-portable", "qualification", "support-bundle", "execution-simulation",
    "diagnostic-recipe", "information-query", "scientific-field", "trace-inspect",
    "causal-delay", "collective-audit", "evidence-query", "allocation-health", "process-profile", "telemetry-anomalies", "phase-alignment",
})

# Only bounded existing source readers and centrally gated dispatchers may use
# the Light background lane. Unknown plugins/callables are not admitted.
LIGHT_BACKGROUND_MODULES = frozenset({
    "activity_ui", "logs", "controller", "log_catalog", "execution_ui",
    "project_ui", "session_tools", "research_commands", "ops_ui",
    "terminal_workflows", "shell_checks_ui", "terminal_fusion", "telemetry_ui",
    "job_progress", "views", "array_manifest_ui", "planning_commands", "trace_ui",
    "quick_advisor", "analysis_ui", "log_tools", "log_workbench", "history_log_export", "data_export_ui", "auto_export_ui", "setup_ui", "series_settings",
    "onboarding_ui", "launch_wizard_ui", "launch_wizard",
    "connect_data_ui", "connect_data", "run_home_ui", "run_home",
})
HEAVY_BACKGROUND_MODULES = frozenset({"log_tools", "log_workbench", "quick_advisor",
                                      "analysis_ui", "history_log_export"})
_PROCESS_ROUTER = None
_SOURCE_BUDGET = ContextVar("raven_source_budget", default=None)
_TASK_ROUTER = ContextVar("raven_task_router", default=None)


class AdmissionError(ValueError):
    """A managed request has no admitted execution target."""


def _json(value, limit, label, max_records=100000):
    """Bound depth and element count before serializing possibly nested data."""
    pending, count = [(value, 0)], 0
    while pending:
        current, depth = pending.pop()
        count += 1
        if depth > 32 or count > max_records:
            raise ValueError(label + " exceeds the depth or record budget")
        if isinstance(current, dict):
            if any(not isinstance(key, str) or len(key) > 4096 for key in current):
                raise ValueError(label + " needs bounded string keys")
            pending.extend((item, depth+1) for item in current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend((item, depth+1) for item in current)
        elif not (current is None or isinstance(current, (str, int, float, bool))):
            raise ValueError(label + " must be JSON data")
        if isinstance(current, str) and len(current) > limit:
            raise ValueError(label + " exceeds its byte budget")
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
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
    if not isinstance(supplied, dict) or set(supplied)-set(defaults):
        raise ValueError("Unknown execution budget field")
    result = dict(defaults, **supplied)
    for key, value in result.items():
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                or value <= 0 or value > defaults[key]
                or key not in {"wall_seconds", "cpu_seconds"} and not isinstance(value, int)):
            raise ValueError("Invalid or excessive execution budget: " + key)
    return result


class _Cancellation:
    def __init__(self, callback, deadline):
        self.callback, self.deadline = callback, deadline

    def is_set(self):
        return time.monotonic() >= self.deadline or bool(self.callback and self.callback())

    def check(self):
        if self.is_set():
            raise AdmissionError("Execution cancelled or its deadline expired")


def _execute_operation(operation, inputs, context=None, budget=None, cancelled=None, verified_execution=None):
    """Worker-only, fixed named adapters; no source strings or commands execute.

    The runtime owns process CPU/RSS/lifetime enforcement. This function adds
    finite JSON, input/output, record and cooperative deadline checks.
    """
    limits = budget_for(operation, budget)
    raw = _json(inputs, limits["input_bytes"], "Task inputs", limits["max_records"])
    ctx_raw = _json(context or {}, 64 << 10, "Task context")
    # Fresh values cannot alias the caller's mutable documents.
    inputs, context = json.loads(raw), json.loads(ctx_raw)
    if not isinstance(inputs, dict) or not isinstance(context, dict):
        raise ValueError("Task inputs and context must be objects")
    cancel = _Cancellation(cancelled, time.monotonic()+limits["wall_seconds"])
    cancel.check()
    if operation in _PURE:
        module, name, fields = _PURE[operation]
        allowed = set(fields) | ({"axis", "index", "bins"} if operation == "scientific-field" else set())
        if set(inputs)-allowed or set(fields)-set(inputs):
            raise ValueError("Unexpected or missing fields for " + operation)
        function = getattr(importlib.import_module("raven."+module), name)
        kwargs = {key: inputs[key] for key in ("axis", "index", "bins") if key in inputs}
        if operation in {"execution-simulation", "diagnostic-recipe", "mcp-evidence-read"}:
            kwargs["cancel"] = cancel
        result = function(*(inputs[key] for key in fields), **kwargs)
    elif operation in {"artifact-publish", "evidence-index-update", "allocation-health-probe", "wasi-analysis-execute", "process-profile-capture"}:
        allowed = {"process-profile"} if operation == "process-profile-capture" else {"wasi-analysis"} if operation == "wasi-analysis-execute" else {"allocation-health"} if operation == "allocation-health-probe" else {"evidence-query"} if operation == "evidence-index-update" else PUBLISH_OPERATIONS
        if set(inputs) != {"operation", "plan"} or inputs["operation"] not in allowed:
            raise ValueError("Unsupported reviewed artifact publication")
        from . import operations as O
        ctx = O.Context(selected=context.get("selected", ""), scope=context.get("scope", {}),
                        jobs=tuple(context.get("attempts", [])), cancel=cancel,
                        verified_execution=verified_execution or {})
        payload = O.validate_plan(inputs["plan"], ctx, inputs["operation"])
        if inputs["operation"] == "log-export":
            from .log_bundle import export_logs
            if set(payload)-{"report", "destination", "root", "state_dir", "clipboard"}:
                raise ValueError("Unknown log export parameter")
            entries = payload["report"].get("entries", [])
            total = sum(entry.get("snapshot", {}).get("size", 0) for entry in entries)
            if len(entries) > 128 or total > limits["input_bytes"]:
                raise ValueError("Log export exceeds 128 sources or its declared input byte budget")
            # JSON transport changes identity tuples to lists. Restore only the
            # captured scalar identity; never replace it with a fresh stat.
            for entry in entries:
                snapshots = [entry.get("snapshot", {})] + [item.get("snapshot", {}) for item in entry.get("associations", ())]
                for snapshot in snapshots:
                    identity = snapshot.get("ident")
                    if not isinstance(identity, (list, tuple)) or not all(isinstance(part, (str, int)) for part in identity):
                        raise ValueError("Log export requires a captured file identity")
                    snapshot["ident"] = tuple(identity)
                    if isinstance(snapshot.get("updated"), list):
                        snapshot["updated"] = tuple(snapshot["updated"])
            exported = export_logs(**payload, cancel=cancel, use_osc52=False, use_tools=False)
            result = O.report("log-export", exported.get("message", "Log export finished"), data=exported,
                              status="ok" if exported.get("status") in {"ready", "partial"} else "error")
        else:
            result = O._registry()[inputs["operation"]][0].apply(inputs["operation"], inputs["plan"], ctx)
    elif operation in FILE_OPERATIONS or operation in {"simulation-review", "scientific-inspection"}:
        expected = {"operation", "params"} if operation == "scientific-inspection" else {"params"}
        if set(inputs) != expected:
            raise ValueError("Artifact operation requires its declared parameters")
        from . import operations as O
        operation_key = inputs["operation"] if operation == "scientific-inspection" else "execution-simulation" if operation == "simulation-review" else operation
        if operation == "scientific-inspection" and operation_key not in _PURE:
            raise ValueError("Unknown named scientific inspection")
        params = O.parameters(operation_key, inputs["params"])
        if operation in {"diagnostic-campaign", "minimize-reproducer", "regression-sweep"} and params.get("action") in {"status", "cancel"}:
            raise ValueError("Driver status and cancellation belong to the original execution host")
        ctx = O.Context(selected=context.get("selected", ""), scope=context.get("scope", {}),
                        jobs=tuple(context.get("attempts", [])), cancel=cancel,
                        state_dir=context.get("state_dir", ""), verified_execution=verified_execution or {})
        result = O._registry()[operation_key][0].run(operation_key, params, ctx)
    elif operation == "planning":
        allowed = {"view", "source", "snap", "job", "observations", "overrides"}
        if set(inputs)-allowed or "view" not in inputs:
            raise ValueError("Invalid planning task fields")
        from .planning import analyze
        result = analyze(inputs["view"], inputs.get("source"), **{key: inputs[key] for key in allowed-{"view", "source"} if key in inputs})
    elif operation == "planning-command":
        if set(inputs)-{"command", "args", "snap", "job", "observations", "choices"}:
            raise ValueError("Invalid planning command fields")
        from .planning_commands import result as planning_result
        result, options = planning_result(inputs["command"], inputs["args"],
            **{key: inputs[key] for key in ("snap", "job", "observations", "choices") if key in inputs})
        result = {"output": result, "options": vars(options)}
    elif operation == "research-command":
        if set(inputs) != {"command", "args"} or inputs["command"] not in {"validate", "passport"}:
            raise ValueError("Unknown read-only research command")
        if inputs["command"] == "passport" and inputs["args"][:1] not in (["show"], ["compare"]):
            raise ValueError("Passport capture is an artifact mutation and needs its separate review")
        from .operations import Context, read_json
        args = inputs["args"]
        if inputs["command"] == "validate":
            from .artifacts import validate_contract
            if len(args) != 2:
                raise ValueError("validate requires CONTRACT ROOT")
            contract = read_json(Context(), os.path.expanduser(args[0]), limit=limits["input_bytes"])
            result = validate_contract(contract, os.path.expanduser(args[1]), max_bytes=source_limit(limits["input_bytes"]), max_entries=128)
            source_read(result["budget"]["bytes_read"])
            code = 0 if result.get("valid") else 1
        else:
            from .provenance import load, diff
            if args[:1] == ["show"] and len(args) == 2:
                result = load(os.path.expanduser(args[1]))
            elif args[:1] == ["compare"] and len(args) == 3:
                documents = [load(os.path.expanduser(path)) for path in args[1:]]
                result = {"differences": diff(*documents)}
            else:
                raise ValueError("passport requires show FILE or compare LEFT RIGHT")
            code = 0
        result = {"output": result, "exit_code": code}
    elif operation == "trace-file":
        if set(inputs) != {"operation", "params"} or inputs["operation"] not in {"trace-inspect", "causal-delay", "collective-audit"}:
            raise ValueError("Invalid trace task")
        from . import operations as O, ops_trace
        ctx = O.Context(selected=context.get("selected", ""), scope=context.get("scope", {}), cancel=cancel)
        result = ops_trace.run(inputs["operation"], O.parameters(inputs["operation"], inputs["params"]), ctx)
    elif operation == "log-search":
        if set(inputs) != {"text", "needle"} or not all(isinstance(inputs[k], str) for k in inputs):
            raise ValueError("Log search needs bounded literal text and needle")
        if not inputs["needle"] or len(inputs["needle"]) > 4096:
            raise ValueError("Search term must contain 1–4096 characters")
        matches = []
        for index, line in enumerate(inputs["text"].splitlines()):
            if index % 256 == 0:
                cancel.check()
            if index >= limits["max_records"]:
                raise ValueError("Log search exceeds the record budget")
            if inputs["needle"] in line:
                matches.append({"line": index+1, "text": line})
        result = {"matches": matches, "coverage": "supplied text only; no whole-file claim"}
    elif operation == "log-search-files":
        if set(inputs) != {"sources", "query", "options"}:
            raise ValueError("Invalid file search fields")
        from . import log_scan
        from .remote import LocalFiles
        options = inputs["options"]
        if not isinstance(options, dict) or set(options)-{"regex", "case", "word", "context", "max_results"}:
            raise ValueError("Unknown file search options")
        sources = inputs["sources"]
        if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
            raise ValueError("Choose at most 32 explicit search sources")
        total = sum(log_scan.snapshot(LocalFiles(), source["path"])["size"] for source in sources)
        if total > limits["input_bytes"]:
            raise ValueError("File search exceeds its declared input byte budget")
        result = log_scan.search_sources(LocalFiles(), sources, inputs["query"],
            **options, max_bytes=limits["input_bytes"], cancel=cancel.is_set)
    else:
        raise ValueError("No worker adapter for " + operation)
    cancel.check()
    receipt = {"schema": RESULT_SCHEMA, "operation": operation, "version": 1,
               "input_sha256": hashlib.sha256(raw).hexdigest(), "context_sha256": hashlib.sha256(ctx_raw).hexdigest(),
               "budget": limits, "result": result,
               "coverage": "Declared inputs only; source gaps and scientific limitations remain in the result."}
    # Existing readers use tuples for identities/aliases. Wire results contain
    # plain JSON only and cannot retain mutable references to handler objects.
    return json.loads(_json(receipt, limits["output_bytes"], "Task result"))


def execute_operation(operation, inputs, context=None, budget=None, cancelled=None, verified_execution=None):
    if router_for().managed:
        raise AdmissionError("Worker adapters execute only in the admitted worker process; use managed dispatch")
    limits = budget_for(operation, budget)
    token = _SOURCE_BUDGET.set({"remaining": limits["input_bytes"], "files": 128})
    try:
        return _execute_operation(operation, inputs, context, limits, cancelled, verified_execution)
    finally:
        _SOURCE_BUDGET.reset(token)


def source_limit(requested):
    state = _SOURCE_BUDGET.get()
    if state is None:
        return requested
    if state["files"] <= 0 or state["remaining"] <= 0:
        raise AdmissionError("Task artifact read budget exhausted")
    return min(requested, state["remaining"])


def source_read(amount):
    state = _SOURCE_BUDGET.get()
    if state is not None:
        if amount < 0 or amount > state["remaining"] or state["files"] <= 0:
            raise AdmissionError("Task artifact read budget exhausted")
        state["remaining"] -= amount
        state["files"] -= 1


def deferred(capability, reason, *, supported=True):
    return {"schema": "raven.operation/v1", "feature": capability, "status": "deferred",
            "summary": reason, "rows": ["Request compute explicitly; no analysis ran on the interface host."],
            "warnings": [], "data": {"required_profile": "heavy", "worker_adapter": supported,
                                        "executed": False, "retry": "explicit request after compute is ready"}}


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
        return worker if worker is not None and getattr(worker, "ready", False) else None

    def require(self, capability, *, worker_operation=None):
        operation = worker_operation or capability
        if not self.managed:
            return None
        if operation not in OPERATIONS:
            self.last_deferred = deferred(capability, "This workflow needs a declared execution adapter; local fallback is disabled.", supported=False)
        elif self.worker() is None:
            self.last_deferred = deferred(capability, "Heavy compute is not ready; the task is prepared/deferred.")
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
            return deferred(operation, "Compute became unavailable before task admission")
        limits = budget_for(operation, budget)
        resources = getattr(worker, "resources", None)
        if resources:
            from .raven_resources import task_limits
            share = task_limits({}, resources)
            limits = {key: min(value, share.get(key, value)) for key, value in limits.items()}
        inputs = json.loads(_json(inputs, limits["input_bytes"], "Task inputs", limits["max_records"]))
        context = json.loads(_json(context or {}, 64 << 10, "Task context"))
        identity = _json(getattr(worker, "identity", {}), 64 << 10, "Worker identity")
        task_id = worker.submit(operation, inputs, context or {}, budget=limits)
        deadline = time.monotonic()+limits["wall_seconds"]+2
        while True:
            cancelled = bool(cancel() if callable(cancel) else cancel.is_set()) if cancel is not None else False
            if cancelled or time.monotonic() >= deadline:
                worker.cancel(task_id)
                raise AdmissionError("Worker task cancelled or timed out; no local retry was started")
            worker.poll()
            if not getattr(worker, "ready", False) or _json(getattr(worker, "identity", {}), 64 << 10, "Worker identity") != identity:
                raise AdmissionError("Execution placement changed; no local fallback was started")
            state = getattr(worker, "tasks", {}).get(task_id, {})
            status = state.get("status")
            if status in {"completed", "complete", "done", "succeeded", "committed"}:
                result = state.get("result")
                _json(result, limits["output_bytes"], "Worker result")
                if not isinstance(result, dict) or result.get("schema") != RESULT_SCHEMA or result.get("operation") != operation:
                    raise AdmissionError("Worker result has the wrong execution contract")
                if result.get("context_sha256") != hashlib.sha256(_json(context or {}, 64 << 10, "Task context")).hexdigest():
                    raise AdmissionError("Worker result belongs to another execution identity")
                ack = getattr(worker, "ack", None)
                if ack is not None:
                    ack(task_id)
                return result
            if status in {"error", "failed", "cancelled", "expired", "rejected", "uncertain"}:
                raise AdmissionError("Worker task " + status + ": " + str(state.get("error", "no result"))[:400])
            time.sleep(.02)


def router_for(value=None):
    explicit = getattr(value, "execution", None) or getattr(value, "raven_execution", None)
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
    _PROCESS_ROUTER = ExecutionRouter(policy, (lambda: manager.worker) if manager else None)
    return _PROCESS_ROUTER


def process_router():
    return _PROCESS_ROUTER


def reset_process(previous=None):
    global _PROCESS_ROUTER
    _PROCESS_ROUTER = previous


def bind(app, manager=None):
    router = ExecutionRouter(policy_for(app), (lambda: manager.worker) if manager else None)
    router.source_backend = getattr(manager, "collection_backend", None) if manager else getattr(getattr(app, "slurm", None), "b", None)
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
    router.last_deferred = deferred("background:"+module,
        "This analysis needs an explicit worker task; arbitrary local callbacks are disabled.", supported=False)
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


def large_log_export(router, payload, cancel=None):
    from . import operations as O
    blocked = router.require("log-export", worker_operation="artifact-publish")
    if blocked is not None:
        return {"status": "deferred", "message": blocked["summary"], "export_path": ""}
    worker = router.worker()
    identity = hashlib.sha256(_json(worker.identity, 64 << 10, "Worker identity")).hexdigest()
    ctx = O.Context(scope={"artifact_target": identity}, execution=router, cancel=cancel,
                    state_dir=str(payload.get("state_dir") or ""))
    plan = O.prepare_plan(ctx, "log-export", {}, payload)
    plan["raven_worker"] = identity
    plan["digest"] = O.digest({key: value for key, value in plan.items() if key != "digest"})
    callback = apply_admission("log-export", plan, ctx)
    from .action_lifecycle import execute
    report = execute("log-export", plan, ctx, callback)
    return report["data"]


def capability_ledger():
    """Every installed proposal and operation receives a versioned contract."""
    from .capabilities import PROPOSALS
    from .operations import catalog
    entries = []
    for specification in catalog():
        key = specification["key"]
        adapter = "trace-file" if key in {"trace-inspect", "causal-delay", "collective-audit"} else "scientific-inspection" if key in _PURE else key
        entry = _contract(key, handler=adapter in OPERATIONS)
        entry["worker_operation"] = adapter if adapter in OPERATIONS else None
        if key in LIGHT_OPERATIONS:
            entry.update(profile="light", target="bounded-source-or-reviewed-target",
                         targets=["interface", "reviewed-job-target"], retry="never replay a mutation",
                         side_effects="adapter review and exact-attempt revalidation required for apply",
                         budget={"input_bytes": 256 << 10, "output_bytes": MAX_RESULT,
                                 "wall_seconds": 120, "concurrency": 2})
        entry["proposal_ids"] = [proposal.id for proposal in PROPOSALS if key in proposal.operations]
        entry["availability"] = "light-reviewed" if key in LIGHT_OPERATIONS else "worker-adapter" if adapter in OPERATIONS else "prepared/deferred; adapter unavailable"
        if key == "clusters":
            entry["availability"] = "prepared/deferred; governed live collector unavailable"
        entry["apply"] = ("existing exact-attempt reviewed target" if key in LIGHT_OPERATIONS
                          else "reviewed verified-allocation probes and receipt" if key == "allocation-health"
                          else "reviewed transactional index update or export" if key == "evidence-query"
                          else "reviewed worker publication" if key in PUBLISH_OPERATIONS
                          else "read-only or deferred until target adapter is registered")
        entries.append(entry)
    return {"schema": "raven.capability-ledger/v1", "operations": entries,
            "proposals": [{"id": proposal.id, "operations": list(proposal.operations), "area": proposal.area,
                            "boundary": proposal.boundary} for proposal in PROPOSALS],
            "background": {"bounded_read_or_gated_dispatch": sorted(LIGHT_BACKGROUND_MODULES),
                           "worker_required": sorted(HEAVY_BACKGROUND_MODULES), "unknown": "rejected"},
            "deferred_paths": dict(DEFERRED_PATHS),
            "worker_operations": [OPERATIONS[key] for key in sorted(OPERATIONS)],
            "limits": ["A registered contract is not site qualification.",
                       "Legacy pure libraries outside a managed Raven runtime are not sandboxed.",
                       "A Heavy worker never grants additional scheduler query or mutation entitlement."]}


def context_data(ctx):
    selected = str(getattr(ctx, "selected", "") or "")
    records = [row for row in getattr(ctx, "jobs", ()) + getattr(ctx, "finished", ()) if str(row.get("id", "")) == selected]
    result = {"selected": selected, "scope": getattr(ctx, "scope", {}), "attempts": records[:2],
              "state_dir": str(getattr(ctx, "state_dir", "") or "")}
    _json(result, 64 << 10, "Execution identity")
    return result


def operation_route(feature, params, ctx):
    """Return None only for admitted Light/legacy handlers; never heavy fallback."""
    router = router_for(ctx)
    if not router.managed:
        return None
    if feature == "campaign-policy" and params.get("action", "compare") != "compare":
        return None  # Bounded interface-owned plan controls, never job submission.
    if feature == "clusters" or (feature == "supervisor" and params.get("action", "status") == "start") or (feature == "site-telemetry" and params.get("action", "inspect") == "query"):
        return deferred(feature, "This live collector needs a governed source adapter; a Heavy worker does not grant another polling entitlement.", supported=False)
    if feature in LIGHT_OPERATIONS:
        return None
    if feature == "queue-model" and params.get("action", "discover") == "discover":
        return None  # Explicit governed reads on the captured scheduler connection.
    trace = feature in {"trace-inspect", "causal-delay", "collective-audit"}
    simulation_review = feature == "execution-simulation" and bool(params.get("destination"))
    operation = "trace-file" if trace else "simulation-review" if simulation_review else "scientific-inspection" if feature in _PURE else feature
    blocked = router.require(feature, worker_operation=operation)
    if blocked is not None:
        return blocked
    if not (trace or feature in FILE_OPERATIONS or simulation_review or feature in _PURE):
        return deferred(feature, "This operation has no artifact inspection adapter", supported=False)
    if getattr(ctx.files, "remote", False):
        return deferred(feature, "Artifact paths need an explicit worker-visible mapping; remote paths are not replaced by local files.", supported=False)
    inputs = {"operation": feature, "params": params} if trace or operation == "scientific-inspection" else {"params": params}
    receipt = router.dispatch(operation, inputs, context_data(ctx), cancel=ctx.cancel)
    if receipt.get("status") == "deferred":
        return receipt
    result = receipt["result"]
    if feature == "campaign-policy":
        from .ops_campaign_policy import controlled_report
        result = controlled_report(result, ctx)
    result["execution_receipt"] = {key: value for key, value in receipt.items() if key != "result"}
    if result.get("plan"):
        from .operations import digest
        plan = result["plan"]
        worker = router.worker()
        if worker is None:
            raise AdmissionError("Compute became unavailable before the review was bound; inspect again")
        plan["raven_worker"] = hashlib.sha256(_json(worker.identity, 64 << 10, "Worker identity")).hexdigest()
        plan["digest"] = digest({key: value for key, value in plan.items() if key != "digest"})
    return result


def apply_admission(feature, plan, ctx):
    router = router_for(ctx)
    if feature == "campaign-policy" and isinstance(plan, dict) and plan.get("payload", {}).get("action") in {"pause", "resume", "cancel", "override"}:
        return None  # The normal reviewed action lifecycle persists local controls.
    if router.managed and feature == "supervisor" and isinstance(plan, dict) and plan.get("payload", {}).get("action") == "start":
        raise AdmissionError("A persistent collector needs its own governed source adapter before it can start")
    if not router.managed or feature in LIGHT_OPERATIONS:
        return None
    if feature in PUBLISH_OPERATIONS or feature in {"evidence-query", "allocation-health", "wasi-analysis", "process-profile"}:
        mutation_operation = "process-profile-capture" if feature == "process-profile" else "wasi-analysis-execute" if feature == "wasi-analysis" else "allocation-health-probe" if feature == "allocation-health" else "evidence-index-update" if feature == "evidence-query" else "artifact-publish"
        blocked = router.require(feature, worker_operation=mutation_operation)
        if blocked is not None:
            raise AdmissionError(blocked["summary"])
        identity = hashlib.sha256(_json(router.worker().identity, 64 << 10, "Worker identity")).hexdigest()
        if not isinstance(plan, dict) or plan.get("raven_worker") != identity:
            raise AdmissionError("The reviewed execution target changed; inspect again before publishing")
        def publish(operation, captured, captured_ctx):
            identity_context = context_data(captured_ctx)
            identity_context["reviewed_plan_digest"] = captured["digest"]
            receipt = router.dispatch(mutation_operation, {"operation": operation, "plan": captured},
                                      identity_context, cancel=captured_ctx.cancel)
            if receipt.get("status") == "deferred":
                raise AdmissionError(receipt["summary"])
            result = receipt["result"]
            result["execution_receipt"] = {key: value for key, value in receipt.items() if key != "result"}
            return result
        return publish
    raise AdmissionError("This Heavy mutation needs a reviewed target adapter; no local execution fallback is permitted")


def planning_inputs(view, source, *, snap=None, job=None, observations=(), overrides=None):
    from .planning import record
    snapshot = snap or {}
    jobs, finished = snapshot.get("jobs", ()), snapshot.get("finished", ())
    if len(jobs) > 10000 or len(finished) > 10000:
        raise AdmissionError("Planning exceeds the 10,000-record input window; choose an explicit bounded report")
    kept = {key: snapshot.get(key, {}) for key in ("details", "nodemap", "health")}
    kept.update({key: [record(row) for row in snapshot.get(key, [])] for key in ("jobs", "finished", "partitions", "share")})
    value = {"view": view, "source": source, "snap": kept, "job": record(job) if job is not None else None,
             "observations": list(observations), "overrides": overrides or {}}
    _json(value, MAX_INPUT, "Planning input")
    return value


def cli_admission(argv, router=None):
    """Guard early legacy commands before their recorder/driver is imported."""
    router = router or router_for()
    if not router.managed or not argv:
        return None
    # A child's --help after argparse.REMAINDER is not help for Raven.
    # Accept only the unambiguous parser-only forms, before any payload.
    if len(argv) <= 3 and argv[-1] in {"--help", "-h"} and "--" not in argv:
        return None
    if list(argv) in (["trace", "native", "--sdk-header"], ["trace", "mpi", "--source"]):
        return None
    if needs_job_local(argv):
        return job_local_admission(argv, router)
    if argv[0] == "trace" and argv[1:2] not in (["register"], ["inspect"], ["export"], ["summary"]):
        return deferred("trace-cli", "Use the admitted Trace operation or an explicitly verified job-local instrumentation launcher; this CLI cannot execute on the interface host.", supported=False)
    if argv[0] in {"diagnostic", "diagnostic-trials"}:
        return deferred("diagnostic-campaign", "Review this diagnostic against its explicit allocation or desktop worker; interface-host execution is disabled.", supported=False)
    if argv[0] == "report" and argv[1:2] in (["exec"], ["submit"], ["passport"], ["aggregate"]):
        return deferred("report-"+argv[1], "This reporting driver needs an explicit governed execution target; bounded report metadata commands remain available.", supported=False)
    return None


def needs_job_local(argv):
    """Whether a legacy explicit launcher needs a governed owner before dispatch.

    Reporting submission obtains the owner but does not require compute. Child
    arguments after ``--`` never become parser help or change this decision.
    """
    if not isinstance(argv, (list, tuple)) or len(argv) < 2:
        return False
    if len(argv) <= 3 and argv[-1] in {"--help", "-h"} and "--" not in argv:
        return False
    if list(argv) in (["trace", "native", "--sdk-header"], ["trace", "mpi", "--source"]):
        return False
    return (argv[0] == "report" and argv[1] in {"exec", "submit", "passport", "aggregate"}
            or argv[0] == "trace" and argv[1] in {"run", "native"})


def job_local_admission(argv, router=None):
    router = router or router_for()
    if not router.managed:
        return None
    command = ":".join(argv[:2])
    policy, backend = router.policy, router.source_backend
    try:
        if policy.get("connection", {}).get("transport") != "local":
            raise AdmissionError("Job-local drivers require the actual local Slurm connection; SSH/frontend paths are not substituted")
        if backend is None or getattr(backend, "ledger", None) is None:
            raise AdmissionError("No governed scheduler source is available for this explicit driver")
        capabilities = policy.get("execution", {}).get("capabilities", ())
        if command == "report:submit":
            if "scheduler-action" not in capabilities:
                raise AdmissionError("The site policy disables scheduler actions")
            return None
        if "instrumentation" not in capabilities:
            raise AdmissionError("The site policy disables job-local instrumentation")
        from .raven_resources import placement_probe, verify_placement, limits
        configuration = {"context": {"cluster_id": policy.get("site", {}).get("cluster_id", "")},
                         "resources": limits({"cpus": 1, "memory_mib": 96})}
        candidate = placement_probe(configuration, purpose="instrumentation")
        candidate["probe_id"] = hashlib.sha256(_json(candidate, 64 << 10, "Local process identity")).hexdigest()
        previous = router.job_local_receipt
        if (previous and previous["expires"] >= time.monotonic() and previous["candidate"] == candidate
                and previous["backend"] is backend):
            return None
        from .raven_collection import make_placement_verifier
        records = make_placement_verifier(backend, purpose="instrumentation")(candidate)
        identity = verify_placement(configuration, candidate, records, purpose="instrumentation")
        router.job_local_receipt = {"candidate": candidate, "identity": identity,
                                    "backend": backend, "expires": time.monotonic()+2.}
        return None
    except (ValueError, OSError, RuntimeError) as exc:
        result = deferred(command, "Job-local launch deferred: " + str(exc)[:400], supported=True)
        result["data"]["prepared_invocation"] = list(argv)
        result["data"]["required_target"] = "same invocation inside a verified owned Slurm job/step"
        return result


@contextmanager
def reporting_submission(argv):
    """Preserve report submit's exact environment/intent under scheduler quota."""
    router = router_for()
    if not router.managed:
        yield
        return
    blocked = job_local_admission(["report", "submit"], router)
    if blocked is not None:
        raise AdmissionError(blocked["summary"])
    from .raven_collection import classify
    descriptor = classify(argv)
    if descriptor is None or descriptor.family != "submit":
        raise AdmissionError("Reporting submission requires one exact sbatch invocation")
    with router.source_backend.ledger.admit(descriptor, argv, validation=True):
        yield


def helper_admission(app, text, metadata=None):
    """Generated PTY helpers cannot smuggle analysis around worker admission."""
    router = router_for(app)
    if not router.managed:
        return None
    metadata = metadata or {}
    if metadata.get("helper_kind") == "user-paste":
        return None
    if (metadata.get("helper_kind") == "shell-hook" and metadata.get("session_kind", "local") == "local"
            and isinstance(text, str) and len(text.encode()) <= 64 << 10):
        return None
    value = deferred("terminal-helper", "Generated helpers need a declared operation and admitted execution target; ordinary shell input remains available.", supported=False)
    value["reason"] = value["summary"]
    return value


def research_command_route(command, args, router=None):
    router = router or router_for()
    if not router.managed or command not in {"validate", "passport"}:
        return None
    # Displaying one bounded provenance document is ordinary Light evidence.
    if command == "passport" and list(args[:1]) == ["show"] and len(args) == 2:
        from .provenance import load
        available = source_limit(256 << 10)
        token = _SOURCE_BUDGET.set({"remaining": available, "files": 1})
        try:
            document = load(os.path.expanduser(args[1]))
        finally:
            used = available - _SOURCE_BUDGET.get()["remaining"]
            _SOURCE_BUDGET.reset(token)
            source_read(max(0, used))
        return document, 0
    if command == "passport" and list(args[:1]) == ["capture"]:
        blocked = job_local_admission(["report", "passport"], router)
        return None if blocked is None else (blocked, 2)
    blocked = router.require(command, worker_operation="research-command")
    if blocked is not None:
        return blocked, 2
    receipt = router.dispatch("research-command", {"command": command, "args": list(args)})
    if receipt.get("status") == "deferred":
        return receipt, 2
    return receipt["result"]["output"], receipt["result"]["exit_code"]
