"""Pointer reports cannot become navigation or use a displaced painted frame."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from raven import chart_interaction as charts, clock, layout, metric_live, text_selection
from raven.config import Config
from raven.controller import App
from raven.metrics import write_metric
from raven.model import Finished, Job, Store
from raven.remote import LocalFiles
from raven.research import ResearchHub
from raven.views import TABS, Views


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: 200.0)
    cfg = Config({"animations": False, "startup": {"enabled": False}, "log_lines": 0})
    store = Store(persist=False)
    store.jobs = [Job(str(jid), "job-" + str(jid), "cpu", "RUNNING", cpus=4,
                      mem_req="8G", submit="submit", start="start") for jid in (7, 8)]
    store.finished = [Finished("17", "old-job", "FAILED", end="2026-10-08T11:00:00")]
    for job in store.jobs:
        for index in range(20):
            store.record(job.id, {"k": "live", "t": 180.0 + index,
                                  "cpu": (index + 1) / 25, "rss": (1 + index / 20) * 1024**3})
    path = tmp_path / "metrics.jsonl"
    for index in range(20):
        write_metric(path, {"loss": 1 / (index + 1)}, step=index, t=180.0 + index)
    app = App(store, None, None, cfg, "tester", interactive=False)
    views = Views(layout.Glyphs(False), cfg, files=LocalFiles())
    app.views_ref, app.logs.files = views, views.files
    app.selected_id = app.analytics_job = app.research_job_id = "7"
    app.research = ResearchHub(cfg, views.files)
    app.research.configure(metrics_file=str(path), workdir=str(tmp_path))
    app.research.request(app.research.context(store.snapshot(), app), wait=True, force=True)

    def draw(tab="jobs", panel="analytics"):
        app.tab = tab
        if tab == "jobs":
            app.job_panel_state.update(mode=panel, analytics_view="job", research_view="experiment")
        rows, hits = views.compose(store.snapshot(), app, 190, 90)
        views.overlay(store.snapshot(), app, 190, 90)
        return rows, hits

    yield SimpleNamespace(app=app, store=store, views=views, draw=draw)
    app.research.close()


@pytest.mark.parametrize("tab", [name for name, _ in TABS])
@pytest.mark.parametrize("button", ["motion", "drag", "release", "wheel-up", "wheel-down", "middle", "unknown"])
def test_nonprimary_event_cannot_activate_another_global_tab(dashboard, tab, button):
    app = dashboard.app
    _, hits = dashboard.draw(tab)
    ty, left, _, target = next(hit for hit in app.tab_hits if hit[3] != tab)
    app.click(ty, left + 1, hits, button=button)
    assert app.tab == tab and app.mode == "main" and not app.quit
    assert target != app.tab


@pytest.mark.parametrize("tab,panel", [("jobs", "analytics"), ("jobs", "research"),
                                      ("analytics", "analytics"), ("research", "research")])
@pytest.mark.parametrize("ascii_", [False, True])
def test_repeated_hover_between_jobs_and_graphs_keeps_selection_and_has_no_source_work(dashboard, monkeypatch, tab, panel, ascii_):
    app = dashboard.app
    dashboard.views.set_ascii(ascii_)
    _, hits = dashboard.draw(tab, panel)
    plot = next(plot for plot in charts.initialize(app)["plots"] if plot.kind == "metric")
    graph_points = [(plot.visible.top + 1, plot.visible.left + 2),
                    (plot.visible.bottom - 1, plot.visible.right - 2)]
    row_points = [(y, 4) for y, kind, _ in hits if kind in ("job", "recent", "fin")]
    if not row_points:
        row_points = [(plot.visible.top + 1, plot.visible.left - 1)]
    points = row_points + graph_points
    selected, marks, mode = app.selected_id, set(app.marks), app.job_panel_state["mode"]
    published_hits = app.last_hits

    def forbidden(*args, **kwargs):
        pytest.fail("Pointer feedback requested source or chart work")

    monkeypatch.setattr(dashboard.store, "snapshot", forbidden)
    monkeypatch.setattr(app.research, "current", forbidden)
    monkeypatch.setattr(app.research, "request", forbidden)
    monkeypatch.setattr(dashboard.views, "metric_curve", forbidden)
    monkeypatch.setattr(dashboard.views.files, "stat", forbidden)
    monkeypatch.setattr(dashboard.views.files, "read", forbidden)
    for _ in range(30):
        for y, x in points:
            app.click(y, x, hits, button="motion")
            assert app.tab == tab and app.mode == "main"
            assert app.selected_id == selected and app.marks == marks
            assert app.job_panel_state["mode"] == mode
            assert app.last_hits is published_hits
    assert not charts.active(app) and not metric_live.active(app)


@pytest.mark.parametrize("button", ["left", "press"])
@pytest.mark.parametrize("mutation", ["selected_job", "scroll", "mode", "tab", "resize"])
def test_displaced_details_button_cannot_activate_from_an_old_frame(dashboard, button, mutation):
    app = dashboard.app
    _, hits = dashboard.draw()
    y, _, (_, left, _) = next(hit for hit in hits if hit[1] == "job_panel_tab" and hit[2][0] == "research")
    if mutation == "selected_job":
        app.selected_id = "8"
    elif mutation == "scroll":
        app.layout_state.scroll["jobs:details"] = 20
    elif mutation == "mode":
        app.mode = "help"
    elif mutation == "tab":
        app.tab = "history"
    else:
        app.width -= 20
    before = (app.tab, app.mode, app.selected_id, app.job_panel_state["mode"])
    app.click(y, left + 1, hits, button=button)
    assert (app.tab, app.mode, app.selected_id, app.job_panel_state["mode"]) == before


@pytest.mark.parametrize("button", ["left", "press", "motion", "drag", "release"])
def test_forged_hit_map_does_not_replace_or_activate_published_content(dashboard, button):
    app = dashboard.app
    _, hits = dashboard.draw()
    y, _, (_, left, right) = next(hit for hit in hits if hit[1] == "job_panel_tab" and hit[2][0] == "research")
    forged = [(y, "job_panel_tab", ("research", left, right)), (y, "research_metric", "forged")]
    actual_hits = app.last_hits
    app.click(y, left + 1, forged, button=button)
    assert app.tab == "jobs" and app.job_panel_state["mode"] == "analytics"
    assert app.last_hits is actual_hits


def test_changed_payload_in_the_same_hit_map_fails_closed(dashboard):
    app = dashboard.app
    _, hits = dashboard.draw()
    y, _, value = next(hit for hit in hits if hit[1] == "control" and isinstance(hit[2], dict))
    value["action"] = ("command", "view experiment")
    app.click(y, value["left"], hits)
    assert app.tab == "jobs" and app.mode == "main"
    assert app.job_panel_state["mode"] == "analytics"


def test_unvalidated_published_hit_token_fails_closed(dashboard):
    app = dashboard.app
    _, hits = dashboard.draw()
    y, _, (_, left, _) = next(hit for hit in hits if hit[1] == "job_panel_tab" and hit[2][0] == "research")
    app.interaction_state["published_hit_token"] = None
    app.click(y, left + 1, hits)
    assert app.tab == "jobs" and app.job_panel_state["mode"] == "analytics"


@pytest.mark.parametrize("hits", [None, "not a frame", {}, [(10, "job")], [True]])
def test_malformed_native_hit_maps_are_inert(dashboard, hits):
    app = dashboard.app
    dashboard.draw()
    actual_hits = app.last_hits
    app.click(10, 4, hits, button="left")
    assert app.last_hits is actual_hits
    assert app.tab == "jobs" and app.mode == "main" and app.selected_id == "7"


@pytest.mark.parametrize("owner,marker", [("scrollbar_state", "discard_release"),
                                          ("pane_drag_state", "discard_release"),
                                          ("history_browser_state", "discard_release"),
                                          ("text_selection_state", "discard_release"),
                                          ("metric_live_state", "cancelled_release")])
def test_cancelled_owner_cannot_steal_a_fresh_graph_gestures_release(dashboard, owner, marker):
    app = dashboard.app
    _, hits = dashboard.draw()
    plot = next(plot for plot in charts.initialize(app)["plots"] if plot.kind == "metric")
    if owner == "text_selection_state":
        text_selection.initialize(app)
    getattr(app, owner)[marker] = True
    start = (plot.visible.top + 1, plot.visible.left + 2)
    end = (plot.visible.top + 1, plot.visible.right - 2)
    app.click(*start, hits, button="press")
    assert charts.active(app)
    app.click(*end, hits, button="drag")
    app.click(*end, hits, button="release")
    assert not charts.active(app)
    assert charts.bounds(app, plot.key) is not None
    assert app.tab == "jobs" and app.selected_id == "7"


def test_cancelled_chart_release_cannot_steal_a_fresh_sampling_drag(dashboard):
    app = dashboard.app
    _, hits = dashboard.draw()
    control = metric_live.initialize(app)["records"][0]
    charts.initialize(app)["cancelled_release"] = True
    slider = control.rate_slider
    app.click(slider.top, slider.left, hits, button="press")
    assert metric_live.active(app)
    app.click(slider.top, slider.right - 1, hits, button="drag")
    app.click(slider.top, slider.right - 1, hits, button="release")
    assert not metric_live.active(app)
    assert metric_live.initialize(app)["entries"][control.key]["rate"] == 100
    assert app.tab == "jobs" and app.selected_id == "7"


def test_fresh_published_hits_remain_valid_when_embedding_clears_last_hits(dashboard):
    app = dashboard.app
    _, hits = dashboard.draw()
    y, _, (_, left, _) = next(hit for hit in hits if hit[1] == "job_panel_tab" and hit[2][0] == "research")
    app.last_hits = []
    app.click(y, left + 1, hits)
    assert app.tab == "jobs" and app.job_panel_state["mode"] == "research"


def test_global_tabs_remain_usable_after_job_selection_without_a_content_repaint(dashboard):
    app = dashboard.app
    _, hits = dashboard.draw()
    y = next(y for y, kind, value in hits if kind == "job" and value == "8")
    app.click(y, app.workspace_main_rect.x + 4, hits)
    assert app.selected_id == "8"
    y, left, _, _ = next(hit for hit in app.tab_hits if hit[3] == "history")
    app.click(y, left + 1, hits)
    assert app.tab == "history"


def test_terminal_probe_observes_motion_without_navigation(dashboard):
    app = dashboard.app
    _, hits = dashboard.draw()
    app.mode = "terminal_probe"
    ty, left, _, _ = next(hit for hit in app.tab_hits if hit[3] == "research")
    app.click(ty, left, hits, button="motion")
    assert app.tab == "jobs" and app.mode == "terminal_probe"
    assert "Mouse: motion" in app.session_tools_state["probe_events"][-1]


@pytest.mark.parametrize("y,x", [(True, 5), (5, False), (None, 5), (5, 1.5), ("4", 6)])
def test_malformed_pointer_coordinates_are_inert(dashboard, y, x):
    app = dashboard.app
    _, hits = dashboard.draw()
    before = (app.tab, app.mode, app.selected_id, deepcopy(app.job_panel_state["mode"]))
    app.click(y, x, hits, button="left")
    assert (app.tab, app.mode, app.selected_id, app.job_panel_state["mode"]) == before


@pytest.mark.parametrize("button", ["left", "press"])
@pytest.mark.parametrize("coordinate", ["left", "right", "top", "bottom"])
def test_activation_outside_the_terminal_cannot_select_a_same_height_row(dashboard, button, coordinate):
    app = dashboard.app
    _, hits = dashboard.draw()
    y = next(y for y, kind, value in hits if kind == "job" and value == "8")
    point = {"left": (y, -1), "right": (y, app.width),
             "top": (-1, 4), "bottom": (app.height, 4)}[coordinate]
    app.click(*point, hits, button=button)
    assert app.selected_id == "7" and app.tab == "jobs" and not app.marks
