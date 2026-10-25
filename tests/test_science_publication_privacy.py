"""Private evidence publication must not depend on the caller's shell umask."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("mask", ["000", "002", "022", "077"])
def test_metadata_and_incident_reopening_are_private_under_permissive_umasks(
    tmp_path, mask
):
    # Change the umask in an isolated process; the test runner may have active
    # background readers. Preserve the reader's strict ownership assertions.
    script = r"""
import json, os, sys
from pathlib import Path
from raven import science_files as SF
from raven.engineering import incidents as I
os.umask(int(sys.argv[1], 8))
root = Path(sys.argv[2])
marker_modes = []
SF.publish(str(root / "ordinary"), files=[("data.txt", b"source", False)],
           receipt={"complete": True},
           final_check=lambda: marker_modes.append((root / "ordinary/.raven-incomplete").stat().st_mode & 0o777))
identity = {"cluster": "test", "job_id": "42", "attempt": "attempt-1"}
I.capture(str(root / "incident"), identity=identity,
          trigger={"kind": "oom", "clock_id": "c", "time_ns": 10, "evidence": "explicit synthetic OOM fixture"})
I.load(str(root / "incident"), identity=identity)
files = [p for p in root.rglob("*") if p.is_file()]
print(json.dumps({"marker_modes": marker_modes, "files": {str(p.relative_to(root)): p.stat().st_mode & 0o777 for p in files}}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, mask, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    assert values["marker_modes"] == [0o600]
    assert len(values["files"]) >= 4
    assert all(mode == 0o600 for mode in values["files"].values())


@pytest.mark.parametrize("fail_on", [1, 2])
def test_metadata_stream_initialization_failure_closes_descriptor_and_cleans_bundle(
    tmp_path, monkeypatch, fail_on
):
    from raven import science_files as SF

    original = SF.os.fdopen
    captured = []
    calls = 0

    def fail_once(fd, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == fail_on:
            captured.append(fd)
            raise OSError("injected stream initialization failure")
        return original(fd, *args, **kwargs)

    monkeypatch.setattr(SF.os, "fdopen", fail_once)
    destination = tmp_path / "bundle"
    with pytest.raises(OSError, match="stream initialization"):
        SF.publish(str(destination), files=[("data", b"unchanged", False)])
    assert not destination.exists()
    assert len(captured) == 1
    with pytest.raises(OSError):
        os.fstat(captured[0])
