"""Bounded, exact native metric preparations and indexed observation windows."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import OrderedDict
import pickle

from . import charts
from raven.recordings.cache_limits import MAX_SERIES_CACHE_ENTRIES as MAX_ENTRIES, MAX_SERIES_CACHE_BYTES as MAX_TOTAL_BYTES

MAX_RECORDS = 64000
MAX_SIGNATURE_BYTES = 4 << 20


class _Uncacheable(Exception):
    pass


class _BoundedSink:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        if len(self.data) + len(data) > MAX_SIGNATURE_BYTES:
            raise _Uncacheable
        self.data.extend(data)
        return len(data)


class _NativePickler(pickle.Pickler):
    def reducer_override(self, value):
        # Plain builtin containers/scalars take pickle's C fast paths. Any
        # extension reaches this hook before its __reduce__ can perform work.
        raise _Uncacheable


def signature(values):
    """Exact bytes, including bool/int, signed zero, NaN, and mapping order.

    These bytes are only compared; they are never deserialized. Restricting
    reducers prevents plugin objects from executing code on a UI frame.
    """
    if type(values) not in (list, tuple) or len(values) > MAX_RECORDS:
        return None
    sink = _BoundedSink()
    try:
        _NativePickler(sink, protocol=4).dump(values)
    except (_Uncacheable, pickle.PickleError, RecursionError, TypeError, ValueError):
        return None
    return bytes(sink.data)


def owned_snapshot(value, memo=None):
    """Copy plain containers without invoking extension copy/reducer hooks.

    Each shallow builtin copy captures that container before its descendants
    are visited. Memoization preserves shared containers and tuple/list cycles;
    a subsequent signature describes exactly the owned snapshot being built.
    """
    memo = {} if memo is None else memo
    identity, kind = id(value), type(value)
    if identity in memo:
        return memo[identity]
    if kind is dict:
        result = dict.copy(value)
        memo[identity] = result
        for key, item in result.items():
            if type(item) in (dict, list, tuple, bytearray, set):
                result[key] = owned_snapshot(item, memo)
        return result
    if kind is list:
        result = list.copy(value)
        memo[identity] = result
        for index, item in enumerate(result):
            if type(item) in (dict, list, tuple, bytearray, set):
                result[index] = owned_snapshot(item, memo)
        return result
    if kind is tuple:
        parts = [owned_snapshot(item, memo) if type(item) in (dict, list, tuple, bytearray, set)
                 else item for item in value]
        # A list descendant can have copied this same tuple during recursion.
        if identity in memo:
            return memo[identity]
        result = value if all(part is original for part, original in zip(parts, value)) else tuple(parts)
        memo[identity] = result
        return result
    if kind is bytearray:
        result = bytearray.copy(value)
        memo[identity] = result
        return result
    if kind is set:
        result = set.copy(value)
        memo[identity] = result
        return result
    return value


class NativeSeriesCache:
    def __init__(self, *, budget=None):
        self.budget = budget
        self.bytes = 0
        from .cache_budget import BudgetCache
        self.entries = BudgetCache(budget, self._expired) if budget else OrderedDict()

    def _expired(self, entry):
        self.bytes -= len(entry[0]) if isinstance(entry[0],bytes) else entry[2]

    def remember(self, key, samples, build):
        from raven.recordings.publication import PublishedSeries
        if type(samples) is PublishedSeries:
            # A Store-owned publication already captured nested corrections;
            # comparing its retained identity is constant-time and exact.
            token = samples.token
            entry = self.entries.get(key)
            if entry is not None and entry[0] is token:
                self.entries.move_to_end(key)
                return entry[1]
            prepared = build(samples.records)
            if entry is not None:
                old = self.entries.pop(key)
                self.bytes -= len(old[0]) if isinstance(old[0], bytes) else old[2]
            # Bound managed preparations by observation count as well as the
            # same byte budget. Unusually large sources take the uncached path.
            cost = samples.byte_size
            if len(samples) <= MAX_RECORDS and cost <= MAX_TOTAL_BYTES:
                self.entries[key] = token, prepared, cost
                self.bytes += cost
                while len(self.entries) > MAX_ENTRIES or self.bytes > MAX_TOTAL_BYTES:
                    _, old = self.entries.popitem(last=False)
                    self.bytes -= len(old[0]) if isinstance(old[0], bytes) else old[2]
            return prepared
        token = signature(samples)
        entry = self.entries.get(key)
        if token is not None and entry is not None and entry[0] == token:
            self.entries.move_to_end(key)
            return entry[1]
        # A producer may correct nested counters after the cheap hit check.
        # Sign the owned data used by the builder, never an earlier raw state.
        try:
            snapshot = owned_snapshot(samples) if token is not None else samples
        except RecursionError:
            # Very deep ignored metadata can be signed by pickle's C stack
            # but exceed the Python snapshot stack. Keep its original reader.
            snapshot, token = samples, None
        token = signature(snapshot) if token is not None else None
        prepared = build(snapshot)
        if entry is not None:
            old = self.entries.pop(key)
            self.bytes -= len(old[0]) if isinstance(old[0], bytes) else old[2]
        if token is not None:
            self.entries[key] = token, prepared
            self.bytes += len(token)
            while len(self.entries) > MAX_ENTRIES or self.bytes > MAX_TOTAL_BYTES:
                _, old = self.entries.popitem(last=False)
                self.bytes -= len(old[0]) if isinstance(old[0], bytes) else old[2]
        return prepared


class ObservationIndex:
    """Exact chronological lookup, preserving original duplicate/source order."""
    def __init__(self, timestamps):
        self.timestamps = timestamps
        self.order = sorted(range(len(timestamps)), key=timestamps.__getitem__)
        self.sorted_times = [timestamps[index] for index in self.order]

    def window(self, values, bounds, *, latest=None):
        lower, upper = bounds
        end = len(self.order) if latest is None else bisect_right(self.sorted_times, latest)
        left = min(end, bisect_left(self.sorted_times, lower))
        right = min(end, bisect_right(self.sorted_times, upper))
        visible = sorted(self.order[left:right])
        selected = list(visible)
        if left:
            selected.append(self.order[left - 1])
        if right < end:
            selected.append(self.order[right])
        selected.sort()
        return ([values[index] for index in selected], [self.timestamps[index] for index in selected],
                [charts._finite(values[index]) for index in visible],
                self.sorted_times[end - 1] if end else None)
