"""Owned metric records with revisioned, immutable render publications.

Only records captured through ``append_owned`` qualify for revision shortcuts.
Direct extension writes retain their object identity and the existing exact
content-checking path. Nested corrections through captured records invalidate
the owning record and series under the same Store lock.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Sequence
import copy
from dataclasses import dataclass
import sys
import threading
import weakref

from .history_snapshots import HistoryDeque


class _Unsupported(ValueError):
    pass


class FrozenRecord(dict):
    def _readonly(self, *args, **kwargs):
        raise TypeError("Published metric records are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _readonly

    def __deepcopy__(self, memo):
        return self

    def __reduce_ex__(self, protocol):
        return FrozenRecord, (dict(self),)


def _freeze(value, *, depth=0, budget=None):
    budget = [20000] if budget is None else budget
    budget[0] -= 1
    if depth > 32 or budget[0] < 0:
        raise _Unsupported("Metric record exceeds owned publication bounds")
    if isinstance(value, _RecordDict):
        return FrozenRecord((key, _freeze(item, depth=depth + 1, budget=budget)) for key, item in dict.items(value))
    if isinstance(value, _RecordList) or type(value) is tuple:
        return tuple(_freeze(item, depth=depth + 1, budget=budget) for item in value)
    return value


def _size(value):
    """Conservative retained-size accounting, including shared occurrences."""
    size = sys.getsizeof(value)
    if isinstance(value, FrozenRecord):
        return size + sum(sys.getsizeof(key) + _size(item) for key, item in value.items())
    if type(value) is tuple:
        return size + sum(_size(item) for item in value)
    return size


class _DetachedSeries:
    lock = threading.RLock()
    trusted = False

    def _changed(self):
        pass


_DETACHED = _DetachedSeries()


class _RecordRevision:
    def __init__(self, series):
        self._series_ref = weakref.ref(series)
        self.revision = 0
        self.cached_revision = -1
        self.cached = None
        self.bytes = 0
        self._root_ref = None

    @property
    def series(self):
        owner = self._series_ref()
        return owner if owner is not None else _DETACHED

    @property
    def root(self):
        return self._root_ref() if self._root_ref is not None else None

    def changed(self):
        self.revision += 1
        self.series._changed()

    def publish(self, record):
        if self.cached_revision != self.revision:
            self.cached = _freeze(record)
            self.bytes = _size(self.cached)
            self.cached_revision = self.revision
        return self.cached


def _own(value, revision, *, depth=0, budget=None):
    budget = [20000] if budget is None else budget
    budget[0] -= 1
    if depth > 32 or budget[0] < 0:
        raise _Unsupported("Metric record exceeds owned publication bounds")
    kind = type(value)
    if kind in (type(None), bool, int, float, str):
        return value
    if kind is dict or kind is _RecordDict:
        if any(type(key) is not str for key in value):
            raise _Unsupported("Extension record keys retain their original reader")
        result = _RecordDict(revision)
        for key, item in dict.items(value):
            dict.__setitem__(result, key, _own(item, revision, depth=depth + 1, budget=budget))
        return result
    if kind in (list, tuple, _RecordList):
        items = [_own(item, revision, depth=depth + 1, budget=budget) for item in value]
        return tuple(items) if kind is tuple else _RecordList(revision, items)
    raise _Unsupported("Extension values retain their original reader")


def _replacement(value, revision):
    try:
        return _own(value, revision)
    except _Unsupported:
        # Preserve extension semantics, but never trust its later mutations.
        revision.series.trusted = False
        return value


class _RecordDict(dict):
    def __init__(self, revision):
        self._revision = revision

    def __setitem__(self, key, value):
        with self._revision.series.lock:
            if type(key) is not str:
                self._revision.series.trusted = False
            dict.__setitem__(self, key, _replacement(value, self._revision))
            self._revision.changed()

    def __delitem__(self, key):
        with self._revision.series.lock:
            dict.__delitem__(self, key)
            self._revision.changed()

    def update(self, *args, **kwargs):
        values = dict(*args, **kwargs)
        with self._revision.series.lock:
            for key, value in values.items():
                self[key] = value

    def clear(self):
        with self._revision.series.lock:
            dict.clear(self)
            self._revision.changed()

    def pop(self, key, *default):
        with self._revision.series.lock:
            value = dict.pop(self, key, *default)
            self._revision.changed()
            return value

    def popitem(self):
        with self._revision.series.lock:
            value = dict.popitem(self)
            self._revision.changed()
            return value

    def setdefault(self, key, default=None):
        with self._revision.series.lock:
            if key not in self:
                self[key] = default
            return self[key]

    def __ior__(self, other):
        self.update(other)
        return self

    def __deepcopy__(self, memo):
        result = {}
        memo[id(self)] = result
        for key, value in dict.items(self):
            result[copy.deepcopy(key, memo)] = copy.deepcopy(value, memo)
        return result

    def __reduce_ex__(self, protocol):
        return dict, (_plain(self),)


class _RecordList(list):
    def __init__(self, revision, values=()):
        self._revision = revision
        list.__init__(self, values)

    def _change(self, method, *args):
        with self._revision.series.lock:
            try:
                return method(self, *args)
            finally:
                self._revision.changed()

    def __setitem__(self, index, value):
        with self._revision.series.lock:
            owned = ([_replacement(item, self._revision) for item in value]
                     if isinstance(index, slice) else _replacement(value, self._revision))
            return self._change(list.__setitem__, index, owned)

    def __delitem__(self, index):
        return self._change(list.__delitem__, index)

    def append(self, value):
        with self._revision.series.lock:
            return self._change(list.append, _replacement(value, self._revision))

    def extend(self, values):
        with self._revision.series.lock:
            values = list(values) if values is self else values
            for value in values:
                self.append(value)

    def insert(self, index, value):
        with self._revision.series.lock:
            return self._change(list.insert, index, _replacement(value, self._revision))

    def pop(self, index=-1):
        return self._change(list.pop, index)

    def remove(self, value):
        return self._change(list.remove, value)

    def clear(self):
        return self._change(list.clear)

    def reverse(self):
        return self._change(list.reverse)

    def sort(self, *args, **kwargs):
        with self._revision.series.lock:
            try:
                return list.sort(self, *args, **kwargs)
            finally:
                self._revision.changed()

    def __iadd__(self, values):
        self.extend(values)
        return self

    def __imul__(self, count):
        self._change(list.__imul__, count)
        return self

    def __deepcopy__(self, memo):
        result = []
        memo[id(self)] = result
        result.extend(copy.deepcopy(value, memo) for value in self)
        return result

    def __reduce_ex__(self, protocol):
        return list, (_plain(self),)


def _plain(value, memo=None, *, budget=None, depth=0):
    """Copy native containers without executing extension reducers/hooks."""
    if budget is not None:
        budget[0] -= 1
        if depth > 32 or budget[0] < 0:
            raise _Unsupported("Metric record exceeds snapshot bounds")
    memo = {} if memo is None else memo
    if id(value) in memo:
        return memo[id(value)]
    if (budget is not None and type(value) in (dict, _RecordDict, list, _RecordList, tuple)
            and len(value) > budget[0]):
        raise _Unsupported("Metric container exceeds snapshot bounds")
    if type(value) in (dict, _RecordDict):
        result = {}
        memo[id(value)] = result
        for key, item in dict.copy(value).items():
            result[key] = _plain(item, memo, budget=budget, depth=depth + 1)
        return result
    if type(value) in (list, _RecordList):
        result = []
        memo[id(value)] = result
        result.extend(_plain(item, memo, budget=budget, depth=depth + 1) for item in list.copy(value))
        return result
    if type(value) is tuple:
        parts = [_plain(item, memo, budget=budget, depth=depth + 1) for item in value]
        if id(value) in memo:
            return memo[id(value)]
        result = tuple(parts)
        memo[id(value)] = result
        return result
    return value


def _foreign_snapshot(record):
    if type(record) is _RecordDict:
        try:
            return _plain(record, budget=[20000])
        except (_Unsupported, RecursionError):
            # An oversized extension retains the original uncached reader.
            # Never return/cache a partially copied record or run copy hooks.
            pass
    return record


def managed_record(value):
    return type(value) is _RecordDict and value._revision.root is value


@dataclass(frozen=True)
class PublishedSeries(Sequence):
    """One immutable source revision; derived caches use its identity token."""
    records: tuple
    token: object
    byte_size: int

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        return self.records[index]


class SeriesDeque(HistoryDeque):
    def __init__(self, iterable=(), maxlen=4000, *, lock=None):
        super().__init__((), maxlen=maxlen)
        self.lock = lock if lock is not None else threading.RLock()
        self.trusted = True
        self._source_revision = -1
        self._source = None
        self.extend(iterable)

    def append_owned(self, sample):
        with self.lock:
            try:
                revision = _RecordRevision(self)
                record = _own(sample, revision)
                revision._root_ref = weakref.ref(record) if type(record) is _RecordDict else None
            except _Unsupported:
                record = sample
                self.trusted = False
            self.append(record)
            return record

    def _accept(self, value):
        if not managed_record(value) or value._revision.series is not self:
            self.trusted = False
        return value

    def append(self, value):
        with self.lock:
            super().append(self._accept(value))

    def appendleft(self, value):
        with self.lock:
            super().appendleft(self._accept(value))

    def extend(self, values):
        with self.lock:
            values = list(values) if values is self else values
            for value in values:
                self.append(value)

    def extendleft(self, values):
        with self.lock:
            values = list(values) if values is self else values
            for value in values:
                self.appendleft(value)

    def insert(self, index, value):
        with self.lock:
            super().insert(index, self._accept(value))

    def __setitem__(self, index, value):
        with self.lock:
            super().__setitem__(index, self._accept(value))

    def clear(self):
        with self.lock:
            super().clear()
            self.trusted = True

    def pop(self):
        with self.lock:
            return super().pop()

    def popleft(self):
        with self.lock:
            return super().popleft()

    def remove(self, value):
        with self.lock:
            return super().remove(value)

    def reverse(self):
        with self.lock:
            return super().reverse()

    def rotate(self, count=1):
        with self.lock:
            return super().rotate(count)

    def __delitem__(self, index):
        with self.lock:
            return super().__delitem__(index)

    def __imul__(self, count):
        with self.lock:
            super().__imul__(count)
            return self

    def __deepcopy__(self, memo):
        with self.lock:
            result = deque(maxlen=self.maxlen)
            memo[id(self)] = result
            result.extend(copy.deepcopy(record, memo) for record in self)
            return result

    def __reduce_ex__(self, protocol):
        with self.lock:
            return deque, ((), self.maxlen), None, iter(_plain(list(self)))

    def release_publication(self):
        """Drop prepared data when the Store's source-publication budget evicts it."""
        with self.lock:
            self._source, self._source_revision = None, -1
            for record in self:
                if managed_record(record):
                    record._revision.cached = None
                    record._revision.cached_revision = -1
                    record._revision.bytes = 0

    def publication(self):
        with self.lock:
            if not self.trusted:
                # Foreign mutable entries require exact checking every read.
                # Return plain owned records for native wrappers, retain other
                # entries unchanged so extension semantics stay compatible.
                return [_foreign_snapshot(record) for record in self]
            if self._source_revision != self.revision:
                try:
                    records = tuple(record._revision.publish(record) for record in self)
                except _Unsupported:
                    self.trusted = False
                    return self.publication()
                size = sys.getsizeof(records) + sum(record._revision.bytes for record in self)
                self._source = PublishedSeries(records, object(), size)
                self._source_revision = self.revision
            return self._source
