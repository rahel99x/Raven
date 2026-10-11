"""Scientific investigation views and reviewed bounded diagnostic execution."""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

from . import diagnostic_trials as trials
from . import investigation_planner, science_contracts, science_diagnostics, science_files, scientific_lineage
from . import operations as O


def _field(key, label, default="", required=False, choices=None):
    value = {"key": key, "label": label, "default": default, "required": required}
    if choices:
        value["choices"] = choices
    return value


_READERS = {
    "recommendation": ("Recommendation validity", "Inspect pinned evidence, applicability, expiry and measured scientific outcome.", science_contracts.evaluate),
    "investigation-plan": ("Next investigation action", "Rank declared information actions within explicit time, resource and scientific-risk budgets.", investigation_planner.plan),
    "scientific-impact": ("Scientific impact and revalidation", "Trace affected claims and propose dependency-aware checks; missing lineage remains unknown.", scientific_lineage.analyze),
    "numerical-stability": ("Numerical stability laboratory", "Analyze measured configuration repeats, tolerance violations and missing measurements.", science_diagnostics.numerical),
    "regression-investigation": ("Performance regression investigation", "Compare ordered versions with scientific checks, noise and multiple comparisons accounted for.", science_diagnostics.regression),
}

SPECIFICATIONS = [
    {"key": "investigation-workflow", "title": "Staged job investigation", "group": "Investigation", "proposal": "Consolidation",
     "summary": "Join evidence, findings, missing information, diagnostic planning, reviewed execution and updated measurements.",
     "fields": [_field("manifest", "Workflow manifest", required=True),
                _field("action", "Stage", "inspect", choices=["inspect", "review-diagnostic", "status"]),
                _field("diagnostic_id", "Diagnostic ID (review or status)"),
                _field("destination", "Diagnostic workspace (new for review)")]},
] + [
    {"key": key, "title": title, "group": "Investigation", "proposal": "Flagship",
     "summary": summary, "fields": [_field("manifest", "Investigation manifest", required=True)]}
    for key, (title, summary, _) in _READERS.items()
] + [
    {"key": key, "title": title, "group": "Investigation", "proposal": "Flagship",
     "summary": summary,
     "fields": [_field("manifest", "Diagnostic recipe (required for launch)"),
                _field("destination", "Diagnostic workspace (new for launch)", required=True),
                _field("action", "Action", "inspect", choices=["inspect", "launch", "status", "cancel"])]}
    for key, title, summary in (
        ("diagnostic-campaign", "Run diagnostic campaign", "Run explicit configurations under one reviewed Slurm allocation or explicit local diagnostic limits."),
        ("minimize-reproducer", "Minimize reproducer", "Reduce a declared input by repeated failure-oracle trials with durable budgets and exact source hashes."),
        ("regression-sweep", "Run Git regression sweep", "Measure immutable Git snapshots with repeated scientific checks and noise-aware bootstrap comparisons."))
]

_CHILDREN = {}
_CHILD_LOCK = threading.Lock()


def _reap():
    with _CHILD_LOCK:
        for pid, child in list(_CHILDREN.items()):
            if child.poll() is not None:
                del _CHILDREN[pid]


def _process_matches(record):
    try:
        pid = science_contracts.integer(record["pid"], "driver pid", 1, 2147483647)
        if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != record["boot"]:
            return False
        process = Path(f"/proc/{pid}")
        if process.stat().st_uid != os.geteuid():
            return False
        data = (process/"stat").read_text()
        fields = data[data.rfind(")")+2:].split()
        return fields[19] == record["start"] and fields[0] != "Z"
    except (OSError, ValueError, KeyError, IndexError):
        return False


def _status(feature, params, ctx):
    O.local_only(ctx)
    _reap()
    root = Path(params.get("destination", "")).expanduser().absolute()
    if not params.get("destination") or root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.geteuid():
        raise ValueError("Choose this user's existing private diagnostic workspace")
    receipt, receipt_source = science_files.json_file(str(root/".tower-receipt.json"), ctx.cancel)
    if not isinstance(receipt, dict) or receipt.get("scope") != ctx.scope or receipt.get("schema") != "tower.diagnostic-workspace/v1":
        raise ValueError("Diagnostic workspace belongs to another connection")
    recipe, recipe_source = science_files.json_file(str(root/"recipe.json"), ctx.cancel)
    trials.validate(recipe)
    if recipe["kind"] != {"diagnostic-campaign": "campaign", "minimize-reproducer": "minimize", "regression-sweep": "regression"}[feature]:
        raise ValueError("Choose the operation matching this diagnostic workspace")
    process, process_source = None, None
    try:
        process, process_source = science_files.json_file(str(root/"driver.process.json"), ctx.cancel)
        science_contracts.obj(process, {"pid", "start", "boot", "recipe_sha256"}, label="driver identity")
        if process.get("recipe_sha256") != trials._hash(recipe):
            raise ValueError("Driver belongs to a different recipe")
    except FileNotFoundError:
        pass
    running = process is not None and _process_matches(process)
    result = None
    for name in ("result.json", "error.json", "submission.result.json"):
        try:
            result, _ = science_files.json_file(str(root/name), ctx.cancel)
            if not isinstance(result, dict):
                raise ValueError("Diagnostic result must be an object")
            if name == "result.json" and result.get("recipe_sha256") != trials._hash(recipe):
                raise ValueError("Diagnostic result belongs to a different recipe")
            break
        except FileNotFoundError:
            pass
    state = str(result.get("state", "unknown")) if result else "running" if running else "interrupted" if process else "starting-or-unknown"
    return {"workspace": str(root), "recipe_sha256": trials._hash(recipe), "mode": recipe["mode"],
            "state": state, "running": running, "process": process, "result": result,
            "workflow_binding": receipt.get("workflow_binding"),
            "cancel_requested": (root/"cancel.intent.json").exists(),
            "sources": [receipt_source, recipe_source, *([process_source] if process_source else [])]}


def _launch_local(recipe, root, ctx):
    """Detach the driver so Single-mode background sampling remains available."""
    _reap()
    O.checkpoint(ctx)
    with _CHILD_LOCK:
        if len(_CHILDREN) >= 32:
            raise ValueError("At most 32 local diagnostic drivers may be active in this Tower process")
        fd = os.open(root/"driver.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            execution, _ = science_files.json_file(str(root/"execution.json"), ctx.cancel)
            child = subprocess.Popen([execution["python"]["path"], "-I", "-S", str(root/"driver.py"), str(root/"recipe.json"), str(root)],
                                     stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, start_new_session=True)
        finally:
            os.close(fd)
        _CHILDREN[child.pid] = child
    result = {"state": "started", "pid": child.pid, "workspace": str(root),
              "status": "Use action=status to read durable progress; leaving Tower does not abandon or repeat the run."}
    O.atomic_json(root/"spawn.json", result)
    return result


def _load(params, ctx):
    value = params.get("manifest")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Choose an explicit investigation manifest")
    O.checkpoint(ctx)
    document = O.read_json(ctx, value)
    return document


def _prepare(feature, params, ctx):
    O.local_only(ctx)
    path = science_files.path_at(os.getcwd(), params["manifest"])
    recipe, record = science_files.json_file(path, ctx.cancel)
    trials.validate(recipe)
    wanted = {"minimize-reproducer": "minimize", "diagnostic-campaign": "campaign", "regression-sweep": "regression"}[feature]
    if recipe["kind"] != wanted:
        raise ValueError("Recipe kind does not match this operation")
    if params.get("action", "inspect") not in {"inspect", "launch"}:
        raise ValueError("Diagnostic action must be inspect or launch")
    destination = science_files.destination(params.get("destination", ""))
    if not params.get("destination"):
        raise ValueError("Choose a new private diagnostic workspace")
    sources = [record]
    copies = []
    total_size = 0
    for entry in recipe["files"]:
        source, _ = science_files.snapshot(science_files.path_at(Path(path).parent, entry["path"]),
                                          cancel=ctx.cancel, limit=8 << 20)
        if source["sha256"] != entry["sha256"]:
            raise ValueError("Declared diagnostic source does not match its SHA256")
        total_size += source["size"]
        if total_size > 32 << 20:
            raise ValueError("Total diagnostic source data exceeds 32 MiB")
        sources.append(source)
        copies.append({"record": source, "target": "sources/" + entry["name"]})
    executable = str(Path(recipe["command"][0]).resolve(strict=True))
    if not os.access(executable, os.X_OK):
        raise ValueError("Diagnostic executable is not executable")
    executable_record, _ = science_files.snapshot(executable, cancel=ctx.cancel, limit=64 << 20)
    sources.append(executable_record)
    driver_record, driver = science_files.snapshot(str(Path(trials.__file__).resolve()), cancel=ctx.cancel, limit=1 << 20, keep=True)
    sources.append(driver_record)
    driver_python = str(Path(sys.executable).resolve())
    python_record, _ = science_files.snapshot(driver_python, cancel=ctx.cancel, limit=64 << 20)
    sources.append(python_record)
    revisions, repository_path = {}, None
    if wanted == "regression":
        repository_path = science_files.path_at(Path(path).parent, recipe["repository"]["path"])
        git = shutil.which("git")
        if not git:
            raise ValueError("Git is required to prepare immutable regression snapshots")
        git_record, _ = science_files.snapshot(str(Path(git).resolve()), cancel=ctx.cancel, limit=64 << 20)
        sources.append(git_record)
        for revision in recipe["repository"]["commits"]:
            O.checkpoint(ctx)
            archive = trials.archive_bytes(repository_path, revision["commit"], cancel=ctx.cancel)
            revisions[revision["id"]] = {"commit": revision["commit"], "sha256": hashlib.sha256(archive).hexdigest(), "size": len(archive)}
    limits = recipe["limits"]
    script = ["#!/bin/sh", "#SBATCH --nodes=1", "#SBATCH --ntasks=1", f"#SBATCH --cpus-per-task={limits['cpus']}",
              f"#SBATCH --mem={limits['memory_mb']}M", f"#SBATCH --time={max(1, math.ceil(limits['total_seconds']/60)+1)}",
              "#SBATCH --job-name=tower-diagnostic", "set -eu",
              "exec " + shlex.join([driver_python, "-I", "-S", str(Path(destination)/"driver.py"),
                                     str(Path(destination)/"recipe.json"), destination])]
    for field in ("partition", "account"):
        if field in recipe:
            script.insert(2, f"#SBATCH --{field}={recipe[field]}")
    return {"recipe": recipe, "manifest": path, "sources": sources, "copies": copies,
            "destination": destination, "driver": driver.decode("utf-8"), "script": "\n".join(script)+"\n",
            "execution": {"executable": executable_record, "python": python_record, "revisions": revisions},
            "repository_path": repository_path,
            "scope": ctx.scope}


def _rows(feature, data):
    if feature == "recommendation":
        return [f"{data['contract_id']}: {data['state']}", data["intervention"], *data["reasons"],
                *[f"Precondition {r['query']['source']}: {r['state']}" for r in data["preconditions"]],
                *[f"Outcome {r['query']['source']}: {r['state']}" for r in data["acceptance"]], data["limit"]]
    if feature == "investigation-plan":
        return [data["method"], *[f"{r['id']} [{r['kind']}]: score {r['score']:.6g}; expected {r['total_seconds']:.3g}s" for r in data["selected"]],
                *[f"Excluded {r['id']}: {r['reason']}" for r in data["excluded"]], *data["limits"]]
    if feature == "scientific-impact":
        return [f"Affected {len(data['affected'])}; uncertain {len(data['uncertain'])}; unresolved claims {len(data['unresolved_claims'])}",
                *[f"Review {r['id']} ({r['method']}): cost {r['cost']}; covers {', '.join(r['covers'])}" for r in data["proposed_actions"]],
                *[f"Unresolved: {item}" for item in data["unresolved_claims"]], data["limit"]]
    if feature == "numerical-stability":
        return [f"Observed checks: {data['state']}", *[f"{r['configuration']} / {r['metric']}: {r['state']}; n={r['n']}; max error={r['max_absolute_error']}; missing={len(r['missing'])}" for r in data["measurements"]], data["limit"]]
    return [data["method"], *[f"{r['version']}: {r['state']}; difference={r['difference']:.6g}; interval={r['interval']}" for r in data["comparisons"]],
            f"Nonmonotonic evidence: {data['nonmonotonic']}", data["limit"]]


def run(feature, params, ctx):
    if feature == "investigation-workflow":
        from .investigation_workflow import run as workflow
        return workflow(params, ctx)
    if feature in _READERS:
        from .investigation_workflow import analyze
        data = analyze(feature, _load(params, ctx), ctx)
        O.checkpoint(ctx)
        warning = data.get("state") in {"failed", "unknown", "inconclusive", "expired", "stale", "refuted", "inapplicable"} or bool(data.get("unresolved_claims"))
        return O.report(feature, _READERS[feature][0], status="warning" if warning else "ok", rows=_rows(feature, data), data=data)
    if feature not in {"diagnostic-campaign", "minimize-reproducer", "regression-sweep"}:
        raise ValueError("Unknown investigation feature")
    if params.get("action") in {"status", "cancel"}:
        status = _status(feature, params, ctx)
        plan = None
        if params["action"] == "cancel":
            if status["mode"] != "local-diagnostic":
                raise ValueError("Cancel a Slurm diagnostic allocation through its reviewed Jobs action")
            if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
                raise ValueError("Safe local cancellation requires Linux pidfd support")
            if status["running"] and not status["result"] and not status["cancel_requested"]:
                plan = O.prepare_plan(ctx, feature, params, {"action": "cancel", "status": status})
        result_rows = []
        outcome = status.get("result") or {}
        for row in outcome.get("comparisons", []):
            result_rows.append(f"{row['id']}: {row['state']}; difference={row['difference']}; interval={row['interval']}")
        for row in outcome.get("cases", []):
            result_rows.append(f"{row['id']}: {row['state']}")
        if "reduced_bytes" in outcome:
            result_rows.append(f"Input bytes: {outcome['original_bytes']} -> {outcome['reduced_bytes']}; 1-minimal={outcome['one_minimal']}")
        return O.report(feature, "Diagnostic status: " + status["state"],
                        status="warning" if status["state"] in {"interrupted", "starting-or-unknown", "partial", "inconclusive"} else "ok",
                        rows=["Workspace: " + status["workspace"], "State: " + status["state"],
                              "Local driver identity: " + ("verified running" if status["running"] else "not running on this host"),
                              "Cancellation already requested" if status["cancel_requested"] else "No cancellation request",
                              *result_rows,
                              "Uncertain launches must be reconciled; they are never automatically retried."], data=status, plan=plan)
    payload = _prepare(feature, params, ctx)
    recipe, limits = payload["recipe"], payload["recipe"]["limits"]
    plan = O.prepare_plan(ctx, feature, params, payload) if params.get("action") == "launch" else None
    return O.report(feature, "Diagnostic execution review" if plan else "Diagnostic recipe inspected",
                    rows=[f"Mode: {recipe['mode']}; kind: {recipe['kind']}",
                          f"At most {limits['max_trials']} trials / {limits['total_seconds']}s, {limits['cpus']} CPUs, {limits['memory_mb']} MiB",
                          f"Per trial: {limits['trial_seconds']}s, {limits['output_bytes']} output bytes; repeated oracle {limits['repeats']} times",
                          "Exact exit code AND output signature define the oracle; different crashes are not matches.",
                          *( [f"Git snapshots: {len(recipe['repository']['commits'])}; metric {recipe['repository']['metric']}; original checkout is not changed."] if recipe["kind"] == "regression" else []),
                          "Workspace: " + payload["destination"], "Slurm mode submits one allocation; local-diagnostic explicitly runs on the Tower host."],
                    warnings=["Trusted user code executes with your permissions. Resource limits and a private working directory are not a security sandbox.",
                              "The staged workspace and selected Python/executable paths must be available on the compute node.",
                              "A crash or uncertain submission leaves a durable intent; Tower never automatically repeats it."],
                    data={"recipe": recipe, "source_identities": payload["sources"]}, plan=plan)


def apply(feature, plan, ctx):
    if feature == "investigation-workflow":
        from .investigation_workflow import apply as workflow
        return workflow(plan, ctx)
    if feature not in {"diagnostic-campaign", "minimize-reproducer", "regression-sweep"}:
        raise ValueError("This investigation is read-only")
    O.local_only(ctx)
    payload = O.validate_plan(plan, ctx, feature)
    if payload.get("action") == "cancel":
        previous = payload["status"]
        for source in previous["sources"]:
            science_files.verify(source, ctx.cancel)
        fresh = _status(feature, plan["params"], ctx)
        if fresh["process"] != previous["process"] or fresh["recipe_sha256"] != previous["recipe_sha256"] or not fresh["running"] or fresh["result"]:
            raise ValueError("The reviewed driver is no longer the same active process")
        process = fresh["process"]
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise ValueError("Safe local cancellation requires Linux pidfd support")
        descriptor = os.pidfd_open(process["pid"])
        try:
            # Validate after opening: the pidfd binds this exact task even if the numeric PID is reused.
            if not _process_matches(process):
                raise ValueError("Driver exited or changed before cancellation")
            O.atomic_json(Path(fresh["workspace"])/"cancel.intent.json", {"process": process, "plan_digest": plan["digest"], "created": time.time()})
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        finally:
            os.close(descriptor)
        return O.report(feature, "Diagnostic cancellation requested", rows=["The verified driver will stop its current process group and write partial outcomes.",
                         "Read action=status to confirm completion; a request is not a completed cancellation."], data={"workspace": fresh["workspace"], "process": process, "state": "cancel-requested"})
    for source in payload["sources"]:
        science_files.verify(source, ctx.cancel)
    # Recreate and compare the complete reviewed recipe without performing a mutation.
    if O.digest(_prepare(feature, plan["params"], ctx)) != O.digest(payload):
        raise ValueError("Diagnostic evidence or execution paths changed; inspect again")
    recipe = payload["recipe"]
    root = Path(payload["destination"])
    archives = []
    for name, record in payload["execution"]["revisions"].items():
        data = trials.archive_bytes(payload["repository_path"], record["commit"], cancel=ctx.cancel)
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError("Reviewed Git archive changed before publication")
        archives.append(("revisions/"+name+".tar", data, False))
    science_files.publish(str(root), files=[("driver.py", payload["driver"].encode(), True),
                                          ("recipe.json", O.canonical(recipe)+b"\n", False),
                                          ("execution.json", O.canonical(payload["execution"])+b"\n", False),
                                          ("diagnostic.sbatch", payload["script"].encode(), True), *archives],
                          copies=[(item["record"], item["target"]) for item in payload["copies"]],
                          receipt={"schema": "tower.diagnostic-workspace/v1", "plan_digest": plan["digest"], "scope": ctx.scope,
                                   **({"workflow_binding": plan["workflow_binding"]} if "workflow_binding" in plan else {})},
                          cancel=ctx.cancel)
    O.atomic_json(root/"launch.intent.json", {"schema": "tower.diagnostic-launch/v1", "state": "started", "plan_digest": plan["digest"], "scope": ctx.scope, "created": time.time()})
    if recipe["mode"] == "local-diagnostic":
        result = _launch_local(recipe, root, ctx)
    else:
        from . import submission
        generated = submission.prepare(root/"diagnostic.sbatch", workdir=root)
        if not generated["valid"]:
            raise ValueError("Generated diagnostic allocation failed native submission checks")
        O.checkpoint(ctx)
        try:
            result = submission.submit(generated, ctx.slurm, root/"passports")
            result = {key: result.get(key) for key in ("state", "ok", "job_id", "output", "passport_path")}
        except Exception as exc:
            result = {"state": "unknown", "ok": False, "job_id": None, "output": str(exc)[:2000]}
        O.atomic_json(root/"submission.result.json", result)
    return O.report(feature, "Diagnostic operation: " + str(result.get("state", "unknown")),
                    status="ok" if result.get("state") in {"completed", "reduced", "accepted", "started"} else "warning",
                    rows=["Workspace: " + str(root), "Use action=status to view outcomes; result.json and trials/* retain the evidence.",
                          "A submitted Slurm job is not a completed diagnostic result."], data={"workspace": str(root), "result": result})
