"""Installed-lifecycle harness supervision stays bounded and owns cleanup."""

import importlib.util
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "qualify_updates",
    Path(__file__).resolve().parents[1] / "scripts/qualify_updates.py",
)
qualification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualification)


def run(tmp_path, code, **kwargs):
    return qualification.supervise(
        [sys.executable, "-c", code],
        tmp_path / "command.log",
        cwd=tmp_path,
        timeout=3,
        grace=1,
        **kwargs,
    )


def test_success_reaps_and_logs(tmp_path):
    result = run(tmp_path, "print('observed command')")
    assert result["exit"] == 0 and result["reaped"] and not result["failure"]
    assert (tmp_path / "command.log").read_text() == "observed command\n"


def test_stdin_script_uses_actual_pipe_style_bootstrap_invocation(tmp_path):
    script = tmp_path / "bootstrap.sh"
    script.write_text('printf "stdin script argument: %s\\n" "$1"\n')
    result = qualification.supervise(
        ["bash", "-s", "--", "owned-source"],
        tmp_path / "command.log",
        cwd=tmp_path,
        stdin_path=script,
    )
    assert result["exit"] == 0 and result["reaped"] and not result["failure"]
    assert (
        tmp_path / "command.log"
    ).read_text() == "stdin script argument: owned-source\n"


def test_output_cap_is_enforced_while_child_is_running(tmp_path):
    result = run(
        tmp_path, "import os,time; os.write(1,b'x'*65536); time.sleep(60)", max_log=1024
    )
    assert result["failure"] and "log limit" in result["failure"]
    assert (
        result["log_bytes"] == 1024
        and result["discarded_log_bytes"] > 0
        and result["reaped"]
    )
    assert (tmp_path / "command.log").stat().st_size == 1024


def test_timeout_allows_installer_to_clean_its_nested_session(tmp_path):
    code = """
import os,signal,subprocess,sys,time
from pathlib import Path
child = subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
Path('nested.pid').write_text(str(child.pid))
def stop(signum,frame):
    time.sleep(.1)
    os.killpg(child.pid,signal.SIGTERM)
    child.wait(timeout=1)
    Path('cleaned').write_text('nested session reaped')
    sys.exit(0)
signal.signal(signal.SIGTERM,stop)
while True: time.sleep(.01)
"""
    result = qualification.supervise(
        [sys.executable, "-c", code],
        tmp_path / "command.log",
        cwd=tmp_path,
        timeout=0.3,
        grace=1,
    )
    assert result["timed_out"] and result["reaped"] and result["failure"]
    assert (tmp_path / "cleaned").read_text() == "nested session reaped"
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "nested.pid").read_text()), 0)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_interruption_is_recorded_after_owned_group_cleanup(tmp_path, signum):
    previous = signal.getsignal(signum)
    result = run(
        tmp_path,
        f"import os,signal,time;os.kill(os.getppid(),{int(signum)});time.sleep(60)",
    )
    assert result["interrupted_signal"] == signum and result["reaped"]
    assert "Interrupted" in result["failure"] and signal.getsignal(signum) == previous


def test_source_snapshot_checks_head_inventory_and_contents(tmp_path):
    def git(*args):
        return subprocess.check_output(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                *args,
            ],
            cwd=tmp_path,
        )

    git("init", "-q")
    (tmp_path / "tracked").write_text("original")
    git("add", "tracked")
    git("commit", "-qm", "first")
    initial = qualification.source_snapshot(tmp_path)
    (tmp_path / "added").write_text("new inventory")
    assert qualification.source_snapshot(tmp_path) != initial
    (tmp_path / "added").unlink()
    (tmp_path / "tracked").write_text("changed")
    assert qualification.source_snapshot(tmp_path) != initial
    (tmp_path / "tracked").write_text("original")
    assert qualification.source_snapshot(tmp_path) == initial
    git("commit", "--allow-empty", "-qm", "head changed")
    final = qualification.source_snapshot(tmp_path)
    assert (
        final["hashes"] == initial["hashes"]
        and final["inventory"] == initial["inventory"]
    )
    assert final != initial


def test_failed_check_is_saved_before_main_raises(tmp_path, monkeypatch):
    root = tmp_path / "source"
    (root / "raven").mkdir(parents=True)
    (root / "raven/__init__.py").write_text('__version__ = "1.2.3"\n')
    monkeypatch.setattr(qualification, "ROOT", root)
    monkeypatch.setattr(
        qualification,
        "source_snapshot",
        lambda root: {
            "commit": "frozen",
            "inventory": ["raven/__init__.py"],
            "hashes": {},
        },
    )
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(qualification.tempfile, "mkdtemp", lambda **kwargs: str(work))
    monkeypatch.setattr(
        qualification,
        "supervise",
        lambda *args, **kwargs: {
            "exit": -15,
            "failure": "Command exceeded 900 seconds",
            "timed_out": True,
            "reaped": True,
        },
    )
    output = tmp_path / "evidence"
    monkeypatch.setattr(sys, "argv", ["qualify_updates.py", "--output", str(output)])
    with pytest.raises(RuntimeError, match="Qualification failed"):
        qualification.main()
    result = json.loads((output / "result.json").read_text())
    assert not result["passed"] and result["checks"][0]["timed_out"]
    assert result["checks"][0]["failure"] == "Command exceeded 900 seconds"
