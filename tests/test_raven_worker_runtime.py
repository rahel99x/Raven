"""Bounded worker transport, placement proof and actual isolated analysis."""
from __future__ import annotations

import json
import os
import socket
import selectors
from pathlib import Path
import struct
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from raven.raven_protocol import Decoder, MAX_FRAME, MAX_QUEUE, MAX_STRING, ProtocolError, build_digest, canonical, digest, encode
from raven.raven_resources import child_environment, limits, placement_probe, task_limits, verify_placement
from raven.raven_worker_client import WorkerClient


def config(**updates):
    return {"site_kind": "desktop", "desktop_explicit": True, "context": {},
            "resources": {"memory_mib": 512, "cpus": 1, "max_tasks": 1, "wall_seconds": 30}, **updates}


def inputs():
    return {"document": {"schema": "raven.statistics/v1", "design": "independent", "unit": "seed",
                         "control": [{"id": "a", "value": 1}, {"id": "b", "value": 2}, {"id": "c", "value": 3}],
                         "treatment": [{"id": "d", "value": 4}, {"id": "e", "value": 5}, {"id": "f", "value": 6}]}}


def until(client, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    observed = []
    while time.monotonic() < deadline:
        observed.extend(client.poll())
        if predicate():
            return observed
        time.sleep(.01)
    pytest.fail(f"Worker did not reach expected state: {client.status} {client.error} {client.diagnostics}")


@pytest.mark.parametrize("chunk", [1, 2, 4, 7, 10000])
def test_fragmented_multiple_frames(chunk):
    values = [{"op": "hello", "text": "界é", "n": 2}, {"op": "admit", "context": {"job": "4"}}]
    raw = b"".join(encode(value) for value in values)
    decoder = Decoder()
    result = []
    for offset in range(0, len(raw), chunk):
        decoder.feed(raw[offset:offset + chunk])
        result.extend(decoder.take())
    assert result == values
    assert not decoder.buffer


@pytest.mark.parametrize("raw", [b'[]', b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'"text"', b'\xff', b'{"x":' + b'[' * 100 + b'0' + b']' * 100 + b'}'])
def test_invalid_frames_fail_closed(raw):
    decoder = Decoder()
    decoder.feed(struct.pack("!I", len(raw)) + raw)
    with pytest.raises(ProtocolError):
        decoder.take()


@pytest.mark.parametrize("value", [{"x": float("inf")}, {"x": 10**500}, {"x": "x" * (MAX_STRING + 1)}, {"x": object()}, {"x": "\ud800"}, {"\ud800": "bad"}, {1: "bad"}])
def test_encoder_limits_before_wire_allocation(value):
    with pytest.raises(ProtocolError):
        encode(value)


def test_receive_frame_and_queue_budgets():
    for size in (0, MAX_FRAME + 1):
        with pytest.raises(ProtocolError):
            Decoder().feed(struct.pack("!I", size))
    with pytest.raises(ProtocolError):
        Decoder().feed(b"x" * (MAX_QUEUE + 1))
    with pytest.raises(ProtocolError):
        encode({"rows": ["x" * 200000] * 6})


def test_digest_is_canonical_and_build_is_nonsecret():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert len(build_digest()) == 64
    assert b"NaN" not in canonical({"finite": 1})


def test_resource_limits_reserve_supervisor_and_do_not_mutate_environment():
    parent = {"SHELL": "/bin/bash", "OMP_NUM_THREADS": "100"}
    env = child_environment(2, parent)
    assert env["OMP_NUM_THREADS"] == "2"
    assert env["OPENBLAS_NUM_THREADS"] == "2"
    assert parent["OMP_NUM_THREADS"] == "100"
    total = limits({"memory_mib": 512, "cpus": 4, "max_tasks": 2})
    task = task_limits({}, total)
    assert task["memory_mib"] * 2 + 64 <= 512
    assert task["cpus"] * 2 <= 4
    with pytest.raises(ValueError):
        task_limits({"memory_mib": 500}, total)
    with pytest.raises(ValueError):
        task_limits({"command": 2}, total)


def slurm_fixture(cgroup=True):
    settings = {"site_kind": "slurm", "context": {"cluster_id": "cluster", "allocation_id": "42", "step_id": "0", "owner_uid": 1000},
                "resources": limits({"cpus": 2})}
    candidate = placement_probe(settings, environ={"SLURM_JOB_ID": "42", "SLURM_STEP_ID": "0"},
                                host="compute1.example", uid=1000, pid=444,
                                cgroups="0::/slurm/job_42/step_0/task_0\n" if cgroup else "0::/user.slice")
    candidate["probe_id"] = "launch-1"
    records = {"probe_id": "launch-1", "job_record": "JobId=42 UserId=user(1000) JobState=RUNNING NumCPUs=4 SubmitTime=a StartTime=b AllocTRES=cpu=4,mem=4G",
               "step_record": "StepId=42.0 CPUs=2", "job_nodes": ["compute1"], "step_nodes": ["compute1.example"]}
    return settings, candidate, records


def test_slurm_env_is_never_sufficient_placement_proof():
    settings, candidate, records = slurm_fixture(False)
    with pytest.raises(ValueError, match="placement is unverified"):
        verify_placement(settings, candidate, records)
    records["pids"] = [444]
    assert verify_placement(settings, candidate, records)["verified"]
    with pytest.raises(ValueError):
        placement_probe(settings, environ={}, host="compute1", uid=1000, pid=444, cgroups="")


def test_verified_identity_retains_observed_allocation_attempt_times():
    settings, candidate, records = slurm_fixture()
    records["job_record"] = records["job_record"].replace("SubmitTime=a", "SubmitTime=2026-10-12T10:00:00").replace(
        "StartTime=b", "StartTime=2026-10-12T10:00:20")
    identity = verify_placement(settings, candidate, records)
    assert identity["submit_time"] == "2026-10-12T10:00:00"
    assert identity["start_time"] == "2026-10-12T10:00:20"


@pytest.mark.parametrize("change", ["job", "step", "owner", "host", "probe", "cpus", "state", "step-state", "memory", "attempt", "hard"])
def test_slurm_mismatches_refuse_heavy(change):
    settings, candidate, records = slurm_fixture()
    if change == "job":
        records["job_record"] = records["job_record"].replace("JobId=42", "JobId=43")
    elif change == "step":
        records["step_record"] = "StepId=42.1 CPUs=2"
    elif change == "owner":
        records["job_record"] = records["job_record"].replace("1000", "2000")
    elif change == "host":
        records["step_nodes"] = ["login1"]
    elif change == "probe":
        records["probe_id"] = "stale"
    elif change == "cpus":
        records["step_record"] = "StepId=42.0 CPUs=1"
    elif change == "state":
        records["job_record"] = records["job_record"].replace("RUNNING", "COMPLETED")
    elif change == "step-state":
        records["step_record"] += " State=COMPLETED"
    elif change == "memory":
        records["job_record"] = records["job_record"].replace("mem=4G", "mem=128M")
    elif change == "attempt":
        settings["context"]["start_time"] = "old"
    else:
        settings["resources"]["enforcement"] = "hard"
    with pytest.raises(ValueError):
        verify_placement(settings, candidate, records)


def test_cgroup_component_match_does_not_accept_similar_job_or_step():
    settings, _, _ = slurm_fixture()
    candidate = placement_probe(settings, environ={"SLURM_JOB_ID": "42", "SLURM_STEP_ID": "0"},
                                host="compute1", uid=1000, pid=444, cgroups="0::/slurm/job_420/step_00/task_0\n")
    assert not candidate["cgroup_proof"]


def test_actual_worker_commits_real_statistics_bound_to_original_context():
    with WorkerClient(config()) as client:
        events = until(client, lambda: client.ready or client.status == "failed")
        assert client.ready, client.error
        assert all("nonce" not in event for event in events)
        assert client.identity["pid"] != os.getpid()
        context = {"job_id": "123", "attempt": "original", "pane": "research"}
        task_id = client.submit("statistics", inputs(), context)
        context["job_id"] = "999"
        result = client.wait(task_id, 10)
        assert result["result"]["difference"] == 3
        receipt = client.tasks[task_id]["receipt"]
        assert receipt["context_digest"] == digest({"job_id": "123", "attempt": "original", "pane": "research"})
        assert receipt["execution"]["site_kind"] == "desktop"
        assert receipt["enforcement"]["hard_address_space"]
        assert not receipt["enforcement"]["hard_rss"]
        until(client, lambda: client.tasks[task_id]["acknowledged"])
        assert client.result_bytes == 0
        assert client.tasks[task_id]["result"] is None


def test_desktop_must_be_explicit_and_hard_policy_must_not_weaken():
    for settings in (config(desktop_explicit=False), config(site_kind="unknown"),
                     config(resources={"enforcement": "hard"})):
        with WorkerClient(settings) as client:
            until(client, lambda: client.status == "failed")
            assert not client.ready


def test_unsupported_function_never_executes_and_valid_followup_still_works(tmp_path):
    marker = tmp_path / "no-execution"
    with WorkerClient(config()) as client:
        until(client, lambda: client.ready)
        task_id = client.submit("os.system", {"command": "touch " + str(marker)}, {})
        until(client, lambda: client.tasks[task_id]["status"] == "failed")
        assert not marker.exists()
        second = client.submit("statistics", inputs(), {"task": "second"})
        assert client.wait(second, 10)["result"]["difference"] == 3


def test_immediate_cancellation_reaps_task_and_later_work_is_admitted():
    with WorkerClient(config()) as client:
        until(client, lambda: client.ready)
        task_id = client.submit("statistics", inputs(), {})
        client.cancel(task_id)
        until(client, lambda: client.tasks[task_id]["status"] in ("cancelled", "committed", "failed"))
        assert client.tasks[task_id]["status"] == "cancelled"
        second = client.submit("statistics", inputs(), {})
        assert client.wait(second, 10)["result"]["difference"] == 3


def test_worker_module_bootstrap_imports_no_sampler_or_ui():
    result = subprocess.run([sys.executable, "-c", "import sys,raven.raven_worker; print(','.join(sorted(k for k in sys.modules if k.startswith('raven.'))))"],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert "raven.sampler" not in result.stdout
    assert "raven.screen" not in result.stdout
    assert "raven.views" not in result.stdout


def test_client_rejects_generation_sequence_nonce_and_context_changes():
    client = WorkerClient(config())
    client._expected_build = "a" * 64
    message = {"op": "hello", "session_id": client.session_id, "generation": client.generation,
               "worker_id": "worker", "seq": 1, "major": 1, "minor": 0, "nonce": client.nonce,
               "build_digest": client._expected_build, "policy_digest": client.config["policy_digest"],
               "identity": {"host": "desktop"}}
    with pytest.raises(ProtocolError):
        client._consume(dict(message, generation=2))
    with pytest.raises(ProtocolError):
        client._consume(dict(message, seq=4))
    with pytest.raises(ProtocolError):
        client._consume(dict(message, nonce="wrong"))
    client.close()


def test_task_cancellation_kills_owned_child_and_preserves_worker_process():
    with WorkerClient(config()) as client:
        until(client, lambda: client.ready)
        task_id = client.submit("statistics", inputs(), {})
        client.cancel(task_id)
        until(client, lambda: client.tasks[task_id]["status"] == "cancelled")
        child = client.tasks[task_id].get("pid")
        assert child and child != client.process.pid
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
        assert client.process.poll() is None


def test_analysis_task_cli_refuses_missing_or_forged_supervisor():
    for parent in (None, os.getpid() + 999999):
        command = [sys.executable, "-m", "raven.raven_worker", "--task"]
        if parent is not None:
            command += ["--parent-pid", str(parent)]
        result = subprocess.run(command, input=b"", capture_output=True, timeout=5)
        assert result.returncode != 0
        assert result.stdout == b""


def test_publication_requires_review_and_never_creates_artifact_on_missing_review(tmp_path):
    with WorkerClient(config()) as client:
        until(client, lambda: client.ready)
        task_id = client.submit("artifact-publish", {"operation": "support-bundle", "plan": {"digest": "unreviewed"}}, {})
        until(client, lambda: client.tasks[task_id]["status"] == "failed")
        assert "reviewed plan" in client.tasks[task_id]["error"]
        assert not list(tmp_path.iterdir())


def test_task_budget_above_grant_refuses_before_launch():
    with WorkerClient(config()) as client:
        until(client, lambda: client.ready)
        task_id = client.submit("statistics", inputs(), {}, budget={"cpus": 2})
        until(client, lambda: client.tasks[task_id]["status"] == "failed")
        assert "cpus" in client.tasks[task_id]["error"]
        assert not client.tasks[task_id].get("pid")


def test_stale_placement_probe_is_not_rebound_to_new_worker():
    client = WorkerClient(config())
    client.status = "placement"
    client.placement = {"probe_id": "new"}
    with pytest.raises(ValueError, match="another worker probe"):
        client.provide_placement({"probe_id": "old"})
    client.close()


def test_allocation_observation_reads_only_complete_exact_startup_lines():
    client = WorkerClient(config(site_kind="slurm"), observe_allocation=True)
    try:
        for ignored in (b"payload: salloc: Pending job allocation 41\n", b"salloc: Granted job allocation 41 error\n",
                        b"salloc: Pending job allocation 0\n", b"srun: Pending job allocation 41\n",
                        b"salloc: Pending job allocation 999999999999999999999\n"):
            client._retain_stderr(ignored)
            assert client.observed_allocation_id == ""
        client._retain_stderr(b"salloc: Pending job allocation 4")
        assert client.observed_allocation_id == ""
        client._retain_stderr(b"2\n")
        assert client.observed_allocation_id == "42"
        client._retain_stderr(b"salloc: Granted job allocation 42\r\n")
        assert client.observed_allocation_id == "42"
        assert not client.ready and client.identity == {} and client.placement is None
    finally:
        client.close()


@pytest.mark.parametrize("site_kind,opt_in", [("slurm", False), ("desktop", True)])
def test_allocation_observation_requires_opted_in_slurm_launch(site_kind, opt_in):
    client = WorkerClient(config(site_kind=site_kind), observe_allocation=opt_in)
    try:
        client._retain_stderr(b"salloc: Pending job allocation 42\n")
        assert client.observed_allocation_id == ""
    finally:
        client.close()


def test_conflicting_allocation_observations_are_never_rebound():
    client = WorkerClient(config(site_kind="slurm"), observe_allocation=True)
    try:
        client._retain_stderr(b"salloc: Pending job allocation 42\nsalloc: Granted job allocation 43\n")
        assert client.observed_allocation_id == ""
        client._retain_stderr(b"salloc: Granted job allocation 42\n")
        assert client.observed_allocation_id == ""
    finally:
        client.close()


@pytest.mark.parametrize("candidate_job", ["42", "43"])
def test_worker_publication_closes_launch_observation_and_rejects_conflict(candidate_job):
    client = WorkerClient(config(site_kind="slurm"), observe_allocation=True)
    try:
        client._retain_stderr(b"salloc: Pending job allocation 42\n")
        client._consume({"op": "placement_probe", "session_id": client.session_id,
                         "generation": client.generation, "worker_id": "worker", "seq": 1,
                         "candidate": {"probe_id": "probe", "job_id": candidate_job}})
        assert client.observed_allocation_id == ("42" if candidate_job == "42" else "")
        assert not client.ready and client.identity == {}
        client._retain_stderr(b"salloc: Granted job allocation 999\n")
        assert client.observed_allocation_id == ("42" if candidate_job == "42" else "")
    finally:
        client.close()


def test_allocation_observation_has_the_same_bounded_stderr_budget():
    client = WorkerClient(config(site_kind="slurm"), observe_allocation=True)
    try:
        client._retain_stderr(b"x" * 16384 + b"\nsalloc: Pending job allocation 42\n")
        client._retain_stderr(b"salloc: Granted job allocation 42\n")
        assert len(client.stderr) == 16384 and client.observed_allocation_id == ""
    finally:
        client.close()


def test_final_large_result_is_drained_after_worker_exits(monkeypatch):
    client = WorkerClient(config())
    parent, child = socket.socketpair()
    parent.setblocking(False)
    try:
        expected = {"op": "test", "text": "x" * 180000}
        child.sendall(encode(expected))
        child.close()
        client.process = SimpleNamespace(poll=lambda: 0)
        client.ready, client.status = True, "ready"
        client.selector.register(parent, selectors.EVENT_READ, "read")
        monkeypatch.setattr(client, "_consume", client._events.append)
        events = []
        for _ in range(10):
            events.extend(client.poll())
            if client.status == "failed":
                break
            assert not client._stdout_eof
        assert events == [expected]
        assert client._stdout_eof
        assert client.status == "failed"
    finally:
        parent.close()
        child.close()
        client.process = None
        client.close()


@pytest.mark.parametrize("field", ["MinMemoryNode=512M", "MinMemoryCPU=256", "AllocTRES=cpu=2,mem=0.5G"])
def test_verified_memory_units_and_per_cpu_grants(field):
    settings, candidate, records = slurm_fixture()
    records["job_record"] = records["job_record"].replace("AllocTRES=cpu=4,mem=4G", field)
    assert verify_placement(settings, candidate, records)["verified_memory_mib"] == 512


def test_aggregate_multinode_memory_is_not_treated_as_per_node():
    settings, candidate, records = slurm_fixture()
    records["job_nodes"] += ["compute2"]
    with pytest.raises(ValueError, match="memory grant"):
        verify_placement(settings, candidate, records)
    records["step_record"] += " TRES=cpu=2,mem=512M"
    assert verify_placement(settings, candidate, records)["verified_memory_mib"] == 512
    records["step_nodes"] += ["compute2"]
    with pytest.raises(ValueError, match="single-node worker step"):
        verify_placement(settings, candidate, records)


def test_actual_task_wall_budget_terminates_child_and_allows_followup():
    with WorkerClient(config()) as client:
        until(client, lambda: client.ready)
        task_id = client.submit("statistics", inputs(), {}, budget={"wall_seconds": .001})
        until(client, lambda: client.tasks[task_id]["status"] in ("failed", "cancelled"))
        assert "wall" in client.tasks[task_id]["error"].lower()
        second = client.submit("statistics", inputs(), {})
        assert client.wait(second, 10)["result"]["difference"] == 3


def test_build_mismatch_has_correlated_diagnostic_without_admission(monkeypatch):
    from raven import raven_worker_client
    monkeypatch.setattr(raven_worker_client, 'build_digest', lambda: '0' * 64)
    with WorkerClient(config()) as client:
        until(client, lambda: client.status == 'failed')
        assert not client.ready
        assert 'build digest' in client.error


@pytest.mark.parametrize('malformed', [None, 'bad', [1, 2], {'id': ['bad']}, {'id': 'bad', 'operation': []}])
def test_malformed_admission_is_bounded_error_and_valid_followup_works(malformed):
    with WorkerClient(config()) as client:
        until(client, lambda: client.ready)
        client._queue(client._message('admit', task=malformed))
        until(client, lambda: bool(client.error))
        assert client.ready and client.process.poll() is None
        task_id = client.submit('statistics', inputs(), {})
        assert client.wait(task_id, 10)['result']['difference'] == 3


def test_instrumentation_proves_batch_membership_without_claiming_heavy_grant():
    settings, _, records = slurm_fixture()
    settings['context'].pop('step_id')
    candidate = placement_probe(settings, environ={'SLURM_JOB_ID': '42'}, host='compute1', uid=1000, pid=444,
                                cgroups='0::/slurm/job_42/step_batch/task_0\n', purpose='instrumentation')
    candidate['probe_id'] = 'launch-1'
    records['step_record'] = 'StepId=42.batch State=RUNNING'
    records['step_nodes'] += ['compute2']
    records['job_nodes'] += ['compute2']
    identity = verify_placement(settings, candidate, records, purpose='instrumentation')
    assert identity['verified'] and identity['purpose'] == 'instrumentation'
    assert not identity['heavy_admission']
    assert 'verified_cpus' not in identity and 'verified_memory_mib' not in identity
    with pytest.raises(ValueError, match='purpose'):
        verify_placement(settings, candidate, records)


def test_instrumentation_never_uses_environment_only_authorization():
    settings, _, records = slurm_fixture(False)
    candidate = placement_probe(settings, environ={'SLURM_JOB_ID': '42', 'SLURM_STEP_ID': '0'},
                                host='compute1', uid=1000, pid=444, cgroups='', purpose='instrumentation')
    candidate['probe_id'] = 'launch-1'
    with pytest.raises(ValueError, match='placement is unverified'):
        verify_placement(settings, candidate, records, purpose='instrumentation')
    with pytest.raises(ValueError):
        placement_probe(settings, environ={'SLURM_JOB_ID': '42'}, host='compute1', uid=1000, pid=444,
                        cgroups='', purpose='instrumentation')


def test_instrumentation_still_requires_running_owned_step():
    settings, candidate, records = slurm_fixture()
    candidate['purpose'] = 'instrumentation'
    records['job_record'] = records['job_record'].replace('1000', '1001')
    with pytest.raises(ValueError, match='owned by the worker user'):
        verify_placement(settings, candidate, records, purpose='instrumentation')


def test_supervisor_and_analysis_child_work_outside_source_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with WorkerClient(config()) as client:
        until(client, lambda: client.ready or client.status == 'failed')
        assert client.ready, client.error
        task_id = client.submit('statistics', inputs(), {})
        assert client.wait(task_id, 10)['result']['difference'] == 3
