"""Bounded, versioned UI preferences with atomic publication and recovery.

Preferences remain at the document root for older Tower readers. The reserved
metadata key detects accidental corruption; it does not authenticate a writer.
Loading is read-only; a failed main file can use the previous valid publication.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile

META = "_tower_ui"
VERSION = 1
MAX_BYTES = 8 << 20


class StateError(ValueError):
    pass


class FutureVersion(StateError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def decode(raw):
    if len(raw) > MAX_BYTES:
        raise StateError("UI preferences exceed the 8 MiB read limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise StateError("UI preferences must be an object")
    has_metadata = META in value
    meta = value.pop(META, None)
    if has_metadata:
        if not isinstance(meta, dict) or type(meta.get("version")) is not int:
            raise StateError("UI preference metadata is invalid")
        if meta["version"] > VERSION:
            raise FutureVersion("UI preferences were written by a newer Tower; preserved without changes")
        if meta["version"] != VERSION:
            raise StateError("UI preference version is unsupported")
        if meta.get("sha256") != hashlib.sha256(_canonical(value)).hexdigest():
            raise StateError("UI preference checksum does not match")
    return value


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise StateError("UI preferences must be a regular file")
        raw = stream.read(MAX_BYTES + 1)
    return decode(raw), raw


def read(path):
    errors = []
    for candidate, recovered in ((path, False), (path + ".bak", True)):
        try:
            value, _ = _read(candidate)
            return value, {"state": "recovered" if recovered else "ready",
                           "message": "; ".join(errors) if recovered else ""}
        except FutureVersion as exc:
            # Never mask an intentionally newer primary state with an old one.
            return {}, {"state": "newer", "message": str(exc)}
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            if not isinstance(exc, FileNotFoundError):
                errors.append(str(exc)[:240])
    return {}, {"state": "unavailable" if errors else "missing", "message": "; ".join(errors)}


def _atomic(path, raw):
    folder = os.path.dirname(path) or "."
    fd, temporary = tempfile.mkstemp(prefix=".tower-ui-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write(path, value):
    """Return a status instead of disrupting the UI on a storage failure."""
    try:
        if not isinstance(value, dict) or META in value:
            raise StateError("UI preferences must be an object without reserved metadata")
        payload = _canonical(value)
        document = dict(value, **{META: {"version": VERSION, "sha256": hashlib.sha256(payload).hexdigest()}})
        raw = _canonical(document)
        if len(raw) > MAX_BYTES:
            raise StateError("UI preferences exceed the 8 MiB write limit")
        try:
            _, previous = _read(path)
        except FutureVersion:
            raise
        except (OSError, ValueError, TypeError, RecursionError):
            previous = None
        if previous is not None:
            _atomic(path + ".bak", previous)
        _atomic(path, raw)
        return {"state": "ready", "message": ""}
    except FutureVersion as exc:
        return {"state": "newer", "message": str(exc)}
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        return {"state": "error", "message": str(exc)[:480]}
