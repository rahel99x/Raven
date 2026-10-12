"""Public CLI acceptance: ordinary scripts → exact runs → independently read exports.

These tests execute real subprocesses and the shipped example, without a TUI,
scheduler, GPU, or Raven installation in the application's environment. They
qualify the local workflow, not Slurm/device/shared-filesystem behavior.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path):
    target = tmp_path / "student project Ω"
    shutil.copytree(ROOT / "examples/automated-reporting", target)
    return target


def environment():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("RAVEN_", "SLURM_", "SBATCH_")) and key != "PYTHONPATH"}
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


def cli(project, *args, timeout=30):
    if args and args[0] != "export-data":
        # An ancestor may be a real Raven project; isolate this explicit example.
        split = 2 if args[0] == "launch" else 1
        args = (*args[:split], "--project", str(project), *args[split:])
    return subprocess.run([sys.executable, str(ROOT / "raven"), *args], cwd=project,
                          env=environment(), capture_output=True, timeout=timeout)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sole_run(project):
    manifests = list((project / "runs").glob("*/run.json"))
    assert len(manifests) == 1
    return manifests[0].parent, read(manifests[0])


def validate_bundle(receipt):
    """Read all emitted files independently of Raven's parser/export helpers."""
    assert receipt["status"] == "complete", receipt
    assert len(receipt["outputs"]) == 1
    bundle = Path(receipt["outputs"][0])
    manifest = read(bundle / "manifest.json")
    assert (bundle / "README.txt").is_file()
    csv_rows = []
    for item in manifest["files"]:
        path = bundle / item["path"]
        raw = path.read_bytes()
        assert len(raw) == item["bytes"]
        assert hashlib.sha256(raw).hexdigest() == item["sha256"]
        if path.suffix == ".csv":
            with path.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            assert len(rows) == item["records"]
            csv_rows.extend(rows)
        elif path.suffix == ".json":
            json.loads(raw)
    return bundle, manifest, csv_rows


def test_canonical_and_shorthand_preview_do_not_create_state(project):
    before = {str(path.relative_to(project)): path.read_bytes() for path in project.rglob("*") if path.is_file()}
    canonical = cli(project, "launch", "test.sh", "--local", "--research", "scaling", "forecast",
                    "--gpu", "--trace", "--check", "--json", "--", "--steps", "3")
    shorthand = cli(project, "test.sh", "--local", "--r", "sc", "f", "--gpu", "--trace",
                    "--check", "--json", "--", "--steps", "3")
    assert canonical.returncode == shorthand.returncode == 0, (canonical.stderr, shorthand.stderr)
    left, right = json.loads(canonical.stdout), json.loads(shorthand.stdout)
    for name in ("script", "script_sha256", "project_root", "workdir", "execution", "features", "script_args"):
        assert left[name] == right[name], name
    assert left["execution"] == "local"
    assert left["script_args"] == ["--steps", "3"]
    after = {str(path.relative_to(project)): path.read_bytes() for path in project.rglob("*") if path.is_file()}
    assert after == before
    assert not (project / "runs").exists()
    assert not (project / ".raven").exists()
    assert not (project / "exports").exists()


def test_ordinary_script_keeps_args_workdir_and_binary_streams(project):
    script = project / "ordinary.sh"
    original = b'#!/bin/bash\nset -eu\npython3 capture.py "$@"\n'
    script.write_bytes(original)
    (project / "capture.py").write_text(
        "import json,os,sys\n"
        "with open('observed.json','w') as f: json.dump({'args':sys.argv[1:],'cwd':os.getcwd()},f)\n"
        "os.write(1,b'application\\x00stdout\\n')\nos.write(2,b'application\\x00stderr\\n')\n",
        encoding="utf-8")
    arguments = ["", "space separated", "$(never_execute)", "Ω", "--help", "semi;colon"]
    result = cli(project, "launch", "ordinary.sh", "--local", "--", *arguments)
    assert result.returncode == 0, result.stderr
    assert script.read_bytes() == original
    assert read(project / "observed.json") == {"args": arguments, "cwd": str(project)}
    assert result.stdout == b"application\x00stdout\n"
    assert b"application\x00stderr\n" in result.stderr
    assert b"Run: " in result.stderr
    run, manifest = sole_run(project)
    assert manifest["state"] == "COMPLETED"
    assert not manifest.get("job_id")
    assert read(run / "summary.json")["exit_code"] == 0


@pytest.mark.parametrize("destination", ["project", "relative", "absolute"])
def test_example_launch_and_export_without_dashboard(project, tmp_path, destination):
    result = cli(project, "launch", "test.sh", "--local", "--research", "experiment", "--",
                 "--steps", "3", "--terms-per-step", "10")
    assert result.returncode == 0, result.stderr
    run, manifest = sole_run(project)
    source_rows = [json.loads(line) for line in (run / "metrics.jsonl").read_text().splitlines()]
    assert len(source_rows) == 3
    assert source_rows[-1]["progress"]["completed"] == 3
    result_data = read(run / "outputs/result.json")
    assert result_data["terms"] == 30
    assert 3 < result_data["pi_estimate"] < 4
    options = []
    parent = project / "exports"
    if destination == "relative":
        options = ["--relative-dir", "figures/experiment α/data"]
        parent = project / "figures/experiment α/data"
    elif destination == "absolute":
        parent = tmp_path / "external destination λ"
        options = ["--absolute-dir", str(parent)]
    selection = ["export-data", "--project", str(project), "--run", manifest["run_id"],
                 "--include", "application,summary", "--metric", "absolute_error", *options, "--json"]
    preview = cli(project, *selection, "--check")
    assert preview.returncode == 0, preview.stderr
    assert not parent.exists()
    exported = cli(project, *selection)
    assert exported.returncode == 0, exported.stderr
    bundle, exported_manifest, rows = validate_bundle(json.loads(exported.stdout))
    assert bundle.is_relative_to(parent)
    assert len(rows) == 3
    for original, exported_row in zip(source_rows, rows):
        assert exported_row["run_id"] == manifest["run_id"]
        assert exported_row["metric"] == "absolute_error"
        assert float(exported_row["value"]) == original["metrics"]["absolute_error"]
        assert float(exported_row["timestamp"]) == original["t"]
        assert exported_row["phase"] == "solve"
        assert exported_row["job_id"] == ""
    # Publication is immutable: a repeated user export gets a distinct bundle.
    previous = (bundle / "manifest.json").read_bytes()
    second = cli(project, *selection)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["outputs"] != [str(bundle)]
    assert (bundle / "manifest.json").read_bytes() == previous


def test_failed_workload_keeps_measurements_and_export_is_independent(project):
    result = cli(project, "launch", "test.sh", "--local", "--research", "experiment", "--",
                 "--steps", "5", "--terms-per-step", "10", "--fail-after", "2")
    assert result.returncode == 7, result.stderr
    run, manifest = sole_run(project)
    assert manifest["state"] == "FAILED"
    assert read(run / "summary.json")["exit_code"] == 7
    assert len((run / "metrics.jsonl").read_text().splitlines()) == 2
    assert not (run / "outputs/result.json").exists()
    exported = cli(project, "export-data", "--project", str(project), "--run", manifest["run_id"],
                   "--include", "application,summary", "--json")
    assert exported.returncode == 0, exported.stderr
    _, _, rows = validate_bundle(json.loads(exported.stdout))
    assert len(rows) == 4
    assert {row["run_id"] for row in rows} == {manifest["run_id"]}


def test_missing_requested_gpu_is_partial_instead_of_zero_series(project):
    result = cli(project, "launch", "test.sh", "--local", "--no-gpu", "--", "--steps", "1")
    assert result.returncode == 0, result.stderr
    _, manifest = sole_run(project)
    exported = cli(project, "export-data", "--project", str(project), "--run", manifest["run_id"],
                   "--include", "application,gpu", "--json")
    assert exported.returncode == 3, exported.stderr
    receipt = json.loads(exported.stdout)
    assert receipt["status"] == "partial"
    assert receipt["issues"]
    bundle = Path(receipt["outputs"][0])
    assert not list(bundle.rglob("gpu.csv"))


def test_example_also_runs_without_any_raven_environment(project):
    result = subprocess.run(["bash", "test.sh", "--steps", "2", "--terms-per-step", "10"],
                            cwd=project, env=environment(), capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert b"step=2" in result.stdout
    assert not (project / "runs").exists()


def test_local_machine_receipt_preserves_stdout_and_refuses_existing_path(project):
    receipt_path = project / "exact receipt.json"
    result = cli(project, "launch", "test.sh", "--local", "--json", "--receipt-file", str(receipt_path),
                 "--", "--steps", "1", "--terms-per-step", "10")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(b"step=1 pi_estimate=")
    assert b"raven.launch-receipt" not in result.stdout
    receipt = read(receipt_path)
    run, manifest = sole_run(project)
    assert receipt["run_id"] == manifest["run_id"]
    assert receipt["run_dir"] == str(run)
    original = receipt_path.read_bytes()
    repeated = cli(project, "launch", "test.sh", "--local", "--json", "--receipt-file", str(receipt_path))
    assert repeated.returncode == 2
    assert receipt_path.read_bytes() == original
    assert len(list((project / "runs").glob("*/run.json"))) == 1


def test_explicit_python_trace_hook_uses_the_same_reporting_attempt(project):
    result = cli(project, "launch", "traced.sh", "--local", "--trace",
                 "--trace-adapter", "python-legacy-profile", "--", "--steps", "1", "--terms-per-step", "10")
    assert result.returncode == 0, result.stderr
    run, manifest = sole_run(project)
    trace = read(run / "trace/index.json")
    assert trace["sources"]
    for source in trace["sources"]:
        assert source["status"] == "complete", source
        assert (run / source["path"]).is_file()
    recorded = [json.loads(line) for line in (run / "metrics.jsonl").read_text().splitlines()]
    assert len(recorded) == 1
    exported = cli(project, "export-data", "--project", str(project), "--run", manifest["run_id"],
                   "--include", "trace,application", "--json")
    assert exported.returncode == 0, exported.stderr
    bundle, evidence, _ = validate_bundle(json.loads(exported.stdout))
    native = [entry for entry in evidence["files"] if entry.get("family") == "trace"]
    assert native
    assert any((bundle / entry["path"]).suffix == ".jsonl" for entry in native)
