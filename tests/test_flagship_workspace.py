"""Cross-module UI regressions with real recording and project evidence."""
from copy import deepcopy
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from raven import background_tasks, flagship_commands, job_panels, layout as L, operations, ops_ui, ops_workspace, trace_ui
from raven.trace_recorder import Recorder
from test_job_panels import dashboard
from test_operations_sequences import operation_context, operation_dashboard


@pytest.fixture(autouse=True)
def connected_dashboard(request):
    if "dashboard" in request.fixturenames:
        from raven.slurm import FakeBackend, Slurm
        app = request.getfixturevalue("dashboard").app
        app.actions = SimpleNamespace(slurm=Slurm(FakeBackend(), "test"))


def test_all_task_commands_are_registered_and_search_keeps_campaign_search(operation_dashboard):
    known = {item["key"] for item in operations.catalog()}
    for task in flagship_commands.TASKS:
        assert set(task.operations) <= known
        operation_dashboard.app.run_command("workbench " + task.key)
        operation_dashboard.draw()
        assert operation_dashboard.app.ops_state["catalog_task"] == task.key
    app = operation_dashboard.app
    app.run_command("ops search")
    assert app.ops_state["feature"] == "search" and app.ops_state["view"] == "form"
    app.run_command("ops find")
    assert app.ops_state["view"] == "catalog" and app.ops_state["catalog_search"]


def test_catalog_filter_does_not_dispatch_tab_keys_or_stale_controls(operation_dashboard):
    app = operation_dashboard.app
    app.run_command("workbench investigate")
    operation_dashboard.draw()
    app.handle("/")
    for character in "trace":
        app.handle(character)
    assert app.tab == "jobs" and app.mode == "operations"
    assert app.ops_state["catalog_query"] == "trace"
    assert all("trace" in (item["title"] + item["summary"] + item["key"] + item["group"]).casefold()
               for item in ops_ui._catalog_specs(app.ops_state))
    app.handle("enter")
    assert not app.ops_state["catalog_search"]
    operation_dashboard.draw()
    app.handle("enter")
    assert app.ops_state["feature"] in {"trace-inspect", "causal-delay", "collective-audit"}


def test_trace_read_resolves_exact_attempt_and_retains_full_frames(dashboard, tmp_path):
    app = dashboard.app
    dashboard.render()
    job = dashboard.store.jobs[0]
    job.submit, job.start = "2026-10-10T10:00:00", "2026-10-10T10:01:00"
    root = dashboard.paths[job.id][0].parent
    with Recorder(root, job_id=job.id, attempt=job.submit + "/" + job.start) as recorder:
        recorder.emit("call", {"function": "long_" + "界" * 50, "file": "app.py", "line": 1, "frame": "f"})
        recorder.flush()
        context = trace_ui._context(app)
        data = trace_ui._read(context, "", 0, "", None, Event())
        assert data["verified"] and data["path"] == str(recorder.path)
        job_panels._activate(app, "trace")
        state = trace_ui.initialize(app)
        state.update(context=context, result=data["snapshot"], sources=data["sources"], verified=True, status="ready")
        rows, _ = trace_ui.panel(dashboard.views, app, job, 22)
        assert all(L.vlen(L.row_text(row)) <= 22 for row in rows)
        assert "long_" in "".join(L.row_text(row) for row in rows)
        wrong = dict(context, start="later", submit="later")
        assert not trace_ui._read(wrong, "", 0, "", None, Event())["sources"]


def test_trace_redraw_never_labels_previous_jobs_frames_as_current(dashboard):
    dashboard.render()
    app = dashboard.app
    state = trace_ui.initialize(app)
    state.update(context=trace_ui._context(app), result={"rows": ["PRIVATE_FRAMES_900"]}, status="ready", verified=True)
    app.selected_id = "700"
    rows, _ = trace_ui.panel(dashboard.views, app, dashboard.store.finished[0], 50)
    assert "PRIVATE_FRAMES_900" not in L.to_text(rows, 50)
    assert "waiting for job evidence" in L.to_text(rows, 50)


@pytest.mark.parametrize("change", ["job", "tab", "mode", "cancel", "attempt"])
def test_late_trace_callback_cannot_restore_old_selection_or_change_page(dashboard, monkeypatch, change):
    app = dashboard.app
    dashboard.render()
    job_panels._activate(app, "trace")
    queued = []
    hub = SimpleNamespace(lock=Lock(), closed=False, pending=None, future=None,
        start_task=lambda work, done: queued.append((work, done)) or True,
        cancel_task=lambda callback: None)
    monkeypatch.setattr(background_tasks, "research_hub", lambda *args: hub)
    trace_ui.tick(app)
    assert len(queued) == 1
    if change == "job":
        app.selected_id = "700"
    elif change == "tab":
        app.tab = "research"
    elif change == "mode":
        app.mode = "operations"
    elif change == "attempt":
        dashboard.store.jobs[0].submit = "new attempt"
    else:
        trace_ui.cancel(app, clear=True)
    before = (app.tab, app.mode, app.selected_id)
    queued[0][1]({"sources": [], "snapshot": {"rows": ["STALE"]}, "reader": None, "path": "old", "verified": True})
    assert trace_ui.initialize(app)["result"] is None
    assert (app.tab, app.mode, app.selected_id) == before


def test_trace_paint_does_not_open_files_or_collect(dashboard, monkeypatch):
    dashboard.render()
    job_panels._activate(dashboard.app, "trace")
    def forbidden(*args, **kwargs):
        raise AssertionError("Paint opened an evidence source")
    monkeypatch.setattr(trace_ui, "_read", forbidden)
    monkeypatch.setattr(background_tasks, "research_hub", forbidden)
    for width, height in ((42, 14), (80, 24), (180, 45)):
        _, rows, _ = dashboard.render(width, height)
        assert all(L.vlen(L.row_text(row)) <= width for row in rows)


def test_unchanged_trace_paint_reuses_wrapping_and_unknown_connection_stays_readable(dashboard, monkeypatch):
    dashboard.render()
    app = dashboard.app
    state = trace_ui.initialize(app)
    state.update(context=trace_ui._context(app), result={"rows": ["a frame" * 40]}, verified=True)
    job = dashboard.store.jobs[0]
    first = trace_ui.panel(dashboard.views, app, job, 45)
    def forbidden(*args):
        raise AssertionError("Unchanged pointer paints rebuilt the trace document")
    monkeypatch.setattr(trace_ui, "_wrap", forbidden)
    assert trace_ui.panel(dashboard.views, app, job, 45) is first
    app.actions = None
    assert trace_ui._context(app) is None


def test_wrapped_trace_controls_keep_hit_rows_and_strip_terminal_escape():
    rows = [[("first_" * 8, "text")], [("button", "accent")], [("\x1b[2Jbad界", "warning")]]
    wrapped, hits = trace_ui._wrap(rows, [(1, "button", (0, 6))], 8)
    assert L.row_text(wrapped[hits[0][0]]) == "button"
    assert all(L.vlen(L.row_text(row)) <= 8 for row in wrapped)
    assert "\x1b" not in L.to_text(wrapped, 8)


def test_project_guide_initializes_only_after_review_and_preserves_existing_data(operation_context, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    user_file = project / "research.txt"
    user_file.write_text("user results")
    result = operations.run("project-guide", {"root": str(project), "initialize": "yes"}, operation_context)
    assert not (project / "raven-project.json").exists()
    applied = operations.apply("project-guide", result["plan"], operation_context)
    assert applied["status"] == "ok" and user_file.read_text() == "user results"
    result = operations.run("project-guide", {"root": str(project), "initialize": "no"}, operation_context)
    assert "--root" in "\n".join(result["rows"])


def test_timeline_never_implies_cross_clock_causality_or_combines_attempts():
    base = dict(id="1", source="application", clock="node-a", kind="metric", message="start", job_id="81", attempt="a", cluster="test", timestamp=10)
    doc = {"schema": "raven.timeline/v1", "events": [base, dict(base, id="2", clock="node-b", timestamp=1)]}
    result = ops_workspace.timeline(doc, "81")
    assert len(result["warnings"]) == 2
    changed = deepcopy(doc)
    changed["events"][1]["attempt"] = "b"
    with pytest.raises(ValueError, match="multiple attempts"):
        ops_workspace.timeline(changed, "81")
    doc["events"][0]["timestamp"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        ops_workspace.timeline(doc, "81")
