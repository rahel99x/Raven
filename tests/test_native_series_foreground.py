"""Native cards prepare visible sources and tolerate malformed saved records."""
from copy import deepcopy
import math
from types import SimpleNamespace

import pytest

from raven import chart_interaction as C, clock, layout as L, metric_live as M
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views, _native_series, _prepare_native_metrics


@pytest.fixture
def dashboard(monkeypatch):
    cfg = Config({"animations": False, "smooth_scrolling": False,
                  "startup_animation": False, "log_lines": 0})
    store = Store(persist=False)
    store.jobs = [Job("7", "native-series", "gpu", "RUNNING", cpus=8, gpus=4, mem_req="8G")]
    for index in range(40):
        store.record("7", {"k": "live", "t": float(index), "cpu": .5, "rss": float(1 << 30)})
        store.record("7", {"k": "gpu", "t": float(index),
                           "gpu": {str(device): [float(index + device)] for device in range(4)}})
    store.trace["7"] = [{"index": device, "t": float(index * 60), "util": float(index + device)}
                        for device in range(4) for index in reversed(range(40))]
    app = App(store, None, None, cfg, "test", interactive=True)
    app.tab, app.analytics_view, app.analytics_job, app.selected_id = "analytics", "job", "7", "7"
    app.analytics_document_mode = True
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    monkeypatch.setattr(clock, "now", lambda: 2400.)
    yield SimpleNamespace(app=app, store=store, views=views)
    if app.research:
        app.research.close()


def test_offscreen_trace_counters_are_not_read_or_materialized(dashboard):
    reads = []

    class Reading(dict):
        def get(self, key, *args):
            if key == "util":
                reads.append(self["index"])
            return super().get(key, *args)

    app, store, views = dashboard.app, dashboard.store, dashboard.views
    store.trace["7"] = [Reading(point) for point in store.trace["7"]]
    app.analytics_series_window = (0, 20)
    C.begin_frame(app, 100, 40)
    rows = views.analytics_job(store.snapshot(), app, 100, 2048)
    assert not reads, "Hidden trace cards extracted their counters before viewport skipping"
    assert len(rows) > 300, "Virtual bands must retain the complete logical document"
    assert {identity[2] for identity in M.initialize(app)["entries"]} == {"cpu-rate"}


def test_scrolled_busy_mean_keeps_entire_source_history_and_detects_corrections(dashboard, monkeypatch):
    app, store, views = dashboard.app, dashboard.store, dashboard.views
    captures, original = [], views.metric_curve

    def curve(target, values, width, height, identity, **options):
        if identity[2] == "gpu-trace:3:busy-mean" and not options.get("filled"):
            captures.append((list(values), list(options["sample_times"])))
        return original(target, values, width, height, identity, **options)

    monkeypatch.setattr(views, "metric_curve", curve)
    app.analytics_series_window = (340, 500)
    views.analytics_job(store.snapshot(), app, 100, 2048)
    assert captures[-1] == ([3. + index / 2 for index in range(40)], [float(index * 60) for index in range(40)])
    # Correct the earliest point while its latest mean card is the only
    # visible family. The provider is fresh and recomputes its full divisor.
    point = next(point for point in store.trace["7"] if point["index"] == 3 and point["t"] == 0.)
    point["util"] = 83.
    views.analytics_job(store.snapshot(), app, 100, 2048)
    values, _ = captures[-1]
    assert values[0] == 83.
    assert values[-1] == pytest.approx(24.5)


@pytest.mark.parametrize("status,expected", [
    ("loading", "Restoring recorded samples"), ("busy", "queued"),
    ("limited", "older observations remain on disk"),
    ("error", "could not be restored"), ("unavailable", "unavailable"),
])
@pytest.mark.parametrize("has_samples", [False, True])
def test_archive_status_is_visible_and_empty_restore_does_not_claim_missing_gpu(
        dashboard, monkeypatch, status, expected, has_samples):
    app, store, views = dashboard.app, dashboard.store, dashboard.views
    store.trace.clear()
    observations = list(store.series["7"]) if has_samples else []
    monkeypatch.setattr(store, "series_view", lambda jid: observations)
    monkeypatch.setattr(store, "series_of", lambda jid: pytest.fail("Interactive native view used synchronous restore"))
    monkeypatch.setattr(store, "series_status", lambda jid: {"status": status, "message": "\x1b[31mread failed\x1b[0m"})
    app.analytics_series_window = (0, 40)
    text = L.to_text(views.analytics_job(store.snapshot(), app, 100, 2048), 100)
    assert expected in text
    assert "\x1b" not in text
    if not has_samples:
        assert "no samples recorded" not in text
        assert "GPU telemetry is unavailable" not in text


@pytest.mark.parametrize("bad", [None, [], {}, "invalid", True, math.nan, math.inf])
def test_malformed_native_saved_records_are_safe_gaps_and_do_not_modify_archive(dashboard, bad):
    app, store, views = dashboard.app, dashboard.store, dashboard.views
    records = [{"k": "live", "t": []}, {"k": {} , "t": 2.}, {"k": "live"},
               {"k": "live", "t": 2., "cpu": bad, "rss": bad},
               {"k": "live", "t": 1., "cpu": .5, "rss": float(1 << 30)}]
    original = deepcopy(records)
    store.series["7"].clear()
    store.series["7"].extend(records)
    store.trace.clear()
    app.analytics_series_window = (0, 100)
    C.begin_frame(app, 100, 40)
    rows = views.analytics_job(store.snapshot(), app, 100, 2048)
    assert "2 cpu samples" in L.to_text(rows, 100)
    assert [record.get("t") for record in _native_series(records)] == [1., 2.]
    assert len(store.series["7"]) == len(original)
    assert store.series["7"][3] is records[3]
    assert records[3]["cpu"] is bad


def test_native_summary_sanitizer_copies_only_malformed_counters():
    clean = {"k": "live", "t": 2., "cpu": .5, "eff": .4, "rss": 1., "cpu_time": 10.}
    malformed = {"k": "live", "t": 1., "cpu": "50", "eff": [], "rss": {}, "cpu_time": True}
    unknown = {"k": "gpu", "t": 3., "gpu": {"0": [None]}}
    output = _native_series([clean, malformed, unknown, {"k": "live", "t": math.inf}])
    assert output == [dict(malformed, cpu=None, eff=None, rss=None, cpu_time=None), clean, unknown]
    assert output[1] is clean and output[2] is unknown
    assert malformed["rss"] == {}


@pytest.mark.parametrize('bad', [None, [], {}, 'invalid', True, math.nan, math.inf, -1.])
def test_prepared_cpu_preserves_chronology_duplicate_order_and_valid_efficiency_fallback(bad):
    observations = [{'k': 'live', 't': 3., 'cpu': 1.5, 'rss': 0},
                    {'k': 'live', 't': 1., 'cpu': bad, 'eff': .5, 'rss': 0},
                    {'k': 'live', 't': 2., 'cpu': .4, 'rss': 0},
                    {'k': 'live', 't': 2., 'cpu': .6, 'rss': 0}]
    prepared = _prepare_native_metrics(observations, 0, False, {'live': .5, 'gpu': .5})
    plot = prepared['plots'][0]
    assert list(plot[1]) == [50., 40., 60., 150.]
    assert list(plot[5]) == [1., 2., 2., 3.]
    assert observations[1]['cpu'] is bad


def test_archived_job_inventory_preserves_order_and_avoids_duplicate_ids(dashboard, monkeypatch):
    app, store, views = dashboard.app, dashboard.store, dashboard.views
    ids = [str(index) for index in range(3000)]
    monkeypatch.setattr(store, "series_jobs_view", lambda: ids + list(reversed(ids)))
    monkeypatch.setattr(store, "series_jobs", lambda: pytest.fail("Interactive inventory read the filesystem"))
    assert views.analytics_jobs(store.snapshot(), app) == ["7"] + [jid for jid in ids if jid != "7"]
