"""Submission comparisons must disclose assumptions and never invent scaling."""
from __future__ import annotations

import copy
import json
import math
import os
import random
import shlex
import sys
from types import ModuleType

import pytest

from raven import tradeoffs


def choice(**values):
    candidate = {"label": "Baseline", "name": "same-work", "cpus": 8, "nodes": 1,
                 "gpus": 0, "mem_bytes": 8 << 30, "time_seconds": 3600,
                 "partition": "cpu", "account": "research", "qos": "normal",
                 "parameters": {"seed": 3}, "estimated_runtime_seconds": 600}
    candidate.update(values)
    return candidate


@pytest.fixture(autouse=True)
def deterministic_clock(monkeypatch):
    monkeypatch.setattr(tradeoffs.clock, "now", lambda: 2000.0)


@pytest.fixture(autouse=True)
def clear_sbatch_environment(monkeypatch):
    for name in os.environ:
        if name.startswith("SBATCH_"):
            monkeypatch.delenv(name)


def fake_predict(monkeypatch, function):
    module = ModuleType("raven.predict")
    module.predict = function
    monkeypatch.setitem(sys.modules, "raven.predict", module)


def fake_forecast(monkeypatch, function):
    module = ModuleType("raven.forecast")
    module.forecast = function
    monkeypatch.setitem(sys.modules, "raven.forecast", module)


def interval(lower, upper, estimate=None, **values):
    result = {"lower": lower, "upper": upper, "estimate": (lower + upper) / 2 if estimate is None else estimate,
              "samples": 32, "method": "split_conformal_median", "status": "ok"}
    result.update(values)
    return result


def test_explicit_estimate_and_reservation_totals_do_not_multiply_nodes():
    result = tradeoffs.compare([choice(cpus=32, nodes=4, gpus=8, gpu_type="a100", time_seconds=7200)])
    metrics = result["candidates"][0]["metrics"]
    assert metrics["runtime_seconds"]["status"] == "assumed"
    assert metrics["runtime_seconds"]["lower"] == metrics["runtime_seconds"]["upper"] == 600
    assert metrics["reserved_core_hours"]["estimate"] == 64
    assert metrics["reserved_gpu_hours"]["estimate"] == 16
    assert metrics["reserved_memory_gib_hours"]["estimate"] == 16
    assert metrics["completion_seconds"]["estimate"] is None
    assert any("no empirical coverage" in text for text in result["candidates"][0]["assumptions"])
    assert "reserved_memory_gib_hours" not in result["objectives"]
    assert json.loads(json.dumps(result, allow_nan=False)) == result


def test_no_resource_speedup_is_invented_without_measurements():
    slow = choice(cpus=4, estimated_runtime_seconds=None)
    fast = choice(cpus=64, estimated_runtime_seconds=None)
    result = tradeoffs.compare([slow, fast])
    assert result["status"] == "partial"
    assert result["frontier"] == [0, 1]
    for row in result["candidates"]:
        assert row["metrics"]["runtime_seconds"]["estimate"] is None
        assert row["pareto"]["compared_with"] == []
        assert row["pareto"]["certainly_dominated_by"] == []


def test_measured_prediction_for_exact_requested_identity(monkeypatch):
    calls = []

    def predict(history, query, coverage):
        calls.append((history, query, coverage))
        return {"metrics": {"runtime_seconds": interval(400, 800)}, "evidence": ["observed cohort"], "limitations": []}

    fake_predict(monkeypatch, predict)
    candidate = choice(estimated_runtime_seconds=None, script_sha256="a" * 64, input_size=32)
    result = tradeoffs.compare([candidate, candidate], history=[{"id": "1"}], coverage=.9)
    assert len(calls) == 1
    assert calls[0][1] == {key: value for key, value in candidate.items() if key not in {"label", "estimated_runtime_seconds"}}
    assert calls[0][2] == .9
    assert result["candidates"][0]["metrics"]["runtime_seconds"]["lower"] == 400
    assert result["candidates"][0]["metrics"]["runtime_seconds"]["status"] == "predicted"
    assert result["candidates"][0]["evidence"] == ["observed cohort"]


def test_user_estimate_does_not_call_history_predictor(monkeypatch):
    fake_predict(monkeypatch, lambda *args, **kwargs: pytest.fail("explicit estimate should remain explicit"))
    result = tradeoffs.compare([choice()], history=[{"id": "1"}])
    assert result["candidates"][0]["status"] == "assumed"


def test_censored_runtime_never_becomes_finite_prediction(monkeypatch):
    fake_predict(monkeypatch, lambda *args, **kwargs: {
        "metrics": {"runtime_seconds": {"status": "censored", "lower": 900, "upper": None}},
        "limitations": ["matching timeout was right-censored"], "evidence": []})
    result = tradeoffs.compare([choice(estimated_runtime_seconds=None)], history=[{"state": "TIMEOUT"}])
    row = result["candidates"][0]
    assert row["metrics"]["runtime_seconds"]["status"] == "censored"
    assert row["metrics"]["runtime_seconds"]["upper"] is None
    assert row["status"] == "incomplete"
    assert any("right-censored" in item for item in row["limitations"])


def test_calibrated_queue_plus_runtime_has_no_joint_coverage_claim(monkeypatch):
    fake_predict(monkeypatch, lambda *args, **kwargs: {"metrics": {"runtime_seconds": interval(100, 300)},
                                                     "evidence": [], "limitations": []})
    captured = []

    def forecast(job, history, *, now, coverage):
        captured.append((job, history, now, coverage))
        return {"calibration": {"valid": True}, "lower_start": 2200, "upper_start": 2600,
                "predicted_start": 2300, "samples": 40, "method": "historical_split_conformal",
                "evidence": [], "limitations": []}

    fake_forecast(monkeypatch, forecast)
    result = tradeoffs.compare([choice(estimated_runtime_seconds=None)], history=[{}], queue_history=[{}])
    metrics = result["candidates"][0]["metrics"]
    assert metrics["wait_seconds"]["lower"] == 200
    assert metrics["wait_seconds"]["upper"] == 600
    assert metrics["completion_seconds"]["lower"] == 300
    assert metrics["completion_seconds"]["upper"] == 900
    assert metrics["completion_seconds"]["estimate"] == 500
    assert "completion_seconds" in result["objectives"]
    assert captured[0][0]["state"] == "PENDING"
    assert captured[0][0]["submit"] == 2000
    assert captured[0][0]["est_start"] == ""
    assert captured[0][0]["mem_bytes"] == 8 << 30
    assert captured[0][0]["time_seconds"] == 3600
    assert any("no joint coverage" in text for text in result["candidates"][0]["assumptions"])


@pytest.mark.parametrize("calibration,lower,upper", [
    (False, 2200, 2300), (True, None, 2400), (True, 1900, 2400),
    (True, 2400, 2300), (True, float("nan"), 2400), (True, 2200, float("inf")),
    (True, True, 2400), (True, 2200, False),
])
def test_uncalibrated_or_bad_queue_intervals_are_unknown(monkeypatch, calibration, lower, upper):
    fake_forecast(monkeypatch, lambda *args, **kwargs: {
        "calibration": {"valid": calibration}, "lower_start": lower, "upper_start": upper,
        "predicted_start": 2300, "samples": 99, "evidence": [], "limitations": []})
    result = tradeoffs.compare([choice()], queue_history=[{}])
    assert result["candidates"][0]["metrics"]["wait_seconds"]["estimate"] is None
    assert result["candidates"][0]["metrics"]["completion_seconds"]["estimate"] is None
    assert "completion_seconds" not in result["objectives"]
    json.dumps(result, allow_nan=False)


def test_missing_request_memory_or_walltime_abstains_from_queue(monkeypatch):
    fake_forecast(monkeypatch, lambda *args, **kwargs: pytest.fail("must not weaken the request identity"))
    result = tradeoffs.compare([choice(mem_bytes=None), choice(time_seconds=None)], queue_history=[{}])
    for row in result["candidates"]:
        assert row["metrics"]["wait_seconds"]["lower"] is None


def test_interval_dominance_separates_possible_and_certain(monkeypatch):
    intervals = {4: (100, 200), 8: (180, 280), 12: (500, 600)}
    fake_predict(monkeypatch, lambda history, query, **kwargs: {
        "metrics": {"runtime_seconds": interval(*intervals[query["cpus"]])}, "evidence": [], "limitations": []})
    result = tradeoffs.compare([choice(cpus=c, estimated_runtime_seconds=None) for c in (4, 8, 12)], history=[{}])
    assert result["candidates"][1]["pareto"]["possibly_dominated_by"] == [0]
    assert result["candidates"][1]["pareto"]["certainly_dominated_by"] == []
    assert result["candidates"][2]["pareto"]["certainly_dominated_by"] == [0, 1]
    assert result["frontier"] == [0, 1]


def test_skyline_matches_independent_scalar_oracle():
    rng = random.Random(71831)
    for _ in range(100):
        candidates = [choice(cpus=rng.randint(1, 24), time_seconds=rng.randint(1, 8) * 600,
                             estimated_runtime_seconds=rng.randint(1, 20) * 30) for __ in range(16)]
        points = [(item["estimated_runtime_seconds"], item["cpus"] * item["time_seconds"] / 3600, 0)
                  for item in candidates]
        oracle = [i for i, point in enumerate(points) if not any(
            j != i and all(a <= b for a, b in zip(other, point)) and any(a < b for a, b in zip(other, point))
            for j, other in enumerate(points))]
        result = tradeoffs.compare(candidates)
        assert result["frontier"] == oracle


@pytest.mark.parametrize("changed", [
    {"name": "different"}, {"parameters": {"seed": 4}}, {"script_sha256": "a" * 64}, {"input_size": 18},
])
def test_different_work_definitions_never_dominate(changed):
    result = tradeoffs.compare([choice(cpus=4, estimated_runtime_seconds=100), choice(cpus=16, estimated_runtime_seconds=1000, **changed)])
    assert result["frontier"] == [0, 1]
    assert all(row["pareto"]["compared_with"] == [] for row in result["candidates"])


def test_labels_and_duplicate_labels_do_not_define_work_identity():
    result = tradeoffs.compare([choice(label="Repeated"), choice(label="Repeated", cpus=12), choice(name=None, label="same-work")])
    assert [row["index"] for row in result["candidates"]] == [0, 1, 2]
    assert result["candidates"][2]["work_id"] is None
    assert 2 not in result["frontier"]
    assert result["candidates"][2]["pareto"]["compared_with"] == []


def test_positive_gpu_hours_need_known_compatible_gpu_type():
    result = tradeoffs.compare([choice(cpus=4, gpus=1, gpu_type="a100", estimated_runtime_seconds=100),
                               choice(cpus=16, gpus=4, gpu_type="v100", estimated_runtime_seconds=1000),
                               choice(cpus=32, gpus=8, gpu_type=None, estimated_runtime_seconds=2000)])
    assert result["frontier"] == [0, 1, 2]
    assert all(row["pareto"]["compared_with"] == [] for row in result["candidates"])


def test_no_gpu_allocation_is_compatible_zero_baseline():
    result = tradeoffs.compare([choice(cpus=4, gpus=0, estimated_runtime_seconds=100),
                               choice(cpus=16, gpus=4, gpu_type="a100", estimated_runtime_seconds=1000)])
    assert result["frontier"] == [0]


def test_unknown_metric_never_dominates_known_candidate():
    result = tradeoffs.compare([choice(cpus=4, estimated_runtime_seconds=None), choice(cpus=32)])
    assert result["frontier"] == [0, 1]
    assert result["candidates"][1]["pareto"]["certainly_dominated_by"] == []


def test_different_metric_units_or_scopes_do_not_compare():
    result = tradeoffs.compare([choice(cpus=4), choice(cpus=8, estimated_runtime_seconds=800)])
    left, right = result["candidates"]
    right["metrics"]["runtime_seconds"]["unit"] = "milliseconds"
    assert tradeoffs._dominance(left, right, result["objectives"]) is None
    right["metrics"]["runtime_seconds"]["unit"] = "seconds"
    right["metrics"]["runtime_seconds"]["scope"] = "other_scope"
    assert tradeoffs._dominance(left, right, result["objectives"]) is None


@pytest.mark.parametrize("field,value", [
    ("cpus", 0), ("cpus", True), ("cpus", 1.5), ("cpus", "8"), ("cpus", 1 << 70),
    ("nodes", 0), ("nodes", 20), ("gpus", -1), ("gpus", False), ("mem_bytes", 0),
    ("mem_bytes", float("nan")), ("time_seconds", 0), ("time_seconds", float("nan")),
    ("time_seconds", float("inf")), ("estimated_runtime_seconds", -1), ("estimated_runtime_seconds", True),
    ("estimated_runtime_seconds", float("inf")), ("input_size", -1), ("input_size", 1 << 5000),
    ("label", "bad\x1b[31m"), ("label", ""), ("name", "n" * 129), ("account", "a\nb"),
    ("script_sha256", "bad"), ("parameters", []), ("parameters", {"x": float("nan")}),
    ("parameters", {"x": {"a": object()}}), ("parameters", {"\x1b": 1}),
    ("label", "\ud800"), ("parameters", {"value": "\udfff"}),
])
def test_bad_candidate_isolated_without_losing_valid_choice(field, value):
    result = tradeoffs.compare([choice(**{field: value}), choice(label="Valid")])
    assert result["status"] == "partial"
    assert result["candidates"][0]["status"] == "invalid"
    assert result["candidates"][1]["status"] == "assumed"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("coverage", [0, 1, -1, float("nan"), float("inf"), True, "0.8", 1 << 5000])
def test_bad_coverage_rejected(coverage):
    with pytest.raises(ValueError):
        tradeoffs.compare([], coverage=coverage)


@pytest.mark.parametrize("limit", [0, 65, -2, 2.5, True])
def test_bad_candidate_bound_rejected(limit):
    with pytest.raises(ValueError):
        tradeoffs.compare([], max_candidates=limit)


def test_inputs_are_bounded_and_generators_are_not_exhausted():
    consumed = []

    def stream():
        for index in range(100000):
            consumed.append(index)
            yield choice(label=str(index))

    result = tradeoffs.compare(stream(), max_candidates=4, history=({} for _ in range(10001)))
    assert consumed == [0, 1, 2, 3, 4]
    assert len(result["candidates"]) == 4
    assert any("truncated" in text for text in result["limitations"])
    assert any("10000" in text for text in result["limitations"])


@pytest.mark.parametrize("source", [None, "text", b"bytes", {"a": 1}, 1])
def test_non_iterable_record_bundles_rejected(source):
    with pytest.raises(ValueError):
        tradeoffs.compare([choice()], history=source)


def test_empty_and_all_invalid_results_are_finite_json():
    assert tradeoffs.compare([])["status"] == "empty"
    result = tradeoffs.compare([None, [], {"cpus": -1}])
    assert result["status"] == "error"
    assert result["frontier"] == []
    json.dumps(result, allow_nan=False)


def test_walltime_risk_uses_interval_upper_bound(monkeypatch):
    fake_predict(monkeypatch, lambda *args, **kwargs: {"metrics": {"runtime_seconds": interval(500, 4000)},
                                                     "evidence": [], "limitations": []})
    row = tradeoffs.compare([choice(estimated_runtime_seconds=None)], history=[{}])["candidates"][0]
    assert any("upper bound" in risk for risk in row["risks"])


def test_prepare_choice_is_exact_argv_and_never_executes(tmp_path, monkeypatch):
    script = tmp_path / "batch's file.sh"
    script.write_text("#!/bin/bash\ntouch NEVER_EXECUTED\n")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("preparation executed subprocess"))
    candidate = choice(cpus=24, nodes=3, gpus=6, gpu_type="a100", mem_bytes=7 * (1 << 30) + 1,
                       time_seconds=90061.1, account="safe account's ; $(touch BAD)")
    result = tradeoffs.compare([candidate])
    plan = tradeoffs.prepare_choice(result, 0, script, workdir=tmp_path)
    assert plan["valid"], plan["issues"]
    assert plan["resources"]["ntasks"] == "3"
    assert plan["resources"]["cpus_per_task"] == "8"
    assert plan["resources"]["nodes"] == "3"
    assert plan["resources"]["gpus"] == "a100:6"
    assert plan["resources"]["mem"] == "2390M"
    assert plan["resources"]["time"] == "1-01:01:02"
    assert plan["parameters"] == {"seed": 3}
    assert shlex.split(plan["command"]) == ["sbatch", *plan["argv"]]
    assert plan["argv"][-1] == str(script)
    assert not (tmp_path / "NEVER_EXECUTED").exists()
    assert not (tmp_path / "BAD").exists()


def test_prepare_does_not_mutate_choice_or_result(tmp_path):
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    result = tradeoffs.compare([choice()])
    before = copy.deepcopy(result)
    tradeoffs.prepare_choice(result, 0, script)
    assert result == before


@pytest.mark.parametrize("field", ["cpus", "nodes", "gpus", "mem_bytes", "time_seconds"])
def test_prepare_requires_complete_explicit_request(tmp_path, field):
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    with pytest.raises(ValueError, match="explicit"):
        tradeoffs.prepare_choice(tradeoffs.compare([choice(**{field: None})]), 0, script)


def test_prepare_nondivisible_cpu_topology_rejected(tmp_path):
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    with pytest.raises(ValueError, match="divide evenly"):
        tradeoffs.prepare_choice(tradeoffs.compare([choice(cpus=10, nodes=3)]), 0, script)


@pytest.mark.parametrize("directive", [
    "--cpus-per-gpu=2", "--ntasks-per-core=2", "--ntasks-per-gpu=2", "--ntasks-per-socket=2",
    "--ntasks-per-node=2", "--threads-per-core=2", "--mem-per-cpu=2G", "--mem-per-gpu=2G",
    "--gpus-per-node=2", "--gpus-per-socket=2", "--gpus-per-task=2", "--gres=gpu:1", "--gpus=1",
])
def test_prepare_rejects_inherited_resource_semantics(tmp_path, directive):
    script = tmp_path / "job.sh"
    script.write_text(f"#!/bin/bash\n#SBATCH {directive}\ntrue\n")
    with pytest.raises(ValueError, match="conflict"):
        tradeoffs.prepare_choice(tradeoffs.compare([choice()]), 0, script)


def test_choice_script_hash_revalidated_without_execution(tmp_path):
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    with pytest.raises(ValueError, match="digest"):
        tradeoffs.prepare_choice(tradeoffs.compare([choice(script_sha256="0" * 64)]), 0, script)


@pytest.mark.parametrize("index", [-1, True, 3, "0"])
def test_bad_choice_index_rejected(tmp_path, index):
    with pytest.raises(ValueError):
        tradeoffs.prepare_choice(tradeoffs.compare([choice()]), index, tmp_path / "unused.sh")


def test_choice_invalid_or_duplicate_index_rejected(tmp_path):
    invalid = tradeoffs.compare([choice(cpus=-1)])
    with pytest.raises(ValueError, match="valid candidate"):
        tradeoffs.prepare_choice(invalid, 0, tmp_path / "unused.sh")
    duplicate = tradeoffs.compare([choice(), choice()])
    duplicate["candidates"][1]["index"] = 0
    with pytest.raises(ValueError, match="one valid"):
        tradeoffs.prepare_choice(duplicate, 0, tmp_path / "unused.sh")


def test_bad_gpu_type_rejected_at_preparation(tmp_path):
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    with pytest.raises(ValueError, match="resource token"):
        tradeoffs.prepare_choice(tradeoffs.compare([choice(gpus=1, gpu_type="GPU:foo")]), 0, script)


def test_preparation_preserves_preflight_failure_as_reviewable_plan(tmp_path):
    script = tmp_path / "missing.sh"
    plan = tradeoffs.prepare_choice(tradeoffs.compare([choice()]), 0, script)
    assert plan["valid"] is False
    assert any(issue["level"] == "error" for issue in plan["issues"])


def test_prepare_local_regular_script_only_fifo_returns_fast(tmp_path):
    path = tmp_path / "pipe"
    os.mkfifo(path)
    plan = tradeoffs.prepare_choice(tradeoffs.compare([choice()]), 0, path)
    assert plan["valid"] is False
    assert any("regular file" in issue["message"] for issue in plan["issues"])


def real_records(candidate, *, count=40, runtime=100):
    return [{**candidate, "id": str(i + 1), "state": "COMPLETED", "runtime_seconds": runtime,
             "submit": 100 + i, "start": 110 + i, "end": 110 + i + runtime,
             "memory_bytes": 1 << 30, "memory_scope": "job_peak", "cpu_seconds": runtime * candidate["cpus"]}
            for i in range(count)]


def test_real_models_use_exact_resources_and_build_conservative_completion():
    base = choice(estimated_runtime_seconds=None)
    history = real_records(base)
    result = tradeoffs.compare([base, choice(cpus=16, estimated_runtime_seconds=None)], history=history, queue_history=history)
    measured, unsupported = result["candidates"]
    assert measured["status"] == "ok"
    assert measured["metrics"]["runtime_seconds"]["estimate"] == 100
    assert measured["metrics"]["wait_seconds"]["estimate"] == 10
    assert measured["metrics"]["completion_seconds"]["lower"] == 110
    assert measured["metrics"]["completion_seconds"]["upper"] == 110
    assert unsupported["status"] == "incomplete"
    assert unsupported["metrics"]["runtime_seconds"]["estimate"] is None
    assert unsupported["metrics"]["wait_seconds"]["estimate"] is None
    assert unsupported["pareto"]["certainly_dominated_by"] == []


def test_real_prediction_recent_drift_is_not_pareto_evidence():
    base = choice(estimated_runtime_seconds=None)
    records = real_records(base, count=30)
    for row in records[24:]:
        row["runtime_seconds"] = 1000
    result = tradeoffs.compare([base, choice(cpus=16, estimated_runtime_seconds=500)], history=records)
    poor = result["candidates"][0]
    assert poor["status"] == "incomplete"
    assert poor["metrics"]["runtime_seconds"]["validation_status"] == "poor_validation"
    assert poor["metrics"]["runtime_seconds"]["observed_coverage"] == 0
    assert poor["metrics"]["runtime_seconds"]["lower"] == 100
    assert poor["metrics"]["completion_seconds"]["lower"] is None
    assert any("validation failed" in risk for risk in poor["risks"])
    assert result["candidates"][1]["pareto"]["certainly_dominated_by"] == []


def test_real_history_timeout_censoring_is_preserved_after_indexing():
    base = choice(estimated_runtime_seconds=None)
    records = real_records(base, count=30)
    records.append({**records[0], "id": "timeout", "state": "TIMEOUT", "runtime_seconds": 3600, "end": 1900})
    row = tradeoffs.compare([base], history=records)["candidates"][0]
    assert row["metrics"]["runtime_seconds"]["status"] == "censored"
    assert row["metrics"]["runtime_seconds"]["estimate"] is None


def test_real_history_model_aliases_remain_available_with_core_indexing():
    from raven.model import Finished
    candidate = choice(account=None, qos=None, parameters=None, estimated_runtime_seconds=None)
    records = [Finished(id=str(i), name="same-work", state="COMPLETED", cpus=8,
                        elapsed="00:01:40", req_mem=8 << 30, rss=1 << 30, cpu_time=800,
                        partition="cpu", submit=100 + i, start=110 + i, end=210 + i,
                        limit="01:00:00") for i in range(40)]
    row = tradeoffs.compare([candidate], history=records, queue_history=records)["candidates"][0]
    assert row["metrics"]["runtime_seconds"]["estimate"] == 100
    assert row["metrics"]["wait_seconds"]["estimate"] == 10


def test_real_queue_memory_and_walltime_cannot_be_weakened_by_alternatives():
    base = choice(estimated_runtime_seconds=None)
    records = real_records(base)
    result = tradeoffs.compare([choice(mem_bytes=16 << 30), choice(time_seconds=7200)], queue_history=records)
    for row in result["candidates"]:
        assert row["metrics"]["wait_seconds"]["lower"] is None
        assert row["metrics"]["completion_seconds"]["upper"] is None


def test_exact_core_index_bounds_work_to_relevant_records(monkeypatch):
    seen = []

    def predict(records, query, **kwargs):
        seen.append(len(records))
        assert all(row["cpus"] == query["cpus"] for row in records)
        return {"metrics": {"runtime_seconds": interval(100, 200)}, "evidence": [], "limitations": []}

    fake_predict(monkeypatch, predict)
    candidates = [choice(cpus=i + 1, estimated_runtime_seconds=None) for i in range(64)]
    history = [{**candidates[i % 64], "id": str(i)} for i in range(10000)]
    result = tradeoffs.compare(candidates, history=history)
    assert len(seen) == 64
    assert sum(seen) == 10000
    assert result["status"] == "ok"
