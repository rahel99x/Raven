"""Detached driver lifecycle, PID reuse protection and child cleanup on Linux."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

import pytest

from tower import ops_investigation as ops
from tower.operations import Context


def wait_until(callback, timeout=12):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        result = callback()
        if result:
            return result
        time.sleep(.025)
    raise AssertionError("Timed out waiting for bounded diagnostic state")


def alive(pid):
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
        return data[data.rfind(")")+2:].split()[0] != "Z"
    except FileNotFoundError:
        return False


@pytest.fixture
def campaign(tmp_path):
    program = tmp_path/"program.py"
    program.write_text("from pathlib import Path\nimport os,subprocess,sys,time,json\np=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\nPath(sys.argv[1]).write_text(json.dumps({'worker':os.getpid(),'child':p.pid}))\nprint('STARTED',flush=True)\ntime.sleep(60)\n")
    recipe = {"schema": "tower.diagnostic-campaign/v1", "id": "async-check", "mode": "local-diagnostic", "kind": "campaign",
              "command": [str(Path(sys.executable).resolve()), "{file:program.py}"],
              "files": [{"name": "program.py", "path": str(program), "sha256": hashlib.sha256(program.read_bytes()).hexdigest()}],
              "cases": [{"id": "slow", "arguments": ["{result}"]}],
              "oracle": {"exit_code": 0, "contains": "STARTED"},
              "limits": {"max_trials": 2, "trial_seconds": 60, "total_seconds": 60, "cpus": 1, "memory_mb": 256, "output_bytes": 8192, "repeats": 1}}
    manifest = tmp_path/"recipe.json"
    manifest.write_text(json.dumps(recipe))
    params = {"manifest": str(manifest), "destination": str(tmp_path/"work"), "action": "launch"}
    ctx = Context(scope={"cluster": "test", "user": "scientist"}, state_dir=str(tmp_path/"state"), cancel=threading.Event())
    yield recipe, params, ctx
    # Cleanup even on an assertion failure; only this test's verified driver.
    root = Path(params["destination"])
    if (root/"driver.process.json").exists():
        record = json.loads((root/"driver.process.json").read_text())
        if ops._process_matches(record):
            fd = os.pidfd_open(record["pid"])
            try:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
            finally:
                os.close(fd)
            wait_until(lambda: not alive(record["pid"]))
    ops._reap()


def launch(campaign):
    recipe, params, ctx = campaign
    reviewed = ops.run("diagnostic-campaign", params, ctx)
    result = ops.apply("diagnostic-campaign", reviewed["plan"], ctx)
    assert result["data"]["result"]["state"] == "started"
    root = Path(params["destination"])
    status_params = {"destination": str(root), "action": "status"}
    status = wait_until(lambda: (r if (r := ops.run("diagnostic-campaign", status_params, ctx))["data"]["running"] else None))
    child_file = root/"trials"/"0000"/"result.json"
    wait_until(lambda: child_file.exists())
    children = json.loads(child_file.read_text())
    return root, status_params, status, children


def test_async_launch_releases_worker_and_reviewed_cancel_reaps_children(campaign):
    _, params, ctx = campaign
    root, status_params, status, children = launch(campaign)
    assert not (root/"result.json").exists()
    # Reading status does not wait for the long-running application.
    assert ops.run("diagnostic-campaign", status_params, ctx)["data"]["running"]
    cancel = ops.run("diagnostic-campaign", {**status_params, "action": "cancel"}, ctx)
    assert "plan" in cancel
    requested = ops.apply("diagnostic-campaign", cancel["plan"], ctx)
    assert requested["data"]["state"] == "cancel-requested"
    result = wait_until(lambda: (json.loads((root/"result.json").read_text()) if (root/"result.json").exists() else None))
    assert result["state"] == "partial" and result["trials"][0]["state"] == "cancelled"
    wait_until(lambda: not any(alive(pid) for pid in children.values()))
    wait_until(lambda: not ops.run("diagnostic-campaign", status_params, ctx)["data"]["running"])
    assert "plan" not in ops.run("diagnostic-campaign", {**status_params, "action": "cancel"}, ctx)


def test_driver_crash_cleans_process_group_and_never_relaunches(campaign):
    _, params, ctx = campaign
    root, status_params, status, children = launch(campaign)
    record = status["data"]["process"]
    fd = os.pidfd_open(record["pid"])
    try:
        signal.pidfd_send_signal(fd, signal.SIGKILL)
    finally:
        os.close(fd)
    wait_until(lambda: not any(alive(pid) for pid in children.values()))
    status = wait_until(lambda: (r if (r := ops.run("diagnostic-campaign", status_params, ctx))["data"]["state"] == "interrupted" else None))
    assert not status["data"]["running"]
    assert (root/"run.claim.json").exists() and not (root/"result.json").exists()
    with pytest.raises(ValueError):
        ops.run("diagnostic-campaign", params, ctx)


def test_cancel_refuses_changed_identity_and_other_connection(campaign):
    _, params, ctx = campaign
    root, status_params, status, children = launch(campaign)
    cancel = ops.run("diagnostic-campaign", {**status_params, "action": "cancel"}, ctx)
    record = json.loads((root/"driver.process.json").read_text())
    original = (root/"driver.process.json").read_bytes()
    record["start"] = str(int(record["start"])+1)
    (root/"driver.process.json").write_text(json.dumps(record))
    try:
        with pytest.raises(ValueError, match="changed"):
            ops.apply("diagnostic-campaign", cancel["plan"], ctx)
    finally:
        (root/"driver.process.json").write_bytes(original)
    assert all(alive(pid) for pid in children.values())
    with pytest.raises(ValueError, match="another connection"):
        ops.run("diagnostic-campaign", status_params, replace(ctx, scope={"cluster": "other"}))
