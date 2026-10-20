"""Managed Light retention budgets; legacy sessions keep their existing limits."""

from __future__ import annotations

from collections import OrderedDict, deque
from itertools import islice
import json
import heapq
import re
import secrets
import os
import stat
import threading

# Automatic tracing has one aggregate budget per immutable launch attempt.
TRACE_PROCESS_BYTES = 64 << 20
TRACE_RUN_BYTES = 256 << 20
TRACE_PROCESSES = 128


def _recording_base(name):
    match = re.fullmatch(r"(.+\.rvb)\.damaged-[0-9]+-[0-9a-f]{8}", name)
    if match:
        return match[1]
    return name[:-14] if name.endswith(".rvb.recovery.json") else name


from ..cache_budget import retained_size, CacheBudget, BudgetCache


def cache(app, name):
    owner = getattr(app, "_chart_owner", app)
    budget = getattr(owner, "raven_cache_budget", None)
    return BudgetCache(budget) if budget else OrderedDict()


class LightLimits:
    def __init__(self, values, state_dir=None):
        defaults = dict(
            max_job_rows=5000,
            max_history_rows=5000,
            max_series_jobs=128,
            series_bytes=16 << 20,
            persistence_queue_bytes=8 << 20,
            storage_bytes=256 << 20,
            storage_files=512,
        )
        self.values = {
            key: max(1, int(values.get(key, default)))
            for key, default in defaults.items()
        }
        self.coverage = {}
        self.series = OrderedDict()
        self.series_bytes = 0
        self.pending_bytes = 0
        self.state_dir = state_dir
        self.disk = None
        self.disk_bytes = 0
        self.lock = threading.RLock()
        self.disk_lock = threading.RLock()
        self.active_paths = frozenset()
        self.pinned_jobs = frozenset()
        self.session_paths = OrderedDict()
        self.metadata_lock = threading.Lock()

    def note_session_path(self, path):
        if path is None:
            return
        with self.metadata_lock:
            if path not in self.session_paths:
                self.session_paths[path] = None
                while len(self.session_paths) > self.values["storage_files"] * 2:
                    self.session_paths.popitem(last=False)

    def rows(self, rows, key):
        limit = self.values["max_job_rows" if key == "jobs" else "max_history_rows"]
        records = (
            list(rows[:limit])
            if hasattr(rows, "__getitem__")
            else list(islice(rows, limit + 1))
        )
        total = len(rows) if hasattr(rows, "__len__") else len(records)
        self.coverage[key] = {
            "retained": len(records),
            "observed": total,
            "limited": total > limit,
        }
        return records[:limit], total > limit

    def retain_series(self, store, jid, cost=None):
        """Call under Store.lock after append, or after a bounded archive merge."""
        source = store.series.get(jid)
        if not source:
            old = self.series.pop(jid, deque())
            self.series_bytes -= sum(old)
            return
        old = self.series.pop(jid, deque())
        if not source:
            self.series_bytes -= sum(old)
            old = deque()
            cost = None
        if cost is None:
            self.series_bytes -= sum(old)
            old = deque(
                retained_size(sample, self.values["series_bytes"]) for sample in source
            )
            self.series_bytes += sum(old)
        else:
            while len(old) >= len(source) and old:
                self.series_bytes -= old.popleft()
            old.append(cost)
            self.series_bytes += cost
        self.series[jid] = old
        trimmed = 0
        while (
            len(self.series) > self.values["max_series_jobs"]
            or self.series_bytes > self.values["series_bytes"]
        ):
            active = {job.id for job in store.jobs}
            oldest = next(
                (key for key in self.series if key not in active),
                next(iter(self.series)),
            )
            costs = self.series[oldest]
            if oldest in active or oldest == jid and len(self.series) == 1:
                target = store.series.get(oldest)
                if len(self.series) > self.values["max_series_jobs"] and all(
                    key in active for key in self.series
                ):
                    # Keep active attempts, sharing the byte budget rather than
                    # evicting an entire live history after browsing History.
                    if self.series_bytes <= self.values["series_bytes"]:
                        break
                self.series_bytes -= costs.popleft()
                if target:
                    target.popleft()
                trimmed += 1
                if not costs:
                    self.series.pop(oldest, None)
                    break
            else:
                self.series.pop(oldest)
                self.series_bytes -= sum(costs)
                store.series.pop(oldest, None)
                publication = store._publications.pop(oldest, None)
                if publication:
                    publication[0].release_publication()
                store._series_loaded.discard(oldest)
                store._series_archive.limited.discard(oldest)
                store.hist_cpu.pop(oldest, None)
                store.hist_gpu.pop(oldest, None)
                trimmed += 1
        if trimmed:
            previous = self.coverage.get("series", {}).get("evicted", 0)
            self.coverage["series"] = {
                "limited": True,
                "evicted": previous + trimmed,
                "retained_jobs": len(self.series),
                "bytes": self.series_bytes,
            }

    def enqueue(self, queue, path, sample):
        # Bound work before JSON materialization as well as queued bytes.
        # Unknown extension values are refused instead of invoking encoders.
        preparation_limit = max(1024, self.values["persistence_queue_bytes"])
        if retained_size(sample, preparation_limit) > preparation_limit:
            self.coverage["persistence"] = {
                "limited": True,
                "reason": "record exceeds queue preparation budget",
            }
            return
        try:
            payload = (
                json.dumps(sample, separators=(",", ":"), allow_nan=False) + "\n"
            ).encode("utf-8")
        except (ValueError, TypeError, RecursionError):
            self.coverage["persistence"] = {
                "limited": True,
                "reason": "unsupported record",
            }
            return
        with self.lock:
            if (
                self.pending_bytes + len(payload)
                > self.values["persistence_queue_bytes"]
            ):
                self.coverage["persistence"] = {
                    "limited": True,
                    "reason": "queue byte budget reached",
                }
                return
            self.pending_bytes += len(payload)
            queue.append((path, payload))

    def _inventory(self):
        if self.disk is not None:
            return
        self.disk = {}
        self.disk_bytes = 0
        self.disk_count = 0
        # Count the whole owned inventory, retaining only a bounded eviction
        # candidate heap and protected attempts. Old state cannot create a
        # permanent "over cap" sentinel that prevents all future cleanup.
        from ..recordings.identity import inventory_job

        protected = {os.path.splitext(value)[0] for value in self.active_paths}
        with self.metadata_lock:
            sessions = frozenset(self.session_paths)
        candidates = []
        maximum = max(16, self.values["storage_files"] * 2)
        roots = (
            [(self.state_dir, False), (os.path.join(self.state_dir, "series"), True)]
            if self.state_dir
            else []
        )
        for directory, series in roots:
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        base = _recording_base(entry.name)
                        if not (
                            base.endswith((".jsonl", ".rvb", ".rvt"))
                            if series
                            else entry.name in ("events.jsonl", "events.jsonl.previous")
                        ):
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode):
                            continue
                        self.disk_bytes += info.st_size
                        if entry.name.endswith(".rvb.recovery.json"):
                            # One small pending descriptor belongs to its data
                            # file; account bytes, not another archive slot.
                            if (
                                inventory_job(base) is not None
                                and info.st_uid == os.getuid()
                                and self._known_size(os.path.join(directory, base))
                                is None
                            ):
                                os.unlink(entry.path)
                                self.disk_bytes -= info.st_size
                            continue
                        self.disk_count += 1
                        name = base
                        job = inventory_job(
                            name if not name.endswith(".rvt") else name[:-4] + ".rvb"
                        )
                        if (
                            os.path.splitext(os.path.join(directory, base))[0]
                            in protected
                            and ".damaged-" not in entry.name
                            or job in self.pinned_jobs
                            or job is None
                            or info.st_uid != os.getuid()
                        ):
                            # Protected entries need no per-file cache; accounting
                            # uses totals and the append target is stat'ed afresh.
                            continue
                        priority = (
                            0 if base != entry.name or entry.path in sessions else 1
                        )
                        item = (-priority, -info.st_mtime_ns, entry.path, info.st_size)
                        if len(candidates) < maximum:
                            heapq.heappush(candidates, item)
                        elif item > candidates[0]:
                            heapq.heapreplace(candidates, item)
            except FileNotFoundError:
                continue
            except OSError:
                self.disk = None
                raise
        self.disk.update((path, size) for _, _, path, size in candidates)

    def _known_size(self, path):
        try:
            value = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(value.st_mode):
            raise OSError("owned history must be a regular file")
        return value.st_size

    def write(self, path, payload):
        """One owned append with rollback and oldest inactive-attempt eviction."""
        with self.disk_lock:
            self._inventory()
            previous_size = self._known_size(path)
            new = previous_size is None
            self._make_room(path, len(payload), new)
            new = self._known_size(path) is None
            if (
                self.disk_bytes + len(payload) > self.values["storage_bytes"]
                or self.disk_count + int(new) > self.values["storage_files"]
            ):
                self.coverage["storage"] = {
                    "limited": True,
                    "reason": "owned history quota reached",
                }
                return False
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(
                path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("owned history must be a regular file")
                previous = self._known_size(path) or 0
                initial = info.st_size
                if str(path).endswith(".jsonl") and initial:
                    reader = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    try:
                        if os.pread(reader, 1, initial - 1) != b"\n":
                            payload = b"\n" + payload
                    finally:
                        os.close(reader)
                if (
                    self.disk_bytes - previous + info.st_size + len(payload)
                    > self.values["storage_bytes"]
                ):
                    self.coverage["storage"] = {
                        "limited": True,
                        "reason": "owned history grew beyond quota",
                    }
                    return False
                view = memoryview(payload)
                try:
                    while view:
                        count = os.write(fd, view)
                        if count <= 0:
                            raise OSError("short history write")
                        view = view[count:]
                    os.fdatasync(fd) if hasattr(os, "fdatasync") else os.fsync(fd)
                except BaseException:
                    os.ftruncate(fd, initial)
                    os.fsync(fd)
                    raise
                finally:
                    actual = os.fstat(fd).st_size
                    self.disk_bytes += actual - previous
                    self.disk[path] = actual
                    if new:
                        self.disk_count += 1
            finally:
                os.close(fd)
            return True

    def _make_room(self, path, size, new, _round=0):
        """Never evict a live/pinned attempt or an unrecognized external file."""
        if self.disk_bytes + size <= self.values["storage_bytes"] and (
            self.disk_count + int(new) <= self.values["storage_files"]
        ):
            return
        # Operational journal history yields space before scientific evidence.
        events = os.path.join(self.state_dir or "", "events.jsonl")
        backup = events + ".previous"
        old_backup = self._known_size(backup)
        if old_backup is not None:
            os.unlink(backup)
            self.disk_bytes -= old_backup
            self.disk_count -= 1
            self.disk.pop(backup, None)
            self.coverage["events"] = {
                "limited": True,
                "reason": "older operational event backup retired at quota",
            }
        maximum = max(1, min(32 << 20, self.values["storage_bytes"] // 8))
        if (self._known_size(events) or 0) > maximum:
            self._rotate_events(
                events, backup, maximum, reserve=int(new or path == events)
            )
        if (
            self.disk_bytes + size <= self.values["storage_bytes"]
            and self.disk_count + int(self._known_size(path) is None)
            <= self.values["storage_files"]
        ):
            return
        from ..recordings.identity import inventory_job

        # A verified compact prefix can retain the exact raw values after a
        # redundant inactive JSONL recovery copy leaves the working quota.
        # Conversion itself remains copy/verify and never deletes its input.
        from ..recordings import storage as storage

        for candidate in list(self.disk):
            if candidate == path or not candidate.endswith(".jsonl"):
                continue
            packed = storage.compact_path(candidate)
            if self._known_size(packed) is None:
                continue
            try:
                before = storage._fingerprint(candidate)
                compact_before = storage._fingerprint(packed)
                compact = iter(storage.block_rows(packed))
                equivalent = True
                for row in storage.json_rows(candidate):
                    if next(compact, None) != row:
                        equivalent = False
                        break
                if (
                    equivalent
                    and storage._fingerprint(candidate) == before
                    and storage._fingerprint(packed) == compact_before
                ):
                    os.unlink(candidate)
                    self.disk_bytes -= self.disk.pop(candidate, 0)
                    self.disk_count -= 1
                    self.coverage["conversion"] = {
                        "limited": False,
                        "reason": "redundant inactive JSONL copy retired after exact compact-prefix verification; raw observations retained",
                    }
            except (OSError, ValueError):
                continue
            if self.disk_bytes + size <= self.values["storage_bytes"] and (
                self.disk_count + int(new) <= self.values["storage_files"]
            ):
                return
        candidates = []
        protected = {os.path.splitext(value)[0] for value in self.active_paths}
        with self.metadata_lock:
            sessions = frozenset(self.session_paths)
        for candidate in self.disk:
            base = _recording_base(os.path.basename(candidate))
            canonical = os.path.join(os.path.dirname(candidate), base)
            if (
                candidate == path
                or os.path.splitext(canonical)[0] in protected
                and ".damaged-" not in os.path.basename(candidate)
            ):
                continue
            name = base
            job = inventory_job(
                name if not name.endswith(".rvt") else name[:-4] + ".rvb"
            )
            if job is None or job in self.pinned_jobs:
                continue
            try:
                info = os.stat(candidate, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
                    priority = (
                        0 if canonical != candidate or candidate in sessions else 1
                    )
                    candidates.append((priority, info.st_mtime_ns, candidate))
            except FileNotFoundError:
                pass
        evicted = []
        for _, _, candidate in sorted(candidates):
            try:
                os.unlink(candidate)
            except FileNotFoundError:
                pass
            self.disk_bytes -= self.disk.pop(candidate, 0)
            self.disk_count -= 1
            if candidate.endswith(".rvb"):
                marker = candidate + ".recovery.json"
                metadata = self._known_size(marker)
                if metadata is not None:
                    os.unlink(marker)
                    self.disk_bytes -= metadata
            evicted.append(os.path.basename(candidate))
            if self.disk_bytes + size <= self.values["storage_bytes"] and (
                self.disk_count + int(new) <= self.values["storage_files"]
            ):
                break
        if evicted:
            old = self.coverage.get("storage", {}).get("evicted_files", 0)
            self.coverage["storage"] = {
                "limited": True,
                "reason": "oldest inactive unpinned attempt archives evicted",
                "evicted_files": old + len(evicted),
            }
        if evicted and (
            self.disk_bytes + size > self.values["storage_bytes"]
            or self.disk_count + int(new) > self.values["storage_files"]
        ):
            self.disk = None
            if _round >= 31:
                return
            self._inventory()
            self._make_room(path, size, new, _round + 1)

    def _rotate_events(self, path, backup, maximum, *, reserve):
        """Retain a bounded complete-line tail, then start a fresh journal."""
        before = self._known_size(path)
        if before is None:
            return
        keep = self.disk_count + reserve <= self.values["storage_files"]
        staged = None
        retained = 0
        try:
            if keep:
                staged = backup + ".stage-" + secrets.token_hex(8)
                source = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    start = max(0, before - maximum)
                    raw = os.pread(source, before - start, start)
                    if start and os.pread(source, 1, start - 1) != b"\n":
                        raw = raw.partition(b"\n")[2]
                    if not raw.endswith(b"\n"):
                        raw = raw[: raw.rfind(b"\n") + 1]
                finally:
                    os.close(source)
                output = os.open(
                    staged,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    view = memoryview(raw)
                    while view:
                        count = os.write(output, view)
                        if count <= 0:
                            raise OSError("short event rotation write")
                        view = view[count:]
                    os.fsync(output)
                finally:
                    os.close(output)
                os.replace(staged, backup)
                staged = None
                retained = len(raw)
            os.unlink(path)
            self.disk_bytes += retained - before
            self.disk_count -= int(not keep)
            self.disk.pop(path, None)
            parent = os.open(
                self.state_dir,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
            self.coverage["events"] = {
                "limited": True,
                "reason": "operational events rotated to a bounded complete-line tail",
                "retained_bytes": retained,
                "previous": backup if keep else None,
            }
        finally:
            if staged is not None:
                try:
                    os.unlink(staged)
                except FileNotFoundError:
                    pass

    def drain(self, store, queue, io_lock):
        with io_lock:
            while True:
                with store._persist_queue_lock:
                    if not queue:
                        return
                    batch = list(queue)
                    queue.clear()
                    if queue is store._persist_queue:
                        store._series_pending_counts.clear()
                        store._series_batch_ready = False
                paths = {}
                for path, payload in batch:
                    path = store._series_aliases.get(path, path)
                    paths.setdefault(path, []).append(payload)
                for path, payloads in paths.items():
                    try:
                        compact = False
                        if path != store._events_path:
                            from ..recordings.storage import (
                                append as append_recordings,
                                compact_path,
                                uses_blocks,
                            )

                            with self.disk_lock:
                                self._inventory()
                                packed = compact_path(path)
                                available = (
                                    self.values["storage_bytes"] - self.disk_bytes
                                )
                                if (
                                    not os.path.exists(packed)
                                    and self.disk_count >= self.values["storage_files"]
                                ):
                                    available = 0

                                def write_block(target, payload):
                                    if not self.write(target, payload):
                                        raise OSError("owned history quota reached")

                                def invalidate_recovery_inventory():
                                    self.disk = None

                                if uses_blocks(path, store._series_format_cache):
                                    compact = append_recordings(
                                        path,
                                        [json.loads(payload) for payload in payloads],
                                        writer=write_block,
                                        quota=available,
                                        cache=store._series_format_cache,
                                        on_recovery=invalidate_recovery_inventory,
                                    )
                        if not compact:
                            if not self.write(path, b"".join(payloads)):
                                raise OSError(
                                    "owned history quota reached; active/pinned attempts preserved"
                                )
                    except (OSError, ValueError) as exc:
                        self.coverage["storage"] = {
                            "limited": True,
                            "reason": str(exc)[:256],
                        }
                        store._state_health("state recordings", str(exc))
                    finally:
                        with self.lock:
                            self.pending_bytes -= sum(map(len, payloads))


def bind(app, store=None, views=None, logs=None):
    """Attach one persistent Light budget; compute mode never resets it."""
    config = getattr(app, "cfg", {}).get("raven")
    if not isinstance(config, dict):
        return None
    values = config.get("resources", {}).get("light", {})
    store = store or getattr(app, "store", None)
    limits = getattr(app, "raven_limits", None) or getattr(store, "raven_limits", None)
    if limits is None:
        limits = LightLimits(values, getattr(store, "state_dir", None))
    app.raven_limits = limits
    if getattr(app, "raven_cache_budget", None) is None:
        app.raven_cache_budget = CacheBudget(
            values.get("prepared_cache_bytes", 32 << 20)
        )
    if store is not None:
        store.raven_limits = limits
    logs = logs or getattr(app, "logs", None)
    if logs is not None:
        logs.max_bytes = min(
            logs.max_bytes, int(values.get("log_window_bytes", 2 << 20))
        )
        logs.max_buffers = int(values.get("max_log_buffers", 4))
        while len(logs.buffers) > logs.max_buffers:
            logs.buffers.pop(next(iter(logs.buffers)))
    views = views or getattr(app, "views_ref", None)
    if views is not None:
        from ..native_series_cache import NativeSeriesCache
        from ..metric_raster import MetricRasterCache

        if getattr(views._native_sources, "budget", None) is not app.raven_cache_budget:
            views._native_sources = NativeSeriesCache(budget=app.raven_cache_budget)
            views._metric_rasters = MetricRasterCache(budget=app.raven_cache_budget)
    return limits
