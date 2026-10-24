"""Four local disposable index slots; never prune raw recording artifacts.

Stable lock inodes avoid unlink/recreate races. The directory guard serializes
slot assignment while each index retains its own lifetime ownership lock.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import socket
import stat
import tempfile

SLOTS = 4


def _safe_file(path):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise ValueError("Disposable trace cache contains an unsafe file")


def _remove_database(path):
    for name in (path, Path(str(path) + "-journal")):
        try:
            _safe_file(name)
        except FileNotFoundError:
            continue
        name.unlink()


def open_index(
    identity, paths, *, max_bytes, reader_memory_bytes=64 << 20, directory=None
):
    import fcntl
    from ..installation.config_migration import private_directory
    from .protocol import encoded
    from .workspace_index import TraceIndex
    from .workspace_index_storage import _private_path

    root = private_directory(
        Path(directory)
        if directory
        else Path(tempfile.gettempdir()) / ("raven-trace-indexes-" + str(os.getuid()))
    )
    digest = hashlib.sha256(
        encoded(
            {
                "identity": identity,
                "paths": sorted(map(str, paths)),
                "host": socket.gethostname(),
            }
        )
    ).hexdigest()
    guard, _ = _private_path(root / "slots.lock")
    descriptor = os.open(guard, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        choices = []
        for number in range(SLOTS):
            slot = private_directory(root / ("slot-" + str(number)))
            path, key = slot / "index.sqlite", slot / "key"
            previous = ""
            if key.exists():
                _safe_file(key)
                if key.stat().st_size <= 64:
                    previous = key.read_text(encoding="ascii", errors="replace")
            touched = path.stat().st_mtime_ns if path.exists() else 0
            choices.append((previous != digest, touched, path, key, previous))
        for different, _, path, key, previous in sorted(choices):
            anchor, _ = _private_path(str(path) + ".lock")
            lease = os.open(anchor, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            try:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                if different:
                    _remove_database(path)
            finally:
                os.close(lease)
            # The guard prevents another automatic owner from claiming this
            # slot between the availability check and Index's lifetime lock.
            rebuilt = False
            try:
                index = TraceIndex(
                    path, max_bytes=max_bytes, reader_memory_bytes=reader_memory_bytes
                )
            except ValueError as exc:
                if not any(
                    reason in str(exc)
                    for reason in (
                        "Unsupported trace workspace index",
                        "Existing trace index exceeds its database budget",
                    )
                ):
                    raise
                # Recheck ownership after a failed constructor released its
                # lease, before removing only our disposable materialization.
                lease = os.open(anchor, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    _remove_database(path)
                finally:
                    os.close(lease)
                index = TraceIndex(
                    path, max_bytes=max_bytes, reader_memory_bytes=reader_memory_bytes
                )
                rebuilt = True
            try:
                key_path, _ = _private_path(key)
                fd = os.open(
                    key_path, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    os.write(fd, digest.encode("ascii"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except BaseException:
                index.close()
                raise
            index.cache_rebuilt = rebuilt
            return index
        raise ValueError(
            "All four local trace index slots are active; close an unused viewer or choose an explicit private --index"
        )
    finally:
        os.close(descriptor)
