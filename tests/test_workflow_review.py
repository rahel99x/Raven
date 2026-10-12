"""Independent path oracle, uncertainty, and terminal boundary review."""
from __future__ import annotations

from itertools import product
import copy
import json
import random
from types import SimpleNamespace

import pytest

from raven import layout, planning_views, workflow


def recipe(nodes):
    return {"kind": "raven.workflow", "version": 1, "nodes": nodes}


def paths(nodes):
    index = {node["id"]: node for node in nodes}
    children = {node["id"]: [] for node in nodes}
    for node in nodes:
        for parent in node.get("depends_on", []):
            children[parent].append(node["id"])
    result = []
    def walk(jid, path):
        current = path + [jid]
        if not children[jid]:
            result.append(current)
        for child in children[jid]:
            walk(child, current)
    for jid, node in index.items():
        if not node.get("depends_on"):
            walk(jid, [])
    return result


def test_scenario_slack_matches_independent_complete_path_oracle():
    rng = random.Random(98013)
    for _ in range(90):
        nodes = []
        for i in range(rng.randint(1, 7)):
            lower = rng.randint(1, 8)
            estimate = lower + rng.randint(0, 8)
            upper = estimate + rng.randint(0, 8)
            nodes.append({"id": f"n{i}", "depends_on": [f"n{j}" for j in range(i) if rng.random() < .35],
                          "duration": {"lower": lower, "estimate": estimate, "upper": upper},
                          "resources": {"cpus_per_task": i + 1, "gpus": i % 3}})
        all_paths = paths(nodes)
        raw = {n["id"]: n for n in nodes}
        result = workflow.analyze(recipe(nodes), now=0)
        for scenario in ("lower", "estimate", "upper"):
            durations = [(path, sum(raw[jid]["duration"][scenario] for jid in path)) for path in all_paths]
            makespan = max(total for _, total in durations)
            assert result["makespan"][scenario] == makespan
            for node in result["nodes"]:
                longest_through_node = max(total for path, total in durations if node["id"] in path)
                assert node["slack"][scenario] == makespan - longest_through_node
                assert node["latest_start"][scenario] == node["earliest_start"][scenario] + node["slack"][scenario]
        critical = {jid for path in all_paths if sum(raw[j]["duration"]["estimate"] for j in path) == result["makespan"]["estimate"] for jid in path}
        assert set(result["critical_nodes"]) == critical


def test_interval_completion_contains_all_endpoint_runtime_realizations():
    nodes = [{"id": "a", "duration": {"lower": 1, "estimate": 2, "upper": 9}},
             {"id": "b", "duration": {"lower": 3, "estimate": 7, "upper": 8}},
             {"id": "c", "depends_on": ["a"], "duration": {"lower": 4, "estimate": 5, "upper": 6}},
             {"id": "d", "depends_on": ["b", "c"], "duration": {"lower": 2, "estimate": 3, "upper": 4}}]
    result = workflow.analyze(recipe(nodes), now=100)
    all_paths = paths(nodes)
    for choices in product(("lower", "upper"), repeat=len(nodes)):
        selected = {node["id"]: node["duration"][scenario] for node, scenario in zip(nodes, choices)}
        makespan = max(sum(selected[jid] for jid in path) for path in all_paths)
        assert result["makespan"]["lower"] <= makespan <= result["makespan"]["upper"]


def test_no_unknown_duration_can_hide_behind_a_long_known_path():
    result = workflow.analyze(recipe([{"id": "known", "runtime_seconds": 1000, "resources": {"cpus_per_task": 2}},
                                      {"id": "unknown", "resources": {"cpus_per_task": 100}},
                                      {"id": "tail", "depends_on": ["unknown"], "runtime_seconds": 1}]), now=0)
    assert result["makespan"]["estimate"] is None
    assert result["critical_path"] == []
    assert result["resource_envelope"]["peak_cpus"] is None
    assert result["resource_envelope"]["known_peak_cpus_lower_bound"] == 2
    assert result["resource_envelope"]["core_hours"]["estimate"] is None
    json.dumps(result, allow_nan=False)


def test_large_duration_plus_tiny_successor_does_not_erase_its_peak():
    data = recipe([{"id": "long", "runtime_seconds": 1e12, "resources": {"cpus_per_task": 1}},
                   {"id": "tiny", "depends_on": ["long"], "runtime_seconds": 1e-9,
                    "resources": {"cpus_per_task": 100}}])
    try:
        result = workflow.analyze(data, now=0)
    except ValueError as exc:
        assert "precision" in str(exc).lower() or "duration" in str(exc).lower()
    else:
        tiny = next(n for n in result["nodes"] if n["id"] == "tiny")
        assert tiny["earliest_finish"]["estimate"] > tiny["earliest_start"]["estimate"]
        assert result["resource_envelope"]["peak_cpus"] >= 100


def test_half_open_resource_boundaries_hold_with_fractional_durations():
    result = workflow.analyze(recipe([{"id": "first", "runtime_seconds": .5, "resources": {"cpus_per_task": 20}},
                                      {"id": "second", "depends_on": ["first"], "runtime_seconds": .25,
                                       "resources": {"cpus_per_task": 30}},
                                      {"id": "parallel", "runtime_seconds": .75, "resources": {"cpus_per_task": 10}}]), now=0)
    assert result["resource_envelope"]["peak_cpus"] == 40
    assert result["resource_envelope"]["peak_concurrent_jobs"] == 2
    assert result["resource_envelope"]["core_hours"]["estimate"] == pytest.approx(25 / 3600)


def test_duplicate_preflight_cache_never_shares_user_metadata(tmp_path, monkeypatch):
    monkeypatch.delenv("SBATCH_ACCOUNT", raising=False)
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    data = recipe([{"id": "a", "runtime_seconds": 1, "script": script.name, "parameters": {"nested": {"rates": [.1, .2]}}},
                   {"id": "b", "runtime_seconds": 1, "script": script.name, "parameters": {"nested": {"rates": [.1, .2]}}}])
    prepared = workflow.plans(data, workdir=tmp_path)
    prepared[0]["parameters"]["nested"]["rates"].append(999)
    assert prepared[1]["parameters"]["nested"]["rates"] == [.1, .2]
    assert data["nodes"][0]["parameters"]["nested"]["rates"] == [.1, .2]


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width", [1, 8, 40, 120])
def test_workflow_diagram_renders_narrow_terminals_without_control_characters(ascii_, width):
    result = workflow.analyze(recipe([{"id": "a", "runtime_seconds": 1},
                                      {"id": "b", "depends_on": ["a"]}]), now=0)
    glyphs = layout.Glyphs(ascii_)
    rows = planning_views.render(SimpleNamespace(g=glyphs), SimpleNamespace(research_view="workflow"), result, width)
    for row in rows:
        for text, _ in row:
            assert all(character.isprintable() for character in text)
            if ascii_:
                text.encode("ascii")


def test_scaling_limits_are_visible_in_terminal_view():
    result = {"status": "incomplete", "points": [], "limits": ["mixed workload identity: speedup suppressed"]}
    rows = planning_views.render(SimpleNamespace(g=layout.Glyphs(True)), SimpleNamespace(research_view="scaling"), result, 80)
    assert "mixed workload identity" in "\n".join("".join(text for text, _ in row) for row in rows)


@pytest.mark.parametrize("depth", [None, "4", float("nan"), float("inf"), 10**400, -999])
def test_blocker_tree_depth_boundary_cannot_crash_or_allocate_unbounded_text(depth):
    result = {"status": "unknown", "summary": "Unknown", "dependencies": {"tree": [{"job_id": "1", "kind": "afterok", "depth": depth}]}}
    rows = planning_views.render(SimpleNamespace(g=layout.Glyphs(True)), SimpleNamespace(research_view="blockers"), result, 80)
    assert max(len("".join(text for text, _ in row)) for row in rows) < 300


def test_symbolic_workflow_plans_cannot_be_previewed_or_submitted_as_independent_jobs(tmp_path, monkeypatch):
    from raven import submission
    import os
    for key in os.environ:
        if key.startswith("SBATCH_"):
            monkeypatch.delenv(key)
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    prepared = workflow.plans(recipe([{"id": "root", "script": script.name},
                                      {"id": "child", "depends_on": ["root"], "script": script.name}]), workdir=tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("a review-only symbolic workflow reached the scheduler")
    slurm = SimpleNamespace(b=object(), preview_submit=forbidden, submit=forbidden)
    for plan in prepared:
        assert plan["submittable"] is False
        assert not submission.preview(plan, slurm)["ok"]
        result = submission.submit(plan, slurm, passport_directory=tmp_path / "must_not_exist")
        assert result["submitted"] is False
        assert "orchestration" in result["error"]
    assert not (tmp_path / "must_not_exist").exists()
    stripped = copy.deepcopy(prepared[1])
    for field in ("workflow_node_id", "workflow_orchestration", "requires_workflow_orchestration", "symbolic_dependencies", "submittable"):
        stripped.pop(field)
    assert stripped["plan_id"] != submission._digest(stripped)
    assert not submission.submit(stripped, slurm)["ok"]


def test_workflow_execution_permission_is_sealed_with_review_fields(tmp_path, monkeypatch):
    import os
    from raven import submission
    for key in os.environ:
        if key.startswith("SBATCH_"):
            monkeypatch.delenv(key)
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    plan = workflow.plans(recipe([{"id": "root", "script": script.name}]), workdir=tmp_path)[0]
    original = plan["plan_id"]
    plan["submittable"] = True
    assert submission._digest(plan) != original
