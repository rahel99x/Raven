"""The copyable project producer works with Raven's real bounded readers."""
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from raven import cli
from raven.artifacts import load_contract, validate_contract
from raven.config import Config
from raven.metrics import MetricReader
from raven.planning_io import load_json
from raven.predict import predict
from raven.scaling import analyze as analyze_scaling
from raven.workflow import analyze as analyze_workflow


TEMPLATE = Path(__file__).resolve().parents[1] / "examples" / "project-template"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "A portable research project"
    shutil.copytree(TEMPLATE, root)
    spec = importlib.util.spec_from_file_location("project_reporting_example", root / "reporting.py")
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    return root, producer


def new_run(project, ident, **kwargs):
    root, producer = project
    source = root / "observed.py"
    if not source.exists():
        source.write_text("# Immutable scientific source for reader integration fixtures.\n")
    return producer.begin_run(root, ident, name="portable-workload", script="observed.py", **kwargs)


def allocation():
    return {"partition": "main", "cpus": 4, "nodes": 1, "gpus": 0,
            "mem_bytes": 4 * 1024**3, "time_seconds": 600}


def load_experiment(project, monkeypatch):
    root, producer = project
    monkeypatch.setitem(sys.modules, "reporting", producer)
    spec = importlib.util.spec_from_file_location("project_experiment_example", root / "experiment.py")
    experiment = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(experiment)
    return experiment


def test_copied_project_produces_real_reports_without_raven_dependency(project, monkeypatch, capsys):
    root, _ = project
    env = {key: value for key, value in os.environ.items() if not key.startswith(("SLURM_", "RAVEN_"))}
    env.pop("PYTHONPATH", None)
    completed = subprocess.run([sys.executable, "-S", str(root / "experiment.py"), "--run-id", "observed",
                                "--steps", "3", "--terms-per-step", "32"],
                               cwd=root, env=env, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "COMPLETED" in completed.stdout
    run = root / "runs" / "observed"
    summary = load_json(run / "summary.json")
    assert summary["state"] == "COMPLETED"
    assert 0 < summary["runtime_seconds"] < 10
    assert summary["cpu_seconds"] >= 0
    assert summary["results"]["terms"] == 96
    assert all(summary.get(field) is None for field in ("cpus", "nodes", "gpus", "partition", "memory_bytes"))
    metrics = MetricReader().read(run / "metrics.jsonl")
    assert metrics["status"] == "ok" and metrics["records"] == 3
    assert metrics["latest"]["absolute_error"] == summary["results"]["absolute_error"]
    assert metrics["progress"]["completed"] == metrics["progress"]["total"] == 96
    assert "Starting 96 terms" in (run / "logs" / "stdout.log").read_text()
    assert (run / "logs" / "stderr.log").read_text() == ""
    contract = load_contract(root / ".raven" / "contracts" / "outputs.v1.json")
    checked = validate_contract(contract, run)
    assert checked["valid"], checked
    exported = subprocess.run([sys.executable, "-S", str(root / "reporting.py"), "export", "runs/observed",
                               "--reference", "runs/observed"], cwd=root, env=env,
                              capture_output=True, text=True, timeout=10)
    assert exported.returncode == 0, exported.stdout + exported.stderr
    assert load_json(root / "reports" / "planning.json")["history"][0]["runtime_seconds"] == summary["runtime_seconds"]
    monkeypatch.setattr(cli, "build", lambda *a, **k: pytest.fail("artifact read constructed scheduler"))
    monkeypatch.setattr(cli, "make_backend", lambda *a, **k: pytest.fail("artifact read constructed scheduler"))
    config = root / "test-config.json"
    config.write_text("{}")
    assert cli.main(["--config", str(config), "--no-state", "--no-plugins", "run", "validate",
                     str(root / ".raven" / "contracts" / "outputs.v1.json"), str(run)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"]


def test_template_config_attaches_explicit_run_and_native_readers_without_extra_queries(project, monkeypatch):
    root, _ = project
    monkeypatch.chdir(root)
    completed = subprocess.run([sys.executable, "-S", "experiment.py", "--run-id", "attached",
                                "--steps", "2", "--terms-per-step", "16"],
                               cwd=root, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    config = root / ".raven" / "config.json"
    session = cli.build(cli.parse(["--fake", "--no-state", "--no-plugins", "--config", str(config),
                                   "--workdir", str(root / "runs" / "attached"),
                                   "--tab", "research", "--research-view", "experiment"]), Config.load(str(config)))
    monkeypatch.setattr(session.backend, "call", lambda *a, **k: pytest.fail("attached file reader queried scheduler"))
    try:
        app, hub = session.app, session.app.research
        result = hub.request(hub.context(session.store.snapshot(), app), wait=True, force=True)
        assert result["records"] == 2 and result["progress"]["completed"] == 32
        assert Path(result["path"]) == root / "runs" / "attached" / "metrics.jsonl"
        app.research_view = "artifacts"
        artifacts = hub.request(hub.context(session.store.snapshot(), app), wait=True, force=True)
        assert artifacts["valid"], artifacts
    finally:
        session.close()


@pytest.mark.parametrize("env, expected", [
    ({}, {}),
    ({"SLURM_CPUS_PER_TASK": "32", "SLURM_NTASKS": "8", "CUDA_VISIBLE_DEVICES": "0,1"}, {}),
    ({"SLURM_JOB_CPUS_PER_NODE": "32(x2),16", "SLURM_JOB_NUM_NODES": "3",
      "SLURM_MEM_PER_NODE": "256", "SLURM_GPUS": "0"},
     {"cpus": 80, "nodes": 3, "mem_bytes": 3 * 256 * 1024**2, "gpus": 0}),
    ({"SLURM_JOB_CPUS_PER_NODE": "4(x2)", "SLURM_JOB_NUM_NODES": "2", "SLURM_MEM_PER_CPU": "128"},
     {"cpus": 8, "nodes": 2, "mem_bytes": 8 * 128 * 1024**2}),
    ({"SLURM_JOB_CPUS_PER_NODE": "4(x2)", "SLURM_JOB_NUM_NODES": "3", "SLURM_MEM_PER_CPU": "128"},
     {"nodes": 3}),
    ({"SLURM_JOB_CPUS_PER_NODE": "4", "SLURM_MEM_PER_NODE": "128", "SLURM_MEM_PER_CPU": "64"},
     {"cpus": 4, "nodes": 1}),
    ({"SLURM_JOB_NUM_NODES": "2", "SLURM_GPUS": "gpu:a100:2"}, {"nodes": 2}),
    ({"SLURM_JOB_CPUS_PER_NODE": "4(x2", "SLURM_MEM_PER_CPU": "128"}, {}),
])
def test_slurm_resource_mapping_uses_total_allocations_and_explicit_memory_scope(project, monkeypatch, env, expected):
    experiment = load_experiment(project, monkeypatch)
    assert experiment._slurm_resources(env) == expected


def test_array_job_identity_is_distinct_from_parent_and_local_run(project, monkeypatch):
    experiment = load_experiment(project, monkeypatch)
    assert experiment._job_id({"SLURM_JOB_ID": "12345", "SLURM_ARRAY_JOB_ID": "12345",
                               "SLURM_ARRAY_TASK_ID": "7"}) == "12345_7"
    assert experiment._job_id({"SLURM_JOB_ID": "12345"}) == "12345"
    assert experiment._job_id({}) is None


def test_metric_stream_remains_native_incremental_and_progress_is_derived(project):
    _, producer = project
    run = new_run(project, "incremental")
    reader = MetricReader()
    for step in range(1, 4):
        producer.write_metric(run, {"loss": -1 / step, "throughput_items_per_s": step * 2},
                              step=step, phase="train", t=1700000000 + step,
                              progress={"completed": step, "total": 4, "unit": "steps"})
        snapshot = reader.read(run / "metrics.jsonl")
        assert snapshot["status"] == "ok" and snapshot["records"] == step
    assert snapshot["latest"]["loss"] == pytest.approx(-1 / 3)
    assert snapshot["progress"]["fraction"] == .75
    assert snapshot["progress"]["eta_seconds"] == 1
    assert reader.read(run / "metrics.jsonl")["records"] == 3
    producer.write_metric(run, {}, phase="evaluate", t=1700000004)
    assert reader.read(run / "metrics.jsonl")["progress"] == {}


@pytest.mark.parametrize("kwargs", [
    {"metrics": {"loss": math.nan}}, {"metrics": {"loss": math.inf}},
    {"metrics": {"loss": True}}, {"metrics": {"loss\n": 1}},
    {"metrics": {"loss": 1}, "step": True},
    {"metrics": {"loss": 1}, "step": 2**63},
    {"metrics": {"loss": 1}, "progress": {"completed": 2, "total": 1}},
    {"metrics": {"loss": 1}, "progress": {"completed": 0, "total": 0}},
    {"metrics": {"loss": 1}, "progress": {"fraction": .5}},
    {"metrics": {"loss": 1}, "phase": "\x1b[31munsafe"},
])
def test_invalid_metric_never_damages_existing_stream(project, kwargs):
    _, producer = project
    run = new_run(project, "invalid-metric")
    producer.write_metric(run, {"loss": 1})
    before = (run / "metrics.jsonl").read_bytes()
    with pytest.raises((ValueError, OSError)):
        producer.write_metric(run, **kwargs)
    assert (run / "metrics.jsonl").read_bytes() == before
    assert MetricReader().read(run / "metrics.jsonl")["records"] == 1


def test_unknown_local_resources_are_not_invented_and_attempts_are_immutable(project):
    root, producer = project
    run = new_run(project, "local-attempt")
    summary = producer.finish_run(run, state="COMPLETED", runtime_seconds=.02,
                                  cpu_seconds=.01, results={"answer": 42})
    assert all(summary.get(field) is None for field in ("partition", "cpus", "nodes", "gpus", "mem_bytes"))
    assert summary.get("memory_bytes") is None
    before = (run / "summary.json").read_bytes()
    with pytest.raises((ValueError, OSError)):
        new_run(project, "local-attempt")
    with pytest.raises((ValueError, OSError)):
        producer.finish_run(run, state="FAILED", runtime_seconds=.03)
    assert (run / "summary.json").read_bytes() == before
    bundle = producer.export_planning(root, ["runs/local-attempt"], reference_run="runs/local-attempt")
    prediction = predict(bundle["history"], bundle["query"])
    assert prediction["status"] == "insufficient"
    assert not any(field in bundle["query"] for field in ("cpus", "nodes", "gpus", "partition"))


def test_explicit_collector_preserves_failed_and_censored_evidence(project):
    root, producer = project
    selected = []
    for index in range(12):
        run = new_run(project, f"completed-{index}", resources=allocation(),
                      parameters={"dataset_sha256": "a" * 64, "iterations": 10})
        producer.finish_run(run, state="COMPLETED", runtime_seconds=10 + index / 10,
                            cpu_seconds=32 + index / 10, memory_bytes=1024,
                            memory_scope="max_task_rss")
        selected.append(str(run.relative_to(root)))
    for state in ("FAILED", "TIMEOUT", "OUT_OF_MEMORY"):
        run = new_run(project, state.lower(), resources=allocation(),
                      parameters={"dataset_sha256": "a" * 64, "iterations": 10})
        producer.finish_run(run, state=state, runtime_seconds=30,
                            memory_bytes=2048, memory_scope="max_task_rss")
        selected.append(str(run.relative_to(root)))
    unselected = new_run(project, "not-selected", resources=allocation())
    producer.finish_run(unselected, state="COMPLETED", runtime_seconds=.01)
    bundle = producer.export_planning(root, selected, reference_run=selected[0])
    assert len(bundle["history"]) == 15
    assert {row["state"] for row in bundle["history"]} == {"COMPLETED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY"}
    assert "not-selected" not in {row["id"] for row in bundle["history"]}
    outcome = predict(bundle["history"], bundle["query"])
    assert outcome["status"] == "partial"
    assert {item["state"] for item in outcome["censored"]} == {"TIMEOUT", "OUT_OF_MEMORY"}
    assert outcome["metrics"]["runtime_seconds"]["lower"] is None
    assert outcome["metrics"]["runtime_seconds"]["upper"] is None


def test_changed_scientific_source_is_not_pooled_in_prediction(project):
    root, producer = project
    selected = []
    for index in range(10):
        run = new_run(project, f"old-source-{index}", resources=allocation(), parameters={"data": "v1"})
        producer.finish_run(run, state="COMPLETED", runtime_seconds=12)
        selected.append(str(run.relative_to(root)))
    (root / "observed.py").write_text("# Different scientific code.\n")
    run = new_run(project, "changed-source", resources=allocation(), parameters={"data": "v1"})
    producer.finish_run(run, state="COMPLETED", runtime_seconds=1)
    selected.append(str(run.relative_to(root)))
    bundle = producer.export_planning(root, selected, reference_run=selected[-1])
    outcome = predict(bundle["history"], bundle["query"])
    assert outcome["status"] == "insufficient"
    assert outcome["metrics"]["runtime_seconds"]["samples"] == 1
    assert len({row["script_sha256"] for row in bundle["history"]}) == 2


def test_observed_memory_scope_is_preserved_in_reference_query(project):
    root, producer = project
    run = new_run(project, "task-memory", resources=allocation())
    producer.finish_run(run, state="COMPLETED", runtime_seconds=1,
                        memory_bytes=4096, memory_scope="max_task_rss")
    bundle = producer.export_planning(root, ["runs/task-memory"], reference_run="runs/task-memory")
    assert bundle["query"]["memory_scope"] == "max_task_rss"
    outcome = predict(bundle["history"], bundle["query"])
    assert outcome["metrics"]["memory_bytes"]["scope"] == "max_task_rss"
    assert bundle["history"][0]["mem_bytes"] == 4 * 1024**3
    assert bundle["history"][0]["memory_bytes"] == 4096


@pytest.mark.parametrize("state", ["UNKNOWN", "INTERRUPTED"])
def test_unverified_terminal_cause_remains_explicit_and_never_becomes_completed(project, state):
    root, producer = project
    run = new_run(project, state.lower(), resources=allocation())
    producer.finish_run(run, state=state, runtime_seconds=.02,
                        metadata={"termination_evidence": "application interruption observed; scheduler cause unknown"})
    bundle = producer.export_planning(root, [str(run.relative_to(root))], reference_run=str(run.relative_to(root)))
    assert bundle["history"][0]["state"] == state
    outcome = predict(bundle["history"], bundle["query"])
    assert outcome["metrics"]["runtime_seconds"]["samples"] == 0
    assert outcome["metrics"]["runtime_seconds"]["estimate"] is None


def test_failed_finalization_keeps_run_unfinished_until_truthful_summary_is_supplied(project):
    _, producer = project
    run = new_run(project, "exit-contradiction")
    with pytest.raises(ValueError):
        producer.finish_run(run, state="COMPLETED", runtime_seconds=1, exit_code=1)
    assert not (run / "summary.json").exists()
    assert load_json(run / "run.json")["state"] == "RUNNING"
    producer.finish_run(run, state="FAILED", runtime_seconds=1, exit_code=1)
    assert load_json(run / "summary.json")["state"] == "FAILED"


def test_collector_failure_preserves_prior_report_and_refuses_duplicate_ids(project):
    root, producer = project
    run = new_run(project, "one", resources=allocation())
    producer.finish_run(run, state="COMPLETED", runtime_seconds=.01)
    producer.export_planning(root, ["runs/one"], reference_run="runs/one")
    report = root / "reports" / "planning.json"
    before = report.read_bytes()
    for selected, reference in [(["runs/one", "runs/one"], None),
                                (["runs/one", "runs/missing"], None),
                                (["runs/one"], "runs/not-selected")]:
        with pytest.raises((ValueError, OSError)):
            producer.export_planning(root, selected, reference_run=reference)
        assert report.read_bytes() == before


def test_collector_limit_stops_before_extra_attempt_and_preserves_previous_report(project, monkeypatch):
    root, producer = project
    paths = []
    for ident in ("first", "second", "beyond-limit"):
        run = new_run(project, ident)
        producer.finish_run(run, state="COMPLETED", runtime_seconds=.01)
        paths.append(str(run.relative_to(root)))
    producer.export_planning(root, paths[:2])
    report = root / "reports" / "planning.json"
    before = report.read_bytes()
    monkeypatch.setattr(producer, "MAX_RUNS", 2)
    original = producer._read_json
    def bounded_read(path):
        assert "beyond-limit" not in str(path), "collector read a run beyond its input limit"
        return original(path)
    monkeypatch.setattr(producer, "_read_json", bounded_read)
    with pytest.raises(ValueError):
        producer.export_planning(root, iter(paths))
    assert report.read_bytes() == before


def test_collector_byte_budget_does_not_silently_drop_large_selected_evidence(project, monkeypatch):
    root, producer = project
    first = new_run(project, "small")
    producer.finish_run(first, state="COMPLETED", runtime_seconds=.01)
    producer.export_planning(root, ["runs/small"])
    report = root / "reports" / "planning.json"
    before = report.read_bytes()
    large = new_run(project, "large")
    producer.finish_run(large, state="FAILED", runtime_seconds=.01, metadata={"notes": "a" * 4096})
    monkeypatch.setattr(producer, "MAX_AGGREGATE", 2048)
    with pytest.raises(ValueError):
        producer.export_planning(root, ["runs/small", "runs/large"])
    assert report.read_bytes() == before


@pytest.mark.parametrize("mutation", [
    "missing_name", "unsafe_id", "empty_name", "nonnumeric_runtime", "negative_runtime",
    "boolean_cpus", "unscoped_memory", "unknown_top_level", "completed_nonzero_exit",
])
def test_collector_rejects_invalid_standard_summary_before_updating_prior_report(project, mutation):
    root, producer = project
    run = new_run(project, "schema-guard", resources=allocation())
    producer.finish_run(run, state="COMPLETED", runtime_seconds=.02)
    producer.export_planning(root, ["runs/schema-guard"])
    report = root / "reports" / "planning.json"
    before = report.read_bytes()
    summary_path = run / "summary.json"
    row = json.loads(summary_path.read_text())
    if mutation == "missing_name":
        del row["name"]
    elif mutation == "unsafe_id":
        row["id"] = "../outside"
    elif mutation == "empty_name":
        row["name"] = ""
    elif mutation == "nonnumeric_runtime":
        row["runtime_seconds"] = "fast"
    elif mutation == "negative_runtime":
        row["runtime_seconds"] = -1
    elif mutation == "boolean_cpus":
        row["cpus"] = True
    elif mutation == "unscoped_memory":
        row["memory_bytes"] = 1024
    elif mutation == "unknown_top_level":
        row["sbatch_command"] = "execute me"
    elif mutation == "completed_nonzero_exit":
        row["exit_code"] = 1
    summary_path.write_text(json.dumps(row, allow_nan=False))
    with pytest.raises((ValueError, OSError)):
        producer.export_planning(root, ["runs/schema-guard"])
    assert report.read_bytes() == before


@pytest.mark.parametrize("kwargs", [
    {"resources": {"cpus": 1_000_000_001}},
    {"resources": {"nodes": 1_000_000_001}},
    {"resources": {"gpus": 1_000_000_001}},
    {"attempt": 2_147_483_648},
    {"parameters": {"k" * 65: 1}},
    {"parameters": {"a": [0] * 64, "b": [0] * 64}},
    {"parameters": {"a": {"b": {"c": {"d": {"e": 1}}}}}},
])
def test_reporter_refuses_nonportable_identity_before_creating_attempt(project, kwargs):
    root, _ = project
    with pytest.raises((ValueError, OSError)):
        new_run(project, "bad-identity", **kwargs)
    assert not (root / "runs" / "bad-identity").exists()


def test_reporter_refuses_oversized_run_id_before_creating_attempt(project):
    root, _ = project
    with pytest.raises((ValueError, OSError)):
        new_run(project, "r" * 129)
    assert not (root / "runs" / ("r" * 129)).exists()


@pytest.mark.parametrize("bad_path", ["../outside", "/tmp/outside-project", "runs/../outside"])
def test_explicit_collector_refuses_project_escape(project, bad_path):
    root, producer = project
    with pytest.raises((ValueError, OSError)):
        producer.export_planning(root, [bad_path])
    assert not (root / "reports" / "planning.json").exists()


def test_symlinked_run_and_metric_sources_never_read_or_written(project, tmp_path):
    root, producer = project
    run = new_run(project, "original")
    producer.finish_run(run, state="COMPLETED", runtime_seconds=.01)
    (root / "runs" / "alias").symlink_to(run, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        producer.export_planning(root, ["runs/alias"])
    live = new_run(project, "live")
    (live / "metrics.jsonl").unlink()
    secret = tmp_path / "outside.jsonl"
    secret.write_text("private original\n")
    (live / "metrics.jsonl").symlink_to(secret)
    with pytest.raises((ValueError, OSError)):
        producer.write_metric(live, {"loss": 1})
    assert secret.read_text() == "private original\n"


def test_nonregular_summary_is_rejected_without_blocking_or_replacing_prior_report(project):
    root, producer = project
    run = new_run(project, "fifo-guard")
    producer.finish_run(run, state="FAILED", runtime_seconds=.01)
    producer.export_planning(root, ["runs/fifo-guard"])
    report = root / "reports" / "planning.json"
    before = report.read_bytes()
    (run / "summary.json").unlink()
    os.mkfifo(run / "summary.json")
    with pytest.raises((ValueError, OSError)):
        producer.export_planning(root, ["runs/fifo-guard"])
    assert report.read_bytes() == before


def test_dangling_report_link_and_symlinked_project_parent_are_refused(project, tmp_path):
    root, producer = project
    run = new_run(project, "report-link")
    producer.finish_run(run, state="COMPLETED", runtime_seconds=.01)
    report = root / "reports" / "planning.json"
    report.symlink_to(tmp_path / "must-stay-absent.json")
    with pytest.raises((ValueError, OSError)):
        producer.export_planning(root, ["runs/report-link"])
    assert report.is_symlink()
    assert not (tmp_path / "must-stay-absent.json").exists()
    alias = tmp_path / "project-alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        producer.export_planning(alias, ["runs/report-link"])


def test_scaling_metadata_is_explicit_and_failures_suppress_relative_claims(project):
    root, producer = project
    selected = []
    for workers in (1, 2):
        for repeat in (1, 2, 3):
            run = new_run(project, f"scale-{workers}-{repeat}", parameters={"problem": "fixed"},
                          job_id=f"local-{workers}-{repeat}")
            producer.finish_run(run, state="COMPLETED", runtime_seconds=2 / workers,
                                scaling={"workers": workers, "problem_size": 100, "repeat": repeat})
            selected.append(str(run.relative_to(root)))
    bundle = producer.export_planning(root, selected)
    output = analyze_scaling(bundle["scaling"], baseline=1)
    assert output["status"] == "ok"
    assert output["points"][1]["speedup"] == 2
    assert all(point["core_hours"] is None and point["gpu_hours"] is None for point in output["points"])
    run = new_run(project, "scale-timeout", parameters={"problem": "fixed"}, job_id="local-failed")
    producer.finish_run(run, state="TIMEOUT", runtime_seconds=3,
                        scaling={"workers": 2, "problem_size": 100, "repeat": 4})
    selected.append(str(run.relative_to(root)))
    updated = producer.export_planning(root, selected)
    output = analyze_scaling(updated["scaling"], baseline=1)
    assert output["points"][1]["speedup"] is None
    assert output["excluded_count"] == 1


def test_template_workflow_unknown_times_remain_unknown(project):
    root, _ = project
    recipe = load_json(root / ".raven" / "definitions" / "workflow.json")
    output = analyze_workflow(recipe)
    assert output["status"] == "incomplete"
    assert len(output["order"]) >= 1
    assert any(node["duration"]["estimate"] is None for node in output["nodes"])
    assert output["makespan"]["estimate"] is None


def test_producer_bundle_is_readable_offline_without_scheduler(project, monkeypatch, capsys):
    root, producer = project
    run = new_run(project, "offline", resources=allocation())
    producer.finish_run(run, state="COMPLETED", runtime_seconds=.01)
    producer.export_planning(root, ["runs/offline"], reference_run="runs/offline")
    monkeypatch.setattr(cli, "build", lambda *a, **k: pytest.fail("offline read constructed scheduler"))
    monkeypatch.setattr(cli, "make_backend", lambda *a, **k: pytest.fail("offline read constructed scheduler"))
    config = root / "private-test-config.toml"
    config.write_text("")
    assert cli.main(["--config", str(config), "--no-state", "--no-plugins", "run", "predict",
                     "--file", str(root / "reports" / "planning.json")]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "insufficient"
    assert payload["metrics"]["runtime_seconds"]["samples"] == 1
