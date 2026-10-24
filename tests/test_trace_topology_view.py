"""Exact receipt opening and typed private workspace commands for the examples."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shlex
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "trace_topology_view", ROOT / "examples/trace-topology/view.py"
)
view = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(view)


def receipt(tmp_path, *, execution="slurm", sources=True):
    project = tmp_path / "project with 'quotes'; $(touch injected)"
    run = project / "runs" / "exact-attempt"
    run.mkdir(parents=True)
    document = dict(
        schema="raven.launch-receipt/v1",
        execution=execution,
        project_root=str(project),
        run_dir=str(run),
        run_id=run.name,
    )
    if execution == "slurm":
        document["job_id"] = "42"
    path = project / "launch.json"
    path.write_text(json.dumps(document))
    configuration = project / "raven-view.json"
    configuration.write_text(
        json.dumps(view.helpers.example_config(execution == "local"))
    )
    if sources:
        trace = run / "trace"
        trace.mkdir()
        entries = []
        for pid, role in ((12, "helper"), (13, "main")):
            folder = trace / ("process-" + str(pid))
            folder.mkdir()
            stream = "stream-" + str(pid)
            (folder / "manifest.json").write_text(
                json.dumps(
                    dict(
                        schema="raven.trace/v1",
                        identity=dict(
                            attempt=run.name,
                            run_id=run.name,
                            pid=pid,
                            stream=stream,
                            job_id=document.get("job_id", "local"),
                        ),
                    )
                )
            )
            entries.append(
                dict(
                    schema="raven.trace/v1",
                    run_id=run.name,
                    path="trace/" + folder.name + "/manifest.json",
                    stream=stream,
                    status="recording",
                    process=dict(pid=pid, role=role),
                )
            )
        (trace / "index.json").write_text(
            json.dumps(dict(schema="raven.trace-index/v1", sources=entries))
        )
    return path, project, run, configuration


@pytest.mark.parametrize("execution", ["local", "slurm"])
def test_browser_prints_exact_attempt_and_follows_project_without_starting(
    execution, tmp_path, monkeypatch, capsys
):
    path, project, run, configuration = receipt(tmp_path, execution=execution)
    monkeypatch.setattr(
        view.subprocess,
        "run",
        lambda *a, **k: pytest.fail("--no-open started a process"),
    )
    monkeypatch.setattr(
        view.helpers,
        "executable",
        lambda *a: pytest.fail("--no-open inspected executable"),
    )
    assert view.main([str(path), "--browser", "--no-open"]) == 0
    line = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("Workspace: ")
    )
    command = shlex.split(line.removeprefix("Workspace: "))
    assert command == [
        "raven",
        "--site",
        "desktop" if execution == "local" else "default-slurm",
        "--config",
        str(configuration),
        "trace",
        "workspace",
        str(run / "trace/process-13/manifest.json"),
        "--follow-project",
        str(project),
        "--serve",
        "--open-browser",
    ]
    assert not (tmp_path / "injected").exists()


def test_workspace_executes_typed_arguments_and_retained_history_context(
    tmp_path, monkeypatch, capsys
):
    path, project, run, configuration = receipt(tmp_path)
    captured = []
    monkeypatch.setattr(view.helpers, "executable", lambda name: "/fixture/raven")

    def execute(command, **options):
        captured.append((command, options))
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(view.subprocess, "run", execute)
    assert view.main([str(path), "--workspace", "--history", "--process", "12"]) == 7
    command, options = captured[0]
    assert command[0] == "/fixture/raven"
    assert str(run / "trace/process-12/manifest.json") in command
    assert "--follow-project" in command and "--serve" in command
    assert "--open-browser" not in command and "--history" not in command
    assert options == {"cwd": project, "check": False}
    assert "Retained History context" in capsys.readouterr().out


@pytest.mark.parametrize("change", ["config", "receipt", "manifest", "custom-run-id"])
def test_workspace_rejects_foreign_or_changed_context(change, tmp_path, monkeypatch):
    path, project, run, configuration = receipt(tmp_path)
    if change == "config":
        configuration.write_text(json.dumps(view.helpers.example_config(True)))
    elif change == "receipt":
        document = json.loads(path.read_text())
        document["run_id"] = "some-other-run"
        path.write_text(json.dumps(document))
    else:
        manifest = run / "trace/process-13/manifest.json"
        document = json.loads(manifest.read_text())
        document["identity"]["attempt" if change == "manifest" else "run_id"] = (
            "some-other-run"
        )
        manifest.write_text(json.dumps(document))
    monkeypatch.setattr(
        view.subprocess,
        "run",
        lambda *a, **k: pytest.fail("Foreign context started a viewer"),
    )
    assert view.main([str(path), "--workspace"]) == 2


@pytest.mark.parametrize("options", [[], ["--history"], ["--process", "42"]])
def test_queued_job_without_sources_is_a_normal_wait(
    options, tmp_path, monkeypatch, capsys
):
    path, _, _, _ = receipt(tmp_path, sources=False)
    monkeypatch.setattr(
        view.subprocess,
        "run",
        lambda *a, **k: pytest.fail("Queued run started a process"),
    )
    assert view.main([str(path), "--workspace", *options]) == 0
    text = capsys.readouterr().out
    assert "may be queued" in text and "No job is resubmitted" in text


def test_missing_requested_pid_is_reported_in_a_populated_exact_run(
    tmp_path, monkeypatch
):
    path, _, _, _ = receipt(tmp_path)
    monkeypatch.setattr(
        view.subprocess,
        "run",
        lambda *a, **k: pytest.fail("Unknown PID started a viewer"),
    )
    assert view.main([str(path), "--workspace", "--process", "999"]) == 2


@pytest.mark.parametrize(
    "options", [[], ["--history", "--no-open"], ["--inspect", "--process", "12"]]
)
def test_default_and_legacy_options_keep_existing_view_workflow(options, monkeypatch):
    captured = []

    def existing(args):
        captured.append(args)
        return 9

    monkeypatch.setattr(view.helpers, "view_example", existing)
    assert view.main(["/exact/launch.json", *options]) == 9
    args = captured[0]
    assert args.receipt == "/exact/launch.json"
    assert args.history == ("--history" in options)
    assert args.inspect == ("--inspect" in options)
    assert args.no_open == ("--no-open" in options)


def test_view_mode_and_pid_errors_are_explicit():
    with pytest.raises(SystemExit):
        view.main(["/exact/launch.json", "--workspace", "--inspect"])
    with pytest.raises(SystemExit):
        view.main(["/exact/launch.json", "--workspace", "--process", "0"])
