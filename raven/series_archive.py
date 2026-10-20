"""Bounded background restore of locally persisted metric histories.

The sampler's existing executor supplies one reader. Foreground views return
published memory immediately; headless callers retain synchronous restoration.
Filesystem operations and JSON parsing never run under the Store lock.
"""
from __future__ import annotations

from collections import OrderedDict
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
    # Compact is authoritative after verified conversion; JSONL stays intact
    # as recovery evidence. File inspection remains outside the Store lock.
    if path and (str(path).endswith(".rvb") or os.path.basename(path).startswith("v2-")):
        from .series_storage import compact_path, json_path
        packed = compact_path(path)
        if os.path.exists(packed):
            from .series_blocks import read
            return read(packed, keep, max_bytes=max_bytes if max_bytes is not None else COLD_READ_BYTES)
        path = json_path(path)
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
            from .series_blocks import _json
            value = _json(line)
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
        self.empty = OrderedDict()

    def configure(self, submit):
        with self.store.lock:
            self.epoch += 1
            self.submit = submit
            self.worker = False
            self.pending.clear()
            self.active.clear()

    def _identity(self, jid):
        return self.store.capture_series_identity(jid)

    def _status(self, key, value):
        self.states[key] = value
        self.states.move_to_end(key)
        while len(self.states) > MAX_STATUS:
            self.states.popitem(last=False)

    def _remember_empty(self, jid, identity):
        self.empty[jid] = identity
        self.empty.move_to_end(jid)
        maximum = self.store.raven_limits.values['max_series_jobs'] if self.store.raven_limits else MAX_STATUS
        while len(self.empty) > maximum:
            old, _ = self.empty.popitem(last=False)
            if old not in self.store.series and self.store.record_context(old)[0] is None:
                for mapping in (self.store._recording_identities, self.store._recording_observations,
                                self.store._recording_evidence, self.store._recording_bindings,
                                self.store._sample_recording_proofs, self.store._isolated_recording_paths):
                    mapping.pop(old, None)

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
            if not identity.verified:
                status = dict(status, attribution="session-only", message=status.get("message", "") +
                    " Attempt identity is unverified; a matching submission/start identity and restart counter are required."
                    " These recordings are isolated to this session. Legacy files are preserved but not merged.")
            mem = list(self.store.series.get(jid, ()))
            seen = {key for sample in mem if (key := _sample_key(sample)) is not None}
            merged = [sample for sample in old if _sample_key(sample) not in seen] + mem
            merged.sort(key=_time)
            retained = merged[-self.store.series_keep:] if self.store.series_keep > 0 else []
            from .series_publication import SeriesDeque, managed_record
            source = SeriesDeque(maxlen=self.store.series_keep, lock=self.store.lock)
            previous = self.store.series.get(jid)
            external = {id(sample) for sample in mem if not managed_record(sample) or sample._revision.series is not previous}
            for sample in retained:
                if id(sample) in external:
                    source.append(sample)
                else:
                    source.append_owned(sample)
            if retained:
                self.store.series[jid] = source
            else:
                self.store.series.pop(jid, None)
            if self.store.raven_limits:
                self.store.raven_limits.retain_series(self.store,jid)
                if len(source)<len(retained):
                    status=dict(status,status='limited',message='Light series byte budget reached; retained history is partial.')
            if retained:
                self.store._series_loaded.add(jid)
                self.empty.pop(jid, None)
            else:
                self._remember_empty(jid, identity)
            if status.get("status") == "limited":
                self.limited.add(jid)
            else:
                self.limited.discard(jid)
            self._status(jid, status)
            self.store._notify_ui()

    def read(self, jid, *, background=False, publication=False):
        def published():
            from .series_publication import SeriesDeque
            source = self.store.series.get(jid, ())
            if source and self.store.raven_limits and jid not in self.store.raven_limits.series:
                self.store.raven_limits.retain_series(self.store,jid)
            return source.publication() if publication and type(source) is SeriesDeque else list(source)
        with self.store.lock:
            if jid in self.store._series_loaded and (background or jid not in self.limited):
                return published()
            path, identity = self.store._series_path(jid), self._identity(jid)
            if self.empty.get(jid) == identity:
                self.empty.move_to_end(jid)
                return published()
            if path is None:
                self._remember_empty(jid, identity)
                return published()
            memory = published()
        if background:
            self._schedule(jid, identity)
            return memory
        limits=self.store.raven_limits
        old, status = read_tail(path, self.store.series_keep,
                               max_bytes=limits.values['series_bytes'] if limits else None,
                               max_line=min(1<<20,limits.values['series_bytes']) if limits else None)
        self._merge(jid, identity, old, status)
        with self.store.lock:
            return published()

    def jobs(self, *, background=False):
        if not background:
            with self.store.lock:
                ids = set(self.store.series)
            if self.store.persist:
                try:
                    ids.update(self._disk_inventory())
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

    def _disk_inventory(self):
        ids=set()
        limits=self.store.raven_limits
        cap=limits.values['storage_files'] if limits else None
        with os.scandir(os.path.join(self.store.state_dir,'series')) as entries:
            for index,entry in enumerate(entries):
                if cap and (index>=cap*2 or len(ids)>=cap):
                    limits.coverage['archive_inventory']={'limited':True,'retained':len(ids),
                        'reason':'saved history inventory scan budget reached'}
                    break
                from .series_identity import inventory_job
                jid = inventory_job(entry.name)
                if jid:
                    ids.add(jid)
                elif entry.name.endswith('.jsonl'):
                    # Keep old jobs discoverable for recovery, without silently
                    # attaching their ambiguous samples to current attempts.
                    ids.add(entry.name[:-6])
        return ids

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
                path = self.store._series_identity_path(identity) if key is not _INDEX else None
            try:
                if key is _INDEX:
                    ids=self._disk_inventory()
                    status = dict(status="ready", message="Saved metric inventory restored.")
                    with self.store.lock:
                        if epoch == self.epoch:
                            self.disk_ids = ids
                            self.index_at = time.monotonic()
                            self._status(_INDEX, status)
                else:
                    limits=self.store.raven_limits
                    old, status = read_tail(path, self.store.series_keep,
                                            max_bytes=min(COLD_READ_BYTES,limits.values['series_bytes']) if limits else COLD_READ_BYTES,
                                            max_line=min(COLD_LINE_BYTES,limits.values['series_bytes'],1<<20) if limits else COLD_LINE_BYTES) if path else ([], {})
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
