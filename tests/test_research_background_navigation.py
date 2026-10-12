"""A completed worker cannot replace navigation made while it was running."""
from copy import deepcopy
from threading import Event
from types import SimpleNamespace
import json
import shlex

import pytest

from raven import research_commands
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store
from raven.remote import LocalFiles
from raven.research import ResearchHub


@pytest.fixture
def dashboard(tmp_path):
    cfg = Config({"animations": False, "startup": {"enabled": False}, "log_lines": 0})
    store = Store(persist=False)
    store.apply_jobs([Job("7", "first", "cpu", "RUNNING"), Job("8", "second", "cpu", "RUNNING")])
    store.apply_finished([Finished("900_1", "array-job", "FAILED", exit="1:0")])
    app = App(store, None, None, cfg, "tester", interactive=True)
    app.files = app.logs.files = LocalFiles()
    app.selected_id = app.research_job_id = "7"
    app.visible_ids = ["7", "8"]
    app.research = ResearchHub(cfg, app.files)
    script = tmp_path / "job.sbatch"
    script.write_text("#!/bin/bash\n#SBATCH --time=00:10:00\necho job\n")
    output = tmp_path / "result.json"
    output.write_text('{"score":1}')
    contract = tmp_path / "outputs.json"
    contract.write_text(json.dumps({"version": 1, "outputs": [{"path": output.name, "format": "json", "required_keys": ["score"]}]}))
    value = SimpleNamespace(app=app, root=tmp_path, script=script, contract=contract)
    yield value
    if app.research.pending:
        app.research.pending[0].result(timeout=5)
    app.research.close()


def command(dashboard, name):
    if name in ("prepare", "submit"):
        return shlex.join([name, str(dashboard.script), "--workdir", str(dashboard.root)])
    if name == "passport":
        return shlex.join([name, "capture", str(dashboard.script), "--workdir", str(dashboard.root),
                           "--output-dir", str(dashboard.root / "passports")])
    if name == "validate":
        return shlex.join([name, str(dashboard.contract), str(dashboard.root)])
    if name == "array":
        return shlex.join([name, "retry", "900", str(dashboard.script), "--workdir", str(dashboard.root)])
    return shlex.join(["metric", str(dashboard.root / "metrics.jsonl"), "--value", "score=1"])


def block_worker(monkeypatch, name):
    entered, release = Event(), Event()
    function = "prepare_args" if name == "submit" else "array_plan" if name == "array" else "offline_result"
    original = getattr(research_commands, function)

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test did not release the research worker"
        return original(*args, **kwargs)

    monkeypatch.setattr(research_commands, function, blocked)
    return entered, release


def finish(app, release):
    release.set()
    app.research.pending[0].result(timeout=5)
    app.research.poll_task()


@pytest.mark.parametrize("name,view", [("prepare", "submit"), ("submit", "submit"),
                                      ("passport", "passport"), ("validate", "artifacts"), ("array", "submit")])
def test_accepted_explicit_command_opens_its_working_view_immediately(dashboard, monkeypatch, name, view):
    app = dashboard.app
    entered, release = block_worker(monkeypatch, name)
    try:
        app.run_command(command(dashboard, name))
        assert entered.wait(3)
        assert app.tab == "research" and app.research_view == view and app.mode == "main"
        assert app.research_result is None
        finish(app, release)
        assert app.tab == "research" and app.research_view == view
        assert app.mode == ("confirm" if name == "submit" else "main")
        assert app.research_result is not None or name in ("submit", "array")
    finally:
        release.set()


@pytest.mark.parametrize("name", ["prepare", "submit", "passport", "validate", "array"])
@pytest.mark.parametrize("change", ["tab", "view", "dialog", "job", "source"])
def test_completion_preserves_a_new_workflow_and_retains_its_result(dashboard, monkeypatch, name, change):
    app = dashboard.app
    entered, release = block_worker(monkeypatch, name)
    try:
        app.run_command(command(dashboard, name))
        assert entered.wait(3)
        if change == "tab":
            app.enter_tab("jobs")
        elif change == "view":
            app.research_view = "arrays"
        elif change == "dialog":
            app.mode, app.detail_id = "help", "8"
        elif change == "job":
            app.selected_id = app.research_job_id = "8"
        else:
            app.research.configure(metrics_file="/new-project/metrics.jsonl", workdir="/new-project")
            app.project_state.update(root="/new-project", binding={"job_id": "8", "run_id": "new",
                "attempt": "new", "project_root": "/new-project", "run_root": "/new-project/runs/new"})
        before = (app.tab, app.mode, app.research_view, app.selected_id, app.research_job_id, app.detail_id,
                  deepcopy(app.project_state.get("binding")), deepcopy(app.research.settings), app.research.generation)
        confirm = deepcopy(app.confirm)
        finish(app, release)
        after = (app.tab, app.mode, app.research_view, app.selected_id, app.research_job_id, app.detail_id,
                 app.project_state.get("binding"), app.research.settings, app.research.generation)
        assert after == before and app.confirm == confirm
        assert app.research_result is not None
        if name in ("prepare", "submit", "array"):
            plan = app.research.plan
            assert plan["script"] == str(dashboard.script)
        assert "current workspace retained" in app.message
    finally:
        release.set()


def test_metric_writer_does_not_navigate_when_queued_or_completed(dashboard, monkeypatch):
    app = dashboard.app
    entered, release = block_worker(monkeypatch, "metric")
    try:
        app.run_command(command(dashboard, "metric"))
        assert entered.wait(3) and app.tab == "jobs"
        app.enter_tab("history")
        finish(app, release)
        assert app.tab == "history" and app.mode == "main" and app.selected_id == "7"
        assert (dashboard.root / "metrics.jsonl").exists()
    finally:
        release.set()


def test_busy_command_rejection_does_not_navigate_or_replace_the_accepted_request(dashboard, monkeypatch):
    app = dashboard.app
    entered, release = block_worker(monkeypatch, "prepare")
    try:
        app.run_command(command(dashboard, "prepare"))
        assert entered.wait(3)
        app.enter_tab("jobs")
        request = app._research_background_request
        app.run_command(command(dashboard, "submit"))
        assert not app.command_ok and app.tab == "jobs"
        assert app._research_background_request is request
        finish(app, release)
        assert app.tab == "jobs" and app.research.plan["script"] == str(dashboard.script)
        assert not app.confirm and app.mode == "main"
    finally:
        release.set()


def test_submit_cannot_review_an_old_plan_while_its_replacement_is_preparing(dashboard, monkeypatch):
    from raven.submission import prepare
    app = dashboard.app
    app.research.plan = old_plan = prepare(str(dashboard.script), workdir=str(dashboard.root))
    entered, release = block_worker(monkeypatch, "prepare")
    try:
        app.run_command(command(dashboard, "prepare"))
        assert entered.wait(3)
        app.run_command("submit")
        assert not app.command_ok and "still running" in app.message
        assert app.mode == "main" and not app.confirm and app.research.plan is old_plan
        finish(app, release)
        app.run_command("submit")
        assert app.mode == "confirm" and app.confirm["plan"] is app.research.plan
        assert app.research.plan is not old_plan
    finally:
        release.set()


def test_detached_completion_cannot_replace_a_newly_selected_plan_or_result(dashboard, monkeypatch):
    app = dashboard.app
    entered, release = block_worker(monkeypatch, "prepare")
    try:
        app.run_command(command(dashboard, "prepare"))
        assert entered.wait(3)
        app.enter_tab("jobs")
        app.research.plan = chosen = {"valid": True, "script": "/chosen/new-job.sbatch"}
        app.research_result = result = {"source": "new foreground action"}
        finish(app, release)
        assert app.tab == "jobs" and app.research.plan is chosen
        assert app.research_result is result
        completed = app.research_background_result
        assert completed["command"] == "prepare"
        assert completed["result"]["script"] == str(dashboard.script)
        assert not app.confirm
    finally:
        release.set()


def test_background_error_keeps_a_new_page_and_cannot_crash_publication(dashboard, monkeypatch):
    app = dashboard.app
    entered, release = Event(), Event()

    def broken(*args):
        entered.set()
        assert release.wait(5)
        raise RuntimeError("deliberate adapter failure")

    monkeypatch.setattr(research_commands, "offline_result", broken)
    try:
        app.run_command(command(dashboard, "prepare"))
        assert entered.wait(3)
        app.enter_tab("jobs")
        release.set()
        with pytest.raises(RuntimeError, match="adapter failure"):
            app.research.pending[0].result(timeout=5)
        app.research.poll_task()
        assert app.tab == "jobs" and app.mode == "main" and not app.command_ok
        assert "adapter failure" in app.message
    finally:
        release.set()
