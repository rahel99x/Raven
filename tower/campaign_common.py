"""Bounded local artifacts and durable execution intents for campaign tools."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def integer(value, name, low=1, high=1000000):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")
    return value


def identifier(value, name="id"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise ValueError(f"{name} must use 1..80 letters, digits, dots, dashes, or underscores")
    return value


def text(value, name, limit=4096):
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} must be a nonempty bounded string without control characters")
    return value


def file_bytes(path, limit=4 << 20):
    from .raven_execution import source_limit, source_read
    limit = source_limit(limit)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("campaign input must be a regular file")
        source_read(before.st_size)
        data = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
        named = os.stat(path, follow_symlinks=False)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if signature(before) != signature(after) or signature(after) != signature(named):
            raise ValueError("campaign input changed during inspection")
    if len(data) > limit:
        raise ValueError("campaign input exceeds its size budget")
    return data


def file_hash(path, cancel=None, limit=1 << 30):
    from .raven_execution import source_limit, source_read
    limit = source_limit(limit)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("checkpoint must be a regular file within the 1 GiB verification budget")
        source_read(before.st_size)
        result, size = hashlib.sha256(), 0
        while True:
            if cancel is not None and cancel.is_set():
                raise ValueError("checkpoint verification cancelled")
            block = handle.read(1 << 20)
            if not block:
                break
            size += len(block)
            if size > limit:
                raise ValueError("checkpoint grew past the verification budget")
            result.update(block)
        after = os.fstat(handle.fileno())
        named = os.stat(path, follow_symlinks=False)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if signature(before) != signature(after) or signature(after) != signature(named):
            raise ValueError("checkpoint changed during verification")
        return result.hexdigest()


def read(path, default=None):
    try:
        from .planning_io import load_json
        return load_json(path, max_bytes=4 << 20)
    except FileNotFoundError:
        return default


def atomic(path, value, overwrite=True):
    path = Path(path)
    data = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    if len(data) > 4 << 20:
        raise ValueError("campaign state exceeds 4 MiB")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name("." + path.name + "." + os.urandom(8).hex())
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            if path.is_symlink():
                raise ValueError("campaign state cannot be a symbolic link")
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def artifact(path, data):
    """Idempotent immutable artifact creation; a changed artifact is never reused."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name("." + path.name + "." + os.urandom(8).hex())
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if file_bytes(path, max(4 << 20, len(data))) != data:
                raise ValueError("campaign artifact changed; use a new reviewed plan")
    finally:
        temporary.unlink(missing_ok=True)
    return str(path)


@contextmanager
def locked(path):
    import fcntl
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".lock", os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("campaign lock must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another process is updating this campaign") from exc
        yield
    finally:
        os.close(fd)


def directory(ctx, name):
    return Path(ctx.state_dir).expanduser().absolute() / "campaigns" / digest(ctx.scope)[:20] / identifier(name)
