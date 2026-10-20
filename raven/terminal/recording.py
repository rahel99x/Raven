"""Explicit, bounded output recordings. Reading a recording never executes it.

A recording contains PTY output, including anything a child echoes. It is not a
secret-safe audit log. Input events are deliberately not part of this format.
"""

from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path
import stat
import time

SCHEMA = "raven.terminal-recording/v1"
MAX_FILE = 16 << 20
MAX_EVENTS = 100000
MAX_EVENT = 65536
MAX_SECONDS = 86400.0


def _json(value):
    return (
        json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        + "\n"
    ).encode()


def private_file(path, *, create=False):
    """Open an owned regular file without following a final symlink."""
    flags = (
        os.O_CLOEXEC
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | (os.O_WRONLY | os.O_CREAT | os.O_EXCL if create else os.O_RDONLY)
    )
    fd = os.open(os.fspath(path), flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("The file must be a regular file owned by this user.")
        if info.st_mode & 0o077:
            raise ValueError("The file must be private (mode 0600).")
        return fd
    except BaseException:
        os.close(fd)
        raise


class Recorder:
    """One session's explicit output-only JSONL recording with hard budgets."""

    def __init__(
        self,
        path,
        session_id,
        *,
        cols=80,
        rows=24,
        max_bytes=MAX_FILE,
        max_events=MAX_EVENTS,
        max_seconds=MAX_SECONDS,
        clock=time.monotonic,
    ):
        if not 1024 <= max_bytes <= MAX_FILE or not 1 <= max_events <= MAX_EVENTS:
            raise ValueError("Invalid recording budget.")
        if not 0 < max_seconds <= MAX_SECONDS:
            raise ValueError("Invalid recording duration.")
        self.path = Path(path)
        self.fd = private_file(path, create=True)
        self.session_id = str(session_id)[:128]
        self.clock = clock
        self.started = clock()
        self.max_bytes, self.max_events, self.max_seconds = (
            max_bytes,
            max_events,
            max_seconds,
        )
        self.bytes = self.events = 0
        self.closed = False
        self.reason = ""
        try:
            self._write(
                {
                    "schema": SCHEMA,
                    "session": self.session_id,
                    "created": time.time(),
                    "cols": _dimension(cols),
                    "rows": _dimension(rows),
                    "input_recorded": False,
                    "warning": "Output can contain secrets echoed by the child.",
                }
            )
        except BaseException:
            self.close("initialization failed")
            raise

    def _write(self, value):
        data = _json(value)
        if self.bytes + len(data) > self.max_bytes:
            self.close("byte budget reached")
            return False
        view = memoryview(data)
        try:
            while view:
                count = os.write(self.fd, view)
                if count <= 0:
                    raise OSError("Recording write made no progress")
                view = view[count:]
        except OSError:
            self.close("recording write failed")
            raise
        self.bytes += len(data)
        return True

    def _event(self, kind, **payload):
        if self.closed:
            return False
        elapsed = self.clock() - self.started
        if not math.isfinite(elapsed) or elapsed < 0 or elapsed > self.max_seconds:
            self.close("time budget reached")
            return False
        if self.events >= self.max_events:
            self.close("event budget reached")
            return False
        if self._write({"event": kind, "t": elapsed, **payload}):
            self.events += 1
            return True
        return False

    def output(self, data):
        if not isinstance(data, bytes):
            raise TypeError("Recorded output must be bytes.")
        for offset in range(0, len(data), MAX_EVENT):
            if not self._event(
                "output",
                data=base64.b64encode(data[offset : offset + MAX_EVENT]).decode(
                    "ascii"
                ),
            ):
                return False
        return not self.closed

    def resize(self, cols, rows):
        return self._event("resize", cols=_dimension(cols), rows=_dimension(rows))

    def close(self, reason="stopped by user"):
        if not self.closed:
            self.closed = True
            self.reason = str(reason)
            os.close(self.fd)
            self.fd = -1

    def status(self):
        return {
            "path": str(self.path),
            "bytes": self.bytes,
            "events": self.events,
            "closed": self.closed,
            "reason": self.reason,
            "input_recorded": False,
        }


def _dimension(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4096:
        raise ValueError("Invalid recorded terminal size.")
    return value


def read_recording(path, *, max_bytes=MAX_FILE):
    """Validate all records before returning inert data; never invoke a shell."""
    fd = private_file(path)
    try:
        if os.fstat(fd).st_size > max_bytes:
            raise ValueError("Recording exceeds the byte budget.")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            raw = stream.read(max_bytes + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > max_bytes:
        raise ValueError("Recording exceeds the byte budget.")
    lines = raw.splitlines()
    if not lines or len(lines) > MAX_EVENTS + 1:
        raise ValueError("Invalid recording event count.")
    try:
        header = json.loads(lines[0])
        if (
            not isinstance(header, dict)
            or header.get("schema") != SCHEMA
            or header.get("input_recorded") is not False
        ):
            raise ValueError("Unsupported terminal recording.")
        _dimension(header.get("cols"))
        _dimension(header.get("rows"))
        result, last = [], 0.0
        for line in lines[1:]:
            if len(line) > MAX_EVENT * 2:
                raise ValueError("Recording event exceeds its budget.")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("Invalid recording event.")
            stamp = value.get("t")
            if (
                isinstance(stamp, bool)
                or not isinstance(stamp, (float, int))
                or not last <= stamp <= MAX_SECONDS
                or not math.isfinite(stamp)
            ):
                raise ValueError("Recording times must be finite and ordered.")
            last = stamp
            kind = value.get("event")
            if kind == "output":
                data = base64.b64decode(value.get("data", ""), validate=True)
                if len(data) > MAX_EVENT:
                    raise ValueError("Recording output chunk exceeds its budget.")
                result.append({"event": kind, "t": stamp, "data": data})
            elif kind == "resize":
                result.append(
                    {
                        "event": kind,
                        "t": stamp,
                        "cols": _dimension(value.get("cols")),
                        "rows": _dimension(value.get("rows")),
                    }
                )
            else:
                raise ValueError(
                    "Recordings may contain only output and resize events."
                )
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("Invalid terminal recording.") from exc
    return header, tuple(result)


def replay(path, renderer):
    """Feed validated output into an emulator, with no process or transport."""
    header, events = read_recording(path)
    renderer.resize(header["cols"], header["rows"])
    for event in events:
        if event["event"] == "output":
            renderer.feed(event["data"])
        else:
            renderer.resize(event["cols"], event["rows"])
    return {
        "events": len(events),
        "duration": events[-1]["t"] if events else 0,
        "execution": "none",
        "session": header.get("session", ""),
    }
