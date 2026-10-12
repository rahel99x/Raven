"""Planning through real CLI, controller, background readers, and terminal rows."""
from copy import deepcopy
import json
from pathlib import Path
import random
import shlex
import threading
import time

import pytest

from raven import cli, clock, layout as L
from raven.config import Config
from raven.metrics import write_metric
from raven.model import Finished, Job
from raven.planning import PLANNING_VIEWS, demo_source
from raven.provenance import capture
from raven.research import RESEARCH_VIEWS
from raven.submission import prepare


def write_json(path, value):
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")
    return path


def finish_command(app):
    if app.research.pending:
        app.research.pending[0].result(timeout=10)
        app.research.poll_task()


@pytest.fixture
def planning_files(tmp_path):
    script = tmp_path / "scientific model's job.sbatch"
    script.write_text("#!/bin/bash\n#SBATCH --time=00:30:00\n#SBATCH --output=slurm-%j.out\nprintf 'example\\n'\n")
    contract = write_json(tmp_path / "outputs.json", {"version": 1, "outputs": [
        {"path": "result.json", "format": "json", "required_keys": ["score"]}]})
    write_json(tmp_path / "result.json", {"score": .8})
    metrics = tmp_path / "metrics.jsonl"
    for i in range(6):
        write_metric(metrics, {"误差": 1 / (i + 1)}, step=i, t=100+i,
                     completed=i + 1, total=10, unit="steps")
    stdout = tmp_path / "stdout.log"
    stdout.write_text("Start\n\x1b[31mRuntimeError: CUDA out of memory\x1b[0m\n")
    job = Job("901", "训练", "main", "PENDING", cpus=4, nodes=1, gpus=0,
              mem_req="8Gn", limit="01:00:00", submit=clock.now() - 10, reason="Resources")
    bundle = demo_source(job)
    for row in bundle["history"]:
        row.update(mem_bytes=8*1024**3, time_seconds=3600)
    bundle["jobs"] = [{"id": job.id, "name": job.name, "partition": job.partition,
                       "state": job.state, "cpus": job.cpus, "nodes": 1, "gpus": 0,
                       "mem_bytes": 8*1024**3, "time_seconds": 3600,
                       "submit": clock.now()-10, "reason": "Resources"},
                      {"id": "902", "name": "another experiment", "partition": "main",
                       "state": "PENDING", "cpus": 4, "nodes": 1, "gpus": 0,
                       "mem_bytes": 8*1024**3, "time_seconds": 3600,
                       "submit": clock.now()-10, "reason": "JobHeldUser"}]
    for node in bundle["workflow"]["nodes"]:
        node["script"] = str(script)
    observations = write_json(tmp_path / "planning observations.json", bundle)
    scaling_recipe = {"version": 1, "kind": "raven.scaling", "name": "controlled", "script": str(script),
                      "mode": "strong", "baseline": 1, "problem_size": 1000, "repeats": 3,
                      "configurations": [{"workers": 1, "cpus_per_task": 1, "nodes": 1, "gpus": 0},
                                         {"workers": 2, "cpus_per_task": 1, "nodes": 1, "gpus": 0}]}
    scaling = write_json(tmp_path / "scaling recipe.json", scaling_recipe)
    workflow = write_json(tmp_path / "workflow recipe.json", bundle["workflow"])
    config = write_json(tmp_path / "config.json", {})
    return {"root": tmp_path, "script": script, "contract": contract, "metrics": metrics,
            "stdout": stdout, "bundle": bundle, "observations": observations,
            "scaling": scaling, "scaling_recipe": scaling_recipe, "workflow": workflow, "config": config}


@pytest.fixture
def planning_dashboard(planning_files, monkeypatch):
    session = cli.build(cli.parse(["--fake", "--no-state", "--no-plugins", "--unicode",
                                   "--planning-file", str(planning_files["observations"])]), Config())
    app, hub, store = session.app, session.app.research, session.store
    app.tab = "research"
    app.research_job_id = "900_2"
    store.jobs = [Job("900_[0-9]", "array experiment", "main", "PENDING"),
                  Job("900_3", "array experiment", "main", "RUNNING", cpus=4)]
    store.finished = [Finished("900_2", "array experiment", "OUT_OF_MEMORY", elapsed="00:01:00", exit="0:125")]
    store.details["900_2"] = {"StdOut": str(planning_files["stdout"]), "StdErr": str(planning_files["stdout"])}
    hub.configure(metrics_file=str(planning_files["metrics"]), contract=str(planning_files["contract"]),
                  workdir=str(planning_files["root"]))
    hub.passport = capture(planning_files["root"], script=planning_files["script"], parameters={"model": "模型"})
    hub.plan = prepare(planning_files["script"], workdir=planning_files["root"])
    calls = []
    original = session.backend.call
    monkeypatch.setattr(session.backend, "call", lambda command, *a, **k: calls.append(list(command)) or original(command, *a, **k))
    yield session, planning_files, calls
    session.close()


def load_view(session, view):
    app = session.app
    app.research_view, app.research_scroll = view, 0
    return app.research.request(app.research.context(session.store.snapshot(), app), wait=True, force=True)


def text_frame(session, width=160, height=None):
    rows, hits = session.views.compose(session.store.snapshot(), session.app, width, height, session.actions)
    return L.to_text(rows, width), rows, hits


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width,height", [(0, 0), (1, 1), (24, 10), (40, 16), (104, 32), (160, 60)])
def test_all_twelve_views_fit_terminal_and_preserve_character_fallback(planning_dashboard, ascii_, width, height):
    session, _, calls = planning_dashboard
    session.views.set_ascii(ascii_)
    session.app._ascii_cfg = ascii_
    session.app.set_theme(session.app.theme)
    assert len(RESEARCH_VIEWS) == 13
    for view, _ in RESEARCH_VIEWS:
        result = load_view(session, view)
        assert result.get("status") not in {"loading", "error", "empty"}, (view, result)
        text, rows, hits = text_frame(session, width, height)
        assert len(rows) == height
        assert all(L.vlen(L.row_text(row)) <= width for row in rows)
        assert all(0 <= y < max(0, height-1) for y, _, _ in hits)
        assert all(c.isprintable() or c == "\n" for c in text)
        if ascii_:
            assert text.isascii(), (view, text)
    assert calls == [], "changing research views issued extra scheduler queries"


def test_all_planning_views_use_existing_sampler_snapshot_after_real_settle(monkeypatch):
    session = cli.build(cli.parse(["--fake", "--no-state", "--no-plugins", "--tab", "research",
                                   "--research-view", "predict"]), Config())
    calls = []
    original = session.backend.call
    monkeypatch.setattr(session.backend, "call", lambda command, *a, **k: calls.append(list(command)) or original(command, *a, **k))
    try:
        cli.settle(session, "research")
        assert calls and session.store.snapshot()["jobs"]
        after_settle = len(calls)
        for view, _ in PLANNING_VIEWS:
            load_view(session, view)
            text_frame(session, 104, 30)
        assert len(calls) == after_settle
    finally:
        session.close()


def test_research_navigation_cycles_twelve_views_and_file_job_selection(planning_dashboard):
    session, _, calls = planning_dashboard
    app = session.app
    app.research_view = "experiment"
    visited = []
    for _ in range(len(RESEARCH_VIEWS)):
        visited.append(app.research_view)
        app.handle("right")
    assert visited == [view for view, _ in RESEARCH_VIEWS]
    assert app.research_view == "experiment"
    app.handle("left")
    assert app.research_view == "operations"
    load_view(session, "forecast")
    text_frame(session)
    assert [j["id"] for j in app.research_data_jobs] == ["901", "902"]
    app.research_job_id = "901"
    app.handle("down")
    assert app.research_job_id == "902"
    result = load_view(session, "forecast")
    assert result["job_id"] == "902" and result["status"] == "blocked"
    app.handle("up")
    assert app.research_job_id == "901"
    assert calls == []


@pytest.mark.parametrize("command", ["predict", "forecast", "blockers", "tradeoffs", "scaling", "workflow", "choose"])
def test_offline_planning_cli_never_constructs_scheduler(planning_files, monkeypatch, capsys, command):
    files = planning_files
    def forbidden(*a, **k):
        pytest.fail("offline planning constructed a scheduler")
    monkeypatch.setattr(cli, "build", forbidden)
    monkeypatch.setattr(cli, "make_backend", forbidden)
    if command in {"predict", "forecast", "blockers"}:
        words = [command, "--file", str(files["observations"])]
    elif command == "tradeoffs":
        words = [command, str(files["observations"])]
    elif command == "choose":
        words = [command, "0", str(files["script"]), "--file", str(files["observations"]), "--workdir", str(files["root"])]
    else:
        words = [command, "analyze", str(files[command])]
    code = cli.main(["--config", str(files["config"]), "--no-state", "--no-plugins", "run", *words])
    assert code == (1 if command == "blockers" else 0)
    output = json.loads(capsys.readouterr().out)
    assert output.get("status") not in {"error", "invalid"}
    if command == "choose":
        assert output["prepared_plan"]["valid"]
        assert output["prepared_plan"]["argv"][-1] == str(files["script"])
    if command == "predict":
        assert output["metrics"]["runtime_seconds"]["samples"] == 24


@pytest.mark.parametrize("command", ["scaling", "workflow"])
def test_offline_planning_prepares_reviewable_commands_without_running_script(planning_files, monkeypatch, capsys, command):
    files = planning_files
    def forbidden(*a, **k):
        pytest.fail("review-only plan constructed a scheduler")
    monkeypatch.setattr(cli, "build", forbidden)
    marker = files["root"] / "executed.txt"
    files["script"].write_text("#!/bin/bash\n#SBATCH --output=slurm-%j.out\ntouch " + shlex.quote(str(marker)) + "\n")
    code = cli.main(["--config", str(files["config"]), "run", command, "plan", str(files[command]),
                     "--workdir", str(files["root"])])
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    plans = [run["submission"] for run in output["runs"]] if command == "scaling" else output["plans"]
    assert len(plans) == (6 if command == "scaling" else 4)
    assert all(p["valid"] and p["argv"][-1] == str(files["script"]) for p in plans)
    assert not marker.exists()
    if command == "workflow":
        assert all("--dependency" not in " ".join(p["argv"]) for p in plans)
        assert output["plans"][-1]["symbolic_dependencies"] == ["train-a", "train-b"]
        assert all(not plan["submittable"] for plan in plans)


@pytest.mark.parametrize("global_option", ["--host", "--replay"])
@pytest.mark.parametrize("command", ["predict", "tradeoffs", "scaling", "workflow"])
def test_remote_or_replay_offline_plan_rejected_before_backend(planning_files, monkeypatch, capsys, global_option, command):
    monkeypatch.setattr(cli, "build", lambda *a, **k: pytest.fail("offline rejection created backend"))
    if command == "predict":
        words = [command, "--file", str(planning_files["observations"])]
    elif command == "tradeoffs":
        words = [command, str(planning_files["observations"])]
    else:
        words = [command, "plan", str(planning_files[command])]
    assert cli.main(["--config", str(planning_files["config"]), global_option, "does-not-exist", "run", *words]) == 1
    assert "local" in capsys.readouterr().out.lower()


def test_palette_runs_analysis_in_bounded_background_and_never_changes_jobs(planning_dashboard, monkeypatch):
    session, files, calls = planning_dashboard
    from raven import planning_commands
    entered, release = threading.Event(), threading.Event()
    original = planning_commands.result
    def blocking(*a, **k):
        entered.set()
        assert release.wait(5)
        return original(*a, **k)
    monkeypatch.setattr(planning_commands, "result", blocking)
    session.app.interactive = True
    try:
        start = time.perf_counter()
        session.app.run_command("tradeoffs " + shlex.quote(str(files["observations"])))
        assert time.perf_counter() - start < 1
        assert entered.wait(2)
        assert session.app.research.pending is not None
        for _ in range(20):
            text_frame(session, 104, 30)
        session.app.run_command("workflow analyze " + shlex.quote(str(files["workflow"])))
        assert not session.app.command_ok
        assert "still running" in session.app.message
        assert calls == []
    finally:
        release.set()
        finish_command(session.app)
    assert session.app.command_ok
    assert session.app.research_view == "tradeoffs"
    assert session.app.mode == "main"
    assert calls == []


@pytest.mark.parametrize("command", ["tradeoffs", "choose", "scaling", "workflow"])
def test_interactive_planning_and_choices_never_enter_confirmation_or_submit(planning_dashboard, command):
    session, files, calls = planning_dashboard
    session.app.interactive = True
    if command == "tradeoffs":
        words = [command, str(files["observations"]), "--choose", "0", "--script", str(files["script"]), "--workdir", str(files["root"])]
    elif command == "choose":
        words = [command, "0", str(files["script"]), "--file", str(files["observations"]), "--workdir", str(files["root"])]
    else:
        words = [command, "plan", str(files[command]), "--workdir", str(files["root"])]
    session.app.run_command(shlex.join(words))
    finish_command(session.app)
    assert session.app.command_ok
    assert session.app.mode == "main" and not session.app.confirm
    assert calls == []
    text, _, _ = text_frame(session)
    if command in {"choose", "tradeoffs"}:
        assert session.app.research_view == "submit"
        assert session.app.research.plan["valid"]
        assert "sbatch" in text and "job.sbatch" in text
        assert session.app.research.plan["argv"][-1] == str(files["script"])
    else:
        assert "review" in text.lower() or "Unlimited-capacity" in text


@pytest.mark.parametrize("words", [["predict", "--file", "--fake"], ["forecast", "--file", "--json"],
                                    ["predict", "--name", "--fake"], ["predict", "--account", "a monitoring account"]])
def test_command_value_is_literal_and_not_reinterpreted_as_global_switch(words):
    args = cli.parse(["--unicode", "run", *words])
    assert shlex.split(args.run) == words
    assert not args.fake and not args.json
    assert args.account == ""
    assert args.ascii is False


@pytest.mark.parametrize("option", ["--planning-file", "--config", "--host", "--workdir", "--metrics-file"])
def test_global_option_value_named_run_does_not_start_palette_command(option):
    args = cli.parse([option, "run", "--once", "--fake"])
    assert args.run == ""
    assert getattr(args, option[2:].replace("-", "_")) == "run"
    assert args.once and args.fake


def test_leading_dash_filename_works_with_equals_and_never_enables_fake(planning_files, monkeypatch, capsys):
    monkeypatch.chdir(planning_files["root"])
    write_json(Path("--fake"), planning_files["bundle"])
    monkeypatch.setattr(cli, "build", lambda *a, **k: pytest.fail("literal filename enabled scheduler"))
    args = cli.parse(["run", "predict", "--file=--fake"])
    assert not args.fake
    assert cli.main(["--config", str(planning_files["config"]), "run", "predict", "--file=--fake"]) == 0
    assert json.loads(capsys.readouterr().out)["metrics"]["runtime_seconds"]["samples"] == 24


def test_cached_foreign_status_is_visible_without_losing_terminal_safety(planning_dashboard):
    session, _, calls = planning_dashboard
    app, hub = session.app, session.app.research
    app.research_view = "forecast"
    context = hub.context(session.store.snapshot(), app)
    hub.cache[hub._key(context)] = (time.monotonic(), {"status": "new_status\x1b[31m", "method": "new_method",
                                                     "limitations": ["未知\x00\x07\x1b[31m"]})
    for ascii_ in [False, True]:
        session.views.set_ascii(ascii_)
        text, _, _ = text_frame(session)
        assert "new_status" in text
        assert all(c.isprintable() or c == "\n" for c in text)
        assert text.isascii() if ascii_ else True
    assert calls == []


@pytest.mark.parametrize("view", [name for name, _ in PLANNING_VIEWS])
def test_malformed_planning_bundles_are_errors_or_unknowns_not_ui_crashes(planning_dashboard, view):
    session, files, calls = planning_dashboard
    rng = random.Random(527)
    base = files["bundle"]
    malformed = [None, 2, "bad", [], {}, {"jobs": None}, {"history": None}, {"nodes": "bad"},
                 {"query": {"cpus": True}}, {"scaling": [{"workers": False, "runtime_seconds": 1}]},
                 {"workflow": {"nodes": [{"id": "a", "depends_on": ["a"]}]}},
                 {"candidates": [{"cpus": float("nan")}]}]
    # NaN is not written: the actual strict loader must reject its lexical form.
    for _ in range(18):
        value = deepcopy(base)
        value[rng.choice(["jobs", "history", "query", "scaling", "workflow", "candidates"])] = deepcopy(rng.choice(malformed[:-1]))
        path = files["root"] / "malformed.json"
        write_json(path, value)
        session.app.research.configure(planning_file=str(path))
        load_view(session, view)
        text, rows, _ = text_frame(session, 40, 16)
        assert len(rows) == 16
        assert all(c.isprintable() or c == "\n" for c in text)
    path.write_text('{"history": [NaN]}')
    session.app.research.configure(planning_file=str(path))
    result = load_view(session, view)
    assert result["status"] == "error"
    assert "non-finite" in result["summary"]
    assert calls == []


def test_censored_resource_results_show_lower_bounds_without_predicted_limits(planning_dashboard):
    session, files, _ = planning_dashboard
    bundle = deepcopy(files["bundle"])
    censored = deepcopy(next(row for row in bundle["history"] if row["cpus"] == 4))
    censored.update(id="oom", state="OUT_OF_MEMORY", runtime_seconds=10000, memory_bytes=1e12)
    bundle["history"].append(censored)
    write_json(files["observations"], bundle)
    session.app.research.configure(planning_file=str(files["observations"]))
    result = load_view(session, "predict")
    assert all(value["upper"] is None for value in result["metrics"].values())
    text, _, _ = text_frame(session)
    assert "Lower-bound evidence" in text and "OUT_OF_MEMORY" in text
    assert "censored" in text
    assert "estimate unavailable" in text


def test_workflow_unknown_duration_preserves_known_graph_and_unavailable_completion(planning_dashboard):
    session, files, _ = planning_dashboard
    bundle = deepcopy(files["bundle"])
    bundle["workflow"]["nodes"][1].pop("duration")
    write_json(files["observations"], bundle)
    session.app.research.configure(planning_file=str(files["observations"]))
    result = load_view(session, "workflow")
    assert result["status"] == "incomplete"
    assert result["makespan"]["estimate"] is None
    text, _, _ = text_frame(session)
    assert "train-a" in text and "evaluate" in text
    assert "Ideal completion: unavailable" in text
    assert "duration unavailable" in text


def test_invalid_scaling_plan_surfaces_preflight_issues_in_terminal(planning_dashboard):
    session, files, calls = planning_dashboard
    recipe = deepcopy(files["scaling_recipe"])
    recipe["script"] = str(files["root"] / "missing.sbatch")
    write_json(files["scaling"], recipe)
    session.app.interactive = False
    session.app.run_command(shlex.join(["scaling", "plan", str(files["scaling"]), "--workdir", str(files["root"])]))
    assert not session.app.command_ok
    text, _, _ = text_frame(session)
    assert "script" in text.lower() and ("unreadable" in text.lower() or "missing" in text.lower())
    assert "No data yet" not in text
    assert calls == []


def test_tradeoff_walltime_risk_cannot_disappear_behind_assumption_limit(planning_dashboard):
    session, files, _ = planning_dashboard
    bundle = deepcopy(files["bundle"])
    bundle["candidates"] = [{"label": "unsafe short time", "name": "train", "partition": "main",
                              "cpus": 4, "nodes": 1, "gpus": 0, "mem_bytes": 8*1024**3,
                              "time_seconds": 100, "estimated_runtime_seconds": 1000}]
    write_json(files["observations"], bundle)
    session.app.research.configure(planning_file=str(files["observations"]))
    result = load_view(session, "tradeoffs")
    assert any("below" in risk for risk in result["candidates"][0]["risks"])
    text, _, _ = text_frame(session)
    assert "walltime is below" in text


def test_solid_blocks_render_real_workflow_and_scaling_without_browser(planning_dashboard):
    session, _, calls = planning_dashboard
    session.views.set_ascii(False)
    for view in ("workflow", "scaling", "tradeoffs"):
        load_view(session, view)
        text, _, _ = text_frame(session)
        assert "█" in text, (view, text)
        assert "<html" not in text
    assert calls == []


def test_explicit_prediction_query_and_coverage_survive_background_refresh(planning_dashboard):
    session, files, calls = planning_dashboard
    session.app.interactive = False
    session.app.run_command(shlex.join(["predict", "--file", str(files["observations"]), "--cpus", "8", "--coverage", ".5"]))
    assert session.app.command_ok
    initial = session.app.research_result
    assert initial["cohort"]["cpus"] == 8
    assert initial["target_coverage"] == .5
    refreshed = load_view(session, "predict")
    assert refreshed["cohort"]["cpus"] == 8
    assert refreshed["target_coverage"] == .5
    assert refreshed["metrics"]["runtime_seconds"]["estimate"] == initial["metrics"]["runtime_seconds"]["estimate"]
    assert calls == []


def test_selected_file_job_and_forecast_coverage_survive_background_refresh(planning_dashboard):
    session, files, calls = planning_dashboard
    session.app.interactive = False
    session.app.run_command(shlex.join(["forecast", "902", "--file", str(files["observations"]), "--coverage", ".9"]))
    assert session.app.research_result["job_id"] == "902"
    refreshed = load_view(session, "forecast")
    assert refreshed["job_id"] == "902"
    assert refreshed["calibration"]["requested_coverage"] == .9
    assert calls == []


def test_scaling_mode_and_baseline_survive_background_refresh(planning_dashboard):
    session, files, calls = planning_dashboard
    session.app.interactive = False
    session.app.run_command(shlex.join(["scaling", "analyze", str(files["observations"]), "--mode", "weak", "--baseline", "2"]))
    initial = session.app.research_result
    assert initial["mode"] == "weak"
    refreshed = load_view(session, "scaling")
    assert refreshed["mode"] == "weak"
    assert refreshed["baseline"] == initial["baseline"]
    assert calls == []


def test_workflow_prepared_symbolic_plans_survive_refresh_without_becoming_submittable(planning_dashboard):
    session, files, calls = planning_dashboard
    session.app.interactive = False
    session.app.run_command(shlex.join(["workflow", "plan", str(files["workflow"]), "--workdir", str(files["root"])]))
    assert session.app.command_ok
    initial = session.app.research_result
    assert len(initial["plans"]) == 4
    refreshed = load_view(session, "workflow")
    assert len(refreshed["plans"]) == 4
    assert [p["plan_id"] for p in refreshed["plans"]] == [p["plan_id"] for p in initial["plans"]]
    assert all(not p["submittable"] and p["workflow_orchestration"] == "review_only" for p in refreshed["plans"])
    assert calls == []


def test_prediction_validation_failure_remains_visible_in_resources_view(planning_dashboard):
    session, files, _ = planning_dashboard
    bundle = deepcopy(files["bundle"])
    selected = sorted([r for r in bundle["history"] if r["cpus"] == 4], key=lambda r: r["end"])
    for i, row in enumerate(selected):
        row["runtime_seconds"] = 100 if i < 20 else 10000
    bundle["history"] = selected
    write_json(files["observations"], bundle)
    session.app.research.configure(planning_file=str(files["observations"]))
    result = load_view(session, "predict")
    assert result["metrics"]["runtime_seconds"]["status"] == "poor_validation"
    text, _, _ = text_frame(session)
    assert "observed coverage 0" in text and "drift" in text


def test_supplied_simulation_bundle_shows_calibrated_forecast_and_provenance(planning_dashboard):
    session, _, calls = planning_dashboard
    path = Path(__file__).resolve().parents[1] / "examples" / "planning" / "observations.json"
    session.app.research.configure(planning_file=str(path))
    result = load_view(session, "forecast")
    assert result["method"] == "scheduler_conformal"
    assert result["calibration"]["valid"]
    assert result["lower_start"] <= result["predicted_start"] <= result["upper_start"]
    assert result["samples"] >= 20
    assert "SIMULATED frozen observations" in result["source"]
    text, _, _ = text_frame(session)
    assert "scheduler_conformal" in text and "SIMULATED" in text
    assert "Start interval: unavailable" not in text
    assert "target coverage" in text
    assert calls == []


@pytest.mark.parametrize("command", ["forecast", "blockers"])
def test_bare_job_list_matches_cli_background_refresh_and_ui_selection(planning_dashboard, command):
    session, files, calls = planning_dashboard
    path = write_json(files["root"] / "bare jobs.json", files["bundle"]["jobs"])
    session.app.interactive = False
    words = [command, "902", "--file", str(path)]
    if command == "forecast":
        words += ["--coverage", ".9"]
    session.app.run_command(shlex.join(words))
    assert session.app.research_result["job_id"] == "902"
    initial_status = session.app.research_result["status"]
    refreshed = load_view(session, command)
    assert refreshed["job_id"] == "902" and refreshed["status"] == initial_status
    if command == "forecast":
        assert refreshed["status"] == "blocked"
        assert refreshed["calibration"]["requested_coverage"] == .9
    else:
        assert any(item["code"] == "JobHeldUser" for item in refreshed["blockers"])
    text_frame(session)
    assert [row["id"] for row in session.app.research_data_jobs] == ["901", "902"]
    session.app.handle("up")
    assert session.app.research_job_id == "901"
    assert load_view(session, command)["job_id"] == "901"
    assert calls == []


@pytest.mark.parametrize("command", ["predict", "forecast", "blockers"])
def test_explicit_unknown_file_job_is_error_without_scheduler_or_replacement(planning_files, monkeypatch, capsys, command):
    monkeypatch.setattr(cli, "build", lambda *a, **k: pytest.fail("explicit unknown job created backend"))
    assert cli.main(["--config", str(planning_files["config"]), "run", command, "unknown-job",
                     "--file", str(planning_files["observations"])]) == 1
    text = capsys.readouterr().out
    assert "selected job is absent" in text


def test_explicit_selected_job_removed_from_file_does_not_silently_switch_or_keep_stale_forecast(planning_dashboard):
    session, files, calls = planning_dashboard
    session.app.interactive = False
    session.app.run_command(shlex.join(["forecast", "902", "--file", str(files["observations"])]))
    assert session.app.research_result["job_id"] == "902"
    bundle = deepcopy(files["bundle"])
    bundle["jobs"] = bundle["jobs"][:1]
    write_json(files["observations"], bundle)
    session.app.research.configure(planning_file=str(files["observations"]))
    refreshed = load_view(session, "forecast")
    assert refreshed["status"] == "error"
    assert "selected job is absent" in refreshed["summary"]
    text, _, _ = text_frame(session)
    assert "selected job is absent" in text and "Scheduler point:" not in text
    assert calls == []


def test_reader_theme_remains_ascii_for_all_new_planning_views(planning_dashboard):
    session, _, calls = planning_dashboard
    session.app.set_theme("reader")
    for view, _ in PLANNING_VIEWS:
        load_view(session, view)
        text, rows, _ = text_frame(session, 104, 40)
        assert text.isascii()
        assert len(rows) == 40
        assert "\x1b" not in text
    assert calls == []
