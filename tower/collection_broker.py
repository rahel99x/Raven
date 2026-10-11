"""Process-local, identity-scoped query coalescing with bounded workers and leases.

Loaders must honor the supplied cancellation Event and their request deadline.
The broker does not claim it can forcibly terminate arbitrary Python callbacks.
Subprocess adapters implement hard deadlines in adapter_protocol instead.
"""
from __future__ import annotations

from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass, field
import copy
import threading
import time

from . import evidence as E, site_policy as P


@dataclass
class _Entry:
    key: str
    scope: str
    policy: str
    admission: dict
    created: float
    stop: threading.Event = field(default_factory=threading.Event)
    future: object = None
    users: int = 0
    invalid: bool = False
    finished: float = 0
    result_bytes: int = 0
    subscribers: dict = field(default_factory=dict)
    receipt: dict = field(default_factory=dict)


class _SubscriberStop(threading.Event):
    """Cooperative cancellation stays live while the caller executes a loader."""
    def __init__(self, broker, entry):
        super().__init__()
        self.broker, self.entry = broker, entry

    def is_set(self):
        with self.broker._lock:
            subscribers = tuple(self.entry.subscribers.values())
            if subscribers and all(cancel is not None and cancel.is_set() for cancel in subscribers):
                self.entry.invalid = True
                self.set()
        return super().is_set()

    def wait(self, timeout=None):
        until = float("inf") if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            remaining = until - time.monotonic()
            if remaining <= 0:
                return False
            super().wait(min(.02, remaining))
        return True


class _CallerFuture(Future):
    """An admitted read runs on the first result caller's existing worker."""
    def __init__(self, fn, args):
        super().__init__()
        self._invoke, self._invoke_args = fn, args
        self._claim = threading.Lock()
        self._owner_thread = None

    def run(self):
        with self._claim:
            if self.running() and self._owner_thread == threading.get_ident():
                raise ValueError("Recursive acquisition of the same in-flight read")
            if self.running() or self.done():
                return
            if not self.set_running_or_notify_cancel():
                return
            self._owner_thread = threading.get_ident()
        try:
            self.set_result(self._invoke(*self._invoke_args))
        except BaseException as exc:
            self.set_exception(exc)
        finally:
            self._invoke = self._invoke_args = None
            self._owner_thread = None


class _CallerLane:
    """No executor, queue, thread, or additional worker permit is allocated."""
    @staticmethod
    def submit(fn, *args):
        return _CallerFuture(fn, args)

    @staticmethod
    def shutdown(wait=True, *, cancel_futures=False):
        pass


class Lease:
    def __init__(self, broker, entry, cancel=None):
        self._broker, self._entry, self._cancel = broker, entry, cancel
        self._closed = False
        self._lock = threading.Lock()
        self._token = object()
        with broker._lock:
            entry.subscribers[self._token] = cancel

    @property
    def receipt(self):
        with self._broker._lock:
            return dict(self._entry.receipt)

    def result(self, timeout=None):
        if timeout is not None:
            E.finite(timeout, "lease timeout", 0)
        deadline = min(self._entry.created + self._entry.admission["request"]["duration"] if not self._entry.future.done() else float("inf"),
                       time.monotonic() + timeout if timeout is not None else float("inf"))
        while True:
            with self._lock:
                if self._closed:
                    raise ValueError("Collection lease is closed")
            if self._cancel is not None and self._cancel.is_set():
                self.cancel()
                raise ValueError("Collection lease cancelled")
            with self._broker._lock:
                if self._entry.invalid:
                    raise ValueError("Collection source identity changed; acquire a new lease")
            remaining = deadline - time.monotonic()
            if remaining <= 0 and not self._entry.future.done():
                self.cancel()
                raise TimeoutError("Collection lease deadline exceeded")
            try:
                if isinstance(self._entry.future, _CallerFuture):
                    self._entry.future.run()
                scheduler = self._broker._scheduler
                if scheduler is not None and scheduler.current() is scheduler:
                    # A native bounded child can execute on this same permit;
                    # opaque loader duration remains its own declared contract.
                    scheduler.gather_children((self._entry.future,), timeout=min(.05, remaining))
                result = self._entry.future.result(timeout=min(.05, max(0, deadline - time.monotonic())))
                with self._lock:
                    if self._closed:
                        raise ValueError("Collection lease is closed")
                if self._cancel is not None and self._cancel.is_set():
                    self.cancel()
                    raise ValueError("Collection lease cancelled")
                with self._broker._lock:
                    if self._entry.invalid:
                        raise ValueError("Collection source changed during the read")
                return copy.deepcopy(result)
            except TimeoutError:
                if self._entry.future.done():
                    raise

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._broker._release(self._entry, self._token)

    cancel = close

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class CollectionBroker:
    def __init__(self, *, workers=2, capacity=64, cache_bytes=16 << 20, lane=None,
                 shared_cache=None, caller_owned=False):
        E.integer(workers, "broker workers", 16)
        E.integer(capacity, "broker capacity", 1024)
        E.integer(cache_bytes, "broker cache bytes", 64 << 20)
        if min(workers, capacity, cache_bytes) < 1:
            raise ValueError("Broker bounds must be positive")
        self._lock = threading.RLock()
        self._shared_cache = shared_cache
        from .worker_scheduler import WorkerScheduler
        current = WorkerScheduler.current()
        self._owned_scheduler = None
        if caller_owned:
            if lane is not None:
                raise ValueError("Caller-owned collection cannot also select a worker lane")
            lane = _CallerLane()
            self._owned_lane = False
        elif lane is None:
            scheduler = current
            if scheduler is None:
                scheduler = self._owned_scheduler = WorkerScheduler(workers=workers, lanes=("source",))
            lane = scheduler.lane("source")
            self._owned_lane = True
        else:
            self._owned_lane = False
        self._pool = lane
        self._scheduler = getattr(lane, "scheduler", None)
        self._entries, self._last = {}, {}
        self._capacity, self._cache_bytes = capacity, cache_bytes
        self._closed = False

    def acquire(self, scope, query, loader, policy, observed, cancel=None):
        E.keys(scope, {"connection", "generation"}, {"user", "cluster"}, "query scope")
        for value in scope.values():
            E.text(value, "query scope", 512)
        E.keys(query, {"request", "parameters"}, label="query")
        if not isinstance(query["parameters"], dict) or not callable(loader):
            raise ValueError("Query parameters and loader are invalid")
        admission = P.admit(policy.value if isinstance(policy, P.SitePolicy) else policy, query["request"], observed)
        if not admission["allowed"]:
            raise ValueError("Collection refused: " + "; ".join(admission["reasons"]))
        if cancel is not None and cancel.is_set():
            raise ValueError("Collection cancelled before admission")
        scope_id = E.digest(scope)
        key = E.digest({"scope": scope, "query": query, "policy": admission["policy_digest"], "capabilities": admission["capabilities_digest"]})
        now = time.monotonic()
        with self._lock:
            if self._closed:
                raise ValueError("Collection broker is closed")
            self._prune(now)
            entry = self._entries.get(key)
            if entry is not None and entry.invalid and not entry.future.done():
                raise ValueError("Previous collection is still draining cancellation")
            if entry is not None and not entry.invalid:
                if not entry.future.done() or (self._shared_cache is None and entry.future.exception() is None and now - entry.finished < query["request"]["interval"]):
                    entry.users += 1
                    return Lease(self, entry, cancel)
            if entry is not None and entry.future.done():
                self._entries.pop(key)
                if entry.users:
                    # Retain outstanding leases in capacity/byte accounting
                    # while allowing a new cache-authoritative observation.
                    entry.key = key + ":retired:" + str(id(entry))
                    self._entries[entry.key] = entry
            floor = admission["effective_min_interval"]
            throttle_key = E.digest({"scope": scope, "source": query["request"]["source"], "operation": query["request"]["operation"]})
            if self._shared_cache is None and now - self._last.get(throttle_key, float("-inf")) < floor:
                raise ValueError("Source query is inside its minimum interval; reuse the existing lease")
            active = sum(not item.future.done() and item.policy == admission["policy_digest"] for item in self._entries.values())
            if active >= admission["max_inflight"] or len(self._entries) >= self._capacity:
                raise ValueError("Collection broker admission capacity reached")
            reserved = sum(item.result_bytes if item.future.done() else item.admission["request"]["max_bytes"] for item in self._entries.values())
            if reserved + query["request"]["max_bytes"] > self._cache_bytes:
                raise ValueError("Collection broker byte budget reached")
            entry = _Entry(key, scope_id, admission["policy_digest"], admission, now, users=1)
            entry.stop = _SubscriberStop(self, entry)
            lease = Lease(self, entry, cancel)
            previous_entry = self._entries.get(key)
            previous_time = self._last.get(throttle_key)
            self._entries[key] = entry
            if self._shared_cache is None:
                self._last[throttle_key] = now
            try:
                if self._shared_cache is not None:
                    original = loader
                    # Preserve the shared cache as authority for cross-process
                    # freshness, throttling and invalidation. The local broker
                    # only shares a read while it is in flight.
                    arguments = copy.deepcopy((scope, query))
                    def loader(stop):
                        selected_scope, selected_query = arguments
                        result = self._shared_cache._query_admitted(selected_scope, selected_query, original,
                                                                    entry.admission, cancel=stop)
                        with self._lock:
                            entry.receipt = {key: value for key, value in result.items() if key != "value"}
                        return result["value"]
                entry.future = self._pool.submit(self._load, entry, loader)
            except BaseException:
                if previous_entry is None:
                    self._entries.pop(key, None)
                else:
                    self._entries[key] = previous_entry
                if previous_time is None:
                    self._last.pop(throttle_key, None)
                else:
                    self._last[throttle_key] = previous_time
                raise
            return lease

    def _load(self, entry, loader):
        try:
            if entry.stop.is_set() or time.monotonic() >= entry.created + entry.admission["request"]["duration"]:
                raise ValueError("Collection cancelled or expired before execution")
            result = loader(entry.stop)
            size = entry.receipt.get("bytes") if self._shared_cache is not None else None
            if size is None:
                size = len(E.canonical(result))
            if size > entry.admission["request"]["max_bytes"]:
                raise ValueError("Collector result exceeds admitted byte budget")
            count = len(result.get("records", [])) if isinstance(result, dict) and isinstance(result.get("records"), list) else len(result) if isinstance(result, list) else 1
            if count > entry.admission["request"]["max_records"]:
                raise ValueError("Collector result exceeds admitted record budget")
            if entry.stop.is_set() or time.monotonic() >= entry.created + entry.admission["request"]["duration"]:
                raise ValueError("Collection expired or lost all subscribers")
            with self._lock:
                entry.result_bytes = size
            # The cache already returned a detached value. Lease.result gives
            # every subscriber its own copy; no third full-output copy here.
            return result if self._shared_cache is not None else copy.deepcopy(result)
        finally:
            with self._lock:
                entry.finished = time.monotonic()

    def _release(self, entry, token):
        with self._lock:
            entry.subscribers.pop(token, None)
            entry.users = max(0, entry.users - 1)
            if entry.users == 0 and not entry.future.done():
                entry.invalid = True
                entry.stop.set()
                entry.future.cancel()
            if entry.users == 0 and entry.future.done() and self._shared_cache is not None and self._entries.get(entry.key) is entry:
                del self._entries[entry.key]

    def _prune(self, now):
        for key, entry in list(self._entries.items()):
            if not entry.future.done() and now >= entry.created + entry.admission["request"]["duration"]:
                entry.invalid = True
                entry.stop.set()
                entry.future.cancel()
            if entry.future.done() and not entry.users and (self._shared_cache is not None or entry.invalid or entry.future.cancelled() or entry.future.exception() is not None or now - entry.finished >= entry.admission["request"]["interval"]):
                del self._entries[key]
        total = sum(item.result_bytes for item in self._entries.values())
        for key, entry in sorted(self._entries.items(), key=lambda pair: pair[1].finished):
            if total <= self._cache_bytes:
                break
            if not entry.users and entry.future.done():
                total -= entry.result_bytes
                del self._entries[key]
        # Throttle state is bounded independently; entries younger than one day
        # cannot be evicted without losing their contractual interval floor.
        self._last = {key: when for key, when in self._last.items() if now - when <= 86400}
        if len(self._last) >= self._capacity * 4:
            raise ValueError("Broker source throttle capacity reached")

    def invalidate(self, scope):
        identity = E.digest(scope)
        with self._lock:
            if self._shared_cache is not None:
                self._shared_cache.invalidate(scope)
            for entry in self._entries.values():
                if entry.scope == identity:
                    entry.invalid = True
                    entry.stop.set()
                    entry.future.cancel()

    def close(self, wait=True):
        with self._lock:
            self._closed = True
            for entry in self._entries.values():
                entry.invalid = True
                entry.stop.set()
                entry.future.cancel()
        if self._owned_lane:
            self._pool.shutdown(wait=wait, cancel_futures=True)
        if self._owned_scheduler is not None:
            self._owned_scheduler.shutdown(wait=wait, cancel_futures=True)

    def stats(self):
        with self._lock:
            return {"entries": len(self._entries), "active": sum(not e.future.done() for e in self._entries.values()),
                    "leases": sum(e.users for e in self._entries.values()), "cache_bytes": sum(e.result_bytes for e in self._entries.values()),
                    "closed": self._closed}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
