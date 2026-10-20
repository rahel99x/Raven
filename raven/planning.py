"""Wave-two analysis over explicit files or the existing scheduler snapshot."""
from __future__ import annotations

from collections.abc import Mapping
import json
import math
from itertools import islice

from . import clock
from .planning_catalog import PLANNING_VIEWS as PLANNING_VIEWS
from .planning_records import record as record





def _records(values, label):
    if isinstance(values, (str, bytes, Mapping)) or values is None:
        raise ValueError(f"{label} must be an iterable of records")
    try:
        return list(islice(iter(values), 10001))
    except TypeError as exc:
        raise ValueError(f"{label} must be an iterable of records") from exc


def _now(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("planning now must be a finite nonnegative epoch timestamp")
    try:
        valid = math.isfinite(value) and 0 <= value <= 253402300799
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("planning now must be a finite nonnegative epoch timestamp")
    return value


def apply_overrides(source, overrides, *, view, snap=None):
    """Merge explicit scientific options while preserving fresh file contents.

    ``action`` and ``workdir`` remain control options passed to ``analyze``;
    inserting them into a bare strict recipe would corrupt its schema.
    """
    overrides = overrides or {}
    if not isinstance(overrides, Mapping):
        raise ValueError("planning overrides must be an object")
    if len(overrides) > 16 or set(overrides) - {"query", "coverage", "mode", "baseline", "job_id", "now", "action", "workdir"}:
        raise ValueError("unsupported planning override")
    changes = {key: value for key, value in overrides.items() if key not in {"action", "workdir"}}
    if not changes:
        return source
    if source is None:
        source = {"history": (snap or {}).get("finished", [])}
    elif isinstance(source, list):
        source = {"history" if view == "predict" else "records" if view == "scaling" else "candidates" if view == "tradeoffs" else "jobs": source}
    elif isinstance(source, Mapping):
        source = dict(source)
        if source.get("kind") == "raven.workflow":
            source = {"workflow": source}
    else:
        raise ValueError("planning source must be an object or array")
    if "query" in changes:
        query = source.get("query", {})
        if not isinstance(query, Mapping) or not isinstance(changes["query"], Mapping):
            raise ValueError("planning query must be an object")
        changes["query"] = dict(query, **changes["query"])
    return dict(source, **changes)


def select_job(jobs, jid=None, *, pending=False):
    rows = _records(jobs, "jobs")[:10000]
    if jid is not None:
        if isinstance(jid, bool) or not isinstance(jid, (str, int)) or isinstance(jid, int) and not 0 <= jid <= 10**20:
            raise ValueError("selected job ID must be bounded text or an integer")
        jid = str(jid)
        if not jid or len(jid) > 128 or not all(c.isprintable() for c in jid):
            raise ValueError("selected job ID must be bounded printable text")
        match = next((j for j in rows if str(record(j).get("id")) == jid or str(record(j).get("job_id")) == jid), None)
        if match is not None:
            return match
        raise ValueError("selected job is absent from the planning data")
    if pending:
        return next((j for j in rows if record(j).get("state") in ("PENDING", "PD")), rows[0] if rows else None)
    return rows[0] if rows else None


def analyze(view, source, *, snap=None, job=None, observations=(), now=None, overrides=None, cancel=None):
    """Source is a planning bundle, individual recipe, or a list of explicit records."""
    if isinstance(source, list) and view in ("forecast", "blockers"):
        source = {"jobs": source}
    source = apply_overrides(source, overrides, view=view, snap=snap)
    control = overrides or {}
    now = clock.now() if now is None else now
    snap = snap or {}
    source = source if source is not None else {}
    mapping = source if isinstance(source, Mapping) else {}
    if view == "forecast" and mapping.get("schema") == "raven.forecast-scenarios/v1":
        from .forecast import scenarios
        return scenarios(mapping)
    if view == "checkpoint-advice":
        from .checkpoint_advisor import advise
        # A bound generic planning report is shared by all research views. Its
        # presence does not mean the run supplied checkpoint measurements.
        if mapping.get("kind") == "raven.planning" and mapping.get("version") == 1:
            if "checkpoint_advice" not in mapping:
                return {"status": "incomplete", "summary": "This planning report has no checkpoint advice inputs. Attach measured durations and allocation exposure with checkpoint-advice FILE."}
            return advise(mapping["checkpoint_advice"])
        if not mapping:
            return {"status": "incomplete", "summary": "Attach measured durations and allocation exposure with checkpoint-advice FILE."}
        return advise(mapping)
    now = _now(mapping.get("now", now))
    coverage = mapping.get("coverage", .8)
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)) or not 0 < coverage < 1:
        raise ValueError("planning coverage must be finite and strictly between zero and one")
    frozen_jobs = "jobs" in mapping
    history = _records(mapping.get("history", [] if frozen_jobs else snap.get("finished", [])), "history")
    jobs = _records(mapping["jobs"] if "jobs" in mapping else _records(snap.get("jobs", []), "jobs") + history, "jobs")
    if mapping.get("job_id") is not None:
        job = select_job(jobs, mapping.get("job_id"), pending=view in ("forecast", "blockers"))
    elif "jobs" in mapping:
        provided_id = record(job).get("id") or record(job).get("job_id")
        try:
            job = select_job(jobs, provided_id, pending=view in ("forecast", "blockers")) if provided_id is not None else select_job(jobs, pending=view in ("forecast", "blockers"))
        except ValueError:
            job = select_job(jobs, pending=view in ("forecast", "blockers"))
    elif job is None:
        job = select_job(jobs, pending=view in ("forecast", "blockers"))
    jid = record(job).get("id") or record(job).get("job_id")
    if view == "predict":
        from .predict import predict
        if isinstance(source, list):
            history = source
        query = mapping.get("query", record(job) if job is not None else None)
        return predict(history, query, coverage=mapping.get("coverage", .8), cancel=cancel)
    if view == "forecast":
        from .forecast import forecast
        if job is None:
            return {"status": "empty", "summary": "No queued job is available for forecasting."}
        return forecast(job, history, observations=mapping.get("observations", () if frozen_jobs else observations),
                        now=now, coverage=mapping.get("coverage", .8), cancel=cancel)
    if view == "blockers":
        from .blockers import explain
        if job is None:
            return {"status": "empty", "summary": "Select a job to inspect scheduler blockers."}
        details = mapping.get("details", {} if frozen_jobs else snap.get("details", {}))
        if isinstance(details, dict) and jid in details:
            details = details[jid]
        return explain(job, jobs=jobs, finished=history, details=details,
                       partitions=mapping.get("partitions", [] if frozen_jobs else snap.get("partitions", [])),
                       nodes=mapping.get("nodes", {} if frozen_jobs else snap.get("nodemap", {})),
                       share=mapping.get("share", [] if frozen_jobs else snap.get("share", [])),
                       health=mapping.get("health", {} if frozen_jobs else snap.get("health", {})), now=now)
    if view == "tradeoffs":
        from .tradeoffs import compare
        candidates = source if isinstance(source, list) else mapping.get("candidates")
        if candidates is None:
            candidates = candidates_from_history(history, job)
        return compare(candidates, history=history, queue_history=_records(mapping.get("queue_history", []), "queue history"),
                       coverage=mapping.get("coverage", .8), cancel=cancel)
    if view == "scaling":
        from .scaling import analyze as scaling_analyze, plan
        action = control.get("action")
        if action == "plan" or action is None and mapping.get("kind") == "raven.scaling":
            return plan(mapping, workdir=control.get("workdir"))
        records = source if isinstance(source, list) else mapping.get("scaling", mapping.get("records", []))
        return scaling_analyze(_records(records, "scaling records"), mode=mapping.get("mode", "strong"), baseline=mapping.get("baseline"),
                               coverage=mapping.get("coverage", .8))
    if view == "workflow":
        from .workflow import analyze as workflow_analyze, plans
        recipe = mapping.get("workflow", mapping)
        if not recipe or "nodes" not in recipe:
            return {"status": "empty", "summary": "Attach a workflow recipe with :workflow FILE or --planning-file FILE."}
        output = workflow_analyze(recipe, now=now)
        if control.get("action") == "plan":
            output["plans"] = plans(recipe, workdir=control.get("workdir"))
        return output
    raise ValueError("unknown planning view")


def candidates_from_history(history, job=None):
    """Only observed configurations; no assumed speedup from requesting more CPUs."""
    from .predict import _identity, _mapping
    try:
        target_data = _mapping(job) if job is not None else {}
        target = _identity(target_data, query=True)
    except (ValueError, TypeError, OverflowError, RecursionError):
        return []
    out, seen = [], set()
    for item in _records(history, "history")[:10000]:
        try:
            data = _mapping(item)
            if not isinstance(data.get("state"), str) or data["state"].split(" ", 1)[0].rstrip("+").upper() != "COMPLETED":
                continue
            identity = _identity(data)
            same_work = ("name", "partition", "account", "qos", "gpu_type", "script_sha256", "input_size", "parameters")
            if any(key in target and identity.get(key) != target[key] for key in same_work):
                continue
            candidate = dict(identity)
            if "parameters" in candidate:
                candidate["parameters"] = json.loads(candidate["parameters"])
            if "gpu_type" in data and data["gpu_type"] == "":
                candidate["gpu_type"] = ""
            if target_data.get("gpu_type") == "" and target_data.get("gpus", 0) and candidate.get("gpu_type") != "":
                continue
            memory, limit = candidate.get("mem_bytes"), candidate.get("time_seconds")
            if memory is None or limit is None or memory <= 0 or limit <= 0 or memory > (1 << 63) - 1 or limit > 100 * 366 * 86400:
                continue
            if isinstance(memory, float):
                if not memory.is_integer():
                    continue
                candidate["mem_bytes"] = int(memory)
            if candidate["cpus"] < candidate["nodes"]:
                continue
            signature = json.dumps(candidate, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
            continue
        if signature in seen:
            continue
        seen.add(signature)
        candidate["label"] = f"observed {candidate['cpus']} CPUs / {candidate['gpus']} GPUs"
        out.append(candidate)
        if len(out) >= 16:
            break
    return out


def demo_source(job=None):
    """Explicitly simulated, reproducible measured runs for offline visual development."""
    now = clock.now()
    j = record(job) if job is not None else {}
    name, partition = j.get("name") or "demo-simulation", j.get("partition") or "main"
    base_cpu = j.get("cpus") or 4
    nodes, gpus = j.get("nodes") or 1, j.get("gpus") or 0
    history, candidates, scaling = [], [], []
    configurations = sorted({max(nodes, (base_cpu // (2 * nodes)) * nodes), base_cpu, base_cpu * 2})
    for cpus in configurations:
        candidates.append({"label": f"{cpus} total CPUs", "name": name, "partition": partition, "cpus": cpus,
                           "nodes": nodes, "gpus": gpus, "mem_bytes": 8 * 1024**3, "time_seconds": 3600})
        for repeat in range(24):
            runtime = 900 / (1 + .65 * (cpus / base_cpu - 1)) * (1 + .07 * math.sin(repeat * 2))
            history.append({"id": f"demo-{cpus}-{repeat}", "name": name, "partition": partition, "state": "COMPLETED",
                            "cpus": cpus, "nodes": nodes, "gpus": gpus, "runtime_seconds": runtime,
                            "mem_bytes": 8 * 1024**3, "time_seconds": 3600,
                            "memory_bytes": 2.5 * 1024**3 * (1 + .05 * math.cos(repeat)), "memory_scope": "job_peak",
                            "cpu_seconds": runtime * cpus * .72, "start": now - 50000 + repeat * 1200,
                            "submit": now - 50180 + repeat * 1200, "end": now - 50000 + repeat * 1200 + runtime})
    for workers in (1, 2, 4, 8):
        for repeat in range(8):
            scaling.append({"job_id": f"scale-{workers}-{repeat}", "repeat": repeat + 1, "workers": workers,
                            "runtime_seconds": (30 + 480 / workers) * (1 + .03 * math.sin(repeat)),
                            "problem_size": 100000, "parameters": {"iterations": 100}, "script_sha256": "a" * 64,
                            "state": "COMPLETED", "cpus": workers, "gpus": 0})
    workflow = {"version": 1, "kind": "raven.workflow", "nodes": [
        {"id": "prepare", "duration": {"lower": 40, "estimate": 60, "upper": 80}, "resources": {"cpus_per_task": 1}},
        {"id": "train-a", "depends_on": ["prepare"], "duration": {"lower": 350, "estimate": 420, "upper": 540}, "resources": {"cpus_per_task": 4, "gpus": 1}},
        {"id": "train-b", "depends_on": ["prepare"], "duration": {"lower": 200, "estimate": 280, "upper": 400}, "resources": {"cpus_per_task": 4}},
        {"id": "evaluate", "depends_on": ["train-a", "train-b"], "duration": {"lower": 70, "estimate": 90, "upper": 120}, "resources": {"cpus_per_task": 2}}]}
    query = {"name": name, "partition": partition, "cpus": base_cpu, "nodes": nodes, "gpus": gpus, "memory_scope": "job_peak"}
    return {"version": 1, "kind": "raven.planning", "simulated": True, "query": query, "history": history,
            "candidates": candidates, "scaling": scaling, "workflow": workflow}
