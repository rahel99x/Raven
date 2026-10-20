"""Explicit completed-launch cleanup. Recordings and results remain in runs/."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import re
import shutil
import stat
import sys
import time

from . import reporting as R
from .report_lock import lock

TERMINAL = {
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "INTERRUPTED",
    "TIMEOUT",
    "NODE_FAIL",
    "OUT_OF_MEMORY",
}


def _owned(path, *, directory=False):
    info = path.lstat()
    if (
        info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    ):
        raise ValueError("Cleanup requires private owned runtime paths: " + str(path))
    return info


def _entries(path, maximum):
    entries = list(itertools.islice(path.iterdir(), maximum + 1))
    if len(entries) > maximum:
        raise ValueError(
            "Cleanup directory exceeds its bounded inventory; archive old project metadata first"
        )
    return entries


def _eligible(root, folder, cutoff):
    from .launching.contract import plan_inventory

    _owned(folder, directory=True)
    _owned(folder / "plan.json")
    plan = R._read_json(folder / "plan.json")
    if (
        plan.get("schema") != "raven.launch-plan/v1"
        or plan.get("token") != folder.name
        or plan.get("project_root") != str(root)
        or plan.get("runtime_bundle") != str(folder / "runtime.pyz")
        or plan.get("run_id_base") != "launch-" + folder.name
    ):
        raise ValueError("Frozen plan does not identify this wrapper")
    attempts = [
        path
        for path in _entries(root / "runs", 10000)
        if path.name.startswith(plan["run_id_base"] + "-")
    ]
    if not attempts:
        raise ValueError("No bounded set of completed attempts is available")
    job_ids = set()
    for attempt in attempts:
        _owned(attempt, directory=True)
        _owned(attempt / "run.json")
        manifest = R._read_json(
            R._under(root, str((attempt / "run.json").relative_to(root)))
        )
        if (
            manifest.get("schema") != "raven.run/v1"
            or manifest.get("run_id") != attempt.name
            or manifest.get("state") not in TERMINAL
        ):
            raise ValueError("An attempt is active or its identity/state is unverified")
        ended = manifest.get("end")
        if (
            type(ended) not in (int, float)
            or not math.isfinite(ended)
            or ended > cutoff
        ):
            raise ValueError(
                "An attempt completed more recently than the selected retention age"
            )
        metadata = manifest.get("metadata", {})
        automation = (
            metadata.get("automation", {}) if isinstance(metadata, dict) else {}
        )
        retained = automation.get("plan") if isinstance(automation, dict) else None
        if retained != plan_inventory(plan):
            raise ValueError(
                "Retained attempt does not identify this exact frozen launch plan"
            )
        job_ids.add(str(manifest.get("job_id", "")))
    if plan.get("execution") == "slurm":
        receipt_path = R._under(root, ".raven/launches/" + folder.name + ".json")
        _owned(receipt_path)
        receipt = R._read_json(receipt_path)
        if (
            receipt.get("schema") != "raven.launch/v1"
            or receipt.get("token") != folder.name
            or receipt.get("status") != "submitted"
        ):
            raise ValueError(
                "Submission receipt is ambiguous or belongs to another launch"
            )
        if receipt.get("plan") != plan or receipt.get("scheduler_context") != plan.get(
            "scheduler_context"
        ):
            raise ValueError(
                "Submission receipt does not identify this exact frozen launch plan"
            )
        job, cluster = str(receipt.get("job_id", "")), receipt.get("cluster", "")
        if (
            not re.fullmatch(r"[1-9][0-9]{0,18}(?:_[0-9]{1,19})?", job)
            or not isinstance(cluster, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{0,128}", cluster)
        ):
            raise ValueError("No exact scheduler identity is available")
        if not job_ids or any(
            job_id.partition("_")[0] != job.partition("_")[0] for job_id in job_ids
        ):
            raise ValueError("Retained attempts disagree with the scheduler receipt")
    elif plan.get("execution") != "local":
        raise ValueError("Unknown execution mode")
    files = _entries(folder, 64)
    return sum(_owned(path).st_size for path in files), receipt if plan.get(
        "execution"
    ) == "slurm" else None


def _confirmed_absent(candidates, execution):
    """One fresh shared-ledger query, on the exact retained connection only."""
    from .launching.scheduler_context import capture, matches

    backend = getattr(execution, "source_backend", None)
    eligible, jobs = [], set()
    for row, folder, size, receipt in candidates:
        if receipt is None:
            continue
        job = str(receipt.get("job_id", ""))
        declared, observed = (
            receipt.get("cluster", ""),
            receipt.get("observed_cluster", ""),
        )
        cluster = observed or declared
        if (
            observed
            and declared
            and observed != declared
            or not isinstance(cluster, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", cluster)
        ):
            row["reason"] = (
                "Retained scheduler receipt has no unambiguous actual cluster; wrapper preserved."
            )
            continue
        if not matches(
            receipt.get("scheduler_context"), backend, execution.policy, cluster
        ):
            row["reason"] = (
                "No matching retained scheduler connection and exact cluster identity. Select the original local Slurm profile and configuration selectors; legacy unknown launch wrappers remain preserved."
            )
            continue
        if job not in jobs and len(jobs) >= 128:
            row["reason"] = (
                "Scheduler confirmation is capped at 128 exact jobs per cleanup; remaining wrappers are preserved."
            )
            continue
        jobs.add(job)
        eligible.append((row, folder, size, dict(receipt, cluster=cluster)))
    if not eligible:
        return set()
    cluster = eligible[0][3]["cluster"]
    # An empty configured cluster is common on single-node workstations. Resolve
    # it once through the same ledger, then bind every confirmation to that
    # observed name. A differently configured/default cluster proves nothing
    # about retained job IDs on the original scheduler.
    current = capture(backend, execution.policy)
    if current is not None and not current["cluster"]:
        try:
            raw = backend.command(
                ["scontrol", "show", "config"], timeout=5.0, limit=65536, cancel=None
            )
            if not isinstance(raw, str) or len(raw.encode()) > 65536:
                raise ValueError(
                    "Cluster confirmation exceeded its bounded text response"
                )
            names = re.findall(
                r"(?m)^\s*ClusterName\s*=\s*([A-Za-z0-9_.-]{1,128})\s*$", raw
            )
            if len(names) != 1:
                raise ValueError("Scheduler did not provide one exact ClusterName")
            confirmed_cluster = names[0]
        except (OSError, ValueError, RuntimeError) as exc:
            for row, _, _, _ in eligible:
                row["reason"] = (
                    "Default scheduler cluster was not verified; wrapper preserved: "
                    + str(exc)
                )
            return set()
        retained = []
        for candidate in eligible:
            if candidate[3]["cluster"] == confirmed_cluster:
                retained.append(candidate)
            else:
                candidate[0]["reason"] = (
                    "Current default ClusterName differs from the retained job cluster; wrapper preserved."
                )
        eligible = retained
        if not eligible:
            return set()
        cluster = confirmed_cluster
        jobs = {str(candidate[3]["job_id"]) for candidate in eligible}
    # Explicit configured cluster matching above admits exactly one cluster.
    if any(candidate[3]["cluster"] != cluster for candidate in eligible):
        for row, _, _, _ in eligible:
            row["reason"] = (
                "Multiple retained clusters cannot be confirmed in one cleanup; select an explicit cluster_id."
            )
        return set()
    argv = [
        "squeue",
        "--noheader",
        "--jobs=" + ",".join(sorted(jobs)),
        "--format=%i",
        "--clusters=" + cluster,
    ]
    try:
        raw = backend.command(argv, timeout=5.0, limit=65536, cancel=None)
        if not isinstance(raw, str) or len(raw.encode()) > 65536:
            raise ValueError(
                "Scheduler confirmation exceeded its bounded text response"
            )
        active = set()
        for line in raw.splitlines():
            value = line.strip()
            if not value:
                continue
            match = re.fullmatch(
                r"([1-9][0-9]{0,18})(?:_(?:[0-9]{1,19}|\[[0-9,%:-]{1,4096}\]))?", value
            )
            if match is None:
                raise ValueError(
                    "Scheduler confirmation contained an unrecognized job identity"
                )
            active.add(match[1])
    except (OSError, ValueError, RuntimeError) as exc:
        for row, _, _, _ in eligible:
            row["reason"] = (
                "Scheduler absence was not verified; wrapper preserved: " + str(exc)
            )
        return set()
    result = set()
    for row, folder, _, receipt in eligible:
        if str(receipt["job_id"]).partition("_")[0] in active:
            row["reason"] = (
                "Scheduler still lists this job or another task in its array; wrapper preserved."
            )
        else:
            result.add(folder.name)
    return result


def cleanup(project, *, apply=False, older_than_days=30.0, execution=None):
    if (
        isinstance(older_than_days, bool)
        or not isinstance(older_than_days, (int, float))
        or not math.isfinite(older_than_days)
        or not 0 <= older_than_days <= 36500
    ):
        raise ValueError("Cleanup retention age must be 0–36500 days")
    root = R._directory(project)
    wrappers = root / ".raven/wrappers"
    result = {
        "schema": "raven.runtime-cleanup/v1",
        "apply": apply,
        "wrappers": [],
        "archives": [],
        "removed_bytes": 0,
        "cache_policy": "Shared content-addressed archives and extracted version caches are retained for detached processes; recordings and results are never removed",
    }
    if not wrappers.exists():
        return result
    _owned(wrappers, directory=True)
    cutoff = time.time() - older_than_days * 86400
    if execution is None:
        from .execution.policy import router_for

        execution = router_for()
    with lock(root, name="runtime-cleanup.lock"):
        folders = sorted(_entries(wrappers, 10000))
        deadline = time.monotonic() + 30
        candidates = []
        for folder in folders:
            if not re.fullmatch(r"[0-9a-f]{32}", folder.name):
                continue
            row = {
                "token": folder.name,
                "path": str(folder.relative_to(root)),
                "status": "preserved",
            }
            result["wrappers"].append(row)
            try:
                info = _owned(folder, directory=True)
                if info.st_mtime > cutoff:
                    raise ValueError("Newer than the selected retention age")
                if time.monotonic() >= deadline:
                    raise ValueError(
                        "Cleanup work budget exhausted; rerun for remaining wrappers"
                    )
                size, receipt = _eligible(root, folder, cutoff)
                candidates.append((row, folder, size, receipt))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                row["reason"] = str(exc)
        absent = _confirmed_absent(candidates, execution)
        for row, folder, size, receipt in candidates:
            if receipt is not None and folder.name not in absent:
                continue
            row.update(status="removed" if apply else "eligible", bytes=size)
            if apply:
                os.chmod(folder, 0o700)  # Older versions used 0500.
                shutil.rmtree(folder)
                result["removed_bytes"] += size
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="raven trace cleanup-runtime",
        description="Preview completed launch runtime cleanup; queued/active/unverified launches are preserved",
    )
    parser.add_argument("--project", default=os.getcwd())
    parser.add_argument("--older-than-days", type=float, default=30.0)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                cleanup(
                    args.project, apply=args.apply, older_than_days=args.older_than_days
                ),
                indent=2,
            )
        )
        return 0
    except (OSError, ValueError) as exc:
        print("Raven runtime cleanup: " + str(exc), file=sys.stderr)
        return 2
