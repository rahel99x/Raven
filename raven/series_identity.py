"""Durable recording identity; scheduler numbers alone never identify attempts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
import re
import socket


@dataclass(frozen=True)
class SeriesIdentity:
    job: str
    scope: str
    attempt: str
    verified: bool

    @property
    def filename(self):
        # Reversible job component for bounded inventory; the digest prevents
        # sanitized-name collisions and binds the complete scope and attempt.
        job = self.job.encode("utf-8")
        if not job or len(job) > 64:
            return None
        digest = fingerprint((self.scope, self.job, self.attempt, self.verified))
        return "v2-" + job.hex() + "-" + digest + ".jsonl"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def default_scope(state_dir):
    return {"host": socket.gethostname(), "uid": os.getuid(), "source": os.path.realpath(state_dir or ".")}


def scheduler_attempt(record, details):
    """Require exact scheduler evidence, including restart count.

    Submit/start alone have one-second resolution and cannot distinguish a
    rapid requeue. Missing or conflicting counters deliberately abstain.
    """
    if record is None or not isinstance(details, dict):
        return None
    submit, start = getattr(record, "submit", ""), getattr(record, "start", "")
    def valid(value):
        if not isinstance(value, str) or len(value) > 64 or "T" not in value:
            return False
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False
    if not valid(submit) or not valid(start):
        return None
    restart = getattr(record, 'restarts', None)
    cluster = getattr(record, 'cluster', '')
    if type(restart) is int and 0 <= restart <= 9999999999 and isinstance(cluster, str) and cluster:
        return fingerprint({'cluster': cluster, 'job': record.id, 'submit': submit, 'start': start, 'restarts': restart})
    direct = details.get("JobId") == record.id
    array = ("_" in record.id and record.id == str(details.get("ArrayJobId", "")) + "_" + str(details.get("ArrayTaskId", "")))
    heterogeneous = ("+" in record.id and record.id == str(details.get("HetJobId", "")) + "+" + str(details.get("HetJobOffset", "")))
    if not (direct or array or heterogeneous) or details.get("SubmitTime") != submit or details.get("StartTime") != start:
        return None
    restart = details.get("Restarts")
    if not isinstance(restart, str) or not re.fullmatch(r"[0-9]{1,10}", restart):
        return None
    cluster = getattr(record, "cluster", "") or details.get("Cluster", "")
    if getattr(record, "cluster", "") and details.get("Cluster", cluster) != cluster:
        return None
    return fingerprint({"cluster": cluster, "job": record.id, "submit": submit, "start": start, "restarts": int(restart)})


def inventory_job(filename):
    match = re.fullmatch(r"v2-([0-9a-f]{2,128})-[0-9a-f]{64}\.(?:jsonl|rvb)", filename)
    if match:
        try:
            return bytes.fromhex(match[1]).decode("utf-8")
        except (ValueError, UnicodeError):
            pass
    return None
