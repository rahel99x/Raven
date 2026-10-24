"""Bounds and private-file primitives for the rebuildable trace index."""

import json
import os
import sqlite3
import stat
import time
from pathlib import Path

SCHEMA = "raven.trace-workspace/v1"
INDEX_SCHEMA = "raven.trace-workspace-index/v1"
MAX_SOURCES = 512
MAX_RESULTS = 10_000


def common_identity(identity):
    """Retain exact attempt identity without duplicating its run-ID alias.

    Original source identities remain untouched. A distinct optional run ID
    is still an additional constraint; absent values are never fabricated.
    """
    binding = {key: identity[key] for key in ("job_id", "attempt", "cluster")}
    if identity.get("run_id") and identity["run_id"] != identity["attempt"]:
        binding["run_id"] = identity["run_id"]
    return binding


def _json(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _time(value):
    if type(value) is not int or not 0 <= value < 1 << 64:
        raise ValueError(
            "Trace workspace time must be an unsigned 64-bit nanosecond integer"
        )
    return f"{value:020d}"


def _text(value, maximum=8192, default=""):
    from ..job_logs.text import display_text

    return display_text(value[:maximum]) if isinstance(value, str) else default


def _limit(value, maximum=MAX_RESULTS):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"Trace workspace result limit must be 1–{maximum}")
    return value


def _private_path(value):
    path = Path(value).absolute()
    if len(str(path)) > 4096:
        raise ValueError("Trace index path exceeds its bound")
    for parent in reversed(path.parents):
        if parent.exists() and not stat.S_ISDIR(parent.lstat().st_mode):
            raise ValueError("Trace index paths must not traverse symlinks")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise ValueError(
            "Trace index needs a private, user-owned directory (mode 0700)"
        )
    created = False
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise ValueError(
                "Trace index must be a private user-owned regular file"
            ) from None
    else:
        os.close(descriptor)
        created = True
    return path, created


class _IntegerSum:
    def __init__(self):
        self.total = 0

    def step(self, value):
        if value is not None:
            self.total += int(value)

    def finalize(self):
        return str(self.total)


def _existing_schema(path):
    """Reject an unrelated database without applying any writer PRAGMA."""
    deadline = time.monotonic() + 1
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            db.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
            owner = db.execute(
                "SELECT type FROM sqlite_master WHERE name='metadata'"
            ).fetchone()
            if owner != ("table",) or db.execute(
                "SELECT value FROM metadata WHERE key='schema'"
            ).fetchone() != (INDEX_SCHEMA,):
                raise ValueError(
                    "Unsupported trace workspace index; choose a new rebuildable cache"
                )
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        raise ValueError(
            "Unsupported trace workspace index; choose a new rebuildable cache"
        ) from exc
