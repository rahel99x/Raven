"""Mouse boxes cover the measured cells in final native and nested layouts."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from raven import chart_interaction as C, clock, layout as L, metric_live, screen, table_ui, workspace_layout
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views


@pytest.fixture
def dashboard():
    cfg = Config({"animations": False, "log_lines": 0})
    store = Store(persist=False)
    store.jobs = [Job(str(jid), "train-" + str(jid), "cpu", "RUNNING", cpus=4,
                      mem_req="8G", submit="2026-10-08T00:00:00") for jid in (7, 8)]
    for job in store.jobs:
        for index in range(20):
            store.record(job.id, dict(k="live", t=1000 + index * 10,
                                     cpu=index / 25, rss=(1 + index / 20) * 1024 ** 3))
    app = App(store, None, None, cfg, "test", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    app.selected_id = app.analytics_job = "7"
    yield SimpleNamespace(app=app, views=views, store=store)
    if app.research:
        app.research.close()


def draw(dashboard, width=160, height=52):
    return dashboard.views.compose(dashboard.store.snapshot(), dashboard.app, width, height)


@pytest.mark.parametrize("tab", ["analytics", "jobs"])
@pytest.mark.parametrize("density", ["compact", "comfortable"])
@pytest.mark.parametrize("ascii_", [False, True])
def test_native_and_inline_graphs_publish_actual_axis_cells(dashboard, tab, density, ascii_):
    app, views = dashboard.app, dashboard.views
    app.tab = tab
    views.set_ascii(ascii_)
    workspace_layout.initialize(app).density = density
    app.job_panel_state["mode"] = "analytics"
    rows, _ = draw(dashboard)
    plots = C.initialize(app)["plots"]
    assert plots and all(plot.key[1] == "7" for plot in plots)
    for plot in plots:
        for y in range(plot.visible.top, plot.visible.bottom):
            text = L.row_text(rows[y])
            assert text[plot.visible.left - 1] == ("|" if ascii_ else "│")
        assert plot.visible.bottom < app.height
        if tab == "jobs":
            assert plot.visible.left >= app.job_panel_rect.x


@pytest.mark.parametrize("dock", ["left", "right", "top", "bottom", "off"])
def test_docked_graph_drag_zooms_exact_source_and_remains_inside_data(dashboard, dock):
    app = dashboard.app
    app.tab = "analytics"
    app.run_command("history-dock " + dock)
    rows, hits = draw(dashboard, 200, 62)
    plot = next(plot for plot in C.initialize(app)["plots"] if plot.key[0] == "resource-series")
    y, x = plot.visible.top + 1, plot.visible.left + 4
    app.click(y, x, hits, button="press", shift=True)
    assert C.active(app)
    app.click(y + 3, x + 12, hits, button="drag")
    app.click(y + 3, x + 12, hits, button="release")
    zoom = C.bounds(app, plot.key)
    assert zoom and plot.x_bounds[0] < zoom["x"][0] < zoom["x"][1] < plot.x_bounds[1]
    assert app.marks == set() and app.analytics_job == "7"
    draw(dashboard, 200, 62)
    current = next(value for value in C.initialize(app)["plots"] if value.key == plot.key)
    assert current.x_bounds == zoom["x"] and current.y_bounds == zoom["y"]
    assert C.undo(app, plot.key)
    draw(dashboard, 200, 62)
    restored = next(value for value in C.initialize(app)["plots"] if value.key == plot.key)
    assert restored.x_bounds == plot.x_bounds and restored.y_bounds == plot.y_bounds


def test_scrolled_details_graph_rows_and_capture_follow_current_viewport(dashboard):
    app = dashboard.app
    app.job_panel_state["mode"] = "analytics"
    app.layout_state.scroll["jobs:details"] = 8
    rows, hits = draw(dashboard)
    plots = C.initialize(app)["plots"]
    assert plots
    for plot in plots:
        assert L.row_text(rows[plot.visible.top])[plot.visible.left - 1] == "│"
    plot = next(plot for plot in plots if plot.visible.bottom - plot.visible.top >= 3)
    y, x = plot.visible.top + 1, plot.visible.left + 4
    app.click(y, x, hits, button="press")
    app.layout_state.scroll["jobs:details"] += 3
    draw(dashboard)
    assert not C.active(app)


@pytest.mark.parametrize("tab", ["analytics", "jobs"])
@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("edge", ["top", "bottom", "left", "right", "top-left", "top-right", "bottom-left", "bottom-right"])
def test_native_and_inline_captured_buffer_clamps_every_edge_without_changing_job(dashboard, tab, ascii_, edge):
    app, views = dashboard.app, dashboard.views
    app.tab = tab
    app.job_panel_state["mode"] = "analytics"
    views.set_ascii(ascii_)
    _, hits = draw(dashboard, 180, 70)
    plot = next(value for value in C.initialize(app)["plots"] if value.key[0] == "resource-series")
    margin = C.capture_bounds(plot)
    top, left, bottom, right = plot.visible.top, plot.visible.left, plot.visible.bottom, plot.visible.right
    y, x = top + (bottom - top) // 2, left + (right - left) // 2
    end_y = margin.top if "top" in edge else margin.bottom - 1 if "bottom" in edge else min(bottom - 1, y + 1)
    end_x = margin.left if "left" in edge else margin.right - 1 if "right" in edge else min(right - 1, x + 5)
    selected, marks = app.selected_id, set(app.marks)
    app.click(y, x, hits, button="press", shift=True)
    assert C.active(app)
    app.click(end_y, end_x, hits, button="drag")
    assert C.active(app)
    current = C.initialize(app)["capture"]["current"]
    assert plot.visible.contains(*current)
    app.click(end_y, end_x, hits, button="release")
    assert not C.active(app) and C.bounds(app, plot.key) is not None
    assert app.selected_id == selected and app.marks == marks


def test_drag_feedback_does_not_recompose_cached_graph(dashboard, monkeypatch):
    app, views, store = dashboard.app, dashboard.views, dashboard.store
    app.tab = "analytics"
    cache = screen._FrameCache()
    cache.rebuild(app, views, store, None, 160, 52)
    plot = next(plot for plot in C.initialize(app)["plots"] if plot.key[0] == "resource-series")
    y, x = plot.visible.top + 1, plot.visible.left + 4
    app.click(y, x, cache.hits, button="press")
    forbidden = Mock(side_effect=AssertionError("Cosmetic drag recomposed the graph"))
    monkeypatch.setattr(views, "compose", forbidden)
    mouse = SimpleNamespace(REPORT_MOUSE_POSITION=256, BUTTON1_PRESSED=2,
                            BUTTON1_RELEASED=4, BUTTON4_PRESSED=64, BUTTON5_PRESSED=128)
    for index in range(500):
        effects = screen._InputEffects()
        effects.record(app, ("mouse", (0, x + index % 10, y + index % 3, 0, 258)), mouse)
        assert not effects.document
        C.handle_mouse(app, y + index % 3, x + index % 10, button="drag")
        _, overlays, _ = cache.feedback(app, views)
        assert overlays
    forbidden.assert_not_called()


def test_legacy_job_columns_migrate_once_and_keep_fixed_progress_width(dashboard):
    app = dashboard.app
    table_ui.restore(app, {"order": {"jobs": ["id", "name", "progress", "part"]},
                           "widths": {"jobs": {"progress": 18, "name": 15}}})
    assert app.table_state["order"]["jobs"][:3] == ["progress", "id", "name"]
    columns = table_ui.columns(app, "jobs", table_ui.definitions("jobs"))
    assert columns[0].lo == columns[0].hi == 6
    app.run_command("columns jobs order id name progress part")
    saved = table_ui.save(app)
    table_ui.initialize(app)
    table_ui.restore(app, saved)
    assert app.table_state["order"]["jobs"][:3] == ["id", "name", "progress"]
    app.run_command("columns jobs width progress 12")
    assert not app.command_ok and "six" in app.message


def test_narrow_inline_comparison_coordinates_follow_replaced_summary_cards(dashboard):
    from raven import job_panels
    app, views, store = dashboard.app, dashboard.views, dashboard.store
    app.compare_ids = ["7", "8"]
    state = job_panels.initialize(app)
    state.update(mode="analytics", analytics_view="compare")
    C.begin_frame(app, 80, 120)
    rows, _ = job_panels._analytics(views, store.snapshot(), app, store.jobs[0], 80, 120, state)
    plots = [plot for plot in C.initialize(app)["pending"] if plot is not None]
    assert plots
    for plot in plots:
        assert L.row_text(rows[plot.rect.top])[plot.rect.left - 1] == "│"


def test_inline_analysis_preserves_explicit_axis_preferences(dashboard):
    from raven import job_panels
    app = dashboard.app
    app.analysis_state["axes"] = {"loss": {"mode": "fixed", "low": 1, "high": 9}}
    copied = job_panels._analysis_settings(app)
    assert copied["axes"] == app.analysis_state["axes"]
    copied["axes"]["loss"]["low"] = -4
    assert app.analysis_state["axes"]["loss"]["low"] == 1


def test_resource_keys_survive_glyph_switch_but_change_for_requeued_attempt(dashboard):
    app, views, store = dashboard.app, dashboard.views, dashboard.store
    app.tab = "analytics"
    draw(dashboard)
    first = {plot.key for plot in C.initialize(app)["plots"] if plot.key[0] == "resource-series"}
    views.set_ascii(True)
    draw(dashboard)
    assert {plot.key for plot in C.initialize(app)["plots"] if plot.key[0] == "resource-series"} == first
    store.jobs[0].start = "2026-10-08T02:00:00"
    draw(dashboard)
    assert first.isdisjoint({plot.key for plot in C.initialize(app)["plots"]})


@pytest.mark.parametrize("tab", ["analytics", "jobs"])
@pytest.mark.parametrize("ascii_", [False, True])
def test_live_controls_follow_exact_job_in_native_and_inline_series(dashboard, monkeypatch, tab, ascii_):
    app, views = dashboard.app, dashboard.views
    app.tab = tab
    views.set_ascii(ascii_)
    app.job_panel_state["mode"] = "analytics"
    monkeypatch.setattr(clock, "now", lambda: 1191.0)
    rows, hits = draw(dashboard, 180, 70)
    records = metric_live.initialize(app)["records"]
    assert records and all(control.key[1] == "7" for control in records)
    assert all(L.row_text(rows[c.toggle.top])[c.toggle.left:c.toggle.right] == "[Live off]" for c in records)
    target = next(control for control in records if control.key[2] == "cpu-rate")
    app.click(target.toggle.top, target.toggle.left + 1, hits, button="press")
    rows, hits = draw(dashboard, 180, 70)
    assert metric_live.enabled(app, target.key)
    plot = next(plot for plot in C.initialize(app)["plots"] if plot.key == target.key)
    # Ten-second observations retain two producer intervals for presentation.
    assert plot.x_bounds == (1141.0, 1171.0)
    assert "Source age" in "\n".join(map(L.row_text, rows))
    assert all(control.key[1] == "7" for control in metric_live.initialize(app)["records"])
    # A window is source scoped. Turning CPU Live on leaves memory unchanged.
    assert not any(metric_live.enabled(app, c.key) for c in records if c.key[2] != "cpu-rate")


@pytest.mark.parametrize("tab", ["analytics", "jobs"])
def test_one_second_live_window_stays_active_when_samples_are_absent(dashboard, monkeypatch, tab):
    app, views, store = dashboard.app, dashboard.views, dashboard.store
    app.tab = tab
    app.job_panel_state["mode"] = "analytics"
    monkeypatch.setattr(clock, "now", lambda: 1192.0)
    # A genuine unknown measurement keeps this buffered interval empty. A
    # simple interval between two valid samples now shows the acquired line.
    store.series["7"][17]["cpu"] = None
    draw(dashboard, 180, 70)
    control = next(c for c in metric_live.initialize(app)["records"] if c.key[2] == "cpu-rate")
    full = next(plot for plot in C.initialize(app)["plots"] if plot.key == control.key)
    assert metric_live.set_enabled(app, control.key, True)
    assert metric_live.set_delta(app, control.key, 1.0)
    original = list(store.series_of("7"))
    rows, hits = draw(dashboard, 180, 70)
    assert metric_live.window(app, control.key) == (1171.0, 1172.0)
    assert metric_live.document_interval(app) == .1
    text = "\n".join(map(L.row_text, rows))
    assert "1s" in text and "no observations in live window" in text
    empty = next(plot for plot in C.initialize(app)["plots"] if plot.key == control.key)
    assert empty.kind == "metric-empty" and empty.x_bounds == (1171.0, 1172.0)
    y, x = empty.visible.top, empty.visible.left
    assert not C.hover(app, y, x) and C.feedback(app) == []
    assert not C.handle_mouse(app, y, x, button="press") and not C.active(app)
    assert list(store.series_of("7")) == original
    selected, marks = app.selected_id, set(app.marks)
    app.click(y, x, hits, button="right")
    assert not metric_live.enabled(app, control.key)
    assert app.selected_id == selected and app.marks == marks
    draw(dashboard, 180, 70)
    restored = next(plot for plot in C.initialize(app)["plots"] if plot.key == control.key)
    assert restored.kind == "metric"
    assert restored.x_bounds == full.x_bounds and restored.y_bounds == full.y_bounds
    assert list(store.series_of("7")) == original


@pytest.mark.parametrize("tab", ["analytics", "jobs"])
def test_one_second_live_window_renders_a_genuine_recent_sample(dashboard, monkeypatch, tab):
    app = dashboard.app
    app.tab = tab
    app.job_panel_state["mode"] = "analytics"
    monkeypatch.setattr(clock, "now", lambda: 1190.5)
    draw(dashboard, 180, 70)
    control = next(c for c in metric_live.initialize(app)["records"] if c.key[2] == "cpu-rate")
    assert metric_live.set_enabled(app, control.key, True)
    assert metric_live.set_delta(app, control.key, 1.0)
    original = list(dashboard.store.series_of("7"))
    rows, _ = draw(dashboard, 180, 70)
    plot = next(plot for plot in C.initialize(app)["plots"] if plot.key == control.key)
    assert plot.kind == "metric" and plot.x_bounds == (1169.5, 1170.5)
    assert "no observations in live window" not in "\n".join(map(L.row_text, rows))
    assert list(dashboard.store.series_of("7")) == original


def test_live_slider_motion_is_cosmetic_and_redraw_deadline_is_bounded(dashboard, monkeypatch):
    app, views, store = dashboard.app, dashboard.views, dashboard.store
    app.tab = "analytics"
    monkeypatch.setattr(clock, "now", lambda: 1191.0)
    # Input handling never paints a document. A separate scheduler may preview
    # the latest accepted slider value after 50 ms, independent of the source
    # publication deadline. Freeze time to inspect both sides of that deadline.
    monkeypatch.setattr(screen.time, "monotonic", lambda: 1000.0)
    cache = screen._FrameCache()
    cache.rebuild(app, views, store, None, 180, 70)
    control = next(c for c in metric_live.initialize(app)["records"] if c.key[2] == "cpu-rate")
    metric_live.set_enabled(app, control.key, True)
    cache.rebuild(app, views, store, None, 180, 70)
    control = next(c for c in metric_live.initialize(app)["records"] if c.key[2] == "cpu-rate")
    app.click(control.slider.top, control.slider.left, cache.hits, button="press")
    cache.rebuild(app, views, store, None, 180, 70)
    assert metric_live.active(app)
    painter = Mock(side_effect=AssertionError("Live slider rerasterized on motion"))
    monkeypatch.setattr(views, "compose", painter)
    mouse = SimpleNamespace(REPORT_MOUSE_POSITION=256, BUTTON1_PRESSED=2,
                            BUTTON1_RELEASED=4, BUTTON4_PRESSED=64, BUTTON5_PRESSED=128)
    for index in range(200):
        x = control.slider.left + index % (control.slider.right - control.slider.left)
        effects = screen._InputEffects()
        effects.record(app, ("mouse", (0, x, control.slider.top, 0, 258)), mouse)
        assert not effects.document
        app.click(control.slider.top, x, cache.hits, button="drag")
        _, overlays, _ = cache.feedback(app, views)
        assert overlays
    assert cache.next_live != float("inf")
    assert cache.preview_ready == pytest.approx(1000.05)
    assert cache.preview_ready < cache.next_live
    assert not cache.due(app, 180, 70, now=cache.preview_ready - .001)
    assert cache.next_preview == cache.preview_ready
    assert cache.due(app, 180, 70, now=cache.preview_ready)
    assert cache.due(app, 180, 70, now=cache.next_live)
    painter.assert_not_called()


def test_live_controls_disappear_when_selected_job_finishes(dashboard, monkeypatch):
    app, store = dashboard.app, dashboard.store
    app.tab = "analytics"
    monkeypatch.setattr(clock, "now", lambda: 1191.0)
    draw(dashboard)
    control = metric_live.initialize(app)["records"][0]
    metric_live.set_enabled(app, control.key, True)
    store.jobs[0].state = "COMPLETED"
    rows, _ = draw(dashboard)
    assert not metric_live.initialize(app)["records"]
    assert not metric_live.enabled(app, control.key)
    assert metric_live.document_interval(app) is None
    assert "[Live" not in "\n".join(map(L.row_text, rows))


@pytest.mark.parametrize("delta", [1.0, 30.0])
def test_native_live_curve_cannot_use_future_observations_to_fill_current_window(dashboard, monkeypatch, delta):
    from raven import charts
    app, views = dashboard.app, dashboard.views
    monkeypatch.setattr(clock, "now", lambda: 1191.0)
    identity = C.key(app, "cpu-rate", "%", "7", scope="resource-series", attempt="2026-10-08T00:00:00|")
    metric_live.set_running(app, identity, True)
    metric_live.set_enabled(app, identity, True)
    assert metric_live.set_delta(app, identity, delta)
    observed = []
    original = charts.braille_chart

    def painter(g, values, width, height, **options):
        observed.append((list(values), list(options["sample_times"])))
        return original(g, values, width, height, **options)

    monkeypatch.setattr(charts, "braille_chart", painter)
    C.begin_frame(app, 80, 30)
    rows = views.metric_curve(app, [10, 90], 80, 5, identity, running=True,
                              hi=100, unit="%", title="CPU", sample_times=[1180, 1200],
                              times=(1180, 1200), sample_interval=12)
    C.publish(app, 80, 30)
    assert observed == [([10], [1180])]
    plot, = C.initialize(app)["plots"]
    # Startup holds at the sole acquired observation; the future endpoint
    # cannot provide a fabricated slope or advance the buffered display.
    assert plot.kind == "metric" and plot.key == identity
    assert plot.x_bounds == (1180.0 - delta, 1180.0)
    y, x = plot.visible.top, plot.visible.left
    text = "\n".join(map(L.row_text, rows))
    assert "Source age 11s" in text
    assert "no observations in live window" not in text
    assert "buffering" in text
    assert C.handle_mouse(app, y, x, button="right")
    assert not metric_live.enabled(app, identity) and C.bounds(app, identity) is None
