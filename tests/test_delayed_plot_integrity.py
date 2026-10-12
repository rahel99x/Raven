"""Independent end-to-end checks of buffered display and real observed ink."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from raven import analysis_ui as A, chart_interaction as C, clock, layout as L, metric_live as M
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.native_series_cache import ObservationIndex
from raven.views import Views


@pytest.fixture
def plot_session(monkeypatch):
    timer = [102.25]
    monkeypatch.setattr(clock, "now", lambda: timer[0])
    cfg = Config({"animations": False, "startup": {"enabled": False}})
    store = Store(persist=False)
    job = Job("17", "buffered", "cpu", "RUNNING", submit="submitted", start="started")
    store.apply_jobs([job])
    app = App(store, None, None, cfg, "test", interactive=False)
    app.tab, app.selected_id, app.analytics_job = "analytics", "17", "17"
    app.width, app.height = 96, 30
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    value = SimpleNamespace(app=app, job=job, store=store, views=views, timer=timer,
                            timestamps=[80., 85., 90., 95., 100.],
                            values=[100., 200., 300., 400., 500.])
    try:
        yield value
    finally:
        if app.research:
            app.research.close()


def render(session, surface, *, running=True, filled=False, indexed=True):
    app, views = session.app, session.views
    C.begin_frame(app, app.width, app.height)
    if surface == "native":
        identity = C.key(app, "memory", "G", "17", scope="resource-series",
                         attempt="submitted|started")
        # Both filled companions and their parent use the same exact source.
        if filled:
            M.set_running(app, identity, running)
            identity = ("resource-area", *identity[1:])
        rows = views.metric_curve(
            app, session.values, app.width, 8, identity, running=running,
            filled=filled, title="memory", sample_times=session.timestamps,
            sample_interval=5., times=(session.timestamps[0], session.timestamps[-1]),
            observation_index=ObservationIndex(session.timestamps) if indexed else None)
    else:
        identity = A.chart_key(app, "memory", "/project/metrics.jsonl", interactive=False,
                               jid="17", job=session.job)
        controls, _ = M.controls(views.g, app, identity, app.width, running=running)
        metadata = {}
        points = [{"t": timestamp, "value": value} for timestamp, value in
                  zip(session.timestamps, session.values)]
        rows = controls + A.chart_rows(
            views.g, app, points, app.width, 8, "memory", "/project/metrics.jsonl",
            running=running, interactive=False, zoom_key=identity, metadata=metadata)
        C.record(app, identity, metadata, row=len(controls))
    C.publish(app, app.width, app.height)
    plots = C.initialize(app)["plots"]
    assert len(plots) <= 1
    return rows, plots[0] if plots else None, identity


def curve_columns(rows, plot):
    """Inspect measured ink, excluding the dim grid and axes."""
    columns = set()
    for y in range(plot.rect.top, plot.rect.bottom):
        x = 0
        for text, style in rows[y]:
            for char in text:
                size = L.vlen(char)
                if plot.rect.left <= x < plot.rect.right and style != "dim" and not char.isspace():
                    columns.add(x)
                x += size
    return columns


@pytest.mark.parametrize("surface", ["native", "reported"])
@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("indexed", [False, True])
def test_one_second_view_draws_complete_observed_segment_without_fake_measurements(
        plot_session, surface, ascii_, indexed):
    session = plot_session
    session.views.g = L.Glyphs(ascii_)
    original = deepcopy((session.timestamps, session.values))
    _, _, identity = render(session, surface, indexed=indexed)
    assert M.set_delta(session.app, identity, 1.)
    assert M.set_enabled(session.app, identity, True)
    rows, plot, _ = render(session, surface, indexed=indexed)
    a, b = plot.x_bounds
    assert b - a == pytest.approx(1.)
    assert session.timestamps[0] < a < b < session.timestamps[-1]
    assert not any(a <= timestamp <= b for timestamp in session.timestamps)
    assert curve_columns(rows, plot) == set(range(plot.rect.left, plot.rect.right))
    # Intersections describe the existing line, not newly observed samples.
    assert "last " not in L.row_text(rows[plot.rect.top - 1])
    assert (session.timestamps, session.values) == original


@pytest.mark.parametrize("surface", ["native", "reported"])
@pytest.mark.parametrize("unknown_index", [2, 3])
def test_buffer_never_connects_across_missing_endpoint(plot_session, surface, unknown_index):
    session = plot_session
    session.values[unknown_index] = None
    _, _, identity = render(session, surface)
    assert M.set_delta(session.app, identity, 1.)
    assert M.set_enabled(session.app, identity, True)
    rows, plot, _ = render(session, surface)
    assert 90. < plot.x_bounds[0] < plot.x_bounds[1] < 95.
    assert not curve_columns(rows, plot)


@pytest.mark.parametrize("surface", ["native", "reported"])
def test_automatic_history_delays_running_plot_and_completion_reveals_retained_end(plot_session, surface):
    session = plot_session
    _, before, identity = render(session, surface)
    assert not M.enabled(session.app, identity)
    assert before.x_bounds[0] == session.timestamps[0]
    assert before.x_bounds[0] < before.x_bounds[1] < session.timestamps[-1]
    session.job.state = "COMPLETED"
    rows, final, _ = render(session, surface, running=False)
    assert final.x_bounds == (session.timestamps[0], session.timestamps[-1])
    assert not M.enabled(session.app, identity)
    assert final.rect.right - 1 in curve_columns(rows, final)


@pytest.mark.parametrize("surface", ["native", "reported"])
def test_future_timestamp_cannot_change_current_display_or_y_range(plot_session, surface):
    session = plot_session
    before_rows, before, _ = render(session, surface)
    session.timestamps.append(200.)
    session.values.append(1e20)
    after_rows, after, _ = render(session, surface)
    assert before.x_bounds == after.x_bounds
    assert before.y_bounds == after.y_bounds
    assert curve_columns(before_rows, before) == curve_columns(after_rows, after)
    assert session.timestamps[-1] == 200. and session.values[-1] == 1e20


@pytest.mark.parametrize("surface", ["native", "reported"])
def test_publication_during_mouse_drag_preserves_painted_axes_and_route(plot_session, surface):
    session = plot_session
    _, before, identity = render(session, surface)
    y, x = before.rect.top + 2, before.rect.left + 3
    assert C.handle_mouse(session.app, y, x, button="press")
    assert C.active(session.app)
    session.timer[0] += 5.
    session.timestamps.append(105.)
    session.values.append(10000.)
    _, during, _ = render(session, surface)
    assert during.x_bounds == before.x_bounds
    assert during.y_bounds == before.y_bounds
    for column in range(x, min(before.rect.right - 1, x + 15)):
        assert C.handle_mouse(session.app, y, column, button="drag")
        assert C.active(session.app)
        assert session.app.tab == "analytics"
    assert C.handle_mouse(session.app, y, x + 12, button="release")
    assert not C.active(session.app)
    assert C.bounds(session.app, identity) is not None
    assert session.app.tab == "analytics"


def test_filled_companion_has_same_delayed_right_edge_as_curve(plot_session):
    session = plot_session
    _, _, identity = render(session, "native")
    assert M.set_delta(session.app, identity, 1.)
    assert M.set_enabled(session.app, identity, True)
    _, curve, _ = render(session, "native")
    rows, area, _ = render(session, "native", filled=True)
    assert area.x_bounds == curve.x_bounds
    assert area.rect.right - 1 in curve_columns(rows, area)


@pytest.mark.parametrize("surface", ["native", "reported"])
def test_future_only_inventory_never_paints_unacquired_values(plot_session, surface):
    session = plot_session
    session.timestamps = [200., 205., 210.]
    session.values = [100., 200., 300.]
    rows, plot, _ = render(session, surface)
    assert plot is None or not curve_columns(rows, plot)
    assert "last 300" not in L.to_text(rows, session.app.width)


def test_keyboard_sample_selection_uses_painted_delayed_frame_without_clock_or_source_work(
        plot_session, monkeypatch):
    session = plot_session
    app, source = session.app, "/project/metrics.jsonl"
    app.mode = "analysis"
    state = A.initialize(app)
    state.update(modal="chart", chart_job="17", metric="memory", zoom=1., pan=0.)
    points = [{"t": timestamp, "value": value} for timestamp, value in
              zip(session.timestamps, session.values)]
    series = {"memory": points}
    state["chart_frame"] = {"series": series, "source": source, "job": "17",
                            "record": session.job,
                            "generation": getattr(app.research, "generation", None),
                            "result": id(getattr(app, "analysis_result", None))}
    identity = A.chart_key(app, "memory", source, jid="17", job=session.job)
    M.set_running(app, identity, True)
    metadata = {}
    A.chart_rows(session.views.g, app, points, 96, 8, "memory", source,
                 interactive=True, zoom_key=identity, running=True, metadata=metadata)
    painted = tuple(state["chart_visible"]["points"])
    assert painted and painted[-1]["t"] < points[-1]["t"]
    session.timer[0] += 20.
    monkeypatch.setattr(M, "display_end", lambda *args, **kwargs: pytest.fail("input advanced playback"))
    monkeypatch.setattr(app.store, "snapshot", lambda: pytest.fail("input requested a snapshot"))
    assert A.handle_key(app, "end")
    assert state["cursor"] == len(painted) - 1
    assert tuple(state["chart_visible"]["points"]) == painted
    assert A.handle_key(app, "up")
    assert state["cursor"] == max(0, len(painted) - 2)
    assert app.mode == "analysis" and app.tab == "analytics"
