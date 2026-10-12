"""Admission races and receipt ordering across scientific and reviewed work."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import copy
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from raven import diagnostic_trials as D
from raven import operations as O
from raven.slurm import CommandError


@pytest.fixture
def reviewed(tmp_path, monkeypatch):
    calls = []
    ctx = O.Context(scope={"user": "owner", "cluster": "test"},
                    state_dir=str(tmp_path), cancel=threading.Event())
    def change(feature, plan, ctx):
        calls.append(copy.deepcopy(plan["payload"]))
        return O.report(feature, "Change accepted", status="warning", data={"state": "unknown"})
    module = SimpleNamespace(apply=change)
    monkeypatch.setattr(O, "_registry", lambda: {"sample": (module, {})})
    return ctx, O.prepare_plan(ctx, "sample", {}, {"target": "original"}), calls, module


@pytest.mark.parametrize("change", ["cancel", "scope", "expiry"])
def test_action_revalidates_after_durable_admission(reviewed, monkeypatch, change):
    ctx, plan, calls, _ = reviewed
    original = O.atomic_json
    def write(path, value, **kwargs):
        result = original(path, value, **kwargs)
        if str(path).endswith(".intent.json"):
            if change == "cancel":
                ctx.cancel.set()
            elif change == "scope":
                ctx.scope["cluster"] = "replacement"
            else:
                monkeypatch.setattr(O.time, "time", lambda: plan["created"] + O.PLAN_LIFETIME + 1)
        return result
    monkeypatch.setattr(O, "atomic_json", write)
    with pytest.raises(ValueError):
        O.apply("sample", plan, ctx)
    assert calls == []
    receipt = json.loads(next((Path(ctx.state_dir)/"operations-receipts").glob("*.result.json")).read_text())
    assert receipt["state"] == "error"
    assert receipt["scope"]["cluster"] == "test"


def test_action_captures_review_before_shared_editor_changes(reviewed, monkeypatch):
    ctx, plan, calls, _ = reviewed
    original = O.atomic_json
    digest = plan["digest"]
    def write(path, value, **kwargs):
        result = original(path, value, **kwargs)
        if str(path).endswith(".intent.json"):
            plan["payload"]["target"] = "unexpected"
            plan["digest"] = O.digest({k: v for k, v in plan.items() if k != "digest"})
        return result
    monkeypatch.setattr(O, "atomic_json", write)
    result = O.apply("sample", plan, ctx)
    assert calls == [{"target": "original"}]
    assert digest in result["receipt_path"]
    assert result["status"] == "warning" and result["data"]["state"] == "unknown"


def test_action_preserves_completed_outcome_when_cancel_arrives_after_mutation(reviewed):
    ctx, plan, calls, module = reviewed
    def change(feature, plan, ctx):
        calls.append("changed")
        ctx.cancel.set()
        return O.report(feature, "Partial outcome", status="warning", data={"state": "partial", "changed": 1})
    module.apply = change
    result = O.apply("sample", plan, ctx)
    saved = json.loads(Path(result["receipt_path"]).read_text())
    assert saved["state"] == "finished"
    assert saved["result"]["data"] == {"state": "partial", "changed": 1}
    assert calls == ["changed"]


def test_interruption_keeps_receipt_and_does_not_allow_exact_review_retry(reviewed):
    ctx, plan, calls, module = reviewed
    def interrupt(*_):
        calls.append("started")
        raise KeyboardInterrupt("interrupted after first step")
    module.apply = interrupt
    with pytest.raises(KeyboardInterrupt):
        O.apply("sample", plan, ctx)
    receipt = json.loads(next((Path(ctx.state_dir)/"operations-receipts").glob("*.result.json")).read_text())
    assert receipt["state"] == "error" and "interrupted" in receipt["error"]
    with pytest.raises(ValueError, match="already attempted"):
        O.apply("sample", plan, ctx)
    assert calls == ["started"]


def test_parallel_actions_share_one_durable_claim(reviewed):
    ctx, plan, calls, _ = reviewed
    def run():
        try:
            return O.apply("sample", plan, replace(ctx))
        except ValueError as error:
            return error
    with ThreadPoolExecutor(max_workers=4) as workers:
        outcomes = list(workers.map(lambda _: run(), range(4)))
    assert sum(isinstance(result, dict) for result in outcomes) == 1
    assert len(calls) == 1
    assert all(isinstance(result, dict) or "already attempted" in str(result) for result in outcomes)


def lifecycle(tmp_path, *, cancel=None, verify=lambda: None, max_trials=4):
    return D.TrialLifecycle(tmp_path, {"total_seconds": 10, "trial_seconds": 2,
                                      "max_trials": max_trials}, verify, cancel)


def prepare(directory, sequence):
    return {"sequence": sequence}, {"directory": directory}


def measure(directory, sequence, observed):
    observed.pop("output", None)
    return {**observed, "sequence": sequence}


@pytest.mark.parametrize("phase", ["verify", "prepare", "intent"])
@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_trial_never_launches_after_staging_consumes_budget(tmp_path, monkeypatch, phase, stop):
    clock = [100.0]
    monkeypatch.setattr(D.time, "monotonic", lambda: clock[0])
    cancel = threading.Event()
    def stop_now():
        if stop == "cancel":
            cancel.set()
        else:
            clock[0] += 11
    session = lifecycle(tmp_path, cancel=cancel, verify=stop_now if phase == "verify" else lambda: None)
    def stage(directory, sequence):
        if phase == "prepare":
            stop_now()
        return prepare(directory, sequence)
    original = D._atomic
    def write(path, value):
        original(path, value)
        if phase == "intent" and Path(path).name == "intent.json":
            stop_now()
    monkeypatch.setattr(D, "_atomic", write)
    def execute(*_):
        pytest.fail("No process may start after cancellation or deadline")
    result = session.perform(stage, execute, measure)
    if phase == "verify":
        assert result is None
        assert not list((tmp_path/"trials").iterdir())
    else:
        assert result["state"] == ("cancelled" if stop == "cancel" else "timeout")
        assert json.loads((tmp_path/"trials/0000/result.json.receipt").read_text()) == result


def test_trial_sequences_and_budget_are_shared_across_concurrent_admission(tmp_path):
    session = lifecycle(tmp_path, max_trials=2)
    calls = []
    def execute(prepared, timeout):
        assert (prepared["directory"]/"intent.json").exists()
        calls.append(prepared["directory"].name)
        return {"state": "finished", "returncode": 0}
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: session.perform(prepare, execute, measure), range(4)))
    assert calls == ["0000", "0001"]
    assert sum(result is None for result in results) == 2
    assert len(session.trials) == 2


def test_failed_measurement_receipt_blocks_further_trial_execution(tmp_path, monkeypatch):
    session = lifecycle(tmp_path)
    original = D._atomic
    calls = []
    def write(path, value):
        if Path(path).name == "result.json.receipt":
            raise OSError("receipt disk full")
        return original(path, value)
    monkeypatch.setattr(D, "_atomic", write)
    def execute(*_):
        calls.append("executed")
        return {"state": "finished", "returncode": 0}
    with pytest.raises(OSError, match="disk full"):
        session.perform(prepare, execute, measure)
    with pytest.raises(ValueError, match="interrupted"):
        session.perform(prepare, execute, measure)
    assert calls == ["executed"] and session.trials == []
    assert (tmp_path/"trials/0000/intent.json").exists()


def test_uncertain_oracle_does_not_repeat_a_failed_infrastructure_trial():
    calls = []
    def trial():
        calls.append("attempt")
        return None
    assert D.repeated_trials(trial, 10) == [None]
    assert calls == ["attempt"]


def test_expired_trial_does_not_start_even_an_executable_that_has_side_effects(tmp_path, monkeypatch):
    def launch(*_, **__):
        pytest.fail("An exhausted trial must not spawn a process")
    monkeypatch.setattr(D.subprocess, "Popen", launch)
    assert D._execute(["/bin/false"], tmp_path, {"output_bytes": 1024}, 0)["state"] == "timeout"


def test_command_cancellation_stays_responsive_after_both_pipes_close(tmp_path):
    ready = tmp_path/"closed"
    cancel = threading.Event()
    code = "import os,time,pathlib,sys; os.close(1); os.close(2); pathlib.Path(sys.argv[1]).touch(); time.sleep(15)"
    def trigger():
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            cancel.wait(.01)
        cancel.set()
    helper = threading.Thread(target=trigger)
    helper.start()
    started = time.monotonic()
    try:
        with pytest.raises(CommandError, match="cancelled"):
            O._process([sys.executable, "-c", code, str(ready)], 10, 4096, cancel)
        assert ready.exists()
        assert time.monotonic() - started < 3
    finally:
        cancel.set()
        helper.join(timeout=6)


def test_closed_pipe_wait_uses_remaining_deadline_and_does_not_busy_spin():
    clock = [1.0]
    waits = []
    class Child:
        def poll(self):
            return None
        def wait(self, timeout):
            waits.append(timeout)
            clock[0] += timeout
            raise subprocess.TimeoutExpired("child", timeout)
    original = D.time.monotonic
    try:
        D.time.monotonic = lambda: clock[0]
        assert D.wait_for_exit(Child(), 1.06) == "timeout"
    finally:
        D.time.monotonic = original
    assert len(waits) == 3 and sum(waits) == pytest.approx(.06)
