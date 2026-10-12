"""One session owns worker limits across sampling, research, and notifications."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from raven import cli
from raven.config import Config
from raven.model import Store


@pytest.mark.parametrize("bad", [None, True, 1, "", "parallel", [], {}])
def test_invalid_worker_mode_fails_before_backend_creation(monkeypatch, bad):
    def unexpected(*args):
        pytest.fail("invalid worker mode created a backend")
    monkeypatch.setattr(cli, "make_backend", unexpected)
    with pytest.raises(ValueError, match="worker_mode"):
        cli.build(cli.parse(["--fake", "--no-state", "--no-plugins"]), Config({"worker_mode": bad}))


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_cli_override_and_all_session_consumers_share_governor(mode):
    cfg = Config({"worker_mode": "multi" if mode == "single" else "single"})
    args = cli.parse(["--fake", "--no-state", "--no-plugins", "--workers", mode])
    session = cli.build(args, cfg)
    governor = session.app.worker_scheduler
    try:
        assert cfg.get("worker_mode") == mode
        assert "worker_mode" in cfg.ui_locked_settings
        assert governor.status()["mode"] == mode
        assert session.sampler.worker_scheduler is governor
        assert session.app.research.worker_scheduler is governor
        assert session.sampler.on_event.worker_scheduler is governor
        assert session.store.alerts.worker_scheduler is governor
        cli.settle(session, "analytics")
        assert session.store.snapshot()["jobs"]
        session.app.run_command("workers status")
        assert session.app.command_ok
        assert mode in session.app.message.lower()
    finally:
        session.close()
    assert governor.status()["closed"]


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_headless_mode_samples_and_exits_without_nested_gpu_wait(mode, tmp_path):
    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    result = subprocess.run([sys.executable, "-m", "raven", "--fake", "--no-state",
                             "--no-plugins", "--workers", mode, "--json"],
                            capture_output=True, text=True, timeout=20, env=env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["jobs"]


def test_workers_flag_is_retained_with_run_shorthand():
    args = cli.parse(["--workers", "single", "run", "workers", "status"])
    assert args.workers == "single"
    assert args.run == "workers status"


@pytest.mark.parametrize("explicit,expected", [(None, "single"), ("multi", "multi")])
def test_restored_preference_applies_before_work_and_cli_override_wins(monkeypatch, explicit, expected):
    monkeypatch.setattr(Store, "load_ui", lambda self: {"workbench": {"worker_ui": {"mode": "single"}}})
    flags = ["--fake", "--no-state", "--no-plugins"]
    if explicit:
        flags += ["--workers", explicit]
    session = cli.build(cli.parse(flags), Config())
    try:
        state = session.app.worker_scheduler.status()
        assert state["mode"] == state["target"] == expected
        assert not state["pending"]
        assert state["running"] == state["queued"] == 0
        assert session.app.cfg.get("worker_mode") == expected
    finally:
        session.close()
