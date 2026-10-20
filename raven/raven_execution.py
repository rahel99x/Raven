"""Named worker adapters and compatibility execution API.

Policy and admission are separate lower-layer contracts; this application
composition module imports scientific adapters only inside admitted children.
"""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import importlib
import json
import os
import time

from . import execution_policy as _policy
from .execution_admission import operation_route as operation_route, apply_admission
from .execution_policy import (AdmissionError, DEFERRED_PATHS, FILE_OPERATIONS, HEAVY_BACKGROUND_MODULES, LIGHT_BACKGROUND_MODULES, LIGHT_OPERATIONS, MAX_RESULT, OPERATIONS, PUBLISH_OPERATIONS, RESULT_SCHEMA, _Cancellation, _PURE, _SOURCE_BUDGET, _contract, _json, budget_for, deferred, router_for, source_limit, source_read)
from .planning_input import planning_inputs as planning_inputs

# Preserve released imports, constants and ContextVar identities.
globals().update({name: value for name, value in vars(_policy).items() if not name.startswith("__")})


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
        if operation in {"execution-simulation", "diagnostic-recipe", "mcp-evidence-read", "scientific-impact"}:
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
        result = analyze(inputs["view"], inputs.get("source"), cancel=cancel, **{key: inputs[key] for key in allowed-{"view", "source"} if key in inputs})
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
        from .local_files import LocalFiles
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
    report = execute("log-export", plan, ctx, callback, services=O.action_services())
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
    if argv[0] == "trace" and argv[1:2] not in (["register"], ["inspect"], ["export"], ["summary"], ["capabilities"], ["cleanup-runtime"]):
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
