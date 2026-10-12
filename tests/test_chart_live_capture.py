"""Sampler publications preserve chart gestures; genuine context changes do not."""
from types import SimpleNamespace

import pytest

from raven import chart_interaction as C, clock, layout as L, screen
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.research import ResearchHub
from raven.views import Views


MOUSE = SimpleNamespace(BUTTON1_CLICKED=1, BUTTON1_PRESSED=2, BUTTON1_RELEASED=4,
    BUTTON1_DOUBLE_CLICKED=8, BUTTON3_CLICKED=16, BUTTON3_PRESSED=32,
    BUTTON4_PRESSED=64, BUTTON5_PRESSED=128, REPORT_MOUSE_POSITION=256, BUTTON_SHIFT=512)


@pytest.fixture
def dashboard(monkeypatch):
    now = {"value": 200.0}
    monkeypatch.setattr(clock, "now", lambda: now["value"])
    cfg = Config({"animations": False, "startup": {"enabled": False}, "log_lines": 0})
    store = Store(persist=False)
    store.jobs = [Job(str(jid), "train-" + str(jid), "cpu", "RUNNING", cpus=4,
                      submit="submitted", start="first-attempt") for jid in (7, 8)]
    for job in store.jobs:
        for index in range(20):
            store.record(job.id, {"k": "live", "t": 180.0 + index,
                                  "cpu": (index + 1) / 25, "rss": (1 + index / 20) * 1024**3})
    app = App(store, None, None, cfg, "tester", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    app.research = ResearchHub(cfg)
    app.selected_id = app.analytics_job = "7"
    app.analytics_view = "job"

    def draw(tab="analytics", ascii_=False, width=190, height=90):
        app.tab = tab
        views.set_ascii(ascii_)
        if tab == "jobs":
            app.job_panel_state.update(mode="analytics", analytics_view="job")
        snap = store.snapshot()
        views.compose(snap, app, width, height)
        views.overlay(snap, app, width, height)

    def mouse(plot, button, *, x=None, y=None):
        x = plot.visible.left + 4 if x is None else x
        y = plot.visible.top + 1 if y is None else y
        bits = {"press": MOUSE.BUTTON1_PRESSED, "drag": MOUSE.REPORT_MOUSE_POSITION | MOUSE.BUTTON1_PRESSED,
                "release": MOUSE.BUTTON1_RELEASED}[button]
        screen._apply_input(app, ("mouse", (0, x, y, 0, bits)), app.last_hits, MOUSE)

    yield SimpleNamespace(app=app, store=store, views=views, now=now, draw=draw, mouse=mouse)
    app.research.close()


def _metric(d, metric):
    return next(plot for plot in C.initialize(d.app)["plots"]
                if plot.key[0] == "resource-series" and plot.key[2] == metric)


@pytest.mark.parametrize("tab", ["analytics", "jobs"])
@pytest.mark.parametrize("metric", ["cpu-rate", "resident-memory"])
@pytest.mark.parametrize("ascii_", [False, True])
def test_forty_sampler_publications_preserve_original_gesture_mapping(dashboard, monkeypatch, tab, metric, ascii_):
    d, app = dashboard, dashboard.app
    d.draw(tab, ascii_)
    original = _metric(d, metric)
    start_x = original.visible.left + 4
    end_x = original.visible.right - 5
    d.mouse(original, "press", x=start_x)
    assert C.active(app)
    app.marks = {"7", "8"}
    selected = app.selected_id
    for index in range(40):
        d.now["value"] = 200.0 + index
        d.store.record("7", {"k": "live", "t": d.now["value"], "cpu": .9,
                             "rss": (10 + index) * 1024**3})
        d.draw(tab, ascii_)
        current = _metric(d, metric)
        assert C.active(app), f"Sampler publication {index} cancelled a stationary drag"
        assert current.key == original.key
        assert current.rect == original.rect and current.visible == original.visible
        assert current.x_bounds == original.x_bounds and current.y_bounds == original.y_bounds
        d.mouse(original, "drag", x=end_x, y=original.visible.bottom - 1)
        assert C.active(app)
    # Final input must use the frozen plot metadata, not scan/copy the changing
    # Store, invoke the renderer, or ask the background report reader for data.
    monkeypatch.setattr(d.store, "snapshot", lambda: pytest.fail("gesture input took a scheduler snapshot"))
    monkeypatch.setattr(d.views, "metric_curve", lambda *args, **kwargs: pytest.fail("gesture input rendered metric data"))
    monkeypatch.setattr(app.research, "current", lambda *args, **kwargs: pytest.fail("gesture input read report data"))
    monkeypatch.setattr(app.research, "request", lambda *args, **kwargs: pytest.fail("gesture input requested report data"))
    with monkeypatch.context() as pointer_only:
        pointer_only.setattr("builtins.open", lambda *args, **kwargs: pytest.fail("gesture input opened a source file"))
        d.mouse(original, "release", x=end_x, y=original.visible.bottom - 1)
    assert not C.active(app)
    bounds = C.bounds(app, original.key)
    span = original.rect.right - original.rect.left - 1
    lo, hi = original.x_bounds
    expected = (lo + (hi - lo) * (start_x - original.rect.left) / span,
                lo + (hi - lo) * (end_x - original.rect.left) / span)
    assert bounds["x"] == pytest.approx(expected)
    assert app.selected_id == selected and app.marks == {"7", "8"}
    assert len(d.store.series["7"]) == 60


@pytest.mark.parametrize("tab", ["analytics", "jobs"])
@pytest.mark.parametrize("unrelated", ["reader-generation", "other-project-binding"])
def test_unrelated_reporting_changes_do_not_reidentify_scheduler_graph(dashboard, tab, unrelated):
    d, app = dashboard, dashboard.app
    if unrelated == "other-project-binding":
        # Keep the project ribbon present throughout. Its first appearance
        # changes the actual viewport and must still cancel a held gesture.
        app.project_state.update(root="/unrelated/project-before", binding={
            "job_id": "8", "attempt": -1, "run_id": "run-before",
            "project_root": "/unrelated/project-before"})
    d.draw(tab)
    original = _metric(d, "cpu-rate")
    d.mouse(original, "press")
    for index in range(10):
        if unrelated == "reader-generation":
            app.research.configure(metrics_file=f"/unrelated/project-{index}/metrics.jsonl")
        else:
            app.project_state.update(root=f"/unrelated/project-{index}", binding={
                "job_id": "8", "attempt": index, "run_id": f"run-{index}",
                "project_root": f"/unrelated/project-{index}"})
        d.draw(tab)
        current = _metric(d, "cpu-rate")
        assert current.key == original.key
        assert C.active(app), "An unrelated report source invalidated a scheduler-owned metric"
        d.mouse(original, "drag", x=original.visible.right - 5, y=original.visible.bottom - 1)
    d.mouse(original, "release", x=original.visible.right - 5, y=original.visible.bottom - 1)
    assert C.bounds(app, original.key) is not None and app.selected_id == "7"


@pytest.mark.parametrize("tab", ["analytics", "jobs"])
@pytest.mark.parametrize("change", ["selected-job", "attempt", "viewport"])
def test_real_context_changes_cancel_without_applying_stale_job_zoom(dashboard, tab, change):
    d, app = dashboard, dashboard.app
    d.draw(tab)
    original = _metric(d, "cpu-rate")
    d.mouse(original, "press")
    assert C.active(app)
    width = 190
    if change == "selected-job":
        if tab == "jobs":
            app.cursor["jobs"] = app.visible_ids.index("8")
        app.selected_id = app.analytics_job = "8"
    elif change == "attempt":
        d.store.jobs[0].start = "requeued-attempt"
    else:
        width = 180
    d.draw(tab, width=width)
    assert not C.active(app)
    d.mouse(original, "release", x=original.visible.right - 5, y=original.visible.bottom - 1)
    assert C.bounds(app, original.key) is None
    assert not C.initialize(app)["zoom"], "A stale release zoomed the current job/source"
    assert app.mode == "main" and not app.confirm and not app.quit


def test_new_project_ribbon_cancels_actual_jobs_chart_layout_change(dashboard):
    d, app = dashboard, dashboard.app
    d.draw("jobs")
    original = _metric(d, "cpu-rate")
    d.mouse(original, "press")
    assert C.active(app)
    app.project_state.update(root="/unrelated/project", binding={
        "job_id": "8", "attempt": 1, "run_id": "run-one",
        "project_root": "/unrelated/project"})
    d.draw("jobs")
    current = _metric(d, "cpu-rate")
    assert current.key == original.key
    assert current.rect != original.rect or current.viewport != original.viewport
    assert not C.active(app)
    d.mouse(original, "release", x=original.visible.right - 4)
    assert not C.initialize(app)["zoom"]


@pytest.mark.parametrize("tab", ["analytics", "jobs"])
@pytest.mark.parametrize("metric", ["cpu-rate", "resident-memory"])
@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width", [150, 190, 240])
def test_retained_native_samples_can_leave_frozen_window_without_cancelling_capture(dashboard, tab, metric, ascii_, width):
    d, app = dashboard, dashboard.app
    d.draw(tab, ascii_, width=width)
    original = _metric(d, metric)
    d.mouse(original, "press")
    assert C.active(app)
    # Simulate bounded Store retention replacing every observation in the
    # initial interval. The display must remain honest and recoverable without
    # giving the eventual release the new job/time coordinate system.
    d.store.series["7"].clear()
    d.now["value"] = 420.0
    for index in range(20):
        d.store.record("7", {"k": "live", "t": 400.0 + index, "cpu": .9,
                             "rss": 50 * 1024**3})
    d.draw(tab, ascii_, width=width)
    current = _metric(d, metric)
    assert C.active(app) and current.kind == "metric-empty"
    assert current.x_bounds == original.x_bounds and current.y_bounds == original.y_bounds
    assert current.rect == original.rect and current.visible == original.visible
    d.mouse(original, "release", x=original.visible.right - 4)
    assert not C.active(app) and C.bounds(app, original.key) is not None
    d.draw(tab, ascii_, width=width)
    assert _metric(d, metric).kind == "metric-empty"
    app.click(current.visible.top + 1, current.visible.left + 4, app.last_hits, button="right")
    d.draw(tab, ascii_, width=width)
    assert _metric(d, metric).kind == "metric"
    assert _metric(d, metric).x_bounds == (400.0, 410.0)
