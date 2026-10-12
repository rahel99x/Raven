from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from raven import runtime_control as runtime


def configuration():
    return {"schema": "raven.runtime-control/v1", "attempts": ["job123:attempt1", "job124:attempt1"],
            "commands": {"learning-rate": {"safe_point": "epoch", "parameters": {"value": {"type": "number", "minimum": 0, "maximum": 1}}}},
            "io": {"capacity": 1, "max_lease_seconds": 60}}


@pytest.fixture
def mailbox(tmp_path):
    clock = [100.0]
    path = tmp_path / "mailbox"
    runtime.initialize(path, configuration(), clock=lambda: clock[0])
    return path, clock, runtime.RuntimeControl(path, "job123:attempt1", clock=lambda: clock[0])


def test_submit_safe_point_epoch_and_idempotence(mailbox):
    path, clock, control = mailbox
    first = control.submit("request1", "learning-rate", {"value": .1}, expected_epoch=0)
    assert first["status"] == "pending"
    assert control.submit("request1", "learning-rate", {"value": .1}, expected_epoch=0) == first
    assert control.poll("iteration") is None
    calls = []
    result = control.safe_point("epoch", lambda command, params, request: calls.append((command, params, request)) or {"value": params["value"]})
    assert result["status"] == "applied"
    assert len(calls) == 1
    assert control.safe_point("epoch", lambda *args: calls.append(args)) is None
    assert control.status()["epochs"][control.attempt] == 1
    assert control.acknowledge("request1", result["claim"], applied=True, result={"value": .1}) == result
    assert control.status()["epochs"][control.attempt] == 1


def test_immutable_request_ids_and_acknowledgments(mailbox):
    _, _, control = mailbox
    control.submit("x", "learning-rate", {"value": .1}, expected_epoch=0)
    with pytest.raises(ValueError, match="immutable"):
        control.submit("x", "learning-rate", {"value": .2}, expected_epoch=0)
    request = control.poll("epoch")
    control.acknowledge("x", request["claim"], applied=False, result={"reason": "unsafe"})
    with pytest.raises(ValueError, match="conflicts"):
        control.acknowledge("x", request["claim"], applied=True)
    assert control.status()["epochs"][control.attempt] == 0


@pytest.mark.parametrize("value", [-1, 2, True, float("nan"), "0.1", None])
def test_typed_parameter_rejection(mailbox, value):
    _, _, control = mailbox
    with pytest.raises(ValueError):
        control.submit("bad", "learning-rate", {"value": value}, expected_epoch=0)
    assert not control.status()["requests"]


def test_expiry_and_stale_epochs(mailbox):
    _, clock, control = mailbox
    control.submit("expired", "learning-rate", {"value": .1}, expected_epoch=0, ttl=1)
    clock[0] = 102
    assert control.poll("epoch") is None
    assert control.status()["requests"][0]["status"] == "expired"
    control.submit("stale", "learning-rate", {"value": .1}, expected_epoch=4)
    assert control.poll("epoch") is None
    assert control.status()["requests"][0]["status"] == "stale"


def test_claim_crash_is_uncertain_never_replayed(mailbox):
    _, clock, control = mailbox
    control.submit("crash", "learning-rate", {"value": .1}, expected_epoch=0, ttl=1)
    request = control.poll("epoch")
    clock[0] = 102
    control.submit("next", "learning-rate", {"value": .2}, expected_epoch=0)
    assert control.poll("epoch") is None
    assert {row["id"]: row["status"] for row in control.status()["requests"]}["crash"] == "uncertain"
    control.acknowledge("crash", request["claim"], applied=False, result={"verified": "not applied"})
    assert control.poll("epoch")["id"] == "next"


def test_handler_exception_does_not_claim_not_applied(mailbox):
    _, _, control = mailbox
    control.submit("bad", "learning-rate", {"value": .1}, expected_epoch=0)
    def partial(*args):
        raise RuntimeError("after side effect")
    with pytest.raises(RuntimeError): control.safe_point("epoch", partial)
    assert control.status()["requests"][0]["status"] == "claimed"
    assert control.poll("epoch") is None


def test_concurrent_claims_only_one_wins(mailbox):
    path, clock, control = mailbox
    control.submit("only", "learning-rate", {"value": .1}, expected_epoch=0)
    def claim(_):
        return runtime.RuntimeControl(path, control.attempt, clock=lambda: clock[0]).poll("epoch")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(16)))
    assert sum(value is not None for value in results) == 1


def test_attempt_scope_and_clock_reversal(mailbox):
    path, clock, control = mailbox
    with pytest.raises(ValueError, match="attempt"):
        runtime.RuntimeControl(path, "job123:attempt2", clock=lambda: clock[0]).status()
    clock[0] -= 1
    with pytest.raises(ValueError, match="backwards"): control.status()


def test_machine_boot_identity_rejects_reuse(mailbox, monkeypatch):
    _, _, control = mailbox
    monkeypatch.setattr(runtime, "_machine", lambda: {"host": "other", "boot": "other"})
    with pytest.raises(ValueError, match="boot changed"): control.status()


def test_permissions_symlinks_and_reinitialization(mailbox, tmp_path):
    path, _, control = mailbox
    assert path.stat().st_mode & 0o777 == 0o700
    assert (path / "control.sqlite3").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError): runtime.initialize(path, configuration())
    (tmp_path / "alias").symlink_to(path)
    with pytest.raises(ValueError, match="symlinks"):
        runtime.RuntimeControl(tmp_path / "alias").status()
    path.chmod(0o755)
    with pytest.raises(ValueError, match="0700"): control.status()


def test_collective_arrivals_capacity_release_and_order(mailbox):
    path, clock, control = mailbox
    first = control.permit("first", participants=["r0", "r1"], arrived=["r0"])
    assert first["state"] == "waiting"
    first = control.permit("first", participants=["r0", "r1"], arrived=["r1"])
    assert first["state"] == "granted"
    other = runtime.RuntimeControl(path, "job124:attempt1", clock=lambda: clock[0])
    second = other.permit("second", participants=["r0"], arrived=["r0"])
    assert second["state"] == "waiting"
    control.release("first", first["token"])
    assert other.status()["permits"][0]["state"] == "granted"
    assert control.release("first", first["token"])["state"] == "released"


def test_expired_active_permit_quarantines_capacity(mailbox):
    path, clock, control = mailbox
    first = control.permit("first", participants=["r0"], arrived=["r0"], lease_seconds=1)
    clock[0] += 2
    second = control.permit("second", participants=["r0"], arrived=["r0"])
    assert second["state"] == "waiting"
    assert {row["id"]: row["state"] for row in control.status()["permits"]}["first"] == "uncertain"
    with pytest.raises(ValueError): control.renew("first", first["token"], 2)
    control.release("first", first["token"])
    assert {row["id"]: row["state"] for row in control.status()["permits"]}["second"] == "granted"


@pytest.mark.parametrize("fallback,arrived,state", [("skip", ["r0"], "skipped"), ("proceed", ["r0"], "bypass"), ("proceed", [], "incomplete")])
def test_deadline_fallback_is_collective_and_explicit(mailbox, fallback, arrived, state):
    _, _, control = mailbox
    result = control.permit("deadline", participants=["r0"], arrived=arrived, wait_seconds=0, fallback=fallback)
    assert result["state"] == state


def test_lease_renewal_and_bad_tokens(mailbox):
    _, clock, control = mailbox
    result = control.permit("x", participants=["r0"], arrived=["r0"], lease_seconds=1)
    clock[0] += .5
    assert control.renew("x", result["token"], 5)["lease_until_monotonic"] == 105.5
    with pytest.raises(ValueError): control.release("x", "wrong")
    with pytest.raises(ValueError): control.permit("x", participants=["r1"], arrived=["r1"])


def test_subprocess_application_reads_and_acknowledges_durable_command(tmp_path):
    path = tmp_path / "real-mailbox"
    runtime.initialize(path, configuration())
    control = runtime.RuntimeControl(path, "job123:attempt1")
    control.submit("actual", "learning-rate", {"value": .25}, expected_epoch=0)
    code = """import sys
from raven.runtime_control import RuntimeControl
c=RuntimeControl(sys.argv[1], 'job123:attempt1')
c.safe_point('epoch', lambda command, params, request: {'observed':params['value']})
"""
    process = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    status = control.status()
    assert status["requests"][0]["result"]["result"] == {"observed": .25}
    assert status["epochs"][control.attempt] == 1
    inspected = subprocess.run([sys.executable, "-m", "raven.runtime_control", "--mailbox", str(path), "--attempt", control.attempt, "status"], capture_output=True, text=True, timeout=10)
    assert inspected.returncode == 0, inspected.stderr
    assert json.loads(inspected.stdout)["requests"][0]["status"] == "applied"


def test_receipt_lookup_and_partial_outcome_are_attempt_scoped(mailbox):
    path, clock, control = mailbox
    control.submit("partial", "learning-rate", {"value": .1}, expected_epoch=0)
    claim = control.poll("epoch")
    result = control.mark_uncertain("partial", claim["claim"], {"failed_ranks": [2]})
    assert result["status"] == "uncertain"
    assert control.inspect_request("partial")["result"] == {"uncertain": {"failed_ranks": [2]}}
    other = runtime.RuntimeControl(path, "job124:attempt1", clock=lambda: clock[0])
    with pytest.raises(ValueError): other.inspect_request("partial")
    control.acknowledge("partial", claim["claim"], applied=True, result={"reconciled": True})
    assert control.inspect_request("partial")["status"] == "applied"


@pytest.mark.parametrize("value", ['{"a":1,"a":2}', '{"a":NaN}', '[' * 2000 + '0' + ']' * 2000, ' ' * 65537], ids=["duplicate", "nonfinite", "depth", "size"])
def test_runtime_cli_json_refuses_ambiguous_or_unbounded_inputs(value):
    with pytest.raises(ValueError): runtime._json_argument(value)
