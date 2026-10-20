"""Research controls preserve exact observations, bounded work and navigation."""
from collections import deque
from types import SimpleNamespace

import pytest
import time

from raven import analysis_ui as A, layout as L
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Live, Store
from raven.research import ResearchHub
from raven.research_views import render
from raven.views import Views


@pytest.fixture(autouse=True)
def deterministic_timezone(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()
    yield
    monkeypatch.undo()
    if hasattr(time, "tzset"):
        time.tzset()


@pytest.fixture
def app():
    cfg = Config()
    store = Store(persist=False)
    store.jobs = [Job("1", "training", "gpu", "RUNNING", cpus=8, gpus=1, mem_req="4G")]
    store.finished = [Finished("2", "previous", "FAILED", cpus=4, req_mem=2 * 1024 ** 3)]
    store.live["1"] = Live(rss=1024 ** 3, rate=.5)
    app = App(store, None, None, cfg, "researcher")
    app.selected_id = "1"
    A.initialize(app)
    app.research = ResearchHub(cfg)
    app.analysis_result = {"path": "reported.jsonl", "series": {
        "loss": [{"t": i, "value": v, "step": i} for i, v in enumerate([4., 3., None, 1.])],
        "accuracy": [{"t": i, "value": .1 * i} for i in range(4)]}}
    app.analysis_result_job = "1"
    app.analysis_result_generation = app.research.generation
    yield app
    app.research.close()


def text(rows):
    return "\n".join(L.row_text(row) for row in rows)


def overlay(app, width=110, height=28, ascii_=False):
    rows = A.overlay(SimpleNamespace(g=L.Glyphs(ascii_)), app.store.snapshot(), app, width, height)
    return "\n".join(L.row_text(row) for _, _, row in rows or [])


def test_dashboard_persistence_does_not_restore_live_modal_or_selected_job(app):
    for command in (["dashboard", "pin", "loss"], ["dashboard", "hide", "accuracy"],
                    ["dashboard", "expand", "loss"], ["dashboard", "color", "loss", "green"]):
        assert A.run_command(app, command)
    A.open_inspector(app, "2")
    saved = A.save(app)
    clone = SimpleNamespace()
    A.restore(clone, saved)
    assert A.dashboard_names(clone, {"accuracy": [], "loss": []}) == ["loss"]
    assert clone.analysis_state["colors"] == {"loss": "green"}
    assert clone.analysis_state["expanded"] == ["loss"]
    assert clone.analysis_state["modal"] == "" and "job" not in clone.analysis_state


def test_restore_rejects_untrusted_names_and_bounds_every_collection():
    app = SimpleNamespace()
    A.restore(app, {"analysis": {"pinned": ["bad\x1bname", {}, "x" * 97] + [str(i) for i in range(100)],
                                 "hidden": "invalid", "colors": {"good": "cyan", "bad": "bogus"}}})
    assert len(app.analysis_state["pinned"]) <= 64
    assert "bad\x1bname" not in app.analysis_state["pinned"]
    assert app.analysis_state["hidden"] == []
    assert app.analysis_state["colors"] == {"good": "cyan"}


def test_dashboard_move_keeps_pin_priority_and_hidden_metric_can_return(app):
    A.run_command(app, ["dashboard", "move", "accuracy", "1"])
    assert A.dashboard_names(app, app.analysis_result["series"]) == ["accuracy", "loss"]
    A.run_command(app, ["dashboard", "pin", "loss"])
    assert A.dashboard_names(app, app.analysis_result["series"]) == ["loss", "accuracy"]
    A.run_command(app, ["dashboard", "hide", "loss"])
    assert A.dashboard_names(app, app.analysis_result["series"]) == ["accuracy"]
    A.run_command(app, ["dashboard", "show", "loss"])
    assert A.dashboard_names(app, app.analysis_result["series"])[0] == "loss"


@pytest.mark.parametrize("command", [["dashboard", "move", "loss", "0"], ["dashboard", "move", "absent", "2"],
                                      ["dashboard", "color", "loss", "bogus"], ["chart", "zoom", "inf"],
                                      ["chart", "pan", "-1"], ["chart", "cursor", "1.1"],
                                      ["chart", "missing"], ["inspect", "unknown"], ["diff", "1", "unknown"]])
def test_invalid_analysis_commands_do_not_open_unrelated_data(app, command):
    A.run_command(app, command)
    assert not app.command_ok
    assert app.mode == "main"


@pytest.mark.parametrize("ascii_", [False, True])
def test_crosshair_reports_original_precision_timestamp_step_and_unknown_value(app, ascii_):
    A.run_command(app, ["chart", "loss"])
    A.handle_key(app, "right")
    rendered = overlay(app, ascii_=ascii_)
    assert "value 3" in rendered and "step 1" in rendered and "1970-01-01" in rendered
    A.handle_key(app, "right")
    assert "value unavailable" in overlay(app, ascii_=ascii_)
    from raven import palette
    theme = getattr(getattr(app, "views", None), "theme", "default")
    assert any(palette.resolve(style, theme).foreground == palette.resolve("yellow", theme).foreground
               and "bold" in palette.resolve(style, theme).flags
               for _, _, row in A.overlay(SimpleNamespace(g=L.Glyphs(ascii_)), app.store.snapshot(), app, 110, 28)
               for _, style in row)
    if ascii_:
        assert rendered.isascii()


def test_chart_viewport_preserves_missing_samples_and_bounds_zoom_pan(app):
    points = app.analysis_result["series"]["loss"]
    A.run_command(app, ["chart", "loss"])
    A.handle_key(app, "+")
    visible, times = A.viewport(points, app.analysis_state)
    assert times == (0, 1.5)
    assert [p["t"] for p in visible] == [0, 1]
    A.run_command(app, ["chart", "pan", "1"])
    visible, times = A.viewport(points, app.analysis_state)
    assert times == (1.5, 3)
    assert visible[0]["value"] is None
    for _ in range(100):
        A.handle_key(app, "+")
        A.handle_key(app, "]")
    assert app.analysis_state["zoom"] == 1024 and app.analysis_state["pan"] == 1


def test_chart_identity_stays_fixed_after_selected_job_changes(app):
    A.run_command(app, ["chart", "loss"])
    app.selected_id = "2"
    assert A.chart_data(app, app.store.snapshot())[2] == "1"
    A.handle_key(app, "esc")
    assert A.chart_data(app, app.store.snapshot())[2] == "2"


def test_chart_window_and_metric_switch_preserve_source_timestamp(app):
    A.run_command(app, ["chart", "loss"])
    A.handle_key(app, "right")
    A.handle_key(app, "tab")
    assert app.analysis_state["metric"] == "accuracy" and app.analysis_state["cursor"] == 1
    A.run_command(app, ["chart", "window", "1"])
    visible, times = A.viewport(app.analysis_result["series"]["accuracy"], app.analysis_state)
    assert times == (2, 3) and [p["t"] for p in visible] == [2, 3]


def test_chart_crosshair_retains_full_numeric_precision(app):
    value = 1.2345678901234567
    app.analysis_result["series"]["precise"] = [{"t": 1.2345678901234567, "value": value}]
    A.run_command(app, ["chart", "precise"])
    assert repr(value) in overlay(app)


def test_zoom_does_not_reinterpret_an_outage_as_a_valid_sampling_cadence(app, monkeypatch):
    points = [{"t": t, "value": 1} for t in (0, 1, 1000, 1001)]
    app.analysis_state.update(zoom=1001 / 999, pan=.5)
    captured = []
    original = A.charts.braille_chart
    def chart(*args, **kwargs):
        captured.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(A.charts, "braille_chart", chart)
    A.chart_rows(L.Glyphs(False), app, points, 80, 4, "outage", "observed")
    assert captured[0]["sample_interval"] == 1
    buckets, _ = A.charts._time_points([1, 1], [1, 1000], 60, (1, 1000), captured[0]["sample_interval"])
    assert not buckets[-1][2]


def test_dashboard_search_is_case_insensitive_and_can_be_cleared(app):
    A.run_command(app, ["dashboard", "search", "LOSS"])
    assert A.dashboard_names(app, app.analysis_result["series"]) == ["loss"]
    A.run_command(app, ["dashboard", "search"])
    assert len(A.dashboard_names(app, app.analysis_result["series"])) == 2


def test_frames_do_not_load_sample_files_from_disk(app, monkeypatch):
    monkeypatch.setattr(app.store, "series_of", lambda jid: pytest.fail("frame attempted a disk load"))
    A.run_command(app, ["diff", "1", "2"])
    overlay(app)
    A.run_command(app, ["timeline"])
    overlay(app)


def test_passport_comparison_runs_in_worker_and_publishes_only_on_tick(app, tmp_path):
    from raven.provenance import capture, save
    left = capture(tmp_path, parameters={"batch_size": 4})
    right = capture(tmp_path, parameters={"batch_size": 8})
    paths = [str(save(left, tmp_path / "passports")), str(save(right, tmp_path / "passports"))]
    A.run_command(app, ["diff", "passport", *paths])
    pending = app.research.pending
    assert pending is not None and app.mode == "main"
    pending[0].result(timeout=5)
    assert app.mode == "main"
    app.research.poll_task()
    assert app.mode == "analysis" and "batch_size" in overlay(app)


def test_inspector_unifies_active_and_historical_jobs_without_scheduler_commands(app):
    assert A.open_inspector(app, "2")
    assert "FAILED" in overlay(app) and "previous" in overlay(app)
    A.handle_key(app, "right")
    assert "4 CPUs" in overlay(app)
    A.handle_key(app, "right")
    assert "No step records" in overlay(app)
    A.handle_key(app, "right")
    assert "StdOut" in overlay(app)
    A.handle_key(app, "right")
    assert "exact job" in overlay(app)
    A.handle_key(app, "esc")
    A.open_inspector(app, "1")
    A.handle_key(app, "right")
    assert "50%" in overlay(app) and "1.0 GB" in overlay(app)


def test_inspector_keeps_exact_job_when_inventory_changes(app):
    A.open_inspector(app, "2")
    app.store.finished = []
    rendered = overlay(app)
    assert "outside the current inventory" in rendered and "Job inspector / 2" in rendered
    assert "training" not in rendered


def test_inspector_log_and_evidence_actions_use_original_job(app, monkeypatch):
    opened = []
    monkeypatch.setattr(app, "open_log", lambda jid: opened.append(jid))
    A.open_inspector(app, "2")
    app.selected_id = "1"
    A.handle_key(app, "l")
    assert opened == ["2"] and app.mode == "main"
    A.open_inspector(app, "2")
    A.handle_key(app, "e")
    assert app.tab == "research" and app.research_view == "evidence" and app.research_job_id == "2"


def test_timeline_is_sorted_deduplicated_and_bounded(app):
    event = {"t": 5, "kind": "completed", "job": "2", "text": "failed"}
    app.store.events = deque([event, event] + [{"t": i, "kind": "sample", "text": str(i)} for i in range(600)])
    events = A.timeline_events(app, app.store.snapshot())
    assert len(events) == 512
    assert [e["t"] for e in events] == sorted(e["t"] for e in events)


def test_reported_phase_events_are_observations_not_invented_history(app):
    result = {"path": "run.jsonl", "phase": "training", "last_t": 10, "series": {}}
    A.observe_metrics(app, result, "1")
    A.observe_metrics(app, result, "1")
    A.observe_metrics(app, dict(result, phase="validation", last_t=20), "1")
    events = A.timeline_events(app, app.store.snapshot())
    assert [e["t"] for e in events] == [10, 20]
    assert "Observed reported phase" in events[0]["text"]


def test_report_ui_copy_cannot_add_phase_events_to_interactive_state(app):
    from raven.report import _ui_copy
    result = {"path": "run.jsonl", "phase": "training", "last_t": 10}
    A.observe_metrics(app, result, "1")
    private = _ui_copy(app)
    A.observe_metrics(private, dict(result, phase="validation", last_t=20), "1")
    assert len(private.analysis_state["metric_events"]) == 2
    assert len(app.analysis_state["metric_events"]) == 1
    assert app.analysis_state["phase_sources"][("1", "run.jsonl")] == "training"


def test_timeline_seek_updates_real_replay_clock_only_when_explicit(app):
    from raven.record import ReplayClock
    clock = ReplayClock(0, 100, paused=True)
    app.replay = SimpleNamespace(clock=clock)
    app.store.events = deque([{"t": 42, "kind": "fail", "text": "failed", "job": "2"}])
    A.run_command(app, ["timeline"])
    assert clock.now() == 0
    A.handle_key(app, "s")
    assert clock.now() == 42
    A.handle_key(app, "enter")
    assert app.analysis_state["modal"] == "inspect" and app.detail_id == "2"
    A.handle_key(app, "esc")
    assert app.analysis_state["modal"] == "timeline" and app.mode == "analysis"


@pytest.mark.parametrize("modal", ["timeline", "chart_events"])
@pytest.mark.parametrize("width", [40, 80, 160])
@pytest.mark.parametrize("ascii_", [False, True])
def test_visible_event_links_open_exact_job_with_mouse(app, modal, width, ascii_):
    jid = "2" if modal == "timeline" else "1"
    app.store.events = deque([{"t": 42, "kind": "failed", "job": jid, "text": "Original job event"}])
    app.analysis_state.update(modal=modal, chart_job=jid, cursor=0, scroll=0)
    app.mode = "analysis"
    A.overlay(SimpleNamespace(g=L.Glyphs(ascii_)), app.store.snapshot(), app, width, 24)
    links = app.analysis_state["control_hits"]
    assert links and all(0 <= value["left"] < value["right"] <= width for _, _, value in links)
    y, _, value = links[-1]
    assert A.handle_mouse(app, y, value["left"], button="left")
    assert app.analysis_state["modal"] == "inspect"
    assert app.analysis_state["job"] == jid and app.detail_id == jid


def test_timeline_open_does_not_seek_and_invalid_index_preserves_context(app):
    from raven.record import ReplayClock
    clock = ReplayClock(0, 100, paused=True)
    app.replay = SimpleNamespace(clock=clock)
    app.store.events = deque([{"t": 42, "kind": "failed", "job": "2", "text": "failed"}])
    A.run_command(app, ["timeline"])
    app.command_ok = True
    A.run_command(app, ["timeline", "open", "2"])
    assert not app.command_ok and app.analysis_state["modal"] == "timeline" and clock.now() == 0
    A.run_command(app, ["timeline", "open", "1"])
    assert app.detail_id == "2" and app.analysis_state["modal"] == "inspect" and clock.now() == 0


def test_event_mouse_target_keeps_painted_identity_when_live_events_change(app):
    app.store.events = deque([{"t": 42, "kind": "failed", "job": "2", "text": "Painted original"}])
    A.run_command(app, ["timeline"])
    A.overlay(SimpleNamespace(g=L.Glyphs(False)), app.store.snapshot(), app, 100, 24)
    y, _, value = app.analysis_state["control_hits"][0]
    app.store.events.appendleft({"t": 1, "kind": "started", "job": "1", "text": "New earlier event"})
    assert A.handle_mouse(app, y, value["left"], button="left")
    assert app.detail_id == "2" and app.analysis_state["job"] == "2"


def test_inspector_drilldown_records_modal_context_before_opening_logs(app, monkeypatch):
    from raven import navigation_ui
    opened = []
    monkeypatch.setattr(app, "open_log", lambda jid: opened.append(jid))
    A.open_inspector(app, "2")
    A.handle_key(app, "right")
    A.handle_key(app, "l")
    captured = app.navigation_state["stack"][-1]
    assert opened == ["2"] and captured.get("analysis_context", {}).get("modal") == "inspect"
    assert captured.get("analysis_context", {}).get("section") == 1
    assert navigation_ui.back(app)
    assert app.mode == "analysis" and app.analysis_state["modal"] == "inspect" and app.analysis_state["section"] == 1


def test_historical_missing_rss_stays_unknown_and_zero_live_measurement_stays_zero(app):
    A.open_inspector(app, "2")
    A.handle_key(app, "right")
    assert "Observed peak RSS unavailable" in overlay(app)
    A.handle_key(app, "esc")
    app.store.live["1"].rss = 0
    A.open_inspector(app, "1")
    A.handle_key(app, "right")
    assert "Observed peak RSS 0 KB" in overlay(app)


def test_job_comparison_highlights_changed_fields_and_measured_curves(app):
    app.store.series["1"].extend([{"t": 10, "k": "live", "cpu": .5, "rss": 100}, {"t": 20, "k": "live", "cpu": None, "rss": None}])
    app.store.series["2"].extend([{"t": 100, "k": "live", "cpu": .25, "rss": 100}, {"t": 110, "k": "live", "cpu": .5, "rss": 200}])
    A.run_command(app, ["diff", "1", "2"])
    rendered = overlay(app, height=100)
    assert "CPUs / changed" in rendered and "CPU per core" in rendered and "aligned measured curves" in rendered
    assert "Nodes / unchanged" not in rendered
    A.handle_key(app, "u")
    assert "Nodes / unchanged" in overlay(app, height=100)


def test_passport_comparison_groups_changes_and_distinguishes_missing_from_null():
    rows = A.passport_rows(L.Glyphs(True), {}, [{"path": "/parameters/rate", "left": None, "right": None,
                                              "left_present": False, "right_present": True}])
    rendered = text(rows)
    assert "Parameters" in rendered and "Before  absent" in rendered and "After   unavailable" in rendered


@pytest.mark.parametrize("modal", ["dashboard", "inspect", "chart", "timeline", "diff"])
@pytest.mark.parametrize("width,height", [(1, 1), (4, 3), (25, 8), (80, 20)])
@pytest.mark.parametrize("ascii_", [False, True])
def test_every_overlay_survives_resize_and_never_emits_controls(app, modal, width, height, ascii_):
    if modal == "inspect":
        A.open_inspector(app, "1")
    elif modal == "diff":
        A.run_command(app, ["diff", "1", "2"])
    else:
        A.run_command(app, [modal])
    rows = A.overlay(SimpleNamespace(g=L.Glyphs(ascii_)), app.store.snapshot(), app, width, height)
    assert all(0 <= y < height and 0 <= x < width and x + L.vlen(L.row_text(row)) <= width for y, x, row in rows)
    assert all(all(ch.isprintable() for ch in L.row_text(row)) for _, _, row in rows)
    if ascii_:
        assert all(L.row_text(row).isascii() for _, _, row in rows)


def test_only_visible_dashboard_charts_are_rasterized_and_scrolling_reaches_metric_64(app, monkeypatch):
    series = {f"metric-{i}": [{"t": 1, "value": i}, {"t": 2, "value": i}] for i in range(64)}
    result = {"status": "ok", "path": "all.jsonl", "series": series}
    hub = SimpleNamespace(context=lambda snap, app: {"job": None, "jid": "1"}, request=lambda context: result)
    app.research, app.research_view, app.research_scroll = hub, "experiment", 0
    calls = []
    original = A.chart_rows
    def chart(*args, **kwargs):
        calls.append(args[5])
        return original(*args, **kwargs)
    monkeypatch.setattr(A, "chart_rows", chart)
    views = SimpleNamespace(g=L.Glyphs(True))
    render(views, {}, app, 80, 20)
    assert len(calls) <= 3
    app.research_scroll = 100000
    calls.clear()
    rows, _ = render(views, {}, app, 80, 20)
    assert len(calls) <= 3 and "metric-63" in text(rows)
    # Restore the real hub so the fixture can close its worker.
    app.research = ResearchHub(app.cfg)


def bind_other_run(app):
    app.project_state["binding"] = {"run_id": "other", "job_id": "2", "run_root": "/bound/run"}
    app.project_state["binding_backup"] = {"settings": {"metrics_file": "/prior/metrics", "contract": "", "workdir": "/prior", "passport": ""},
                                           "log_manifest": "/prior/logs.json", "passport_record": None, "passport_diff": None}
    app.research.configure(metrics_file="/bound/run/metrics", workdir="/bound/run")
    app.tab, app.research_job_id = "research", "2"


def test_manual_source_detaches_before_settings_restore_and_back_restores_bound_run(app):
    from raven import navigation_ui, project_ui
    bind_other_run(app)
    app.run_command("metrics /explicit/manual.jsonl")
    assert app.command_ok and project_ui.selected_binding(app) is None
    assert app.research.settings["metrics_file"] == "/explicit/manual.jsonl"
    assert app.research.settings["workdir"] == "/prior"
    assert navigation_ui.back(app)
    assert project_ui.selected_binding(app)["job_id"] == "2"
    assert app.research.settings["metrics_file"] == "/bound/run/metrics"


def test_immediate_chart_after_manual_attachment_cannot_reuse_previous_file_samples(app):
    assert A.chart_data(app, app.store.snapshot())[1] == "reported.jsonl"
    app.run_command("metrics /explicit/new-source.jsonl")
    series, source, _ = A.chart_data(app, app.store.snapshot())
    assert series == {} and source != "reported.jsonl"


def test_selecting_same_bound_job_keeps_declared_run_sources(app):
    from raven.research import select_job
    from raven.project_ui import selected_binding
    bind_other_run(app)
    select_job(app, "2", view="evidence")
    assert selected_binding(app)["run_id"] == "other"
    assert app.research.settings["metrics_file"] == "/bound/run/metrics"


@pytest.mark.parametrize("route", ["investigate", "keyboard"])
def test_research_job_change_restores_original_sources_before_selecting_other_job(app, route):
    from raven import navigation_ui, project_ui
    bind_other_run(app)
    if route == "investigate":
        app.run_command("investigate 1")
    else:
        app.research_view = "experiment"
        app.handle("up")
    assert app.research_job_id == "1" and project_ui.selected_binding(app) is None
    assert app.research.settings["metrics_file"] == "/prior/metrics"
    assert navigation_ui.back(app)
    assert project_ui.selected_binding(app)["job_id"] == "2"
