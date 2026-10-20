"""Fresh owned-allocation qualification through the existing collection owner.

This does not promise idle CPUs. Slurm's exact, non-overlapping worker step
must reserve those resources; an occupied or unsupported allocation fails
within the step's immediate deadline. The worker then verifies its real step.
"""

from __future__ import annotations

import math
import os
import re
import time

from .allocation_adapters import VerifiedReuse


def duration(value):
    if value == "UNLIMITED":
        return 604800
    match = re.fullmatch(r"(?:(\d+)-)?(\d+):(\d{2}):(\d{2})", value or "")
    if match is None:
        raise ValueError("Allocation has no bounded, readable remaining time")
    days, hours, minutes, seconds = (int(item or 0) for item in match.groups())
    if minutes >= 60 or seconds >= 60:
        raise ValueError("Invalid scheduler duration")
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def memory_mib(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]?)", value or "", re.I)
    if match is None:
        raise ValueError("Allocation memory grant is unavailable")
    number = (
        float(match[1])
        * {"": 1, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[match[2].upper()]
    )
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Allocation memory grant is unavailable")
    return math.floor(number)


def review(backend, job_id, planned, cancel=None):
    """Fresh admitted queries; invoke only on the manager's finite lane."""
    from ..slurm import parse_kv
    from raven.collection.ledger import expand_hostlist

    if getattr(backend, "ledger", None) is None:
        raise ValueError("Reuse requires the governed collection owner")
    if not re.fullmatch(r"[0-9]{1,20}", job_id):
        raise ValueError("Reuse requires an unambiguous allocation JobID")
    cluster = planned["cluster_id"]
    if not cluster:
        # Job records commonly omit their cluster. An empty profile means the
        # actual selected controller, not an empty identity in the receipt.
        from .allocation_recovery import _configuration, _read

        cluster = _configuration(
            _read(backend, ["scontrol", "show", "config"], cancel)
        )["ClusterName"]
    expected_uid = os.getuid()
    if planned.get("transport") == "ssh":
        identity = backend.command(["id", "-u"], 8.0, 64, cancel).strip()
        if not re.fullmatch(r"[0-9]{1,10}", identity):
            raise ValueError(
                "Remote user identity could not be verified through the approved connection"
            )
        expected_uid = int(identity)
    record = backend.command(
        ["scontrol", "show", "job", job_id, "-o"], 8.0, 65536, cancel
    )
    observed = time.time()
    if cancel is not None and cancel.is_set():
        raise ValueError("Allocation reuse review was cancelled")
    if (
        not isinstance(record, str)
        or len(record.encode()) > 65536
        or len(record.strip().splitlines()) != 1
    ):
        raise ValueError("Allocation review did not return one bounded job record")
    fields = parse_kv(record)
    if fields.get("JobId") != job_id or fields.get("JobState") != "RUNNING":
        raise ValueError("Reuse requires the exact currently running allocation")
    uid = re.fullmatch(r"[^()\s]+\(([0-9]+)\)", fields.get("UserId", ""))
    if uid is None or int(uid[1]) != expected_uid:
        raise ValueError(
            "Reuse allocation does not belong to the current verified user identity"
        )
    observed_cluster = fields.get("Cluster", fields.get("ClusterName", cluster))
    if observed_cluster != cluster:
        raise ValueError("Reuse allocation belongs to another cluster")
    for name in ("SubmitTime", "StartTime"):
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:?\d{2}|Z)?",
            fields.get(name, ""),
        ):
            raise ValueError(
                "Reuse requires exact scheduler submit and start timestamps"
            )
    nodes = expand_hostlist(fields.get("NodeList", ""))
    if not nodes or len(nodes) > 1024:
        raise ValueError("Reuse allocation has no bounded usable node list")
    required = planned["requested_resources"]
    for name, field in (
        ("account", "Account"),
        ("partition", "Partition"),
        ("qos", "QOS"),
    ):
        if required.get(name) and fields.get(field) != required[name]:
            raise ValueError(
                "Reuse allocation " + name + " differs from the reviewed site request"
            )
    # Only claim capacity verified for the pinned node. Total allocation
    # memory/CPU counts cannot establish capacity on each node of a large job.
    cpus = int(fields.get("MinCPUsNode", "0") or 0)
    if len(nodes) == 1:
        cpus = max(cpus, int(fields.get("NumCPUs", "0") or 0))
    tres = dict(
        part.split("=", 1)
        for part in fields.get("AllocTRES", "").split(",")
        if "=" in part
    )
    memory = None
    if len(nodes) == 1 and "mem" in tres:
        memory = memory_mib(tres["mem"])
    elif fields.get("MinMemoryNode"):
        memory = memory_mib(fields["MinMemoryNode"])
    elif fields.get("MinMemoryCPU"):
        memory = memory_mib(fields["MinMemoryCPU"]) * min(
            cpus, required["cpus_per_task"]
        )
    if (
        cpus < required["cpus_per_task"]
        or memory is None
        or memory < required["memory_mib"]
    ):
        raise ValueError(
            "Reuse needs a verified per-node CPU and memory grant sufficient for the worker"
        )
    # GPU reuse is deliberately unavailable without per-node type/count proof.
    if required.get("gpus", 0):
        raise ValueError(
            "GPU allocation reuse requires a site-qualified GPU reservation adapter"
        )
    remaining = duration(fields.get("TimeLimit")) - duration(fields.get("RunTime"))
    if remaining < required["walltime_seconds"] + 5:
        raise ValueError(
            "Reuse allocation has insufficient time for the worker and review deadline"
        )
    return VerifiedReuse(
        job_id,
        cluster,
        int(uid[1]),
        fields["SubmitTime"],
        fields["StartTime"],
        observed,
        remaining,
        cpus,
        memory,
        (nodes[0],),
        True,
        True,
    )
