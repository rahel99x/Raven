"""Durable exactly-once protection for reviewed legacy job clones.

An unknown scheduler result retains its unique comment marker. A fresh review
of the same source/argv cannot submit until accounting verifies that marker.
Empty or delayed accounting is never permission to submit a duplicate.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

from .config_trust import directory_fd
from .resubmit import apply_overrides, parse_submitted


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def _write(directory, name, value):
    raw = _encoded(value)
    if len(raw) > 1 << 20:
        raise ValueError("Resubmit intent exceeds its 1 MiB bound")
    temporary = ".intent-" + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _read(directory, name):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        import stat
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 1 << 20:
            raise ValueError("Resubmit intent must be a private bounded regular file")
        return json.loads(stream.read((1 << 20) + 1))


@contextmanager
def _locked(path):
    with directory_fd(path, private=True, create=True) as directory:
        lock = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("Another reviewed resubmission is being recorded; try again shortly") from exc
            yield directory
        finally:
            os.close(lock)


def _reconcile(slurm, intent):
    hours = min(87600, max(48, math.ceil(max(0, time.time() - intent["created"]) / 3600) + 24))
    start, end = f"now-{hours}hours", "now"
    argv = ["sacct", "-u", slurm.user, "-S", start, "-E", end, "-X", "-n", "-P",
            "-o", "JobID,Comment,User"]
    raw, _ = slurm.b.run(argv, slurm.timeout)
    if not isinstance(raw, str) or len(raw.encode()) > 1 << 20:
        raise ValueError("Resubmit reconciliation exceeded its accounting bound")
    found = set()
    for row in raw.splitlines():
        values = row.rstrip("|").split("|")
        if len(values) != 3:
            continue
        jid, comment, user = values
        if user == slurm.user and comment.split(" ", 1)[0] == intent["marker"] and re.fullmatch(r"[1-9][0-9]{0,19}(?:_[0-9]{1,10})?", jid):
            found.add(jid.partition("_")[0])
    if len(found) > 1:
        raise ValueError("More than one scheduler job has this resubmit marker; inspect the jobs before proceeding")
    return next(iter(found)) if found else None


def submit(slurm, store, clone):
    if not isinstance(clone.intent_token, str) or not re.fullmatch(r"[0-9a-f]{32}", clone.intent_token):
        raise ValueError("Reviewed resubmit token is invalid")
    root = Path(store.state_dir) if store.state_dir else Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "raven"
    path = root / "resubmissions"
    # Remove only our fresh marker when deciding whether an older identical
    # review has an unresolved submission. Account and other resources remain.
    canonical = apply_overrides(clone.argv, {"comment": ""})
    scope = {"source_job": clone.id, "argv": canonical, "workdir": clone.workdir,
             "user": slurm.user, "host": str(getattr(slurm.b, "host", "local")),
             "source_scope": str(getattr(store, "_recording_scope", ""))}
    name = hashlib.sha256(_encoded(scope)).hexdigest() + ".json"
    token_name = "token-" + clone.intent_token + ".json"
    marker = "raven-resubmit-" + clone.intent_token
    with _locked(path) as directory:
        own = _read(directory, token_name)
        if own and own.get("state") in {"accepted", "rejected"}:
            return own["state"] == "accepted", own.get("output", "Reviewed submission was already attempted"), own.get("job_id")
        previous = own if own and own.get("state") in {"submitting", "unknown"} else _read(directory, name)
        if previous and (previous.get("state") in {"submitting", "unknown"} or previous.get("token") == clone.intent_token):
            if previous.get("state") == "accepted":
                return True, "The reviewed submission was already accepted; no command repeated", previous["job_id"]
            try:
                recovered = _reconcile(slurm, previous)
            except Exception as exc:
                return False, f"Resubmit outcome unknown; reconciliation unavailable: {exc}. Receipt: {path / name}; do not retry submission", None
            if recovered:
                previous.update(state="accepted", job_id=recovered)
                _write(directory, name, previous)
                _write(directory, "token-" + previous["token"] + ".json", previous)
                _write(directory, token_name, dict(previous, token=clone.intent_token))
                return True, "Recovered the prior accepted submission; no command repeated", recovered
            return False, f"Resubmit outcome unknown; no exact accounting receipt yet. Receipt: {path / name}; marker {previous['marker']}. Do not resubmit until reconciled", None
        intent = {"schema": "raven.resubmit-intent/v1", "token": clone.intent_token, "marker": marker,
                  "created": time.time(), "state": "submitting", "scope": scope, "argv": clone.argv}
        _write(directory, name, intent)
        _write(directory, token_name, intent)
    try:
        ok, output, jid = slurm.submit(clone.argv, clone.workdir)
        jid = parse_submitted(output) if ok and isinstance(output, str) else None
        uncertain = ok and not jid or not ok and bool(re.search(r"unknown|timed?\s*out|timeout|connection (?:reset|lost)", output, re.I))
    except Exception as exc:
        ok, output, jid, uncertain = False, str(exc), None, True
    intent.update(state="unknown" if uncertain else "accepted" if ok else "rejected", output=output[:8192], job_id=jid)
    try:
        with _locked(path) as directory:
            _write(directory, name, intent)
            _write(directory, token_name, intent)
    except Exception as exc:
        return False, f"Resubmit outcome unknown while saving receipt: {exc}. Inspect marker {marker}; do not retry submission", None
    return bool(ok and jid), (f"Resubmit outcome unknown; receipt {path / name}; do not retry submission. " + output if uncertain else output), jid
