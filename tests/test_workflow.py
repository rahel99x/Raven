"""Offline workflow schedules stay bounded, conditional, and review-only."""
from __future__ import annotations

import copy
import json
import math
import random
from pathlib import Path

import pytest

from raven import workflow


def node(jid, runtime=10, parents=(), **extra):
    result = {"id": jid, "depends_on": list(parents), "resources": {"cpus_per_task": 2, "gpus": 0}}
    if runtime is not None:
        result["runtime_seconds"] = runtime
    result.update(extra)
    return result


def recipe(*nodes, **extra):
    return {"kind": "raven.workflow", "version": 1, "nodes": list(nodes), **extra}


def by_id(result):
    return {item["id"]: item for item in result["nodes"]}


def test_diamond_schedule_slack_paths_and_half_open_peak():
    result = workflow.analyze(recipe(node("prepare", 10), node("fast", 20, ["prepare"]),
                                     node("slow", 40, ["prepare"]), node("collect", 5, ["fast", "slow"])), now=1000)
    nodes = by_id(result)
    assert result["status"] == "ok"
    assert result["makespan"] == {"lower": 55, "estimate": 55, "upper": 55}
    assert result["completion_at"]["estimate"] == 1055
    assert nodes["fast"]["earliest_start"]["estimate"] == 10
    assert nodes["collect"]["earliest_start"]["estimate"] == 50
    assert nodes["fast"]["latest_start"]["estimate"] == 30
    assert nodes["fast"]["slack"]["estimate"] == 20
    assert result["critical_path"] == ["prepare", "slow", "collect"]
    assert result["critical_nodes"] == ["prepare", "slow", "collect"]
    assert result["critical_edges"] == [["prepare", "slow"], ["slow", "collect"]]
    assert result["layers"] == [["prepare"], ["fast", "slow"], ["collect"]]
    assert result["parallel_frontier"] == ["prepare"]
    envelope = result["resource_envelope"]
    assert envelope["peak_cpus"] == 4
    assert envelope["peak_concurrent_jobs"] == 2
    assert envelope["core_hours"]["estimate"] == pytest.approx(150 / 3600)
    assert envelope["gpu_hours"]["estimate"] == 0
    assert envelope["peak_requested_nodes"] == 2
    assert envelope["complete"] is True
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert any("unlimited" in assumption for assumption in result["assumptions"])
    assert any("review-only" in assumption for assumption in result["assumptions"])


def test_duration_intervals_follow_different_critical_paths():
    data = recipe(node("a", duration={"lower": 2, "estimate": 10, "upper": 20}),
                  node("b", duration={"lower": 3, "estimate": 8, "upper": 25}), node("c", 5, ["a", "b"]))
    for item in data["nodes"][:2]:
        item.pop("runtime_seconds")
    result = workflow.analyze(data, now=0)
    assert result["makespan"] == {"lower": 8, "estimate": 15, "upper": 30}
    assert result["critical_path"] == ["a", "c"]
    nodes = by_id(result)
    assert nodes["a"]["slack"] == {"lower": 1, "estimate": 0, "upper": 5}
    assert nodes["b"]["slack"] == {"lower": 0, "estimate": 2, "upper": 0}
    assert nodes["c"]["earliest_start"] == {"lower": 3, "estimate": 10, "upper": 25}
    assert result["resource_envelope"]["core_hours"] == pytest.approx({"lower": 20 / 3600, "estimate": 46 / 3600, "upper": 100 / 3600})


def test_tied_critical_paths_include_both_branches_but_select_one_deterministically():
    result = workflow.analyze(recipe(node("root", 1), node("left", 2, ["root"]),
                                     node("right", 2, ["root"]), node("tail", 3, ["left", "right"])), now=0)
    assert result["critical_path"] == ["root", "left", "tail"]
    assert set(result["critical_nodes"]) == {"root", "left", "right", "tail"}
    assert len(result["critical_edges"]) == 4


def test_unknown_durations_propagate_only_downstream_and_disable_global_slack():
    result = workflow.analyze(recipe(node("unknown", None), node("downstream", 8, ["unknown"]), node("known", 10)), now=0)
    nodes = by_id(result)
    assert result["status"] == "incomplete"
    assert nodes["unknown"]["earliest_start"]["estimate"] == 0
    assert nodes["unknown"]["earliest_finish"]["estimate"] is None
    assert nodes["downstream"]["earliest_start"]["estimate"] is None
    assert nodes["known"]["earliest_finish"]["estimate"] == 10
    assert all(value is None for value in result["makespan"].values())
    assert all(item["slack"]["estimate"] is None for item in result["nodes"])
    assert result["critical_nodes"] == result["critical_path"] == []
    assert result["resource_envelope"]["peak_cpus"] is None
    assert result["resource_envelope"]["known_peak_cpus_lower_bound"] == 2
    assert result["resource_envelope"]["gpu_hours"]["estimate"] == 0


def test_explicit_resource_counts_multi_node_and_default_single_task():
    data = recipe(node("one", 3600, resources={"cpus_per_task": 8, "ntasks": 4, "nodes": 2, "gpus": 3}),
                  node("two", 1800, resources={"cpus_per_task": 2, "ntasks_per_node": 3, "nodes": 4, "gpus": 1}),
                  node("three", 10, ["one", "two"], resources={"cpus_per_task": 5}))
    result = workflow.analyze(data, now=0)
    counts = by_id(result)
    assert counts["one"]["resource_counts"] == {"cpus": 32, "gpus": 3, "nodes": 2}
    assert counts["two"]["resource_counts"] == {"cpus": 24, "gpus": 1, "nodes": 4}
    assert counts["three"]["resource_counts"] == {"cpus": 5, "gpus": 0, "nodes": 1}
    assert result["resource_envelope"]["core_hours"]["estimate"] == pytest.approx(32 + 12 + 50 / 3600)
    assert result["resource_envelope"]["gpu_hours"]["estimate"] == pytest.approx(3.5)
    assert result["resource_envelope"]["peak_cpus"] == 56
    assert result["resource_envelope"]["peak_gpus"] == 4
    assert result["resource_envelope"]["peak_requested_nodes"] == 6


def test_unknown_cpu_requests_and_multi_node_task_counts_are_not_guessed():
    result = workflow.analyze(recipe(node("unknown", resources={}), node("multi", resources={"cpus_per_task": 8, "nodes": 2})), now=0)
    assert result["resource_envelope"]["core_hours"]["estimate"] is None
    assert result["resource_envelope"]["peak_cpus"] is None
    assert result["resource_envelope"]["peak_concurrent_jobs"] == 2
    assert result["resource_envelope"]["complete"] is False
    assert len([warning for warning in result["warnings"] if "CPU allocation" in warning]) == 2


@pytest.mark.parametrize("remaining,feasible,estimate_feasible", [(5, False, False), (10, None, True), (25, True, True), (0, False, False)])
def test_conditional_deadline_slack(remaining, feasible, estimate_feasible):
    item = node("task", duration={"lower": 8, "estimate": 10, "upper": 20})
    item.pop("runtime_seconds")
    result = workflow.analyze(recipe(item, deadline_seconds=remaining), now=100)
    deadline = result["deadline"]
    assert deadline["at"] == 100 + remaining
    assert deadline["feasible"] is feasible
    assert deadline["estimate_feasible"] is estimate_feasible
    assert deadline["slack"]["estimate"] == remaining - 10
    assert "capacity" in deadline["basis"]


def test_absolute_past_deadline_unknown_deadline_and_injected_clock(monkeypatch):
    monkeypatch.setattr(workflow.clock, "now", lambda: 100)
    result = workflow.analyze(recipe(node("a"), deadline=90))
    assert result["now"] == 100
    assert result["deadline"]["feasible"] is False
    unknown = workflow.analyze(recipe(node("a", None), deadline=90), now=100)
    assert unknown["deadline"]["feasible"] is None
    assert unknown["deadline"]["estimate_feasible"] is None


@pytest.mark.parametrize("duration", [0, -1, True, "10", None, float("nan"), float("inf"), -float("inf"), 10**500, 1e13])
def test_runtime_estimate_is_strict_finite_positive_and_bounded(duration):
    data = recipe({"id": "a", "runtime_seconds": duration})
    with pytest.raises(ValueError, match="runtime_seconds"):
        workflow.analyze(data, now=0)


@pytest.mark.parametrize("duration", [{}, {"estimate": 10}, {"lower": 20, "estimate": 10, "upper": 30},
                                     {"lower": 1, "estimate": 10, "upper": 5}, {"lower": 0, "estimate": 1, "upper": 2},
                                     {"lower": 1, "estimate": float("nan"), "upper": 2},
                                     {"lower": 1, "estimate": 2, "upper": 3, "extra": 4}, "1,2,3"])
def test_malformed_duration_intervals(duration):
    with pytest.raises(ValueError, match="duration"):
        workflow.analyze(recipe({"id": "a", "duration": duration}), now=0)


def test_duration_forms_are_mutually_exclusive_and_input_is_never_mutated():
    data = recipe(node("a", 5))
    original = copy.deepcopy(data)
    workflow.analyze(data, now=0)
    assert data == original
    data["nodes"][0]["duration"] = {"lower": 1, "estimate": 2, "upper": 3}
    with pytest.raises(ValueError, match="choose runtime_seconds"):
        workflow.analyze(data, now=0)


@pytest.mark.parametrize("jid", [None, 1, "", "white space", "a:b", "x\n", "x\x1b[0m", "../x", "x" * 65, "用户", "a\u200b"])
def test_invalid_ids(jid):
    with pytest.raises(ValueError, match="id"):
        workflow.analyze(recipe(node(jid)), now=0)


@pytest.mark.parametrize("data,message", [(recipe(node("a"), node("a")), "duplicate"),
                                         (recipe(node("a", parents=["missing"])), "unknown"),
                                         (recipe(node("a", parents=["a"])), "cycle"),
                                         (recipe(node("a", parents=["b"]), node("b", parents=["a"])), "cycle"),
                                         (recipe(node("a", parents=["b", "b"]), node("b")), "duplicate dependencies"),
                                         (recipe(node("a", parents=["afterok:123"])), "external"),
                                         (recipe({"id": "a", "depends_on": "b"}), "list"),
                                         (recipe({"id": "a", "depends_on": [None]}), "node IDs")])
def test_graph_errors_are_actionable(data, message):
    with pytest.raises(ValueError, match=message):
        workflow.analyze(data, now=0)


@pytest.mark.parametrize("data", [None, [], {}, {"version": 1, "kind": "raven.workflow", "nodes": []},
                                {"version": True, "kind": "raven.workflow", "nodes": [node("a")]},
                                {"version": 1.0, "kind": "raven.workflow", "nodes": [node("a")]},
                                {"version": 1, "kind": "unknown", "nodes": [node("a")]},
                                recipe(node("a"), unknown=True), recipe({"id": "a", "execute": "touch bad"}),
                                recipe(node("a"), deadline=10, deadline_seconds=10)])
def test_strict_recipe_schema(data):
    with pytest.raises(ValueError):
        workflow.analyze(data, now=0)


@pytest.mark.parametrize("field", ["label", "script"])
@pytest.mark.parametrize("bad", ["", "x\n", "\x1b[2J", "x\x00", 8, "x\u200b"])
def test_control_free_labels_and_script_paths(field, bad):
    with pytest.raises(ValueError, match=field):
        workflow.analyze(recipe(node("a", **{field: bad})), now=0)


@pytest.mark.parametrize("resources", [[], {"unapproved": 4}, {"cpus_per_task": 0}, {"nodes": -1},
                                      {"cpus_per_task": True}, {"ntasks": "4"}, {"gpus": -1}, {"gpus": 1.5},
                                      {"nodes": 2147483648}, {"mem": "NaNG"}, {"time": "10:90:00"},
                                      {"partition": "x\n"}, {"account": "\x1b"}, {"ntasks": 4, "ntasks_per_node": 2}])
def test_resource_schema_and_numeric_values(resources):
    with pytest.raises(ValueError, match="resources"):
        workflow.analyze(recipe(node("a", resources=resources)), now=0)


@pytest.mark.parametrize("parameters", [[], {"key": float("inf")}, {"key": 10**500}, {"key": "\x1b[2J"},
                                       {"key": object()}, {"key": [0] * 2049}, {"": 1}])
def test_parameters_are_bounded_printable_json(parameters):
    with pytest.raises(ValueError, match="parameters"):
        workflow.analyze(recipe(node("a", parameters=parameters)), now=0)


def test_parameter_cycles_and_depth_reject_before_deepcopy():
    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="level"):
        workflow.analyze(recipe(node("a", parameters=cyclic)), now=0)


@pytest.mark.parametrize("now", [True, -1, "100", float("nan"), float("inf"), 10**500])
def test_invalid_clock(now):
    with pytest.raises(ValueError, match="now"):
        workflow.analyze(recipe(node("a")), now=now)


def test_large_chain_uses_iterative_graph_and_critical_path():
    data = recipe(*(node(f"n{i}", 1, [f"n{i-1}"] if i else []) for i in range(512)))
    result = workflow.analyze(data, now=0)
    assert result["makespan"]["estimate"] == 512
    assert len(result["critical_path"]) == len(result["critical_nodes"]) == 512
    assert len(result["layers"]) == 512
    assert result["resource_envelope"]["peak_concurrent_jobs"] == 1
    with pytest.raises(ValueError, match="512"):
        workflow.analyze(recipe(*(node(f"n{i}") for i in range(513))), now=0)


def test_edge_budget_rejects_dense_graph_before_scheduler_work():
    data = recipe(*(node(f"n{i}", 1, [f"n{j}" for j in range(i)]) for i in range(100)))
    with pytest.raises(ValueError, match="4096-edge"):
        workflow.analyze(data, now=0)


def test_cycle_error_includes_nodes_blocked_downstream():
    data = recipe(node("a", parents=["b"]), node("b", parents=["a"]), node("blocked", parents=["a"]), node("free"))
    with pytest.raises(ValueError, match="a, b, blocked"):
        workflow.analyze(data, now=0)


def test_random_dag_matches_bruteforce_paths_and_resource_sweep():
    rng = random.Random(9217)
    for _ in range(120):
        count = rng.randint(1, 9)
        data = recipe(*(node(f"n{i}", rng.randint(1, 20), [f"n{j}" for j in range(i) if rng.random() < .3],
                                  resources={"cpus_per_task": rng.randint(1, 8), "gpus": rng.randint(0, 3)}) for i in range(count)))
        raw = {item["id"]: item for item in data["nodes"]}
        def paths_to(jid):
            item = raw[jid]
            if not item["depends_on"]:
                return [[jid]]
            return [path + [jid] for parent in item["depends_on"] for path in paths_to(parent)]
        all_paths = [path for jid in raw for path in paths_to(jid)]
        longest = max(sum(raw[jid]["runtime_seconds"] for jid in path) for path in all_paths)
        result = workflow.analyze(data, now=0)
        assert result["makespan"]["estimate"] == longest
        assert sum(raw[jid]["runtime_seconds"] for jid in result["critical_path"]) == longest
        critical_nodes = {jid for path in all_paths if sum(raw[x]["runtime_seconds"] for x in path) == longest for jid in path}
        assert set(result["critical_nodes"]) == critical_nodes
        nodes = by_id(result)
        boundaries = sorted({value for item in nodes.values() for value in (item["earliest_start"]["estimate"], item["earliest_finish"]["estimate"])})
        for key, resource in (("peak_cpus", "cpus"), ("peak_gpus", "gpus")):
            expected = max(sum(item["resource_counts"][resource] for item in nodes.values()
                               if item["earliest_start"]["estimate"] <= t < item["earliest_finish"]["estimate"]) for t in boundaries)
            assert result["resource_envelope"][key] == expected


@pytest.fixture(autouse=True)
def remove_sbatch_env(monkeypatch):
    import os
    for key in os.environ:
        if key.startswith("SBATCH_"):
            monkeypatch.delenv(key)


def test_plans_are_exact_review_only_and_symbolic_not_scheduler_dependencies(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("preparing workflow executed a subprocess"))
    script = tmp_path / "some script's name.sh"
    marker = tmp_path / "NEVER_EXECUTE"
    script.write_text(f"#!/bin/bash\ntouch '{marker}'\n")
    data = recipe(node("child", 5, ["parent"], script=script.name, parameters={"rate": .1}),
                  node("parent", 10, script=script.name, resources={"cpus_per_task": 4, "mem": "8G", "gpus": 0}))
    plans = workflow.plans(data, workdir=tmp_path)
    assert [plan["workflow_node_id"] for plan in plans] == ["parent", "child"]
    assert all(plan["valid"] for plan in plans)
    assert all(plan["workflow_orchestration"] == "review_only" for plan in plans)
    assert plans[1]["symbolic_dependencies"] == ["parent"]
    assert plans[1]["requires_workflow_orchestration"] is True
    assert not any("dependency" in argument for plan in plans for argument in plan["argv"])
    assert plans[0]["resources"]["cpus_per_task"] == "4"
    assert plans[1]["parameters"] == {"rate": .1}
    assert not marker.exists()
    from raven.submission import _digest
    assert all(plan["plan_id"] == _digest(plan) for plan in plans)
    assert json.loads(json.dumps(plans, allow_nan=False)) == plans


@pytest.mark.parametrize("data,message", [(recipe(node("a", parents=["missing"], script="missing.sh")), "unknown"),
                                         (recipe(node("a", parents=["a"], script="missing.sh")), "cycle"),
                                         (recipe(node("a", script="missing.sh"), node("b")), "script declarations")])
def test_invalid_graph_or_missing_script_fails_before_any_preparation(data, message, monkeypatch):
    monkeypatch.setattr("raven.submission.prepare", lambda *args, **kwargs: pytest.fail("read scripts before full graph validation"))
    with pytest.raises(ValueError, match=message):
        workflow.plans(data)


def test_any_script_preflight_error_prevents_partial_plans(tmp_path):
    script = tmp_path / "valid.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    data = recipe(node("valid", script=script.name), node("invalid", script="missing.sh"))
    with pytest.raises(ValueError, match="no plans returned.*invalid"):
        workflow.plans(data, workdir=tmp_path)


def test_existing_script_dependencies_cannot_masquerade_as_symbolic_workflow(tmp_path):
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\n#SBATCH --dependency=afterok:123\ntrue\n")
    with pytest.raises(ValueError, match="scheduler dependency"):
        workflow.plans(recipe(node("a", script=script.name)), workdir=tmp_path)


def test_explicit_zero_gpu_recipe_rejects_script_gpu_request(tmp_path):
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\n#SBATCH --gpus=1\ntrue\n")
    with pytest.raises(ValueError, match="zero GPUs"):
        workflow.plans(recipe(node("a", script=script.name)), workdir=tmp_path)


@pytest.mark.parametrize("directives,resources,message", [
    ("#SBATCH --gpus=1\n", {"cpus_per_task": 2}, "zero GPUs"),
    ("#SBATCH --nodes=2\n", {"cpus_per_task": 2}, "node allocation"),
    ("#SBATCH --ntasks=8\n", {"cpus_per_task": 2}, "CPU/task allocation"),
    ("#SBATCH --ntasks-per-node=3\n", {"cpus_per_task": 2}, "CPU/task allocation"),
])
def test_script_requests_cannot_silently_contradict_recipe_envelope(tmp_path, directives, resources, message):
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\n" + directives + "true\n")
    with pytest.raises(ValueError, match=message):
        workflow.plans(recipe(node("a", script=script.name, resources=resources)), workdir=tmp_path)


def test_repeated_identical_script_preflight_is_cached_without_shared_plan_mutation(tmp_path, monkeypatch):
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    from raven import submission
    real_prepare = submission.prepare
    calls = []
    def counted_prepare(*args, **kwargs):
        calls.append(args)
        return real_prepare(*args, **kwargs)
    monkeypatch.setattr(submission, "prepare", counted_prepare)
    data = recipe(node("a", script=script.name), node("b", parents=["a"], script=script.name),
                  node("c", parents=["a"], script=script.name, resources={"cpus_per_task": 4}))
    plans = workflow.plans(data, workdir=tmp_path)
    assert len(calls) == 2
    assert [plan["workflow_node_id"] for plan in plans] == ["a", "b", "c"]
    assert plans[0]["symbolic_dependencies"] == []
    assert plans[1]["symbolic_dependencies"] == ["a"]
    plans[0]["resources"]["cpus_per_task"] = "999"
    assert plans[1]["resources"]["cpus_per_task"] == "2"


@pytest.mark.parametrize("workdir", [False, [], "x\n", "\x1b[2J"])
def test_plan_workdir_paths_are_strict_before_preflight(workdir, monkeypatch):
    monkeypatch.setattr("raven.submission.prepare", lambda *args, **kwargs: pytest.fail("invalid workdir entered preflight"))
    with pytest.raises(ValueError, match="workdir"):
        workflow.plans(recipe(node("a", script="job.sh")), workdir=workdir)


def test_loaded_recipe_remains_reusable_and_unknown_durations_stay_unknown(tmp_path):
    source = tmp_path / "workflow.json"
    data = recipe(node("a", None), node("b", 3, ["a"]))
    source.write_text(json.dumps(data))
    loaded = workflow.load(source)
    assert "duration" not in loaded["nodes"][0]
    assert workflow.analyze(loaded, now=0)["status"] == "incomplete"


def test_loaded_cycles_and_duplicate_keys_are_rejected(tmp_path):
    source = tmp_path / "workflow.json"
    source.write_text(json.dumps(recipe(node("a", parents=["a"]))))
    with pytest.raises(ValueError, match="cycle"):
        workflow.load(source)
    source.write_text('{"version":1,"version":2,"kind":"raven.workflow","nodes":[]}')
    with pytest.raises(ValueError):
        workflow.load(source)
