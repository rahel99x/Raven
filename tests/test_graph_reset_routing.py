"""Graph context resets through real screen/controller routing without click-through."""
from types import SimpleNamespace

import pytest

from raven import chart_interaction as C, clock, layout as L, metric_live as M, screen
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store
from raven.views import Views


MOUSE = SimpleNamespace(BUTTON1_CLICKED=1, BUTTON1_PRESSED=2, BUTTON1_RELEASED=4,
    BUTTON1_DOUBLE_CLICKED=8, BUTTON3_CLICKED=16, BUTTON3_PRESSED=32,
    BUTTON4_PRESSED=64, BUTTON5_PRESSED=128, REPORT_MOUSE_POSITION=256, BUTTON_SHIFT=512)


@pytest.fixture
def dashboard(monkeypatch, tmp_path):
    monkeypatch.setattr(clock, "now", lambda: 200.0)
    cfg = Config({"animations": False, "startup": {"enabled": False}, "log_lines": 0,
                  "exports": {"projects_root": str(tmp_path)}})
    store = Store(persist=False)
    store.jobs = [Job(str(jid), "job-" + str(jid), "cpu", "RUNNING", cpus=4,
                      mem_req="8G", submit="submit", start="start") for jid in (7, 8)]
    store.finished = [Finished(str(jid), "done-" + str(jid), "FAILED") for jid in (17, 18, 19)]
    for job in store.jobs:
        for index in range(20):
            store.record(job.id, {"k": "live", "t": 180.0 + index,
                                  "cpu": (index + 1) / 25, "rss": (1 + index / 20) * 1024**3})
    app = App(store, None, None, cfg, "tester", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    app.selected_id, app.analytics_job, app.analytics_view = "7", "7", "job"

    def draw(tab="analytics", ascii_=False):
        app.tab = tab
        views.set_ascii(ascii_)
        if tab == "jobs":
            app.job_panel_state.update(mode="analytics", analytics_view="job")
        snap = store.snapshot()
        result = views.compose(snap, app, 190, 90)
        views.overlay(snap, app, 190, 90)
        return result

    def right(y, x, route="pressed"):
        if route == "app":
            app.click(y, x, app.last_hits, button="right")
        else:
            bits = MOUSE.BUTTON3_PRESSED if route == "pressed" else MOUSE.BUTTON3_CLICKED
            screen._apply_input(app, ("mouse", (0, x, y, 0, bits)), app.last_hits, MOUSE)

    def release(y, x):
        screen._apply_input(app, ("mouse", (0, x, y, 0, MOUSE.BUTTON1_RELEASED)), app.last_hits, MOUSE)

    yield SimpleNamespace(app=app, store=store, views=views, draw=draw, right=right, release=release)
    if app.research:
        app.research.close()


def _plots(d):
    return [plot for plot in C.initialize(d.app)["plots"] if plot.kind in ("metric", "metric-empty")]


def _point(plot):
    return (plot.visible.top + min(1, plot.visible.bottom - plot.visible.top - 1),
            plot.visible.left + min(2, plot.visible.right - plot.visible.left - 1))


def _zoom(d, plot):
    lo, hi = plot.x_bounds
    C._apply(d.app, plot, {"x": (lo + (hi - lo) / 4, lo + 3 * (hi - lo) / 4),
                          "y": plot.y_bounds, "fit_y": True})


@pytest.mark.parametrize("tab", ["jobs", "analytics"])
@pytest.mark.parametrize("route", ["app", "pressed", "clicked"])
@pytest.mark.parametrize("ascii_", [False, True])
def test_right_reset_is_metric_scoped_preserves_jobs_and_uses_only_published_geometry(dashboard, monkeypatch, tab, route, ascii_):
    d, app = dashboard, dashboard.app
    d.draw(tab, ascii_)
    plots = _plots(d)
    chosen = plots[0]
    other = next(plot for plot in plots if M.canonical(plot.key) != M.canonical(chosen.key))
    _zoom(d, chosen)
    _zoom(d, other)
    d.draw(tab, ascii_)
    chosen = next(plot for plot in _plots(d) if plot.key == chosen.key)
    app.marks = {"7", "8"}
    selected = app.selected_id
    other_bounds = C.bounds(app, other.key)
    monkeypatch.setattr(d.store, "snapshot", lambda *args: pytest.fail("input read a scheduler snapshot"))
    monkeypatch.setattr(d.views, "metric_curve", lambda *args, **kwargs: pytest.fail("input rendered source metrics"))
    if app.research:
        monkeypatch.setattr(app.research, "current", lambda *args, **kwargs: pytest.fail("input read source metrics"))
        monkeypatch.setattr(app.research, "request", lambda *args, **kwargs: pytest.fail("input requested source work"))
    d.right(*_point(chosen), route)
    assert C.bounds(app, chosen.key) is None
    assert C.bounds(app, other.key) == other_bounds
    assert app.selected_id == selected and app.marks == {"7", "8"}
    assert app.mode == "main" and not app.quit and not app.confirm


@pytest.mark.parametrize("route", ["app", "pressed", "clicked"])
def test_empty_cropped_graph_has_right_reset_recovery_without_left_capture(dashboard, route):
    d, app = dashboard, dashboard.app
    d.draw("jobs")
    chosen = _plots(d)[0]
    C._apply(app, chosen, {"x": (chosen.x_bounds[1] + 10, chosen.x_bounds[1] + 20),
                          "y": chosen.y_bounds, "fit_y": True})
    d.draw("jobs")
    empty = next(plot for plot in _plots(d) if plot.key == chosen.key)
    assert empty.kind == "metric-empty"
    assert not C.handle_mouse(app, *_point(empty), button="press")
    assert not C.active(app)
    selected = app.selected_id
    d.right(*_point(empty), route)
    assert C.bounds(app, chosen.key) is None and app.selected_id == selected
    d.draw("jobs")
    assert next(plot for plot in _plots(d) if plot.key == chosen.key).kind == "metric"


@pytest.mark.parametrize("tab", ["jobs", "analytics"])
def test_right_reset_ends_chart_capture_and_delayed_release_cannot_zoom_or_select(dashboard, tab):
    d, app = dashboard, dashboard.app
    d.draw(tab)
    chosen = _plots(d)[0]
    _zoom(d, chosen)
    d.draw(tab)
    chosen = next(plot for plot in _plots(d) if plot.key == chosen.key)
    assert C.handle_mouse(app, *_point(chosen), button="press")
    app.marks = {"7", "8"}
    selected = app.selected_id
    d.right(*_point(chosen))
    assert not C.active(app) and C.bounds(app, chosen.key) is None
    d.release(chosen.visible.bottom - 1, chosen.visible.right - 1)
    assert C.bounds(app, chosen.key) is None
    assert app.selected_id == selected and app.marks == {"7", "8"}


def test_jobs_divider_capture_is_cancelled_before_reset_and_delayed_release(dashboard):
    from raven import pane_drag
    d, app = dashboard, dashboard.app
    d.draw("jobs")
    chosen = _plots(d)[0]
    _zoom(d, chosen)
    d.draw("jobs")
    chosen = next(plot for plot in _plots(d) if plot.key == chosen.key)
    divider = next(value for value in pane_drag.initialize(app)["dividers"].values()
                   if value.axis == "vertical")
    assert pane_drag.handle_mouse(app, divider.y + divider.height // 2, divider.x, button="press")
    capture = pane_drag.initialize(app)["capture"]
    original = capture["original"]
    d.right(*_point(chosen))
    assert not pane_drag.active(app)
    assert C.bounds(app, chosen.key) is None
    d.release(chosen.visible.top, chosen.visible.right - 1)
    current = pane_drag.initialize(app)["dividers"][divider.key]
    assert current.value == original


def test_jobs_rate_slider_capture_ends_before_graph_reset_and_delayed_release(dashboard):
    from raven import toolbar
    d, app = dashboard, dashboard.app
    d.draw("jobs")
    chosen = _plots(d)[0]
    _zoom(d, chosen)
    d.draw("jobs")
    chosen = next(plot for plot in _plots(d) if plot.key == chosen.key)
    state = toolbar.initialize(app)
    y, left, right, _, _ = next(hit for hit in state["hits"] if hit[3] == "track")
    assert toolbar.handle_mouse(app, y, left, button="press")
    assert state["dragging"]
    rate = app.cfg["polling_multiplier"]
    d.right(*_point(chosen))
    assert not state["dragging"] and not state["pressed"]
    d.release(chosen.visible.top, chosen.visible.right - 1)
    assert app.cfg["polling_multiplier"] == rate


@pytest.mark.parametrize("route", ["app", "pressed", "clicked"])
def test_history_job_list_right_click_retains_export_menu_priority(dashboard, route):
    d, app = dashboard, dashboard.app
    d.draw("history")
    app.marks = {"17", "18"}
    y = next(y for y, kind, identifier in app.last_hits if kind == "fin" and identifier == "19")
    d.right(y, app.history_jobs_rect.left + 2, route)
    assert app.mode == "history_log_menu"
    assert app.history_log_export_state["jobs"] == ("17", "18")
    assert app.marks == {"17", "18"} and not app.confirm


def test_jobs_right_click_outside_graph_still_clears_without_activating_toolbar(dashboard):
    d, app = dashboard, dashboard.app
    d.draw("jobs")
    chosen = _plots(d)[0]
    _zoom(d, chosen)
    app.marks = {"7", "8"}
    d.right(0, 1)
    assert app.selected_id is None and not app.marks and not app.quit
    assert C.bounds(app, chosen.key) is not None


@pytest.mark.parametrize("tab", ["jobs", "analytics"])
@pytest.mark.parametrize("block", ["toolbar", "modal"])
def test_right_click_cannot_reset_graph_through_dropdown_or_other_modal(dashboard, tab, block):
    d, app = dashboard, dashboard.app
    d.draw(tab)
    chosen = _plots(d)[0]
    _zoom(d, chosen)
    d.draw(tab)
    chosen = next(plot for plot in _plots(d) if plot.key == chosen.key)
    before = C.bounds(app, chosen.key)
    if block == "toolbar":
        app.toolbar_state["menu"] = "View"
    else:
        app.mode = "history_log_menu"
        app.history_log_export_state.update(stage="menu", jobs=("17",))
    d.right(*_point(chosen))
    assert C.bounds(app, chosen.key) == before


def test_jobs_outside_graph_clear_cancels_divider_without_a_delayed_resize(dashboard):
    from raven import pane_drag
    d, app = dashboard, dashboard.app
    d.draw("jobs")
    divider = next(value for value in pane_drag.initialize(app)["dividers"].values()
                   if value.axis == "vertical")
    assert pane_drag.handle_mouse(app, divider.y + divider.height // 2, divider.x, button="press")
    capture = pane_drag.initialize(app)["capture"]
    setter, writes = capture["setter"], []

    def observe(value):
        writes.append(value)
        setter(value)

    capture["setter"] = observe
    app.marks = {"7", "8"}
    d.right(0, 1)
    retained = pane_drag.initialize(app)["capture"]
    committed = list(writes)
    d.release(divider.y + divider.height // 2, app.width - 2)
    assert retained is None, "Selection clearing retained an unfinished divider gesture"
    assert writes == committed, "A delayed release resized/saved the pane after selection clearing"
    assert app.selected_id is None and not app.marks and not app.quit


@pytest.mark.parametrize("tab", ["jobs", "history"])
def test_outside_graph_clear_cancels_update_slider_without_delayed_rate_change(dashboard, tab):
    from raven import toolbar
    d, app = dashboard, dashboard.app
    d.draw(tab)
    state = toolbar.initialize(app)
    y, left, right, _, _ = next(hit for hit in state["hits"] if hit[3] == "track")
    assert toolbar.handle_mouse(app, y, left, button="press")
    assert state["dragging"]
    app.marks = {"7", "8"} if tab == "jobs" else {"17", "18"}
    rate = app.cfg["polling_multiplier"]
    d.right(0, 1)
    retained = state["dragging"] or state["pressed"]
    d.release(y, right - 1)
    assert not retained, "Selection clearing retained an unfinished update-slider gesture"
    assert app.cfg["polling_multiplier"] == rate
    assert app.selected_id is None and not app.marks and not app.quit


@pytest.mark.parametrize("return_method", ["row-press", "row-click", "f6", "esc"])
def test_marked_jobs_arrows_resume_after_explicit_return_from_details(dashboard, return_method):
    d, app = dashboard, dashboard.app
    d.draw("jobs")

    def row(identifier):
        return next((y, 6) for y, kind, value in app.last_hits if kind == "job" and value == identifier)

    app.click(*row("7"), app.last_hits, button="press")
    app.click(*row("8"), app.last_hits, button="drag")
    app.click(*row("8"), app.last_hits, button="release")
    assert app.marks == {"7", "8"} and app.selected_id == "8"
    d.draw("jobs")
    y, _, (target, left, right) = next(hit for hit in app.last_hits
        if hit[1] == "job_panel_tab" and hit[2][0] == "analytics")
    app.click(y, left, app.last_hits, button="left")
    assert app.layout_state.focus == "details"
    d.draw("jobs")
    if return_method.startswith("row-"):
        app.click(*row("7"), app.last_hits,
                  button="press" if return_method == "row-press" else "left")
        if return_method == "row-press":
            app.click(*row("7"), app.last_hits, button="release")
        expected, direction = "8", "down"
    else:
        app.handle(return_method)
        expected, direction = "7", "up"
    assert app.layout_state.focus == "main"
    d.draw("jobs")
    mode = app.job_panel_state["mode"]
    app.handle(direction)
    assert app.selected_id == expected, "A stale Details/control focus stole Jobs row navigation"
    assert app.marks == {"7", "8"} and app.job_panel_state["mode"] == mode


@pytest.mark.parametrize("tab", ["jobs", "analytics"])
@pytest.mark.parametrize("distance", [1, 2, 3])
def test_graph_drag_tolerates_three_cells_beyond_axis_labels_without_changing_source(dashboard, tab, distance):
    d, app = dashboard, dashboard.app
    d.draw(tab)
    chosen = _plots(d)[0]
    start = (chosen.visible.top + 1, chosen.visible.left + 4)
    assert chosen.axes is not None and chosen.axes.left < chosen.visible.left
    endpoint = (chosen.visible.bottom - 1, chosen.axes.left - distance)
    assert C.capture_bounds(chosen).contains(*endpoint)
    selected = app.selected_id
    app.marks = {"7", "8"}
    app.click(*start, app.last_hits, button="press")
    assert C.active(app)
    screen._apply_input(app, ("mouse", (0, endpoint[1], endpoint[0], 0,
                         MOUSE.REPORT_MOUSE_POSITION | MOUSE.BUTTON1_PRESSED)), app.last_hits, MOUSE)
    assert C.active(app)
    capture = C.initialize(app)["capture"]
    assert capture["plot"].key == chosen.key
    assert capture["current"] == (endpoint[0], chosen.visible.left)
    assert all(chosen.visible.contains(y, x) for y, x, _ in C.feedback(app))
    d.release(*endpoint)
    assert not C.active(app)
    selected_bounds = C.bounds(app, chosen.key)
    assert selected_bounds is not None
    assert selected_bounds["x"][0] == chosen.x_bounds[0]
    assert selected_bounds["x"][1] < chosen.x_bounds[1]
    assert all(C.bounds(app, plot.key) is None for plot in _plots(d) if plot.key != chosen.key)
    assert app.selected_id == selected and app.marks == {"7", "8"}


@pytest.mark.parametrize("tab", ["jobs", "analytics"])
def test_graph_drag_beyond_buffer_cannot_resume_or_activate_delayed_release(dashboard, tab):
    d, app = dashboard, dashboard.app
    d.draw(tab)
    chosen = _plots(d)[0]
    selected = app.selected_id
    start = (chosen.visible.top + 1, chosen.visible.left + 4)
    app.marks = {"7", "8"}
    app.click(*start, app.last_hits, button="press")
    assert C.active(app)
    app.click(chosen.visible.bottom - 1, C.capture_bounds(chosen).left - 1, app.last_hits, button="drag")
    assert not C.active(app)
    app.click(*start, app.last_hits, button="motion")
    app.click(chosen.visible.bottom - 1, chosen.visible.right - 1, app.last_hits, button="release")
    assert C.bounds(app, chosen.key) is None and not C.active(app)
    assert app.selected_id == selected and app.marks == {"7", "8"}
    assert not app.confirm and not app.quit and app.mode == "main"
