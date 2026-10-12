"""Independent execution review regressions; never invokes a real scheduler."""
import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven import execution_ui, orchestrator
from raven.layout import Glyphs, row_text


class Scheduler:
    def __init__(self, *, uncertain=False):
        self.b = SimpleNamespace()
        self.calls = []
        self.info = {}
        self.uncertain = uncertain

    def submit(self, argv, workdir):
        self.calls.append((list(argv), workdir))
        return (True, "accepted without usable receipt", None) if self.uncertain else (True, "101", "101")

    def submit_info(self, jid):
        return self.info.get(jid, {})


@pytest.fixture
def review(tmp_path, monkeypatch):
    for name in os.environ:
        if name.startswith("SBATCH_"):
            monkeypatch.delenv(name)
    script = tmp_path / "reviewed.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    recipe = {"version": 1, "kind": "raven.workflow", "nodes": [
        {"id": "parent", "script": str(script), "resources": {"cpus_per_task": 1}},
        {"id": "child", "script": str(script), "depends_on": ["parent"], "resources": {"cpus_per_task": 1}},
    ]}
    return orchestrator.prepare_review("workflow", recipe, workdir=tmp_path)


def rewrite(receipt, mutate):
    path = Path(receipt["receipt_path"])
    saved = json.loads(path.read_text())
    mutate(saved)
    path.write_text(json.dumps(saved))
    return str(path)


@pytest.mark.parametrize("changed", ["missing_marker", "wrong_node_marker", "wrong_script", "wrong_workdir", "wrong_hash"])
def test_recovery_receipt_cannot_bind_unreviewed_attempt_even_when_accounting_matches(review, tmp_path, changed):
    scheduler = Scheduler(uncertain=True)
    receipt = orchestrator.execute(review, scheduler, tmp_path / "state", confirmed=True)
    unrelated = tmp_path / "unrelated.sh"
    unrelated.write_text("#!/bin/bash\ntrue\n")

    def mutate(saved):
        attempt = saved["nodes"][0]["attempts"][-1]
        command = shlex.split(attempt["command"])
        if changed == "missing_marker":
            command = [word for word in command if not word.startswith("--comment=")]
        elif changed == "wrong_node_marker":
            command = [word.replace(":parent:1", ":child:1") for word in command]
        elif changed == "wrong_script":
            command[-1] = str(unrelated)
        elif changed == "wrong_workdir":
            attempt["workdir"] = str(tmp_path / "other")
        else:
            attempt["script_sha256"] = "0" * 64
        attempt["command"] = shlex.join(command)
        scheduler.info["777"] = {"id": "777", "workdir": attempt["workdir"], "submit_line": attempt["command"]}

    path = rewrite(receipt, mutate)
    with pytest.raises(ValueError):
        orchestrator.recover(path, "parent", "777", scheduler, confirmed=True)
    assert len(scheduler.calls) == 1


def test_accepted_receipt_job_id_must_match_accepted_attempt_before_resume(review, tmp_path):
    scheduler = Scheduler()
    receipt = orchestrator.execute(review, scheduler, tmp_path / "state", confirmed=True,
                                   cancel=lambda: bool(scheduler.calls))
    assert receipt["nodes"][0]["job_id"] == "101"
    path = rewrite(receipt, lambda saved: saved["nodes"][0].update(job_id="999"))
    with pytest.raises(ValueError):
        orchestrator.execute(review, scheduler, tmp_path / "state", confirmed=True, receipt_path=path)
    assert len(scheduler.calls) == 1


@pytest.mark.parametrize("operation", [("retry", "child"), ("recover", "child", "777")])
def test_execution_action_review_names_actual_target_and_recovery_id(review, tmp_path, operation):
    app = SimpleNamespace(mode="execution")
    state = execution_ui.initialize(app)
    state.update(view="review", review=review, pending_action=operation)
    rendered = "\n".join(row_text(row) for _, _, row in execution_ui.overlay(SimpleNamespace(g=Glyphs(True)), {}, app, 120, 35))
    assert "Target node: child" in rendered
    if operation[0] == "recover":
        assert "Scheduler job: 777" in rendered


def test_cancellation_skips_unneeded_script_revalidation_before_dispatch(review, tmp_path, monkeypatch):
    examined = []
    fresh = orchestrator._fresh_base
    def inspect(node):
        examined.append(node["id"])
        return fresh(node)
    monkeypatch.setattr(orchestrator, "_fresh_base", inspect)
    scheduler = Scheduler()
    receipt = orchestrator.execute(review, scheduler, tmp_path / "state", confirmed=True, cancel=lambda: True)
    assert receipt["status"] == "cancelled" and scheduler.calls == []
    assert examined == []


def test_cancellation_during_batch_revalidation_stops_at_next_script(review, tmp_path, monkeypatch):
    examined, cancelled = [], [False]
    fresh = orchestrator._fresh_base
    def inspect(node):
        examined.append(node["id"])
        result = fresh(node)
        cancelled[0] = True
        return result
    monkeypatch.setattr(orchestrator, "_fresh_base", inspect)
    scheduler = Scheduler()
    receipt = orchestrator.execute(review, scheduler, tmp_path / "state", confirmed=True, cancel=lambda: cancelled[0])
    assert receipt["status"] == "cancelled" and scheduler.calls == []
    assert examined == ["parent"]
