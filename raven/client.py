"""Raven's vendorable, standard-library-only application metrics client.

Copy this file as ``raven.py`` to use the same API without installing Raven.
Reporting is opt-in through RAVEN_RUN_DIR, RAVEN_METRICS_FILE, or configure(path=).
Calls enqueue bounded data; only the daemon writer touches the filesystem. A
normal exit waits at most two seconds. Abrupt exit can lose unflushed data; a
blocked filesystem can extend the loss window beyond the usual one second.
"""
# Reporting boundaries deliberately contain arbitrary user scalar/callback errors;
# ValueError validation mirrors the established metrics stream contract.
# ruff: noqa: BLE001, S110, TRY004
from __future__ import annotations

import atexit
import dis
import functools
import inspect
import json
import math
import os
import stat
import sys
import threading
import time
from collections import deque
from collections.abc import Mapping
from contextvars import ContextVar
from enum import Enum

API_VERSION = 1
__raven_vendored__ = __name__ != "raven.client"
__all__ = ["Kind", "configure", "measure", "metric", "phase", "progress", "watch"]
MAX_METRICS = 64
MAX_POINTS = 10000
MAX_LINE_BYTES = 65536


class Kind(str, Enum):
    OBJECTIVE = "objective"
    RESIDUAL = "residual"
    THROUGHPUT = "throughput"
    PROGRESS = "progress"
    CONSERVED = "conserved"
    ERROR = "error"
    GAUGE = "gauge"


def _number(value, name, *, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ValueError(f"{name} must be a finite {'nonnegative ' if nonnegative else ''}number")
    return result


def _text(value, name, limit, *, empty=True):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value):
        raise ValueError(f"{name} must be a string of {'0' if empty else '1'} to {limit} characters")
    if value and not value.isprintable():
        raise ValueError(f"{name} must contain only printable characters")
    return value


def _record(value):
    """Intentionally mirrored from metrics._record; parity has property tests."""
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
    if "pid" in value:
        pid = value["pid"]
        if type(pid) is not int or not 1 <= pid < 1 << 63:
            raise ValueError("pid must be a positive 64-bit integer")
        rec["pid"] = pid
    if "progress" in value:
        progress_value = value["progress"]
        if not isinstance(progress_value, dict):
            raise ValueError("progress must be an object")
        completed = _number(progress_value.get("completed"), "progress.completed", nonnegative=True)
        total = _number(progress_value.get("total"), "progress.total", nonnegative=True)
        if total == 0 or completed > total:
            raise ValueError("progress must satisfy 0 <= completed <= total and total > 0")
        rec["progress"] = {
            "completed": completed, "total": total,
            "unit": _text(progress_value.get("unit", ""), "progress.unit", 64),
            "fraction": completed / total,
        }
    return rec


def _scalar(value):
    # Strings and bools are not scientific numbers, even if float() accepts them.
    if isinstance(value, (str, bytes, bool)):
        raise ValueError("unsupported metric value; expected one finite numeric scalar")
    if not isinstance(value, (int, float)):
        detach = getattr(value, "detach", None)
        if callable(detach):
            value = detach()
        item = getattr(value, "item", None)
        value = item() if callable(item) else float(value)
    return _number(value, "metric value")


def _encoded(value):
    payload = (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()
    if len(payload) > MAX_LINE_BYTES:
        raise ValueError("metric payload exceeds 65536 bytes")
    return payload


def _parent(path):
    """Anchor every directory component; neither parent nor leaf symlinks follow."""
    # Resolve the user-selected root once. Leaves remain no-follow and the
    # anchored descriptor prevents later ancestor replacement from redirecting
    # a writer. Cluster home/scratch aliases commonly contain symlinks.
    path = os.path.join(os.path.realpath(os.path.dirname(os.path.abspath(path))), os.path.basename(path))
    parts = path.split(os.sep)
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def _open(parent, name, *, create=False, append=False):
    flags = os.O_RDWR if create else os.O_RDONLY
    flags |= os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    if create:
        flags |= os.O_CREAT
    if append:
        flags |= os.O_APPEND
    fd = os.open(name, flags, 0o600, dir_fd=parent)
    current = os.fstat(fd)
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or current.st_uid != os.getuid():
        os.close(fd)
        raise ValueError("metrics destination must be an owned regular file with one link")
    return fd


def _write_all(fd, payload):
    while payload:
        try:
            size = os.write(fd, payload)
        except InterruptedError:
            continue
        if size <= 0:
            raise OSError("metrics write made no progress")
        payload = payload[size:]


def _lock(fd):
    import fcntl
    # A competing writer must not strand shutdown. Filesystem calls themselves
    # can still block in the kernel, which is why this work stays on a daemon.
    until = time.monotonic() + 1
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= until:
                raise TimeoutError("metrics writer lock is busy")
            time.sleep(.01)


def _unlock(fd):
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _append(path, records, *, directory_fd=None):
    parent, name = _parent(path) if directory_fd is None else (os.dup(directory_fd), os.path.basename(path))
    try:
        fd = _open(parent, name, create=True, append=True)
        try:
            _lock(fd)
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                _write_all(fd, b"\n")
            for record in records:
                _write_all(fd, _encoded(record))
        finally:
            _unlock(fd)
            os.close(fd)
    finally:
        os.close(parent)


def _json_update(path, update, *, directory_fd=None):
    """Bounded shared merge under an independent lock and atomic replacement."""
    parent, name = _parent(path) if directory_fd is None else (os.dup(directory_fd), os.path.basename(path))
    temporary = None
    lock_fd = None
    try:
        lock_fd = _open(parent, "." + name + ".lock", create=True)
        _lock(lock_fd)
        try:
            source = _open(parent, name)
        except FileNotFoundError:
            value = {}
        else:
            try:
                data = os.read(source, MAX_LINE_BYTES + 1)
                if len(data) > MAX_LINE_BYTES:
                    raise ValueError("metadata exceeds 65536 bytes")
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise ValueError("metadata must be an object")
            finally:
                os.close(source)
        payload = _encoded(update(value))
        temporary = "." + name + "." + str(os.getpid()) + "." + str(time.monotonic_ns())
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent)
        try:
            _write_all(fd, payload)
        finally:
            os.close(fd)
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=parent)
        if lock_fd is not None:
            _unlock(lock_fd)
            os.close(lock_fd)
        os.close(parent)


_phase = ContextVar("raven_metric_phase", default="")
_phase_scopes = ContextVar("raven_phase_scopes", default=())
_state = None
_state_lock = threading.RLock()
_options = {}
_registered = False
_fork_child = False
_revision = 0


def _environment_rank():
    # Launcher rank variables are more specific than an inherited outer srun
    # allocation's SLURM_PROCID (which can be zero in every mpirun child).
    invalid = ""
    for name in ("RANK", "OMPI_COMM_WORLD_RANK", "PMI_RANK", "SLURM_PROCID"):
        value = os.environ.get(name)
        if value is not None:
            try:
                rank = int(value)
                if rank < 0 or rank > (1 << 31) - 1:
                    raise ValueError()
                return rank, name
            except ValueError:
                # An empty generic RANK must not hide a valid MPI/Slurm rank.
                invalid = invalid or name
                continue
    return (None, "invalid " + invalid) if invalid else (0, "single process")


def _rank(communicator=None):
    if _fork_child:
        # Inherited MPI/torch runtimes may hold locks across fork.
        return _environment_rank()
    # Consult initialized frameworks only. Importing MPI can initialize it, so
    # merely finding an installed library must never change application state.
    distributed = sys.modules.get("torch.distributed")
    mpi = sys.modules.get("mpi4py.MPI")
    try:
        if communicator is not None and mpi is not None and mpi.Is_initialized() and not mpi.Is_finalized():
            return communicator.Get_rank(), "mpi4py.MPI"
        if distributed is not None and distributed.is_initialized():
            return distributed.get_rank(), "torch.distributed"
        if mpi is not None and mpi.Is_initialized() and not mpi.Is_finalized():
            return mpi.COMM_WORLD.Get_rank(), "mpi4py.MPI"
    except Exception:
        pass
    return _environment_rank()


def _helper():
    import multiprocessing
    if multiprocessing.parent_process() is not None:
        return True
    # Known framework worker contexts are evidence; an arbitrary PPID or
    # subprocess name is not. Never import a framework just to detect it.
    ray = sys.modules.get("ray._private.worker")
    if ray is not None:
        worker = getattr(ray, "global_worker", None)
        marker = getattr(ray, "WORKER_MODE", None)
        if marker is not None and getattr(worker, "mode", None) == marker:
            return True
    dask = sys.modules.get("distributed.worker")
    if dask is not None:
        try:
            return dask.get_worker() is not None
        except Exception:
            pass
    return False


def _meta_path(path):
    stem, extension = os.path.splitext(path)
    return stem + ".meta.json" if extension else path + ".meta.json"


class _Client:
    def __init__(self, path, options):
        self.path = os.path.abspath(os.fspath(path))
        self.run = os.environ.get("RAVEN_RUN_DIR", "")
        self.communicator = options.get("communicator")
        self.rank, self.rank_source = _rank(self.communicator)
        declared_rank, declared_source = _environment_rank()
        self.rank_warning = ""
        invalid_rank_fields = []
        for key in ("RANK", "OMPI_COMM_WORLD_RANK", "PMI_RANK", "SLURM_PROCID"):
            if key in os.environ:
                raw = os.environ[key]
                try:
                    if not 0 <= int(raw) <= (1 << 31) - 1:
                        raise ValueError()
                except ValueError:
                    invalid_rank_fields.append(key)
        if invalid_rank_fields:
            self.rank_warning = "Ignored invalid rank fields: " + ", ".join(invalid_rank_fields)
        if self.rank_source in {"torch.distributed", "mpi4py.MPI"} and declared_source != "single process" and declared_rank != self.rank:
            self.rank_warning = f"Initialized {self.rank_source} rank {self.rank} overrides conflicting {declared_source}={declared_rank}"
        self.helper = (_fork_child or _helper()) and self.rank_source not in {"torch.distributed", "mpi4py.MPI"}
        self.all_ranks = bool(options.get("all_ranks", False))
        self.allowed = self.rank is not None and (self.all_ranks or (self.rank == 0 and not self.helper))
        if self.all_ranks:
            stem, extension = os.path.splitext(self.path)
            suffix = str(self.rank) if self.rank is not None else "invalid"
            # Pool children have no distributed rank. Give each explicitly opted
            # in helper its own stream instead of colliding with rank zero.
            if self.helper:
                suffix += "-pid-" + str(os.getpid())
            self.path = stem + ".rank-" + suffix + (extension or ".jsonl")
        self.meta = _meta_path(self.path)
        self.interval = options.get("flush_interval", 1.0)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.idle = threading.Event()
        self.idle.set()
        self.queue = deque()
        self.latest = {}
        self.kinds = {}
        self.units = {}
        self.baselines = {}
        self.coalesced = {}
        self.counts = {name: 0 for name in ("accepted", "written", "suppressed", "unsupported", "dropped", "progress_coalesced")}
        self.sent_counts = dict(self.counts)
        self.sent_coalesced = {}
        self.steps = {}
        self.last_sample = {}
        self.last_emitted = {}
        self.coverage = {self.rank_warning} if self.rank_warning else set()
        self.diagnostics = {}
        self.diagnosed = set()
        self.failed = False
        self.stopping = False
        self.flush_requested = False
        self.thread = None
        self.registered = False
        self.registered_names = set()
        self.directory_fd = None
        self.fork_pause = threading.Event()
        self.fork_quiet = threading.Event()

    def refresh_rank(self):
        """Distributed runtimes can initialize after the first metric call."""
        rank, source = _rank(self.communicator)
        helper = (_fork_child or _helper()) and source not in {"torch.distributed", "mpi4py.MPI"}
        owner = os.environ.get("RAVEN_METRICS_OWNER_PID")
        if source not in {"torch.distributed", "mpi4py.MPI"} and owner and owner != str(os.getpid()):
            helper = True
        with self.lock:
            self.rank, self.rank_source, self.helper = rank, source, helper
            self.allowed = rank is not None and (self.all_ranks or (rank == 0 and not helper))

    def start(self, *, diagnostics=False):
        if self.thread is None and (self.allowed or diagnostics):
            thread = threading.Thread(target=self.work, name="raven-metrics", daemon=True)
            try:
                thread.start()
            except Exception:
                self.failed = True
                self.counts["dropped"] += len(self.queue) + len(self.latest)
                self.queue.clear()
                self.latest.clear()
                self.idle.set()
                return False
            self.thread = thread
        return not self.failed

    def note(self, category, message):
        with self.lock:
            if category not in self.diagnosed and len(self.diagnosed) < 32:
                self.diagnosed.add(category)
                self.diagnostics[category] = "".join(char if char.isprintable() else "?" for char in str(message)[:512])
                self.idle.clear()
                self.start(diagnostics=True)
                self.wake.set()

    def step(self, name):
        with self.lock:
            if name not in self.steps and len(self.steps) >= MAX_METRICS:
                return None
            value = self.steps.get(name, 0)
            self.steps[name] = value + 1
            return value

    def _coalesce(self, name):
        if name is None:
            self.counts["progress_coalesced"] += 1
        else:
            self.coalesced[name] = self.coalesced.get(name, 0) + 1

    def accept(self, record, kind, unit, sampled=False):
        with self.lock:
            self.counts["accepted"] += 1
            if not self.allowed or self.failed or self.stopping:
                self.counts["suppressed"] += 1
                return
            record = dict(record, pid=os.getpid())
            name = next(iter(record["metrics"]), None)
            if name is not None:
                if name not in self.kinds and len(self.kinds) >= MAX_METRICS:
                    self.counts["dropped"] += 1
                    self.note("names", "64 metric names reached; additional names are counted but not written")
                    return
                self.baselines.setdefault(name, record["metrics"][name])
                previous_kind = self.kinds.setdefault(name, kind)
                if previous_kind != kind:
                    self.note("kind", "Conflicting metric kinds; the first declared kind is retained")
                if unit:
                    self.units[name] = unit
            now = time.monotonic()
            if sampled and now - self.last_sample.get(name, -math.inf) >= .05:
                self.last_sample[name] = now
                if name in self.latest:
                    self._coalesce(name)
                    del self.latest[name]
                if len(self.queue) == MAX_POINTS:
                    old_name, _, _ = self.queue.popleft()
                    self._coalesce(old_name)
                self.queue.append((name, record, now))
            else:
                if name in self.latest:
                    self._coalesce(name)
                self.latest[name] = (record, now)
            self.idle.clear()
            self.start()
            if len(self.queue) + len(self.latest) >= 64:
                self.wake.set()

    def _take(self):
        with self.lock:
            # Explicit samples already satisfy the per-name enqueue-time cap.
            records = [record for _, record, _ in self.queue]
            now = time.monotonic()
            for name, _, _ in self.queue:
                self.last_emitted[name] = now
            self.queue.clear()
            for name, (record, stamp) in list(self.latest.items()):
                if not self.stopping and now - self.last_emitted.get(name, -math.inf) < .05:
                    continue
                records.append(record)
                self.last_emitted[name] = now
                del self.latest[name]
            return records

    def _metadata(self):
        with self.lock:
            counts = dict(self.counts)
            coalesced = dict(self.coalesced)
            kinds, units = dict(self.kinds), dict(self.units)
            baselines = dict(self.baselines)
            coverage = sorted(self.coverage)[:16]
        def merge(value):
            if value and value.get("schema") != "raven.metrics-meta/v1":
                raise ValueError("unrecognized metrics metadata schema")
            value.setdefault("schema", "raven.metrics-meta/v1")
            value.setdefault("client", {"api_version": API_VERSION})
            existing = value.setdefault("kinds", {})
            if not isinstance(existing, dict) or len(existing) > MAX_METRICS:
                raise ValueError("invalid metadata metric inventory")
            # A full shared inventory excludes new names, not updates to names
            # that already exist. One other producer cannot disable this writer.
            admitted = set(existing)
            for name, kind in kinds.items():
                if name in existing or len(existing) < MAX_METRICS:
                    existing.setdefault(name, kind)
                    admitted.add(name)
            rejected = set(kinds) - admitted
            if rejected:
                with self.lock:
                    for name in rejected:
                        self.kinds.pop(name, None)
                        self.units.pop(name, None)
                        self.baselines.pop(name, None)
                    self.counts["dropped"] += len(rejected)
            for key, current, previous in (("counts", counts, self.sent_counts),
                                            ("coalesced", coalesced, self.sent_coalesced)):
                target = value.setdefault(key, {})
                if not isinstance(target, dict) or len(target) > MAX_METRICS + 1:
                    raise ValueError("invalid metadata counters")
                for name, count in current.items():
                    before = target.get(name, 0)
                    if isinstance(before, bool) or not isinstance(before, int) or before < 0:
                        raise ValueError("invalid metadata counter")
                    target[name] = min((1 << 63) - 1, before + count - previous.get(name, 0))
            value.setdefault("units", {}).update(units)
            target_baselines = value.setdefault("baselines", {})
            if not isinstance(target_baselines, dict) or len(target_baselines) > MAX_METRICS:
                raise ValueError("invalid metadata baselines")
            for name, baseline in baselines.items():
                if name in target_baselines or len(target_baselines) < MAX_METRICS:
                    target_baselines.setdefault(name, baseline)
            value["rank_policy"] = {"mode": "all_ranks" if self.all_ranks else "coordinator",
                                    "rank": self.rank, "source": self.rank_source,
                                    "helper": self.helper, "coverage": coverage}
            return value
        _json_update(self.meta, merge, directory_fd=self.directory_fd)
        self.sent_counts, self.sent_coalesced = counts, coalesced
        self.registered_names.update(self.kinds)

    def _index(self):
        if not self.all_ranks or self.registered:
            return
        path = os.path.join(os.path.dirname(self.path), "metrics.index.json")
        item = {"path": os.path.basename(self.path), "meta": os.path.basename(self.meta), "rank": self.rank}
        def merge(value):
            if value and value.get("schema") != "raven.metrics-index/v1":
                raise ValueError("unrecognized rank metrics index")
            streams = value.setdefault("streams", [])
            if not isinstance(streams, list) or len(streams) > 128:
                raise ValueError("rank stream index exceeds 128 streams")
            if not any(isinstance(old, dict) and old.get("path") == item["path"] for old in streams):
                if len(streams) == 128:
                    raise ValueError("rank stream index exceeds 128 streams")
                streams.append(item)
            value["schema"] = "raven.metrics-index/v1"
            return value
        _json_update(path, merge, directory_fd=self.directory_fd)
        self.registered = True

    def _diagnostics(self):
        with self.lock:
            messages = list(self.diagnostics.values())
            self.diagnostics.clear()
        for message in messages:
            try:
                print("Raven metrics: " + message, file=sys.stderr)
            except Exception:
                pass
            # Preserve the established run-diagnostic location when its lock
            # directory exists. Do not create directories in an application.
            if self.run and self.allowed:
                try:
                    parent, name = _parent(os.path.join(self.run, ".raven", "report.lock"))
                    try:
                        fd = _open(parent, name, create=True)
                        try:
                            _lock(fd)
                            def amend(value, message=message):
                                warnings = value.setdefault("metadata", {}).setdefault("automation", {}).setdefault("warnings", [])
                                text = "Raven metrics: " + message
                                if text not in warnings and sum(len(str(w).encode()) for w in warnings) + len(text.encode()) <= 8192:
                                    warnings.append(text)
                                return value
                            _json_update(os.path.join(self.run, "run.json"), amend)
                        finally:
                            _unlock(fd)
                            os.close(fd)
                    finally:
                        os.close(parent)
                except Exception:
                    pass

    def work(self):
        while True:
            self.wake.wait(min(.05, self.interval) if self.flush_requested else self.interval)
            self.wake.clear()
            if self.fork_pause.is_set():
                self.fork_quiet.set()
                while self.fork_pause.is_set():
                    self.wake.wait(.01)
                    self.wake.clear()
                self.fork_quiet.clear()
            records = self._take()
            try:
                if self.directory_fd is None and self.allowed and not self.failed:
                    self.directory_fd, _ = _parent(self.path)
                if records and not self.failed:
                    # Register names before appending so sequential or concurrent
                    # coordinator processes cannot exceed one stream's budget.
                    with self.lock:
                        new_names = set(self.kinds) - self.registered_names
                    if new_names:
                        self._metadata()
                    records = [record for record in records if not record["metrics"]
                               or all(name in self.registered_names for name in record["metrics"])]
                    self._index()
                    _append(self.path, records, directory_fd=self.directory_fd)
                    with self.lock:
                        self.counts["written"] += len(records)
                if self.allowed and not self.failed and (records or self.diagnostics):
                    self._metadata()
            except TimeoutError:
                # A peer can briefly hold the file lock across fork or I/O.
                # Keep records for a later bounded retry without disabling it.
                with self.lock:
                    for record in reversed(records):
                        name = next(iter(record["metrics"]), None)
                        self.queue.appendleft((name, record, time.monotonic()))
                    while len(self.queue) > MAX_POINTS:
                        name, _, _ = self.queue.popleft()
                        self._coalesce(name)
            except Exception as exc:
                self.failed = True
                self.note("write", "Reporting disabled after write error: " + str(exc))
            self._diagnostics()
            with self.lock:
                if not self.queue and not self.latest:
                    self.idle.set()
                    self.flush_requested = False
                    if self.stopping and self.directory_fd is not None:
                        os.close(self.directory_fd)
                        self.directory_fd = None
                    # Idle clients need no permanent daemon. Restart lazily on
                    # the next publication, reducing fork-with-threads hazards.
                    self.thread = None
                    return

    def flush(self, timeout=2):
        with self.lock:
            if self.thread is None:
                return not self.failed
            self.flush_requested = True
            self.wake.set()
        return self.idle.wait(timeout) and not self.failed

    def close(self, timeout=2):
        with self.lock:
            self.stopping = True
            self.wake.set()
        thread = self.thread
        if thread is not None and thread.ident is not None:
            thread.join(timeout)
            return not thread.is_alive() and not self.failed
        if self.directory_fd is not None:
            os.close(self.directory_fd)
            self.directory_fd = None
        return True


def _before_fork():
    current = _state
    if current is not None and current.thread is not None:
        current.fork_pause.set()
        current.wake.set()
        # A storage stall must not turn an unrelated fork into an unbounded
        # wait. Explicit unlocks and child descriptor closure cover that case.
        current.fork_quiet.wait(.05)


def _parent_after_fork():
    if _state is not None:
        _state.fork_pause.clear()
        _state.wake.set()


def _after_fork():
    global _state, _state_lock, _fork_child, _phase, _phase_scopes
    if _state is not None and _state.directory_fd is not None:
        os.close(_state.directory_fd)
    _phase = ContextVar("raven_metric_phase", default="")
    _phase_scopes = ContextVar("raven_phase_scopes", default=())
    _state = None
    _fork_child = True
    _state_lock = threading.RLock()


def _shutdown():
    current = _state
    if current is not None:
        current.close()


def _get_client():
    global _state
    path = _options.get("path") or os.environ.get("RAVEN_METRICS_FILE")
    if not path and os.environ.get("RAVEN_RUN_DIR"):
        path = os.path.join(os.environ["RAVEN_RUN_DIR"], "metrics.jsonl")
    if not path:
        return None
    with _state_lock:
        if _state is None:
            os.environ.setdefault("RAVEN_METRICS_OWNER_PID", str(os.getpid()))
            _state = _Client(path, _options)
            if _state.rank_warning:
                _state.note("rank", _state.rank_warning)
        _state.refresh_rank()
        return _state


def configure(*, path=None, all_ranks=False, communicator=None, flush_interval=1.0):
    """Enable local reporting or explicit all-rank streams.

    ``communicator`` may be an already initialized mpi4py communicator. With no
    communicator, reductions use an *already loaded and initialized* MPI or
    torch.distributed runtime. Raven never initializes a distributed runtime.
    Reconfiguration drains the previous writer for at most two seconds. Call
    configure() after changing environment variables in an already running
    process so decorators refresh their cached inactive decision.
    """
    global _state, _options, _revision
    if path is not None and (not isinstance(os.fspath(path), str) or not os.fspath(path)):
        raise ValueError("path must be a nonempty text path")
    if not isinstance(all_ranks, bool):
        raise ValueError("all_ranks must be boolean")
    flush_interval = _number(flush_interval, "flush_interval")
    if not .05 <= flush_interval <= 60:
        raise ValueError("flush_interval must be between 0.05 and 60 seconds")
    with _state_lock:
        if _state is not None:
            _state.close()
            # Recover from a failed writer once it has actually stopped, but
            # never create a second writer while a blocked one might still run.
            if _state.thread is not None and _state.thread.is_alive():
                return False
        _options = {"path": os.fspath(path) if path is not None else None, "all_ranks": all_ranks,
                    "communicator": communicator, "flush_interval": flush_interval}
        _state = None
        _revision += 1
        if path is not None:
            os.environ["RAVEN_METRICS_OWNER_PID"] = str(os.getpid())
    return True


def _reduce(client, value, reduction, *, valid=True, device=None):
    if reduction is None:
        return value
    if _fork_child:
        # An inherited distributed runtime is not a runtime initialized in this
        # process. Calling it after raw fork is unsafe even to query readiness.
        return None
    distributed = sys.modules.get("torch.distributed")
    torch = sys.modules.get("torch")
    if client.communicator is None and distributed is not None and torch is not None and distributed.is_initialized():
        tensor_device = "cpu"
        if distributed.get_backend() == "nccl":
            raw = os.environ.get("LOCAL_RANK", "")
            tensor_device = "cuda:" + str(int(raw)) if raw.isdecimal() else (device or "cuda")
            if device is not None and raw.isdecimal() and str(device) != "cuda:" + str(int(raw)):
                valid = False
        ready = torch.tensor(int(valid), dtype=torch.int32, device=tensor_device)
        distributed.all_reduce(ready, op=distributed.ReduceOp.MIN)
        if not ready.item():
            client.note("collective-input", "Collective metric omitted: at least one rank supplied an invalid scalar or metric field")
            return None
        tensor = torch.tensor(value, dtype=torch.float64, device=tensor_device)
        operation = getattr(distributed.ReduceOp, {"sum": "SUM", "mean": "SUM", "max": "MAX", "min": "MIN"}[reduction])
        distributed.all_reduce(tensor, op=operation)
        result = tensor.item()
        if distributed.get_rank() != 0 and not client.all_ranks:
            return None
        return result / distributed.get_world_size() if reduction == "mean" else result
    mpi = sys.modules.get("mpi4py.MPI")
    communicator = client.communicator
    if communicator is None and mpi is not None and mpi.Is_initialized() and not mpi.Is_finalized():
        communicator = mpi.COMM_WORLD
    if communicator is not None:
        if mpi is None or not mpi.Is_initialized() or mpi.Is_finalized():
            raise ValueError("communicator requires an already initialized mpi4py.MPI runtime")
        # This agreement is part of the explicitly requested reduction. It
        # prevents malformed local observations from stranding valid peers.
        ready = communicator.allreduce(int(valid), op=mpi.MIN)
        if not ready:
            client.note("collective-input", "Collective metric omitted: at least one rank supplied an invalid scalar or metric field")
            return None
        operation = getattr(mpi, {"sum": "SUM", "mean": "SUM", "max": "MAX", "min": "MIN"}[reduction])
        result = communicator.reduce(value, op=operation, root=0)
        if communicator.Get_rank() != 0:
            return None
        return result / communicator.Get_size() if reduction == "mean" else result
    with client.lock:
        client.coverage.add("rank 0 only: no initialized reduction communicator")
    return value if valid else None


class _CollectiveContext:
    """Disk-independent participant when a peer alone configured publication."""
    def __init__(self):
        self.communicator = _options.get("communicator")
        self.all_ranks = False
        self.lock = threading.RLock()
        self.coverage = set()
        self.counts = {"suppressed": 0, "unsupported": 0}

    def step(self, name):
        return None

    def note(self, *args):
        pass

    def accept(self, *args):
        pass


def _report_collective(client, name, value, *, kind, step, unit, reduction, sampled):
    """Validate all local fields, then let every explicit participant agree."""
    record = None
    error = None
    try:
        if reduction not in ("sum", "mean", "min", "max"):
            raise ValueError("reduce must be sum, mean, max, or min")
        name = _text(name, "metric name", 96, empty=False)
        if "=" in name:
            raise ValueError("metric name must not contain '='")
        kind = Kind(kind).value
        unit = _text(unit, "unit", 64)
        proposed = {"t": time.time(), "metrics": {name: _scalar(value)}}
        if step == "auto":
            step = client.step(name)
        if step is not None:
            proposed["step"] = step
        if _phase.get():
            proposed["phase"] = _phase.get()
        record = _record(proposed)
    except Exception as exc:
        error = exc
    # Local validation failure still enters readiness agreement. No disk state
    # participates in that decision; failed writers continue communicating.
    reduced = _reduce(client, record["metrics"][name] if record else 0.0, reduction,
                      valid=record is not None, device=getattr(value, "device", None))
    if error is not None:
        raise error
    if reduced is None:
        with client.lock:
            client.counts["suppressed"] += 1
        return
    record["metrics"][name] = _number(reduced, "reduced metric value")
    client.accept(record, kind, unit, sampled)


def _report(name, value, *, kind="gauge", step=None, unit="", reduce=None, sampled=False):
    client = _get_client()
    if client is None and reduce is None:
        return
    if client is None:
        client = _CollectiveContext()
    try:
        if reduce is not None:
            _report_collective(client, name, value, kind=kind, step=step, unit=unit,
                               reduction=reduce, sampled=sampled)
            return
        name = _text(name, "metric name", 96, empty=False)
        if "=" in name:
            raise ValueError("metric name must not contain '='")
        kind = Kind(kind).value
        unit = _text(unit, "unit", 64)
        # A storage failure must not remove this rank from an explicit
        # collective that its peers still execute. Only publication is disabled.
        if (client.failed and reduce is None) or (client.helper and not client.all_ranks) or (not client.allowed and reduce is None):
            with client.lock:
                client.counts["accepted"] += 1
                client.counts["suppressed"] += 1
            return
        number = _reduce(client, _scalar(value), reduce)
        if number is None:
            with client.lock:
                client.counts["suppressed"] += 1
            return
        record = {"t": time.time(), "metrics": {name: number}}
        if step == "auto":
            step = client.step(name)
        if step is not None:
            record["step"] = step
        current_phase = _phase.get()
        if current_phase:
            record["phase"] = current_phase
        client.accept(_record(record), kind, unit, sampled)
    except Exception as exc:
        with client.lock:
            client.counts["unsupported"] += 1
        client.note("unsupported:" + type(value).__name__, "Metric was not recorded: " + str(exc))


def metric(name, value, *, kind="gauge", step=None, unit="", reduce=None):
    """Enqueue a scalar and return the original value, even if reporting fails.

    Explicit reduce= performs a validity agreement and then a value collective:
    all ranks must call it in the same order. No other API call communicates
    with another process.
    """
    try:
        _report(name, value, kind=kind, step=step, unit=unit, reduce=reduce)
    except Exception:
        pass
    return value


def progress(completed, total, unit="steps", *, step=None):
    """Report completed/total progress; preserve the completed input value."""
    try:
        client = _get_client()
        if client is None:
            return completed
        value = {"t": time.time(), "metrics": {}, "progress": {
            "completed": _scalar(completed), "total": _scalar(total), "unit": unit}}
        if step is not None:
            value["step"] = step
        if _phase.get():
            value["phase"] = _phase.get()
        record = _record(value)
        record["progress"].pop("fraction")
        client.accept(record, "progress", "")
    except Exception as exc:
        if "client" in locals() and client is not None:
            client.note("progress", "Progress was not recorded: " + str(exc))
    return completed


class phase:
    """Context-local phase scope, including nested reuse of the same instance."""
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        scope = None
        try:
            if _get_client() is not None:
                name = _text(self.name, "phase", 160, empty=False)
                scope = (_phase.set(name), time.monotonic())
        except Exception:
            pass
        _phase_scopes.set((*_phase_scopes.get(), (self, scope)))
        return self

    def __exit__(self, *exception):
        try:
            scopes = _phase_scopes.get()
            if scopes and scopes[-1][0] is self:
                scope = scopes[-1][1]
                _phase_scopes.set(scopes[:-1])
                if scope is not None:
                    token, started = scope
                    name = "phase." + self.name + ".seconds"
                    if len(name) > 96 or "=" in name:
                        import hashlib
                        name = "phase." + self.name[:70].replace("=", "_") + "." + hashlib.sha256(self.name.encode()).hexdigest()[:8] + ".seconds"
                    metric(name, time.monotonic() - started, unit="s")
                    _phase.reset(token)
        except Exception:
            # Even fork/context teardown must never replace the user's error.
            pass
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *exception):
        return self.__exit__(*exception)


def measure(name, *, kind="objective", step="auto", every=1, seconds=None, unit="", reduce=None):
    """Decorate a scalar/mapping-returning function, preserving its outcome."""
    if reduce is not None and seconds is not None:
        raise ValueError("seconds sampling cannot be combined with a collective reduction; use matching every=N calls on all ranks")
    if isinstance(every, bool) or not isinstance(every, int) or every < 1:
        raise ValueError("every must be a positive integer")
    if seconds is not None and _number(seconds, "seconds", nonnegative=True) <= 0:
        raise ValueError("seconds must be positive")
    def decorate(function):
        # Environment selection belongs to process startup. Explicit configure
        # invalidates this cached no-op decision, including late local enable.
        revision = _revision
        enabled = bool(_options.get("path") or os.environ.get("RAVEN_METRICS_FILE") or os.environ.get("RAVEN_RUN_DIR"))
        calls = 0
        last = -math.inf
        owner_pid = os.getpid()
        lock = threading.Lock()
        def reported(value, args, kwargs):
            nonlocal calls, last, owner_pid, lock
            try:
                if owner_pid != os.getpid():
                    owner_pid, calls, last, lock = os.getpid(), 0, -math.inf, threading.Lock()
                with lock:
                    index = calls
                    calls += 1
                    now = time.monotonic()
                    if index % every or (seconds is not None and now - last < seconds):
                        return
                    last = now
                client = _get_client()
                if client is None and reduce is None:
                    return
                automatic_step = index if step == "auto" else None
                selected_step = step
                if step not in (None, "auto") and isinstance(step, str):
                    try:
                        bound = inspect.signature(function).bind_partial(*args, **kwargs).arguments
                        parts = step.split(".")
                        selected_step = bound.get(parts[0], kwargs.get(parts[0]))
                        if selected_step is None and args and len(parts) == 1:
                            selected_step = getattr(args[0], step)
                        for part in parts[1:]:
                            selected_step = getattr(selected_step, part)
                    except Exception:
                        if reduce is None:
                            raise
                        # Keep the explicit collective schedule even when this
                        # rank cannot resolve the requested step attribute.
                        selected_step = object()
                elif step == "auto":
                    selected_step = automatic_step
                items = value.items() if isinstance(value, Mapping) else ((name, value),)
                for index, (key, number) in enumerate(items):
                    if index >= MAX_METRICS:
                        client.note("mapping", "Mapping return exceeds 64 metric names; remaining values ignored")
                        break
                    _report(key, number, kind=kind, step=selected_step, unit=unit, reduce=reduce,
                            sampled=every != 1 or seconds is not None)
            except Exception as exc:
                if "client" in locals() and client is not None:
                    client.note("measure", "Measurement was not recorded: " + str(exc))
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def asynchronous(*args, **kwargs):
                nonlocal revision, enabled
                value = await function(*args, **kwargs)
                if revision != _revision:
                    revision = _revision
                    enabled = bool(_options.get("path") or os.environ.get("RAVEN_METRICS_FILE") or os.environ.get("RAVEN_RUN_DIR"))
                if enabled or reduce is not None:
                    reported(value, args, kwargs)
                return value
            return asynchronous
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            nonlocal revision, enabled
            value = function(*args, **kwargs)
            if revision != _revision:
                revision = _revision
                enabled = bool(_options.get("path") or os.environ.get("RAVEN_METRICS_FILE") or os.environ.get("RAVEN_RUN_DIR"))
            if enabled or reduce is not None:
                reported(value, args, kwargs)
            return value
        return wrapped
    return decorate


class _Watch:
    def __init__(self, function, names, kind, step, every, seconds):
        self.function = function
        self.names = names
        self.kind = kind
        self.step = step
        self.every = every
        self.seconds = seconds
        self.calls = 0
        self.last = -math.inf
        self.previous = None
        self.callback = None
        self.thread_id = threading.get_ident()
        self.active = True
        self.tool_id = None

    def _returned(self, frame):
        if not self.active or threading.get_ident() != self.thread_id:
            return
        index = self.calls
        self.calls += 1
        now = time.monotonic()
        if index % self.every or (self.seconds is not None and now - self.last < self.seconds):
            return
        self.last = now
        for name in self.names:
            if name in frame.f_locals:
                selected_step = index if self.step == "auto" else frame.f_locals.get(self.step) if isinstance(self.step, str) else self.step
                try:
                    _report(name, frame.f_locals[name], kind=self.kind, step=selected_step,
                            sampled=self.every != 1 or self.seconds is not None)
                except Exception:
                    pass

    def install(self):
        if _get_client() is None:
            return self
        if not (inspect.isfunction(self.function) or inspect.ismethod(self.function)) or len(self.names) > MAX_METRICS:
            _get_client().note("watch", "watch requires a Python function and at most 64 local names")
            return self
        try:
            for name in self.names:
                _text(name, "local name", 96, empty=False)
        except ValueError as exc:
            _get_client().note("watch", "Watch was not installed: " + str(exc))
            return self
        self.previous = sys.getprofile()
        code = self.function.__code__
        monitoring = getattr(sys, "monitoring", None)
        if monitoring is not None:
            for candidate in (5, 4, 3, 2, 1, 0):
                if monitoring.get_tool(candidate) is None:
                    try:
                        monitoring.use_tool_id(candidate, "raven-watch")
                        self.tool_id = candidate
                        break
                    except ValueError:
                        continue
            if self.tool_id is not None:
                def returned(current, offset, result):
                    try:
                        if current is code and self.active:
                            self._returned(sys._getframe(1))
                    except Exception:
                        pass
                self.callback = returned
                monitoring.register_callback(self.tool_id, monitoring.events.PY_RETURN, returned)
                monitoring.set_local_events(self.tool_id, code, monitoring.events.PY_RETURN)
                return self
        def profile(frame, event, argument):
            if self.previous is not None:
                self.previous(frame, event, argument)
            if self.active and event == "return" and frame.f_code is code:
                # Profile "return" also describes yields and exception unwinds.
                # Observe a real completed return only, never stale locals from
                # suspension or a failed function invocation.
                if frame.f_lasti < 0 or dis.opname[code.co_code[frame.f_lasti]] not in {"RETURN_VALUE", "RETURN_CONST"}:
                    return
                self._returned(frame)
        profile._raven_watch_owner = self
        self.callback = profile
        sys.setprofile(profile)
        return self

    def close(self):
        self.active = False
        if self.tool_id is not None:
            monitoring = sys.monitoring
            monitoring.set_local_events(self.tool_id, self.function.__code__, 0)
            monitoring.register_callback(self.tool_id, monitoring.events.PY_RETURN, None)
            monitoring.free_tool_id(self.tool_id)
            self.tool_id = None
            return
        if threading.get_ident() == self.thread_id and sys.getprofile() is self.callback:
            previous = self.previous
            owner = getattr(previous, "_raven_watch_owner", None)
            while owner is not None and not owner.active:
                previous = owner.previous
                owner = getattr(previous, "_raven_watch_owner", None)
            sys.setprofile(previous)

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()
        return False


def watch(function, *names, kind="gauge", step="auto", every=1, seconds=None):
    """Observe locals at returns on this thread; ``close()`` removes the hook.

    Existing profile callbacks are chained and never silently replaced. Install
    separately inside each worker thread. Generator/coroutine suspension and
    exception unwinds are ignored; named locals are read only at final return.
    """
    if isinstance(every, bool) or not isinstance(every, int) or every < 1:
        raise ValueError("every must be a positive integer")
    if seconds is not None and _number(seconds, "seconds", nonnegative=True) <= 0:
        raise ValueError("seconds must be positive")
    return _Watch(function, names, kind, step, every, seconds).install()


def __getattr__(name):
    # A copied raven.py already exposes the public functions directly; this
    # fallback makes `from raven import client` useful in the same environment.
    if name == "client":
        return sys.modules[__name__]
    raise AttributeError(name)


# Register before application exit hooks installed after importing Raven. The
# first metric call can happen much later, including inside such an exit hook.
atexit.register(_shutdown)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(before=_before_fork, after_in_parent=_parent_after_fork, after_in_child=_after_fork)
_registered = True
