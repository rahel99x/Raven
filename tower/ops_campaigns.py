"""Reviewed recovery, bounded scientific campaigns, and allocation composers."""
from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

from . import campaign_common as C
from . import operations as O
from . import submission


def _spec(key, title, proposal, summary, choices, default, extra=()):
    return {"key": key, "title": title, "group": "Campaigns", "proposal": proposal, "summary": summary,
            "fields": [{"key": "manifest", "label": "Manifest JSON", "required": True},
                       {"key": "action", "label": "Action", "default": default, "choices": choices}, *extra]}


SPECIFICATIONS = [
    _spec("checkpoint", "Checkpoint recovery", "S01", "Verify an application checkpoint and review an exact restart.", ["inspect", "restart"], "inspect"),
    _spec("search", "Adaptive parameter search", "A06", "Propose or launch bounded, distinct scientific trials.", ["inspect", "propose", "launch"], "propose",
          [{"key": "count", "label": "Trial count", "default": "1"}]),
    _spec("packing", "Short-task packing", "A09", "Pack resource-accounted tasks into one Slurm allocation.", ["inspect", "prepare"], "prepare"),
    _spec("dask", "Elastic Dask pool", "A10", "Inspect or control one owned dask-jobqueue pool.", ["inspect", "start", "scale", "stop"], "inspect",
          [{"key": "target", "label": "Target Slurm jobs", "default": "0"}]),
    _spec("heterogeneous", "Heterogeneous allocation", "A12", "Review distinct resource components and a coupled srun launch.", ["inspect", "prepare"], "prepare"),
]

_SCHEMAS = {"checkpoint": "tower.checkpoint-restart/v1", "search": "tower.parameter-search/v1",
            "packing": "tower.packed-tasks/v1", "dask": "tower.dask-pool/v1",
            "heterogeneous": "tower.heterogeneous-allocation/v1"}


def _load(feature, params, ctx):
    O.local_only(ctx)
    path = Path(C.text(params.get("manifest"), "manifest")).expanduser().absolute()
    value = C.read(path)
    if not isinstance(value, dict) or value.get("schema") != _SCHEMAS[feature]:
        raise ValueError("expected " + _SCHEMAS[feature])
    return path, value


def _path(value, base):
    result = Path(C.text(value, "path")).expanduser()
    return str((base / result if not result.is_absolute() else result).absolute())


def _script(recipe, base):
    script = _path(recipe.get("script"), base)
    raw = C.file_bytes(script, 256 << 10).decode("utf-8")
    workdir = _path(recipe.get("workdir", str(base)), base)
    flags = recipe.get("sbatch", [])
    if not isinstance(flags, list) or len(flags) > 128 or any(not isinstance(a, str) for a in flags):
        raise ValueError("sbatch must be a bounded argument list")
    plan = submission.prepare(script, workdir=workdir, overrides=flags)
    if not plan["valid"]:
        raise ValueError("; ".join(row["message"] for row in plan["issues"] if row["level"] == "error"))
    if plan["resources"].get("array"):
        raise ValueError("campaign scripts must not declare arrays; logical trials have individual receipts")
    if not re.match(r"^#!\s*(?:/usr/bin/env\s+)?(?:[^\s]*/)?(?:bash|sh|dash)(?:\s|$)", raw):
        raise ValueError("campaign scripts require a bash, sh, or dash shebang")
    if hashlib.sha256(raw.encode()).hexdigest() != plan["script_sha256"]:
        raise ValueError("campaign script changed during inspection")
    return plan, raw


def _with_environment(script, values, guard=""):
    """Insert literal environment values after leading Slurm directives."""
    lines = script.splitlines(keepends=True)
    end = next((i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")), len(lines))
    prefix = "".join(lines[:end])
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    exports = "".join("export " + key + "=" + shlex.quote(value) + "\n" for key, value in values.items())
    return prefix + exports + guard + "".join(lines[end:])


def _evidence(path, recipe):
    return {"manifest": str(path), "manifest_digest": C.digest(recipe)}


def _checkpoint(path, recipe, params, ctx):
    checkpoint = recipe.get("checkpoint")
    compatibility = recipe.get("compatibility")
    if not isinstance(checkpoint, dict) or checkpoint.get("complete") is not True:
        raise ValueError("checkpoint must explicitly declare complete: true after atomic publication")
    filename = _path(checkpoint.get("path"), path.parent)
    expected = checkpoint.get("sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("checkpoint requires its SHA-256 digest")
    if not isinstance(compatibility, dict) or not isinstance(compatibility.get("expected"), dict) or not compatibility["expected"]:
        raise ValueError("declare expected and actual application/runtime checkpoint compatibility")
    if compatibility["expected"] != compatibility.get("actual"):
        raise ValueError("checkpoint compatibility differs from the restart environment")
    for key in ("application", "format", "runtime"):
        C.text(compatibility["expected"].get(key), "compatibility " + key, 256)
    if C.file_hash(filename, ctx.cancel) != expected:
        raise ValueError("checkpoint SHA-256 does not match the completed application checkpoint")
    base, script = _script(recipe, path.parent)
    if recipe.get("script_sha256") != base["script_sha256"]:
        raise ValueError("restart script requires its exact script_sha256 compatibility pin")
    source = recipe.get("source_job")
    if not isinstance(source, dict) or not re.fullmatch(r"[1-9][0-9]{0,19}(?:_[0-9]{1,10}|\+[0-9]{1,5})?", str(source.get("id", ""))) or not source.get("start") or not source.get("cluster"):
        raise ValueError("source_job requires id, start, and cluster to identify the checkpoint attempt")
    C.text(source["start"], "source start", 80)
    C.text(source["cluster"], "source cluster", 256)
    if ctx.scope.get("cluster") and source["cluster"] != ctx.scope["cluster"]:
        raise ValueError("checkpoint belongs to a different cluster")
    for row in ctx.jobs + ctx.finished:
        if str(row.get("id")) == str(source["id"]):
            if str(row.get("start")) != str(source["start"]) or row.get("cluster", source["cluster"]) != source["cluster"]:
                raise ValueError("source job ID now refers to a different attempt")
            if row in ctx.jobs and str(row.get("state", "")).upper() in {"R", "RUNNING", "CG", "COMPLETING"}:
                raise ValueError("source job is still active; finish or stop it before restarting")
    verifier = ("import hashlib,os,stat,sys\n"
                "p,expected=sys.argv[1:]\n"
                "fd=os.open(p,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|os.O_NONBLOCK)\n"
                "with os.fdopen(fd,'rb') as f:\n"
                " before=os.fstat(f.fileno()); h=hashlib.sha256(); total=0\n"
                " if not stat.S_ISREG(before.st_mode) or before.st_size>1073741824: sys.exit('invalid checkpoint')\n"
                " for block in iter(lambda:f.read(1048576),b''):\n"
                "  total+=len(block)\n"
                "  if total>1073741824: sys.exit('checkpoint exceeds limit')\n"
                "  h.update(block)\n"
                " after=os.fstat(f.fileno()); named=os.stat(p,follow_symlinks=False)\n"
                " sig=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)\n"
                " if sig(before)!=sig(after) or sig(after)!=sig(named) or h.hexdigest()!=expected: sys.exit('checkpoint changed since review')\n")
    guard = "python3 -c " + shlex.quote(verifier) + ' "$TOWER_CHECKPOINT" "$TOWER_CHECKPOINT_SHA256" || exit 65\n'
    content = _with_environment(script, {"TOWER_CHECKPOINT": filename, "TOWER_CHECKPOINT_SHA256": expected}, guard)
    run_id = C.identifier(recipe["id"], "restart id") if "id" in recipe else "default"
    root = C.directory(ctx, "checkpoint") / C.digest({"source": source, "checkpoint": expected, "script": base["script_sha256"], "id": run_id})[:24]
    payload = {**_evidence(path, recipe), "base_plan": base, "content": content, "script_path": str(root / "restart.sh"),
               "checkpoint": filename, "checkpoint_sha256": expected, "source_job": source, "receipt": str(root / "receipt.json")}
    return payload, ["Application: " + compatibility["expected"]["application"], "Checkpoint: " + filename,
                     "Verified SHA-256: " + expected, "Restart: " + base["command"],
                     "The application reads TOWER_CHECKPOINT; Tower never guesses a restart flag."]


def _search_candidates(recipe):
    space = recipe.get("parameters")
    if not isinstance(space, dict) or not 1 <= len(space) <= 16:
        raise ValueError("search needs 1..16 discrete parameter dimensions")
    dimensions, size = [], 1
    for name, values in sorted(space.items()):
        C.identifier(name, "parameter name")
        if not isinstance(values, list) or not 1 <= len(values) <= 128:
            raise ValueError("each parameter needs 1..128 explicit values")
        if any(isinstance(v, (dict, list)) or v is None or isinstance(v, float) and not math.isfinite(v) for v in values):
            raise ValueError("parameter values must be finite JSON scalars")
        if any(isinstance(v, str) and len(v) > 1024 for v in values):
            raise ValueError("parameter strings cannot exceed 1024 characters")
        if len({C.digest(v) for v in values}) != len(values):
            raise ValueError("parameter dimensions cannot contain duplicate values")
        size *= len(values)
        if size > 4096:
            raise ValueError("search grid exceeds 4096 configurations")
        dimensions.append((name, values))
    return [{name: value for (name, _), value in zip(dimensions, values)} for values in itertools.product(*(v for _, v in dimensions))]


def _search(path, recipe, params, ctx):
    campaign_id = C.identifier(recipe.get("id"), "campaign id")
    base, script = _script(recipe, path.parent)
    candidates = _search_candidates(recipe)
    budget = C.integer(recipe.get("budget"), "budget", 1, min(4096, len(candidates)))
    parallel = C.integer(recipe.get("max_parallel", 1), "max_parallel", 1, 64)
    direction = recipe.get("direction", "minimize")
    if direction not in {"minimize", "maximize"}:
        raise ValueError("direction must be minimize or maximize")
    count = C.integer(int(params.get("count", "1")), "trial count", 1, 64)
    fingerprint = C.digest({"base": base["plan_id"], "parameters": recipe["parameters"], "budget": budget,
                            "max_parallel": parallel, "direction": direction})
    root = C.directory(ctx, "search") / campaign_id
    maximum_entry = max(len(json.dumps(values, ensure_ascii=True).encode()) for values in candidates) + len(str(root).encode()) + 1024
    if maximum_entry * budget > 3 << 20:
        raise ValueError("search journal would exceed its budget; reduce trial budget or parameter sizes")
    journal_path = root / "journal.json"
    journal = C.read(journal_path, {"schema": "tower.search-state/v1", "fingerprint": fingerprint, "trials": {}})
    if not isinstance(journal, dict) or journal.get("schema") != "tower.search-state/v1" or journal.get("fingerprint") != fingerprint or not isinstance(journal.get("trials"), dict):
        raise ValueError("campaign settings changed; use a new campaign id")
    trials = journal["trials"]
    if len(trials) > budget:
        raise ValueError("search journal exceeds its trial budget")
    for key, row in trials.items():
        if not re.fullmatch(r"[0-9a-f]{24}", key) or not isinstance(row, dict) or row.get("state") not in {"launching", "unknown", "accepted", "rejected", "not_submitted"}:
            raise ValueError("search journal has an invalid trial receipt")
    observations = recipe.get("observations", [])
    if not isinstance(observations, list) or len(observations) > 4096:
        raise ValueError("observations must be a bounded list")
    outcomes = {}
    known = {C.digest(values)[:24]: values for values in candidates}
    if any(key not in known or row.get("parameters") != known[key] for key, row in trials.items()):
        raise ValueError("search journal parameters do not match this campaign")
    for observation in observations:
        if not isinstance(observation, dict):
            raise ValueError("each observation must be an object")
        trial = observation.get("trial")
        if trial not in known or trial in outcomes or trial not in trials:
            raise ValueError("observation must identify a unique launched trial in this campaign")
        status = observation.get("status")
        if status not in {"success", "scientific_failed", "infrastructure_failed"}:
            raise ValueError("use success, scientific_failed, or infrastructure_failed outcomes")
        if status == "success":
            metric = observation.get("metric")
            if isinstance(metric, bool) or not isinstance(metric, (int, float)) or not math.isfinite(metric):
                raise ValueError("successful observations require a finite metric")
        # An application cannot mark an ambiguous launch finished to bypass concurrency.
        if not trials[trial].get("job_id"):
            raise ValueError("resolve uncertain launch receipts before adding scientific outcomes")
        outcomes[trial] = observation
    successful = [(o["metric"], key) for key, o in outcomes.items() if o["status"] == "success"]
    best = (min(successful) if direction == "minimize" else max(successful))[1] if successful else None
    active_ids = {str(row.get("id")) for row in ctx.jobs}
    active = sum(row.get("state") not in {"rejected", "not_submitted"} and
                 (key not in outcomes or str(row.get("job_id")) in active_ids)
                 for key, row in trials.items())
    available = min(count, budget - len(trials), max(0, parallel - active))
    if available * len(script.encode()) > 1 << 20:
        raise ValueError("reviewed trial scripts exceed 1 MiB; reduce the trial count")
    remaining = {key: values for key, values in known.items() if key not in trials}
    numeric_ranges = {}
    for name, values in recipe["parameters"].items():
        numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if numbers:
            scale = max(1, *(abs(v) for v in numbers))
            normalized = [v / scale for v in numbers]
            numeric_ranges[name] = (scale, max(1 / scale, max(normalized) - min(normalized)))
    def distance(values):
        result = 0.0
        for name, value in values.items():
            other = known[best][name]
            if type(value) is type(other) and value == other:
                continue
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (value, other)):
                scale, span = numeric_ranges[name]
                result += abs(value / scale - other / scale) / span
            else:
                result += 1
        return result
    proposals = []
    for index in range(available):
        exploit = best is not None and (len(trials) + index) % 4 != 0
        def score(item):
            key, values = item
            return distance(values) if exploit else 0, key
        trial, values = min(remaining.items(), key=score)
        remaining.pop(trial)
        content = _with_environment(script, {"TOWER_TRIAL_ID": trial,
                                            "TOWER_TRIAL_PARAMETERS": json.dumps(values, sort_keys=True, separators=(",", ":"))})
        proposals.append({"id": trial, "parameters": values, "strategy": "exploit" if exploit else "explore",
                          "content": content, "script_path": str(root / "trials" / trial / "trial.sh")})
    payload = {**_evidence(path, recipe), "journal_path": str(journal_path), "journal_digest": C.digest(journal),
               "journal": journal, "base_plan": base, "proposals": proposals}
    rows = [f"Campaign {campaign_id}: {len(trials)}/{budget} trials committed; {active}/{parallel} active or unresolved.",
            f"Successful scientific results: {len(successful)}; infrastructure failures do not train the objective."]
    rows += [f"{p['id']} [{p['strategy']}] " + json.dumps(p["parameters"], sort_keys=True) for p in proposals]
    if not proposals:
        rows.append("No launch slots: budget exhausted, all configurations used, or active outcomes are unresolved.")
    return payload, rows


def _packing(path, recipe, params, ctx):
    from .campaign_packing import validate
    adjusted = dict(recipe)
    adjusted["workdir"] = _path(recipe.get("workdir", str(path.parent)), path.parent)
    if not isinstance(recipe.get("tasks"), list):
        raise ValueError("packed tasks must be a bounded list")
    adjusted["tasks"] = [dict(task, cwd=_path(task.get("cwd", adjusted["workdir"]), path.parent))
                         if isinstance(task, dict) else task for task in recipe.get("tasks", [])]
    normalized = validate(adjusted)
    for task in normalized["tasks"]:
        if not Path(task["cwd"]).is_dir():
            raise ValueError("packed task working directory does not exist: " + task["cwd"])
    run_id = C.identifier(recipe["id"], "packed run id") if "id" in recipe else C.digest(normalized)[:24]
    root = C.directory(ctx, "packing") / run_id
    manifest_path, runner_path = root / "tasks.json", root / "runner.py"
    runner = C.file_bytes(Path(__file__).with_name("campaign_packing.py"), 1 << 20).decode()
    walltime = recipe.get("walltime", "01:00:00")
    if not isinstance(walltime, str) or not submission._time(walltime):
        raise ValueError("invalid packing walltime")
    extra = recipe.get("sbatch", [])
    if not isinstance(extra, list) or len(extra) > 64:
        raise ValueError("packing sbatch must be a bounded option list")
    issues = []
    parsed = submission._options(extra, "packing", issues)
    if any(row["key"] not in {"account", "partition", "qos", "reservation", "constraint"} for row in parsed) or any(row["level"] == "error" for row in issues):
        raise ValueError("packing sbatch permits only account, partition, qos, reservation, and constraint")
    site_directives = "".join("#SBATCH " + shlex.join([row["flag"], row["value"]]) + "\n" for row in parsed)
    command = shlex.join(["python3", str(runner_path), str(manifest_path), str(root / "results")])
    script = ("#!/bin/sh\n#SBATCH --nodes=1\n#SBATCH --ntasks=1\n"
              f"#SBATCH --cpus-per-task={normalized['cpus']}\n#SBATCH --mem={normalized['memory_mb']}M\n"
              f"#SBATCH --time={walltime}\n{site_directives}exec {command}\n")
    payload = {**_evidence(path, recipe), "recipe": normalized, "runner": runner, "runner_path": str(runner_path),
               "manifest_path": str(manifest_path), "content": script, "script_path": str(root / "packed.sh"),
               "workdir": adjusted["workdir"], "receipt": str(root / "receipt.json")}
    return payload, [f"{len(normalized['tasks'])} logical tasks; at most {normalized['parallel']} concurrent tasks.",
                     f"Allocation: {normalized['cpus']} CPUs, {normalized['memory_mb']} MiB, one node.",
                     "Each task uses an exclusive srun step and separate logs and durable receipts.", command]


def _heterogeneous(path, recipe, params, ctx):
    inherited = sorted(key for key in os.environ if key.startswith("SBATCH_"))
    if inherited:
        raise ValueError("unset inherited sbatch options before planning: " + ", ".join(inherited))
    components = recipe.get("components")
    if not isinstance(components, list) or not 2 <= len(components) <= 32:
        raise ValueError("heterogeneous allocation needs 2..32 components")
    workdir = _path(recipe.get("workdir", str(path.parent)), path.parent)
    if not Path(workdir).is_dir():
        raise ValueError("heterogeneous working directory does not exist")
    walltime = recipe.get("walltime", "01:00:00")
    if not isinstance(walltime, str) or not submission._time(walltime):
        raise ValueError("invalid heterogeneous walltime")
    sbatch = ["--parsable", "--chdir=" + workdir, "--time=" + walltime]
    launch, descriptions = ["srun"], []
    allowed = {"nodes", "ntasks", "ntasks-per-node", "cpus-per-task", "mem", "mem-per-cpu", "gpus", "gpus-per-node",
               "gpus-per-task", "gres", "partition", "account", "qos", "constraint", "reservation", "exclusive"}
    for index, component in enumerate(components):
        if not isinstance(component, dict) or not isinstance(component.get("resources"), dict):
            raise ValueError("components need resources and argv")
        resources, flags, issues = component["resources"], [], []
        if set(resources) - allowed:
            raise ValueError("unsupported heterogeneous resource option")
        if "nodes" not in resources or "ntasks" not in resources:
            raise ValueError("each component must explicitly request nodes and ntasks")
        for key, value in sorted(resources.items()):
            if key == "exclusive":
                if value is not True:
                    raise ValueError("exclusive must be true when present")
                flags.append("--exclusive")
            elif isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError("component resource values must be strings or integers")
            else:
                flags.append("--" + key + "=" + str(value))
        parsed = submission._options(flags, "component", issues)
        submission._validate_resources({row["key"]: row["value"] for row in parsed if row["value"] is not None}, issues)
        if any(item["level"] == "error" for item in issues):
            raise ValueError("; ".join(item["message"] for item in issues))
        argv = component.get("argv")
        if not isinstance(argv, list) or not 1 <= len(argv) <= 128 or any(not isinstance(a, str) or not a or len(a) > 8192 or "\0" in a for a in argv) or argv[0].startswith("-") or ":" in argv:
            raise ValueError("component argv must be bounded and cannot contain standalone colon separators")
        if index:
            sbatch.append(":")
            launch.append(":")
        sbatch.extend(flags)
        step_keys = {"nodes", "ntasks", "ntasks-per-node", "cpus-per-task", "mem", "mem-per-cpu", "gpus", "gpus-per-node", "gpus-per-task", "gres"}
        step_flags = ["--" + key + "=" + str(value) for key, value in sorted(resources.items()) if key in step_keys]
        launch.extend([f"--het-group={index}", *step_flags, *argv])
        descriptions.append(f"Component {index}: " + shlex.join(flags) + " / " + shlex.join(argv))
    content = "#!/bin/sh\nexec " + shlex.join(launch) + "\n"
    run_id = C.identifier(recipe["id"], "allocation id") if "id" in recipe else "default"
    root = C.directory(ctx, "heterogeneous") / C.digest({"argv": sbatch, "script": content, "id": run_id})[:24]
    script_path = str(root / "heterogeneous.sh")
    payload = {**_evidence(path, recipe), "argv": [*sbatch, script_path], "content": content,
               "script_path": script_path, "workdir": workdir, "receipt": str(root / "receipt.json")}
    return payload, descriptions + ["Submit: " + shlex.join(["sbatch", *payload["argv"]]),
                                    "Launch: " + shlex.join(launch), "Site support for heterogeneous jobs is required."]


_CHILDREN = {}


def _dask(path, recipe, params, ctx):
    from .campaign_dask import validate, alive
    normalized = validate(recipe)
    target = C.integer(int(params.get("target", "0")), "target jobs", 0, normalized["max_jobs"])
    pool_id = C.identifier(recipe.get("id"), "pool id")
    root = C.directory(ctx, "dask") / pool_id
    status = C.read(root / "status.json", {})
    config = C.read(root / "config.json", {})
    if not isinstance(status, dict) or not isinstance(config, dict):
        raise ValueError("Dask controller state must contain JSON objects")
    running = alive(status)
    if running and (config.get("scope") != ctx.scope or config.get("recipe") != normalized):
        raise ValueError("pool belongs to a different configuration or connection")
    available = importlib.util.find_spec("dask_jobqueue") is not None
    action = params.get("action", "inspect")
    if action == "start" and (running or config and status.get("state") != "stopped"):
        raise ValueError("pool already has a controller or unresolved start; inspect its receipt")
    if action in {"scale", "stop"} and not running:
        raise ValueError("no live controller owns this pool; inspect scheduler jobs before starting again")
    if action == "start" and not available:
        raise ValueError("optional dask-jobqueue is unavailable in Tower's Python environment")
    if action == "start" and normalized["adaptive"] and target:
        raise ValueError("adaptive start uses min_jobs/max_jobs; leave target at zero")
    payload = {**_evidence(path, recipe), "directory": str(root), "recipe": normalized, "target": target,
               "action": action, "status_identity": {key: status.get(key) for key in ("owner", "pid", "process_start", "sequence")},
               "config_digest": C.digest(config)}
    rows = [f"Optional dask-jobqueue: {'available' if available else 'not installed'}", f"Pool {pool_id}: {'running' if running else 'not running'}",
            f"Capacity: {normalized['max_jobs']} Slurm jobs, {normalized['cores']} CPUs and {normalized['memory_mb']} MiB per job."]
    if status:
        rows += [f"Requested {status.get('requested_jobs', '?')}; connected {status.get('connected_jobs', '?')}; queued/starting {status.get('queued_or_starting_jobs', '?')}",
                 "Scheduler: " + str(status.get("scheduler_address", "unavailable")), "Controller: " + str(status.get("error", status.get("state", "unknown")))]
    rows.append("One persistent controller owns scaling; it remains active after Tower exits.")
    return payload, rows


_PREPARE = {"checkpoint": _checkpoint, "search": _search, "packing": _packing,
            "dask": _dask, "heterogeneous": _heterogeneous}


def run(feature, params, ctx):
    path, recipe = _load(feature, params, ctx)
    payload, rows = _PREPARE[feature](path, recipe, params, ctx)
    action = params.get("action", "inspect")
    mutates = action in {"restart", "launch", "prepare", "start", "scale", "stop"}
    if feature == "search" and not payload["proposals"]:
        mutates = False
    plan = O.prepare_plan(ctx, feature, params, payload) if mutates else None
    summary = next(spec["title"] for spec in SPECIFICATIONS if spec["key"] == feature)
    return O.report(feature, summary + (" — review before Apply" if plan else " — inspected"), rows=rows, data=payload, plan=plan)


def _generated_plan(payload, content=None, script_path=None):
    target = C.artifact(script_path or payload["script_path"], (content or payload["content"]).encode())
    base = payload.get("base_plan")
    plan = submission.prepare(target, workdir=base["workdir"] if base else payload["workdir"],
                              overrides=base["overrides"] if base else (),
                              parameters=base["parameters"] if base else {})
    if not plan["valid"]:
        raise ValueError("generated submission preflight failed: " + "; ".join(row["message"] for row in plan["issues"] if row["level"] == "error"))
    return plan


def _verify_checkpoint_attempt(payload, ctx):
    source = payload["source_job"]
    job_id = str(source["id"])
    queued = O.command(ctx, ["squeue", "--noheader", "--jobs=" + job_id, "--format=%i|%T"])
    if queued.strip():
        raise ValueError("checkpoint source is still queued or active; restart refused")
    output = O.command(ctx, ["sacct", "--noheader", "--parsable2", "--duplicates", "--jobs=" + job_id,
                             "--format=JobID%64,JobIDRaw%64,Cluster%256,Start,State%64"])
    matches = set()
    for line in output.splitlines():
        fields = line.split("|")
        if len(fields) >= 5 and fields[0] == job_id:
            matches.add(tuple(fields[:5]))
    if len(matches) != 1:
        raise ValueError("checkpoint source attempt is absent or ambiguous in current accounting")
    _, raw_id, cluster, start, state = matches.pop()
    terminal = {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED"}
    if cluster != source["cluster"] or start != source["start"] or not state or state.split()[0].rstrip("+") not in terminal or source.get("raw_id", raw_id) != raw_id:
        raise ValueError("checkpoint source identity or final state changed in current accounting")
    return {"id": job_id, "raw_id": raw_id, "cluster": cluster, "start": start, "state": state}


def _submit_once(feature, payload, ctx, raw=False):
    path = Path(payload["receipt"])
    with C.locked(path):
        previous = C.read(path)
        if previous:
            raise ValueError("this exact campaign launch already has a receipt; inspect it before any new launch: " + str(path))
        O.checkpoint(ctx)
        source_attempt = _verify_checkpoint_attempt(payload, ctx) if feature == "checkpoint" else None
        generated = None if raw else _generated_plan(payload)
        if raw:
            C.artifact(payload["script_path"], payload["content"].encode())
        intent = {"schema": "tower.campaign-receipt/v1", "feature": feature, "state": "launching",
                  "scope": ctx.scope, "payload_digest": C.digest(payload), "created": time.time(),
                  "command": shlex.join(["sbatch", *payload["argv"]]) if raw else generated["command"]}
        if source_attempt:
            intent["source_attempt"] = source_attempt
        C.atomic(path, intent, overwrite=False)
        try:
            if raw:
                ok, out, _ = ctx.slurm.submit(payload["argv"], payload["workdir"])
                jid = submission._job_id(out) if ok else None
                uncertain = bool(re.search(r"timeout|timed.?out|connection reset", out, re.I)) or bool(ok and not jid)
                result = {"state": "unknown" if uncertain else "accepted" if jid else "rejected",
                          "ok": bool(ok and jid), "job_id": jid, "output": out[:65536]}
            else:
                result = submission.submit(generated, ctx.slurm, path.parent / "passports")
                # Large provenance lives in its passport; keep journal state bounded.
                result = {key: result.get(key) for key in ("state", "ok", "job_id", "output", "passport_path")}
                if isinstance(result.get("output"), str):
                    result["output"] = result["output"][:65536]
        except Exception as exc:
            result = {"state": "unknown", "ok": False, "job_id": None, "output": str(exc)[:2000]}
        intent.update(result)
        C.atomic(path, intent)
    return intent


def _apply_search(payload, ctx):
    path = Path(payload["journal_path"])
    with C.locked(path):
        journal = C.read(path, payload["journal"])
        if C.digest(journal) != payload["journal_digest"]:
            raise ValueError("campaign journal changed; prepare a fresh proposal")
        receipts = []
        for proposed in payload["proposals"]:
            if O.cancelled(ctx) and receipts:
                break
            O.checkpoint(ctx)
            trial = proposed["id"]
            if trial in journal["trials"]:
                raise ValueError("trial is already committed")
            plan = _generated_plan(payload, proposed["content"], proposed["script_path"])
            intent = {"parameters": proposed["parameters"], "strategy": proposed["strategy"],
                      "state": "launching", "job_id": None, "created": time.time()}
            journal["trials"][trial] = intent
            C.atomic(path, journal)
            try:
                result = submission.submit(plan, ctx.slurm, path.parent / "trials" / trial / "passports")
                intent.update({key: result.get(key) for key in ("state", "ok", "job_id", "output", "passport_path")})
                if isinstance(intent.get("output"), str):
                    intent["output"] = intent["output"][:512]
            except Exception as exc:
                intent.update(state="unknown", ok=False, output=str(exc)[:2000])
            C.atomic(path, journal)
            receipts.append({"trial": trial, **intent})
            if intent.get("state") in {"unknown", "launching"}:
                break
    partial = len(receipts) != len(payload["proposals"]) or any(row.get("state") != "accepted" for row in receipts)
    return {"state": "partial" if partial else "accepted", "journal": str(path), "trials": receipts}


def _apply_dask(payload, ctx):
    from .campaign_dask import alive
    root = Path(payload["directory"])
    with C.locked(root / "control"):
        status = C.read(root / "status.json", {})
        config = C.read(root / "config.json", {})
        identity = {key: status.get(key) for key in ("owner", "pid", "process_start", "sequence")}
        if identity != payload["status_identity"] or C.digest(config) != payload["config_digest"]:
            raise ValueError("pool controller changed; inspect it again")
        action = payload["action"]
        if action == "start":
            if alive(status):
                raise ValueError("pool controller already running")
            config = {"owner": os.urandom(16).hex(), "recipe": payload["recipe"], "target": payload["target"], "scope": ctx.scope}
            C.atomic(root / "config.json", config)
            # This intent remains if launch or status acknowledgement is uncertain.
            C.atomic(root / "status.json", {"state": "launching", "owner": config["owner"], "scope": ctx.scope})
            fd = os.open(root / "controller.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                proc = subprocess.Popen([sys.executable, "-m", "tower.campaign_dask", str(root)], stdin=subprocess.DEVNULL,
                                        stdout=fd, stderr=fd, start_new_session=True)
            finally:
                os.close(fd)
            for pid, child in list(_CHILDREN.items()):
                if child.poll() is not None:
                    del _CHILDREN[pid]
            _CHILDREN[proc.pid] = proc
            return {"state": "controller_start_requested", "pid": proc.pid, "directory": str(root)}
        if not alive(status) or config.get("scope") != ctx.scope:
            raise ValueError("no matching live controller owns this pool")
        previous = C.read(root / "request.json", {})
        sequence = max(int(status.get("sequence", 0)), int(previous.get("sequence", 0))) + 1
        request = {"owner": config["owner"], "sequence": sequence, "action": action, "target": payload["target"]}
        C.atomic(root / "request.json", request)
        return {"state": "controller_request_queued", **request, "directory": str(root)}


def apply(feature, plan, ctx):
    O.local_only(ctx)
    payload = O.validate_plan(plan, ctx, feature)
    path, recipe = _load(feature, plan["params"], ctx)
    if str(path) != payload["manifest"] or C.digest(recipe) != payload["manifest_digest"]:
        raise ValueError("campaign manifest changed; prepare a new review")
    fresh, _ = _PREPARE[feature](path, recipe, plan["params"], ctx)
    if C.digest(fresh) != C.digest(payload):
        raise ValueError("campaign evidence changed; prepare a new review")
    if feature in {"checkpoint", "search"}:
        submission._revalidate(payload["base_plan"], ctx.slurm)
    if feature == "checkpoint":
        if C.file_hash(payload["checkpoint"], ctx.cancel) != payload["checkpoint_sha256"]:
            raise ValueError("checkpoint changed before restart")
        result = _submit_once(feature, payload, ctx)
    elif feature == "search":
        result = _apply_search(payload, ctx)
    elif feature == "packing":
        C.artifact(payload["runner_path"], payload["runner"].encode())
        C.artifact(payload["manifest_path"], (json.dumps(payload["recipe"], sort_keys=True) + "\n").encode())
        result = _submit_once(feature, payload, ctx)
    elif feature == "heterogeneous":
        result = _submit_once(feature, payload, ctx, raw=True)
    elif feature == "dask":
        result = _apply_dask(payload, ctx)
    else:
        raise ValueError("unknown campaign feature")
    status = "ok" if result.get("state") not in {"unknown", "rejected", "not_submitted", "partial"} else "warning"
    return O.report(feature, "Campaign action recorded", status=status, rows=[json.dumps(result, sort_keys=True)], data=result)


# Managed Raven entry points also protect direct adapter callers.
from .operations import managed_run as _managed_run, managed_apply as _managed_apply
run = _managed_run(run)
apply = _managed_apply(apply)
