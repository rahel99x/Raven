"""Aggregate bounded in-memory cache accounting, independent of UI wiring."""

from collections import OrderedDict, deque
import sys
import threading


def retained_size(value, limit=32 << 20):
    """Bounded conservative Python-container accounting, never a serialization."""
    pending, seen, total = [value], set(), 0
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if len(seen) > 100000:
            return limit + 1
        kind = type(item)
        if isinstance(item, dict):
            total += dict.__sizeof__(item)
            pending.extend(dict.keys(item))
            pending.extend(dict.values(item))
        elif isinstance(item, (list, tuple, deque, set, frozenset)):
            total += sys.getsizeof(item)
            pending.extend(item)
        elif kind in (
            str,
            bytes,
            bytearray,
            int,
            float,
            bool,
            type(None),
            object,
            type,
        ):
            total += sys.getsizeof(item)
        elif kind.__module__.startswith("raven.") and hasattr(item, "__dict__"):
            total += sys.getsizeof(item)
            pending.append(vars(item))
        else:
            return limit + 1  # Unknown extension storage is never admitted.
        if total > limit:
            return total
    return total


class CacheBudget:
    """One aggregate LRU across participating UI preparation caches."""

    def __init__(self, limit):
        self.limit = max(1, int(limit))
        self.used = 0
        self.evictions = 0
        self.entries = OrderedDict()
        self.lock = threading.RLock()

    def forget(self, owner, key):
        with self.lock:
            old = self.entries.pop((id(owner), key), None)
            if old:
                self.used -= old[0]

    def touch(self, owner, key):
        with self.lock:
            ident = id(owner), key
            if ident in self.entries:
                self.entries.move_to_end(ident)

    def admit(self, owner, key, value):
        cost = retained_size((key, value), self.limit)
        expired = []
        with self.lock:
            self.forget(owner, key)
            self.entries[(id(owner), key)] = cost, owner, value
            self.used += cost
            while self.used > self.limit:
                (_, old_key), (size, old_owner, old_value) = self.entries.popitem(
                    last=False
                )
                self.used -= size
                self.evictions += 1
                expired.append((old_owner, old_key, old_value))
        # No foreign callback runs under the ledger lock. The value identity
        # also prevents a delayed eviction from removing a newer publication.
        for old_owner, old_key, old_value in expired:
            old_owner.expire(old_key, old_value)


class BudgetCache(OrderedDict):
    """Drop-in bounded cache. Owners keep their existing point/count counters."""

    def __init__(self, budget, expired=None):
        super().__init__()
        self.budget, self.expired = budget, expired

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.budget.admit(self, key, value)

    def get(self, key, default=None):
        value = super().get(key, default)
        if key in self:
            self.budget.touch(self, key)
        return value

    def move_to_end(self, key, last=True):
        if key in self:
            super().move_to_end(key, last)
            self.budget.touch(self, key)

    def pop(self, key, *default):
        value = super().pop(key, *default)
        self.budget.forget(self, key)
        return value

    def popitem(self, last=True):
        key, value = super().popitem(last)
        self.budget.forget(self, key)
        return key, value

    def __delitem__(self, key):
        super().__delitem__(key)
        self.budget.forget(self, key)

    def clear(self):
        for key in tuple(self):
            self.budget.forget(self, key)
        super().clear()

    def expire(self, key, expected):
        if super().get(key) is not expected:
            return
        value = super().pop(key, None)
        if value is not None and self.expired:
            self.expired(value)
