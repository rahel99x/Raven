"""Separate presentation boxes reuse exact source and sampling identities."""
from dataclasses import replace

import pytest

from raven import chart_interaction as C, metric_live as M
from raven.pane_context import PaneContext
from test_chart_interaction import app, metadata, publish
from test_metric_live import palette_dashboard, palette_frame


@pytest.mark.parametrize("workspace", ["jobs", "history"])
def test_inline_zoom_is_local_but_plot_keeps_source_identity(app, workspace):
    app.tab = workspace
    app.job_panel_state = {"mode": "analytics", "analytics_view": "job"}
    pane = PaneContext(app, identity=workspace + ":details:analytics:job", tab="analytics", mode="main")
    identity = C.key(pane, "CPU", "%", "101", scope="resource-series", attempt="attempt-1")
    plot = C.record(pane, identity, metadata())
    C.publish(app)
    assert plot.key == identity and M.canonical(plot.key) == identity
    target = {"x": (10., 80.), "y": (20., 100.), "fit_y": True}
    C._apply(app, plot, target)
    assert C.bounds(pane, identity) == C.bounds(app, identity) == {"x": target["x"], "y": target["y"]}
    assert C.autofit(pane, identity)
    app.tab = "analytics"
    assert C.bounds(app, identity) is None
    assert not C.reset(app, identity)
    assert C.bounds(pane, identity) is not None
    assert C.undo(pane, identity) and C.bounds(pane, identity) is None


def test_fullscreen_scope_and_inline_scope_keep_independent_undo_and_reset(app):
    identity = ("metric", "101", "CPU")
    native = publish(app, identity)
    C._apply(app, native, {"x": (10., 90.), "y": (10., 190.)})
    native_bounds = C.bounds(app, identity)
    app.mode = "analysis"
    app.analysis_state["modal"] = "chart"
    C.begin_frame(app)
    full = C.record(app, identity, metadata(), layer=1)
    C.publish(app)
    assert C.bounds(app, identity) is None
    C._apply(app, full, {"x": (20., 70.), "y": (20., 170.)})
    assert C.bounds(app, identity) != native_bounds
    assert C.reset(app, identity)
    app.mode = "main"
    assert C.bounds(app, identity) == native_bounds


def test_live_reset_only_clears_matching_source_in_current_presentation(app):
    identity = ("resource-series", "101", "CPU", "%")
    native = publish(app, identity)
    target = {"x": (10., 90.), "y": (10., 190.)}
    C._apply(app, native, target)
    pane = PaneContext(app, identity="jobs:details:analytics:job")
    inline = replace(native, pane=pane.pane_identity)
    C._apply(app, inline, target)
    assert C.clear_source_zoom(pane, identity, M.canonical)
    assert C.bounds(pane, identity) is None
    assert C.bounds(app, identity) is not None
    assert not C.clear_source_zoom(pane, identity, M.canonical)


def test_actual_jobs_details_and_analytics_keep_distinct_boxes(palette_dashboard):
    dashboard, app = palette_dashboard, palette_dashboard.app
    app.tab = "jobs"
    app.job_panel_state["mode"] = "analytics"
    palette_frame(dashboard, width=220, height=72)
    plot = next(plot for plot in C.initialize(app)["plots"] if plot.key[0] == "resource-series")
    assert plot.pane == "jobs:details:analytics:job"
    start = (plot.visible.top + 1, plot.visible.left + 2)
    end = (plot.visible.top + 1, plot.visible.right - 2)
    for point, button in ((start, "press"), (end, "drag"), (end, "release")):
        app.click(*point, app.last_hits, button=button)
    box = C.bounds(app, plot.key)
    assert box is not None
    app.tab = "analytics"
    palette_frame(dashboard, width=220, height=72)
    same = next(item for item in C.initialize(app)["plots"] if item.key == plot.key)
    assert same.pane is None and C.bounds(app, plot.key) is None
    assert same.x_bounds != box["x"]
    app.tab = "jobs"
    palette_frame(dashboard, width=220, height=72)
    assert C.bounds(app, plot.key) == box
    restored = next(item for item in C.initialize(app)["plots"] if item.key == plot.key)
    assert restored.x_bounds == box["x"]


@pytest.mark.parametrize("surface", ["research", "fullscreen"])
def test_headless_legacy_plot_uses_current_presentation_without_global_zoom_leak(app, surface):
    from raven.interaction import Rect
    if surface == "research":
        app.tab, app.research_view = "research", "experiment"
    else:
        app.mode = "analysis"
        app.analysis_state["modal"] = "chart"
    identity = ("reported-metric", "101", "loss")
    plot = C.Plot(identity, Rect(0, 0, 4, 20), Rect(0, 0, 4, 20), (0., 100.), (0., 100.))
    C._apply(app, plot, {"x": (10., 20.), "y": (0., 10.)})
    assert C.bounds(app, identity) == {"x": (10., 20.), "y": (0., 10.)}
    app.tab, app.mode = "analytics", "main"
    assert C.bounds(app, identity) is None
