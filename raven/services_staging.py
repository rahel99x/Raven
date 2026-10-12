"""Declared, streamed and verified local-mount transfers with durable receipts."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import time
import secrets

from .services_io import private_directory, write_json_file

SCHEMA = "raven.staging/v1"
CHUNK = 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _fingerprint(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def regular_source(path, *, dir_fd=None):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise ValueError("Transfer source must be a regular file")
    return fd, info


def validate(document):
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ValueError(f"Expected schema {SCHEMA}")
    if set(document) - {"schema", "id", "entries", "retention"}:
        raise ValueError("Unknown staging manifest field")
    identity = document.get("id")
    if not isinstance(identity, str) or not identity or len(identity) > 128 or not identity.isprintable():
        raise ValueError("Manifest id must be a printable string (1–128 characters)")
    if document.get("retention", "keep-source") != "keep-source":
        raise ValueError("Only keep-source retention is supported; no transfer deletes a source")
    entries = document.get("entries")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 1000:
        raise ValueError("Declare between 1 and 1000 transfer entries")
    result, ids, destinations = [], set(), set()
    for item in entries:
        if not isinstance(item, dict) or set(item) - {"id", "source", "destination", "sha256", "size", "direction"}:
            raise ValueError("Invalid transfer entry fields")
        entry = dict(item)
        eid = entry.get("id")
        if not isinstance(eid, str) or not eid or not eid.isprintable() or len(eid) > 128 or eid in ids:
            raise ValueError("Transfer entry ids must be unique printable strings")
        ids.add(eid)
        for name in ("source", "destination"):
            value = entry.get(name)
            if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value or not Path(value).is_absolute():
                raise ValueError("Transfer paths must be explicit absolute filesystem paths")
            if name == "source":
                # Resolve parents, not the final component: source symlinks are refused.
                entry[name] = str(Path(value).parent.resolve() / Path(value).name)
            else:
                entry[name] = str(Path(value).parent.resolve() / Path(value).name)
        if entry["source"] == entry["destination"]:
            raise ValueError("Transfer source and destination must differ")
        if entry["destination"] in destinations:
            raise ValueError("Transfer destinations must be unique")
        destinations.add(entry["destination"])
        if not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[a-fA-F0-9]{64}", entry["sha256"]):
            raise ValueError("Each entry needs an expected SHA-256 checksum")
        entry["sha256"] = entry["sha256"].lower()
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= 2**63 - 1:
            raise ValueError("Each entry needs a nonnegative byte size")
        if entry.get("direction") not in ("input", "output"):
            raise ValueError("Direction must be input or output")
        result.append(entry)
    # Refuse intra-manifest chains whose outcome would depend on transfer order.
    if destinations.intersection(entry["source"] for entry in result):
        raise ValueError("A transfer destination cannot be another entry's source")
    return {"schema": SCHEMA, "id": identity, "entries": result, "retention": "keep-source"}


def inspect(document, direction="all"):
    if direction not in ("all", "input", "output"):
        raise ValueError("Unknown transfer direction")
    manifest = validate(document)
    entries = []
    for entry in manifest["entries"]:
        if direction != "all" and entry["direction"] != direction:
            continue
        fd, info = regular_source(entry["source"])
        os.close(fd)
        if info.st_size != entry["size"]:
            raise ValueError(f"{entry['id']}: source size differs from the manifest")
        destination = Path(entry["destination"])
        if not destination.parent.is_dir():
            raise ValueError(f"{entry['id']}: destination parent directory does not exist")
        parent_info = destination.parent.stat()
        entries.append({**entry, "source_identity": _fingerprint(info),
                        "destination_parent_identity": [parent_info.st_dev, parent_info.st_ino]})
    if not entries:
        raise ValueError("No entries match this direction")
    return {"manifest": manifest, "revision": digest(manifest), "entries": entries,
            "direction": direction, "bytes": sum(entry["size"] for entry in entries)}


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Transfer cancelled; completed entries remain recorded")


def _hash_file(path, expected_size, cancel, *, dir_fd=None):
    fd, info = regular_source(path, dir_fd=dir_fd)
    try:
        if info.st_size != expected_size:
            raise ValueError("Existing destination size differs")
        hasher, total = hashlib.sha256(), 0
        while True:
            _check_cancel(cancel)
            data = os.read(fd, min(CHUNK, expected_size - total + 1))
            if not data:
                break
            total += len(data)
            if total > expected_size:
                raise ValueError("Destination grew during verification")
            hasher.update(data)
        named = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        if (total != expected_size or _fingerprint(info) != _fingerprint(os.fstat(fd))
                or _fingerprint(info) != _fingerprint(named)):
            raise ValueError("Destination changed during verification")
        return hasher.hexdigest(), info
    finally:
        os.close(fd)


def transfer(entry, *, cancel=None):
    """Publish only verified complete bytes. Repetition verifies existing output."""
    _check_cancel(cancel)
    destination = Path(entry["destination"])
    parent_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    parent = os.fstat(parent_fd)
    if [parent.st_dev, parent.st_ino] != entry["destination_parent_identity"]:
        os.close(parent_fd)
        raise ValueError("Destination directory changed after review")
    fd = None
    temporary = None
    try:
        fd, source_info = regular_source(entry["source"])
        if _fingerprint(source_info) != entry["source_identity"]:
            raise ValueError("Source changed after review")
        try:
            os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
            exists = True
        except FileNotFoundError:
            exists = False
        if exists:
            checksum, existing_info = _hash_file(destination.name, entry["size"], cancel, dir_fd=parent_fd)
            if checksum != entry["sha256"]:
                raise FileExistsError("Destination exists with different contents; no file was overwritten")
            if (source_info.st_dev, source_info.st_ino) == (existing_info.st_dev, existing_info.st_ino):
                raise ValueError("Source and destination are the same file")
            current_parent = destination.parent.stat()
            if (current_parent.st_dev, current_parent.st_ino) != (parent.st_dev, parent.st_ino):
                raise ValueError("Destination directory moved during verification")
            return "verified-existing"
        temporary = ".raven-transfer-" + secrets.token_hex(16)
        temp_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
        hasher, total = hashlib.sha256(), 0
        with os.fdopen(temp_fd, "wb") as output:
            while True:
                _check_cancel(cancel)
                data = os.read(fd, min(CHUNK, entry["size"] - total + 1))
                if not data:
                    break
                total += len(data)
                if total > entry["size"]:
                    raise ValueError("Source grew during transfer")
                hasher.update(data)
                output.write(data)
            if total != entry["size"] or hasher.hexdigest() != entry["sha256"]:
                raise ValueError("Source checksum or size differs; no destination was published")
            if _fingerprint(os.fstat(fd)) != entry["source_identity"] or _fingerprint(os.stat(entry["source"], follow_symlinks=False)) != entry["source_identity"]:
                raise ValueError("Source changed during transfer")
            output.flush()
            os.fsync(output.fileno())
        _check_cancel(cancel)
        # Link is an atomic no-replace publication. Unlike replace(), it cannot
        # destroy a file created by another process after the review.
        current_parent = destination.parent.stat()
        if (current_parent.st_dev, current_parent.st_ino) != (parent.st_dev, parent.st_ino):
            raise ValueError("Destination directory moved during transfer")
        os.link(temporary, destination.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        os.fsync(parent_fd)
        return "copied"
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def execute(prepared, receipt_directory, *, cancel=None):
    """Each entry is independent; retain completed receipts if later work fails."""
    manifest = validate(prepared["manifest"])
    if digest(manifest) != prepared["revision"]:
        raise ValueError("Transfer plan revision does not match its manifest")
    directory = private_directory(receipt_directory)
    lock = os.open(directory / (prepared["revision"] + ".lock"), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Invalid transfer receipt lock")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("This transfer revision is already running") from exc
        return _execute_locked(prepared, manifest, directory, cancel)
    finally:
        os.close(lock)


def _execute_locked(prepared, manifest, directory, cancel):
    results = []
    for index, entry in enumerate(prepared["entries"]):
        try:
            outcome = transfer(entry, cancel=cancel)
            result = {"id": entry["id"], "status": outcome, "source": entry["source"],
                      "destination": entry["destination"], "sha256": entry["sha256"], "size": entry["size"]}
        except InterruptedError:
            result = {"id": entry["id"], "status": "cancelled"}
        except (OSError, ValueError) as exc:
            result = {"id": entry["id"], "status": "failed", "reason": str(exc)[:800]}
        results.append(result)
        if result["status"] == "cancelled":
            results.extend({"id": remaining["id"], "status": "not-attempted"}
                           for remaining in prepared["entries"][index + 1:])
        receipt = {"schema": "raven.staging-receipt/v1", "manifest_id": manifest["id"],
                   "revision": prepared["revision"], "time": time.time(), "entries": results}
        write_json_file(directory / (prepared["revision"] + ".json"), receipt)
        if result["status"] == "cancelled":
            break
    return results
