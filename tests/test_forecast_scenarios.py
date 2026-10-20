"""Constructed outcome fixtures validate algorithms, not external efficacy."""
import copy
import json
from pathlib import Path
import threading

import pytest

from raven import forecast
from raven import forecast_scenarios as S


def request(wall=240):
    return dict(name="solver-v1", partition="local", account="lab", qos="normal", cpus=1,
                nodes=1, gpus=0, mem_bytes=104857600, time_seconds=wall)


def observation(i, wall=240, **changes):
    when = i * 1000
    value = dict(id=f"attempt-{i}-{wall}", request=request(wall),
                 context=dict(policy_digest="policy-a", reservations_visible=True, policy_complete=True),
                 origin="recorded", submit=when, issued_at=when + 1, actual_start=when + 21,
                 actual_end=when + 61, state="completed", model_wait_seconds=10, scheduler_wait_seconds=15)
    return dict(value, **changes)


@pytest.fixture
def source():
    return dict(schema="raven.forecast-scenarios/v1", now=1000000, request=request(),
                context=dict(policy_digest="policy-a", reservations_visible=True, policy_complete=True),
                alternatives=[dict(id="shorter", request=request(120), model_wait_seconds=5),
                              dict(id="unsafe", request=request(30), model_wait_seconds=0)],
                observations=[observation(i, wall) for i in range(80) for wall in (240, 120)],
                wait_threshold_seconds=18)


def test_same_workload_shorter_request_requires_runtime_risk_evidence(source):
    result = forecast.scenarios(source)
    safe, unsafe = result["alternatives"][1:]
    assert safe["forecast"]["wait_seconds"] == 15
    assert safe["runtime_risk"]["upper_probability"] < .1
    assert result["preferred_scenario"] == "shorter"
    assert unsafe["runtime_risk"]["probability"] == 1
    assert not unsafe["eligible_for_comparison"]
    assert not result["production_qualified"]
    assert "No job request" in " ".join(result["limits"])


def test_audit_compares_three_models_and_discloses_probability_intervals(source):
    result = S.analyze(source)
    assert {r["model"] for r in result["evaluation"]} == {"simple_cohort", "current_predictor", "queue_residual"}
    residual = next(r for r in result["evaluation"] if r["model"] == "queue_residual")
    assert residual["mae_seconds"] == 0 and residual["interval_coverage"] == 1
    assert 0 < residual["prediction_coverage"] < 1
    assert residual["brier_score"] > 0 and residual["log_score"] > 0
    assert residual["probability_samples"] == residual["predictions"]
    current = next(r for r in result["evaluation"] if r["model"] == "current_predictor")
    assert current["mae_seconds"] == 5


def test_no_future_outcome_can_train_an_earlier_prediction(source):
    baseline = S.analyze(source)
    # A future-at-the-time extreme observation cannot change any preceding case.
    late = observation(900, actual_start=900900, actual_end=900950)
    earlier = S._audit(source["observations"], .8, 18, None)
    without_future = S._audit(source["observations"] + [late], .8, 18, None)
    # Test the per-target training predicate independently, including long jobs.
    target = source["observations"][80]
    allowed = [r for r in source["observations"] + [late] if r["actual_end"] < target["issued_at"]]
    assert late not in allowed
    assert S._fit(S._eligible(allowed, target["request"], target["context"]), 10, .8, 18, residual=True)["status"] == "forecast"
    assert baseline["evaluation"] == earlier
    assert all(r["eligible_attempts"] <= S.MAX_EVALUATIONS for r in without_future)


def test_missing_visibility_ood_drift_and_censoring_abstain(source):
    source["context"]["reservations_visible"] = False
    assert S.analyze(source)["preferred_scenario"] is None
    source["context"]["reservations_visible"] = True
    source["alternatives"][0]["request"]["cpus"] = 8
    assert not S.analyze(source)["alternatives"][1]["eligible_for_comparison"]
    source["alternatives"][0]["request"]["cpus"] = 1
    for row in source["observations"][-40:]:
        row["actual_start"] += 50
        row["actual_end"] += 50
    assert "drift" in S.analyze(source)["alternatives"][1]["forecast"]["reason"]
    source["observations"][0]["state"] = "timeout"
    assert S.analyze(source)["alternatives"][1]["runtime_risk"]["status"] == "unknown"


def test_synthetic_outcomes_never_calibrate_recorded_scenarios(source):
    for row in source["observations"]:
        row["origin"] = "synthetic"
    result = S.analyze(source)
    assert result["status"] == "abstain"
    assert all(row["origin"] == "synthetic" for row in result["evaluation"])


@pytest.mark.parametrize("damage", ["duplicate", "future", "after", "unknown", "wall", "overflow"])
def test_rejects_unsafe_or_ambiguous_contract(source, damage):
    if damage == "duplicate": source["observations"].append(copy.deepcopy(source["observations"][0]))
    elif damage == "future": source["observations"][0]["actual_end"] = source["now"] + 1
    elif damage == "after": source["observations"][0]["issued_at"] = source["observations"][0]["actual_start"]
    elif damage == "unknown": source["alternatives"][0]["submit"] = True
    elif damage == "wall": source["alternatives"][0]["request"]["time_seconds"] = 0
    else: source["observations"] *= 7
    with pytest.raises(ValueError): S.analyze(source)


def test_cancellation_and_input_immutability(source):
    original = copy.deepcopy(source)
    assert S.analyze(source) == S.analyze(source)
    assert source == original
    event = threading.Event()
    event.set()
    with pytest.raises(ValueError, match="cancelled"):
        S.analyze(source, cancel=event)


def test_existing_forecast_file_cli_and_research_share_result(source, tmp_path, capsys):
    from raven import cli, planning, planning_commands
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps(source))
    result, _ = planning_commands.result("forecast", ["--file", str(path)])
    assert result["input_sha256"] == planning.analyze("forecast", source)["input_sha256"]
    assert cli.main(["run", "forecast", "--file", str(path), "--no-state", "--no-plugins"]) == 0
    assert S.MODEL in capsys.readouterr().out


def test_queue_model_target_resources_change_only_in_copied_scenario(source):
    model = json.loads((Path(__file__).resolve().parents[1] / "examples/queue-model.json").read_text())
    target = model["jobs"][0]
    target.update(duration=10, wall_limit=240, account="lab", qos="normal")
    source.update(queue_model=model, target_job_id=target["id"], queue_model_issued_at=target["submit"])
    original = copy.deepcopy(source)
    result = S.analyze(source)
    assert all(item["queue_model_receipt"]["production_qualified"] is False for item in result["alternatives"])
    assert source == original


def test_current_historical_probability_does_not_borrow_scheduler_residuals(source):
    rows = [r for r in source["observations"] if r["request"]["time_seconds"] == 240]
    target = dict(request=request())
    first = S._current(rows, target, source["now"], .8, 18)
    for row in rows:
        row["scheduler_wait_seconds"] = 100000
    second = S._current(rows, target, source["now"], .8, 18)
    assert first["probability_wait_within_threshold"] == second["probability_wait_within_threshold"]
    assert first["probability_wait_within_threshold"] == pytest.approx(.5/81)


def test_queue_scenarios_require_clock_origin_and_supported_inventory(source):
    model = json.loads((Path(__file__).resolve().parents[1] / "examples/queue-model.json").read_text())
    target = model["jobs"][0]
    target.update(duration=10, wall_limit=240, account="lab", qos="normal")
    source.update(queue_model=model, target_job_id=target["id"])
    with pytest.raises(ValueError, match="queue_model_issued_at"):
        S.analyze(source)
    source["queue_model_issued_at"] = target["submit"]
    source["alternatives"][0]["request"]["partition"] = "another-partition"
    result = S.analyze(source)
    assert "another partition" in result["alternatives"][1]["model_error"]
    assert not result["alternatives"][1]["eligible_for_comparison"]
    source["alternatives"][0]["request"]["partition"] = "local"
    source["alternatives"][0]["request"].update(gpus=1, gpu_type="another-type")
    assert "GPU type" in S.analyze(source)["alternatives"][1]["model_error"]
    source["queue_model_issued_at"] = 100000
    result = S.analyze(source)
    assert "already started" in result["alternatives"][0]["model_error"]
    assert not result["alternatives"][0]["eligible_for_comparison"]


def test_queue_wait_is_relative_to_explicit_issue_clock(source, monkeypatch):
    from raven import slurm_simulator
    model = json.loads((Path(__file__).resolve().parents[1] / "examples/queue-model.json").read_text())
    target = model["jobs"][0]
    target.update(duration=10, wall_limit=240, account="lab", qos="normal", submit=0)
    source.update(queue_model=model, target_job_id=target["id"], queue_model_issued_at=5)
    def simulated(model, **kwargs):
        return dict(model="controlled-clock", input_sha256="fixture", timeline=[dict(id=target["id"], start=12, wait_seconds=12)], blocked={})
    monkeypatch.setattr(slurm_simulator, "simulate", simulated)
    result = S.analyze(source)
    assert result["alternatives"][0]["model_wait_seconds"] == 7
    assert result["alternatives"][0]["queue_model_receipt"]["issued_at_model_clock"] == 5


@pytest.mark.parametrize("model", [None, [], {"jobs": None}, {"jobs": "invalid"}])
def test_queue_model_shapes_use_existing_runtime_validation(source, model):
    source.update(queue_model=model, queue_model_issued_at=0, target_job_id="one")
    with pytest.raises(ValueError):
        S.analyze(source)


def test_actual_preissued_2605_holdout_replays_with_explicit_negative_efficacy():
    root = Path(__file__).resolve().parents[1] / "tests/fixtures/review-temporal-holdout/forecast"
    document = json.loads((root / "model.json").read_text())
    retained = json.loads((root / "result.json").read_text())
    original = copy.deepcopy(document)
    actual = S.analyze(document)
    assert actual["evaluation"] == retained["evaluation"]
    assert document == original
    assert len(document["observations"]) == 65
    assert all(row["origin"] == "recorded" and row["submit"] <= row["issued_at"] < row["actual_start"] < row["actual_end"] <= document["now"] for row in document["observations"])
    rows = {row["model"]: row for row in actual["evaluation"]}
    assert all(row["predictions"] == row["probability_samples"] == 25 for row in rows.values())
    assert rows["queue_residual"]["mae_seconds"] == rows["simple_cohort"]["mae_seconds"]
    assert rows["queue_residual"]["mae_seconds"] >= rows["current_predictor"]["mae_seconds"]
    assert not actual["production_qualified"]
