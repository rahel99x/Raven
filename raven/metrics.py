"""Bounded, incremental application telemetry with an optional JSONL reporter.

Only explicitly reported numeric metrics and progress are interpreted. Reading a
stream uses the same byte-offset file interface as local and SSH log following;
it never runs an application or polls the scheduler.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
import errno
import json
import math
import os
import stat
import threading
import time
from typing import Mapping, Optional

from .remote import LocalFiles


MAX_METRICS = 64
MAX_LINE_BYTES = 65536
MAX_ERRORS = 32
MAX_LINES_PER_READ = 8192
MAX_READ_BYTES = 64 << 20
MAX_POINTS = 10000
MAX_STREAMS = 128


def _number(value, name: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ValueError(f"{name} must be a finite {'nonnegative ' if nonnegative else ''}number")
    return result


def _text(value, name: str, limit: int, *, empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value):
        raise ValueError(f"{name} must be a string of {'0' if empty else '1'} to {limit} characters")
    if value and not value.isprintable():
        raise ValueError(f"{name} must contain only printable characters")
    return value


def _record(value: object) -> dict:
    """Validate atomically: malformed records never contribute partial metrics."""
    if not isinstance(value, dict):
        raise ValueError("record must be a JSON object")
    rec = {"t": _number(value.get("t"), "t", nonnegative=True)}
    metrics = value.get("metrics", {})
    if not isinstance(metrics, dict) or len(metrics) > MAX_METRICS:
        raise ValueError(f"metrics must be an object with at most {MAX_METRICS} entries")
    rec["metrics"] = {
        _text(key, "metric name", 96, empty=False): _number(number, "metric value")
        for key, number in metrics.items()
    }
    if "step" in value:
        step = value["step"]
        if isinstance(step, bool) or not isinstance(step, int) or step < 0 or step > (1 << 63) - 1:
            raise ValueError("step must be a nonnegative 64-bit integer")
        rec["step"] = step
    if "phase" in value:
        rec["phase"] = _text(value["phase"], "phase", 160)
    if "progress" in value:
        progress = value["progress"]
        if not isinstance(progress, dict):
            raise ValueError("progress must be an object")
        completed = _number(progress.get("completed"), "progress.completed", nonnegative=True)
        total = _number(progress.get("total"), "progress.total", nonnegative=True)
        if total == 0 or completed > total:
            raise ValueError("progress must satisfy 0 <= completed <= total and total > 0")
        rec["progress"] = {
            "completed": completed, "total": total,
            "unit": _text(progress.get("unit", ""), "progress.unit", 64),
            "fraction": completed / total,
        }
    return rec


def _unique_object(pairs):
    """Ambiguous duplicate JSON keys are malformed rather than silently replaced."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


@dataclass
class _Stream:
    ident: object = None
    offset: int = 0
    partial: bytes = b""
    discard_line: bool = False
    truncated: bool = False
    series: dict = field(default_factory=dict)
    latest: dict = field(default_factory=dict)
    phase: str = ""
    progress: dict = field(default_factory=dict)
    progress_samples: deque = field(default_factory=lambda: deque(maxlen=32))
    errors: deque = field(default_factory=lambda: deque(maxlen=MAX_ERRORS))
    records: int = 0
    last_t: Optional[float] = None


class MetricReader:
    """Follow a bounded LRU of explicit JSONL metric streams.

    ``max_bytes`` bounds each read; ``max_points`` bounds each of at most 64
    metric series. Incomplete lines remain raw bytes until their newline arrives,
    so UTF-8 characters split across appends are not damaged. Snapshots are
    independent JSON-serializable objects and may safely be handed to the UI.
    """

    def __init__(self, files=None, max_bytes=1048576, max_points=600, max_streams=32):
        for name, value, maximum in (("max_bytes", max_bytes, MAX_READ_BYTES),
                                     ("max_points", max_points, MAX_POINTS),
                                     ("max_streams", max_streams, MAX_STREAMS)):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
                raise ValueError(f"{name} must be an integer from 1 to {maximum}")
        self.files = LocalFiles() if files is None else files
        self.max_bytes, self.max_points, self.max_streams = max_bytes, max_points, max_streams
        self.max_line_bytes, self.max_metrics = MAX_LINE_BYTES, MAX_METRICS
        self._streams = OrderedDict()
        self._owners = {}
        self._lock = threading.RLock()

    @staticmethod
    def _error(stream: _Stream, message: str) -> None:
        # An error may originate in a user-selected path or remote command.
        stream.errors.append("".join(c if c.isprintable() else "?" for c in message)[:240])

    def _accept(self, stream: _Stream, rec: dict) -> None:
        timestamp = rec["t"]
        for name, number in rec["metrics"].items():
            if name not in stream.series:
                if len(stream.series) >= MAX_METRICS:
                    self._error(stream, f"metric limit ({MAX_METRICS}) reached; additional names ignored")
                    stream.truncated = True
                    continue
                stream.series[name] = deque(maxlen=self.max_points)
            points = stream.series[name]
            if len(points) == self.max_points:
                stream.truncated = True
            # Compact immutable internal points avoid retaining thousands of
            # identical dictionary keys. Dictionaries are built only for the UI.
            point = (timestamp, number, rec["step"]) if "step" in rec else (timestamp, number)
            points.append(point)
            stream.latest[name] = number
        if "phase" in rec and rec["phase"] != stream.phase:
            stream.phase = rec["phase"]
            stream.progress_samples.clear()
            # A phase's progress must not be presented as the next phase's.
            stream.progress = {}
        if "progress" in rec:
            progress = dict(rec["progress"])
            samples = stream.progress_samples
            previous = stream.progress
            if (previous.get("total") != progress["total"] or previous.get("unit") != progress["unit"]
                    or (samples and (timestamp <= samples[-1][0] or progress["completed"] < samples[-1][1]))):
                samples.clear()
            samples.append((timestamp, progress["completed"]))
            if len(samples) >= 3:
                elapsed = timestamp - samples[0][0]
                advance = progress["completed"] - samples[0][1]
                if elapsed > 0 and advance > 0:
                    rate = advance / elapsed
                    if math.isfinite(rate) and rate > 0:
                        eta = (progress["total"] - progress["completed"]) / rate
                        if math.isfinite(eta):
                            progress.update(eta_seconds=eta, rate_per_second=rate,
                                            eta_source="reported_progress", eta_samples=len(samples))
            stream.progress = progress
        stream.records += 1
        stream.last_t = timestamp

    def _consume(self, stream: _Stream, data: bytes) -> None:
        if stream.discard_line:
            newline = data.find(b"\n")
            if newline < 0:
                return
            data = data[newline + 1:]
            stream.discard_line = False
        raw = stream.partial + data
        lines = raw.rsplit(b"\n", MAX_LINES_PER_READ + 1)
        stream.partial = lines.pop()
        if len(lines) > MAX_LINES_PER_READ:
            lines.pop(0)
            stream.truncated = True
            stream.progress_samples.clear()
            self._error(stream, "record burst exceeded the per-read line limit; oldest lines skipped")
        for line in lines:
            if len(line) > MAX_LINE_BYTES:
                stream.truncated = True
                self._error(stream, "record exceeded the 65536-byte line limit; ignored")
                continue
            if not line.strip():
                continue
            try:
                rec = _record(json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object))
            except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
                self._error(stream, f"invalid metric record: {exc}")
                continue
            self._accept(stream, rec)
        if len(stream.partial) > MAX_LINE_BYTES:
            stream.partial = b""
            stream.discard_line = True
            stream.truncated = True
            self._error(stream, "unterminated record exceeded the 65536-byte line limit; ignored")

    @staticmethod
    def _snapshot(path: str, stream: _Stream, status: Optional[str] = None) -> dict:
        if status is None:
            status = ("partial" if stream.errors else "ok") if stream.records else (
                "error" if stream.errors else "partial" if stream.partial or stream.discard_line else "empty")
        return {
            "path": path, "series": {
                name: [{"t": point[0], "value": point[1], **({"step": point[2]} if len(point) == 3 else {})}
                       for point in points]
                for name, points in stream.series.items()
            },
            "latest": dict(stream.latest), "phase": stream.phase, "progress": dict(stream.progress),
            "errors": list(stream.errors), "status": status, "records": stream.records,
            "truncated": stream.truncated, "last_t": stream.last_t,
        }

    def read(self, path) -> dict:
        """Return a snapshot, using one stat and only newly appended file bytes."""
        path = os.fspath(path)
        if not isinstance(path, str) or not path:
            raise ValueError("metric path must be a nonempty text path")
        with self._lock:
            stream = self._streams.get(path)
            if stream is None:
                stream = self._streams[path] = _Stream()
                if len(self._streams) > self.max_streams:
                    evicted, _ = self._streams.popitem(last=False)
                    self._owners.pop(evicted, None)
            self._streams.move_to_end(path)
            return self._read_stream(path, stream)

    def read_confined(self, path, binding) -> dict:
        """Follow a declared run stream after revalidating its exact job identity.

        Both metadata and incremental reads use one anchored regular descriptor.
        Replacing any project/run parent with a symlink cannot redirect a read.
        """
        from .projects import bound_run_root, bound_relative
        path = os.fspath(path)
        if not isinstance(path, str) or not path:
            raise ValueError("metric path must be a nonempty text path")
        with self._lock:
            owner = tuple(binding.get(key) for key in ("project_root", "run_id", "job_id", "attempt", "remote_identity"))
            launch = binding.get("launch", {})
            if isinstance(launch, dict):
                owner += tuple(launch.get(key) for key in ("cluster", "restart_count", "submission_token"))
            if self._owners.get(path) != owner:
                self._streams.pop(path, None)
            self._owners[path] = owner
            stream = self._streams.get(path)
            if stream is None:
                stream = self._streams[path] = _Stream()
                if len(self._streams) > self.max_streams:
                    evicted, _ = self._streams.popitem(last=False)
                    self._owners.pop(evicted, None)
            self._streams.move_to_end(path)
            if getattr(self.files, "remote", False):
                import base64
                from .project_remote import request
                try:
                    value = request(self.files, "read_bound_metric_chunk", binding, path,
                                    stream.offset, list(stream.ident) if stream.ident else None, self.max_bytes)
                    data = base64.b64decode(value["data"], validate=True)
                    start = value["offset"]
                    def read_bytes(offset, length):
                        if offset < start or offset + length > start + len(data):
                            raise OSError("SSH metric read window no longer matches this stream")
                        return data[offset - start:offset - start + length]
                    return self._read_bytes(path, stream, value["size"], tuple(value["ident"]), read_bytes)
                except OSError as exc:
                    self._error(stream, f"cannot read metrics: {exc}")
                    return self._snapshot(path, stream, "missing" if isinstance(exc, FileNotFoundError) else "error")
            with bound_run_root(binding) as (_, root_fd, current):
                relative = bound_relative(binding, path, current, key="metrics")
                return self._read_stream(path, stream, root_fd=root_fd, relative=relative)

    @contextmanager
    def _source(self, path, *, root_fd=None, relative=None):
        """Pin local stat and reads to one nonblocking regular-file descriptor."""
        if root_fd is not None:
            from .artifacts import _open_local
            fd, parent, _ = _open_local(root_fd, relative)
            try:
                current = os.fstat(fd)
                if not stat.S_ISREG(current.st_mode):
                    raise OSError(errno.EINVAL, "metric stream changed to a nonregular file before reading")
                yield current.st_size, (current.st_dev, current.st_ino), lambda offset, length: os.pread(fd, length, offset)
            finally:
                os.close(fd)
                os.close(parent)
            return
        if type(self.files) is not LocalFiles:
            size, ident = self.files.stat(path)
            yield size, ident, lambda offset, length: self.files.read(path, offset, length)
            return
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(errno.EINVAL, "metric stream must be a regular file; symlinks and devices are refused")
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags)
        try:
            current = os.fstat(fd)
            if not stat.S_ISREG(current.st_mode):
                raise OSError(errno.EINVAL, "metric stream changed to a nonregular file before reading")
            yield current.st_size, (current.st_dev, current.st_ino), lambda offset, length: os.pread(fd, length, offset)
        finally:
            os.close(fd)

    def _read_stream(self, path, stream, *, root_fd=None, relative=None) -> dict:
        try:
            with self._source(path, root_fd=root_fd, relative=relative) as (size, ident, read_bytes):
                return self._read_bytes(path, stream, size, ident, read_bytes)
        except OSError as exc:
            self._error(stream, f"cannot read metrics: {exc}")
            missing = isinstance(exc, FileNotFoundError) or exc.errno in (errno.ENOENT, errno.ENOTDIR)
            return self._snapshot(path, stream, "missing" if missing else "error")

    def _read_bytes(self, path, stream, size, ident, read_bytes) -> dict:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise OSError("invalid metric file size")
        if stream.ident != ident or size < stream.offset:
            stream = self._streams[path] = _Stream(ident=ident)
        if size == stream.offset:
            return self._snapshot(path, stream)
        offset = max(stream.offset, size - self.max_bytes)
        jumped = offset > stream.offset
        # Include a preceding byte when the byte budget permits. This tells
        # an exact line boundary from a partial first record, without an
        # extra remote round trip or exceeding the read budget.
        preceding = jumped and offset > 0 and self.max_bytes > 1
        read_offset = offset - 1 if preceding else offset
        length = min(self.max_bytes, size - read_offset)
        try:
            data = read_bytes(read_offset, length)
            if not isinstance(data, bytes):
                raise OSError("metric file reader returned non-byte data")
            data = data[:length]
        except OSError as exc:
            self._error(stream, f"cannot read metrics: {exc}")
            return self._snapshot(path, stream, "error")
        consumed = len(data)
        if jumped:
            stream.partial = b""
            stream.progress_samples.clear()
            stream.truncated = True
            stream.discard_line = not (preceding and data.startswith(b"\n"))
            if preceding and data:
                # This prefix was already outside the selected read window.
                prefix, data = data[:1], data[1:]
                if prefix == b"\n":
                    stream.discard_line = False
        stream.offset = read_offset + consumed
        self._consume(stream, data)
        return self._snapshot(path, stream)


def write_metric(path, metrics: Mapping[str, float], *, step=None, phase="", completed=None,
                 total=None, unit="", t=None) -> dict:
    """Append one validated record and return it; never truncate an existing file.

    New files are private (0600). A regular file is required and symlink targets
    are refused. POSIX advisory locking serializes cooperating writers, including
    separate processes. A missing final newline is separated rather than merged
    with the new record. Parent directories are deliberately not created.
    """
    if not isinstance(metrics, Mapping):
        raise ValueError("metrics must be a mapping")
    phase = _text(phase, "phase", 160)
    unit = _text(unit, "unit", 64)
    value = {"t": time.time() if t is None else t, "metrics": dict(metrics)}
    if step is not None:
        value["step"] = step
    if phase:
        value["phase"] = phase
    if completed is not None or total is not None:
        value["progress"] = {"completed": completed, "total": total, "unit": unit}
    elif unit:
        raise ValueError("unit requires completed and total")
    rec = _record(value)
    # fraction is derived, not part of the reporting format.
    if "progress" in rec:
        rec["progress"].pop("fraction")
    payload = (json.dumps(rec, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) - 1 > MAX_LINE_BYTES:
        raise ValueError("metric record exceeds the 65536-byte line limit")
    path = os.fspath(path)
    if not isinstance(path, str) or not path:
        raise ValueError("metric path must be a nonempty text path")
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise ValueError("metric destination must be a regular file, not a symlink or device")
    import fcntl  # Slurm hosts and the supported terminal runtime are POSIX.
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("metric destination must be a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            payload = b"\n" + payload
        while payload:
            try:
                written = os.write(fd, payload)
            except InterruptedError:
                continue
            if written <= 0:
                raise OSError("metric write made no progress")
            payload = payload[written:]
    finally:
        os.close(fd)
    return rec
