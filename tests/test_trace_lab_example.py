"""Qualify the runnable heat example and its retained scientific evidence.

These are local CPU launches, not live Slurm or GPU qualification.
"""
import csv
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

from raven.metrics import MetricReader
from raven.trace_protocol import TraceReader


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "trace-lab"


@pytest.fixture(scope="module")
def heat_lab():
    specification = importlib.util.spec_from_file_location("raven_example_heat_lab", EXAMPLE / "heat_lab.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _environment():
    environment = {key: value for key, value in os.environ.items() if not key.startswith("RAVEN_")}
    environment["PYTHONPATH"] = str(ROOT)
    return environment


def _run(arguments, *, cwd, environment=None, timeout=30):
    process = subprocess.Popen(arguments, cwd=cwd, env=environment or _environment(),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        output, errors = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, errors = process.communicate(timeout=10)
        pytest.fail("Trace lab did not finish within its bounded test: " + errors[-4000:])
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
    return subprocess.CompletedProcess(arguments, process.returncode, output, errors)


def _application(tmp_path, name, *, workers=0, extra=()):
    output = tmp_path / name
    metrics = tmp_path / (name + ".jsonl")
    environment = _environment()
    environment["RAVEN_METRICS_FILE"] = str(metrics)
    command = [sys.executable, str(EXAMPLE / "heat_lab.py"), "--output-dir", str(output),
               "--grid", "8", "--steps", "3", "--pause", "0", "--workers", str(workers),
               *extra]
    return _run(command, cwd=tmp_path, environment=environment), output, metrics


def _last_progress(metrics):
    return [record["progress"] for line in metrics.read_text().splitlines()
            if "progress" in (record := json.loads(line))][-1]


@pytest.mark.parametrize("alpha", [.01, .20, .25])
def test_diffusion_conserves_heat_and_contracts_the_temperature_range(heat_lab, alpha):
    initial = heat_lab.initialize_field(12, 7)
    assert initial == heat_lab.initialize_field(12, 7)
    assert initial != heat_lab.initialize_field(12, 8)
    field = initial
    initial_heat = math.fsum(value for row in initial for value in row)
    for _ in range(20):
        previous = field
        field = heat_lab.advance_step(previous, alpha)
        assert min(map(min, field)) >= min(map(min, previous)) - 1e-12
        assert max(map(max, field)) <= max(map(max, previous)) + 1e-12
        assert math.fsum(value for row in field for value in row) == pytest.approx(initial_heat, rel=1e-12)
    checks = heat_lab.numerical_checks(initial, field)
    assert checks["heat_conserved"] and checks["maximum_principle"]
    assert checks["absolute_heat_drift"] <= checks["heat_drift_tolerance"]
    # Immutable input is useful both to the numerical scheme and spawned workers.
    assert initial == heat_lab.initialize_field(12, 7)


def test_single_and_spawned_analysis_preserve_seeded_results_and_artifact_contract(tmp_path):
    results = [_application(tmp_path, "workers-" + str(workers), workers=workers) for workers in (0, 1, 2)]
    summaries = []
    fields = []
    for result, output, metrics in results:
        assert result.returncode == 0, (result.stdout, result.stderr)
        summary = json.loads((output / "summary.json").read_text())
        summaries.append(summary)
        assert summary["schema"] == "raven.trace-lab/v1"
        assert summary["checks"]["heat_conserved"] and summary["checks"]["maximum_principle"]
        assert summary["parameters"]["seed"] == 7
        assert summary["deliberate_pause_seconds"] == 0
        assert summary["elapsed_seconds"] >= 0
        assert summary["field_csv"] == {"path": "final-field.csv", "rows": 8, "columns": 8, "header": False}
        with (output / "final-field.csv").open(newline="") as handle:
            field = [[float(value) for value in row] for row in csv.reader(handle)]
        fields.append(field)
        assert len(field) == 8 and all(len(row) == 8 for row in field)
        assert all(math.isfinite(value) for row in field for value in row)
        assert math.fsum(value for row in field for value in row) == summary["checks"]["final_heat"]
        assert [snapshot["step"] for snapshot in summary["snapshots"]] == [0, 1, 2, 3]
        assert all(snapshot["cells"] == sum(snapshot["histogram"]) == 64 for snapshot in summary["snapshots"])
        observed = MetricReader().read(metrics)
        assert observed["status"] == "ok" and not observed["errors"]
        progress = _last_progress(metrics)
        assert progress["completed"] == progress["total"] == 3
        for name, value in summary["final_metrics"].items():
            assert observed["latest"][name] == value
    for field, summary in zip(fields[1:], summaries[1:]):
        assert field == fields[0]
        assert summary["final_metrics"] == summaries[0]["final_metrics"]
        assert summary["checks"] == summaries[0]["checks"]
        for sequential, spawned in zip(summaries[0]["snapshots"], summary["snapshots"]):
            assert sequential["analysis_pids"] == [summaries[0]["parent_pid"]]
            assert summary["parent_pid"] not in spawned["analysis_pids"]
            for key in ("step", "cells", "histogram"):
                assert sequential[key] == spawned[key]
            # Different partition sums may differ by one rounding unit.
            for key in ("minimum", "maximum", "mean", "first_mode_amplitude"):
                assert sequential[key] == pytest.approx(spawned[key], rel=1e-12)


def test_existing_artifacts_are_not_replaced(tmp_path):
    result, output, _metrics = _application(tmp_path, "results")
    assert result.returncode == 0, result.stderr
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    result, _output, _metrics = _application(tmp_path, "results", extra=["--seed", "9"])
    assert result.returncode == 1 and "Refusing to replace" in result.stderr
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


def test_controlled_failure_retains_real_metrics_without_claiming_completed_results(tmp_path):
    result, output, metrics = _application(tmp_path, "failed", extra=["--fail-at-step", "2"])
    assert result.returncode == 1 and "Requested demonstration failure after step 2" in result.stderr
    assert not (output / "summary.json").exists()
    assert not (output / "final-field.csv").exists()
    observed = MetricReader().read(metrics)
    assert observed["status"] == "ok" and not observed["errors"]
    progress = _last_progress(metrics)
    assert progress["completed"] == 2
    assert progress["total"] == 3
    assert observed["latest"]["residual"] > 0
    assert observed["latest"]["total_heat"] > 0


@pytest.mark.parametrize("arguments", [["--alpha", ".26"], ["--alpha", "nan"], ["--pause", "inf"],
                                       ["--grid", "128", "--steps", "2000"], ["--workers", "5"]])
def test_invalid_or_unbounded_work_is_refused_before_artifact_creation(tmp_path, arguments):
    result, output, metrics = _application(tmp_path, "invalid", extra=arguments)
    assert result.returncode == 2 and "error:" in result.stderr
    assert not output.exists() and not metrics.exists()


def _recordings(run):
    index = run / "trace" / "index.json"
    deadline = time.monotonic() + 5
    while True:
        entries = json.loads(index.read_text())["sources"]
        sources = [entry for entry in entries if entry.get("schema") == "raven.trace/v1"]
        if all(entry["status"] != "recording" for entry in sources) or time.monotonic() >= deadline:
            break
        time.sleep(.01)
    snapshots = []
    for source in sources:
        assert source["status"] == "complete", source
        reader = TraceReader(run / source["path"], retain=10000)
        previous = -1
        while True:
            snapshot = reader.refresh(max_events=10000, max_bytes=8 << 20)
            assert snapshot["status"] != "INVALID", snapshot["warnings"]
            if snapshot["caught_up"]:
                break
            assert snapshot["read_seq"] > previous
            previous = snapshot["read_seq"]
        assert snapshot["status"] == "COMPLETE", snapshot["warnings"]
        assert snapshot["manifest"]["state"] == "CLOSED"
        assert snapshot["identity"]["attempt"] == run.name
        snapshots.append(snapshot)
    return snapshots


def _launch_example(tmp_path, extra=()):
    project = tmp_path / "project with spaces"
    project.mkdir()
    for name in ("heat_lab.py", "job.sh"):
        shutil.copyfile(EXAMPLE / name, project / name)
    receipt = project / "launch.json"
    output = project / "results"
    arguments = [sys.executable, "-m", "raven", "launch", str(project / "job.sh"),
                 "--project", str(project), "--local", "--trace", "--trace-helpers",
                 "--json", "--receipt-file", str(receipt)]
    if sys.version_info < (3, 12):
        arguments += ["--trace-adapter", "python-legacy-profile"]
    arguments += ["--", "--output-dir", str(output), "--grid", "8", "--steps", "3",
                  "--pause", "0", "--workers", "1", *extra]
    environment = _environment()
    environment["RAVEN_EXAMPLE_PYTHON"] = sys.executable
    # --project declares ownership; it does not change the submission cwd.
    result = _run(arguments, cwd=tmp_path, environment=environment)
    assert receipt.exists(), (result.stdout, result.stderr)
    receipt_data = json.loads(receipt.read_text())
    return result, receipt_data, output


def test_real_local_launch_records_main_and_spawned_analysis_with_metric_semantics(tmp_path):
    result, receipt_data, output = _launch_example(tmp_path)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert receipt_data["exit_code"] == 0
    run = Path(receipt_data["run_dir"])
    summary = json.loads((output / "summary.json").read_text())
    snapshots = _recordings(run)
    by_pid = {snapshot["identity"]["pid"]: snapshot for snapshot in snapshots}
    assert len(by_pid) == len(snapshots) >= 2
    parent = by_pid[summary["parent_pid"]]
    functions = {event.get("data", {}).get("function") for event in parent["events"]}
    assert {"main", "evolve", "advance_step", "update_row", "report_step", "write_results"} <= functions
    worker_ids = {pid for snapshot in summary["snapshots"] for pid in snapshot["analysis_pids"]}
    assert worker_ids and summary["parent_pid"] not in worker_ids
    for pid in worker_ids:
        helper = by_pid[pid]
        assert helper["identity"]["role"] == "helper"
        functions = {event.get("data", {}).get("function") for event in helper["events"]}
        assert {"analyze_region", "summarize_region", "histogram", "spatial_modes"} <= functions
    observed = MetricReader().read(run / "metrics.jsonl")
    assert observed["status"] == "ok" and not observed["errors"]
    for name, value in summary["final_metrics"].items():
        assert observed["latest"][name] == value
    metadata = json.loads((run / "metrics.meta.json").read_text())
    assert metadata["schema"] == "raven.metrics-meta/v1"
    assert metadata["kinds"]["residual"] == "residual"
    assert metadata["kinds"]["thermal_variance"] == "objective"
    assert metadata["kinds"]["total_heat"] == "conserved"
    assert metadata["baselines"]["total_heat"] == pytest.approx(summary["checks"]["initial_heat"])


def test_failed_local_launch_keeps_its_receipt_closed_traces_and_failure_evidence(tmp_path):
    result, receipt, output = _launch_example(tmp_path, ["--fail-at-step", "2"])
    assert result.returncode == receipt["exit_code"] == 1, (result.stdout, result.stderr)
    assert "Requested demonstration failure after step 2" in result.stderr
    assert not (output / "summary.json").exists()
    run = Path(receipt["run_dir"])
    manifest = json.loads((run / "run.json").read_text())
    assert manifest["state"] == "FAILED"
    assert manifest["metadata"]["automation"]["application_exit_code"] == 1
    snapshots = _recordings(run)
    assert len(snapshots) >= 2
    functions = {event.get("data", {}).get("function") for snapshot in snapshots for event in snapshot["events"]}
    assert {"main", "evolve", "analyze_region"} <= functions
    assert "write_results" not in functions
    observed = MetricReader().read(run / "metrics.jsonl")
    assert observed["status"] == "ok" and not observed["errors"]
    assert observed["latest"]["residual"] > 0
    assert _last_progress(run / "metrics.jsonl")["completed"] == 2


def _helper_environment(tmp_path, *, real_raven=False):
    binary = tmp_path / "bin"
    binary.mkdir()
    capture = tmp_path / "invocations.jsonl"
    if real_raven:
        source = "from raven.raven_launch import light_main\nraise SystemExit(light_main())\n"
    else:
        source = '''import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['TRACE_LAB_CAPTURE'], 'a') as handle:
    handle.write(json.dumps({'argv': args, 'cwd': os.getcwd(),
                             'python': os.environ.get('RAVEN_EXAMPLE_PYTHON'),
                             'environment': {key: value for key, value in os.environ.items()
                                 if key.startswith(('RAVEN_', 'SBATCH_')) or key in
                                 ('XDG_CONFIG_HOME', 'SLURM_CONF', 'MODULEPATH')}}) + '\\n')
if 'launch' in args:
    project = pathlib.Path(args[args.index('--project') + 1])
    receipt = pathlib.Path(args[args.index('--receipt-file') + 1])
    run = project / 'runs' / 'demo-123'
    (run / 'trace').mkdir(parents=True)
    sources = [{'schema': 'raven.trace/v1', 'path': 'trace/process-' + str(pid) + '/manifest.json',
                'run_id': run.name, 'stream': 'stream-' + str(pid),
                'status': 'complete', 'process': {'pid': pid, 'role': role}}
               for pid, role in [(40, 'helper'), (30, 'main')]]
    for entry in sources:
        manifest = run / entry['path']
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({'schema': 'raven.trace/v1', 'identity': {
            'attempt': run.name, 'run_id': run.name, 'pid': entry['process']['pid'],
            'stream': entry['stream'], 'job_id': 'local' if '--local' in args else '123'}}))
    (run / 'trace/index.json').write_text(json.dumps({'schema': 'raven.trace-index/v1', 'sources': sources}))
    document = {'schema': 'raven.launch-receipt/v1',
        'execution': 'local' if '--local' in args else 'slurm',
        'project_root': str(project), 'run_dir': str(run), 'run_id': run.name,
        'exit_code': int(os.environ.get('TRACE_LAB_EXIT', '0'))}
    if '--local' not in args:
        document['job_id'] = '123'
    receipt.write_text(json.dumps(document))
raise SystemExit(int(os.environ.get('TRACE_LAB_EXIT', '0')))
'''
    for name in ("raven", "ravenl"):
        path = binary / name
        path.write_text("#!" + sys.executable + "\n" + source)
        path.chmod(0o700)
    sbatch = binary / "sbatch"
    sbatch.write_text("#!/bin/sh\nexit 99\n")
    sbatch.chmod(0o700)
    environment = _environment()
    environment["PATH"] = str(binary) + os.pathsep + os.environ.get("PATH", "")
    environment["TRACE_LAB_CAPTURE"] = str(capture)
    return environment, capture


def _helper_run(project, *, mode="--local", extra=()):
    arguments = ["bash", str(EXAMPLE / "run.sh"), mode, "--directory", str(project),
                 "--python", sys.executable]
    if sys.version_info < (3, 12):
        arguments.append("--legacy-profile")
    return [*arguments, *extra]


def test_run_helper_keeps_paths_account_and_arguments_literal_and_uses_private_copy(tmp_path):
    environment, capture = _helper_environment(tmp_path)
    project = tmp_path / "project with 'quotes'; $(touch injected)"
    before = {name: (EXAMPLE / name).read_bytes() for name in ("heat_lab.py", "job.sh")}
    result = _run(_helper_run(project, mode="--slurm", extra=[
        "--account", "LabX;$(touch injected)", "--partition", "gpu partition", "--quick",
        "--", "--seed", "19"]), cwd=tmp_path, environment=environment)
    assert result.returncode == 0, (result.stdout, result.stderr)
    invocation = json.loads(capture.read_text())
    command = invocation["argv"]
    assert command[command.index("--site") + 1] == "default-slurm"
    assert command[command.index("--account") + 1] == "LabX;$(touch injected)"
    assert "--sbatch=--partition=gpu partition" in command
    assert command[command.index("--project") + 1] == str(project)
    assert command[command.index("--receipt-file") + 1] == str(project / "launch.json")
    assert command[-4:] == ["--seed", "19", "--output-dir", str(project / "results")]
    assert "--local" not in command
    assert invocation["cwd"] == str(project)
    assert invocation["python"] == sys.executable
    assert not (tmp_path / "injected").exists() and not (project / "injected").exists()
    assert project.stat().st_mode & 0o777 == 0o700
    assert (project / "raven-view.json").stat().st_mode & 0o777 == 0o600
    for name, original in before.items():
        assert (project / name).read_bytes() == (EXAMPLE / name).read_bytes() == original


def test_run_helper_isolates_recording_overrides_and_preserves_site_configuration(tmp_path):
    environment, capture = _helper_environment(tmp_path)
    stale = {"RAVEN_METRICS_FILE": "/another/run/metrics.jsonl", "RAVEN_TRACE_SKIP_AUTO": "1",
             "RAVEN_RUN_ID": "old-attempt", "RAVEN_PROJECT_ROOT": "/another/project",
             "SBATCH_ARRAY_INX": "1-2", "SBATCH_ACCOUNT": "another-lab", "SBATCH_PARTITION": "other"}
    preserved = {"RAVEN_SITE_POLICY": "/etc/cluster/raven-policy.json", "SLURM_CONF": "/etc/cluster/slurm.conf",
                 "MODULEPATH": "/opt/site/modules", "RAVEN_PROFILE": "unrelated-profile",
                 # Nested launch must retain this pair long enough to verify
                 # ownership before removing the previous runtime from PYTHONPATH.
                 "RAVEN_RUN_DIR": "/another/run", "RAVEN_LAUNCH_PLAN": "/another/plan.json"}
    environment.update(stale, **preserved, RAVEN_TRACE_AUTO="0", XDG_CONFIG_HOME=str(tmp_path / "user-config"))
    project = tmp_path / "private example"
    result = _run(_helper_run(project), cwd=tmp_path, environment=environment)
    assert result.returncode == 0, (result.stdout, result.stderr)
    invocation = json.loads(capture.read_text())
    child = invocation["environment"]
    assert not (stale.keys() & child.keys())
    assert all(child[key] == value for key, value in preserved.items())
    assert child["RAVEN_TRACE_AUTO"] == "1"
    assert child["XDG_CONFIG_HOME"] == str(project / ".example-config")
    assert (project / ".example-config").stat().st_mode & 0o777 == 0o700
    command = invocation["argv"]
    assert command[command.index("--trace-scope") + 1] == "project"
    assert command[command.index("--trace-adapter") + 1] == (
        "python-legacy-profile" if sys.version_info < (3, 12) else "python-monitoring")
    assert "--no-gpu" in command and "--no-export-on-finish" in command


@pytest.mark.parametrize("extra", [["--account", "LabX"], ["--", "--output-dir", "elsewhere"],
                                   ["--", "--output=elsewhere"]])
def test_run_helper_refuses_mode_mistakes_and_output_redirection_before_launch(tmp_path, extra):
    environment, capture = _helper_environment(tmp_path)
    project = tmp_path / "new project"
    result = _run(_helper_run(project, extra=extra), cwd=tmp_path, environment=environment)
    assert result.returncode == 2
    assert not project.exists() and not capture.exists()


def test_run_helper_refuses_existing_directory_and_preserves_failed_launch_receipt(tmp_path):
    environment, capture = _helper_environment(tmp_path)
    project = tmp_path / "run"
    project.mkdir()
    (project / "keep.txt").write_text("previous work")
    result = _run(_helper_run(project), cwd=tmp_path, environment=environment)
    assert result.returncode == 2 and not capture.exists()
    assert list(project.iterdir()) == [project / "keep.txt"]
    environment["TRACE_LAB_EXIT"] = "7"
    failed = tmp_path / "failed"
    result = _run(_helper_run(failed), cwd=tmp_path, environment=environment)
    assert result.returncode == 7
    assert json.loads((failed / "launch.json").read_text())["exit_code"] == 7
    assert str(failed / "launch.json") in result.stdout


def _fake_receipt(tmp_path, *, mode="--slurm"):
    environment, capture = _helper_environment(tmp_path)
    project = tmp_path / "project"
    result = _run(_helper_run(project, mode=mode), cwd=tmp_path, environment=environment)
    assert result.returncode == 0, (result.stdout, result.stderr)
    return environment, capture, project / "launch.json"


def test_view_helper_reopens_exact_receipt_main_or_requested_process_and_prints_history(tmp_path):
    environment, capture, receipt = _fake_receipt(tmp_path)
    command = ["bash", str(EXAMPLE / "view.sh"), str(receipt)]
    result = _run([*command, "--history", "--no-open"], cwd=tmp_path, environment=environment)
    assert result.returncode == 0 and "--tab history" in result.stdout
    assert "--site default-slurm" in result.stdout
    assert "select job 123" in result.stdout and ":run select demo-123" in result.stdout
    assert len(capture.read_text().splitlines()) == 1
    result = _run([*command, "--inspect"], cwd=tmp_path, environment=environment)
    assert result.returncode == 0, result.stderr
    invocation = json.loads(capture.read_text().splitlines()[-1])
    assert invocation["argv"][invocation["argv"].index("--site") + 1] == "default-slurm"
    assert invocation["argv"][-3:] == ["trace", "inspect", str(receipt.parent / "runs/demo-123/trace/process-30/manifest.json")]
    result = _run([*command, "--process", "40"], cwd=tmp_path, environment=environment)
    assert result.returncode == 0
    assert json.loads(capture.read_text().splitlines()[-1])["argv"][-1].endswith("process-40/manifest.json")
    before = capture.read_bytes()
    result = _run([*command, "--process", "999"], cwd=tmp_path, environment=environment)
    assert result.returncode == 2 and "no registered trace" in result.stderr
    assert capture.read_bytes() == before


@pytest.mark.parametrize("mutation", ["relative", "different_attempt", "run_symlink", "manifest_escape", "remote_config",
                                      "extended_config"])
def test_view_helper_refuses_changed_identity_or_connection_before_opening(tmp_path, mutation):
    environment, capture, receipt = _fake_receipt(tmp_path)
    data = json.loads(receipt.read_text())
    run = Path(data["run_dir"])
    if mutation == "relative":
        data["project_root"] = "project"
    elif mutation == "different_attempt":
        data["run_dir"] = str(run.parent / "another-run")
    elif mutation == "run_symlink":
        run.rename(run.parent / "another-run")
        run.symlink_to(run.parent / "another-run", target_is_directory=True)
    elif mutation == "manifest_escape":
        index = run / "trace/index.json"
        entries = json.loads(index.read_text())
        entries["sources"][0]["path"] = "../../outside/manifest.json"
        index.write_text(json.dumps(entries))
    else:
        configuration = receipt.parent / "raven-view.json"
        config = json.loads(configuration.read_text())
        if mutation == "remote_config":
            config["connection"]["host"] = "another-host"
        else:
            config["plugins"] = {"enabled": True, "paths": ["unexpected.py"]}
        configuration.write_text(json.dumps(config))
    receipt.write_text(json.dumps(data))
    before = capture.read_bytes()
    result = _run(["bash", str(EXAMPLE / "view.sh"), str(receipt), "--inspect"],
                  cwd=tmp_path, environment=environment)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert capture.read_bytes() == before


@pytest.mark.parametrize("field", ["index_run_id", "attempt", "run_id", "stream", "pid", "job_id", "identity_object"])
def test_view_helper_requires_matching_attempt_process_and_stream_evidence(tmp_path, field):
    environment, capture, receipt = _fake_receipt(tmp_path)
    run = Path(json.loads(receipt.read_text())["run_dir"])
    index_path = run / "trace/index.json"
    index = json.loads(index_path.read_text())
    if field == "index_run_id":
        index["sources"][0]["run_id"] = "another-attempt"
        index_path.write_text(json.dumps(index))
    else:
        path = run / index["sources"][0]["path"]
        manifest = json.loads(path.read_text())
        if field == "identity_object":
            manifest["identity"] = []
        else:
            manifest["identity"][field] = 999 if field == "pid" else "different-identity"
        path.write_text(json.dumps(manifest))
    before = capture.read_bytes()
    result = _run(["bash", str(EXAMPLE / "view.sh"), str(receipt), "--inspect"],
                  cwd=tmp_path, environment=environment)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "another run attempt" in result.stderr or "identity differs" in result.stderr
    assert capture.read_bytes() == before


def test_view_helper_accepts_same_attempt_activity_without_redundant_run_alias(tmp_path):
    environment, capture, receipt = _fake_receipt(tmp_path)
    run = Path(json.loads(receipt.read_text())["run_dir"])
    index = json.loads((run / "trace/index.json").read_text())
    path = run / index["sources"][0]["path"]
    document = json.loads(path.read_text())
    del document["identity"]["run_id"]
    path.write_text(json.dumps(document))
    result = _run(["bash", str(EXAMPLE / "view.sh"), str(receipt), "--no-open"],
                  cwd=tmp_path, environment=environment)
    assert result.returncode == 0, result.stderr
    assert ":run select " + run.name in result.stdout
    assert len(capture.read_text().splitlines()) == 1


@pytest.mark.parametrize("kind", ["oversized", "fifo", "non_object"])
def test_view_helper_bounds_receipt_reads_and_never_blocks_on_pipes(tmp_path, kind):
    receipt = tmp_path / "receipt.json"
    if kind == "oversized":
        receipt.write_bytes(b" " * (1024 * 1024 + 1))
    elif kind == "fifo":
        os.mkfifo(receipt)
    else:
        receipt.write_text("[]")
    result = _run(["bash", str(EXAMPLE / "view.sh"), str(receipt), "--no-open"], cwd=tmp_path, timeout=5)
    assert result.returncode == 2


def test_real_run_and_view_helpers_complete_small_local_workload_without_scheduler(tmp_path):
    environment, _capture = _helper_environment(tmp_path, real_raven=True)
    # The example's explicit scoped site must win over a user's unrelated saved
    # selection for both the real workload launch and retained-trace inspector.
    environment["RAVEN_PROFILE"] = "unrelated-profile"
    from raven.launch_settings import validate_settings
    user_config = tmp_path / "user-config"
    (user_config / "raven").mkdir(parents=True)
    automation = user_config / "raven/automation.json"
    settings = validate_settings({"launch": {"trace_adapter": "mpi", "trace_scope": "all", "gpu": True,
                                 "auto_export": {"include": ["summary"]},
                                 "sbatch_defaults": {"array": "1-2", "account": "another-lab"}}})
    original = json.dumps(settings).encode()
    automation.write_bytes(original)
    stale_metrics = tmp_path / "old-metrics.jsonl"
    environment.update(XDG_CONFIG_HOME=str(user_config), RAVEN_METRICS_FILE=str(stale_metrics),
                       RAVEN_TRACE_AUTO="0", RAVEN_TRACE_SKIP_AUTO="1", SBATCH_ARRAY_INX="1-2")
    project = tmp_path / "real helper project"
    result = _run(_helper_run(project, extra=["--", "--grid", "8", "--steps", "3",
                  "--pause", "0", "--workers", "1"]), cwd=tmp_path, environment=environment)
    assert result.returncode == 0, (result.stdout, result.stderr)
    receipt = project / "launch.json"
    data = json.loads(receipt.read_text())
    assert data["execution"] == "local" and data["exit_code"] == 0
    plan = json.loads((project / ".raven/wrappers" / data["token"] / "plan.json").read_text())
    assert plan["settings"]["trace_adapter"] == (
        "python-legacy-profile" if sys.version_info < (3, 12) else "python-monitoring")
    assert plan["settings"]["trace_scope"] == "project" and plan["settings"]["gpu"] is False
    assert plan["auto_export"] is False
    assert "array" not in plan["effective_options"] and "account" not in plan["effective_options"]
    assert not stale_metrics.exists() and automation.read_bytes() == original
    observed = MetricReader().read(Path(data["run_dir"]) / "metrics.jsonl")
    assert observed["status"] == "ok" and observed["latest"]["total_heat"] > 0
    assert json.loads((project / "results/summary.json").read_text())["checks"]["heat_conserved"]
    result = _run(["bash", str(EXAMPLE / "view.sh"), str(receipt)], cwd=tmp_path, environment=environment)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "Standalone run" in result.stdout and str(project / "runs" / data["run_id"]) in result.stdout
    assert "bounded prefix, not every recorded event" in result.stdout
    assert "checked events" in result.stdout and "INVALID" not in result.stdout
    # The CLI deliberately reads a bounded prefix; page every journal separately
    # to qualify complete on-disk evidence rather than relabeling that prefix.
    assert len(_recordings(Path(data["run_dir"]))) >= 2
