import json
import multiprocessing
from pathlib import Path
import queue

import pytest

from raven.collective_runtime import CollectiveControlError, CollectiveRuntime, TorchMetrics, TorchDistributedCommunicator
from raven.runtime_control import RuntimeControl, initialize


class ProcessCommunicator:
    """Real process transport that exercises the MPI-style collective algorithm."""
    def __init__(self, rank, size, values, barrier):
        self.rank, self.size, self.values, self.barrier = rank, size, values, barrier
        self.sequence = 0

    def Get_rank(self): return self.rank
    def Get_size(self): return self.size

    def gather(self, value, root=0):
        key = self.sequence
        self.sequence += 1
        self.values[(key, self.rank)] = value
        self.barrier.wait(10)
        result = [self.values[(key, rank)] for rank in range(self.size)] if self.rank == root else None
        self.barrier.wait(10)
        if self.rank == root:
            for rank in range(self.size): del self.values[(key, rank)]
        self.barrier.wait(10)
        return result

    def bcast(self, value, root=0):
        key = self.sequence
        self.sequence += 1
        if self.rank == root: self.values[key] = value
        self.barrier.wait(10)
        result = self.values[key]
        self.barrier.wait(10)
        if self.rank == root: del self.values[key]
        self.barrier.wait(10)
        return result


def _rank(rank, size, values, barrier, output, mailbox, mode):
    state = {"rate": .1}
    try:
        control = CollectiveRuntime(ProcessCommunicator(rank, size, values, barrier), mailbox, "job123:attempt1")
        def prepare(command, parameters, request_id):
            if mode == "prepare-failure" and rank == 1:
                raise ValueError("Rank cannot accept this update")
            return parameters["value"]
        def commit(value):
            state["rate"] = value
            if mode == "commit-failure" and rank == 1:
                raise RuntimeError("Failure after a parameter changed")
            return dict(state)
        point = "different" if mode == "mismatch" and rank == 1 else "epoch"
        receipt = control.safe_point(point, prepare, commit)
        if mode == "io":
            decision = control.permit("checkpoint", lease_seconds=30)
            assert decision["state"] == "granted"
            control.release("checkpoint", decision["token"])
        output.put({"rank": rank, "ok": True, "state": state, "epoch": control.epoch, "receipt": receipt})
    except Exception as exc:
        output.put({"rank": rank, "ok": False, "state": state, "error": str(exc), "receipt": getattr(exc, "receipt", None)})


@pytest.mark.parametrize("mode", ["success", "prepare-failure", "commit-failure", "mismatch", "io"])
def test_real_multiprocess_collective_control(tmp_path, mode):
    manifest = json.loads((Path(__file__).resolve().parents[1] / "examples/runtime/control-config.json").read_text())
    mailbox = tmp_path / "mailbox"
    initialize(mailbox, manifest)
    control = RuntimeControl(mailbox, "job123:attempt1")
    control.submit("change", "learning-rate", {"value": .25}, expected_epoch=0)
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        values, barrier, output = manager.dict(), context.Barrier(2), context.Queue()
        processes = [context.Process(target=_rank, args=(rank, 2, values, barrier, output, str(mailbox), mode)) for rank in range(2)]
        try:
            for process in processes: process.start()
            results = [output.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(10)
                assert process.exitcode == 0
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            output.close()
    status = control.inspect_request("change")
    if mode in {"success", "io"}:
        assert all(row["ok"] and row["epoch"] == 1 and row["state"]["rate"] == .25 for row in results)
        assert status["status"] == "applied"
        assert status["result"]["result"]["ranks"] == 2
    elif mode == "prepare-failure":
        assert all(row["ok"] and row["epoch"] == 0 and row["state"]["rate"] == .1 for row in results)
        assert status["status"] == "rejected"
    elif mode == "commit-failure":
        assert all(not row["ok"] and row["receipt"]["status"] == "uncertain" for row in results)
        assert status["status"] == "uncertain"
        assert control.poll("epoch") is None
    else:
        assert all(not row["ok"] and "calls differ" in row["error"] for row in results)
        assert status["status"] == "pending"
    if mode == "io":
        assert control.inspect_permit("checkpoint")["state"] == "released"


class FakeEvent:
    ready = False
    def record(self): pass
    def query(self): return self.ready
    def elapsed_time(self, other): return 4.5


class DeviceContext:
    def __enter__(self): return self
    def __exit__(self, *args): return False


class FakeCuda:
    def is_available(self): return True
    def Event(self, **kwargs): return FakeEvent()
    def device(self, device): return DeviceContext()
    def memory_allocated(self, device): return 1024
    def memory_reserved(self, device): return 2048
    def max_memory_allocated(self, device): return 1536


class FakeTorch:
    cuda = FakeCuda()


def test_cuda_timing_is_nonblocking_bounded_and_explicit_about_loss():
    sampler = TorchMetrics(FakeTorch(), "cuda:0", max_pending=2)
    assert sampler.begin() and sampler.end()
    assert sampler.begin() and sampler.end()
    assert not sampler.begin()
    result = sampler.sample()
    assert result["pending_timing_samples"] == 2
    assert result["skipped_timing_samples"] == 1
    assert result["gpu_stream_seconds"] == []
    sampler.pending[0][1].ready = True
    result = sampler.sample()
    assert result["gpu_stream_seconds"] == [.0045]
    assert result["pending_timing_samples"] == 1
    assert sampler.begin()
    with pytest.raises(ValueError): sampler.begin()


def test_cpu_metrics_never_invent_gpu_data():
    sampler = TorchMetrics(FakeTorch(), "cpu")
    assert sampler.begin() is False
    assert sampler.sample() == {"cuda_available": False, "skipped_timing_samples": 0, "pending_timing_samples": 0}


def test_torch_subgroup_uses_global_root_and_existing_group():
    class Distributed:
        def __init__(self): self.calls = []
        def is_initialized(self): return True
        def get_global_rank(self, group, rank): return 5
        def get_rank(self, group): return 0
        def get_world_size(self, group): return 2
        def broadcast_object_list(self, values, **kwargs): self.calls.append(kwargs)
        def gather_object(self, value, object_gather_list, **kwargs):
            self.calls.append(kwargs)
            object_gather_list[:] = [value, value]
    distributed = Distributed()
    comm = TorchDistributedCommunicator(distributed, "existing-control-group")
    assert comm.bcast({"epoch": 1}) == {"epoch": 1}
    assert comm.gather("ready") == ["ready", "ready"]
    assert distributed.calls == [{"src": 5, "group": "existing-control-group"}, {"dst": 5, "group": "existing-control-group"}]
