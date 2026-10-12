"""Revisioned scalar histories for allocation-free unchanged render snapshots.

Native writers hold Store.lock. Every deque mutation invalidates its immutable
tuple, including edits to old samples. External/plugin deques retain the safe
uncached conversion path; neither length nor the final sample proves equality.
"""
from collections import deque


class HistoryDeque(deque):
    def __init__(self, iterable=(), maxlen=600):
        super().__init__(iterable, maxlen=maxlen)
        self.revision = 0
        self._published_revision = -1
        self._published = ()

    def immutable(self):
        if self._published_revision != self.revision:
            self._published = tuple(self)
            self._published_revision = self.revision
        return self._published

    def _changed(self):
        self.revision += 1

    def append(self, value):
        super().append(value)
        self._changed()

    def appendleft(self, value):
        super().appendleft(value)
        self._changed()

    def extend(self, values):
        try:
            super().extend(values)
        finally:
            # A failing iterator may already have appended a prefix.
            self._changed()

    def extendleft(self, values):
        try:
            super().extendleft(values)
        finally:
            self._changed()

    def clear(self):
        super().clear()
        self._changed()

    def pop(self):
        value = super().pop()
        self._changed()
        return value

    def popleft(self):
        value = super().popleft()
        self._changed()
        return value

    def remove(self, value):
        super().remove(value)
        self._changed()

    def reverse(self):
        super().reverse()
        self._changed()

    def rotate(self, count=1):
        super().rotate(count)
        self._changed()

    def insert(self, index, value):
        super().insert(index, value)
        self._changed()

    def __setitem__(self, index, value):
        super().__setitem__(index, value)
        self._changed()

    def __delitem__(self, index):
        super().__delitem__(index)
        self._changed()

    def __iadd__(self, values):
        self.extend(values)
        return self

    def __imul__(self, count):
        super().__imul__(count)
        self._changed()
        return self


def immutable_histories(histories):
    """Isolate the mapping and reuse native immutable scalar values."""
    return {key: values.immutable() if type(values) is HistoryDeque else tuple(values)
            for key, values in histories.items()}
