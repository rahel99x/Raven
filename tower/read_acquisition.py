"""One read-only acquisition path for governed scheduler and telemetry sources.

The broker shares concurrent subscriptions within this process. SQLite remains
the host-local authority for cached values, source floors and invalidation.
Loaders execute on the calling worker, so changing the session worker budget
does not leave a private executor or an abandoned nested wait behind.
"""
from __future__ import annotations

import os
import threading
import weakref

from .collection_broker import CollectionBroker


class ReadAcquisition:
    def __init__(self, cache):
        self.cache = cache
        self.broker = CollectionBroker(capacity=cache.capacity, cache_bytes=cache.max_bytes,
                                       shared_cache=cache, caller_owned=True)

    def query(self, scope, query, loader, policy, observed, *, read_only=False, cancel=None, fresh=False):
        if read_only is not True:
            raise ValueError("Acquisition is restricted to explicitly read-only queries")
        if type(fresh) is not bool:
            raise ValueError("Collection freshness must be boolean")
        if os.getpid() != self.cache._owner[0]:
            raise ValueError("Create a new acquisition client after forking")
        if fresh:
            # Reviews require an observation begun after this request; they
            # cannot join an older local flight. The cache preserves source
            # floors and waits for older owners to drain before recollecting.
            return self.cache.query(scope, query, loader, policy, observed,
                                    read_only=True, cancel=cancel, fresh=True)
        with self.broker.acquire(scope, query, loader, policy, observed, cancel) as lease:
            value = lease.result()
            return {**lease.receipt, "value": value}

    def invalidate(self, scope):
        self.broker.invalidate(scope)

    def stats(self):
        return self.broker.stats()


_CLIENTS = weakref.WeakValueDictionary()
_LOCK = threading.Lock()


def for_cache(cache):
    """Share active clients without retaining idle sources or their credentials."""
    key = (os.getpid(), str(cache.path), cache.host, cache.capacity, cache.max_bytes)
    with _LOCK:
        client = _CLIENTS.get(key)
        if client is None:
            if len(_CLIENTS) >= 128:
                raise ValueError("Active acquisition client capacity reached")
            client = ReadAcquisition(cache)
            _CLIENTS[key] = client
        return client
