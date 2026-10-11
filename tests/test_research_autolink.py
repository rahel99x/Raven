"""Automatic linking stays exact, asynchronous, bounded, and reversible."""
import copy
import json
import os
import threading
from pathlib import Path

import pytest

from tower import projects, project_ui, navigation_ui
from tower.config import Config
from tower.controller import App
from tower.metrics import MetricReader
from tower.model import Job, Store
from tower.remote import LocalFiles, RemoteFiles
from tower.research import ResearchHub, detach_manual_source


class DeferredHub:
    def __init__(self):
        self.files = LocalFiles()
        self.settings = {"metrics_file": "/manual/metrics", "contract": "/manual/contract", "workdir": "/manual/workdir",
                         "passport": "", "planning_file": "/manual/planning", "planning_overrides": {"forecast": {"job_id": "manual"}}}
        self.passport = self.passport_diff = self.pending = None
        self.interval, self.polling_multiplier = 5.0, 1
        self.generation = 0
        self.plan = {"manual": True}

    def configure(self, **values):
        self.settings.update(values)
        self.generation += 1

    def start_task(self, worker, complete):
        if self.pending:
            return False
        self.pending = worker, complete
        return True

    def finish(self):
        worker, complete = self.pending
        self.pending = None
        try:
            result = worker()
        except Exception as exc:
            result = exc
        complete(result)


def make_run(root, run_id="attempt", job_id="7", paths=None):
    run = root / "runs" / run_id
    run.mkdir(parents=True, exist_ok=True)
    value = {"schema": "tower.run/v1", "run_id": run_id, "experiment_id": "observed", "attempt": 1,
             "state": "RUNNING", "job_id": job_id, "paths": paths or {"metrics": "metrics.jsonl"}}
    (run / "run.json").write_text(json.dumps(value))
    return run


def rewrite(run, **values):
    value = json.loads((run / "run.json").read_text())
    value.update(values)
    (run / "run.json").write_text(json.dumps(value))


@pytest.fixture
def app(tmp_path):
    app = App(Store(persist=False), None, None, Config({}), "tester")
    app.research = DeferredHub()
    app.files = app.logs.files = app.research.files
    app.state_dir = str(tmp_path / "private")
    app.store.apply_jobs([Job("7", "CPU local", "localcpu", "RUNNING"), Job("8", "CPU other", "localcpu", "RUNNING")])
    app.tab, app.selected_id = "jobs", "7"
    project_ui.initialize(app)
    return app


def refresh(app, *, force=True):
    scheduled = project_ui.tick(app, force=force)
    if scheduled:
        app.research.finish()
    return scheduled


def attach_workdir(app, root, jid="7"):
    app.store.details[jid] = {"WorkDir": str(root)}


def test_tick_never_reads_files_or_navigates_and_restores_all_sources(app, tmp_path, monkeypatch):
    root = tmp_path / "project with spaces"
    run = make_run(root, paths={"metrics": "metrics.jsonl", "planning": "reports/bundle.json", "scaling": "reports/scaling.json"})
    attach_workdir(app, root)
    before = copy.deepcopy(app.research.settings)
    app.research_scroll, app.mode = 19, "main"
    calls = []
    original = projects.discover_project
    monkeypatch.setattr(projects, "discover_project", lambda *args, **kw: (calls.append(args), original(*args, **kw))[1])
    assert project_ui.tick(app)
    assert not calls and project_ui.selected_binding(app) is None
    for _ in range(10):
        assert not project_ui.tick(app)
    app.research.finish()
    assert len(calls) == 1
    assert project_ui.selected_binding(app)["job_id"] == "7"
    assert (app.tab, app.mode, app.selected_id, app.research_scroll) == ("jobs", "main", "7", 19)
    assert app.research.settings["planning_file"] == str(run / "reports/bundle.json")
    assert app.research.settings["planning_files"] == {"scaling": str(run / "reports/scaling.json")}
    assert app.research.settings["planning_overrides"] == {}
    app.selected_id = "8"
    assert not project_ui.tick(app)
    assert project_ui.selected_binding(app) is None
    assert app.research.settings == before


@pytest.mark.parametrize("claim,selected,expected", [("81", "81_2", False), ("81_20", "81_2", False), ("81_2", "81_2", True), ("81.batch", "81", False), ("81_2", "81", False)])
def test_exact_array_and_step_identity_only(tmp_path, claim, selected, expected):
    make_run(tmp_path, job_id=claim)
    result = projects.discover_job([str(tmp_path)], selected)
    assert (result["status"] == "ready") is expected


def test_ambiguous_new_attempt_detaches_and_never_guesses_newest(app, tmp_path):
    make_run(tmp_path, "older")
    attach_workdir(app, tmp_path)
    assert refresh(app)
    make_run(tmp_path, "newer")
    assert refresh(app)
    assert project_ui.selected_binding(app) is None
    assert app.project_state["auto_status"] == "ambiguous"
    (tmp_path / "runs/newer/run.json").unlink()
    (tmp_path / "runs/newer").rmdir()
    assert refresh(app)
    assert project_ui.selected_binding(app)["run_id"] == "older"


@pytest.mark.parametrize("problem", ["limited", "invalid", "symlink"])
def test_incomplete_project_cannot_hide_ambiguous_job(tmp_path, monkeypatch, problem):
    make_run(tmp_path, "first")
    second = make_run(tmp_path, "second", job_id="8")
    if problem == "limited":
        monkeypatch.setattr(projects, "MAX_RUNS", 1)
    elif problem == "invalid":
        (second / "run.json").write_text("{")
    else:
        (tmp_path / "runs/hidden").symlink_to(second, target_is_directory=True)
    assert projects.discover_job([str(tmp_path)], "7")["status"] == "incomplete"
    assert projects.select_run(str(tmp_path), "first")["binding"]["job_id"] == "7"


def test_late_inventory_and_completed_state_update_without_restart(app, tmp_path):
    tmp_path.mkdir(exist_ok=True)
    attach_workdir(app, tmp_path)
    assert refresh(app)
    assert project_ui.selected_binding(app) is None
    run = make_run(tmp_path)
    assert refresh(app)
    app.logs.top = 23
    generation = app.research.generation
    rewrite(run, state="COMPLETED", end=100)
    assert refresh(app)
    assert project_ui.selected_binding(app)["state"] == "COMPLETED"
    assert app.project_state["runs"][0]["end"] == 100
    assert app.logs.top == 23
    assert app.research.generation == generation


def test_added_log_files_preserve_current_stream_and_selected_lines(app, tmp_path):
    run = make_run(tmp_path, paths={"metrics": "metrics.jsonl", "stdout": "stdout.log", "log_index": "logs.json"})
    (run / "stdout.log").write_text("actual\n")
    (run / "logs.json").write_text(json.dumps({"schema": "tower.logs/v1", "job_id": "7", "logs": []}))
    attach_workdir(app, tmp_path)
    refresh(app)
    app.logs.entry = dict(project_ui.log_entries(app)[0])
    app.logs.top, app.logs.cursor = 31, 32
    (run / "worker.log").write_text("worker\n")
    (run / "logs.json").write_text(json.dumps({"schema": "tower.logs/v1", "job_id": "7", "logs": [{"id": "worker", "path": "worker.log"}]}))
    refresh(app)
    assert len(project_ui.log_entries(app)) == 2
    assert app.logs.entry["path"] == str(run / "stdout.log")
    assert (app.logs.top, app.logs.cursor) == (31, 32)


def test_selected_job_change_invalidates_slow_worker_before_publication(app, tmp_path):
    make_run(tmp_path)
    attach_workdir(app, tmp_path)
    assert project_ui.tick(app)
    worker, complete = app.research.pending
    app.research.pending = None
    result = worker()
    app.selected_id = "8"
    assert not project_ui.tick(app)
    complete(result)
    assert project_ui.selected_binding(app) is None
    assert app.research.settings["metrics_file"] == "/manual/metrics"


def test_explicit_project_command_takes_priority_over_pending_auto_read(app, tmp_path):
    root = tmp_path / "auto"
    other = tmp_path / "explicit"
    make_run(root)
    make_run(other, "chosen", "8")
    attach_workdir(app, root)
    assert project_ui.tick(app)
    old_worker, old_complete = app.research.pending
    assert project_ui.run_command(app, ["project", str(other)])
    app.research.finish()
    old_complete(old_worker())
    assert app.project_state["registered_root"] == str(other)
    assert app.project_state["root"] == str(other)
    assert project_ui.selected_binding(app) is None


def test_explicit_run_resolves_duplicates_and_stays_selected_on_refresh(app, tmp_path):
    make_run(tmp_path, "a")
    make_run(tmp_path, "b")
    attach_workdir(app, tmp_path)
    project_ui.run_command(app, ["project", str(tmp_path)])
    app.research.finish()
    project_ui.run_command(app, ["run", "select", "a"])
    app.research.finish()
    assert app.project_state["binding_origin"] == "manual"
    assert refresh(app)
    assert project_ui.selected_binding(app)["run_id"] == "a"


@pytest.mark.parametrize("previously_bound", [False, True])
def test_manual_attachment_suppresses_auto_until_job_selection_changes(app, tmp_path, previously_bound):
    make_run(tmp_path)
    attach_workdir(app, tmp_path)
    if previously_bound:
        refresh(app)
    detach_manual_source(app)
    app.research.configure(metrics_file="/chosen/manual")
    assert not project_ui.tick(app, force=True)
    assert app.research.settings["metrics_file"] == "/chosen/manual"
    run8 = make_run(tmp_path, "job8", "8")
    attach_workdir(app, tmp_path, "8")
    app.selected_id = "8"
    assert refresh(app)
    assert app.research.settings["metrics_file"] == str(run8 / "metrics.jsonl")


def test_auto_cadence_uses_base_interval_once_and_bounded_slider_speed(app, tmp_path, monkeypatch):
    make_run(tmp_path)
    attach_workdir(app, tmp_path)
    times = [100.0]
    monkeypatch.setattr(project_ui.time, "monotonic", lambda: times[0])
    assert refresh(app, force=False)
    times[0] = 104.9
    assert not refresh(app, force=False)
    app.research.polling_multiplier = 50
    assert refresh(app, force=False)
    times[0] = 105.1
    assert not refresh(app, force=False)
    times[0] = 105.399
    assert not refresh(app, force=False)
    times[0] = 105.4
    assert refresh(app, force=False)


def test_registered_root_supports_arbitrary_workdir_without_parent_walk(app, tmp_path):
    root = tmp_path / "root"
    make_run(root)
    nested = root / "source/deep"
    nested.mkdir(parents=True)
    assert projects.job_project_roots(str(nested)) == [str(nested)]
    attach_workdir(app, nested)
    app.project_state["registered_root"] = str(root)
    assert refresh(app)
    assert project_ui.selected_binding(app)["project_root"] == str(root)
    assert projects.job_project_roots(str(root / "runs/attempt/checkpoints/deeper")) == [str(root)]


def test_remote_backend_does_not_scan_or_issue_commands(app, tmp_path, monkeypatch):
    make_run(tmp_path)
    attach_workdir(app, tmp_path)
    class Remote(LocalFiles):
        remote = True
    app.research.files = Remote()
    monkeypatch.setattr(projects, "discover_project", lambda *a, **kw: pytest.fail("remote automatic directory scan"))
    assert not project_ui.tick(app)
    assert app.project_state["auto_status"] == "unavailable"


def test_real_worker_io_does_not_run_on_ui_thread(app, tmp_path, monkeypatch):
    make_run(tmp_path)
    attach_workdir(app, tmp_path)
    hub = ResearchHub(app.cfg)
    app.research = hub
    observed = []
    original = projects.discover_project
    def read(*args, **kwargs):
        observed.append(threading.get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(projects, "discover_project", read)
    try:
        binding = project_ui.settle(app)
        assert binding["job_id"] == "7"
        assert observed and all(ident != threading.get_ident() for ident in observed)
    finally:
        hub.close()


@pytest.mark.parametrize("source", ["metrics", "planning", "passport", "artifacts"])
def test_parent_symlink_replacement_cannot_redirect_bound_report_read(tmp_path, source):
    root = tmp_path / "project"
    run = make_run(root, paths={"metrics": "metrics.jsonl", "planning": "planning.json", "passports": "passports"})
    (run / "metrics.jsonl").write_text('{"t":1,"metrics":{"public":1}}\n')
    (run / "planning.json").write_text('{"public":1}')
    (root / ".tower/contracts").mkdir(parents=True)
    (root / ".tower/contracts/outputs.v1.json").write_text('{"version":1,"outputs":[]}')
    binding = projects.select_run(str(root), "attempt")["binding"]
    old = tmp_path / "old-project"
    root.rename(old)
    outside = tmp_path / "private"
    private_run = make_run(outside)
    (private_run / "metrics.jsonl").write_text('{"t":1,"metrics":{"private":99}}\n')
    (private_run / "planning.json").write_text('{"private":99}')
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        if source == "metrics":
            MetricReader().read_confined(binding["metrics_file"], binding)
        elif source == "planning":
            projects.read_bound_json(binding, binding["planning_file"], key="planning")
        elif source == "passport":
            projects.read_bound_passport(binding, str(run / "passports/p.json"))
        else:
            projects.read_bound_artifacts(binding)


def test_artifact_validation_uses_pinned_fd_when_named_run_is_replaced(tmp_path, monkeypatch):
    root = tmp_path / "project"
    run = make_run(root)
    (run / "result.json").write_text('{"public":1}')
    (root / ".tower/contracts").mkdir(parents=True)
    (root / ".tower/contracts/outputs.v1.json").write_text(json.dumps({"version":1,"outputs":[{"path":"result.json","format":"json","required_keys":["public"]}]}))
    binding = projects.select_run(str(root), "attempt")["binding"]
    private = tmp_path / "private"
    private.mkdir()
    (private / "result.json").write_text('{"private":99}')
    original = projects.validate_contract
    def race(*args, **kwargs):
        run.rename(run.with_name("renamed"))
        run.symlink_to(private, target_is_directory=True)
        return original(*args, **kwargs)
    monkeypatch.setattr(projects, "validate_contract", race)
    assert projects.read_bound_artifacts(binding)["valid"]


def test_bound_metrics_incremental_append_and_replacement_keep_exact_run_identity(tmp_path):
    run = make_run(tmp_path)
    path = run / "metrics.jsonl"
    path.write_bytes(b'{"t":1,"metrics":{"loss":.2}}\n'.replace(b':.2', b':0.2'))
    binding = projects.select_run(str(tmp_path), "attempt")["binding"]
    reader = MetricReader()
    first = reader.read_confined(str(path), binding)
    assert first["records"] == 1
    with path.open("ab") as target:
        target.write(b'{"t":2,"metrics":{"loss":0.1}}\n')
    second = reader.read_confined(str(path), binding)
    assert second["records"] == 2 and second["latest"]["loss"] == .1
    replacement = run / "replacement"
    replacement.write_text('{"t":3,"metrics":{"loss":0.05}}\n')
    os.replace(replacement, path)
    assert reader.read_confined(str(path), binding)["records"] == 1
    rewrite(run, job_id="8")
    with pytest.raises(ValueError, match="identity changed"):
        reader.read_confined(str(path), binding)


def test_run_path_redeclaration_is_rejected_until_rebinding(tmp_path):
    run = make_run(tmp_path, paths={"planning": "old.json"})
    (run / "old.json").write_text('{"old":1}')
    binding = projects.select_run(str(tmp_path), "attempt")["binding"]
    rewrite(run, paths={"planning": "new.json"})
    (run / "new.json").write_text('{"new":1}')
    with pytest.raises(ValueError, match="declaration changed"):
        projects.read_bound_json(binding, binding["planning_file"], key="planning")


def test_back_forward_preserve_per_view_planning_sources_without_aliasing(app, tmp_path):
    run = make_run(tmp_path, paths={"metrics":"metrics.jsonl", "predict":"reports/predict.json", "scaling":"reports/scaling.json", "submit":"reports/submit.json"})
    attach_workdir(app, tmp_path)
    refresh(app)
    app.tab, app.research_view = "research", "scaling"
    expected = copy.deepcopy(app.research.settings)
    saved = navigation_ui.location(app)
    app.research.settings["planning_files"]["scaling"] = "wrong"
    app.research.settings["submit_file"] = "wrong"
    navigation_ui._restore_location(app, saved)
    assert app.research.settings["planning_files"] == expected["planning_files"]
    assert app.research.settings["submit_file"] == str(run / "reports/submit.json")
    assert app.project_state["binding_origin"] == "automatic"
    assert not app.project_state["auto_pending"]
    saved["research_settings"]["planning_files"]["scaling"] = "changed_saved_copy"
    assert app.research.settings["planning_files"]["scaling"] == expected["planning_files"]["scaling"]


@pytest.mark.parametrize("view", ["predict", "forecast", "blockers", "tradeoffs", "scaling", "workflow"])
def test_native_hub_reads_each_exact_per_view_source_and_refreshes_replacement(app, tmp_path, view):
    run = make_run(tmp_path, paths={"planning": "planning.json", view: "view.json"})
    source = {"version": 1, "kind": "tower.planning", "job_id": "7", "jobs": [{"id": "7", "name": "CPU local", "state": "PENDING", "partition": "localcpu", "cpus": 1, "nodes": 1, "gpus": 0}], "history": [], "now": 100}
    if view == "workflow":
        source = {"version": 1, "kind": "tower.workflow", "nodes": [{"id": "task", "script": "jobs/task.sh"}]}
    (run / "view.json").write_text(json.dumps(source))
    (run / "planning.json").write_text('{"private_aggregate_marker":99}')
    attach_workdir(app, tmp_path)
    hub = ResearchHub(app.cfg)
    app.research = hub
    try:
        project_ui.settle(app)
        app.research_view = view
        context = hub.context(app.store.snapshot(), app)
        first = hub.request(context, wait=True, force=True)
        assert first.get("source") == str(run / "view.json")
        assert first["status"] != "error", first
        replacement = run / "replace.json"
        changed = dict(source, now=200) if view != "workflow" else dict(source, nodes=[{"id": "changed_task", "script": "jobs/task.sh"}])
        replacement.write_text(json.dumps(changed))
        os.replace(replacement, run / "view.json")
        # Expire the published planning snapshot without changing base cadences.
        with hub.lock:
            hub.planning_source = None
        newer = hub.request(hub.context(app.store.snapshot(), app), wait=True, force=True)
        assert newer.get("source") == str(run / "view.json")
        assert newer["status"] != "error", newer
        (run / "view.json").unlink()
        with hub.lock:
            hub.planning_source = None
        missing = hub.request(hub.context(app.store.snapshot(), app), wait=True, force=True)
        assert missing["status"] == "error"
        assert "private_aggregate_marker" not in str(missing)
    finally:
        hub.close()


@pytest.mark.parametrize("view", ["predict", "forecast", "blockers", "tradeoffs"])
def test_bound_planning_never_falls_back_to_another_job_in_report(app, tmp_path, view):
    run = make_run(tmp_path, paths={view: "view.json"})
    (run / "view.json").write_text(json.dumps({"version":1, "kind":"tower.planning", "jobs":[{"id":"8","state":"PENDING"}], "history":[]}))
    attach_workdir(app, tmp_path)
    hub = ResearchHub(app.cfg)
    app.research = hub
    try:
        project_ui.settle(app)
        app.research_view = view
        result = hub.request(hub.context(app.store.snapshot(), app), wait=True, force=True)
        assert result["status"] == "error"
        assert "selected job is absent" in result["summary"]
    finally:
        hub.close()


def test_source_status_row_is_cached_bounded_and_reflects_conflicts(app, tmp_path, monkeypatch):
    make_run(tmp_path)
    attach_workdir(app, tmp_path)
    refresh(app)
    monkeypatch.setattr(projects, "discover_project", lambda *a, **kw: pytest.fail("status renderer read project files"))
    from tower.layout import row_text, vlen
    text = row_text(project_ui.status_row(app, 120))
    assert "Auto-linked attempt" in text and "job 7" in text
    assert vlen(row_text(project_ui.status_row(app, 10, True))) <= 10
    project_ui.clear_binding(app)
    app.project_state.update(auto_status="ambiguous", auto_summary="2 inventories declare job_id 7")
    assert "2 inventories" in row_text(project_ui.status_row(app, 120))


def test_saved_location_preserves_original_planning_paths_and_rejects_invalid_extra_settings(app, tmp_path):
    from tower import navigation_tools
    run = make_run(tmp_path)
    attach_workdir(app, tmp_path)
    original = copy.deepcopy(app.research.settings)
    original["planning_files"] = {"forecast": "/explicit/job7/forecast.json"}
    original["submit_file"] = "/explicit/job7/submit.json"
    app.research.settings = copy.deepcopy(original)
    refresh(app)
    app.tab = "research"
    portable = navigation_tools._portable_location(app)
    valid = navigation_tools._valid_location(portable)
    assert valid["project_context"]["binding_backup"]["settings"]["planning_files"] == original["planning_files"]
    navigation_ui._restore_location(app, valid)
    project_ui.clear_binding(app)
    assert app.research.settings == original
    broken = copy.deepcopy(portable)
    broken["project_context"]["binding_backup"]["settings_extra"] = {"shell_command": "execute me"}
    with pytest.raises(ValueError, match="original research setting"):
        navigation_tools._valid_location(broken)


def test_evidence_revalidates_log_declarations_and_refuses_replaced_parent(tmp_path):
    root = tmp_path / "project"
    run = make_run(root, paths={"stdout": "stdout.log", "log_index": "logs.json"})
    (run / "stdout.log").write_text("public stdout\\n")
    (run / "worker.log").write_text("public worker\\n")
    (run / "logs.json").write_text(json.dumps({"schema":"tower.logs/v1", "job_id":"7", "logs":[{"id":"worker", "path":"worker.log"}]}))
    selected = projects.select_run(str(root), "attempt")
    worker = next(entry for entry in selected["logs"] if entry["path"].endswith("worker.log"))
    assert projects.read_bound_tail(selected["binding"], worker, 65536)[0] == b"public worker\\n"
    (run / "logs.json").write_text(json.dumps({"schema":"tower.logs/v1", "job_id":"7", "logs":[]}))
    with pytest.raises(ValueError, match="no longer declared"):
        projects.read_bound_tail(selected["binding"], worker, 65536)
    private = tmp_path / "private"
    make_run(private, paths={"stdout":"stdout.log"})
    (private / "runs/attempt/stdout.log").write_text("private secret\\n")
    root.rename(tmp_path / "old")
    root.symlink_to(private, target_is_directory=True)
    stdout = next(entry for entry in selected["logs"] if entry["role"] == "stdout")
    with pytest.raises((OSError, ValueError)):
        projects.read_bound_tail(selected["binding"], stdout, 65536)


def test_bound_read_only_report_cannot_submit_previous_manual_plan(app, tmp_path):
    from tower import research_commands
    from tower.submission import prepare
    run = make_run(tmp_path)
    attach_workdir(app, tmp_path)
    script = tmp_path / "batch.sh"
    script.write_text("#!/bin/bash\nprintf 'actual task'\n")
    app.research.plan = prepare(str(script), workdir=str(tmp_path))
    refresh(app)
    assert project_ui.selected_binding(app)
    assert research_commands.execute(app, "submit", [])
    assert not app.command_ok and "read-only" in app.message
    assert app.mode != "confirm"
    assert not app.confirm


def test_explicit_preparation_detaches_auto_report_and_shows_new_manual_plan(app, tmp_path):
    from tower import research_commands
    run = make_run(tmp_path, paths={"submit":"old-submit.json"})
    attach_workdir(app, tmp_path)
    hub = ResearchHub(app.cfg)
    app.research = hub
    app.interactive = False
    script = tmp_path / "new-batch.sh"
    script.write_text("#!/bin/bash\nprintf 'actual task'\n")
    try:
        project_ui.settle(app)
        assert research_commands.execute(app, "prepare", [str(script), "--workdir", str(tmp_path)])
        assert app.command_ok, app.message
        assert project_ui.selected_binding(app) is None
        assert app.research_view == "submit"
        result = hub.request(hub.context(app.store.snapshot(), app), wait=True, force=True)
        assert result["plan"]["script"] == str(script)
        assert not result.get("display_only")
        assert not project_ui.tick(app, force=True)
    finally:
        hub.close()


def test_actual_slow_automatic_worker_yields_to_explicit_project_command(app, tmp_path, monkeypatch):
    automatic, manual = tmp_path / "automatic", tmp_path / "manual"
    make_run(automatic)
    make_run(manual, "explicit", "8")
    attach_workdir(app, automatic)
    hub = ResearchHub(app.cfg)
    app.research = hub
    entered, release = threading.Event(), threading.Event()
    original = projects.discover_project
    threads = []
    def blocked(path, **kwargs):
        threads.append(threading.get_ident())
        if path == str(automatic):
            entered.set()
            assert release.wait(3)
        return original(path, **kwargs)
    monkeypatch.setattr(projects, "discover_project", blocked)
    try:
        assert project_ui.tick(app)
        assert entered.wait(3)
        old_complete = hub.pending[1]
        old_future = hub.pending[0]
        assert project_ui.run_command(app, ["project", str(manual)])
        assert hub.pending[0] is not old_future
        release.set()
        hub.pending[0].result(timeout=5)
        hub.poll_task()
        old_complete(old_future.result(timeout=5))
        assert app.project_state["root"] == str(manual)
        assert project_ui.selected_binding(app) is None
        assert len(set(threads)) == 1 and threads[0] != threading.get_ident()
    finally:
        release.set()
        hub.close()


def test_offline_refresh_of_removed_manual_run_publishes_error_without_traceback(app, tmp_path):
    run = make_run(tmp_path)
    hub = ResearchHub(app.cfg)
    app.research = hub
    try:
        project_ui.run_command(app, ["project", str(tmp_path)])
        hub.pending[0].result(timeout=5)
        hub.poll_task()
        project_ui.run_command(app, ["run", "select", "attempt"])
        hub.pending[0].result(timeout=5)
        hub.poll_task()
        (run / "run.json").unlink()
        assert project_ui.settle(app) is None
        assert app.project_state["auto_status"] == "error"
        assert hub.settings["metrics_file"] == ""
    finally:
        hub.close()


def test_bare_app_uses_remote_log_backend_and_never_probes_another_machine(tmp_path, monkeypatch):
    class NoCommands:
        def run(self, *args, **kwargs):
            pytest.fail("automatic discovery issued a remote command")
    files = RemoteFiles(NoCommands())
    app = App(Store(persist=False), None, None, Config({}), "remote-reader")
    app.files = None
    app.logs.files = files
    app.store.apply_jobs([Job("7", "remote job", "main", "RUNNING")])
    app.store.details["7"] = {"WorkDir": str(tmp_path / "remote-project")}
    app.selected_id, app.tab = "7", "jobs"
    monkeypatch.setattr(projects, "discover_project", lambda *a, **kw: pytest.fail("automatic discovery probed a local directory"))
    calls = []
    def remote_discovery(*args, **kwargs):
        calls.append((threading.get_ident(), kwargs["files"]))
        return {"status": "missing", "selected": None, "projects": [], "warnings": [], "summary": "No report"}
    monkeypatch.setattr(projects, "discover_job", remote_discovery)
    try:
        assert project_ui.tick(app)
        assert app.research.files is files
        app.research.pending[0].result(timeout=5)
        app.research.poll_task()
        assert calls and all(thread != threading.get_ident() and backend is files for thread, backend in calls)
        assert app.project_state["auto_status"] == "missing"
        assert project_ui.selected_binding(app) is None
    finally:
        app.research.close()


@pytest.mark.parametrize("field,before,after", [("Restarts", "0", "1"), ("Cluster", "first", "other"),
                                                ("SubmitTime", "2026-01-01T00:00:00", "2026-02-01T00:00:00")])
def test_pending_read_rejects_same_job_id_with_new_scheduler_attempt_without_waiting_for_tick(app, tmp_path, field, before, after):
    make_run(tmp_path)
    attach_workdir(app, tmp_path)
    app.store.details["7"][field] = before
    assert project_ui.tick(app)
    worker, callback = app.research.pending
    app.research.pending = None
    value = worker()
    app.store.details["7"][field] = after
    callback(value)
    assert project_ui.selected_binding(app) is None
    assert not app.project_state["auto_pending"]


def test_manual_project_read_cannot_publish_after_connection_adapter_changes(app, tmp_path):
    make_run(tmp_path)
    assert project_ui.run_command(app, ["project", str(tmp_path)])
    original = app.research.files
    app.research.files = LocalFiles()
    app.research.finish()
    assert app.project_state["root"] == ""
    assert "connection changed" in app.project_state["summary"]
    assert original is not app.research.files
