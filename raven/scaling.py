"""Controlled scaling experiments, without scheduler actions or assumed speedups.

Recipes produce ordinary submission plans for review. Runtime summaries require
completed, identified repeats with explicit comparable scientific metadata.
Interval bounds describe observed repeat spread, not future-run confidence.
"""
from __future__ import annotations

from collections import defaultdict
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics

MAX_BYTES = 8 << 20
MAX_RECORDS = 10000
MAX_CONFIGURATIONS = 128
MAX_RUNS = 128
MAX_WORKERS = 1000000
MAX_RESOURCE = 2147483647
MAX_RUNTIME = 1e12
MAX_PROBLEM = 1e300
MAX_SCRIPT_INSPECTION_BYTES = 32 << 20
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_SHA = re.compile(r"[0-9a-fA-F]{64}\Z")
_SECRET = re.compile(r"secret|(^|_)token($|_)|password|passwd|credential|private.?key|api.?key|authorization|access.?key|signing.?key|bearer|(^|_)auth($|_)|(^|_)key($|_)", re.I)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_RESERVED = {"seed", "repeat", "workers", "problem_size", "configuration", "label", "experiment_id", "config"}


def _integer(value, name, minimum=1, maximum=MAX_RESOURCE):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _number(value, name, *, positive=True, maximum=MAX_PROBLEM):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite {'positive' if positive else 'nonnegative'} number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} exceeds its numeric limit") from exc
    if not math.isfinite(number) or number > maximum or (number <= 0 if positive else number < 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}, at most {maximum:g}")
    return number


def _metadata(value, *, max_values=30000, max_bytes=MAX_BYTES):
    """Validate iteratively before JSON serialization, including hostile depth."""
    stack, count = [(value, 0)], 0
    while stack:
        current, depth = stack.pop()
        count += 1
        if depth > 16 or count > max_values:
            raise ValueError("scaling metadata exceeds nesting or value limits")
        if isinstance(current, dict):
            if len(current) > 1000:
                raise ValueError("scaling metadata object is too large")
            for key, child in current.items():
                if not isinstance(key, str) or not key or len(key) > 128 or not key.isprintable():
                    raise ValueError("scaling metadata requires bounded printable string keys")
                if _SECRET.search(key):
                    raise ValueError("scaling metadata contains a secret-like field name")
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            if len(current) > MAX_RECORDS:
                raise ValueError("scaling metadata list is too large")
            stack.extend((child, depth + 1) for child in current)
        elif isinstance(current, str):
            if len(current) > 4096 or current and not current.isprintable():
                raise ValueError("scaling metadata strings must be bounded and printable")
            try:
                current.encode("utf-8")
            except UnicodeError as exc:
                raise ValueError("scaling metadata strings must be valid Unicode") from exc
        elif isinstance(current, bool) or current is None:
            pass
        elif isinstance(current, int):
            if current.bit_length() > 1024:
                raise ValueError("scaling metadata integer is too large")
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError("scaling metadata numbers must be finite")
        else:
            raise ValueError("scaling metadata must contain JSON values")
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(encoded) > max_bytes:
        raise ValueError("scaling metadata exceeds its byte budget")
    return encoded


def load(path):
    """Load a bounded regular-file recipe or analysis record document."""
    from .planning_io import load_json
    result = load_json(path, max_bytes=MAX_BYTES, max_depth=16)
    if not isinstance(result, dict):
        raise ValueError("scaling document must be a JSON object")
    _metadata(result, max_values=100000)
    return result


def _quantile(values, probability):
    # Linear interpolation of sorted observed values; no stochastic inference.
    ordered = sorted(values)
    location = (len(ordered) - 1) * probability
    left = int(location)
    right = min(left + 1, len(ordered) - 1)
    weight = location - left
    return ordered[left] * (1 - weight) + ordered[right] * weight


def _identity(record, mode):
    script = record.get("script_sha256")
    if not isinstance(script, str) or not _SHA.fullmatch(script):
        raise ValueError("missing or invalid script_sha256 comparability evidence")
    parameters = record.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("parameters object is required as comparability evidence")
    signature = _metadata(parameters)
    problem = _number(record.get("problem_size"), "problem_size")
    workers = record["workers"]
    # Exact decimal-ratio equality avoids broad tolerances accepting changed work.
    size = Fraction(str(record["problem_size"]))
    if mode == "weak":
        size /= workers
    fingerprint = record.get("fingerprint")
    if fingerprint is not None and (not isinstance(fingerprint, str) or not fingerprint or len(fingerprint) > 256 or _CONTROL.search(fingerprint)):
        raise ValueError("fingerprint must be a bounded printable string")
    return (script.lower(), signature, size, fingerprint), problem


def analyze(records, *, mode="strong", baseline=None, coverage=.8, max_records=MAX_RECORDS):
    """Summarize comparable measured repeats and suppress unsupported claims.

    ``cpus`` and ``gpus`` are measured total allocations, never inferred from the
    worker count. Failed/censored groups retain completed runtimes but receive no
    speedup or efficiency; a failed baseline suppresses all relative claims.
    """
    safe_mode = mode if isinstance(mode, str) and mode in {"strong", "weak"} else None
    safe_baseline = baseline if isinstance(baseline, int) and not isinstance(baseline, bool) and 1 <= baseline <= MAX_WORKERS else None
    safe_coverage = coverage if isinstance(coverage, (int, float)) and not isinstance(coverage, bool) and 0 < coverage < 1 else None
    result = {"schema": "raven.scaling-analysis/v1", "status": "error", "mode": safe_mode,
              "baseline": safe_baseline, "coverage": safe_coverage, "points": [], "excluded": [],
              "excluded_count": 0, "limits": [], "evidence": []}
    try:
        if not isinstance(mode, str) or mode not in {"strong", "weak"}:
            raise ValueError("mode must be strong or weak")
        _number(coverage, "coverage", maximum=1)
        if coverage >= 1:
            raise ValueError("coverage must be strictly between zero and one")
        _integer(max_records, "max_records", maximum=MAX_RECORDS)
        if baseline is not None:
            _integer(baseline, "baseline", maximum=MAX_WORKERS)
        if not isinstance(records, (list, tuple)):
            raise ValueError("records must be a bounded list of measured runs")
    except ValueError as exc:
        result["limits"].append(str(exc))
        return result

    groups, identities, seen, blocked = defaultdict(list), set(), {}, set()
    observed_workers, metadata_bytes = set(), 0
    truncated = len(records) > max_records
    identity_failed = False
    for index, record in enumerate(records[:max_records]):
        worker = None
        try:
            if not isinstance(record, dict):
                raise ValueError("record must be an object")
            worker = _integer(record.get("workers"), "workers", maximum=MAX_WORKERS)
            observed_workers.add(worker)
            metadata_bytes += len(_metadata(record, max_values=2000, max_bytes=65536))
            if metadata_bytes > MAX_BYTES:
                truncated = True
                result["limits"].append("aggregate record metadata byte budget exhausted")
                break
            state = record.get("state")
            if not isinstance(state, str) or state.upper() != "COMPLETED":
                blocked.add(worker)
                raise ValueError("run is not explicitly COMPLETED; failed/censored runtimes are excluded")
            identity, problem = _identity(record, mode)
            runtime = _number(record.get("runtime_seconds"), "runtime_seconds", maximum=MAX_RUNTIME)
            if runtime < 1e-9:
                raise ValueError("runtime_seconds must be at least 1 nanosecond")
            repeat = record.get("repeat")
            if repeat is not None:
                _integer(repeat, "repeat", maximum=MAX_RECORDS)
            job_id = record.get("job_id")
            if job_id is not None and (not isinstance(job_id, str) or not job_id or len(job_id) > 128 or _CONTROL.search(job_id)):
                raise ValueError("job_id must be a bounded printable string")
            if job_id is None and repeat is None:
                raise ValueError("unique job_id or repeat is required for observed run identity")
            run_id = ("job", job_id) if job_id is not None else ("repeat", worker, repeat)
            if run_id in seen:
                previous = seen[run_id]
                if previous != (worker, identity, runtime):
                    blocked.add(worker)
                    blocked.add(previous[0])
                    identity_failed = True
                raise ValueError("duplicate run identity; repeated records are not independent repeats")
            cpus = record.get("cpus")
            gpus = record.get("gpus")
            if cpus is not None:
                _integer(cpus, "cpus", maximum=MAX_RESOURCE)
            if gpus is not None:
                _integer(gpus, "gpus", minimum=0, maximum=MAX_RESOURCE)
            work_units = record.get("work_units")
            if work_units is not None:
                _number(work_units, "work_units")
            seen[run_id] = (worker, identity, runtime)
            identities.add(identity)
            groups[worker].append({"runtime": runtime, "problem_size": problem,
                                   "core_hours": cpus * runtime / 3600 if cpus is not None else None,
                                   "gpu_hours": gpus * runtime / 3600 if gpus is not None else None,
                                   "job_id": job_id, "repeat": repeat})
        except (ValueError, TypeError, OverflowError) as exc:
            reason = str(exc)
            if worker is not None and not reason.startswith("duplicate run identity"):
                blocked.add(worker)
            if "comparability" in reason or "problem_size" in reason:
                identity_failed = True
            result["excluded_count"] += 1
            if len(result["excluded"]) < 256:
                result["excluded"].append({"index": index, "reason": reason})

    if truncated:
        result["limits"].append(f"input truncated after {max_records} records; relative claims suppressed")
    if len(identities) > 1:
        result["limits"].append("incompatible scientific metadata: script, parameters, fingerprint, or problem size differs")
    if identity_failed:
        result["limits"].append("some records lack verified scientific comparability evidence")
    if result["excluded_count"]:
        result["limits"].append("excluded runs can bias completed-only timing; no relative claims for affected worker groups")
    if not groups:
        result["status"] = "insufficient"
        result["limits"].append("no identified completed runs with valid timing and scientific metadata")
        return result
    baseline = min(observed_workers) if baseline is None else baseline
    result["baseline"] = baseline
    baseline_runs = groups.get(baseline)
    if baseline_runs is None:
        result["limits"].append("requested baseline has no valid completed repeats")
    if baseline in blocked:
        result["limits"].append("baseline contains failed, censored, or invalid observations; relative claims suppressed")
    comparable = len(identities) == 1 and not identity_failed and not truncated and baseline_runs is not None and baseline not in blocked
    baseline_time = statistics.median(run["runtime"] for run in baseline_runs) if baseline_runs else None
    low = (1 - coverage) / 2
    for workers, runs in sorted(groups.items()):
        times = [run["runtime"] for run in runs]
        median = statistics.median(times)
        spread = len(times) >= 3
        point = {"workers": workers, "samples": len(times),
                 "runtime": {"median": median, "lower": _quantile(times, low) if spread else None,
                             "upper": _quantile(times, 1 - low) if spread else None,
                             "interval_method": "empirical_runtime_quantiles" if spread else "insufficient_repeats"},
                 "speedup": None, "efficiency": None,
                 "problem_size": runs[0]["problem_size"],
                 "core_hours": None, "gpu_hours": None,
                 "run_ids": [{"job_id": run["job_id"], "repeat": run["repeat"]} for run in runs[:32]]}
        for resource in ("core_hours", "gpu_hours"):
            observed = [run[resource] for run in runs if run[resource] is not None]
            if observed and len(observed) == len(runs):
                point[resource] = {"median": statistics.median(observed), "total": math.fsum(observed), "samples": len(observed)}
        if comparable and workers not in blocked:
            ratio = baseline_time / median
            if mode == "strong":
                point["speedup"] = ratio
                point["efficiency"] = ratio / (workers / baseline)
            else:
                point["efficiency"] = ratio
        result["points"].append(point)
    if any(point["samples"] < 3 for point in result["points"]):
        result["limits"].append("fewer than three repeats in at least one group; observed spread unavailable there")
    result["evidence"] = ["runtime statistics use identified COMPLETED repeats only",
                          "intervals are empirical observed runtime spread, not confidence or prediction intervals",
                          "resource hours use recorded total CPU/GPU allocations; missing allocations remain unknown",
                          "strong scaling holds problem size fixed" if mode == "strong" else "weak scaling requires exact proportional problem size per worker"]
    if not comparable:
        result["status"] = "insufficient"
    elif result["limits"]:
        result["status"] = "partial"
    else:
        result["status"] = "ok"
    return result


def _parameters(value):
    if not isinstance(value, dict) or len(value) > 64:
        raise ValueError("parameters must be an object with at most 64 explicit scalar values")
    _metadata(value)
    exports, seen = {}, set()
    for name, value in value.items():
        if not _ENV_NAME.fullmatch(name) or name.lower() in _RESERVED or name.upper().startswith(("RAVEN_", "SBATCH_", "SLURM_")):
            raise ValueError("parameter names must be environment-safe and must not use reserved scaling controls")
        exported = "RAVEN_PARAM_" + name.upper()
        if exported in seen:
            raise ValueError("parameter names collide after uppercase environment conversion")
        seen.add(exported)
        if not isinstance(value, (str, int, float, bool)) or isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > 128:
            raise ValueError("exported parameters must be bounded scalar strings, numbers, or booleans")
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        if not text or len(text) > 1024 or "," in text or _CONTROL.search(text):
            raise ValueError("exported parameter values must be bounded, nonempty, and contain no commas or controls")
        exports[exported] = text
    return exports


def _recipe(recipe, workdir):
    if not isinstance(recipe, dict):
        raise ValueError("scaling recipe must be an object")
    _metadata(recipe)
    if not isinstance(recipe.get("version"), int) or isinstance(recipe.get("version"), bool) or recipe.get("version") != 1 or recipe.get("kind") != "raven.scaling":
        raise ValueError("recipe requires version 1 and kind raven.scaling")
    allowed = {"version", "kind", "script", "workdir", "mode", "baseline", "repeats", "seed", "configurations", "max_runs", "problem_size", "parameters", "name"}
    if set(recipe) - allowed:
        raise ValueError("unsupported scaling recipe fields: " + ", ".join(sorted(set(recipe) - allowed)))
    script = recipe.get("script")
    directory = workdir if workdir is not None else recipe.get("workdir")
    if not isinstance(script, str) or not script or len(script) > 4096 or _CONTROL.search(script):
        raise ValueError("script must be a nonempty bounded path without controls")
    if directory is not None and (not isinstance(directory, (str, os.PathLike)) or not os.fspath(directory) or _CONTROL.search(os.fspath(directory))):
        raise ValueError("workdir must be a nonempty path without controls")
    mode = recipe.get("mode", "strong")
    if not isinstance(mode, str) or mode not in {"strong", "weak"}:
        raise ValueError("mode must be strong or weak")
    repeats = _integer(recipe.get("repeats", 3), "repeats", maximum=20)
    max_runs = _integer(recipe.get("max_runs", 64), "max_runs", maximum=MAX_RUNS)
    seed = recipe.get("seed", 0)
    _integer(seed, "seed", minimum=0, maximum=2147483627)
    configs = recipe.get("configurations")
    if not isinstance(configs, list) or not 1 <= len(configs) <= MAX_CONFIGURATIONS:
        raise ValueError("configurations must contain 1..128 entries")
    if len(configs) * repeats > max_runs:
        raise ValueError(f"experiment exceeds its {max_runs}-run budget")
    name = recipe.get("name", "scaling")
    if not isinstance(name, str) or not _LABEL.fullmatch(name):
        raise ValueError("name must be a simple printable label of at most 48 characters")
    labels, counts, prepared, identities = set(), set(), [], set()
    for config in configs:
        if not isinstance(config, dict) or set(config) - {"label", "workers", "cpus_per_task", "nodes", "gpus", "parameters", "problem_size"}:
            raise ValueError("configuration has unsupported fields or is not an object")
        workers = _integer(config.get("workers"), "workers", maximum=MAX_WORKERS)
        cpus = _integer(config.get("cpus_per_task", 1), "cpus_per_task")
        if workers * cpus > MAX_RESOURCE:
            raise ValueError("total requested CPUs exceed the 32-bit resource limit")
        nodes = _integer(config.get("nodes", 1), "nodes", maximum=workers)
        gpus = _integer(config.get("gpus", 0), "gpus", minimum=0)
        label = config.get("label", f"w{workers}")
        if not isinstance(label, str) or not _LABEL.fullmatch(label) or label in labels:
            raise ValueError("configuration labels must be unique simple labels of at most 48 characters")
        if workers in counts:
            raise ValueError("each worker count must have one controlled configuration")
        labels.add(label)
        counts.add(workers)
        parameters = config.get("parameters", recipe.get("parameters", {}))
        exports = _parameters(parameters)
        problem_raw = config.get("problem_size", recipe.get("problem_size"))
        problem = _number(problem_raw, "problem_size")
        size = Fraction(str(problem_raw)) / (workers if mode == "weak" else 1)
        identities.add((_metadata(parameters), size))
        prepared.append({"label": label, "workers": workers, "cpus_per_task": cpus, "nodes": nodes,
                         "gpus": gpus, "parameters": parameters, "exports": exports, "problem_size": problem})
    if len(identities) != 1:
        raise ValueError("controlled scaling requires identical parameters and fixed strong or proportional weak problem sizes")
    baseline = recipe.get("baseline", min(counts))
    _integer(baseline, "baseline", maximum=MAX_WORKERS)
    if baseline not in counts:
        raise ValueError("baseline must be an explicitly planned worker count")
    return script, directory, mode, repeats, seed, baseline, name, prepared


def plan(recipe, *, workdir=None):
    """Prepare bounded repeated jobs for review; never submit or probe Slurm.

    Workloads explicitly read ``RAVEN_PARAM_<NAME>`` and ``RAVEN_SCALING_*``.
    No script arguments, generated wrappers, shell expansion, or arrays are used.
    """
    result = {"schema": "raven.scaling-plan/v1", "status": "error", "valid": False, "runs": [],
              "warnings": [], "issues": [], "run_count": 0, "experiment_id": None}
    try:
        script, directory, mode, repeats, seed, baseline, name, configs = _recipe(recipe, workdir)
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        result["issues"].append({"level": "error", "code": "invalid_recipe", "message": str(exc)})
        return result
    from . import submission
    # Resolve a relative script consistently with the explicitly selected workdir.
    candidate = Path(script).expanduser()
    if not candidate.is_absolute() and directory is not None:
        candidate = Path(directory).expanduser() / candidate
    base = submission.prepare(candidate, workdir=directory)
    fatal = [issue for issue in base["issues"] if issue["code"] in {"script_unreadable", "missing_shebang", "script_encoding", "invalid_workdir", "inherited_sbatch_options"}]
    if fatal:
        result["issues"].extend(fatal)
        return result
    try:
        inspected_bytes = os.stat(base["script"]).st_size * (1 + len(configs) * repeats)
        if inspected_bytes > MAX_SCRIPT_INSPECTION_BYTES:
            raise ValueError("repeated script inspection exceeds the aggregate 32 MiB planning budget; reduce script size or runs")
    except (OSError, ValueError) as exc:
        result["issues"].append({"level": "error", "code": "script_inspection_budget", "message": str(exc)})
        return result
    dangerous_resources = {"array", "ntasks_per_node", "ntasks_per_gpu", "ntasks_per_core", "ntasks_per_socket", "cpus_per_gpu",
                           "gpus_per_node", "gpus_per_socket", "gpus_per_task", "gres"}
    conflict = sorted(dangerous_resources & base["resources"].keys())
    if conflict:
        result["issues"].append({"level": "error", "code": "uncontrolled_resources", "message": "remove ambiguous or array directives from scaling script: " + ", ".join(conflict)})
    if base["resources"].get("gpus") and any(config["gpus"] == 0 for config in configs):
        result["issues"].append({"level": "error", "code": "inherited_gpu_request", "message": "a zero-GPU configuration would inherit the script GPU request; remove the script --gpus directive"})
    # One hash identifies the scientific experiment, independent of repeat order.
    identity = {"script_sha256": base["script_sha256"], "mode": mode, "seed": seed,
                "configurations": [{key: config[key] for key in ("label", "workers", "cpus_per_task", "nodes", "gpus", "parameters", "problem_size")} for config in configs]}
    experiment_id = hashlib.sha256(_metadata(identity).encode()).hexdigest()
    result.update({"mode": mode, "baseline": baseline, "repeats": repeats, "experiment_id": experiment_id})
    if repeats < 3:
        result["warnings"].append("fewer than three repeats cannot describe empirical runtime spread")
    result["warnings"].append("review only: no scheduler tests or submissions have been performed")
    result["warnings"].append("workload must explicitly consume RAVEN_SCALING_* controls and RAVEN_PARAM_* parameters; forwarding does not prove workload compliance")
    for config in configs:
        for repeat in range(1, repeats + 1):
            unique = f"{name}-{config['label']}-r{repeat}"
            exports = dict(config["exports"])
            exports.update({"RAVEN_SCALING_WORKERS": str(config["workers"]), "RAVEN_SCALING_REPEAT": str(repeat),
                            "RAVEN_SCALING_SEED": str(seed + repeat - 1), "RAVEN_SCALING_CONFIGURATION": config["label"],
                            "RAVEN_SCALING_PROBLEM_SIZE": str(config["problem_size"]), "RAVEN_SCALING_EXPERIMENT_ID": experiment_id})
            overrides = [f"--ntasks={config['workers']}", f"--cpus-per-task={config['cpus_per_task']}", f"--nodes={config['nodes']}",
                         "--job-name=" + unique, "--output=" + unique + "-%j.out", "--error=" + unique + "-%j.err",
                         "--export=ALL," + ",".join(f"{key}={value}" for key, value in sorted(exports.items()))]
            if config["gpus"]:
                overrides.append(f"--gpus={config['gpus']}")
            metadata = {"scaling": {"experiment_id": experiment_id, "mode": mode, "workers": config["workers"],
                                   "configuration": config["label"], "repeat": repeat, "seed": seed + repeat - 1,
                                   "problem_size": config["problem_size"], "parameters": config["parameters"]}}
            prepared = submission.prepare(candidate, workdir=directory, overrides=overrides, parameters=metadata)
            if prepared["script_sha256"] != base["script_sha256"]:
                result["issues"].append({"level": "error", "code": "script_changed", "message": "script changed during experiment preparation; prepare and review the entire experiment again"})
                result["run_count"] = len(result["runs"])
                return result
            result["runs"].append({"configuration": config["label"], "repeat": repeat, "workers": config["workers"],
                                   "seed": seed + repeat - 1, "submission": prepared})
    result["run_count"] = len(result["runs"])
    invalid = [run for run in result["runs"] if not run["submission"]["valid"]]
    if invalid:
        result["issues"].append({"level": "error", "code": "submission_preflight", "message": f"{len(invalid)} planned runs failed submission preflight; inspect each exact plan"})
    result["valid"] = not result["issues"] and bool(result["runs"])
    result["status"] = "ok" if result["valid"] else "error"
    return result
