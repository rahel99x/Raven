"""Actual child processes, cancellation, resource admission, and Dask ownership."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from raven import campaign_common as common
from raven import campaign_dask as dask
from raven import campaign_packing as packing


def manifest(tmp_path, tasks=None):
    return {"schema": "raven.packed-tasks/v1", "cpus": 4, "memory_mb": 8, "parallel": 3,
            "tasks": tasks if tasks is not None else [
                {"id": "task-" + str(i), "argv": [sys.executable, "-c", "import time; print(time.monotonic(),flush=True); time.sleep(.08); print(time.monotonic())"],
                 "cwd": str(tmp_path), "cpus": 2, "memory_mb": 4} for i in range(4)]}


def test_actual_packed_resource_admission_and_idempotent_receipts(tmp_path):
    output = tmp_path / "results"
    result = packing.execute(manifest(tmp_path), output, local=True, tick=.005)
    assert result["ok"]
    assert len(result["tasks"]) == 4
    events = []
    for i in range(4):
        start, end = map(float, (output / "logs" / f"task-{i}.out").read_text().split())
        events.extend([(start, 1), (end, -1)])
    active = maximum = 0
    for _, change in sorted(events):
        active += change
        maximum = max(maximum, active)
    assert maximum <= 2  # CPU and memory admission both limit concurrency.
    originals = {p.name: p.stat().st_mtime_ns for p in (output / "logs").iterdir()}
    again = packing.execute(manifest(tmp_path), output, local=True)
    assert again == result
    assert originals == {p.name: p.stat().st_mtime_ns for p in (output / "logs").iterdir()}


def test_task_named_manifest_and_literal_shell_metacharacters(tmp_path):
    tasks = [{"id": "manifest", "argv": [sys.executable, "-c", "import sys; print(sys.argv[1])", "$(touch should-not-exist); *"],
              "cwd": str(tmp_path), "memory_mb": 1}]
    output = tmp_path / "results"
    result = packing.execute(manifest(tmp_path, tasks), output, local=True)
    assert result["tasks"]["manifest"]["state"] == "completed"
    assert (output / "logs" / "manifest.out").read_text().strip() == "$(touch should-not-exist); *"
    assert not (tmp_path / "should-not-exist").exists()


def test_cancel_terminates_active_and_never_starts_pending(tmp_path):
    tasks = [{"id": "task-" + str(i), "argv": [sys.executable, "-c", "import time; print('started',flush=True); time.sleep(60)"],
              "cwd": str(tmp_path), "cpus": 4, "memory_mb": 8} for i in range(3)]
    cancel = threading.Event()
    timer = threading.Timer(.15, cancel.set)
    timer.start()
    try:
        started = time.monotonic()
        result = packing.execute(manifest(tmp_path, tasks), tmp_path / "results", local=True, cancel=cancel, tick=.01)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 3
    assert {row["state"] for row in result["tasks"].values()} == {"cancelled"}
    assert not (tmp_path / "results" / "logs" / "task-1.out").exists()


def test_timeout_and_exit_failure_have_distinct_receipts(tmp_path):
    tasks = [{"id": "timeout", "argv": [sys.executable, "-c", "import time; time.sleep(60)"], "timeout_s": 1,
              "cwd": str(tmp_path), "memory_mb": 1},
             {"id": "failure", "argv": [sys.executable, "-c", "raise SystemExit(7)"], "cwd": str(tmp_path), "memory_mb": 1}]
    result = packing.execute(manifest(tmp_path, tasks), tmp_path / "results", local=True, tick=.01)
    assert result["tasks"]["timeout"]["state"] == "timeout"
    assert result["tasks"]["failure"]["returncode"] == 7
    assert not result["ok"]


def test_descendant_cannot_escape_resource_accounting(tmp_path):
    code = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid,flush=True)"
    tasks = [{"id": "parent", "argv": [sys.executable, "-c", code], "cwd": str(tmp_path), "memory_mb": 1}]
    packing.execute(manifest(tmp_path, tasks), tmp_path / "results", local=True)
    pid = int((tmp_path / "results" / "logs" / "parent.out").read_text())
    status = Path(f"/proc/{pid}/stat")
    for _ in range(20):
        if not status.exists() or status.read_text().rsplit(")", 1)[1].split()[0] == "Z":
            break
        time.sleep(.01)
    else:
        pytest.fail("background descendant remained alive after logical task completion")


@pytest.mark.parametrize("damage", ["duplicate", "cpu", "memory", "argv", "switch", "timeout", "parallel", "schema"])
def test_packing_manifest_bounds(damage, tmp_path):
    recipe = manifest(tmp_path)
    if damage == "duplicate":
        recipe["tasks"][1]["id"] = recipe["tasks"][0]["id"]
    elif damage == "cpu":
        recipe["tasks"][0]["cpus"] = 5
    elif damage == "memory":
        recipe["tasks"][0]["memory_mb"] = 9
    elif damage == "argv":
        recipe["tasks"][0]["argv"] = "echo unsafe"
    elif damage == "switch":
        recipe["tasks"][0]["argv"] = ["--pty"]
    elif damage == "timeout":
        recipe["tasks"][0]["timeout_s"] = True
    elif damage == "parallel":
        recipe["parallel"] = 0
    else:
        recipe["schema"] = "unsupported"
    with pytest.raises(ValueError):
        packing.validate(recipe)


def test_rejects_unowned_directory_symlink_and_changed_manifest(tmp_path):
    target = tmp_path / "actual"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        packing.execute(manifest(tmp_path), link, local=True)
    (target / "unrelated").write_text("keep")
    with pytest.raises(ValueError, match="empty"):
        packing.execute(manifest(tmp_path), target, local=True)
    results = tmp_path / "results"
    packing.execute(manifest(tmp_path), results, local=True)
    changed = manifest(tmp_path)
    changed["tasks"][0]["argv"] = ["/bin/false"]
    with pytest.raises(ValueError, match="different"):
        packing.execute(changed, results, local=True)


def test_fifo_receipt_does_not_block(tmp_path):
    recipe = manifest(tmp_path)
    output = tmp_path / "results"
    output.mkdir()
    (output / "manifest.json").write_text(json.dumps(packing.validate(recipe)))
    (output / "receipts").mkdir()
    os.mkfifo(output / "receipts" / "task-0.json")
    start = time.monotonic()
    with pytest.raises(ValueError, match="regular"):
        packing.execute(recipe, output, local=True)
    assert time.monotonic() - start < .5


def test_standalone_runner_needs_no_raven_import(tmp_path):
    runner = tmp_path / "runner.py"
    runner.write_bytes(Path(packing.__file__).read_bytes())
    recipe = manifest(tmp_path, [{"id": "one", "argv": [sys.executable, "-c", "print('portable')"],
                               "cwd": str(tmp_path), "memory_mb": 1}])
    recipe_path = tmp_path / "tasks.json"
    recipe_path.write_text(json.dumps(recipe))
    result = subprocess.run([sys.executable, "-I", str(runner), str(recipe_path), str(tmp_path / "out"), "--local"],
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert (tmp_path / "out" / "logs" / "one.out").read_text().strip() == "portable"


class Cluster:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.scales = []
        self.worker_spec = {i: {} for i in range(3)}
        self.scheduler_info = {"workers": {"connected": {}}}
        self.scheduler_address = "tcp://127.0.0.1:8786"
        self.closed = False
        self.adaptive_stopped = False

    def scale(self, jobs):
        self.scales.append(jobs)

    def adapt(self, **kwargs):
        self.adapt_kwargs = kwargs
        return self

    def stop(self):
        self.adaptive_stopped = True

    def close(self, timeout):
        self.closed = True


def pool(tmp_path, adaptive=False):
    recipe = {"schema": "raven.dask-pool/v1", "cores": 4, "memory_mb": 1024, "max_jobs": 5, "adaptive": adaptive}
    config = {"recipe": recipe, "owner": "one-controller", "scope": {"cluster": "test"}, "target": 1}
    common.atomic(tmp_path / "config.json", config)
    return config


def test_dask_counts_queued_and_connected_in_same_units(tmp_path):
    pool(tmp_path)
    created = []
    def factory(**kwargs):
        created.append(Cluster(**kwargs))
        return created[-1]
    state = dask.controller(tmp_path, cluster_factory=factory, max_cycles=1)
    assert state["requested_jobs"] == 3
    assert state["connected_jobs"] == 1
    assert state["queued_or_starting_jobs"] == 2
    assert created[0].kwargs["processes"] == 1
    assert created[0].scales == [1]
    assert created[0].closed


def test_dask_scale_stops_competing_adaptive_controller(tmp_path):
    config = pool(tmp_path, adaptive=True)
    common.atomic(tmp_path / "request.json", {"owner": config["owner"], "sequence": 1, "action": "scale", "target": 2})
    cluster = Cluster()
    state = dask.controller(tmp_path, cluster_factory=lambda **_: cluster, max_cycles=1)
    assert cluster.adaptive_stopped
    assert cluster.scales == [2]
    assert state["desired_jobs"] == 2
    assert state["adaptive"] is False


def test_dask_wrong_owner_cannot_scale(tmp_path):
    pool(tmp_path)
    common.atomic(tmp_path / "request.json", {"owner": "someone-else", "sequence": 1, "action": "scale", "target": 5})
    cluster = Cluster()
    dask.controller(tmp_path, cluster_factory=lambda **_: cluster, max_cycles=1)
    assert cluster.scales == [1]


def test_dask_controller_failure_is_durable_and_closes_cluster(tmp_path):
    config = pool(tmp_path)
    common.atomic(tmp_path / "request.json", {"owner": config["owner"], "sequence": 1, "action": "scale", "target": 500})
    cluster = Cluster()
    with pytest.raises(ValueError, match="target"):
        dask.controller(tmp_path, cluster_factory=lambda **_: cluster, max_cycles=1)
    assert common.read(tmp_path / "status.json")["state"] == "failed"
    assert cluster.closed


@pytest.mark.parametrize("field,value", [("cores", 0), ("memory_mb", True), ("max_jobs", 10000), ("min_jobs", 20),
                                         ("walltime", "1; touch x"), ("adaptive", "yes")])
def test_dask_recipe_resource_limits(field, value):
    recipe = {"schema": "raven.dask-pool/v1", "cores": 4, "memory_mb": 1024, "max_jobs": 5, field: value}
    with pytest.raises(ValueError):
        dask.validate(recipe)


def test_pid_reuse_is_not_controller_ownership():
    status = {"pid": os.getpid(), "process_start": "different-start", "state": "running"}
    assert not dask.alive(status)
