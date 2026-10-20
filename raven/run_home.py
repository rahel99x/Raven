"""Bounded, read-only evidence for one immutable run attempt.

A page keeps its identity when queue membership changes. Missing recordings,
an unavailable scheduler, or reconnecting never imply completion or submission.
"""
from __future__ import annotations

import copy
import shlex

from . import projects


def identity(binding):
    """Stable across queued/running/finished transitions, distinct across attempts."""
    launch = binding.get("launch") or {}
    automation = binding.get("automation_identity") or {}
    return (binding.get("project_root", ""), binding.get("run_id", ""),
            binding.get("attempt"), binding.get("job_id"),
            launch.get("cluster", ""), launch.get("submission_token") or automation.get("token", ""))


def load(binding, *, files=None):
    """Revalidate the exact inventory, then read independent optional sources."""
    selected = projects.select_run(binding["project_root"], binding["run_id"], files=files)
    current = selected["binding"]
    # A receipt may precede its first inventory; it carries only root/run/token.
    for index, expected in enumerate(identity(binding)):
        if expected not in (None, "") and identity(current)[index] != expected:
            raise ValueError("This path now identifies a different run attempt. Reconnect to the original project; nothing was submitted.")
    for key in ("project_id", "experiment_id"):
        if binding.get(key) not in (None, "") and binding[key] != current.get(key):
            raise ValueError("The retained project or experiment identity changed; this run was not rebound.")
    filesystem = (binding.get("automation_identity") or {}).get("filesystem_id")
    if filesystem and filesystem != (current.get("automation_identity") or {}).get("filesystem_id"):
        raise ValueError("The recorded project filesystem identity changed; reconnect its original filesystem.")
    result = dict(selected=selected, report=None, errors={}, telemetry={}, inventory=selected["run"]["inventory"])
    if current.get("submit_file"):
        try:
            from .launching.results import validate
            result["report"] = validate(projects.read_bound_json(current, current["submit_file"],
                key="submit", files=files), current)
        except (OSError, ValueError) as exc:
            result["errors"]["launch report"] = str(exc)
    if current.get("telemetry_index"):
        try:
            data = projects.read_bound_telemetry(current, files=files, max_bytes=256 << 10)
            for source in data["index"]["sources"]:
                family = source["family"]
                result["telemetry"][family] = dict(status=source.get("status", "unknown"),
                    samples=len(data.get(family, ())), reason=source.get("reason", ""),
                    error=data.get("errors", {}).get(family, ""))
            result["errors"].update(data.get("errors", {}))
        except (OSError, ValueError) as exc:
            result["errors"]["recordings"] = str(exc)
    return result


def relaunch_draft(report):
    """Copy a validated retained request into the ordinary reviewed launch flow."""
    plan = report["plan"]
    return dict(project=plan["project_root"], script=plan["script"],
        arguments=shlex.join(plan.get("script_args", [])), execution=plan["execution"],
        recipe="", recipe_name="run-again", account="", partition="", qos="", cpus="",
        gpus="", memory="", time="", gpu=None, cpu_memory=None, basic_research=False,
        captured_plan=copy.deepcopy(plan), submit_dir=plan["submit_dir"])


def receipt_binding(receipt):
    return dict(project_root=receipt["project_root"], run_id=receipt["run_id"],
                job_id=receipt.get("job_id"), attempt=receipt.get("attempt"),
                launch=dict(submission_token=receipt.get("token", ""), cluster=receipt.get("cluster", "")))
