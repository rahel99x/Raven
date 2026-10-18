"""Hand-computable supported schedules and explicit unavailable semantics."""
import copy
import json
import threading
from types import SimpleNamespace

import pytest

from raven import slurm_simulator as S


def job(ident, **values):
    return dict(dict(id=ident, submit=0, duration=10, wall_limit=10, nodes=1, cpus_per_node=1,
                     memory_mb_per_node=100, gpus_per_node=0, priority=1, account="lab", qos="normal", depends=[]), **values)


@pytest.fixture
def model():
    return {"schema": "raven.queue-model/v1", "id": "hand-computed", "seed": 12,
            "nodes": [{"id": "n1", "cpus": 2, "memory_mb": 1000, "gpus": 1}],
            "policy": {"backfill": True, "fairshare_weight": 0, "usage_half_life_seconds": 86400,
                       "accounts": {"lab": {"weight": 1, "usage_cpu_seconds": 0}, "other": {"weight": 1, "usage_cpu_seconds": 1000}},
                       "qos": {"normal": {"max_running": 10, "max_cpus": 10, "max_wall_seconds": 1000}}},
            "jobs": [job("a", cpus_per_node=2), job("b", depends=["a"]), job("c", depends=["a"])]}


def starts(result):
    return {row["id"]: row["start"] for row in result["timeline"]}


def test_hand_computed_resources_dependencies_and_reproducibility(model):
    result = S.simulate(model)
    assert starts(result) == {"a": 0, "b": 10, "c": 10}
    assert result["makespan_seconds"] == 20
    assert result == S.simulate(copy.deepcopy(model))
    assert result["production_qualified"] is False
    # Independent sweep of every resource boundary establishes conservation.
    for when in {item[key] for item in result["timeline"] for key in ("start", "end")}:
        active = [item for item in result["timeline"] if item["start"] <= when < item["end"]]
        records = {item["id"]: item for item in model["jobs"]}
        for field, capacity in (("cpus_per_node", 2), ("memory_mb_per_node", 1000), ("gpus_per_node", 1)):
            assert sum(records[item["id"]][field] for item in active) <= capacity


def test_conservative_backfill_protects_head_wall_reservation(model):
    model["jobs"] = [job("running", priority=100, duration=20, wall_limit=20),
                     job("head", priority=90, cpus_per_node=2), job("small", priority=80, duration=5, wall_limit=5),
                     job("long", priority=70, duration=30, wall_limit=30)]
    result = S.simulate(model)
    assert starts(result) == {"running": 0, "small": 0, "head": 20, "long": 30}
    model["policy"]["backfill"] = False
    assert starts(S.simulate(model))["small"] == 30


def test_reservations_qos_and_failed_dependencies(model):
    model["reservations"] = [{"id": "reserved", "start": 0, "end": 30, "nodes": ["n1"], "accounts": ["other"]}]
    model["jobs"] = [job("a", success=False), job("b", depends=["a"]), job("c", wall_limit=2000, duration=5)]
    result = S.simulate(model)
    assert starts(result) == {"a": 30}
    assert "afterok" in result["blocked"]["b"]
    assert "QOS" in result["blocked"]["c"]
    model["reservations"] = []
    model["jobs"] = [job("a"), job("b")]
    model["policy"]["qos"]["normal"]["max_running"] = 1
    assert starts(S.simulate(model)) == {"a": 0, "b": 10}


def test_fairshare_subset_uses_declared_usage_not_invented_site_state(model):
    model["jobs"] = [job("a", cpus_per_node=2, account="other"), job("z", cpus_per_node=2)]
    assert starts(S.simulate(model))["a"] == 0
    model["policy"]["fairshare_weight"] = 100
    assert starts(S.simulate(model))["z"] == 0


def test_temporal_holdout_metrics_use_actual_separate_trace(model):
    model["traces"] = [
        {"id": "training", "role": "calibration", "origin": "synthetic", "jobs": [job("train", actual_start=0, actual_end=12)]},
        {"id": "testing", "role": "validation", "origin": "synthetic", "jobs": [
            job("test-a", submit=20, cpus_per_node=2, duration=10, wall_limit=20, actual_start=23, actual_end=35),
            job("test-b", submit=20, cpus_per_node=2, duration=10, wall_limit=20, actual_start=35, actual_end=47)]}]
    result = S.simulate(model)
    assert result["parameters"] == {"runtime_factor": 1.2, "calibration_samples": 1}
    measured, = result["validation"]
    assert measured["start_mae_seconds"] == 3
    assert measured["wait_mae_seconds"] == 3 and measured["coverage"] == 1
    assert measured["ranking_accuracy"] == 1
    assert result["hidden_state_sensitivity"]["maximum_start_shift_seconds"] >= 60
    model["traces"][1]["jobs"][0]["submit"] = 5
    with pytest.raises(ValueError, match="strictly after"):
        S.simulate(model)


def test_runtime_clipping_prevents_afterok_followup(model):
    model["traces"] = [{"id": "train", "role": "calibration", "origin": "synthetic",
                        "jobs": [job("train", duration=10, wall_limit=30, actual_start=0, actual_end=20)]}]
    result = S.simulate(model)
    assert result["timeline"][0]["runtime_clipped"]
    assert "b" in result["blocked"] and "c" in result["blocked"]


@pytest.mark.parametrize("mutation", ["cycle", "unknown", "negative", "duplicate", "unsupported", "future_leak"])
def test_invalid_or_unsupported_contract_cannot_be_guessed(model, mutation):
    if mutation == "cycle": model["jobs"][0]["depends"] = ["b"]
    elif mutation == "unknown": model["jobs"][0]["depends"] = ["hidden"]
    elif mutation == "negative": model["nodes"][0]["gpus"] = -1
    elif mutation == "duplicate": model["jobs"].append(copy.deepcopy(model["jobs"][0]))
    elif mutation == "unsupported": model["policy"]["preemption"] = "requeue"
    else:
        record = job("same", actual_start=0, actual_end=10)
        model["traces"] = [{"id": name, "role": role, "origin": "synthetic", "jobs": [record]}
                           for name, role in (("one", "calibration"), ("two", "validation"))]
    with pytest.raises(ValueError): S.simulate(model)


def test_compute_budget_and_cancellation_are_explicit(model, monkeypatch):
    event = threading.Event()
    event.set()
    with pytest.raises(ValueError, match="cancelled"): S.simulate(model, cancel=event)
    monkeypatch.setattr(S, "MAX_STEPS", 1)
    with pytest.raises(ValueError, match="budget"): S.simulate(model)


def test_operations_discovery_partial_and_simulation_are_read_only(model, tmp_path):
    from raven import operations as O
    calls = []
    def run(argv, timeout):
        calls.append(argv)
        if "config" in argv: return "ClusterName = fixture\nPriorityType = priority/basic\n", 0
        if "nodes" in argv: return "NodeName=n1 CPUTot=2 RealMemory=1000 Gres=gpu:1\n", 0
        if "partitions" in argv: return "PartitionName=cpu Nodes=n1 Default=YES\n", 0
        raise RuntimeError("Reservation visibility denied")
    ctx = O.Context(slurm=SimpleNamespace(b=SimpleNamespace(run=run)), state_dir=str(tmp_path / "state"))
    discovered = O.run("queue-model", {"action": "discover"}, ctx)
    assert discovered["data"]["observations"]["partitions"][0]["PartitionName"] == "cpu"
    assert "reservations" in discovered["data"]["errors"]
    assert len(calls) == 4 and not discovered["data"]["model_ready"]
    source = tmp_path / "model.json"
    source.write_text(json.dumps(model))
    receipt = O.run("queue-model", {"action": "simulate", "path": str(source)}, ctx)
    assert receipt["data"]["completion_coverage"] == 1 and "plan" not in receipt
    assert len(calls) == 4 and not (tmp_path / "state").exists()


def test_managed_simulation_requires_worker_and_runs_same_bounded_adapter(model, tmp_path):
    from raven import operations as O, raven_execution as R
    path = tmp_path / "model.json"
    path.write_text(json.dumps(model))
    options = {"action": "simulate", "path": str(path)}
    ctx = O.Context(cfg={"raven": {"managed": True, "mode": "light", "site": "desktop"}})
    assert O.run("queue-model", options, ctx)["status"] == "deferred"
    result = R.execute_operation("queue-model", {"params": options})
    assert result["result"]["data"]["model"] == S.MODEL
    assert result["result"]["data"]["completion_coverage"] == 1


def test_compute_task_cannot_acquire_live_discovery_transport():
    from raven import raven_execution as R
    with pytest.raises(ValueError, match="scheduler connection"):
        R.execute_operation("queue-model", {"params": {"action": "discover", "path": ""}},
                            verified_execution={"target": "desktop"})


def test_recorded_real_slurm_temporal_holdout_remains_negative_evidence():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "docs/implementation/review-2026-10/measurements/queue-temporal-holdout"
    model = json.loads((root / "model.json").read_text())
    recorded = json.loads((root / "result.json").read_text())
    result = S.simulate(model)
    assert result == recorded
    held_out, = result["validation"]
    assert held_out["origin"] == "recorded" and held_out["samples"] == 4
    assert held_out["start_mae_seconds"] == 2.75
    assert held_out["ranking_accuracy"] == .6
    assert not result["production_qualified"]
