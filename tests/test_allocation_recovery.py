"""Persisted recovery faults use deterministic fake commands, not live Slurm."""
import json
import os
import threading
import time

import pytest

from raven.raven_allocation import AllocationManager
from raven.raven_allocation_adapters import prepare, build_launch
from raven.raven_allocation_helper import OwnershipRecord
from raven.raven_allocation_recovery import (MAX_ATTEMPTS, connection_binding, receipt_path, reconcile)
from raven.slurm import CommandError


def config():
    return {"site": {"kind": "slurm", "cluster_id": "fixture"}, "connection": {"transport": "local"},
            "execution": {"heavy_target": "slurm", "heavy_requires_allocation": True},
            "allocation": {"cpus_per_task": 2, "memory_mib": 512, "walltime_seconds": 30}}


def receipt(tmp_path, **updates):
    plan = prepare(config())
    value = {"request_id": "a" * 32, "state": "pending", "cleanup_uncertain": True,
             "request_name": "raven-heavy-" + "a" * 32, "job_id": "71", "ownership": "dedicated-allocation",
             "connection_binding": connection_binding(plan), "created_at": 1792279500.,
             "cleanup_on_loss": True, "identity": {}}
    value.update(updates)
    owned = OwnershipRecord(tmp_path, plan, value["request_id"])
    owned.write(value); owned.close()
    return plan, value


class Scheduler:
    ledger = object()
    def __init__(self, *, job_state="RUNNING", name=None, uid=None, accounting="", version="26.05.5", client=None):
        self.calls = []
        self.job_state = job_state
        self.name = name or "raven-heavy-" + "a" * 32
        self.uid = os.getuid() if uid is None else uid
        self.accounting = accounting
        self.version = version
        self.client = client or version
        self.error = None
        self.cancel_error = None
        self.cluster = "fixture"
    def command(self, argv, timeout, limit, cancel):
        self.calls.append(argv)
        assert timeout == 8. and limit <= 262144
        if self.error:
            raise CommandError(self.error)
        if argv[:3] == ["scontrol", "show", "config"]:
            return "ClusterName = " + self.cluster + "\nSLURM_VERSION = " + self.version + "\n"
        if argv[:3] == ["scontrol", "show", "job"]:
            if self.job_state == "missing":
                raise CommandError("Invalid job id specified")
            return f"JobId=71 JobName={self.name} JobState={self.job_state} UserId=owner({self.uid}) SubmitTime=2026-10-17T00:00:00 StartTime=2026-10-17T00:00:01"
        if argv == ["scancel", "--version"]:
            return "slurm " + self.client + "\n"
        if argv[:2] == ["scancel", "--ctld"]:
            if self.cancel_error:
                raise CommandError(self.cancel_error)
            return ""
        if argv[0] == "squeue":
            return "71\n" if self.job_state != "missing" else ""
        if argv[0] == "sacct":
            assert "-D" in argv and "-S" in argv and "-E" in argv
            assert "1970" not in " ".join(argv)
            return self.accounting
        if argv == ["id", "-u"]:
            return str(self.uid)
        pytest.fail("Unexpected recovery command " + repr(argv))
    @property
    def cancellations(self):
        return [call for call in self.calls if call[:2] == ["scancel", "--ctld"]]


def test_orphan_cancel_requires_terminal_confirmation_and_survives_restart(tmp_path):
    plan, _ = receipt(tmp_path)
    backend = Scheduler()
    first = reconcile(tmp_path, plan, backend, now=1792279500.)
    assert first["cleanup_uncertain"] and first["recovery"]["phase"] == "confirming"
    assert backend.cancellations == [["scancel", "--ctld", "--name=raven-heavy-" + "a" * 32, "--user=" + str(os.getuid()), "71"]]
    durable = json.loads(receipt_path(tmp_path, plan).read_text())
    assert durable["recovery"]["attempts"] == 1
    backend.job_state = "CANCELLED"
    second = reconcile(tmp_path, plan, backend, now=first["recovery"]["next_retry"] + 1)
    assert second["state"] == "light" and not second["cleanup_uncertain"]
    assert len(backend.cancellations) == 1
    assert not json.loads(receipt_path(tmp_path, plan).read_text())["cleanup_uncertain"]


def test_disconnect_before_grant_recovers_by_unique_request_name(tmp_path):
    plan, _ = receipt(tmp_path, job_id="")
    backend = Scheduler(job_state="PENDING")
    result = reconcile(tmp_path, plan, backend, now=1792279500.)
    assert result["cleanup_uncertain"] and backend.calls[1][0] == "squeue"
    assert backend.cancellations[-1][-1] == "71"


@pytest.mark.parametrize("mismatch", ["name", "uid", "cluster", "connection"])
def test_recycled_or_unrelated_allocations_are_never_cancelled(tmp_path, mismatch):
    plan, _ = receipt(tmp_path)
    backend = Scheduler()
    if mismatch == "name": backend.name = "another-job"
    if mismatch == "uid": backend.uid += 1
    if mismatch == "cluster": backend.cluster = "other"
    if mismatch == "connection": plan = {**plan, "ssh_target": "changed"}
    result = reconcile(tmp_path, plan, backend, now=1792279500.)
    assert result["cleanup_uncertain"] and not backend.cancellations


@pytest.mark.parametrize("client,controller", [("23.02.8", "26.05.5"), ("26.05.5", "23.02.8"), ("26.05.99", "26.05.5"), ("26.05.5", "26.05.99")])
def test_client_and_actual_controller_both_require_qualification(tmp_path, client, controller):
    plan, _ = receipt(tmp_path)
    backend = Scheduler(version=controller, client=client)
    result = reconcile(tmp_path, plan, backend, now=1792279500.)
    assert result["cleanup_uncertain"] and "not qualified" in result["error"]
    assert not backend.cancellations


def test_old_terminal_attempt_clears_even_after_numeric_id_recycled(tmp_path):
    plan, value = receipt(tmp_path)
    backend = Scheduler(name="new-unrelated", accounting=f"71|{value['request_name']}|COMPLETED|{os.getuid()}|2026-10-17T00:00:00|2026-10-17T00:00:01|fixture|\n")
    result = reconcile(tmp_path, plan, backend, now=1792279500.)
    assert not result["cleanup_uncertain"] and not backend.cancellations


def test_legacy_active_receipt_remains_blocked_but_exact_terminal_can_clear(tmp_path):
    identity = {"verified": True, "allocation_id": "71", "uid": os.getuid(), "submit_time": "2026-10-17T00:00:00", "start_time": "2026-10-17T00:00:01"}
    plan, _ = receipt(tmp_path, request_name="", connection_binding="", identity=identity)
    backend = Scheduler()
    first = reconcile(tmp_path, plan, backend, now=1792279500.)
    assert first["cleanup_uncertain"] and "Legacy active" in first["error"]
    backend.job_state = "COMPLETED"
    second = reconcile(tmp_path, plan, backend, now=first["recovery"]["next_retry"] + 1)
    assert not second["cleanup_uncertain"] and not backend.cancellations


def test_unavailable_controller_never_unblocks_and_backoff_budget_is_durable(tmp_path):
    plan, _ = receipt(tmp_path)
    backend = Scheduler(job_state="missing")
    backend.error = "controller unavailable"
    now = 1792279500.
    for expected in range(1, MAX_ATTEMPTS + 1):
        result = reconcile(tmp_path, plan, backend, now=now)
        assert result["cleanup_uncertain"] and result["recovery"]["attempts"] == expected
        delay = result["recovery"]["next_retry"] - now
        assert .9 * min(300., 5. * 2 ** (expected - 1)) <= delay <= 1.1 * min(300., 5. * 2 ** (expected - 1))
        now = result["recovery"]["next_retry"] + 1
    assert result["recovery"]["phase"] == "paused"
    calls = len(backend.calls)
    reconcile(tmp_path, plan, backend, now=now + 1000)
    assert len(backend.calls) == calls
    resumed = reconcile(tmp_path, plan, backend, reset=True, now=now + 1001)
    assert resumed["recovery"]["attempts"] == 1 and resumed["cleanup_uncertain"]


@pytest.mark.parametrize("error", ["Permission denied", "controller unavailable", "RPC acknowledgement lost"])
def test_failures_do_not_fall_back_to_bare_job_cancel(tmp_path, error):
    plan, _ = receipt(tmp_path)
    backend = Scheduler(); backend.cancel_error = error
    result = reconcile(tmp_path, plan, backend, now=1792279500.)
    assert result["cleanup_uncertain"] and error in result["error"]
    assert len(backend.cancellations) == 1
    assert all(call[1] in ("--ctld", "--version") for call in backend.calls if call[0] == "scancel")


def test_reused_parent_is_never_cancelled(tmp_path):
    plan, _ = receipt(tmp_path, ownership="owned-step-only")
    backend = Scheduler()
    result = reconcile(tmp_path, plan, backend, now=1792279500.)
    assert result["cleanup_uncertain"] and "parent is never cancelled" in result["error"]
    assert not backend.cancellations


def test_existing_owner_lock_prevents_all_recovery_commands(tmp_path, monkeypatch):
    plan, value = receipt(tmp_path)
    owner = OwnershipRecord(tmp_path, plan, value["request_id"], reconcile=True)
    backend = Scheduler()
    monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("never signal a recorded PID"))
    try:
        with pytest.raises(BlockingIOError):
            reconcile(tmp_path, plan, backend)
        assert backend.calls == []
    finally:
        owner.close()


def test_atomic_request_name_is_added_only_to_dedicated_launch():
    plan = prepare(config())
    argv = build_launch(plan, ["/usr/bin/python", "-m", "raven.raven_worker"], request_id="f" * 32)
    assert argv[1] == "--job-name=raven-heavy-" + "f" * 32
    assert "--job-name" not in " ".join(plan["prefix"])
    with pytest.raises(ValueError):
        build_launch(plan, ["/usr/bin/python"], request_id="bad")


def test_manager_remains_responsive_and_ignores_stale_completion(tmp_path, monkeypatch):
    plan, _ = receipt(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def slow(*args, **kwargs):
        entered.set(); assert release.wait(2)
        return {"state": "light", "cleanup_uncertain": False}
    monkeypatch.setattr("raven.raven_allocation_recovery.reconcile", slow)
    manager = AllocationManager(config(), state_dir=tmp_path)
    manager.collection_backend = Scheduler()
    manager.tick(); assert entered.wait(1)
    start = time.monotonic(); manager.tick()
    assert time.monotonic() - start < .1
    manager.generation += 1
    manager._state = {**manager._state, "state": "reconciling", "cleanup_uncertain": True}
    release.set(); manager._recovery_future[0].result(1)
    manager.tick()
    assert manager.snapshot()["cleanup_uncertain"]
    manager.shutdown()


@pytest.mark.parametrize("schema", [None, "raven.allocation-receipt/v2", "unknown"])
def test_unsupported_receipt_schema_preserved_without_scheduler_actions(tmp_path, schema):
    plan, _ = receipt(tmp_path)
    path = receipt_path(tmp_path, plan)
    value = json.loads(path.read_text())
    if schema is None:
        value.pop("schema")
    else:
        value["schema"] = schema
    path.write_text(json.dumps(value))
    original = path.read_bytes()
    backend = Scheduler()
    with pytest.raises(ValueError, match="schema"):
        reconcile(tmp_path, plan, backend)
    assert path.read_bytes() == original and backend.calls == []


def test_missing_receipt_pauses_without_repeated_background_reads(tmp_path):
    manager = AllocationManager(config(), state_dir=tmp_path)
    manager._state.update(state="reconciling", cleanup_uncertain=True)
    manager.tick(); manager._recovery_future[0].result(1); manager.tick()
    assert manager.snapshot()["cleanup_uncertain"]
    assert manager.snapshot()["recovery"]["phase"] == "paused"
    for _ in range(10): manager.tick()
    assert manager._recovery_future is None
    manager.shutdown()


def test_job_recycled_between_read_and_cancel_is_filtered_atomically(tmp_path):
    plan, _ = receipt(tmp_path)
    class Crossing(Scheduler):
        def command(self, argv, *args):
            if argv[:2] == ["scancel", "--ctld"]:
                # The numeric ID now belongs to a different request. The
                # qualified controller must evaluate the name/UID together.
                assert "--name=" + self.name in argv
                self.name = "new-request-after-observation"
                self.calls.append(argv)
                raise CommandError("Job name filter does not match")
            return super().command(argv, *args)
    backend = Crossing()
    result = reconcile(tmp_path, plan, backend, now=1792279500.)
    assert result["cleanup_uncertain"] and "filter does not match" in result["error"]
    assert len(backend.cancellations) == 1


def test_revoked_read_permission_preserves_uncertainty_without_mutation(tmp_path):
    plan, _ = receipt(tmp_path)
    backend = Scheduler(); backend.error = "Permission denied by controller"
    result = reconcile(tmp_path, plan, backend, now=1792279500.)
    assert result["cleanup_uncertain"] and "Permission denied" in result["error"]
    assert not backend.cancellations


def test_crash_after_budget_reservation_cannot_replay_without_accounting_for_attempt(tmp_path, monkeypatch):
    plan, _ = receipt(tmp_path)
    def crash(*args, **kwargs): raise SystemExit("crash after reservation")
    with monkeypatch.context() as patch:
        patch.setattr("raven.raven_allocation_recovery._observe", crash)
        with pytest.raises(SystemExit): reconcile(tmp_path, plan, Scheduler(), now=1792279500.)
    durable = json.loads(receipt_path(tmp_path, plan).read_text())
    assert durable["recovery"]["attempts"] == 1 and durable["cleanup_uncertain"]
    result = reconcile(tmp_path, plan, Scheduler(job_state="COMPLETED"), now=durable["recovery"]["next_retry"] + 1)
    assert not result["cleanup_uncertain"] and result["recovery"]["attempts"] == 2


def test_disk_failure_before_reservation_sends_no_scheduler_command(tmp_path, monkeypatch):
    plan, _ = receipt(tmp_path)
    original = receipt_path(tmp_path, plan).read_bytes()
    def fail(*args): raise OSError("disk full")
    monkeypatch.setattr(OwnershipRecord, "write", fail)
    backend = Scheduler()
    with pytest.raises(OSError, match="disk full"):
        reconcile(tmp_path, plan, backend, now=1792279500.)
    assert backend.calls == [] and receipt_path(tmp_path, plan).read_bytes() == original
