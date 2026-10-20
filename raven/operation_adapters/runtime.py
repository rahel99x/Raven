"""Optional runtime research views and reviewed cooperative application actions."""

from __future__ import annotations

import json
import os

from .. import diagnostic_recipes, execution_simulator, science_fields, science_files
from ..operation_support import (
    checkpoint,
    digest,
    local_only,
    prepare_plan,
    read_json,
    report,
    validate_plan,
)
from ..runtime_contracts import identifier, integer, number, object_, text, unique_names
from ..runtime_control import RuntimeControl


def _field(key, label, default="", required=False):
    return {"key": key, "label": label, "default": default, "required": required}


SPECIFICATIONS = [
    {
        "key": "execution-simulation",
        "title": "Execution scenario laboratory",
        "group": "Runtime",
        "proposal": "V6-SIM",
        "summary": "Replay a bounded fixed-workload resource DAG; show calibration and held-out prediction residuals.",
        "fields": [
            _field("manifest", "Execution model JSON", required=True),
            _field("scenario", "Scenario JSON (rates/placement)", "{}"),
            _field("destination", "New prediction-record directory (optional)"),
        ],
    },
    {
        "key": "diagnostic-recipe",
        "title": "Compile diagnostic intent",
        "group": "Runtime",
        "proposal": "V6-DIAG",
        "summary": "Choose compatible permitted collection recipes under declared evidence and resource constraints.",
        "fields": [
            _field("manifest", "Diagnostic intent JSON", required=True),
            _field("capabilities", "Site capability JSON", required=True),
        ],
    },
    {
        "key": "information-query",
        "title": "Evidence-preserving summaries",
        "group": "Runtime",
        "proposal": "V6-QUERY",
        "summary": "Compute declared count/sum/extrema summaries with missing values, coverage and raw references.",
        "fields": [
            _field("manifest", "Query source JSON", required=True),
            _field("query", "Summary query JSON", required=True),
        ],
    },
    {
        "key": "scientific-field",
        "title": "Scientific field and slices",
        "group": "Runtime",
        "proposal": "V6-FIELD",
        "summary": "Inspect bounded matrix/volume slices, extrema, gaps, units and distributions inside the terminal.",
        "fields": [
            _field("manifest", "Scientific field JSON", required=True),
            _field("axis", "Slice axis", "0"),
            _field("index", "Slice index", "0"),
            _field("bins", "Distribution bins", "16"),
        ],
    },
    {
        "key": "runtime-steering",
        "title": "Cooperative runtime steering",
        "group": "Runtime",
        "proposal": "V6-STEER",
        "summary": "Review a typed command bound to one exact attempt and parameter epoch; the application applies it at its own safe point.",
        "fields": [_field("manifest", "Runtime request JSON", required=True)],
    },
    {
        "key": "io-coordination",
        "title": "Cooperative I/O permits",
        "group": "Runtime",
        "proposal": "V6-IO",
        "summary": "Inspect or review a bounded group permit; the application reports arrivals and owns collective synchronization.",
        "fields": [_field("manifest", "I/O request JSON", required=True)],
    },
]


def _required(params, key):
    return text(params.get(key), key, 4096)


def _read(params, key, ctx):
    return read_json(ctx, _required(params, key))


def _parse_scenario(value):
    if not isinstance(value, str) or len(value) > 65536:
        raise ValueError("Scenario must be a bounded JSON object")

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate scenario key")
            result[key] = item
        return result

    try:
        result = json.loads(
            value or "{}",
            object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("Scenario numbers must be finite")
            ),
        )
        from ..planning_io import _bounded

        _bounded(result, 16)
        return result
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("Scenario JSON must be bounded and shallow") from exc


def _runtime_document(feature, params, ctx):
    local_only(ctx)
    path = science_files.path_at(os.getcwd(), _required(params, "manifest"))
    document, record = science_files.json_file(path, ctx.cancel)
    if feature == "runtime-steering":
        object_(
            document,
            {
                "schema",
                "mailbox",
                "attempt",
                "request_id",
                "command",
                "parameters",
                "expected_epoch",
                "ttl",
            },
        )
        if document["schema"] != "raven.runtime-request/v1":
            raise ValueError("Expected raven.runtime-request/v1")
    else:
        object_(
            document,
            {"schema", "mailbox", "attempt"},
            {"request_id", "participants", "lease_seconds", "wait_seconds", "fallback"},
        )
        if document["schema"] != "raven.io-request/v1":
            raise ValueError("Expected raven.io-request/v1")
        if "request_id" in document and set(document) != {
            "schema",
            "mailbox",
            "attempt",
            "request_id",
            "participants",
            "lease_seconds",
            "wait_seconds",
            "fallback",
        }:
            raise ValueError(
                "I/O permit creation requires all lease, deadline, participants and fallback fields"
            )
        if "request_id" not in document and set(document) != {
            "schema",
            "mailbox",
            "attempt",
        }:
            raise ValueError("I/O inspection accepts only mailbox and attempt")
    control = RuntimeControl(
        science_files.path_at(os.path.dirname(path), document["mailbox"]),
        document["attempt"],
    )
    status = control.status()
    if feature == "runtime-steering":
        identifier(document["request_id"], "request ID")
        identifier(document["command"], "command")
        integer(document["expected_epoch"], "expected parameter epoch", 0, 2**53)
        number(document["ttl"], "request TTL", 0.01, 3600)
    elif "request_id" in document:
        identifier(document["request_id"], "permit ID")
        unique_names(document["participants"], "participants", 4096)
        if not document["participants"]:
            raise ValueError("I/O permit requires participants")
        number(
            document["lease_seconds"],
            "lease duration",
            0.01,
            control.inspect_configuration()["io"]["max_lease_seconds"],
        )
        number(document["wait_seconds"], "maximum wait", 0, 3600)
        if not isinstance(document["fallback"], str) or document["fallback"] not in {
            "skip",
            "proceed",
        }:
            raise ValueError("I/O fallback must be skip or proceed")
    return document, record, control, status


def run(feature, params, ctx):
    checkpoint(ctx)
    if feature == "execution-simulation":
        result = execution_simulator.simulate(
            _read(params, "manifest", ctx),
            _parse_scenario(params.get("scenario", "{}")),
            cancel=ctx.cancel,
        )
        prediction = (
            f"{result['predicted_seconds']:.6g} s"
            if result["predicted_seconds"] is not None
            else "WITHDRAWN"
        )
        rows = [
            f"Baseline model: {result['baseline_seconds']:.6g} s; scenario prediction: {prediction}",
            f"Prediction: {result['prediction_status']}; calibration available: {result['calibrated']}; validation: {result['validation']['status']}",
            result["validation"]["reason"],
            f"Acceptance threshold: {result['validation']['acceptance_threshold']}; held-out observations: {result['validation']['held_out_count']}",
        ]
        rows.append(
            "Candidate directly observed in validation: "
            + (
                ", ".join(result["applicability"]["direct_validation_observations"])
                or "no"
            )
        )
        rows += [
            f"{item['id']} ({item['role']}): residual {item['residual_seconds']:.6g} s; relative error {item['relative_error']:.2%}"
            for item in result["residuals"]
        ]
        rows += [
            f"{item['id']}: {item['resource']} {item['start']:.6g}..{item['end']:.6g} model s"
            for item in result["timeline"][:512]
        ]
        plan = None
        if params.get("destination", "").strip():
            local_only(ctx)
            source, record = science_files.json_file(
                science_files.path_at(os.getcwd(), _required(params, "manifest")),
                ctx.cancel,
            )
            if digest(source) != result["model_digest"]:
                raise ValueError(
                    "Execution model changed during prediction; review again"
                )
            plan = prepare_plan(
                ctx,
                feature,
                params,
                {
                    "source": record,
                    "destination": science_files.destination(
                        _required(params, "destination")
                    ),
                    "prediction": result,
                },
            )
            rows.append(
                "Apply records the reviewed model, scenario, prediction and qualification state in a new directory."
            )
        return report(
            feature,
            "Scenario prediction withdrawn"
            if result["prediction_status"] == "withdrawn"
            else "Fixed-workload scenario prediction",
            status="ok" if result["validated"] else "warning",
            rows=rows,
            data=result,
            warnings=result["limitations"],
            plan=plan,
        )
    if feature == "diagnostic-recipe":
        result = diagnostic_recipes.compile_recipe(
            _read(params, "manifest", ctx),
            _read(params, "capabilities", ctx),
            cancel=ctx.cancel,
        )
        rows = [
            f"{key}: {value['observed']} ({'exact' if value['exact'] else 'explicit approximation'})"
            for key, value in result["coverage"].items()
        ]
        rows += [
            f"Adapter {adapter['id']}: {json.dumps(adapter['argv'])}"
            for adapter in result["adapters"]
        ]
        return report(
            feature,
            "Diagnostic recipe compiled; commands are not executed",
            rows=rows,
            data=result,
            warnings=result["limitations"],
        )
    if feature == "information-query":
        result = diagnostic_recipes.summarize_query(
            _read(params, "manifest", ctx), _read(params, "query", ctx)
        )
        return report(
            feature,
            "Declared summary with raw references",
            rows=[
                f"{key}: {json.dumps(value)}; missing {result['null_or_missing'][key]}"
                for key, value in result["summaries"].items()
            ],
            data=result,
            status="ok" if result["coverage"]["complete"] else "warning",
            warnings=[result["precision"], result["coverage"]["reason"]],
        )
    if feature == "scientific-field":
        result = science_fields.inspect_field(
            _read(params, "manifest", ctx),
            axis=int(params.get("axis", "0") or 0),
            index=int(params.get("index", "0") or 0),
            bins=int(params.get("bins", "16") or 16),
        )
        rows = [
            f"{result['identity']}: shape {result['shape']} [{result['units']}]",
            f"Selected: {json.dumps(result['selected'])}",
        ]
        rows += science_fields.terminal_rows(
            result, unicode=not ctx.cfg.get("ascii", False)
        )
        rows.append(
            "Distribution counts: " + " ".join(map(str, result["histogram"]["counts"]))
        )
        return report(
            feature,
            "Scientific field with preserved extrema and gaps",
            rows=rows,
            data=result,
            warnings=result["limitations"],
        )
    if feature not in {"runtime-steering", "io-coordination"}:
        raise ValueError("Unknown runtime operation")
    document, record, control, status = _runtime_document(feature, params, ctx)
    plan = None
    if feature == "runtime-steering":
        control._command(
            control.inspect_configuration(), document["command"], document["parameters"]
        )
        if document["expected_epoch"] != status["epochs"][document["attempt"]]:
            raise ValueError(
                "Parameter epoch changed; inspect the application's current state"
            )
    if feature == "runtime-steering" or "request_id" in document:
        plan = prepare_plan(
            ctx,
            feature,
            params,
            {
                "source": record,
                "document": document,
                "mailbox": str(control.directory),
                "mailbox_id": status["mailbox_id"],
                "configuration_digest": status["configuration_digest"],
            },
        )
    rows = [
        f"Attempt {document['attempt']}; parameter epoch {status['epochs'][document['attempt']]}."
    ]
    rows += [
        f"Command {row['id']}: {row['command']} — {row['status']}"
        for row in status["requests"]
    ]
    rows += [
        f"I/O {row['id']}: {row['state']}; {len(row['arrivals'])}/{len(row['participants'])} arrivals"
        for row in status["permits"]
    ]
    if plan:
        rows.append(
            "Apply queues the reviewed request. The application remains responsible for safe points and collective barriers."
        )
    return report(
        feature,
        "Cooperative application control",
        rows=rows,
        data=status,
        warnings=status["limitations"],
        plan=plan,
    )


def apply(feature, plan, ctx):
    if feature not in {"runtime-steering", "io-coordination", "execution-simulation"}:
        raise ValueError("This runtime operation is read-only")
    local_only(ctx)
    payload = validate_plan(plan, ctx, feature)
    science_files.verify(payload["source"], ctx.cancel)
    if feature == "execution-simulation":
        prediction = payload["prediction"]
        receipt = {
            "schema": "raven.execution-record/v1",
            "prediction_id": prediction["prediction_id"],
            "model_digest": prediction["model_digest"],
            "prediction_digest": digest(prediction),
            "validation_revision": prediction["validation_revision"],
            "prediction_status": prediction["prediction_status"],
            "validation": prediction["validation"],
        }
        destination = science_files.publish(
            payload["destination"],
            files=[
                (
                    "prediction.json",
                    json.dumps(
                        prediction, sort_keys=True, indent=2, allow_nan=False
                    ).encode(),
                    False,
                ),
                (
                    "scenario.json",
                    json.dumps(
                        prediction["scenario"],
                        sort_keys=True,
                        indent=2,
                        allow_nan=False,
                    ).encode(),
                    False,
                ),
            ],
            copies=[(payload["source"], "model.json")],
            receipt=receipt,
            cancel=ctx.cancel,
            final_check=lambda: science_files.verify(payload["source"], ctx.cancel),
        )
        return report(
            feature,
            "Prediction record published",
            rows=[destination, "Prediction state: " + prediction["prediction_status"]],
            data={"destination": destination, "receipt": receipt},
            status="ok" if prediction["validated"] else "warning",
        )
    document = payload["document"]
    control = RuntimeControl(payload["mailbox"], document["attempt"])
    status = control.status()
    if (
        status["mailbox_id"] != payload["mailbox_id"]
        or status["configuration_digest"] != payload["configuration_digest"]
    ):
        raise ValueError(
            "Application mailbox identity or configuration changed since review"
        )
    checkpoint(ctx)
    if feature == "runtime-steering":
        if status["epochs"][document["attempt"]] != document["expected_epoch"]:
            raise ValueError("Application parameter epoch changed since review")
        result = control.submit(
            document["request_id"],
            document["command"],
            document["parameters"],
            expected_epoch=document["expected_epoch"],
            ttl=document["ttl"],
        )
    else:
        result = control.permit(
            document["request_id"],
            participants=document["participants"],
            arrived=[],
            lease_seconds=document["lease_seconds"],
            wait_seconds=document["wait_seconds"],
            fallback=document["fallback"],
        )
    return report(
        feature,
        "Request durably queued; inspect its application acknowledgment",
        rows=[json.dumps(result, sort_keys=True)],
        data=result,
    )


# Managed Raven entry points also protect direct adapter callers.
from ..operation_support import (
    managed_run as _managed_run,
    managed_apply as _managed_apply,
)

run = _managed_run(run)
apply = _managed_apply(apply)
