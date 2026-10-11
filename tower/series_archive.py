"""Bounded background restore of locally persisted metric histories.

The sampler's existing executor supplies one reader. Foreground views return
published memory immediately; headless callers retain synchronous restoration.
Filesystem operations and JSON parsing never run under the Store lock.
"""
from __future__ import annotations

from collections import OrderedDict, deque
import json
import math
import os
import stat
import time

MAX_REQUESTS = 16
MAX_STATUS = 256
READ_CHUNK = 64 << 10
COLD_READ_BYTES = 64 << 20
COLD_LINE_BYTES = 8 << 20
INDEX_INTERVAL = 5.0
_INDEX = object()


def _time(sample):
    value = sample.get("t", 0)
    if type(value) is int:
        return value
    return value if type(value) is float and math.isfinite(value) else 0


def _sample_key(sample):
    key = sample.get("t"), sample.get("k")
    try:
        hash(key)
    except TypeError:
        return None
    return key


def read_tail(path, keep, *, max_bytes=None, max_line=None):
    """Read the newest valid objects without parsing the entire append file."""
    if keep <= 0:
        return [], dict(status="ready", message="Metric history retention is disabled.")
    old, skipped, oversized, consumed = [], 0, 0, 0
    current, dropping = b"", False

    def accept(line):
        nonlocal skipped, oversized
        if not line.strip():
            return
        if max_line is not None and len(line) > max_line:
            oversized += 1
            return
        try:
            value = json.loads(line)
        except (ValueError, RecursionError):
            skipped += 1
            return
        if isinstance(value, dict):
            old.append(value)
        else:
            skipped += 1

    try:
        opener = lambda target, flags: os.open(target, flags | getattr(os, "O_NONBLOCK", 0))
        with open(path, "rb", opener=opener) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("Saved metric history must be a regular file.")
            position = stream.seek(0, os.SEEK_END)
            while position > 0 and len(old) < keep:
                size = min(READ_CHUNK, position)
                if max_bytes is not None:
                    size = min(size, max_bytes - consumed)
                if size <= 0:
                    break
                position -= size
                stream.seek(position)
                block = stream.read(size)
                consumed += len(block)
                if not block:
                    break
                parts = block.split(b"\n")
                if len(parts) == 1:
                    if not dropping:
                        current = parts[0] + current
                        if max_line is not None and len(current) > max_line:
                            current, dropping = b"", True
                            oversized += 1
                    continue
                if not dropping:
                    accept(parts[-1] + current)
                for line in reversed(parts[1:-1]):
                    if len(old) >= keep:
                        break
                    accept(line)
                current, dropping = parts[0], False
                if max_line is not None and len(current) > max_line:
                    current, dropping = b"", True
                    oversized += 1
            if position == 0 and not dropping and len(old) < keep:
                accept(current)
        limited = bool(oversized or position > 0 and len(old) < keep)
        return list(reversed(old[-keep:])), dict(status="limited" if limited else "ready",
            message=("Saved history restore reached its read limit; use a headless export for the complete retained history."
                     if limited else "Saved metric history restored."),
            bytes=consumed, ignored=skipped, oversized=oversized)
    except FileNotFoundError:
        return [], dict(status="missing", message="No saved metric history; live samples remain available.")
    except OSError as exc:
        return [], dict(status="error", message="Could not restore saved metrics: " + str(exc)[:256])


class SeriesArchive:
    def __init__(self, store):
        self.store = store
        self.submit = None
        self.epoch = 0
        self.worker = False
        self.pending = OrderedDict()
        self.active = {}
        self.states = OrderedDict()
        # Keep degradation flags alongside the retained job caches even after
        # their transient status message leaves the small diagnostics LRU.
        self.limited = set()
        self.disk_ids = set()
        self.index_at = None

    def configure(self, submit):
        with self.store.lock:
            self.epoch += 1
            self.submit = submit
            self.worker = False
            self.pending.clear()
            self.active.clear()

    def _identity(self, jid):
        record, _ = self.store.record_context(jid)
        return (self.store._job_attempts.get(jid, 0),
                getattr(record, "submit", None), getattr(record, "start", None))

    def _status(self, key, value):
        self.states[key] = value
        self.states.move_to_end(key)
        while len(self.states) > MAX_STATUS:
            self.states.popitem(last=False)

    def _schedule(self, key, identity):
        with self.store.lock:
            if self.submit is None:
                self._status(key, dict(status="unavailable", message="Saved metric restore needs the background sampler."))
                return
            if self.active.get(key, _INDEX) == identity:
                return
            if key not in self.pending and len(self.pending) + len(self.active) >= MAX_REQUESTS:
                self._status(key, dict(status="busy", message="Restoring other saved histories; this view will retry."))
                return
            self.pending[key] = identity
            self._status(key, dict(status="loading", message="Restoring saved metric history in the background."))
            if self.worker:
                return
            self.worker = True
            epoch, submit = self.epoch, self.submit
        try:
            submit(lambda: self._drain(epoch))
        except RuntimeError:
            with self.store.lock:
                if epoch == self.epoch:
                    self.worker = False
                    self.pending.clear()
                    self._status(key, dict(status="unavailable", message="Saved metric restore stopped with the sampler."))

    def _merge(self, jid, identity, old, status, *, epoch=None):
        with self.store.lock:
            if ((epoch is not None and epoch != self.epoch) or self._identity(jid) != identity):
                return
            mem = list(self.store.series[jid])
            seen = {key for sample in mem if (key := _sample_key(sample)) is not None}
            merged = [sample for sample in old if _sample_key(sample) not in seen] + mem
            merged.sort(key=_time)
            retained = merged[-self.store.series_keep:] if self.store.series_keep > 0 else []
            from .series_publication import SeriesDeque, managed_record
            source = SeriesDeque(maxlen=self.store.series_keep, lock=self.store.lock)
            previous = self.store.series[jid]
            external = {id(sample) for sample in mem if not managed_record(sample) or sample._revision.series is not previous}
            for sample in retained:
                if id(sample) in external:
                    source.append(sample)
                else:
                    source.append_owned(sample)
            self.store.series[jid] = source
            self.store._series_loaded.add(jid)
            if status.get("status") == "limited":
                self.limited.add(jid)
            else:
                self.limited.discard(jid)
            self._status(jid, status)

    def read(self, jid, *, background=False, publication=False):
        def published():
            from .series_publication import SeriesDeque
            source = self.store.series[jid]
            return source.publication() if publication and type(source) is SeriesDeque else list(source)
        with self.store.lock:
            if jid in self.store._series_loaded and (background or jid not in self.limited):
                return published()
            path, identity = self.store._series_path(jid), self._identity(jid)
            if path is None:
                self.store._series_loaded.add(jid)
                return published()
            memory = published()
        if background:
            self._schedule(jid, identity)
            return memory
        old, status = read_tail(path, self.store.series_keep)
        self._merge(jid, identity, old, status)
        with self.store.lock:
            return published()

    def jobs(self, *, background=False):
        if not background:
            with self.store.lock:
                ids = set(self.store.series)
            if self.store.persist:
                try:
                    ids.update(name[:-6] for name in os.listdir(os.path.join(self.store.state_dir, "series")) if name.endswith(".jsonl"))
                except OSError:
                    pass
            return sorted(ids)
        with self.store.lock:
            ids = set(self.store.series) | self.disk_ids
            requested = (self.store.persist and
                         (self.index_at is None or time.monotonic() - self.index_at >= INDEX_INTERVAL))
        if requested:
            self._schedule(_INDEX, None)
        return sorted(ids)

    def status(self, jid):
        with self.store.lock:
            if jid in self.limited and jid not in self.states:
                return dict(status="limited", message="Saved history restore reached its read limit; use a headless export for the complete retained history.")
            return dict(self.states.get(jid, {}))

    def _drain(self, epoch):
        while True:
            with self.store.lock:
                if epoch != self.epoch:
                    return
                if not self.pending:
                    self.worker = False
                    return
                key, identity = self.pending.popitem(last=False)
                self.active[key] = identity
                path = self.store._series_path(key) if key is not _INDEX else None
            try:
                if key is _INDEX:
                    with os.scandir(os.path.join(self.store.state_dir, "series")) as entries:
                        ids = {entry.name[:-6] for entry in entries if entry.name.endswith(".jsonl")}
                    status = dict(status="ready", message="Saved metric inventory restored.")
                    with self.store.lock:
                        if epoch == self.epoch:
                            self.disk_ids = ids
                            self.index_at = time.monotonic()
                            self._status(_INDEX, status)
                else:
                    old, status = read_tail(path, self.store.series_keep,
                                            max_bytes=COLD_READ_BYTES, max_line=COLD_LINE_BYTES) if path else ([], {})
                    self._merge(key, identity, old, status, epoch=epoch)
            except Exception as exc:
                # A corrupt saved record or disappearing directory cannot leave
                # the sole archive worker permanently marked as running.
                with self.store.lock:
                    if epoch == self.epoch and (key is _INDEX or self._identity(key) == identity):
                        self._status(key, dict(status="error", message="Could not restore saved metrics: " + str(exc)[:256]))
                        if key is _INDEX:
                            self.index_at = time.monotonic()
                        else:
                            self.store._series_loaded.add(key)
            finally:
                with self.store.lock:
                    if epoch == self.epoch:
                        self.active.pop(key, None)
