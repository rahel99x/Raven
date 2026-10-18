"""Analytical, independent stochastic and complete reviewed advice paths."""
import copy
import json
import math

import pytest

from raven import checkpoint_advisor as C


@pytest.fixture
def document():
    return {"schema": "raven.checkpoint-advice/v1", "scope": {"partition": "cpu", "nodes": 2, "application": "solver-v1"},
            "support": {"compatible": True, "restart_verified": True}, "useful_seconds": 10000,
            "checkpoint_seconds": {"samples_seconds": [2, 2, 2], "source": "monotonic elapsed around atomic publication"},
            "restart_seconds": {"samples_seconds": [10, 10, 10], "source": "timed application restart"},
            "failure_model": {"allocation_mtbf": {"lower_seconds": 100000, "upper_seconds": 100000, "source": "declared estimate"}},
            "fixed_interval_seconds": 3600}


def test_young_units_and_finite_work_integer_optimum(document):
    value = C.advise(document)
    assert value["status"] == "advisory"
    assert value["recommendation"]["young_interval_seconds"] == pytest.approx(math.sqrt(400000))
    assert value["recommendation"]["segments"] == 16
    assert value["recommendation"]["interval_seconds"] == 625
    costs = {row["name"]: row for row in value["baselines"]}
    assert costs["advised"]["writes"] == 15
    assert costs["advised"]["overhead_seconds"] < costs["fixed"]["overhead_seconds"] < costs["none"]["overhead_seconds"]
    # Brute-force equal segmentation independently checks the integer choice.
    brute = min(range(1, 100), key=lambda n: (n - 1) * 2 + .00001 * 10000**2 / (2 * n))
    assert brute == value["recommendation"]["segments"]


def test_independent_first_order_simulation_matches_expected_loss(document):
    value = C.advise(document)
    for row in value["baselines"]:
        simulated = C.simulate_first_order(10000, row["interval_seconds"], 2, 10, .00001, trials=10000, seed=1701)
        assert abs(simulated["mean_overhead_seconds"] - row["overhead_seconds"]) < 4 * simulated["standard_error_seconds"]
        assert "no live" in simulated["scope"]


def test_zero_node_failures_never_uses_timeout_or_preemption_as_mtbf(document):
    document["failure_model"] = {"stationary": True, "representative": True, "observations": [
        {"id": name, "exposure_seconds": 1000, "outcome": name} for name in ("timeout", "preempted", "completed", "censored")]}
    result = C.advise(document)
    assert result["status"] == "abstain"
    hazard = result["failure_model"]
    assert hazard["counts"]["node_failure"] == 0 and hazard["allocation_exposure_seconds"] == 4000
    assert hazard["rate_upper"] == pytest.approx(-math.log(.05) / 4000)
    assert hazard["censored_observations"] == 4


def test_poisson_exact_interval_and_biased_observation_abstention(document):
    document["failure_model"] = {"stationary": True, "representative": True, "observations": [
        {"id": "failed", "exposure_seconds": 100000, "outcome": "node_failure"}]}
    result = C.advise(document)
    hazard = result["failure_model"]
    assert hazard["rate_lower"] * 100000 == pytest.approx(.0253178, rel=1e-5)
    assert hazard["rate_upper"] * 100000 == pytest.approx(5.57164, rel=1e-5)
    assert len(result["sensitivity"]) == 2
    document["failure_model"]["representative"] = False
    assert C.advise(document)["status"] == "abstain"


def test_uncertain_costs_produce_sensitivity_and_separate_notice_budget(document):
    document["checkpoint_seconds"] = {"lower_seconds": 1, "upper_seconds": 4, "source": "explicit range before measurement"}
    document.update(preemption_notice_seconds=3, remaining_wall_seconds=120)
    result = C.advise(document)
    assert result["interval_range_seconds"][0] < result["interval_range_seconds"][1]
    assert not result["policy_events"]["preemption"]["checkpoint_fits_estimated_budget"]
    assert result["policy_events"]["wall_time"]["checkpoint_fits_estimated_budget"]
    assert all(row["overhead_upper_seconds"] >= row["overhead_lower_seconds"] for row in result["baselines"])


@pytest.mark.parametrize("change", ["compatibility", "unknown_cost", "frequent_failure", "no_exposure"])
def test_unsupported_or_unknown_inputs_abstain(document, change):
    if change == "compatibility":
        document["support"]["compatible"] = False
    elif change == "unknown_cost":
        document.pop("checkpoint_seconds")
    elif change == "frequent_failure":
        document["failure_model"]["allocation_mtbf"].update(lower_seconds=10, upper_seconds=10)
    else:
        document["failure_model"] = {"stationary": True, "representative": True, "observations": []}
    assert C.advise(document)["status"] == "abstain"


def test_short_work_needs_no_final_checkpoint(document):
    document["useful_seconds"] = 50
    result = C.advise(document)
    assert result["recommendation"]["interval_seconds"] is None
    assert result["baselines"][0]["writes"] == 0


def test_unmeasured_metadata_is_not_a_duration(document):
    document["checkpoint_seconds"] = {"bytes": 1000, "mtime": 20}
    with pytest.raises(ValueError):
        C.advise(document)


def test_source_is_immutable_and_receipt_reproducible(document):
    original = copy.deepcopy(document)
    a, b = C.advise(document), C.advise(document)
    assert a == b and document == original
    assert C.simulate_first_order(100, 10, 1, 1, .001, seed=4) == C.simulate_first_order(100, 10, 1, 1, .001, seed=4)


def test_planning_command_cli_and_research_share_receipt(tmp_path, document, capsys):
    from raven import cli, planning, planning_commands
    path = tmp_path / "advice.json"
    path.write_text(json.dumps(document))
    result, _ = planning_commands.result("checkpoint-advice", [str(path)])
    assert result["input_sha256"] == planning.analyze("checkpoint-advice", document)["input_sha256"]
    assert cli.main(["run", "checkpoint-advice", str(path), "--no-state", "--no-plugins"]) == 0
    assert "young-finite-work/1" in capsys.readouterr().out


def test_launch_review_captures_advice_and_changed_input_requires_new_review(tmp_path, document, monkeypatch):
    from raven import launch, launch_settings, launch_wizard
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/sh\ntrue\n")
    path = tmp_path / "advice.json"
    path.write_text(json.dumps(document))
    launch_settings.update_settings(str(tmp_path), {"launch": {"checkpoint_advice": "advice.json"}})
    draft = {"project": str(tmp_path), "script": "job.sh", "execution": "slurm"}
    reviewed = launch_wizard.review(draft)
    assert reviewed["plan"]["checkpoint_advice"]["status"] == "advisory"
    assert "Assumption:" in launch.format_plan(reviewed["plan"])
    assert "--signal" not in reviewed["plan"]["sbatch_options"]
    document["useful_seconds"] = 5000
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="changed after review"):
        launch_wizard.submit(reviewed, draft, runner=lambda *a: pytest.fail("must not submit"))


def test_advice_paths_cannot_leave_project(tmp_path, document):
    from raven import launch
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/sh\ntrue\n")
    with pytest.raises(ValueError, match="project-relative"):
        launch.resolve_plan([str(script), "--project", str(tmp_path), "--checkpoint-advice", "../outside.json"])


@pytest.mark.parametrize("field", ["checkpoint_seconds", "restart_seconds", "useful_seconds", "fixed_interval_seconds", "mtbf", "exposure"])
def test_subnormal_durations_are_rejected_before_arithmetic(field):
    from pathlib import Path
    import json
    from raven import checkpoint_advisor as advisor
    value = json.loads((Path(__file__).resolve().parents[1] / "examples/checkpoint-advice.json").read_text())
    if field in {"checkpoint_seconds", "restart_seconds"}:
        value[field] = {"samples_seconds": [1e-308], "source": "invalid precision"}
    elif field == "mtbf":
        value["failure_model"] = {"allocation_mtbf": {"lower_seconds": 1e-308, "upper_seconds": 1e-308, "source": "invalid precision"}}
    elif field == "exposure":
        value["failure_model"] = {"stationary": True, "representative": True, "observations": [{"id": "one", "exposure_seconds": 1e-308, "outcome": "node_failure"}]}
    else:
        value[field] = 1e-308
    with pytest.raises(ValueError, match="1e-09"):
        advisor.advise(value)


def test_generic_planning_bundle_is_incomplete_without_advice_and_rejects_invalid_explicit_input():
    from raven import planning
    bundle = {"version": 1, "kind": "raven.planning", "history": []}
    assert planning.analyze("checkpoint-advice", bundle)["status"] == "incomplete"
    for source in ({"schema": "raven.checkpoint-advice/v1"}, {**bundle, "checkpoint_advice": {"schema": "raven.checkpoint-advice/v1"}}):
        with pytest.raises(ValueError):
            planning.analyze("checkpoint-advice", source)
