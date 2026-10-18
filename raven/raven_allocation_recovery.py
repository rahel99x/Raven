"""Finite, persisted reconciliation of Raven's own foreground allocations.

A receipt is evidence, never authority to cancel a bare recycled job number.
Only controller-atomic name/user filtering on qualified Slurm versions may
cancel an orphan. Read failures and an empty accounting response remain unknown.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import random
import re
import secrets
import time

MAX_ATTEMPTS = 8
# Source review is insufficient to qualify a deployment. These exact upstream
# client/controller versions also passed real scheduler filter-mismatch tests.
ATOMIC_CANCEL_VERSIONS = frozenset({"26.05.5"})
TERMINAL = frozenset({"COMPLETED", "CANCELLED", "FAILED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED"})


def connection_binding(plan):
    fields = {key: plan.get(key) for key in ("site_kind", "cluster_id", "target", "transport", "ssh_target", "ssh_options", "owner_uid")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def receipt_path(directory, plan):
    key = hashlib.sha256(json.dumps({"site": plan["site_kind"], "cluster": plan["cluster_id"], "target": plan["target"]}, sort_keys=True).encode()).hexdigest()[:32]
    return Path(directory).expanduser() / "allocations" / (key + ".json")


def _read(backend, argv, cancel, limit=65536):
    if cancel is not None and cancel.is_set():
        raise ValueError("Allocation recovery cancelled")
    value = backend.command(argv, 8., limit, cancel)
    if not isinstance(value, str) or len(value.encode()) > limit:
        raise ValueError("Allocation recovery response exceeded its bound")
    return value


def _configuration(text):
    fields = {}
    for key in ("ClusterName", "SLURM_VERSION"):
        matches = re.findall(r"^\s*" + key + r"\s*=\s*(\S+)\s*$", text, re.M)
        if len(matches) != 1:
            raise ValueError("The actual controller " + key + " could not be verified")
        fields[key] = matches[0]
    return fields


def _owned(fields, receipt, uid, cluster):
    owner = re.fullmatch(r"[^()\s]+\(([0-9]+)\)", fields.get("UserId", ""))
    if owner is None or int(owner[1]) != uid or fields.get("Cluster", cluster) != cluster:
        return False
    identity = receipt.get("identity") or {}
    token = receipt.get("request_name")
    if token:
        return fields.get("JobName") == token
    # Older receipts remain useful as terminal evidence only. They never
    # authorize mutation; a matching job number alone is never sufficient.
    return bool(identity.get("verified") and identity.get("submit_time") and identity.get("start_time")
                and fields.get("SubmitTime") == identity["submit_time"]
                and fields.get("StartTime") == identity["start_time"]
                and str(identity.get("allocation_id", identity.get("job_id", ""))) == fields.get("JobId"))


def _terminal(fields):
    return (fields.get("JobState", "").split() or [""])[0].rstrip("+") in TERMINAL


def _accounting(backend, receipt, uid, cluster, cancel):
    from .slurm import Slurm
    identity = receipt.get("identity") or {}
    submit = identity.get("submit_time")
    if not submit and isinstance(receipt.get("created_at"), (int, float)):
        # Local request creation is only a bounded search hint, never proof.
        from datetime import datetime, timezone
        submit = datetime.fromtimestamp(receipt["created_at"], timezone.utc).isoformat()
    start, end, _ = Slurm.accounting_window(None, {"submit": submit}) if submit else ("now-14days", "now", "bounded fallback")
    job = str(receipt.get("job_id") or identity.get("allocation_id") or "")
    token = receipt.get("request_name", "")
    if not job and not token:
        raise ValueError("Prior request has no exact job or unique name; inspect its receipt and scheduler records")
    selector = ["-j", job] if job else ["--name", token]
    output = _read(backend, ["sacct", *selector, "-u", str(uid), "-X", "-D", "-S", start, "-E", end, "-n", "-P", "-o", "JobID,JobName,State,UID,Submit,Start,Cluster"], cancel)
    matches = []
    for row in output.splitlines():
        fields = row.split("|")
        if len(fields) == 8 and fields[-1] == "":
            fields.pop()
        if len(fields) != 7 or any(len(item.encode()) > 4096 for item in fields):
            raise ValueError("Accounting returned an ambiguous recovery record")
        jid, name, state, owner, submitted, started, actual_cluster = fields
        if not re.fullmatch(r"[0-9]{1,20}", jid) or job and jid != job:
            continue
        candidate = {"JobId": jid, "JobName": name, "JobState": state, "UserId": "accounting(" + owner + ")", "SubmitTime": submitted, "StartTime": started, "Cluster": actual_cluster}
        if _owned(candidate, receipt, uid, cluster):
            matches.append(candidate)
    if not matches:
        raise ValueError("No exact owned terminal record is available; empty or delayed accounting is not cleanup proof")
    if not all(_terminal(item) for item in matches):
        raise ValueError("Accounting still contains an active or unknown owned attempt")
    return "exact owned terminal accounting records"


def _observe(backend, receipt, planned, cancel):
    from .slurm import parse_kv
    if planned["site_kind"] != "slurm":
        raise ValueError("Local orphan cleanup requires verified process ownership; inspect the receipt, never signal a recorded PID")
    if getattr(backend, "ledger", None) is None:
        raise ValueError("Recovery requires the governed collection owner")
    binding = receipt.get("connection_binding")
    if binding and binding != connection_binding(planned):
        raise ValueError("Connection identity changed; restore the original profile before recovery")
    config = _configuration(_read(backend, ["scontrol", "show", "config"], cancel, 262144))
    cluster = planned.get("cluster_id")
    if not cluster or config["ClusterName"] != cluster:
        raise ValueError("The current controller does not match the receipt's explicit cluster")
    uid = os.getuid()
    if planned["transport"] == "ssh":
        raw_uid = _read(backend, ["id", "-u"], cancel, 64).strip()
        if not re.fullmatch(r"[0-9]{1,10}", raw_uid):
            raise ValueError("Remote account UID is unverifiable")
        uid = int(raw_uid)
    identity = receipt.get("identity") or {}
    recorded_uid = identity.get("owner_uid", identity.get("uid"))
    if recorded_uid is not None and recorded_uid != uid:
        raise ValueError("Recorded allocation owner differs from the current account")
    job = str(receipt.get("job_id") or identity.get("allocation_id") or "")
    token = receipt.get("request_name", "")
    if token and token != "raven-heavy-" + receipt.get("request_id", ""):
        raise ValueError("Receipt name does not match the original request token")
    if not job and token:
        queued = _read(backend, ["squeue", "--name", token, "-u", str(uid), "-h", "-o", "%i"], cancel, 4096).split()
        if len(queued) > 1 or any(not re.fullmatch(r"[0-9]{1,20}", item) for item in queued):
            raise ValueError("The pending request name does not identify one allocation")
        job = queued[0] if queued else ""
    if job and not re.fullmatch(r"[0-9]{1,20}", job):
        raise ValueError("Invalid retained allocation identifier")
    fields = {}
    if job:
        try:
            output = _read(backend, ["scontrol", "show", "job", job, "-o"], cancel)
        except Exception as exc:
            from .slurm import CommandError
            if not isinstance(exc, CommandError) or not any(word in str(exc).lower() for word in ("invalid job id", "invalid job_id", "invalid job identifier")):
                raise
        else:
            if len(output.strip().splitlines()) != 1:
                raise ValueError("Controller did not return one exact allocation")
            fields = parse_kv(output)
    if fields.get("JobId") == job and _owned(fields, receipt, uid, cluster):
        if _terminal(fields):
            return True, "exact owned terminal controller record"
        if receipt.get("ownership") != "dedicated-allocation":
            raise ValueError("Only the owned step may be released; an active reused parent is never cancelled")
        if not binding or not token or not receipt.get("cleanup_on_loss"):
            raise ValueError("Legacy active receipt has no unique reviewed cleanup authority; inspect its exact allocation")
        client = _read(backend, ["scancel", "--version"], cancel, 256).strip()
        match = re.fullmatch(r"slurm (\d+\.\d+\.\d+)", client)
        if match is None or match[1] not in ATOMIC_CANCEL_VERSIONS or config["SLURM_VERSION"] not in ATOMIC_CANCEL_VERSIONS:
            raise ValueError("Atomic filtered cancellation is not qualified for this client/controller version; inspect the owned allocation")
        # --ctld performs all filters in one controller RPC. Never fall back to
        # scancel JOBID, even after an error, timeout, or unsupported response.
        _read(backend, ["scancel", "--ctld", "--name=" + token, "--user=" + str(uid), job], cancel, 4096)
        return False, "Atomic owned cancellation requested; terminal confirmation is still required"
    return True, _accounting(backend, receipt, uid, cluster, cancel)


def reconcile(directory, planned, backend, cancel=None, *, reset=False, now=None):
    """One background attempt; lock, budget and next retry survive UI restart."""
    now = time.time() if now is None else now
    path = receipt_path(directory, planned)
    if not path.exists():
        return None
    from .raven_allocation_helper import OwnershipRecord
    # Kernel ownership, not hostnames/PIDs, decides whether an owner is active.
    record = OwnershipRecord(directory, planned, secrets.token_hex(16), reconcile=True)
    try:
        receipt = record.previous
        if not receipt:
            return None
        if receipt.get("state") == "light" and not receipt.get("cleanup_uncertain"):
            return {"state": "light", "mode": "light", "cleanup_uncertain": False, "error": "",
                    "recovery": {"phase": "resolved", "next_retry": 0.},
                    "cleanup_basis": receipt.get("cleanup_basis", "confirmed owner cleanup receipt")}
        if not re.fullmatch(r"[0-9a-f]{32}", str(receipt.get("request_id", ""))):
            raise ValueError("Prior allocation request identity is invalid; preserve and inspect its receipt")
        recovery = dict(receipt.get("recovery") or {})
        attempts = recovery.get("attempts", 0)
        if type(attempts) is not int or not 0 <= attempts <= MAX_ATTEMPTS:
            raise ValueError("Invalid recovery attempt budget")
        if reset:
            attempts = 0
            recovery = {}
        next_retry = recovery.get("next_retry", 0.)
        if not isinstance(next_retry, (int, float)) or not 0 <= next_retry <= now + 600:
            next_retry = now + 300
        result = {"state": "reconciling", "mode": "light", "cleanup_uncertain": True, "receipt_path": str(path), "job_id": receipt.get("job_id", ""), "recovery": recovery, "error": recovery.get("reason") or receipt.get("error", "")}
        if attempts >= MAX_ATTEMPTS or now < next_retry:
            return result
        attempts += 1
        delay = min(300., 5. * 2 ** (attempts - 1)) * random.SystemRandom().uniform(.9, 1.1)
        recovery = {"attempts": attempts, "budget": MAX_ATTEMPTS, "next_retry": now + delay, "phase": "checking", "next_action": "Retry cleanup after checking the profile and scheduler"}
        receipt = {**receipt, "state": "reconciling", "cleanup_uncertain": True, "recovery": recovery}
        record.write(receipt)  # Persist reservation BEFORE any scheduler action.
        complete = False
        try:
            complete, reason = _observe(backend, receipt, planned, cancel)
            recovery["phase"] = "resolved" if complete else "confirming"
        except Exception as exc:
            from .raven_collection import Deferred
            reason = str(exc)[:2048]
            recovery["phase"] = "deferred" if isinstance(exc, Deferred) else "waiting"
            if isinstance(exc, Deferred):
                recovery["next_retry"] = now + max(1., min(300., exc.retry_after))
        recovery["reason"] = reason
        if attempts >= MAX_ATTEMPTS and not complete:
            recovery["phase"] = "paused"
            recovery["next_action"] = "Automatic budget exhausted; inspect the error, then choose Retry cleanup"
        receipt.update(state="light" if complete else "reconciling", cleanup_uncertain=not complete, recovery=recovery, error="" if complete else reason, updated_at=now)
        if complete:
            receipt["cleanup_basis"] = reason
            recovery["next_retry"] = 0.
            recovery["next_action"] = "Request compute when needed; no scientific job was resubmitted"
        record.write(receipt)
        return {**result, "state": receipt["state"], "error": receipt["error"], "cleanup_uncertain": not complete, "recovery": recovery, "cleanup_basis": receipt.get("cleanup_basis", "")}
    finally:
        record.close()
