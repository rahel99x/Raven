"""Adversarial application-client regressions from the October review."""

import json
import os
import subprocess
import sys
import threading
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from raven import client


@pytest.fixture(autouse=True)
def isolated_client(monkeypatch):
    client._shutdown()
    client._state = None
    client._options = {}
    client._fork_child = False
    for name in (
        "RAVEN_RUN_DIR",
        "RAVEN_METRICS_FILE",
        "RAVEN_METRICS_OWNER_PID",
        "RANK",
        "OMPI_COMM_WORLD_RANK",
        "PMI_RANK",
        "SLURM_PROCID",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    client._shutdown()
    client._state = None
    client._options = {}


def mpi(monkeypatch, rank=0):
    calls = []
    initialized = [True]
    communicator = SimpleNamespace(
        Get_rank=lambda: rank,
        Get_size=lambda: 2,
        allreduce=lambda value, op: calls.append(("ready", value)) or value,
        reduce=lambda value, op, root: calls.append(("value", value)) or value * 2,
    )
    runtime = SimpleNamespace(
        COMM_WORLD=communicator,
        MIN="min",
        SUM="sum",
        Is_initialized=lambda: initialized[0],
        Is_finalized=lambda: False,
    )
    monkeypatch.setitem(sys.modules, "mpi4py.MPI", runtime)
    return calls, initialized


def test_explicit_reduction_participates_even_without_a_configured_client(monkeypatch):
    calls, _ = mpi(monkeypatch, rank=1)
    assert client.metric("loss", 3, reduce="sum") == 3
    assert calls == [("ready", 1), ("value", 3)]
    assert client._state is None


def test_client_reclassifies_a_spawn_helper_when_runtime_initializes(tmp_path, monkeypatch):
    calls, initialized = mpi(monkeypatch)
    initialized[0] = False
    monkeypatch.setattr(client, "_helper", lambda: True)
    path = tmp_path / "metrics.jsonl"
    client.configure(path=path)
    client.metric("before", 1)
    assert client._state.helper and not path.exists()
    initialized[0] = True
    client.metric("after", 3, reduce="sum")
    assert not client._state.helper and client._state.allowed
    assert calls == [("ready", 1), ("value", 3)]
    assert client._state.flush()
    assert any(
        json.loads(line)["metrics"].get("after") == 6 for line in path.read_text().splitlines()
    )


def test_blank_rank_does_not_mask_an_initialized_launcher_rank(monkeypatch):
    monkeypatch.setenv("RANK", "")
    monkeypatch.setenv("PMI_RANK", "3")
    assert client._environment_rank() == (3, "PMI_RANK")


def test_nccl_uses_local_rank_device_without_changing_application_cuda_state(monkeypatch):
    devices, calls = [], []

    class Tensor:
        def __init__(self, value):
            self.value = value

        def item(self):
            return self.value

    def tensor(value, **kwargs):
        devices.append(kwargs["device"])
        return Tensor(value)

    runtime = SimpleNamespace(
        is_initialized=lambda: True,
        get_rank=lambda: 1,
        get_world_size=lambda: 2,
        get_backend=lambda: "nccl",
        all_reduce=lambda value, op: calls.append((value.value, op)),
        ReduceOp=SimpleNamespace(MIN="min", SUM="sum"),
    )
    monkeypatch.setitem(
        sys.modules, "torch", SimpleNamespace(tensor=tensor, int32="int32", float64="float64")
    )
    monkeypatch.setitem(sys.modules, "torch.distributed", runtime)
    monkeypatch.setenv("LOCAL_RANK", "1")
    context = client._CollectiveContext()
    assert client._reduce(context, 2, "sum", device="cuda:1") is None
    assert devices == ["cuda:1", "cuda:1"]
    assert len(calls) == 2
    devices.clear()
    calls.clear()
    assert client._reduce(context, 2, "sum", device="cuda:0") is None
    assert devices == ["cuda:1"] and calls == [(0, "min")]


def test_repeated_disabled_phases_leave_constant_context_size():
    before = len(copy_context())
    for _ in range(50000):
        with client.phase("step"):
            pass
    assert len(copy_context()) <= before + 1
    assert client._phase_scopes.get() == ()


def test_thread_limit_never_escapes_measure_progress_or_reconfigure(tmp_path, monkeypatch):
    def unavailable(self):
        raise RuntimeError("thread quota")

    monkeypatch.setattr(threading.Thread, "start", unavailable)
    assert client.configure(path=tmp_path / "metrics.jsonl")
    assert client.progress(11, 10) == 11

    @client.measure("loss")
    def work():
        return 7

    assert work() == 7
    assert client.configure(path=tmp_path / "replacement.jsonl")


def test_measure_sampling_skips_client_and_step_work(tmp_path, monkeypatch):
    client.configure(path=tmp_path / "metrics.jsonl")

    @client.measure("loss", every=1000)
    def work():
        return 7

    assert work() == 7
    monkeypatch.setattr(
        client, "_get_client", lambda: pytest.fail("skipped sample looked up client")
    )
    for _ in range(999):
        assert work() == 7


@pytest.mark.skipif(not hasattr(sys, "monitoring"), reason="local monitoring needs Python 3.12")
def test_watch_uses_local_monitoring_without_thread_profile(tmp_path, monkeypatch):
    client.configure(path=tmp_path / "metrics.jsonl")

    def work():
        local = 7
        return local

    monkeypatch.setattr(
        sys, "setprofile", lambda *args: pytest.fail("watch installed thread-wide profiler")
    )
    with client.watch(work, "local"):
        assert work() == 7
    assert client._state.flush()


def test_late_registered_exit_hook_can_emit_its_final_metric(tmp_path):
    path = tmp_path / "metrics.jsonl"
    program = (
        "import atexit; from raven import client as r; "
        f"r.configure(path={str(path)!r}); "
        "atexit.register(lambda:r.metric('final',7)); r.metric('initial',1)"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert any(
        json.loads(line)["metrics"] == {"final": 7} for line in path.read_text().splitlines()
    )


def test_plain_subprocess_does_not_become_a_second_coordinator(tmp_path):
    path = tmp_path / "metrics.jsonl"
    client.configure(path=path)
    client.metric("parent", 1)
    assert client._state.flush()
    env = dict(os.environ, RAVEN_METRICS_FILE=str(path))
    result = subprocess.run(
        [sys.executable, "-c", "from raven import client; client.metric('child',99)"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert all("child" not in json.loads(line)["metrics"] for line in path.read_text().splitlines())


def test_lock_retry_does_not_overwrite_a_newer_pending_metric(tmp_path, monkeypatch):
    path = tmp_path / "metrics.jsonl"
    entered, release = threading.Event(), threading.Event()
    original = client._append
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(3)
            raise TimeoutError("peer briefly held the metrics lock")
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "_append", interrupted)
    client.configure(path=path, flush_interval=0.05)
    client.metric("loss", 1)
    assert entered.wait(2)
    client.metric("loss", 2)
    release.set()
    assert client._state.flush()
    assert not client._state.failed
    assert json.loads(path.read_text().splitlines()[-1])["metrics"]["loss"] == 2
