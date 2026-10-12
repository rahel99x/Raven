"""Graph right-click resets only its published source, with safe empty recovery."""
from types import SimpleNamespace

import pytest

from raven import analysis_ui as A, chart_interaction as C, layout as L, metric_live as M
from raven.config import Config
from raven.views import Views


@pytest.fixture
def app():
    value = SimpleNamespace(mode="main", tab="analytics", width=120, height=40,
                            selected_id="101", analytics_job="101", analytics_view="job",
                            toolbar_state={}, project_state={}, job_panel_state={}, marks={"101", "102"},
                            research=SimpleNamespace(generation=3), messages=[])
    value.say = value.messages.append
    A.initialize(value)
    C.begin_frame(value, 120, 40)
    return value


def record(app, identity=("resource-series", "101", "CPU", "%"), *, row=0, empty=False, scale="linear"):
    return C.record(app, identity, {"plot_rect": (2, 10, 12, 60), "x_bounds": (0., 100.),
                                    "y_bounds": (0., 200.), "has_data": not empty}, row=row, scale=scale)


def publish(app, **options):
    record(app, **options)
    return C.publish(app, 120, 40)[-1]


@pytest.mark.parametrize("tab", ["jobs", "analytics"])
@pytest.mark.parametrize("fit", [False, True])
def test_right_click_cancels_preview_resets_zoom_and_preserves_selected_jobs(app, tab, fit, monkeypatch):
    app.tab = tab
    C.begin_frame(app, 120, 40)
    item = publish(app)
    C._apply(app, item, {"x": (10., 60.), "y": (20., 70.), "fit_y": fit})
    C.handle_mouse(app, 3, 15, button="press")
    C.handle_mouse(app, 8, 45, button="drag")
    assert C.active(app)
    app.store = SimpleNamespace(snapshot=lambda: pytest.fail("right-click read scheduler source"))
    app.research.request = lambda *args: pytest.fail("right-click requested metrics")
    monkeypatch.setattr(C.charts, "braille_chart", lambda *args, **kwargs: pytest.fail("right-click rasterized graph"))
    assert C.handle_mouse(app, 5, 30, button="right")
    assert C.bounds(app, item.key) is None and not C.active(app)
    assert not C.autofit(app, item.key)
    assert app.selected_id == "101" and app.marks == {"101", "102"}
    assert app.messages[-1] == "Chart reset to its full default view"
    assert C.handle_mouse(app, 8, 45, button="release")  # consume the cancelled drag's late release
    assert C.bounds(app, item.key) is None


def test_already_full_graph_consumes_right_click_without_bumping_zoom_revision(app):
    item = publish(app)
    revision = C.initialize(app)["revision"]
    assert C.handle_mouse(app, 5, 30, button="right")
    assert C.initialize(app)["revision"] == revision
    assert C.initialize(app)["last_key"] == item.key
    assert app.selected_id == "101" and app.marks == {"101", "102"}


@pytest.mark.parametrize("point", [(1, 30), (2, 9), (12, 30), (5, 60), (0, 0), (39, 119)])
def test_axes_margins_and_unrelated_surfaces_do_not_reset_a_chart(app, point):
    item = publish(app)
    C._apply(app, item, {"x": (10., 60.), "y": (20., 70.)})
    assert not C.handle_mouse(app, *point, button="right")
    assert C.bounds(app, item.key) == {"x": (10., 60.), "y": (20., 70.)}


@pytest.mark.parametrize("change", ["menu", "panel", "mode", "width", "job"])
def test_modal_covered_and_stale_plot_geometry_cannot_be_reset(app, change):
    item = publish(app)
    C._apply(app, item, {"x": (10., 60.), "y": (20., 70.)})
    if change == "menu": app.toolbar_state["menu"] = "View"
    elif change == "panel": app.toolbar_state["panel"] = "help"
    elif change == "mode": app.mode = "confirm"
    elif change == "width": app.width = 121
    elif change == "job": app.analytics_job = "102"
    assert not C.handle_mouse(app, 5, 30, button="right")
    assert C.bounds(app, item.key) is not None


def test_unrelated_reader_generation_keeps_native_capture_and_right_reset_valid(app):
    identity = C.key(app, "CPU", "%", "101", scope="resource-series", attempt="first")
    item = publish(app, identity=identity)
    C._apply(app, item, {"x": (10., 60.), "y": (20., 70.)})
    assert C.handle_mouse(app, 3, 15, button="press")
    app.research.generation += 1
    assert C.key(app, "CPU", "%", "101", scope="resource-series", attempt="first") == identity
    assert C.handle_mouse(app, 8, 45, button="drag") and C.active(app)
    assert C.handle_mouse(app, 5, 30, button="right")
    assert not C.active(app) and C.bounds(app, identity) is None
    assert C.handle_mouse(app, 8, 45, button="release")
    assert C.bounds(app, identity) is None


def test_reported_source_generation_invalidates_stale_reset_and_capture(app):
    options = {"scope": "reported-metric", "attempt": "first"}
    identity = C.key(app, "loss", "/project/job-101/metrics.jsonl", "101", **options)
    item = publish(app, identity=identity)
    C._apply(app, item, {"x": (10., 60.), "y": (20., 70.)})
    assert C.handle_mouse(app, 3, 15, button="press")
    app.research.generation += 1
    current_key = C.key(app, "loss", "/project/job-101/metrics.jsonl", "101", **options)
    assert current_key != identity
    assert not C.handle_mouse(app, 5, 30, button="right")
    assert not C.active(app) and C.bounds(app, identity) is not None
    C.begin_frame(app, 120, 40)
    current = publish(app, identity=current_key)
    assert current.rect == item.rect and current.x_bounds == item.x_bounds
    assert C.handle_mouse(app, 8, 45, button="release")
    assert C.bounds(app, current_key) is None
    assert C.bounds(app, identity) == {"x": (10., 60.), "y": (20., 70.)}


def test_only_clicked_source_resets_while_duplicate_views_share_the_same_exact_zoom(app):
    first = record(app)
    duplicate = record(app, row=12)
    other_key = ("resource-series", "101", "Memory", "GiB")
    other = record(app, other_key, row=24)
    C.publish(app, 120, 40)
    for item in (first, other):
        C._apply(app, item, {"x": (10., 60.), "y": (20., 70.)})
    assert duplicate.key == first.key
    assert C.handle_mouse(app, 17, 30, button="right")
    assert C.bounds(app, first.key) is None and C.bounds(app, duplicate.key) is None
    assert C.bounds(app, other.key) == {"x": (10., 60.), "y": (20., 70.)}


def test_empty_zoom_interval_is_reset_only_and_recovers_without_new_capture(app):
    item = publish(app)
    C._apply(app, item, {"x": (10., 60.), "y": (20., 70.), "fit_y": True})
    C.begin_frame(app, 120, 40)
    empty = publish(app, empty=True)
    assert empty.kind == "metric-empty"
    assert not C.hover(app, 5, 30) and C.feedback(app) == []
    assert not C.handle_mouse(app, 5, 30, button="press")
    assert not C.active(app)
    assert C.handle_mouse(app, 5, 30, button="right")
    assert C.bounds(app, item.key) is None
    C.begin_frame(app, 120, 40)
    assert record(app, empty=True) is None
    assert C.publish(app, 120, 40) == ()


def test_empty_retention_preserves_held_axes_until_right_reset_cancels_and_recovers(app):
    item = publish(app)
    C._apply(app, item, {"x": (10., 60.), "y": (20., 70.)})
    C.handle_mouse(app, 3, 15, button="press")
    C.begin_frame(app, 120, 40)
    empty = publish(app, empty=True)
    assert empty.rect == item.rect and empty.x_bounds == item.x_bounds
    assert C.active(app) and empty.kind == "metric-empty"
    assert C.captured_bounds(app, item.key) == {"x": item.x_bounds, "y": item.y_bounds}
    assert C.handle_mouse(app, 5, 30, button="right")
    assert not C.active(app) and C.bounds(app, item.key) is None
    assert C.handle_mouse(app, 8, 45, button="release")
    assert C.bounds(app, item.key) is None
    C.begin_frame(app, 120, 40)
    recovered = publish(app)
    assert recovered.kind == "metric" and recovered.x_bounds == item.x_bounds
    assert app.selected_id == "101" and app.marks == {"101", "102"}


def test_empty_unzoomed_or_different_scale_chart_never_becomes_a_reset_target(app):
    identity = ("resource-series", "101", "CPU", "%")
    assert record(app, identity, empty=True) is None
    item = publish(app, identity=identity, scale="log")
    C._apply(app, item, {"x": (10., 60.), "y": (20., 70.)})
    C.begin_frame(app, 120, 40)
    assert record(app, identity, empty=True, scale="linear") is None
    assert record(app, ("different",), empty=True, scale="log") is None


def test_right_click_stops_only_clicked_live_metric_even_when_its_window_is_empty(app):
    identity = ("resource-series", "101", "CPU", "%")
    other = ("resource-series", "101", "Memory", "GiB")
    for key in (identity, other):
        M._entry(app, key, running=True)
        assert M.set_enabled(app, key, True)
    empty = publish(app, identity=identity, empty=True)
    assert empty.kind == "metric-empty"
    assert C.handle_mouse(app, 5, 30, button="right")
    assert not M.initialize(app)["entries"][identity]["enabled"]
    assert M.initialize(app)["entries"][other]["enabled"]


def test_inspector_right_click_restores_full_keyboard_window_and_discards_stale_visible_cache(app):
    app.mode = "analysis"
    identity = ("reported-metric", "101", "loss", "reported.jsonl")
    app.analysis_state.update(modal="chart", chart_interaction_key=identity, zoom=8., pan=.75,
                              window=3., preset="custom", cursor=10, chart_visible={"points": []})
    C.begin_frame(app, 120, 40)
    C.record(app, identity, {"plot_rect": (2, 10, 12, 60), "x_bounds": (40., 43.),
                              "y_bounds": (0., 200.), "has_data": False}, layer=1)
    empty = C.publish(app, 120, 40)[0]
    assert empty.kind == "metric-empty"
    assert C.handle_mouse(app, 5, 30, button="right")
    assert app.analysis_state["zoom"] == 1. and app.analysis_state["pan"] == 0.
    assert app.analysis_state["cursor"] == 0 and app.analysis_state["preset"] == "all"
    assert "window" not in app.analysis_state and "chart_visible" not in app.analysis_state


@pytest.mark.parametrize("fit", [False, True])
def test_shared_native_curve_renderer_returns_full_time_and_original_percent_axes_after_right_reset(app, fit):
    views = Views(L.Glyphs(False), Config({}))
    identity = ("resource-series", "101", "CPU", "%")
    item = publish(app, identity=identity)
    C._apply(app, item, {"x": (40., 60.), "y": (45., 55.), "fit_y": fit})
    C.begin_frame(app, 120, 40)
    views.metric_curve(app, [0., 100.], 100, 8, identity, lo=0., hi=100., title="CPU", unit="%",
                       sample_times=[0., 100.], sample_interval=100.)
    zoomed = C.publish(app, 120, 40)[0]
    assert zoomed.x_bounds == (40., 60.)
    assert C.handle_mouse(app, zoomed.visible.top + 1, zoomed.visible.left + 1, button="right")
    C.begin_frame(app, 120, 40)
    views.metric_curve(app, [0., 100.], 100, 8, identity, lo=0., hi=100., title="CPU", unit="%",
                       sample_times=[0., 100.], sample_interval=100.)
    full = C.publish(app, 120, 40)[0]
    assert full.x_bounds == (0., 100.) and full.y_bounds == (0., 100.)
