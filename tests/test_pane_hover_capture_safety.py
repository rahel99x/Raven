"""Pointer traffic cannot activate stale Details actions or rescan live controls."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from raven import chart_interaction as C, interaction as I, job_panels as J
from raven import job_selection as S, layout as L, metric_live as M, metric_sampling
from raven import screen, workspace_layout as W
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views


MOUSE = SimpleNamespace(
    BUTTON1_CLICKED=1, BUTTON1_PRESSED=2, BUTTON1_RELEASED=4,
    BUTTON1_DOUBLE_CLICKED=8, BUTTON3_CLICKED=16, BUTTON3_PRESSED=32,
    BUTTON4_PRESSED=64, BUTTON5_PRESSED=128, REPORT_MOUSE_POSITION=256,
    BUTTON_SHIFT=512,
)


@pytest.fixture
def dashboard():
    cfg = Config({"animations": False, "startup": {"enabled": False}, "log_lines": 0})
    store = Store(persist=False)
    store.jobs = [Job(str(jid), "train-" + str(jid), "cpu", "RUNNING", cpus=4,
                      submit="submitted", start="attempt-1") for jid in (7, 8)]
    for job in store.jobs:
        for index in range(20):
            store.record(job.id, {"k": "live", "t": 180.0 + index,
                                  "cpu": (index + 1) / 25, "rss": 1024 ** 3})
    app = App(store, None, None, cfg, "tester", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    app.selected_id = app.analytics_job = "7"
    app.analytics_view = "job"
    app.job_panel_state.update(mode="analytics", analytics_view="job")

    def draw(tab="jobs", ascii_=False):
        app.tab = tab
        views.set_ascii(ascii_)
        snap = store.snapshot()
        return views.compose(snap, app, 190, 90)

    draw()
    yield SimpleNamespace(app=app, store=store, views=views, draw=draw)
    if app.research:
        app.research.close()


@pytest.mark.parametrize("tab", ["jobs", "analytics"])
@pytest.mark.parametrize("ascii_", [False, True])
def test_repeated_job_graph_hover_keeps_page_job_and_controls(dashboard, monkeypatch, tab, ascii_):
    d, app = dashboard, dashboard.app
    d.draw(tab, ascii_)
    app.marks = {"7", "8"}
    controls = M.initialize(app)["records"]
    assert controls
    plots = C.initialize(app)["plots"]
    plot = next(value for value in plots if value.key[0] == "resource-series")
    graph = I.initialize(app)["graph"]
    row = next((value for value in graph.controls if value.group in ("job", "job-history")),
               next(value for value in graph.controls if value.group == "metric-live"))
    points = [(row.rect.top, row.rect.left),
              (plot.visible.top, plot.visible.left),
              (plot.visible.bottom - 1, plot.visible.right - 1)]
    before = (app.tab, app.selected_id, app.job_panel_state["mode"],
              M.initialize(app)["revision"])
    monkeypatch.setattr(d.store, "snapshot", lambda: pytest.fail("hover copied scheduler data"))
    monkeypatch.setattr(app, "run_command", lambda *_: pytest.fail("hover ran a command"))
    for _ in range(50):
        for y, x in points:
            screen._apply_input(app, ("mouse", (0, x, y, 0, MOUSE.REPORT_MOUSE_POSITION)),
                                app.last_hits, MOUSE)
    assert (app.tab, app.selected_id, app.job_panel_state["mode"],
            M.initialize(app)["revision"]) == before
    assert app.marks == {"7", "8"}
    assert not M.active(app) and not S.active(app)


@pytest.mark.parametrize("kind", ["research_metric", "research_evidence", "control"])
def test_previous_job_details_actions_are_rejected_before_next_frame(dashboard, monkeypatch, kind):
    app = dashboard.app
    state = J.initialize(app)
    state.update(job="7", mode="analytics")
    proxy = SimpleNamespace(selected_id="7", research_evidence={"E1": {"path": "/old/log"}},
                            run_command=lambda *_: pytest.fail("stale Details ran a command"))
    state["view_states"]["analytics:job"] = {"proxy": proxy}
    app.selected_id = "8"
    monkeypatch.setattr("raven.analysis_ui.run_command", lambda *_: pytest.fail("stale metric opened"))
    monkeypatch.setattr("raven.log_workbench.open_citation", lambda *_: pytest.fail("stale citation opened"))
    value = {"action": ("command", "workspace experiment")} if kind == "control" else "E1"
    assert not J._content_action(app, (kind, value))
    assert app.tab == "jobs" and app.selected_id == "8"


@pytest.mark.parametrize("payload", [None, {}, "x", (), ("research",),
                                     ("research", 0, 3, 4), ("research", None, 3)])
def test_malformed_details_hit_cannot_raise_or_activate(dashboard, monkeypatch, payload):
    app = dashboard.app
    rect = app.job_panel_rect
    app.last_hits = [(rect.y, "job_panel_tab", payload)]
    monkeypatch.setattr(J, "_activate", lambda *_: pytest.fail("invalid Details activated"))
    J.handle_mouse(app, rect.y, rect.x, button="left")


def test_old_details_hit_outside_current_pane_does_not_activate(dashboard, monkeypatch):
    app = dashboard.app
    rect = app.job_panel_rect
    app.last_hits = [(rect.y, "job_panel_tab", ("research", rect.x, rect.x + 10))]
    app.job_panel_rect = W.Rect(rect.x + 12, rect.y, rect.width - 12, rect.height)
    monkeypatch.setattr(J, "_activate", lambda *_: pytest.fail("old hit activated under Main"))
    assert not J.handle_mouse(app, rect.y, rect.x, button="left")


@pytest.mark.parametrize("target", [None, {}, (), "analytics", 10])
def test_malformed_details_view_cannot_crash_or_activate(dashboard, monkeypatch, target):
    app = dashboard.app
    rect = app.job_panel_rect
    app.last_hits = [(rect.y, "job_panel_view", (target, rect.x, rect.x + 5))]
    monkeypatch.setattr(J, "_activate", lambda *_: pytest.fail("invalid Details view activated"))
    assert J.handle_mouse(app, rect.y, rect.x, button="left")


@pytest.mark.parametrize("y,x", [(None, 2), (2, None), (True, 2), (2, False),
                                 (-1, 2), (2, -1), (10000, 2), (2, 10000)])
def test_invalid_job_coordinates_cannot_select_stale_published_row(dashboard, y, x):
    app = dashboard.app
    app.last_hits = [(y, "job", "8")]
    before = app.selected_id
    assert not S.handle_mouse(app, y, x, button="press")
    assert not J.handle_mouse(app, y, x, button="left")
    assert app.selected_id == before and not S.active(app)


@pytest.mark.parametrize("button", ["motion", "drag", "release", "wheel-up", "wheel-down"])
def test_uncaptured_metric_pointer_reports_do_not_revalidate_other_metrics(dashboard, monkeypatch, button):
    app = dashboard.app
    state = M.initialize(app)
    control = state["records"][0]
    state["focus"] = control.token
    before = state["revision"]
    monkeypatch.setattr(M, "tick", lambda *_: pytest.fail("inert report rescanned live metrics"))
    assert not M.handle_mouse(app, control.slider.top, control.slider.left, button=button)
    assert state["revision"] == before and state["focus"] == control.token


def test_cancelled_slider_release_is_consumed_once(dashboard):
    app = dashboard.app
    control = M.initialize(app)["records"][0]
    assert M.handle_mouse(app, control.slider.top, control.slider.left, button="press")
    assert M.cancel(app)
    assert M.handle_mouse(app, control.slider.top, control.slider.left, button="release")
    assert not M.handle_mouse(app, control.slider.top, control.slider.left, button="release")
    assert app.tab == "jobs"


def test_slider_context_change_cancels_capture_without_applying_release(dashboard):
    app = dashboard.app
    control = M.initialize(app)["records"][0]
    assert M.handle_mouse(app, control.slider.top, control.slider.right - 1, button="press")
    app.tab = "research"
    assert M.handle_mouse(app, control.slider.top, control.slider.left, button="release")
    entry = M.initialize(app)["entries"][control.key]
    assert entry["delta"] == M.MAX_DELTA
    assert not M.active(app)


def test_metric_feedback_reuses_rows_and_tracks_shared_cadence_geometry_and_units(dashboard, monkeypatch):
    app = dashboard.app
    state = M.initialize(app)
    control = state["records"][0]
    state["records"] = (control,)
    state.pop("feedback_rows", None)
    polls = {"value": 5.0}
    calls = []
    original = M._row

    def row(*args, **kwargs):
        calls.append(kwargs["poll_interval"])
        return original(*args, **kwargs)

    monkeypatch.setattr(M, "_row", row)
    monkeypatch.setattr(metric_sampling, "cadence", lambda *_: polls["value"])
    first = M.feedback(app, L.Glyphs(False))
    for _ in range(20):
        assert M.feedback(app, L.Glyphs(False)) == first
    assert calls == [5.0]
    # Another metric sharing this source can change its effective interval
    # without modifying this metric's own slider or revision.
    polls["value"] = .5
    changed = M.feedback(app, L.Glyphs(False))
    assert changed != first and calls == [5.0, .5]
    M.feedback(app, L.Glyphs(True))
    assert len(calls) == 3
    moved = replace(control, visible=I.Rect(control.visible.top, control.visible.left + 1,
                                           control.visible.bottom, control.visible.right))
    state["records"] = (moved,)
    M.feedback(app, L.Glyphs(True))
    assert len(calls) == 4 and len(state["feedback_rows"]) == 1
    assert M.set_delta(app, control.key, 1)
    M.feedback(app, L.Glyphs(True))
    assert len(calls) == 5


def test_feedback_cache_is_bounded_to_published_records(dashboard):
    app = dashboard.app
    M.feedback(app, L.Glyphs(False))
    state = M.initialize(app)
    assert len(state["feedback_rows"]) <= len(state["records"]) <= M.MAX_METRICS
    state["records"] = ()
    assert M.feedback(app, L.Glyphs(False)) == []
    assert state["feedback_rows"] == {}
