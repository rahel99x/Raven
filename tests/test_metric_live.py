"""Live windows affect only displays, with bounded exact-source controls."""

import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

from raven import (
    chart_interaction as C,
    clock,
    interaction as I,
    layout as L,
    metric_live as M,
)
from raven.model import Job
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.views import Views


@pytest.fixture
def app():
    value = SimpleNamespace(
        mode="main",
        tab="analytics",
        width=120,
        height=40,
        selected_id="101",
        analytics_job="101",
        analytics_view="job",
        toolbar_state={},
        project_state={},
        job_panel_state={},
        analysis_state={},
        research=SimpleNamespace(generation=3),
        messages=[],
    )
    value.say = value.messages.append
    value.run_command = lambda command: M.run_command(value, command.split())
    from raven.workbench import bind_actions
    bind_actions(value)
    C.begin_frame(value, 120, 40)
    return value


def key(jid="101", metric="cpu", attempt=None, scope="resource-series"):
    return (scope, jid, metric, "%", attempt, None, None, 3)


def draw(app, identity=None, *, width=70, row=2, column=4, running=True, ascii_=False):
    identity = identity or key()
    rows, hits = M.controls(
        L.Glyphs(ascii_), app, identity, width, running=running, row=row, column=column
    )
    C.publish(app, app.width, app.height)
    return rows, hits


def current(app):
    return M.initialize(app)["records"][0]


def enable(app, identity=None):
    identity = identity or key()
    draw(app, identity)
    assert M.set_enabled(app, identity, True)
    return current(app)


@pytest.mark.parametrize("value", [30.0, 15.0, 5.0, 2.0, 1.0])
def test_log_mapping_is_reversible_and_endpoints_exact(value):
    assert M.delta_at(M.fraction(value)) == pytest.approx(value)
    assert 0 <= M.fraction(value) <= 1
    assert M.delta_at(0) == 30.0 and M.delta_at(1) == 1.0
    assert M.delta_at(0.5) == pytest.approx(math.sqrt(30 * 1))


@pytest.mark.parametrize("value", [
    True, False, None, "10", -1, 0, .001, .999, 30.001, 31,
    float("nan"), float("inf"), 10**1000,
])
def test_invalid_window_values_preserve_display_state_and_revision(app, value):
    enable(app)
    assert M.set_delta(app, key(), 10.0)
    revision = M.document_revision(app)
    assert not M.set_delta(app, key(), value)
    assert M.window(app, key(), now=100.0) == (90.0, 100.0)
    assert M.document_revision(app) == revision


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width", [24, 25, 32, 35, 36, 50, 120])
def test_controls_always_fit_one_row_with_two_labeled_ends(app, ascii_, width):
    rows, hits = draw(app, width=width, ascii_=ascii_, column=0)
    assert len(rows) == 1 and L.vlen(L.row_text(rows[0])) == width
    assert "30s" in L.row_text(rows[0]) and "1s" in L.row_text(rows[0])
    assert len(hits) == 3
    if ascii_:
        assert L.row_text(rows[0]).isascii()
    assert all(0 <= hit[2]["left"] < hit[2]["right"] <= width for hit in hits)
    geometry = current(app)
    M.set_enabled(app, key(), True)
    C.begin_frame(app, 120, 40)
    draw(app, width=width, ascii_=ascii_, column=0)
    assert current(app).slider_full == geometry.slider_full


def test_slider_state_is_independent_for_metrics_jobs_and_attempts(app):
    one = key()
    two = key(metric="memory")
    otherjob = key("102")
    attempt = key(attempt="new-start")
    draw(app, one)
    M.set_delta(app, one, 10.0)
    M.set_enabled(app, one, True)
    draw(app, two, row=4)
    M.set_delta(app, two, 1.0)
    draw(app, otherjob, row=6)
    M.set_delta(app, otherjob, 2.0)
    assert M.window(app, one, now=100.0) == (90.0, 100.0)
    assert M.window(app, two, now=100.0) is None
    draw(app, attempt, row=8)
    assert not M.enabled(app, one)
    assert M.initialize(app)["entries"][one]["delta"] == 10.0
    assert not M.enabled(app, attempt)
    assert M.initialize(app)["entries"][otherjob]["delta"] == 2.0


def test_area_companion_reads_exact_same_canonical_window_with_one_control_row(app):
    curve = key()
    area = key(scope="resource-area")
    enable(app, curve)
    M.set_delta(app, curve, 1.0)
    assert M.canonical(area) == curve
    assert M.window(app, area, now=100.0) == M.window(app, curve, now=100.0)
    assert len(M.initialize(app)["records"]) == 1


@pytest.mark.parametrize("status", [False, None, "RUNNING", 1])
def test_only_explicit_current_running_eligibility_creates_controls(app, status):
    enable(app)
    C.begin_frame(app, 120, 40)
    rows, hits = draw(app, running=status)
    assert rows == hits == []
    assert M.window(app, key(), now=100.0) is None
    assert M.initialize(app)["records"] == ()


def test_finished_removed_and_mutated_jobs_disable_live_without_full_snapshot(app):
    job = Job("101", "running", "cpu", "RUNNING", submit="s", start="r")
    app.store = SimpleNamespace(
        jobs=[job], snapshot=lambda: pytest.fail("live took a full snapshot")
    )
    identity = key(attempt="s|r")
    enable(app, identity)
    assert M.window(app, identity, now=100.0) == (70.0, 100.0)
    job.state = "COMPLETED"
    M.tick(app)
    assert not M.enabled(app, identity) and M.window(app, identity, now=100.0) is None
    job.state = "RUNNING"
    M.set_running(app, identity, True)
    M.set_enabled(app, identity, True)
    app.store.jobs = []
    M.tick(app)
    assert M.window(app, identity, now=100.0) is None


def test_old_native_attempt_cannot_be_enabled_again_after_inplace_restart(app):
    job = Job("101", "same job ID", "cpu", "RUNNING", submit="s", start="r")
    app.store = SimpleNamespace(jobs=[job])
    old = key(attempt="s|r")
    enable(app, old)
    job.start = "new"
    assert M.window(app, old, now=100.0) is None
    assert not M.set_enabled(app, old, True)
    new = key(attempt="s|new")
    draw(app, new)
    assert M.set_enabled(app, new, True)
    assert M.window(app, new, now=100.0) == (70.0, 100.0)


def test_clock_anchored_window_never_requests_sampler_or_source_io(app, monkeypatch):
    app.store = SimpleNamespace(
        snapshot=lambda: pytest.fail("live took a full snapshot")
    )
    app.research.request = lambda *args: pytest.fail("live requested metrics")
    app.research.current = lambda *args: pytest.fail("live read a source cache")
    app.sampler = SimpleNamespace(
        refresh_all=lambda: pytest.fail("live refreshed scheduler")
    )
    enable(app)
    M.set_delta(app, key(), 1.0)
    monkeypatch.setattr(clock, "now", lambda: 100.0)
    assert M.window(app, key()) == (99.0, 100.0)
    for i in range(1000):
        assert M.window(app, key(), now=100.0 + i) == (99.0 + i, 100.0 + i)
    assert M.window(app, key(), now=float("nan")) is None
    assert M.window(app, key(), now=float("inf")) is None


def test_raw_slider_drag_clamps_log_endpoints_and_keyboard_adjusts_same_metric(app):
    control = enable(app)
    y = control.slider.top
    left = control.slider_full.left
    right = control.slider_full.right - 1
    assert M.handle_mouse(app, y, left, button="press")
    assert M.active(app)
    assert M.handle_mouse(app, y, right, button="motion")
    assert M.initialize(app)["entries"][key()]["delta"] == 1.0
    assert M.handle_mouse(app, y, right + 50, button="release")
    assert not M.active(app) and M.initialize(app)["entries"][key()]["delta"] == 1.0
    assert M.handle_key(app, "home")
    assert M.initialize(app)["entries"][key()]["delta"] == 30.0
    assert M.handle_key(app, "right")
    assert 1.0 < M.initialize(app)["entries"][key()]["delta"] < 30.0
    assert M.handle_key(app, "end")
    assert M.initialize(app)["entries"][key()]["delta"] == 1.0
    assert not M.handle_key(app, "down") and M.initialize(app)["focus"] is None


@pytest.mark.parametrize(
    "change",
    ["width", "height", "tab", "mode", "job", "menu", "project", "run", "source"],
)
def test_slider_stale_context_cancels_restores_preview_and_consumes_release(
    app, change
):
    control = enable(app)
    M.handle_mouse(app, control.slider.top, control.slider.right - 1, button="press")
    assert M.initialize(app)["entries"][key()]["delta"] != 30.0
    if change == "width":
        app.width += 1
    elif change == "height":
        app.height += 1
    elif change == "tab":
        app.tab = "research"
    elif change == "mode":
        app.mode = "confirm"
    elif change == "job":
        app.selected_id = "102"
    elif change == "menu":
        app.toolbar_state["menu"] = "View"
    elif change == "project":
        app.project_state["root"] = "/new"
    elif change == "run":
        app.project_state["binding"] = {"job_id": "101", "run_id": "new"}
    else:
        C.begin_frame(app, 120, 40)
        draw(app, key(metric="other"))
    assert M.handle_mouse(
        app, control.slider.top, control.slider.right - 1, button="release"
    )
    assert not M.active(app) and M.initialize(app)["entries"][key()]["delta"] == 30.0


def test_slider_geometry_change_timeout_vertical_outside_and_escape_cancel(app):
    control = enable(app)
    M.handle_mouse(app, control.slider.top, control.slider.right - 1, button="press")
    C.begin_frame(app, 120, 40)
    draw(app, width=60)
    assert not M.active(app) and M.initialize(app)["entries"][key()]["delta"] == 30.0
    for mode in ("timeout", "outside", "escape"):
        control = current(app)
        M.handle_mouse(
            app, control.slider.top, control.slider.right - 1, button="press"
        )
        if mode == "timeout":
            M.tick(
                app,
                now=M.initialize(app)["capture"]["last"] + M.CAPTURE_TIMEOUT + 0.001,
            )
        elif mode == "outside":
            M.handle_mouse(
                app, control.slider.top + 3, control.slider.right - 1, button="release"
            )
        else:
            assert M.handle_key(app, "esc")
        assert not M.active(app) and M.initialize(app)["entries"][key()]["delta"] == 30.0


def test_layout_pipeline_maps_live_controls_and_clips_them_with_source_document(app):
    marker = C.mark(app)
    rows, _ = M.controls(L.Glyphs(False), app, key(), 60, row=8, column=2)
    records = C.take_since(app, marker)
    assert records[0].kind == "live-controls"
    mapped = C.map_records(records, {8: 15}, dx=10, dy=3, clip=(18, 12, 19, 72))
    C.put_records(app, mapped)
    C.publish(app, 120, 40)
    control = current(app)
    assert control.rect == I.Rect(18, 12, 19, 72)
    assert M.descriptors(app)[0]["rect"].top == 18
    assert M.descriptors(app)[1]["rect"].right <= 72
    C.begin_frame(app, 120, 40)
    C.put_records(app, C.map_records(records, {8: 100}, clip=(0, 0, 40, 120)))
    C.publish(app, 120, 40)
    assert M.descriptors(app) == ()


def test_xy_capture_temporarily_freezes_clock_mapping_then_commit_stops_live(app):
    enable(app)
    metadata = {
        "plot_rect": (5, 10, 13, 60),
        "x_bounds": (70.0, 100.0),
        "y_bounds": (0.0, 100.0),
        "valid": True,
        "has_data": True,
    }
    C.record(app, key(), metadata)
    C.publish(app, 120, 40)
    assert C.handle_mouse(app, 6, 15, button="press")
    assert M.window(app, key(), now=999.0) == (70.0, 100.0)
    assert M.document_interval(app) is None
    C.cancel(app)
    assert M.window(app, key(), now=999.0) == (969.0, 999.0) and M.enabled(app, key())
    C.handle_mouse(app, 6, 15, button="press")
    C.handle_mouse(app, 11, 45, button="release")
    assert not M.enabled(app, key()) and M.window(app, key(), now=999.0) is None
    assert C.bounds(app, key())
    assert C.undo(app, key()) and C.bounds(app, key()) is None


def test_enabling_live_removes_line_and_area_boxes_only_for_this_metric(app):
    draw(app)
    area = key(scope="resource-area")
    other = key(metric="memory")
    C.initialize(app)["zoom"][key()] = {
        "x": (1.0, 2.0),
        "y": (3.0, 4.0),
        "scale": "linear",
        "undo": [],
    }
    C.initialize(app)["zoom"][area] = {
        "x": (1.0, 2.0),
        "y": (3.0, 4.0),
        "scale": "linear",
        "undo": [],
    }
    C.initialize(app)["zoom"][other] = {
        "x": (1.0, 2.0),
        "y": (3.0, 4.0),
        "scale": "linear",
        "undo": [],
    }
    M.set_enabled(app, key(), True)
    assert (
        C.bounds(app, key()) is None
        and C.bounds(app, area) is None
        and C.bounds(app, other)
    )
    M.set_enabled(app, key(), False)
    assert M.window(app, key(), now=100.0) is None


def test_empty_one_second_graph_still_gets_bounded_document_deadline(app):
    enable(app)
    M.set_delta(app, key(), 1.0)
    assert C.initialize(app)["plots"] == ()
    assert M.document_interval(app) == 0.1
    assert M.document_revision(app) > 0
    C.begin_frame(app, 120, 40)
    C.publish(app, 120, 40)
    assert M.document_interval(app) is None


def test_cached_feedback_keeps_pointer_and_arrow_focus_without_activating_twice(app):
    draw(app)
    canvas = [[(" " * 120, "")] for _ in range(40)]
    I.publish(app, canvas, [], 120, 40, extra_controls=M.descriptors(app))
    control = current(app)
    I.handle_mouse(app, control.toggle.top, control.toggle.left, button="motion")
    assert M.handle_mouse(app, control.toggle.top, control.toggle.left, button="left")
    assert M.enabled(app, key())
    state = I.initialize(app)
    assert state["focused"] == "metric-live:" + control.token and state["active"]
    painted = M.feedback(app, L.Glyphs(False))
    assert any(
        "under" in style and "bg:track" in style
        for _, _, row in painted
        for _, style in row
    )
    assert I.handle_key(app, "right")
    assert state["focused"] == "metric-window:" + control.token
    assert I.handle_key(app, "enter")
    assert M.initialize(app)["focus"] == control.token
    assert M.handle_key(app, "right")
    assert M.initialize(app)["entries"][key()]["delta"] < 30.0


def test_commands_only_target_visible_exact_running_source(app):
    draw(app)
    token = current(app).token
    assert M.run_command(app, ["metric-live", token, "on"])
    assert M.enabled(app, key())
    assert M.run_command(app, ["metric-window", token, "1"])
    assert M.initialize(app)["entries"][key()]["delta"] == 1.0
    for invalid in ("nan", "inf", "0", "0.999", "31", "bad"):
        assert M.run_command(app, ["metric-window", token, invalid])
        assert M.initialize(app)["entries"][key()]["delta"] == 1.0
    assert M.run_command(app, ["metric-live", "old-token", "on"])
    C.begin_frame(app, 120, 40)
    draw(app, running=False)
    assert M.run_command(app, ["metric-live", token, "on"]) and not M.enabled(
        app, key()
    )
    assert not M.run_command(app, ["unrelated"])


@pytest.fixture
def palette_dashboard(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: 200.0)
    cfg = Config({"animations": False, "startup": {"enabled": False}, "log_lines": 0})
    store = Store(persist=False)
    store.jobs = [
        Job(
            str(jid),
            "train-" + str(jid),
            "cpu",
            "RUNNING",
            cpus=4,
            mem_req="8G",
            submit="submit",
            start="start",
        )
        for jid in (7, 8)
    ]
    for job in store.jobs:
        for index in range(20):
            store.record(
                job.id,
                {
                    "k": "live",
                    "t": 180.0 + index,
                    "cpu": index / 25,
                    "rss": (1 + index / 20) * 1024**3,
                },
            )
    app = App(store, None, None, cfg, "test", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    app.tab, app.selected_id, app.analytics_job = "analytics", "7", "7"
    return SimpleNamespace(app=app, store=store, views=views)


def palette_frame(dashboard, *, width=160, height=52):
    snapshot = dashboard.store.snapshot()
    rows, hits = dashboard.views.compose(snapshot, dashboard.app, width, height)
    dashboard.views.overlay(snapshot, dashboard.app, width, height)
    return rows, hits


def type_palette_command(dashboard, command, *, before_enter=None):
    app = dashboard.app
    origin = app.mode
    app.handle(":")
    assert app.mode == "palette"
    palette_frame(dashboard)
    assert M.initialize(app)["records"] == ()
    assert M.descriptors(app) == ()
    assert not M.handle_mouse(app, 0, 0, button="left")
    assert not M.handle_key(app, "right")
    for char in command:
        app.handle("space" if char == " " else char)
    assert app.palette_edit == command
    palette_frame(dashboard)
    if before_enter:
        before_enter(dashboard)
    app.handle("enter")
    assert app.mode == origin


def test_actual_app_palette_live_commands_restore_main_without_a_redraw(
    palette_dashboard,
):
    dashboard = palette_dashboard
    palette_frame(dashboard)
    control = current(dashboard.app)
    type_palette_command(dashboard, "metric-live " + control.token + " on")
    assert M.enabled(dashboard.app, control.key)
    assert M.initialize(dashboard.app)["records"] == ()
    palette_frame(dashboard)
    type_palette_command(dashboard, "metric-window " + control.token + " 1")
    assert M.initialize(dashboard.app)["entries"][control.key]["delta"] == 1.0
    palette_frame(dashboard)
    type_palette_command(dashboard, "metric-live " + control.token + " off")
    assert not M.enabled(dashboard.app, control.key)


@pytest.mark.parametrize(
    "change", ["page", "job", "attempt", "finished", "resize", "scroll", "split"]
)
def test_actual_palette_rejects_a_token_after_document_change(
    palette_dashboard, change
):
    dashboard = palette_dashboard
    palette_frame(dashboard)
    control = current(dashboard.app)

    def change_document(value):
        app = value.app
        if change == "page":
            app.tab = "jobs"
        elif change == "job":
            app.selected_id = app.analytics_job = "8"
        elif change == "attempt":
            value.store.jobs[0].start = "new-start"
        elif change == "finished":
            value.store.jobs[0].state = "COMPLETED"
        elif change == "resize":
            palette_frame(value, width=130)
        elif change == "scroll":
            app.analytics_document_state["top"] += 3
        else:
            app.layout_state.ratio += 5

    type_palette_command(
        dashboard, "metric-live " + control.token + " on", before_enter=change_document
    )
    assert not M.enabled(dashboard.app, control.key)
    assert "visible Live control" in dashboard.app.message


def open_reported_modal(dashboard):
    app = dashboard.app
    app.analysis_result_job = "7"
    app.analysis_result_generation = None
    app.analysis_result = {
        "path": "/run/metrics.jsonl",
        "series": {"loss": [{"t": 196.0, "value": 2.0}, {"t": 199.0, "value": 1.0}]},
    }
    app.run_command("chart loss")
    assert app.mode == "analysis"
    palette_frame(dashboard)
    return current(app)


@pytest.mark.parametrize("surface", ["analytics", "jobs", "reported-modal"])
@pytest.mark.parametrize("ascii_", [False, True])
def test_actual_graph_windows_cover_both_delta_endpoints_and_right_reset(
    palette_dashboard, surface, ascii_
):
    dashboard = palette_dashboard
    app = dashboard.app
    dashboard.views.set_ascii(ascii_)
    app.tab = "jobs" if surface == "jobs" else "analytics"
    app.job_panel_state["mode"] = "analytics"
    if surface == "reported-modal":
        control = open_reported_modal(dashboard)
    else:
        palette_frame(dashboard)
        control = current(app)
    retained_samples = tuple(dict(sample) for sample in dashboard.store.series["7"])
    assert M.set_enabled(app, control.key, True)
    # Native resource sampling retains two five-second intervals. The report
    # fixture has only two acquired records and initially holds at its oldest.
    end = 196.0 if surface == "reported-modal" else 190.0
    for delta in (30.0, 1.0):
        expected = (end - delta, end)
        assert M.set_delta(app, control.key, delta)
        palette_frame(dashboard)
        plot = next(
            plot for plot in C.initialize(app)["plots"]
            if M.canonical(plot.key) == control.key
        )
        assert plot.x_bounds == expected
        assert M.window(app, control.key) == expected
    control = next(
        record for record in M.initialize(app)["records"]
        if record.key == control.key
    )
    app.click(control.slider.top, control.slider.left, [], button="right")
    palette_frame(dashboard)
    restored = next(
        plot for plot in C.initialize(app)["plots"]
        if M.canonical(plot.key) == control.key
    )
    assert restored.x_bounds == (end - 30.0, end)
    assert M.enabled(app, control.key)
    assert tuple(dashboard.store.series["7"]) == retained_samples


def test_actual_app_palette_commands_restore_exact_chart_modal(palette_dashboard):
    dashboard = palette_dashboard
    control = open_reported_modal(dashboard)
    assert control.layer == 1
    type_palette_command(dashboard, "metric-live " + control.token + " on")
    assert dashboard.app.mode == "analysis" and M.enabled(dashboard.app, control.key)
    palette_frame(dashboard)
    type_palette_command(dashboard, "metric-window " + control.token + " 10")
    assert M.initialize(dashboard.app)["entries"][control.key]["delta"] == 10.0


@pytest.mark.parametrize("modal", [False, True])
def test_palette_slider_focus_survives_tick_and_first_restored_frame(
    palette_dashboard, modal
):
    dashboard = palette_dashboard
    if modal:
        control = open_reported_modal(dashboard)
    else:
        palette_frame(dashboard)
        control = current(dashboard.app)
    type_palette_command(dashboard, "metric-window " + control.token + " focus")
    state = M.initialize(dashboard.app)
    assert state["focus"] == control.token and state["pending_focus"]
    dashboard.app.tick()
    dashboard.app.tick()
    assert state["focus"] == control.token and state["pending_focus"]
    assert M.descriptors(dashboard.app) == ()
    palette_frame(dashboard)
    assert state["focus"] == control.token and state["pending_focus"] is None
    dashboard.app.handle("right")
    assert state["entries"][control.key]["delta"] < M.MAX_DELTA
    dashboard.app.handle("down")
    assert state["focus"] is None


@pytest.mark.parametrize(
    "change", ["page", "job", "attempt", "finished", "resize", "scroll", "menu"]
)
def test_palette_pending_focus_rejects_stale_context_before_first_frame(
    palette_dashboard, change
):
    dashboard = palette_dashboard
    palette_frame(dashboard)
    control = current(dashboard.app)
    type_palette_command(dashboard, "metric-window " + control.token + " focus")
    app = dashboard.app
    if change == "page":
        app.tab = "jobs"
    elif change == "job":
        app.selected_id = app.analytics_job = "8"
    elif change == "attempt":
        dashboard.store.jobs[0].start = "new-start"
    elif change == "finished":
        dashboard.store.jobs[0].state = "COMPLETED"
    elif change == "resize":
        app.width = 130
    elif change == "scroll":
        app.analytics_document_state["top"] += 3
    else:
        app.toolbar_state["menu"] = "view"
    app.tick()
    state = M.initialize(app)
    assert state["focus"] is None and state["pending_focus"] is None
    assert state["entries"][control.key]["delta"] == M.MAX_DELTA


def test_palette_pending_focus_clears_when_fresh_main_publication_hides_control(
    palette_dashboard,
):
    dashboard = palette_dashboard
    palette_frame(dashboard)
    control = current(dashboard.app)
    type_palette_command(dashboard, "metric-window " + control.token + " focus")
    C.begin_frame(dashboard.app, dashboard.app.width, dashboard.app.height)
    C.publish(dashboard.app)
    state = M.initialize(dashboard.app)
    assert state["focus"] is None and state["pending_focus"] is None


@pytest.mark.parametrize("change", ["source", "metric", "modal", "hidden"])
def test_actual_palette_modal_token_rejects_changed_or_hidden_chart(
    palette_dashboard, change
):
    dashboard = palette_dashboard
    control = open_reported_modal(dashboard)

    def change_document(value):
        app = value.app
        if change == "source":
            app.analysis_result["path"] = "/other/metrics.jsonl"
        elif change == "metric":
            app.analysis_state["metric"] = "accuracy"
        elif change == "modal":
            app.analysis_state["modal"] = "inspect"
        else:
            app.command_state["origin_mode"] = "main"

    if change == "hidden":
        app = dashboard.app
        app.handle(":")
        palette_frame(dashboard)
        for char in "metric-live " + control.token + " on":
            app.handle("space" if char == " " else char)
        change_document(dashboard)
        app.handle("enter")
        assert app.mode == "main"
    else:
        type_palette_command(
            dashboard,
            "metric-live " + control.token + " on",
            before_enter=change_document,
        )
    assert not M.enabled(dashboard.app, control.key)


def test_transient_state_caches_and_registry_remain_bounded(app):
    for n in range(M.MAX_METRICS + 20):
        M.set_running(app, key(jid=str(n)), True)
    state = M.initialize(app)
    assert len(state["entries"]) == len(state["current"]) == M.MAX_METRICS
    assert "save" not in dir(M) and "restore" not in dir(M)
    assert state["counter"] == M.MAX_METRICS + 20


def test_ascii_default_crosshair_uses_real_views_ref(app):
    app.views_ref = SimpleNamespace(g=L.Glyphs(True))
    C.begin_frame(app, 120, 40)
    C.record(
        app,
        key(),
        {
            "plot_rect": (5, 10, 13, 60),
            "x_bounds": (0.0, 10.0),
            "y_bounds": (0.0, 100.0),
        },
    )
    C.publish(app, 120, 40)
    C.hover(app, 8, 30)
    assert any(row == [(".", "accent")] for _, _, row in C.feedback(app))


def test_running_job_beyond_ten_thousand_rows_stays_eligible_with_bounded_index(app):
    wanted = Job("10001", "visible far row", "cpu", "RUNNING", submit="s", start="r")
    app.store = SimpleNamespace(
        jobs=[Job(str(n), "earlier", "cpu", "PENDING") for n in range(10001)] + [wanted]
    )
    identity = key(jid="10001", attempt="s|r")
    draw(app, identity)
    assert M.set_enabled(app, identity, True)
    assert M.window(app, identity, now=100.0) == (70.0, 100.0)
    assert M.initialize(app)["job_map"] == {"10001": wanted}
    # Adding a second metric for this ID reuses the same bounded Job index.
    existing = M.initialize(app)["job_map"]
    second = key(jid="10001", metric="memory", attempt="s|r")
    draw(app, second)
    assert M.set_enabled(app, second, True)
    assert M.initialize(app)["job_map"] is existing
    wanted.state = "FAILED"
    assert M.window(app, second, now=100.0) is None


def test_inplace_job_append_refreshes_only_wanted_id_index(app):
    first = Job("101", "first", "cpu", "RUNNING")
    app.store = SimpleNamespace(jobs=[first])
    draw(app)
    assert M.set_enabled(app, key(), True)
    second = Job("102", "appended", "cpu", "RUNNING")
    app.store.jobs.append(second)
    identity = key(jid="102")
    draw(app, identity)
    assert M.set_enabled(app, identity, True)
    assert M.window(app, identity, now=100.0) == (70.0, 100.0)
    assert len(M.initialize(app)["job_map"]) == 2


def test_reported_scheduler_attempt_fallback_rejects_previous_requeue(app):
    job = Job("101", "reported", "cpu", "RUNNING", submit="S", start="StartA")
    app.store = SimpleNamespace(jobs=[job])
    old = (
        "reported-metric",
        "101",
        "loss",
        "metrics.jsonl",
        "scheduler:S|StartA",
        None,
        None,
        3,
    )
    draw(app, old)
    assert M.set_enabled(app, old, True)
    assert M.window(app, old, now=100.0) == (70.0, 100.0)
    job.start = "StartB"
    assert M.window(app, old, now=100.0) is None
    assert not M.set_enabled(app, old, True)
    new = (*old[:4], "scheduler:S|StartB", *old[5:])
    draw(app, new)
    assert M.set_enabled(app, new, True)
    assert M.window(app, new, now=100.0) == (70.0, 100.0)


def test_project_declared_attempt_stays_opaque_to_scheduler_start_labels(app):
    job = Job("101", "project reported", "cpu", "RUNNING", submit="S", start="StartA")
    app.store = SimpleNamespace(jobs=[job])
    identity = (
        "reported-metric",
        "101",
        "loss",
        "metrics.jsonl",
        4,
        "/project",
        "run-4",
        3,
    )
    draw(app, identity)
    assert M.set_enabled(app, identity, True)
    assert M.window(app, identity, now=100.0) == (70.0, 100.0)


def test_stale_running_snapshot_cannot_show_controls_for_finished_actual_job(app):
    actual = Job(
        "101", "finished behind frozen view", "cpu", "COMPLETED", submit="s", start="r"
    )
    app.store = SimpleNamespace(jobs=[actual])
    identity = key(attempt="s|r")
    rows, hits = draw(app, identity, running=True)
    assert rows == hits == []
    assert M.descriptors(app) == () and M.window(app, identity, now=100.0) is None


def test_stale_native_attempt_controls_are_hidden_even_if_snapshot_says_running(app):
    actual = Job("101", "new attempt", "cpu", "RUNNING", submit="s", start="new")
    app.store = SimpleNamespace(jobs=[actual])
    rows, hits = draw(app, key(attempt="s|old"), running=True)
    assert rows == hits == []
    assert M.descriptors(app) == ()


def test_same_length_inplace_job_slot_replacement_uses_new_actual_attempt(app):
    old_job = Job("101", "old slot", "cpu", "RUNNING", submit="S", start="A")
    app.store = SimpleNamespace(jobs=[old_job])
    old = key(attempt="S|A")
    enable(app, old)
    fresh_job = Job("101", "new slot", "cpu", "RUNNING", submit="S", start="B")
    app.store.jobs[0] = fresh_job
    assert M.window(app, old, now=100.0) is None
    fresh = key(attempt="S|B")
    C.begin_frame(app, 120, 40)
    rows, _ = draw(app, fresh)
    assert len(rows) == 1 and M.set_enabled(app, fresh, True)
    assert M.window(app, fresh, now=100.0) == (70.0, 100.0)
    assert M.initialize(app)["job_map"]["101"] is fresh_job


def test_sampling_control_has_independent_geometry_and_graph_neighbor(app):
    _, hits = draw(app)
    control = current(app)
    assert control.slider_full.right <= control.rate_slider_full.left
    assert control.rate_slider.right <= control.rect.right
    assert [hit[2]["id"] for hit in hits] == [
        "metric-live:" + control.token,
        "metric-window:" + control.token,
        "metric-sampling:" + control.token,
    ]
    canvas = [[(" " * 120, "")] for _ in range(40)]
    I.publish(app, canvas, [], 120, 40, extra_controls=M.descriptors(app))
    assert M.run_command(app, ["metric-window", control.token, "focus"])
    M.handle_key(app, "down")
    I.initialize(app).update(focused="metric-window:" + control.token, active=True)
    assert I.handle_key(app, "right")
    assert I.initialize(app)["focused"] == "metric-sampling:" + control.token
    assert I.handle_key(app, "enter")
    assert M.initialize(app)["focus_kind"] == "sampling"
    assert M.handle_key(app, "right")
    assert M.initialize(app)["entries"][key()]["rate"] == 2
    assert M.initialize(app)["entries"][key()]["delta"] == M.MAX_DELTA


@pytest.mark.parametrize("rate", [1, 2, 25, 50, 99, 100])
def test_sampling_fraction_round_trips_integer_endpoints(rate):
    assert M.rate_at(M.rate_fraction(rate)) == rate
    assert M.rate_at(-10) == 1 and M.rate_at(10) == 100


@pytest.mark.parametrize("value", [True, False, None, "50", 0, -1, 101, 1.1,
                                  float("nan"), float("inf"), 10**1000])
def test_invalid_sampling_values_do_not_change_a_valid_request(app, value):
    draw(app)
    assert M.set_rate(app, key(), 50)
    revision = M.document_revision(app)
    assert not M.set_rate(app, key(), value)
    assert M.initialize(app)["entries"][key()]["rate"] == 50
    assert M.document_revision(app) == revision


@pytest.mark.parametrize("kind", ["window", "sampling"])
def test_right_reset_only_changes_its_slider_and_keeps_live_zoom_and_marks(app, kind):
    control = enable(app)
    assert M.set_delta(app, key(), 10.0) and M.set_rate(app, key(), 50)
    app.marks = {"101", "102"}
    app.sel_anchor, app.sel_end = 2, 8
    C.initialize(app)["zoom"][key()] = {
        "x": (1.0, 2.0), "y": (3.0, 4.0), "scale": "linear", "undo": [],
    }
    slider = control.slider if kind == "window" else control.rate_slider
    assert M.handle_mouse(app, slider.top, slider.left, button="right")
    entry = M.initialize(app)["entries"][key()]
    assert entry["delta"] == (30.0 if kind == "window" else 10.0)
    assert entry["rate"] == (1 if kind == "sampling" else 50)
    assert entry["enabled"]
    assert app.marks == {"101", "102"} and (app.sel_anchor, app.sel_end) == (2, 8)
    assert C.bounds(app, key()) is not None


def test_rate_drag_publishes_only_changed_snapshots_and_escape_restores_backend(app):
    requests = []
    app.sampler = SimpleNamespace(set_metric_sampling=lambda value: requests.append(dict(value)),
                                  intervals={"live": 30.0})
    control = enable(app)
    assert M.set_rate(app, key(), 50)
    assert requests[-1] == {key(): 50}
    y, right = control.rate_slider.top, control.rate_slider_full.right - 1
    assert M.handle_mouse(app, y, right, button="press")
    assert M.active(app) and requests[-1] == {key(): 100}
    request_count = len(requests)
    for _ in range(50):
        assert M.handle_mouse(app, y, right + 100, button="motion")
    assert len(requests) == request_count
    assert M.handle_key(app, "esc")
    assert not M.active(app) and requests[-1] == {key(): 50}
    assert M.initialize(app)["entries"][key()]["rate"] == 50
    assert M.enabled(app, key())


@pytest.mark.parametrize("change", ["geometry", "timeout", "outside", "tab", "source"])
def test_rate_drag_cancellation_restores_original_request_and_consumes_release(app, change):
    requests = []
    app.sampler = SimpleNamespace(set_metric_sampling=lambda value: requests.append(dict(value)))
    control = enable(app)
    assert M.set_rate(app, key(), 17)
    y, x = control.rate_slider.top, control.rate_slider.right - 1
    assert M.handle_mouse(app, y, x, button="press")
    assert requests[-1] == {key(): 100}
    if change == "geometry":
        C.begin_frame(app, 120, 40)
        draw(app, width=60)
    elif change == "timeout":
        M.tick(app, now=M.initialize(app)["capture"]["last"] + M.CAPTURE_TIMEOUT + 1)
    elif change == "outside":
        M.handle_mouse(app, y + 3, x, button="release")
    elif change == "tab":
        app.tab = "jobs"
        M.tick(app)
    else:
        C.begin_frame(app, 120, 40)
        draw(app, key(metric="memory"))
    assert not M.active(app)
    assert M.initialize(app)["entries"][key()]["rate"] == 17
    assert requests[-1].get(key()) == 17
    assert M.handle_mouse(app, y, x, button="release")


def test_rate_slider_keyboard_uses_integer_steps_home_end_and_pages(app):
    draw(app)
    control = current(app)
    assert M.run_command(app, ["metric-sampling", control.token, "focus"])
    assert M.initialize(app)["focus_kind"] == "sampling"
    for input_key, expected in (("end", 100), ("left", 99), ("pgup", 89),
                                ("pgdn", 99), ("right", 100), ("right", 100),
                                ("home", 1), ("left", 1), ("pgup", 1)):
        assert M.handle_key(app, input_key)
        assert M.initialize(app)["entries"][key()]["rate"] == expected
    assert M.initialize(app)["entries"][key()]["delta"] == 30.0
    assert M.handle_key(app, "esc")
    assert M.initialize(app)["focus"] is None


def test_sampling_and_delta_commands_reset_without_live_or_source_actions(app):
    control = enable(app)
    assert M.run_command(app, ["metric-sampling", control.token, "50"])
    assert M.initialize(app)["entries"][key()]["rate"] == 50
    assert "polling interval" in app.messages[-1] and "50x" not in app.messages[-1]
    for invalid in ("0", "101", "nan", "inf", "2.5", "bad", "1.0"):
        assert M.run_command(app, ["metric-sampling", control.token, invalid])
        assert M.initialize(app)["entries"][key()]["rate"] == 50
    assert M.run_command(app, ["metric-window", control.token, "1"])
    assert M.run_command(app, ["metric-window", control.token, "reset"])
    assert M.run_command(app, ["metric-sampling", control.token, "reset"])
    entry = M.initialize(app)["entries"][key()]
    assert entry["delta"] == 30.0 and entry["rate"] == 1 and entry["enabled"]


@pytest.mark.parametrize("change", ["completed", "removed", "attempt", "replacement"])
def test_nonlive_rate_request_removed_on_ineligible_job_without_any_source_read(app, change):
    requests = []
    job = Job("101", "running", "cpu", "RUNNING", submit="s", start="r")
    app.store = SimpleNamespace(jobs=[job])
    app.sampler = SimpleNamespace(set_metric_sampling=lambda value: requests.append(dict(value)),
                                  refresh_all=lambda: pytest.fail("unexpected scheduler refresh"))
    identity = key(attempt="s|r")
    draw(app, identity)
    assert M.set_rate(app, identity, 100)
    assert not M.enabled(app, identity) and requests[-1] == {identity: 100}
    if change == "completed":
        job.state = "COMPLETED"
    elif change == "removed":
        app.store.jobs = []
    elif change == "attempt":
        job.start = "new-start"
    else:
        replacement = key(attempt="s|r", scope="resource-series")
        replacement = (*replacement[:4], "new", *replacement[5:])
        M.set_running(app, replacement, True)
    M.tick(app)
    assert requests[-1] == {}
    assert not M.set_rate(app, identity, 50)


def test_shared_source_current_interval_uses_fastest_metric_request(app):
    app.cfg = Config({"intervals": {"live": 30.0}})
    first, second = key(metric="cpu"), key(metric="memory")
    draw(app, first)
    draw(app, second, row=4)
    assert M.set_rate(app, first, 100)
    assert M.set_rate(app, second, 1)
    C.begin_frame(app, 120, 40)
    rows, _ = draw(app, second, width=120)
    text = L.row_text(rows[0])
    assert "Poll 500ms" in text and "30s" in text
    assert "100x" not in text and "1x" not in text
    assert M.initialize(app)["entries"][second]["rate"] == 1


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width", [24, 25, 36, 63, 64, 67, 68, 69, 120])
def test_feedback_and_actual_interval_labels_preserve_geometry_during_rate_changes(app, ascii_, width):
    app.cfg = Config({"intervals": {"live": 30.0}})
    draw(app, width=width, column=0, ascii_=ascii_)
    initial = current(app)
    for rate in (1, 2, 17, 100):
        assert M.set_rate(app, key(), rate)
        C.begin_frame(app, 120, 40)
        rows, _ = draw(app, width=width, column=0, ascii_=ascii_)
        control = current(app)
        assert control.slider_full == initial.slider_full
        assert control.rate_slider_full == initial.rate_slider_full
        assert L.vlen(L.row_text(rows[0])) == width
        assert "1x" not in L.row_text(rows[0]) and "100x" not in L.row_text(rows[0])
        feedback = M.feedback(app, L.Glyphs(ascii_))
        assert len(feedback) == 1 and L.vlen(L.row_text(feedback[0][2])) == width


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width", [24, 36, 64, 68, 120])
def test_requested_polling_remains_readable_when_effective_cadence_is_shared(app, ascii_, width):
    from raven.metric_sampling import format_interval
    from raven.refresh_rate import poll_interval

    app.sampler = SimpleNamespace(sampling_interval=lambda *_: .5)
    draw(app, width=width, column=0, ascii_=ascii_)
    control = current(app)
    for rate in (1, 25, 75, 100):
        M.set_rate(app, control.key, rate)
        output = M.feedback(app, L.Glyphs(ascii_))[0][2]
        text = L.row_text(output)
        requested = format_interval(poll_interval(rate, maximum=100), ascii_=ascii_)
        assert "500ms" in text and requested in text
        assert ("Set " if width >= 68 else "S" + requested) in text
        assert L.vlen(text) == width
        assert not ascii_ or text.isascii()


@pytest.mark.parametrize("modal", [False, True])
def test_sampling_palette_focus_and_rate_restore_exact_main_or_modal(palette_dashboard, modal):
    dashboard = palette_dashboard
    if modal:
        control = open_reported_modal(dashboard)
    else:
        palette_frame(dashboard)
        control = current(dashboard.app)
    type_palette_command(dashboard, "metric-sampling " + control.token + " focus")
    state = M.initialize(dashboard.app)
    assert state["focus_kind"] == "sampling" and state["pending_focus"]
    dashboard.app.tick()
    palette_frame(dashboard)
    assert state["focus_kind"] == "sampling" and state["pending_focus"] is None
    dashboard.app.handle("right")
    assert state["entries"][control.key]["rate"] == 2
    assert state["entries"][control.key]["delta"] == 30.0


@pytest.mark.parametrize("kind", ["window", "sampling"])
@pytest.mark.parametrize("modal", [False, True])
def test_actual_app_right_reset_does_not_clear_selected_jobs_or_text(palette_dashboard, kind, modal):
    from raven import text_selection
    dashboard = palette_dashboard
    if modal:
        control = open_reported_modal(dashboard)
    else:
        palette_frame(dashboard)
        control = current(dashboard.app)
    app = dashboard.app
    assert M.set_enabled(app, control.key, True)
    assert M.set_delta(app, control.key, 10.0) and M.set_rate(app, control.key, 50)
    app.marks = {"7", "8"}
    app.sel_anchor, app.sel_end = 3, 9
    selected_lines = {"anchor": "preserved sentinel"}
    text_selection.initialize(app)["selection"] = selected_lines
    slider = control.slider if kind == "window" else control.rate_slider
    app.click(slider.top, slider.left, [], button="right")
    entry = M.initialize(app)["entries"][control.key]
    assert entry["delta"] == (30.0 if kind == "window" else 10.0)
    assert entry["rate"] == (1 if kind == "sampling" else 50)
    assert entry["enabled"] and app.marks == {"7", "8"}
    assert (app.sel_anchor, app.sel_end) == (3, 9)
    assert text_selection.initialize(app)["selection"] is selected_lines
    assert app.mode == ("analysis" if modal else "main")


@pytest.mark.parametrize("change", ["page", "job", "attempt", "finished", "resize", "scroll", "split"])
def test_sampling_palette_command_rejects_stale_published_control(palette_dashboard, change):
    dashboard = palette_dashboard
    palette_frame(dashboard)
    control = current(dashboard.app)

    def change_document(value):
        app = value.app
        if change == "page":
            app.tab = "jobs"
        elif change == "job":
            app.selected_id = app.analytics_job = "8"
        elif change == "attempt":
            value.store.jobs[0].start = "new-start"
        elif change == "finished":
            value.store.jobs[0].state = "COMPLETED"
        elif change == "resize":
            palette_frame(value, width=130)
        elif change == "scroll":
            app.analytics_document_state["top"] += 3
        else:
            app.layout_state.ratio += 5

    type_palette_command(dashboard, "metric-sampling " + control.token + " 100",
                         before_enter=change_document)
    assert M.initialize(dashboard.app)["entries"][control.key]["rate"] == 1
    assert "visible Live control" in dashboard.app.message


@pytest.mark.parametrize("observed_departure", [False, True])
def test_store_generation_reuse_resets_hidden_rate_window_and_live_without_dates(app, observed_departure):
    requests = []
    store = Store(persist=False)
    store.apply_jobs([Job("101", "old attempt", "cpu", "RUNNING")])
    app.store = store
    app.sampler = SimpleNamespace(set_metric_sampling=lambda value: requests.append(dict(value)))
    identity = key(attempt="|")
    old_control = enable(app, identity)
    assert M.set_rate(app, identity, 100) and M.set_delta(app, identity, 1.0)
    assert M.initialize(app)["entries"][identity]["generation"] == (id(store), 0)
    # The metric can be hidden while a job leaves and reuses the same ID; its
    # absent submit/start fields do not supply a distinct canonical chart key.
    C.begin_frame(app, 120, 40)
    C.publish(app, 120, 40)
    store.apply_jobs([])
    if observed_departure:
        M.tick(app)
        assert M.initialize(app)["entries"][identity]["rate"] == 1
    store.apply_jobs([Job("101", "new attempt", "cpu", "RUNNING")])
    assert store.job_attempt("101") > 0
    C.begin_frame(app, 120, 40)
    draw(app, identity)
    fresh = current(app)
    entry = M.initialize(app)["entries"][identity]
    assert fresh.token != old_control.token
    assert entry["rate"] == 1 and entry["delta"] == 30.0 and not entry["enabled"]
    assert requests[-1] == {}
    M.run_command(app, ["metric-sampling", old_control.token, "100"])
    assert entry["rate"] == 1
    M.run_command(app, ["metric-sampling", fresh.token, "50"])
    assert entry["rate"] == 50 and requests[-1] == {identity: 50}


@pytest.mark.parametrize("kind", ["window", "sampling"])
def test_same_identity_reused_attempt_cancels_capture_without_restoring_old_values(app, kind):
    requests = []
    store = Store(persist=False)
    store.apply_jobs([Job("101", "old attempt", "cpu", "RUNNING")])
    app.store = store
    app.sampler = SimpleNamespace(set_metric_sampling=lambda value: requests.append(dict(value)))
    identity = key(attempt="|")
    old_control = enable(app, identity)
    assert M.set_rate(app, identity, 17) and M.set_delta(app, identity, 10.0)
    slider = old_control.slider if kind == "window" else old_control.rate_slider
    assert M.handle_mouse(app, slider.top, slider.right - 1, button="press")
    assert M.active(app)
    store.apply_jobs([])
    store.apply_jobs([Job("101", "new attempt", "cpu", "RUNNING")])
    # Even without an intermediate redraw, a stale token must expire and the
    # original capture preview must never be restored into the replacement.
    M.tick(app)
    entry = M.initialize(app)["entries"][identity]
    assert not M.active(app) and entry["token"] != old_control.token
    assert entry["rate"] == 1 and entry["delta"] == 30.0 and not entry["enabled"]
    assert requests[-1] == {}
    assert M.handle_mouse(app, slider.top, slider.left, button="release")
    assert entry["rate"] == 1 and entry["delta"] == 30.0
    C.begin_frame(app, 120, 40)
    draw(app, identity)
    fresh = current(app)
    assert M.set_rate(app, identity, 3)
    assert M.handle_mouse(app, fresh.rate_slider.top, fresh.rate_slider.right - 1, button="press")
    assert M.handle_mouse(app, fresh.rate_slider.top, fresh.rate_slider.right - 1, button="release")
    assert entry["rate"] == 100 and requests[-1] == {identity: 100}


def test_unobserved_reused_attempt_rejects_old_palette_token_until_fresh_publication(app):
    store = Store(persist=False)
    store.apply_jobs([Job("101", "old", "cpu", "RUNNING")])
    app.store = store
    identity = key(attempt="|")
    old = enable(app, identity)
    assert M.set_rate(app, identity, 100)
    store.apply_jobs([])
    store.apply_jobs([Job("101", "replacement", "cpu", "RUNNING")])
    assert M.run_command(app, ["metric-sampling", old.token, "50"])
    entry = M.initialize(app)["entries"][identity]
    assert entry["rate"] == 1 and not entry["running"]
    assert entry["token"] != old.token
    assert not M.set_rate(app, identity, 25)
    C.begin_frame(app, 120, 40)
    draw(app, identity)
    assert M.set_rate(app, identity, 25)


@pytest.mark.parametrize("kind", ["window", "sampling"])
def test_finished_capture_never_restores_a_faster_rate_after_invalidation(app, kind):
    job = Job("101", "finishing", "cpu", "RUNNING", submit="s", start="r")
    requests = []
    app.store = SimpleNamespace(jobs=[job])
    app.sampler = SimpleNamespace(set_metric_sampling=lambda value: requests.append(dict(value)))
    identity = key(attempt="s|r")
    control = enable(app, identity)
    assert M.set_rate(app, identity, 50)
    slider = control.slider if kind == "window" else control.rate_slider
    M.handle_mouse(app, slider.top, slider.right - 1, button="press")
    job.state = "COMPLETED"
    M.tick(app)
    assert not M.active(app)
    assert M.initialize(app)["entries"][identity]["rate"] == 1
    assert requests[-1] == {}
