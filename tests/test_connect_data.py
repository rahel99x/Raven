"""Real retained observations, portable producer snippets and asynchronous UI."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from raven import connect_data as C, connect_data_ui as U, layout as L
from raven import projects, reporting
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.remote import LocalFiles, RemoteFiles


class Background:
    demo = False

    def __init__(self):
        self.files, self.settings, self.pending = LocalFiles(), {}, None

    def start_task(self, work, complete):
        if self.pending:
            return False
        self.pending = work, complete
        return True

    def finish(self):
        work, complete = self.pending
        self.pending = None
        try:
            value = work()
        except Exception as exc:
            value = exc
        complete(value)

    def configure(self, **values):
        self.settings.update(values)


@pytest.fixture
def run(tmp_path):
    (tmp_path / "job.sh").write_text("#!/bin/sh\ntrue\n")
    directory = reporting.begin_run(tmp_path, "fit-a1", name="Fit", script="job.sh", job_id="1170")
    reporting.write_metric(directory, {"loss": .9}, step=0, t=1700000000)
    reporting.write_metric(directory, {"loss": .2}, step=1, t=1700000001)
    return directory


def binding(run, files=None):
    return projects.select_run(str(run.parent.parent), run.name, files=files)["binding"]


@pytest.fixture
def app(run):
    app = App(Store(persist=False), None, None, Config({}), "researcher")
    app.research = Background()
    app.files = app.research.files
    app.width, app.height = 80, 24
    app.project_state.update(root=str(run.parent.parent), binding=binding(run))
    return app


def json_file(path, value):
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(json.dumps(value) + "\n")


def telemetry(run, *, gpu=False):
    from raven.retained_telemetry_io import INDEX_SCHEMA, CPU_SCHEMA, GPU_SCHEMA
    sources = [{"family": "cpu-memory", "schema": CPU_SCHEMA, "path": "telemetry/cpu-memory.jsonl", "status": "complete"},
               {"family": "gpu", "schema": GPU_SCHEMA, "path": "telemetry/gpu.jsonl", "status": "complete" if gpu else "unavailable", "reason": "" if gpu else "GPU provider unavailable"}]
    json_file(run / "telemetry/index.json", {"schema": INDEX_SCHEMA, "run_id": run.name, "sources": sources})
    json_file(run / "telemetry/cpu-memory.jsonl", {"schema": CPU_SCHEMA, "run_id": run.name, "timestamp": 1700000000, "cpu_seconds": 1.5, "rss_bytes": 4096})
    if gpu:
        json_file(run / "telemetry/gpu.jsonl", {"schema": GPU_SCHEMA, "run_id": run.name, "timestamp": 1700000000, "utilization_percent": None, "memory_used_mib": None})


def trace(run):
    from raven.trace_recorder import Recorder
    recorder = Recorder(run, job_id="1170", attempt=run.name, identity_extra={"run_id": run.name}, background_flush=False)
    recorder.emit("science", {"phase": "fit"})
    recorder.close()
    json_file(run / "trace/index.json", {"schema": "raven.trace-index/v1", "sources": [{"schema": "raven.trace/v1", "path": str(recorder.path.relative_to(run))}]})
    return recorder


def test_real_metric_contract_validates_and_previews_without_mutation(run):
    before = {str(path): path.read_bytes() for path in run.rglob("*") if path.is_file()}
    result = C.inspect(str(run / "metrics.jsonl"), binding=binding(run), declared=True)
    assert result["readiness"]["scientific"]["status"] == "ready"
    assert result["metrics"]["records"] == 2
    assert result["metrics"]["latest"] == {"loss": .2}
    lines = C.preview_lines(result["metrics"], L.Glyphs(False))
    assert any("loss: last 0.2" in row for row in lines)
    assert any("2 observations" in row for row in lines)
    assert before == {str(path): path.read_bytes() for path in run.rglob("*") if path.is_file()}


@pytest.mark.parametrize("record,field", [({"metrics": {"loss": 1}}, "t"),
    ({"t": 2, "metrics": {"loss": None}}, "metric value"),
    ({"t": 2, "metrics": {"loss": True}}, "metric value"),
    ({"t": 2, "progress": {"completed": 2}}, "progress.total"),
    ({"t": 2, "step": -1}, "step")])
def test_missing_or_invalid_fields_report_real_error(tmp_path, record, field):
    source = tmp_path / "metrics.jsonl"
    json_file(source, record)
    result = C.validate_metrics(str(source))
    assert not result["usable"] and result["records"] == 0
    assert field in " ".join(result["issues"])


def test_valid_measurements_survive_bad_record_and_partial_tail(run):
    with (run / "metrics.jsonl").open("ab") as stream:
        stream.write(b'{"t": 4, "metrics": {"loss": null}}\n{"t": 5')
    result = C.validate_metrics(str(run / "metrics.jsonl"))
    assert result["usable"] and result["records"] == 2
    assert result["series"]["loss"][-1]["value"] == .2
    assert "finite number" in result["issues"][0]


def test_bounded_tail_and_chart_points(tmp_path):
    source = tmp_path / "large.jsonl"
    with source.open("w") as stream:
        for i in range(9000):
            stream.write(json.dumps({"t": i, "metrics": {"measured": i}}) + "\n")
    result = C.validate_metrics(str(source))
    assert result["truncated"] and len(result["series"]["measured"]) == C.SAMPLE_POINTS
    assert result["records"] < 9000


def test_no_zero_is_invented_for_unavailable_gpu_or_tracing(run):
    telemetry(run)
    result = C.inspect(str(run / "metrics.jsonl"), binding=binding(run), declared=True)
    assert result["readiness"]["cpu-memory"]["status"] == "ready"
    assert result["readiness"]["cpu-memory"]["samples"] == 1
    assert result["readiness"]["gpu"]["status"] == "unavailable"
    assert "GPU provider unavailable" in result["readiness"]["gpu"]["reason"]
    assert result["readiness"]["tracing"]["status"] == "not-recorded"
    assert result["metrics"]["latest"] == {"loss": .2}


def test_null_gpu_counters_are_waiting_not_ready(run):
    telemetry(run, gpu=True)
    result = C.inspect(binding=binding(run))
    assert result["readiness"]["gpu"]["samples"] == 0
    assert result["readiness"]["gpu"]["status"] == "waiting"


def test_one_missing_gpu_counter_keeps_measured_counter_with_partial_status(run):
    telemetry(run, gpu=True)
    path = run / "telemetry/gpu.jsonl"
    row = json.loads(path.read_text())
    row["memory_used_mib"] = 512
    json_file(path, row)
    result = C.inspect(binding=binding(run))
    assert result["readiness"]["gpu"]["samples"] == 1
    assert result["readiness"]["gpu"]["status"] == "partial"
    assert "available observations remain usable" in result["readiness"]["gpu"]["reason"]


def test_actual_journal_events_verified_against_exact_attempt(run):
    trace(run)
    result = C.inspect(binding=binding(run))
    assert result["readiness"]["tracing"]["status"] == "ready"
    assert result["readiness"]["tracing"]["samples"] >= 2


def test_wrong_trace_identity_does_not_affect_cpu_or_scientific_data(run):
    recorder = trace(run)
    value = json.loads(recorder.path.read_text())
    value["identity"]["run_id"] = "different-attempt"
    json_file(recorder.path, value)
    telemetry(run)
    result = C.inspect(str(run / "metrics.jsonl"), binding=binding(run), declared=True)
    assert result["readiness"]["tracing"]["status"] == "unavailable"
    assert "identity does not match" in result["readiness"]["tracing"]["reason"]
    assert result["readiness"]["cpu-memory"]["status"] == "ready"
    assert result["readiness"]["scientific"]["status"] == "ready"


def test_changed_run_identity_refuses_all_bound_reads(run):
    selected = binding(run)
    value = json.loads((run / "run.json").read_text())
    value["job_id"] = "9999"
    json_file(run / "run.json", value)
    result = C.inspect(str(run / "metrics.jsonl"), binding=selected, declared=True)
    assert all(row["status"] == "unavailable" for row in result["readiness"].values())
    assert result["metrics"] is None


def test_symlink_and_fifo_sources_never_open_or_block(tmp_path):
    source = tmp_path / "real"
    json_file(source, {"t": 1, "metrics": {"x": 2}})
    link = tmp_path / "link"
    link.symlink_to(source)
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    for path in (link, pipe):
        result = C.validate_metrics(str(path))
        assert not result["usable"] and "regular file" in result["issues"][0]


class IsolatedSSH:
    def run(self, command, timeout):
        assert command[:4] == ["python3", "-I", "-S", "-c"]
        result = subprocess.run(command, cwd="/tmp", capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise OSError(result.stderr)
        return result.stdout, 0


def test_isolated_ssh_uses_confined_readers_and_trace_metadata_limit(run):
    telemetry(run)
    trace(run)
    files = RemoteFiles(IsolatedSSH())
    selected = binding(run, files)
    result = C.inspect(selected["metrics_file"], files=files, binding=selected, declared=True)
    assert result["metrics"]["latest"] == {"loss": .2}
    assert result["readiness"]["cpu-memory"]["status"] == "ready"
    assert result["readiness"]["tracing"]["status"] == "declared"
    assert "trace host" in result["readiness"]["tracing"]["reason"]


def test_ui_defers_all_reads_and_keeps_bound_identity_when_opening_chart(app, monkeypatch):
    original = C.inspect
    calls = []
    monkeypatch.setattr(C, "inspect", lambda *a, **k: (calls.append(k), original(*a, **k))[1])
    U.open_connect(app)
    assert calls == [] and app.connect_data_state["busy"]
    app.research.finish()
    assert len(calls) == 1 and app.connect_data_state["result"]["metrics"]["usable"]
    selected = app.project_state["binding"]
    U.run_command(app, ["connectdata", "action", "attach"])
    assert app.mode == "connect_data"
    app.research.finish()
    assert app.mode == "main" and app.tab == "research" and app.research_view == "experiment"
    assert app.project_state["binding"] == selected


def test_ui_manual_attachment_explicitly_detaches_run_after_revalidation(app, run):
    manual = run.parent.parent / "independent.jsonl"
    json_file(manual, {"t": 3, "metrics": {"real": 8}})
    U.open_connect(app, str(manual))
    app.research.finish()
    assert not app.connect_data_state["declared"]
    U.run_command(app, ["connectdata", "action", "attach"])
    assert app.project_state["binding"] is not None
    app.research.finish()
    assert app.project_state["binding"] is None
    assert app.research.settings["metrics_file"] == str(manual)


def test_closed_and_stale_read_results_cannot_reopen_or_attach(app):
    U.open_connect(app)
    U.handle_key(app, "esc")
    app.research.finish()
    assert app.mode == "main" and app.connect_data_state["result"] is None
    U.open_connect(app)
    app.selected_id = "another-job"
    app.research.finish()
    assert "changed" in app.connect_data_state["message"]
    assert app.connect_data_state["result"] is None


def test_remote_relative_path_cannot_use_local_files(app, monkeypatch):
    app.files = SimpleNamespace(remote=True)
    monkeypatch.setattr(C, "inspect", lambda *a, **k: pytest.fail("local fallback"))
    U.open_connect(app, "metrics.jsonl")
    assert app.research.pending is None
    assert "absolute path" in app.connect_data_state["message"]


@pytest.mark.parametrize("ascii_", [True, False])
@pytest.mark.parametrize("width,height", [(24, 7), (80, 24)])
def test_preview_keyboard_mouse_and_narrow_render_remain_read_only(app, ascii_, width, height, monkeypatch):
    U.open_connect(app)
    app.research.finish()
    app.width, app.height = width, height
    monkeypatch.setattr(C, "inspect", lambda *a, **k: pytest.fail("paint triggered read"))
    U.run_command(app, ["connectdata", "action", "preview"])
    views = SimpleNamespace(g=L.Glyphs(ascii_))
    for _ in range(5):
        rows = U.overlay(views, {}, app, width, height)
        assert len(rows) <= height and all(x + L.vlen(L.row_text(row)) <= width for _, x, row in rows)
        U.handle_key(app, "tab")
    assert app.research.pending is None


def test_field_paste_snippet_does_not_execute_or_rewrite_source(app, run):
    U.open_connect(app)
    app.research.finish()
    U.run_command(app, ["connectdata", "action", "snippet"])
    U.run_command(app, ["connectdata", "action", "edit:metric"])
    U.handle_key(app, "ctrl-u")
    U.paste(app, "accuracy")
    U.handle_key(app, "enter")
    text = C.snippet(app.connect_data_state["language"], app.connect_data_state["metric"])
    assert "accuracy" in text and app.research.pending is None
    assert C.validate_metrics(str(run / "metrics.jsonl"))["records"] == 2


@pytest.mark.parametrize("language", ["bash", "python"])
def test_generated_snippet_runs_in_real_portable_launch_and_produces_preview(tmp_path, language):
    from raven.launch_wizard import review, submit
    name = "accuracy'$(echo literal)"
    if language == "bash":
        program = C.snippet(language, name) + "\nreport_measurement 0.75\n"
    else:
        python_file = tmp_path / "program.py"
        python_file.write_text(C.snippet(language, name) + "\nreport_measurement(0.75, step=2)\n")
        program = shlex.join([sys.executable, str(python_file)]) + "\n"
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\nset -eu\n" + program)
    draft = {"project": str(tmp_path), "script": "job.sh", "execution": "local", "basic_research": True}
    env = {"PATH": "/usr/bin:/bin"}
    reviewed = review(draft, environ=env, connection={"transport": "none"})
    result = submit(reviewed, draft, environ=env, connection={"transport": "none"})
    directory = Path(result["run_dir"])
    deadline = time.monotonic() + 10
    manifest = {}
    while time.monotonic() < deadline:
        try:
            manifest = json.loads((directory / "run.json").read_text())
            if manifest["state"] in ("COMPLETED", "FAILED"):
                break
        except FileNotFoundError:
            pass
        time.sleep(.02)
    assert manifest.get("state") == "COMPLETED", Path(result["logs"]["stderr"]).read_text()
    inspected = C.inspect(str(directory / "metrics.jsonl"), binding=binding(directory), declared=True)
    assert inspected["metrics"]["latest"] == {name: .75}
    assert inspected["readiness"]["scientific"]["status"] == "ready"


def test_optional_fields_are_conditional_and_saved_values_are_preserved(app):
    from raven import launch_setup_ui as S, data_export_ui as E
    state = E.initialize(app)
    state.update(setup_ready=True, setup_root="/project", setup_form={"features": [], "scientific_context": {"workers": 8}}, settings={}, error="", message="")
    S._fields(state)
    actions = {action for _, action in S.document(app)}
    assert "field:setup:workers" not in actions and "setup:adapter" not in actions
    S.activate(app, "setup:feature:scaling")
    S.activate(app, "setup:toggle:trace")
    actions = {action for _, action in S.document(app)}
    assert "field:setup:workers" in actions and "setup:adapter" in actions
    S.activate(app, "setup:feature:scaling")
    S.activate(app, "setup:toggle:trace")
    assert S._resolved(state)["scientific_context"]["workers"] == 8


def test_preview_extreme_finite_numbers_never_overflows():
    result = {"series": {"signed": [{"t": 1, "value": -1e308}, {"t": 2, "value": 1e308}]}}
    assert C.preview_lines(result, L.Glyphs(False))


def test_managed_light_reads_on_existing_background_worker_without_heavy(app):
    from raven.research import ResearchHub
    from raven.raven_execution import ExecutionRouter
    cfg = Config({"raven": {"managed": True, "mode": "light", "site": "desktop"}})
    hub = ResearchHub(cfg, files=app.files)
    hub.execution = ExecutionRouter(cfg["raven"], lambda: pytest.fail("Connect data requested Heavy"))
    app.research = hub
    try:
        U.open_connect(app)
        hub.future.result(timeout=5)
        hub.poll_task()
        result = app.connect_data_state["result"]
        assert result["metrics"]["latest"] == {"loss": .2}, app.connect_data_state["message"]
        assert not app.connect_data_state["busy"]
    finally:
        hub.close()


def test_stale_mouse_control_cannot_attach_after_source_change(app):
    U.open_connect(app)
    app.research.finish()
    U.run_command(app, ["connectdata", "action", "preview"])
    state = app.connect_data_state
    choices = [action for _, action in U._document(app) if action]
    state.update(cursor=choices.index("attach"), follow_cursor=True)
    U.overlay(SimpleNamespace(g=L.Glyphs(False)), {}, app, 80, 24)
    y, _, control = next(hit for hit in state["control_hits"] if hit[2]["action"][1] == "connectdata action attach")
    app.selected_id = "different-job"
    U.handle_mouse(app, y, control["left"])
    assert app.research.pending is None and app.mode == "connect_data"


def test_real_f8_enter_and_mouse_controls_dispatch_shared_commands(app):
    from raven import interaction, workbench
    U.open_connect(app)
    app.research.finish()
    state = app.connect_data_state
    choices = [action for _, action in U._document(app) if action]
    state.update(cursor=choices.index("snippet"), follow_cursor=True)
    views = SimpleNamespace(g=L.Glyphs(False))
    rows = U.overlay(views, {}, app, 80, 24)
    interaction.publish(app, [[("", "")]]*app.height, [], 80, 24, overlays=rows)
    app.handle("f8")
    graph = interaction._current(app)
    target = next(control for control in graph.controls if control.action == ("command", "connectdata action snippet"))
    # F8 provides focus; tab to the actual published semantic action.
    for _ in graph.controls:
        if app.interaction_state["focused"] == target.id:
            break
        app.handle("tab")
    app.handle("enter")
    assert state["page"] == "snippet"
    rows = U.overlay(views, {}, app, 80, 24)
    hit = next(hit for hit in state["control_hits"] if hit[2]["action"][1] == "connectdata action language")
    y, _, control = hit
    language = state["language"]
    assert workbench.handle_mouse(app, y, control["left"])
    assert state["language"] != language


@pytest.mark.parametrize("finish", ["enter", "esc"])
def test_f8_field_editor_owns_enter_and_escape_after_repaint(app, finish):
    from raven import interaction
    U.open_connect(app)
    app.research.finish()
    state = app.connect_data_state
    views = SimpleNamespace(g=L.Glyphs(False))
    def paint():
        rows = U.overlay(views, {}, app, 80, 24)
        interaction.publish(app, [[("", "")]] * app.height, [], 80, 24, overlays=rows)
    paint()
    app.handle("f8")
    graph = interaction._current(app)
    target = next(control for control in graph.controls
                  if control.action == ("command", "connectdata action edit:path"))
    for _ in graph.controls:
        if app.interaction_state["focused"] == target.id:
            break
        app.handle("tab")
    app.handle("enter")
    assert state["editing"][0] == "path"
    before = state["path"]
    paint()
    app.handle(finish)
    assert state["editing"] is None and state["path"] == before
    assert not app.interaction_state["active"]
