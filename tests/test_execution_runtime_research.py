import copy
import json
import math
import threading

import pytest

from tower import diagnostic_recipes as recipes
from tower import execution_simulator as simulator
from tower import science_fields


def model():
    return {"schema": "tower.execution-model/v1", "workload": "sha256:fixed-input",
            "resources": {"cpu0": {"kind": "cpu", "rate": 10}, "cpu1": {"kind": "cpu", "rate": 10},
                          "link": {"kind": "network", "rate": 5, "latency": 1}},
            "placement": {"r0": "cpu0", "r1": "cpu1"}, "routes": {"cpu0->cpu1": "link", "cpu1->cpu0": "link"},
            "domain": {"minimum_rate_ratio": .5, "maximum_rate_ratio": 2, "fixed_workload": True},
            "events": [{"id": "a", "kind": "cpu", "work": 100, "rank": "r0", "depends": []},
                       {"id": "b", "kind": "cpu", "work": 100, "rank": "r1", "depends": []},
                       {"id": "c", "kind": "network", "work": 10, "source": "r0", "destination": "r1", "depends": ["a", "b"]}]}


def test_resource_dag_replays_parallel_work_and_dependency():
    result = simulator.simulate(model())
    assert result["baseline_seconds"] == 13
    assert result["timeline"][-1]["start"] == 10
    assert not result["calibrated"] and not result["validated"]


def test_resource_contention_and_supported_placement():
    data = model()
    data["routes"]["cpu0->cpu0"] = "link"
    result = simulator.simulate(data, {"placement": {"r1": "cpu0"}})
    assert result["predicted_seconds"] == 23
    assert result["timeline"][1]["dependency_ready"] == 0
    assert result["timeline"][1]["start"] == 10


def test_calibration_and_heldout_are_distinct():
    data = model()
    data["observations"] = [{"id": "fit", "role": "calibration", "scenario": {}, "actual_seconds": 26},
                            {"id": "holdout", "role": "validation", "scenario": {"rates": {"link": 10}}, "actual_seconds": 25}]
    result = simulator.simulate(data, {"rates": {"link": 10}})
    assert result["calibration_factor"] == 2
    assert result["predicted_seconds"] == 24
    assert result["residuals"][1]["residual_seconds"] == 1
    assert result["validation_max_relative_error"] == pytest.approx(.04)
    assert result["validated"] is False
    assert result["validation"]["status"] == "unqualified"


@pytest.mark.parametrize("threshold,actual,state,prediction", [(.1, 25, "passed", "available"),
    (.01, 25, "failed", "withdrawn"), (0, 24, "passed", "available"), (0, 24.01, "failed", "withdrawn")])
def test_held_out_acceptance_controls_validation_and_withdrawal(threshold, actual, state, prediction):
    data = model()
    data["domain"]["max_validation_relative_error"] = threshold
    data["observations"] = [{"id": "fit", "role": "calibration", "scenario": {}, "actual_seconds": 26},
                            {"id": "holdout", "role": "validation", "scenario": {"rates": {"link": 10}}, "actual_seconds": actual}]
    result = simulator.simulate(data, {"rates": {"link": 10}})
    assert result["validation"]["status"] == state
    assert result["validated"] is (state == "passed")
    assert result["prediction_status"] == prediction
    assert (result["predicted_seconds"] is None) is (state == "failed")
    assert result["diagnostic_model_seconds"] == 24
    assert result["applicability"]["direct_validation_observations"] == ["holdout"]


def test_fit_only_cannot_validate_itself_and_unseen_candidate_is_explicit():
    data = model()
    data["domain"]["max_validation_relative_error"] = .1
    data["observations"] = [{"id": "fit", "role": "calibration", "scenario": {}, "actual_seconds": 26}]
    result = simulator.simulate(data)
    assert result["validation"]["status"] == "unqualified"
    assert result["validation"]["held_out_count"] == 0
    assert not result["validated"]
    data["observations"].append({"id": "holdout", "role": "validation", "scenario": {}, "actual_seconds": 26})
    unseen = simulator.simulate(data, {"rates": {"link": 10}})
    assert unseen["validated"]
    assert unseen["applicability"]["direct_validation_observations"] == []


def test_new_failure_withdraws_same_prediction_and_retains_residuals():
    data = model()
    data["domain"]["max_validation_relative_error"] = .1
    data["observations"] = [{"id": "initial", "role": "validation", "scenario": {}, "actual_seconds": 13}]
    before = simulator.simulate(data)
    data["observations"].append({"id": "later-failure", "role": "validation", "scenario": {"rates": {"link": 10}}, "actual_seconds": 100})
    after = simulator.simulate(data)
    assert before["prediction_id"] == after["prediction_id"]
    assert before["validation_revision"] != after["validation_revision"]
    assert after["validation"]["failed_observations"] == ["later-failure"]
    assert after["prediction_status"] == "withdrawn" and after["predicted_seconds"] is None
    assert len(after["residuals"]) == 2
    assert before["prediction_status"] == "available"


@pytest.mark.parametrize("threshold", [True, None, -.1, 1.1, float("nan"), "0.1"])
def test_invalid_validation_policy_rejected(threshold):
    data = model()
    data["domain"]["max_validation_relative_error"] = threshold
    with pytest.raises(ValueError): simulator.simulate(data)


@pytest.mark.parametrize("scenario", [{"rates": {"link": 100}}, {"rates": {"absent": 1}}, {"rates": {"cpu0": 0}},
                                       {"placement": {"r9": "cpu0"}}, {"rank_count": 5}, {"rates": {"cpu0": True}},
                                       {"placement": {"r0": "link"}}])
def test_simulator_refuses_out_of_domain(scenario):
    with pytest.raises(ValueError):
        simulator.simulate(model(), scenario)


@pytest.mark.parametrize("mutation", [lambda d: d["events"][0].update(depends=["c"]),
    lambda d: d["events"][0].update(depends=["absent"]), lambda d: d["events"].append(d["events"][0]),
    lambda d: d["events"][0].update(work=float("nan")), lambda d: d["events"][0].update(resource="cpu0"),
    lambda d: d["domain"].update(fixed_workload=False)])
def test_simulator_rejects_ambiguous_or_invalid_models(mutation):
    data = model()
    mutation(data)
    with pytest.raises(ValueError):
        simulator.simulate(data)


def test_simulator_cancelled():
    stop = threading.Event()
    stop.set()
    with pytest.raises(ValueError, match="cancelled"):
        simulator.simulate(model(), cancel=stop)


def cost(cpu=.1, memory=100, disk=100, seconds=1):
    return {"cpu_fraction": cpu, "memory_bytes": memory, "disk_bytes": disk, "seconds": seconds}


def intent_site():
    intent = {"schema": "tower.diagnostic-intent/v1", "question": "What delayed this phase?", "required": ["mpi", "gpu"], "budget": cost(1, 1000, 1000, 60)}
    site = {"schema": "tower.site-capabilities/v1", "identity": "siteA", "runtime": {"mpi": "openmpi-5"},
            "adapters": [{"id": "mpi-tool", "provides": ["mpi"], "requires": {"mpi": "openmpi-5"}, "conflicts": [], "cost": cost(), "permitted": True, "argv": ["collector", "mpi"]},
                         {"id": "gpu-tool", "provides": ["gpu"], "requires": {}, "conflicts": [], "cost": cost(), "permitted": True, "argv": ["collector", "gpu"]}]}
    return intent, site


def test_recipe_exact_plan_and_no_execution():
    intent, site = intent_site()
    result = recipes.compile_recipe(intent, site)
    assert {item["id"] for item in result["adapters"]} == {"mpi-tool", "gpu-tool"}
    assert all(row["exact"] for row in result["coverage"].values())
    assert result["executable"] is False


@pytest.mark.parametrize("mode", ["missing", "policy", "conflict", "runtime", "budget"])
def test_recipe_mandatory_failure(mode):
    intent, site = intent_site()
    if mode == "missing": site["adapters"].pop()
    if mode == "policy": site["adapters"][1]["permitted"] = False
    if mode == "conflict": site["adapters"][0]["conflicts"] = ["gpu-tool"]
    if mode == "runtime": site["runtime"]["mpi"] = "mpich"
    if mode == "budget": intent["budget"]["cpu_fraction"] = .01
    with pytest.raises(ValueError, match="No permitted"):
        recipes.compile_recipe(intent, site)


def test_only_explicit_approximation_and_exact_preferred():
    intent, site = intent_site()
    fallback = copy.deepcopy(site["adapters"][1])
    fallback.update(id="cheap", provides=["gpu-sampled"], cost=cost(0, 0, 0, 0))
    site["adapters"].append(fallback)
    intent["approximations"] = {"gpu": ["gpu-sampled"]}
    result = recipes.compile_recipe(intent, site)
    assert result["coverage"]["gpu"]["exact"]
    site["adapters"][1]["permitted"] = False
    result = recipes.compile_recipe(intent, site)
    assert result["coverage"]["gpu"] == {"observed": "gpu-sampled", "exact": False}
    intent.pop("approximations")
    with pytest.raises(ValueError): recipes.compile_recipe(intent, site)


def test_exact_summary_retains_gaps_and_references():
    source = {"schema": "tower.query-source/v1", "identity": "run-1", "coverage": {"complete": False, "reason": "rank 3 absent"},
              "rows": [{"ref": "chunk:0", "values": {"x": 1e16}}, {"ref": "chunk:1", "values": {"x": 1}},
                       {"ref": "chunk:2", "values": {"x": -1e16}}, {"ref": "chunk:3", "values": {"x": None}}]}
    query = {"schema": "tower.summary-query/v1", "fields": ["x"], "operations": ["count", "sum", "min", "max"]}
    result = recipes.summarize_query(source, query)
    assert result["summaries"]["x"] == {"count": 3, "sum": 1, "min": -1e16, "max": 1e16}
    assert result["null_or_missing"]["x"] == 1
    assert result["raw_references"] == [f"chunk:{index}" for index in range(4)]
    assert not result["coverage"]["complete"]
    query["operations"] = ["median"]
    with pytest.raises(ValueError): recipes.summarize_query(source, query)


def field(shape, values):
    return {"schema": "tower.scientific-field/v1", "identity": "density", "shape": shape, "values": values, "units": "kg/m3"}


def test_field_extrema_and_mixed_gaps_survive_downsampling():
    result = science_fields.inspect_field(field([2, 3], [1, None, 100, 2, 3, 4]), width=1, height=1)
    assert result["tiles"][0][0]["max"] == 100
    assert result["tiles"][0][0]["gaps"] == 1
    assert result["full"]["max_flat_index"] == 2
    assert sum(result["histogram"]["counts"]) == 5
    assert science_fields.terminal_rows(result) == ["░"]


@pytest.mark.parametrize("axis,index,expected", [(0, 1, [4, 5, 6, 7]), (1, 1, [2, 3, 6, 7]), (2, 0, [0, 2, 4, 6])])
def test_row_major_volume_slices(axis, index, expected):
    result = science_fields.inspect_field(field([2, 2, 2], list(range(8))), axis=axis, index=index)
    assert [tile["mean"] for row in result["tiles"] for tile in row] == expected


def test_empty_values_are_gaps_not_zero():
    result = science_fields.inspect_field(field([2], [None, None]))
    assert result["selected"]["min"] is None
    assert sum(result["histogram"]["counts"]) == 0
    assert science_fields.terminal_rows(result, unicode=False) == ["??"]


@pytest.mark.parametrize("shape,values", [([2], [1]), ([0], []), ([True], [1]), ([1], [float("inf")]), ([513, 513], [])])
def test_field_invalid_bounds(shape, values):
    with pytest.raises(ValueError): science_fields.inspect_field(field(shape, values))
