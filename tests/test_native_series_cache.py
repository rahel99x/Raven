"""Prepared publications stay exact, bounded, and safe for extension objects."""
from copy import deepcopy
import math
import random
import struct

import pytest

from raven import charts, native_series_cache as N
from raven.views import _metric_observations, _prepare_native_metrics, _prepare_trace_metrics


def test_content_cache_reuses_preparation_and_detects_deep_in_place_corrections():
    cache, builds = N.NativeSeriesCache(), []
    samples = [{"k": "gpu", "t": 1., "gpu": {"0": [24., None], "1": [28.]}}]

    def prepare(owned):
        result = object()
        builds.append(result)
        return result

    original = cache.remember(("native", "7"), samples, prepare)
    assert cache.remember(("native", "7"), samples, prepare) is original
    samples[0]["gpu"]["0"][0] = 70.
    corrected = cache.remember(("native", "7"), samples, prepare)
    assert corrected is not original
    assert cache.remember(("native", "7"), samples, prepare) is corrected
    samples[0]["t"] = 0.5
    assert cache.remember(("native", "7"), samples, prepare) is not corrected
    assert len(builds) == 3
    assert cache.remember(("native", "8"), samples, prepare) is not builds[2]


@pytest.mark.parametrize("source", ["native", "trace"])
def test_lazy_preparation_owns_records_after_equal_source_replacement(source):
    cache = N.NativeSeriesCache()
    original = ([{"k": "gpu", "t": 1., "gpu": {"0": [24.], "1": [28.]}}]
                if source == "native" else [{"index": 0, "t": 1., "util": 24.}])
    current = original

    def prepare(owned):
        return (_prepare_native_metrics(owned, 0, False, {"live": .5, "gpu": .5}, owned=True)
                if source == "native" else _prepare_trace_metrics(owned, owned=True))

    publication = cache.remember(source, current, prepare)
    # Replace with equal data, then change a detached producer-owned object.
    # An unmaterialized offscreen card must still read the signed publication.
    current = deepcopy(original)
    if source == "native":
        original[0]["gpu"]["0"][0] = 99.
    else:
        original[0]["util"] = 99.
    assert cache.remember(source, current, prepare) is publication
    assert list(publication["plots"][0][1]) == [24.]
    assert list(publication["plots"][1][1]) == [24.]
    # A correction to the actual current record must prepare a fresh vector.
    if source == "native":
        current[0]["gpu"]["0"][0] = 70.
    else:
        current[0]["util"] = 70.
    corrected = cache.remember(source, current, prepare)
    assert corrected is not publication
    assert list(corrected["plots"][0][1]) == [70.]


@pytest.mark.parametrize("before,after", [
    ([True], [1]), ([1], [1.]), ([0.], [-0.]),
    ([{"0": [], "1": []}], [{"1": [], "0": []}]),
    ([struct.unpack(">d", bytes.fromhex("7ff8000000000001"))[0]],
     [struct.unpack(">d", bytes.fromhex("7ff8000000000002"))[0]]),
])
def test_signatures_preserve_typed_values_order_and_float_bits(before, after):
    assert N.signature(before) != N.signature(after)
    assert N.signature(before) == N.signature(before)


@pytest.mark.parametrize("base", [object, dict, list, float, int])
def test_extension_reducers_are_never_executed_on_foreground_frame(base):
    calls = []

    class Extension(base):
        def __reduce__(self):
            calls.append("reduce")
            raise AssertionError("A native cache signature executed an extension reducer")

        def __reduce_ex__(self, protocol):
            calls.append("reduce_ex")
            raise AssertionError("A native cache signature executed an extension reducer")

    source = [{"k": "live", "t": 1., "extension": Extension()}]
    assert N.signature(source) is None
    assert calls == []
    cache = N.NativeSeriesCache()
    results = [cache.remember("source", source, lambda owned: object()) for _ in range(2)]
    assert results[0] is not results[1]
    assert not cache.entries and not cache.bytes


def test_entry_and_byte_limits_evict_least_recently_used_preparations(monkeypatch):
    monkeypatch.setattr(N, "MAX_ENTRIES", 2)
    cache = N.NativeSeriesCache()
    first = cache.remember("first", [1], lambda owned: object())
    cache.remember("second", [2], lambda owned: object())
    assert cache.remember("first", [1], lambda owned: object()) is first
    cache.remember("third", [3], lambda owned: object())
    assert list(cache.entries) == ["first", "third"]
    assert cache.bytes == sum(len(entry[0]) for entry in cache.entries.values())
    monkeypatch.setattr(N, "MAX_TOTAL_BYTES", len(N.signature([1])))
    cache.remember("fourth", [4], lambda owned: object())
    assert list(cache.entries) == ["fourth"]
    monkeypatch.setattr(N, "MAX_TOTAL_BYTES", 0)
    cache.remember("fifth", [5], lambda owned: object())
    assert not cache.entries and cache.bytes == 0


def test_oversize_signature_bypasses_and_removes_stale_preparation(monkeypatch):
    cache = N.NativeSeriesCache()
    original = cache.remember("source", [1], lambda owned: object())
    monkeypatch.setattr(N, "MAX_SIGNATURE_BYTES", 20)
    assert N.signature(["x" * 100]) is None
    assert cache.remember("source", ["x" * 100], lambda owned: object()) is not original
    assert not cache.entries and not cache.bytes
    monkeypatch.setattr(N, "MAX_RECORDS", 1)
    assert N.signature([1, 2]) is None


@pytest.mark.parametrize("seed", range(30))
def test_indexed_observations_match_exact_edges_gaps_duplicate_order_and_fit(seed):
    source = random.Random(seed)
    pairs = [(source.randrange(-30, 31) / 2, source.choice([None, math.nan, source.uniform(-100, 100)]))
             for _ in range(80)]
    source.shuffle(pairs)
    times, values = zip(*pairs)
    bounds = (source.uniform(-8, -1), source.uniform(1, 8))
    latest = source.choice([None, -20., 0., 10., 30.])
    actual = N.ObservationIndex(times).window(values, bounds, latest=latest)
    expected = _metric_observations(values, times, bounds, latest=latest)
    assert actual == expected
    full = [(value, timestamp) for value, timestamp in zip(values, times)
            if latest is None or timestamp <= latest]
    assert charts.fit_time_bounds(actual[0], actual[1], bounds, (0., 100.), .5) == charts.fit_time_bounds(
        [value for value, _ in full], [timestamp for _, timestamp in full], bounds, (0., 100.), .5)
