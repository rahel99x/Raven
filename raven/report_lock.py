"""Small advisory reporting lock usable inside application startup hooks."""

import fcntl
import os
import stat
import time
from contextlib import contextmanager

from . import reporting as R


@contextmanager
def lock(root, name="report.lock", *, timeout=0.2):
    root = R._directory(root)
    folder = R._under(root, ".raven/.unused", create_parent=True).parent
    fd = os.open(folder / name, os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600)
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("report lock must be a regular file")
        until = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                remaining = until - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("another reporter is busy; retry later") from None
                time.sleep(min(0.01, remaining))
        yield root
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
