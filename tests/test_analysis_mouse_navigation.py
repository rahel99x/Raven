"""Analysis documents expose the existing navigation and comparison actions."""

import pytest

from raven import analysis_ui as A, chart_interaction, layout as L
from raven.views import Views
from test_analysis_ui import app as app


def paint(app, width=80, height=24):
    views = Views(L.Glyphs(True), app.cfg)
    app.views_ref = views
    _, hits = views.compose(app.store.snapshot(), app, width, height)
    overlays = views.overlay(app.store.snapshot(), app, width, height)
    return hits, overlays


def target(app, name):
    return next((y, value) for y, _, value in app.analysis_state["control_hits"]
                if value["id"] == "analysis-navigation:" + name)


@pytest.mark.parametrize("command", ["chart loss", "inspect 1", "timeline", "diff 1 2", "dashboard"])
def test_every_analysis_document_closes_with_a_native_mouse_gesture(app, command):
    app.run_command(command)
    hits, _ = paint(app)
    y, value = target(app, "back")
    assert value["label"] == "Close"
    app.click(y, value["left"], hits, button="press")
    assert app.mode == "analysis"
    app.click(y, value["left"], hits, button="release")
    assert app.mode == "main"
    assert app.selected_id == "1"


def test_mouse_back_restores_the_same_chart_after_inspector_drilldown(app):
    app.run_command("chart loss")
    A.open_inspector(app, "1")
    hits, _ = paint(app)
    y, value = target(app, "back")
    assert value["label"] == "Back"
    app.click(y, value["left"], hits)
    assert app.mode == "analysis"
    assert app.analysis_state["modal"] == "chart"
    assert app.analysis_state["metric"] == "loss"
    assert app.analysis_state["chart_job"] == "1"


def test_chart_event_footer_returns_to_the_same_recorded_metric(app):
    app.run_command("chart loss")
    A.handle_key(app, "e")
    assert app.analysis_state["modal"] == "chart_events"
    hits, _ = paint(app)
    y, value = target(app, "back")
    assert value["label"] == "Back"
    app.click(y, value["left"], hits)
    assert app.analysis_state["modal"] == "chart"
    assert app.analysis_state["metric"] == "loss"


def test_an_old_footer_release_cannot_close_a_new_analysis_document(app):
    app.run_command("chart loss")
    hits, _ = paint(app)
    y, value = target(app, "back")
    app.click(y, value["left"], hits, button="press")
    app.analysis_state.update(modal="inspect", job="2")
    app.click(y, value["left"], hits, button="release")
    assert app.mode == "analysis"
    assert app.analysis_state["modal"] == "inspect" and app.analysis_state["job"] == "2"


@pytest.mark.parametrize("name,tab", [("logs", "log"), ("evidence", "research")])
def test_inspector_footer_navigates_to_the_exact_jobs_existing_tools(app, name, tab):
    app.run_command("inspect 1")
    hits, _ = paint(app)
    y, value = target(app, name)
    app.click(y, value["left"], hits)
    assert app.mode == "main" and app.tab == tab
    if name == "logs":
        assert app.log_job == "1"
    else:
        assert app.research_job_id == "1" and app.research_view == "evidence"


def test_comparison_toggles_and_chart_metric_navigation_are_mouse_actions(app):
    app.run_command("diff 1 2")
    hits, _ = paint(app)
    y, value = target(app, "unchanged")
    app.click(y, value["left"], hits)
    assert app.analysis_state["unchanged"]
    hits, _ = paint(app)
    y, value = target(app, "scales")
    previous = app.analysis_state["shared_scale"]
    app.click(y, value["left"], hits)
    assert app.analysis_state["shared_scale"] != previous
    app.run_command("chart loss")
    hits, _ = paint(app)
    y, value = target(app, "next")
    app.click(y, value["left"], hits)
    assert app.analysis_state["metric"] == "accuracy"


@pytest.mark.parametrize("width,height", [(24, 8), (40, 12), (80, 24), (160, 40)])
def test_analysis_footer_remains_visible_and_outside_scrollable_plot_geometry(app, width, height):
    app.run_command("chart loss")
    for scroll in (0, 100000):
        app.analysis_state["scroll"] = scroll
        _, overlays = paint(app, width, height)
        y, value = target(app, "back")
        assert 0 <= y < height and 0 <= value["left"] < value["right"] <= width
        assert all(not plot.visible.contains(y, value["left"])
                   for plot in chart_interaction.initialize(app)["plots"])
        assert all(L.row_text(row).isascii() for _, _, row in overlays)
