"""Stable local file snapshots and no-clobber publication for science operations."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat

METADATA_LIMIT = 4 * 1024 * 1024
DATA_LIMIT = 64 * 1024**3


def cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise ValueError("Operation cancelled")


def path_at(base, value):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise ValueError("A nonempty file path is required")
    path = Path(value).expanduser()
    return str((Path(base)/path).absolute()) if not path.is_absolute() else str(path)


def signature(value):
    """Keep native identity exact within the worker's JSON integer bounds."""
    parts = (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    return tuple(part if -(1 << 63) <= part < (1 << 63) else str(part) for part in parts)


def snapshot(path, *, cancel=None, limit=DATA_LIMIT, keep=False):
    """Open one regular file without following a final symlink, hash and verify stability."""
    cancelled(cancel)
    from .raven_execution import source_limit, source_read
    limit = source_limit(limit)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError(f"Expected a regular file of at most {limit} bytes: {path}")
        source_read(before.st_size)
        digest, chunks, size = hashlib.sha256(), [], 0
        while True:
            cancelled(cancel)
            chunk = os.read(fd, min(1024*1024, limit-size+1))
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ValueError(f"File grew past the size limit: {path}")
            digest.update(chunk)
            if keep:
                chunks.append(chunk)
        if signature(before) != signature(os.fstat(fd)) or signature(before) != signature(os.stat(path, follow_symlinks=False)):
            raise ValueError(f"File changed while reading: {path}")
        return {"path": str(path), "sha256": digest.hexdigest(), "size": size, "signature": list(signature(before))}, b"".join(chunks) if keep else None
    finally:
        os.close(fd)


def json_file(path, cancel=None):
    record, raw = snapshot(path, cancel=cancel, limit=METADATA_LIMIT, keep=True)
    def constant(value):
        raise ValueError(f"Invalid JSON numeric constant: {value}")
    def pairs(values):
        out = {}
        for key, value in values:
            if key in out:
                raise ValueError(f"Duplicate JSON key: {key}")
            out[key] = value
        return out
    from .planning_io import _bounded, _float, _integer
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=constant, object_pairs_hook=pairs,
                           parse_float=_float, parse_int=_integer)
        _bounded(value, 32)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Use bounded UTF-8 JSON without excessive nesting") from exc
    return value, record


def verify(record, cancel=None):
    actual, _ = snapshot(record["path"], cancel=cancel)
    if actual["sha256"] != record["sha256"] or actual["size"] != record["size"] or actual["signature"] != record["signature"]:
        raise ValueError(f"Reviewed source changed: {record['path']}")
    return actual


def destination(path):
    path = Path(path).expanduser().absolute()
    if not path.name or path.name in {".", ".."} or path.exists() or path.is_symlink():
        raise ValueError("Destination must be a new directory")
    if not path.parent.is_dir():
        raise ValueError("Destination parent directory must already exist")
    for parent in (path.parent, *path.parent.parents):
        if parent.is_symlink():
            raise ValueError("Destination parent cannot contain symlinks")
    return str(path)


def relative_target(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value:
        raise ValueError("Output target must be a relative file path")
    target = Path(value)
    if not target.parts or target.is_absolute() or any(part in {"..", "."} for part in target.parts) or value.endswith("/"):
        raise ValueError("Output target must stay within its destination directory")
    if target.parts[0].startswith(".tower-"):
        raise ValueError("Output target conflicts with Tower's completion metadata")
    return str(target)


def publish(path, *, files=(), copies=(), receipt=None, cancel=None, final_check=None):
    """Exclusively reserve a directory. A receipt is written last; interrupted copies cannot pass verification."""
    path = destination(path)
    cancelled(cancel)
    os.mkdir(path, 0o700)
    root = Path(path)
    # Only remove the directory we just created if a synchronous failure occurs.
    identity = (root.stat().st_dev, root.stat().st_ino)
    try:
        (root/".tower-incomplete").write_text("Operation incomplete. Do not reuse.\n", encoding="utf-8")
        for target, content, executable in files:
            cancelled(cancel)
            output = root/relative_target(target)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            output.chmod(0o700 if executable else 0o600)
        for record, target in copies:
            cancelled(cancel)
            output = root/relative_target(target)
            output.parent.mkdir(parents=True, exist_ok=True)
            source = os.open(record["path"], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            try:
                before = os.fstat(source)
                if not stat.S_ISREG(before.st_mode) or list(signature(before)) != record["signature"]:
                    raise ValueError(f"Reviewed source changed: {record['path']}")
                digest, size = hashlib.sha256(), 0
                with output.open("xb") as stream:
                    while True:
                        cancelled(cancel)
                        chunk = os.read(source, 1024*1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > record["size"]:
                            raise ValueError("Source grew during materialization")
                        digest.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                output.chmod(0o600)
                if digest.hexdigest() != record["sha256"] or size != record["size"] or list(signature(os.fstat(source))) != record["signature"] or list(signature(os.stat(record["path"], follow_symlinks=False))) != record["signature"]:
                    raise ValueError(f"Source changed during materialization: {record['path']}")
            finally:
                os.close(source)
        cancelled(cancel)
        if final_check is not None:
            final_check()
        with (root/".tower-receipt.json").open("x", encoding="utf-8") as stream:
            json.dump(receipt or {}, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        (root/".tower-incomplete").unlink()
        for directory in (root, root.parent):
            fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return path
    except BaseException:
        if root.exists() and not root.is_symlink() and (root.stat().st_dev, root.stat().st_ino) == identity:
            shutil.rmtree(root)
        raise
