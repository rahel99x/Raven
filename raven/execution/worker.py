"""Explicit admitted worker adapters above log, trace and planning services."""

from __future__ import annotations
import hashlib
import json
import os
import time
from .policy import (
    AdmissionError,
    budget_for,
    _json,
    _Cancellation,
    _PURE,
    FILE_OPERATIONS,
    PUBLISH_OPERATIONS,
    _SOURCE_BUDGET,
    RESULT_SCHEMA,
    router_for,
    source_limit,
    source_read,
)
from .registry import pure_binding


def _execute_operation(
    operation,
    inputs,
    context=None,
    budget=None,
    cancelled=None,
    verified_execution=None,
):
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
    cancel = _Cancellation(cancelled, time.monotonic() + limits["wall_seconds"])
    cancel.check()
    if operation in _PURE:
        binding = pure_binding(operation)
        fields = binding.fields
        allowed = set(fields) | (
            {"axis", "index", "bins"} if operation == "scientific-field" else set()
        )
        if set(inputs) - allowed or set(fields) - set(inputs):
            raise ValueError("Unexpected or missing fields for " + operation)
        function = binding.function
        kwargs = {
            key: inputs[key] for key in ("axis", "index", "bins") if key in inputs
        }
        if operation in {
            "execution-simulation",
            "diagnostic-recipe",
            "mcp-evidence-read",
            "scientific-impact",
        }:
            kwargs["cancel"] = cancel
        result = function(*(inputs[key] for key in fields), **kwargs)
    elif operation in {
        "artifact-publish",
        "evidence-index-update",
        "allocation-health-probe",
        "wasi-analysis-execute",
        "process-profile-capture",
    }:
        allowed = (
            {"process-profile"}
            if operation == "process-profile-capture"
            else {"wasi-analysis"}
            if operation == "wasi-analysis-execute"
            else {"allocation-health"}
            if operation == "allocation-health-probe"
            else {"evidence-query"}
            if operation == "evidence-index-update"
            else PUBLISH_OPERATIONS
        )
        if set(inputs) != {"operation", "plan"} or inputs["operation"] not in allowed:
            raise ValueError("Unsupported reviewed artifact publication")
        from .. import operations as O

        ctx = O.Context(
            selected=context.get("selected", ""),
            scope=context.get("scope", {}),
            jobs=tuple(context.get("attempts", [])),
            cancel=cancel,
            verified_execution=verified_execution or {},
        )
        payload = O.validate_plan(inputs["plan"], ctx, inputs["operation"])
        if inputs["operation"] == "log-export":
            from ..job_logs.bundle import export_logs

            if set(payload) - {
                "report",
                "destination",
                "root",
                "state_dir",
                "clipboard",
            }:
                raise ValueError("Unknown log export parameter")
            entries = payload["report"].get("entries", [])
            total = sum(entry.get("snapshot", {}).get("size", 0) for entry in entries)
            if len(entries) > 128 or total > limits["input_bytes"]:
                raise ValueError(
                    "Log export exceeds 128 sources or its declared input byte budget"
                )
            # JSON transport changes identity tuples to lists. Restore only the
            # captured scalar identity; never replace it with a fresh stat.
            for entry in entries:
                snapshots = [entry.get("snapshot", {})] + [
                    item.get("snapshot", {}) for item in entry.get("associations", ())
                ]
                for snapshot in snapshots:
                    identity = snapshot.get("ident")
                    if not isinstance(identity, (list, tuple)) or not all(
                        isinstance(part, (str, int)) for part in identity
                    ):
                        raise ValueError("Log export requires a captured file identity")
                    snapshot["ident"] = tuple(identity)
                    if isinstance(snapshot.get("updated"), list):
                        snapshot["updated"] = tuple(snapshot["updated"])
            exported = export_logs(
                **payload, cancel=cancel, use_osc52=False, use_tools=False
            )
            result = O.report(
                "log-export",
                exported.get("message", "Log export finished"),
                data=exported,
                status="ok"
                if exported.get("status") in {"ready", "partial"}
                else "error",
            )
        else:
            result = O._registry()[inputs["operation"]][0].apply(
                inputs["operation"], inputs["plan"], ctx
            )
    elif operation == "engineering-analysis":
        from ..engineering import service

        if (
            set(inputs) != {"domain", "action", "arguments"}
            or (inputs["domain"], inputs["action"]) not in service.COMPUTE_ACTIONS
        ):
            raise ValueError("Unknown bounded engineering analysis")
        result = service.execute(
            inputs["domain"], inputs["action"], inputs["arguments"], cancel=cancel
        )
    elif operation in FILE_OPERATIONS or operation in {
        "simulation-review",
        "scientific-inspection",
    }:
        expected = (
            {"operation", "params"}
            if operation == "scientific-inspection"
            else {"params"}
        )
        if set(inputs) != expected:
            raise ValueError("Artifact operation requires its declared parameters")
        from .. import operations as O

        operation_key = (
            inputs["operation"]
            if operation == "scientific-inspection"
            else "execution-simulation"
            if operation == "simulation-review"
            else operation
        )
        if operation == "scientific-inspection" and operation_key not in _PURE:
            raise ValueError("Unknown named scientific inspection")
        params = O.parameters(operation_key, inputs["params"])
        if operation in {
            "diagnostic-campaign",
            "minimize-reproducer",
            "regression-sweep",
        } and params.get("action") in {"status", "cancel"}:
            raise ValueError(
                "Driver status and cancellation belong to the original execution host"
            )
        ctx = O.Context(
            selected=context.get("selected", ""),
            scope=context.get("scope", {}),
            jobs=tuple(context.get("attempts", [])),
            cancel=cancel,
            state_dir=context.get("state_dir", ""),
            verified_execution=verified_execution or {},
        )
        result = O._registry()[operation_key][0].run(operation_key, params, ctx)
    elif operation == "planning":
        allowed = {"view", "source", "snap", "job", "observations", "overrides"}
        if set(inputs) - allowed or "view" not in inputs:
            raise ValueError("Invalid planning task fields")
        from ..planning import analyze

        result = analyze(
            inputs["view"],
            inputs.get("source"),
            cancel=cancel,
            **{
                key: inputs[key]
                for key in allowed - {"view", "source"}
                if key in inputs
            },
        )
    elif operation == "planning-command":
        if set(inputs) - {"command", "args", "snap", "job", "observations", "choices"}:
            raise ValueError("Invalid planning command fields")
        from .planning import result as planning_result

        result, options = planning_result(
            inputs["command"],
            inputs["args"],
            **{
                key: inputs[key]
                for key in ("snap", "job", "observations", "choices")
                if key in inputs
            },
        )
        result = {"output": result, "options": vars(options)}
    elif operation == "research-command":
        if set(inputs) != {"command", "args"} or inputs["command"] not in {
            "validate",
            "passport",
        }:
            raise ValueError("Unknown read-only research command")
        if inputs["command"] == "passport" and inputs["args"][:1] not in (
            ["show"],
            ["compare"],
        ):
            raise ValueError(
                "Passport capture is an artifact mutation and needs its separate review"
            )
        from ..operations import Context, read_json

        args = inputs["args"]
        if inputs["command"] == "validate":
            from ..artifacts import validate_contract

            if len(args) != 2:
                raise ValueError("validate requires CONTRACT ROOT")
            contract = read_json(
                Context(), os.path.expanduser(args[0]), limit=limits["input_bytes"]
            )
            result = validate_contract(
                contract,
                os.path.expanduser(args[1]),
                max_bytes=source_limit(limits["input_bytes"]),
                max_entries=128,
            )
            source_read(result["budget"]["bytes_read"])
            code = 0 if result.get("valid") else 1
        else:
            from ..provenance import load, diff

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
        if set(inputs) != {"operation", "params"} or inputs["operation"] not in {
            "trace-inspect",
            "causal-delay",
            "collective-audit",
        }:
            raise ValueError("Invalid trace task")
        from .. import operations as O, ops_trace

        ctx = O.Context(
            selected=context.get("selected", ""),
            scope=context.get("scope", {}),
            cancel=cancel,
        )
        result = ops_trace.run(
            inputs["operation"],
            O.parameters(inputs["operation"], inputs["params"]),
            ctx,
        )
    elif operation == "log-search":
        if set(inputs) != {"text", "needle"} or not all(
            isinstance(inputs[k], str) for k in inputs
        ):
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
                matches.append({"line": index + 1, "text": line})
        result = {
            "matches": matches,
            "coverage": "supplied text only; no whole-file claim",
        }
    elif operation == "log-search-files":
        if set(inputs) != {"sources", "query", "options"}:
            raise ValueError("Invalid file search fields")
        from ..job_logs import scan as log_scan
        from ..local_files import LocalFiles

        options = inputs["options"]
        if not isinstance(options, dict) or set(options) - {
            "regex",
            "case",
            "word",
            "context",
            "max_results",
        }:
            raise ValueError("Unknown file search options")
        sources = inputs["sources"]
        if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
            raise ValueError("Choose at most 32 explicit search sources")
        total = sum(
            log_scan.snapshot(LocalFiles(), source["path"])["size"]
            for source in sources
        )
        if total > limits["input_bytes"]:
            raise ValueError("File search exceeds its declared input byte budget")
        result = log_scan.search_sources(
            LocalFiles(),
            sources,
            inputs["query"],
            **options,
            max_bytes=limits["input_bytes"],
            cancel=cancel.is_set,
        )
    else:
        raise ValueError("No worker adapter for " + operation)
    cancel.check()
    receipt = {
        "schema": RESULT_SCHEMA,
        "operation": operation,
        "version": 1,
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "context_sha256": hashlib.sha256(ctx_raw).hexdigest(),
        "budget": limits,
        "result": result,
        "coverage": "Declared inputs only; source gaps and scientific limitations remain in the result.",
    }
    # Existing readers use tuples for identities/aliases. Wire results contain
    # plain JSON only and cannot retain mutable references to handler objects.
    return json.loads(_json(receipt, limits["output_bytes"], "Task result"))


def execute_operation(
    operation,
    inputs,
    context=None,
    budget=None,
    cancelled=None,
    verified_execution=None,
):
    if router_for().managed:
        raise AdmissionError(
            "Worker adapters execute only in the admitted worker process; use managed dispatch"
        )
    limits = budget_for(operation, budget)
    token = _SOURCE_BUDGET.set({"remaining": limits["input_bytes"], "files": 128})
    try:
        return _execute_operation(
            operation, inputs, context, limits, cancelled, verified_execution
        )
    finally:
        _SOURCE_BUDGET.reset(token)
