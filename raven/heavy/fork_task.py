"""Fresh task processes forked from the single-threaded, preloaded worker.

Only Raven's fixed operation modules are inherited. Each child has a new
session, private pipes and task rlimits; no task state is returned to the owner.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
import ctypes  # Preload the child ownership syscall adapter before forking.

_LIBC = ctypes.CDLL(None)


class TaskProcess:
    def __init__(self, environment, target):
        incoming, write = os.pipe()
        read, outgoing = os.pipe()
        errors, error_write = os.pipe()
        # Closing a million *possible* descriptors defeats the fork-server
        # gain on runtimes without close_range. This owner is single-threaded,
        # so a kernel fd snapshot is stable until fork and includes every pipe.
        try:
            inherited = tuple(
                int(name) for name in os.listdir("/proc/self/fd") if name.isdecimal()
            )
        except OSError:
            inherited = None
        parent = os.getpid()
        try:
            pid = os.fork()
        except BaseException:
            for fd in (incoming, write, read, outgoing, errors, error_write):
                os.close(fd)
            raise
        if pid == 0:
            try:
                os.setsid()
                for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                    signal.signal(sig, signal.SIG_DFL)
                os.dup2(incoming, 0)
                os.dup2(outgoing, 1)
                os.dup2(error_write, 2)
                if inherited is None:
                    os.closerange(3, 65536)
                else:
                    for fd in inherited:
                        if fd > 2:
                            try:
                                os.close(fd)
                            except OSError:
                                pass
                os.environ.clear()
                os.environ.update(environment)
                code = target(parent)
            except BaseException:
                code = 2
            os._exit(code)
        self.pid, self.returncode = pid, None
        for fd in (incoming, outgoing, error_write):
            os.close(fd)
        self.stdin, self.stdout, self.stderr = (
            os.fdopen(write, "wb", buffering=0),
            os.fdopen(read, "rb", buffering=0),
            os.fdopen(errors, "rb", buffering=0),
        )

    def poll(self):
        if self.returncode is None:
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                return self.returncode
            if pid:
                self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("Raven task", timeout)
            time.sleep(0.005)
        return self.returncode
