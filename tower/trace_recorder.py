"""Opt-in, user-owned event recording and a Python launch adapter.

Recording is independent of Tower's UI. Each process owns one journal. Flush
checkpoints bound durability; an incomplete tail is never described as lossless.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import runpy
import socket
import stat
import sys
import threading
import time
import uuid
import weakref

from . import trace_protocol as protocol
from .reporting import _atomic_json, _directory

_RECORDERS = weakref.WeakSet()


def _fork_child():
    # Register one process-wide callback; completed snapshots must not leak
    # recorder instances through os.register_at_fork's permanent callback list.
    for recorder in list(_RECORDERS):
        recorder._after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_fork_child)


def _process_start(pid=None):
    try:
        # comm may contain spaces or parentheses; fields after its final ')'.
        return Path(f"/proc/{pid or os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return "unknown"


def _boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def default_job_id():
    parent, task = os.environ.get("SLURM_ARRAY_JOB_ID", ""), os.environ.get("SLURM_ARRAY_TASK_ID", "")
    return parent + "_" + task if parent.isdecimal() and task.isdecimal() else os.environ.get("SLURM_JOB_ID", "local")


def _safe_directory(path):
    path = Path(path).absolute()
    _directory(path)
    return path


def _trace_root(project):
    root = _safe_directory(project)
    for name in (".tower", "traces"):
        root /= name
        root.mkdir(mode=0o700, exist_ok=True)
        _directory(root)
    return root


def register_trace(project, manifest_path):
    project = _safe_directory(project)
    trace_root = _trace_root(project)
    manifest_path = Path(manifest_path).absolute()
    try:
        relative = str(manifest_path.relative_to(project))
    except ValueError as exc:
        raise ValueError("Registered trace must be inside its project") from exc
    manifest = protocol.read_json(manifest_path)
    identity = protocol.validate_identity(manifest.get("identity"))
    if manifest.get("schema") != protocol.SCHEMA:
        raise ValueError("Expected trace manifest")
    lock_fd = os.open(trace_root / ".index.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise ValueError("Trace index lock must be a regular file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        index = trace_root / "index.json"
        doc = protocol.read_json(index) if index.exists() else {"schema": "tower.trace-index/v1", "records": []}
        if doc.get("schema") != "tower.trace-index/v1" or not isinstance(doc.get("records"), list):
            raise ValueError("Invalid trace registry")
        records = [entry for entry in doc["records"] if entry.get("stream") != identity["stream"]]
        if len(records) >= 4096:
            raise ValueError("Trace registry is full (4096 streams); archive old registrations explicitly")
        records.append({**identity, "path": relative})
        _atomic_json(index, {"schema": "tower.trace-index/v1", "records": records}, replace=True)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


class Recorder:
    """Cooperative API usable from Python and language-specific adapters.

    `emit` records metadata, never locals or application payloads. On a storage
    or budget failure recording stops and marks incomplete; the application is
    not killed. This policy is explicit, not a strict lossless guarantee.
    """
    def __init__(self, project, *, job_id=None, attempt=None, cluster=None,
                 coverage=None, max_bytes=64 << 20, flush_events=64,
                 flush_interval=0.05, identity_extra=None, stream_id=None, initial_mono_ns=None,
                 background_flush=True):
        if not 4096 <= max_bytes <= 1 << 40 or not 1 <= flush_events <= 65536 or not 0 <= flush_interval <= 60:
            raise ValueError("Invalid recorder storage or flush budget")
        self.project = _safe_directory(project)
        self._pid = os.getpid()
        self.lock = threading.RLock()
        self._fork_disabled = False
        self.stream = stream_id or uuid.uuid4().hex
        self.identity = protocol.validate_identity({
            "job_id": str(job_id or default_job_id()),
            "attempt": str(attempt or os.environ.get("TOWER_RUN_ID") or "unverified-" + self.stream),
            "cluster": cluster or os.environ.get("SLURM_CLUSTER_NAME") or "local",
            "node": socket.gethostname(), "boot_id": _boot_id(), "pid": self._pid,
            "process_start": _process_start(), "stream": self.stream,
            **(identity_extra or {})})
        self.directory = _trace_root(self.project) / self.stream
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / "manifest.json"
        self.fd = os.open(self.directory / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.sequence = self.bytes = self.committed_seq = self.committed_bytes = self.dropped = 0
        self.previous = "0" * 64
        self.max_bytes, self.flush_events, self.flush_interval = max_bytes, flush_events, flush_interval
        self.last_flush = time.monotonic()
        self.state = "OPEN"
        self.warnings = []
        self.coverage = coverage or {"adapter": "cooperative", "scope": "Explicit events submitted by this process; uninstrumented code is unknown"}
        try:
            self._publish()
            register_trace(self.project, self.path)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise
        _RECORDERS.add(self)
        self.emit("stream_start", {"coverage": self.coverage}, mono_ns=initial_mono_ns)
        self._flush_stop = threading.Event()
        self._flush_thread = threading.Thread(target=self._flush_loop, name="TowerTraceFlush", daemon=True) if background_flush else None
        if self._flush_thread is not None:
            self._flush_thread.start()

    def _flush_loop(self):
        while not self._flush_stop.wait(max(0.01, self.flush_interval)):
            with self.lock:
                if self.fd is None or self.state != "OPEN":
                    return
                if self.sequence != self.committed_seq:
                    try:
                        self.flush()
                    except OSError:
                        self.dropped += 1
                        self._fail("ERROR", "Background durable flush failed")
                        return

    def _after_fork(self):
        # Never touch a lock held by another pre-fork thread or parent journal.
        self._fork_disabled = True
        self.lock = threading.RLock()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _publish(self):
        _atomic_json(self.path, {"schema": protocol.SCHEMA, "identity": self.identity,
            "journal": "events.jsonl", "coverage": self.coverage, "state": self.state,
            "committed_seq": self.committed_seq, "committed_bytes": self.committed_bytes,
            "dropped": self.dropped, "warnings": self.warnings[-16:], "updated_ns": time.time_ns(),
            "durability": "Events through committed_seq were fsynced before this manifest was published"}, replace=True)

    def emit(self, kind, data=None, *, thread=None, mono_ns=None):
        if self._fork_disabled or os.getpid() != self._pid:
            return False
        with self.lock:
            if self.state != "OPEN" or self.fd is None:
                return False
            event = protocol.validate_event({"kind": kind, "mono_ns": mono_ns if mono_ns is not None else time.monotonic_ns(),
                "thread": str(thread or threading.get_ident()), "data": data or {}}, check_size=False)
            record = {"schema": "tower.trace-event/v1", "stream": self.stream, "seq": self.sequence + 1,
                      "prev": self.previous, "event": event}
            record["sha256"] = protocol.event_digest(record)
            raw = protocol.encoded(record) + b"\n"
            if len(raw) > protocol.MAX_EVENT:
                raise ValueError("Trace event envelope exceeds 64 KiB")
            if self.bytes + len(raw) > self.max_bytes:
                self.dropped += 1
                self._fail("LIMIT", "Storage budget reached; recording stopped, application continues")
                return False
            try:
                view = memoryview(raw)
                while view:
                    written = os.write(self.fd, view)
                    if written <= 0:
                        raise OSError("Trace journal write made no progress")
                    view = view[written:]
                self.bytes += len(raw)
                self.sequence += 1
                self.previous = record["sha256"]
                if self.sequence - self.committed_seq >= self.flush_events or time.monotonic() - self.last_flush >= self.flush_interval:
                    self.flush()
                return True
            except OSError as exc:
                self.dropped += 1
                self._fail("ERROR", f"Trace storage failed: {type(exc).__name__}")
                return False

    def _fail(self, state, message):
        with self.lock:
            self.state = state
            self.warnings.append(message)
            try:
                self.flush()
            except OSError:
                pass

    def flush(self):
        if self._fork_disabled or self.fd is None:
            return
        with self.lock:
            os.fsync(self.fd)
            self.committed_seq, self.committed_bytes = self.sequence, self.bytes
            self.last_flush = time.monotonic()
            self._publish()

    def close(self):
        if self._fork_disabled or self.fd is None:
            return
        self._flush_stop.set()
        if self._flush_thread is not None and threading.current_thread() is not self._flush_thread:
            self._flush_thread.join(timeout=max(1, self.flush_interval + 0.1))
        with self.lock:
            try:
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
        return self.emit("link", {"source": source, "target": target, "relation": relation})


class PythonCapture:
    """CPython monitoring, with an explicitly less capable legacy profile mode."""
    def __init__(self, recorder, *, legacy=False):
        self.recorder = recorder
        self.legacy = legacy
        self.local = threading.local()
        self.tool_id = None
        self.previous_profile = None
        self.previous_thread_profile = None
        self.threads = {}
        self.callbacks = {}
        self.excluded = {str(Path(__file__).resolve()), str(Path(protocol.__file__).resolve())}
        self._stopped = False

    def _event(self, kind, code):
        if getattr(self.local, "busy", False) or self._stopped or self.recorder._fork_disabled or code.co_filename in self.excluded:
            return
        self.local.busy = True
        try:
            ident = threading.get_ident()
            flush_thread = getattr(self.recorder, "_flush_thread", None)
            if flush_thread is not None and ident == flush_thread.ident:
                return
            # current_thread() manufactures persistent DummyThreads while
            # CPython tears a thread down. Monitoring must not mutate that
            # registry merely to label a callback.
            current = getattr(threading, "_active", {}).get(ident)
            thread = getattr(self.local, "identity", None)
            if thread is None:
                thread = f"{ident}:{uuid.uuid4().hex[:12]}"
                self.local.identity = thread
            if thread not in self.threads:
                name = getattr(current, "name", f"thread-{ident}")
                self.threads[thread] = name
                self.recorder.emit("thread_start", {"name": name,
                    "native_id": threading.get_native_id()}, thread=thread)
            self.recorder.emit(kind, {"function": getattr(code, "co_qualname", code.co_name),
                "file": code.co_filename, "line": code.co_firstlineno,
                "frame": f"{code.co_filename}:{code.co_firstlineno}:{code.co_name}"}, thread=thread)
        except Exception as exc:
            # Instrumentation must not inject errors into the research program.
            self.recorder._fail("ERROR", f"Python capture stopped: {type(exc).__name__}")
            self._stopped = True
        finally:
            self.local.busy = False

    def start(self):
        monitoring = getattr(sys, "monitoring", None)
        if monitoring is None and not self.legacy:
            raise ValueError("Python capture requires CPython 3.12+; --legacy-profile explicitly accepts 3.10/3.11 coverage limits")
        if monitoring is not None and not self.legacy:
            for candidate in (5, 4, 3, 2, 1, 0):
                if monitoring.get_tool(candidate) is None:
                    try:
                        monitoring.use_tool_id(candidate, "tower-trace")
                        self.tool_id = candidate
                        break
                    except ValueError:
                        continue
            if self.tool_id is None:
                raise ValueError("All Python monitoring tool IDs are in use; existing profilers were not replaced")
            events = monitoring.events
            mask = 0
            for name, kind in (("PY_START", "call"), ("PY_RESUME", "resume"), ("PY_RETURN", "return"),
                               ("PY_YIELD", "yield"), ("PY_THROW", "resume"), ("PY_UNWIND", "unwind")):
                event_id = getattr(events, name)
                def callback(code, offset, *args, _kind=kind):
                    self._event(_kind, code)
                self.callbacks[event_id] = callback
                monitoring.register_callback(self.tool_id, event_id, callback)
                mask |= event_id
            monitoring.set_events(self.tool_id, mask)
        else:
            self.previous_profile = sys.getprofile()
            self.previous_thread_profile = threading.getprofile() if hasattr(threading, "getprofile") else None
            if self.previous_profile is not None or self.previous_thread_profile is not None:
                raise ValueError("Existing Python profiler detected; Tower will not replace it")
            def profile(frame, event, arg):
                if event in {"call", "return"}:
                    self._event(event, frame.f_code)
            self.profile = profile
            threading.setprofile(profile)
            sys.setprofile(profile)
        return self

    def stop(self):
        self._stopped = True
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
            if not hasattr(threading, "getprofile") or threading.getprofile() is self.profile:
                threading.setprofile(self.previous_thread_profile)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv is not None and argv and argv[0] == "native":
        from .trace_native import main as native_main
        return native_main(argv[1:])
    if argv and argv[0] == "mpi":
        from .trace_mpi import main as mpi_main
        return mpi_main(argv[1:])
    parser = argparse.ArgumentParser(prog="tower trace", description="Opt-in event recording; no debugger attach or scheduler modification")
    sub = parser.add_subparsers(dest="action", required=True)
    launch = sub.add_parser("run", help="Record Python execution in a new process-owned journal")
    launch.add_argument("--project", default=os.getcwd())
    launch.add_argument("--job-id", default=default_job_id())
    launch.add_argument("--attempt", default=os.environ.get("TOWER_RUN_ID"))
    launch.add_argument("--cluster", default=os.environ.get("SLURM_CLUSTER_NAME", "local"))
    launch.add_argument("--slurm-submit")
    launch.add_argument("--slurm-start")
    launch.add_argument("--max-mib", type=int, default=64)
    launch.add_argument("--legacy-profile", action="store_true")
    launch.add_argument("script", nargs=argparse.REMAINDER)
    inspect = sub.add_parser("inspect", help="Read checked events and reconstructed stacks")
    inspect.add_argument("path")
    inspect.add_argument("--json", action="store_true")
    register = sub.add_parser("register", help="Register an existing project-contained trace manifest")
    register.add_argument("--project", required=True)
    register.add_argument("path")
    args = parser.parse_args(argv)
    if args.action == "inspect":
        value = protocol.read_trace(args.path)
        print(json.dumps(value, indent=2) if args.json else "\n".join(value["rows"] + value["warnings"]))
        return 0 if value["status"] != "INVALID" else 2
    if args.action == "register":
        register_trace(args.project, args.path)
        return 0
    script = list(args.script)
    if script and script[0] == "--":
        script.pop(0)
    if not script:
        parser.error("run requires -- SCRIPT.py [arguments]")
    target = Path(script[0]).absolute()
    if not target.is_file():
        parser.error("run requires an existing Python script")
    coverage = {"adapter": "python-profile" if args.legacy_profile else "python-sys-monitoring",
        "scope": "Python frames in this launched process and newly observed threads; native extensions, device execution and fork children are not covered",
        "limitations": "Legacy profile cannot distinguish yield from return or exception unwind" if args.legacy_profile else "Only declared Python frame events; pre-existing frames are unknown"}
    recorder = Recorder(args.project, job_id=args.job_id, attempt=args.attempt,
        cluster=args.cluster, max_bytes=args.max_mib << 20, coverage=coverage,
        identity_extra={key: value for key, value in {"slurm_submit": args.slurm_submit,
            "slurm_start": args.slurm_start, "run_id": os.environ.get("TOWER_RUN_ID")}.items() if value})
    capture = PythonCapture(recorder, legacy=args.legacy_profile)
    old_argv, old_path = sys.argv, list(sys.path)
    print(f"Tower trace: {recorder.path}", file=sys.stderr)
    started = False
    try:
        sys.argv = script
        sys.path.insert(0, str(target.parent))
        capture.start()
        started = True
        runpy.run_path(str(target), run_name="__main__")
    finally:
        capture.stop()
        if not started:
            recorder._fail("ERROR", "Python capture could not start; the application was not launched")
        remaining = [thread for thread in threading.enumerate()
                     if thread is not threading.current_thread() and thread is not recorder._flush_thread and thread.is_alive()]
        if remaining:
            recorder._fail("PARTIAL", "Python script returned while other threads remained alive; later execution was not captured")
        sys.argv, sys.path[:] = old_argv, old_path
        recorder.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
