"""Reviewed shadow admission over immutable Raven campaign identities.

No scheduler calls or submissions occur here. The existing execution receipt
remains authoritative; uncertain attempts never become new candidate work.
"""
from __future__ import annotations
import copy
import math

from . import orchestrator as X, slurm_simulator as S
from .evidence import bounded, digest
from .runtime_contracts import integer, number, object_

MODEL = "campaign-shadow-policy/1"
MAX_WORK = 1_000_000
POLICIES = ("fifo", "submit-all", "shortest", "backfill-friendly", "fairshare")
LIMITS = [
    "Shadow decisions only: no scientific job is submitted, cancelled, reprioritized or retried by this policy module.",
    "Known admission limits conservatively count every outstanding job's full requested resources, including pending jobs.",
    "Evaluation uses the declared observable queue subset; simulated makespan and waste are not live site outcomes.",
    "Fairshare timing is a bounded heuristic over declared usage, not full Slurm fair-tree, policy evasion or a globally optimal schedule.",
    "External demand and site policy can change after observation. Existing reviewed execution and recovery remain separate explicit actions.",
]


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise ValueError("Campaign policy analysis cancelled")


def validate(document):
    bounded(document)
    object_(document, {"schema", "review", "queue_model", "limits"}, {"receipt", "policy", "metadata"}, "campaign policy")
    if document["schema"] != "raven.campaign-policy/v1":
        raise ValueError("Expected raven.campaign-policy/v1")
    review = X.validate_review(document["review"])
    if "scope" not in review:
        raise ValueError("Campaign policy requires the original scoped execution review")
    model = S.validate(document["queue_model"])
    if len(model["jobs"]) > X.MAX_NODES:
        raise ValueError("Campaign policy is limited to 64 already-declared nodes")
    by_id = {j["id"]: j for j in model["jobs"]}
    if set(by_id) != {n["id"] for n in review["nodes"]}:
        raise ValueError("Queue model must describe exactly the reviewed campaign nodes")
    for node in review["nodes"]:
        job = by_id[node["id"]]
        if job["depends"] != node["depends_on"]:
            raise ValueError("Modeled dependencies differ from the sealed campaign review")
        resources = node["plan"]["resources"]
        # Concrete resources are required; site defaults cannot be guessed.
        from .model import nbytes, secs
        try:
            nodes = int(resources["nodes"])
            tasks = int(resources.get("ntasks", 1))
            cpus = tasks * int(resources["cpus_per_task"])
            memory = nbytes(resources["mem"])
            wall = secs(resources["time"])
            gpus = int(resources.get("gpus", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Policy comparison requires explicit nodes, tasks/CPUs, memory per node and wall time in every reviewed script") from exc
        if any(k in resources for k in ("mem_per_cpu", "gpus_per_task", "gpus_per_node", "gres", "ntasks_per_node")):
            raise ValueError("Normalize campaign resource requests explicitly before comparing this homogeneous policy subset")
        if (nodes != job["nodes"] or cpus != job["nodes"]*job["cpus_per_node"] or
                gpus != job["nodes"]*job["gpus_per_node"] or memory != job["memory_mb_per_node"]*1048576 or
                wall != job["wall_limit"] or resources.get("account") != job["account"] or resources.get("qos") != job["qos"]):
            raise ValueError("Modeled request differs from the immutable reviewed resources")
    limits = document["limits"]
    object_(limits, {"max_running", "max_submit_jobs", "grp_cpus", "grp_memory_mb", "grp_gpus", "commands_per_minute",
                     "external_jobs", "external_cpus", "external_memory_mb", "external_gpus", "complete"},
            {"starvation_seconds", "fairshare_delay_seconds", "source"}, "known policy limits")
    for key in ("max_running", "max_submit_jobs", "commands_per_minute"):
        integer(limits[key], key, 1, 100000)
    for key in ("grp_cpus", "grp_memory_mb", "grp_gpus", "external_jobs", "external_cpus", "external_memory_mb", "external_gpus"):
        integer(limits[key], key, 0, 10**12)
    if type(limits["complete"]) is not bool:
        raise ValueError("Known-limit completeness must be an explicit boolean")
    number(limits.get("starvation_seconds", 3600), "starvation threshold", low=1, high=1e9)
    number(limits.get("fairshare_delay_seconds", 60), "fairshare maximum delay", high=86400)
    if document.get("policy", "fifo") not in POLICIES:
        raise ValueError("Choose a supported campaign policy")
    if "receipt" in document:
        receipt = X._validate_receipt(document["receipt"])
        if receipt["review"] != review:
            raise ValueError("Execution receipt belongs to a different immutable review")
    return document


def _units(job):
    return (job["cpus_per_node"]*job["nodes"], job["memory_mb_per_node"]*job["nodes"], job["gpus_per_node"]*job["nodes"])


def _fits(job, active, limits):
    if len(active)+limits["external_jobs"] >= min(limits["max_running"], limits["max_submit_jobs"]):
        return False
    requested = _units(job)
    total = [sum(_units(a)[i] for a in active) for i in range(3)]
    return all(total[i] + requested[i] + limits[external] <= limits[maximum]
               for i, (external, maximum) in enumerate((("external_cpus", "grp_cpus"), ("external_memory_mb", "grp_memory_mb"), ("external_gpus", "grp_gpus"))))


def _key(policy, job, now, model, limits, order):
    age = now-job["submit"]
    if age >= limits.get("starvation_seconds", 3600):
        return (0, job["submit"], order[job["id"]])
    if policy == "shortest":
        score = job["duration"]
    elif policy == "backfill-friendly":
        score = job["wall_limit"] * max(1, _units(job)[0])
    elif policy == "fairshare":
        account = model["policy"]["accounts"][job["account"]]
        score = account["usage_cpu_seconds"] * math.pow(2.0, -max(0, now-job["submit"])/model["policy"]["usage_half_life_seconds"]) / account["weight"]
    else:
        score = order[job["id"]]
    return (1, score, order[job["id"]])


def schedule(document, policy, *, factor=1., cancel=None, work_budget=None):
    model, limits = document["queue_model"], document["limits"]
    work_budget = work_budget if work_budget is not None else {"remaining": MAX_WORK}
    jobs = copy.deepcopy(model["jobs"])
    order = {n["id"]: i for i, n in enumerate(document["review"]["nodes"])}
    pending = {j["id"]: j for j in jobs}
    admitted, blocked, timeline, decisions = [], {}, {}, []
    now = min(j["submit"] for j in jobs)
    origin = now
    next_command = now
    steps = 0
    while pending:
        _check(cancel)
        steps += 1
        if steps > 6*len(jobs)+8:
            raise ValueError("Campaign admission event budget exhausted")
        completed = {k for k, row in timeline.items() if row["end"] <= now and not row["runtime_clipped"]}
        failed = {k for k, row in timeline.items() if row["end"] <= now and (row["runtime_clipped"] or not next(j for j in jobs if j["id"] == k).get("success", True))}
        completed -= failed
        for key, job in list(pending.items()):
            if any(dep in failed or dep in blocked for dep in job["depends"]):
                blocked[key] = "Upstream afterok condition failed; no descendant admission"
                del pending[key]
        active = [j for j in admitted if timeline.get(j["id"], {}).get("end", float("inf")) > now]
        available = [j for j in pending.values() if j["submit"] <= now and set(j["depends"]) <= completed]
        available.sort(key=lambda j: _key(policy, j, now, model, limits, order))
        chosen = None
        if now >= next_command:
            for job in available:
                if policy == "fairshare":
                    usage = model["policy"]["accounts"][job["account"]]["usage_cpu_seconds"]
                    if usage > 0 and now < job["submit"] + limits.get("fairshare_delay_seconds", 60):
                        continue
                if _fits(job, active, limits):
                    chosen = job
                    break
                if policy in {"fifo", "shortest"}:
                    break  # Explicit head-of-line policy; alternatives may fill gaps.
        if chosen is not None:
            admitted.append(dict(chosen, submit=now))
            del pending[chosen["id"]]
            scenario = dict(model, jobs=admitted, traces=[])
            calculated = S._run(scenario, factor=factor, cancel=cancel, work_budget=work_budget)
            timeline = {r["id"]: r for r in calculated["timeline"]}
            blocked.update(calculated["blocked"])
            decisions.append({"id": chosen["id"], "admit_at": now, "active_before": [j["id"] for j in active],
                              "requested_units": list(_units(chosen))})
            next_command = now + 60/limits["commands_per_minute"]
            continue
        future = [row["end"] for row in timeline.values() if row["end"] > now]
        future += [j["submit"] for j in pending.values() if j["submit"] > now]
        if next_command > now and any(_fits(job, active, limits) for job in available):
            future.append(next_command)
        if policy == "fairshare":
            future += [j["submit"] + limits.get("fairshare_delay_seconds", 60) for j in available
                       if j["submit"] + limits.get("fairshare_delay_seconds", 60) > now]
        if not future:
            for key in pending:
                blocked[key] = "Declared policy/resource limits or an unavailable dependency prevent admission"
            break
        now = min(future)
    rows = list(timeline.values())
    end = max((r["end"] for r in rows), default=origin)
    waits = sorted(r["start"]-next(j["submit"] for j in jobs if j["id"] == r["id"]) for r in rows)
    waste = sum((r["end"]-r["start"])*_units(next(j for j in jobs if j["id"] == r["id"]))[0]
                for r in rows if r["runtime_clipped"] or not next(j for j in jobs if j["id"] == r["id"]).get("success", True))
    span = max(1., end-origin)
    return {"policy": policy, "decisions": decisions, "timeline": rows, "blocked": blocked,
            "metrics": {"makespan_seconds": end-origin, "throughput_jobs_per_hour": len(rows)*3600/span,
                        "p95_wait_seconds": waits[math.ceil(.95*len(waits))-1] if waits else None,
                        "maximum_wait_seconds": max(waits, default=None), "starvation_count": sum(w >= limits.get("starvation_seconds", 3600) for w in waits),
                        "modeled_wasted_cpu_seconds": waste, "proposed_scheduler_commands": len(decisions),
                        "commands_per_minute": len(decisions)*60/span, "completion_coverage": len(rows)/len(jobs)},
            "events": steps}


def analyze(document, *, cancel=None):
    validate(document)
    work_budget = {"remaining": MAX_WORK}
    comparisons = [schedule(document, p, cancel=cancel, work_budget=work_budget) for p in POLICIES]
    baseline = comparisons[0]["metrics"]
    for result in comparisons:
        result["deltas_vs_fifo"] = {key: value-baseline[key] for key, value in result["metrics"].items()
                                    if isinstance(value, (int, float)) and isinstance(baseline[key], (int, float))}
        result["forecast_error_sensitivity"] = [dict(runtime_factor=factor, **schedule(document, result["policy"], factor=factor, cancel=cancel, work_budget=work_budget)["metrics"])
                                                for factor in (.5, 2.)]
    receipt = document.get("receipt")
    states = {n["id"]: n["state"] for n in receipt["nodes"]} if receipt else {n["id"]: "planned" for n in document["review"]["nodes"]}
    unknown = [key for key, state in states.items() if state in {"unknown", "launching", "rejected", "not_submitted"}]
    selected = next(r for r in comparisons if r["policy"] == document.get("policy", "fifo"))
    safe = document["limits"]["complete"] and not unknown
    # A shadow preview never clears actual receipt states or dependencies.
    completed = {key for key, state in states.items() if state == "completed"}
    active = [j for j in document["queue_model"]["jobs"] if states[j["id"]] in {"accepted", "pending", "running"}]
    previews = {}
    for comparison in comparisons:
        eligible = []
        occupied = list(active)
        for decision in comparison["decisions"] if safe else []:
            job = next(j for j in document["queue_model"]["jobs"] if j["id"] == decision["id"])
            if states[job["id"]] == "planned" and set(job["depends"]) <= completed and _fits(job, occupied, document["limits"]):
                eligible.append({"id": job["id"], "not_before": decision["admit_at"]})
                occupied.append(job)
        previews[comparison["policy"]] = eligible
    return {"schema": "raven.campaign-policy-result/v1", "model": MODEL, "input_sha256": digest(document),
            "review_id": document["review"]["review_id"], "scope": document["review"]["scope"],
            "receipt_digest": digest(receipt) if receipt else None, "status": "shadow" if safe else "blocked",
            "selected_policy": selected["policy"], "shadow_decisions": previews[selected["policy"]], "policy_previews": previews,
            "eligible_nodes": [r["id"] for r in previews[selected["policy"]]], "uncertain_nodes": unknown,
            "comparisons": comparisons, "production_qualified": False,
            "placement_operations": MAX_WORK-work_budget["remaining"], "placement_budget": MAX_WORK,
            "summary": "Review owned campaign admission policies; this result does not execute jobs.", "limits": list(LIMITS),
            "blocked_reason": "" if safe else "Known limits are incomplete or existing execution requires explicit reconciliation"}


def export_manifest(review, model_path, limits_path, destination, *, receipt=None):
    """Export a current sealed review without overwriting or submitting work."""
    from .planning_io import load_json
    from .operations import atomic_json
    value = {"schema": "raven.campaign-policy/v1", "review": copy.deepcopy(review),
             "queue_model": load_json(model_path, max_bytes=4 << 20),
             "limits": load_json(limits_path, max_bytes=64 << 10)}
    if receipt is not None:
        value["receipt"] = {key: copy.deepcopy(item) for key, item in receipt.items() if key != "receipt_path"}
    validate(value)
    atomic_json(destination, value)
    return str(destination)
