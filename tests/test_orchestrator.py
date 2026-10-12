"""Execution uses real argv, private durable receipts, and a fake scheduler only."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven import orchestrator, submission, workflow
from raven.model import Finished, Job


@pytest.fixture(autouse=True)
def clear_sbatch_environment(monkeypatch):
    for name in os.environ:
        if name.startswith("SBATCH_"):
            monkeypatch.delenv(name)


@pytest.fixture
def recipe(tmp_path):
    for name in ("first", "second", "third"):
        (tmp_path / (name + ".sh")).write_text("#!/bin/bash\ntrue\n")
    return {"version": 1, "kind": "raven.workflow", "nodes": [
        {"id": "first", "script": "first.sh", "resources": {"cpus_per_task": 1}},
        {"id": "second", "script": "second.sh", "depends_on": ["first"], "resources": {"cpus_per_task": 1}},
        {"id": "third", "script": "third.sh", "depends_on": ["first", "second"], "resources": {"cpus_per_task": 1}},
    ]}


class Scheduler:
    def __init__(self, responses=None, callback=None):
        self.b = SimpleNamespace()
        self.calls = []
        self.responses = responses or []
        self.callback = callback
        self.info = {}

    def submit(self, argv, workdir):
        self.calls.append((list(argv), workdir))
        if self.callback:
            self.callback(len(self.calls), argv, workdir)
        response = self.responses[len(self.calls) - 1] if len(self.calls) <= len(self.responses) else (True, str(100 + len(self.calls)), str(100 + len(self.calls)))
        if isinstance(response, Exception):
            raise response
        return response

    def submit_info(self, job_id):
        return self.info.get(job_id, {})


def reviewed(recipe, tmp_path):
    return orchestrator.prepare_review("workflow", recipe, workdir=tmp_path)


def receipt_file(tmp_path):
    return next((tmp_path / "state" / "executions").glob("*.json"))


def test_review_does_not_call_scheduler_or_unseal_workflow(recipe, tmp_path, monkeypatch):
    monkeypatch.setattr(submission, "submit", lambda *a, **k: pytest.fail("review submitted a job"))
    review = reviewed(recipe, tmp_path)
    assert len(review["nodes"]) == 3
    for node in review["nodes"]:
        assert node["plan"]["submittable"] is False
        assert node["plan"]["workflow_orchestration"] == "review_only"
    assert not (tmp_path / "state").exists()


def test_generic_submit_still_rejects_all_workflow_plans(recipe, tmp_path):
    scheduler = Scheduler()
    for plan in workflow.plans(recipe, workdir=tmp_path):
        result = submission.submit(plan, scheduler)
        assert not result["ok"] and result["submitted"] is False
    assert scheduler.calls == []


@pytest.mark.parametrize("confirmed", [False, None, 1, "yes"])
def test_execution_requires_exact_confirmation(recipe, tmp_path, confirmed):
    scheduler = Scheduler()
    with pytest.raises(ValueError, match="confirmation"):
        orchestrator.execute(reviewed(recipe, tmp_path), scheduler, tmp_path / "state", confirmed=confirmed)
    assert not scheduler.calls


def test_execution_rebuilds_dependencies_from_real_receipts(recipe, tmp_path):
    review = reviewed(recipe, tmp_path)
    original = copy.deepcopy(review)
    scheduler = Scheduler()
    receipt = orchestrator.execute(review, scheduler, tmp_path / "state", confirmed=True)
    assert receipt["status"] == "submitted"
    assert [node["job_id"] for node in receipt["nodes"]] == ["101", "102", "103"]
    assert not any(token.startswith("--dependency") for token in scheduler.calls[0][0])
    assert "--dependency=afterok:101" in scheduler.calls[1][0]
    assert "--dependency=afterok:101:102" in scheduler.calls[2][0]
    for index, (argv, _) in enumerate(scheduler.calls):
        assert any(token.startswith("--comment=raven-execution:" + receipt["execution_id"]) for token in argv)
        assert receipt["nodes"][index]["attempts"][0]["passport_path"]
    assert review == original
    stored = orchestrator.load(receipt["receipt_path"])
    assert stored["nodes"] == receipt["nodes"]
    assert Path(receipt["receipt_path"]).stat().st_mode & 0o777 == 0o600
    assert Path(receipt["receipt_path"]).parent.stat().st_mode & 0o777 == 0o700


def test_durable_intent_exists_before_scheduler_call(recipe, tmp_path):
    states = []

    def observe(index, argv, cwd):
        receipt = orchestrator.load(receipt_file(tmp_path))
        states.append(receipt["nodes"][index - 1]["state"])
        assert receipt["nodes"][index - 1]["attempts"][-1]["command"].endswith(argv[-1])

    orchestrator.execute(reviewed(recipe, tmp_path), Scheduler(callback=observe), tmp_path / "state", confirmed=True)
    assert states == ["launching"] * 3


@pytest.mark.parametrize("response", [(True, "accepted but no ID", None), (False, "transport timeout", None), RuntimeError("lost response")])
def test_unknown_receipt_stops_descendants_and_cannot_resume_or_retry(recipe, tmp_path, response):
    scheduler = Scheduler([response])
    receipt = orchestrator.execute(reviewed(recipe, tmp_path), scheduler, tmp_path / "state", confirmed=True)
    assert len(scheduler.calls) == 1
    assert receipt["nodes"][0]["state"] == "unknown"
    assert receipt["nodes"][1]["state"] == "planned"
    with pytest.raises(ValueError, match="unknown"):
        orchestrator.execute(receipt["review"], scheduler, tmp_path / "state", confirmed=True, receipt_path=receipt["receipt_path"])
    with pytest.raises(ValueError, match="unknown"):
        orchestrator.retry(receipt["receipt_path"], "first", confirmed=True)
    assert len(scheduler.calls) == 1


def test_definitive_rejection_requires_explicit_retry_and_preserves_attempts(recipe, tmp_path):
    scheduler = Scheduler([(False, "invalid partition", None)])
    receipt = orchestrator.execute(reviewed(recipe, tmp_path), scheduler, tmp_path / "state", confirmed=True)
    assert receipt["nodes"][0]["state"] == "rejected"
    with pytest.raises(ValueError, match="failed"):
        orchestrator.execute(receipt["review"], scheduler, tmp_path / "state", confirmed=True, receipt_path=receipt["receipt_path"])
    with pytest.raises(ValueError, match="confirmation"):
        orchestrator.retry(receipt["receipt_path"], "first")
    retry = orchestrator.retry(receipt["receipt_path"], "first", confirmed=True)
    assert retry["nodes"][0]["state"] == "planned"
    done = orchestrator.execute(receipt["review"], scheduler, tmp_path / "state", confirmed=True, receipt_path=receipt["receipt_path"])
    assert done["status"] == "submitted"
    assert [attempt["state"] for attempt in done["nodes"][0]["attempts"]] == ["rejected", "accepted"]
    assert ":first:2" in done["nodes"][0]["attempts"][-1]["command"]


def test_cancel_after_one_accepted_job_can_resume_without_resubmitting_it(recipe, tmp_path):
    cancelled = [False]
    scheduler = Scheduler(callback=lambda *args: cancelled.__setitem__(0, True))
    receipt = orchestrator.execute(reviewed(recipe, tmp_path), scheduler, tmp_path / "state", confirmed=True,
                                   cancel=lambda: cancelled[0])
    assert receipt["status"] == "cancelled"
    assert len(scheduler.calls) == 1
    scheduler.callback = None
    done = orchestrator.execute(receipt["review"], scheduler, tmp_path / "state", confirmed=True, receipt_path=receipt["receipt_path"])
    assert done["status"] == "submitted"
    assert len(scheduler.calls) == 3
    assert len(done["nodes"][0]["attempts"]) == 1


def test_cancel_before_first_call_makes_no_scheduler_action(recipe, tmp_path):
    scheduler = Scheduler()
    receipt = orchestrator.execute(reviewed(recipe, tmp_path), scheduler, tmp_path / "state", confirmed=True, cancel=lambda: True)
    assert receipt["status"] == "cancelled"
    assert not scheduler.calls
    assert all(not node["attempts"] for node in receipt["nodes"])


def test_interrupted_launching_intent_needs_recovery(recipe, tmp_path):
    def crash(value):
        raise RuntimeError("interrupted before scheduler call")

    scheduler = Scheduler()
    with pytest.raises(RuntimeError, match="interrupted"):
        orchestrator.execute(reviewed(recipe, tmp_path), scheduler, tmp_path / "state", confirmed=True, progress=crash)
    path = receipt_file(tmp_path)
    receipt = orchestrator.load(path)
    assert receipt["nodes"][0]["state"] == "launching"
    with pytest.raises(ValueError, match="unknown"):
        orchestrator.execute(receipt["review"], scheduler, tmp_path / "state", confirmed=True, receipt_path=path)
    assert scheduler.calls == []


def test_changed_script_prevents_all_scheduler_calls(recipe, tmp_path):
    review = reviewed(recipe, tmp_path)
    (tmp_path / "third.sh").write_text("#!/bin/bash\necho changed\n")
    scheduler = Scheduler()
    with pytest.raises(ValueError, match="changed"):
        orchestrator.execute(review, scheduler, tmp_path / "state", confirmed=True)
    assert scheduler.calls == []


def test_mid_batch_script_change_preserves_first_receipt_and_stops(recipe, tmp_path):
    scheduler = Scheduler(callback=lambda *args: (tmp_path / "second.sh").write_text("#!/bin/bash\necho changed\n"))
    with pytest.raises(ValueError, match="changed"):
        orchestrator.execute(reviewed(recipe, tmp_path), scheduler, tmp_path / "state", confirmed=True)
    receipt = orchestrator.load(receipt_file(tmp_path))
    assert receipt["nodes"][0]["job_id"] == "101"
    assert receipt["nodes"][1]["state"] == "planned"
    assert len(scheduler.calls) == 1


def test_duplicate_scheduler_id_is_unknown_and_stops(recipe, tmp_path):
    scheduler = Scheduler([(True, "101", "101"), (True, "101", "101")])
    receipt = orchestrator.execute(reviewed(recipe, tmp_path), scheduler, tmp_path / "state", confirmed=True)
    assert receipt["nodes"][1]["state"] == "unknown"
    assert receipt["nodes"][1]["job_id"] is None
    assert len(scheduler.calls) == 2


def test_recovery_requires_exact_unique_submit_line_then_resume_uses_id(recipe, tmp_path):
    scheduler = Scheduler([(True, "accepted but no ID", None)])
    receipt = orchestrator.execute(reviewed(recipe, tmp_path), scheduler, tmp_path / "state", confirmed=True)
    attempt = receipt["nodes"][0]["attempts"][-1]
    scheduler.info["777"] = {"id": "777", "workdir": attempt["workdir"], "submit_line": attempt["command"]}
    with pytest.raises(ValueError, match="confirmation"):
        orchestrator.recover(receipt["receipt_path"], "first", "777", scheduler)
    recovered = orchestrator.recover(receipt["receipt_path"], "first", "777", scheduler, confirmed=True)
    assert recovered["nodes"][0]["job_id"] == "777"
    assert recovered["nodes"][0]["attempts"][-1]["recovery"]["job_id"] == "777"
    done = orchestrator.execute(receipt["review"], scheduler, tmp_path / "state", confirmed=True, receipt_path=receipt["receipt_path"])
    assert "--dependency=afterok:777" in scheduler.calls[1][0]
    assert done["status"] == "submitted"


def test_historical_receipt_validation_does_not_reopen_changed_or_missing_scripts(recipe, tmp_path, monkeypatch):
    receipt = orchestrator.execute(reviewed(recipe, tmp_path), Scheduler(), tmp_path / "state", confirmed=True)
    for name in ("first", "second", "third"):
        (tmp_path / (name + ".sh")).unlink()
    monkeypatch.setattr(submission, "prepare", lambda *args, **kwargs: pytest.fail("history validation reopened scripts"))
    assert orchestrator.load(receipt["receipt_path"])["nodes"][2]["job_id"] == "103"


@pytest.mark.parametrize("operation", ["retry", "recover"])
def test_recovery_or_retry_refuses_a_different_review_than_was_displayed(recipe, tmp_path, operation):
    response = (False, "definitely rejected", None) if operation == "retry" else (True, "unknown receipt", None)
    scheduler = Scheduler([response])
    receipt = orchestrator.execute(reviewed(recipe, tmp_path), scheduler, tmp_path / "state", confirmed=True)
    with pytest.raises(ValueError, match="review changed"):
        if operation == "retry":
            orchestrator.retry(receipt["receipt_path"], "first", confirmed=True, expected_review_id="different-reviewed-document")
        else:
            orchestrator.recover(receipt["receipt_path"], "first", "777", scheduler, confirmed=True, expected_review_id="different-reviewed-document")
    assert len(scheduler.calls) == 1


@pytest.mark.parametrize("change", ["cwd", "command", "id", "missing"])
def test_recovery_refuses_unverified_accounting(recipe, tmp_path, change):
    scheduler = Scheduler([(True, "unclear", None)])
    receipt = orchestrator.execute(reviewed(recipe, tmp_path), scheduler, tmp_path / "state", confirmed=True)
    attempt = receipt["nodes"][0]["attempts"][-1]
    info = {"id": "777", "workdir": attempt["workdir"], "submit_line": attempt["command"]}
    if change == "cwd":
        info["workdir"] += "/other"
    elif change == "command":
        info["submit_line"] = info["submit_line"].replace("--comment=", "--comment=unrelated-")
    elif change == "id":
        info["id"] = "778"
    else:
        info = {}
    scheduler.info["777"] = info
    with pytest.raises(ValueError, match="verify"):
        orchestrator.recover(receipt["receipt_path"], "first", "777", scheduler, confirmed=True)
    assert orchestrator.load(receipt["receipt_path"])["nodes"][0]["state"] == "unknown"


def test_review_tampering_is_rejected_before_state_write(recipe, tmp_path):
    review = reviewed(recipe, tmp_path)
    review["nodes"][0]["plan"]["argv"].insert(0, "--mem=1T")
    with pytest.raises(ValueError, match="changed"):
        orchestrator.execute(review, Scheduler(), tmp_path / "state", confirmed=True)
    assert not (tmp_path / "state").exists()


def test_execution_budget_is_smaller_than_offline_planning_budget(recipe, tmp_path):
    recipe["nodes"] = [{"id": f"node{index}", "script": "first.sh", "resources": {"cpus_per_task": 1}} for index in range(65)]
    with pytest.raises(ValueError, match="64"):
        reviewed(recipe, tmp_path)


def test_oversized_workflow_is_rejected_before_inspecting_scripts(recipe, tmp_path, monkeypatch):
    recipe["nodes"] = [{"id": f"node{index}", "script": "first.sh", "resources": {"cpus_per_task": 1}} for index in range(65)]
    monkeypatch.setattr(submission, "prepare", lambda *args, **kwargs: pytest.fail("oversized batch inspected scripts"))
    with pytest.raises(ValueError, match="64"):
        reviewed(recipe, tmp_path)


def test_failed_post_submit_write_leaves_an_uncertain_durable_intent(recipe, tmp_path, monkeypatch):
    original = orchestrator._save

    def fail_after_accept(receipt, path):
        if receipt["nodes"][0]["state"] == "accepted":
            raise OSError("disk full after accepted receipt")
        return original(receipt, path)

    monkeypatch.setattr(orchestrator, "_save", fail_after_accept)
    scheduler = Scheduler()
    with pytest.raises(OSError, match="disk full"):
        orchestrator.execute(reviewed(recipe, tmp_path), scheduler, tmp_path / "state", confirmed=True)
    receipt = orchestrator.load(receipt_file(tmp_path))
    assert receipt["nodes"][0]["state"] == "launching"
    assert len(scheduler.calls) == 1
    with pytest.raises(ValueError, match="unknown"):
        orchestrator.execute(receipt["review"], scheduler, tmp_path / "state", confirmed=True, receipt_path=receipt_file(tmp_path))
    assert len(scheduler.calls) == 1


def test_receipt_cannot_reset_accepted_job_to_unattempted(recipe, tmp_path):
    receipt = orchestrator.execute(reviewed(recipe, tmp_path), Scheduler(), tmp_path / "state", confirmed=True)
    receipt["nodes"][0].update(state="planned", job_id=None)
    path = receipt["receipt_path"]
    orchestrator._save({key: value for key, value in receipt.items() if key != "receipt_path"}, path)
    with pytest.raises(ValueError, match="unattempted"):
        orchestrator.load(path)


def test_atomic_save_refuses_receipt_symlink(recipe, tmp_path):
    review = reviewed(recipe, tmp_path)
    receipt = orchestrator.create_receipt(review, tmp_path / "state")
    target = tmp_path / "private.txt"
    target.write_text("keep")
    Path(receipt["receipt_path"]).unlink()
    Path(receipt["receipt_path"]).symlink_to(target)
    with pytest.raises(ValueError):
        orchestrator._save({key: value for key, value in receipt.items() if key != "receipt_path"}, receipt["receipt_path"])
    assert target.read_text() == "keep"


def test_receipt_lock_refuses_concurrent_execution(recipe, tmp_path):
    receipt = orchestrator.create_receipt(reviewed(recipe, tmp_path), tmp_path / "state")
    with orchestrator._locked(receipt["receipt_path"]):
        with pytest.raises(ValueError, match="another Raven"):
            orchestrator.execute(receipt["review"], Scheduler(), tmp_path / "state", confirmed=True, receipt_path=receipt["receipt_path"])


def test_receipt_lock_refuses_fifo_without_blocking(recipe, tmp_path):
    receipt = orchestrator.create_receipt(reviewed(recipe, tmp_path), tmp_path / "state")
    lock = Path(receipt["receipt_path"] + ".lock")
    lock.unlink()
    os.mkfifo(lock)
    scheduler = Scheduler()
    with pytest.raises((OSError, ValueError)):
        orchestrator.execute(receipt["review"], scheduler, tmp_path / "state", confirmed=True, receipt_path=receipt["receipt_path"])
    assert not scheduler.calls


def test_collection_records_real_job_states_without_scheduler_action(recipe, tmp_path):
    scheduler = Scheduler()
    receipt = orchestrator.execute(reviewed(recipe, tmp_path), scheduler, tmp_path / "state", confirmed=True)
    finished = [Finished("101", state="COMPLETED", elapsed="00:00:45", cpus=1, end="2026-01-01T00:00:45")]
    collected = orchestrator.collect(receipt["receipt_path"], {"jobs": [Job("102", "second", "main", "RUNNING")], "finished": finished})
    assert collected["nodes"][0]["state"] == "completed"
    assert collected["nodes"][0]["observation"]["elapsed"] == "00:00:45"
    assert collected["nodes"][1]["state"] == "running"
    assert collected["collection"]["missing"] == ["third"]
    assert len(scheduler.calls) == 3


def test_resume_refuses_unsubmitted_descendants_of_observed_failure(recipe, tmp_path):
    cancelled = [False]
    scheduler = Scheduler(callback=lambda *args: cancelled.__setitem__(0, True))
    receipt = orchestrator.execute(reviewed(recipe, tmp_path), scheduler, tmp_path / "state", confirmed=True,
                                   cancel=lambda: cancelled[0])
    orchestrator.collect(receipt["receipt_path"], {"finished": [Finished("101", state="FAILED", elapsed="00:00:10")]})
    with pytest.raises(ValueError, match="upstream job failed"):
        orchestrator.execute(receipt["review"], scheduler, tmp_path / "state", confirmed=True, receipt_path=receipt["receipt_path"])
    assert len(scheduler.calls) == 1


def scaling_recipe(tmp_path):
    (tmp_path / "scaling.sh").write_text("#!/bin/bash\ntrue\n")
    return {"version": 1, "kind": "raven.scaling", "script": "scaling.sh", "mode": "strong", "repeats": 1,
            "problem_size": 100, "parameters": {"algorithm": "controlled"},
            "configurations": [{"workers": 1}, {"workers": 2}]}


def test_scaling_execution_collects_only_measured_completed_runtime(tmp_path):
    review = orchestrator.prepare_review("scaling", scaling_recipe(tmp_path), workdir=tmp_path)
    scheduler = Scheduler()
    receipt = orchestrator.execute(review, scheduler, tmp_path / "state", confirmed=True)
    assert all(not any(token.startswith("--dependency") for token in argv) for argv, _ in scheduler.calls)
    collected = orchestrator.collect(receipt["receipt_path"], {"finished": [
        Finished("101", state="COMPLETED", elapsed="00:01:00", cpus=1),
        Finished("102", state="COMPLETED", elapsed="00:00:40", cpus=2),
    ]})
    assert [record["runtime_seconds"] for record in collected["collection"]["records"]] == [60, 40]
    assert collected["collection"]["analysis"]["status"] == "partial"  # one repeat cannot quantify spread
    assert collected["status"] == "finished"
    resumed = orchestrator.execute(review, scheduler, tmp_path / "state", confirmed=True, receipt_path=receipt["receipt_path"])
    assert resumed["status"] == "finished" and len(scheduler.calls) == 2


def test_scaling_unobserved_repeats_are_censored_not_invented(tmp_path):
    review = orchestrator.prepare_review("scaling", scaling_recipe(tmp_path), workdir=tmp_path)
    receipt = orchestrator.execute(review, Scheduler(), tmp_path / "state", confirmed=True)
    collected = orchestrator.collect(receipt["receipt_path"], {"finished": [Finished("101", state="COMPLETED", elapsed="00:01:00", cpus=1)]})
    assert len(collected["collection"]["records"]) == 2
    assert collected["collection"]["records"][1]["runtime_seconds"] is None
    assert collected["collection"]["analysis"]["excluded_count"] == 1


def test_scaling_previously_failed_attempt_stays_censored_after_external_requeue(tmp_path):
    review = orchestrator.prepare_review("scaling", scaling_recipe(tmp_path), workdir=tmp_path)
    receipt = orchestrator.execute(review, Scheduler(), tmp_path / "state", confirmed=True)
    orchestrator.collect(receipt["receipt_path"], {"finished": [Finished("101", state="FAILED", elapsed="00:00:10", cpus=1)]})
    collected = orchestrator.collect(receipt["receipt_path"], {"finished": [Finished("101", state="COMPLETED", elapsed="00:01:00", cpus=1)]})
    assert collected["nodes"][0]["state"] == "completed"
    assert collected["nodes"][0]["observation_history"][-1]["state"] == "FAILED"
    assert collected["collection"]["records"][0]["state"] == "CENSORED"
    assert collected["collection"]["records"][0]["observed_state"] == "COMPLETED"
    assert collected["collection"]["analysis"]["excluded_count"] == 2
