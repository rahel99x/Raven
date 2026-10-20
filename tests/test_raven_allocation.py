"""Real local supervisor/worker journeys and fake-executable Slurm faults.

The Slurm fixtures are not site qualification and never contact a scheduler.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

import pytest

from raven.raven_allocation import AllocationManager, Decoder, MAX_FRAME, WorkerProxy, encode
from raven.raven_allocation_adapters import VerifiedReuse, build_launch, prepare, scrub_launch_environment
from raven.raven_allocation_helper import OwnershipRecord


def desktop(**allocation):
    return {"site": {"kind": "desktop"}, "execution": {"desktop_execution": "explicit", "heavy_requires_allocation": False, "heavy_target": "local"},
            "connection": {"transport": "none"}, "resources": {"heavy": {"memory_mib": 512, "max_workers": 2, "max_tasks": 1, "wall_seconds": 30}},
            "allocation": {"idle_release_seconds": 0, **allocation}}


def cluster(**allocation):
    return {"site": {"kind": "slurm", "cluster_id": "fixture"},
            "execution": {"heavy_target": "slurm", "heavy_requires_allocation": True, "worker_python": sys.executable},
            "connection": {"transport": "local"}, "resources": {"heavy": {"memory_mib": 512, "max_workers": 2, "max_tasks": 1, "wall_seconds": 30}},
            "allocation": {"adapter": "slurm-cli", "nodes": 1, "tasks": 1, "cpus_per_task": 2, "memory_mib": 512, "walltime_seconds": 30,
                           "account": "project", "partition": "compute", "idle_release_seconds": 0, **allocation}}


def until(manager, condition, timeout=10):
    stop = time.monotonic() + timeout
    while time.monotonic() < stop:
        manager.tick()
        if condition():
            return
        time.sleep(.005)
    pytest.fail(f"Allocation condition timed out: {manager.snapshot()}")


def drained(manager):
    until(manager, lambda: manager.snapshot()["state"] in ("light", "reconciling"))
    until(manager, lambda: manager._process.poll() is not None)


def placement(candidate, cancel=None):
    assert not cancel or not cancel.is_set()
    return {"probe_id": candidate["probe_id"], "job_record": f"JobId={candidate['job_id']} JobState=RUNNING UserId=test({os.getuid()}) NumCPUs=2 MinMemoryNode=512 SubmitTime=submit StartTime=start TimeLimit=01:00:00 RunTime=00:00:01",
            "step_record": f"StepId={candidate['job_id']}.{candidate['step_id']} CPUs=2 Nodes={candidate['host']}",
            "job_nodes": [candidate["host"]], "step_nodes": [candidate["host"]], "pids": [candidate["pid"]]}


@pytest.fixture
def fake_slurm(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "launches.jsonl"
    script = '''#!/usr/bin/env python3
import json,os,sys,time,subprocess,shlex
name=os.path.basename(sys.argv[0]); args=sys.argv[1:]
with open(os.environ['RAVEN_FAKE_LOG'],'a') as f:
 f.write(json.dumps({'name':name,'argv':args,'parent_hints':sorted(k for k in os.environ if k.startswith(('SBATCH_','SRUN_')) or k=='SLURM_PARENT_TEST')})+'\\n')
mode=os.environ.get('RAVEN_FAKE_MODE','grant')
if name=='ssh':
 os.environ['SLURM_PARENT_TEST']='remote-parent'; command=shlex.split(args[-1]); os.execvp(command[0],command)
elif name=='salloc':
 if mode=='deny':sys.exit(1)
 if mode in ('pending','pending-grant'):
  sys.stderr.write('salloc: Pending job allocation 7654\\n');sys.stderr.flush()
  if mode=='pending':time.sleep(30)
  while not os.path.exists(os.environ['RAVEN_FAKE_GATE']):time.sleep(.01)
 if mode=='disconnect':sys.exit(255)
 os.environ['SLURM_JOB_ID']='7654'
 sys.stderr.write('salloc: Granted job allocation 7654\\n');sys.stderr.flush()
 command=args[args.index('srun'):];sys.exit(subprocess.call(command))
elif name=='srun':
 os.environ['SLURM_JOB_ID']=next((a.split('=',1)[1] for a in args if a.startswith('--jobid=')),os.environ.get('SLURM_JOB_ID','7654'))
 os.environ['SLURM_STEP_ID']='0'
 start=next(i for i,a in enumerate(args) if not a.startswith('-'))
 os.execvpe(args[start],args[start:],os.environ)
else:raise SystemExit('Unexpected scheduler executable')
'''
    for name in ("salloc", "srun", "ssh", "scancel"):
        path = bindir / name
        path.write_text(script)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("RAVEN_FAKE_LOG", str(log))
    return log


@pytest.mark.parametrize("chunk", [1, 2, 3, 4, 7, 16, 100])
def test_generation_protocol_fragmentation(chunk):
    expected = [{"op": "state", "generation": 2, "text": "κόσμος"}, {"op": "end"}]
    data = b"".join(encode(value) for value in expected)
    decoder, got = Decoder(), []
    for offset in range(0, len(data), chunk):
        decoder.feed(data[offset:offset + chunk])
        got.extend(decoder.take())
    assert got == expected


def test_protocol_rejects_size_depth_and_nonfinite():
    decoder = Decoder()
    decoder.feed(struct.pack("!I", MAX_FRAME + 1))
    with pytest.raises(ValueError):
        decoder.take()
    deep = {}
    from raven.raven_protocol import MAX_DEPTH
    for _ in range(MAX_DEPTH + 1):
        deep = {"nested": deep}
    with pytest.raises(ValueError):
        encode(deep)
    with pytest.raises(ValueError):
        encode({"n": float("nan")})


def test_plan_does_not_create_state_or_launch_commands(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Plan must not start a process")
    monkeypatch.setattr("raven.raven_allocation.subprocess.Popen", forbidden)
    manager = AllocationManager(desktop(), state_dir=tmp_path / "absent")
    plan = manager.plan()
    assert plan["site_kind"] == "desktop" and not (tmp_path / "absent").exists()
    assert manager.snapshot()["state"] == "light"


@pytest.mark.parametrize("mutation", ["desktop_implicit", "login", "target", "wrapper", "tasks", "nodes", "injection"])
def test_adapters_reject_ambiguous_or_unapproved_launches(mutation):
    cfg = cluster()
    if mutation == "desktop_implicit":
        cfg = desktop()
        cfg["execution"]["desktop_execution"] = "disabled"
    elif mutation == "login":
        cfg["execution"]["allow_login_heavy"] = True
    elif mutation == "target":
        cfg["execution"]["heavy_target"] = "local"
    elif mutation == "wrapper":
        cfg["allocation"]["adapter"] = "arbitrary-shell-command"
    elif mutation in ("tasks", "nodes"):
        cfg["allocation"][mutation] = 2
    else:
        cfg["allocation"]["account"] = "a; touch unwanted"
    with pytest.raises(ValueError):
        prepare(cfg)


def test_new_slurm_plan_is_one_explicit_worker_with_no_implicit_overlap():
    value = prepare(cluster(qos="test", gpus=1))
    prefix = value["prefix"]
    assert prefix.count("salloc") == prefix.count("srun") == 1
    assert prefix.count("--ntasks=1") == 2
    assert "--overlap" not in prefix and "--exact" not in prefix
    assert "--gpus=1" in prefix and "--account=project" in prefix
    assert value["qualification"]["real_slurm"] == "untested"


def test_launch_environment_removes_parent_scheduler_hints_without_editing_original():
    original = {"PATH": "/bin", "HOME": "/example", "SLURM_JOB_ID": "1", "SBATCH_NTASKS": "999", "SRUN_GPUS": "8", "MODULEPATH": "retained"}
    value = scrub_launch_environment(original, cpus=2)
    assert value["PATH"] == "/bin" and value["MODULEPATH"] == "retained"
    assert not any(k.startswith(("SLURM_", "SBATCH_", "SRUN_")) for k in value)
    assert original["SLURM_JOB_ID"] == "1"
    assert value["OMP_NUM_THREADS"] == "2"


def test_ssh_argv_quotes_complete_remote_command_once():
    cfg = cluster()
    cfg["connection"] = {"transport": "ssh", "host": "approved-alias"}
    cfg["execution"]["worker_python"] = "/opt/runtime with spaces/python"
    planned = prepare(cfg)
    argv = build_launch(planned, [sys.executable, "-m", "raven.raven_worker"])
    assert argv[:2] == ["ssh", "-T"] and argv[-2] == "approved-alias"
    remote = shlex.split(argv[-1])
    assert remote[0] == cfg["execution"]["worker_python"]
    assert remote.count("salloc") == remote.count("srun") == 1
    assert "StrictHostKeyChecking=no" not in " ".join(argv)


def test_ssh_launch_preserves_governed_connection_routing_and_safe_options():
    cfg = cluster()
    cfg["connection"] = {"transport": "ssh", "host": "approved-alias", "user": "researcher",
                         "ssh_options": ["-p", "2222", "-i", "/keys/key with spaces", "-o", "ProxyJump=gateway"]}
    value = prepare(cfg)
    argv = build_launch(value, [sys.executable, "-m", "raven.raven_worker"])
    assert argv[-2] == "researcher@approved-alias"
    assert argv[10:-3] == cfg["connection"]["ssh_options"]
    assert argv[6:10] == ["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
    for options in (["-tt"], ["another-host"], ["-o", "ProxyCommand=command"], ["-o", "StrictHostKeyChecking=no"], ["-o", "RemoteCommand=command"]):
        cfg["connection"]["ssh_options"] = options
        with pytest.raises(ValueError):
            prepare(cfg)


def test_reuse_requires_fresh_owned_reserved_exact_receipt():
    receipt = VerifiedReuse("7654", "fixture", os.getuid(), "submit", "start", time.time(), 60, 2, 512, ("node",), True, True)
    value = prepare(cluster(), receipt)
    assert value["ownership"] == "owned-step-only"
    assert "salloc" not in value["prefix"] and "--overlap" not in value["prefix"]
    assert "--jobid=7654" in value["prefix"] and "--exact" in value["prefix"]
    for bad in ("7654", replace(receipt, owner_uid=os.getuid() + 1), replace(receipt, observed_at=time.time() - 30),
                replace(receipt, reserved_cpus=1), replace(receipt, exclusive_worker_resources=False), replace(receipt, exact_step_supported=False)):
        with pytest.raises(ValueError):
            prepare(cluster(), bad)


def test_desktop_request_ready_real_task_release_and_receipt(tmp_path):
    with AllocationManager(desktop(), state_dir=tmp_path) as manager:
        reviewed = manager.plan()
        request = manager.request(plan=reviewed)
        assert manager.request() == request
        until(manager, lambda: manager.worker.ready)
        assert manager.snapshot()["identity"]["verified"] is True
        document = {"schema": "raven.statistics/v1", "design": "independent", "unit": "independent runs",
                    "control": [{"id": "a", "value": 1}, {"id": "b", "value": 2}, {"id": "c", "value": 3}],
                    "treatment": [{"id": "d", "value": 4}, {"id": "e", "value": 5}, {"id": "f", "value": 6}]}
        task = manager.worker.submit("statistics", {"document": document}, {"test": "exact"})
        until(manager, lambda: manager.worker.tasks.get(task, {}).get("status") in ("committed", "failed"))
        assert manager.worker.tasks[task]["status"] == "committed", manager.worker.tasks[task]
        assert manager.worker.tasks[task]["receipt"]["generation"] == manager.generation
        assert manager.worker.tasks[task]["result"]["result"]["difference"] == 3
        manager.worker.ack(task)
        assert manager.release(generation=manager.generation)
        drained(manager)
        assert manager.snapshot()["state"] == "light"
        receipt = Path(manager.snapshot()["receipt_path"])
        assert receipt.stat().st_mode & 0o777 == 0o600
        record = json.loads(receipt.read_text())
        assert record["state"] == "light" and not record["cleanup_uncertain"]
        assert "nonce" not in receipt.read_text()


def test_desktop_source_worker_from_another_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with AllocationManager(desktop(), state_dir=tmp_path / "state") as manager:
        manager.request()
        until(manager, lambda: manager.worker.ready)
        assert manager.snapshot()["host"] == socket.gethostname()
        task = manager.worker.submit("statistics", {"document": {"schema": "raven.statistics/v1", "design": "independent",
            "unit": "runs", "control": [{"id": "a", "value": 1}, {"id": "b", "value": 2}],
            "treatment": [{"id": "c", "value": 4}, {"id": "d", "value": 5}]}}, {})
        until(manager, lambda: manager.worker.tasks.get(task, {}).get("status") in ("committed", "failed"))
        assert manager.worker.tasks[task]["status"] == "committed", manager.worker.tasks[task]


def test_cancel_before_grant_never_becomes_heavy_and_stale_generation_is_inert(tmp_path):
    with AllocationManager(desktop(), state_dir=tmp_path) as manager:
        manager.request()
        generation = manager.generation
        assert not manager.cancel(generation=generation - 1)
        assert manager.cancel(generation=generation)
        assert manager.cancel(generation=generation)
        drained(manager)
        assert manager.snapshot()["state"] == "light" and not manager.worker.ready


def test_old_worker_proxy_cannot_dispatch_to_a_new_generation(tmp_path):
    with AllocationManager(desktop(), state_dir=tmp_path) as manager:
        manager.request()
        until(manager, lambda: manager.worker.ready)
        previous = manager.worker
        manager.release()
        drained(manager)
        manager.request()
        until(manager, lambda: manager.worker.ready)
        assert not previous.ready
        assert previous.identity == {} and previous.status == "closed"
        assert not previous.close()
        assert manager.worker.ready
        with pytest.raises(RuntimeError, match="not ready"):
            previous.submit("statistics", {"values": [1]}, {})


def test_review_change_is_rejected_before_launch(tmp_path):
    manager = AllocationManager(desktop(), state_dir=tmp_path)
    plan = manager.plan()
    manager.config["resources"]["heavy"]["memory_mib"] = 1024
    with pytest.raises(ValueError, match="review changed"):
        manager.request(plan=plan)
    assert manager._process is None


def test_stale_events_do_not_change_current_request(tmp_path):
    manager = AllocationManager(desktop(), state_dir=tmp_path)
    manager.generation, manager.request_id = 2, "current"
    assert not manager._apply({"op": "state", "generation": 1, "request_id": "old", "sequence": 50, "state": {"state": "heavy"}})
    assert manager.snapshot()["state"] == "light"


def test_fake_slurm_grant_verifies_placement_and_release_owns_only_its_lifetime(tmp_path, fake_slurm, monkeypatch):
    monkeypatch.setenv("SLURM_PARENT_TEST", "never-inherit")
    monkeypatch.setenv("SBATCH_NTASKS", "999")
    with AllocationManager(cluster(), state_dir=tmp_path / "state", placement_verifier=placement) as manager:
        request = manager.request()
        for _ in range(20):
            assert manager.request() == request
        until(manager, lambda: manager.worker.ready)
        snap = manager.snapshot()
        assert snap["job_id"] == "7654" and snap["identity"]["verified"]
        assert snap["qualification"]["real_slurm"] == "untested"
        manager.release()
        drained(manager)
        assert manager.snapshot()["state"] == "light"
    commands = [json.loads(line) for line in fake_slurm.read_text().splitlines()]
    assert [item["name"] for item in commands] == ["salloc", "srun"]
    assert all(not item["parent_hints"] for item in commands)


@pytest.mark.parametrize("transport", ["local", "ssh"])
def test_pending_allocation_observation_is_presentation_only_until_verified(tmp_path, fake_slurm, monkeypatch, transport):
    gate = tmp_path / "grant"
    monkeypatch.setenv("RAVEN_FAKE_MODE", "pending-grant")
    monkeypatch.setenv("RAVEN_FAKE_GATE", str(gate))
    cfg = cluster()
    if transport == "ssh":
        cfg["connection"] = {"transport": "ssh", "host": "approved-alias"}
    verify_allowed = threading.Event()

    def verify(candidate, cancel=None):
        assert verify_allowed.wait(5), "Test did not release placement verification"
        return placement(candidate, cancel)

    with AllocationManager(cfg, state_dir=tmp_path / "state", placement_verifier=verify) as manager:
        try:
            request = manager.request()
            until(manager, lambda: manager.snapshot().get("job_id") == "7654")
            pending = manager.snapshot()
            assert pending["state"] == "pending" and pending["identity"] == {}
            assert not manager.worker.ready
            observation = pending["allocation_observation"]
            assert observation == {"allocation_id": "7654", "source": "owned-salloc-stderr",
                                   "request_id": request, "generation": manager.generation,
                                   "cluster_id": "fixture", "transport": transport,
                                   **({"owner_uid": os.getuid()} if transport == "local" else {})}
            record = json.loads(Path(pending["receipt_path"]).read_text())
            assert record["allocation_observation"] == observation
            assert record["identity"] == {} and not record["cleanup_uncertain"]
            with pytest.raises(RuntimeError, match="not ready"):
                manager.worker.submit("statistics", {}, {})
            gate.touch()
            until(manager, lambda: manager.snapshot()["state"] == "starting")
            assert manager.snapshot()["job_id"] == "7654"
            assert manager.snapshot()["identity"] == {} and not manager.worker.ready
            verify_allowed.set()
            until(manager, lambda: manager.worker.ready)
            assert manager.snapshot()["identity"]["verified"] is True
            assert manager.snapshot()["allocation_observation"] == {}
            manager.release()
            drained(manager)
            assert manager.snapshot()["state"] == "light"
        finally:
            verify_allowed.set()
    commands = [json.loads(line)["name"] for line in fake_slurm.read_text().splitlines()]
    assert commands == (["ssh"] if transport == "ssh" else []) + ["salloc", "srun"]


@pytest.mark.parametrize("outside_step", [False, True])
def test_real_slurm_step_fields_admit_only_the_allocated_worker(tmp_path, fake_slurm, outside_step):
    from raven.platform_session import wrap_backend
    from raven.raven_collection import make_placement_verifier

    class ControllerRecords:
        candidate = None

        def __init__(self):
            self.commands = []

        def run(self, argv, timeout=8):
            self.commands.append(tuple(argv))
            probe = self.candidate
            if argv == ["scontrol", "show", "job", probe["job_id"], "-o"]:
                return (f"JobId={probe['job_id']} JobState=RUNNING UserId=test({os.getuid()}) "
                        f"NumCPUs=2 MinMemoryNode=512 NodeList={probe['host']} TimeLimit=01:00:00 RunTime=00:00:01"), .001
            if argv == ["scontrol", "listpids", probe["job_id"] + "." + probe["step_id"]]:
                return ("PID JOBID STEPID LOCALID GLOBALID\n"
                        f"{probe['pid']} {probe['job_id']} {probe['step_id']} 0 0\n"), .001
            assert argv == ["scontrol", "show", "step", probe["job_id"] + "." + probe["step_id"], "-o"]
            node = "outside-allocation.invalid" if outside_step else probe["host"]
            return (f"StepId={probe['job_id']}.{probe['step_id']} State=RUNNING "
                    f"NodeList={node} Nodes=1 CPUs=2"), .001

    raw = ControllerRecords()
    governed = wrap_backend(raw, {"raven": {"collection": {}},
                                  "collection_cache_dir": str(tmp_path / "cache")},
                            {"cluster": "fixture", "user": "test"}, None)
    read_placement = make_placement_verifier(governed)

    def verify(candidate, cancel=None):
        raw.candidate = candidate
        return read_placement(candidate, cancel=cancel)

    with AllocationManager(cluster(), state_dir=tmp_path / "state", placement_verifier=verify) as manager:
        manager.request()
        until(manager, lambda: manager.worker.ready or bool(manager.snapshot()["error"]))
        if outside_step:
            assert not manager.worker.ready
            assert "Worker host is outside" in manager.snapshot()["error"]
        else:
            assert manager.snapshot()["state"] == "heavy"
            assert manager.snapshot()["identity"]["verified"]
            manager.release()
        drained(manager)
        assert manager.snapshot()["state"] == "light"

    assert len(raw.commands) == 3
    assert raw.commands[-1] == ("scontrol", "listpids", "7654.0")
    assert governed.ledger.report()["rates_last_minute"]["validation"] == 3


@pytest.mark.parametrize("mode", ["pending", "deny", "disconnect"])
def test_fake_slurm_uncertainty_never_falls_back_or_resubmits(tmp_path, fake_slurm, monkeypatch, mode):
    monkeypatch.setenv("RAVEN_FAKE_MODE", mode)
    with AllocationManager(cluster(pending_timeout_seconds=1), state_dir=tmp_path / "state", placement_verifier=placement) as manager:
        original = manager.request()
        if mode == "pending":
            until(manager, lambda: manager.snapshot()["state"] == "pending")
            manager.cancel()
        drained(manager)
        assert manager.snapshot()["state"] == "reconciling"
        assert manager.snapshot()["cleanup_uncertain"] and not manager.worker.ready
        assert manager.request() == original
    commands = [json.loads(line) for line in fake_slurm.read_text().splitlines()]
    assert [item["name"] for item in commands] == ["salloc"]


def test_slurm_without_governed_verifier_refuses_heavy(tmp_path, fake_slurm):
    with AllocationManager(cluster(), state_dir=tmp_path / "state") as manager:
        manager.request()
        drained(manager)
        assert not manager.worker.ready
        assert "governed placement verifier" in manager.snapshot()["error"]


def test_fake_ssh_uses_remote_scrub_and_preserves_same_request(tmp_path, fake_slurm):
    cfg = cluster()
    cfg["connection"] = {"transport": "ssh", "host": "approved-alias"}
    with AllocationManager(cfg, state_dir=tmp_path / "state", placement_verifier=placement) as manager:
        manager.request()
        until(manager, lambda: manager.worker.ready)
        assert manager.snapshot()["target"] == "approved-alias"
        manager.release()
        drained(manager)
        assert manager.snapshot()["state"] == "light"
    commands = [json.loads(line) for line in fake_slurm.read_text().splitlines()]
    assert [item["name"] for item in commands] == ["ssh", "salloc", "srun"]
    assert all(not item["parent_hints"] for item in commands if item["name"] != "ssh")


def test_fake_reuse_launches_only_owned_step_and_never_cancels_parent(tmp_path, fake_slurm):
    receipt = VerifiedReuse("7654", "fixture", os.getuid(), "submit", "start", time.time(), 60, 2, 512, (socket.gethostname(),), True, True)
    with AllocationManager(cluster(), state_dir=tmp_path / "state", placement_verifier=placement) as manager:
        manager.request(reuse=receipt)
        until(manager, lambda: manager.worker.ready)
        assert manager.snapshot()["ownership"] == "owned-step-only"
        manager.release()
        drained(manager)
    commands = [json.loads(line) for line in fake_slurm.read_text().splitlines()]
    assert [item["name"] for item in commands] == ["srun"]


def test_prior_uncertain_receipt_blocks_new_allocation(tmp_path):
    planned = prepare(desktop())
    record = OwnershipRecord(tmp_path, planned, "a" * 32)
    record.write({"state": "pending", "request_id": "a" * 32, "cleanup_uncertain": True})
    record.close()
    with AllocationManager(desktop(), state_dir=tmp_path) as manager:
        manager.request()
        until(manager, lambda: manager.snapshot()["state"] == "reconciling")
        assert "previous allocation" in manager.snapshot()["error"]
        assert not manager.worker.ready


def test_context_lock_prevents_duplicate_ui_allocations(tmp_path):
    with AllocationManager(desktop(), state_dir=tmp_path) as first, AllocationManager(desktop(), state_dir=tmp_path) as second:
        first.request()
        until(first, lambda: first.worker.ready)
        second.request()
        until(second, lambda: second.snapshot()["state"] == "reconciling")
        assert first.worker.ready and not second.worker.ready


def test_worker_lease_expiry_releases_to_light(tmp_path):
    cfg = desktop()
    cfg["resources"]["heavy"]["wall_seconds"] = 1
    with AllocationManager(cfg, state_dir=tmp_path) as manager:
        manager.request()
        until(manager, lambda: manager.worker.ready)
        drained(manager)
        assert manager.snapshot()["state"] == "light" and not manager.worker.ready


def test_idle_release_does_not_wait_on_mouse_activity(tmp_path):
    with AllocationManager(desktop(idle_release_seconds=1), state_dir=tmp_path) as manager:
        manager.request()
        until(manager, lambda: manager.worker.ready)
        drained(manager)
        assert "Idle compute released" in manager.snapshot()["error"]


def reuse_record(**fields):
    values = {"JobId": "7654", "JobState": "RUNNING", "UserId": f"tester({os.getuid()})", "NumCPUs": "4",
              "Account": "project", "Partition": "compute",
              "NodeList": socket.gethostname(), "AllocTRES": "cpu=4,mem=1G", "TimeLimit": "01:00:00", "RunTime": "00:05:00",
              "SubmitTime": "2026-10-01T00:00:00", "StartTime": "2026-10-01T00:01:00", **fields}
    return " ".join(key + "=" + value for key, value in values.items())


class ReuseBackend:
    ledger = object()

    def __init__(self, record=None):
        self.record, self.calls = record or reuse_record(), []

    def command(self, argv, timeout, limit, cancel=None):
        self.calls.append((argv, timeout, limit))
        return self.record


def test_reuse_review_is_async_governed_and_token_bound(tmp_path, fake_slurm):
    def exact_placement(candidate, cancel=None):
        result = placement(candidate, cancel)
        result["job_record"] = result["job_record"].replace("SubmitTime=submit StartTime=start", "SubmitTime=2026-10-01T00:00:00 StartTime=2026-10-01T00:01:00")
        return result
    with AllocationManager(cluster(), state_dir=tmp_path / "state", placement_verifier=exact_placement) as manager:
        manager.collection_backend = backend = ReuseBackend()
        token = manager.request_reuse_review("7654")
        assert manager.snapshot()["reuse_review"]["state"] == "reviewing"
        assert manager._process is None
        until(manager, lambda: manager.snapshot()["reuse_review"]["state"] == "ready")
        reviewed = manager.snapshot()["reuse_review"]
        assert len(backend.calls) == 1
        assert backend.calls[0][0] == ["scontrol", "show", "job", "7654", "-o"]
        assert reviewed["plan"]["ownership"] == "owned-step-only"
        assert reviewed["expires_at"] > time.monotonic()
        assert "--overlap" not in reviewed["plan"]["prefix"]
        manager.request(reuse=token, plan=reviewed["plan"])
        until(manager, lambda: manager.worker.ready)
        manager.release()
        drained(manager)
    assert [json.loads(line)["name"] for line in fake_slurm.read_text().splitlines()] == ["srun"]


@pytest.mark.parametrize("changed", [
    {"UserId": f"other({os.getuid() + 1})"}, {"JobId": "999"}, {"JobState": "PENDING"},
    {"SubmitTime": "Unknown"}, {"StartTime": "Unknown"}, {"NodeList": "(null)"},
    {"NumCPUs": "1"}, {"AllocTRES": "cpu=4,mem=128M"}, {"RunTime": "00:59:40"},
    {"Cluster": "another"}, {"NodeList": "node[1-2]"},
])
def test_reuse_review_rejects_unproven_ownership_capacity_and_attempt(changed):
    from raven.raven_allocation_reuse import review
    with pytest.raises(ValueError):
        review(ReuseBackend(reuse_record(**changed)), "7654", prepare(cluster()))


def test_reuse_token_cannot_be_replayed_after_expiry_or_generation(tmp_path):
    with AllocationManager(cluster(), state_dir=tmp_path) as manager:
        manager.collection_backend = ReuseBackend()
        token = manager.request_reuse_review("7654")
        until(manager, lambda: manager.snapshot()["reuse_review"]["state"] == "ready")
        original = manager._reuse_receipt
        manager._reuse_receipt = (*original[:3], time.monotonic() - 1)
        with pytest.raises(ValueError, match="expired"):
            manager.request(reuse=token)
        manager._reuse_receipt = original
        manager.generation += 1
        with pytest.raises(ValueError, match="expired"):
            manager.request(reuse=token)
        assert manager._process is None


def test_remote_reuse_verifies_remote_uid_instead_of_desktop_uid():
    from raven.raven_allocation_reuse import review
    remote_uid = os.getuid() + 20000
    class RemoteBackend(ReuseBackend):
        def command(self, argv, timeout, limit, cancel=None):
            self.calls.append((argv, timeout, limit))
            return str(remote_uid) + "\n" if argv == ["id", "-u"] else reuse_record(UserId=f"remote({remote_uid})")
    cfg = cluster()
    cfg["connection"] = {"transport": "ssh", "host": "approved-alias", "user": "remote"}
    backend = RemoteBackend()
    receipt = review(backend, "7654", prepare(cfg))
    assert receipt.owner_uid == remote_uid
    assert prepare(cfg, receipt)["ownership"] == "owned-step-only"
    assert [call[0] for call in backend.calls] == [["id", "-u"], ["scontrol", "show", "job", "7654", "-o"]]


def test_missing_launch_executable_is_confirmed_light_without_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "absent"))
    with AllocationManager(cluster(), state_dir=tmp_path / "state") as manager:
        manager.request()
        drained(manager)
        assert manager.snapshot()["state"] == "light"
        assert not manager.snapshot()["cleanup_uncertain"]
        assert "unavailable" in manager.snapshot()["error"]


def test_task_with_declared_deadline_beyond_lease_is_rejected(tmp_path):
    with AllocationManager(desktop(), state_dir=tmp_path) as manager:
        manager.request()
        until(manager, lambda: manager.worker.ready)
        task = manager.worker.submit("statistics", {"document": {}}, {}, budget={"wall_seconds": 60})
        until(manager, lambda: manager.worker.tasks[task].get("status") == "failed")
        assert "remaining allocation lease" in manager.worker.tasks[task]["error"]
        assert manager.worker.ready


def test_captured_light_policy_still_blocks_later_heavy_request(tmp_path):
    from raven.raven_config import resolve
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"schema": "raven.site-policy/v1", "restrictions": {"deny_heavy": True}}))
    resolved = resolve(site="desktop", env={"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config"),
                       "XDG_STATE_HOME": str(tmp_path / "state")}, system_policy_paths=[str(policy)])
    manager = AllocationManager(resolved, state_dir=tmp_path / "state")
    with pytest.raises(ValueError, match="disables new Heavy"):
        manager.plan()
    assert manager._process is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux parent-death ownership primitive")
def test_helper_crash_stops_owned_desktop_worker_and_refuses_replay(tmp_path):
    # A dedicated test-side subreaper lets this fault test itself reap a child
    # whose intentionally killed supervisor can no longer collect it.
    code = '''
import ctypes,json,os,signal,sys,time
from raven.raven_allocation import AllocationManager
assert ctypes.CDLL(None).prctl(36,1,0,0,0)==0
manager=AllocationManager(json.loads(sys.argv[1]),state_dir=sys.argv[2])
try:
 manager.request(); deadline=time.monotonic()+8
 while not manager.worker.ready and time.monotonic()<deadline:
  manager.tick();time.sleep(.005)
 assert manager.worker.ready,manager.snapshot()
 worker=manager.snapshot()['identity']['pid']; request=manager.request_id
 manager._process.kill();manager._process.wait(timeout=2)
 manager.tick()
 assert manager.snapshot()['state']=='reconciling'
 assert manager.request()==request
 deadline=time.monotonic()+5
 while time.monotonic()<deadline:
  try:
   found,status=os.waitpid(worker,os.WNOHANG)
  except ChildProcessError:
   found=worker
  if found==worker:break
  time.sleep(.01)
 else:raise AssertionError('owned worker survived its supervisor')
finally:manager.shutdown()
'''
    result = subprocess.run([sys.executable, "-c", code, json.dumps(desktop()), str(tmp_path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
