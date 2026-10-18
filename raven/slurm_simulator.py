"""Bounded observable queue model; a declared subset, never a Slurm replacement.

Nonpreemptive homogeneous-per-job placement, afterok dependencies, deterministic
priority, conservative EASY backfill, account usage weights, exclusive node
reservations and a small explicit QOS limit subset are supported. Hidden demand
and unsupported site policy remain outside the model.
"""
from __future__ import annotations

import heapq
import math
import statistics

from .evidence import bounded, digest
from .runtime_contracts import identifier, integer, number, object_, sequence, unique_names

MODEL = "observable-slurm-subset/1"
MAX_JOBS = 2000
MAX_STEPS = 5_000_000
LIMITS = [
    "Experimental observable subset: no hidden jobs, full Slurm multifactor priority, topology, licenses, federation, heterogeneous components or preemption.",
    "Resource requests are homogeneous per node; integer CPU/GPU counts and memory MiB use deterministic first-fit placement.",
    "Backfill conservatively requires a lower-priority job to finish within the protected head reservation; wall limits reserve capacity.",
    "Fairshare is an explicit decayed account-usage score, not Slurm's full hierarchical fair-tree algorithm.",
    "QOS supports only declared running-job, CPU and wall-time limits. Missing policies are unknown, not observed unlimited capacity.",
    "Calibration and held-out validation use different trace identities and chronological windows; simulated outcomes are not real ground truth.",
]


def _jobs(values, config):
    jobs = sequence(values, "jobs", MAX_JOBS)
    ids = set()
    for job in jobs:
        object_(job, {"id", "submit", "duration", "wall_limit", "nodes", "cpus_per_node", "memory_mb_per_node", "gpus_per_node", "priority", "account", "qos", "depends"},
                {"success", "actual_start", "actual_end", "metadata"}, "job")
        jid = identifier(job["id"], "job identity")
        if jid in ids:
            raise ValueError("Duplicate job identity")
        ids.add(jid)
        number(job["submit"], "submission timestamp", high=1e12)
        for key in ("duration", "wall_limit"):
            number(job[key], key, low=1e-6, high=1e9, positive=True)
        if job["duration"] > job["wall_limit"]:
            raise ValueError("Declared duration exceeds wall limit; timeout semantics require a separate scenario")
        for key in ("nodes", "cpus_per_node"):
            integer(job[key], key, 1, 100000)
        for key in ("gpus_per_node", "memory_mb_per_node", "priority"):
            integer(job[key], key, 0, 10**12)
        for key, dictionary in (("account", config["policy"]["accounts"]), ("qos", config["policy"]["qos"])):
            if not isinstance(job[key], str) or job[key] not in dictionary:
                raise ValueError("Job references an undeclared " + key)
        if "success" in job and type(job["success"]) is not bool:
            raise ValueError("success must be an explicit boolean")
        unique_names(job["depends"], "afterok dependencies", MAX_JOBS)
        if ("actual_start" in job) != ("actual_end" in job):
            raise ValueError("Measured attempts require both actual_start and actual_end")
        if "actual_start" in job:
            start = number(job["actual_start"], "measured start", high=1e12)
            end = number(job["actual_end"], "measured end", high=1e12)
            if not job["submit"] <= start < end:
                raise ValueError("Measured submit/start/end ordering is invalid")
    if any(dep not in ids for job in jobs for dep in job["depends"]):
        raise ValueError("Dependencies must be present; hidden dependencies cannot be guessed")
    degree = {job["id"]: len(job["depends"]) for job in jobs}
    children = {key: [] for key in degree}
    for job in jobs:
        for dep in job["depends"]:
            children[dep].append(job["id"])
    ready, visited = [key for key, count in degree.items() if not count], 0
    while ready:
        key = ready.pop()
        visited += 1
        for child in children[key]:
            degree[child] -= 1
            if not degree[child]:
                ready.append(child)
    if visited != len(jobs):
        raise ValueError("Dependency cycle in declared jobs")
    return jobs


def validate(document):
    bounded(document)
    object_(document, {"schema", "id", "nodes", "policy", "jobs"},
            {"reservations", "traces", "seed", "hidden_delay_seconds", "metadata", "observability"}, "queue model")
    if document["schema"] != "raven.queue-model/v1":
        raise ValueError("Expected raven.queue-model/v1")
    identifier(document["id"], "model identity")
    integer(document.get("seed", 0), "receipt seed", 0, 2**32 - 1)
    number(document.get("hidden_delay_seconds", 60), "hidden-state sensitivity delay", high=1e9)
    nodes = sequence(document["nodes"], "nodes", 256, 1)
    names = set()
    for node in nodes:
        object_(node, {"id", "cpus", "memory_mb", "gpus"}, name="node")
        ident = identifier(node["id"], "node name")
        if ident in names:
            raise ValueError("Duplicate node identity")
        names.add(ident)
        integer(node["cpus"], "node CPUs", 1, 100000)
        integer(node["memory_mb"], "node memory", 0, 10**12)
        integer(node["gpus"], "node GPUs", 0, 100000)
    policy = document["policy"]
    object_(policy, {"backfill", "fairshare_weight", "usage_half_life_seconds", "accounts", "qos"}, name="model policy")
    if type(policy["backfill"]) is not bool:
        raise ValueError("backfill must be true or false")
    number(policy["fairshare_weight"], "fairshare weight", high=1e12)
    number(policy["usage_half_life_seconds"], "usage half life", high=1e12, positive=True)
    for name in ("accounts", "qos"):
        values = policy[name]
        if not isinstance(values, dict) or not 1 <= len(values) <= 256:
            raise ValueError(name + " must declare 1..256 entries")
        for key, value in values.items():
            identifier(key, name)
            if name == "accounts":
                object_(value, {"weight", "usage_cpu_seconds"}, name="account")
                number(value["weight"], "account weight", high=1e9, positive=True)
                number(value["usage_cpu_seconds"], "historical CPU usage", high=1e18)
            else:
                object_(value, {"max_running", "max_cpus", "max_wall_seconds"}, name="QOS limits")
                integer(value["max_running"], "QOS running limit", 1, 100000)
                integer(value["max_cpus"], "QOS CPU limit", 1, 10000000)
                number(value["max_wall_seconds"], "QOS wall limit", high=1e9, positive=True)
    reservations = sequence(document.get("reservations", []), "reservations", 256)
    reservation_ids = set()
    for item in reservations:
        object_(item, {"id", "start", "end", "nodes", "accounts"}, name="reservation")
        rid = identifier(item["id"], "reservation ID")
        if rid in reservation_ids:
            raise ValueError("Duplicate reservation ID")
        reservation_ids.add(rid)
        number(item["start"], "reservation start", high=1e12)
        number(item["end"], "reservation end", high=1e12)
        if item["start"] >= item["end"]:
            raise ValueError("Reservation has an empty or reversed window")
        unique_names(item["nodes"], "reservation nodes", 256)
        unique_names(item["accounts"], "reservation accounts", 256)
        if not set(item["nodes"]) <= names or not set(item["accounts"]) <= set(policy["accounts"]):
            raise ValueError("Reservation references an undeclared node or account")
    _jobs(document["jobs"], document)
    return document


def _run(document, *, factor=1., cancel=None, initial_delay=0., work_budget=None):
    jobs, nodes, policy = document["jobs"], document["nodes"], document["policy"]
    by_id = {job["id"]: job for job in jobs}
    waiting = sorted(jobs, key=lambda job: (job["submit"], job["id"]))
    running, completed, failed, output, pending = [], set(), set(), [], []
    blocked = {}
    resources = {node["id"]: [node["cpus"], node["memory_mb"], node["gpus"]] for node in nodes}
    usage = {account: value["usage_cpu_seconds"] for account, value in policy["accounts"].items()}
    reservations = document.get("reservations", [])
    now = min((job["submit"] for job in jobs), default=0.)
    delay_until, last_usage = now + initial_delay, now
    next_job, steps, events = 0, 0, 0

    def touch():
        nonlocal steps
        steps += 1
        if work_budget is not None:
            work_budget["remaining"] -= 1
            if work_budget["remaining"] < 0:
                raise ValueError("Combined queue-policy comparison exhausted its placement-operation budget")
        if steps > MAX_STEPS:
            raise ValueError("Queue simulation exceeds its 5,000,000 placement-operation budget")
        if steps % 256 == 0 and cancel is not None and cancel.is_set():
            raise ValueError("Queue simulation cancelled")

    def placement(job, at, active):
        touch()
        if at < delay_until:
            return None
        qos = policy["qos"][job["qos"]]
        same = [row for row in active if row[2]["qos"] == job["qos"]]
        if len(same) >= qos["max_running"] or sum(row[2]["nodes"] * row[2]["cpus_per_node"] for row in same) + job["nodes"] * job["cpus_per_node"] > qos["max_cpus"]:
            return None
        available = {key: list(value) for key, value in resources.items()}
        for _, _, other, assigned, _ in active:
            for name in assigned:
                for index, key in enumerate(("cpus_per_node", "memory_mb_per_node", "gpus_per_node")):
                    available[name][index] -= other[key]
        assigned = []
        for name in sorted(available):
            touch()
            if any(item["start"] < at + job["wall_limit"] and item["end"] > at and name in item["nodes"] and job["account"] not in item["accounts"] for item in reservations):
                continue
            if all(available[name][index] >= job[key] for index, key in enumerate(("cpus_per_node", "memory_mb_per_node", "gpus_per_node"))):
                assigned.append(name)
                if len(assigned) == job["nodes"]:
                    return assigned
        return None

    def priority(job):
        share = policy["accounts"][job["account"]]["weight"] / (1 + usage[job["account"]])
        return (-job["priority"] - policy["fairshare_weight"] * share, job["submit"], job["id"])

    def protected_start(job):
        times = sorted({row[4] for row in running} | {item["end"] for item in reservations if item["end"] > now} | ({delay_until} if delay_until > now else set()))
        for at in times:
            # Reserve against declared wall limits, never an oracle's duration.
            if placement(job, at, [row for row in running if row[4] > at]):
                return at
        return math.inf

    while next_job < len(waiting) or pending or running:
        events += 1
        if events > 10000:
            raise ValueError("Queue simulation exceeds its 10,000-event budget")
        if cancel is not None and cancel.is_set():
            raise ValueError("Queue simulation cancelled")
        decay = 2 ** (-(now - last_usage) / policy["usage_half_life_seconds"])
        usage = {account: value * decay for account, value in usage.items()}
        last_usage = now
        while running and running[0][0] <= now:
            end, ident, job, assigned, wall_end = heapq.heappop(running)
            completed.add(ident)
            if not job.get("success", True) or job["duration"] * factor > job["wall_limit"]:
                failed.add(ident)
            usage[job["account"]] += (min(job["duration"] * factor, job["wall_limit"])) * job["cpus_per_node"] * job["nodes"]
        while next_job < len(waiting) and waiting[next_job]["submit"] <= now:
            pending.append(waiting[next_job])
            next_job += 1
        eligible = []
        for job in pending:
            touch()
            qos = policy["qos"][job["qos"]]
            if job["wall_limit"] > qos["max_wall_seconds"]:
                blocked[job["id"]] = "Declared wall limit exceeds known QOS limit"
            elif any(dep in failed or dep in blocked for dep in job["depends"]):
                blocked[job["id"]] = "afterok dependency cannot succeed"
            elif all(dep in completed for dep in job["depends"]):
                eligible.append(job)
        pending = [job for job in pending if job["id"] not in blocked]
        eligible.sort(key=priority)
        protected = None
        started = set()
        for job in eligible:
            assigned = placement(job, now, running)
            if protected is not None and now + job["wall_limit"] > protected:
                continue
            if assigned:
                duration = min(job["duration"] * factor, job["wall_limit"])
                heapq.heappush(running, (now + duration, job["id"], job, assigned, now + job["wall_limit"]))
                started.add(job["id"])
                output.append({"id": job["id"], "submit": job["submit"], "start": now, "end": now + duration,
                               "wait_seconds": now - job["submit"], "nodes": assigned, "reserved_until": now + job["wall_limit"],
                               "runtime_clipped": job["duration"] * factor > job["wall_limit"]})
            elif protected is None:
                if not policy["backfill"]:
                    break
                protected = protected_start(job)
        pending = [job for job in pending if job["id"] not in started]
        future = ([waiting[next_job]["submit"]] if next_job < len(waiting) else []) + ([running[0][0]] if running else [])
        future += [item[key] for item in reservations for key in ("start", "end") if item[key] > now]
        if delay_until > now:
            future.append(delay_until)
        if not future:
            for job in pending:
                blocked[job["id"]] = "No feasible declared resource/policy/dependency event remains"
            break
        now = min(value for value in future if value > now)
    return {"timeline": output, "blocked": blocked, "events": events, "placement_operations": steps,
            "makespan_seconds": max((row["end"] for row in output), default=0) - min((job["submit"] for job in jobs), default=0),
            "completion_coverage": len(output) / len(jobs) if jobs else 1.}


def simulate(document, *, cancel=None):
    validate(document)
    traces = sequence(document.get("traces", []), "independent traces", 20)
    ids, attempts, training_ends, testing_submits, ratios = set(), set(), [], [], []
    for trace in traces:
        object_(trace, {"id", "role", "origin", "jobs"}, {"source_sha256"}, "trace")
        identifier(trace["id"], "trace identity")
        if trace["id"] in ids:
            raise ValueError("Calibration and validation must have distinct trace identities")
        ids.add(trace["id"])
        if trace["role"] not in ("calibration", "validation") or trace["origin"] not in ("recorded", "synthetic"):
            raise ValueError("Trace role/origin must distinguish calibration/validation and recorded/synthetic")
        for job in _jobs(trace["jobs"], document):
            if "actual_start" not in job:
                raise ValueError("Trace evaluation requires actual start/end observations")
            key = (job["id"], job["submit"], job["actual_start"])
            if key in attempts:
                raise ValueError("A measured attempt cannot appear in both calibration and validation")
            attempts.add(key)
            if len(attempts) > 10000:
                raise ValueError("Trace corpus exceeds 10,000 measured attempts")
            if trace["role"] == "calibration":
                training_ends.append(job["actual_end"])
                ratios.append((job["actual_end"] - job["actual_start"]) / job["duration"])
            else:
                testing_submits.append(job["submit"])
    if training_ends and testing_submits and max(training_ends) > min(testing_submits):
        raise ValueError("Validation must occur strictly after the calibration observation window")
    factor = statistics.median(ratios) if ratios else 1.
    if not .1 <= factor <= 10:
        raise ValueError("Runtime calibration factor is outside the supported 0.1..10 domain")
    result = _run(document, factor=factor, cancel=cancel)
    evaluations = []
    for trace in traces:
        if trace["role"] != "validation":
            continue
        fixture = dict(document, jobs=trace["jobs"])
        predicted = _run(fixture, factor=factor, cancel=cancel)
        by_id = {row["id"]: row for row in predicted["timeline"]}
        covered = [job for job in trace["jobs"] if job["id"] in by_id]
        errors = [abs(by_id[job["id"]]["start"] - job["actual_start"]) for job in covered]
        pairs = correct = 0
        for index, left in enumerate(covered):
            if pairs >= 50000:
                break
            for right in covered[index + 1:]:
                if pairs >= 50000:
                    break
                actual = left["actual_start"] - right["actual_start"]
                if actual:
                    pairs += 1
                    estimate = by_id[left["id"]]["start"] - by_id[right["id"]]["start"]
                    correct += (actual < 0 and estimate < 0) or (actual > 0 and estimate > 0)
        evaluations.append({"trace_id": trace["id"], "origin": trace["origin"], "samples": len(errors),
                            "coverage": len(errors) / len(trace["jobs"]) if trace["jobs"] else 0,
                            "start_mae_seconds": statistics.mean(errors) if errors else None,
                            "wait_mae_seconds": statistics.mean(errors) if errors else None,
                            "tail_start_error_seconds": sorted(errors)[max(0, math.ceil(.95 * len(errors)) - 1)] if errors else None,
                            "ranking_accuracy": correct / pairs if pairs else None, "ranked_pairs": pairs})
    delayed = _run(document, factor=factor, cancel=cancel, initial_delay=document.get("hidden_delay_seconds", 60))
    actual_by_id = {row["id"]: row for row in result["timeline"]}
    shifts = [row["start"] - actual_by_id[row["id"]]["start"] for row in delayed["timeline"] if row["id"] in actual_by_id]
    return {"schema": "raven.queue-model-result/v1", "status": "experimental", "model": MODEL, "input_sha256": digest(document),
            "seed": document.get("seed", 0), "parameters": {"runtime_factor": factor, "calibration_samples": len(ratios)},
            **result, "validation": evaluations,
            "hidden_state_sensitivity": {"assumed_all_nodes_blocked_seconds": document.get("hidden_delay_seconds", 60),
                                         "maximum_start_shift_seconds": max(shifts) if shifts else None},
            "limits": list(LIMITS), "production_qualified": False,
            "summary": "A deterministic declared-policy simulation; observed-site fidelity requires independent held-out qualification."}
