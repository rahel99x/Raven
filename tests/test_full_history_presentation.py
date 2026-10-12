"""Full-history reuse must preserve visible boundaries and fresh source truth."""
from types import SimpleNamespace

import pytest

from raven import analysis_ui as A, chart_interaction as C, charts, clock, layout as L
from raven import metric_live as M
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.native_series_cache import ObservationIndex
from raven.views import Views


@pytest.fixture(params=["native", "reported", "reported-modal"])
def presentation(request, monkeypatch):
    timer = [102.25]
    monkeypatch.setattr(clock, "now", lambda: timer[0])
    cfg = Config({"animations": False, "startup_animation": False})
    store = Store(persist=False)
    job = Job("17", "same-history", "gpu", "RUNNING", submit="s", start="r")
    store.apply_jobs([job])
    app = App(store, None, None, cfg, "test", interactive=False)
    app.tab, app.analytics_job, app.selected_id = "analytics", "17", "17"
    app.width, app.height = 180, 30
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    calls, painter = [], charts.braille_chart

    def capture(*args, **kwargs):
        rows = painter(*args, **kwargs)
        calls.append({"bounds": kwargs.get("times"), "values": tuple(args[1]),
                      "samples": tuple(kwargs.get("sample_times", ())),
                      "metadata": dict(kwargs.get("metadata", {}))})
        return rows

    monkeypatch.setattr(charts, "braille_chart", capture)
    item = SimpleNamespace(app=app, views=views, store=store, job=job, timer=timer,
                           surface=request.param, timestamps=[-10000.] + list(map(float, range(0, 101, 5))),
                           values=[50.] * 22, calls=calls)
    try:
        yield item
    finally:
        if app.research:
            app.research.close()


def draw(item):
    app = item.app
    C.begin_frame(app, app.width, app.height)
    if item.surface == "native":
        identity = C.key(app, "cpu-rate", "%", "17", scope="resource-series", attempt="s|r")
        rows = item.views.metric_curve(
            app, item.values, app.width, 8, identity, running=True, title="Measured CPU", hi=100.,
            times=(item.timestamps[0], item.timestamps[-1]), sample_times=item.timestamps,
            sample_interval=5., observation_index=ObservationIndex(item.timestamps))
    else:
        interactive = item.surface == "reported-modal"
        A.initialize(app)["chart_events"] = False
        identity = A.chart_key(app, "metric", "/run/metrics.jsonl", interactive=interactive,
                               jid="17", job=item.job)
        A.initialize(app)["axes"]["metric"] = {"mode": "fixed", "low": 0., "high": 100.}
        controls, _ = M.controls(item.views.g, app, identity, app.width, running=True)
        points = [{"t": t, "value": v} for t, v in zip(item.timestamps, item.values)]
        metadata = {}
        rows = controls + A.chart_rows(item.views.g, app, points, app.width, 8, "metric",
                                        "/run/metrics.jsonl", running=True, interactive=interactive,
                                        zoom_key=identity, metadata=metadata)
        C.record(app, identity, metadata, row=len(controls))
    C.publish(app, app.width, app.height)
    plot = next(plot for plot in C.initialize(app)["plots"] if plot.key == identity)
    return rows, plot, identity


def test_unchanged_flat_history_reuses_raster_and_keeps_published_axes_and_lag_honest(presentation):
    item = presentation
    _, before, identity = draw(item)
    item.calls.clear()
    for _ in range(5):
        item.timer[0] += .2
        rows, current, _ = draw(item)
        assert current.x_bounds == before.x_bounds
        assert not item.calls
        # The canonical source clock continues, but the label and input map
        # must describe the older endpoint that is actually on screen.
        status = M.playback_status(item.app, identity)
        assert status.end >= current.x_bounds[1]
        from raven.metric_sampling import format_interval
        expected = "buffered " + format_interval(item.timer[0] - current.x_bounds[1]) + " behind"
        assert M.playback_label(item.app, identity, displayed_end=current.x_bounds[1]) == expected
        assert expected in L.to_text(rows, item.app.width)
    # Data collection continues to be reported from actual acquired timestamp100.
    assert "3s" in L.to_text(rows, item.app.width)


def test_same_bracket_steep_edge_updates_before_one_horizontal_subcolumn(presentation):
    item = presentation
    item.values[item.timestamps.index(80.)] = 100.
    item.values[item.timestamps.index(90.)] = 0.
    item.values[item.timestamps.index(95.)] = 100.
    _, before, _ = draw(item)
    item.calls.clear()
    item.timer[0] += .2
    _, after, _ = draw(item)
    assert 90. < before.x_bounds[1] < after.x_bounds[1] < 95.
    assert after.x_bounds[1] - before.x_bounds[1] < (before.x_bounds[1] - before.x_bounds[0]) / 200
    assert item.calls


@pytest.mark.parametrize("kind", ["spike", "gap"])
def test_newly_crossed_short_feature_cannot_hide_behind_same_endpoint_pixel(presentation, kind):
    item = presentation
    extras = [(93., 100. if kind == "spike" else None), (93.05, 0.), (93.1, 50.)]
    samples = sorted(list(zip(item.timestamps, item.values)) + extras)
    item.timestamps, item.values = map(list, zip(*samples))
    item.timer[0] = 102.8
    _, before, _ = draw(item)
    item.calls.clear()
    item.timer[0] += .4
    _, after, _ = draw(item)
    assert before.x_bounds[1] < 93. < 93.1 < after.x_bounds[1]
    assert item.calls
    assert any(93. in call["samples"] and 93.05 in call["samples"] for call in item.calls)


@pytest.mark.parametrize("replacement", [80., None])
def test_interior_correction_refreshes_curve_without_moving_the_clock(presentation, replacement):
    item = presentation
    _, before, _ = draw(item)
    item.calls.clear()
    item.values[item.timestamps.index(50.)] = replacement
    _, after, _ = draw(item)
    assert after.x_bounds == before.x_bounds
    assert item.calls
    assert any(dict(zip(call["samples"], call["values"]))[50.] == replacement for call in item.calls)


def test_live_window_remains_continuous_and_does_not_inherit_full_history_hold(presentation):
    item = presentation
    _, full, identity = draw(item)
    assert M.set_enabled(item.app, identity, True)
    assert M.set_delta(item.app, identity, 1.)
    _, before, _ = draw(item)
    assert before.x_bounds[1] - before.x_bounds[0] == pytest.approx(1.)
    for _ in range(4):
        item.timer[0] += .1
        _, after, _ = draw(item)
        assert after.x_bounds[1] > before.x_bounds[1]
        assert after.x_bounds[1] - after.x_bounds[0] == pytest.approx(1.)
        assert after.x_bounds[1] >= full.x_bounds[1]
        before = after
