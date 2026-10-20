"""Reproductions from the adversarial review; scheduler journeys use fakes."""
from concurrent.futures import Future
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from raven.raven_allocation import AllocationManager, Decoder, WorkerProxy, encode
from raven.raven_allocation_adapters import VerifiedReuse, prepare, scrub_launch_environment
from raven.raven_allocation_helper import OwnershipRecord, Supervisor, boot_identity
from raven.raven_allocation_recovery import connection_binding, forget, receipt_path, reconcile
from raven.raven_config import DEFAULTS, merge
from raven.raven_protocol import canonical, encode as worker_encode
from raven.raven_worker_client import WorkerClient
from test_allocation_recovery import Scheduler, config, receipt
from test_raven_allocation import fake_slurm as fake_slurm, cluster, desktop, placement, until, reuse_record
from test_raven_worker_runtime import config as worker_config, inputs


def test_hv2_fractional_creation_time_reaches_accounting(tmp_path):
    plan, value = receipt(tmp_path, created_at=time.time())
    backend = Scheduler(job_state="missing", accounting=f"71|{value['request_name']}|COMPLETED|{os.getuid()}|2026-10-17T00:00:00|2026-10-17T00:00:01|fixture|\n")
    result = reconcile(tmp_path, plan, backend)
    assert result["state"] == "light"
    accounting = next(argv for argv in backend.calls if argv[0] == "sacct")
    assert "." not in accounting[accounting.index("-S") + 1]


def test_hv1_default_cluster_receipt_uses_actual_controller_and_stable_key(tmp_path):
    plan, _ = receipt(tmp_path, cluster_id="fixture")
    empty = {**plan, "cluster_id": ""}
    assert receipt_path(tmp_path, plan) == receipt_path(tmp_path, empty)
    assert connection_binding(plan) == connection_binding(empty)
    backend = Scheduler(job_state="COMPLETED")
    result = reconcile(tmp_path, empty, backend)
    assert result["state"] == "light"
    assert not backend.cancellations


def test_hv1_legacy_cluster_key_is_migrated_without_hiding_its_receipt(tmp_path):
    plan, value = receipt(tmp_path, cluster_id="fixture")
    canonical_path = receipt_path(tmp_path, plan)
    old_key = hashlib.sha256(json.dumps({"site": "slurm", "cluster": "fixture", "target": plan["target"]}, sort_keys=True).encode()).hexdigest()[:32]
    legacy_path = canonical_path.with_name(old_key + ".json")
    canonical_path.rename(legacy_path)
    changed = {**plan, "cluster_id": ""}
    result = reconcile(tmp_path, changed, Scheduler(job_state="COMPLETED"))
    assert result["state"] == "light" and canonical_path.exists() and not legacy_path.exists()


def test_hv1_managed_default_cluster_is_resolved_off_ui_before_salloc(tmp_path, fake_slurm, monkeypatch):
    configuration = merge(copy.deepcopy(DEFAULTS), cluster())
    configuration["site"]["cluster_id"] = ""
    configuration["storage"]["allocation_receipts_dir"] = str(tmp_path / "durable" / "allocations")
    configuration["execution"]["worker_python"] = sys.executable
    backend = Scheduler()
    threads = []
    original = backend.command
    def observe(*args, **kwargs):
        threads.append(threading.current_thread().name)
        return original(*args, **kwargs)
    backend.command = observe
    monkeypatch.setattr("raven.raven_collection.admit_allocation_launch", lambda *args: {"admitted": True})
    with AllocationManager(configuration, state_dir=tmp_path / "local", placement_verifier=placement) as manager:
        manager.collection_backend = backend
        manager.request()
        until(manager, lambda: manager.worker.ready)
        assert manager.snapshot()["cluster_id"] == "fixture"
        assert threads and all(thread != "MainThread" for thread in threads)
        path = Path(manager.snapshot()["receipt_path"])
        assert path.parent == tmp_path / "durable" / "allocations"
        assert json.loads(path.read_text())["cluster_id"] == "fixture"
        manager.release(); until(manager, lambda: manager.snapshot()["state"] == "light")


def test_hv1_unverifiable_controller_fails_before_any_allocation(tmp_path, fake_slurm, monkeypatch):
    configuration = merge(copy.deepcopy(DEFAULTS), cluster())
    configuration["site"]["cluster_id"] = ""
    backend = Scheduler(); backend.error = "controller inaccessible"
    monkeypatch.setattr("raven.raven_collection.admit_allocation_launch", lambda *args: pytest.fail("must not admit before controller proof"))
    with AllocationManager(configuration, state_dir=tmp_path / "local") as manager:
        manager.collection_backend = backend
        manager.request()
        until(manager, lambda: manager.snapshot()["state"] == "light")
        assert manager._process is None
        assert "controller inaccessible" in manager.snapshot()["error"]


def test_hv1_default_cluster_reuse_binds_controller_when_job_omits_cluster():
    from raven.raven_allocation_reuse import review

    configuration = cluster()
    configuration["site"]["cluster_id"] = ""

    class Backend:
        ledger = object()

        def __init__(self):
            self.calls = []

        def command(self, argv, timeout, limit, cancel=None):
            self.calls.append((argv, timeout, limit))
            if argv == ["scontrol", "show", "config"]:
                return "ClusterName = fixture\nSLURM_VERSION = 26.05.5\n"
            assert argv == ["scontrol", "show", "job", "7654", "-o"]
            return reuse_record()

    backend = Backend()
    verified = review(backend, "7654", prepare(configuration))
    assert verified.cluster == "fixture"
    planned = prepare(configuration, verified)
    assert planned["cluster_id"] == "fixture"
    assert planned["ownership"] == "owned-step-only"
    assert planned["reuse"]["cluster"] == "fixture"
    assert prepare(configuration, verified, observed_cluster="fixture") == planned
    with pytest.raises(ValueError, match="owner, cluster"):
        prepare(configuration, verified, observed_cluster="another")
    assert [call[0] for call in backend.calls] == [
        ["scontrol", "show", "config"],
        ["scontrol", "show", "job", "7654", "-o"],
    ]

    backend.command = lambda argv, *args: (
        "ClusterName = fixture\nSLURM_VERSION = 26.05.5\n"
        if argv == ["scontrol", "show", "config"]
        else reuse_record(Cluster="another")
    )
    with pytest.raises(ValueError, match="another cluster"):
        review(backend, "7654", prepare(configuration))


@pytest.mark.parametrize("config_record", ["", "ClusterName = a\nClusterName = b\nSLURM_VERSION = 26.05.5\n"])
def test_hv1_default_cluster_reuse_rejects_unverified_controller(config_record):
    from raven.raven_allocation_reuse import review

    configuration = cluster()
    configuration["site"]["cluster_id"] = ""

    class Backend:
        ledger = object()

        def command(self, argv, timeout, limit, cancel=None):
            assert argv == ["scontrol", "show", "config"]
            return config_record

    with pytest.raises(ValueError, match="actual controller ClusterName"):
        review(Backend(), "7654", prepare(configuration))


def test_hv14_recovery_identity_does_not_request_heavy_entitlement(tmp_path, monkeypatch):
    manager = AllocationManager(config(), state_dir=tmp_path)
    manager.collection_backend = Scheduler()
    monkeypatch.setattr(manager, "plan", lambda *a, **kw: (_ for _ in ()).throw(ValueError("Heavy forbidden")))
    try:
        manager.tick()
        assert manager._recovery_future is not None
        manager._recovery_future[0].result(2)
        manager.tick()
        assert not manager.snapshot()["cleanup_uncertain"]
    finally:
        manager.shutdown()


def test_hv13_desktop_orphan_clear_uses_identity_without_signalling(tmp_path, monkeypatch):
    plan = prepare(desktop())
    record = OwnershipRecord(tmp_path, plan, "b" * 32)
    record.write({"request_id": "b" * 32, "state": "heavy", "cleanup_uncertain": True,
                  "helper_host": socket.gethostname(), "helper_boot": boot_identity(),
                  "helper_pid": os.getpid(), "helper_start": "old-process-start"})
    record.close()
    monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("Recorded PID must never be signalled"))
    result = reconcile(tmp_path, plan, None)
    assert result["state"] == "light" and not result["cleanup_uncertain"]


def test_hv4_two_controller_absence_reads_are_evidence_independent_of_accounting(tmp_path):
    plan, _ = receipt(tmp_path)
    backend = Scheduler(job_state="missing")
    first = reconcile(tmp_path, plan, backend, now=1792279500.)
    assert first["cleanup_uncertain"]
    second = reconcile(tmp_path, plan, backend, reset=False, now=1792279506.)
    assert second["cleanup_uncertain"]  # Less than MessageTimeout + margin.
    last = reconcile(tmp_path, plan, backend, now=1792279520.)
    assert last["state"] == "light" and "two verified controller reads" in last["cleanup_basis"]
    assert not backend.cancellations


def test_hv16_network_receipts_use_local_lock_and_exclusive_shared_claim(tmp_path, monkeypatch):
    import raven.state_ownership as state
    shared, runtime = tmp_path / "shared", tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    original = state.filesystem_kind
    monkeypatch.setattr(state, "filesystem_kind", lambda path: "nfs4" if str(path).startswith(str(shared)) else original(path))
    plan = prepare(cluster())
    owner = OwnershipRecord(shared, plan, "a" * 32)
    try:
        owner.write({"request_id": "a" * 32, "state": "pending", "cleanup_uncertain": True})
        assert owner.path.parent == shared / "allocations"
        assert list((runtime / ("raven-allocation-locks-" + str(os.getuid()))).glob("*.lock"))
        assert owner.path.with_suffix(".claim").exists()
        with pytest.raises(BlockingIOError):
            OwnershipRecord(shared, plan, "b" * 32)
    finally:
        owner.close()


def test_hv1_reviewed_forget_preserves_original_and_does_not_touch_scheduler(tmp_path):
    plan, value = receipt(tmp_path)
    original = receipt_path(tmp_path, plan).read_bytes()
    with pytest.raises(ValueError, match="changed"):
        forget(tmp_path, plan, "b" * 32)
    result = forget(tmp_path, plan, value["request_id"])
    assert result["manually_forgotten"] and result["state"] == "light"
    assert "not verified" in result["cleanup_basis"]
    archives = list(receipt_path(tmp_path, plan).parent.glob("*-forgotten.json"))
    assert len(archives) == 1 and archives[0].read_bytes() == original
    assert not receipt_path(tmp_path, plan).with_suffix(".claim").exists()


def test_hv8_worker_rejections_never_consume_live_admission_forever():
    manager = SimpleNamespace(generation=1, _state={"state": "heavy"}, _cancelled=False,
                              _lock=threading.RLock(), _send=lambda value: True)
    proxy = WorkerProxy(manager)
    for number in range(20):
        task = proxy.submit("statistics", {}, task_id=str(number))
        proxy._event({"op": "error", "id": task, "error": "concurrency full"})
        assert proxy.tasks[task]["status"] == "failed"
    assert proxy.submit("statistics", {}, task_id="after-recovery") == "after-recovery"


def test_hv9_valid_large_worker_payload_can_cross_allocation_relay_without_teardown():
    result = {"matches": [{"line": i, "text": "matched", "source": {"path": "run.log", "offset": i}} for i in range(8000)]}
    message = {"op": "worker", "generation": 1, "request_id": "a" * 32,
               "sequence": 1, "event": {"op": "committed", "id": "task", "result": result}}
    assert len(worker_encode(message)) < 1 << 20
    decoder = Decoder(); decoder.feed(encode(message))
    assert decoder.take()[0]["event"]["result"] == result
    assert len(canonical({"text": "x" * 450000})) > 450000


def test_hv12_unsent_summary_is_replaced_and_terminal_state_survives_pause():
    a, b = socket.socketpair()
    supervisor = Supervisor(a)
    supervisor.generation, supervisor.request_id = 1, "a" * 32
    try:
        for number in range(1000):
            supervisor.emit("state", state={"state": "heavy", "remaining_seconds": 3600 - number})
        assert len(supervisor.out) == 1
        assert not supervisor.stopping and supervisor.out_bytes < 1024
        supervisor.emit("state", state={"state": "light", "cleanup_uncertain": False})
        raw = b"".join(supervisor.out)
        decoder = Decoder(); decoder.feed(raw)
        assert decoder.take()[0]["state"]["state"] == "light"
    finally:
        a.close(); b.close(); supervisor.selector.close()


def test_hv7_deferred_placement_retains_request_and_retries():
    from raven.raven_collection import Deferred
    manager = AllocationManager(cluster())
    manager.generation, manager.request_id = 1, "a" * 32
    manager._state["state"] = "starting"
    candidate = {"probe_id": "probe"}
    failed = Future(); failed.set_exception(Deferred("validation lane busy", retry_after=.01))
    manager._verification = (failed, 1, "a" * 32, candidate, time.monotonic() + 5)
    sent = []
    manager._send = lambda message: sent.append(message) or True
    try:
        manager._finish_verification()
        assert manager._verification_retry is not None and not sent
        assert manager.snapshot()["state"] == "starting"
        manager._placement_verifier = lambda candidate, cancel: {"probe_id": candidate["probe_id"]}
        manager._verification_retry = (0., candidate, time.monotonic() + 5)
        manager._finish_verification()
        if manager._verification:
            manager._verification[0].result(2)
            manager._finish_verification()
        assert sent == [{"op": "placement", "records": {"probe_id": "probe"}}]
    finally:
        manager.shutdown()


def test_hv15_salloc_hints_cannot_change_reviewed_resources_or_controller():
    env = scrub_launch_environment({"PATH": "/bin", "SLURM_CONF": "/approved/slurm.conf", "SALLOC_ACCOUNT": "hidden-lab", "SALLOC_EXCLUSIVE": "1", "SLURM_JOB_ID": "old"})
    assert env["SLURM_CONF"] == "/approved/slurm.conf"
    assert not any(key.startswith("SALLOC_") for key in env)
    assert "SLURM_JOB_ID" not in env


def test_hv20_reuse_review_has_a_human_scale_window_and_rechecks_capacity():
    reviewed = VerifiedReuse("71", "fixture", os.getuid(), "submit", "start", time.time() - 30,
                             3600, 2, 512, ("compute",), True, True)
    assert prepare(cluster(), reviewed)["ownership"] == "owned-step-only"
    with pytest.raises(ValueError, match="expired"):
        prepare(cluster(), replace(reviewed, observed_at=time.time() - 121))
    with pytest.raises(ValueError, match="remaining"):
        prepare(cluster(), replace(reviewed, remaining_seconds=40))


def test_hv5_terminal_owned_step_can_clear_while_parent_is_running(tmp_path):
    plan, value = receipt(tmp_path, ownership="owned-step-only", request_name="raven-step-" + "a" * 32,
                          step_id="0", reuse={"job_id": "71"})
    backend = Scheduler(job_state="RUNNING", accounting=f"71.0|{value['request_name']}|FAILED|{os.getuid()}|2026-10-17T00:00:00|2026-10-17T00:00:01|fixture|\n")
    result = reconcile(tmp_path, plan, backend)
    assert result["state"] == "light" and not backend.cancellations
    assert "parent allocation preserved" in result["cleanup_basis"]


def test_hv3_pending_stop_delivers_catchable_signal_before_hard_kill(tmp_path):
    ready, revoked = tmp_path / "ready", tmp_path / "revoked"
    code = "import pathlib,signal,sys,time\ndef stop(*a):\n pathlib.Path(sys.argv[2]).write_text('revoked'); raise SystemExit(0)\nsignal.signal(signal.SIGTERM,stop)\npathlib.Path(sys.argv[1]).write_text('ready')\ntime.sleep(60)\n"
    client = WorkerClient(worker_config(site_kind="slurm"), launch_argv=[sys.executable, "-c", code, str(ready), str(revoked)], observe_allocation=True)
    try:
        client.start()
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            client.poll(timeout=.01)
        assert ready.exists()
        client.close(timeout=.01)
        assert revoked.read_text() == "revoked" and not client.forced_stop
    finally:
        client.close()


@pytest.mark.parametrize("diagnostic,basis", [
    ("salloc: error: Job submit/allocate failed: Invalid account", "submission rejected"),
    ("salloc: Granted job allocation 71\nsalloc: Relinquishing job allocation 71", "confirmed allocation relinquish"),
])
def test_hv4_hv6_salloc_lifecycle_evidence_is_independent_of_command_exit(tmp_path, diagnostic, basis):
    client = WorkerClient(worker_config(site_kind="slurm"), launch_argv=[sys.executable, "-c", "import sys;sys.stderr.write(sys.argv[1]+'\\n');sys.stderr.flush();raise SystemExit(2)", diagnostic], observe_allocation=True)
    a, b = socket.socketpair(); supervisor = Supervisor(a)
    supervisor.generation, supervisor.request_id = 1, "a" * 32
    supervisor.plan = prepare(cluster())
    supervisor.client = client
    try:
        client.start()
        deadline = time.monotonic() + 3
        while client.process.poll() is None and time.monotonic() < deadline:
            client.poll(timeout=.01)
        client.poll()
        supervisor.drain()
        assert supervisor.state["state"] == "light" and not supervisor.state["cleanup_uncertain"]
        assert basis in supervisor.state["cleanup_basis"]
        if "rejected" in basis:
            assert "Invalid account" in supervisor.state["error"]
    finally:
        client.close(); a.close(); b.close(); supervisor.selector.close()


def test_hv10_two_thousand_acknowledged_tasks_keep_one_local_worker_usable():
    configuration = worker_config()
    configuration["resources"]["wall_seconds"] = 600
    client = WorkerClient(configuration)
    try:
        client.start()
        deadline = time.monotonic() + 5
        while not client.ready and time.monotonic() < deadline:
            client.poll(timeout=.1)
        assert client.ready, client.diagnostics
        original_worker = client.worker_id
        for number in range(2000):
            task_id = client.submit("statistics", inputs(), {"run": "soak", "iteration": number}, task_id="soak-" + str(number))
            result = client.wait(task_id, 3)
            assert result["schema"] == "raven.execution-result/v1"
            assert client.ready and client.worker_id == original_worker
        client.poll(timeout=.1)
        assert len(client.tasks) <= 128 and len(client._retired) <= 4096
        with pytest.raises(ValueError, match="identity"):
            client.submit("statistics", inputs(), {}, task_id="soak-0")
    finally:
        client.close()


def test_hv3_remote_ssh_eof_is_translated_to_catchable_hangup(tmp_path):
    ready, revoked = tmp_path / "remote-ready", tmp_path / "remote-revoked"
    adapter = tmp_path / "salloc"
    adapter.write_text("#!" + sys.executable + "\nimport pathlib,signal,time\n"
        + "def stop(*args):\n pathlib.Path(" + repr(str(revoked)) + ").write_text('revoked');raise SystemExit(0)\n"
        + "signal.signal(signal.SIGHUP,stop)\npathlib.Path(" + repr(str(ready)) + ").write_text('ready')\ntime.sleep(60)\n")
    adapter.chmod(0o700)
    environment = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    process = subprocess.Popen([sys.executable, "-m", "raven.raven_allocation_helper", "--remote-launch", "salloc"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        process.stdin.close()
        process.wait(timeout=5)
        assert revoked.read_text() == "revoked"
    finally:
        if process.poll() is None:
            process.terminate(); process.wait(timeout=5)
        process.stdout.close(); process.stderr.close()


def test_hv13_inaccessible_kernel_identity_is_not_orphan_proof(tmp_path, monkeypatch):
    import raven.raven_allocation_ownership as ownership
    plan = prepare(desktop())
    record = OwnershipRecord(tmp_path, plan, "c" * 32)
    record.write({"request_id": "c" * 32, "state": "heavy", "cleanup_uncertain": True,
                  "helper_host": socket.gethostname(), "helper_boot": "known-boot",
                  "helper_pid": os.getpid(), "helper_start": "known-process-start"})
    record.close()
    monkeypatch.setattr(ownership, "boot_identity", lambda: "unavailable")
    monkeypatch.setattr(ownership, "process_start", lambda pid: "unavailable")
    monkeypatch.setattr(os, "kill", lambda pid, sig: None if sig == 0 else pytest.fail("must not signal"))
    assert reconcile(tmp_path, plan, None)["cleanup_uncertain"]


@pytest.mark.parametrize("transport,code,cleared", [("local", 1, True), ("ssh", 1, True), ("ssh", 255, False)])
def test_hv5_failed_srun_proves_no_step_except_ambiguous_ssh_exit(transport, code, cleared):
    a, b = socket.socketpair(); supervisor = Supervisor(a)
    supervisor.generation, supervisor.request_id = 1, "a" * 32
    supervisor.plan = {"site_kind": "slurm", "ownership": "owned-step-only", "transport": transport}
    supervisor.client = SimpleNamespace(close=lambda **kwargs: None,
        process=SimpleNamespace(poll=lambda: code), allocation_rejected=False,
        allocation_relinquished=False, observed_allocation_id="", forced_stop=False,
        worker_id="", diagnostics="srun: error: Unable to create step: Requested nodes are busy")
    try:
        supervisor.drain()
        assert supervisor.state["cleanup_uncertain"] is not cleared
        assert supervisor.state["state"] == ("light" if cleared else "reconciling")
    finally:
        a.close(); b.close(); supervisor.selector.close()


def test_hv6_already_exited_salloc_final_relinquish_is_drained_on_close():
    diagnostic = "salloc: Granted job allocation 71\nsalloc: Relinquishing job allocation 71\n"
    client = WorkerClient(worker_config(site_kind="slurm"), launch_argv=[sys.executable, "-c", "import sys;sys.stderr.write(sys.argv[1]);raise SystemExit(2)", diagnostic], observe_allocation=True)
    try:
        client.start()
        client.process.wait(timeout=3)
        client.close()
        assert client.allocation_relinquished and not client.forced_stop
    finally:
        client.close()


def test_hv17_future_completion_wakes_wait_even_with_idle_worker_channel():
    manager = AllocationManager(desktop())
    a, b = socket.socketpair()
    manager._socket = a
    future = Future()
    manager._wake_on_completion(future)
    timer = threading.Timer(.02, lambda: future.set_result(True))
    try:
        timer.start()
        start = time.monotonic()
        manager.wait_for_activity(1)
        assert time.monotonic() - start < .5
        assert future.result() is True
    finally:
        timer.join()
        manager.shutdown()
        b.close()
