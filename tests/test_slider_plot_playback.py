"""Real mouse slider drags refresh native, inline, and modal plots in place."""
import math
import time
from types import SimpleNamespace

import pytest

from raven import analysis_ui as A, chart_interaction as C, charts, clock, layout as L
from raven import metric_live as M, metric_sampling as S, screen
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.text_selection import _slice
from raven.views import Views


@pytest.fixture(params=["analytics", "jobs", "modal"])
def scene(request, monkeypatch):
    timer = [202.25]
    monkeypatch.setattr(clock, "now", lambda: timer[0])
    cfg = Config({"animations": False, "startup_animation": False, "log_lines": 0,
                  "workspace": {"density": "compact"}})
    store = Store(persist=False)
    job = Job("17", "training", "gpu", "RUNNING", cpus=4, mem_req="8G",
              submit="2026-10-09T00:00:00", start="2026-10-09T00:00:01")
    store.apply_jobs([job])
    points = []
    for index in range(2001):
        timestamp = 100. + index / 20
        value = .5 + .2 * math.sin(index * .9) + .1 * math.sin(index / 30)
        store.record("17", {"k": "live", "t": timestamp, "cpu": value,
                            "rss": (2 + value) * 1024 ** 3})
        points.append({"t": timestamp, "value": value, "step": index})
    app = App(store, None, None, cfg, "test", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    app.selected_id = app.analytics_job = "17"
    app.tab = request.param if request.param != "modal" else "analytics"
    app.job_panel_state.update(mode="analytics", analytics_view="job")
    if request.param == "modal":
        app.analysis_result = {"series": {"loss": points}, "path": "/project/metrics.jsonl"}
        app.analysis_result_job = "17"
        app.analysis_result_generation = getattr(app.research, "generation", None)
        state = A.initialize(app)
        state.update(modal="chart", metric="loss", chart_job="17", chart_events=False,
                     zoom=1., pan=0., cursor=0)
        app.mode = "analysis"
    draws, painter = [], charts.braille_chart

    def capture(*args, **kwargs):
        result = painter(*args, **kwargs)
        metadata = kwargs.get("metadata", {})
        draws.append({"times": kwargs.get("times"), "metadata": dict(metadata),
                      "samples": tuple(kwargs.get("sample_times", ()))})
        return result

    monkeypatch.setattr(charts, "braille_chart", capture)
    item = SimpleNamespace(app=app, views=views, store=store, timer=timer,
                           surface=request.param, cache=screen._FrameCache(), draws=draws,
                           width=260, height=70)
    try:
        redraw(item)
        yield item
    finally:
        if app.research:
            app.research.close()


def redraw(scene):
    scene.cache.rebuild(scene.app, scene.views, scene.store, None, scene.width, scene.height)


def control(scene):
    records = M.initialize(scene.app)["records"]
    if scene.surface == "modal":
        return next(record for record in records if record.layer == 1 and record.key[2] == "loss")
    return next(record for record in records if record.key[0] == "resource-series"
                and record.key[2] == "cpu-rate")


def plot(scene):
    key = control(scene).key
    return next(record for record in C.initialize(scene.app)["plots"] if record.key == key)


def click(scene, y, x, button):
    before = (scene.app.tab, scene.app.mode, scene.app.selected_id, scene.app.analytics_job)
    scene.app.click(y, x, scene.cache.hits, button=button)
    assert (scene.app.tab, scene.app.mode, scene.app.selected_id, scene.app.analytics_job) == before


def control_text(scene, *, feedback=False):
    target = control(scene)
    rows, overlays = scene.cache.rows, scene.cache.content
    if feedback:
        rows, overlays, _ = scene.cache.feedback(scene.app, scene.views)
    for y, x, row in reversed(overlays):
        if y == target.rect.top and x <= target.rect.left and x + L.vlen(L.row_text(row)) >= target.rect.right:
            return _slice(row, target.rect.left - x, target.rect.right - x)
    return _slice(rows[target.rect.top], target.rect.left, target.rect.right)


def assert_drag_refresh(scene):
    # Isolate changed slider state from maintenance/source/animation deadlines.
    # A held pointer must publish new graph content within a short UI interval.
    scene.cache.next_maintenance = scene.cache.next_animation = scene.cache.next_live = math.inf
    assert scene.cache.due(scene.app, scene.width, scene.height, now=time.monotonic() + .2)
    redraw(scene)
    assert M.active(scene.app)


def test_delta_drag_repaints_live_plot_and_fit_before_release(scene):
    target = control(scene)
    click(scene, target.toggle.top, target.toggle.left, "left")
    redraw(scene)
    target = control(scene)
    assert M.enabled(scene.app, target.key)
    geometry = target.slider_full
    y = target.slider.top
    click(scene, y, geometry.left, "press")
    redraw(scene)
    previous = plot(scene)
    for x in (geometry.right - 1, (geometry.left + geometry.right - 1) // 2, geometry.left):
        scene.timer[0] += .1
        scene.draws.clear()
        click(scene, y, x, "drag")
        delta = M.initialize(scene.app)["entries"][target.key]["delta"]
        assert_drag_refresh(scene)
        current = plot(scene)
        assert current.x_bounds[1] - current.x_bounds[0] == pytest.approx(delta)
        assert current.x_bounds != previous.x_bounds
        assert current.x_bounds[1] >= previous.x_bounds[1]
        assert control(scene).slider_full == geometry
        matching = [draw for draw in scene.draws if draw["metadata"].get("x_bounds") == current.x_bounds]
        assert matching
        assert all(draw["times"] == current.x_bounds for draw in matching)
        assert all(max(draw["samples"]) <= scene.timer[0] for draw in matching if draw["samples"])
        assert any(draw["metadata"].get("trend") for draw in matching)
        if delta == 1.:
            # Twenty real samples spread over this wide one-second view are
            # sparse. A dense-history range band must not survive its cache.
            assert all(not draw["metadata"].get("band") for draw in matching)
        assert M.format_delta(delta) in control_text(scene)
        previous = current
    click(scene, y, geometry.left, "release")
    assert not M.active(scene.app)
    redraw(scene)
    assert plot(scene).x_bounds[1] - plot(scene).x_bounds[0] == pytest.approx(30.)


def test_delta_drag_in_automatic_history_is_only_next_live_preference(scene):
    target = control(scene)
    assert not M.enabled(scene.app, target.key)
    original = plot(scene)
    geometry = target.slider_full
    y = target.slider.top
    click(scene, y, geometry.left, "press")
    redraw(scene)
    scene.cache.next_maintenance = scene.cache.next_animation = scene.cache.next_live = math.inf
    for x in (geometry.right - 1, geometry.left):
        click(scene, y, x, "drag")
        assert M.active(scene.app)
        assert not M.enabled(scene.app, target.key)
        assert not scene.cache.due(scene.app, scene.width, scene.height,
                                   now=time.monotonic() + .2)
        assert plot(scene).x_bounds == original.x_bounds
        assert control(scene).slider_full == geometry
        delta = M.initialize(scene.app)["entries"][target.key]["delta"]
        # The preference is visible immediately even when the full-history
        # document needs no extra rasterization for this slider movement.
        assert M.format_delta(delta) in control_text(scene, feedback=True)
    click(scene, y, geometry.right - 1, "release")
    redraw(scene)
    assert not M.enabled(scene.app, target.key)
    assert plot(scene).x_bounds == original.x_bounds
    click(scene, control(scene).toggle.top, control(scene).toggle.left, "left")
    redraw(scene)
    assert plot(scene).x_bounds[1] - plot(scene).x_bounds[0] == pytest.approx(1.)


@pytest.mark.parametrize("live", [False, True])
def test_sampling_drag_reports_current_rate_and_never_reverses_playhead(scene, live):
    target = control(scene)
    if live:
        click(scene, target.toggle.top, target.toggle.left, "left")
        redraw(scene)
        target = control(scene)
    geometry = target.rate_slider_full
    y = target.rate_slider.top
    click(scene, y, geometry.left, "press")
    redraw(scene)
    end = plot(scene).x_bounds[1]
    for x, expected in ((geometry.right - 1, .5), (geometry.left, 5.),
                        (geometry.right - 1, .5), (geometry.left, 5.)):
        scene.timer[0] += .1
        click(scene, y, x, "drag")
        assert_drag_refresh(scene)
        current = plot(scene)
        assert current.x_bounds[1] >= end
        assert current.x_bounds[1] - end <= .125 + 1e-9
        assert current.x_bounds[1] <= 200.
        assert M.enabled(scene.app, target.key) is live
        assert S.cadence(scene.app, target.key) == pytest.approx(expected)
        text = control_text(scene)
        assert "Poll " + S.format_interval(expected) in text
        assert "Set " + S.format_interval(expected) in text
        assert control(scene).rate_slider_full == geometry
        end = current.x_bounds[1]
    click(scene, y, geometry.left, "release")
    assert not M.active(scene.app)
    redraw(scene)
    assert plot(scene).x_bounds[1] == end


def test_dense_slider_reports_use_bounded_preview_deadline_and_latest_value(scene):
    target = control(scene)
    click(scene, target.toggle.top, target.toggle.left, "left")
    redraw(scene)
    target = control(scene)
    geometry, y = target.slider_full, target.slider.top
    click(scene, y, geometry.left, "press")
    redraw(scene)
    scene.cache.next_maintenance = scene.cache.next_animation = scene.cache.next_live = math.inf
    ready = scene.cache.preview_ready
    assert ready > 0
    before = plot(scene).x_bounds
    for x in ((geometry.left + geometry.right) // 2, geometry.left + 1, geometry.right - 1):
        click(scene, y, x, "drag")
        assert not scene.cache.due(scene.app, scene.width, scene.height, now=ready - .001)
    assert plot(scene).x_bounds == before
    assert scene.cache.next_preview == ready
    assert 1 <= scene.cache.wait_ms(now=ready - .01) <= 11
    # No new mouse report is needed for the pending preview to wake the UI.
    assert scene.cache.due(scene.app, scene.width, scene.height, now=ready)
    redraw(scene)
    assert M.active(scene.app)
    assert plot(scene).x_bounds[1] - plot(scene).x_bounds[0] == pytest.approx(1.)
    # A release is committed immediately, even inside the next preview period.
    click(scene, y, geometry.left, "release")
    assert not M.active(scene.app)
    assert scene.cache.due(scene.app, scene.width, scene.height,
                           now=scene.cache.preview_ready - .001)
    redraw(scene)
    assert plot(scene).x_bounds[1] - plot(scene).x_bounds[0] == pytest.approx(30.)

