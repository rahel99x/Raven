"""Read one declared sample attempt with existing confined, bounded readers."""
from __future__ import annotations

import os
import shlex

from . import operations as O, projects


def inspect_sample(root, run_id, ctx):
    selected = projects.select_run(root, run_id, files=ctx.files)
    binding = selected["binding"]
    inventory = selected["run"]["inventory"]
    paths = inventory.get("paths", {})
    checks = [{"source": "inventory", "status": "ready", "detail": "Exact run, attempt and declared path structure validated."}]

    def check(source, callback):
        O.checkpoint(ctx)
        try:
            checks.append(dict(source=source, **callback()))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            O.checkpoint(ctx)
            checks.append({"source": source, "status": "missing" if isinstance(exc, FileNotFoundError) else "invalid", "detail": O.clean(exc, 480)})

    if "metrics" in paths:
        def metrics():
            from .metrics import MetricReader
            result = MetricReader(ctx.files, max_bytes=262144, max_points=128, max_streams=1).read_confined(
                os.path.join(binding["run_root"], paths["metrics"]), binding)
            errors = result.get("errors", [])
            state = result["status"]
            status = "invalid" if errors and state != "missing" else "missing" if state == "missing" else "partial" if result["truncated"] or state != "ok" else "ready"
            return {"status": status, "detail": f"{result['records']} complete records; {len(result['series'])} metric names. " + "; ".join(errors[:4]),
                    "coverage": "One bounded 256 KiB read; at most 128 retained points per series. Incomplete final lines await newline; older data can be outside this sample."}
        check("metrics", metrics)
    else:
        checks.append({"source": "metrics", "status": "not-declared", "detail": "Declare paths.metrics and write complete metric JSONL records."})

    if "summary" in paths:
        def summary():
            from .reporting import _summary, TERMINAL
            value = _summary(projects.read_bound_json(binding, os.path.join(binding["run_root"], paths["summary"]),
                                                      key="summary", max_bytes=262144, files=ctx.files))
            if value["id"] != binding["run_id"]:
                raise ValueError("Summary id does not match the selected run.")
            for field in ("job_id", "attempt", "experiment_id", "project_id"):
                if value.get(field) is not None and binding.get(field) is not None and value[field] != binding[field]:
                    raise ValueError("Summary " + field + " does not match the selected attempt.")
            if inventory["state"] in TERMINAL and value["state"] != inventory["state"]:
                raise ValueError("Summary terminal state differs from the selected inventory.")
            return {"status": "ready", "detail": "Terminal summary content and declared attempt identity validated."}
        check("summary", summary)
    else:
        checks.append({"source": "summary", "status": "not-declared", "detail": "Declare paths.summary; tower report finish writes the actual terminal result."})

    if binding.get("contract"):
        def artifacts():
            result = projects.read_bound_artifacts(binding, files=ctx.files)
            return {"status": "ready" if result.get("valid") is True else "partial" if result.get("status") == "incomplete" else "invalid", "detail": O.clean(result.get("summary") or result.get("status") or "Declared output checks completed.", 480),
                    "coverage": "Native output validation: at most 256 entries and 8 MiB of content.",
                    "result": result}
        check("artifacts", artifacts)
    else:
        checks.append({"source": "artifacts", "status": "not-declared", "detail": "Declare an output contract to validate output contents; a directory path alone does not validate results."})

    for entry in selected.get("logs", [])[:2]:
        def log(entry=entry):
            data, size, _ = projects.read_bound_tail(binding, entry, 16384, files=ctx.files)
            return {"status": "ready", "detail": f"Read {len(data)} of {size} log bytes; content remains in Logs.", "coverage": "Bounded 16 KiB tail; no log semantics inferred."}
        check("log:" + str(entry.get("id", "declared")), log)
    O.checkpoint(ctx)
    return {"run_id": run_id, "job_id": binding.get("job_id"), "attempt": binding.get("attempt"),
            "cluster": binding.get("launch", {}).get("cluster", ""), "checks": checks,
            "warnings": selected.get("warnings", []),
            "status": "warning" if selected.get("warnings") or any(item["status"] != "ready" for item in checks) else "ok",
            "scope": "One explicit sample, not complete project certification. File presence and structural validity do not prove scientific correctness."}


def hooks(root, *, script="batch.sh", experiment="experiment"):
    projects.relative_path(script)
    projects._text(experiment, "experiment", 128)
    root_arg, script_arg, experiment_arg = map(shlex.quote, (root, script, experiment))
    batch = ["#!/usr/bin/env bash", "set -euo pipefail",
             f"export TOWER_PROJECT_ROOT={root_arg}", f"export TOWER_EXPERIMENT={experiment_arg}",
             f"export TOWER_SCRIPT={script_arg}",
             'if [ "$#" -eq 0 ]; then printf "%s\\n" "Pass the actual program and arguments to this batch script." >&2; exit 64; fi',
             'exec tower report exec --root "$TOWER_PROJECT_ROOT" --experiment "$TOWER_EXPERIMENT" --script "$TOWER_SCRIPT" -- "$@"']
    metric = ['# Inside the application wrapper launched by report exec:',
              'tower report metric --run "${TOWER_RUN_DIR:?}" --value "loss=${LOSS:?set the actual measured loss}" --step "${STEP:?set the actual step}"',
              '# Declare an actual output only after your application writes it:',
              'tower report artifact outputs/result.json --run "${TOWER_RUN_DIR:?}" --format json']
    manual = ["# Alternative for an existing launcher that owns its lifecycle and traps:",
              f'TOWER_RUN_DIR=$(tower report start --root {root_arg} --experiment {experiment_arg} --script {script_arg})',
              "export TOWER_RUN_DIR", "# Run your actual payload and preserve its exit status; do not also use report exec.",
              'tower report finish --run "$TOWER_RUN_DIR" --exit-code "${TOWER_PAYLOAD_STATUS:?set the actual payload exit status}"']
    return {"batch": batch, "metrics": metric, "manual": manual,
            "notes": ["Save the batch hook at the declared script path, then pass the actual program argv through your existing submission procedure.",
                      "Run hooks inside an allocation with Slurm identity. Arrays and restarts use the reporting command's exact identity rules.",
                      "report exec records start, logs, measured wall time and finish; do not add duplicate start/finish hooks.",
                      "Choose metric names and scientific values from your application; the guide never invents samples or submits jobs."]}
