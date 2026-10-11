"""Exact-job Details tabs stay live, bounded and independent from full Logs."""
from __future__ import annotations

import copy
import json
from threading import Event, current_thread
from types import SimpleNamespace

import pytest

from tower import job_panels as J, layout as L, workspace_layout as W
from tower.config import Config
from tower.controller import App
from tower.model import Finished, Job, Live, Step, Store
from tower.remote import LocalFiles
from tower.research import ResearchHub
from tower.views import Views


@pytest.fixture
def dashboard(tmp_path):
    cfg = Config()
    cfg.set("workspace", {"density": "compact", "split": 50})
    cfg.set("log_lines", 0)
    store = Store(persist=False)
    store.jobs = [Job("900", "active-only", "cpu", "RUNNING", cpus=4, mem_req="4G")]
    store.finished = [Finished("700", "failed-only", "FAILED", cpus=2, exit="1:0", elapsed="00:00:20")]
    store.live["900"] = Live(rss=1024 ** 3, rate=.5)
    paths = {}
    for job in store.jobs + store.finished:
        root = tmp_path / job.id
        root.mkdir()
        out, err, extra = root / "stdout.log", root / "stderr.log", root / "application.log"
        out.write_text(f"AB_OUTPUT_{job.id}\n")
        err.write_text(f"CD_ERROR_{job.id}: RuntimeError failed\n")
        extra.write_text(f"EXTRA_{job.id}\n")
        manifest = root / "logs.json"
        manifest.write_text(json.dumps({"schema": "tower.logs/v1", "job_id": job.id,
            "logs": [{"id": "app", "path": "application.log", "label": "Application"}]}))
        store.details[job.id] = {"StdOut": str(out), "StdErr": str(err), "WorkDir": str(root)}
        paths[job.id] = (out, err, extra)
    cfg.set("logs", {"manifest_file": "logs.json"})
    app = App(store, None, None, cfg, "test", interactive=False)
    views = Views(L.Glyphs(False), cfg, files=LocalFiles())
    app.views_ref, app.logs.files = views, views.files
    result = SimpleNamespace(app=app, views=views, store=store, paths=paths)
    def render(width=160, height=32):
        rows, hits = views.compose(store.snapshot(), app, width, height)
        return L.to_text(rows, width), rows, hits
    result.render = render
    yield result
    if app.research:
        app.research.close()


def button(hits, name):
    return next((y, value) for y, kind, value in hits if kind == "job_panel_tab" and value[0] == name)


def test_default_compact_jobs_uses_second_column_and_exact_bounded_buttons(dashboard):
    text, _, hits = dashboard.render()
    assert "Main" in text and "Details" in text
    assert "Allocation  4 CPUs" in text
    assert "Overview" in text and "Resources" in text
    rect = dashboard.app.job_panel_rect
    assert rect.x > 0
    for name in J.MODES:
        y, (_, left, right) = button(hits, name)
        assert rect.x <= left < right <= rect.x + rect.width
        assert rect.y <= y < rect.y + rect.height
    jobs = [(y, value) for y, kind, value in hits if kind == "job"]
    assert jobs and jobs[0][1] == "900"


def test_clicked_tab_arrow_cycle_keeps_job_and_esc_returns_to_rows(dashboard):
    app = dashboard.app
    _, _, hits = dashboard.render()
    y, (_, left, _) = button(hits, "logs")
    before = copy.deepcopy(app.cursor)
    assert J.handle_mouse(app, y, left)
    assert J.initialize(app)["mode"] == "logs"
    assert J.handle_key(app, "right")
    assert J.initialize(app)["mode"] == "investigate"
    assert J.handle_key(app, "down")
    assert J.initialize(app)["mode"] == "research"
    assert J.handle_key(app, "down")
    assert J.initialize(app)["mode"] == "analytics"
    assert J.handle_key(app, "down")
    assert J.initialize(app)["mode"] == "quick"
    assert J.handle_key(app, "down")
    assert J.initialize(app)["mode"] == "trace"
    assert J.handle_key(app, "down")
    assert J.initialize(app)["mode"] == "off"
    assert J.handle_key(app, "left")
    assert J.initialize(app)["mode"] == "trace"
    assert J.handle_key(app, "left")
    assert J.initialize(app)["mode"] == "quick"
    assert J.handle_key(app, "left")
    assert J.initialize(app)["mode"] == "analytics"
    assert app.cursor == before and app.tab == "jobs" and app.mode == "main"
    assert J.handle_key(app, "esc")
    assert not J.handle_key(app, "down")
    assert app.layout_state.focus == "main"


def test_off_shows_buttons_without_content_or_file_worker_io(dashboard, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Off started a reader")
    monkeypatch.setattr(J, "_worker", forbidden)
    monkeypatch.setattr(dashboard.views.files, "stat", forbidden)
    monkeypatch.setattr(dashboard.views.files, "tail", forbidden)
    assert J.run_command(dashboard.app, ["jobpanel", "off"])
    text, rows, hits = dashboard.render()
    assert all(button(hits, name) for name in J.MODES)
    right = dashboard.app.job_panel_rect
    panel = "\n".join(L.row_text(row)[right.x:] for row in rows[right.y:right.y + right.height])
    assert "Allocation" not in panel and "active-only" not in panel
    assert "Logs" in panel and "Off" in panel
    assert dashboard.app.research is None


def test_live_and_recent_selection_update_without_changing_details_mode(dashboard):
    app, store = dashboard.app, dashboard.store
    J.run_command(app, ["jobpanel", "inspector"])
    text, _, _ = dashboard.render()
    assert "Job 900" in text
    app.cursor["jobs"] = 1
    text, _, _ = dashboard.render()
    assert app.selected_id == "700" and "Job 700" in text
    assert J.initialize(app)["mode"] == "inspector"
    assert "Exit" in text and "1:0" in text
    # Existing identity remains selected when a new active job arrives.
    store.jobs.insert(0, Job("901", "new-job", "cpu", "PENDING"))
    text, _, _ = dashboard.render()
    assert app.selected_id == "700" and "Job 700" in text
    assert app.tab == "jobs" and app.mode == "main"


def test_inspector_does_not_change_full_modal_or_log_selections(dashboard):
    app = dashboard.app
    app.analysis_state.update(job="other", section=3, modal="inspect", scroll=17)
    app.log_job = "other"
    app.logs.path, app.logs.top = "/other/log", 13
    before = copy.deepcopy(app.analysis_state)
    dashboard.render()
    assert app.analysis_state == before
    assert (app.log_job, app.logs.path, app.logs.top) == ("other", "/other/log", 13)


def test_inspector_retains_live_rank_diagnostics_dependencies_tags_and_notes(dashboard):
    store = dashboard.store
    store.tags["900"] = {"tags": ["urgent", "paper"], "note": "rerun with 32 cores"}
    store.jobs.append(Job("901", "follow-up", "cpu", "PENDING", dependency="afterok:900"))
    store.steps["900"] = [Step("900.0", name="0", ntasks=8, cpu_time=3000,
        rss=2 * 1024 ** 3, rss_task="3", rss_node="node-b", min_cpu=1200,
        min_cpu_task="3", min_cpu_node="node-b")]
    text, _, _ = dashboard.render(width=150, height=60)
    assert "step 0        8 tasks" in text
    assert "peak 2.0 GB on task 3@node-b" in text
    assert "slowest rank 3@node-b at 40% of the mean" in text
    assert "1 job wait for this one: 901(follow-up)" in text
    assert "tags #urgent #paper" in text and "Tags urgent paper" in text
    assert "rerun with 32 cores" in text


def test_inline_logs_exact_recent_sources_preserve_first_chars_and_full_log_state(dashboard):
    app = dashboard.app
    app.cursor["jobs"] = 1
    app.log_job = "900"
    app.logs.path, app.logs.top, app.logs.search = str(dashboard.paths["900"][0]), 13, "old search"
    original = (app.log_job, app.logs.path, app.logs.top, app.logs.search)
    J.run_command(app, ["jobpanel", "logs"])
    text, _, hits = dashboard.render()
    assert "AB_OUTPUT_700" in text and "AB_OUTPUT_900" not in text
    state = J.initialize(app)
    assert len(state["entries"]) == 3
    file_hit = next((y, value) for y, kind, value in hits if kind == "job_panel_file" and value[0] == "scheduler.stderr")
    assert J.handle_mouse(app, file_hit[0], file_hit[1][1])
    text, _, _ = dashboard.render()
    assert "CD_ERROR_700" in text and "AB_OUTPUT_700" not in text
    assert J.handle_key(app, "right")
    text, _, _ = dashboard.render()
    assert "EXTRA_700" in text
    assert (app.log_job, app.logs.path, app.logs.top, app.logs.search) == original
    assert app.tab == "jobs" and app.mode == "main"
    assert state["session"].max_bytes == J.MAX_LOG_BYTES


def test_log_content_scroll_and_end_follow_do_not_move_job_cursor(dashboard):
    out = dashboard.paths["900"][0]
    out.write_text("".join(f"LINE_{i:04}\n" for i in range(100)))
    J.run_command(dashboard.app, ["jobpanel", "logs"])
    dashboard.render()
    state = J.initialize(dashboard.app)
    assert J.handle_key(dashboard.app, "enter")
    assert J.handle_key(dashboard.app, "home")
    text, _, _ = dashboard.render()
    assert "LINE_0000" in text
    assert dashboard.app.cursor["jobs"] == 0
    assert J.handle_key(dashboard.app, "end")
    text, _, _ = dashboard.render()
    assert "LINE_0099" in text and state["session"].top is None
    with out.open("a") as output:
        output.write("LINE_NEW\n")
    text, _, _ = dashboard.render()
    assert "LINE_NEW" in text


def test_investigate_uses_selected_recent_job_and_leaves_research_selection(dashboard):
    app = dashboard.app
    app.cursor["jobs"] = 1
    app.research_job_id, app.research_view = "900", "experiment"
    J.run_command(app, ["jobpanel", "investigate"])
    text, _, _ = dashboard.render()
    assert "Job 700" in text and "CD_ERROR_700" in text
    state = J.initialize(app)
    assert state["evidence_job"] == "700" and state["evidence"]
    assert app.research_job_id == "900" and app.research_view == "experiment"
    assert app.tab == "jobs" and app.mode == "main"


def test_shared_filesystem_logs_do_not_block_frame_or_start_parallel_workers(dashboard):
    entered, release = Event(), Event()
    class SlowFiles(LocalFiles):
        def listdir(self, path):
            assert current_thread().name.startswith("tower-research")
            entered.set()
            assert release.wait(3)
            return super().listdir(path)
        def stat(self, path):
            assert current_thread().name.startswith("tower-research")
            return super().stat(path)
        def read(self, path, offset, length):
            assert current_thread().name.startswith("tower-research")
            return super().read(path, offset, length)
    app, files = dashboard.app, SlowFiles()
    app.interactive = True
    dashboard.views.files = files
    app.logs.files = files
    app.research = ResearchHub(app.cfg, files)
    J.run_command(app, ["jobpanel", "logs"])
    try:
        text, _, _ = dashboard.render()
        assert "Waiting" in text and entered.wait(1)
        future = app.research.future
        for _ in range(20):
            dashboard.render()
            assert app.research.future is future
        release.set()
        future.result(timeout=3)
        app.research.poll_task()
        dashboard.render()
        app.research.future.result(timeout=3)
        app.research.poll_task()
        text, _, _ = dashboard.render()
        assert "AB_OUTPUT_900" in text
    finally:
        release.set()


def test_details_background_click_does_not_select_same_y_left_job(dashboard):
    _, _, hits = dashboard.render()
    y = next(y for y, kind, _ in hits if kind == "job")
    rect = dashboard.app.job_panel_rect
    assert J.contains(dashboard.app, y, rect.x + rect.width - 1)
    before = dashboard.app.selected_id
    assert J.handle_mouse(dashboard.app, y, rect.x + rect.width - 1)
    assert dashboard.app.selected_id == before


def test_real_app_routes_fresh_mouse_hits_and_arrows_to_details_buttons(dashboard):
    app = dashboard.app
    _, _, hits = dashboard.render()
    y, (_, left, _) = button(hits, "logs")
    app.last_hits = []  # The actual click receives the current renderer map.
    app.click(y, left, hits)
    assert J.initialize(app)["mode"] == "logs"
    assert app.layout_state.focus == "details"
    app.handle("right")
    assert J.initialize(app)["mode"] == "investigate"
    app.handle("end")
    assert J.initialize(app)["mode"] == "off"
    app.handle("esc")
    app.handle("down")
    dashboard.render()
    assert app.selected_id == "700" and app.tab == "jobs"


def test_requested_rate_reaches_independent_log_readers_without_changing_base(dashboard):
    from tower.refresh_rate import set_multiplier
    app = dashboard.app
    set_multiplier(app, 50)
    J.run_command(app, ["jobpanel", "logs"])
    dashboard.render()
    state = J.initialize(app)
    assert state["session"].polling_multiplier == 50
    assert state["catalog"].polling_multiplier == 50
    assert state["catalog"].ttl == 5
    set_multiplier(app, 1)
    dashboard.render()
    assert state["session"].polling_multiplier == state["catalog"].polling_multiplier == 1
    assert state["catalog"].ttl == 5


@pytest.mark.parametrize("width,height,ascii_", [(40, 22, True), (80, 22, False), (110, 28, False),
                                                 (160, 32, False), (1, 1, True), (0, 0, True)])
def test_resize_ascii_and_focus_keep_rows_and_hits_bounded(dashboard, width, height, ascii_):
    dashboard.views.set_ascii(ascii_)
    if ascii_:
        dashboard.app.set_theme("reader")
    text, rows, hits = dashboard.render(width, height)
    assert len(rows) <= height
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    assert all(0 <= y < height for y, _, _ in hits)
    for _, kind, value in hits:
        if kind in ("job_panel_tab", "job_panel_file"):
            assert 0 <= value[1] < value[2] <= width
    if ascii_:
        assert text.isascii()
    if width >= 40 and height >= 22:
        assert all(button(hits, name) for name in J.MODES)
    if width:
        W.initialize(dashboard.app).focus = "details"
        W.initialize(dashboard.app).maximized = True
        text, _, hits = dashboard.render(width, height)
        assert all(kind != "job" for _, kind, _ in hits)


def test_restore_accepts_only_known_mode_and_does_not_restore_live_state():
    app = SimpleNamespace()
    J.restore(app, {"mode": "logs", "job": "old", "focus": "tabs", "file_id": "old-file"})
    assert J.save(app) == {"mode": "logs"}
    assert J.initialize(app)["job"] is None and J.initialize(app)["focus"] == ""
    J.restore(app, {"mode": "../bad"})
    assert J.save(app) == {"mode": "logs"}


def test_tick_requests_finished_details_for_recent_selection_and_off_does_not(dashboard):
    selected = []
    dashboard.app.sampler = SimpleNamespace(select_fin=selected.append, select=lambda jid: None,
                                           select_trace=lambda jid: None)
    dashboard.app.selected_id = "700"
    dashboard.app.tick()
    assert selected == ["700"]
    J.initialize(dashboard.app)["mode"] = "off"
    dashboard.app.tick()
    assert selected == ["700", None]


@pytest.fixture
def published_workspaces(dashboard, monkeypatch):
    app = dashboard.app
    app.research = ResearchHub(app.cfg, dashboard.views.files)
    reports = {
        "experiment": {"status": "ok", "path": "/exact/job/900/metrics.jsonl", "records": 2,
                       "series": {"loss": [{"t": 1, "value": .75}, {"t": 2, "value": .25}]}},
        "arrays": {"status": "ok", "groups": []},
        "evidence": {"status": "ok", "summary": "Exact job observation", "evidence": [
            {"id": "E1", "source": "scheduler", "text": "Measured state from job 900"}]},
        "artifacts": {"status": "ok", "valid": True, "summary": "Valid outputs", "outputs": [
            {"path": "results/final-output.json", "status": "valid", "checks": [{"name": "shape", "status": "pass", "message": "MATCH"}]}]},
        "passport": {"status": "ok", "passport": {"schema": "tower.passport/v1", "job_id": "900"}},
        "submit": {"status": "ok", "plan": {"valid": True, "command": "sbatch not-executed.sh", "resources": {}, "issues": []}},
        "predict": {"status": "ok", "job_id": "900", "metrics": {"runtime_seconds": {
            "estimate": 42, "lower": 30, "upper": 60, "samples": 5, "unit": "s"}}},
        "forecast": {"status": "ok", "predicted_start": 1700000000, "evidence": ["Confirmed scheduler observation"]},
        "blockers": {"status": "ok", "summary": "No blockers", "blockers": []},
        "tradeoffs": {"status": "ok", "candidates": []},
        "scaling": {"status": "ok", "points": []},
        "workflow": {"status": "ok", "nodes": []},
    }
    requests = []
    def request(context, **kwargs):
        requests.append(context)
        return reports[context["view"]]
    monkeypatch.setattr(app.research, "request", request)
    dashboard.reports, dashboard.requests = reports, requests
    dashboard.render()
    return dashboard


@pytest.mark.parametrize("group,view", [("research", key) for key, _ in J._choices("research")]
                         + [("analytics", key) for key, _ in J._choices("analytics")])
@pytest.mark.parametrize("width,height,ascii_", [(40, 80, True), (80, 50, False),
                                               (120, 60, False), (200, 60, False)])
def test_all_seventeen_inline_views_fit_and_leave_global_workspaces_unchanged(published_workspaces, group, view, width, height, ascii_):
    dashboard, app = published_workspaces, published_workspaces.app
    dashboard.views.set_ascii(ascii_)
    if ascii_:
        app.set_theme("reader")
    app.research_view, app.analytics_view, app.analytics_job = "passport", "timeline", "700"
    app.research_scroll, app.research_array_open, app.research_task_offset = 17, True, 48
    app.log_job, app.logs.path, app.logs.top = "700", "/separate/log", 13
    before = (app.tab, app.research_view, app.analytics_view, app.analytics_job,
              app.research_scroll, app.research_array_open, app.research_task_offset,
              app.log_job, app.logs.path, app.logs.top, copy.deepcopy(app.analysis_state), dict(app.cursor))
    assert J.run_command(app, ["jobpanel", group, view])
    text, rows, hits = dashboard.render(width, height)
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    assert len(rows) <= height
    assert text.isascii() if ascii_ else True
    if dashboard.app.job_panel_rect.width >= 30:
        assert "Job 900" in text
    assert all(0 <= y < height for y, _, _ in hits)
    for _, kind, value in hits:
        if kind.startswith("job_panel_"):
            assert 0 <= value[1] < value[2] <= width
    after = (app.tab, app.research_view, app.analytics_view, app.analytics_job,
             app.research_scroll, app.research_array_open, app.research_task_offset,
             app.log_job, app.logs.path, app.logs.top, app.analysis_state, app.cursor)
    assert after == before
    assert any(kind == "job_panel_view" and value[0] == group + ":" + view for _, kind, value in hits)
    for context in dashboard.requests:
        assert context["jid"] == context["explicit_jid"] == "900"
        assert context["job"].id == "900"


def test_every_inline_view_button_is_clickable_and_subviews_arrow_cycle(published_workspaces):
    dashboard, app = published_workspaces, published_workspaces.app
    for group in ("research", "analytics"):
        J.run_command(app, ["jobpanel", group])
        for name, _ in J._choices(group):
            _, _, hits = dashboard.render(200, 80)
            y, _, (target, left, right) = next(hit for hit in hits
                if hit[1] == "job_panel_view" and hit[2][0] == group + ":" + name)
            assert J.handle_mouse(app, y, (left + right - 1) // 2)
            assert J.initialize(app)[group + "_view"] == name
        assert J.handle_key(app, "home")
        assert J.initialize(app)[group + "_view"] == J._choices(group)[0][0]
        assert J.handle_key(app, "end")
        assert J.initialize(app)[group + "_view"] == J._choices(group)[-1][0]


def test_inline_recent_analytics_job_never_falls_back_to_running_job(published_workspaces):
    dashboard, app = published_workspaces, published_workspaces.app
    app.cursor["jobs"] = 1
    app.analytics_job = "900"
    J.run_command(app, ["jobpanel", "analytics", "job"])
    text, _, _ = dashboard.render(200, 60)
    assert "Job 700 / failed-only" in text
    assert "no samples recorded for this job yet" in text
    assert app.analytics_job == "900"
    assert J.initialize(app)["view_states"]["analytics:job"]["proxy"].analytics_job == "700"


def test_inline_long_documents_scroll_per_view_and_reset_on_job_change(published_workspaces):
    dashboard, app = published_workspaces, published_workspaces.app
    dashboard.reports["artifacts"]["outputs"] = [{"path": f"results/output-{index:04}.json",
        "status": "valid", "checks": [{"name": "shape", "status": "pass", "message": f"CHECK_{index:04}"}]} for index in range(180)]
    J.run_command(app, ["jobpanel", "research", "artifacts"])
    text, _, _ = dashboard.render(180, 50)
    assert "CHECK_0000" in text and "CHECK_0179" not in text
    J.handle_key(app, "enter")
    J.handle_key(app, "end")
    text, _, hits = dashboard.render(180, 50)
    assert "CHECK_0179" in text
    assert any(kind == "job_panel_tab" for _, kind, _ in hits)
    last = app.layout_state.scroll["jobs:details"]
    J.run_command(app, ["jobpanel", "analytics", "history"])
    dashboard.render(180, 50)
    assert app.layout_state.scroll["jobs:details"] == 0
    J.run_command(app, ["jobpanel", "research", "artifacts"])
    dashboard.render(180, 50)
    assert app.layout_state.scroll["jobs:details"] == last
    app.cursor["jobs"] = 1
    dashboard.render(180, 50)
    assert app.selected_id == "700"
    assert app.layout_state.scroll["jobs:details"] == 0


def test_inline_metric_mouse_opens_exact_job_chart_without_changing_research_selection(published_workspaces):
    dashboard, app = published_workspaces, published_workspaces.app
    app.research_view, app.research_job_id = "passport", "900"
    J.run_command(app, ["jobpanel", "research", "experiment"])
    _, _, hits = dashboard.render(180, 80)
    y, _, (target, left, _) = next(hit for hit in hits
        if hit[1] == "job_panel_action" and hit[2][0][0] == "research_metric")
    assert J.handle_mouse(app, y, left)
    assert app.mode == "analysis" and app.analysis_state["modal"] == "chart"
    assert app.analysis_state["metric"] == "loss"
    assert app.analysis_state["chart_job"] == "900"
    assert app.research_view == "passport" and app.tab == "jobs"
    assert app.logs.path == ""


def test_inline_research_detaches_mismatched_run_files_and_never_exposes_manual_submit_plan(published_workspaces):
    dashboard, app = published_workspaces, published_workspaces.app
    app.research_job_id = "700"
    J.run_command(app, ["jobpanel", "research", "experiment"])
    text, _, _ = dashboard.render(180, 60)
    assert "another run's files remain detached" in text
    assert not dashboard.requests
    app.research_job_id = "900"
    app.research.plan = {"valid": True, "command": "MUST_NOT_APPEAR"}
    J.run_command(app, ["jobpanel", "research", "submit"])
    text, _, _ = dashboard.render(180, 60)
    assert "no linked submission report" in text and "MUST_NOT_APPEAR" not in text
    assert not dashboard.requests


def test_inline_metrics_virtualize_all_sixty_four_cards_and_jump_to_last(published_workspaces, monkeypatch):
    from tower import analysis_ui
    dashboard, app = published_workspaces, published_workspaces.app
    dashboard.reports["experiment"]["series"] = {
        f"metric_{index:02}": [{"t": 1, "value": index}, {"t": 2, "value": index + 1}]
        for index in range(64)}
    rendered = []
    original = analysis_ui.chart_rows
    def chart(*args, **kwargs):
        rendered.append(args[5])
        return original(*args, **kwargs)
    monkeypatch.setattr(analysis_ui, "chart_rows", chart)
    J.run_command(app, ["jobpanel", "research", "experiment"])
    dashboard.render(180, 40)
    assert "metric_63" not in rendered
    assert len(rendered) < 30
    rendered.clear()
    J.handle_key(app, "enter")
    J.handle_key(app, "end")
    text, _, _ = dashboard.render(180, 40)
    assert "metric_63" in text and "metric_63" in rendered
    assert len(rendered) < 30
    assert app.layout_state.sizes["jobs:details"][0] > 500


def test_inline_project_metrics_fill_old_empty_viewport_on_first_publication(published_workspaces, monkeypatch):
    from tower import analysis_ui, chart_interaction
    dashboard, app = published_workspaces, published_workspaces.app
    app.interactive = True
    dashboard.reports["experiment"]["series"] = {}
    J.run_command(app, ["jobpanel", "research", "experiment"])
    dashboard.render(320, 160)
    assert not chart_interaction.initialize(app)["plots"]
    dashboard.reports["experiment"]["series"] = {
        f"metric_{index:02}": [{"t": 1, "value": index}, {"t": 2, "value": index + 1}]
        for index in range(64)}
    rendered = []
    original = analysis_ui.chart_rows
    def chart(*args, **kwargs):
        rendered.append(args[5])
        return original(*args, **kwargs)
    monkeypatch.setattr(analysis_ui, "chart_rows", chart)
    dashboard.render(320, 160)
    assert chart_interaction.initialize(app)["plots"]
    assert "metric_09" in rendered, "New cards must fill the previously empty lower part of a tall viewport"
    assert 0 < len(rendered) < 30, "A growing source should fill its viewport without rasterizing all cards"


def test_inline_evidence_click_opens_cited_log_for_exact_job(published_workspaces):
    dashboard, app = published_workspaces, published_workspaces.app
    dashboard.reports["evidence"]["evidence"] = [{"id": "E1", "source": "stderr", "text": "RuntimeError failed",
        "path": str(dashboard.paths["900"][1]), "line": 1, "line_basis": "original"}]
    J.run_command(app, ["jobpanel", "research", "evidence"])
    _, _, hits = dashboard.render(180, 80)
    y, _, (target, left, _) = next(hit for hit in hits
        if hit[1] == "job_panel_action" and hit[2][0][0] == "research_evidence")
    assert J.handle_mouse(app, y, left)
    assert app.tab == "log" and app.log_job == "900"
    assert app.logs.entry["path"] == str(dashboard.paths["900"][1])
    text, _, _ = dashboard.render(180, 80)
    assert app.logs.path == str(dashboard.paths["900"][1])
    assert "CD_ERROR_900" in text


def test_inline_reports_use_new_published_snapshots_without_restart(published_workspaces):
    dashboard, app = published_workspaces, published_workspaces.app
    J.run_command(app, ["jobpanel", "research", "artifacts"])
    text, _, _ = dashboard.render(180, 60)
    assert "MATCH" in text
    dashboard.reports["artifacts"]["outputs"][0]["checks"][0]["message"] = "NEW_PUBLISHED_VALUE"
    text, _, _ = dashboard.render(180, 60)
    assert "NEW_PUBLISHED_VALUE" in text and "MATCH" not in text
    assert J.initialize(app)["research_view"] == "artifacts"
    assert app.selected_id == "900"


def test_inline_narrow_compare_preserves_exact_identity_and_measured_values(published_workspaces):
    dashboard, app = published_workspaces, published_workspaces.app
    app.store.series["900"].append({"t": 1, "k": "live", "cpu": .73, "rss": 3 * 1024 ** 3})
    app.store.series["900"].append({"t": 2, "k": "live", "cpu": .87, "rss": 3.5 * 1024 ** 3})
    app.compare_ids = ["700"]
    J.run_command(app, ["jobpanel", "analytics", "compare"])
    text, _, _ = dashboard.render(40, 150)
    assert "Job 900 / active-only" in text and "Job 700 / failed-only" in text
    assert "mean 80% / max 87%" in text
    assert "Peak memory 3.5 GB / 88% of request" in text
    assert app.compare_ids == ["700"]


def test_inspector_file_links_and_browser_open_exact_recent_job(dashboard):
    dashboard.app.cursor["jobs"] = 1
    J.run_command(dashboard.app, ["jobpanel", "inspector"])
    _, _, hits = dashboard.render(200, 110)
    y, _, (target, left, _) = next(hit for hit in hits if hit[1] == "job_panel_action"
        and hit[2][0][0] == "inline_log" and hit[2][0][1]["id"] == "StdErr")
    assert J.handle_mouse(dashboard.app, y, left)
    assert dashboard.app.tab == "log" and dashboard.app.log_job == "700"
    assert dashboard.app.logs.entry["path"] == str(dashboard.paths["700"][1])


def test_inspector_evidence_link_stays_in_jobs_and_opens_exact_inline_workspace(dashboard):
    J.run_command(dashboard.app, ["jobpanel", "inspector"])
    _, _, hits = dashboard.render(200, 110)
    y, _, (target, left, _) = next(hit for hit in hits if hit[1] == "job_panel_action"
        and hit[2][0][0] == "inline_evidence")
    assert J.handle_mouse(dashboard.app, y, left)
    assert dashboard.app.tab == "jobs" and dashboard.app.selected_id == "900"
    assert J.initialize(dashboard.app)["mode"] == "research"
    assert J.initialize(dashboard.app)["research_view"] == "evidence"


def test_investigation_cited_files_are_clickable_for_exact_selected_job(dashboard):
    dashboard.app.cursor["jobs"] = 1
    J.run_command(dashboard.app, ["jobpanel", "investigate"])
    _, _, hits = dashboard.render(200, 110)
    y, _, (target, left, _) = next(hit for hit in hits if hit[1] == "job_panel_action"
        and hit[2][0][0] == "inline_log")
    assert target[1]["job"] == "700"
    assert J.handle_mouse(dashboard.app, y, left)
    assert dashboard.app.tab == "log" and dashboard.app.log_job == "700"
    assert dashboard.app.logs.entry["path"] == target[1]["path"]


def test_wrapped_inline_link_keeps_every_character_and_all_rows_clickable():
    source = " /project/with/a/very/long/path/that/remains/readable/worker-errors.log"
    target = ("inline_log", {"job": "700", "id": "E1", "path": source.strip()})
    rows, hits = W._reflow([[(source, "cyan")]], [(0, "job_panel_action", (target, 0, 20))], 20)
    assert "".join(L.row_text(row) for row in rows).replace(" ", "") == source.replace(" ", "")
    assert len(hits) == len(rows) > 1
    assert all(kind == "job_panel_action" and value[0] == target for _, kind, value in hits)


@pytest.mark.parametrize("width,height", [(160, 45), (80, 55), (40, 70)])
def test_history_native_details_are_fitted_bounded_and_exact(dashboard, width, height):
    app = dashboard.app
    app.enter_tab("history")
    text, rows, hits = dashboard.render(width, height)
    assert app.selected_id == "700"
    assert "Job 700" in text
    assert "active-only" not in text
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    assert all(button(hits, name) for name in J.MODES)
    rect = app.job_panel_rect
    assert rect is not None
    for _, kind, value in hits:
        if kind.startswith("job_panel_"):
            assert rect.x <= value[1] < value[2] <= rect.x + rect.width
    assert app.history_jobs_rect is not None
    assert not app.history_jobs_rect.contains(rect.y + 1, rect.x + 2)
    assert all(control.rect.right <= app.workspace_main_rect.x + app.workspace_main_rect.width
               for control in app.interaction_state["graph"].controls if control.group == "fin")


def test_history_mode_and_preferences_are_independent_from_jobs(dashboard):
    app = dashboard.app
    J.run_command(app, ["jobpanel", "analytics", "compare"])
    dashboard.render()
    app.enter_tab("history")
    dashboard.render()
    assert J.initialize(app)["mode"] == "inspector"
    J.run_command(app, ["jobpanel", "logs"])
    dashboard.render()
    assert J.save(app) == {"mode": "analytics", "analytics_view": "compare", "history": {"mode": "logs"}}
    app.enter_tab("jobs")
    dashboard.render()
    assert J.initialize(app)["mode"] == "analytics"
    assert J.initialize(app)["analytics_view"] == "compare"
    restored = SimpleNamespace(tab="jobs")
    J.restore(restored, J.save(app))
    assert J.initialize(restored)["mode"] == "analytics"
    restored.tab = "history"
    assert J.initialize(restored)["mode"] == "logs"
    assert J.initialize(restored)["job"] is None


def test_history_logs_keep_exact_files_after_multiselect_and_right_clear(dashboard):
    app = dashboard.app
    dashboard.store.finished.append(Finished("701", "completed", "COMPLETED"))
    app.enter_tab("history")
    dashboard.render()
    app.cursor["history"] = app.last_history_ids.index("700")
    app.marks = {"700", "701"}
    app.log_job = "900"
    J.run_command(app, ["jobpanel", "logs"])
    text, _, hits = dashboard.render(200, 60)
    assert "AB_OUTPUT_700" in text and "AB_OUTPUT_900" not in text
    assert app.log_job == "900" and app.selected_id == "700"
    y, (_, left, _) = next((y, value) for y, kind, value in hits
                          if kind == "job_panel_file" and value[0] == "scheduler.stderr")
    app.click(y, left, hits, button="left")
    text, _, hits = dashboard.render(200, 60)
    assert "CD_ERROR_700" in text and "AB_OUTPUT_700" not in text
    rect = app.job_panel_rect
    app.click(rect.y + rect.height - 1, rect.x + 2, hits, button="right")
    text, _, _ = dashboard.render(200, 60)
    assert not app.marks and app.selected_id is None and app.mode == "main"
    assert "Select a job in the list" in text
    assert "CD_ERROR_700" not in text and "AB_OUTPUT_900" not in text


@pytest.mark.parametrize("group,view", [("research", name) for name, _ in J._choices("research")]
                         + [("analytics", name) for name, _ in J._choices("analytics")])
def test_history_all_inline_views_use_exact_finished_job(published_workspaces, group, view):
    dashboard, app = published_workspaces, published_workspaces.app
    app.enter_tab("history")
    dashboard.render()
    dashboard.requests.clear()
    app.analytics_job, app.research_job_id, app.log_job = "900", "900", "900"
    assert J.run_command(app, ["jobpanel", group, view])
    text, rows, hits = dashboard.render(180, 52)
    assert app.selected_id == "700" and "Job 700" in text
    assert app.tab == "history" and app.mode == "main"
    assert (app.analytics_job, app.research_job_id, app.log_job) == ("900", "900", "900")
    assert all(L.vlen(L.row_text(row)) <= 180 for row in rows)
    assert any(kind == "job_panel_view" and value[0] == group + ":" + view for _, kind, value in hits)
    assert all(context["jid"] == context["explicit_jid"] == context["job"].id == "700"
               for context in dashboard.requests)
    if group == "analytics" and view == "job":
        assert "no samples recorded for this job yet" in text
        assert J.initialize(app)["view_states"]["analytics:job"]["proxy"].analytics_job == "700"


def test_history_off_and_passive_controls_do_not_request_io(dashboard, monkeypatch):
    app = dashboard.app
    app.enter_tab("history")
    dashboard.render()
    J.run_command(app, ["jobpanel", "off"])
    _, _, hits = dashboard.render()
    def forbidden(*args, **kwargs):
        raise AssertionError("Passive History controls requested source work")
    for name in ("stat", "tail"):
        monkeypatch.setattr(dashboard.views.files, name, forbidden)
    monkeypatch.setattr(J, "_worker", forbidden)
    monkeypatch.setattr(app.store, "snapshot", forbidden)
    for name in J.MODES:
        y, (_, left, _) = button(hits, name)
        for event in ("motion", "drag", "release"):
            app.click(y, left, hits, button=event)
    assert app.tab == "history" and app.mode == "main"
    assert J.initialize(app)["mode"] == "off" and app.research is None


def test_history_finished_sampling_and_trace_targets_follow_selected_id(dashboard):
    app = dashboard.app
    app.enter_tab("history")
    dashboard.render()
    finished, traces = [], []
    app.sampler = SimpleNamespace(select_fin=finished.append, select=lambda jid: None,
                                  select_trace=traces.append)
    J.run_command(app, ["jobpanel", "analytics", "job"])
    app.tick()
    assert finished == ["700"] and traces == ["700"]
    J.run_command(app, ["jobpanel", "off"])
    app.tick()
    assert finished == ["700", None] and traces == ["700", None]


def test_old_details_controls_are_rejected_between_page_switch_and_frame(dashboard):
    app = dashboard.app
    _, _, hits = dashboard.render()
    y, (_, left, _) = button(hits, "logs")
    app.enter_tab("history")
    assert not J.contains(app, y, left)
    assert not J.handle_mouse(app, y, left)
    assert J.initialize(app)["mode"] == "inspector"


def test_selected_id_change_preserves_pane_boundary_but_rejects_old_file_action(dashboard):
    app = dashboard.app
    app.enter_tab("history")
    dashboard.render()
    J.run_command(app, ["jobpanel", "logs"])
    _, _, hits = dashboard.render(200, 60)
    y, (_, left, _) = next((y, value) for y, kind, value in hits
                          if kind == "job_panel_file" and value[0] == "scheduler.stderr")
    previous = J.initialize(app)["file_id"]
    app.selected_id = "another-job"
    assert J.contains(app, y, left)  # Selection changes must never expose table hits under Details.
    assert J.handle_mouse(app, y, left)
    assert J.initialize(app)["file_id"] == previous


def test_narrow_history_retains_tag_badge_without_duplicate_mark_columns(dashboard):
    app = dashboard.app
    app.enter_tab("history")
    dashboard.store.tags["700"] = {"tags": ["old"], "note": "Keep this run for comparison"}
    dashboard.store.finished[0].state = "OUT_OF_MEMORY"
    text, rows, hits = dashboard.render(190, 45)
    y = next(y for y, kind, value in hits if kind == "fin" and value == "700")
    table_row = L.row_text(rows[y])[:app.workspace_main_rect.width]
    assert "old" in table_row and "Keep this run for comparison" in text
    assert table_row.index("700") == 4


def test_history_long_details_scrolls_use_history_viewport_and_sticky_buttons(published_workspaces):
    dashboard, app = published_workspaces, published_workspaces.app
    app.enter_tab("history")
    dashboard.render()
    dashboard.reports["artifacts"]["outputs"] = [
        {"path": f"results/{index:04}.json", "status": "valid",
         "checks": [{"name": "shape", "status": "pass", "message": f"PAST_{index:04}"}]}
        for index in range(180)]
    J.run_command(app, ["jobpanel", "research", "artifacts"])
    app.layout_state.scroll["jobs:details"] = 17
    text, _, _ = dashboard.render(180, 50)
    assert "PAST_0000" in text and "PAST_0179" not in text
    J.handle_key(app, "enter")
    J.handle_key(app, "end")
    text, _, hits = dashboard.render(180, 50)
    assert "PAST_0179" in text
    assert app.layout_state.scroll["jobs:details"] == 17
    position = app.layout_state.scroll["history:details"]
    assert position > 0 and all(button(hits, name) for name in J.MODES)
    J.run_command(app, ["jobpanel", "inspector"])
    dashboard.render(180, 50)
    assert app.layout_state.scroll["history:details"] == 0
    J.run_command(app, ["jobpanel", "research", "artifacts"])
    dashboard.render(180, 50)
    assert app.layout_state.scroll["history:details"] == position
