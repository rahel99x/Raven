"""Run the copied portable starter without Raven installed in the child process."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

from raven.metrics import _record
from raven.projects import validate_inventory


@pytest.fixture
def project(tmp_path):
    source = Path(__file__).resolve().parents[1]
    root = tmp_path / "student project with spaces"
    helper = root / ".raven/reporting-v2"
    helper.mkdir(parents=True)
    for name in ("portable_run.py", "run.sh", "batch.sh", "demo.py"):
        shutil.copyfile(source / "examples/reporting-v2" / name, helper / name)
    shutil.copyfile(source / "examples/project-template/reporting.py", helper / "reporting.py")
    (root / "science.py").write_text("# Scientific source declaration for these test commands.\n")
    return root, helper


def environment(**extra):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("SLURM_", "RAVEN_")) and key != "PYTHONPATH"}
    env.update(RAVEN_REPORT_PYTHON=sys.executable, **extra)
    return env


def launch(project, command, *, extra=None, arguments=(), cwd=None):
    root, helper = project
    result = subprocess.run([sys.executable, str(helper / "portable_run.py"), "run", "--root", str(root),
                             "--experiment", "solver", "--script", "science.py", *arguments,
                             "--", *command], cwd=cwd or root, env=environment(**(extra or {})),
                            capture_output=True, text=True, timeout=15)
    return result, Path(result.stdout.strip()) if result.stdout.strip() else None


def read(path):
    return json.loads(path.read_text())


def test_local_command_cwd_logs_identity_and_native_inventory(project, tmp_path):
    result, run = launch(project, [sys.executable, "-c",
                                   'import os,sys; print(os.getcwd()); print(os.environ["RAVEN_RUN_DIR"]); print("error stream",file=sys.stderr)'],
                         cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    inventory = validate_inventory(read(run / "run.json"))
    summary = read(run / "summary.json")
    assert inventory["state"] == "COMPLETED"
    assert inventory["resources"] == {}
    assert "job_id" not in inventory and "job_id" not in summary
    assert inventory["metadata"]["reporting_v2"]["uid"] == os.getuid()
    assert inventory["metadata"]["reporting_v2"]["mode"] == "local"
    assert (run / "logs/stdout.log").read_text().splitlines() == [str(project[0]), str(run)]
    assert (run / "logs/stderr.log").read_text() == "error stream\n"
    assert summary["exit_code"] == 0 and summary["runtime_seconds"] >= 0
    assert not {"cpu_seconds", "memory_bytes", "gpus"} & summary.keys()


def test_shell_demo_emits_valid_metrics_and_actual_result(project):
    root, helper = project
    env = environment(RAVEN_EXPERIMENT="pi", RAVEN_SCRIPT=".raven/reporting-v2/demo.py",
                      RAVEN_PARAMETERS='{"steps":3,"terms":30}')
    result = subprocess.run(["bash", str(helper / "run.sh"), sys.executable, str(helper / "demo.py"),
                             "--steps", "3", "--terms-per-step", "10"], cwd=root, env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    run = Path(result.stdout.strip())
    records = [_record(json.loads(line)) for line in (run / "metrics.jsonl").read_text().splitlines()]
    assert len(records) == 3
    assert records[-1]["progress"]["completed"] == records[-1]["progress"]["total"] == 3
    output = read(run / "outputs/result.json")
    assert output["terms"] == 30 and 3 < output["estimated_pi"] < 4
    assert read(run / "run.json")["parameters"] == {"steps": 3, "terms": 30}


def test_array_identity_resources_and_repeated_allocation_commands(project):
    slurm = {"SLURM_JOB_ID": "733", "SLURM_ARRAY_JOB_ID": "700", "SLURM_ARRAY_TASK_ID": "0",
             "SLURM_CLUSTER_NAME": "cluster-a", "SLURM_RESTART_COUNT": "2",
             "SLURM_JOB_ACCOUNT": "LabX", "SLURM_JOB_PARTITION": "cpu",
             "SLURM_JOB_NUM_NODES": "2", "SLURM_JOB_CPUS_PER_NODE": "4(x2)",
             "SLURM_MEM_PER_NODE": "8192", "SLURM_GPUS": "1"}
    first, one = launch(project, [sys.executable, "-c", "pass"], extra=slurm)
    second, two = launch(project, [sys.executable, "-c", "pass"], extra=slurm)
    assert first.returncode == second.returncode == 0
    assert one != two
    for run in (one, two):
        inventory = validate_inventory(read(run / "run.json"))
        assert inventory["job_id"] == "700_0" and inventory["attempt"] == 3
        assert inventory["resources"] == {"account": "LabX", "partition": "cpu", "nodes": 2,
                                          "cpus": 8, "mem_bytes": 16384 * 1024**2, "gpus": 1}
        assert inventory["metadata"]["raven_launch"]["cluster"] == "cluster-a"
        assert inventory["metadata"]["reporting_v2"]["user"]
        assert read(run / "summary.json")["account"] == "LabX"


def test_batch_runs_one_coordinator_around_application_step(project, tmp_path):
    root, helper = project
    commands = tmp_path / "fake scheduler"
    commands.mkdir()
    step = commands / "srun"
    step.write_text('#!/usr/bin/env bash\nprintf "step\\n" >> "$RAVEN_PROJECT_ROOT/step-calls"\nexec "$@"\n')
    step.chmod(0o700)
    # Slurm's --chdir selects root even when sbatch was called from tmp_path.
    env = environment(RAVEN_EXPERIMENT="batch", RAVEN_SCRIPT="science.py",
                      SLURM_JOB_ID="44", SLURM_SUBMIT_DIR=str(tmp_path), SLURM_JOB_ACCOUNT="LabX")
    env["PATH"] = str(commands) + os.pathsep + env["PATH"]
    result = subprocess.run(["bash", str(helper / "batch.sh"), sys.executable, "-c", 'print("application")'],
                            cwd=root, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    run = Path(result.stdout.strip())
    assert read(run / "run.json")["job_id"] == "44"
    assert (root / "step-calls").read_text() == "step\n"
    assert (run / "logs/stdout.log").read_text() == "application\n"
    assert len(list((root / "runs").iterdir())) == 1
    assert not (tmp_path / "step-calls").exists()


def test_batch_refuses_workdir_without_project_integration(project, tmp_path):
    root, helper = project
    result = subprocess.run(["bash", str(helper / "batch.sh"), sys.executable, "-c", "pass"],
                            cwd=tmp_path, env=environment(SLURM_JOB_ID="44", SLURM_SUBMIT_DIR=str(root)),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "job WorkDir must be the project root" in result.stderr
    assert not (root / "runs").exists()


@pytest.mark.parametrize("extra", [
    {"SLURM_JOB_ID": "not-a-job"},
    {"SLURM_JOB_ID": "700", "SLURM_ARRAY_TASK_ID": "1"},
    {"SLURM_JOB_ID": "700", "SLURM_RESTART_COUNT": "-1"},
])
def test_invalid_slurm_identity_does_not_launch_payload(project, extra):
    result, _ = launch(project, [sys.executable, "-c", 'open("launched","w").close()'], extra=extra)
    assert result.returncode == 2
    assert not (project[0] / "launched").exists()


def test_failure_missing_command_and_duplicate_attempt_preserve_evidence(project):
    result, run = launch(project, [sys.executable, "-c", "raise SystemExit(23)"], arguments=("--run-id", "failed"))
    assert result.returncode == 23
    assert read(run / "summary.json")["exit_code"] == 23
    assert read(run / "run.json")["state"] == "FAILED"
    before = (run / "summary.json").read_bytes()
    duplicate, _ = launch(project, [sys.executable, "-c", 'open("unexpected","w").close()'],
                          arguments=("--run-id", "failed"))
    assert duplicate.returncode == 2
    assert (run / "summary.json").read_bytes() == before
    assert not (project[0] / "unexpected").exists()
    missing, absent = launch(project, [str(project[0] / "no-such-executable")])
    assert missing.returncode == 127
    assert read(absent / "summary.json")["exit_code"] == 127


def test_application_crash_is_failed_with_normalized_signal_status(project):
    result, run = launch(project, [sys.executable, "-c",
                                   "import os,resource,signal; resource.setrlimit(resource.RLIMIT_CORE,(0,0)); "
                                   "os.kill(os.getpid(),signal.SIGABRT)"])
    assert result.returncode == 128 + signal.SIGABRT
    summary = read(run / "summary.json")
    assert summary["state"] == "FAILED"
    assert summary["exit_code"] == 128 + signal.SIGABRT
    assert read(run / "run.json")["state"] == "FAILED"


@pytest.mark.parametrize("options", [
    ["--value", "loss=nan"], ["--value", "loss=inf"],
    ["--value", "loss=1", "--value", "loss=2"],
    ["--completed", "2", "--total", "1"], ["--completed", "1"],
])
def test_invalid_metrics_never_append_partial_rows(project, options):
    _, run = launch(project, [sys.executable, "-c", "pass"])
    result = subprocess.run([sys.executable, str(project[1] / "portable_run.py"), "metric", "--run", str(run),
                             *options], env=environment(), capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert (run / "metrics.jsonl").read_bytes() == b""


def test_term_reaches_payload_cleanup_and_retains_payload_exit(project):
    root, helper = project
    application = root / "signal_application.py"
    application.write_text('''import signal, sys, time
from pathlib import Path
def stop(signum, frame):
    Path("cleaned").write_text(str(signum))
    sys.exit(23)
signal.signal(signal.SIGTERM, stop)
Path("ready").touch()
while True:
    time.sleep(.05)
''')
    process = subprocess.Popen([sys.executable, str(helper / "portable_run.py"), "run", "--root", str(root),
                                "--experiment", "signal", "--script", "signal_application.py", "--", sys.executable,
                                str(application)], env=environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True)
    try:
        until = time.monotonic() + 5
        while not (root / "ready").exists() and time.monotonic() < until and process.poll() is None:
            time.sleep(.02)
        assert (root / "ready").exists()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 23, stderr
        assert (root / "cleaned").read_text() == str(signal.SIGTERM)
        summary = read(Path(stdout.strip()) / "summary.json")
        assert summary["state"] == "INTERRUPTED" and summary["exit_code"] == 23
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
