"""Visible Advisor jobs share retained publications without LRU thrashing."""
from types import SimpleNamespace

from raven import model as M
from raven import native_series_cache as N
from raven import series_publication as P
from raven.model import Store
from raven.views import normalized_series


def advisor_sources():
    store = Store(persist=False)
    for jid in map(str, range(9)):
        for index in range(20):
            store.record(jid, {"k": "live", "t": float(index), "cpu": .5, "rss": 1024.0})
            store.record(jid, {"k": "gpu", "t": float(index), "gpu": {"0": [25.0]}})
    return store, SimpleNamespace(store=store, interactive=True)


def test_nine_unchanged_advisor_jobs_reuse_sources_and_preparations(monkeypatch):
    store, app = advisor_sources()
    first = {jid: normalized_series(app, jid) for jid in store.series}
    def forbidden(*args, **kwargs):
        raise AssertionError("Unchanged visible Advisor histories were frozen or signed again")
    monkeypatch.setattr(P, "_freeze", forbidden)
    monkeypatch.setattr(N, "signature", forbidden)
    for _ in range(3):
        for jid in store.series:
            assert normalized_series(app, jid) is first[jid]
    assert len(store._publications) == 9
    assert len(app._series_preparations.entries) == 9
    assert app._series_preparations.bytes <= N.MAX_TOTAL_BYTES


def test_nested_correction_rebuilds_only_changed_record_in_advisor_working_set(monkeypatch):
    store, app = advisor_sources()
    first = {jid: normalized_series(app, jid) for jid in store.series}
    changed = store.series["0"][1]
    changed["gpu"]["0"][0] = 80.0
    original = P._freeze
    frozen = []
    def freeze(value, *args, **kwargs):
        if isinstance(value, dict) and "k" in value:
            frozen.append(id(value))
        return original(value, *args, **kwargs)
    monkeypatch.setattr(P, "_freeze", freeze)
    current = {jid: normalized_series(app, jid) for jid in store.series}
    assert frozen == [id(changed)]
    assert current["0"] is not first["0"]
    assert current["0"][1]["gpu"]["0"][0] == 80.0
    assert first["0"][1]["gpu"]["0"][0] == 25.0
    assert all(current[jid] is first[jid] for jid in store.series if jid != "0")


def test_source_byte_limit_still_evicts_below_larger_entry_allowance(monkeypatch):
    store = Store(persist=False)
    for jid in ("first", "second"):
        store.record(jid, {"k": "live", "t": 1.0, "cpu": .5, "metadata": "x" * 8192})
    first = store.series_publication("first")
    monkeypatch.setattr(M, "MAX_SERIES_CACHE_BYTES", first.byte_size + 1)
    second = store.series_publication("second")
    assert list(store._publications) == ["second"]
    assert store.series["first"]._source is None
    assert first[0]["cpu"] == .5 and second[0]["cpu"] == .5
    assert sum(entry[1] for entry in store._publications.values()) <= M.MAX_SERIES_CACHE_BYTES


def test_prepared_byte_limit_still_evicts_below_larger_entry_allowance(monkeypatch):
    cache = N.NativeSeriesCache()
    sources = []
    for index in range(2):
        series = P.SeriesDeque()
        series.append_owned({"k": "live", "t": 1.0, "cpu": .5, "metadata": "x" * 8192})
        sources.append(series.publication())
    monkeypatch.setattr(N, "MAX_TOTAL_BYTES", sources[0].byte_size + 1)
    cache.remember("first", sources[0], lambda records: tuple(records))
    cache.remember("second", sources[1], lambda records: tuple(records))
    assert list(cache.entries) == ["second"]
    assert cache.bytes <= N.MAX_TOTAL_BYTES
