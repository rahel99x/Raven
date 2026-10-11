"""Scientific false-confidence, bounded planning and incomplete provenance cases."""
import copy

import pytest

from tower import investigation_planner as planner
from tower import science_contracts as contracts
from tower import science_diagnostics as diagnostics
from tower import scientific_lineage as lineage
from tower.operations import digest


def evidence(name, data, complete=True):
    return {"id": name, "data": data, "sha256": digest(data), "complete": complete}


def recommendation():
    source = evidence("baseline", {"utilization": 20})
    identity = {"cluster": "test", "job_id": "12", "attempt": "first", "inputs_sha256": "a"*64}
    contract = {"id": "fewer-ranks", "identity": identity, "created": 100, "expires": 200,
                "evidence": [{"id": source["id"], "sha256": source["sha256"]}],
                "preconditions": [{"source": "baseline", "path": ["utilization"], "op": "lt", "value": 30}],
                "acceptance": [{"source": "experiment", "path": ["error"], "op": "le", "value": .001},
                               {"source": "experiment", "path": ["runtime"], "op": "lt", "value": 10}],
                "intervention": "Prepare a matched fewer-ranks experiment"}
    return {"schema": "tower.recommendation-review/v1", "contract": contract,
            "snapshot": {"identity": copy.deepcopy(identity), "evidence": [source]}}


def outcome(doc, error=0, runtime=8):
    doc["outcome"] = {"contract_sha256": digest(doc["contract"]), "experiment_id": "trial1",
                      "identity": {**doc["contract"]["identity"], "job_id": "13"},
                      "evidence": [evidence("experiment", {"error": error, "runtime": runtime})]}
    return doc


def test_contract_requires_experiment_science_and_performance_evidence():
    doc = recommendation()
    assert contracts.evaluate(doc, now=150)["state"] == "hypothesis"
    assert contracts.evaluate(outcome(doc), now=150)["state"] == "supported"
    assert contracts.evaluate(outcome(doc, error=.1), now=150)["state"] == "refuted"
    assert contracts.evaluate(outcome(doc, runtime=12), now=150)["state"] == "refuted"
    doc["outcome"]["evidence"] = []
    assert contracts.evaluate(doc, now=150)["state"] == "inconclusive"


@pytest.mark.parametrize("clock", [0, 98, 200, 201])
def test_expired_or_future_evidence_never_endorsed(clock):
    assert contracts.evaluate(outcome(recommendation()), now=clock)["state"] == "expired"


@pytest.mark.parametrize("change", ["attempt", "job_id", "inputs_sha256"])
def test_exact_identity_prevents_stale_endorsement(change):
    doc = outcome(recommendation())
    doc["snapshot"]["identity"][change] = "b"*64 if change.endswith("sha256") else "different"
    assert contracts.evaluate(doc, now=150)["state"] == "stale"


def test_tampered_or_missing_or_changed_evidence_are_distinct():
    doc = recommendation()
    doc["snapshot"]["evidence"][0]["data"]["utilization"] = 1
    with pytest.raises(ValueError, match="digest"):
        contracts.evaluate(doc, now=150)
    doc["snapshot"]["evidence"] = [evidence("baseline", {"utilization": 1})]
    assert contracts.evaluate(doc, now=150)["state"] == "stale"
    doc["snapshot"]["evidence"] = []
    assert contracts.evaluate(doc, now=150)["state"] == "inconclusive"


@pytest.mark.parametrize("value", [None, True, "20", float("inf"), float("nan"), {}, []])
def test_missing_nonnumeric_queries_cannot_pass(value):
    index = {"e": {"data": {"x": value}, "complete": True}}
    assert contracts.query({"source": "e", "path": ["x"], "op": "lt", "value": 30}, index)["state"] == "unknown"


def test_boolean_equality_cannot_use_integer_as_evidence():
    q = {"source": "e", "path": ["x"], "op": "eq", "value": True}
    assert contracts.query(q, {"e": {"data": {"x": 1}, "complete": True}})["state"] == "fail"


@pytest.mark.parametrize("change", ["digest", "context"])
def test_experiment_belongs_to_this_contract(change):
    doc = outcome(recommendation())
    if change == "digest":
        doc["outcome"]["contract_sha256"] = "f"*64
    else:
        doc["outcome"]["identity"]["inputs_sha256"] = "f"*64
    with pytest.raises(ValueError):
        contracts.evaluate(doc, now=150)


def action(name, kind="repeat", **extra):
    return {"id": name, "kind": kind, "seconds": 5, "resource_seconds": 10,
            "queue_seconds": 0, "decision_gain": .5, "success_probability": 1,
            "scientific_risk": 0, "requires": [], "model": "declared pilot measurements", **extra}


def test_mixed_action_selection_respects_every_budget_and_committed_intent():
    doc = {"schema": "tower.investigation-plan/v1", "budget": {"seconds": 16, "resource_seconds": 30, "trials": 3},
           "known_evidence": ["validated"], "committed": [{"id": "old", "seconds": 5, "resource_seconds": 10}],
           "actions": [action("slow", queue_seconds=100), action("trace", "trace"), action("audit", "scientific-validation", decision_gain=.9),
                       action("risk", scientific_risk=.1), action("missing", requires=["absent"]), action("old")]}
    result = planner.plan(doc)
    assert [a["id"] for a in result["selected"]] == ["audit", "trace"]
    assert result["remaining"] == {"seconds": 1, "resource_seconds": 0, "trials": 0}
    assert {x["reason"] for x in result["excluded"]} == {"risk limit", "missing evidence", "already committed", "remaining budget"}


@pytest.mark.parametrize("field,value", [("scientific_risk", float("nan")), ("success_probability", 1.1), ("seconds", 0), ("resource_seconds", -1), ("queue_seconds", True), ("model", "")])
def test_planner_refuses_unsound_model_values(field, value):
    doc = {"schema": "tower.investigation-plan/v1", "budget": {"seconds": 100, "resource_seconds": 100, "trials": 2},
           "known_evidence": [], "actions": [action("a", **{field: value})]}
    with pytest.raises(ValueError):
        planner.plan(doc)


def graph():
    return {"schema": "tower.scientific-lineage/v1", "nodes": [
        {"id": "raw", "kind": "artifact", "dependencies": [], "complete": True},
        {"id": "parser", "kind": "policy", "dependencies": [], "complete": True},
        {"id": "parsed", "kind": "artifact", "dependencies": ["raw", "parser"], "complete": True},
        {"id": "claim", "kind": "claim", "dependencies": ["parsed"], "complete": True}],
        "changed": ["parser"], "actions": [
            {"id": "parse", "covers": ["parser", "parsed"], "requires": ["raw"], "cost": 1, "method": "correct parser and verify output"},
            {"id": "validate", "covers": ["claim"], "requires": ["parsed"], "cost": 1, "method": "check scientific observable"},
            {"id": "recompute", "covers": ["parser", "parsed", "claim"], "requires": ["raw"], "cost": 100, "method": "full recomputation"}]}


def test_impact_does_not_repeat_unaffected_solver_and_orders_checks():
    result = lineage.analyze(graph())
    assert result["affected"] == ["claim", "parsed", "parser"]
    assert [a["id"] for a in result["proposed_actions"]] == ["parse", "validate"]
    assert result["unresolved_claims"] == []
    assert result["witnesses"]["claim"] == {"root": "parser", "parent": "parsed", "distance": 2}
    assert "proposal" in result["limit"]


@pytest.mark.parametrize("incomplete", ["missing", "coverage"])
def test_unknown_lineage_cannot_be_marked_valid(incomplete):
    doc = graph()
    if incomplete == "missing":
        doc["nodes"][2]["dependencies"].append("hidden")
    else:
        doc["nodes"][2]["complete"] = False
    result = lineage.analyze(doc)
    assert "claim" in result["uncertain"] and "claim" in result["unresolved_claims"]


def test_lineage_cycles_and_unbound_actions_refused():
    doc = graph()
    doc["nodes"][0]["dependencies"] = ["claim"]
    with pytest.raises(ValueError, match="acyclic"):
        lineage.analyze(doc)


def test_lineage_does_not_propose_unneeded_work_without_affected_claims():
    doc = graph()
    doc["nodes"][-1]["kind"] = "artifact"
    assert lineage.analyze(doc)["proposed_actions"] == []
    doc = graph()
    doc["actions"][0]["covers"].append("invisible")
    with pytest.raises(ValueError, match="known"):
        lineage.analyze(doc)


def test_deep_lineage_is_iterative_and_linear_witness_storage():
    doc = {"schema": "tower.scientific-lineage/v1", "nodes": [{"id": str(i), "kind": "claim", "dependencies": [str(i-1)] if i else [], "complete": True} for i in range(2000)], "changed": ["0"], "actions": []}
    result = lineage.analyze(doc)
    assert len(result["affected"]) == 2000
    assert result["witnesses"]["1999"]["distance"] == 1999


def test_numerical_analysis_preserves_missing_and_failed_measurements():
    doc = {"schema": "tower.numerical-stability/v1", "metrics": [{"name": "mass", "reference": 0, "absolute": .1, "relative": .01}],
           "runs": [{"id": "a", "configuration": "safe", "metrics": {"mass": .01}},
                    {"id": "b", "configuration": "safe", "metrics": {}},
                    {"id": "c", "configuration": "fast", "metrics": {"mass": .2}}]}
    result = diagnostics.numerical(doc)
    assert result["state"] == "failed"
    states = {row["configuration"]: row for row in result["measurements"]}
    assert states["safe"]["state"] == "unknown" and states["safe"]["standard_deviation"] is None
    assert states["fast"]["violations"] == ["c"]


def regression():
    return {"schema": "tower.performance-investigation/v1", "unit": "independent seed", "design": "paired", "baseline": "base", "minimum_change": .5,
            "versions": [{"id": name, "scientific_valid": True,
                          "samples": [{"id": str(i), "value": v} for i, v in enumerate(values)]}
                         for name, values in [("base", [10, 10.1, 9.9, 10.2, 10]), ("slow", [20, 20.2, 19.9, 20.5, 20.1]), ("fixed", [8, 8.2, 8, 8.3, 7.9])]]}


def test_regression_detects_nonmonotonic_recovery_without_binary_search():
    result = diagnostics.regression(regression())
    assert result["first_observed_regression"] == "slow"
    assert result["nonmonotonic"] is True
    assert [row["state"] for row in result["comparisons"]] == ["regression", "improvement"]


@pytest.mark.parametrize("issue", ["zero-variance", "missing-science", "noise"])
def test_regression_does_not_invent_confidence(issue):
    doc = regression()
    if issue == "zero-variance":
        for v in doc["versions"]:
            for sample in v["samples"]:
                sample["value"] = 10 if v["id"] == "base" else 20
    elif issue == "missing-science":
        doc["versions"][0]["scientific_valid"] = None
    else:
        for sample, value in zip(doc["versions"][1]["samples"], [1, 100, 2, 70, 9]):
            sample["value"] = value
    result = diagnostics.regression(doc)
    assert result["comparisons"][0]["state"] in {"inconclusive", "scientifically-incomparable"}


@pytest.mark.parametrize("analyzer,schema", [(planner.plan, "tower.investigation-plan/v1"), (contracts.evaluate, "tower.recommendation-review/v1"), (lineage.analyze, "tower.scientific-lineage/v1"), (diagnostics.numerical, "tower.numerical-stability/v1"), (diagnostics.regression, "tower.performance-investigation/v1")])
def test_analysis_rejects_empty_and_unknown_fields(analyzer, schema):
    with pytest.raises(ValueError):
        analyzer({"schema": schema, "surprise": True})
