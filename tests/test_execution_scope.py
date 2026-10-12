"""Receipts cannot consume another profile/owner's reused numeric scheduler ID."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven import execution_ui, orchestrator
from raven.model import Finished


class Scheduler:
    def __init__(self):
        self.user = "alice"
        self.b = SimpleNamespace()
        self.calls = []
        self.info_calls = []

    def submit(self, argv, workdir):
        self.calls.append((list(argv), workdir))
        return True, str(100 + len(self.calls)), str(100 + len(self.calls))

    def submit_info(self, job_id):
        self.info_calls.append(job_id)
        return {}


class Worker:
    def __init__(self):
        self.pending = None

    def start_task(self, function, completion):
        if self.pending:
            return False
        self.pending = (function, completion)
        return True

    def complete(self):
        function, completion = self.pending
        self.pending = None
        try:
            value = function()
        except Exception as exc:
            value = exc
        completion(value)


@pytest.fixture
def app(tmp_path, monkeypatch):
    for name in os.environ:
        if name.startswith("SBATCH_"):
            monkeypatch.delenv(name)
    messages = []
    cfg = {"host": "", "cluster_name": "carc-test"}
    result = SimpleNamespace(user="alice", profile_name="carc", cfg=cfg, mode="main", interactive=True,
                             research=Worker(), replay=None, files=SimpleNamespace(remote=False), sampler=None,
                             actions=SimpleNamespace(slurm=Scheduler()), state_dir=str(tmp_path / "state"),
                             store=SimpleNamespace(snapshot=lambda: {"finished": [Finished("101", state="COMPLETED", elapsed="00:00:10")]}),
                             say=lambda message: messages.append((True, message)), fail=lambda message: messages.append((False, message)),
                             messages=messages)
    execution_ui.initialize(result)
    return result


@pytest.fixture
def recipe(tmp_path):
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    return {"version": 1, "kind": "raven.workflow", "nodes": [
        {"id": "parent", "script": str(script), "resources": {"cpus_per_task": 1}},
        {"id": "child", "script": str(script), "resources": {"cpus_per_task": 1}, "depends_on": ["parent"]},
    ]}


def receipt(app, recipe, tmp_path, *, cancel_after_parent=False):
    review = orchestrator.prepare_review("workflow", recipe, workdir=tmp_path, scope=execution_ui.connection_scope(app))
    scheduler = app.actions.slurm
    return orchestrator.execute(review, scheduler, app.state_dir, confirmed=True,
                                cancel=(lambda: bool(scheduler.calls)) if cancel_after_parent else None,
                                expected_scope=execution_ui.connection_scope(app))


def test_scope_uses_known_configuration_without_scheduler_queries(app, monkeypatch):
    monkeypatch.setenv("SLURM_CLUSTER_NAME", "environment-cluster")
    scope = execution_ui.connection_scope(app)
    assert scope["profile"] == "carc" and scope["user"] == "alice"
    assert scope["cluster"] == "carc-test" and scope["configured_host"] == ""
    assert scope["connection_host"] and scope["backend"] == "types.SimpleNamespace"
    assert not app.actions.slurm.calls and not app.actions.slurm.info_calls


def test_recording_wrappers_do_not_change_original_backend_identity(app):
    original = execution_ui.connection_scope(app)
    app.actions.slurm.b = SimpleNamespace(inner=app.actions.slurm.b)
    assert execution_ui.connection_scope(app) == original


def test_cluster_environment_is_used_only_when_already_known(app, monkeypatch):
    app.cfg.pop("cluster_name")
    monkeypatch.setenv("SLURM_CLUSTER_NAME", "known-from-login")
    assert execution_ui.connection_scope(app)["cluster"] == "known-from-login"
    assert not app.actions.slurm.calls and not app.actions.slurm.info_calls


def test_profile_switch_cannot_collect_another_clusters_reused_job_id(app, recipe, tmp_path):
    saved = receipt(app, recipe, tmp_path)
    state = app.execution_state
    state.update(receipt=saved, receipt_path=saved["receipt_path"], review=saved["review"])
    before = Path(saved["receipt_path"]).read_bytes()
    app.profile_name = "different-cluster"
    # Its cached scheduler snapshot contains ID 101 too, but belongs elsewhere.
    execution_ui.run_command(app, ["execution", "collect"])
    assert not app.research.pending
    assert Path(saved["receipt_path"]).read_bytes() == before
    assert "scope differs" in app.messages[-1][1]
    assert len(app.actions.slurm.calls) == 2


@pytest.mark.parametrize("action", [["resume"], ["retry", "parent"], ["recover", "parent", "777"]])
def test_ui_mutating_commands_refuse_receipts_after_profile_switch(app, recipe, tmp_path, action):
    saved = receipt(app, recipe, tmp_path, cancel_after_parent=True)
    app.execution_state.update(receipt=saved, receipt_path=saved["receipt_path"], review=saved["review"])
    app.profile_name = "other-site"
    execution_ui.run_command(app, ["execution", *action])
    assert app.mode == "main" and not app.research.pending
    assert "scope differs" in app.messages[-1][1]
    assert len(app.actions.slurm.calls) == 1 and not app.actions.slurm.info_calls


@pytest.mark.parametrize("field,value", [("profile", "other"), ("configured_host", "other-login"),
                                         ("connection_host", "other-node"), ("user", "bob"),
                                         ("uid", 424242), ("backend", "other.Backend"), ("cluster", "other-site")])
def test_direct_scoped_mutations_reject_each_identity_mismatch_before_data_changes(app, recipe, tmp_path, field, value):
    saved = receipt(app, recipe, tmp_path, cancel_after_parent=True)
    different = dict(execution_ui.connection_scope(app), **{field: value})
    before = Path(saved["receipt_path"]).read_bytes()
    with pytest.raises(ValueError, match="scope differs"):
        orchestrator.collect(saved["receipt_path"], app.store.snapshot(), expected_scope=different)
    with pytest.raises(ValueError, match="scope differs"):
        orchestrator.execute(saved["review"], app.actions.slurm, app.state_dir, confirmed=True,
                             receipt_path=saved["receipt_path"], expected_scope=different)
    with pytest.raises(ValueError, match="scope differs"):
        orchestrator.retry(saved["receipt_path"], "parent", confirmed=True, expected_scope=different)
    with pytest.raises(ValueError, match="scope differs"):
        orchestrator.recover(saved["receipt_path"], "parent", "777", app.actions.slurm,
                             confirmed=True, expected_scope=different)
    assert Path(saved["receipt_path"]).read_bytes() == before
    assert len(app.actions.slurm.calls) == 1 and not app.actions.slurm.info_calls


def test_original_scope_collects_measured_results_and_resumes_correct_parent(app, recipe, tmp_path):
    saved = receipt(app, recipe, tmp_path, cancel_after_parent=True)
    scope = execution_ui.connection_scope(app)
    collected = orchestrator.collect(saved["receipt_path"], app.store.snapshot(), expected_scope=scope)
    assert collected["nodes"][0]["state"] == "completed"
    resumed = orchestrator.execute(saved["review"], app.actions.slurm, app.state_dir, confirmed=True,
                                   receipt_path=saved["receipt_path"], expected_scope=scope)
    assert resumed["nodes"][1]["job_id"] == "102"
    assert "--dependency=afterok:101" in app.actions.slurm.calls[1][0]


def test_unknown_legacy_scope_is_inspectable_but_ui_collect_refuses(app, recipe, tmp_path):
    review = orchestrator.prepare_review("workflow", recipe, workdir=tmp_path)
    saved = orchestrator.create_receipt(review, app.state_dir)
    assert orchestrator.load(saved["receipt_path"])["review"] == review
    app.execution_state.update(receipt=saved, receipt_path=saved["receipt_path"], review=review)
    execution_ui.run_command(app, ["execution", "collect"])
    assert not app.research.pending and "scope is unknown" in app.messages[-1][1]
    assert not app.actions.slurm.calls


def test_scope_is_detached_hashed_and_bounded(app, recipe, tmp_path):
    source = execution_ui.connection_scope(app)
    review = orchestrator.prepare_review("workflow", recipe, workdir=tmp_path, scope=source)
    source["profile"] = "later-change"
    assert review["scope"]["profile"] == "carc"
    tampered = copy.deepcopy(review)
    tampered["scope"]["profile"] = "other-site"
    with pytest.raises(ValueError, match="review changed"):
        orchestrator.validate_review(tampered)
    for value in ({}, None, dict(review["scope"], connection_host=""), dict(review["scope"], user="x" * 257)):
        with pytest.raises(ValueError, match="scope is unknown"):
            orchestrator.validate_scope(value)


def test_confirmation_rechecks_profile_changed_after_review_was_displayed(app, recipe, tmp_path):
    from raven.layout import Glyphs
    review = orchestrator.prepare_review("workflow", recipe, workdir=tmp_path, scope=execution_ui.connection_scope(app))
    app.mode = "execution"
    state = app.execution_state
    state.update(view="review", review=review, pending_action=("start",))
    execution_ui.overlay(SimpleNamespace(g=Glyphs(True)), {}, app, 120, 35)
    app.profile_name = "different-profile"
    execution_ui.handle_key(app, "tab")
    execution_ui.handle_key(app, "tab")
    execution_ui.handle_key(app, "enter")
    assert not app.research.pending and not app.actions.slurm.calls
    assert not (tmp_path / "state").exists()
    assert "scope differs" in app.messages[-1][1]
