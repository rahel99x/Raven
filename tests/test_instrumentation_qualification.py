"""Actual optional frameworks through the local launch boundary, never emulators.

These tests qualify two local CPU ranks only. Missing PyTorch/mpi4py/launchers
skip explicitly; Slurm, GPU/NCCL, and remote cluster behavior are not claimed.
"""
import importlib.util
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from raven.metrics import MetricReader
from raven.trace_protocol import TraceReader

ROOT = Path(__file__).resolve().parents[1]


def _available(module):
    return importlib.util.find_spec(module) is not None


def _launch(tmp_path, command, application, *, timeout=120):
    (tmp_path / "application.py").write_text(application)
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/sh\nset -eu\n" + shlex.join(command) + "\n")
    receipt = tmp_path / "receipt.json"
    code = (
        "import json,sys; from pathlib import Path; "
        "from raven.launch import resolve_plan; from raven.launch_runtime import execute_plan; "
        "p=resolve_plan(sys.argv[2:],cwd=Path(sys.argv[2]).parent); "
        "Path(sys.argv[1]).write_text(json.dumps(execute_plan(p)))"
    )
    arguments = [sys.executable, "-c", code, str(receipt), str(script), "--project", str(tmp_path), "--local", "--trace"]
    if not hasattr(sys, "monitoring"):
        arguments += ["--trace-adapter", "python-legacy-profile"]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("RAVEN_")}
    environment["PYTHONPATH"] = str(ROOT)
    environment.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    process = subprocess.Popen(arguments, cwd=ROOT, env=environment, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        output, errors = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, errors = process.communicate(timeout=10)
        pytest.fail("Optional framework launch timed out: " + errors[-4000:])
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
    assert process.returncode == 0, (output, errors)
    result = json.loads(receipt.read_text())
    assert result["exit_code"] == 0, (output, errors)
    return Path(result["run_dir"])


def _rank_recordings(run):
    index = json.loads((run / "trace" / "index.json").read_text())
    entries = [source for source in index["sources"] if source.get("schema") == "raven.trace/v1"]
    by_rank = {}
    for entry in entries:
        reader = TraceReader(run / entry["path"], retain=20000)
        previous = -1
        while True:
            snapshot = reader.refresh(max_events=20000, max_bytes=8 << 20)
            assert snapshot["status"] != "INVALID", snapshot["warnings"]
            if snapshot["caught_up"]:
                break
            assert snapshot["read_seq"] > previous
            previous = snapshot["read_seq"]
        identity = snapshot["identity"]
        if "rank" in identity:
            by_rank[identity["rank"]] = snapshot
    assert set(by_rank) == {0, 1}, index
    assert len({snapshot["identity"]["pid"] for snapshot in by_rank.values()}) == 2
    assert {snapshot["identity"]["attempt"] for snapshot in by_rank.values()} == {run.name}
    for snapshot in by_rank.values():
        assert snapshot["identity"]["parent_pid"] > 0
        assert snapshot["identity"]["world_size"] == 2
        assert snapshot["read_seq"] > 0
        assert any(event.get("data", {}).get("file", "").endswith("application.py")
                   for event in snapshot["events"]), snapshot
    return by_rank


@pytest.mark.skipif(not _available("mpi4py") or not shutil.which("mpirun"),
                    reason="Actual mpi4py/Open MPI optional qualification requires both installed")
def test_actual_two_rank_mpi_launch_traces_and_collective_metrics(tmp_path):
    application = """
from mpi4py import MPI
import raven
comm = MPI.COMM_WORLD
def step():
    raven.metric('rank_sum', float(comm.rank + 1), reduce='sum')
    raven.metric('coordinator_only', float(comm.rank + 1))
    return comm.allreduce(comm.rank + 1, op=MPI.SUM)
assert step() == 3
"""
    run = _launch(tmp_path, [shutil.which("mpirun"), "-np", "2", sys.executable, "application.py"], application, timeout=30)
    snapshots = _rank_recordings(run)
    assert all(snapshot["identity"]["launcher"] in {"mpirun", "mpi", "openmpi"} for snapshot in snapshots.values())
    metrics = MetricReader().read(run / "metrics.jsonl")
    assert metrics["latest"]["rank_sum"] == 3.0
    assert metrics["latest"]["coordinator_only"] == 1.0
    assert not list(run.glob("metrics.rank-*.jsonl"))
    assert not metrics["errors"]


@pytest.mark.skipif(not _available("mpi4py") or not shutil.which("mpirun"),
                    reason="Actual MPI failure recovery requires installed mpi4py and Open MPI")
def test_actual_mpi_invalid_rank_and_failed_coordinator_keep_collective_order(tmp_path):
    application = """
from pathlib import Path
from mpi4py import MPI
import raven
from raven import client
comm = MPI.COMM_WORLD
def exercise():
    raven.metric('warmup', 1.0, reduce='sum')
    raven.metric('invalid_rank', float('nan') if comm.rank else 1.0, reduce='sum')
    raven.metric('after_invalid', float(comm.rank + 1), reduce='sum')
    if comm.rank == 0:
        assert client._get_client().flush(timeout=5)
        client._get_client().failed = True
    # A storage failure may suppress publication; every explicitly requested
    # collective must still occur in the same order on every rank.
    raven.metric('after_failure', float(comm.rank + 1), reduce='sum')
    raven.metric('after_failure_again', float(comm.rank + 2), reduce='mean')
    comm.Barrier()
    Path('rank-' + str(comm.rank) + '.done').write_text('complete')
exercise()
"""
    run = _launch(tmp_path, [shutil.which("mpirun"), "-np", "2", sys.executable, "application.py"], application, timeout=30)
    _rank_recordings(run)
    assert (tmp_path / "rank-0.done").read_text() == "complete"
    assert (tmp_path / "rank-1.done").read_text() == "complete"
    metrics = MetricReader().read(run / "metrics.jsonl")
    assert metrics["latest"]["warmup"] == 2.0
    assert metrics["latest"]["after_invalid"] == 3.0
    assert "invalid_rank" not in metrics["latest"]
    assert "after_failure" not in metrics["latest"]
    assert "after_failure_again" not in metrics["latest"]
    assert not metrics["errors"]


@pytest.mark.skipif(not _available("torch"), reason="Actual two-rank CPU gloo qualification requires installed PyTorch")
def test_actual_two_rank_gloo_launch_traces_and_tensor_metrics(tmp_path):
    application = """
import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel
import raven
dist.init_process_group('gloo')
torch.set_num_threads(1)
model = DistributedDataParallel(torch.nn.Linear(4, 1))
@raven.measure('loss', kind='objective')
def step():
    loss = model(torch.ones(2, 4)).square().mean()
    loss.backward()
    return loss.detach()
step()
raven.metric('rank_mean', torch.tensor(float(dist.get_rank() + 1)), reduce='mean')
dist.destroy_process_group()
"""
    run = _launch(tmp_path, [sys.executable, "-m", "torch.distributed.run", "--standalone",
                             "--nnodes=1", "--nproc-per-node=2", "application.py"], application)
    _rank_recordings(run)
    metrics = MetricReader().read(run / "metrics.jsonl")
    assert metrics["latest"]["rank_mean"] == 1.5
    assert metrics["latest"]["loss"] >= 0.0
    assert not list(run.glob("metrics.rank-*.jsonl"))
    assert not metrics["errors"]
