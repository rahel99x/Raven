"""Read-only presentation of one captured launch and its exact run receipt."""
from __future__ import annotations

import shlex

from . import reporting as R

SCHEMA = "raven.launch-report/v1"


def validate(document, binding):
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ValueError("unsupported retained launch report")
    R._encoded(document)
    plan, receipt = document.get("plan"), document.get("receipt")
    if not isinstance(plan, dict) or plan.get("schema") != "raven.launch-plan/v1" or not isinstance(receipt, dict):
        raise ValueError("launch report requires a captured plan and exact receipt")
    for key in ("run_id", "job_id", "attempt"):
        if receipt.get(key) != binding.get(key):
            raise ValueError("launch report belongs to a different run attempt")
    launch = binding.get("launch", {})
    automation = binding.get("automation_identity", {})
    token = launch.get("submission_token") or automation.get("token")
    if not token or receipt.get("token") != token or plan.get("token") != token:
        raise ValueError("launch report submission token does not match the selected attempt")
    if receipt.get("cluster", "") != launch.get("cluster", ""):
        raise ValueError("launch report belongs to another cluster")
    if plan.get("project_root") != binding.get("project_root"):
        raise ValueError("launch report belongs to another project filesystem")
    if plan.get("script_sha256") != binding.get("scientific_query", {}).get("script_sha256"):
        raise ValueError("launch report source digest differs from the execution record")
    for key in ("script_args", "sbatch_options"):
        values = plan.get(key, [])
        if not isinstance(values, list) or len(values) > 4096 or any(not isinstance(value, str) for value in values):
            raise ValueError("launch report contains invalid argument boundaries")
    rows = plan.get("readiness", [])
    if not isinstance(rows, list) or len(rows) > 32 or any(not isinstance(row, dict) for row in rows):
        raise ValueError("launch report contains invalid recording readiness")
    return document


def rows(document, width, *, ascii_=False):
    """Small retained document; no source IO or scheduler side effects."""
    from .research import clean
    plan, receipt = document["plan"], document["receipt"]
    output = []
    def add(text, style=""):
        value = clean(text, ascii_)
        size = max(1, width - 2)
        for offset in range(0, max(1, len(value)), size):
            output.append([(" " + value[offset:offset + size], style)])
    add("Run / Results · Captured launch", "cyan+bold")
    add("Run " + str(receipt["run_id"]) + " · attempt " + str(receipt["attempt"]))
    add("Job " + str(receipt.get("job_id") or "local execution") + " · cluster " + (receipt.get("cluster") or "not supplied"))
    add("This is the recorded execution. Viewing it does not submit another job.", "dim")
    add("Script: " + str(plan.get("script", "")))
    add("Job's project folder: " + str(plan.get("project_root", "")))
    add("Working directory: " + str(plan.get("workdir", "")))
    add("Execution: " + str(plan.get("execution", "")))
    add("Application exit: " + str(receipt.get("application_exit_code", "not recorded")))
    add("Scheduler outcome: " + str(receipt.get("scheduler_outcome", "not yet observed")))
    if plan.get("script_args"):
        add("Application arguments: " + shlex.join(plan["script_args"]), "dim")
    if plan.get("sbatch_options"):
        add("Explicit sbatch options: " + shlex.join(plan["sbatch_options"]), "dim")
    for key, value in plan.get("effective_options", {}).items():
        source = plan.get("option_sources", {}).get(key, "unspecified")
        add(str(key) + ": " + str(value) + " (" + str(source) + ")")
    add("Recording readiness at launch", "bold")
    for item in plan.get("readiness", []):
        status = item.get("status", "unknown")
        add(str(item.get("label", item.get("feature", "Recording"))) + ": " + str(status).replace("-", " ")
            + " · " + str(item.get("reason", "")), "yellow" if status in {"needs-input", "unsupported", "failed", "partial"} else "dim")
    add("Readiness describes the launch request. Retained telemetry and export coverage describe what was actually recorded.", "dim")
    return output
