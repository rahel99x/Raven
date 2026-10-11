"""Cross-feature sequence regressions for the operations workbench.

These tests change the environment between inspection and action rather than
only testing a validator with one static document.
"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import copy
import hashlib
import json
import os
import signal
import sys
import threading
import time

import pytest

from tower import operations
from tower.remote import LocalFiles
from tower.slurm import Backend, CommandError


@pytest.fixture
def operation_context(tmp_path):
    return operations.Context(
        slurm=SimpleNamespace(b=Backend(), user="researcher"), files=LocalFiles(),
        jobs=({"id": "81", "name": "experiment", "state": "PENDING",
               "submit": "2026-10-09T10:00:00", "start": "Unknown",
               "cluster": "lab", "user": "researcher", "account": "project"},),
        selected="81", scope={"cluster": "lab", "user": "researcher",
                                "profile": "local", "connection_host": "login"},
        cancel=threading.Event(), state_dir=str(tmp_path))


def _review(ctx):
    return operations.prepare_plan(ctx, "pending-edit", {"job_id": "81"},
                                   {"changes": {"TimeLimit": "10"}})


@pytest.mark.parametrize("field,new_value", [
    ("cluster", "other"), ("profile", "other"), ("user", "other"),
    ("connection_host", "other-login"),
])
def test_prepare_switch_connection_return_and_apply_sequence(operation_context, field, new_value):
    ctx = operation_context
    reviewed = _review(ctx)
    changed = replace(ctx, scope={**ctx.scope, field: new_value})
    with pytest.raises(ValueError, match="connection changed"):
        operations.validate_plan(reviewed, changed, "pending-edit")
    # Merely reading a stale plan must not mutate the original review.
    assert operations.validate_plan(reviewed, ctx, "pending-edit") == {"changes": {"TimeLimit": "10"}}


@pytest.mark.parametrize("field,new_value", [
    ("submit", "2026-10-09T11:00:00"), ("start", "2026-10-09T10:01:00"),
    ("cluster", "other"), ("account", "other"), ("user", "other"),
])
def test_job_refresh_cannot_retarget_review_to_reused_identity(operation_context, field, new_value):
    reviewed = _review(operation_context)
    changed = replace(operation_context,
                      jobs=({**operation_context.jobs[0], field: new_value},))
    with pytest.raises(ValueError, match="attempt changed"):
        operations.validate_plan(reviewed, changed, "pending-edit")


def test_completed_job_replaced_by_new_active_id_does_not_match_old_review(operation_context):
    old = {**operation_context.jobs[0], "state": "COMPLETED",
           "start": "2026-10-09T10:01:00", "end": "2026-10-09T10:20:00"}
    before = replace(operation_context, jobs=(), finished=(old,))
    reviewed = _review(before)
    new = {**operation_context.jobs[0], "submit": "2026-10-09T11:00:00"}
    after = replace(before, jobs=(new,))
    with pytest.raises(ValueError, match="attempt changed"):
        operations.validate_plan(reviewed, after, "pending-edit")


def test_mutating_original_editor_data_does_not_change_prepared_payload(operation_context):
    fields = {"job_id": "81"}
    payload = {"changes": {"TimeLimit": "10"}}
    reviewed = operations.prepare_plan(operation_context, "pending-edit", fields, payload)
    fields["job_id"] = "82"
    payload["changes"]["TimeLimit"] = "999"
    assert operations.validate_plan(reviewed, operation_context, "pending-edit") == {"changes": {"TimeLimit": "10"}}
    returned = operations.validate_plan(reviewed, operation_context, "pending-edit")
    returned["changes"]["TimeLimit"] = "500"
    assert operations.validate_plan(reviewed, operation_context, "pending-edit")["changes"]["TimeLimit"] == "10"


def test_cancel_and_replay_block_prepared_action(operation_context):
    reviewed = _review(operation_context)
    operation_context.cancel.set()
    with pytest.raises(ValueError, match="cancelled"):
        operations.validate_plan(reviewed, operation_context, "pending-edit")
    operation_context.cancel.clear()
    with pytest.raises(ValueError, match="Recorded"):
        operations.validate_plan(reviewed, replace(operation_context, replay=True), "pending-edit")


def test_local_operation_refuses_remote_transport_even_with_local_file_reader(operation_context):
    remote = SimpleNamespace(inner=SimpleNamespace(host="cluster.example"))
    ctx = replace(operation_context, slurm=SimpleNamespace(b=remote))
    with pytest.raises(ValueError, match="target host"):
        operations.local_only(ctx)


def test_source_read_detects_path_replaced_during_inspection(operation_context, tmp_path, monkeypatch):
    source, replacement = tmp_path / "source.json", tmp_path / "replacement.json"
    source.write_text('{"generation":1}')
    replacement.write_text('{"generation":2}')
    original = os.fstat
    calls = 0

    def switched(fd):
        nonlocal calls
        result = original(fd)
        calls += 1
        if calls == 2:
            os.replace(replacement, source)
        return result

    monkeypatch.setattr(os, "fstat", switched)
    with pytest.raises(ValueError, match="changed during inspection"):
        operations.read_json(operation_context, source)


@pytest.mark.parametrize("body", ['{"id":1,"id":2}', '{"value":NaN}', '{"value":1e999}',
                                  '[' * 40 + '0' + ']' * 40])
def test_manifest_replacement_with_invalid_evidence_is_rejected(operation_context, tmp_path, body):
    source = tmp_path / "result.json"
    source.write_text('{"value":1}')
    assert operations.read_json(operation_context, source) == {"value": 1}
    source.write_text(body)
    with pytest.raises(ValueError):
        operations.read_json(operation_context, source)


def test_atomic_publish_preserves_existing_destination(operation_context, tmp_path):
    path = tmp_path / "result.json"
    operations.atomic_json(path, {"generation": 1})
    with pytest.raises(FileExistsError):
        operations.atomic_json(path, {"generation": 2})
    assert json.loads(path.read_text()) == {"generation": 1}
    assert list(tmp_path.glob(".tower-*")) == []


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
def test_large_command_output_is_bounded_before_return(operation_context, channel):
    code = "import sys; getattr(sys, " + repr(channel) + ").write('x' * 1000000)"
    with pytest.raises(CommandError, match="output exceeded"):
        operations.command(operation_context, [sys.executable, "-c", code], limit=1024)


def test_command_failure_then_success_does_not_reuse_failed_output(operation_context):
    with pytest.raises(CommandError, match="exit 3"):
        operations.command(operation_context, [sys.executable, "-c", "print('old'); raise SystemExit(3)"])
    assert operations.command(operation_context, [sys.executable, "-c", "print('new')"]).strip() == "new"


def _running(pid):
    try:
        record = Path(f"/proc/{pid}/stat").read_text()
        return record[record.rfind(")") + 2:].split()[0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.skipif(not hasattr(os, "fork") or not Path("/proc").exists(), reason="Linux process-group lifecycle")
def test_command_timeout_kills_descendant_after_leader_exits(operation_context, tmp_path):
    marker = tmp_path / "child.pid"
    code = ("import os,sys,time; child=os.fork(); "
            "os._exit(0) if child else None; "
            "open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(20)")
    child = None
    try:
        with pytest.raises(CommandError, match="timed out"):
            operations.command(operation_context, [sys.executable, "-c", code, str(marker)], timeout=.5)
        child = int(marker.read_text())
        deadline = time.monotonic() + 2
        while _running(child) and time.monotonic() < deadline:
            time.sleep(.01)
        assert not _running(child), "A descendant survived its operation timeout"
    finally:
        if child is None and marker.exists():
            child = int(marker.read_text())
        if child and _running(child):
            os.kill(child, signal.SIGKILL)


def _transfer_manifest(tmp_path, contents=(b"scientific output",)):
    entries = []
    for index, content in enumerate(contents):
        source = tmp_path / f"source-{index}.dat"
        source.write_bytes(content)
        entries.append({"id": f"result-{index}", "source": str(source),
                        "destination": str(tmp_path / f"destination-{index}.dat"),
                        "sha256": hashlib.sha256(content).hexdigest(), "size": len(content),
                        "direction": "output"})
    manifest = tmp_path / "transfer.json"
    manifest.write_text(json.dumps({"schema": "tower.staging/v1", "id": "experiment-results",
                                    "entries": entries, "retention": "keep-source"}))
    return manifest, entries


def test_transfer_review_then_manifest_edit_never_copies_new_source(operation_context, tmp_path):
    from tower import ops_services
    manifest, entries = _transfer_manifest(tmp_path)
    reviewed = ops_services.run("staging", {"manifest": str(manifest), "direction": "all"}, operation_context)["plan"]
    value = json.loads(manifest.read_text())
    value["entries"][0]["destination"] = str(tmp_path / "other.dat")
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="manifest changed"):
        ops_services.apply("staging", reviewed, operation_context)
    assert not Path(entries[0]["destination"]).exists()
    assert not (tmp_path / "other.dat").exists()


def test_transfer_review_then_source_edit_reports_failure_and_keeps_collision(operation_context, tmp_path):
    from tower import ops_services
    manifest, entries = _transfer_manifest(tmp_path, (b"result-one", b"result-two"))
    reviewed = ops_services.run("staging", {"manifest": str(manifest), "direction": "all"}, operation_context)["plan"]
    Path(entries[0]["source"]).write_bytes(b"new result")
    collision = Path(entries[1]["destination"])
    collision.write_bytes(b"unrelated data")
    result = ops_services.apply("staging", reviewed, operation_context)
    assert result["status"] == "partial"
    assert [row["status"] for row in result["data"]["entries"]] == ["failed", "failed"]
    assert not Path(entries[0]["destination"]).exists()
    assert collision.read_bytes() == b"unrelated data"


def test_repeated_transfer_verifies_published_output_and_preserves_source(operation_context, tmp_path):
    from tower import ops_services
    manifest, entries = _transfer_manifest(tmp_path)
    arguments = {"manifest": str(manifest), "direction": "all"}
    first_plan = ops_services.run("staging", arguments, operation_context)["plan"]
    first = ops_services.apply("staging", first_plan, operation_context)
    assert first["data"]["entries"][0]["status"] == "copied"
    destination = Path(entries[0]["destination"])
    identity = destination.stat().st_ino
    second_plan = ops_services.run("staging", arguments, operation_context)["plan"]
    second = ops_services.apply("staging", second_plan, operation_context)
    assert second["data"]["entries"][0]["status"] == "verified-existing"
    assert destination.stat().st_ino == identity
    assert Path(entries[0]["source"]).read_bytes() == destination.read_bytes()


def _scientific_reuse(tmp_path):
    def write(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value))
        return path

    def hashed(path, **extra):
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), **extra}

    policy = write("acceptance.json", {"schema": "tower.acceptance/v1", "cases": [
        {"id": "seed-1", "expected": {"accuracy": {"value": .9, "absolute": .01, "relative": 0}}}]})
    results = write("measurements.json", {"schema": "tower.results/v1", "runs": [
        {"id": "seed-1", "metrics": {"accuracy": .901}}]})
    dependencies, identity = [], {"project": "research-model", "inputs_sha256": {}}
    for role, value in (("code", {"version": "abc123"}), ("environment", {"python": "3.11"}),
                        ("parameters", {"seed": 1})):
        path = write(role + ".json", value)
        entry = hashed(path, role=role)
        identity[role + "_sha256"] = entry["sha256"]
        dependencies.append(entry)
    output = tmp_path / "model.dat"
    output.write_bytes(b"validated model coefficients")
    request = write("requested-identity.json", identity)
    manifest = write("reuse.json", {"schema": "tower.reuse/v1", "identity": identity,
        "dependencies": dependencies, "outputs": [hashed(output, target="model.dat")],
        "acceptance": hashed(policy), "results": hashed(results)})
    return SimpleNamespace(policy=policy, results=results, manifest=manifest, identity=request, output=output,
                           destination=tmp_path / "reused")


def test_acceptance_then_verified_reuse_then_staging_pipeline(operation_context, tmp_path):
    source = _scientific_reuse(tmp_path)
    accepted = operations.run("acceptance", {"manifest": str(source.policy), "results": str(source.results)}, operation_context)
    assert accepted["status"] == "ok" and accepted["data"]["passed"] == 1
    reviewed = operations.run("reuse", {"manifest": str(source.manifest), "identity": str(source.identity),
        "destination": str(source.destination)}, operation_context)
    reused = operations.apply("reuse", reviewed["plan"], operation_context)
    assert reused["status"] == "ok"
    materialized = source.destination / "model.dat"
    assert materialized.read_bytes() == source.output.read_bytes()
    assert (source.destination / ".tower-receipt.json").is_file()
    assert not (source.destination / ".tower-incomplete").exists()
    returned = tmp_path / "returned-model.dat"
    transfer = tmp_path / "return.json"
    transfer.write_text(json.dumps({"schema": "tower.staging/v1", "id": "scientific-return", "entries": [{
        "id": "model", "source": str(materialized), "destination": str(returned), "direction": "output",
        "size": materialized.stat().st_size, "sha256": hashlib.sha256(materialized.read_bytes()).hexdigest()}]}))
    stage = operations.run("staging", {"manifest": str(transfer)}, operation_context)
    outcome = operations.apply("staging", stage["plan"], operation_context)
    assert outcome["status"] == "ok"
    assert returned.read_bytes() == source.output.read_bytes()


def test_acceptance_pass_does_not_authorize_changed_measurements_for_reuse(operation_context, tmp_path):
    source = _scientific_reuse(tmp_path)
    reviewed = operations.run("reuse", {"manifest": str(source.manifest), "identity": str(source.identity),
        "destination": str(source.destination)}, operation_context)
    source.results.write_text(json.dumps({"schema": "tower.results/v1", "runs": [
        {"id": "seed-1", "metrics": {"accuracy": .2}}]}))
    failed = operations.run("acceptance", {"manifest": str(source.policy), "results": str(source.results)}, operation_context)
    assert failed["status"] == "failed"
    with pytest.raises(ValueError, match="source changed"):
        operations.apply("reuse", reviewed["plan"], operation_context)
    assert not source.destination.exists()


def test_roadmap_catalog_contains_every_remaining_proposal_once():
    catalog = operations.catalog()
    proposals = [item["proposal"] for item in catalog if item["proposal"] != "QoL"]
    expected = ({f"S{i:02d}" for i in range(1, 6)} | {f"P{i:02d}" for i in range(1, 6)} |
                {f"A{i:02d}" for i in range(1, 21)}) - {"A03", "A13", "A14", "A16"}
    assert set(proposals) == expected
    assert len(proposals) == len(expected)
    assert [item["key"] for item in catalog if item["proposal"] == "QoL"] == ["queue-order"]


@pytest.mark.parametrize("first", ["pending-edit", "dependency-repair"])
def test_pending_editor_and_dependency_repair_detect_each_others_changes(operation_context, tmp_path, first):
    class Scheduler:
        def __init__(self):
            self.updates = []
            self.jobs = {str(jid): {"JobId": str(jid), "UserId": "researcher(1000)",
                "SubmitTime": "2026-10-09T10:00:00", "StartTime": "Unknown", "RestartCnt": "0",
                "JobState": "PENDING" if jid == 81 else "COMPLETED", "Dependency": "afterok:82" if jid == 81 else "(null)"}
                for jid in (81, 82, 83, 84)}

        def run(self, argv, timeout=8):
            if argv[:3] == ["scontrol", "show", "job"]:
                return " ".join(f"{key}={value}" for key, value in self.jobs[argv[-1]].items()), 0
            if argv[:2] == ["scontrol", "update"]:
                self.updates.append(argv)
                jid = argv[2].split("=", 1)[1]
                key, value = argv[3].split("=", 1)
                self.jobs[jid][key] = value
                return "", 0
            raise AssertionError(f"Unexpected scheduler call: {argv}")

    scheduler = Scheduler()
    ctx = replace(operation_context, slurm=SimpleNamespace(b=scheduler, user="researcher"))
    manifest = tmp_path / "repair.json"
    manifest.write_text(json.dumps({"schema": "tower.dependency-repair/v1", "repairs": [
        {"job_id": "81", "submit_time": "2026-10-09T10:00:00", "dependency": "afterok:84"}]}))
    plans = {
        "pending-edit": operations.run("pending-edit", {"job_id": "81", "field": "Dependency", "value": "afterok:83"}, ctx)["plan"],
        "dependency-repair": operations.run("dependency-repair", {"manifest": str(manifest)}, ctx)["plan"],
    }
    second = "dependency-repair" if first == "pending-edit" else "pending-edit"
    operations.apply(first, plans[first], ctx)
    with pytest.raises(ValueError, match="changed"):
        operations.apply(second, plans[second], ctx)
    assert len(scheduler.updates) == 1
    assert scheduler.jobs["81"]["Dependency"] == ("afterok:83" if first == "pending-edit" else "afterok:84")


@pytest.fixture
def operation_dashboard(tmp_path):
    from tower import layout as L
    from tower.actions import Actions
    from tower.config import Config
    from tower.controller import App
    from tower.model import Job, Store
    from tower.research import ResearchHub
    from tower.sampler import Sampler
    from tower.slurm import FakeBackend, Slurm
    from tower.views import Views
    from tower.worker_scheduler import WorkerScheduler

    cfg = Config()
    for key, value in (("startup_animation", False), ("animations", False), ("log_lines", 0)):
        cfg.set(key, value)
    store = Store(persist=False)
    store.jobs = [Job("81", "selected", "main", "PENDING", submit="2026-10-09T10:00:00"),
                  Job("82", "other", "main", "RUNNING", submit="2026-10-09T10:10:00")]
    slurm = Slurm(FakeBackend(), "test")
    scheduler = WorkerScheduler()
    sampler = Sampler(slurm, store, cfg["intervals"], cfg["gpu_types"], weather=False, budget=False,
                      worker_scheduler=scheduler)
    app = App(store, sampler, Actions(slurm, store), cfg, "test", interactive=False)
    app.research = ResearchHub(cfg, slurm=slurm, worker_scheduler=sampler.worker_scheduler)
    app.worker_scheduler = sampler.worker_scheduler
    app.state_dir = str(tmp_path)
    app.selected_id = "81"
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views

    def draw(width=150, height=45):
        rows, hits = views.compose(store.snapshot(), app, width, height)
        return rows, hits, views.overlay(store.snapshot(), app, width, height)

    result = SimpleNamespace(app=app, views=views, draw=draw, slurm=slurm, store=store,
                             scheduler=sampler.worker_scheduler)
    draw()
    yield result
    if app.ops_state.get("cancel") is not None:
        app.ops_state["cancel"].set()
    app.research.close()
    sampler.shutdown()
    sampler.worker_scheduler.shutdown(wait=True, cancel_futures=True)


_MOUSE = SimpleNamespace(BUTTON1_CLICKED=1, BUTTON1_PRESSED=2, BUTTON1_RELEASED=4,
    BUTTON1_DOUBLE_CLICKED=8, BUTTON3_CLICKED=16, BUTTON3_PRESSED=32,
    BUTTON4_PRESSED=64, BUTTON5_PRESSED=128, REPORT_MOUSE_POSITION=256, BUTTON_SHIFT=512)


@pytest.mark.parametrize("bits", [1, 2, 4, 16, 64, 128, 256, 258])
def test_operations_modal_owns_raw_input_over_hidden_browser(operation_dashboard, monkeypatch, bits):
    from tower import chart_interaction, history_browser, screen
    app = operation_dashboard.app
    app.enter_tab("deps")
    history_browser._view(app).update(explicit=True, selected="81")
    app.run_command("ops")
    operation_dashboard.draw()
    initial = (app.tab, app.selected_id, tuple(app.marks))
    def forbidden(*args, **kwargs):
        raise AssertionError("Operation overlay dispatched to a hidden pane")
    monkeypatch.setattr(app, "move", forbidden)
    monkeypatch.setattr(chart_interaction, "hover", forbidden)
    monkeypatch.setattr(history_browser, "handle_mouse", forbidden)
    screen._apply_input(app, ("mouse", (0, 2, app.height-3, 0, bits)), app.last_hits, _MOUSE)
    assert (app.tab, app.selected_id, tuple(app.marks)) == initial
    assert app.mode == "operations"


@pytest.mark.parametrize("ascii_mode", [False, True])
@pytest.mark.parametrize("dimensions", [(28, 12), (70, 24), (150, 45)])
@pytest.mark.parametrize("feature", [spec["key"] for spec in operations.catalog()])
def test_all_operation_forms_render_without_collecting_or_overflow(operation_dashboard, monkeypatch, feature, dimensions, ascii_mode):
    from tower import layout as L
    app = operation_dashboard.app
    operation_dashboard.views.set_ascii(ascii_mode)
    def forbidden(*args, **kwargs):
        raise AssertionError("Rendering started a scheduler or file inspection")
    monkeypatch.setattr(operation_dashboard.slurm.b, "run", forbidden)
    monkeypatch.setattr(operations, "read_bytes", forbidden)
    app.run_command("ops " + feature)
    assert app.mode == "operations" and app.ops_state["feature"] == feature
    _, _, positioned = operation_dashboard.draw(*dimensions)
    assert positioned
    for y, x, row in positioned:
        assert 0 <= y < dimensions[1]
        assert 0 <= x < dimensions[0]
        assert x + L.vlen(L.row_text(row)) <= dimensions[0]
    for control in app.interaction_state["graph"].controls:
        assert control.rect.clip(*dimensions) == control.rect


def _ui_action(monkeypatch):
    def run(feature, params, ctx):
        return operations.report(feature, "Review time limit", plan=operations.prepare_plan(
            ctx, feature, params, {"field": "TimeLimit", "value": params["value"]}))
    monkeypatch.setattr(operations, "run", run)


def test_apply_requires_painted_review_and_press_release_cannot_apply_twice(operation_dashboard, monkeypatch):
    from tower import screen
    app = operation_dashboard.app
    initial_selection = app.tab, app.selected_id
    _ui_action(monkeypatch)
    applied = []
    def apply(feature, plan, ctx):
        applied.append(plan["digest"])
        return operations.report(feature, "Applied", data={"applied": True})
    monkeypatch.setattr(operations, "apply", apply)
    app.run_command("ops run pending-edit job_id=81 field=TimeLimit value=10")
    app.run_command("ops review")
    app.run_command("ops apply")
    assert applied == []
    operation_dashboard.draw()
    target = next(control for control in app.interaction_state["graph"].controls if control.id == "ops:apply")
    for bits in (2, 4, 1, 2, 4):
        screen._apply_input(app, ("mouse", (0, target.rect.left, target.rect.top, 0, bits)), app.last_hits, _MOUSE)
    assert len(applied) == 1
    assert (app.tab, app.selected_id) == initial_selection


def test_edit_form_command_from_catalog_remains_renderable(operation_dashboard):
    app = operation_dashboard.app
    app.run_command("ops")
    app.run_command("ops form")
    assert operation_dashboard.draw()[2]


@pytest.mark.parametrize("initial_mode,target_mode", [("single", "multi"), ("multi", "single")])
def test_action_completion_survives_close_navigation_and_worker_switch(operation_dashboard, monkeypatch, initial_mode, target_mode):
    from tower import ops_ui
    app = operation_dashboard.app
    _ui_action(monkeypatch)
    operation_dashboard.scheduler.request_mode(initial_mode)
    app.run_command("ops run pending-edit job_id=81 field=TimeLimit value=10")
    app.run_command("ops review")
    operation_dashboard.draw()
    entered, release = threading.Event(), threading.Event()
    def apply(feature, plan, ctx):
        entered.set()
        assert release.wait(3)
        assert not ctx.cancel.is_set(), "Closing an action discarded its receipt"
        return operations.report(feature, "Recorded", data={"receipt": "one"})
    monkeypatch.setattr(operations, "apply", apply)
    app.interactive = True
    app.run_command("ops apply")
    try:
        assert entered.wait(3)
        operation_dashboard.scheduler.request_mode(target_mode)
        ops_ui.close(app)
        app.enter_tab("history")
        mode, tab = app.mode, app.tab
    finally:
        release.set()
    deadline = time.monotonic() + 3
    while app.ops_state["running"] and time.monotonic() < deadline:
        app.research.poll_task()
        time.sleep(.005)
    assert not app.ops_state["running"]
    assert app.ops_state["result"]["data"]["receipt"] == "one"
    assert (app.mode, app.tab) == (mode, tab)
    assert operation_dashboard.scheduler.status()["mode"] == target_mode
