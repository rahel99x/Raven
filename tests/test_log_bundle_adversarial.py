"""Cross-job ownership and late worker events in bulk historical log exports."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace

import pytest

from raven import history_log_export, log_bundle
from raven.cli import Session
from raven.model import Finished


@pytest.fixture
def shared_project(tmp_path):
    root = tmp_path / "project"
    shutil.copytree(Path(__file__).resolve().parents[1] / "examples/project-template", root)
    spec = importlib.util.spec_from_file_location("adversarial_bundle_reporting", root / "reporting.py")
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    run = producer.begin_run(root, "attempt-1", job_id="8", name="project job", script="experiment.py")
    for name in ("stdout", "stderr"):
        (run / "logs" / (name + ".log")).write_bytes((name + " from project\n").encode())
    shared = tmp_path / "shared-output.log"
    shared.write_bytes(b"shared original content\r\n")
    producer.register_log(run, "shared", shared, label="Shared worker")
    records = [Finished("7", workdir=str(tmp_path)), Finished("8", workdir=str(run))]
    details = {jid: {"JobId": jid, "StdOut": str(shared), "StdErr": str(shared),
                     "WorkDir": record.workdir} for jid, record in zip(("7", "8"), records)}
    request = log_bundle.capture_jobs(["7", "8"], {"finished": records, "details": details},
                                      registered_root=str(root))
    report = log_bundle.discover_logs(request)
    entry = next(item for item in report["entries"] if item["path"] == str(shared))
    assert entry["job_ids"] == ["7", "8"]
    return SimpleNamespace(root=root, run=run, producer=producer, shared=shared, report=report)


@pytest.mark.parametrize("change", ["job", "attempt", "declaration"])
def test_deduplicated_scheduler_source_revalidates_later_project_association(shared_project, tmp_path, change):
    p = shared_project
    if change in ("job", "attempt"):
        inventory = json.loads((p.run / "run.json").read_text())
        inventory["job_id" if change == "job" else "attempt"] = "9" if change == "job" else 99
        (p.run / "run.json").write_text(json.dumps(inventory))
    else:
        manifest = json.loads((p.run / "logs.json").read_text())
        manifest["logs"] = [item for item in manifest["logs"] if item["id"] != "shared"]
        (p.run / "logs.json").write_text(json.dumps(manifest))
    result = log_bundle.export_logs(p.report, str(tmp_path), root=str(tmp_path))
    # A file shared with a scheduler stream cannot validate a stale project
    # declaration merely because that scheduler stream was discovered first.
    copied = [item for item in result["files"] if item["source_path"] == str(p.shared)]
    assert not any("8" in item["job_ids"] for item in copied), result
    assert any(item["job_id"] == "8" and item["path"] == str(p.shared)
               for item in result["missing"]), result


def test_deduplicated_source_retains_original_bytes_and_all_valid_associations(shared_project, tmp_path):
    p = shared_project
    result = log_bundle.export_logs(p.report, str(tmp_path), root=str(tmp_path))
    copied = next(item for item in result["files"] if item["source_path"] == str(p.shared))
    assert copied["job_ids"] == ["7", "8"]
    assert copied["sha256"] == hashlib.sha256(p.shared.read_bytes()).hexdigest()
    assert (Path(result["export_path"]) / copied["relative_path"]).read_bytes() == p.shared.read_bytes()
    manifest = json.loads((Path(result["export_path"]) / "manifest.json").read_text())
    assert next(item for item in manifest["files"] if item["source_path"] == str(p.shared))["job_ids"] == ["7", "8"]


def _report(tmp_path):
    paths = [tmp_path / "17.out", tmp_path / "17.err"]
    for source in paths:
        source.write_bytes(("complete " + source.name + "\r\n").encode())
    record = Finished("17", workdir=str(tmp_path))
    request = log_bundle.capture_jobs(["17"], {"finished": [record], "details": {
        "17": {"JobId": "17", "StdOut": str(paths[0]), "StdErr": str(paths[1]), "WorkDir": str(tmp_path)}}})
    return log_bundle.discover_logs(request), paths


def test_unavailable_later_file_leaves_truthful_saved_manifest_and_ui_alert(tmp_path):
    report, paths = _report(tmp_path)
    paths[1].unlink()
    result = log_bundle.export_logs(report, str(tmp_path), root=str(tmp_path))
    assert result["status"] == "partial" and result["export_path"]
    manifest = json.loads((Path(result["export_path"]) / "manifest.json").read_text())
    assert manifest["missing"] == result["missing"]
    assert manifest["missing"][0]["job_id"] == "17"
    assert manifest["missing"][0]["path"] == str(paths[1])
    app = SimpleNamespace(mode="history_log_missing")
    state = history_log_export.initialize(app)
    state.update(stage="missing", result=result, report=result)
    displayed = "\n".join(history_log_export._details(app))
    assert result["export_path"] in displayed
    assert str(paths[1]) in displayed and "Job 17:" in displayed
    assert "No bundle was copied" not in displayed


class ControlledHub:
    """Hold completion until the test makes a deliberate UI state change."""

    def __init__(self):
        self.lock = threading.RLock()
        self.closed = False
        self.pending = None
        self.future = None
        self.completion = None
        self.work = None

    def start_task(self, work, completion):
        self.work, self.completion = work, completion
        self.pending = (work, completion)
        return True

    def finish(self, result):
        callback = self.completion
        self.pending = None
        callback(result)

    def close(self):
        self.closed = True


def _modal_app(tmp_path):
    messages = []
    app = SimpleNamespace(mode="main", tab="history", selected_id="17", marks=set(),
                          cfg={}, state_dir=str(tmp_path / "state"), project_state={},
                          history_all_records=[Finished("17")], research=ControlledHub(),
                          store=SimpleNamespace(snapshot=lambda: {}), say=messages.append,
                          fail=messages.append, width=80, height=24)
    assert history_log_export.open_menu(app)
    return app, messages


@pytest.mark.parametrize("methods", [[], ["OSC 52 request sent"], ["test local clipboard"]])
def test_late_cancel_keeps_published_bundle_and_delivery_receipt(tmp_path, methods):
    app, messages = _modal_app(tmp_path)
    history_log_export._request_export(app, clipboard=True)
    state = history_log_export.initialize(app)
    assert state["callback"] is not None and state["cancel"] is not None
    history_log_export.cancel(app)
    result = {"status": "partial", "message": "Saved complete original logs", "missing": [],
              "export_path": str(tmp_path / "saved-bundle"), "clipboard_path": str(tmp_path / "saved-bundle/all-logs.txt"),
              "clipboard": {"methods": methods, "warnings": ["Cancelled after publication"]}, "warnings": []}
    app.research.finish(result)
    assert state["stage"] == "receipt", (state, messages)
    assert state["result"]["export_path"] == result["export_path"]
    displayed = "\n".join(history_log_export._details(app))
    assert result["export_path"] in displayed
    for method in methods:
        assert method in displayed


def test_cancelled_worker_callback_cannot_reopen_or_change_new_modal(tmp_path):
    app, messages = _modal_app(tmp_path)
    history_log_export._request_export(app, clipboard=True)
    old_callback = app.research.completion
    history_log_export.cancel(app, close=True)
    assert app.mode == "main"
    app.selected_id = "18"
    app.history_all_records.append(Finished("18"))
    assert history_log_export.open_menu(app)
    state = history_log_export.initialize(app)
    token = state["token"]
    old_callback({"status": "ready", "export_path": "/stale/path", "message": "old result"})
    assert state["token"] == token and state["jobs"] == ("18",)
    assert state["stage"] == "menu" and state["result"] is None
    assert "old result" not in messages


def test_rename_of_projects_root_cleans_held_destination_and_keeps_replacement(tmp_path):
    report, paths = _report(tmp_path)
    root = tmp_path / "projects"
    root.mkdir()
    destination = root / "sub"
    destination.mkdir()
    moved = tmp_path / "old-projects"
    changed = False

    def replace_root(done, total):
        nonlocal changed
        if done and not changed:
            changed = True
            root.rename(moved)
            root.mkdir()
            (root / "sub").mkdir()
            (root / "sub/keep.txt").write_bytes(b"new destination must be preserved")

    result = log_bundle.export_logs(report, str(destination), root=str(root), progress=replace_root)
    assert result["status"] == "error" and not result["export_path"]
    assert (root / "sub/keep.txt").read_bytes() == b"new destination must be preserved"
    assert not list((moved / "sub").iterdir())
    assert all(path.exists() for path in paths)


@pytest.mark.parametrize("linked", ["state", "ancestor", "exports"])
def test_rejected_clipboard_staging_symlink_never_creates_target_directories(tmp_path, linked):
    report, paths = _report(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    if linked == "state":
        state = tmp_path / "state"
        state.symlink_to(outside, target_is_directory=True)
    elif linked == "ancestor":
        parent = tmp_path / "linked-parent"
        parent.symlink_to(outside, target_is_directory=True)
        state = parent / "state"
    else:
        state = tmp_path / "state"
        state.mkdir()
        (state / "exports").symlink_to(outside, target_is_directory=True)
    result = log_bundle.export_logs(report, state_dir=str(state), clipboard=True,
                                   use_osc52=False, use_tools=False)
    assert result["status"] == "error" and not result["export_path"]
    assert not list(outside.iterdir()), result
    assert all(path.exists() for path in paths)


def test_session_shutdown_cancels_bulk_worker_before_any_log_io_or_clipboard(tmp_path, monkeypatch):
    app, messages = _modal_app(tmp_path)
    history_log_export._request_export(app, clipboard=True)
    work = app.research.work
    cancel_event = history_log_export.initialize(app)["cancel"]
    order = []

    def save():
        assert cancel_event.is_set(), "Export must stop before saving/closing the session"
        order.append("save")

    def close_hub():
        assert cancel_event.is_set(), "Closing the executor alone cannot cancel a running export"
        app.research.closed = True
        order.append("hub-close")

    app.save = save
    app.research.close = close_hub
    app.logs = SimpleNamespace(catalog=None)
    session = Session("tester", None, SimpleNamespace(shutdown=lambda: None),
                      None, None, app, SimpleNamespace(), None, None)
    session.close()
    assert order == ["save", "hub-close"]
    monkeypatch.setattr(log_bundle, "_job_details", lambda *args: pytest.fail("closed export must not discover source metadata"))
    monkeypatch.setattr(log_bundle.clipboard_io, "copy_file", lambda *args, **kwargs: pytest.fail("closed export must not deliver clipboard"))
    result = work()
    outcome = result.get("discovery", result)
    assert outcome["status"] == "cancelled", result
    assert not result.get("export_path")
