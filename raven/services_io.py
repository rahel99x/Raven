"""Bounded POSIX service I/O; no scheduler writes and no shell evaluation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import tempfile
import time


def private_directory(path):
    """Create one private service directory; refuse symlink/foreign owners."""
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ValueError("Service directory must not be a symbolic link")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Service directory must be owned by the current user")
    if info.st_mode & 0o077:
        raise ValueError("Service directory must have mode 0700")
    return path


def read_json_file(path, limit=4 * 1024 * 1024):
    from .planning_io import load_json
    return load_json(path, max_bytes=limit)


def write_json_file(path, value):
    """Atomically replace a service-owned record, never follow its old target."""
    path = Path(path)
    encoded = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
    if len(encoded) > 8 * 1024 * 1024:
        raise ValueError("Service record exceeds 8 MiB")
    fd, temporary = tempfile.mkstemp(prefix=".raven-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def bounded_command(argv, *, timeout=8, limit=1024 * 1024, cancel=None, env=None):
    """Drain both pipes within one byte/time budget and always reap the child."""
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Operation cancelled")
    process = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, start_new_session=True)
    output, errors = bytearray(), bytearray()
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ, output)
        selector.register(process.stderr, selectors.EVENT_READ, errors)
        while selector.get_map():
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Operation cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{argv[0]} exceeded {timeout:g} seconds")
            for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                else:
                    key.data.extend(data)
                    if len(output) + len(errors) > limit:
                        raise ValueError(f"{argv[0]} output exceeds {limit} bytes")
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode:
            reason = bytes(errors or output).decode("utf-8", "replace")[:600].strip()
            raise RuntimeError(f"{argv[0]} exit {process.returncode}: {reason}")
        return bytes(output).decode("utf-8", "replace")
    finally:
        selector.close()
        # A leader can exit while its descendants still own our pipes.
        import signal
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
        process.stderr.close()
