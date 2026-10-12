"""Durable shared receipts protect an exact review across process restarts."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven import operations as O


@pytest.fixture
def action(tmp_path, monkeypatch):
    calls = []
    ctx = O.Context(scope={"user": "owner", "cluster": "lab"}, state_dir=str(tmp_path))
    module = SimpleNamespace(apply=lambda feature, plan, context:
                             (calls.append(plan["digest"]) or O.report(feature, "Changed one object.")))
    monkeypatch.setattr(O, "_registry", lambda: {"sample": (module, {})})
    plan = O.prepare_plan(ctx, "sample", {}, {"target": "one"})
    return ctx, module, plan, calls


def test_exact_plan_claim_and_result_survive_new_context(action):
    ctx, module, plan, calls = action
    result = O.apply("sample", plan, ctx)
    receipt = Path(result["receipt_path"])
    assert json.loads(receipt.read_text())["result"]["summary"] == "Changed one object."
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert receipt.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ValueError, match="already attempted"):
        O.apply("sample", plan, replace(ctx))
    assert calls == [plan["digest"]]


def test_partial_exception_is_recorded_and_not_automatically_retried(action):
    ctx, module, plan, calls = action
    def partial(*args):
        calls.append("first step")
        raise ValueError("second step failed")
    module.apply = partial
    with pytest.raises(ValueError, match="second step"):
        O.apply("sample", plan, ctx)
    receipt = next((Path(ctx.state_dir) / "operations-receipts").glob("*.result.json"))
    value = json.loads(receipt.read_text())
    assert value["state"] == "error" and "partially completed" in value["reconciliation"]
    with pytest.raises(ValueError, match="already attempted"):
        O.apply("sample", plan, ctx)
    assert calls == ["first step"]


def test_failed_intent_write_prevents_scheduler_change(action, monkeypatch):
    ctx, module, plan, calls = action
    monkeypatch.setattr(O, "atomic_json", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        O.apply("sample", plan, ctx)
    assert calls == []


def test_result_save_failure_returns_report_and_retains_intent(action, monkeypatch):
    ctx, module, plan, calls = action
    original = O.atomic_json
    def write(path, value, **kwargs):
        if str(path).endswith("result.json"):
            raise OSError("disk full")
        return original(path, value, **kwargs)
    monkeypatch.setattr(O, "atomic_json", write)
    value = O.apply("sample", plan, ctx)
    assert "could not be saved" in value["warnings"][0]
    assert len(list((Path(ctx.state_dir) / "operations-receipts").glob("*.intent.json"))) == 1
    assert len(calls) == 1


def test_scope_change_fails_before_claim(action):
    ctx, module, plan, calls = action
    with pytest.raises(ValueError, match="connection changed"):
        O.apply("sample", plan, replace(ctx, scope={"cluster": "elsewhere"}))
    assert not (Path(ctx.state_dir) / "operations-receipts").exists()
    assert calls == []


def test_symlink_receipt_directory_is_refused(action, tmp_path):
    ctx, module, plan, calls = action
    destination = tmp_path / "elsewhere"
    destination.mkdir()
    (tmp_path / "operations-receipts").symlink_to(destination)
    with pytest.raises(ValueError, match="symbolic link"):
        O.apply("sample", plan, ctx)
    assert calls == []


def test_no_state_action_carries_persistence_warning(action):
    ctx, module, plan, calls = action
    value = O.apply("sample", plan, replace(ctx, state_dir=""))
    assert "disabled" in value["warnings"][0]
    assert calls == [plan["digest"]]
