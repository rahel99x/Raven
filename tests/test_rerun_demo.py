"""Exercise retained-job reruns through the real parsers and reviewed action lifecycle."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import threading

import pytest

from tower import operations as O, ops_cluster as C
from tower.slurm import CommandError, FakeBackend, Slurm


def context(tmp_path, job_id, *, array=False):
    backend = FakeBackend(user="alice", speed=0)
    if array:
        base = next(row for row in backend.spec if row["id"] == "12480002_[0-7]")
        backend.spec.remove(base)
        backend.spec.extend(dict(base, id="12480002_" + str(task), allocation_id=str(12490000 + task))
                            for task in (3, 4))
    slurm = Slurm(backend, "alice")
    assert slurm.cancel([job_id])[0]
    return O.Context(slurm=slurm, selected=job_id,
                     jobs=tuple(asdict(job) for job in slurm.jobs()),
                     finished=tuple(asdict(job) for job in slurm.finished(7)),
                     scope={"host": "demo", "user": "alice"},
                     state_dir=str(tmp_path), cancel=threading.Event())


@pytest.mark.parametrize("job_id,array", [("12480001", False), ("12480003", False), ("12480005", False), ("12480002_3", True)])
def test_reviewed_demo_rerun_keeps_individual_launch_and_other_jobs(tmp_path, job_id, array):
    ctx = context(tmp_path, job_id, array=array)
    backend = ctx.slurm.b
    originals = {row["id"]: deepcopy(row) for row in backend.spec}
    script = backend.run(["scontrol", "write", "batch_script", job_id, "-"])[0]
    review = O.run("rerun-job", {"job_id": job_id}, ctx)
    assert review["status"] == "review"
    assert review["data"]["script_sha256"]
    assert review["data"]["dependency"] == (originals[job_id].get("dep") or "(null)")
    assert not any(call[:2] == ["scontrol", "requeue"] for call in backend.calls)

    result = O.apply("rerun-job", review["plan"], ctx)
    assert result["status"] == "ok"
    assert result["data"]["confirmed"] is True
    assert result["data"]["observed_restarts"] == "1"
    assert result["data"]["observed_state"] == "PENDING"
    assert json.loads(Path(result["receipt_path"]).read_text())["state"] == "finished"
    assert [call for call in backend.calls if call[:2] == ["scontrol", "requeue"]] == [["scontrol", "requeue", job_id]]
    assert backend.run(["scontrol", "write", "batch_script", job_id, "-"])[0] == script
    assert not any(call[0] == "sbatch" for call in backend.calls)
    for current in backend.spec:
        original = originals[current["id"]]
        if current["id"] == job_id:
            assert current["start"] is None and current["restarts"] == 1
            assert current["submit"] > original["submit"]
            assert {key: value for key, value in current.items() if key not in {"start", "submit", "restarts", "reason"}} == {
                key: value for key, value in original.items() if key not in {"start", "submit", "restarts", "reason"}}
        else:
            assert current == original
    assert job_id not in {job.id for job in ctx.slurm.finished(7)}
    assert next(job for job in ctx.slurm.jobs() if job.id == job_id).state == "PENDING"
    with pytest.raises(ValueError, match="already attempted"):
        O.apply("rerun-job", review["plan"], ctx)


def test_demo_rerun_blocks_changed_dependency_since_review(tmp_path):
    ctx = context(tmp_path, "12480003")
    review = O.run("rerun-job", {}, ctx)
    next(row for row in ctx.slurm.b.spec if row["id"] == ctx.selected)["dep"] = "afterok:999"
    with pytest.raises(ValueError, match="launch settings.*changed"):
        O.apply("rerun-job", review["plan"], ctx)
    assert not any(call[:2] == ["scontrol", "requeue"] for call in ctx.slurm.b.calls)


def test_demo_retrieval_reads_retained_cancelled_script_without_local_script(tmp_path):
    ctx = context(tmp_path, "12480001")
    report = O.run("batch-script", {}, ctx)
    assert report["status"] == "ok"
    assert report["data"]["source"] == "controller"
    assert report["data"]["script"].startswith("#!/bin/bash\n")
    assert ctx.selected in report["data"]["script"]
    assert not any(call[:2] == ["scontrol", "requeue"] for call in ctx.slurm.b.calls)


@pytest.mark.parametrize("via_run", [True, False])
def test_demo_existing_requeue_action_and_operation_share_attempt_state(via_run):
    backend = FakeBackend(speed=0)
    argv = ["scontrol", "requeue", "12480001"]
    if via_run:
        assert backend.run(argv)[0] == ""
    else:
        assert backend.call(argv) == (True, "")
    record = C.parse_records(backend.run(["scontrol", "show", "job", "-o", "12480001"])[0], "JobId")[0]
    assert record["JobState"] == "PENDING"
    assert record["Restarts"] == "1"
    assert record["StartTime"] == record["EndTime"] == "Unknown"
    assert backend.calls.count(argv) == 1


def test_demo_requeue_invalid_job_does_not_partially_mutate_requested_jobs():
    backend = FakeBackend(speed=0)
    original = deepcopy(backend.spec)
    with pytest.raises(CommandError, match="Invalid job id"):
        backend.run(["scontrol", "requeue", "12480001", "999"])
    assert backend.spec == original
    assert backend.call(["scontrol", "requeue", "12480001", "999"])[0] is False
    assert backend.spec == original
