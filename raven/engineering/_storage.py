"""Private, anchored project state shared by engineering workflows.

Reads do not create directories. Explicit writes take a bounded advisory lock,
replace a regular file atomically, and sync its data and containing directory.
The project may be shared; the engineering namespace is owned by this user.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
import os
import re
import stat
import time
import uuid

from .. import projects

MAX_JSON = 2 << 20
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\Z")


def name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError("State names must be bounded ASCII identifiers")
    return value


def encoded(document, limit=MAX_JSON):
    pending, count = [(document, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 24 or count > 100000:
            raise ValueError("State JSON exceeds nesting or item limits")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("State JSON keys must be strings")
            pending.extend((key, depth + 1) for key in item)
            pending.extend((value, depth + 1) for value in item.values())
        elif isinstance(item, list):
            pending.extend((value, depth + 1) for value in item)
        elif isinstance(item, str):
            if len(item) > limit:
                raise ValueError("State JSON string exceeds its byte budget")
        elif type(item) is int and item.bit_length() > 256:
            raise ValueError("State integer exceeds the bounded reader profile")
        elif item is not None and type(item) not in (bool, int, float):
            raise ValueError("State contains a non-JSON value")
        elif type(item) is float and not math.isfinite(item):
            raise ValueError("State numbers must be finite")
    try:
        raw = json.dumps(document, sort_keys=True, ensure_ascii=True, allow_nan=False,
                         separators=(",", ":")).encode("ascii") + b"\n"
    except (ValueError, OverflowError) as exc:
        raise ValueError("State contains invalid JSON") from exc
    if len(raw) > limit:
        raise ValueError(f"State JSON exceeds its {limit}-byte budget")
    return raw


def _private(info, directory=False):
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Engineering state must be user-owned and private (directories 700, files 600)")


def _child(parent, component, *, create=False, private=True):
    if create:
        try:
            os.mkdir(component, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if private:
            _private(info, directory=True)
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def project_store(project, namespace, *, create=False):
    """Yield an open namespace directory; a missing read leaves no state behind."""
    name(namespace)
    _, root = projects._root(str(project))
    root_info = os.fstat(root)
    opened = [root]
    links = []
    try:
        for component, private in ((".raven", False), ("engineering", True), (namespace, True)):
            parent = opened[-1]
            fd = _child(parent, component, create=create, private=private)
            opened.append(fd)
            links.append((parent, component, fd))
        yield opened[-1]
        # Detect a replaced state directory before treating a completed write
        # as published. No path outside the pinned directory was followed.
        for parent, component, fd in links:
            actual = os.stat(component, dir_fd=parent, follow_symlinks=False)
            original = os.fstat(fd)
            if (actual.st_dev, actual.st_ino) != (original.st_dev, original.st_ino):
                raise ValueError("Project state directory changed; reopen and retry")
        _, current = projects._root(str(project))
        try:
            actual = os.fstat(current)
            if (actual.st_dev, actual.st_ino) != (root_info.st_dev, root_info.st_ino):
                raise ValueError("Project root changed; reopen and retry")
        finally:
            os.close(current)
    finally:
        for fd in reversed(opened):
            os.close(fd)


def read(fd, filename, limit=MAX_JSON):
    name(filename)
    out = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        before = os.fstat(out)
        _private(before)
        if before.st_size > limit:
            raise ValueError("State file exceeds its byte budget")
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(out, min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > limit:
            raise ValueError("State file grew beyond its byte budget")
        from ..science_files import signature

        if (signature(before) != signature(os.fstat(out))
                or signature(before) != signature(os.stat(filename, dir_fd=fd, follow_symlinks=False))):
            raise ValueError("State changed during inspection; refresh to retry")
        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError("Duplicate state JSON key")
                value[key] = item
            return value
        try:
            document = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=pairs,
                                  parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite state number")))
            encoded(document, limit)
            return document
        except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
            raise ValueError("State must be bounded UTF-8 JSON") from exc
    finally:
        os.close(out)


def write(fd, filename, document, *, exclusive=False, limit=MAX_JSON):
    """Caller holds locked(); optional exclusive publication never overwrites."""
    name(filename)
    data = encoded(document, limit)
    try:
        _private(os.stat(filename, dir_fd=fd, follow_symlinks=False))
    except FileNotFoundError:
        pass
    else:
        if exclusive:
            raise ValueError("State already exists; choose another name")
    temporary = "pending-" + uuid.uuid4().hex + ".json"
    created = False
    try:
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      0o600, dir_fd=fd)
        created = True
        with os.fdopen(out, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # The private namespace is protected by the advisory lock. link()
        # additionally provides no-clobber publication for immutable records.
        if exclusive:
            os.link(temporary, filename, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            os.unlink(temporary, dir_fd=fd)
        else:
            try:
                _private(os.stat(filename, dir_fd=fd, follow_symlinks=False))
            except FileNotFoundError:
                pass
            os.replace(temporary, filename, src_dir_fd=fd, dst_dir_fd=fd)
        created = False
        os.fsync(fd)
        return json.loads(data)
    finally:
        if created:
            os.unlink(temporary, dir_fd=fd)


@contextmanager
def locked(project, namespace, *, timeout=0.25):
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("State lock timeout must be between 0 and 60 seconds")
    with project_store(project, namespace, create=True) as folder:
        fd = os.open("state.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=folder)
        acquired = False
        try:
            _private(os.fstat(fd))
            until = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    remaining = until - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Engineering state is busy; retry later") from None
                    time.sleep(min(0.01, remaining))
            yield folder
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
