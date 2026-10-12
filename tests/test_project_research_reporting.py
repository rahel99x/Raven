"""Copied stdlib reports round-trip through native per-run research readers."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from raven import projects
from raven.planning import analyze
from raven.research import ResearchHub
from raven.submission import prepare, _digest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "examples" / "project-template"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "Portable project with spaces"
    shutil.copytree(TEMPLATE, root)
    spec = importlib.util.spec_from_file_location("portable_research_reporting", root / "reporting.py")
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    run = producer.begin_run(root, "attempt-1", name="pi-series", script="experiment.py", job_id="12345_7")
    return root, producer, run


def bundle():
    return {"version": 1, "kind": "raven.planning", "history": [], "job_id": "12345_7",
            "jobs": [{"id": "12345_7", "name": "pi-series", "state": "PENDING",
                      "partition": "main", "cpus": 1, "nodes": 1, "gpus": 0,
                      "submit": 1700000000, "reason": "Dependency"}], "now": 1700000010}


def test_new_runs_declare_future_reports_without_creating_measurements(project):
    root, _, run = project
    document = json.loads((run / "run.json").read_text())
    assert document["paths"]["planning"] == "reports/planning.json"
    assert document["paths"]["submit"] == "reports/submit.json"
    assert list((run / "reports").iterdir()) == []
    selected = projects.select_run(str(root), run.name)
    assert selected["binding"]["planning_file"] == str(run / "reports/planning.json")
    assert any("planning: not created yet" in warning for warning in selected["warnings"])
    assert any("submit: not created yet" in warning for warning in selected["warnings"])
    assert selected["binding"]["job_id"] == "12345_7"


@pytest.mark.parametrize("view", ["predict", "forecast", "blockers", "tradeoffs", "scaling", "workflow"])
def test_per_view_sources_round_trip_without_fabricating_analysis(project, view):
    root, producer, run = project
    document = bundle()
    if view == "workflow":
        document = json.loads((root / ".raven/definitions/workflow.json").read_text())
    path = producer.publish_research(run, view, document)
    selected = projects.select_run(str(root), run.name)
    assert selected["binding"]["planning_files"][view] == str(path)
    loaded = projects.read_bound_json(selected["binding"], str(path), key=view)
    assert loaded == document
    result = analyze(view, loaded)
    assert isinstance(result, dict) and result["status"] in {
        "ok", "partial", "empty", "insufficient", "incomplete", "uncalibrated", "blocked", "unavailable"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list((run / "reports").glob(".*.json.*"))


def test_shared_aggregate_and_view_override_are_distinct_exact_files(project):
    root, producer, run = project
    producer.finish_run(run, state="FAILED", runtime_seconds=.2, exit_code=1)
    aggregate = producer.export_planning(root, ["runs/attempt-1"], reference_run="runs/attempt-1",
                                        output="runs/attempt-1/reports/planning.json")
    producer.publish_research(run, "predict", aggregate)
    selected = projects.select_run(str(root), run.name)
    binding = selected["binding"]
    assert binding["planning_file"] != binding["planning_files"]["predict"]
    assert projects.read_bound_json(binding, binding["planning_file"], key="planning")["history"][0]["state"] == "FAILED"
    assert projects.read_bound_json(binding, binding["planning_files"]["predict"], key="predict") == aggregate
    assert "job_id" not in aggregate  # query/reference does not invent a queued snapshot


def test_actual_native_preflight_is_preserved_as_read_only_report(project, monkeypatch):
    root, producer, run = project
    plan = prepare("jobs/run.sbatch", workdir=str(root))
    assert plan["schema"] == "raven.submission-plan/v1" and plan["plan_id"] == _digest(plan)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("report publisher ran a command"))
    path = producer.publish_research(run, "submit", plan)
    selected = projects.select_run(str(root), run.name)
    assert selected["binding"]["submit_file"] == str(path)
    loaded = projects.read_bound_json(selected["binding"], str(path), key="submit")
    assert loaded == plan and loaded["plan_id"] == _digest(loaded)
    hub = ResearchHub({})
    manual_plan = {"source": "previous explicitly prepared plan"}
    hub.plan = manual_plan
    try:
        result = hub._read({"view": "submit", "settings": {"submit_file": str(path)},
                            "snap": {}, "binding": selected["binding"]})
        assert result["display_only"] is True and result["plan"] == plan
        assert hub.plan is manual_plan
    finally:
        hub.close()


def test_changed_preflight_is_refused_before_existing_report_or_inventory_changes(project):
    root, producer, run = project
    plan = prepare("jobs/run.sbatch", workdir=str(root))
    path = producer.publish_research(run, "submit", plan)
    before = {file: file.read_bytes() for file in (path, run / "run.json")}
    changed = copy.deepcopy(plan)
    changed["argv"].append("--mem=99G")
    with pytest.raises(ValueError, match="plan_id"):
        producer.publish_research(run, "submit", changed)
    assert {file: file.read_bytes() for file in before} == before


@pytest.mark.parametrize("view, document", [
    ("../forecast", {"version": 1, "kind": "raven.planning"}),
    ("arrays", {"version": 1, "kind": "raven.planning"}),
    ("predict", {"version": True, "kind": "raven.planning"}),
    ("predict", {"version": 1, "kind": "raven.workflow", "nodes": []}),
    ("forecast", {"version": 1, "kind": "raven.planning", "job_id": "12345"}),
    ("forecast", {"version": 1, "kind": "raven.planning", "now": float("inf")}),
    ("forecast", {"version": 1, "kind": "raven.planning", "description": "x" * (1 << 20)}),
    ("submit", {"schema": "raven.submission-plan/v1", "valid": True}),
    ("predict", []),
])
def test_invalid_reports_leave_existing_evidence_untouched(project, view, document):
    _, producer, run = project
    path = producer.publish_research(run, "forecast", bundle())
    before = {file: file.read_bytes() for file in (path, run / "run.json")}
    with pytest.raises((ValueError, TypeError)):
        producer.publish_research(run, view, document)
    assert {file: file.read_bytes() for file in before} == before
    assert set(file.name for file in (run / "reports").iterdir()) == {"forecast.json"}


@pytest.mark.parametrize("kind", ["file-symlink", "directory-symlink", "fifo", "directory"])
def test_report_publication_refuses_special_files_and_symlink_directories(project, tmp_path, kind):
    _, producer, run = project
    path = run / "reports/forecast.json"
    if kind == "file-symlink":
        external = tmp_path / "external.json"
        external.write_text("retained external evidence")
        path.symlink_to(external)
    elif kind == "directory-symlink":
        (run / "reports").rmdir()
        (run / "reports").symlink_to(tmp_path, target_is_directory=True)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    before = (run / "run.json").read_bytes()
    with pytest.raises((ValueError, OSError)):
        producer.publish_research(run, "forecast", bundle())
    assert (run / "run.json").read_bytes() == before
    if kind == "file-symlink":
        assert external.read_text() == "retained external evidence"


def test_cli_publication_runs_without_site_packages_or_raven_import(project):
    root, _, run = project
    source = root / "observed-source.json"
    source.write_text(json.dumps(bundle()))
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    completed = subprocess.run([sys.executable, "-S", "reporting.py", "report", "forecast", str(source),
                                "--run", str(run)], cwd=root, env=env,
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "no scheduler action" in completed.stdout
    assert json.loads((run / "reports/forecast.json").read_text()) == bundle()
    assert projects.validate_inventory(json.loads((run / "run.json").read_text()))["job_id"] == "12345_7"


def test_old_minimum_v1_inventories_and_explicit_report_apis_remain_valid(tmp_path):
    run = tmp_path / "runs/old"
    run.mkdir(parents=True)
    old = {"schema": "raven.run/v1", "run_id": "old", "experiment_id": "old-work", "attempt": 1,
           "state": "RUNNING", "paths": {"metrics": "metrics.jsonl"}}
    (run / "run.json").write_text(json.dumps(old))
    selected = projects.select_run(str(tmp_path), "old")
    assert selected["binding"]["planning_files"] == {}
    assert selected["binding"]["planning_file"] == selected["binding"]["submit_file"] == ""
    assert selected["binding"]["job_id"] is None


def test_run_path_schema_vocabulary_matches_native_inventory_reader():
    schema = json.loads((ROOT / "docs/schemas/run.v1.schema.json").read_text())
    assert set(schema["properties"]["paths"]["properties"]) == projects._PATHS
