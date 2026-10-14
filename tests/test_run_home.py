"""Exact attempt navigation, verified recovery, and reviewed replay end to end."""
import copy
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

from raven import launch, launch_runtime, launch_settings, launch_wizard, run_home, run_home_ui as UI
from raven import notifications as N, notification_ui
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.remote import LocalFiles


class Background:
    demo = False
    closed = False

    def __init__(self):
        self.files = LocalFiles()
        self.pending = None
        self.settings = {}

    def start_task(self, work, done):
        if self.pending:
            return False
        self.pending = work, done
        return True

    def finish(self):
        work, done = self.pending
        self.pending = None
        try:
            result = work()
        except Exception as exc:
            result = exc
        done(result)


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    script = tmp_path / "science.sh"
    script.write_text("#!/bin/sh\nprintf 'retained stdout\\n'\nprintf 'retained stderr\\n' >&2\nexit 0\n")
    plan = launch.resolve_plan([str(script), "--project", str(tmp_path), "--local", "--no-cpu-memory"], environ={})
    receipt = launch_runtime.start_local_plan(plan, environ={"PATH": "/usr/bin:/bin"})
    inventory = Path(receipt["run_dir"]) / "run.json"
    until = time.monotonic()+10
    while time.monotonic() < until:
        if inventory.exists() and json.loads(inventory.read_text()).get("state") == "COMPLETED":
            break
        time.sleep(.02)
    assert json.loads(inventory.read_text())["state"] == "COMPLETED"
    return script, receipt, run_home.load(run_home.receipt_binding(receipt))


def make_app():
    app = App(Store(persist=False), None, None, Config({}), "researcher")
    app.research = Background()
    app.files = app.research.files
    app.width, app.height = 80, 24
    return app


def test_standalone_submission_results_replay_is_readonly_and_exact(recorded):
    script, receipt, result = recorded
    assert result["inventory"]["state"] == "COMPLETED"
    assert result["report"]["receipt"]["token"] == receipt["token"]
    assert result["report"]["receipt"]["application_exit_code"] == 0
    assert len(result["selected"]["logs"]) == 2
    assert Path(receipt["logs"]["stdout"]).read_text() == "retained stdout\n"
    before = set(script.parent.rglob("*"))
    draft = run_home.relaunch_draft(result["report"])
    review = launch_wizard.review(draft, environ={})
    assert review["plan"]["script_sha256"] == result["report"]["plan"]["script_sha256"]
    assert review["plan"]["settings"] == result["report"]["plan"]["settings"]
    assert review["plan"]["execution"] == "local"
    assert set(script.parent.rglob("*")) == before


def test_source_changes_refuse_replay_and_stale_submission(recorded):
    script, _, result = recorded
    draft = run_home.relaunch_draft(result["report"])
    reviewed = launch_wizard.review(draft, environ={})
    script.write_text("#!/bin/sh\nprintf changed\n")
    with pytest.raises(ValueError, match="original script changed"):
        launch_wizard.submit(reviewed, draft, environ={}, runner=lambda *a, **k: pytest.fail("must not submit"))


@pytest.mark.parametrize("field,value", [("attempt", 12), ("job_id", "987"), ("run_id", "other")])
def test_identity_conflicts_do_not_rebind(recorded, field, value):
    _, _, result = recorded
    binding = copy.deepcopy(result["selected"]["binding"])
    binding[field] = value
    with pytest.raises((ValueError, FileNotFoundError)):
        run_home.load(binding)


def test_launch_report_wrong_attempt_does_not_hide_other_results(recorded):
    _, _, result = recorded
    binding = result["selected"]["binding"]
    path = Path(binding["submit_file"])
    report = json.loads(path.read_text())
    report["receipt"]["attempt"] = 99
    path.write_text(json.dumps(report))
    refreshed = run_home.load(binding)
    assert refreshed["report"] is None
    assert "different run attempt" in refreshed["errors"]["launch report"]
    assert refreshed["inventory"]["state"] == "COMPLETED" and len(refreshed["selected"]["logs"]) == 2


def test_replay_preserves_advanced_settings_and_new_scheduler_env_cannot_leak(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    script = tmp_path / "batch.sh"
    script.write_text("#!/bin/sh\n#SBATCH --account=LabX\n#SBATCH --mem=8G\nexit 0\n")
    launch_settings.update_settings(tmp_path, {"launch": {"gpu": False, "cpu_memory": False,
        "parameters": {"optimizer": "adam"}, "telemetry": {"interval_seconds": 7}}})
    plan = launch.resolve_plan([str(script), "--project", str(tmp_path), "--", "two words"], environ={})
    draft = run_home.relaunch_draft({"plan": plan})
    launch_settings.update_settings(tmp_path, {"launch": {"gpu": True, "parameters": {"optimizer": "sgd"}}})
    env = {"SBATCH_ACCOUNT": "OtherLab", "SBATCH_DEPENDENCY": "afterok:123"}
    review = launch_wizard.review(draft, environ=env)
    assert review["plan"]["account"] == "LabX"
    assert "dependency" not in review["plan"]["effective_options"]
    assert review["plan"]["settings"] == plan["settings"]
    assert review["plan"]["script_args"] == ["two words"]
    captured = []
    def scheduler(argv, **options):
        captured.append(options["env"])
        options["stdout"].write(b"935;lab\n")
        return subprocess.CompletedProcess(argv, 0)
    receipt = launch_wizard.submit(review, draft, environ=env, runner=scheduler)
    assert receipt["job_id"] == "935" and len(captured) == 1
    assert not any(key.startswith("SBATCH_") for key in captured[0])


def test_replay_resource_override_is_reviewed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    script = tmp_path / "batch.sh"
    script.write_text("#!/bin/sh\n#SBATCH --account=LabX\n#SBATCH --mem=8G\nexit 0\n")
    plan = launch.resolve_plan([str(script), "--project", str(tmp_path)], environ={})
    draft = run_home.relaunch_draft({"plan": plan})
    draft.update(account="LabY", memory="16 GiB")
    review = launch_wizard.review(draft, environ={})
    assert review["plan"]["account"] == "LabY"
    assert review["plan"]["effective_options"]["mem"] == "16384M"


def test_run_home_completion_keeps_page_selection_and_scroll(recorded):
    _, _, result = recorded
    app = make_app()
    app.selected_id, app.jobs_top = "another-job", 7
    UI.open_run(app, result["selected"]["binding"])
    app.run_home_state.update(top=12, cursor=3)
    app.research.finish()
    assert app.mode == "run_home" and app.selected_id == "another-job" and app.jobs_top == 7
    assert app.run_home_state["top"] == 12 and app.run_home_state["cursor"] == 3
    assert app.run_home_state["result"]["inventory"]["state"] == "COMPLETED"
    text = "\n".join(line for line, _ in UI._document(app))
    assert "Run again with these settings" in text and "no retained source declared" in text


def test_reconnect_saved_location_requires_explicit_read(recorded):
    _, _, result = recorded
    app = make_app()
    UI.open_run(app, result["selected"]["binding"])
    app.research.finish()
    saved = UI.save(app)
    fresh = make_app()
    UI.restore(fresh, saved)
    UI.open_run(fresh)
    assert fresh.research.pending is None
    UI.run_command(fresh, ["runhome", "action", "recover"])
    fresh.research.finish()
    assert fresh.run_home_state["binding"]["run_id"] == result["selected"]["binding"]["run_id"]


def test_different_connection_result_is_discarded(recorded):
    _, _, result = recorded
    app = make_app()
    UI.open_run(app, result["selected"]["binding"])
    app.profile_name = "different"
    app.research.finish()
    assert app.run_home_state["result"] is None
    assert "Connection changed" in app.run_home_state["error"]


def test_runhome_transient_error_retries_without_backoff_then_resolves(recorded, monkeypatch):
    _, _, result = recorded
    app = make_app()
    original = run_home.load
    monkeypatch.setattr(run_home, "load", lambda *a, **k: (_ for _ in ()).throw(OSError("retry later: stream busy")))
    UI.open_run(app, result["selected"]["binding"])
    app.research.finish()
    notice = app.notifications.snapshot().notices[0]
    assert notice.run[1] == result["selected"]["binding"]["run_id"]
    app.run_home_state["last_read"] -= 2.1
    UI.tick(app)
    assert app.research.pending is not None
    monkeypatch.setattr(run_home, "load", original)
    app.research.finish()
    assert app.notifications.get(notice.id).resolved_at
    assert not app.notifications.snapshot().counts["critical"]


def test_notification_resolution_requires_new_success_and_recurrence_reopens():
    center = N.NotificationCenter()
    key = ("source", "metrics")
    notice = center.condition(key, "read failed", observed_at=20)
    center.condition(key, "read failed", observed_at=30)
    assert center.get(notice).occurrences == 2
    assert not center.resolve_condition(key, "old success", observed_at=25)
    assert center.resolve_condition(key, "Verified sample parsed", observed_at=31)
    assert center.get(notice).resolution == "Verified sample parsed"
    assert center.snapshot().counts["critical"] == 0
    assert center.condition(key, "read failed", observed_at=32) == notice
    assert not center.get(notice).resolved_at and center.get(notice).occurrences == 3


def test_disabling_missing_or_empty_source_does_not_claim_recovery():
    app = SimpleNamespace(store=SimpleNamespace(alerts=None), profile_name="site", logs=SimpleNamespace(error=""), raven_ui_state={})
    item = {"error": "read failed", "enabled": True, "last_try": 20, "last_ok": 10}
    notice = N.sync(app, {"health": {"metrics": item}}).notices[0]
    item.update(error="", enabled=False, last_ok=25)
    N.sync(app, {"health": {"metrics": item}})
    assert not app.notifications.get(notice.id).resolved_at


def test_successful_source_refresh_marks_observed_condition_resolved():
    app = SimpleNamespace(store=SimpleNamespace(alerts=None), profile_name="site", logs=SimpleNamespace(error=""), raven_ui_state={})
    item = {"error": "read failed", "enabled": True, "last_try": 20, "last_ok": 10}
    notice = N.sync(app, {"health": {"metrics": item}}).notices[0]
    item.update(error="", last_try=25, last_ok=26)
    N.sync(app, {"health": {"metrics": item}})
    assert app.notifications.get(notice.id).resolved_at == 26


def test_notification_link_preserves_exact_run_context(recorded):
    _, _, result = recorded
    app = make_app()
    center = N.initialize(app)
    notice = center.post("recording read failed", run=run_home.identity(result["selected"]["binding"]))
    notification_ui.open_notifications(app)
    notification_ui.run_command(app, ["notifications", "related", notice])
    assert app.mode == "run_home"
    assert run_home.identity(app.run_home_state["binding"]) == run_home.identity(result["selected"]["binding"])


def test_cpu_memory_results_remain_visible_when_gpu_read_fails(recorded, monkeypatch):
    _, _, result = recorded
    binding = result["selected"]["binding"]
    selected = copy.deepcopy(result["selected"])
    selected["binding"]["telemetry_index"] = "declared/index.json"
    from raven import projects
    monkeypatch.setattr(projects, "select_run", lambda *a, **k: selected)
    monkeypatch.setattr(projects, "read_bound_telemetry", lambda *a, **k: {
        "index": {"sources": [{"family": "cpu-memory", "status": "complete"}, {"family": "gpu", "status": "partial"}]},
        "cpu-memory": [{"cpu": 12}, {"cpu": 30}], "gpu": [], "errors": {"gpu": "permission denied for GPU trace"}})
    loaded = run_home.load(binding)
    assert loaded["telemetry"]["cpu-memory"]["samples"] == 2
    assert loaded["errors"]["gpu"] == "permission denied for GPU trace"
    app = make_app()
    state = UI.initialize(app)
    state.update(binding=loaded["selected"]["binding"], result=loaded)
    text = "\n".join(line for line, _ in UI._document(app))
    assert "CPU and memory: 2 retained samples" in text and "permission denied for GPU trace" in text


def test_linked_logs_enter_canonical_tab(recorded, monkeypatch):
    _, _, result = recorded
    app = make_app()
    state = UI.initialize(app)
    state.update(binding=result["selected"]["binding"], result=result, scope=UI._scope(app))
    app.mode = "run_home"
    monkeypatch.setattr(UI, "_bind", lambda app: None)
    UI._activate(app, "logs")
    assert app.tab == "log" and app.mode == "main"


def test_exact_export_does_not_capture_marked_other_jobs(recorded, monkeypatch):
    _, _, result = recorded
    app = make_app()
    state = UI.initialize(app)
    state.update(binding=result["selected"]["binding"], result=result, scope=UI._scope(app))
    app.mode = "run_home"
    app.marked = {"unrelated-job"}
    monkeypatch.setattr(UI, "_bind", lambda app: None)
    app.project_state["binding"] = result["selected"]["binding"]
    from raven import data_export_ui
    captured = []
    def open_export(app):
        captured.extend(data_export_ui.capture_selection(app))
        data_export_ui.initialize(app)
        app.mode = "data_export"
    monkeypatch.setattr(data_export_ui, "open_export", open_export)
    UI._activate(app, "export")
    assert len(captured) == 1 and captured[0]["binding"]["run_id"] == state["binding"]["run_id"]
    assert app.data_export_state["return_mode"] == "run_home"
