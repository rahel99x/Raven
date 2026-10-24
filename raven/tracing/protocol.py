"""Bounded, checksummed cooperative trace journals. Readers never attach to jobs.

The protocol proves continuity of recorded events, not completeness of arbitrary
program execution. A writer's open/crashed tail is explicitly uncommitted.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
from collections import deque
from pathlib import Path

from .reader_budget import retained_size as _retained_object_size

SCHEMA = "raven.trace/v1"
MAX_EVENT = 64 * 1024
MAX_EVENTS = 20_000
MAX_BYTES = 8 * 1024 * 1024
BLOCK_FORMAT = "raven.trace-block/v1"
EVENT_FORMAT = "raven.trace-event/v1"
MAX_BLOCK_BYTES = 1 << 20
MAX_BLOCK_EVENTS = 65536
MAX_BLOCK_HEADER = 1024
_CONTROL_TEXT = re.compile(r"[\x00-\x1f\x7f]")
IDENTITY_KEYS = (
    "job_id",
    "attempt",
    "cluster",
    "node",
    "boot_id",
    "pid",
    "process_start",
    "stream",
)
OPTIONAL_IDENTITY_KEYS = {
    "slurm_submit",
    "slurm_start",
    "run_id",
    "launcher",
    "role",
    "rank",
    "local_rank",
    "world_size",
    "parent_pid",
    "interpreter",
}


def encoded(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def event_digest(record):
    return hashlib.sha256(
        encoded({key: value for key, value in record.items() if key != "sha256"})
    ).hexdigest()


def encode_event_record(record):
    """Encode v1 once while preserving its exact canonical bytes and digest.

    The fixed envelope's final canonical key is stream. Insert the checksum
    immediately before it, so existing readers and archived hashes are unchanged.
    This removes redundant event serialization without a new integrity format.
    """
    if set(record) != {"schema", "stream", "seq", "prev", "event"}:
        raise ValueError("Expected an unsigned v1 trace event envelope")
    raw = encoded(record)
    digest = hashlib.sha256(raw).hexdigest()
    prefix, separator, stream = raw.rpartition(b',"stream":')
    if not separator:
        raise ValueError("Trace event envelope has no stream")
    return prefix + b',"sha256":"' + digest.encode(
        "ascii"
    ) + b'"' + separator + stream + b"\n", digest


def encode_block(stream, first_seq, previous, lines):
    """Hash one bounded batch; include all framing fields in the chain."""
    header = {
        "schema": BLOCK_FORMAT,
        "stream": stream,
        "first_seq": first_seq,
        "count": len(lines),
        "bytes": sum(map(len, lines)),
        "prev": previous,
    }
    if (
        not 1 <= len(lines) <= MAX_BLOCK_EVENTS
        or not 1 <= header["bytes"] <= MAX_BLOCK_BYTES
    ):
        raise ValueError("Invalid trace block budget")
    digest = hashlib.sha256(encoded(header) + b"\n")
    for line in lines:
        digest.update(line)
    checksum = digest.hexdigest()
    raw = encoded(dict(header, sha256=checksum)) + b"\n"
    if len(raw) > MAX_BLOCK_HEADER:
        raise ValueError("Trace block header exceeds its byte budget")
    return raw, checksum


def text(value, name, maximum=1024):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or not value.isprintable()
    ):
        raise ValueError(
            f"{name} must be a nonempty printable string of at most {maximum} characters"
        )
    return value


def regular_open(path):
    """Refuse symlinks in every path component and special-file blocking."""
    path = Path(path).absolute()
    for parent in reversed(path.parents):
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise ValueError(
                "Trace paths must not contain symlinks or non-directory parents"
            )
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("Trace input must be a regular file")
    return fd


def read_json(path, limit=1 << 20):
    fd = regular_open(path)
    try:
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(limit + 1)
    except BaseException:
        raise
    if len(raw) > limit:
        raise ValueError("Trace metadata exceeds its byte budget")
    return decode_json(raw)


def decode_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate trace JSON key")
            result[key] = value
        return result

    try:
        return json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("Nonfinite trace JSON")
            ),
        )
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid trace JSON") from exc


def validate_identity(value):
    if (
        not isinstance(value, dict)
        or not set(IDENTITY_KEYS) <= set(value)
        or set(value) - set(IDENTITY_KEYS) - OPTIONAL_IDENTITY_KEYS
    ):
        raise ValueError(
            "Trace identity must explicitly include job, attempt, cluster, node and process identity"
        )
    for key in value:
        if key in {"pid", "parent_pid", "rank", "local_rank", "world_size"}:
            minimum = 1 if key in {"pid", "world_size"} else 0
            if type(value[key]) is not int or not minimum <= value[key] < 1 << 63:
                raise ValueError(f"Trace {key} must be an integer >= {minimum}")
        else:
            text(value[key], key, 256)
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value["stream"]):
        raise ValueError("Trace stream must be an opaque safe identifier")
    return dict(value)


def _bounded_data(value, depth=0):
    if depth > 12:
        raise ValueError("Trace event nesting exceeds 12")
    if isinstance(value, dict):
        if len(value) > 64 or any(
            not isinstance(key, str) or not key.isprintable() or len(key) > 128
            for key in value
        ):
            raise ValueError("Invalid trace event fields")
        for child in value.values():
            _bounded_data(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > 4096:
            raise ValueError("Trace event list exceeds 4096 values")
        for child in value:
            _bounded_data(child, depth + 1)
    elif isinstance(value, str):
        if len(value) > 8192 or _CONTROL_TEXT.search(value):
            raise ValueError("Trace strings must be bounded printable text")
    elif isinstance(value, int) and not isinstance(value, bool):
        if value.bit_length() > 64:
            raise ValueError("Trace integer exceeds 64 bits")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Trace values must be finite")
    elif value is not None and not isinstance(value, (float, bool)):
        raise ValueError("Invalid trace event value")


def validate_event(event, *, check_size=True):
    if not isinstance(event, dict) or set(event) != {
        "kind",
        "mono_ns",
        "thread",
        "data",
    }:
        raise ValueError("Trace event requires kind, mono_ns, thread, data")
    text(event["kind"], "kind", 64)
    text(event["thread"], "thread", 128)
    if (
        isinstance(event["mono_ns"], bool)
        or not isinstance(event["mono_ns"], int)
        or not 0 <= event["mono_ns"] < 1 << 63
    ):
        raise ValueError("Trace time must be monotonic nanoseconds")
    if not isinstance(event["data"], dict):
        raise ValueError("Trace event data must be an object")
    _bounded_data(event["data"])
    if check_size and len(encoded(event)) > MAX_EVENT - 512:
        raise ValueError("Trace event exceeds 64 KiB")
    return event


def read_trace(path, *, max_events=MAX_EVENTS, max_bytes=MAX_BYTES, cancel=None):
    """Validate a stable committed prefix; bounded results are never called complete.

    Large journals are intentionally read incrementally by TraceReader, or read
    offline with explicit larger budgets. This one-shot API starts at sequence 1.
    """
    reader = TraceReader(path)
    return reader.refresh(max_events=max_events, max_bytes=max_bytes, cancel=cancel)


class TraceReader:
    """Incremental worker-owned journal reader with bounded retained events."""

    def __init__(
        self, path, *, retain=MAX_EVENTS, retain_bytes=MAX_BYTES, memory_budget=None
    ):
        path = Path(path).absolute()
        self.path = path / "manifest.json" if path.is_dir() else path
        self.offset = 0
        self.sequence = 0
        self.previous = "0" * 64
        self.mono = -1
        self.identity = None
        self.signature = None
        self.retain = max(1, min(int(retain), 1_000_000))
        if (
            type(retain_bytes) is not int
            or not 2 * MAX_EVENT <= retain_bytes <= 1 << 30
        ):
            raise ValueError("Trace retained byte budget must be 128 KiB–1 GiB")
        self.retain_bytes = retain_bytes
        self.retained_bytes = 0
        self.events = deque()
        self._event_sizes = deque()
        self.journal_format = None
        self._block = None
        self._checkpoint = (0, 0)
        self.stack_map = {}
        self.warnings = []
        self.invalid = False
        self.end_seen = False
        self.evicted = 0
        self.source_loss = False
        self._codes = {}
        self._code_sizes = {}
        self._code_bytes = 0
        self.frame_encoding = None
        self._memory_budget = memory_budget
        self._working_charges = {}
        self._frame_charges = {}
        self._event_charges = deque()
        self._closed = False
        if memory_budget is not None:
            self._charge(
                12288 + 4 * len(str(self.path)),
                "reader metadata and bounded diagnostics",
            )

    def _charge(self, amount, component):
        if self._memory_budget is not None:
            self._memory_budget.reserve(amount, component)
            self._working_charges[component] = (
                self._working_charges.get(component, 0) + amount
            )

    def _release(self, amount, component):
        if self._memory_budget is not None and amount:
            held = self._working_charges.get(component, 0)
            if not 0 <= amount <= held:
                raise ValueError(
                    "Trace reader working-state component release is inconsistent"
                )
            self._memory_budget.release(amount)
            self._working_charges[component] = held - amount

    def release_consumed_window(self):
        """Drop published event copies; preserve cursor/hash/decoding state."""
        self.events.clear()
        self._event_sizes.clear()
        self.retained_bytes = 0
        if self._memory_budget is not None:
            self._release(sum(self._event_charges), "event window")
            self._event_charges.clear()

    def close(self):
        """Release all owned working state; this reader cannot resume afterward."""
        if self._closed:
            return
        self.release_consumed_window()
        self._block = None
        self.stack_map.clear()
        self._frame_charges.clear()
        self._codes.clear()
        self._code_sizes.clear()
        self._code_bytes = 0
        if self._memory_budget is not None:
            for amount in self._working_charges.values():
                self._memory_budget.release(amount)
        self._working_charges.clear()
        self._closed = True

    def iter_pages(
        self,
        *,
        max_events=MAX_EVENTS,
        max_bytes=MAX_BYTES,
        cancel=None,
        max_seconds=None,
    ):
        """Yield bounded consecutive snapshots until caught up or interrupted.

        Each snapshot retains at most ``retain`` events. Select a page size no
        larger than ``retain`` when consuming every event (exports/analysis).
        Existing readers can resume with refresh after an open writer advances.
        """
        while True:
            previous = self.sequence
            snapshot = self.refresh(
                max_events=min(max_events, self.retain),
                max_bytes=min(
                    max_bytes, self.retain_bytes - MAX_EVENT - MAX_BLOCK_HEADER
                ),
                cancel=cancel,
                max_seconds=max_seconds,
            )
            yield snapshot
            if snapshot["caught_up"] or self.invalid or self.sequence == previous:
                return

    def _block_event(self, stream, identity, committed, committed_bytes):
        """Authenticate a bounded whole block before exposing any of its events."""
        header_bytes = 0
        if self._block is None:
            raw = stream.readline(
                min(MAX_BLOCK_HEADER + 1, committed_bytes - self.offset)
            )
            if not raw.endswith(b"\n") or len(raw) > MAX_BLOCK_HEADER:
                raise ValueError("Truncated or oversized committed trace block header")
            header = decode_json(raw)
            keys = {"schema", "stream", "first_seq", "count", "bytes", "prev", "sha256"}
            if not isinstance(header, dict) or set(header) != keys:
                raise ValueError("Invalid trace block envelope")
            if (
                header["schema"] != BLOCK_FORMAT
                or header["stream"] != identity["stream"]
                or type(header["first_seq"]) is not int
                or header["first_seq"] != self.sequence + 1
                or header["prev"] != self.previous
            ):
                raise ValueError("Trace block sequence or chain mismatch")
            if (
                type(header["count"]) is not int
                or not 1 <= header["count"] <= MAX_BLOCK_EVENTS
                or type(header["bytes"]) is not int
                or not 1 <= header["bytes"] <= MAX_BLOCK_BYTES
                or header["first_seq"] + header["count"] - 1 > committed
                or self.offset + len(raw) + header["bytes"] > committed_bytes
            ):
                raise ValueError(
                    "Trace block exceeds its committed checkpoint or budget"
                )
            self._charge(header["bytes"] + 4096, "authenticated block")
            try:
                body = stream.read(header["bytes"])
                signed = {
                    key: value for key, value in header.items() if key != "sha256"
                }
                digest = hashlib.sha256(encoded(signed) + b"\n")
                digest.update(body)
                if (
                    len(body) != header["bytes"]
                    or digest.hexdigest() != header["sha256"]
                ):
                    raise ValueError(
                        "Trace block checksum mismatch or truncated committed block"
                    )
            except BaseException:
                self._release(header["bytes"] + 4096, "authenticated block")
                raise
            self._block = [header, body, 0, 0]
            header_bytes = len(raw)
        header, body, cursor, count = self._block
        end = body.find(b"\n", cursor, cursor + MAX_EVENT + 1)
        if end < 0 or end - cursor + 1 > MAX_EVENT:
            raise ValueError("Truncated or oversized committed trace event")
        raw = body[cursor : end + 1]
        record = decode_json(raw)
        if not isinstance(record, dict) or set(record) != {
            "schema",
            "stream",
            "seq",
            "event",
        }:
            raise ValueError("Invalid block trace event envelope")
        if (
            record["schema"] != EVENT_FORMAT
            or record["stream"] != identity["stream"]
            or type(record["seq"]) is not int
            or record["seq"] != self.sequence + 1
        ):
            raise ValueError("Trace sequence gap, duplicate, or stream mismatch")
        count += 1
        if (count == header["count"]) != (end + 1 == len(body)):
            raise ValueError("Trace block event count mismatch")
        self._block[2:] = [end + 1, count]
        if count == header["count"]:
            self._block = None
            self._release(header["bytes"] + 4096, "authenticated block")
        return (
            record,
            header_bytes + len(raw),
            header["sha256"] if self._block is None else self.previous,
        )

    def refresh(
        self,
        *,
        max_events=MAX_EVENTS,
        max_bytes=MAX_BYTES,
        cancel=None,
        max_seconds=None,
    ):
        if self._closed:
            raise ValueError("Trace reader is closed")
        if (
            isinstance(max_events, bool)
            or not 1 <= max_events <= 1_000_000
            or not 1024 <= max_bytes <= 1 << 30
        ):
            raise ValueError("Invalid trace reader budget")
        if max_seconds is not None and (
            isinstance(max_seconds, bool)
            or not isinstance(max_seconds, (int, float))
            or not 0.001 <= max_seconds <= 60
        ):
            raise ValueError("Trace work slice must be 1 ms–60 s")
        deadline = time.monotonic() + max_seconds if max_seconds is not None else None
        manifest = read_json(self.path)
        if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
            raise ValueError("Expected raven.trace/v1 manifest")
        identity = validate_identity(manifest.get("identity"))
        process = {
            key: identity[key]
            for key in (
                "pid",
                "parent_pid",
                "role",
                "launcher",
                "rank",
                "local_rank",
                "world_size",
                "interpreter",
            )
            if key in identity
        }
        if "process" in manifest and manifest["process"] != process:
            raise ValueError("Trace process metadata disagrees with its identity")
        coverage = manifest.get("coverage")
        if not isinstance(coverage, dict):
            raise ValueError("Trace manifest must declare its coverage")
        _bounded_data(coverage)
        text(coverage.get("scope"), "coverage scope", 8192)
        if (
            "complete" in coverage
            and coverage["complete"] is not None
            and type(coverage["complete"]) is not bool
        ):
            raise ValueError("Trace coverage completeness must be true, false or null")
        warnings_field = manifest.get("warnings", [])
        if (
            not isinstance(warnings_field, list)
            or len(warnings_field) > 32
            or any(not isinstance(w, str) for w in warnings_field)
        ):
            raise ValueError("Invalid trace warnings")
        _bounded_data(warnings_field)
        if manifest.get("state") not in {"OPEN", "CLOSED", "ERROR", "LIMIT", "PARTIAL"}:
            raise ValueError("Unknown trace recording state")
        if (
            isinstance(manifest.get("dropped", 0), bool)
            or not isinstance(manifest.get("dropped", 0), int)
            or manifest.get("dropped", 0) < 0
        ):
            raise ValueError("Invalid trace loss count")
        if self.identity is not None and self.identity != identity:
            raise ValueError(
                "Trace identity changed; open a new reader for a new attempt"
            )
        if self.identity is None:
            self._charge(1024 + _retained_object_size(identity), "source identity")
        self.identity = identity
        journal_format = manifest.get("journal_format", EVENT_FORMAT)
        if journal_format not in {EVENT_FORMAT, BLOCK_FORMAT}:
            raise ValueError("Unknown trace journal format")
        if self.journal_format is not None and self.journal_format != journal_format:
            raise ValueError("Trace journal format changed")
        self.journal_format = journal_format
        frame_encoding = manifest.get("frame_encoding")
        if frame_encoding not in {None, "code-dictionary/v1"}:
            raise ValueError("Unsupported trace frame encoding")
        if self.frame_encoding is not None and frame_encoding != self.frame_encoding:
            raise ValueError("Trace frame encoding regressed or changed")
        self.frame_encoding = frame_encoding
        if journal_format == BLOCK_FORMAT and (
            type(manifest.get("block_size")) is not int
            or not 1 <= manifest["block_size"] <= MAX_BLOCK_EVENTS
            or manifest.get("max_block_bytes") != MAX_BLOCK_BYTES
        ):
            raise ValueError("Invalid trace block storage bounds")
        if manifest.get("journal") != "events.jsonl":
            raise ValueError(
                "Trace manifest journal must be events.jsonl beside the manifest"
            )
        committed = manifest.get("committed_seq")
        committed_bytes = manifest.get("committed_bytes")
        if (
            isinstance(committed, bool)
            or not isinstance(committed, int)
            or committed < self.sequence
            or isinstance(committed_bytes, bool)
            or not isinstance(committed_bytes, int)
            or committed_bytes < self.offset
        ):
            raise ValueError("Trace commit checkpoint regressed or is invalid")
        if committed < self._checkpoint[0] or committed_bytes < self._checkpoint[1]:
            raise ValueError(
                "Trace commit checkpoint regressed since the previous manifest"
            )
        self._checkpoint = (committed, committed_bytes)
        count = used = 0
        read_error = None
        fd = regular_open(self.path.parent / "events.jsonl")
        opened = None
        try:
            st = os.fstat(fd)
            signature = (st.st_dev, st.st_ino)
            if self.signature is not None and signature != self.signature:
                raise ValueError("Trace journal was replaced")
            self.signature = signature
            if st.st_size < committed_bytes:
                self._warn(
                    "Committed journal bytes are missing; recording is incomplete"
                )
                self.invalid = True
            opened = os.fdopen(fd, "rb")
            fd = None  # The file object now owns the descriptor.
            with opened as stream:
                stream.seek(self.offset)
                while (
                    not self.invalid
                    and self.sequence < committed
                    and count < max_events
                    and used < max_bytes
                ):
                    if count and deadline is not None and time.monotonic() >= deadline:
                        break
                    if cancel is not None and cancel.is_set():
                        self._warn("Trace read cancelled; available prefix only")
                        break
                    try:
                        if journal_format == BLOCK_FORMAT:
                            record, consumed, checksum = self._block_event(
                                stream, identity, committed, committed_bytes
                            )
                        else:
                            remaining = min(
                                MAX_EVENT + 1, committed_bytes - self.offset
                            )
                            if remaining <= 0:
                                raise ValueError(
                                    "Commit sequence exceeds journal checkpoint"
                                )
                            raw = stream.readline(remaining)
                            if not raw.endswith(b"\n") or len(raw) > MAX_EVENT:
                                raise ValueError(
                                    "Truncated or oversized committed trace record"
                                )
                            record = decode_json(raw)
                            if not isinstance(record, dict) or set(record) != {
                                "schema",
                                "stream",
                                "seq",
                                "prev",
                                "event",
                                "sha256",
                            }:
                                raise ValueError("Invalid trace envelope")
                            if (
                                record["schema"] != EVENT_FORMAT
                                or record["stream"] != identity["stream"]
                                or record["sha256"] != event_digest(record)
                            ):
                                raise ValueError(
                                    "Trace checksum or stream identity mismatch"
                                )
                            if (
                                type(record["seq"]) is not int
                                or record["seq"] != self.sequence + 1
                                or record["prev"] != self.previous
                            ):
                                raise ValueError(
                                    "Trace sequence gap, duplicate, or chain mismatch"
                                )
                            consumed, checksum = len(raw), record["sha256"]
                        # The enclosing record already has a checked byte bound.
                        # Do not JSON-encode every event again just to size it.
                        event = validate_event(record["event"], check_size=False)
                        data = event["data"]
                        retained_size = consumed
                        if (
                            frame_encoding == "code-dictionary/v1"
                            and event["kind"] == "code"
                        ):
                            code_id = data.get("id")
                            if (
                                type(code_id) is not int
                                or not 1 <= code_id <= 65536
                                or not isinstance(data.get("function"), str)
                                or not isinstance(data.get("file"), str)
                                or not isinstance(data.get("frame"), str)
                                or type(data.get("line")) is not int
                                or data["line"] < 0
                            ):
                                raise ValueError("Invalid trace code dictionary entry")
                            metadata = {
                                key: data[key]
                                for key in ("function", "file", "line", "frame")
                            }
                            prior = self._codes.get(code_id)
                            if prior is not None and prior != metadata:
                                raise ValueError(
                                    "Trace code dictionary identity changed"
                                )
                            if prior is None:
                                metadata_size = len(encoded(metadata))
                                if self._code_bytes + metadata_size > 16 << 20:
                                    raise ValueError(
                                        "Trace code dictionary exceeds its byte budget"
                                    )
                                if self._memory_budget is not None:
                                    self._charge(
                                        1024 + _retained_object_size(metadata),
                                        "code dictionary",
                                    )
                                self._code_bytes += metadata_size
                                self._codes[code_id] = metadata
                                self._code_sizes[code_id] = metadata_size
                        elif (
                            frame_encoding == "code-dictionary/v1"
                            and event["kind"]
                            in {"call", "resume", "return", "yield", "unwind"}
                            and "code" in data
                            and not any(
                                key in data
                                for key in ("function", "file", "line", "frame")
                            )
                        ):
                            code_id = data["code"]
                            if type(code_id) is not int or code_id not in self._codes:
                                raise ValueError(
                                    "Trace references unknown code metadata"
                                )
                            event = {**event, "data": {**self._codes[code_id], **data}}
                            retained_size += self._code_sizes[code_id]
                        if event["mono_ns"] < self.mono:
                            raise ValueError("Trace monotonic clock moved backwards")
                        self.mono = event["mono_ns"]
                    except (
                        ValueError,
                        TypeError,
                        KeyError,
                        RecursionError,
                        OverflowError,
                    ) as exc:
                        self._warn(str(exc))
                        self.invalid = True
                        break
                    except OSError as exc:
                        # Earlier events in this page are already verified.
                        # Publish them once instead of stranding their cursor
                        # behind an exception and skipping them on the retry.
                        read_error = str(exc)[:8192]
                        break
                    self.offset += consumed
                    self.sequence = record["seq"]
                    self.previous = checksum
                    flat = {**identity, **event, "seq": self.sequence}
                    if self._memory_budget is not None:
                        charge = 256 + _retained_object_size(flat)
                        self._charge(charge, "event window")
                        self._event_charges.append(charge)
                    self.events.append(flat)
                    self._event_sizes.append(retained_size)
                    self.retained_bytes += retained_size
                    while (
                        len(self.events) > self.retain
                        or self.retained_bytes > self.retain_bytes
                    ):
                        self.events.popleft()
                        self.retained_bytes -= self._event_sizes.popleft()
                        if self._memory_budget is not None:
                            self._release(self._event_charges.popleft(), "event window")
                        self.evicted += 1
                    self._stack(flat)
                    if event["kind"] in {"gap", "lost", "drop", "loss", "dropped"}:
                        self.source_loss = True
                        self._warn(
                            "Source reports omitted events; declared coverage is incomplete"
                        )
                    self.end_seen |= event["kind"] == "stream_end"
                    count += 1
                    used += consumed
                    if journal_format == BLOCK_FORMAT and self._block is None:
                        try:
                            stream.seek(self.offset)
                        except OSError as exc:
                            read_error = str(exc)[:8192]
                            break
        except BaseException:
            # A raw descriptor number may already have been reused after the
            # context exits. Close only the resource still owned here.
            try:
                if opened is not None:
                    opened.close()
                elif fd is not None:
                    os.close(fd)
            except OSError:
                pass
            raise
        caught_up = (
            self.sequence == committed
            and self.offset == committed_bytes
            and self._block is None
            and read_error is None
        )
        warnings = list(self.warnings)
        if read_error is not None:
            warnings.append("Temporary trace read error: " + read_error)
        state = manifest.get("state", "UNKNOWN")
        loss = manifest.get("dropped", 0)
        if not caught_up:
            warnings.append(
                "Reader budget reached or evidence missing; more committed records remain"
            )
        if st.st_size > committed_bytes:
            warnings.append(
                "Journal has an uncommitted tail beyond its durable checkpoint"
            )
        if state != "CLOSED":
            warnings.append(
                "Recording is open or interrupted; its uncommitted tail is unknown"
            )
        if loss:
            warnings.append(
                f"Producer reports {loss} omitted events; coverage is incomplete"
            )
        if self.evicted:
            warnings.append(
                f"View retains at most {self.retain} events / {self.retain_bytes} encoded bytes; raw journal remains on disk"
            )
        warnings.extend(
            str(item)
            for item in manifest.get("warnings", [])[:16]
            if isinstance(item, str)
        )
        coverage = manifest.get("coverage", {})
        declared_complete = coverage.get("complete", True) is True
        if not declared_complete:
            warnings.append(
                "Producer declares incomplete or unknown coverage within its instrumented scope"
            )
        complete = (
            state == "CLOSED"
            and st.st_size == committed_bytes
            and caught_up
            and self.end_seen
            and not self.invalid
            and not loss
            and not self.source_loss
            and coverage.get("mode") != "snapshot"
            and declared_complete
        )
        status = (
            "INVALID"
            if self.invalid
            else "COMPLETE"
            if complete
            else "LIVE"
            if state == "OPEN" and caught_up
            else "PARTIAL"
        )
        stacks = [
            {"thread": key, "frames": list(value[-128:])}
            for key, value in sorted(self.stack_map.items())
        ]
        rows = [
            f"Trace {identity['job_id']} / attempt {identity['attempt']} · {status}",
            f"{identity['node']} · PID {identity['pid']} · {self.sequence} checked events",
            f"Coverage: {manifest.get('coverage', {}).get('scope', 'undeclared')}",
        ]
        for stack in stacks[:64]:
            rows.append(f"Thread {stack['thread']}: {len(stack['frames'])} frames")
            rows.extend(
                "  "
                + str(frame.get("function") or "?")
                + f" ({frame.get('file', '?')}:{frame.get('line', '?')})"
                for frame in stack["frames"][-16:]
            )
        if not stacks or not any(item["frames"] for item in stacks):
            rows.append("No active recorded frames in the committed prefix")
        return {
            "schema": "raven.trace-snapshot/v1",
            "identity": identity,
            "events": list(self.events),
            "stacks": stacks,
            "status": status,
            "rows": rows,
            "warnings": warnings,
            "complete": complete,
            "covered": manifest.get("coverage", {}),
            "caught_up": caught_up,
            "committed_seq": committed,
            "read_seq": self.sequence,
            "manifest": manifest,
            "sha256": self.previous,
            "evicted": self.evicted,
            "retained_bytes": self.retained_bytes,
            "read_error": read_error,
            "window_start": self.events[0]["seq"] if self.events else self.sequence + 1,
            "window_end": self.sequence,
            "process": {
                key: identity[key]
                for key in (
                    "pid",
                    "parent_pid",
                    "role",
                    "launcher",
                    "rank",
                    "local_rank",
                    "world_size",
                    "interpreter",
                )
                if key in identity
            },
        }

    def _warn(self, message):
        if message not in self.warnings and len(self.warnings) < 32:
            self.warnings.append(message)

    def _thread_stack(self, thread):
        if thread not in self.stack_map:
            if self._memory_budget is not None:
                self._charge(512 + _retained_object_size(thread), "thread metadata")
                self._frame_charges[thread] = []
            self.stack_map[thread] = []
        return self.stack_map[thread]

    def _clear_stack(self, thread):
        self.stack_map[thread].clear()
        if self._memory_budget is not None:
            self._release(sum(self._frame_charges[thread]), "stack frames")
            self._frame_charges[thread].clear()

    def _stack(self, event):
        kind, data, thread = event["kind"], event["data"], event["thread"]
        if (
            kind in {"stack_checkpoint", "call", "resume", "return", "yield", "unwind"}
            and thread not in self.stack_map
            and len(self.stack_map) >= 4096
        ):
            self._warn("Trace thread reconstruction exceeds 4096 threads")
            self.invalid = True
            return
        if kind == "stack_checkpoint":
            frames = data.get("frames")
            if (
                not isinstance(frames, list)
                or len(frames) > 4096
                or any(not isinstance(frame, dict) for frame in frames)
            ):
                self._warn("Invalid stack checkpoint")
                self.invalid = True
                return
            self._thread_stack(thread)
            charges = []
            if self._memory_budget is not None:
                charges = [
                    256
                    + _retained_object_size(
                        {
                            key: frame.get(key)
                            for key in ("function", "file", "line", "frame")
                        }
                    )
                    for frame in frames
                ]
                self._charge(sum(charges), "stack frames")
            replacement = [
                {key: frame.get(key) for key in ("function", "file", "line", "frame")}
                for frame in frames
            ]
            self._clear_stack(thread)
            self.stack_map[thread] = replacement
            if self._memory_budget is not None:
                self._frame_charges[thread] = charges
        elif kind in {"call", "resume"}:
            stack = self._thread_stack(thread)
            if len(stack) >= 4096:
                self._warn(
                    "Recorded stack exceeds 4096 frames; stack reconstruction truncated"
                )
                self.invalid = True
                return
            frame = {
                key: data.get(key) for key in ("function", "file", "line", "frame")
            }
            if self._memory_budget is not None:
                charge = 256 + _retained_object_size(frame)
                self._charge(charge, "stack frames")
                self._frame_charges[thread].append(charge)
            stack.append(frame)
        elif kind in {"return", "yield", "unwind"}:
            stack = self._thread_stack(thread)
            if not stack:
                self._warn("Return without a recorded entry; stack prefix is unknown")
            elif (
                data.get("frame") is not None
                and stack[-1].get("frame") != data["frame"]
            ):
                self._warn("Stack transition mismatch; stack prefix reset")
                self._clear_stack(thread)
            else:
                stack.pop()
                if self._memory_budget is not None:
                    self._release(self._frame_charges[thread].pop(), "stack frames")


def discover_trace(project_root, job_id, attempt=None):
    """Resolve registered sources through the shared canonical parser."""
    from .registrations import discover_trace as discover

    return discover(
        project_root,
        job_id,
        attempt,
        read_json=read_json,
        validate_identity=validate_identity,
    )


def discover_trace_candidates(
    project_root,
    job_id,
    attempt=None,
    *,
    max_sources=512,
    cancel=None,
    max_metadata_bytes=64 << 20,
):
    """Discover bounded registrations without opening individual manifests."""
    from .registrations import discover_trace_candidates as candidates

    return candidates(
        project_root,
        job_id,
        attempt,
        max_sources=max_sources,
        cancel=cancel,
        max_metadata_bytes=max_metadata_bytes,
        read_json=read_json,
        validate_identity=validate_identity,
    )
