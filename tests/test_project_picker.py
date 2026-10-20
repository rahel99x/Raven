"""Explicit attempts stay isolated and all redraws use published snapshots."""
import json
import os
from pathlib import Path
import shutil

import pytest

from raven import projects, project_ui, provenance
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text, vlen
from raven.model import Store
from raven.remote import LocalFiles
from raven.views import Views


def inventory(run_id="fit-a1", **values):
    return {"schema": "raven.run/v1", "run_id": run_id, "experiment_id": "fit", "attempt": 1, "state": "RUNNING", **values}


def make_run(root, run_id="fit-a1", **values):
    run = root / "runs" / run_id
    run.mkdir(parents=True, exist_ok=True)
    (run / "run.json").write_text(json.dumps(inventory(run_id, **values)))
    return run


def contract(root, outputs):
    path = root / ".raven" / "contracts" / "outputs.v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "outputs": outputs}))
    return path


class DeferredHub:
    def __init__(self):
        self.files = LocalFiles()
        self.settings = dict(metrics_file="", contract="", workdir="", passport="")
        self.passport = self.passport_diff = self.pending = None

    def configure(self, **values):
        self.settings.update(values)

    def start_task(self, worker, complete):
        if self.pending:
            return False
        self.pending = worker, complete
        return True

    def finish(self):
        worker, complete = self.pending
        self.pending = None
        try:
            value = worker()
        except Exception as exc:
            value = exc
        complete(value)


@pytest.fixture
def app():
    value = App(Store(persist=False), None, None, Config({}), "project-tester")
    value.research = DeferredHub()
    value.files = value.research.files
    project_ui.initialize(value)
    return value


def execute(app, args):
    assert project_ui.run_command(app, args)
    if app.research.pending:
        app.research.finish()


@pytest.mark.parametrize("change", [
    {"schema": "fake/v1"}, {"run_id": "../wrong"}, {"run_id": True}, {"experiment_id": ""}, {"attempt": True}, {"attempt": 0}, {"attempt": 2147483648},
    {"state": "SUCCESS"}, {"state": []}, {"paths": {"metrics": "../metrics.jsonl"}}, {"paths": {"metrics": "/private/file"}}, {"paths": {"metrics": "logs//a"}},
    {"paths": {"metrics": "logs/./a"}}, {"paths": {"metrics": "logs/*.log"}}, {"paths": {"metrics": "logs\\a"}}, {"paths": {"metrics": "a\x1bb"}},
    {"paths": {"made_up": "a"}}, {"paths": []}, {"provenance": {"script": "../bad"}}, {"provenance": {"script_sha256": "not-a-hash"}},
    {"provenance": {"git_commit": "main"}}, {"parameters": []}, {"metadata": []}, {"results": []}, {"start": True}, {"start": -1},
    {"start": float("inf")}, {"end": 253402300800}, {"input_size": -1}, {"resources": {"cpus": False}}, {"resources": {"gpus": -1}},
    {"resources": {"nodes": 0}}, {"resources": {"mem_bytes": 2.1}}, {"resources": {"mem_bytes": 0}}, {"resources": {"time_seconds": 0}},
    {"resources": {"time_seconds": 3162240001}}, {"resources": {"host_cpu_count": 4}}, {"job_id": "12\x1b34"}, {"surprise": "a"},
])
def test_inventory_rejects_invalid_schema(change):
    with pytest.raises(ValueError):
        projects.validate_inventory(inventory(**change))


def test_inventory_preserves_real_identity_unknown_allocation_and_parameters():
    value = inventory(job_id=None, resources={"gpus": 0, "cpus": None, "gpu_type": "", "time_seconds": 10.5}, parameters={"rate": -0.01, "groups": ["a", "b"]})
    assert projects.validate_inventory(value) is value
    assert value["resources"]["cpus"] is None


def test_project_discovery_only_reads_direct_attempts(tmp_path):
    make_run(tmp_path, "older", start=10)
    make_run(tmp_path, "newer", start=20)
    buried = tmp_path / "source" / "deep" / "runs"
    make_run(buried, "not-discovered")
    (tmp_path / "runs" / "loose.json").write_text("not an inventory")
    result = projects.discover_project(str(tmp_path))
    assert [row["run_id"] for row in result["runs"]] == ["newer", "older"]
    assert not result["limited"]


def test_empty_project_is_valid_and_explicit(tmp_path):
    result = projects.discover_project(str(tmp_path))
    assert result["runs"] == [] and "No runs directory" in result["summary"]


def test_invalid_attempt_does_not_hide_others(tmp_path):
    good = make_run(tmp_path, "good")
    bad = make_run(tmp_path, "bad")
    (bad / "run.json").write_text('{"schema":"raven.run/v1","run_id":"bad","run_id":"other"}')
    mismatch = make_run(tmp_path, "mismatch")
    (mismatch / "run.json").write_text(json.dumps(inventory("different")))
    result = projects.discover_project(str(tmp_path))
    assert [row["run_id"] for row in result["runs"]] == [good.name]
    assert any("duplicate JSON" in message for message in result["warnings"])
    assert any("must match" in message for message in result["warnings"])


def test_attempt_and_inventory_symlinks_are_refused(tmp_path):
    outside = tmp_path / "outside"
    make_run(outside, "target")
    root = tmp_path / "project"
    (root / "runs").mkdir(parents=True)
    (root / "runs" / "target").symlink_to(outside / "runs" / "target", target_is_directory=True)
    run = make_run(root, "linked")
    (run / "run.json").unlink()
    (run / "run.json").symlink_to(outside / "runs" / "target" / "run.json")
    result = projects.discover_project(str(root))
    assert not result["runs"] and len(result["warnings"]) == 2


def test_special_inventory_is_never_opened(tmp_path):
    run = make_run(tmp_path)
    (run / "run.json").unlink()
    os.mkfifo(run / "run.json")
    result = projects.discover_project(str(tmp_path))
    assert not result["runs"] and "regular file" in result["warnings"][0]


@pytest.mark.parametrize("nested", [False, True])
def test_selected_project_symlink_resolves_to_explicit_root(tmp_path, nested):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    selected = alias / "project" if nested else alias
    actual = real / "project" if nested else real
    make_run(actual, "selected")
    result = projects.discover_project(str(selected))
    assert result["root"] == str(actual)
    assert [row["run_id"] for row in result["runs"]] == ["selected"]


def test_selected_project_alias_retarget_does_not_change_pinned_read(tmp_path, monkeypatch):
    real = tmp_path / "real"
    outside = tmp_path / "outside"
    make_run(real, "selected")
    make_run(outside, "wrong")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    original = projects._root

    def retarget_after_pin(path):
        root, fd = original(path)
        alias.unlink()
        alias.symlink_to(outside, target_is_directory=True)
        return root, fd

    monkeypatch.setattr(projects, "_root", retarget_after_pin)
    result = projects.discover_project(str(alias))
    assert result["root"] == str(real)
    assert [row["run_id"] for row in result["runs"]] == ["selected"]


def test_inventory_size_and_attempt_limits_are_visible(tmp_path, monkeypatch):
    for name in ("one", "two", "three"):
        make_run(tmp_path, name)
    monkeypatch.setattr(projects, "MAX_RUNS", 2)
    result = projects.discover_project(str(tmp_path))
    assert len(result["runs"]) == 2 and result["limited"]
    monkeypatch.setattr(projects, "MAX_RUNS", 256)
    (tmp_path / "runs" / "one" / "run.json").write_bytes(b" " * (projects.MAX_INVENTORY_BYTES + 1))
    result = projects.discover_project(str(tmp_path))
    assert len(result["runs"]) == 2
    assert "byte limit" in result["warnings"][0]


def test_invalid_json_reads_still_count_toward_shared_budget(tmp_path, monkeypatch):
    for name in ("a", "b", "c"):
        make_run(tmp_path, name)
        (tmp_path / "runs" / name / "run.json").write_bytes(b"x" * projects.MAX_INVENTORY_BYTES)
    monkeypatch.setattr(projects, "MAX_INVENTORY_TOTAL", 2 * projects.MAX_INVENTORY_BYTES)
    result = projects.discover_project(str(tmp_path))
    assert result["bytes_read"] == 2 * projects.MAX_INVENTORY_BYTES
    assert result["limited"] and len(result["warnings"]) == 2


def test_read_refuses_in_place_replacement(tmp_path, monkeypatch):
    run = make_run(tmp_path)
    original = projects.os.read
    changed = False

    def race(fd, size):
        nonlocal changed
        data = original(fd, size)
        if not changed:
            changed = True
            temporary = run / "replacement.json"
            temporary.write_bytes(data)
            os.replace(temporary, run / "run.json")
        return data

    monkeypatch.setattr(projects.os, "read", race)
    result = projects.discover_project(str(tmp_path))
    assert not result["runs"] and "changed during inspection" in result["warnings"][0]


def test_read_detects_parent_directory_replacement(tmp_path, monkeypatch):
    run = make_run(tmp_path)
    original = projects.os.read
    changed = False

    def race(fd, size):
        nonlocal changed
        data = original(fd, size)
        if not changed:
            changed = True
            os.rename(run, run.with_name("old"))
            make_run(tmp_path)
        return data

    monkeypatch.setattr(projects.os, "read", race)
    result = projects.discover_project(str(tmp_path))
    assert not result["runs"] and "changed during inspection" in result["warnings"][0]


@pytest.mark.parametrize("reader", [type("Remote", (), {"remote": True})(), type("Custom", (), {"remote": False})()])
def test_project_reads_never_fall_back_from_other_backend(tmp_path, reader):
    with pytest.raises(ValueError, match="locally on CARC|unsupported SSH project reader"):
        projects.discover_project(str(tmp_path), files=reader)
    with pytest.raises(ValueError, match="locally on CARC|unsupported SSH project reader"):
        projects.artifact_tree("anything", str(tmp_path), files=reader)


def test_run_binding_matches_inventory_and_exact_sources(tmp_path):
    run = make_run(tmp_path, job_id="123_4", paths={"metrics": "metrics.jsonl", "log_index": "logs.json", "stdout": "logs/stdout.log", "passports": "passports"})
    (run / "logs").mkdir()
    (run / "logs" / "stdout.log").write_text("observed output\n")
    (run / "metrics.jsonl").write_text("{}\n")
    (run / "logs.json").write_text(json.dumps({"schema": "raven.logs/v1", "run_id": "fit-a1", "job_id": "123_4", "logs": [{"id": "worker", "path": "logs/worker.log"}]}))
    contract(tmp_path, [{"path": "run.json", "format": "json"}])
    result = projects.select_run(str(tmp_path), "fit-a1")
    assert result["binding"]["job_id"] == "123_4"
    assert result["binding"]["run_root"] == str(run)
    assert result["binding"]["metrics_file"] == str(run / "metrics.jsonl")
    assert result["binding"]["contract"] == str(tmp_path / ".raven" / "contracts" / "outputs.v1.json")
    assert [entry["path"] for entry in result["logs"]] == [str(run / "logs/stdout.log"), str(run / "logs/worker.log")]
    assert result["binding"]["passport"] == ""


def test_run_does_not_guess_undeclared_paths_or_job_identity(tmp_path):
    make_run(tmp_path)
    result = projects.select_run(str(tmp_path), "fit-a1")
    assert result["binding"]["job_id"] is None
    assert result["binding"]["metrics_file"] == result["binding"]["log_manifest"] == ""
    assert not result["logs"]


@pytest.mark.parametrize("claimed", [{"run_id": "different"}, {"job_id": "999"}])
def test_mismatched_log_inventory_cannot_bind(tmp_path, claimed):
    run = make_run(tmp_path, job_id="123", paths={"log_index": "logs.json"})
    (run / "logs.json").write_text(json.dumps({"schema": "raven.logs/v1", "logs": [{"id": "x", "path": "x.log"}], **claimed}))
    result = projects.select_run(str(tmp_path), "fit-a1")
    assert result["binding"]["log_manifest"] == "" and not result["logs"]
    assert any("does not match" in message for message in result["warnings"])


def test_native_external_logs_preserve_explicit_locations_and_symlinks_refused(tmp_path):
    root = tmp_path / "project"
    outside = tmp_path / "outside.log"
    outside.write_text("private\n")
    run = make_run(root, paths={"log_index": "logs.json", "metrics": "linked"})
    (run / "linked").symlink_to(outside)
    (run / "logs.json").write_text(json.dumps({"schema": "raven.logs/v1", "logs": [{"id": "outside", "path": str(outside)}, {"id": "link", "path": "linked"}]}))
    result = projects.select_run(str(root), "fit-a1")
    assert [entry["path"] for entry in result["logs"]] == [str(outside)]
    assert result["logs"][0]["external"] is True
    assert result["binding"]["metrics_file"] == ""
    assert any("External log declaration" in message for message in result["warnings"])
    assert any("unsafe declaration refused" in message for message in result["warnings"])


def test_native_sibling_log_declaration_is_preserved(tmp_path):
    run = make_run(tmp_path, paths={"log_index": "logs.json"})
    shared = run.parent / "shared"
    shared.mkdir()
    (shared / "driver.log").write_text("actual shared output")
    (run / "logs.json").write_text(json.dumps({"schema": "raven.logs/v1", "logs": [{"id": "driver", "path": "../shared/driver.log"}]}))
    result = projects.select_run(str(tmp_path), "fit-a1")
    assert [entry["path"] for entry in result["logs"]] == [str(shared / "driver.log")]
    assert result["logs"][0]["external"]


def test_unique_native_passport_binds_and_wrong_job_is_refused(tmp_path):
    run = make_run(tmp_path, job_id="123", paths={"passports": "passports"})
    directory = run / "passports"
    directory.mkdir()
    native = provenance.capture(run, job_id="123")
    (directory / "actual.json").write_text(json.dumps(native))
    result = projects.select_run(str(tmp_path), "fit-a1")
    assert result["binding"]["passport"] == str(directory / "actual.json")
    native = provenance.capture(run, job_id="999")
    (directory / "actual.json").write_text(json.dumps(native))
    result = projects.select_run(str(tmp_path), "fit-a1")
    assert result["binding"]["passport"] == ""
    assert any("job identity" in message for message in result["warnings"])


def test_multiple_passports_need_explicit_selection(tmp_path):
    run = make_run(tmp_path, paths={"passports": "passports"})
    directory = run / "passports"
    directory.mkdir()
    for name in ("one", "two"):
        (directory / (name + ".json")).write_text("{}")
    result = projects.select_run(str(tmp_path), "fit-a1")
    assert not result["binding"]["passport"]
    assert any("Multiple passports" in message for message in result["warnings"])


def test_declared_output_tree_distinguishes_missing_optional_and_invalid(tmp_path):
    run = make_run(tmp_path)
    (run / "outputs").mkdir()
    (run / "outputs" / "good.json").write_text('{"value":1}')
    (run / "outputs" / "bad.json").write_text("broken")
    path = contract(tmp_path, [{"path": "outputs/good.json", "format": "json"}, {"path": "outputs/bad.json", "format": "json"}, {"path": "required.csv"}, {"path": "optional.csv", "required": False}])
    tree = projects.artifact_tree(str(path), str(run))
    statuses = {node["path"]: node["status"] for node in tree["nodes"]}
    assert statuses == {"outputs": "directory", "outputs/good.json": "valid", "outputs/bad.json": "invalid", "required.csv": "missing", "optional.csv": "missing"}


def test_tree_validation_budget_is_explicit(tmp_path):
    run = make_run(tmp_path)
    (run / "big.txt").write_bytes(b"a" * (9 * 1024 * 1024))
    path = contract(tmp_path, [{"path": "big.txt", "format": "text"}])
    tree = projects.artifact_tree(str(path), str(run))
    assert tree["nodes"][0]["status"] == "incomplete"
    assert tree["validation"]["budget"]["bytes_read"] == 0


@pytest.mark.parametrize("fmt,data,needle", [("text", "plain\nnext", "plain"), ("json", '{"value":1}', '"value": 1'), ("csv", "name,value\na,1\n", "name | value")])
def test_bounded_preview_formats(tmp_path, fmt, data, needle):
    (tmp_path / "result").write_text(data)
    result = projects.preview_artifact(str(tmp_path), {"path": "result", "format": fmt})
    assert result["status"] == "ready" and not result["truncated"]
    assert needle in "\n".join(result["lines"])


def test_preview_bounds_bytes_rows_and_split_utf8(tmp_path):
    source = tmp_path / "large.txt"
    source.write_bytes(b"a" * (projects.MAX_PREVIEW_BYTES - 1) + "界".encode() + b"tail")
    result = projects.preview_artifact(str(tmp_path), {"path": source.name})
    assert result["status"] == "ready" and result["truncated"] and result["bytes_read"] == projects.MAX_PREVIEW_BYTES
    source.write_text("line\n" * 300)
    result = projects.preview_artifact(str(tmp_path), {"path": source.name})
    assert len(result["lines"]) == projects.MAX_PREVIEW_LINES and result["truncated"]


@pytest.mark.parametrize("data", [b"\xff\xfe", b"valid\x00binary"])
def test_binary_preview_is_explicitly_unsupported(tmp_path, data):
    (tmp_path / "binary").write_bytes(data)
    result = projects.preview_artifact(str(tmp_path), {"path": "binary"})
    assert result["status"] == "unsupported" and not result["lines"]


def test_previews_refuse_symlink_parent_and_fifo(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data").write_text("private")
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    os.mkfifo(tmp_path / "pipe")
    for path in ("link/data", "pipe"):
        with pytest.raises(ValueError):
            projects.preview_artifact(str(tmp_path), {"path": path})


def test_project_command_reads_only_on_worker_and_redraw_is_cached(app, tmp_path, monkeypatch):
    make_run(tmp_path)
    calls = []
    original = projects.discover_project
    monkeypatch.setattr(projects, "discover_project", lambda *a, **kw: (calls.append(a), original(*a, **kw))[1])
    assert project_ui.run_command(app, ["project", str(tmp_path)])
    assert calls == [] and app.project_state["busy"]
    views = Views(Glyphs(False), app.cfg)
    for _ in range(10):
        project_ui.overlay(views, app.store.snapshot(), app, 120, 30)
    assert calls == []
    app.research.finish()
    assert len(calls) == 1 and not app.project_state["busy"]


def test_keyboard_run_selection_clears_unrelated_job_and_binds_one_attempt(app, tmp_path):
    make_run(tmp_path, "one", paths={"metrics": "metrics.jsonl"}, job_id="123")
    make_run(tmp_path, "two", paths={"metrics": "metrics.jsonl"})
    app.selected_id, app.research_job_id = "unrelated", "unrelated"
    execute(app, ["project", str(tmp_path)])
    execute(app, ["run", "select", "one"])
    assert app.research_job_id == app.selected_id == "123"
    assert app.research.settings["workdir"] == str(tmp_path / "runs/one")
    execute(app, ["runs"])
    assert project_ui.handle_key(app, "home")
    assert app.project_state["runs"][0]["run_id"] == "two"
    project_ui.handle_key(app, "enter")
    app.research.finish()
    assert app.selected_id is None and app.research_job_id is None and app.log_job is None
    assert project_ui.selected_binding(app)["run_id"] == "two"
    assert app.research.settings["metrics_file"] == str(tmp_path / "runs/two/metrics.jsonl")


def test_run_clear_restores_actual_prior_paths_and_keeps_inventory(app, tmp_path):
    make_run(tmp_path, paths={"metrics": "metrics.jsonl"})
    original = dict(metrics_file="/actual/manual/metrics", contract="/actual/manual/contract", workdir="/actual/manual/workdir", passport="")
    app.research.settings.update(original)
    app.research.passport = {"actual cached": "record"}
    app.cfg["logs"]["manifest_file"] = "/actual/manual/logs.json"
    execute(app, ["project", str(tmp_path)])
    execute(app, ["run", "select", "fit-a1"])
    execute(app, ["run", "clear"])
    assert app.research.settings == original
    assert app.research.passport == {"actual cached": "record"}
    assert app.cfg["logs"]["manifest_file"] == "/actual/manual/logs.json"
    assert project_ui.selected_binding(app) is None and len(app.project_state["runs"]) == 1


def test_cached_stream_resolution_never_substitutes_stdout_for_missing_stderr(app, tmp_path):
    run = make_run(tmp_path, paths={"stdout": "stdout.log"})
    (run / "stdout.log").write_text("exact stdout")
    execute(app, ["project", str(tmp_path)])
    execute(app, ["run", "select", "fit-a1"])
    assert project_ui.resolve_log_entry(app)["path"] == str(run / "stdout.log")
    app.logs.which = "err"
    assert project_ui.resolve_log_entry(app) is None


def test_explicit_stderr_browser_entry_synchronizes_toggle_intent(app, tmp_path):
    run = make_run(tmp_path, paths={"stdout": "stdout.log", "stderr": "stderr.log"})
    for name in ("stdout.log", "stderr.log"):
        (run / name).write_text("observed")
    execute(app, ["project", str(tmp_path)])
    execute(app, ["run", "select", "fit-a1"])
    app.logs.entry = next(entry for entry in project_ui.log_entries(app) if entry["role"] == "stderr")
    assert app.logs.which == "out"
    assert project_ui.resolve_log_entry(app)["path"] == str(run / "stderr.log")
    assert app.logs.which == "err"


def test_explicit_clear_invalidates_pending_run_selection(app, tmp_path):
    make_run(tmp_path)
    execute(app, ["project", str(tmp_path)])
    project_ui.run_command(app, ["run", "select", "fit-a1"])
    project_ui.clear_binding(app)
    app.selected_id = "manual-new-job"
    app.research.finish()
    assert project_ui.selected_binding(app) is None and app.selected_id == "manual-new-job"


@pytest.mark.parametrize("destination", ["main", "setup", "project_runs"])
def test_closed_run_home_request_does_not_reopen_or_replace_selection(app, tmp_path, destination):
    make_run(tmp_path)
    execute(app, ["project", str(tmp_path)])
    app.selected_id = "other-job"
    assert project_ui.run_command(app, ["run", "home", "fit-a1"])
    assert app.research.pending and app.mode == "project_runs"
    assert project_ui.handle_key(app, "esc")
    app.mode = destination
    app.research.finish()
    assert app.mode == destination and app.selected_id == "other-job"
    assert project_ui.selected_binding(app) is None
    assert app.research.pending is None


def test_run_picker_opens_home_only_after_exact_binding_is_read(app, tmp_path):
    make_run(tmp_path)
    execute(app, ["project", str(tmp_path)])
    assert project_ui.run_command(app, ["run", "home", "fit-a1"])
    assert app.mode == "project_runs" and project_ui.selected_binding(app) is None
    app.research.finish()
    assert app.mode == "run_home"
    assert app.run_home_state["binding"]["run_id"] == "fit-a1"
    assert project_ui.selected_binding(app)["run_id"] == "fit-a1"


def test_restored_project_requires_revalidation_before_binding(app, tmp_path):
    project_ui.restore(app, {"project_root": str(tmp_path), "run_id": "fit-a1"})
    assert app.research.pending is None and project_ui.selected_binding(app) is None
    assert project_ui.save(app) == {"project_root": str(tmp_path), "run_id": "fit-a1"}


def test_artifact_keyboard_expand_preview_and_return_same_cursor(app, tmp_path):
    run = make_run(tmp_path)
    (run / "outputs").mkdir()
    (run / "outputs" / "result.json").write_text('{"observed": true}')
    path = contract(tmp_path, [{"path": "outputs/result.json", "format": "json"}])
    app.research.settings.update(contract=str(path), workdir=str(run))
    execute(app, ["outputs"])
    assert app.mode == "project_outputs"
    project_ui.handle_key(app, "enter")
    assert app.project_state["collapsed"] == ["outputs"]
    project_ui.handle_key(app, "enter")
    project_ui.handle_key(app, "down")
    project_ui.handle_key(app, "enter")
    app.research.finish()
    assert app.mode == "project_preview" and '"observed": true' in "\n".join(app.project_state["preview"]["lines"])
    project_ui.handle_key(app, "esc")
    assert app.mode == "project_outputs" and app.project_state["output_cursor"] == 1
    project_ui.handle_key(app, "left")
    assert app.project_state["output_cursor"] == 0
    assert app.project_state["collapsed"] == ["outputs"]
    project_ui.handle_key(app, "/")
    for key in "result":
        project_ui.handle_key(app, key)
    project_ui.handle_key(app, "enter")
    assert [node["path"] for node in project_ui._visible_outputs(app.project_state)] == ["outputs/result.json"]


def test_failed_project_change_preserves_previous_binding_and_sources(app, tmp_path):
    make_run(tmp_path, paths={"metrics": "metrics.jsonl"})
    execute(app, ["project", str(tmp_path)])
    execute(app, ["run", "select", "fit-a1"])
    binding = project_ui.selected_binding(app)
    settings = dict(app.research.settings)
    execute(app, ["project", str(tmp_path / "missing-project")])
    assert project_ui.selected_binding(app) is binding
    assert app.research.settings == settings
    assert "No such file" in app.message


def test_change_project_restores_prior_manual_context(app, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    make_run(first)
    make_run(second)
    app.research.settings["metrics_file"] = "/explicit/prior/metrics"
    execute(app, ["project", str(first)])
    execute(app, ["run", "select", "fit-a1"])
    execute(app, ["project", str(second)])
    assert project_ui.selected_binding(app) is None
    assert app.research.settings["metrics_file"] == "/explicit/prior/metrics"
    assert app.project_state["root"] == str(second)


def test_output_filter_and_notices_are_keyboard_scrollable(app, tmp_path):
    for name in ("one", "two"):
        make_run(tmp_path, name)
    execute(app, ["project", str(tmp_path)])
    project_ui.handle_key(app, "/")
    for key in "one":
        project_ui.handle_key(app, key)
    project_ui.handle_key(app, "enter")
    assert [run["run_id"] for run in project_ui._visible_runs(app.project_state)] == ["one"]
    app.project_state["warnings"] = [f"Notice {index}" for index in range(20)]
    project_ui.handle_key(app, "!")
    project_ui.handle_key(app, "end")
    views = Views(Glyphs(True), app.cfg)
    text = "\n".join(row_text(row) for _, _, row in project_ui.overlay(views, app.store.snapshot(), app, 80, 20))
    assert "Notice 19" in text
    project_ui.handle_key(app, "esc")
    assert not app.project_state["notices_open"] and app.mode == "project_runs"


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width,height", [(1, 1), (3, 2), (20, 7), (40, 12), (120, 30)])
def test_picker_and_explorer_all_sizes_without_io(app, tmp_path, monkeypatch, ascii_, width, height):
    run = make_run(tmp_path)
    (run / "data.txt").write_text("observed\n")
    path = contract(tmp_path, [{"path": "data.txt", "format": "text"}])
    execute(app, ["project", str(tmp_path)])
    execute(app, ["run", "select", "fit-a1"])
    execute(app, ["outputs"])
    app.project_state["preview"] = projects.preview_artifact(str(run), {"path": "data.txt"})
    monkeypatch.setattr(projects, "_read", lambda *a, **kw: pytest.fail("redraw performed filesystem read"))
    views = Views(Glyphs(ascii_), app.cfg)
    for mode in project_ui.MODES:
        app.mode = mode
        for y, x, row in project_ui.overlay(views, app.store.snapshot(), app, width, height):
            assert 0 <= y < height and 0 <= x < width
            assert vlen(row_text(row)) + x <= width
            if ascii_:
                assert row_text(row).isascii()


def test_busy_worker_is_bounded_and_unknown_command_is_unhandled(app, tmp_path):
    make_run(tmp_path)
    project_ui.run_command(app, ["project", str(tmp_path)])
    worker = app.research.pending
    project_ui.run_command(app, ["project", str(tmp_path)])
    assert app.research.pending is worker and "already running" in app.message
    assert not project_ui.run_command(app, ["unrelated"])


def test_real_portable_template_run_is_consumed(tmp_path):
    template = Path(__file__).resolve().parents[1] / "examples/project-template"
    root = tmp_path / "portable project"
    shutil.copytree(template, root)
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-S", str(root / "experiment.py"), "--run-id", "actual", "--steps", "2", "--terms-per-step", "16"], cwd=root, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    found = projects.discover_project(str(root))
    assert found["runs"][0]["run_id"] == "actual" and found["runs"][0]["state"] == "COMPLETED"
    bound = projects.select_run(str(root), "actual")
    assert bound["binding"]["metrics_file"] == str(root / "runs/actual/metrics.jsonl")
    assert bound["binding"]["job_id"] is None
    assert bound["logs"]
    tree = projects.artifact_tree(bound["binding"]["contract"], bound["binding"]["run_root"])
    assert tree["status"] == "valid"
    spec = next(node["specification"] for node in tree["nodes"] if node["path"] == "outputs/results.json")
    preview = projects.preview_artifact(bound["binding"]["run_root"], spec)
    assert "estimated_pi" in "\n".join(preview["lines"])
