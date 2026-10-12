import copy
import math

import pytest

from raven import science_stats as stats


def comparison(design="independent", a=(1, 2, 3), b=(4, 5, 6)):
    return {"schema": "raven.statistics/v1", "design": design, "unit": "independently seeded experiment",
            "control": [{"id": str(i), "value": v} for i, v in enumerate(a)],
            "treatment": [{"id": str(i if design == "paired" else i+100), "value": v} for i, v in enumerate(b)]}


@pytest.mark.parametrize("df,critical", [(1, 12.7062047364), (2, 4.30265272975), (5, 2.57058183564),
                                           (10, 2.22813885196), (30, 2.0422724563), (1000, 1.96233908083)])
def test_student_t_against_published_quantiles(df, critical):
    assert stats.t_critical(df) == pytest.approx(critical, rel=1e-9)
    assert stats.t_survival(critical, df) == pytest.approx(.05, rel=1e-9)


def test_welch_unequal_groups_independent_units():
    result = stats.compare(comparison())
    assert result["difference"] == 3
    assert result["df"] == pytest.approx(4)
    assert result["interval"] == pytest.approx([.733042064, 5.266957936])
    assert result["p_value"] == pytest.approx(.0213116411)


def test_paired_uses_differences_and_ids_not_input_order():
    doc = comparison("paired", (10, 12, 14), (12, 15, 18))
    doc["treatment"].reverse()
    result = stats.compare(doc)
    assert result["difference"] == 3
    assert result["df"] == 2
    assert result["interval"] == pytest.approx([.515862288, 5.484137712])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, "3", None, 10**400, 1e101])
def test_invalid_observations_are_never_silently_coerced(value):
    doc = comparison()
    doc["control"][0]["value"] = value
    with pytest.raises(ValueError):
        stats.compare(doc)


@pytest.mark.parametrize("design", ["paired", "independent"])
def test_missing_units_unknown_uncertainty(design):
    result = stats.compare(comparison(design, (1,), (2,)))
    assert result["interval"] is None
    assert result["p_value"] is None


@pytest.mark.parametrize("design", ["paired", "independent"])
def test_zero_variance_does_not_invent_certainty(design):
    result = stats.compare(comparison(design, (1, 1, 1), (2, 2, 2)))
    assert result["interval"] is None
    assert result["p_value"] is None
    assert "zero" in result["warnings"][0]


@pytest.mark.parametrize("mutation", ["duplicate", "missing_pair", "independent_reuse", "bad_design", "missing_unit", "extra_key", "empty"])
def test_unit_contract_rejects_ambiguous_experiments(mutation):
    doc = comparison("paired" if mutation == "missing_pair" else "independent")
    if mutation == "duplicate":
        doc["control"][1]["id"] = doc["control"][0]["id"]
    elif mutation == "missing_pair":
        doc["treatment"].pop()
    elif mutation == "independent_reuse":
        doc["treatment"][0]["id"] = doc["control"][0]["id"]
    elif mutation == "bad_design":
        doc["design"] = "unpaired-but-repeated"
    elif mutation == "missing_unit":
        doc["unit"] = ""
    elif mutation == "extra_key":
        doc["discard_missing"] = True
    else:
        doc["control"] = []
    with pytest.raises(ValueError):
        stats.compare(doc)


@pytest.mark.parametrize("confidence", [.79, 1, -1, True, float("inf")])
def test_invalid_confidence(confidence):
    doc = comparison()
    doc["confidence"] = confidence
    with pytest.raises(ValueError):
        stats.compare(doc)


def test_large_valid_values_keep_finite_welch_results():
    result = stats.compare(comparison(a=(1e99, 2e99, 3e99), b=(4e99, 5e99, 6e99)))
    assert all(math.isfinite(value) for value in result["interval"])
    assert result["df"] == pytest.approx(4)


def policy():
    return {"schema": "raven.acceptance/v1", "cases": [{"id": "experiment-a", "expected": {
        "energy": {"value": 100, "absolute": .1, "relative": .01},
        "residual": {"value": 0, "absolute": .001, "relative": .1}}}]}


def results(energy=101, residual=.001):
    return {"schema": "raven.results/v1", "runs": [{"id": "experiment-a", "metrics": {"energy": energy, "residual": residual}}]}


def test_acceptance_boundary_inclusive_and_absolute_for_zero():
    result = stats.acceptance(policy(), results())
    assert result["passed"] == result["total"] == 2
    assert [row["allowed_error"] for row in result["matrix"]] == [1, .001]


@pytest.mark.parametrize("value", [102, None, True, "100", float("nan"), float("inf"), -float("inf")])
def test_missing_or_invalid_cannot_pass_or_leak_nonfinite_json(value):
    import json
    result = stats.acceptance(policy(), results(energy=value))
    assert result["passed"] == 1
    json.dumps(result, allow_nan=False)


def test_missing_case_and_metric_are_explicit_failures():
    assert stats.acceptance(policy(), {"schema": "raven.results/v1", "runs": []})["passed"] == 0
    doc = results()
    del doc["runs"][0]["metrics"]["energy"]
    assert stats.acceptance(policy(), doc)["passed"] == 1


@pytest.mark.parametrize("key,value", [("value", float("nan")), ("absolute", -.1), ("relative", -.1), ("absolute", True)])
def test_invalid_policy_is_an_error_not_a_passing_tolerance(key, value):
    doc = policy()
    doc["cases"][0]["expected"]["energy"][key] = value
    with pytest.raises(ValueError):
        stats.acceptance(doc, results())


@pytest.mark.parametrize("target", ["cases", "runs"])
def test_duplicate_case_or_result_cannot_shadow_previous_failure(target):
    spec, observed = policy(), results()
    doc = spec if target == "cases" else observed
    doc[target].append(copy.deepcopy(doc[target][0]))
    with pytest.raises(ValueError):
        stats.acceptance(spec, observed)


def test_empty_policy_cannot_pass_vacuously():
    with pytest.raises(ValueError):
        stats.acceptance({"schema": "raven.acceptance/v1", "cases": []}, results())
