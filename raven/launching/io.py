"""Anchored launch writes and reporting locks, independent of orchestration."""

from __future__ import annotations
from contextlib import contextmanager
import fcntl
import os
import stat
import time
from .. import reporting as R


@contextmanager
def _lock(root, name="report.lock"):
    """Never wait indefinitely for another reporting coordinator."""
    root = R._directory(root)
    folder = R._under(root, ".raven/.unused", create_parent=True).parent
    fd = os.open(
        folder / name, os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("report lock must be a regular file")
        until = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise ValueError(
                        "another reporter is busy; retry the report command"
                    )
                time.sleep(0.02)
        yield root
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _exclusive(path, data, mode=0o400):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), mode)
