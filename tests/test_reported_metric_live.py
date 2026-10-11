"""Reported Live windows retain real samples and exact nested chart geometry."""
from types import SimpleNamespace

import pytest

from tower import analysis_ui as A, chart_interaction as C, clock, layout as L, metric_live as M
from tower.config import Config
from tower.controller import App
from tower.model import Finished, Job, Store
from tower.research_views import render
from tower.views import Views


@pytest.fixture
def dashboard(monkeypatch):
    now = [200.0]
    monkeypatch.setattr(clock, "now", lambda: now[0])
    cfg = Config({"animations": False, "startup": {"enabled": False}})
    store = Store(persist=False)
    store.jobs = [Job("1", "current", "gpu", "RUNNING"), Job("3", "pending", "gpu", "PENDING")]
    store.finished = [Finished("2", "historical", "COMPLETED")]
    app = App(store, None, None, cfg, "test")
    app.tab, app.selected_id, app.research_job_id = "research", "1", "1"
    points = [{"t": 190.0, "value": 1.0, "step": 1}, {"t": 194.0, "value": 2.0, "step": 2},
              {"t": 196.0, "value": None, "step": 3}, {"t": 199.0, "value": 3.123456789012345, "step": 4},
              {"t": 203.0, "value": 999.0, "step": 5}]
    result = {"path": "/runs/exact/metrics.jsonl", "series": {"loss": points}, "records": 5, "status": "ok"}
    selected = ["1"]
    calls = []

    def context(snapshot, current):
        jid = selected[0]
        return {"job": current.job_record(jid, snapshot), "jid": jid, "generation": 0, "view": "experiment"}

    hub = SimpleNamespace(generation=0, context=context,
                          request=lambda ctx: (calls.append(ctx["jid"]), result)[1],
                          current=lambda ctx: result)
    app.research = hub
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    return SimpleNamespace(app=app, store=store, cfg=cfg, views=views, now=now, points=points,
                           result=result, selected=selected, calls=calls)


def research_frame(dashboard, width=120, height=30):
    app = dashboard.app
    C.begin_frame(app, width, height or 4096)
    rows, hits = render(dashboard.views, dashboard.store.snapshot(), app, width, height)
    C.publish(app, width, height or max(1, len(rows)))
    return rows, hits


def modal_frame(dashboard, width=120, height=36):
    C.begin_frame(dashboard.app, width, height)
    rows = A.overlay(dashboard.views, dashboard.store.snapshot(), dashboard.app, width, height)
    C.publish(dashboard.app, width, height)
    return rows


def sampling_tool_rows(hits):
    """Account for visible diagnostic controls without fixing their wrapping.

    Card heights remain exact assertions below. Only the independent toolbar
    can occupy a different number of rows as terminal width changes.
    """
    controls = [(y, value) for y, kind, value in hits
                if kind == "control" and value.get("group") == "research-tools"]
    assert {value["id"] for _, value in controls} == {
        "research-tools:telemetry", "research-tools:gpu-provider"}
    assert {value["action"] for _, value in controls} == {
        ("command", "telemetry 1"), ("command", "gpuprovider")}
    operations = [(y, value) for y, kind, value in hits
                  if kind == "control" and value.get("group") == "operation-tools"]
    assert {value["action"] for _, value in operations} == {
        ("command", "ops acceptance"), ("command", "ops statistics"), ("command", "ops energy"),
        ("command", "ops numerical-stability"), ("command", "ops diagnostic-campaign"), ("command", "ops scientific-field")}
    return len({y for y, _ in controls + operations})


@pytest.mark.parametrize("width", [23, 24, 35, 36, 120])
@pytest.mark.parametrize("jid", ["1", "2", "3", "missing"])
def test_reported_live_controls_only_exist_for_the_exact_current_running_job(dashboard, width, jid):
    dashboard.selected[0] = jid
    rows, hits = research_frame(dashboard, width)
    records = M.initialize(dashboard.app)["records"]
    expected = width >= M.MIN_WIDTH and jid == "1"
    assert bool(records) == expected
    assert any(value["id"].startswith("metric-live:") for value in M.descriptors(dashboard.app)) == expected
    assert not any(kind == "control" and value["id"].startswith("metric-live:") for _, kind, value in hits)
    assert all(record.key[1] == jid for record in records)
    assert all(0 <= record.visible.left < record.visible.right <= width for record in records)
    assert all(L.vlen(L.row_text(row)) <= width for row in rows if "[Live" in L.row_text(row))


def test_reported_context_job_identity_wins_over_other_running_selection(dashboard):
    dashboard.store.jobs.append(Job("9", "project-bound", "main", "RUNNING"))
    dashboard.selected[0] = "9"
    research_frame(dashboard)
    records = M.initialize(dashboard.app)["records"]
    assert records and all(record.key[1] == "9" for record in records)
    assert dashboard.app.selected_id == "1" and dashboard.app.research_job_id == "1"


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("delta", [1.0, 30.0])
def test_live_card_uses_only_observed_values_keeps_gaps_and_rejects_future_raster_endpoint(dashboard, monkeypatch, ascii_, delta):
    # Put the genuine discontinuity inside the delayed window and retain an
    # already acquired successor beyond its end. The separate future record
    # must not enter the curve, freshness, or observed cadence.
    timestamps = [189.1, 189.3, 189.6, 189.9, 203.0] if delta == 1 else [180., 184., 186., 189., 203.]
    for point, timestamp in zip(dashboard.points, timestamps):
        point["t"] = timestamp
    dashboard.points.insert(-1, {"t": 194.9 if delta == 1 else 194., "value": 4., "step": 5})
    dashboard.views.g = L.Glyphs(ascii_)
    research_frame(dashboard)
    identity = M.initialize(dashboard.app)["records"][0].key
    assert M.set_enabled(dashboard.app, identity, True)
    assert M.set_delta(dashboard.app, identity, delta)
    captured = []
    raster = A.charts.braille_chart

    def chart(g, values, *args, **kwargs):
        captured.append((tuple(values), kwargs["sample_times"], kwargs["times"], kwargs["sample_interval"]))
        return raster(g, values, *args, **kwargs)

    monkeypatch.setattr(A.charts, "braille_chart", chart)
    rows, _ = research_frame(dashboard)
    assert captured[-1][0] == (1.0, 2.0, None, dashboard.points[3]["value"], 4.)
    assert captured[-1][1] == [point["t"] for point in dashboard.points[:5]]
    assert captured[-1][2] == (190.0 - delta, 190.0)
    assert captured[-1][3] == pytest.approx(.3 if delta == 1 else 3.5)
    source = next(L.row_text(row) for row in rows if " Source:" in L.row_text(row))
    assert "4/6 samples" in source and "gaps 1" in source
    assert "ahead of clock" not in source
    assert ("observed cadence 300ms" if delta == 1 else "observed cadence 3.5s") in source
    header = next(L.row_text(row) for row in rows if "loss  last" in L.row_text(row))
    assert "999" not in header
    if ascii_:
        assert all(L.row_text(row).isascii() for row in rows)


def test_moving_window_invalidates_raster_and_off_restores_buffered_full_history(dashboard, monkeypatch):
    research_frame(dashboard)
    identity = M.initialize(dashboard.app)["records"][0].key
    assert M.set_enabled(dashboard.app, identity, True)
    calls, raster = [], A.charts.braille_chart
    monkeypatch.setattr(A.charts, "braille_chart", lambda *args, **kwargs: (calls.append(kwargs["times"]), raster(*args, **kwargs))[1])
    research_frame(dashboard)
    dashboard.now[0] += 1.5
    research_frame(dashboard)
    assert calls == [(160.0, 190.0), (161.5, 191.5)]
    assert M.set_delta(dashboard.app, identity, 1.0)
    rows, _ = research_frame(dashboard)
    assert calls[-1] == (190.5, 191.5)
    assert any("0/5 samples" in L.row_text(row) for row in rows)
    narrow_calls = len(calls)
    assert M.set_delta(dashboard.app, identity, 30.0)
    rows, _ = research_frame(dashboard)
    assert len(calls) == narrow_calls  # The retained 30-second raster is reused.
    assert C.initialize(dashboard.app)["plots"][0].x_bounds == (161.5, 191.5)
    assert any("1/5 samples" in L.row_text(row) for row in rows)
    assert M.set_enabled(dashboard.app, identity, False)
    research_frame(dashboard)
    off_calls = len(calls)
    dashboard.now[0] += 1
    research_frame(dashboard)
    assert len(calls) == off_calls + 1
    assert C.initialize(dashboard.app)["plots"][0].x_bounds == (190.0, 192.5)


def test_running_to_completed_hides_controls_and_immediately_restores_retained_card(dashboard):
    research_frame(dashboard)
    identity = M.initialize(dashboard.app)["records"][0].key
    assert M.set_enabled(dashboard.app, identity, True)
    dashboard.store.jobs = [Job("3", "different", "gpu", "RUNNING")]
    dashboard.store.finished.append(Finished("1", "completed-current", "COMPLETED"))
    rows, hits = research_frame(dashboard)
    assert not M.initialize(dashboard.app)["records"]
    assert not M.enabled(dashboard.app, identity)
    assert not any(kind == "control" and value["id"].startswith("metric-live:") for _, kind, value in hits)
    assert any("5/5 samples" in L.row_text(row) for row in rows)


def test_virtual_dashboard_has_identical_card_rows_and_total_at_every_scroll_window(dashboard, monkeypatch):
    dashboard.result["series"] = {f"metric-{i:02}": dashboard.points for i in range(24)}
    dashboard.app.analysis_state["expanded"] = ["metric-07"]
    dashboard.app.analysis_state["pinned"] = ["metric-05"]
    dashboard.result.update(errors=["one report warning"], truncated=True)
    full_rows, full_hits = research_frame(dashboard, height=None)
    total = dashboard.app.research_rows
    metric_rows = {value: y for y, kind, value in full_hits if kind == "research_metric"}
    assert total == 3 + sampling_tool_rows(full_hits) + 24 * 11 + 5 + 1 + 2
    calls, raster = [], A.charts.braille_chart
    monkeypatch.setattr(A.charts, "braille_chart", lambda *args, **kwargs: (calls.append(1), raster(*args, **kwargs))[1])
    dashboard.app.research_document_mode = True
    for start in (0, 36, 143, total - 20):
        dashboard.app.research_document_window = (start, start + 20)
        rows, hits = research_frame(dashboard, height=24)
        assert dashboard.app.research_rows == total
        assert len(rows) == len(full_rows)
        assert {value: y for y, kind, value in hits if kind == "research_metric"} == metric_rows
        assert len(calls) <= 8  # Bounded visible cards, never all 24 per window.
        calls.clear()


@pytest.mark.parametrize("width", [23, 120])
@pytest.mark.parametrize("unsupported", [False, True])
def test_hidden_or_unidentifiable_controls_do_not_reserve_phantom_document_rows(dashboard, width, unsupported):
    dashboard.result["series"] = {f"metric-{i:02}": dashboard.points for i in range(12)}
    if unsupported:
        dashboard.result["path"] = "/" + "long-source/" * 60
    full_rows, full_hits = research_frame(dashboard, width, height=None)
    full_count = dashboard.app.research_rows
    expected_control_rows = int(width >= M.MIN_WIDTH and not unsupported)
    assert full_count == 3 + sampling_tool_rows(full_hits) + 12 * (10 + expected_control_rows)
    dashboard.app.research_document_mode = True
    dashboard.app.research_document_window = (40, 60)
    rows, hits = research_frame(dashboard, width, height=20)
    assert dashboard.app.research_rows == full_count and len(rows) == len(full_rows)
    assert [(y, key) for y, kind, key in hits if kind == "research_metric"] == [
        (y, key) for y, kind, key in full_hits if kind == "research_metric"]


@pytest.mark.parametrize("ascii_", [False, True])
def test_modal_controls_align_with_box_content_and_cursor_keeps_exact_samples(dashboard, monkeypatch, ascii_):
    dashboard.now[0] = 209.
    dashboard.views.g = L.Glyphs(ascii_)
    research_frame(dashboard)
    A.run_command(dashboard.app, ["chart", "loss"])
    modal_frame(dashboard)
    records = M.initialize(dashboard.app)["records"]
    assert len(records) == 1 and records[0].layer == 1
    identity = records[0].key
    assert M.set_enabled(dashboard.app, identity, True)
    rendered = modal_frame(dashboard)
    record = M.initialize(dashboard.app)["records"][0]
    line = next((x, L.row_text(row)) for y, x, row in rendered if y == record.toggle.top)
    assert line[0] + line[1].index("[Live ON ") == record.toggle.left
    plot = C.initialize(dashboard.app)["plots"][0]
    assert plot.visible.top >= record.visible.bottom + 1
    assert plot.x_bounds == (169.0, 199.0)
    A.handle_key(dashboard.app, "end")
    rendered = modal_frame(dashboard)
    assert dashboard.app.analysis_state["cursor_t"] == 199.0
    assert dashboard.app.analysis_state["chart_visible"]["points"][-1]["step"] == 4
    assert any("value 3.123456789012345" in L.row_text(row) for _, _, row in rendered)
    before = tuple(tuple(row) for _, _, row in rendered)
    monkeypatch.setattr(dashboard.store, "snapshot", lambda: pytest.fail("Hover requested a new scheduler snapshot"))
    monkeypatch.setattr(A.charts, "braille_chart", lambda *args, **kwargs: pytest.fail("Hover rasterized"))
    C.hover(dashboard.app, plot.visible.top, plot.visible.left + 2)
    C.feedback(dashboard.app, ascii_=ascii_)
    assert tuple(tuple(row) for _, _, row in rendered) == before


def test_historical_modal_has_no_live_controls_even_while_another_job_runs(dashboard):
    research_frame(dashboard)
    dashboard.app.analysis_result_job = "2"
    dashboard.app.analysis_state.update(modal="chart", chart_job="2", metric="loss")
    dashboard.app.mode = "analysis"
    rows = modal_frame(dashboard)
    assert not M.initialize(dashboard.app)["records"]
    assert not any("[Live" in L.row_text(row) for _, _, row in rows)
    assert dashboard.app.analysis_state["chart_visible"]["points"][-1]["t"] == 203.0


@pytest.mark.parametrize("in_place", [False, True])
def test_reported_live_does_not_continue_across_a_new_scheduler_attempt(dashboard, in_place):
    current = dashboard.store.jobs[0]
    current.submit, current.start = "submit-S", "start-A"
    research_frame(dashboard)
    before = M.initialize(dashboard.app)["records"][0].key
    assert before[4] == "scheduler:submit-S|start-A"
    assert M.set_enabled(dashboard.app, before, True)
    if in_place:
        current.start = "start-B"
    else:
        dashboard.store.jobs = [Job("1", "current", "gpu", "RUNNING", submit="submit-S", start="start-B")]
    research_frame(dashboard)
    after = M.initialize(dashboard.app)["records"][0].key
    assert after[4] == "scheduler:submit-S|start-B" and after != before
    assert not M.enabled(dashboard.app, before) and not M.enabled(dashboard.app, after)
    assert M.window(dashboard.app, after) is None


def test_declared_run_attempt_stays_authoritative_and_changes_are_scoped(dashboard):
    app = dashboard.app
    app.project_state.update(binding={"job_id": "1", "project_root": "/exact/project", "run_id": "run-1", "attempt": 4})
    research_frame(dashboard)
    before = M.initialize(app)["records"][0].key
    assert before[4] == 4 and M.set_enabled(app, before, True)
    dashboard.store.jobs[0].start = "new-observation"
    research_frame(dashboard)
    assert M.initialize(app)["records"][0].key == before and M.enabled(app, before)
    app.project_state["binding"]["attempt"] = 5
    research_frame(dashboard)
    after = M.initialize(app)["records"][0].key
    assert after[4] == 5 and after != before
    assert not M.enabled(app, before) and not M.enabled(app, after)


def test_published_attempt_key_lookup_reuses_exact_slot_without_snapshot_and_detects_slot_replacement(dashboard, monkeypatch):
    app, store = dashboard.app, dashboard.store
    first = A.chart_key(app, "loss", dashboard.result["path"])
    monkeypatch.setattr(store, "snapshot", lambda: pytest.fail("Key requested a full scheduler snapshot"))
    store.jobs[0].start = "observed-start-A"
    second = A.chart_key(app, "loss", dashboard.result["path"])
    assert second != first and second[4] == "scheduler:|observed-start-A"
    store.jobs[0] = Job("1", "replacement-slot", "gpu", "RUNNING", start="observed-start-B")
    third = A.chart_key(app, "loss", dashboard.result["path"])
    assert third != second and third[4] == "scheduler:|observed-start-B"


def test_inline_reported_live_has_one_canonical_graph_node_per_button_and_shared_state(dashboard):
    from tower import job_panels as J
    app = dashboard.app
    app.tab, app.research_view = "jobs", "passport"
    assert J.run_command(app, ["jobpanel", "research", "experiment"])
    rows, hits = dashboard.views.compose(dashboard.store.snapshot(), app, 180, 70)
    records = M.initialize(app)["records"]
    assert len(records) == 1 and records[0].key[1] == "1"
    graph = app.interaction_state["graph"]
    live = [control for control in graph.controls if control.action[0] == "command" and
            control.action[1].startswith(("metric-live ", "metric-window "))]
    assert len(live) == 2
    assert len({(control.rect, control.action) for control in live}) == 2
    assert all(control.id.startswith(("metric-live:", "metric-window:")) for control in live)
    toggle = next(control for control in live if control.id.startswith("metric-live:"))
    assert "Live" in L.row_text(rows[toggle.rect.top])[toggle.rect.left:toggle.rect.right]
    app.click(toggle.rect.top, toggle.rect.left, hits)
    assert M.enabled(app, records[0].key)
    assert app.research_view == "passport" and app.tab == "jobs" and app.research_job_id == "1"
    dashboard.views.compose(dashboard.store.snapshot(), app, 180, 70)
    plots = C.initialize(app)["plots"]
    assert any(plot.key == records[0].key and plot.x_bounds == (160.0, 190.0) for plot in plots)
