"""Exercise real scripted CLI publication with scheduler-discovered identity."""
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("command,job_id", [
    ("telemetry", "12480001"),
    ("telemetry 12477369", "12477369"),
    ("telemetry 12480003", "12480003"),
])
def test_fake_cli_telemetry_enriches_unknown_queue_cluster_without_losing_attempt(command, job_id):
    result = subprocess.run([sys.executable, "-m", "raven", "--fake", "--no-state", "--no-plugins",
        "--run", command, "--json"], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["schema"] == "raven.telemetry/v1" and report["status"] == "ok"
    assert report["job_id"] == job_id and report["cluster"] == "fake"
    assert report["configured_task_seconds"] == 30
    assert report["config"]["JobAcctGatherType"] == "jobacct_gather/linux"
    assert report["attempt"]["submit"] and report["attempt"]["cluster"] == "fake"
    assert report["job"]["JobId"] == job_id
    assert not report["errors"]
