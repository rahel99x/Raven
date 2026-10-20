"""Bounded, read-only explanations of scheduler reasons and dependency evidence.

This module consumes a snapshot. It never queries Slurm, releases a hold, or
predicts a start time. Capacity screening is conditional on the supplied node
metadata and does not reproduce Slurm's placement or priority calculation.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import math
import re
from itertools import islice

from . import clock

MAX_RECORDS = 20000
MAX_TEXT = 1024
MAX_DEPENDENCY_TEXT = 8192
MAX_CLAUSES = 64
MAX_TARGETS = 128
MAX_TREE = 128
MAX_DEPTH = 12
MAX_TARGET_EVALUATIONS = 1024
MAX_EVIDENCE = 96
MAX_NODES = 4096
STALE_SECONDS = 120
_ID = re.compile(r"\d{1,20}(?:_\d{1,20})?\Z")
_TERMINAL = frozenset(("COMPLETED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL", "PREEMPTED", "CANCELLED", "DEADLINE", "REVOKED"))
_FAILED = _TERMINAL - {"COMPLETED", "CANCELLED", "REVOKED"}
_STARTED = frozenset(("RUNNING", "COMPLETING", "SUSPENDED", "STAGE_OUT", "RESIZING"))
_NONPENDING = _TERMINAL | _STARTED | frozenset(("CONFIGURING", "REQUEUED", "REQUEUE_FED", "REQUEUE_HOLD", "SPECIAL_EXIT", "STOPPED", "RESV_DEL_HOLD", "R", "CG", "CF", "S", "CD", "F", "TO", "NF", "BF", "OOM", "CA", "PR", "RQ", "RH", "SE", "ST", "SO"))
_KINDS = frozenset(("after", "afterany", "afterok", "afternotok", "aftercorr", "afterburstbuffer", "singleton"))


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


def _text(value, limit=MAX_TEXT):
    if value is None:
        return ""
    if not isinstance(value, (str, int, float, bool)):
        return ""
    if isinstance(value, int) and value.bit_length() > 4096:
        return "<out of range>"
    return "".join(c if c.isprintable() else " " for c in str(value)[:limit])


def _num(value, maximum=10**15):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and 0 <= result <= maximum else None


def _integer(value, maximum=10**9):
    number = _num(value, maximum)
    return int(number) if number is not None and number.is_integer() else None


def _records(values, limit):
    if isinstance(values, Mapping):
        values = values.values()
    if isinstance(values, (str, bytes)) or values is None:
        return [], False
    try:
        items = list(islice(iter(values), limit + 1))
    except TypeError:
        return [], False
    return items[:limit], len(items) > limit


def _state(obj):
    return _text(_get(obj, "state", _get(obj, "JobState", "")), 64).split(" ", 1)[0].rstrip("+").upper()


def _job_id(obj):
    return _text(_get(obj, "id", _get(obj, "JobId", "")), 64)


def _timestamp(value):
    number = _num(value, 10**12)
    if number is not None:
        return number
    text = _text(value, 64)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _seconds(value):
    text = _text(value, 64)
    if text.upper() in {"UNLIMITED", "INFINITE", "N/A", "NONE", "", "PARTITION_TIME_LIMIT"}:
        return None
    match = re.fullmatch(r"(?:(\d{1,8})-)?(\d{1,8})(?::(\d{1,2}))?(?::(\d{1,2}(?:\.\d{1,6})?))?", text)
    if not match:
        return None
    days, a, b, c = match.groups()
    if b is not None and int(b) >= 60 or c is not None and float(c) >= 60:
        return None
    if c is not None:
        result = int(days or 0) * 86400 + int(a) * 3600 + int(b) * 60 + float(c)
    elif b is not None:
        result = int(days or 0) * 86400 + int(a) * 60 + int(b)
    else:
        # Slurm TimeLimit integer values are minutes.
        result = int(days or 0) * 86400 + int(a) * 60
    return result if result <= 10**12 else None


_REASONS = {
    "Resources": ("Resources unavailable", "Slurm reports that the requested resources are not currently available. This reason alone does not identify CPU, memory, GPU, or topology as the limiting resource.", ["Inspect per-node placement constraints and reservation metadata; aggregate free capacity may be fragmented."]),
    "Priority": ("Scheduler priority", "Slurm reports that higher-priority work currently takes precedence. Fairshare is only one possible priority component.", ["Inspect sprio and the site's priority policy; a numeric priority or fairshare value alone does not establish queue order."]),
    "Dependency": ("Dependency waiting", "Slurm reports an unsatisfied dependency. The dependency expression below separates observed predecessors from missing records.", ["Check the dependency expression and predecessor states; do not remove a dependency until its purpose is understood."]),
    "DependencyNeverSatisfied": ("Dependency cannot be satisfied", "Slurm reports that a dependency can never be satisfied. A failed afterok predecessor is one possible explanation, which requires predecessor evidence.", ["Check which dependency clause failed and whether a replacement prerequisite is appropriate."]),
    "JobHeldUser": ("User hold", "Slurm reports that the job is held by its user.", ["Inspect the hold and submission intent before an explicit release."]),
    "JobHeldAdmin": ("Administrator hold", "Slurm reports an administrator hold.", ["Check the administrative reason with the cluster operator; releasing it may require administrator privileges."]),
    "BeginTime": ("Scheduled begin time", "The requested begin time has not been reached according to Slurm.", ["Inspect BeginTime or EligibleTime and the cluster's time zone."]),
    "Reservation": ("Reservation availability", "The requested reservation is not currently available to this job.", ["Inspect the named reservation's time window, users, accounts, and resources."]),
    "ReqNodeNotAvail": ("Required node unavailable", "A requested node is unavailable. The scheduler may provide unavailable node names in the reason suffix.", ["Inspect requested/excluded nodes and maintenance, drain, or reservation state."]),
    "BadConstraints": ("Invalid placement constraints", "Slurm cannot satisfy the job's configured node constraints.", ["Inspect Features, GRES types, node count, and the selected partition."]),
    "PartitionDown": ("Partition unavailable", "The requested partition is down according to Slurm.", ["Inspect the partition state and maintenance announcements."]),
    "PartitionInactive": ("Partition inactive", "The requested partition is inactive according to Slurm.", ["Inspect the partition state and access policy."]),
    "PartitionNodeLimit": ("Partition node limit", "The requested node count conflicts with a partition limit.", ["Compare NumNodes with MinNodes and MaxNodes in partition metadata."]),
    "PartitionTimeLimit": ("Partition time limit", "The requested time limit exceeds a partition limit.", ["Compare TimeLimit with the selected partition's MaxTime."]),
    "PartitionConfig": ("Partition configuration", "The job conflicts with partition configuration.", ["Inspect partition constraints, access, node counts, and time limits."]),
    "InvalidAccount": ("Invalid account association", "Slurm reports that the account association is invalid for this job.", ["Check the user/account/partition association with the site's documented account policy."]),
    "InvalidQOS": ("Invalid QoS", "Slurm reports that the requested QoS is invalid or unavailable to the job.", ["Inspect association QoS access and the submitted QoS."]),
    "QOSNotAllowed": ("QoS access denied", "The requested QoS is not allowed for this association or partition.", ["Inspect the association and partition QoS access lists."]),
    "AccountNotAllowed": ("Account access denied", "The submitted account is not allowed in the selected partition.", ["Inspect the partition's AllowAccounts/DenyAccounts policy."]),
    "Licenses": ("License unavailable", "The requested licensed resource is not available according to Slurm.", ["Inspect requested licenses and license availability; CPU or memory changes do not establish license availability."]),
    "AssociationResourceLimit": ("Association resource limit", "A configured association resource limit blocks this job.", ["Inspect association usage and its configured limits with an authorized accounting command."]),
    "AssociationJobLimit": ("Association job limit", "A configured association job-count limit blocks this job.", ["Inspect running or submitted job counts and the association's limits."]),
    "AssociationTimeLimit": ("Association time limit", "A configured association time limit blocks this job.", ["Inspect association wall-time limits and cumulative usage."]),
    "QOSResourceLimit": ("QoS resource limit", "A configured QoS resource limit blocks this job.", ["Inspect QoS usage and limits; the snapshot does not include the complete policy."]),
    "QOSJobLimit": ("QoS job limit", "A configured QoS job-count limit blocks this job.", ["Inspect QoS job-count usage and limits."]),
    "QOSTimeLimit": ("QoS time limit", "A configured QoS time limit blocks this job.", ["Inspect QoS wall-time limits and cumulative usage."]),
    "JobArrayTaskLimit": ("Array concurrency limit", "The job array's configured task concurrency limit has been reached.", ["Inspect the array's %concurrency limit and active task count."]),
    "MaxSubmitJobsPerAccount": ("Account submission limit", "The account's permitted submitted-job count has been reached.", ["Inspect the account's submitted-job usage and configured limit."]),
    "MaxSubmitJobsPerUser": ("User submission limit", "The user's permitted submitted-job count has been reached.", ["Inspect the user's submitted-job usage and configured limit."]),
    "JobLaunchFailure": ("Job launch failure", "Slurm reports a launch failure; the reason does not establish its underlying cause.", ["Inspect controller/step logs, prolog failures, and node health."]),
    "Cleaning": ("Allocation cleanup", "Slurm is waiting for allocation cleanup.", ["Inspect completing work and node epilog or cleanup state."]),
    "Prolog": ("Prolog waiting", "Slurm is waiting for a job prolog.", ["Inspect prolog status with the cluster operator."]),
    "SystemFailure": ("Scheduler system failure", "Slurm reports a system failure.", ["Inspect scheduler and node health with the cluster operator."]),
    "FrontEndDown": ("Frontend unavailable", "Slurm reports that the required frontend is down.", ["Inspect the site's frontend health and maintenance status."]),
    "WaitingForScheduling": ("Scheduling pending", "Slurm has not completed scheduling this job yet.", ["Refresh the job snapshot; this reason does not identify a resource bottleneck."]),
    "FedJobLock": ("Federation lock", "The job is waiting for a federation scheduling lock.", ["Inspect federation state with the cluster operator."]),
    "BurstBufferResources": ("Burst buffer resources", "Requested burst-buffer resources are unavailable.", ["Inspect burst-buffer capacity and allocation policy."]),
    "BurstBufferStageIn": ("Burst buffer stage-in", "Slurm is waiting for burst-buffer data staging.", ["Inspect the stage-in operation and data transfer status."]),
}

# These are exact documented limit families, not guesses based on a QOS/Assoc
# prefix. Unknown future reason codes stay unknown.
for _scope in ("QOS", "Assoc"):
    for _suffix in ("GrpCpuLimit", "GrpCPUMinutesLimit", "GrpCPURunMinutesLimit", "GrpMemLimit", "GrpNodeLimit", "GrpJobsLimit", "GrpSubmitJobsLimit", "GrpWallLimit", "MaxCpuPerJobLimit", "MaxCpuPerNode", "MaxNodePerJobLimit", "MaxWallDurationPerJobLimit", "MaxJobsLimit", "MaxSubmitJobLimit", "MaxJobsPerUserLimit", "MaxSubmitJobPerUserLimit", "MaxJobsPerAccountLimit", "MaxSubmitJobPerAccountLimit", "MaxMemoryPerJob", "MaxGRESPerJob", "MaxGRESPerNode", "MinGRES", "GrpGRES", "GrpGRESMinutes", "GrpGRESRunMinutes"):
        _REASONS[_scope + _suffix] = (f"{_scope} policy limit", f"Slurm reports the {_scope + _suffix} policy limit. The snapshot does not contain the full limit values or all concurrent usage.", [f"Inspect the exact {_scope} limit named {_suffix} and its current usage with authorized accounting tools."])


def _reason(raw):
    text = _text(raw)
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    match = re.match(r"^([A-Za-z][A-Za-z0-9_]*)(.*)$", text)
    return {"code": match.group(1) if match else "", "suffix": match.group(2).lstrip(" ,:") if match else text, "raw": text}


def _combine(statuses, operator):
    if not statuses:
        return "satisfied"
    if operator == "or":
        if "satisfied" in statuses:
            return "satisfied"
        if all(s == "impossible" for s in statuses):
            return "impossible"
        return "unknown" if "unknown" in statuses else "waiting"
    if "impossible" in statuses:
        return "impossible"
    if "waiting" in statuses:
        return "waiting"
    return "unknown" if "unknown" in statuses else "satisfied"


def _parse_dependencies(value):
    text = _text(value, MAX_DEPENDENCY_TEXT + 1)
    out = {"operator": "none", "status": "satisfied", "clauses": [], "tree": [], "truncated": False}
    if text in {"", "(null)", "None", "NONE", "N/A"}:
        return out
    if len(text) > MAX_DEPENDENCY_TEXT:
        out.update(operator="invalid", status="unknown", truncated=True)
        return out
    if "," in text and "?" in text:
        out.update(operator="invalid", status="unknown")
        return out
    out["operator"] = "or" if "?" in text else "and"
    clauses = text.split("?" if out["operator"] == "or" else ",")
    if len(clauses) > MAX_CLAUSES:
        out.update(status="unknown", truncated=True)
        return out
    target_count = 0
    for original in clauses:
        annotation = None
        clause = original.strip()
        mark = re.search(r"\((fulfilled|unfulfilled)\)$", clause)
        if mark:
            annotation = mark.group(1)
            clause = clause[:mark.start()]
        bits = clause.split(":")
        kind = bits[0]
        targets = []
        invalid = kind not in _KINDS or (kind == "singleton" and len(bits) != 1) or (kind != "singleton" and len(bits) < 2)
        remaining = MAX_TARGETS - target_count
        if len(bits) - 1 > remaining:
            out["truncated"] = True
        for bit in bits[1:remaining + 1]:
            match = re.fullmatch(r"(\d{1,20}(?:_\d{1,20})?)(?:\+(\d{1,8}))?", bit)
            if not match or (match.group(2) and kind != "after"):
                invalid = True
                continue
            targets.append({"job_id": match.group(1), "delay_minutes": int(match.group(2) or 0), "state": "", "status": "unknown", "summary": "Dependency target has not been evaluated."})
        target_count += len(targets)
        out["clauses"].append({"kind": _text(kind, 32), "targets": targets, "annotation": annotation, "status": "unknown" if invalid else "waiting", "invalid": invalid, "truncated": len(bits) - 1 > remaining, "summary": "Malformed or unsupported dependency clause." if invalid else ""})
    if any(c["invalid"] for c in out["clauses"]):
        out["status"] = "unknown"
    return out


def _target(kind, target, index, owner, now):
    jid = target["job_id"]
    if kind == "aftercorr":
        owner_id = _job_id(owner)
        if "_" not in owner_id:
            return dict(target, state="", status="unknown", summary="Corresponding array task index is absent; parent-array completion is not task evidence.", external=jid not in index)
        task = owner_id.rsplit("_", 1)[1]
        jid = jid.split("_", 1)[0] + "_" + task
    predecessor = index.get(jid)
    result = dict(target, job_id=jid, state=_state(predecessor) if predecessor else "", status="unknown", external=predecessor is None)
    if predecessor is None:
        result["summary"] = "Predecessor is outside the supplied queue/history snapshot; absence does not mean failure or completion."
        return result
    state = result["state"]
    terminal = state in _TERMINAL
    exitcode = _text(_get(predecessor, "exit", _get(predecessor, "ExitCode", "")), 32)
    nonzero = bool(re.fullmatch(r"\d{1,9}:\d{1,9}", exitcode) and any(int(n) for n in exitcode.split(":")))
    if kind in {"afterok", "aftercorr"}:
        if state == "COMPLETED" and not nonzero:
            result["status"] = "satisfied"
        elif state == "COMPLETED" and nonzero:
            result["summary"] = "Completed state conflicts with a non-zero recorded exit; verify accounting."
        elif terminal:
            result["status"] = "impossible"
        elif state:
            result["status"] = "waiting"
    elif kind == "afterany":
        result["status"] = "satisfied" if terminal else "waiting" if state else "unknown"
    elif kind == "afternotok":
        if state == "COMPLETED" and nonzero:
            result["summary"] = "Completed state conflicts with a non-zero recorded exit; verify accounting."
        elif state in _FAILED or (terminal and nonzero):
            result["status"] = "satisfied"
        elif state == "COMPLETED" and not nonzero:
            result["status"] = "impossible"
        elif not terminal and state:
            result["status"] = "waiting"
        else:
            result["summary"] = "Cancellation/revocation semantics and exit outcome need scheduler evidence."
    elif kind == "after":
        began = state in _STARTED or terminal
        delay = target["delay_minutes"] * 60
        if began and not delay:
            result["status"] = "satisfied"
        elif began:
            start = _timestamp(_get(predecessor, "start", _get(predecessor, "StartTime")))
            if state == "CANCELLED" and start is None:
                start = _timestamp(_get(predecessor, "end", _get(predecessor, "EndTime")))
            result["status"] = ("satisfied" if now >= start + delay else "waiting") if start is not None else "unknown"
        elif state:
            result["status"] = "waiting"
    elif kind == "afterburstbuffer":
        result["summary"] = "Job termination does not prove burst-buffer stage-out completion; explicit stage-out metadata is required."
    result.setdefault("summary", f"Observed {state or 'unknown'} predecessor; {kind} is {result['status']} in this snapshot.")
    return result


def _dependencies(job, index, peers, now):
    root = _parse_dependencies(_get(job, "dependency", _get(job, "Dependency", "")))
    seen_edges = set()
    parsed_cache = {}
    evaluations = 0

    def evaluate(owner):
        nonlocal evaluations
        jid = _job_id(owner)
        if jid in parsed_cache:
            return parsed_cache[jid]
        result = root if owner is job else _parse_dependencies(_get(owner, "dependency", _get(owner, "Dependency", "")))
        parsed_cache[jid] = result
        for clause in result["clauses"]:
            if clause["invalid"]:
                continue
            if clause["kind"] == "singleton":
                username = _text(_get(owner, "user", _get(owner, "UserId", "")), 128).split("(", 1)[0]
                name = _text(_get(owner, "name", _get(owner, "JobName", "")))
                submitted = _timestamp(_get(owner, "submit", _get(owner, "SubmitTime")))
                same = peers.get((username, name), ()) if username and name else ()
                candidates = []
                uncertain = not (username and name and submitted is not None)
                for peer in same[:MAX_TARGETS + 1]:
                    if _job_id(peer) == jid or _state(peer) in _TERMINAL:
                        continue
                    peer_submit = _timestamp(_get(peer, "submit", _get(peer, "SubmitTime")))
                    if peer_submit is None or submitted is None:
                        uncertain = True
                    elif peer_submit < submitted:
                        candidates.append({"job_id": _job_id(peer), "state": _state(peer), "status": "waiting", "summary": "Earlier same-user, same-name job remains active in the local snapshot.", "external": False})
                clause["targets"] = candidates[:MAX_TARGETS]
                clause["status"] = "waiting" if candidates else "unknown"
                clause["summary"] = "Singleton checks same-user, same-name earlier submissions. A partial/local snapshot cannot establish that all such jobs have finished, including federation peers."
                if uncertain or len(same) > MAX_TARGETS:
                    result["truncated"] |= len(same) > MAX_TARGETS
            else:
                remaining = max(0, MAX_TARGET_EVALUATIONS - evaluations)
                if len(clause["targets"]) > remaining:
                    result["truncated"] = True
                    root["truncated"] = True
                    clause["truncated"] = True
                evaluated = clause["targets"][:remaining]
                evaluations += len(evaluated)
                clause["targets"] = [_target(clause["kind"], target, index, owner, now) for target in evaluated]
                clause["status"] = _combine([t["status"] for t in clause["targets"]], "and")
                if clause["truncated"]:
                    clause["status"] = "unknown"
                    clause["summary"] = "Dependency target evaluation reached its complexity bound."
            if clause["annotation"] == "fulfilled":
                if clause["status"] == "impossible":
                    clause["status"] = "unknown"
                    clause["summary"] = "Scheduler fulfilled annotation conflicts with the supplied predecessor state."
                else:
                    clause["status"] = "satisfied"
                    clause["summary"] = "The dependency clause is explicitly annotated fulfilled by Slurm."
            elif clause["annotation"] == "unfulfilled" and clause["status"] == "satisfied":
                clause["status"] = "unknown"
                clause["summary"] = "Scheduler unfulfilled annotation conflicts with the apparent predecessor outcome; snapshots may have different collection times."
        if result["operator"] != "invalid" and not result["truncated"] and not any(c["invalid"] for c in result["clauses"]):
            result["status"] = _combine([c["status"] for c in result["clauses"]], result["operator"])
        return result

    evaluate(job)
    stack = [(job, 0, frozenset({_job_id(job)}))]
    while stack and len(root["tree"]) < MAX_TREE:
        owner, depth, ancestors = stack.pop()
        result = evaluate(owner)
        if result["truncated"]:
            root["truncated"] = True
        children = []
        for clause in result["clauses"]:
            if clause["invalid"]:
                continue
            for target in clause["targets"]:
                jid = target["job_id"]
                edge = (_job_id(owner), clause["kind"], jid)
                if edge in seen_edges:
                    continue
                seen_edges.add(edge)
                cycle = jid in ancestors
                root["tree"].append({"job_id": jid, "parent_id": _job_id(owner), "depth": depth + 1, "kind": clause["kind"], "state": target["state"], "status": target["status"], "external": target.get("external", False), "cycle": cycle})
                if cycle:
                    if root["status"] != "satisfied":
                        root["status"] = "unknown"
                elif jid in index and depth + 1 < MAX_DEPTH:
                    children.append((index[jid], depth + 1, ancestors | {jid}))
                elif jid in index and _text(_get(index[jid], "dependency", "")):
                    root["truncated"] = True
                if len(root["tree"]) >= MAX_TREE:
                    root["truncated"] = True
                    break
            if len(root["tree"]) >= MAX_TREE:
                break
        stack.extend(reversed(children))
    if stack:
        root["truncated"] = True
    if root["truncated"]:
        root["status"] = "unknown"
    return root


def _memory(value):
    match = re.fullmatch(r"(\d{1,12}(?:\.\d{1,6})?)([KMGT]?)([cn]?)", _text(value, 64), re.IGNORECASE)
    if not match:
        return None, ""
    amount = float(match.group(1)) * {"": 1024**2, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[match.group(2).upper()]
    return (amount, match.group(3).lower()) if math.isfinite(amount) and amount <= 10**18 else (None, "")


def _placement(job, details, partitions, nodes):
    result = {"status": "not_checked", "eligible_nodes": 0, "observed_nodes": 0, "required_nodes": None, "complete": False, "nodes": [], "limitations": []}
    rows, truncated = _records(nodes, MAX_NODES)
    if not rows:
        result["limitations"].append("No per-node placement metadata was supplied.")
        return result
    wanted = _text(_get(job, "partition", details.get("Partition", "")), 256).rstrip("*")
    if not wanted or "," in wanted:
        result.update(status="unknown")
        result["limitations"].append("A single requested partition is required for node screening.")
        return result
    n = _integer(_get(job, "nodes", details.get("NumNodes")))
    cpus = _integer(_get(job, "cpus", details.get("NumCPUs")))
    result["required_nodes"] = n
    if n is None or n < 1 or cpus is None or cpus < 1:
        result["status"] = "unknown"
        result["limitations"].append("A valid requested node count and CPU count are required for node screening.")
        return result
    min_cpu = _integer(details.get("MinCPUsNode"))
    cpu_per_node = max(1, min_cpu or (cpus if n == 1 else 1))
    distribution_unknown = n > 1 and not min_cpu
    memory, scope = _memory(_get(job, "mem_req", ""))
    if memory is None:
        memory, scope = _memory(details.get("MinMemoryNode", ""))
    if memory is None and details.get("MinMemoryCPU"):
        memory, _ = _memory(details["MinMemoryCPU"])
        scope = "c"
    if memory is not None and scope == "c":
        memory *= cpu_per_node
    gpu_total = _integer(_get(job, "gpus", 0))
    gpu_per_node = gpu_total if n == 1 else 0 if gpu_total == 0 else None
    gpu_type = _text(_get(job, "gpu_type", ""), 64)
    explicit_gpu = re.search(r"(?:^|,)(?:gres/)?gpu(?::([A-Za-z0-9_.-]{1,64}))?:(\d{1,9})(?:,|$)", _text(details.get("TresPerNode", details.get("TRESPerNode", "")), 512))
    if explicit_gpu:
        gpu_type = explicit_gpu.group(1) or gpu_type
        gpu_per_node = int(explicit_gpu.group(2))
    constraints = _text(details.get("Features", details.get("Constraint", "")), 256)
    other_constraints = bool(constraints and constraints not in {"(null)", "None", "N/A"}) or any(details.get(k) not in (None, "", "(null)", "None") for k in ("ReqNodeList", "ExcNodeList", "Reservation"))
    result["request_per_node_lower_bound"] = {"cpus": cpu_per_node, "memory_bytes": memory, "gpus": gpu_per_node, "gpu_type": gpu_type}
    eligible = 0
    unknown = False
    seen_names = set()
    for node in rows:
        memberships = _get(node, "partitions", "")
        if isinstance(memberships, str):
            memberships = memberships.split(",")
        if not isinstance(memberships, (list, tuple)) or wanted not in [_text(p, 128).rstrip("*") for p in memberships[:64]]:
            continue
        name = _text(_get(node, "name", ""), 128)
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        result["observed_nodes"] += 1
        state = _text(_get(node, "state", ""), 64).lower()
        unavailable = any(bit in state for bit in ("down", "drain", "drng", "fail", "maint", "inval", "power_down", "powered_down")) or state.endswith("*")
        idle = _integer(_get(node, "cpus_idle"))
        configured_cpus = _integer(_get(node, "cpus"))
        if idle is not None and configured_cpus is not None and (configured_cpus == 0 or idle > configured_cpus):
            idle = None
        if idle is None:
            total = _integer(_get(node, "cpus"))
            allocated = _integer(_get(node, "alloc"))
            idle = total - allocated if total is not None and total > 0 and allocated is not None and allocated <= total else None
        total_mem = _num(_get(node, "mem"))
        allocated_mem = _num(_get(node, "mem_alloc"))
        sched_mem = (total_mem - allocated_mem) * 1024**2 if total_mem is not None and total_mem > 0 and allocated_mem is not None and allocated_mem <= total_mem else None
        free_gpu = None
        node_gpu_type = _text(_get(node, "gpu_type", ""), 64)
        gpu_count = _integer(_get(node, "gpus"))
        gpu_used = _integer(_get(node, "gpus_used"))
        if gpu_count is not None and gpu_used is not None and gpu_used <= gpu_count:
            free_gpu = gpu_count - gpu_used
        checks = []
        if unavailable:
            checks.append("unavailable_state")
        elif not any(bit in state for bit in ("idle", "mix", "alloc")):
            checks.append("unknown_node_state")
        if idle is None:
            checks.append("unknown_cpu_availability")
        elif idle < cpu_per_node:
            checks.append("cpu_capacity")
        if memory is None:
            checks.append("unknown_memory_request")
        elif memory == 0:
            checks.append("unknown_all_memory_semantics")
        elif sched_mem is None:
            checks.append("unknown_memory_reservations")
        elif sched_mem < memory:
            checks.append("memory_capacity")
        if gpu_per_node is None:
            checks.append("unknown_gpu_request")
        elif gpu_per_node:
            if gpu_type and not node_gpu_type:
                checks.append("unknown_gpu_type")
            elif gpu_type and gpu_type != node_gpu_type:
                checks.append("gpu_type")
            if free_gpu is None:
                checks.append("unknown_gpu_availability")
            elif free_gpu < gpu_per_node:
                checks.append("gpu_capacity")
        rejects = any(c in checks for c in ("unavailable_state", "cpu_capacity", "memory_capacity", "gpu_type", "gpu_capacity"))
        uncertain = any(c.startswith("unknown_") for c in checks)
        if not rejects:
            eligible += 1
            unknown |= uncertain
        if len(result["nodes"]) < 64:
            result["nodes"].append({"name": name, "status": "excluded" if rejects else "unknown" if uncertain else "possible", "checks": checks, "cpu_unallocated": idle, "memory_unallocated_bytes": sched_mem, "gpu_unallocated": free_gpu})
    p = next((p for p in partitions if _text(_get(p, "name", ""), 128).rstrip("*") == wanted), None)
    partition_nodes = _integer(_get(p, "nodes")) if p else None
    complete = bool(not truncated and partition_nodes is not None and partition_nodes > 0 and partition_nodes == result["observed_nodes"])
    result.update(eligible_nodes=eligible, complete=complete)
    if not result["observed_nodes"]:
        result["status"] = "unknown"
    elif eligible < n:
        result["status"] = "insufficient" if complete else "unknown"
    elif unknown or other_constraints or distribution_unknown:
        result["status"] = "unknown"
    else:
        result["status"] = "possible"
    if not complete:
        result["limitations"].append("Node coverage is partial or cannot be established from partition metadata; missing nodes may fit the request.")
    if other_constraints:
        result["limitations"].append("Feature, required/excluded-node, or reservation constraints are not evaluated by this capacity screen.")
    if distribution_unknown or gpu_per_node is None:
        result["limitations"].append("Multi-node totals do not establish an even per-node CPU/GPU distribution; per-node allocation metadata is incomplete.")
    result["limitations"].append("Per-node CPU/memory/GPU lower bounds are screened together. Memory is scheduler-unallocated configured capacity, not measured free RAM. A possible fit does not establish simultaneous placement, topology, exclusivity, priority, reservations, or a start time.")
    if truncated:
        result["limitations"].append("Node metadata was bounded to the first 4096 records.")
    return result


def explain(job, *, jobs=(), finished=(), details=None, partitions=(), nodes=None, share=(), health=None, now=None):
    """Explain one job using only the supplied, possibly partial, snapshot."""
    details = details if isinstance(details, Mapping) else {}
    now_value = _num(clock.now() if now is None else now, 10**12)
    now_value = now_value if now_value is not None else 0.0
    current, queue_truncated = _records(jobs, MAX_RECORDS)
    history, history_truncated = _records(finished, MAX_RECORDS)
    partition_rows, partitions_truncated = _records(partitions, 256)
    share_rows, share_truncated = _records(share, 128)
    result = {"job_id": _job_id(job), "state": _state(job), "reason": _reason(_get(job, "reason", details.get("Reason", ""))), "status": "unknown", "summary": "", "blockers": [], "evidence": [], "dependencies": {}, "placement": {}, "fairshare": [], "sources": [], "limitations": [], "truncated": queue_truncated or history_truncated or partitions_truncated or share_truncated}

    def evidence(source, field, value, text):
        if len(result["evidence"]) >= MAX_EVIDENCE:
            result["truncated"] = True
            return None
        eid = f"E{len(result['evidence']) + 1}"
        result["evidence"].append({"id": eid, "source": source, "field": field, "value": _text(value), "text": _text(text)})
        return eid

    def blocker(code, title, summary, status, support, checks):
        result["blockers"].append({"code": code, "title": title, "summary": summary, "status": status, "support": [s for s in support if s], "checks": checks, "editable": False})

    index = {}
    for record in history:
        jid = _job_id(record)
        if _ID.fullmatch(jid):
            index[jid] = record
    for record in current:
        jid = _job_id(record)
        if _ID.fullmatch(jid):
            index[jid] = record
    if _ID.fullmatch(result["job_id"]):
        index[result["job_id"]] = job
    peers = {}
    for record in index.values():
        user = _text(_get(record, "user", _get(record, "UserId", "")), 128).split("(", 1)[0]
        name = _text(_get(record, "name", _get(record, "JobName", "")))
        if user and name:
            peers.setdefault((user, name), []).append(record)
    dependency_job = job
    if not _get(job, "dependency", None) and details.get("Dependency"):
        dependency_job = {"id": result["job_id"], "dependency": details["Dependency"], "name": _get(job, "name", ""), "user": _get(job, "user", ""), "submit": _get(job, "submit", "")}
    result["dependencies"] = _dependencies(dependency_job, index, peers, now_value)
    if result["dependencies"]["truncated"]:
        result["truncated"] = True
        result["limitations"].append("Dependency parsing or traversal reached its complexity bound; omitted dependencies remain unknown.")
    if any(row["cycle"] for row in result["dependencies"]["tree"]):
        result["limitations"].append("A dependency cycle was observed; the tree is stopped at the repeated job.")
    if result["dependencies"]["operator"] == "invalid" or any(c.get("invalid") for c in result["dependencies"]["clauses"]):
        result["limitations"].append("Malformed, mixed AND/OR, or unsupported dependency syntax cannot be evaluated.")
    external = sum(1 for row in result["dependencies"]["tree"] if row["external"])
    if external:
        result["limitations"].append(f"{external} dependency references lack predecessor records; the snapshot may exclude other users or older history.")
    _jid_evidence = evidence("job", "state", result["state"], "Observed job state in the supplied snapshot.")
    reason_evidence = evidence("job", "reason", result["reason"]["raw"], "Slurm reason is a reported scheduler condition, not a complete causal diagnosis.")
    code = result["reason"]["code"]
    pending = result["state"] in {"PENDING", "PD"}
    if pending and code in _REASONS:
        title, summary, checks = _REASONS[code]
        blocker(code, title, summary, "confirmed", [reason_evidence], list(checks))
    elif pending and code not in {"", "None", "NONE", "null", "N", "N_A"}:
        blocker(code, "Unrecognized scheduler reason", f"The exact reported reason is {code}. Raven has no interpretation for this code.", "unknown", [reason_evidence], ["Inspect the site's Slurm version and documented reason-code meaning."])
    if pending and result["dependencies"]["operator"] != "none":
        dep = result["dependencies"]
        dep_evidence = evidence("job", "dependency", _get(dependency_job, "dependency", ""), f"Dependency expression uses {dep['operator'].upper()} semantics and is {dep['status']} from available records.")
        if dep["status"] in {"waiting", "impossible"}:
            blocker("dependency_snapshot", "Observed dependency condition", f"The supplied predecessors make the dependency {dep['status']}; Slurm's current reason may report another simultaneous blocker.", "confirmed", [dep_evidence], ["Review each clause's predecessor state and its success/failure requirement."])
        elif dep["status"] == "unknown":
            blocker("dependency_snapshot", "Dependency evidence incomplete", "The dependency cannot be fully evaluated from the supplied snapshot.", "unknown", [dep_evidence], ["Collect the missing predecessor or corresponding-array-task accounting record."])
        if code in {"Dependency", "DependencyNeverSatisfied"} and dep["status"] == "satisfied":
            result["limitations"].append("The reported dependency reason conflicts with the satisfied snapshot expression; collection times or missing scheduler metadata may differ.")
    result["placement"] = _placement(job, details, partition_rows, nodes) if pending else {"status": "not_checked", "limitations": ["Placement screening applies to pending jobs only."], "nodes": []}
    result["limitations"].extend(result["placement"].get("limitations", ()))
    if pending and result["placement"]["status"] == "insufficient":
        placement = result["placement"]
        eid = evidence("nodes", "eligible_nodes", placement["eligible_nodes"], f"Complete partition snapshot has {placement['eligible_nodes']} individually eligible nodes for {placement['required_nodes']} required nodes under the screened lower bounds.")
        blocker("placement_snapshot", "Per-node capacity insufficient", "The observed complete node snapshot cannot satisfy the screened per-node resource lower bounds; free resources on different nodes are not combined.", "possible", [eid], ["Refresh node allocation metadata and inspect the excluded-node checks."])
    requested_part = _text(_get(job, "partition", details.get("Partition", "")), 256).rstrip("*")
    p = next((p for p in partition_rows if _text(_get(p, "name", ""), 128).rstrip("*") == requested_part), None)
    if pending and p is not None:
        requested_limit = _seconds(_get(job, "limit", details.get("TimeLimit")))
        partition_limit = _seconds(_get(p, "limit", _get(p, "MaxTime")))
        if requested_limit is not None and partition_limit is not None and requested_limit > partition_limit:
            eid = evidence("partitions", "limit", _get(p, "limit", _get(p, "MaxTime")), "Observed requested wall time exceeds the partition's configured maximum.")
            blocker("partition_time_snapshot", "Requested time exceeds partition maximum", "The requested time exceeds the maximum reported in this partition snapshot.", "possible", [eid], ["Confirm the job's effective limit and any QoS override in current partition policy."])
    account = _text(_get(job, "account", details.get("Account", "")), 128)
    for row in share_rows:
        if _text(_get(row, "account", ""), 128) == account and account:
            usage = _text(_get(row, "usage", ""), 128)
            value = _text(_get(row, "fairshare", ""), 128)
            eid = evidence("share", "fairshare", value, "Reported fairshare is descriptive; priority weights, QoS, reservations, and other users' eligible work are not reconstructed.")
            result["fairshare"].append({"account": account, "usage": usage, "fairshare": value, "support": [eid], "interpretation": "This value alone cannot establish queue position or a start time."})
            if len(result["fairshare"]) >= 8:
                break
    relevant_sources = ("jobs", "details", "finished", "partitions", "nodes", "share")
    stale_job = False
    if isinstance(health, Mapping):
        for source in relevant_sources:
            h = health.get(source)
            if h is None:
                continue
            last_ok = _num(_get(h, "last_ok"), 10**12)
            age = now_value - last_ok if last_ok is not None and 0 < last_ok <= now_value else None
            err = _text(_get(h, "error", ""), 256)
            enabled = _get(h, "enabled", True)
            status = "disabled" if enabled is False else "unknown" if age is None else "stale" if age > STALE_SECONDS else "fresh"
            if err and status == "fresh":
                status = "error"
            result["sources"].append({"source": source, "last_ok": last_ok, "age_seconds": age, "status": status, "error": err})
            if status != "fresh":
                result["limitations"].append(f"{source} source is {status}; its supplied metadata may be incomplete or stale.")
            if source == "jobs" and status != "fresh":
                stale_job = True
    if not result["sources"]:
        result["limitations"].append("Snapshot source timestamps were not supplied; freshness cannot be verified.")
    if result["truncated"]:
        result["limitations"].append("Input or output bounds were reached; omitted metadata cannot establish a complete diagnosis.")
    if not pending:
        if result["state"] in _NONPENDING:
            result["status"] = "clear"
            result["summary"] = f"Job {result['job_id'] or '?'} is {result['state']}; a pending scheduler blocker is not applicable. This does not validate execution health or outputs."
        else:
            result["summary"] = "Job state is missing or unrecognized; a scheduler blocker cannot be established."
    elif stale_job or result["truncated"]:
        result["status"] = "partial"
        result["summary"] = "Scheduler conditions are shown from a stale or bounded snapshot; refresh before drawing a current conclusion."
    elif any(b["status"] == "confirmed" for b in result["blockers"]):
        result["status"] = "blocked"
        result["summary"] = result["blockers"][0]["summary"]
    elif result["blockers"]:
        result["status"] = "partial" if any(b["status"] == "possible" for b in result["blockers"]) else "unknown"
        result["summary"] = result["blockers"][0]["summary"]
    else:
        result["summary"] = "The job is pending, but no blocking condition is established by this snapshot. An empty reason does not guarantee immediate eligibility."
    return result
