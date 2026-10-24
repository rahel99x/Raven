"""Startup survives mapped runtimes without weakening memory or placement checks."""

from __future__ import annotations

import io
from pathlib import Path
import resource
import sys
import time
from types import SimpleNamespace

import pytest

from raven.heavy import resources
from raven.heavy.worker_client import WorkerClient


MIB = 1 << 20
LINUX_PROC = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not Path("/proc/self/statm").is_file(),
    reason="The portable Heavy adapter measures virtual mappings through Linux procfs",
)


def memory_ports(monkeypatch, raw, *, inherited=None, page_size=4096):
    """Replace only this resource owner's ports; never limit the pytest process."""
    reads, changes = [], []

    class Measurement(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 256, "Startup memory measurement must stay bounded"
            reads.append(size)
            return super().read(size)

    def open_measurement(path, mode):
        assert (path, mode) == ("/proc/self/statm", "rb")
        if isinstance(raw, OSError):
            raise raw
        return Measurement(raw)

    inherited = (
        (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
        if inherited is None
        else inherited
    )
    monkeypatch.setattr(resources, "open", open_measurement, raising=False)
    monkeypatch.setattr(
        resources, "os", SimpleNamespace(sysconf=lambda name: page_size)
    )
    monkeypatch.setattr(
        resources,
        "resource",
        SimpleNamespace(
            RLIMIT_AS=resource.RLIMIT_AS,
            RLIM_INFINITY=resource.RLIM_INFINITY,
            getrlimit=lambda kind: inherited,
            setrlimit=lambda kind, values: changes.append((kind, values)),
        ),
    )
    return reads, changes


def total_budget(memory_mib=512, max_tasks=2):
    return resources.limits(
        {"memory_mib": memory_mib, "cpus": max_tasks, "max_tasks": max_tasks}
    )


def test_observed_supervisor_mapping_is_charged_to_every_task_share(monkeypatch):
    reads, changes = memory_ports(monkeypatch, b"24576 1000 0 0 0 0 0\n")
    available = total_budget()
    original = dict(available)

    reservation = resources.reserve_supervisor(available)
    advertised = {**available, "supervisor_memory_mib": reservation}
    share = resources.task_limits({}, advertised)

    assert reservation == 128  # 96 MiB mappings plus 32 MiB working headroom.
    assert share["memory_mib"] == 192
    assert share["cpus"] == 1
    assert reservation + 2 * share["memory_mib"] == 512
    assert available == original
    assert reads and changes == [
        (resource.RLIMIT_AS, (128 * MIB, resource.RLIM_INFINITY))
    ]


@pytest.mark.parametrize("pages, expected", [(1, 64), (24577, 129)])
def test_supervisor_reservation_has_a_floor_and_rounds_mappings_up(
    monkeypatch, pages, expected
):
    _, changes = memory_ports(monkeypatch, str(pages).encode())
    assert resources.reserve_supervisor(total_budget()) == expected
    assert changes[0][1][0] == expected * MIB


def test_supervisor_preserves_the_inherited_hard_limit(monkeypatch):
    _, changes = memory_ports(monkeypatch, b"24576", inherited=(256 * MIB, 256 * MIB))
    assert resources.reserve_supervisor(total_budget()) == 128
    assert changes == [(resource.RLIMIT_AS, (128 * MIB, 256 * MIB))]


@pytest.mark.parametrize(
    "inherited",
    [(127 * MIB, resource.RLIM_INFINITY), (127 * MIB, 127 * MIB)],
    ids=["stricter-soft", "stricter-soft-and-hard"],
)
def test_startup_never_raises_an_inherited_address_space_limit(monkeypatch, inherited):
    _, changes = memory_ports(monkeypatch, b"24576", inherited=inherited)
    with pytest.raises(ValueError, match="inherited RLIMIT_AS"):
        resources.reserve_supervisor(total_budget())
    assert not changes


def test_insufficient_total_memory_refuses_admission_before_changing_limits(
    monkeypatch,
):
    _, changes = memory_ports(monkeypatch, b"24576")
    with pytest.raises(ValueError, match="increase Heavy memory or reduce max_tasks"):
        resources.reserve_supervisor(total_budget(memory_mib=128))
    assert not changes


@pytest.mark.parametrize(
    "raw",
    [b"", b"not-a-measurement", b"0 0", b"-1 0", b"9" * 256],
)
def test_invalid_memory_measurements_fail_closed_with_bounded_reads(monkeypatch, raw):
    reads, changes = memory_ports(monkeypatch, raw)
    with pytest.raises(ValueError, match="Cannot measure Heavy supervisor"):
        resources.reserve_supervisor(total_budget())
    assert reads and not changes


@pytest.mark.parametrize(
    "error", [FileNotFoundError("missing"), PermissionError("denied")]
)
def test_unavailable_procfs_has_an_actionable_admission_error(monkeypatch, error):
    _, changes = memory_ports(monkeypatch, error)
    with pytest.raises(ValueError, match="safe memory admission is unavailable"):
        resources.reserve_supervisor(total_budget())
    assert not changes


def test_invalid_page_size_does_not_change_process_limits(monkeypatch):
    _, changes = memory_ports(monkeypatch, b"24576", page_size=0)
    with pytest.raises(ValueError, match="Cannot measure Heavy supervisor"):
        resources.reserve_supervisor(total_budget())
    assert not changes


def test_legacy_resource_messages_keep_the_default_supervisor_reservation():
    available = total_budget()
    assert resources.task_limits({}, available)["memory_mib"] == 224
    assert (
        resources.task_limits({}, available, supervisor_memory_mib=128)["memory_mib"]
        == 192
    )


@pytest.mark.parametrize("reservation", [True, 0, 63, 128.0, "128", float("nan")])
def test_malformed_advertised_reservations_are_not_used_for_admission(reservation):
    with pytest.raises(ValueError, match="supervisor memory reservation"):
        resources.task_limits(
            {}, {**total_budget(), "supervisor_memory_mib": reservation}
        )


def test_tasks_cannot_override_worker_owned_supervisor_reservation():
    available = {**total_budget(), "supervisor_memory_mib": 128}
    with pytest.raises(ValueError, match="Unsupported task resource field"):
        resources.task_limits({"supervisor_memory_mib": 64}, available)
    assert resources.task_limits({}, available)["memory_mib"] == 192


def slurm_records(candidate):
    return {
        "probe_id": candidate["probe_id"],
        "job_record": (
            "JobId=42 UserId=owner(1000) JobState=RUNNING AllocTRES=cpu=2,mem=4G"
        ),
        "step_record": "StepId=42.0 State=RUNNING CPUs=2",
        "job_nodes": ["compute1.cluster.example"],
        "step_nodes": ["compute1.cluster.example"],
    }


@pytest.mark.parametrize(
    "error", [LookupError("unknown encoding: idna"), OSError("DNS")]
)
def test_optional_hostname_failure_preserves_real_placement_checks(monkeypatch, error):
    def failed_resolution(host):
        assert host == "compute1"
        raise error

    monkeypatch.setattr(
        resources,
        "socket",
        SimpleNamespace(gethostname=lambda: "compute1", getfqdn=failed_resolution),
    )
    config = {"resources": total_budget(), "context": {"allocation_id": "42"}}
    candidate = resources.placement_probe(
        config,
        environ={"SLURM_JOB_ID": "42", "SLURM_STEP_ID": "0"},
        uid=1000,
        pid=444,
        cgroups="0::/slurm/job_42/step_0/task_0\n",
    )
    candidate["probe_id"] = "launch-1"
    assert candidate["host"] == candidate["fqdn"] == "compute1"
    assert type(error).__name__ in candidate["hostname_resolution_error"]
    assert len(candidate["hostname_resolution_error"]) <= 240
    records = slurm_records(candidate)
    assert resources.verify_placement(config, candidate, records)["verified"]
    with pytest.raises(ValueError, match="outside the verified allocation"):
        resources.verify_placement(
            config, candidate, {**records, "step_nodes": ["other"]}
        )
    with pytest.raises(ValueError, match="process placement is unverified"):
        resources.verify_placement(
            config, {**candidate, "cgroup_proof": False}, records
        )


def test_successful_dns_alias_remains_compatible(monkeypatch):
    monkeypatch.setattr(
        resources,
        "socket",
        SimpleNamespace(
            gethostname=lambda: "kernel-name", getfqdn=lambda host: "compute1.cluster"
        ),
    )
    config = {"resources": total_budget(), "context": {}}
    candidate = resources.placement_probe(
        config,
        environ={"SLURM_JOB_ID": "42", "SLURM_STEP_ID": "0"},
        uid=1000,
        pid=444,
        cgroups="0::/slurm/job_42/step_0/task_0\n",
    )
    candidate["probe_id"] = "launch-1"
    assert candidate["fqdn"] == "compute1.cluster"
    assert "hostname_resolution_error" not in candidate
    assert resources.verify_placement(config, candidate, slurm_records(candidate))[
        "verified"
    ]


def worker_config(memory_mib=1024, *, max_tasks=4, slurm=False):
    return {
        "site_kind": "slurm" if slurm else "desktop",
        "desktop_explicit": not slurm,
        "context": {"allocation_id": "42", "step_id": "0"} if slurm else {},
        "resources": {
            "memory_mib": memory_mib,
            "cpus": max_tasks,
            "max_tasks": max_tasks,
            "wall_seconds": 30,
        },
    }


def mapped_worker(mapped_mib, *, codec_error=False):
    root = str(Path(__file__).resolve().parents[1])
    setup = (
        "import mmap, os, runpy, socket, sys\n"
        f"sys.path.insert(0, {root!r})\n"
        f"retained_mapping = mmap.mmap(-1, {mapped_mib} << 20)\n"
    )
    if codec_error:
        setup += (
            "os.environ.update(SLURM_JOB_ID='42', SLURM_STEP_ID='0')\n"
            "def missing_codec(*args):\n"
            "    raise LookupError('unknown encoding: idna')\n"
            "socket.getfqdn = missing_codec\n"
        )
    setup += "runpy.run_module('raven.raven_worker', run_name='__main__')\n"
    return [sys.executable, "-c", setup]


def poll_until(client, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    events = []
    while time.monotonic() < deadline:
        events.extend(client.poll(0.05))
        if predicate():
            return events
    pytest.fail(
        f"Worker startup timed out: {client.status}: {client.error}; {client.diagnostics}"
    )


def statistics_inputs():
    return {
        "document": {
            "schema": "raven.statistics/v1",
            "design": "independent",
            "unit": "seed",
            "control": [{"id": str(i), "value": i} for i in (1, 2, 3)],
            "treatment": [{"id": str(i), "value": i} for i in (4, 5, 6)],
        }
    }


@LINUX_PROC
def test_mapped_supervisor_runs_a_smaller_fresh_child_with_its_real_quota():
    with WorkerClient(worker_config(), launch_argv=mapped_worker(256)) as client:
        poll_until(client, lambda: client.ready or client.status == "failed")
        assert client.ready, (client.error, client.diagnostics)
        reservation = client.resources["supervisor_memory_mib"]
        share = resources.task_limits({}, client.resources)
        assert reservation >= 256 + 32
        assert share["memory_mib"] < reservation
        assert reservation + 4 * share["memory_mib"] <= 1024
        task_id = client.submit("statistics", statistics_inputs(), {})
        result = client.wait(task_id, 10)
        assert result["result"]["difference"] == 3
        receipt = client.tasks[task_id]["receipt"]
        assert receipt["process_pid"] != client.identity["pid"]
        assert receipt["enforcement"]["hard_address_space"]
        assert not receipt["enforcement"]["hard_rss"]
        actual_limit = receipt["enforcement"]["limits"][str(resource.RLIMIT_AS)]
        assert 32 * MIB <= actual_limit <= share["memory_mib"] * MIB
    assert client.process.poll() == 0
    assert "Traceback" not in client.diagnostics


@LINUX_PROC
def test_codec_failure_reaches_placement_without_admitting_a_slurm_task():
    with WorkerClient(
        worker_config(max_tasks=2, slurm=True),
        launch_argv=mapped_worker(128, codec_error=True),
    ) as client:
        poll_until(client, lambda: client.placement or client.status == "failed")
        assert client.status == "placement", (client.error, client.diagnostics)
        assert not client.ready and not client.tasks
        assert client.placement["host"] == client.placement["fqdn"]
        assert "unknown encoding: idna" in client.placement["hostname_resolution_error"]
        assert client.placement["pid"] == client.process.pid
    assert client.process.poll() == 0
    assert "Traceback" not in client.diagnostics


@LINUX_PROC
def test_insufficient_runtime_budget_publishes_an_error_without_starting_tasks():
    with WorkerClient(
        worker_config(memory_mib=128, max_tasks=2), launch_argv=mapped_worker(256)
    ) as client:
        events = poll_until(client, lambda: client.status == "failed")
        assert not client.ready and not client.tasks
        assert any(event["op"] == "error" for event in events)
        assert "increase Heavy memory or reduce max_tasks" in client.error
        assert not any(event["op"] in {"hello", "running"} for event in events)
    assert client.process.poll() == 0
    assert "Traceback" not in client.diagnostics


def startup_error_worker(error_name):
    root = str(Path(__file__).resolve().parents[1])
    assert error_name in {"LookupError", "MemoryError"}
    setup = (
        "import sys\n"
        f"sys.path.insert(0, {root!r})\n"
        "from raven.heavy import worker\n"
        "def failed_manifest():\n"
        f"    raise {error_name}('unknown encoding: idna')\n"
        "worker._manifest = failed_manifest\n"
        "raise SystemExit(worker.main())\n"
    )
    return [sys.executable, "-c", setup]


@LINUX_PROC
def test_unexpected_startup_codec_failure_is_correlated_without_admission():
    with WorkerClient(
        worker_config(), launch_argv=startup_error_worker("LookupError")
    ) as client:
        events = poll_until(client, lambda: client.status == "failed")
        assert not client.ready and not client.tasks
        errors = [event for event in events if event["op"] == "error"]
        assert len(errors) == 1
        assert errors[0]["session_id"] == client.session_id
        assert errors[0]["generation"] == client.generation
        assert errors[0]["worker_id"] == client.worker_id
        assert "unknown encoding: idna" in client.error
        assert not any(event["op"] in {"hello", "running"} for event in events)
    assert client.process.poll() == 0
    assert "Traceback" not in client.diagnostics


@LINUX_PROC
def test_startup_memory_error_has_actionable_diagnostics_and_exit_status():
    with WorkerClient(
        worker_config(), launch_argv=startup_error_worker("MemoryError")
    ) as client:
        events = poll_until(client, lambda: client.status == "failed")
        assert not client.ready and not client.tasks
        assert not any(event["op"] in {"hello", "running"} for event in events)
    assert client.process.poll() == 2
    assert "memory budget exhausted" in client.diagnostics
    assert "increase Heavy memory" in client.diagnostics
    assert "reduce worker concurrency" in client.diagnostics
    assert "Traceback" not in client.diagnostics
