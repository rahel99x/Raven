"""Real shell/process tests for the existing-script submission boundary."""
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

import pytest

from raven import launch_runtime as runtime
from raven import research_reporting as reporting
from raven.launch import resolve_plan


def read(path):
    return json.loads(Path(path).read_text())


def plan(tmp_path, source, *args, local=True, env=None):
    script = tmp_path / "ordinary.sh"
    script.write_text(source)
    argv = [str(script), "--project", str(tmp_path), *( ["--local"] if local else []), *args]
    return resolve_plan(argv, cwd=tmp_path, environ={} if env is None else env)


def frozen(tmp_path, source, *args):
    return runtime.materialize(plan(tmp_path, source, *args))


def test_real_shell_argv_status_cwd_binary_streams_and_no_slurm_identity(tmp_path, capfd):
    arguments = ["", "two words", "$(touch NEVER)", "; rm nothing", "--option", "雪"]
    value = plan(tmp_path, '#!/bin/bash\nprintf "a\\000b\\377"\nprintf "err\\000or" >&2\n' +
                 '"$RAVEN_REPORT_PYTHON" -c \'import json,os,sys;open("args.json","w").write(json.dumps(sys.argv[1:]));open("cwd","w").write(os.getcwd())\' "$@"\nexit 19\n', "--", *arguments)
    receipt = runtime.execute_plan(value)
    out, err = capfd.readouterr()
    assert out.startswith("a\x00b") and "err\x00or" in err
    assert read(tmp_path / "args.json") == arguments
    assert (tmp_path / "cwd").read_text() == str(tmp_path)
    assert receipt["exit_code"] == 19
    manifest = read(Path(receipt["run_dir"]) / "run.json")
    assert "job_id" not in manifest and manifest["state"] == "FAILED"
    assert not (tmp_path / "NEVER").exists()
    assert read(Path(receipt["run_dir"]) / "summary.json")["exit_code"] == 19
    assert read(Path(receipt["run_dir"]) / "logs.json")["logs"] == []


def test_snapshot_is_exclusive_private_and_runs_captured_bytes(tmp_path):
    value = frozen(tmp_path, '#!/bin/sh\nprintf original > result\nexec /bin/sh -c "exit 23"\n')
    Path(value["script"]).write_text('#!/bin/sh\ntouch SHOULD_NOT_EXIST\n')
    code, run = runtime.supervise(value)
    assert code == 23 and (tmp_path / "result").read_text() == "original"
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()
    assert read(run / "run.json")["provenance"]["script_sha256"] == value["script_sha256"]
    assert (Path(value["runtime_bundle"]).stat().st_mode & 0o777) == 0o400
    assert (Path(value["runtime_bundle"]).parent.stat().st_mode & 0o777) == 0o700
    with pytest.raises(FileExistsError):
        runtime.supervise(value)


def test_changed_source_never_materializes_or_submits(tmp_path):
    value = plan(tmp_path, '#!/bin/bash\nexit 0\n', local=False)
    Path(value["script"]).write_text('#!/bin/bash\nexit 17\n')
    with pytest.raises(ValueError, match="changed"):
        runtime.execute_plan(value, runner=lambda *a, **kw: pytest.fail("must not submit"))
    assert not (tmp_path / ".raven").exists()


def test_portable_runtime_and_metric_helper_after_clean_environment(tmp_path):
    value = frozen(tmp_path, '#!/bin/bash\nunset PYTHONPATH\n' +
                   '"$RAVEN_REPORT_PYTHON" -I "$RAVEN_REPORT_HELPER" metric --value loss=0.125 --step 2 --phase train\n')
    process = subprocess.run([value["report_python"], "-I", value["runtime_bundle"], "supervise", str(Path(value["runtime_bundle"]).with_name("plan.json"))],
                             cwd="/", env={"PATH": "/usr/bin:/bin"}, capture_output=True)
    assert process.returncode == 0, process.stderr
    run = tmp_path / "runs" / (value["run_id_base"] + "-a1")
    row = json.loads((run / "metrics.jsonl").read_text())
    assert row["metrics"]["loss"] == .125 and row["step"] == 2 and row["phase"] == "train"


@pytest.mark.parametrize("export", ["NONE", "NIL", "ONLY_THIS", "ALL"])
def test_single_submission_fast_start_preserves_header_precedence_and_isolated_exports(tmp_path, export):
    other = tmp_path / "work"; other.mkdir()
    value = plan(tmp_path, '#!/bin/bash\n#SBATCH --job-name="my experiment"\n#SBATCH --chdir=work\n#SBATCH --output="stdout log"\n#SBATCH --export=' + export + '\n\nprintf done > completed\n#SBATCH --array=1-999\n', local=False)
    calls = []
    def submit(argv, **kwargs):
        calls.append((argv, kwargs))
        batch = Path(argv[-1])
        header = batch.read_text()
        assert '#SBATCH --job-name="my experiment"' in header
        assert '#SBATCH --array=1-999' not in header
        assert "--export=ALL" not in argv
        assert kwargs["cwd"] == str(tmp_path)
        env = {"SLURM_JOB_ID": "441", "SLURM_CLUSTER_NAME": "lab", "SLURM_JOB_NAME": "my experiment", "PATH": "/usr/bin:/bin"}
        result = subprocess.run(["/bin/sh", str(batch)], cwd=other, env=env, capture_output=True)
        assert result.returncode == 0, result.stderr
        kwargs["stdout"].write(b"441;lab\n")
        return subprocess.CompletedProcess(argv, 0)
    receipt = runtime.execute_plan(value, runner=submit, environ={"SBATCH_EXPORT": export, "ONLY_THIS": "preserved"})
    assert len(calls) == 1 and calls[0][1]["env"] == {"SBATCH_EXPORT": export, "ONLY_THIS": "preserved"}
    assert (other / "completed").read_text() == "done"
    run = Path(receipt["run_dir"])
    assert read(run / "run.json")["state"] == "COMPLETED"
    assert list((tmp_path / "runs").iterdir()) == [run]
    logs = read(run / "logs.json")["logs"]
    assert logs[0]["path"] == str(other / "stdout log")


def test_arrays_requeues_and_recycled_ids_use_exact_existing_indexes(tmp_path):
    value = plan(tmp_path, '#!/bin/sh\nexit 0\n', "--sbatch=--array=2,19", local=False)
    def submit(argv, **kwargs):
        kwargs["stdout"].write(b"811;carc\n")
        return subprocess.CompletedProcess(argv, 0)
    receipt = runtime.execute_plan(value, runner=submit)
    saved = read(tmp_path / ".raven" / "launches" / (receipt["token"] + ".json"))
    runtime_plan = saved["plan"]
    runs = []
    for task, restart in [("2", "0"), ("19", "0"), ("2", "1")]:
        code, run = runtime.supervise(runtime_plan, environ={"SLURM_JOB_ID": "900", "SLURM_ARRAY_JOB_ID": "811", "SLURM_ARRAY_TASK_ID": task, "SLURM_RESTART_COUNT": restart, "SLURM_CLUSTER_NAME": "carc"})
        assert code == 0
        runs.append(run)
        assert read(run / "run.json")["job_id"] == "811_" + task
    assert len(set(runs)) == 3
    prefix = hashlib.sha256(b"carc").hexdigest()[:12]
    assert read(tmp_path / ".raven" / "jobs" / (prefix + "-811_2.json"))["run_ids"] == [runs[0].name, runs[2].name]
    # A fresh launch token on a recycled scheduler ID cannot overwrite history.
    later = runtime.execute_plan(value, runner=submit)
    assert later["run_id"] != receipt["run_id"]


def test_ambiguous_submission_never_retries_and_retains_intent(tmp_path):
    value = plan(tmp_path, '#!/bin/bash\nexit 0\n', local=False)
    calls = []
    def timeout(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 30)
    with pytest.raises(ValueError, match="unknown.*do not submit again"):
        runtime.execute_plan(value, runner=timeout)
    assert len(calls) == 1
    assert read(next((tmp_path / ".raven" / "launches").glob("*.json")))["status"] == "unknown"


def test_signal_forwarding_preserves_workload_trap_exit_status(tmp_path):
    value = frozen(tmp_path, '#!/bin/bash\ntrap \'printf caught > trapped; exit 42\' TERM\nprintf ready\\n\nwhile :; do read -r -t 1 _ || :; done\n')
    process = subprocess.Popen([value["report_python"], "-I", value["runtime_bundle"], "supervise", str(Path(value["runtime_bundle"]).with_name("plan.json"))], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        selector = selectors.DefaultSelector(); selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(10), "workload never became ready"
        os.read(process.stdout.fileno(), 5)
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=10)
        assert process.returncode == 42, err
        assert (tmp_path / "trapped").read_text() == "caught"
        run = tmp_path / "runs" / (value["run_id_base"] + "-a1")
        assert read(run / "summary.json")["state"] == "INTERRUPTED"
        assert read(run / "summary.json")["exit_code"] == 42
    finally:
        if process.poll() is None:
            process.kill(); process.wait()


def test_optional_recorder_failure_does_not_replace_application_status(tmp_path, monkeypatch):
    from raven import retained_telemetry
    monkeypatch.setattr(retained_telemetry, "start_recording", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    receipt = runtime.execute_plan(plan(tmp_path, '#!/bin/sh\nexit 33\n'))
    run = Path(receipt["run_dir"])
    assert receipt["exit_code"] == 33
    assert "disk full" in read(run / "run.json")["metadata"]["automation"]["warnings"][0]


def test_explicit_python_trace_hook_uses_same_attempt_and_preserves_args(tmp_path):
    (tmp_path / "app.py").write_text('import json,sys\nopen("python-args.json","w").write(json.dumps(sys.argv[1:]))\ndef work(): return 7\nwork()\nraise SystemExit(29)\n')
    value = plan(tmp_path, '#!/bin/bash\nsource "$RAVEN_SHELL_HELPER"\nraven_exec "$RAVEN_REPORT_PYTHON" app.py "$@"\n', "--trace", "--trace-adapter", "python-legacy-profile", "--", "", "hello 雪", "--x")
    receipt = runtime.execute_plan(value)
    assert receipt["exit_code"] == 29
    assert read(tmp_path / "python-args.json") == ["", "hello 雪", "--x"]
    run = Path(receipt["run_dir"])
    assert list((tmp_path / "runs").iterdir()) == [run]
    index = read(run / "trace" / "index.json")
    assert len(index["sources"]) == 2
    trace = read(run / index["sources"][0]["path"])
    assert trace["identity"]["run_id"] == run.name
    assert trace["identity"]["attempt"] == run.name
    assert trace["state"] == "CLOSED"


def test_shell_helper_plain_execution_preserves_options_and_traps(tmp_path):
    helper = tmp_path / "helper.sh"; helper.write_text(runtime.SHELL_HELPER)
    process = subprocess.run(["/bin/bash", "-c", 'set -eu; trap "printf original" EXIT; before=$-; source "$1"; test "$before" = "$-"; raven_exec /bin/sh -c "exit 0"', "shell", str(helper)], env={}, capture_output=True)
    assert process.returncode == 0 and process.stdout == b"original"

@pytest.mark.parametrize("batch_only", [True, False])
def test_slurm_signal_target_forwarding_does_not_duplicate_broadcast(tmp_path, batch_only):
    source = '#!/bin/bash\ntrap \'printf handled\\\\n; exit 41\' ' + ('USR1' if batch_only else 'TERM') + '\nprintf "%s\\n" "$$"\nwhile :; do read -r -t 1 _ || :; done\n'
    value = plan(tmp_path, source, *( ["--sbatch=--signal=B:USR1@60"] if batch_only else []), local=False)
    def submit(argv, **kwargs):
        kwargs["stdout"].write(b"922;uci\n")
        return subprocess.CompletedProcess(argv, 0)
    receipt = runtime.execute_plan(value, runner=submit)
    saved = read(tmp_path / ".raven" / "launches" / (receipt["token"] + ".json"))["plan"]
    process = subprocess.Popen([saved["report_python"], "-I", saved["runtime_bundle"], "supervise", str(Path(saved["runtime_bundle"]).with_name("plan.json"))],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env={"SLURM_JOB_ID": "922", "SLURM_CLUSTER_NAME": "uci", "PATH": "/usr/bin:/bin"})
    try:
        selector = selectors.DefaultSelector(); selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(10), "workload did not announce its PID"
        child_pid = int(process.stdout.readline())
        if batch_only:
            process.send_signal(signal.SIGUSR1)
        else:
            # Slurm cancellation delivers to all job processes itself. The
            # supervisor must not add a second signal to the child's trap.
            process.send_signal(signal.SIGTERM)
            os.killpg(child_pid, signal.SIGTERM)
        out, err = process.communicate(timeout=10)
        assert process.returncode == 41, err
        assert out.count(b"handled") == 1
        assert read(Path(receipt["run_dir"]) / "summary.json")["exit_code"] == 41
    finally:
        if process.poll() is None:
            process.kill(); process.wait()


def test_portable_trace_respects_existing_profiler_and_still_runs_application(tmp_path):
    (tmp_path / "sitecustomize.py").write_text('import sys\ndef profile(*args): pass\nsys.setprofile(profile)\n')
    (tmp_path / "app.py").write_text('import sys\nassert sys.getprofile() is not None\nopen("ran","w").write("yes")\nraise SystemExit(37)\n')
    value = plan(tmp_path, '#!/bin/bash\nexport PYTHONPATH="$PWD"\nsource "$RAVEN_SHELL_HELPER"\nraven_exec "$RAVEN_REPORT_PYTHON" app.py\n', "--trace", "--trace-adapter", "python-legacy-profile")
    receipt = runtime.execute_plan(value)
    assert receipt["exit_code"] == 37 and (tmp_path / "ran").read_text() == "yes"
    warning = read(Path(receipt["run_dir"]) / "run.json")["metadata"]["automation"]["warnings"]
    assert any("profiler" in text for text in warning)


def test_declared_per_attempt_metric_and_artifact_paths_and_scaling(tmp_path):
    value = plan(tmp_path, '#!/bin/sh\nexit 0\n', "--metrics-file", "native/{run_id}/metrics.jsonl", "--artifact", "outputs/{run_id}/result.csv", "--workers", "4", "--problem-size", "800", "--repeat", "2")
    receipt = runtime.execute_plan(value)
    run = Path(receipt["run_dir"])
    metadata = read(run / "run.json")["metadata"]["automation"]
    assert metadata["metric_binding"]["path"] == str(tmp_path / "native" / run.name / "metrics.jsonl")
    assert metadata["artifact_bindings"][0]["path"] == str(tmp_path / "outputs" / run.name / "result.csv")
    summary = read(run / "summary.json")
    assert (summary["workers"], summary["problem_size"], summary["repeat"]) == (4, 800, 2)


def test_known_accepted_id_survives_receipt_alias_write_failure(tmp_path, monkeypatch):
    value = plan(tmp_path, '#!/bin/sh\nexit 0\n', local=False)
    original = reporting.R._atomic_json
    def write(path, *args, **kwargs):
        if Path(path).parent.name == "submissions":
            raise OSError("read-only receipt directory")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(reporting.R, "_atomic_json", write)
    calls = []
    def submit(argv, **kwargs):
        calls.append(argv)
        kwargs["stdout"].write(b"717;carc\n")
        return subprocess.CompletedProcess(argv, 0)
    with pytest.raises(ValueError, match="717;carc WAS submitted.*do not resubmit"):
        runtime.execute_plan(value, runner=submit)
    intent = read(next((tmp_path / ".raven" / "launches").glob("*.json")))
    assert intent["status"] == "submitted" and intent["job_id"] == "717"
    assert len(calls) == 1


def test_discarded_slurm_logs_do_not_prevent_workload_start(tmp_path):
    value = plan(tmp_path, '#!/bin/sh\n#SBATCH --output=/dev/null\nexit 24\n', local=False)
    def submit(argv, **kwargs):
        kwargs["stdout"].write(b"616;uci\n")
        return subprocess.CompletedProcess(argv, 0)
    receipt = runtime.execute_plan(value, runner=submit)
    saved = read(tmp_path / ".raven" / "launches" / (receipt["token"] + ".json"))["plan"]
    code, run = runtime.supervise(saved, environ={"SLURM_JOB_ID": "616", "SLURM_CLUSTER_NAME": "uci"})
    assert code == 24
    assert any("regular retained log" in warning for warning in read(run / "run.json")["metadata"]["automation"]["warnings"])


def test_pending_submit_report_and_compact_index_do_not_duplicate_large_argv(tmp_path):
    argument = "x" * 100_000
    value = plan(tmp_path, '#!/bin/sh\nexit 0\n', "--", argument, local=False)
    def submit(argv, **kwargs):
        kwargs["stdout"].write(b"515;uci\n")
        return subprocess.CompletedProcess(argv, 0)
    receipt = runtime.execute_plan(value, runner=submit)
    run = Path(receipt["run_dir"])
    report = read(run / "reports" / "submit.json")
    assert report["schema"] == "raven.launch-report/v1" and report["plan"]["script_args"] == [argument]
    assert (run / "run.json").stat().st_size < 65536
    manifest = read(run / "run.json")
    assert "script_args" not in manifest["metadata"]["automation"]["plan"]
    assert manifest["metadata"]["automation"]["filesystem_id"] == read(tmp_path / ".raven" / "filesystem.json")["id"]


def test_changed_shared_filesystem_marker_prevents_wrong_project_execution(tmp_path):
    value = frozen(tmp_path, '#!/bin/sh\ntouch WRONG_PROJECT\n')
    marker = tmp_path / ".raven" / "filesystem.json"
    document = read(marker); document["id"] = "1" * 32; marker.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="filesystem marker changed"):
        runtime.supervise(value)
    assert not (tmp_path / "WRONG_PROJECT").exists() and not (tmp_path / "runs").exists()


def test_runtime_observations_survive_ambiguous_sbatch_response(tmp_path):
    value = plan(tmp_path, '#!/bin/sh\nexit 0\n', local=False)
    def submit(argv, **kwargs):
        result = subprocess.run(["/bin/sh", argv[-1]], cwd=tmp_path,
                                env={"SLURM_JOB_ID": "313", "SLURM_CLUSTER_NAME": "carc"}, capture_output=True)
        assert result.returncode == 0, result.stderr
        kwargs["stdout"].write(b"site banner\n313;carc\n")
        return subprocess.CompletedProcess(argv, 0)
    with pytest.raises(ValueError, match="unambiguous"):
        runtime.execute_plan(value, runner=submit)
    intent = read(next((tmp_path / ".raven" / "launches").glob("*.json")))
    assert intent["status"] == "unknown" and intent["observed_job_id"] == "313"
    assert intent["observed_cluster"] == "carc"
    assert read(tmp_path / "runs" / intent["observed_run_id"] / "summary.json")["state"] == "COMPLETED"


def test_passport_and_declared_native_research_export_bindings_are_exact(tmp_path):
    (tmp_path / "native.json").write_text(json.dumps({"schema": "lab.evidence/v1", "observed": 3}))
    value = plan(tmp_path, '#!/bin/sh\nexit 0\n', "--research", "passport", "evidence", "--research-source", "evidence=native.json")
    receipt = runtime.execute_plan(value)
    run = Path(receipt["run_dir"])
    bindings = {entry["id"]: entry for entry in read(run / "run.json")["metadata"]["automation"]["research_bindings"]}
    assert (run / bindings["passport"]["path"]).is_file()
    assert read(run / bindings["passport"]["path"])["schema"] == bindings["passport"]["schema"]
    assert read(run / bindings["evidence"]["path"])["observed"] == 3
    assert bindings["evidence"]["status"] == "unverified"
