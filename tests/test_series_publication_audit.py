"""Adversarial compatibility and race checks for revisioned metric sources."""
from collections import deque
import copy
import json
import pickle
import threading
import weakref

import pytest

from tower import native_series_cache as N
from tower import series_archive as A
from tower import series_publication as P
from tower.model import Job, Store
from tower.report import _ReportStore
from tower.series_cache_limits import MAX_SERIES_CACHE_ENTRIES
from tower.views import _prepare_native_metrics


def sample(value=24.0, timestamp=1.0):
    return {"t": timestamp, "k": "gpu", "gpu": {"0": [value, 128, 1024]}}


def prepare(values):
    return _prepare_native_metrics(values, 0, False, {"live": .5, "gpu": .5}, owned=True)


def test_owned_capture_detaches_producer_and_historical_corrections_refresh_publication():
    source = sample()
    series = P.SeriesDeque()
    record = series.append_owned(source)
    first = series.publication()
    cache = N.NativeSeriesCache()
    prepared = cache.remember("job", first, prepare)
    source["gpu"]["0"][0] = 99.0
    assert series.publication() is first
    assert list(prepared["plots"][0][1]) == [24.0]
    record["gpu"]["0"][0] = 70.0
    second = series.publication()
    assert second is not first
    assert second[0]["gpu"]["0"][0] == 70.0
    assert first[0]["gpu"]["0"][0] == 24.0
    assert list(cache.remember("job", second, prepare)["plots"][0][1]) == [70.0]


def test_overwritten_nested_container_cannot_reappear_in_later_publications():
    series = P.SeriesDeque()
    record = series.append_owned(sample())
    old = record["gpu"]
    first = series.publication()
    record["gpu"] = {"1": [50.0]}
    old["0"][0] = 99.0
    current = series.publication()
    assert current[0]["gpu"] == {"1": (50.0,)}
    assert first[0]["gpu"]["0"][0] == 24.0


def test_evicted_owned_record_does_not_wait_for_cyclic_garbage_collection():
    series = P.SeriesDeque(maxlen=1)
    record = series.append_owned(sample())
    reference = weakref.ref(record)
    publication = series.publication()
    del record
    series.append_owned(sample(50, 2))
    assert reference() is None, "Evicted metric ownership created a reference cycle"
    assert publication[0]["gpu"]["0"][0] == 24.0


def test_source_publication_eviction_keeps_existing_readers_immutable():
    store = Store(persist=False)
    first = None
    for index in range(MAX_SERIES_CACHE_ENTRIES + 1):
        store.record(str(index), sample(index))
        current = store.series_publication(str(index))
        if first is None:
            first = current
    assert len(store._publications) == MAX_SERIES_CACHE_ENTRIES
    assert store.series["0"]._source is None
    store.series["0"][0]["gpu"]["0"][0] = 50.0
    refreshed = store.series_publication("0")
    assert first[0]["gpu"]["0"][0] == 0.0
    assert refreshed[0]["gpu"]["0"][0] == 50.0
    assert refreshed is not first


def test_large_nested_metadata_cannot_hide_behind_record_count_cache_budget(monkeypatch):
    series = P.SeriesDeque()
    source = sample()
    source["metadata"] = {"payload": "x" * (128 << 10)}
    series.append_owned(source)
    publication = series.publication()
    assert publication.byte_size >= 128 << 10
    monkeypatch.setattr(N, "MAX_TOTAL_BYTES", 64 << 10)
    cache = N.NativeSeriesCache()
    assert list(cache.remember("job", publication, prepare)["plots"][0][1]) == [24.0]
    assert not cache.entries and not cache.bytes


@pytest.mark.parametrize("base", [object, dict, list, tuple, int, float, str])
def test_extension_added_to_owned_record_never_executes_copy_or_reducer_hooks(base):
    calls = []
    class Extension(base):
        def __deepcopy__(self, memo):
            calls.append("deepcopy")
            raise AssertionError("Extension deepcopy ran on the publication path")
        def __reduce_ex__(self, protocol):
            calls.append("reduce")
            raise AssertionError("Extension reducer ran on the publication path")
    series = P.SeriesDeque()
    record = series.append_owned(sample())
    series.publication()
    extension = Extension()
    record["extension"] = extension
    captured = series.publication()
    assert type(captured) is list
    assert captured[0]["extension"] is extension
    cache = N.NativeSeriesCache()
    assert list(cache.remember("job", captured, prepare)["plots"][0][1]) == [24.0]
    assert not cache.entries and not calls


def test_nested_dictionary_appended_as_record_cannot_reuse_root_cached_value():
    series = P.SeriesDeque()
    record = series.append_owned(sample())
    series.publication()
    series.append(record["gpu"])
    captured = series.publication()
    assert captured[1] == {"0": [24.0, 128, 1024]}
    assert "k" not in captured[1]
    assert captured[0]["k"] == "gpu"


@pytest.mark.parametrize("operation", ["extend", "iadd", "extendleft"])
def test_series_self_extension_matches_builtin_deque(operation):
    series = P.SeriesDeque(maxlen=3)
    series.append_owned(sample(10))
    series.append_owned(sample(20, 2))
    expected = deque([sample(10), sample(20, 2)], maxlen=3)
    if operation == "iadd":
        series += series
        expected += expected
    else:
        getattr(series, operation)(series)
        getattr(expected, operation)(expected)
    assert list(series) == list(expected)
    assert len(series.publication()) == len(expected)


@pytest.mark.parametrize("operation", ["extend", "iadd"])
def test_nested_list_self_extension_terminates_with_builtin_result(operation):
    # Fail safely even if a regression starts appending forever: a guarded
    # append catches unexpected growth before the test can consume memory.
    series = P.SeriesDeque()
    values = series.append_owned(sample())["gpu"]["0"]
    original = values.append
    def append(value):
        if len(values) > 12:
            pytest.fail("Self extension is iterating its own growing output")
        return original(value)
    values.append = append
    if operation == "iadd":
        values += values
    else:
        values.extend(values)
    assert list(values) == [24.0, 128, 1024] * 2


@pytest.mark.parametrize("operation", ["pop", "popleft", "remove", "reverse", "rotate", "delete", "multiply"])
def test_all_deque_mutations_serialize_against_publication(operation, monkeypatch):
    # The writer announces lock admission before blocking. No sleep or wall
    # clock speed assumption is needed to prove it cannot mutate mid-freeze.
    attempted = threading.Event()
    released = threading.Event()
    writer_ready = threading.Event()
    completed = threading.Event()
    class ObservedLock:
        def __init__(self):
            self.inner = threading.RLock()
        def __enter__(self):
            if threading.current_thread().name == "publication-audit-writer":
                attempted.set()
            self.inner.acquire()
            return self
        def __exit__(self, *args):
            self.inner.release()
    series = P.SeriesDeque(lock=ObservedLock())
    series.append_owned(sample())
    series.append_owned(sample(50, 2))
    victim = series[0]
    real_freeze = P._freeze
    staged = False
    errors = []
    def freeze(value, *args, **kwargs):
        nonlocal staged
        if not staged:
            staged = True
            released.set()
            assert writer_ready.wait(2)
            assert attempted.wait(2), "Deque mutation bypassed publication lock"
            assert not completed.is_set()
        return real_freeze(value, *args, **kwargs)
    monkeypatch.setattr(P, "_freeze", freeze)
    def mutate():
        try:
            assert released.wait(2)
            writer_ready.set()
            if operation == "remove":
                series.remove(victim)
            elif operation == "delete":
                del series[0]
            elif operation == "multiply":
                series.__imul__(2)
            else:
                getattr(series, operation)()
            completed.set()
        except BaseException as error:
            errors.append(error)
    writer = threading.Thread(target=mutate, name="publication-audit-writer")
    writer.start()
    try:
        publication = series.publication()
        assert len(publication) == 2
    finally:
        released.set()
        writer.join(timeout=3)
    assert not writer.is_alive() and not errors
    assert completed.is_set()
    assert series.publication() is not publication


def test_report_copy_preserves_capture_without_copying_the_store_lock():
    store = Store(persist=False)
    store.record("7", sample())
    report = _ReportStore(store, store.snapshot(), 10.0)
    store.series["7"][0]["gpu"]["0"][0] = 99.0
    assert report.series_of("7")[0]["gpu"]["0"][0] == 24.0
    assert report._reader._series_archive.store is report._reader


@pytest.mark.parametrize("operation", ["copy", "pickle"])
def test_plain_export_of_owned_series_contains_no_locks_or_revision_objects(operation):
    series = P.SeriesDeque()
    series.append_owned(sample())
    captured = copy.deepcopy(series) if operation == "copy" else pickle.loads(pickle.dumps(series))
    assert type(captured) is deque
    assert type(captured[0]) is dict and type(captured[0]["gpu"]["0"]) is list
    captured[0]["gpu"]["0"][0] = 88.0
    assert series[0]["gpu"]["0"][0] == 24.0


def test_frozen_record_pickle_roundtrip_stays_immutable():
    series = P.SeriesDeque()
    series.append_owned(sample())
    original = series.publication()[0]
    restored = pickle.loads(pickle.dumps(original))
    assert restored == original
    with pytest.raises(TypeError):
        restored["t"] = 2
    with pytest.raises(TypeError):
        restored["gpu"]["0"] = (50,)


@pytest.mark.parametrize("operation", ["copy", "pickle"])
def test_external_self_referential_deque_export_preserves_cycle(operation):
    series = P.SeriesDeque()
    series.append(series)
    captured = copy.deepcopy(series) if operation == "copy" else pickle.loads(pickle.dumps(series))
    assert captured is not series and captured[0] is captured


def test_external_nested_tuple_list_cycle_keeps_topology_without_copy_hooks():
    series = P.SeriesDeque()
    record = series.append_owned(sample())
    shared = []
    pair = (shared,)
    shared.append(pair)
    record["cycle"] = pair
    captured = series.publication()
    assert type(captured) is list
    cycle = captured[0]["cycle"]
    assert cycle is not pair
    assert cycle[0][0] is cycle
    assert N.signature(captured) is not None


def test_archive_restore_preserves_foreign_record_identity_and_later_corrections(tmp_path):
    directory = tmp_path/"series"
    directory.mkdir()
    (directory/"7.jsonl").write_text(json.dumps(sample(10, 0)) + "\n")
    store = Store(state_dir=str(tmp_path))
    foreign = sample()
    store.series["7"].append(foreign)
    store.series_of("7")
    assert store.series["7"][-1] is foreign
    assert type(store.series_publication("7")) is list
    foreign["gpu"]["0"][0] = 90.0
    assert store.series_publication("7")[-1]["gpu"]["0"][0] == 90.0


def test_archive_cannot_adopt_a_record_owned_by_another_store(tmp_path):
    original = Store(persist=False)
    original.record("7", sample())
    foreign = original.series["7"][0]
    directory = tmp_path/"series"
    directory.mkdir()
    (directory/"7.jsonl").write_text(json.dumps(sample(10, 0)) + "\n")
    store = Store(state_dir=str(tmp_path))
    store.series["7"].append(foreign)
    store.series_of("7")
    assert store.series["7"][-1] is foreign
    assert type(store.series_publication("7")) is list
    foreign["gpu"]["0"][0] = 90.0
    assert store.series_publication("7")[-1]["gpu"]["0"][0] == 90.0


@pytest.mark.parametrize("mutation", ["extend", "multiply", "nest"])
def test_owned_record_growth_cannot_bypass_publication_bounds(mutation):
    series = P.SeriesDeque()
    record = series.append_owned(sample())
    series.publication()
    values = record["gpu"]["0"]
    if mutation == "extend":
        values.extend(range(20001))
    elif mutation == "multiply":
        # Keep a local reference: a parent __setitem__ must not be necessary
        # for checking the expanded record's publication budget.
        values *= 7000
    else:
        child = record
        for _ in range(40):
            child["nested"] = {}
            child = child["nested"]
    captured = series.publication()
    assert type(captured) is list, "A record beyond publication bounds stayed trusted"
    assert captured[0]["k"] == "gpu"


def test_failed_archive_worker_cannot_mark_requeued_attempt_as_loaded(tmp_path, monkeypatch):
    store = Store(state_dir=str(tmp_path))
    store.apply_jobs([Job("7", "old", "cpu", "RUNNING", submit="old")])
    pending = []
    store.configure_series_loader(pending.append)
    store.series_view("7")
    def stale_failure(*args, **kwargs):
        store.apply_jobs([])
        store.apply_jobs([Job("7", "new", "cpu", "RUNNING", submit="new")])
        store.record("7", sample(), _persist=False)
        raise ValueError("Stale restore failed after attempt changed")
    monkeypatch.setattr(A, "read_tail", stale_failure)
    pending.pop()()
    assert "7" not in store._series_loaded
    store.series_view("7")
    assert len(pending) == 1, "New attempt must still be eligible for history restore"
