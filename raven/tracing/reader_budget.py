"""Shared admission accounting for retained trace-reader working state.

Charges include conservative Python-container overhead. This is not an RSS
limit: transient decoding, caller snapshots, SQLite and the rest of Raven have
their own bounds. A refused charge does not make source evidence corrupt.
"""

from __future__ import annotations

import threading
import sys


def retained_size(value):
    """Conservatively size a bounded JSON object graph before retaining it.

    Objects shared with another retained component are deliberately counted
    again. Temporary decoding/traversal storage is outside this state charge.
    """
    pending, seen, total = [value], set(), 0
    while pending:
        item = pending.pop()
        address = id(item)
        if address in seen:
            continue
        seen.add(address)
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return total


class ReaderMemoryLimit(RuntimeError):
    """A reader cannot retain more state inside its owner's shared budget."""


class ReaderMemoryBudget:
    def __init__(self, limit):
        if type(limit) is not int or not 1 << 20 <= limit <= 1 << 30:
            raise ValueError("Trace reader working-state budget must be 1 MiB–1 GiB")
        self.limit = limit
        self.used = 0
        self.peak = 0
        self._lock = threading.Lock()

    def reserve(self, amount, component):
        if type(amount) is not int or amount < 0:
            raise ValueError("Invalid trace reader working-state charge")
        with self._lock:
            if amount > self.limit - self.used:
                raise ReaderMemoryLimit(
                    f"Trace reader working-state budget exhausted while retaining {component}: "
                    f"{self.used} charged bytes + {amount} requested exceeds {self.limit}; "
                    "raw recordings are unchanged. Reopen fewer sources or increase the reader working-state budget."
                )
            self.used += amount
            self.peak = max(self.peak, self.used)

    def release(self, amount):
        if type(amount) is not int or amount < 0:
            raise ValueError("Invalid trace reader working-state release")
        with self._lock:
            if amount > self.used:
                raise ValueError(
                    "Trace reader working-state release exceeds its charge"
                )
            self.used -= amount

    def snapshot(self):
        with self._lock:
            return dict(
                limit_bytes=self.limit,
                charged_bytes=self.used,
                peak_charged_bytes=self.peak,
                scope="Shared retained reader state with conservative container overhead; not process RSS or transient decoding",
            )
