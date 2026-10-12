"""Project bindings stay truthful through normal workbench commands and navigation."""
import json
import threading

import pytest

from raven import project_ui
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text
from raven.model import Job, Store
from raven.research import ResearchHub
from raven.views import Views


@pytest.fixture
def project_dashboard(tmp_path):
    root = tmp_path / "actual project"
    run = root / "runs" / "observed-a1"
    (run / "logs").mkdir(parents=True)
    (root / ".raven/contracts").mkdir(parents=True)
    (root / ".raven/contracts/outputs.v1.json").write_text(json.dumps({"version": 1, "outputs": [{"path": "run.json", "format": "json"}]}))
    (run / "logs/stdout.log").write_bytes(b"first\tline\r\nsecond line\r\nthird line\r\n")
    (run / "logs/worker.log").write_bytes(b"worker\tactual\r\n")
    (run / "logs/stderr.log").write_text("actual stderr\n")
    (run / "metrics.jsonl").write_text('{"schema":"raven.metric/v1","t":1,"values":{"loss":0.2}}\n')
    (run / "run.json").write_text(json.dumps({"schema": "raven.run/v1", "run_id": "observed-a1", "experiment_id": "actual", "attempt": 1, "state": "COMPLETED", "paths": {"metrics": "metrics.jsonl", "log_index": "logs.json", "stdout": "logs/stdout.log", "stderr": "logs/stderr.log"}}))
    (run / "logs.json").write_text(json.dumps({"schema": "raven.logs/v1", "run_id": "observed-a1", "logs": [{"id": "worker", "path": "logs/worker.log", "label": "Worker output", "group": "Application"}]}))
    cfg = Config({"clipboard": {"osc52": False, "tools": False}, "log_lines": 0})
    store = Store(persist=False)
    first = Job("11", "unrelated live job", "main", "RUNNING")
    second = Job("22", "GPU observed job", "gpu", "PENDING")
    store.apply_jobs([first, second])
    other = tmp_path / "unrelated-job.log"
    other.write_text("unrelated job output\n")
    store.details["11"] = {"StdOut": str(other)}
    # Binding/navigation assertions use synchronous log observations; slow
    # interactive filesystems are covered in test_local_log_snapshots.py.
    app = App(store, None, None, cfg, "integration-tester", interactive=False)
    app.state_dir = str(tmp_path / "private-state")
    app.research = ResearchHub(cfg)
    app.files = app.logs.files = app.research.files
    views = Views(Glyphs(True), cfg, files=app.files)
    app.views_ref = views
    try:
        yield app, views, root, run
    finally:
        app.research.close()


def settle(app):
    if app.research.pending:
        app.research.future.result(timeout=5)
        app.tick()


def bind(app, root):
    app.run_command("project " + repr(str(root)))
    settle(app)
    app.run_command("run select observed-a1")
    settle(app)
    assert project_ui.selected_binding(app)["run_id"] == "observed-a1"


def render(app, views):
    rows, hits = views.compose(app.store.snapshot(), app, 120, 28)
    return "\n".join(row_text(row) for row in rows)


def test_bound_run_without_scheduler_id_renders_declared_logs_and_exact_copy(project_dashboard):
    app, views, root, run = project_dashboard
    bind(app, root)
    app.enter_tab("log")
    text = render(app, views)
    assert "observed-a1" in text and "first" in text
    assert "unrelated job output" not in text
    app.handle("Y")
    settle(app)
    _, task = app.activity.snapshot()
    assert task["status"] == "ready"
    notices, _ = app.activity.snapshot()
    export = next(notice["path"] for notice in reversed(notices) if notice.get("path"))
    assert __import__("pathlib").Path(export).read_bytes() == (run / "logs/stdout.log").read_bytes()


def test_standalone_run_browser_opens_worker_and_returns_with_identity(project_dashboard):
    app, views, root, run = project_dashboard
    bind(app, root)
    app.enter_tab("log")
    render(app, views)
    app.handle("O")
    text = render(app, views)
    assert app.logs.browser and "Worker output" in text
    entries = app.log_entries()
    app.logs.browser_cursor = next(index for index, item in enumerate(entries) if item["path"] == str(run / "logs/worker.log"))
    app.handle("enter")
    text = render(app, views)
    assert "worker" in text and "actual" in text and not app.logs.browser
    app.handle("esc")
    render(app, views)
    assert app.logs.browser
    assert project_ui.selected_binding(app)["run_id"] == "observed-a1"
    assert app.log_job is None


def test_standalone_stderr_toggle_copies_exact_stream_before_redraw(project_dashboard):
    app, views, root, run = project_dashboard
    bind(app, root)
    app.enter_tab("log")
    render(app, views)
    app.handle("e")
    # Copy must resolve the new stream even before a frame publishes its path.
    app.handle("Y")
    settle(app)
    notices, _ = app.activity.snapshot()
    export = next(notice["path"] for notice in reversed(notices) if notice.get("path"))
    assert __import__("pathlib").Path(export).read_bytes() == (run / "logs/stderr.log").read_bytes()
    assert "actual stderr" in render(app, views)


def test_standalone_cycle_reaches_declared_worker_log(project_dashboard):
    app, views, root, run = project_dashboard
    bind(app, root)
    app.enter_tab("log")
    render(app, views)
    observed = []
    for _ in range(len(project_ui.log_entries(app))):
        app.handle("o")
        render(app, views)
        observed.append(app.logs.path)
    assert str(run / "logs/worker.log") in observed


def test_declared_job_id_without_accounting_record_remains_visible(project_dashboard):
    app, views, root, run = project_dashboard
    inventory = json.loads((run / "run.json").read_text())
    inventory["job_id"] = "12345_6"
    (run / "run.json").write_text(json.dumps(inventory))
    bind(app, root)
    app.enter_tab("log")
    text = render(app, views)
    assert "12345_6" in text
    assert "job not recorded" not in text
    assert "first" in text


def test_standalone_keyboard_selection_copies_source_bytes(project_dashboard):
    app, views, root, run = project_dashboard
    bind(app, root)
    app.enter_tab("log")
    render(app, views)
    for key in ("home", "down", "v", "down", "y"):
        app.handle(key)
    content = __import__("pathlib").Path(app.state_dir) / "clipboard.txt"
    assert content.read_bytes() == b"second line\r\nthird line\r\n"


def test_open_other_job_clears_run_and_restores_prior_attachment(project_dashboard):
    app, views, root, run = project_dashboard
    app.research.configure(metrics_file="/explicit/manual/metrics")
    bind(app, root)
    app.open_log("11")
    assert project_ui.selected_binding(app) is None
    assert app.research.settings["metrics_file"] == "/explicit/manual/metrics"
    assert "unrelated job output" in render(app, views)


def test_back_from_other_job_restores_bound_run_sources(project_dashboard):
    app, views, root, run = project_dashboard
    bind(app, root)
    app.enter_tab("log")
    render(app, views)
    app.open_log("11")
    render(app, views)
    app.run_command("back")
    text = render(app, views)
    assert project_ui.selected_binding(app)["run_id"] == "observed-a1"
    assert app.research.settings["workdir"] == str(run)
    assert "first" in text and "unrelated job output" not in text


def test_enter_research_on_other_job_drops_previous_run_identity(project_dashboard):
    app, views, root, run = project_dashboard
    bind(app, root)
    app.enter_tab("jobs")
    render(app, views)
    app.selected_id = "11"
    app.cursor["jobs"] = app.visible_ids.index("11")
    app.enter_tab("research")
    assert project_ui.selected_binding(app) is None
    assert app.research_job_id == "11"
    context = app.research.context(app.store.snapshot(), app)
    assert context["jid"] == "11" and context["run_id"] is None


def test_saved_table_filters_columns_and_sort_restore_together(project_dashboard):
    app, views, root, run = project_dashboard
    app.run_command("facet jobs state=PENDING partition=gpu")
    app.run_command("columns jobs hide cpu% mem%")
    app.sort["jobs"], app.reverse["jobs"] = "name", True
    app.run_command("savedview jobs save 'GPU waiting'")
    render(app, views)
    assert app.visible_ids == ["22"]
    app.run_command("facet jobs clear")
    app.run_command("columns jobs reset")
    app.sort["jobs"], app.reverse["jobs"] = "state", False
    app.enter_tab("history")
    app.run_command("savedview jobs load 'GPU waiting'")
    render(app, views)
    assert app.tab == "jobs" and app.visible_ids == ["22"]
    assert app.sort["jobs"] == "name" and app.reverse["jobs"] is True
    assert app.table_state["hidden"]["jobs"] == ["cpu%", "mem%"]


def test_cancel_copy_from_activity_cleans_partial_export(project_dashboard, monkeypatch):
    app, views, root, run = project_dashboard
    bind(app, root)
    app.enter_tab("log")
    render(app, views)
    (run / "logs/stdout.log").write_bytes(b"x" * (3 * 1024 * 1024))
    import raven.log_copy as log_copy
    reached, release = threading.Event(), threading.Event()
    original = log_copy.os.read

    def pause_read(fd, length):
        data = original(fd, length)
        if length == 1024 * 1024 and not reached.is_set():
            reached.set()
            if not release.wait(5):
                raise RuntimeError("test copy release timed out")
        return data

    monkeypatch.setattr(log_copy.os, "read", pause_read)
    try:
        app.handle("Y")
        assert reached.wait(5)
        app.run_command("activity")
        app.handle("c")
        release.set()
        settle(app)
        _, task = app.activity.snapshot()
        assert task["cancel"].is_set() and task["status"] == "error"
        assert "cancel" in app.message.lower()
        export_dir = __import__("pathlib").Path(app.state_dir) / "exports"
        assert not export_dir.exists() or not list(export_dir.iterdir())
    finally:
        release.set()
