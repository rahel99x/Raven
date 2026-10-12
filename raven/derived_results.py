"""Bounded shared results for pure analyses, never source acquisition or actions.

Stored JSON bytes are immutable. Callers receive separate decoded values, so a
pane cannot mutate another pane's evidence. Keys retain connection, exact source
identity, evidence revision, algorithm version and settings. All work belongs in
an operation worker; this module performs no I/O and schedules no background work.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass
import hashlib
import json
import threading

from .evidence import canonical, text
from .operations import canonical as result_canonical


class StaleResult(ValueError):
    """A source or selection changed before a derived result was published."""


@dataclass(frozen=True)
class ResultKey:
    token: str

    @classmethod
    def create(cls, *, scope, identity, revision, algorithm, version, settings=None):
        if not isinstance(scope, dict) or not isinstance(identity, dict) or not identity:
            raise ValueError("Derived results require a connection scope and explicit source identity")
        if settings is not None and not isinstance(settings, dict):
            raise ValueError("Derived analysis settings must be an object")
        for value in (revision, algorithm, version):
            text(value, "result key", 512)
        payload = {"scope": scope, "identity": identity, "revision": revision,
                   "algorithm": algorithm, "version": version, "settings": settings or {}}
        return cls(hashlib.sha256(canonical(payload)).hexdigest())


def _check(cancel, current):
    if cancel is not None and cancel.is_set():
        raise ValueError("Derived analysis cancelled")
    if current is not None and not current():
        raise StaleResult("Evidence or selection changed during analysis; inspect again")


class ResultCache:
    def __init__(self, *, max_entries=128, max_bytes=16 << 20, max_pending=32):
        if any(type(n) is not int or n < 1 for n in (max_entries, max_bytes, max_pending)):
            raise ValueError("Result cache limits must be positive integers")
        self.max_entries, self.max_bytes, self.max_pending = max_entries, max_bytes, max_pending
        self._values, self._pending = OrderedDict(), {}
        self._lock = threading.Lock()
        self._bytes = self._hits = self._misses = self._joins = 0

    def get_or_compute(self, key, compute, *, cancel=None, current=None):
        if not isinstance(key, ResultKey):
            raise ValueError("Use a versioned ResultKey")
        _check(cancel, current)
        with self._lock:
            raw = self._values.get(key)
            if raw is not None:
                self._values.move_to_end(key)
                self._hits += 1
                future, owner = None, False
            elif key in self._pending:
                future, owner = self._pending[key], False
                self._joins += 1
            else:
                self._misses += 1
                future, owner = Future(), True
                # Admission remains bounded even if an embedding supplies more
                # callers than Raven's worker pool. Overflow work is not retained.
                if len(self._pending) < self.max_pending:
                    self._pending[key] = future
        if raw is not None:
            _check(cancel, current)
            return json.loads(raw)
        if not owner:
            while True:
                _check(cancel, current)
                try:
                    raw = future.result(timeout=.05)
                    break
                except TimeoutError:
                    if future.done():
                        # The producer may have completed just after the wait
                        # expired. Read its actual outcome, including a genuine
                        # computation TimeoutError, instead of raising our wait.
                        raw = future.result()
                        break
                    continue
            _check(cancel, current)
            return json.loads(raw)
        try:
            raw = result_canonical(compute())
            _check(cancel, current)
            with self._lock:
                if len(raw) <= self.max_bytes:
                    previous = self._values.pop(key, None)
                    self._bytes -= len(previous) if previous is not None else 0
                    self._values[key] = raw
                    self._bytes += len(raw)
                    while len(self._values) > self.max_entries or self._bytes > self.max_bytes:
                        _, evicted = self._values.popitem(last=False)
                        self._bytes -= len(evicted)
            future.set_result(raw)
            return json.loads(raw)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._pending.get(key) is future:
                    del self._pending[key]

    def stats(self):
        with self._lock:
            return {"entries": len(self._values), "bytes": self._bytes,
                    "pending": len(self._pending), "hits": self._hits,
                    "misses": self._misses, "joined": self._joins}


RESULTS = ResultCache()
