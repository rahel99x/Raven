"""Heavy process budgets and verified placement; no scheduler command owner."""

from __future__ import annotations

import math
import os
import re
import resource
import socket

_DEFAULTS = {
    "cpus": 1,
    "memory_mib": 512,
    "scratch_mib": 64,
    "max_tasks": 1,
    "cpu_seconds": 60,
    "wall_seconds": 120,
    "input_bytes": 512 << 10,
    "output_bytes": 512 << 10,
    "max_records": 100000,
}
_CEILINGS = {
    "cpus": 64,
    "memory_mib": 1048576,
    "scratch_mib": 1048576,
    "max_tasks": 8,
    "cpu_seconds": 86400,
    "wall_seconds": 86400,
    "input_bytes": 8 << 20,
    "output_bytes": 512 << 10,
    "max_records": 100000,
}
_JOB = re.compile(r"^[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?$")
_STEP = re.compile(r"^(?:[0-9]+|batch|extern)$")
_MIB = 1 << 20
_SUPERVISOR_MIN_MIB = 64
_SUPERVISOR_HEADROOM_MIB = 32


def limits(raw=None):
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("Heavy resource limits must be an object")
    result = {}
    for key, default in _DEFAULTS.items():
        value = raw.get(key, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 < value <= _CEILINGS[key]
            or not math.isfinite(value)
        ):
            raise ValueError("Invalid Heavy resource limit: " + key)
        result[key] = (
            int(value) if key not in ("cpu_seconds", "wall_seconds") else float(value)
        )
        if result[key] <= 0:
            raise ValueError("Heavy resource limit must be at least one: " + key)
    if result["memory_mib"] < 32:
        raise ValueError("Heavy process memory must include at least 32 MiB")
    result["max_tasks"] = min(result["max_tasks"], result["cpus"])
    result["enforcement"] = raw.get("enforcement", "best-available")
    if result["enforcement"] not in ("best-available", "portable", "hard"):
        raise ValueError("Unknown Heavy resource enforcement policy")
    return result


def task_limits(requested, available, *, supervisor_memory_mib=None):
    if supervisor_memory_mib is None:
        supervisor_memory_mib = available.get(
            "supervisor_memory_mib", _SUPERVISOR_MIN_MIB
        )
    if (
        type(supervisor_memory_mib) is not int
        or supervisor_memory_mib < _SUPERVISOR_MIN_MIB
    ):
        raise ValueError("Invalid Heavy supervisor memory reservation")
    bounded = dict(available)
    # This is worker-observed metadata, never a task-requestable resource.
    bounded.pop("supervisor_memory_mib", None)
    bounded["memory_mib"] = (
        available["memory_mib"] - supervisor_memory_mib
    ) // available["max_tasks"]
    if bounded["memory_mib"] < 32:
        raise ValueError(
            "Heavy memory budget cannot cover this Python runtime and the "
            "requested worker concurrency; increase Heavy memory or reduce max_tasks"
        )
    bounded["cpus"] = max(1, available["cpus"] // available["max_tasks"])
    if requested:
        for key, value in requested.items():
            if key not in bounded or key in ("max_tasks", "enforcement"):
                raise ValueError("Unsupported task resource field: " + str(key))
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < value <= bounded[key]
            ):
                raise ValueError("Task exceeds admitted " + key + " budget")
            bounded[key] = value
    return limits(bounded)


def reserve_supervisor(available):
    """Bound existing mappings plus working headroom before admitting children.

    AS includes interpreter/library mappings, not just resident Python objects.
    Charge the full supervisor cap to the same budget used by task admission;
    neither scheduler memory nor inherited process limits are increased here.
    """
    try:
        with open("/proc/self/statm", "rb") as source:
            raw = source.read(256)
        fields = raw.split()
        if len(raw) == 256 or not fields or not fields[0].isdigit():
            raise ValueError("Invalid process virtual-memory measurement")
        page_size = os.sysconf("SC_PAGE_SIZE")
        virtual_bytes = int(fields[0]) * page_size
        if page_size <= 0 or virtual_bytes <= 0:
            raise ValueError("Invalid process virtual-memory measurement")
    except (OSError, ValueError) as exc:
        raise ValueError(
            "Cannot measure Heavy supervisor virtual memory from /proc/self/statm; "
            "safe memory admission is unavailable on this host"
        ) from exc
    reservation = max(
        _SUPERVISOR_MIN_MIB,
        (virtual_bytes + _MIB - 1) // _MIB + _SUPERVISOR_HEADROOM_MIB,
    )
    task_limits({}, available, supervisor_memory_mib=reservation)
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    wanted = reservation * _MIB
    for inherited in (soft, hard):
        if inherited != resource.RLIM_INFINITY and wanted > inherited:
            raise ValueError(
                f"Heavy supervisor needs {reservation} MiB of virtual address space "
                "including this Python runtime, exceeding an inherited RLIMIT_AS; "
                "review the process memory limit with your site administrator"
            )
    resource.setrlimit(resource.RLIMIT_AS, (wanted, hard))
    return reservation


def child_environment(cpus, source=None):
    env = dict(os.environ if source is None else source)
    for name in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "BLIS_NUM_THREADS",
    ):
        env[name] = str(max(1, int(cpus)))
    env["PYTHONNOUSERSITE"] = "1"
    return env


def apply_limits(budget):
    """Portable hard per-process AS/CPU/file limits, not an RSS/CPU-rate claim."""
    budget = limits(budget)
    values = (
        (resource.RLIMIT_AS, int(budget["memory_mib"]) << 20),
        (resource.RLIMIT_CPU, max(1, math.ceil(budget["cpu_seconds"]))),
        (resource.RLIMIT_FSIZE, int(budget["scratch_mib"]) << 20),
        (resource.RLIMIT_NOFILE, 128),
    )
    applied = {}
    for kind, wanted in values:
        _soft, hard = resource.getrlimit(kind)
        target = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
        resource.setrlimit(kind, (target, target))
        applied[str(kind)] = target
    return {
        "mechanism": "rlimit-process-group",
        "hard_address_space": True,
        "hard_rss": False,
        "cpu_percentage": False,
        "cpu_time": True,
        "wall_time": "supervisor termination",
        "disk_scope": "per-file size; adapters forbid arbitrary writes",
        "limits": applied,
    }


def _proc_cgroups():
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as source:
            return source.read(16384)
    except OSError:
        return ""


def placement_probe(
    config,
    *,
    environ=None,
    host=None,
    uid=None,
    pid=None,
    cgroups=None,
    purpose="analysis",
):
    if purpose not in ("analysis", "instrumentation"):
        raise ValueError("Unknown placement verification purpose")
    env = os.environ if environ is None else environ
    job, step = env.get("SLURM_JOB_ID", ""), env.get("SLURM_STEP_ID", "")
    raw = _proc_cgroups() if cgroups is None else cgroups
    if purpose == "instrumentation" and not step:
        # Batch scripts need not export SLURM_STEP_ID. The local kernel path
        # can supply a candidate, which still needs fresh scheduler validation.
        for possible in ("batch", "extern"):
            if re.search(r"(?:/|^)step_" + possible + r"(?:/|$)", raw, re.M):
                step = possible
                break
    if not _JOB.fullmatch(job) or not _STEP.fullmatch(step):
        raise ValueError(
            "Heavy requires a verified Slurm compute step; environment hints are insufficient"
        )
    context = config.get("context") or {}
    expected = context.get("allocation_id", context.get("job_id"))
    if expected is not None and str(expected) != job:
        raise ValueError(
            "Worker allocation differs from the requested exact allocation"
        )
    if context.get("step_id") is not None and str(context["step_id"]) != step:
        raise ValueError("Worker step differs from the requested step")
    # Require complete path components, not job_12 accidentally matching job_123.
    proof = bool(
        re.search(r"(?:/|^)job_" + re.escape(job) + r"(?:/|$)", raw, re.M)
        and re.search(r"(?:/|^)step_" + re.escape(step) + r"(?:/|$)", raw, re.M)
    )
    hostname = socket.gethostname() if host is None else host
    fqdn = hostname
    resolution = {}
    if host is None:
        try:
            fqdn = socket.getfqdn(hostname)
        except (OSError, LookupError) as exc:
            # DNS/codec enrichment is optional. Kernel hostname, fresh scheduler
            # membership and local cgroup/PID proof still decide placement.
            resolution["hostname_resolution_error"] = (
                type(exc).__name__ + ": " + str(exc)
            )[:240]
    return {
        **resolution,
        "job_id": job,
        "step_id": step,
        "host": hostname,
        "fqdn": fqdn,
        "uid": os.getuid() if uid is None else uid,
        "pid": os.getpid() if pid is None else pid,
        "cgroup_proof": proof,
        "purpose": purpose,
    }


def _record(text):
    if not isinstance(text, str) or len(text) > 256 << 10:
        raise ValueError("Invalid scheduler placement record")
    return dict(re.findall(r"(?:^|\s)([A-Za-z][A-Za-z0-9_]*)=([^\s]*)", text))


def _memory_mib(value):
    """Parse Slurm memory units conservatively; unitless memory is MiB."""
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGT]?)", str(value), re.I)
    if not match:
        return None
    amount = (
        float(match[1])
        * {"K": 1 / 1024, "": 1, "M": 1, "G": 1024, "T": 1024**2}[match[2].upper()]
    )
    return amount if math.isfinite(amount) and amount > 0 else None


def _memory_grant(job, step, nodes):
    # Never divide a multi-node aggregate by node count: nodes may have unequal
    # grants. A per-node minimum is conservative and a per-step grant is useful
    # only for this worker's single-node step.
    grants = []
    per_node = _memory_mib(job.get("MinMemoryNode"))
    if per_node is not None:
        grants.append(per_node)
    for record, single_node, fields in (
        (step, len(nodes["step_nodes"]) == 1, ("TRES", "AllocTRES")),
        (job, len(nodes["job_nodes"]) == 1, ("AllocTRES",)),
    ):
        if single_node:
            for field in fields:
                values = dict(
                    item.split("=", 1)
                    for item in record.get(field, "").split(",")
                    if "=" in item
                )
                memory = _memory_mib(values.get("mem"))
                if memory is not None:
                    grants.append(memory)
    per_cpu = _memory_mib(job.get("MinMemoryCPU"))
    if per_cpu is not None and len(nodes["step_nodes"]) == 1:
        cpus = step.get("CPUs", step.get("NumCPUs", ""))
        if cpus.isdigit():
            grants.append(per_cpu * int(cpus))
    return min(grants) if grants else None


def verify_placement(config, candidate, records, *, purpose="analysis"):
    """Check owner-collected fresh metadata against this process's local proof."""
    if (
        purpose not in ("analysis", "instrumentation")
        or candidate.get("purpose", "analysis") != purpose
    ):
        raise ValueError("Placement verification purpose differs from its local probe")
    if not isinstance(records, dict) or records.get("probe_id") != candidate.get(
        "probe_id"
    ):
        raise ValueError("Placement validation belongs to a different launch")
    job, step = _record(records.get("job_record")), _record(records.get("step_record"))
    job_id, step_id = candidate["job_id"], candidate["step_id"]
    if job.get("JobId") != job_id or job.get("JobState") != "RUNNING":
        raise ValueError("Slurm allocation is not the expected running allocation")
    owner = re.search(r"\(([0-9]+)\)$", job.get("UserId", ""))
    owner_uid = (
        int(owner.group(1))
        if owner
        else int(job["UserId"])
        if job.get("UserId", "").isdigit()
        else None
    )
    if owner_uid != candidate["uid"]:
        raise ValueError("Slurm allocation is not owned by the worker user")
    expected = config.get("context") or {}
    if (
        expected.get("owner_uid") is not None
        and expected["owner_uid"] != candidate["uid"]
    ):
        raise ValueError("Worker user differs from the requested owner")
    for name, field in (("submit_time", "SubmitTime"), ("start_time", "StartTime")):
        if expected.get(name) is not None and str(expected[name]) != job.get(field):
            raise ValueError("Allocation attempt changed after review")
    if step.get("StepId") != job_id + "." + step_id:
        raise ValueError("Slurm step identity does not match this worker")
    if step.get("State", step.get("StepState", "RUNNING")) != "RUNNING":
        raise ValueError("Slurm worker step is not running")
    actual = {candidate["host"].lower(), candidate["fqdn"].lower()}
    actual.update(name.split(".")[0] for name in tuple(actual))
    for name in ("job_nodes", "step_nodes"):
        nodes = records.get(name)
        if (
            not isinstance(nodes, list)
            or not nodes
            or len(nodes) > 10000
            or not all(isinstance(node, str) and len(node) <= 255 for node in nodes)
        ):
            raise ValueError("Missing bounded scheduler node membership")
        if not any(
            node.lower() in actual or node.lower().split(".")[0] in actual
            for node in nodes
        ):
            raise ValueError("Worker host is outside the verified allocation or step")
    pids = records.get("pids", [])
    if not candidate.get("cgroup_proof") and (
        not isinstance(pids, list) or candidate["pid"] not in pids
    ):
        raise ValueError(
            "Worker process placement is unverified: need step cgroup or fresh task PID membership"
        )
    identity = {
        **candidate,
        "site_kind": "slurm",
        "cluster_id": config.get("context", {}).get("cluster_id", ""),
        "allocation_id": job_id,
        "verified": True,
        "purpose": purpose,
        "submit_time": job.get("SubmitTime", ""),
        "start_time": job.get("StartTime", ""),
        "verification": "scheduler-owner + local process membership",
        "cgroup_enforcement": "membership observed; CPU/RSS enforcement not inferred",
    }
    if purpose == "instrumentation":
        return {
            **identity,
            "heavy_admission": False,
            "resource_scope": "Existing job-local instrumentation only; no Heavy worker grant",
        }
    if len(records["step_nodes"]) != 1:
        raise ValueError(
            "Portable Heavy requires an explicitly single-node worker step"
        )
    granted_cpus = step.get(
        "CPUs",
        step.get(
            "NumCPUs",
            job.get("NumCPUs", "0") if len(records["job_nodes"]) == 1 else "0",
        ),
    )
    try:
        if int(granted_cpus) < config["resources"]["cpus"]:
            raise ValueError(
                "Slurm step grants fewer CPUs than the requested worker budget"
            )
    except (TypeError, ValueError) as exc:
        raise ValueError("Slurm CPU grant is missing or insufficient") from exc
    memory = _memory_grant(job, step, records)
    if memory is None or memory < config["resources"]["memory_mib"]:
        raise ValueError("Slurm per-node memory grant is missing or insufficient")
    if config["resources"]["enforcement"] == "hard":
        raise ValueError(
            "Hard owned-cgroup enforcement is not qualified by this portable adapter"
        )
    return {
        **identity,
        "verified_memory_mib": memory,
        "verified_cpus": int(granted_cpus),
    }
