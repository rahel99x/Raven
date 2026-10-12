"""Scientific comparability and exact, bounded scaling review plans."""
from __future__ import annotations

import copy
import json
import os
import random
import shlex
import subprocess
import sys

import pytest

from raven import scaling


HASH = "a" * 64


def measured(workers, runtime, repeat=1, **changes):
    record = {"workers": workers, "runtime_seconds": runtime, "state": "COMPLETED", "repeat": repeat,
              "parameters": {"solver": "cg"}, "script_sha256": HASH, "problem_size": 1000,
              "cpus": workers * 2, "gpus": 0}
    record.update(changes)
    return record


def point(result, workers):
    return next(item for item in result["points"] if item["workers"] == workers)


def test_strong_scaling_known_speedup_efficiency_and_measured_cost():
    records = [measured(workers, runtime, repeat) for workers, runtime in [(2, 100), (4, 50), (8, 40)] for repeat in range(1, 4)]
    result = scaling.analyze(records, baseline=2)
    assert result["status"] == "ok"
    assert point(result, 4)["speedup"] == 2
    assert point(result, 4)["efficiency"] == 1
    assert point(result, 8)["speedup"] == 2.5
    assert point(result, 8)["efficiency"] == .625
    assert point(result, 4)["core_hours"]["median"] == pytest.approx(8 * 50 / 3600)
    assert point(result, 4)["gpu_hours"] == {"median": 0, "total": 0, "samples": 3}
    assert json.loads(json.dumps(result, allow_nan=False)) == result


def test_weak_scaling_fixed_proportion_efficiency_only():
    records = [measured(workers, runtime, repeat, problem_size=workers * 100) for workers, runtime in [(2, 100), (4, 110)] for repeat in range(1, 4)]
    result = scaling.analyze(records, mode="weak", baseline=2)
    assert result["status"] == "ok"
    assert point(result, 4)["efficiency"] == pytest.approx(100 / 110)
    assert point(result, 4)["speedup"] is None


def test_reproducible_empirical_interval_and_median():
    result = scaling.analyze([measured(2, t, i) for i, t in enumerate([100, 10, 40, 60, 20], 1)], coverage=.8)
    runtime = result["points"][0]["runtime"]
    assert runtime["median"] == 40
    assert runtime["lower"] == pytest.approx(14)
    assert runtime["upper"] == pytest.approx(84)
    assert runtime["interval_method"] == "empirical_runtime_quantiles"
    assert result == scaling.analyze([measured(2, t, i) for i, t in enumerate([100, 10, 40, 60, 20], 1)], coverage=.8)
    assert any("not confidence" in item for item in result["evidence"])


@pytest.mark.parametrize("count", [1, 2])
def test_too_few_repeats_do_not_invent_interval(count):
    result = scaling.analyze([measured(2, 100 + i, i + 1) for i in range(count)])
    assert result["status"] == "partial"
    assert result["points"][0]["runtime"]["lower"] is None
    assert result["points"][0]["runtime"]["upper"] is None


@pytest.mark.parametrize("change", [{"problem_size": 2000}, {"parameters": {"solver": "other"}}, {"script_sha256": "b" * 64}, {"fingerprint": "different"}])
def test_incompatible_experiments_suppress_claims(change):
    result = scaling.analyze([measured(1, 100), measured(2, 50, **change)])
    assert result["status"] == "insufficient"
    assert all(item["speedup"] is None and item["efficiency"] is None for item in result["points"])


def test_weak_scaling_nonproportional_size_is_incompatible():
    result = scaling.analyze([measured(1, 100), measured(2, 100, problem_size=1500)], mode="weak")
    assert result["status"] == "insufficient"
    assert point(result, 2)["efficiency"] is None


@pytest.mark.parametrize("state", ["FAILED", "TIMEOUT", "RUNNING", "CANCELLED", "COMPLETING", None, True])
def test_failed_and_censored_groups_are_not_relative_claims(state):
    result = scaling.analyze([measured(1, 100), measured(2, 50), measured(2, 80, repeat=2, state=state)])
    assert result["status"] == "partial"
    assert point(result, 2)["speedup"] is None
    assert result["excluded_count"] == 1


def test_failed_baseline_suppresses_every_relative_claim():
    result = scaling.analyze([measured(1, 100), measured(1, 120, 2, state="FAILED"), measured(2, 50)])
    assert result["status"] == "insufficient"
    assert all(item["speedup"] is None for item in result["points"])


def test_missing_baseline_does_not_assume_scaling():
    result = scaling.analyze([measured(4, 100)], baseline=2)
    assert result["status"] == "insufficient"
    assert result["points"][0]["efficiency"] is None


@pytest.mark.parametrize("field,value", [
    ("workers", True), ("workers", 0), ("workers", 1.5), ("runtime_seconds", 0), ("runtime_seconds", -1),
    ("runtime_seconds", True), ("runtime_seconds", float("nan")), ("runtime_seconds", float("inf")),
    ("runtime_seconds", 1e300), ("problem_size", 0), ("problem_size", "1000"), ("parameters", None),
    ("script_sha256", "guess"), ("repeat", 0), ("repeat", True), ("cpus", -1), ("cpus", 0),
    ("gpus", -1), ("job_id", "\x1b[2J"), ("work_units", float("inf")), ("parameters", {"api_token": "oops"}),
])
def test_invalid_records_excluded_without_false_values(field, value):
    record = measured(1, 100)
    record[field] = value
    result = scaling.analyze([record])
    assert result["status"] == "insufficient"
    assert result["points"] == []
    assert result["excluded_count"] == 1
    json.dumps(result, allow_nan=False)


def test_missing_run_identity_prevents_pseudoreplication():
    record = measured(1, 100)
    del record["repeat"]
    assert scaling.analyze([record])["points"] == []


def test_duplicate_job_id_and_repeat_are_not_new_samples():
    records = [measured(1, 100, job_id="700"), measured(1, 100, repeat=2, job_id="700"), measured(2, 50), measured(2, 50)]
    result = scaling.analyze(records)
    assert result["excluded_count"] == 2
    assert point(result, 1)["samples"] == 1
    assert point(result, 2)["samples"] == 1


def test_conflicting_duplicate_identity_suppresses_claims():
    result = scaling.analyze([measured(1, 100, job_id="700"), measured(2, 50, repeat=2, job_id="700"), measured(2, 50, repeat=3)])
    assert result["status"] == "insufficient"
    assert all(item["speedup"] is None for item in result["points"])


def test_failed_smallest_group_does_not_silently_promote_baseline():
    result = scaling.analyze([measured(1, 100, state="FAILED"), measured(2, 50)])
    assert result["baseline"] == 1
    assert result["status"] == "insufficient"


def test_resource_hours_unknown_if_any_allocation_missing():
    result = scaling.analyze([measured(2, 100), measured(2, 90, repeat=2, cpus=None, gpus=None)])
    assert result["points"][0]["core_hours"] is None
    assert result["points"][0]["gpu_hours"] is None


def test_record_budget_and_exclusion_output_bounded():
    result = scaling.analyze([measured(1, 100)] * 1000, max_records=300)
    assert result["status"] == "insufficient"
    assert len(result["excluded"]) == 256
    assert result["excluded_count"] == 299
    assert result["points"][0]["speedup"] is None


@pytest.mark.parametrize("kwargs", [{"mode": "fast"}, {"mode": {}}, {"coverage": 0}, {"coverage": 1}, {"coverage": True}, {"coverage": float("nan")}, {"coverage": float("inf")}, {"baseline": True}, {"baseline": 10 ** 10000}, {"max_records": 0}, {"max_records": 10001}])
def test_bad_analysis_options_produce_actionable_error(kwargs):
    result = scaling.analyze([], **kwargs)
    assert result["status"] == "error"
    json.dumps(result, allow_nan=False)


def test_subnormal_runtime_cannot_overflow_speedup():
    result = scaling.analyze([measured(1, 1e12), measured(2, 5e-324)])
    assert len(result["points"]) == 1
    json.dumps(result, allow_nan=False)


def test_maximum_total_resources_are_finite():
    result = scaling.analyze([measured(1, 1e12, i, cpus=2147483647, gpus=2147483647) for i in range(1, 4)])
    json.dumps(result, allow_nan=False)
    assert result["status"] == "ok"


def test_deep_metadata_is_bounded():
    nested = {}
    for _ in range(100):
        nested = {"nested": nested}
    assert scaling.analyze([measured(1, 100, parameters=nested)])["points"] == []


@pytest.fixture(autouse=True)
def clear_sbatch_environment(monkeypatch):
    for name in os.environ:
        if name.startswith("SBATCH_"):
            monkeypatch.delenv(name)


@pytest.fixture
def recipe(tmp_path):
    script = tmp_path / "scaling's script.sh"
    script.write_text("#!/bin/bash\n#SBATCH --time=00:10:00\nsrun ./solver\n")
    return {"version": 1, "kind": "raven.scaling", "script": script.name, "workdir": str(tmp_path),
            "mode": "strong", "problem_size": 1000, "seed": 42, "repeats": 3,
            "parameters": {"batch_size": 32, "solver": "cg"},
            "configurations": [{"label": "small", "workers": 2, "cpus_per_task": 4, "nodes": 1, "gpus": 0},
                               {"label": "large", "workers": 4, "cpus_per_task": 4, "nodes": 2, "gpus": 2}]}


def test_plan_exact_repeated_unique_jobs_no_execution(recipe, monkeypatch):
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("planning executed a subprocess"))
    result = scaling.plan(recipe)
    assert result["valid"], result
    assert result["run_count"] == 6
    assert len({run["submission"]["resources"]["job_name"] for run in result["runs"]}) == 6
    assert len({run["submission"]["resources"]["output"] for run in result["runs"]}) == 6
    assert all("%j" in run["submission"]["resources"]["output"] for run in result["runs"])
    first = result["runs"][0]["submission"]
    assert first["resources"]["ntasks"] == "2"
    assert first["resources"]["cpus_per_task"] == "4"
    assert "RAVEN_PARAM_BATCH_SIZE=32" in first["resources"]["export"]
    assert "RAVEN_SCALING_WORKERS=2" in first["resources"]["export"]
    assert [run["seed"] for run in result["runs"]] == [42, 43, 44, 42, 43, 44]
    assert shlex.split(first["command"]) == ["sbatch", *first["argv"]]
    assert "array" not in first["resources"]
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert scaling.plan(recipe)["experiment_id"] == result["experiment_id"]


def test_script_controls_are_parameters_not_shell_arguments(recipe):
    recipe["parameters"] = {"danger": "$(touch NEVER); 'quoted'"}
    result = scaling.plan(recipe)
    assert result["valid"]
    plan = result["runs"][0]["submission"]
    assert "RAVEN_PARAM_DANGER=$(touch NEVER); 'quoted'" in plan["resources"]["export"]
    assert shlex.split(plan["command"]) == ["sbatch", *plan["argv"]]
    assert not (os.path.join(recipe["workdir"], "NEVER") and os.path.exists(os.path.join(recipe["workdir"], "NEVER")))


@pytest.mark.parametrize("change", [{"repeats": 0}, {"repeats": True}, {"repeats": 21}, {"max_runs": 129}, {"max_runs": 1}, {"seed": -1}, {"seed": True}, {"seed": 2 ** 31}, {"baseline": 99}, {"mode": "magic"}, {"version": True}, {"version": 1.0}, {"version": 2}, {"script": "x\n.sh"}, {"workdir": "x\x00"}, {"name": "../escape"}, {"extra": "unsupported"}, {"problem_size": None}])
def test_invalid_recipe_is_reviewable_error(recipe, change):
    recipe.update(change)
    result = scaling.plan(recipe)
    assert result["status"] == "error"
    assert not result["valid"]
    assert result["runs"] == []
    assert result["issues"]


@pytest.mark.parametrize("change", [{"label": "../escape"}, {"label": "comma,label"}, {"label": "small"}, {"workers": 2}, {"workers": False}, {"workers": 0}, {"cpus_per_task": -1}, {"nodes": 999}, {"gpus": -1}, {"parameters": {"x": [1, 2]}}, {"parameters": {"repeat": 2}}, {"parameters": {"password": "oops"}}, {"parameters": {"a": "comma,value"}}, {"parameters": {"a": "\n"}}, {"parameters": {"X": 1, "x": 2}}, {"problem_size": 2000}, {"unsupported": 1}])
def test_invalid_configuration_is_bounded_error(recipe, change):
    recipe["configurations"][1].update(change)
    assert not scaling.plan(recipe)["valid"]


def test_weak_recipe_requires_explicit_proportional_sizes(recipe):
    recipe["mode"] = "weak"
    for config in recipe["configurations"]:
        config["problem_size"] = config["workers"] * 100
    assert scaling.plan(recipe)["valid"]
    recipe["configurations"][1]["problem_size"] = 999
    assert not scaling.plan(recipe)["valid"]


@pytest.mark.parametrize("directive", ["--array=0-100", "--gres=gpu:1", "--ntasks-per-node=8", "--cpus-per-gpu=8", "--gpus-per-task=1", "--gpus=1"])
def test_ambiguous_script_resources_prevent_valid_controlled_plan(recipe, directive):
    path = os.path.join(recipe["workdir"], recipe["script"])
    with open(path, "w") as handle:
        handle.write(f"#!/bin/bash\n#SBATCH {directive}\nsrun ./solver\n")
    result = scaling.plan(recipe)
    assert not result["valid"]
    assert any(issue["code"] in {"uncontrolled_resources", "inherited_gpu_request"} for issue in result["issues"])


def test_missing_script_produces_per_run_preflight_errors(recipe):
    recipe["script"] = "missing.sh"
    result = scaling.plan(recipe)
    assert not result["valid"]
    assert result["run_count"] == 0
    assert any(issue["code"] == "script_unreadable" for issue in result["issues"])


def test_script_inspection_budget_stops_large_repeated_reads(recipe):
    path = os.path.join(recipe["workdir"], recipe["script"])
    with open(path, "w") as handle:
        handle.write("#!/bin/bash\n" + "#" * (5 << 20) + "\ntrue\n")
    result = scaling.plan(recipe)
    assert not result["valid"]
    assert result["runs"] == []
    assert result["issues"][0]["code"] == "script_inspection_budget"


def test_changed_script_during_preparation_stops_remaining_runs(recipe, monkeypatch):
    from raven import submission
    original = submission.prepare
    calls = []
    def mutation(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            with open(os.path.join(recipe["workdir"], recipe["script"]), "a") as handle:
                handle.write("\n# changed\n")
        return original(*args, **kwargs)
    monkeypatch.setattr(submission, "prepare", mutation)
    result = scaling.plan(recipe)
    assert not result["valid"]
    assert len(calls) == 3
    assert result["run_count"] == 1
    assert result["issues"][-1]["code"] == "script_changed"


def test_override_workdir_resolves_script_in_correct_directory(recipe, tmp_path):
    newdir = tmp_path / "different"
    newdir.mkdir()
    (newdir / recipe["script"]).write_text("#!/bin/bash\ntrue\n")
    result = scaling.plan(recipe, workdir=newdir)
    assert result["valid"]
    assert result["runs"][0]["submission"]["script"] == str(newdir / recipe["script"])


def test_sparse_failure_and_huge_numeric_payload_do_not_crash():
    result = scaling.analyze([measured(1, 1, runtime_seconds=10 ** 10000), None, [], measured(2, 2)])
    assert result["excluded_count"] == 3
    assert all(item["speedup"] is None for item in result["points"])


def test_analysis_does_not_mutate_records_and_plan_does_not_mutate_recipe(recipe):
    before = copy.deepcopy(recipe)
    scaling.plan(recipe)
    assert recipe == before
    records = [measured(1, 10)]
    original = copy.deepcopy(records)
    scaling.analyze(records)
    assert records == original


def test_randomized_known_scaling_matches_definitions_without_assumed_model():
    rng = random.Random(903)
    for _ in range(100):
        baseline = rng.randint(1, 32)
        workers = baseline * rng.randint(2, 16)
        first, second = rng.uniform(1, 10000), rng.uniform(1, 10000)
        records = [measured(w, t, i) for w, t in ((baseline, first), (workers, second)) for i in range(1, 4)]
        result = scaling.analyze(records, baseline=baseline)
        assert point(result, workers)["speedup"] == pytest.approx(first / second)
        assert point(result, workers)["efficiency"] == pytest.approx((first / second) / (workers / baseline))
        shuffled = records[:]
        rng.shuffle(shuffled)
        changed = scaling.analyze(shuffled, baseline=baseline)
        for output in (result, changed):
            for group in output["points"]:
                group.pop("run_ids")
        assert result == changed


def test_analysis_record_metadata_budget_prevents_large_repeat_payloads():
    records = [measured(1, 100, i, notes="a" * 4096) for i in range(1, 3001)]
    result = scaling.analyze(records)
    assert result["status"] == "insufficient"
    assert result["points"][0]["speedup"] is None
    assert any("byte budget" in item for item in result["limits"])


def test_loader_rejects_duplicate_keys_nonfinite_symlinks_and_fifo(tmp_path):
    path = tmp_path / "recipe.json"
    path.write_text('{"version":1,"version":2}')
    with pytest.raises(ValueError):
        scaling.load(path)
    path.write_text('{"version":NaN}')
    with pytest.raises(ValueError):
        scaling.load(path)
    path.write_text('{}')
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises((ValueError, OSError)):
        scaling.load(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises((ValueError, OSError)):
        scaling.load(fifo)


def test_loader_retains_valid_recipe(recipe, tmp_path):
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(recipe))
    assert scaling.load(path) == recipe


def test_shipped_recipe_prepares_real_controls_without_scheduler():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    result = scaling.plan(scaling.load(root / "examples/planning/scaling.json"), workdir=root)
    assert result["valid"], result
    assert result["run_count"] == 9
    assert all("RAVEN_PARAM_KERNEL_TERMS=8" in run["submission"]["resources"]["export"] for run in result["runs"])


def test_actual_local_scaling_measurements_and_explicit_collection(tmp_path):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    example = root / "examples/planning/scaling_workload.py"
    paths = []
    for workers in (1, 2):
        path = tmp_path / f"w{workers}.json"
        paths.append(path)
        result = subprocess.run([sys.executable, str(example), "local", "--workers", str(workers), "--problem-size", "100", "--output", str(path)],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        record = scaling.load(path)
        assert record["workers"] == workers
        assert record["runtime_seconds"] > 0
        assert record["measurement_context"] == "local"
        assert record["cpus"] is None and record["gpus"] is None
        assert path.stat().st_mode & 0o777 == 0o600
    combined = tmp_path / "measurements.json"
    collected = subprocess.run([sys.executable, str(example), "combine", *(str(path) for path in paths), "--output", str(combined)],
                               capture_output=True, text=True, timeout=10)
    assert collected.returncode == 0, collected.stderr
    result = scaling.analyze(scaling.load(combined)["records"])
    assert result["status"] == "partial"
    assert len(result["points"]) == 2
    assert all(item["speedup"] is not None for item in result["points"])


@pytest.mark.parametrize("arguments", [("--workers", "99"), ("--problem-size", "1000001"), ("--kernel-terms", "65"), ("--scale", "nan")])
def test_example_rejects_unbounded_work_before_writing(tmp_path, arguments):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "record.json"
    result = subprocess.run([sys.executable, str(root / "examples/planning/scaling_workload.py"), "local", *arguments, "--output", str(output)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert not output.exists()


def test_example_does_not_overwrite_record(tmp_path):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "record.json"
    output.write_text("immutable")
    result = subprocess.run([sys.executable, str(root / "examples/planning/scaling_workload.py"), "local", "--problem-size", "100", "--output", str(output)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert output.read_text() == "immutable"


def test_example_consumes_reviewed_controls_in_simulated_slurm_environment(tmp_path):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "record.json"
    environment = dict(os.environ)
    for name in list(environment):
        if name.startswith(("RAVEN_PARAM_", "RAVEN_SCALING_", "SLURM_")):
            environment.pop(name)
    environment.update({"RAVEN_SCALING_WORKERS": "1", "RAVEN_SCALING_REPEAT": "2", "RAVEN_SCALING_SEED": "9",
                        "RAVEN_SCALING_PROBLEM_SIZE": "100.0", "RAVEN_SCALING_EXPERIMENT_ID": "c" * 64,
                        "RAVEN_PARAM_KERNEL_TERMS": "2", "RAVEN_PARAM_SCALE": "0.005",
                        "SLURM_JOB_ID": "10001", "SLURM_JOB_NUM_NODES": "1", "SLURM_JOB_CPUS_PER_NODE": "1", "SLURM_GPUS_ON_NODE": "0"})
    completed = subprocess.run([sys.executable, str(root / "examples/planning/scaling_workload.py"), "run",
                                "--script", str(root / "examples/planning/scaling.sbatch"), "--output", str(output)],
                               env=environment, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    record = scaling.load(output)
    assert record["workers"] == 1 and record["problem_size"] == 100
    assert record["repeat"] == 2 and record["seed"] == 9
    assert record["parameters"] == {"kernel_terms": 2, "scale": .005}
    assert record["cpus"] == 1 and record["gpus"] == 0
    assert scaling.analyze([record])["status"] == "partial"


def test_example_missing_controls_and_remote_node_topology_fail_before_work(tmp_path):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for number_nodes in ("1", "2"):
        environment = {name: value for name, value in os.environ.items() if not name.startswith(("RAVEN_PARAM_", "RAVEN_SCALING_", "SLURM_"))}
        environment["SLURM_JOB_NUM_NODES"] = number_nodes
        output = tmp_path / f"invalid-{number_nodes}.json"
        failed = subprocess.run([sys.executable, str(root / "examples/planning/scaling_workload.py"), "run", "--output", str(output)],
                                env=environment, capture_output=True, text=True, timeout=10)
        assert failed.returncode == 2
        assert not output.exists()


def test_collector_counts_actual_input_bytes_including_whitespace(tmp_path):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    document = '{"workers":1,"state":"COMPLETED"}'
    path = tmp_path / "padded.json"
    path.write_text(document + " " * (65536 - len(document)))
    output = tmp_path / "records.json"
    result = subprocess.run([sys.executable, str(root / "examples/planning/scaling_workload.py"), "combine",
                             *([str(path)] * 129), "--output", str(output)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "byte budget" in result.stderr
    assert not output.exists()
