"""Connect real reporter output without inventing evidence or changing jobs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from raven import ops_workspace, projects, reporting, research_reporting
from raven.operations import Context
from raven.remote import LocalFiles, RemoteFiles
from test_project_remote import IsolatedSSH


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project ' with spaces"
    root.mkdir()
    (root / "program.sh").write_text("#!/bin/bash\nexit 0\n")
    run = research_reporting.start(str(root), experiment="actual experiment", script="program.sh",
                                    environ={"SLURM_JOB_ID": "81", "SLURM_CLUSTER_NAME": "lab"})
    reporting.write_metric(run, {"loss": .125}, step=1, t=10)
    (run / "outputs/result.json").write_text('{"observed": 12}\n')
    assert research_reporting.main(["artifact", "outputs/result.json", "--run", str(run), "--format", "json"]) == 0
    research_reporting.finish(run, exit_code=0, runtime_seconds=2)
    return root, run


def inspect(root, run, *, files=None, **params):
    ctx = Context(files=files or LocalFiles(), scope={"backend": "local", "cluster": "lab"})
    return ops_workspace.run("project-guide", {"root": str(root), "run_id": run.name, **params}, ctx)


@pytest.mark.parametrize("remote", [False, True])
def test_actual_reporter_sample_validates_content_on_local_and_isolated_ssh(project, remote):
    root, run = project
    before = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    files = RemoteFiles(IsolatedSSH()) if remote else LocalFiles()
    result = inspect(root, run, files=files)
    checks = {item["source"]: item for item in result["data"]["sample"]["checks"]}
    assert checks["inventory"]["status"] == checks["metrics"]["status"] == checks["summary"]["status"] == checks["artifacts"]["status"] == "ready"
    assert checks["artifacts"]["result"]["outputs"][0]["path"] == "outputs/result.json"
    assert "Experiment" not in result["data"]["coverage"][0]["missing_views"]
    assert "Logs" not in result["data"]["coverage"][0]["missing_views"]
    assert "metrics" in result["data"]["coverage"][0]["declared"]
    assert before == {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert "plan" not in result


@pytest.mark.parametrize("source", ["metrics", "summary", "artifacts"])
def test_bad_sample_contents_are_reported_despite_valid_declarations(project, source):
    root, run = project
    path = run / {"metrics": "metrics.jsonl", "summary": "summary.json", "artifacts": "outputs/result.json"}[source]
    path.write_text('{"not": "the required record"}\n' if source != "artifacts" else "not JSON\n")
    result = inspect(root, run)
    check = next(item for item in result["data"]["sample"]["checks"] if item["source"] == source)
    assert check["status"] == "invalid"
    assert result["status"] == "warning"


def test_summary_cannot_describe_another_attempt(project):
    root, run = project
    path = run / "summary.json"
    data = json.loads(path.read_text())
    data["job_id"] = "82"
    path.write_text(json.dumps(data))
    result = inspect(root, run)
    check = next(item for item in result["data"]["sample"]["checks"] if item["source"] == "summary")
    assert check["status"] == "invalid" and "job_id" in check["detail"]


@pytest.mark.parametrize("mutation", ["symlink", "missing", "oversized"])
def test_declared_summary_is_confined_and_bounded(project, tmp_path, mutation):
    root, run = project
    summary = run / "summary.json"
    summary.unlink()
    if mutation == "symlink":
        outside = tmp_path / "private.json"
        outside.write_text('{"secret":"never include this"}')
        summary.symlink_to(outside)
    elif mutation == "oversized":
        summary.write_text(" " * 262145)
    result = inspect(root, run)
    check = next(item for item in result["data"]["sample"]["checks"] if item["source"] == "summary")
    assert check["status"] in {"missing", "invalid"}
    assert "never include this" not in json.dumps(result)


def test_exact_selection_never_substitutes_another_run(project):
    root, run = project
    with pytest.raises(ValueError, match="no other attempt"):
        inspect(root, Path("absent"))
    ctx = Context(selected="different-job", scope={"cluster": "lab"})
    result = ops_workspace.run("project-guide", {"root": str(root)}, ctx)
    assert result["data"]["sample"]["status"] == "not-selected"
    matched = ops_workspace.run("project-guide", {"root": str(root)}, Context(selected="81", scope={"cluster": "lab"}))
    assert matched["data"]["sample"]["status"] == "not-selected"
    sole = ops_workspace.run("project-guide", {"root": str(root)}, Context())
    assert sole["data"]["sample"]["run_id"] == run.name


def test_same_job_id_cannot_automatically_associate_a_previous_scheduler_attempt(project):
    root, run = project
    ctx = Context(selected="81", scope={"cluster": "lab"}, jobs=({
        "id": "81", "cluster": "lab", "submit": "2099-01-01T00:00:00", "start": "2099-01-01T00:01:00"},))
    result = ops_workspace.run("project-guide", {"root": str(root)}, ctx)
    assert result["data"]["sample"]["status"] == "not-selected"
    assert not result["data"]["sample"]["checks"]
    explicit = ops_workspace.run("project-guide", {"root": str(root), "run_id": run.name}, ctx)
    assert explicit["data"]["sample"]["run_id"] == run.name


def test_cancellation_stops_sample_reads(project, monkeypatch):
    root, run = project
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(projects, "select_run", lambda *a, **kw: pytest.fail("cancelled guide read sample"))
    with pytest.raises(ValueError, match="cancelled"):
        ops_workspace.run("project-guide", {"root": str(root), "run_id": run.name}, Context(cancel=stop))


def test_generated_batch_hook_executes_report_lifecycle_and_preserves_actual_failure(tmp_path):
    root = tmp_path / "project $(not-a-command) ' quoted"
    root.mkdir()
    result = ops_workspace.run("project-guide", {"root": str(root), "experiment": "test ' experiment", "script": "raven-batch.sh"}, Context())
    hook = root / "raven-batch.sh"
    hook.write_text("\n".join(result["data"]["hooks"]["batch"]) + "\n")
    binary = tmp_path / "bin"
    binary.mkdir()
    # Exercise the reporting library contract in an explicitly local test
    # process. Managed raven additionally requires real job placement; an
    # environment-only JobID fixture must not authorize that execution.
    import shlex
    executable = binary / "raven"
    executable.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -c " + shlex.quote("from raven.cli import main; raise SystemExit(main())") + ' "$@"\n')
    executable.chmod(0o700)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("SLURM_", "RAVEN_"))}
    env.update(PATH=str(binary) + os.pathsep + env.get("PATH", ""), PYTHONPATH=str(Path(__file__).resolve().parents[1]), SLURM_JOB_ID="82")
    payload = 'import os,subprocess; subprocess.run(["raven","report","metric","--run",os.environ["RAVEN_RUN_DIR"],"--value","loss=0.25"],check=True); print("actual output"); raise SystemExit(7)'
    completed = subprocess.run(["bash", str(hook), sys.executable, "-c", payload], env=env, cwd=root, capture_output=True, timeout=20)
    assert completed.returncode == 7, completed.stderr.decode()
    run = root / "runs/job-82-a1"
    summary = json.loads((run / "summary.json").read_text())
    assert summary["state"] == "FAILED" and summary["exit_code"] == 7
    assert "actual output" in (run / "logs/stdout.log").read_text()
    assert json.loads((run / "metrics.jsonl").read_text())["metrics"]["loss"] == .25
    missing = subprocess.run(["bash", str(hook)], env=env, cwd=root, capture_output=True, timeout=10)
    assert missing.returncode == 64
