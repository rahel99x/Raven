"""Prepared native evidence is shared without sharing presentation state."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tower.config import Config
from tower.controller import App
from tower.layout import Glyphs
from tower.model import Job, Store
from tower.native_series_cache import NativeSeriesCache
from tower.pane_context import PaneContext
from tower.series_publication import PublishedSeries
from tower.views import Views, normalized_series, prepared_series


def dashboard():
    store = Store(persist=False)
    store.jobs = [Job("7", "shared", "cpu", "RUNNING", submit="first")]
    for index in range(20):
        store.record("7", dict(k="live", t=float(index), cpu=.5, rss=1024.))
    cfg = Config({"log_lines": 0})
    app = App(store, None, None, cfg, "test", interactive=True)
    app.views_ref = Views(Glyphs(False), cfg)
    return store, app


def test_unchanged_owned_series_skips_signing_copying_and_rebuilding(monkeypatch):
    store, _ = dashboard()
    cache, build = NativeSeriesCache(), Mock(side_effect=lambda records: tuple(row["cpu"] for row in records))
    first = store.series_publication("7")
    assert isinstance(first, PublishedSeries)
    result = cache.remember("cpu", first, build)
    def forbidden(*args, **kwargs):
        pytest.fail("Unchanged owned sources must not sign or copy their retained records")
    monkeypatch.setattr("tower.native_series_cache.signature", forbidden)
    monkeypatch.setattr("tower.native_series_cache.owned_snapshot", forbidden)
    monkeypatch.setattr("tower.series_publication._freeze", forbidden)
    for _ in range(30):
        assert store.series_publication("7") is first
        assert cache.remember("cpu", store.series_publication("7"), build) is result
    assert build.call_count == 1


def test_each_revision_is_shared_by_root_and_scoped_pane():
    store, app = dashboard()
    pane = PaneContext(app, identity=("jobs", "details", "7"))
    pane._chart_owner = app
    build = Mock(side_effect=lambda records: tuple(row["cpu"] for row in records))
    first = prepared_series(app, "7", "test/v1", build)
    assert prepared_series(pane, "7", "test/v1", build) is first
    assert build.call_count == 1
    store.series["7"][3]["cpu"] = .8
    second = prepared_series(pane, "7", "test/v1", build)
    assert second is not first and second[3] == .8 and first[3] == .5
    assert prepared_series(app, "7", "test/v1", build) is second
    assert build.call_count == 2


def test_append_reuses_unchanged_record_publications():
    store, _ = dashboard()
    first = store.series_publication("7")
    store.record("7", dict(k="live", t=21., cpu=.8))
    second = store.series_publication("7")
    assert second is not first
    assert all(left is right for left, right in zip(first, second))
    assert len(second) == len(first) + 1


@pytest.mark.parametrize("interactive", [False, True])
def test_inspector_preparation_does_not_request_archive(monkeypatch, interactive):
    from tower.analysis_ui import _resource_series
    store, app = dashboard()
    app.interactive = interactive
    def forbidden(*args, **kwargs):
        pytest.fail("Inspector must only use published memory")
    monkeypatch.setattr(store._series_archive, "read", forbidden)
    first = _resource_series(app, "7")
    assert len(first["CPU per core (%)"]) == 20
    assert _resource_series(app, "7") is first
    store.series["7"][0]["cpu"] = .75
    assert _resource_series(app, "7")["CPU per core (%)"][0]["value"] == 75.


def test_foreign_mutation_still_changes_normalized_summary():
    store, app = dashboard()
    foreign = dict(k="live", t=22., cpu=.7, rss=2.)
    store.series["7"].append(foreign)
    first = normalized_series(app, "7")
    foreign["cpu"] = .9
    second = normalized_series(app, "7")
    assert first[-1]["cpu"] == .7 and second[-1]["cpu"] == .9


def test_same_job_number_from_new_store_cannot_reuse_analysis():
    store, app = dashboard()
    build = Mock(side_effect=lambda records: object())
    first = prepared_series(app, "7", "test/v1", build)
    replacement = Store(persist=False)
    replacement.jobs = list(store.jobs)
    for row in store.series_of("7"):
        replacement.record("7", row)
    app.store = replacement
    assert prepared_series(app, "7", "test/v1", build) is not first
    assert build.call_count == 2


def test_adapted_store_without_explicit_reader_retains_resource_columns():
    from tower.analysis_ui import _resource_series
    app = SimpleNamespace(store=SimpleNamespace(series={"7": [dict(k="live", t=1., cpu=.5, rss=1024**3)]}))
    result = _resource_series(app, "7")
    assert result["CPU per core (%)"] == [{"t": 1., "value": 50.}]
    assert result["Memory (GB)"] == [{"t": 1., "value": 1.}]
