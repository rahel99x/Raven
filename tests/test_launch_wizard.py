"""Guided launch uses actual immutable submission and portable run recording."""
import copy
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

from raven import launch, launch_settings, launch_wizard as service, launch_wizard_ui as UI
from raven import layout as L
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.remote import LocalFiles


class Background:
    demo = False
    closed = False

    def __init__(self):
        self.pending = None
        self.files = LocalFiles()

    def start_task(self, work, complete):
        if self.pending:
            return False
        self.pending = (work, complete)
        return True

    def finish(self):
        work, complete = self.pending
        self.pending = None
        try:
            result = work()
        except Exception as exc:
            result = exc
        complete(result)


@pytest.fixture
def script(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    source = tmp_path / "science.sh"
    source.write_text("#!/bin/sh\n#SBATCH --account=LabX\n#SBATCH --mem=4G\nprintf '%s\\n' \"$@\"\n")
    return source


def draft(script, **values):
    return dict(dict(project=str(script.parent), script=script.name, arguments="", execution="slurm"), **values)


@pytest.fixture
def app(script):
    value = App(Store(persist=False), None, None, Config({}), "researcher")
    value.research = Background()
    value.files = value.research.files
    value.width, value.height = 80, 24
    UI.open_launch(value)
    value.launch_wizard_state["draft"]["project"] = str(script.parent)
    return value


def action(app, command):
    UI.run_command(app, ["launchjob", "action", command])


def advance(app, script):
    action(app, "next")
    app.research.finish()
    assert app.launch_wizard_state["stage"] == "script"
    app.launch_wizard_state["draft"]["script"] = script.name
    action(app, "next")
    app.research.finish()
    assert app.launch_wizard_state["stage"] == "resources", app.launch_wizard_state["error"]
    action(app, "next")
    app.research.finish()
    action(app, "next")
    app.research.finish()
    assert app.launch_wizard_state["stage"] == "review", app.launch_wizard_state["error"]


def test_readonly_import_exact_arguments_human_units_and_sources(script):
    source = script.read_bytes()
    value = draft(script, memory="8 GiB", time="1h", arguments="'' 'two words' '$(touch NEVER)' --gpu '雪'", cpu_memory=False)
    reviewed = service.review(value, environ={"SBATCH_ACCOUNT": "LabY"})
    plan = reviewed["plan"]
    assert plan["account"] == "LabY" and plan["account_source"] == "environment SBATCH_ACCOUNT"
    assert plan["effective_options"]["mem"] == "8192M"
    assert plan["effective_options"]["time"] == "01:00:00"
    assert plan["script_args"] == ["", "two words", "$(touch NEVER)", "--gpu", "雪"]
    assert plan["settings"]["cpu_memory"] is False
    assert "--no-cpu-memory" in reviewed["command"]
    assert script.read_bytes() == source
    assert not (script.parent / ".raven").exists()


@pytest.mark.parametrize("changed", ["script", "settings", "recipe", "environment", "connection", "draft"])
def test_stale_review_never_creates_or_submits(script, changed):
    value = draft(script)
    env, connection = {}, {"transport": "local"}
    if changed == "recipe":
        launch_settings.update_settings(script.parent, {"recipes": {"lab": {"gpu": False}}})
        value["recipe"] = "lab"
    reviewed = service.review(value, environ=env, connection=connection)
    if changed == "script":
        script.write_text("#!/bin/sh\nexit 0\n")
    elif changed == "settings":
        launch_settings.update_settings(script.parent, {"launch": {"gpu": True}})
    elif changed == "recipe":
        launch_settings.update_settings(script.parent, {"recipes": {"lab": {"gpu": True}}})
    elif changed == "environment":
        env["MY_APPLICATION_MODE"] = "different"
    elif changed == "connection":
        connection["host"] = "another-host"
    else:
        value["arguments"] = "--different"
    with pytest.raises(ValueError, match="changed after review"):
        service.submit(reviewed, value, environ=env, connection=connection,
                       runner=lambda *a, **kw: pytest.fail("must not submit"))
    assert not (script.parent / ".raven" / "wrappers").exists()


def test_fake_scheduler_runs_captured_script_and_receipt(script):
    value = draft(script, arguments="'hello world'")
    reviewed = service.review(value, environ={})
    calls = []
    def scheduler(argv, **kwargs):
        calls.append(argv)
        wrapper = argv[-2]
        assert Path(wrapper).read_text().startswith("#!/bin/sh\n#SBATCH --account=LabX")
        result = subprocess.run(["/bin/sh", wrapper, argv[-1]], cwd=script.parent,
            env={"PATH": "/usr/bin:/bin", "SLURM_JOB_ID": "923", "SLURM_CLUSTER_NAME": "lab"}, capture_output=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout == b"hello world\n"
        kwargs["stdout"].write(b"923;lab\n")
        return subprocess.CompletedProcess(argv, 0)
    receipt = service.submit(reviewed, value, environ={}, runner=scheduler)
    assert len(calls) == 1 and receipt["job_id"] == "923"
    manifest = json.loads((Path(receipt["run_dir"]) / "run.json").read_text())
    assert manifest["state"] == "COMPLETED"
    assert manifest["provenance"]["script_sha256"] == reviewed["plan"]["script_sha256"]


@pytest.mark.parametrize("status", [0, 17])
def test_explicit_standalone_subprocess_records_logs_and_outcome_without_ui_output(script, capfd, status):
    script.write_text("#!/bin/sh\nprintf 'visible stdout'\nprintf 'visible stderr' >&2\nsleep 0.1\nexit " + str(status) + "\n")
    value = draft(script)
    connection = {"transport": "none"}
    with pytest.raises(ValueError, match="standalone"):
        service.review(value, environ={}, connection=connection)
    value["execution"] = "local"
    reviewed = service.review(value, environ={"PATH": "/usr/bin:/bin"}, connection=connection)
    receipt = service.submit(reviewed, value, environ={"PATH": "/usr/bin:/bin"}, connection=connection)
    assert receipt["status"] == "supervisor-started" and receipt["pid"] > 0
    run = Path(receipt["run_dir"])
    deadline = time.monotonic() + 10
    manifest = {}
    while time.monotonic() < deadline:
        try:
            manifest = json.loads((run / "run.json").read_text())
            if manifest["state"] in ("COMPLETED", "FAILED"):
                break
        except FileNotFoundError:
            pass
        time.sleep(.02)
    assert manifest["state"] == ("FAILED" if status else "COMPLETED"), receipt
    assert json.loads((run / "summary.json").read_text())["exit_code"] == status
    assert (run / "telemetry" / "index.json").exists()
    logs = json.loads((run / "logs.json").read_text())["logs"]
    assert {row["id"] for row in logs} == {"local.stdout", "local.stderr"}
    assert Path(receipt["logs"]["stdout"]).read_text() == "visible stdout"
    assert "visible stderr" in Path(receipt["logs"]["stderr"]).read_text()
    assert capfd.readouterr() == ("", "")


def test_wizard_works_without_open_project_and_double_click_submits_once(app, script, monkeypatch):
    assert not app.project_state["root"]
    advance(app, script)
    original = service.submit
    calls = []
    def scheduler(argv, **kwargs):
        calls.append(argv)
        kwargs["stdout"].write(b"924;lab\n")
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(service, "submit", lambda *a, **kw: original(*a, **kw, runner=scheduler))
    action(app, "submit")
    action(app, "submit")
    assert app.launch_wizard_state["busy"]
    app.research.finish()
    assert len(calls) == 1
    assert app.launch_wizard_state["receipt"]["job_id"] == "924"
    action(app, "submit")
    assert not app.research.pending


def test_closing_before_queued_submission_never_executes(app, script, monkeypatch):
    advance(app, script)
    monkeypatch.setattr(service, "submit", lambda *a, **kw: pytest.fail("must not submit"))
    action(app, "submit")
    UI.cancel(app)
    app.research.finish()
    assert not (script.parent / ".raven").exists()


def test_connection_change_before_worker_start_cannot_retarget(app, script):
    advance(app, script)
    action(app, "submit")
    app.raven_resolved = SimpleNamespace(data={"connection": {"transport": "ssh", "host": "cluster.example"}})
    app.research.finish()
    assert "connection changed" in app.launch_wizard_state["error"]
    assert not (script.parent / ".raven").exists()


def test_remote_project_is_not_scanned_locally(app, monkeypatch):
    app.raven_resolved = SimpleNamespace(data={"connection": {"transport": "ssh", "host": "cluster.example"}})
    monkeypatch.setattr(service, "project_options", lambda *a, **kw: pytest.fail("no local read"))
    action(app, "next")
    assert "cluster.example" in app.launch_wizard_state["error"]
    assert not app.research.pending


@pytest.mark.parametrize("width,height,ascii_", [(80, 24, False), (32, 12, True), (12, 6, True)])
def test_review_actions_reachable_and_paint_never_reads_files(app, script, monkeypatch, width, height, ascii_):
    advance(app, script)
    monkeypatch.setattr(launch, "resolve_plan", lambda *a, **kw: pytest.fail("paint must not read"))
    app.width, app.height = width, height
    choices = [key for _, key in UI._document(app) if key]
    app.launch_wizard_state.update(cursor=choices.index("submit"), follow=True)
    rendered = UI.overlay(SimpleNamespace(g=L.Glyphs(ascii_)), {}, app, width, height)
    assert rendered
    assert any(item[2]["action"][1].endswith("submit") for item in app.launch_wizard_state["control_hits"])
    UI.handle_key(app, "up")
    UI.overlay(SimpleNamespace(g=L.Glyphs(ascii_)), {}, app, width, height)


def test_resource_profile_save_then_reload_preserves_script_precedence(app, script):
    action(app, "next")
    app.research.finish()
    state = app.launch_wizard_state
    state["draft"].update(script=script.name, account="LabY", memory="8 GiB", recipe_name="lab-y")
    action(app, "next")
    app.research.finish()
    action(app, "save-recipe")
    app.research.finish()
    saved = launch_settings.load_settings(script.parent)
    assert saved["recipes"]["lab-y"]["sbatch_defaults"]["account"] == "LabY"
    value = draft(script, recipe="lab-y")
    assert service.review(value, environ={})["plan"]["account"] == "LabX"


def test_saved_profile_provenance_and_advanced_recording_settings_are_preserved(app, script):
    launch_settings.update_settings(script.parent, {"recipes": {"lab-run": {"gpu": True, "features": ["scaling"]}}})
    action(app, "next")
    app.research.finish()
    state = app.launch_wizard_state
    state["draft"].update(script=script.name, cpus="8")
    action(app, "next")
    app.research.finish()
    action(app, "save-recipe")
    app.research.finish()
    reviewed = service.review(draft(script, recipe="lab-run"), environ={})
    assert reviewed["plan"]["gpu"] is True
    assert reviewed["plan"]["features"] == ["scaling"]
    assert reviewed["plan"]["option_sources"]["cpus-per-task"] == "resource default (recipe lab-run)"


def test_ambiguous_submission_is_not_retried_by_repeated_ui_action(app, script, monkeypatch):
    advance(app, script)
    original = service.submit
    calls = []
    def scheduler(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 30)
    monkeypatch.setattr(service, "submit", lambda *a, **kw: original(*a, **kw, runner=scheduler))
    action(app, "submit")
    app.research.finish()
    assert "unknown" in app.launch_wizard_state["error"]
    action(app, "submit")
    assert len(calls) == 1 and app.research.pending is None
    assert not any(key == "submit" for _, key in UI._document(app))


def test_advanced_reporting_has_dedicated_mode_and_returns_to_wizard(app, script):
    from raven import data_export_ui, launch_setup_ui
    app.launch_wizard_state["stage"] = "recordings"
    action(app, "advanced")
    assert app.mode == "launch_setup" and data_export_ui.active(app)
    app.research.finish()
    assert app.data_export_state["stage"] == "setup"
    data_export_ui.cancel(app, close=True)
    assert app.mode == "launch_job"


def test_managed_standalone_profile_real_background_launch_needs_no_heavy(app, script):
    from raven import raven_config
    from raven.research import ResearchHub
    from raven.raven_execution import ExecutionRouter
    cfg = Config({"raven": {"managed": True, "mode": "light", "site": "desktop"}})
    hub = ResearchHub(cfg, files=app.files)
    hub.execution = ExecutionRouter(cfg["raven"], lambda: pytest.fail("workload launch requested Heavy"))
    app.research = hub
    app.raven_resolved = raven_config.resolve(site="desktop", env={"XDG_CONFIG_HOME": str(script.parent / "config")})
    UI.open_launch(app)
    app.launch_wizard_state["draft"].update(project=str(script.parent), script=script.name)
    def finish():
        assert hub.future is not None
        hub.future.result(timeout=10)
        hub.poll_task()
    try:
        action(app, "next")
        finish()
        # The explicit saved standalone site selects local; the review still
        # requires the user to press Launch before anything executes.
        assert app.launch_wizard_state["draft"]["execution"] == "local"
        for _ in range(3):
            action(app, "next")
            finish()
        assert app.launch_wizard_state["stage"] == "review", app.launch_wizard_state["error"]
        action(app, "submit")
        finish()
        receipt = app.launch_wizard_state["receipt"]
        assert receipt and receipt["execution"] == "local", app.launch_wizard_state["error"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (Path(receipt["run_dir"]) / "summary.json").exists():
            time.sleep(.02)
        assert (Path(receipt["run_dir"]) / "summary.json").exists()
    finally:
        hub.close()


@pytest.mark.parametrize("kind,transport,expected", [("desktop", "none", "local"), ("slurm", "local", "slurm"), ("slurm", "ssh", "slurm")])
def test_saved_site_selects_execution_without_inference_from_missing_slurm(app, kind, transport, expected):
    app.raven_resolved = SimpleNamespace(data={"site": {"kind": kind}, "connection": {"transport": transport}})
    UI.open_launch(app)
    assert app.launch_wizard_state["draft"]["execution"] == expected


def test_standalone_profile_cannot_switch_to_slurm_without_selecting_slurm_site(app, script):
    app.raven_resolved = SimpleNamespace(data={"site": {"kind": "desktop"}, "connection": {"transport": "none"}})
    UI.open_launch(app)
    app.launch_wizard_state["draft"].update(project=str(script.parent), script=script.name)
    action(app, "next")
    app.research.finish()
    action(app, "execution:slurm")
    action(app, "next")
    app.research.finish()
    assert "standalone workstation" in app.launch_wizard_state["error"]
    assert app.launch_wizard_state["stage"] == "script"
