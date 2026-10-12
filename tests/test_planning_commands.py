"""Direct planning commands must preserve choices and never borrow frozen evidence."""
from copy import deepcopy
import json
import math

import pytest

from raven import cli, clock, planning, planning_commands, tradeoffs
from raven.config import Config
from raven.model import Finished, Job


def save_json(tmp_path, value, name="planning.json"):
    path = tmp_path / name
    path.write_text(json.dumps(value, allow_nan=False))
    return path


def measured(**values):
    return {"name": "file-work", "partition": "cpu", "cpus": 4, "nodes": 1, "gpus": 0,
            "mem_bytes": 8 << 30, "time_seconds": 3600, "state": "COMPLETED", **values}


@pytest.fixture
def bundle(tmp_path):
    query = {"name": "file-work", "partition": "cpu", "cpus": 4, "nodes": 1, "gpus": 0}
    jobs = [{"id": "901", **query, "state": "PENDING", "submit": 990, "reason": "Resources"},
            {"id": "902", **query, "state": "PENDING", "submit": 990, "reason": "JobHeldUser"}]
    history = [measured(id=str(i), runtime_seconds=100, submit=100 + i, start=110 + i, end=210 + i)
               for i in range(40)]
    script = tmp_path / "safe's script.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    workflow = {"version": 1, "kind": "raven.workflow", "nodes": [
        {"id": "a", "script": str(script), "duration": {"lower": 10, "estimate": 10, "upper": 10}, "resources": {"cpus_per_task": 1}},
        {"id": "b", "script": str(script), "depends_on": ["a"],
         "duration": {"lower": 20, "estimate": 20, "upper": 20}, "resources": {"cpus_per_task": 1}}]}
    data = {"jobs": jobs, "history": history, "query": query, "now": 1000,
            "candidates": [dict(query, mem_bytes=8 << 30, time_seconds=3600, estimated_runtime_seconds=100)],
            "workflow": workflow}
    return save_json(tmp_path, data), data, script


@pytest.fixture
def dashboard(monkeypatch):
    session = cli.build(cli.parse(["--fake", "--once", "--no-state", "--no-plugins"]), Config())
    session.store.jobs = [Job("live", "unrelated-live-job", "other-cluster", "PENDING", cpus=16, submit=990)]
    session.app.selected_id = "live"
    session.app.research_job_id = "live"
    monkeypatch.setattr(session.backend, "call", lambda *args, **kwargs: pytest.fail("planning queried the scheduler"))
    yield session
    session.close()


def test_file_jobs_replace_live_selected_object_without_explicit_id(bundle):
    path, _, _ = bundle
    live = Job("live", "unrelated", "other", "PENDING", cpus=100, submit=0)
    output, opts = planning_commands.result("forecast", ["--file", str(path)], job=live)
    assert output["job_id"] == "901"
    assert output["cohort"]["name"] == "file-work"
    assert opts.selected_job_id == "901"


def test_file_explicit_job_id_is_selected_or_truthfully_absent(bundle):
    path, _, _ = bundle
    output, opts = planning_commands.result("forecast", ["902", "--file", str(path)])
    assert output["job_id"] == "902"
    assert output["status"] == "blocked"
    assert opts.analysis_overrides == {"job_id": "902"}
    with pytest.raises(ValueError, match="absent"):
        planning_commands.result("forecast", ["missing", "--file", str(path)])


def test_prediction_cli_partial_query_merges_file_identity(bundle):
    path, data, _ = bundle
    data["query"].update(script_sha256="a" * 64, parameters={"seed": 1}, input_size=10)
    for row in data["history"]:
        row.update(script_sha256="a" * 64, parameters={"seed": 1}, input_size=10)
    path.write_text(json.dumps(data))
    output, opts = planning_commands.result("predict", ["--file", str(path), "--cpus", "4", "--coverage", ".5"])
    assert output["cohort"]["script_sha256"] == "a" * 64
    assert output["cohort"]["parameters"] == {"seed": 1}
    assert output["cohort"]["input_size"] == 10
    assert output["target_coverage"] == .5
    assert opts.analysis_overrides == {"query": {"cpus": 4}, "coverage": .5}


def test_execute_persists_explicit_file_job_and_coverage_across_refresh(dashboard, bundle):
    path, _, _ = bundle
    app, hub = dashboard.app, dashboard.app.research
    assert planning_commands.execute(app, "forecast", ["902", "--file", str(path), "--coverage", ".9"])
    assert not app.command_ok  # A scheduler-held job reports a blocked forecast.
    assert app.research_job_id == "902"
    assert hub.settings["planning_overrides"]["forecast"] == {"job_id": "902", "coverage": .9}
    refreshed = hub.request(hub.context(dashboard.store.snapshot(), app), wait=True, force=True)
    assert refreshed["job_id"] == "902"
    assert refreshed["calibration"]["requested_coverage"] == .9


def test_execute_persists_merged_prediction_query_across_refresh(dashboard, bundle):
    path, _, _ = bundle
    app, hub = dashboard.app, dashboard.app.research
    planning_commands.execute(app, "predict", ["--file", str(path), "--cpus", "4", "--coverage", ".5"])
    assert app.command_ok
    refreshed = hub.request(hub.context(dashboard.store.snapshot(), app), wait=True, force=True)
    assert refreshed["cohort"]["name"] == "file-work"
    assert refreshed["cohort"]["cpus"] == 4
    assert refreshed["target_coverage"] == .5


def test_choose_existing_comparison_retains_file_and_prepares_only(dashboard, bundle):
    path, _, script = bundle
    app, hub = dashboard.app, dashboard.app.research
    planning_commands.execute(app, "tradeoffs", [str(path)])
    assert app.command_ok
    planning_commands.execute(app, "choose", ["0", str(script)])
    assert app.command_ok
    assert hub.settings["planning_file"] == str(path)
    assert app.research_view == "submit"
    assert hub.plan["valid"]
    assert app.mode != "confirm"
    assert not hasattr(app, "submitted_job_id")


def test_choose_without_comparison_and_unknown_index_are_explicit(bundle):
    path, _, script = bundle
    with pytest.raises(ValueError, match="attach tradeoffs"):
        planning_commands.result("choose", ["0", str(script)])
    with pytest.raises(ValueError, match="index"):
        planning_commands.result("choose", ["100", str(script), "--file", str(path)])


def test_workflow_command_honors_frozen_bundle_clock(bundle):
    path, _, _ = bundle
    output, opts = planning_commands.result("workflow", ["analyze", str(path)])
    assert output["now"] == 1000
    assert opts.analysis_overrides == {"action": "analyze"}


def test_workflow_plan_action_and_workdir_survive_ui_refresh(dashboard, bundle, tmp_path):
    _, data, _ = bundle
    path = save_json(tmp_path, data["workflow"], "workflow.json")
    app, hub = dashboard.app, dashboard.app.research
    planning_commands.execute(app, "workflow", ["plan", str(path), "--workdir", str(tmp_path)])
    assert app.command_ok
    before = app.research_result
    assert hub.settings["planning_overrides"]["workflow"] == {"action": "plan", "workdir": str(tmp_path)}
    refreshed = hub.request(hub.context(dashboard.store.snapshot(), app), wait=True, force=True)
    assert [item["plan_id"] for item in refreshed["plans"]] == [item["plan_id"] for item in before["plans"]]


def test_scaling_recipe_analyze_action_does_not_refresh_into_plan(dashboard, tmp_path, bundle):
    _, _, script = bundle
    recipe = {"version": 1, "kind": "raven.scaling", "script": str(script), "problem_size": 100,
              "mode": "strong", "repeats": 3, "configurations": [{"workers": 1, "cpus_per_task": 1}]}
    path = save_json(tmp_path, recipe)
    app, hub = dashboard.app, dashboard.app.research
    planning_commands.execute(app, "scaling", ["analyze", str(path), "--mode", "weak", "--baseline", "1"])
    initial = app.research_result
    refreshed = hub.request(hub.context(dashboard.store.snapshot(), app), wait=True, force=True)
    assert initial["schema"] == refreshed["schema"] == "raven.scaling-analysis/v1"
    assert initial["mode"] == refreshed["mode"] == "weak"
    assert hub.settings["planning_overrides"]["scaling"] == {"action": "analyze", "mode": "weak", "baseline": 1}


def test_scaling_load_uses_its_bounded_eight_mib_limit(tmp_path):
    path = save_json(tmp_path, {"records": [], "description": "x" * (2 << 20)})
    output, _ = planning_commands.result("scaling", ["analyze", str(path)])
    assert output["schema"] == "raven.scaling-analysis/v1"


@pytest.mark.parametrize("cmd,args", [
    ("tradeoffs", []), ("choose", ["0"]), ("scaling", ["unknown", "file"]),
    ("workflow", ["analyze", "file", "--unknown"]), ("predict", ["--cpus", "abc"]),
    ("unknown", ["analyze", "NONEXISTENT_FILE"]),
])
def test_unknown_or_malformed_commands_fail_before_actions(cmd, args):
    with pytest.raises(ValueError):
        planning_commands.result(cmd, args)


def test_analysis_only_flags_never_silently_affect_scaling_plan(tmp_path, bundle):
    path, _, _ = bundle
    with pytest.raises(ValueError, match="apply to analysis"):
        planning_commands.result("scaling", ["plan", str(path), "--mode", "weak"])


@pytest.mark.parametrize("cmd", ["workflow", "scaling"])
def test_workdir_on_read_only_analysis_is_rejected(cmd, bundle):
    path, _, _ = bundle
    with pytest.raises(ValueError, match="applies to plan"):
        planning_commands.result(cmd, ["analyze", str(path), "--workdir", "unused"])


def test_file_jobs_cannot_borrow_live_blocker_metadata(monkeypatch):
    import raven.blockers
    captured = []
    monkeypatch.setattr(raven.blockers, "explain", lambda job, **kwargs: captured.append(kwargs) or {"status": "unknown"})
    job = {"id": "901", "state": "PENDING", "reason": "Resources"}
    live = {"finished": [{"id": "upstream", "state": "COMPLETED"}], "details": {"901": {"Dependency": "afterok:upstream"}},
            "partitions": ["unrelated"], "nodemap": {"other": {}}, "share": [{"Priority": 1}], "health": {"jobs": {"stale": False}}}
    planning.analyze("blockers", {"jobs": [job], "now": 1000}, snap=live)
    assert captured[0]["finished"] == []
    assert captured[0]["details"] == {}
    assert captured[0]["partitions"] == []
    assert captured[0]["nodes"] == {}
    assert captured[0]["share"] == []
    assert captured[0]["health"] == {}
    assert captured[0]["now"] == 1000


def test_file_jobs_cannot_borrow_live_forecast_observations(monkeypatch):
    import raven.forecast
    captured = []
    monkeypatch.setattr(raven.forecast, "forecast", lambda job, history, **kwargs: captured.append((history, kwargs)) or {"status": "unknown"})
    planning.analyze("forecast", {"jobs": [{"id": "901", "state": "PENDING"}], "now": 1000},
                     snap={"finished": [{"id": "other", "state": "COMPLETED"}]}, observations=[{"job_id": "901", "actual_start": 950}])
    assert captured[0][0] == []
    assert captured[0][1]["observations"] == ()
    assert captured[0][1]["now"] == 1000


@pytest.mark.parametrize("value", [True, None, "1000", "2026-01-01T00:00:00Z", -1, float("inf"), float("nan"), 1 << 5000])
def test_malformed_bundle_clock_is_rejected_consistently(value):
    source = {"now": value, "jobs": []}
    for view in ("predict", "forecast", "blockers", "tradeoffs", "scaling", "workflow"):
        with pytest.raises(ValueError, match="planning now"):
            planning.analyze(view, source)


def test_known_generic_gpu_request_preserved_without_hardware_dominance():
    rows = planning.candidates_from_history([measured(gpus=1, gpu_type="")])
    assert rows[0]["gpu_type"] == ""
    result = tradeoffs.compare([dict(rows[0], estimated_runtime_seconds=100), dict(rows[0], cpus=8, estimated_runtime_seconds=500)])
    assert all(row["status"] == "assumed" for row in result["candidates"])
    assert result["frontier"] == [0, 1]
    assert all(row["pareto"]["compared_with"] == [] for row in result["candidates"])


def test_historical_candidates_retain_exact_work_hash_inputs_and_parameters():
    first = measured(script_sha256="a" * 64, input_size=100, parameters={"seed": 3}, account="study", qos="normal")
    second = measured(cpus=8, script_sha256="a" * 64, input_size=100, parameters={"seed": 3}, account="study", qos="normal")
    rows = planning.candidates_from_history([first, deepcopy(first), second], job=first)
    assert len(rows) == 2
    assert all(row["script_sha256"] == "a" * 64 and row["input_size"] == 100 and row["parameters"] == {"seed": 3} for row in rows)
    assert [row["cpus"] for row in rows] == [4, 8]


@pytest.mark.parametrize("change", [
    {"script_sha256": "b" * 64}, {"parameters": {"seed": 4}}, {"input_size": 101},
])
def test_historical_candidates_cannot_drop_work_identity_differences(change):
    first = measured(script_sha256="a" * 64, input_size=100, parameters={"seed": 3})
    wrong = dict(first, cpus=8, **change)
    assert len(planning.candidates_from_history([first, wrong], job=first)) == 1


@pytest.mark.parametrize("change", [
    {"cpus": []}, {"cpus": True}, {"cpus": 1.5}, {"nodes": 0}, {"gpus": -1},
    {"mem_bytes": 1.1}, {"mem_bytes": float("inf")}, {"time_seconds": None, "limit": []},
    {"time_seconds": None, "limit": {"bad": "value"}}, {"time_seconds": float("nan")},
    {"name": []}, {"partition": {}}, {"account": [1]}, {"script_sha256": "bad"},
    {"parameters": {"seed": float("nan")}}, {"parameters": {"seed": [None] * 1000}},
])
def test_malformed_history_records_are_skipped_without_losing_valid_candidates(change):
    invalid = measured(**change)
    rows = planning.candidates_from_history([invalid, measured()])
    assert len(rows) == 1
    assert rows[0]["cpus"] == 4
    assert rows[0]["mem_bytes"] == 8 << 30


def test_historical_finished_aliases_and_known_optional_identity():
    old = Finished("1", "file-work", "COMPLETED", elapsed="00:01:40", cpus=4, req_mem=8 << 30,
                   partition="cpu", limit="01:00:00")
    rows = planning.candidates_from_history([old])
    assert len(rows) == 1
    assert rows[0]["mem_bytes"] == 8 << 30
    assert rows[0]["time_seconds"] == 3600


def test_history_generators_remain_bounded_and_models_see_truncation():
    consumed = []

    def records():
        for i in range(100000):
            consumed.append(i)
            yield measured(id=str(i), runtime_seconds=100, end=1000 + i)

    output = planning.analyze("predict", {"history": records(), "query": {"name": "file-work", "partition": "cpu"}})
    assert len(consumed) == 10001
    assert output["truncated"] is True
    assert output["records_considered"] == 10000


def test_pending_job_selection_does_not_exhaust_unbounded_generators():
    consumed = []

    def records():
        for i in range(100000):
            consumed.append(i)
            yield {"id": str(i), "state": "COMPLETED"}

    assert planning.select_job(records(), pending=True)["id"] == "0"
    assert len(consumed) == 10001


def test_live_job_identifier_can_select_finished_job_for_prediction():
    history = [measured(id=str(i), runtime_seconds=100, end=1000+i) for i in range(30)]
    output, opts = planning_commands.result("predict", ["1"], snap={"jobs": [], "finished": history})
    assert opts.selected_job_id == "1"
    assert output["metrics"]["runtime_seconds"]["estimate"] == 100


def test_bare_file_job_array_is_supported_by_command_and_analyze(tmp_path):
    jobs = [{"id": "10", "name": "held", "partition": "cpu", "state": "PENDING", "reason": "JobHeldUser", "submit": 100}]
    path = save_json(tmp_path, jobs)
    direct, opts = planning_commands.result("forecast", ["--file", str(path)])
    refresh = planning.analyze("forecast", jobs)
    assert opts.selected_job_id == "10"
    assert direct["job_id"] == refresh["job_id"] == "10"
    assert direct["status"] == refresh["status"] == "blocked"


def test_explicit_bundle_now_reaches_workflow_and_blockers(bundle, monkeypatch):
    _, source, _ = bundle
    import raven.blockers
    seen = []
    monkeypatch.setattr(raven.blockers, "explain", lambda job, **kwargs: seen.append(kwargs["now"]) or {"status": "unknown"})
    planning.analyze("blockers", source, now=2000)
    output = planning.analyze("workflow", source, now=2000)
    assert seen == [1000]
    assert output["now"] == 1000


def test_demo_scaling_repeat_identity_is_valid_and_one_based():
    source = planning.demo_source()
    assert min(row["repeat"] for row in source["scaling"]) == 1
    result = planning.analyze("scaling", source)
    assert result["status"] != "error"
    assert result["excluded_count"] == 0
