"""Foreign corrections remain bounded and exact after native ownership ends."""
from types import SimpleNamespace

import pytest

from tower import series_publication as P
from tower.analysis_ui import _resource_series
from tower.model import Store
from tower.native_series_cache import NativeSeriesCache
from tower.views import _prepare_native_metrics


@pytest.mark.parametrize("shape", ["deep", "wide"])
def test_oversized_foreign_correction_preserves_reader_without_hooks_or_partial_cache(shape, monkeypatch):
    calls = []
    class Extension:
        def __deepcopy__(self, memo):
            calls.append("copy")
            raise AssertionError("Extension copy hook ran")
        def __reduce_ex__(self, protocol):
            calls.append("reduce")
            raise AssertionError("Extension reducer ran")
    extension = Extension()
    if shape == "deep":
        metadata = extension
        for _ in range(1500):
            metadata = [metadata]
    else:
        metadata = [0] * 30000 + [extension]
    series = P.SeriesDeque()
    record = series.append_owned({"t": 1.0, "k": "live", "cpu": .5})
    series.publication()
    record["metadata"] = metadata
    original = P._plain
    visited = []
    def bounded(value, *args, **kwargs):
        visited.append(type(value))
        return original(value, *args, **kwargs)
    monkeypatch.setattr(P, "_plain", bounded)
    publication = series.publication()
    assert len(visited) <= 20002
    assert type(publication) is list and publication[0] is record
    cache = NativeSeriesCache()
    def prepare(records):
        return _prepare_native_metrics(records, 0, False, {"live": .5, "gpu": .5}, owned=True)
    first = cache.remember("job", publication, prepare)
    assert list(first["plots"][0][1]) == [50.0]
    record["cpu"] = .9
    second = cache.remember("job", series.publication(), prepare)
    assert second is not first
    assert list(second["plots"][0][1]) == [90.0]
    assert not cache.entries and not calls


def test_foreign_container_changed_during_snapshot_does_not_break_iteration(monkeypatch):
    series = P.SeriesDeque()
    record = series.append_owned({"t": 1.0, "k": "live", "cpu": .5})
    opaque = object()
    foreign = {"opaque": opaque, "value": 1}
    record["metadata"] = foreign
    original = P._plain
    staged = False
    def changed(value, *args, **kwargs):
        nonlocal staged
        if value is opaque and not staged:
            staged = True
            foreign["new"] = 2
            del foreign["value"]
            foreign["last"] = 3
        return original(value, *args, **kwargs)
    monkeypatch.setattr(P, "_plain", changed)
    captured = series.publication()
    assert captured[0]["metadata"] == {"opaque": opaque, "value": 1}
    assert series.publication()[0]["metadata"] == {"opaque": opaque, "new": 2, "last": 3}


def test_memory_only_inspector_never_creates_or_loads_unknown_series(tmp_path, monkeypatch):
    store = Store(state_dir=str(tmp_path))
    app = SimpleNamespace(store=store)
    def forbidden(*args, **kwargs):
        pytest.fail("Memory-only inspection touched the archive")
    monkeypatch.setattr(store._series_archive, "read", forbidden)
    assert _resource_series(app, "missing") == {}
    assert "missing" not in store.series and "missing" not in store._series_loaded
    assert not store._series_archive.pending


def test_memory_only_inspector_rechecks_external_nested_gpu_corrections(monkeypatch):
    store = Store(persist=False)
    foreign = {"k": "gpu", "t": 1.0, "gpu": {"0": [25.0]}}
    store.series["7"].append(foreign)
    app = SimpleNamespace(store=store)
    def forbidden(*args, **kwargs):
        pytest.fail("Memory-only inspection touched the archive")
    monkeypatch.setattr(store._series_archive, "read", forbidden)
    first = _resource_series(app, "7")
    foreign["gpu"]["0"][0] = 90.0
    second = _resource_series(app, "7")
    assert first["GPU utilization (%)"] == [{"t": 1.0, "value": 25.0}]
    assert second["GPU utilization (%)"] == [{"t": 1.0, "value": 90.0}]
    assert second is not first
