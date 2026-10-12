"""Exercise real report files and subprocesses, including adverse lifecycle paths."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from raven import research_reporting as P
from raven import projects


@pytest.fixture
def root(tmp_path):
    value = tmp_path / "project with spaces"
    value.mkdir()
    (value / "batch.sh").write_text("#!/bin/bash\n#SBATCH --output=logs-%j.out\ntrue\n")
    return value


def fake_submit(job="123", *, status=0):
    def run(argv, **kwargs):
        kwargs["stdout"].write((job + "\n").encode())
        kwargs["stderr"].write(b"failure" if status else b"")
        return SimpleNamespace(returncode=status)
    return run


def begin(root, job="123", **kwargs):
    return P.start(root, script="batch.sh", experiment="experiment", job_id=job, environ={}, **kwargs)


def document(path):
    return json.loads(path.read_text())


def test_submit_start_finish_real_inventory(root):
    job = P.submit(root, experiment="experiment", script="batch.sh", options=["--dependency=afterany:17?afterok:18"], runner=fake_submit())
    assert job == "123"
    pending = projects.discover_project(str(root))["runs"][0]
    assert pending["state"] == "SUBMITTED"
    assert "start" not in pending["inventory"]
    run = P.start(root, job_id=job, environ={})
    value = document(run / "run.json")
    assert value["state"] == "RUNNING"
    assert value["metadata"]["raven_launch"]["dependency"] == "afterany:17?afterok:18"
    assert len(document(run / "logs.json")["logs"]) == 3
    assert P.start(root, job_id=job, environ={}) == run
    P.finish(run, exit_code=1, runtime_seconds=2)
    assert document(run / "summary.json")["state"] == "FAILED"
    assert document(run / "summary.json")["runtime_seconds"] == 2
    assert P.finish(run, exit_code=1)["state"] == "FAILED"
    with pytest.raises(ValueError, match="different result"):
        P.finish(run, exit_code=0)
    with pytest.raises(ValueError, match="finished attempt"):
        P.start(root, job_id=job, environ={})


def test_empty_optional_descriptor_is_idempotent(root):
    assert P.initialize(root) == P.initialize(root)
    assert document(root / ".raven" / "project.json")["runs"] == "runs"


def test_requeue_and_cluster_are_distinct(root):
    first = begin(root)
    second = P.start(root, experiment="experiment", script="batch.sh", environ={"SLURM_JOB_ID": "123", "SLURM_RESTART_COUNT": "1"})
    remote = P.start(root, experiment="experiment", script="batch.sh", environ={"SLURM_JOB_ID": "123", "SLURM_CLUSTER_NAME": "other"})
    assert len({first, second, remote}) == 3
    assert document(second / "run.json")["attempt"] == 2


def test_array_concurrency_sparse_identity_and_binding(root):
    source = root / "array.json"
    source.write_text(json.dumps({"schema": "raven.array-manifest/v1", "cluster": "", "array_id": "1", "entries": [{"index": n, "id": f"sample-{n}"} for n in (2, 19, 44)]}))
    P.submit(root, experiment="experiment", script="batch.sh", options=["--array=2,19,44"], array_manifest=source, runner=fake_submit("500;cluster"))
    def one(n):
        return P.start(root, environ={"SLURM_ARRAY_JOB_ID": "500", "SLURM_ARRAY_TASK_ID": str(n), "SLURM_JOB_ID": str(700+n), "SLURM_CLUSTER_NAME": "cluster"})
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        runs = list(pool.map(one, (2, 19, 44)))
    assert len(set(runs)) == 3
    for n, run in zip((2, 19, 44), runs):
        manifest = document(run / "run.json")
        assert manifest["job_id"] == f"500_{n}"
        assert document(run / manifest["paths"]["array_manifest"])["array_id"] == "500"
        binding = projects.select_run(str(root), run.name)
        assert binding["binding"]["array_manifest"]


def test_unscoped_submission_can_start_on_authoritative_cluster(root):
    P.submit(root, experiment="experiment", script="batch.sh", runner=fake_submit())
    run = P.start(root, environ={"SLURM_JOB_ID": "123", "SLURM_CLUSTER_NAME": "cluster"})
    assert run.name == "job-123-a1"
    assert len(projects.discover_project(str(root))["runs"]) == 1
    assert document(run / "run.json")["metadata"]["raven_launch"]["cluster"] == "cluster"


def test_fast_runtime_before_sbatch_response_has_complete_identity(root):
    runs = []
    def fast(argv, **kwargs):
        env = dict(kwargs["env"], SLURM_JOB_ID="401", SLURM_CLUSTER_NAME="desktop")
        runs.append(P.start(root, environ=env))
        kwargs["stdout"].write(b"401\n")
        return SimpleNamespace(returncode=0)
    P.submit(root, experiment="experiment", script="batch.sh", parameters={"alpha": .5},
             options=["--dependency=afterok:400"], runner=fast)
    found = projects.discover_project(str(root))["runs"]
    assert len(found) == 1
    value = found[0]["inventory"]
    assert value["state"] == "RUNNING"
    assert value["parameters"] == {"alpha": .5}
    assert value["metadata"]["raven_launch"]["dependency"] == "afterok:400"
    assert value["metadata"]["raven_launch"]["submission_token"]
    assert P.start(root, environ={"SLURM_JOB_ID": "401", "SLURM_CLUSTER_NAME": "desktop"}) == runs[0]


def test_fast_array_before_response_has_real_manifest(root):
    source = root / "array.json"
    source.write_text(json.dumps({"schema": "raven.array-manifest/v1", "cluster": "", "array_id": "1", "entries": [{"index": 9, "id": "sample"}]}))
    runs = []
    def fast(argv, **kwargs):
        env = dict(kwargs["env"], SLURM_JOB_ID="700", SLURM_ARRAY_JOB_ID="600", SLURM_ARRAY_TASK_ID="9", SLURM_CLUSTER_NAME="desktop")
        runs.append(P.start(root, environ=env))
        kwargs["stdout"].write(b"600\n")
        return SimpleNamespace(returncode=0)
    P.submit(root, experiment="experiment", script="batch.sh", array_manifest=source, options=["--array=9"], runner=fast)
    assert document(runs[0] / "reports/array-manifest.json")["array_id"] == "600"
    assert document(runs[0] / "reports/array-manifest.json")["cluster"] == "desktop"
    assert len(projects.discover_project(str(root))["runs"]) == 2  # parent + exact task


def test_finish_recovers_summary_then_manifest_interruption(root, monkeypatch):
    run = begin(root)
    real = P.R._atomic_json
    def fail(path, value, **kwargs):
        if Path(path).name == "run.json":
            raise OSError("interrupted publication")
        return real(path, value, **kwargs)
    monkeypatch.setattr(P.R, "_atomic_json", fail)
    with pytest.raises(OSError):
        P.finish(run, exit_code=0)
    assert document(run / "summary.json")["state"] == "COMPLETED"
    assert document(run / "run.json")["state"] == "RUNNING"
    monkeypatch.setattr(P.R, "_atomic_json", real)
    P.finish(run, exit_code=0)
    assert document(run / "run.json")["state"] == "COMPLETED"


def test_exact_index_keeps_requeue_attempts(root):
    first = begin(root)
    second = P.start(root, experiment="experiment", script="batch.sh", environ={"SLURM_JOB_ID": "123", "SLURM_RESTART_COUNT": "1"})
    assert document(root / ".raven/jobs/123.json")["run_ids"] == [first.name, second.name]


def test_cluster_alias_becomes_permanently_ambiguous(root):
    def one(cluster):
        return P.start(root, experiment="experiment", script="batch.sh", environ={"SLURM_JOB_ID": "456", "SLURM_CLUSTER_NAME": cluster})
    first, second = one("alpha"), one("beta")
    assert first != second
    alias = root / ".raven/jobs/456.json"
    assert document(alias)["schema"] == "raven.job-index-ambiguous/v1"
    one("alpha")
    assert document(alias)["schema"] == "raven.job-index-ambiguous/v1"


@pytest.mark.parametrize("source", ["cli", "environment", "directive"])
@pytest.mark.parametrize("export", ["NONE", "NIL", "PATH", "ALL,RAVEN_SUBMISSION_TOKEN=oops"])
def test_incompatible_exports_never_submit(root, monkeypatch, source, export):
    options = []
    if source == "cli":
        options = ["--export=" + export]
    elif source == "environment":
        monkeypatch.setenv("SBATCH_EXPORT", export)
    else:
        (root / "batch.sh").write_text("#!/bin/bash\n#SBATCH --export=" + export + "\ntrue\n")
    calls = []
    with pytest.raises(ValueError, match="export"):
        P.submit(root, experiment="experiment", script="batch.sh", options=options, runner=lambda *a, **k: calls.append(a))
    assert not calls


@pytest.mark.parametrize("directive", ["hetjob", "packjob", "--wait", "-Mother", "--test-only", "--clu=other", "--export-file=/tmp/environment"])
def test_directive_mode_bypasses_never_submit(root, directive):
    (root / "batch.sh").write_text("#!/bin/bash\n#SBATCH " + directive + "\ntrue\n")
    calls = []
    with pytest.raises(ValueError):
        P.submit(root, experiment="experiment", script="batch.sh", runner=lambda *a, **k: calls.append(a))
    assert not calls


def test_run_metric_lock_does_not_wait_for_another_run_or_aggregation(root):
    first, second = begin(root), begin(root, "124")
    with P._lock(first), P._lock(root, "aggregate.lock"):
        assert P.main(["metric", "--run", str(second), "--value", "x=1"]) == 0
    assert document(second / "metrics.jsonl")["metrics"] == {"x": 1}


def test_metadata_obeys_slurm_environment_and_cli_precedence(root, monkeypatch):
    monkeypatch.setenv("SBATCH_DEPENDENCY", "afterany:10")
    monkeypatch.setenv("SBATCH_OUTPUT", "environment-%j.log")
    values = P._metadata_options(root / "batch.sh", ["--dependency=afterok:11"])
    assert values["dependency"] == "afterok:11"
    assert values["stdout"] == "environment-%j.log"


def test_launch_from_other_cwd_resolves_source_and_manifest_at_root(root, monkeypatch):
    source = root / "array.json"
    source.write_text(json.dumps({"schema": "raven.array-manifest/v1", "cluster": "", "array_id": "1", "entries": [{"index": 2, "id": "sample", "parameters": {"seed": 8}}]}))
    monkeypatch.chdir(root.parent)
    P.submit(root, experiment="experiment", script="batch.sh", array_manifest="array.json", options=["--array=2"], runner=fake_submit())
    run = P.start(root, environ={"SLURM_ARRAY_JOB_ID": "123", "SLURM_ARRAY_TASK_ID": "2", "SLURM_JOB_ID": "124"})
    assert document(run / "run.json")["parameters"] == {"seed": 8}


def test_signal_between_handler_install_and_popen_return_is_delivered(root, monkeypatch):
    import signal
    monkeypatch.setenv("SLURM_JOB_ID", "905")
    sent = []
    class Process:
        def poll(self):
            return None
        def send_signal(self, value):
            sent.append(value)
        def wait(self):
            return 0
    def popen(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        return Process()
    monkeypatch.setattr(P.subprocess, "Popen", popen)
    assert P.main(["exec", "--root", str(root), "--script", "batch.sh", "--experiment", "actual", "--", "command"]) == 143
    assert sent == [signal.SIGTERM]
    assert document(root / "runs/job-905-a1/summary.json")["state"] == "INTERRUPTED"


@pytest.mark.parametrize("option", ["--wrap=echo unsafe", "--wait", "--test-only", "--chdir=/tmp", "--clusters=other", "evil.sh", "--dependency", "--"])
def test_unsafe_submission_shape_never_calls_scheduler(root, option):
    calls = []
    with pytest.raises(ValueError):
        P.submit(root, experiment="experiment", script="batch.sh", options=[option], runner=lambda *a, **k: calls.append(a))
    assert not calls


@pytest.mark.parametrize("reply", ["123\n124", "Submitted batch job 123", "", "00012", "123;bad/name"])
def test_ambiguous_reply_retains_intent_and_no_run(root, reply):
    with pytest.raises(ValueError, match="unambiguous"):
        P.submit(root, experiment="experiment", script="batch.sh", runner=fake_submit(reply))
    records = list((root / ".raven" / "launches").glob("*.json"))
    assert len(records) == 1
    assert document(records[0])["status"] == "unknown"
    assert not (root / "runs").exists()


def test_timeout_records_unknown_and_does_not_retry(root):
    calls = []
    def timeout(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 30)
    with pytest.raises(ValueError, match="do not submit again"):
        P.submit(root, experiment="experiment", script="batch.sh", runner=timeout)
    assert len(calls) == 1
    assert document(next((root / ".raven" / "launches").glob("*.json")))["status"] == "unknown"


def test_metric_artifact_research_and_aggregate(root, capsys):
    run = begin(root)
    assert P.main(["metric", "--run", str(run), "--value", "loss=.5", "--completed", "2", "--total", "10"]) == 0
    row = document(run / "metrics.jsonl")
    assert row["metrics"] == {"loss": .5}
    assert row["progress"]["completed"] == 2
    assert P.main(["artifact", "outputs/results.json", "--format", "json", "--run", str(run)]) == 0
    value = document(run / "run.json")
    assert document(run / value["paths"]["contract"])["outputs"][0]["path"] == "outputs/results.json"
    source = root / "workflow.json"
    source.write_text(json.dumps({"version": 1, "kind": "raven.workflow", "nodes": []}))
    assert P.main(["research", "workflow", str(source), "--run", str(run)]) == 0
    assert document(run / "run.json")["paths"]["workflow"] == "reports/workflow.json"
    P.finish(run, exit_code=0, runtime_seconds=10, results={"accuracy": .8})
    bundle = P.aggregate(root)
    assert bundle["history"][0]["results"] == {"accuracy": .8}
    assert "cpu_seconds" not in bundle["history"][0]
    assert "memory_bytes" not in bundle["history"][0]
    assert document(root / "reports" / "planning.json") == bundle
    assert not (run / "reports" / "planning.json").exists()


@pytest.mark.parametrize("args", [
    ["--value", "loss=nan"], ["--value", "x=1", "--value", "x=2"],
    ["--completed", "5", "--total", "2"], ["--completed", "1"],
])
def test_bad_metrics_leave_empty_stream(root, args):
    run = begin(root)
    assert P.main(["metric", "--run", str(run), *args]) == 2
    assert (run / "metrics.jsonl").read_bytes() == b""


def test_external_metric_binding_does_not_write_wrong_stream(root):
    run = begin(root, metrics="telemetry/metrics.jsonl")
    assert P.main(["metric", "--run", str(run), "--value", "x=2"]) == 2
    assert (run / "metrics.jsonl").read_bytes() == b""


def test_exec_actual_subprocess_records_failure_and_child_directory(root, monkeypatch):
    monkeypatch.setenv("SLURM_JOB_ID", "901")
    code = P.main(["exec", "--root", str(root), "--script", "batch.sh", "--experiment", "actual",
                   "--", sys.executable, "-c", "import os,sys; print(os.environ['RAVEN_RUN_DIR']); print('oops',file=sys.stderr); sys.exit(7)"])
    assert code == 7
    run = root / "runs" / "job-901-a1"
    assert str(run) in (run / "logs" / "stdout.log").read_text()
    assert "oops" in (run / "logs" / "stderr.log").read_text()
    assert document(run / "summary.json")["exit_code"] == 7


def test_exec_missing_command_records_failure(root, monkeypatch):
    monkeypatch.setenv("SLURM_JOB_ID", "902")
    assert P.main(["exec", "--root", str(root), "--script", "batch.sh", "--experiment", "actual", "--", "/does/not/exist"]) == 127
    assert document(root / "runs/job-902-a1/summary.json")["state"] == "FAILED"


def test_exclusive_execution_claim_prevents_duplicate_command(root, monkeypatch):
    monkeypatch.setenv("SLURM_JOB_ID", "904")
    run = P.start(root, script="batch.sh", experiment="actual", environ={"SLURM_JOB_ID": "904"})
    P.R._atomic_json(run / ".execution.json", {"schema": "raven.execution/v1", "pid": 1, "started": 1})
    marker = root / "must-not-run"
    assert P.main(["exec", "--root", str(root), "--script", "batch.sh", "--experiment", "actual", "--", sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]) == 2
    assert not marker.exists()


def test_passport_uses_exact_job_and_refuses_secret_environment(root):
    run = begin(root)
    (root / "input.csv").write_text("a,b\n1,2\n")
    assert P.main(["passport", "--run", str(run), "--input", "input.csv"]) == 0
    value = document(next((run / "passports").glob("*.json")))
    assert value["job_id"] == "123"
    assert value["inputs"][0]["sha256"] is None
    assert P.main(["passport", "--run", str(run), "--environment", "API_TOKEN"]) == 2


def test_no_command_does_not_create_run(root, monkeypatch):
    monkeypatch.setenv("SLURM_JOB_ID", "903")
    assert P.main(["exec", "--root", str(root), "--script", "batch.sh", "--experiment", "actual"]) == 2
    assert not (root / "runs").exists()


def test_symlink_script_and_lock_are_refused(root):
    (root / "alias.sh").symlink_to(root / "batch.sh")
    with pytest.raises(ValueError):
        P.submit(root, experiment="experiment", script="alias.sh", runner=fake_submit())
    (root / ".raven").mkdir(exist_ok=True)
    (root / ".raven/report.lock").unlink(missing_ok=True)
    outside = root / "outside"
    outside.write_text("unchanged")
    (root / ".raven/report.lock").symlink_to(outside)
    with pytest.raises(OSError):
        begin(root)
    assert outside.read_text() == "unchanged"


def test_log_tokens_are_exact_and_unknown_rank_tokens_are_not_guessed(root):
    assert P._log_pattern("logs/%A_%a.%j.%%.out", "12_3", {"SLURM_JOB_ID": "91"}, root) == str(root / "logs/12_3.91.%.out")
    assert P._log_pattern("logs/%N-%t.out", "12_3", {}, root) is None
