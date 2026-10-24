"""Opt-in, user-owned event recording and a Python launch adapter.

Recording is independent of Raven's UI. Each process owns one journal. Flush
checkpoints bound durability; an incomplete tail is never described as lossless.
"""

from __future__ import annotations

import fcntl
import os
import socket
import stat
import sys
import threading
import time
import uuid
import weakref
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from . import protocol as protocol
from ..reporting import _atomic_json, _directory

_RECORDERS = weakref.WeakSet()
_CAPTURES = weakref.WeakSet()


@contextmanager
def _internal_callbacks():
    """Own synchronization must not recursively profile its held Event locks."""
    previous = []
    for capture in list(_CAPTURES):
        busy = getattr(capture.local, "busy", False)
        capture.local.busy = True
        previous.append((capture, busy))
    try:
        yield
    finally:
        for capture, busy in previous:
            capture.local.busy = busy


def _fork_before():
    with _internal_callbacks():
        deadline = time.monotonic() + 0.05
        for recorder in list(_RECORDERS):
            recorder._fork_pause.set()
            recorder._flush_wake.set()
        for recorder in list(_RECORDERS):
            if recorder._flush_thread is not None:
                recorder._fork_quiet.wait(max(0.0, deadline - time.monotonic()))


def _fork_parent():
    with _internal_callbacks():
        for recorder in list(_RECORDERS):
            recorder._fork_pause.clear()
            recorder._flush_wake.set()


def _fork_child():
    # Register one process-wide callback; completed snapshots must not leak
    # recorder instances through os.register_at_fork's permanent callback list.
    for recorder in list(_RECORDERS):
        recorder._after_fork()
    for capture in list(_CAPTURES):
        capture._after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_fork_before, after_in_parent=_fork_parent, after_in_child=_fork_child
    )


def _process_start(pid=None):
    try:
        # comm may contain spaces or parentheses; fields after its final ')'.
        return (
            Path(f"/proc/{pid or os.getpid()}/stat")
            .read_text()
            .rsplit(")", 1)[1]
            .split()[19]
        )
    except (OSError, IndexError):
        return "unknown"


def _boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def default_job_id():
    parent, task = (
        os.environ.get("SLURM_ARRAY_JOB_ID", ""),
        os.environ.get("SLURM_ARRAY_TASK_ID", ""),
    )
    return (
        parent + "_" + task
        if parent.isdecimal() and task.isdecimal()
        else os.environ.get("SLURM_JOB_ID", "local")
    )


def _safe_directory(path):
    path = Path(path).absolute()
    _directory(path)
    return path


def _trace_root(project):
    root = _safe_directory(project)
    for name in (".raven", "traces"):
        root /= name
        root.mkdir(mode=0o700, exist_ok=True)
        _directory(root)
    return root


def _register(project, record):
    trace_root = _trace_root(project)
    lock_fd = os.open(
        trace_root / ".index.lock",
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise ValueError("Trace index lock must be a regular file")
        deadline = time.monotonic() + 0.2
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "Trace registry is busy; optional attachment skipped"
                    ) from None
                time.sleep(min(0.01, remaining))
        index = trace_root / "index.json"
        doc = (
            protocol.read_json(index, limit=4 << 20)
            if index.exists()
            else {"schema": "raven.trace-index/v1", "records": []}
        )
        if doc.get("schema") != "raven.trace-index/v1" or not isinstance(
            doc.get("records"), list
        ):
            raise ValueError("Invalid trace registry")
        if record in doc["records"]:
            return
        records = [
            entry
            for entry in doc["records"]
            if isinstance(entry, dict)
            and entry.get("path") != record["path"]
            and isinstance(entry.get("path"), str)
            and not Path(entry["path"]).is_absolute()
            and ".." not in Path(entry["path"]).parts
            and (Path(project) / entry["path"]).exists()
        ]
        omitted = max(0, len(records) + 1 - 4096)
        records = records[-4095:]
        records.append(record)
        # Recover an older registry that already exceeded the old 1 MiB
        # limit. A count cap alone does not bound long legacy paths/metadata.
        costs = []
        for entry in records:
            pending, count, depth = [(entry, 0)], 0, 0
            while pending:
                value, level = pending.pop()
                count += 1
                depth = max(depth, level)
                if isinstance(value, dict):
                    pending.extend((child, level + 1) for child in value.values())
                elif isinstance(value, list):
                    pending.extend((child, level + 1) for child in value)
            costs.append(
                (len(protocol.encoded(entry)) + 1, count if depth <= 28 else 100001)
            )
        size, nodes = sum(item[0] for item in costs), sum(item[1] for item in costs)
        first = 0
        while first < len(records) - 1 and (size > (1 << 20) - 1024 or nodes > 90000):
            size -= costs[first][0]
            nodes -= costs[first][1]
            first += 1
        omitted += first
        records = records[first:]
        prior_omitted = doc.get("omitted_registrations", 0)
        if type(prior_omitted) is not int or prior_omitted < 0:
            raise ValueError("Invalid trace registration omission count")
        _atomic_json(
            index,
            {
                "schema": "raven.trace-index/v1",
                "records": records,
                "omitted_registrations": min(1_000_000, prior_omitted + omitted),
            },
            replace=True,
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def register_trace(project, manifest_path):
    project = _safe_directory(project)
    manifest_path = Path(manifest_path).absolute()
    try:
        relative = str(manifest_path.relative_to(project))
    except ValueError as exc:
        raise ValueError("Registered trace must be inside its project") from exc
    manifest = protocol.read_json(manifest_path)
    identity = protocol.validate_identity(manifest.get("identity"))
    if manifest.get("schema") != protocol.SCHEMA:
        raise ValueError("Expected trace manifest")
    _register(project, {**identity, "path": relative})


def register_run(project, run, job_id, attempt):
    """Register one run index, never one project-wide entry per interpreter."""
    project = _safe_directory(project)
    relative = str(Path(run).absolute().relative_to(project) / "trace/index.json")
    _register(
        project,
        {
            "schema": "raven.trace-run/v1",
            "job_id": str(job_id),
            "attempt": str(attempt),
            "path": relative,
        },
    )


class Recorder:
    """Cooperative API usable from Python and language-specific adapters.

    `emit` records metadata, never locals or application payloads. On a storage
    or budget failure recording stops and marks incomplete; the application is
    not killed. This policy is explicit, not a strict lossless guarantee.
    """

    def __init__(
        self,
        project,
        *,
        job_id=None,
        attempt=None,
        cluster=None,
        coverage=None,
        max_bytes=64 << 20,
        flush_events=64,
        flush_interval=0.05,
        identity_extra=None,
        stream_id=None,
        initial_mono_ns=None,
        background_flush=True,
        journal_format="events",
        reserve_bytes=None,
        cpu_placement=False,
        register=True,
    ):
        if (
            type(max_bytes) is not int
            or not 4096 <= max_bytes <= 1 << 40
            or type(flush_events) is not int
            or not 1 <= flush_events <= 65536
            or isinstance(flush_interval, bool)
            or not isinstance(flush_interval, (int, float))
            or not 0 <= flush_interval <= 60
        ):
            raise ValueError("Invalid recorder storage or flush budget")
        if type(cpu_placement) is not bool:
            raise ValueError("cpu_placement must be true or false")
        if journal_format not in {"events", "blocks"}:
            raise ValueError("Unknown trace journal format")
        self.journal_format = journal_format
        if reserve_bytes is not None and not callable(reserve_bytes):
            raise ValueError("Trace byte reservation must be callable")
        self.reserve_bytes = reserve_bytes
        self._pending = []
        self._pending_bytes = 0
        self._written_seq = 0
        self._storage_failed = False
        self.project = _safe_directory(project)
        self._pid = os.getpid()
        self.lock = threading.RLock()
        self._fork_disabled = False
        self._emitter = threading.local()
        self._queue_lock = threading.Lock()
        self._frame_queue = deque()
        self._accepting_frames = True
        self._drain_owner = None
        self._deferred = deque(maxlen=128)
        self._flush_wake = threading.Event()
        self._flush_complete = threading.Event()
        self._force_flush = False
        self._async_frames = False
        self._buffer_failed = False
        self._fork_pause = threading.Event()
        self._fork_quiet = threading.Event()
        self._codes = {}
        self._code_bytes = 0
        self.stream = stream_id or uuid.uuid4().hex
        self.identity = protocol.validate_identity(
            {
                "job_id": str(job_id or default_job_id()),
                "attempt": str(
                    attempt
                    or os.environ.get("RAVEN_RUN_ID")
                    or "unverified-" + self.stream
                ),
                "cluster": cluster or os.environ.get("SLURM_CLUSTER_NAME") or "local",
                "node": socket.gethostname(),
                "boot_id": _boot_id(),
                "pid": self._pid,
                "process_start": _process_start(),
                "stream": self.stream,
                **(identity_extra or {}),
            }
        )
        self.directory = _trace_root(self.project) / self.stream
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / "manifest.json"
        self.fd = os.open(
            self.directory / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        self.sequence = self.bytes = self.committed_seq = self.committed_bytes = (
            self.dropped
        ) = 0
        self.previous = "0" * 64
        self.max_bytes, self.flush_events, self.flush_interval = (
            max_bytes,
            flush_events,
            flush_interval,
        )
        self.last_flush = time.monotonic()
        self.state = "OPEN"
        self.warnings = []
        self.coverage = dict(
            coverage
            or {
                "adapter": "cooperative",
                "scope": "Explicit events submitted by this process; uninstrumented code is unknown",
            }
        )
        self._cpu_placement = None
        if cpu_placement:
            from .cpu import CPUPlacement

            self._cpu_placement = CPUPlacement()
            self.coverage["cpu_placement"] = self._cpu_placement.coverage
        try:
            self._publish()
            if register:
                register_trace(self.project, self.path)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise
        _RECORDERS.add(self)
        self.emit("stream_start", {"coverage": self.coverage}, mono_ns=initial_mono_ns)
        self._flush_stop = threading.Event()
        self._flush_thread = (
            threading.Thread(
                target=self._flush_loop, name="RavenTraceFlush", daemon=True
            )
            if background_flush
            else None
        )
        if self._flush_thread is not None:
            self._flush_thread.start()

    def _flush_loop(self):
        while True:
            self._flush_wake.wait(max(0.01, self.flush_interval))
            self._flush_wake.clear()
            if self._fork_pause.is_set():
                self._fork_quiet.set()
                while self._fork_pause.is_set():
                    self._flush_wake.wait(0.01)
                    self._flush_wake.clear()
                self._fork_quiet.clear()
            self._drain_frames()
            while self._deferred:
                try:
                    self._deferred.popleft()()
                except Exception:
                    pass
            if self.fd is None:
                return
            cadence = (
                max(1.0, self.flush_interval)
                if self._async_frames
                else self.flush_interval
            )
            if (
                self._force_flush
                or self._flush_stop.is_set()
                or self.state != "OPEN"
                or time.monotonic() - self.last_flush >= cadence
            ):
                try:
                    self.flush()
                    self._force_flush = False
                    self._flush_complete.set()
                except OSError:
                    self.dropped += 1
                    self._fail("ERROR", "Background durable flush failed")
                    return
            if self._flush_stop.is_set() or self.state != "OPEN":
                return

    def enqueue(self, kind, data, *, thread):
        """Capture callbacks enqueue immutable metadata, never encode or fsync."""
        if (
            self._fork_disabled
            or self._buffer_failed
            or self.state != "OPEN"
            or os.getpid() != self._pid
        ):
            return False
        # Acceptance order is timestamp order, including a sched_getcpu call
        # that may release the GIL. No encoding, fsync or journal I/O occurs
        # under this lock; optional CPU observation adds serialized overhead.
        wake = False
        self._queue_lock.acquire()
        try:
            if not self._accepting_frames or self.state != "OPEN":
                return False
            if len(self._frame_queue) >= 65536:
                self.dropped += 1
                self._buffer_failed = True
                self.warnings.append(
                    "Trace callback buffer reached 65,536 events; recording stopped with an incomplete prefix"
                )
                wake = True
                accepted = False
            else:
                stamp = time.monotonic_ns()
                observation = (
                    self._cpu_placement.observe()
                    if self._cpu_placement is not None
                    else None
                )
                self._frame_queue.append((kind, data, thread, stamp, observation))
                wake = len(self._frame_queue) >= self.flush_events
                accepted = True
        finally:
            self._queue_lock.release()
        if wake:
            self._flush_wake.set()
        return accepted

    def defer(self, callback):
        """Schedule optional diagnostic I/O outside an application's audit hook."""
        if self._flush_thread is None:
            return
        if len(self._deferred) < self._deferred.maxlen:
            self._deferred.append(callback)
            self._flush_wake.set()

    def _drain_frames(self):
        with _internal_callbacks(), self.lock:
            if self._drain_owner == threading.get_ident():
                return
            with self._queue_lock:
                pending, self._frame_queue = self._frame_queue, deque()
            self._drain_pending(pending)

    def _drain_pending(self, pending):
        previous, self._drain_owner = self._drain_owner, threading.get_ident()
        try:
            self._write_frames(pending)
        finally:
            self._drain_owner = previous

    def _write_frames(self, pending):
        for index, (kind, data, thread, stamp, observation) in enumerate(pending):
            if self.state != "OPEN":
                self.dropped += len(pending) - index
                break
            try:
                if kind in {"call", "resume", "return", "yield", "unwind"}:
                    key = (data["frame"], data["function"], data["file"], data["line"])
                    code_id = self._codes.get(key)
                    if code_id is None:
                        size = len(protocol.encoded(data))
                        if (
                            len(self._codes) >= 65536
                            or self._code_bytes + size > 16 << 20
                        ):
                            self.dropped += len(pending) - index
                            self._buffer_failed = True
                            self.warnings.append(
                                "Trace code dictionary reached its bounded capacity; recording stopped"
                            )
                            break
                        code_id = len(self._codes) + 1
                        self._codes[key] = code_id
                        self._code_bytes += size
                        self.emit(
                            "code",
                            {"id": code_id, **data},
                            thread=thread,
                            mono_ns=stamp,
                        )
                    data = {"code": code_id}
                if not self.emit(
                    kind, data, thread=thread, mono_ns=stamp, _observation=observation
                ):
                    self.dropped += len(pending) - index - 1
                    break
            except (
                ValueError,
                TypeError,
                KeyError,
                OverflowError,
                RecursionError,
            ) as error:
                # Accepted callback metadata is an instrumentation dependency,
                # not a direct caller argument. Its fault must not escape into
                # a later valid scientific span that happens to drain it.
                self.dropped += len(pending) - index
                with self._queue_lock:
                    self._accepting_frames = False
                    self.dropped += len(self._frame_queue)
                    self._frame_queue.clear()
                self._fail(
                    "ERROR",
                    "Queued trace metadata failed validation: "
                    + type(error).__name__
                    + "; verified prefix retained",
                )
                break
        if self._buffer_failed and self.state == "OPEN":
            self.state = "LIMIT"

    def _after_fork(self):
        # Never touch a lock held by another pre-fork thread or parent journal.
        self._fork_disabled = True
        self.lock = threading.RLock()
        self._queue_lock = threading.Lock()
        self._frame_queue = deque()
        self._accepting_frames = False
        self._drain_owner = None
        self._deferred = deque(maxlen=128)
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _publish(self):
        format_fields = (
            {
                "journal_format": protocol.BLOCK_FORMAT,
                "block_size": self.flush_events,
                "max_block_bytes": protocol.MAX_BLOCK_BYTES,
            }
            if self.journal_format == "blocks"
            else {}
        )
        process = {
            key: self.identity[key]
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
            if key in self.identity
        }
        _atomic_json(
            self.path,
            {
                "schema": protocol.SCHEMA,
                "identity": self.identity,
                **format_fields,
                **(
                    {"frame_encoding": "code-dictionary/v1"}
                    if self._async_frames
                    else {}
                ),
                "process": process,
                "journal": "events.jsonl",
                "coverage": self.coverage,
                "state": self.state,
                "committed_seq": self.committed_seq,
                "committed_bytes": self.committed_bytes,
                "dropped": self.dropped,
                "warnings": self.warnings[-16:],
                "updated_ns": time.time_ns(),
                "durability": "Events through committed_seq were fsynced before this manifest was published",
            },
            replace=True,
        )

    def emit(self, kind, data=None, *, thread=None, mono_ns=None, _observation=None):
        if self._drain_owner == threading.get_ident():
            if mono_ns is None:
                # A caller re-entering its reservation callback cannot insert
                # a new present-time event in the middle of an older batch.
                return self.enqueue(
                    kind, data or {}, thread=str(thread or threading.get_ident())
                )
            return self._emit_ordered(
                kind, data, thread=thread, mono_ns=mono_ns, _observation=_observation
            )
        with _internal_callbacks():
            return self._emit_ordered(
                kind, data, thread=thread, mono_ns=mono_ns, _observation=_observation
            )

    def _emit_ordered(
        self, kind, data=None, *, thread=None, mono_ns=None, _observation=None
    ):
        if self._fork_disabled or os.getpid() != self._pid:
            return False
        with self.lock:
            if self.state != "OPEN" or self.fd is None:
                return False
            event = protocol.validate_event(
                {
                    "kind": kind,
                    "mono_ns": mono_ns if mono_ns is not None else 0,
                    "thread": str(thread or threading.get_ident()),
                    "data": data or {},
                },
                check_size=False,
            )
            if "cpu_observation" in event["data"]:
                raise ValueError(
                    "cpu_observation is reserved for the local CPU placement observer"
                )
            # Historical/native-emitter events must never inherit the reader's
            # CPU. PythonCapture registers its reuse-safe identity in the
            # emitter thread itself; another thread cannot borrow that alias.
            observe = (
                self._cpu_placement is not None
                and mono_ns is None
                and len(event["data"]) < 64
                and (
                    thread is None
                    or str(thread)
                    in (
                        str(threading.get_ident()),
                        getattr(self._emitter, "identity", None),
                    )
                )
            )
            if self._drain_owner != threading.get_ident():
                # Atomically reserve a current event's position after every
                # already accepted callback. Later callbacks receive later
                # timestamps while this writer encodes the detached prefix.
                with self._queue_lock:
                    if mono_ns is None:
                        event["mono_ns"] = time.monotonic_ns()
                    observation = (
                        self._cpu_placement.observe() if observe else _observation
                    )
                    pending, self._frame_queue = self._frame_queue, deque()
                self._drain_pending(pending)
                if self.state != "OPEN":
                    return False
            else:
                observation = _observation
            if observation is not None:
                event["data"] = dict(event["data"], cpu_observation=observation)
            if self.journal_format == "blocks":
                return self._emit_block_event(event)
            record = {
                "schema": "raven.trace-event/v1",
                "stream": self.stream,
                "seq": self.sequence + 1,
                "prev": self.previous,
                "event": event,
            }
            raw, digest = protocol.encode_event_record(record)
            if len(raw) > protocol.MAX_EVENT:
                raise ValueError("Trace event envelope exceeds 64 KiB")
            if self.bytes + len(raw) > self.max_bytes:
                self.dropped += 1
                self._fail(
                    "LIMIT",
                    "Storage budget reached; recording stopped, application continues",
                )
                return False
            try:
                if self.reserve_bytes is not None:
                    try:
                        reserved = self.reserve_bytes(len(raw))
                    except Exception as exc:
                        self.dropped += 1
                        self._fail(
                            "ERROR",
                            f"Shared run storage reservation failed: {type(exc).__name__}",
                        )
                        return False
                    if not reserved:
                        self.dropped += 1
                        self._fail(
                            "LIMIT",
                            "Shared run storage budget reached; recording stopped, application continues",
                        )
                        return False
                view = memoryview(raw)
                while view:
                    written = os.write(self.fd, view)
                    if written <= 0:
                        raise OSError("Trace journal write made no progress")
                    view = view[written:]
                self.bytes += len(raw)
                self.sequence += 1
                self.previous = digest
                if not self._async_frames and (
                    self.sequence - self.committed_seq >= self.flush_events
                    or time.monotonic() - self.last_flush >= self.flush_interval
                ):
                    self.flush()
                return True
            except OSError as exc:
                self.dropped += 1
                self._fail("ERROR", f"Trace storage failed: {type(exc).__name__}")
                return False

    def _emit_block_event(self, event):
        raw = (
            protocol.encoded(
                {
                    "schema": protocol.EVENT_FORMAT,
                    "stream": self.stream,
                    "seq": self.sequence + 1,
                    "event": event,
                }
            )
            + b"\n"
        )
        if len(raw) > protocol.MAX_EVENT:
            raise ValueError("Trace event envelope exceeds 64 KiB")
        try:
            if self._pending_bytes + len(raw) > protocol.MAX_BLOCK_BYTES:
                if self._async_frames:
                    self._write_block()
                else:
                    self.flush()
                if self.state != "OPEN":
                    self.dropped += 1
                    return False
            # Reserve the bounded header before accepting an event. Budget
            # exhaustion cannot leave a committed checkpoint inside a block.
            if (
                self.bytes + self._pending_bytes + len(raw) + protocol.MAX_BLOCK_HEADER
                > self.max_bytes
            ):
                self.dropped += 1
                self._fail(
                    "LIMIT",
                    "Storage budget reached; recording stopped, application continues",
                )
                return False
            self._pending.append(raw)
            self._pending_bytes += len(raw)
            self.sequence += 1
            if len(self._pending) >= self.flush_events or (
                not self._async_frames
                and time.monotonic() - self.last_flush >= self.flush_interval
            ):
                if self._async_frames:
                    self._write_block()
                else:
                    self.flush()
            return self.state == "OPEN"
        except OSError as exc:
            self._fail("ERROR", f"Trace storage failed: {type(exc).__name__}")
            return False

    def _write_block(self):
        if not self._pending or self._storage_failed:
            return
        header, digest = protocol.encode_block(
            self.stream, self._written_seq + 1, self.previous, self._pending
        )
        try:
            reserved = self.reserve_bytes is None or self.reserve_bytes(
                len(header) + self._pending_bytes
            )
        except Exception as exc:
            # A reservation service is an instrumentation dependency. Its
            # failure may stop capture, never the scientific application.
            self.state = "ERROR"
            self.warnings.append(
                f"Shared run storage reservation failed: {type(exc).__name__}"
            )
            reserved = False
        if not reserved:
            if self.state != "ERROR":
                self.state = "LIMIT"
                self.warnings.append(
                    "Shared run storage budget reached; recording stopped, application continues"
                )
            self.dropped += len(self._pending)
            self._pending.clear()
            self._pending_bytes = 0
            return
        # POSIX IOV_MAX is commonly 1024; coalesce only oversized batches.
        vectors = [header, *self._pending]
        maximum = os.sysconf("SC_IOV_MAX") if hasattr(os, "writev") else 1
        if len(vectors) > maximum:
            vectors = [b"".join(vectors)]
        views = [memoryview(raw) for raw in vectors]
        try:
            while views:
                written = (
                    os.writev(self.fd, views)
                    if hasattr(os, "writev")
                    else os.write(self.fd, views[0])
                )
                if written <= 0:
                    raise OSError("Trace journal write made no progress")
                while views and written >= len(views[0]):
                    written -= len(views.pop(0))
                if views and written:
                    views[0] = views[0][written:]
        except OSError:
            # A partial block must remain an uncommitted tail. Never retry it
            # at the advanced descriptor or certify its accepted sequences.
            self._storage_failed = True
            self.dropped += len(self._pending)
            self._pending.clear()
            self._pending_bytes = 0
            raise
        self.bytes += len(header) + self._pending_bytes
        self._written_seq = self.sequence
        self.previous = digest
        self._pending.clear()
        self._pending_bytes = 0

    def _fail(self, state, message):
        with self.lock:
            self.state = state
            self.warnings.append(message)
            try:
                self.flush()
            except OSError:
                pass

    def flush(self):
        if self._drain_owner == threading.get_ident():
            return self._flush_ordered()
        with _internal_callbacks():
            return self._flush_ordered()

    def _flush_ordered(self):
        if self._fork_disabled or self.fd is None:
            return
        if (
            self._async_frames
            and self._flush_thread is not None
            and self._flush_thread.is_alive()
            and threading.current_thread() is not self._flush_thread
            and self._drain_owner != threading.get_ident()
        ):
            self._flush_complete.clear()
            self._force_flush = True
            self._flush_wake.set()
            self._flush_complete.wait(2.0)
            return
        self._drain_frames()
        with self.lock:
            if self.journal_format == "blocks":
                self._write_block()
            os.fsync(self.fd)
            self.committed_seq = (
                self._written_seq if self.journal_format == "blocks" else self.sequence
            )
            self.committed_bytes = self.bytes
            self.last_flush = time.monotonic()
            self._publish()

    def close(self):
        with _internal_callbacks():
            return self._close_ordered()

    def _close_ordered(self):
        if self._fork_disabled or self.fd is None:
            return
        self._flush_stop.set()
        self._flush_wake.set()
        if (
            self._flush_thread is not None
            and threading.current_thread() is not self._flush_thread
        ):
            self._flush_thread.join(timeout=max(1, self.flush_interval + 0.1))
            if self._flush_thread.is_alive():
                self.state = "PARTIAL"
                self.warnings.append(
                    "Trace writer did not drain before shutdown; its tail remains uncommitted"
                )
                return
        with self.lock:
            try:
                # Close admission atomically, then drain the entire accepted
                # prefix while OPEN. Callbacks cannot append behind stream_end.
                with self._queue_lock:
                    self._accepting_frames = False
                    pending, self._frame_queue = self._frame_queue, deque()
                self._drain_pending(pending)
                if self.state == "OPEN":
                    self.emit("stream_end", {"reason": "clean_close"})
                    if self.state == "OPEN":
                        self.state = "CLOSED"
                self.flush()
            finally:
                os.close(self.fd)
                self.fd = None
                _RECORDERS.discard(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def span(self, name, *, span_id=None, parent_id=None):
        span_id = span_id or uuid.uuid4().hex
        data = {"name": name, "span_id": span_id}
        if parent_id is not None:
            data["parent_id"] = parent_id
        self.emit("span_start", data)
        try:
            yield f"{self.stream}:{span_id}"
        finally:
            self.emit("span_end", {"span_id": span_id})

    def link(self, source, target, *, relation="precedes"):
        return self.emit(
            "link", {"source": source, "target": target, "relation": relation}
        )


class PythonCapture:
    """CPython monitoring, with an explicitly less capable legacy profile mode."""

    def __init__(self, recorder, *, legacy=False, scope="all", roots=()):
        if scope not in {"project", "all"}:
            raise ValueError("Python trace scope must be project or all")
        self.recorder = recorder
        self.legacy = legacy
        self.scope = scope
        self.roots = tuple(
            str(Path(root).resolve()) + os.sep
            for root in (roots or (recorder.project,))
        )
        self._scope_cache = {}
        self.local = threading.local()
        self.tool_id = None
        self.previous_profile = None
        self.previous_thread_profile = None
        self.threads = {}
        self.callbacks = {}
        self.excluded = {
            str(Path(__file__).resolve()),
            str(Path(protocol.__file__).resolve()),
            str(Path(__file__).with_name("trace_cpu.py").resolve()),
        }
        self.raven_prefix = str(Path(__file__).resolve().parent) + os.sep
        self._stopped = False
        self._metadata = {}
        self._metadata_lock = threading.RLock()
        import sysconfig

        self.excluded_prefixes = tuple(
            dict.fromkeys(
                os.path.abspath(path) + os.sep
                for path in (
                    sys.prefix,
                    sys.base_prefix,
                    sys.exec_prefix,
                    *sysconfig.get_paths().values(),
                )
                if isinstance(path, str) and path
            )
        )
        # A project may add a second conda/venv library to sys.path. Discover
        # those environment roots once before callbacks start; never stat
        # ancestors on an application's call/return path.
        environment_roots = set()
        checks = 0
        deadline = time.monotonic() + 0.01
        for entry in sys.path[:128]:
            if not isinstance(entry, str):
                continue
            path = os.path.abspath(entry or os.getcwd())
            if not any(path.startswith(root) for root in self.roots):
                continue
            for _ in range(16):
                if checks >= 128 or time.monotonic() >= deadline:
                    break
                checks += 1
                try:
                    if os.path.isfile(
                        os.path.join(path, "pyvenv.cfg")
                    ) or os.path.isdir(os.path.join(path, "conda-meta")):
                        environment_roots.add(path + os.sep)
                        break
                except OSError:
                    break
                parent = os.path.dirname(path)
                if parent == path or not any(
                    (parent + os.sep).startswith(root) for root in self.roots
                ):
                    break
                path = parent
        self.excluded_prefixes = (*self.excluded_prefixes, *sorted(environment_roots))
        _CAPTURES.add(self)

    def _after_fork(self):
        # Weakref cleanup and explicit stop may run before a fresh child
        # attachment. Never acquire a cache lock inherited from another thread.
        self._stopped = True
        self._metadata_lock = threading.RLock()
        self.local = threading.local()

    def _included(self, code):
        """A bounded, weak code cache keeps legacy filtering cheap and stable."""
        if code.co_filename in self.excluded or code.co_filename.startswith(
            self.raven_prefix
        ):
            return False
        key = id(code)
        cached = self._scope_cache.get(key)
        if cached is not None and cached[0]() is code:
            return cached[1]
        filename = code.co_filename
        allowed = filename not in self.excluded
        if allowed and self.scope == "project":
            if filename.startswith("<"):
                allowed = filename in {"<string>", "<stdin>"}
            else:
                path = os.path.abspath(filename)
                raven_root = str(Path(__file__).resolve().parent) + os.sep
                allowed = (
                    not path.startswith(raven_root)
                    and "site-packages" not in Path(path).parts
                    and "dist-packages" not in Path(path).parts
                    and not path.startswith(self.excluded_prefixes)
                    and any(path.startswith(root) for root in self.roots)
                )
        owner = weakref.ref(self)

        def discard(reference):
            capture = owner()
            if capture is not None and capture._metadata_lock.acquire(blocking=False):
                try:
                    current = capture._scope_cache.get(key)
                    if current is not None and current[0] is reference:
                        del capture._scope_cache[key]
                finally:
                    capture._metadata_lock.release()

        if not self._metadata_lock.acquire(blocking=False):
            return allowed
        try:
            if len(self._scope_cache) >= 1024:
                del self._scope_cache[next(iter(self._scope_cache))]
            self._scope_cache[key] = (weakref.ref(code, discard), allowed)
        finally:
            self._metadata_lock.release()
        return allowed

    def _code_metadata(self, code):
        """Bounded immutable-code metadata without keeping dynamic code alive."""
        key = id(code)
        cached = self._metadata.get(key)
        if cached is not None and cached[0]() is code:
            return cached[1]
        data = {
            "function": getattr(code, "co_qualname", code.co_name),
            "file": code.co_filename,
            "line": code.co_firstlineno,
            "frame": f"{code.co_filename}:{code.co_firstlineno}:{code.co_name}",
        }
        owner = weakref.ref(self)

        def discard(reference):
            capture = owner()
            if capture is not None and capture._metadata_lock.acquire(blocking=False):
                try:
                    current = capture._metadata.get(key)
                    if current is not None and current[0] is reference:
                        del capture._metadata[key]
                finally:
                    capture._metadata_lock.release()

        if not self._metadata_lock.acquire(blocking=False):
            return data
        try:
            if len(self._metadata) >= 1024:
                del self._metadata[next(iter(self._metadata))]
            self._metadata[key] = (weakref.ref(code, discard), data)
        finally:
            self._metadata_lock.release()
        return data

    def _thread_identity(self, *, queued=False):
        """Reuse one lifetime-safe thread label across calls and metric points."""
        ident = threading.get_ident()
        # current_thread() can manufacture DummyThreads during teardown.
        current = getattr(threading, "_active", {}).get(ident)
        thread = getattr(self.local, "identity", None)
        if thread is None:
            thread = f"{ident}:{uuid.uuid4().hex[:12]}"
            self.local.identity = thread
            self.recorder._emitter.identity = thread
        if thread not in self.threads:
            name = getattr(current, "name", f"thread-{ident}")
            self.threads[thread] = name
            writer = (
                self.recorder.enqueue
                if queued or self.recorder._async_frames
                else self.recorder.emit
            )
            writer(
                "thread_start",
                {"name": name, "native_id": threading.get_native_id()},
                thread=thread,
            )
        return thread

    def _event(self, kind, code):
        if (
            getattr(self.local, "busy", False)
            or self._stopped
            or self.recorder._fork_disabled
            or self.recorder.state != "OPEN"
            or os.getpid() != self.recorder._pid
        ):
            return
        self.local.busy = True
        try:
            if not self._included(code):
                return
            ident = threading.get_ident()
            flush_thread = getattr(self.recorder, "_flush_thread", None)
            if flush_thread is not None and ident == flush_thread.ident:
                return
            thread = self._thread_identity()
            writer = (
                self.recorder.enqueue
                if self.recorder._async_frames
                else self.recorder.emit
            )
            writer(kind, self._code_metadata(code), thread=thread)
        except Exception as exc:
            # Instrumentation must not inject errors into the research program.
            self._stopped = True
            message = f"Python capture stopped: {type(exc).__name__}"
            self.recorder.defer(lambda: self.recorder._fail("ERROR", message))
        finally:
            self.local.busy = False

    def start(self):
        if self.recorder._flush_thread is not None:
            self.recorder._async_frames = True
        monitoring = getattr(sys, "monitoring", None)
        if monitoring is None and not self.legacy:
            raise ValueError(
                "Python capture requires CPython 3.12+; --legacy-profile explicitly accepts 3.10/3.11 coverage limits"
            )
        if monitoring is not None and not self.legacy:
            for candidate in (5, 4, 3, 2, 1, 0):
                if monitoring.get_tool(candidate) is None:
                    try:
                        monitoring.use_tool_id(candidate, "raven-trace")
                        self.tool_id = candidate
                        break
                    except ValueError:
                        continue
            if self.tool_id is None:
                raise ValueError(
                    "All Python monitoring tool IDs are in use; existing profilers were not replaced"
                )
            events = monitoring.events
            mask = 0
            for name, kind in (
                ("PY_START", "call"),
                ("PY_RESUME", "resume"),
                ("PY_RETURN", "return"),
                ("PY_YIELD", "yield"),
                ("PY_THROW", "resume"),
                ("PY_UNWIND", "unwind"),
            ):
                event_id = getattr(events, name)

                def callback(
                    code,
                    offset,
                    *args,
                    _kind=kind,
                    _disable=name in {"PY_START", "PY_RESUME", "PY_RETURN", "PY_YIELD"},
                ):
                    # DISABLE is per code/location across threads, so only use
                    # it for permanently excluded code and local events. Never
                    # disable on a transient recursion or flush-thread guard;
                    # PY_THROW/PY_UNWIND do not permit this return value.
                    try:
                        if (
                            self._stopped
                            or self.recorder._fork_disabled
                            or self.recorder.state != "OPEN"
                            or os.getpid() != self.recorder._pid
                        ):
                            return monitoring.DISABLE if _disable else None
                        if _disable and not self._included(code):
                            return monitoring.DISABLE
                        self._event(_kind, code)
                    except Exception:
                        self._stopped = True

                self.callbacks[event_id] = callback
                monitoring.register_callback(self.tool_id, event_id, callback)
                mask |= event_id
            monitoring.set_events(self.tool_id, mask)
            # CPython keeps DISABLE locations when a tool ID is freed. A later
            # capture with a wider scope must not inherit the former filter.
            monitoring.restart_events()
        else:
            self.previous_profile = sys.getprofile()
            self.previous_thread_profile = (
                threading.getprofile() if hasattr(threading, "getprofile") else None
            )
            if (
                self.previous_profile is not None
                or self.previous_thread_profile is not None
            ):
                raise ValueError(
                    "Existing Python profiler detected; Raven will not replace it"
                )

            def profile(frame, event, arg):
                if event in {"call", "return"}:
                    self._event(event, frame.f_code)

            self.profile = profile
            threading.setprofile(profile)
            sys.setprofile(profile)
        try:
            from .metrics_overlay import register

            if not register(self):
                self.recorder.coverage["scientific_metrics"] = {
                    "status": "unavailable",
                    "reason": "Scientific overlay registration budget reached",
                }
        except Exception:  # noqa: BLE001 - optional hook cannot invalidate scientific execution
            # Optional metric integration cannot invalidate Python capture.
            self.recorder.coverage["scientific_metrics"] = {
                "status": "unavailable",
                "reason": "Scientific overlay hook could not be registered",
            }
        return self

    def stop(self):
        self._stopped = True
        overlay = sys.modules.get(__package__ + ".metrics_overlay")
        if overlay is not None:
            try:
                overlay.unregister(self)
            except Exception:  # noqa: BLE001 - teardown must still free the tracing tool
                self.recorder.coverage["scientific_metrics"] = {
                    "status": "stopped",
                    "reason": "Overlay cleanup failed; stopped/closed ownership is independently checked",
                }
        if self.tool_id is not None:
            monitoring = sys.monitoring
            monitoring.set_events(self.tool_id, 0)
            for event_id in self.callbacks:
                monitoring.register_callback(self.tool_id, event_id, None)
            monitoring.free_tool_id(self.tool_id)
            self.tool_id = None
        elif hasattr(self, "profile"):
            if sys.getprofile() is self.profile:
                sys.setprofile(self.previous_profile)
            if (
                not hasattr(threading, "getprofile")
                or threading.getprofile() is self.profile
            ):
                threading.setprofile(self.previous_thread_profile)
