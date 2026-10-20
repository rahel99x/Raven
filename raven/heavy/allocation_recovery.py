"""Finite, persisted reconciliation of Raven's own foreground allocations.

A receipt is evidence, never authority to cancel a bare recycled job number.
Only controller-atomic name/user filtering on qualified Slurm versions may
cancel an orphan. Empty accounting alone is unknown; separated successful
controller reads can establish absence of a unique one-shot owned request.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
import time

MAX_ATTEMPTS = 8
# Source review is insufficient to qualify a deployment. These exact upstream
# client/controller versions also passed real scheduler filter-mismatch tests.
ATOMIC_CANCEL_VERSIONS = frozenset({"26.05.5"})
TERMINAL = frozenset(
    {
        "COMPLETED",
        "CANCELLED",
        "FAILED",
        "TIMEOUT",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PREEMPTED",
        "BOOT_FAIL",
        "DEADLINE",
        "REVOKED",
    }
)


from .allocation_identity import connection_binding, receipt_path
from .allocation_identity import (
    receipt_key as receipt_key,
    recovery_plan as recovery_plan,
)


def _read(backend, argv, cancel, limit=65536):
    if cancel is not None and cancel.is_set():
        raise ValueError("Allocation recovery cancelled")
    value = backend.command(argv, 8.0, limit, cancel)
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
    timeout = re.findall(
        r"^\s*MessageTimeout\s*=\s*([0-9]{1,4})(?:\s+sec)?\s*$", text, re.M
    )
    fields["MessageTimeout"] = (
        max(1, min(600, int(timeout[0]))) if len(timeout) == 1 else 10
    )
    return fields


def _owned(fields, receipt, uid, cluster):
    owner = re.fullmatch(r"[^()\s]+\(([0-9]+)\)", fields.get("UserId", ""))
    if (
        owner is None
        or int(owner[1]) != uid
        or fields.get("Cluster", cluster) != cluster
    ):
        return False
    identity = receipt.get("identity") or {}
    token = receipt.get("request_name")
    if token:
        return fields.get("JobName") == token
    # Older receipts remain useful as terminal evidence only. They never
    # authorize mutation; a matching job number alone is never sufficient.
    return bool(
        identity.get("verified")
        and identity.get("submit_time")
        and identity.get("start_time")
        and fields.get("SubmitTime") == identity["submit_time"]
        and fields.get("StartTime") == identity["start_time"]
        and str(identity.get("allocation_id", identity.get("job_id", "")))
        == fields.get("JobId")
    )


def _terminal(fields):
    return (fields.get("JobState", "").split() or [""])[0].rstrip("+") in TERMINAL


def _accounting(backend, receipt, uid, cluster, cancel):
    identity = receipt.get("identity") or {}
    submit = identity.get("submit_time")
    if not submit and isinstance(receipt.get("created_at"), (int, float)):
        # Local request creation is only a bounded search hint, never proof.
        from datetime import datetime, timezone

        submit = datetime.fromtimestamp(receipt["created_at"], timezone.utc).isoformat(
            timespec="seconds"
        )
    if submit:
        # This is a pure search hint, not a Slurm instance method or identity.
        from datetime import datetime, timedelta

        observed = datetime.fromisoformat(submit)
        start = (observed - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00")
        end = (observed + timedelta(days=2)).strftime("%Y-%m-%dT00:00:00")
    else:
        start, end = "now-14days", "now"
    job = str(receipt.get("job_id") or identity.get("allocation_id") or "")
    token = receipt.get("request_name", "")
    if not job and not token:
        raise ValueError(
            "Prior request has no exact job or unique name; inspect its receipt and scheduler records"
        )
    selector = ["-j", job] if job else ["--name", token]
    output = _read(
        backend,
        [
            "sacct",
            *selector,
            "-u",
            str(uid),
            "-X",
            "-D",
            "-S",
            start,
            "-E",
            end,
            "-n",
            "-P",
            "-o",
            "JobID,JobName,State,UID,Submit,Start,Cluster",
        ],
        cancel,
    )
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
        candidate = {
            "JobId": jid,
            "JobName": name,
            "JobState": state,
            "UserId": "accounting(" + owner + ")",
            "SubmitTime": submitted,
            "StartTime": started,
            "Cluster": actual_cluster,
        }
        if _owned(candidate, receipt, uid, cluster):
            matches.append(candidate)
    if not matches:
        raise ValueError(
            "No exact owned terminal record is available; empty or delayed accounting is not cleanup proof"
        )
    if not all(_terminal(item) for item in matches):
        raise ValueError("Accounting still contains an active or unknown owned attempt")
    return "exact owned terminal accounting records"


def _observe(backend, receipt, planned, cancel, *, now=None):
    from ..slurm import parse_kv

    if planned["site_kind"] != "slurm":
        from .allocation_ownership import boot_identity, process_start
        import socket

        if receipt.get("helper_host") != socket.gethostname():
            raise ValueError(
                "Local receipt belongs to another host; inspect its original owner before forgetting the request"
            )
        old_boot, actual_boot = receipt.get("helper_boot"), boot_identity()
        if (
            old_boot
            and old_boot != "unavailable"
            and actual_boot != "unavailable"
            and old_boot != actual_boot
        ):
            return True, "original local worker boot ended"
        pid, start = receipt.get("helper_pid"), receipt.get("helper_start")
        if type(pid) is int and pid > 1 and start and start != "unavailable":
            actual_start = process_start(pid)
            absent = False
            if actual_start == "unavailable":
                try:
                    os.kill(
                        pid, 0
                    )  # Existence check only; never signal a retained PID.
                except ProcessLookupError:
                    absent = True
                except PermissionError:
                    pass
            if absent or actual_start != "unavailable" and actual_start != start:
                # This function runs under the local flock. A recycled PID is
                # never signalled; inaccessible metadata proves nothing.
                return True, "original local helper no longer owns its process identity"
        raise ValueError(
            "Local helper ownership is not yet disproved; inspect the receipt before forgetting the request"
        )
    if getattr(backend, "ledger", None) is None:
        raise ValueError("Recovery requires the governed collection owner")
    binding = receipt.get("connection_binding")
    legacy_fields = {
        key: planned.get(key)
        for key in (
            "site_kind",
            "cluster_id",
            "target",
            "transport",
            "ssh_target",
            "ssh_options",
            "owner_uid",
        )
    }
    legacy_fields["cluster_id"] = receipt.get("cluster_id", planned.get("cluster_id"))
    legacy_binding = hashlib.sha256(
        json.dumps(legacy_fields, sort_keys=True).encode()
    ).hexdigest()
    if binding and binding not in {connection_binding(planned), legacy_binding}:
        raise ValueError(
            "Connection identity changed; restore the original profile before recovery"
        )
    config = _configuration(
        _read(backend, ["scontrol", "show", "config"], cancel, 262144)
    )
    cluster = (
        receipt.get("cluster_id") or planned.get("cluster_id") or config["ClusterName"]
    )
    if not cluster or config["ClusterName"] != cluster:
        raise ValueError(
            "The current controller does not match the receipt's explicit cluster"
        )
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
    prefix = (
        "raven-step-"
        if receipt.get("ownership") == "owned-step-only"
        else "raven-heavy-"
    )
    allowed_tokens = {prefix + receipt.get("request_id", "")}
    if receipt.get("ownership") == "owned-step-only":
        allowed_tokens.add(
            "raven-heavy-" + receipt.get("request_id", "")
        )  # Legacy step receipt, never parent authority.
    if token and token not in allowed_tokens:
        raise ValueError("Receipt name does not match the original request token")
    if receipt.get("ownership") == "owned-step-only":
        return _observe_step(backend, receipt, uid, cluster, cancel)
    if not job and token:
        queued = _read(
            backend,
            ["squeue", "--name", token, "-u", str(uid), "-h", "-o", "%i"],
            cancel,
            4096,
        ).split()
        if len(queued) > 1 or any(
            not re.fullmatch(r"[0-9]{1,20}", item) for item in queued
        ):
            raise ValueError(
                "The pending request name does not identify one allocation"
            )
        job = queued[0] if queued else ""
    if job and not re.fullmatch(r"[0-9]{1,20}", job):
        raise ValueError("Invalid retained allocation identifier")
    fields = {}
    if job:
        try:
            output = _read(backend, ["scontrol", "show", "job", job, "-o"], cancel)
        except Exception as exc:
            from ..slurm import CommandError

            if not isinstance(exc, CommandError) or not any(
                word in str(exc).lower()
                for word in (
                    "invalid job id",
                    "invalid job_id",
                    "invalid job identifier",
                )
            ):
                raise
        else:
            if len(output.strip().splitlines()) != 1:
                raise ValueError("Controller did not return one exact allocation")
            fields = parse_kv(output)
    if fields.get("JobId") == job and _owned(fields, receipt, uid, cluster):
        if _terminal(fields):
            return True, "exact owned terminal controller record"
        if receipt.get("ownership") != "dedicated-allocation":
            raise ValueError(
                "Only the owned step may be released; an active reused parent is never cancelled"
            )
        if not binding or not token or not receipt.get("cleanup_on_loss"):
            raise ValueError(
                "Legacy active receipt has no unique reviewed cleanup authority; inspect its exact allocation"
            )
        import socket

        if receipt.get("helper_host") not in (None, "", socket.gethostname()):
            raise ValueError(
                "The active request belongs to another login host; return to that owner or use the reviewed forget action after checking its allocation"
            )
        client = _read(backend, ["scancel", "--version"], cancel, 256).strip()
        match = re.fullmatch(r"slurm (\d+\.\d+\.\d+)", client)
        if (
            match is None
            or match[1] not in ATOMIC_CANCEL_VERSIONS
            or config["SLURM_VERSION"] not in ATOMIC_CANCEL_VERSIONS
        ):
            raise ValueError(
                "Atomic filtered cancellation is not qualified for this client/controller version; inspect the owned allocation"
            )
        # --ctld performs all filters in one controller RPC. Never fall back to
        # scancel JOBID, even after an error, timeout, or unsupported response.
        _read(
            backend,
            ["scancel", "--ctld", "--name=" + token, "--user=" + str(uid), job],
            cancel,
            4096,
        )
        return (
            False,
            "Atomic owned cancellation requested; terminal confirmation is still required",
        )
    try:
        return True, _accounting(backend, receipt, uid, cluster, cancel)
    except ValueError as exc:
        if not token or "No exact owned terminal record" not in str(exc):
            raise
        # A successful name+uid lookup is controller evidence. Two separated
        # reads establish absence of this unique one-shot request, not merely
        # absence from delayed accounting.
        queued = _read(
            backend,
            ["squeue", "--name", token, "-u", str(uid), "-h", "-o", "%i"],
            cancel,
            4096,
        ).split()
        if queued:
            raise
        import socket

        if receipt.get("helper_host") not in (None, "", socket.gethostname()):
            raise ValueError(
                "Absence cannot disprove a helper on another login host; inspect that owner before forgetting the request"
            )
        recovery = receipt.setdefault("recovery", {})
        observed = time.time() if now is None else now
        first = recovery.get("absence_verified_at")
        if (
            isinstance(first, (int, float))
            and config["MessageTimeout"] + 2 <= observed - first <= 86400
        ):
            return (
                True,
                "unique owned request absent from two verified controller reads",
            )
        if not isinstance(first, (int, float)) or not 0 <= observed - first <= 86400:
            recovery["absence_verified_at"] = observed
        return (
            False,
            "Unique owned request is absent; waiting for a second verified controller read",
        )


def _observe_step(backend, receipt, uid, cluster, cancel):
    identity = receipt.get("identity") or {}
    reuse = receipt.get("reuse") or {}
    job = str(
        receipt.get("job_id")
        or reuse.get("job_id")
        or identity.get("allocation_id")
        or ""
    )
    step = str(receipt.get("step_id") or identity.get("step_id") or "")
    if not re.fullmatch(r"[0-9]{1,20}", job):
        raise ValueError(
            "Reused receipt lacks its exact parent; the parent is never cancelled"
        )
    if step and not re.fullmatch(r"[0-9]{1,20}", step):
        raise ValueError("Invalid owned step identity; parent is never cancelled")
    # A step terminal record is sufficient even if its parent remains running.
    start, end = "now-14days", "now"
    selector = job + "." + step if step else job
    output = _read(
        backend,
        [
            "sacct",
            "-j",
            selector,
            "-u",
            str(uid),
            "--name",
            receipt.get("request_name", ""),
            "-D",
            "-S",
            start,
            "-E",
            end,
            "-n",
            "-P",
            "-o",
            "JobID,JobName,State,UID,Submit,Start,Cluster",
        ],
        cancel,
    )
    matches = []
    for row in output.splitlines():
        fields = row.rstrip("|").split("|")
        if len(fields) != 7:
            raise ValueError("Owned step accounting is ambiguous")
        jid, name, state, owner, submitted, started, actual_cluster = fields
        exact_step = (
            jid == job + "." + step
            if step
            else bool(re.fullmatch(re.escape(job) + r"\.[0-9]{1,20}", jid))
        )
        if (
            exact_step
            and owner == str(uid)
            and actual_cluster == cluster
            and name == receipt.get("request_name")
        ):
            matches.append(state.rstrip("+"))
    if matches and all(state in TERMINAL for state in matches):
        return True, "exact owned terminal step records; parent allocation preserved"
    raise ValueError("Owned step is active or unconfirmed; parent is never cancelled")


def reconcile(directory, planned, backend, cancel=None, *, reset=False, now=None):
    """One background attempt; lock, budget and next retry survive UI restart."""
    now = time.time() if now is None else now
    path = receipt_path(directory, planned)
    if not path.exists() and not path.with_suffix(".claim").exists():
        try:
            if not any(
                item.name.endswith(".json") and len(item.name) == 37
                for item in path.parent.iterdir()
            ):
                return None
        except FileNotFoundError:
            return None
    from .allocation_ownership import OwnershipRecord

    # Kernel ownership, not hostnames/PIDs, decides whether an owner is active.
    record = OwnershipRecord(directory, planned, secrets.token_hex(16), reconcile=True)
    try:
        receipt = record.previous
        if not receipt:
            return None
        if receipt.get("state") == "light" and not receipt.get("cleanup_uncertain"):
            return {
                "state": "light",
                "mode": "light",
                "cleanup_uncertain": False,
                "error": "",
                "recovery": {"phase": "resolved", "next_retry": 0.0},
                "cleanup_basis": receipt.get(
                    "cleanup_basis", "confirmed owner cleanup receipt"
                ),
            }
        if not re.fullmatch(r"[0-9a-f]{32}", str(receipt.get("request_id", ""))):
            raise ValueError(
                "Prior allocation request identity is invalid; preserve and inspect its receipt"
            )
        recovery = dict(receipt.get("recovery") or {})
        attempts = recovery.get("attempts", 0)
        if type(attempts) is not int or not 0 <= attempts <= MAX_ATTEMPTS:
            raise ValueError("Invalid recovery attempt budget")
        if reset:
            attempts = 0
            recovery = {}
        next_retry = recovery.get("next_retry", 0.0)
        if not isinstance(next_retry, (int, float)) or not 0 <= next_retry <= now + 600:
            next_retry = now + 300
        result = {
            "state": "reconciling",
            "mode": "light",
            "cleanup_uncertain": True,
            "receipt_path": str(path),
            "request_id": receipt.get("request_id", ""),
            "job_id": receipt.get("job_id", ""),
            "recovery": recovery,
            "error": recovery.get("reason") or receipt.get("error", ""),
        }
        if attempts >= MAX_ATTEMPTS or now < next_retry:
            return result
        attempts += 1
        delay = min(300.0, 5.0 * 2 ** (attempts - 1)) * random.SystemRandom().uniform(
            0.9, 1.1
        )
        recovery = {
            **recovery,
            "attempts": attempts,
            "budget": MAX_ATTEMPTS,
            "next_retry": now + delay,
            "phase": "checking",
            "next_action": "Retry cleanup after checking the profile and scheduler",
        }
        receipt = {
            **receipt,
            "state": "reconciling",
            "cleanup_uncertain": True,
            "recovery": recovery,
        }
        record.write(receipt)  # Persist reservation BEFORE any scheduler action.
        complete = False
        try:
            complete, reason = _observe(backend, receipt, planned, cancel, now=now)
            recovery["phase"] = "resolved" if complete else "confirming"
        except Exception as exc:
            from ..raven_collection import Deferred

            reason = str(exc)[:2048]
            recovery["phase"] = "deferred" if isinstance(exc, Deferred) else "waiting"
            if isinstance(exc, Deferred):
                recovery["next_retry"] = now + max(1.0, min(300.0, exc.retry_after))
        recovery["reason"] = reason
        if attempts >= MAX_ATTEMPTS and not complete:
            recovery["phase"] = "paused"
            recovery["next_action"] = (
                "Automatic budget exhausted; inspect the error, then choose Retry cleanup"
            )
        receipt.update(
            state="light" if complete else "reconciling",
            cleanup_uncertain=not complete,
            recovery=recovery,
            error="" if complete else reason,
            updated_at=now,
        )
        if complete:
            receipt["cleanup_basis"] = reason
            recovery["next_retry"] = 0.0
            recovery["next_action"] = (
                "Request compute when needed; no scientific job was resubmitted"
            )
        record.write(receipt)
        return {
            **result,
            "state": receipt["state"],
            "error": receipt["error"],
            "cleanup_uncertain": not complete,
            "recovery": recovery,
            "cleanup_basis": receipt.get("cleanup_basis", ""),
        }
    finally:
        record.close()


def forget(directory, planned, request_id):
    """Explicit reviewed bookkeeping override; never cancels scheduler work."""
    if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("Forgetting requires the exact reviewed request token")
    from .allocation_ownership import OwnershipRecord

    record = OwnershipRecord(directory, planned, secrets.token_hex(16), reconcile=True)
    try:
        receipt = record.previous
        if not receipt or receipt.get("request_id") != request_id:
            raise ValueError("The ownership receipt changed; review it again")
        archive = record.path.stem + "-" + request_id + "-forgotten.json"
        try:
            os.link(
                record.path.name,
                archive,
                src_dir_fd=record.directory_fd,
                dst_dir_fd=record.directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise ValueError(
                "This request already has a forgotten-receipt archive; inspect it before retrying"
            ) from None
        except FileNotFoundError:
            # A crash can leave only the exclusive claim. Publish the preserved
            # evidence first rather than losing it during the override.
            record.write(receipt)
            os.link(
                record.path.name,
                archive,
                src_dir_fd=record.directory_fd,
                dst_dir_fd=record.directory_fd,
                follow_symlinks=False,
            )
        value = {
            **receipt,
            "state": "light",
            "mode": "light",
            "cleanup_uncertain": False,
            "error": "",
            "manually_forgotten": True,
            "forgotten_at": time.time(),
            "cleanup_basis": "User explicitly forgot bookkeeping after independent inspection; scheduler cleanup was not verified",
            "recovery": {"phase": "manually forgotten", "next_retry": 0.0},
        }
        record.write(value)
        return value
    finally:
        record.close()
