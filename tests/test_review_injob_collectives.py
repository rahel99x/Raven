"""Optional live-runtime qualification; never substitutes rank-shaped mocks."""

import importlib.util
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parents[1])


@pytest.mark.skipif(
    importlib.util.find_spec("torch") is None,
    reason="optional CPU PyTorch/gloo runtime is not installed",
)
def test_live_spawn_gloo_early_metric_unconfigured_peer_and_application_collective(tmp_path):
    program = tmp_path / "gloo.py"
    program.write_text(
        textwrap.dedent("""
        import sys,json
        from pathlib import Path
        from datetime import timedelta
        sys.path.insert(0, ROOT)
        import torch
        import torch.distributed as dist
        import torch.multiprocessing as mp
        from raven import client
        def worker(rank, rendezvous, output):
            if rank == 0:
                client.configure(path=output)
                client.metric("early", 99)
                assert client._state.helper and not client._state.allowed
            dist.init_process_group("gloo",init_method="file://"+rendezvous,rank=rank,world_size=2,timeout=timedelta(seconds=20))
            client.metric("sum",rank+1,reduce="sum")
            value=torch.tensor(float(rank+1));dist.all_reduce(value);assert value.item()==3
            client.metric("invalid",float("nan") if rank==0 else 2,reduce="sum")
            value=torch.tensor(float(rank+1));dist.all_reduce(value);assert value.item()==3
            if rank == 0:
                assert client._state.allowed and not client._state.helper
                assert client._state.flush()
            dist.destroy_process_group()
        if __name__ == "__main__":
            mp.spawn(worker,args=(sys.argv[1],sys.argv[2]),nprocs=2,join=True)
    """).replace("ROOT", repr(ROOT))
    )
    metrics = tmp_path / "metrics.jsonl"
    result = subprocess.run(
        [sys.executable, str(program), str(tmp_path / "rendezvous"), str(metrics)],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [json.loads(line) for line in metrics.read_text().splitlines()]
    assert [row["metrics"]["sum"] for row in rows if "sum" in row["metrics"]] == [3.0]
    assert not any("invalid" in row["metrics"] for row in rows)


@pytest.mark.skipif(
    importlib.util.find_spec("mpi4py") is None or shutil.which("mpiexec") is None,
    reason="optional mpi4py and real mpiexec runtime are not installed",
)
def test_live_mpi_unconfigured_peer_then_application_collective(tmp_path):
    program = tmp_path / "mpi.py"
    program.write_text(
        textwrap.dedent("""
        import sys
        sys.path.insert(0, ROOT)
        from raven import client
        # Rank zero uses Raven before mpi4py initializes MPI.
        import os
        rank = int(os.environ.get("PMI_RANK",os.environ.get("OMPI_COMM_WORLD_RANK","0")))
        if rank == 0:
            client.configure(path=sys.argv[1])
            client.metric("early",99)
        from mpi4py import MPI
        comm = MPI.COMM_WORLD
        client.metric("sum",comm.rank+1,reduce="sum")
        assert comm.allreduce(comm.rank+1) == 3
        if comm.rank == 0:
            assert client._state.flush()
    """).replace("ROOT", repr(ROOT))
    )
    metrics = tmp_path / "metrics.jsonl"
    result = subprocess.run(
        [shutil.which("mpiexec"), "-n", "2", sys.executable, str(program), str(metrics)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [json.loads(line) for line in metrics.read_text().splitlines()]
    assert [row["metrics"]["sum"] for row in rows if "sum" in row["metrics"]] == [3.0]
