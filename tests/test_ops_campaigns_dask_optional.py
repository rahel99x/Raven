"""Real optional Dask API integration; scheduler submit/cancel remain simulated."""
from pathlib import Path

import pytest

pytest.importorskip("dask_jobqueue")
from dask_jobqueue.slurm import SLURMJob

from raven.campaign_common import atomic
from raven.campaign_dask import controller


def test_real_dask_jobqueue_creates_and_closes_owned_slurm_workers(tmp_path, monkeypatch):
    submitted, closed = [], []
    async def submit(self, path):
        submitted.append(Path(path).read_text())
        return "Submitted batch job " + str(9000 + len(submitted))
    async def close(job_id, cancel_command):
        closed.append(job_id)
    monkeypatch.setattr(SLURMJob, "_submit_job", submit)
    monkeypatch.setattr(SLURMJob, "_close_job", staticmethod(close))
    atomic(tmp_path / "config.json", {
        "owner": "adapter-test", "scope": {"cluster": "offline"}, "target": 2,
        "recipe": {"schema": "raven.dask-pool/v1", "cores": 2, "memory_mb": 256,
                   "max_jobs": 3, "adaptive": False}})
    state = controller(tmp_path, max_cycles=3)
    assert state["state"] == "stopped"
    assert state["requested_jobs"] == state["queued_or_starting_jobs"] == 2
    assert state["connected_jobs"] == 0
    assert len(submitted) == len(closed) == 2
    assert all("#SBATCH --cpus-per-task=2" in script for script in submitted)
    assert all("--nworkers " not in script or "--nworkers 1" in script for script in submitted)
