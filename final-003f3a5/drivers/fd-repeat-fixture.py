"""Setup must remain offline, preserve personal files, and work outside the checkout."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

ROOT = Path("/workspace/Raven")
_spec = importlib.util.spec_from_file_location("raven_setup", ROOT / "scripts" / "setup.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)


def test_setup_selects_mode_without_contacting_cluster(monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda _: None)
    assert setup.select_mode("auto", "") == "demo"
    assert setup.select_mode("auto", "research") == "remote"
    monkeypatch.setattr(setup.shutil, "which", lambda _: "/usr/bin/squeue")
    assert setup.select_mode("auto", "") == "local"
    assert setup.select_mode("demo", "") == "demo"
    with pytest.raises(setup.SetupError, match="needs --host"):
        setup.select_mode("remote", "")
    with pytest.raises(setup.SetupError, match="only be used"):
        setup.select_mode("local", "research")
    with pytest.raises(setup.SetupError, match="SSH hostname"):
        setup.select_mode("remote", "-oProxyCommand=anything")


def test_setup_rejects_unknown_or_remote_local_profile_before_creating_files(tmp_path, monkeypatch, capsys):
    def forbidden(_path):
        pytest.fail("Invalid profile created or reused an environment")
    monkeypatch.setattr(setup, "prepare_venv", forbidden)
    for profile, message in (("unknown-profile", "Unknown setup profile"), ("remote", "without an SSH host")):
        assert setup.main(["--mode", "local", "--profile", profile, "--venv", str(tmp_path / "env")]) == 1
        assert message in capsys.readouterr().err
        assert not (tmp_path / "env").exists()


@pytest.mark.parametrize("requested,expected", [("desktop", "desktop"), ("", "carc")])
def test_local_setup_checks_and_prints_selected_profile_without_running_slurm(tmp_path, requested, expected):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    called = tmp_path / "slurm-was-called"
    for name in ("squeue", "scontrol", "sinfo", "sacct", "sstat", "sshare", "sreport", "sacctmgr", "sbatch", "scancel"):
        tool = binaries / name
        tool.write_text("#!/bin/sh\nprintf '%s\\n' \"$0\" >> " + shlex.quote(str(called)) + "\nexit 97\n")
        tool.chmod(0o755)
    personal = tmp_path / "personal.json"
    personal.write_text("invalid personal settings must not affect setup")
    env = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ.get("PATH", ""),
               RAVEN_CONFIG=str(personal), XDG_CONFIG_HOME=str(tmp_path / "config"), XDG_STATE_HOME=str(tmp_path / "state"))
    command = [sys.executable, str(ROOT / "scripts" / "setup.py"), "--mode", "local",
               "--venv", str(tmp_path / "environment with spaces"), "--report", str(tmp_path / "report.txt")]
    if requested:
        command += ["--profile", requested]
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Raven / local environment" in result.stdout and "Ready (local)" in result.stdout
    launch = next(line.strip() for line in result.stdout.splitlines() if line.startswith("  ") and "--profile " in line)
    args = shlex.split(launch)
    assert args[args.index("--profile") + 1] == expected and "--fake" not in args
    assert args[args.index("--config") + 1] == str(ROOT / "docs" / "config.example.json")
    assert not called.exists(), "Setup must only inspect local Slurm capabilities"
    assert personal.read_text() == "invalid personal settings must not affect setup"


def test_setup_covers_every_research_workspace():
    from raven.research import RESEARCH_VIEWS
    assert set(setup.RESEARCH_SMOKE_VIEWS) == {name for name, _ in RESEARCH_VIEWS}
    assert len(setup.RESEARCH_SMOKE_VIEWS) == 14


@pytest.mark.parametrize("recording", [True] * 12)
def test_batched_views_use_fresh_closed_real_launcher_sessions(tmp_path, monkeypatch, recording):
    import signal
    import threading
    import time
    from raven import cli, clock, raven_execution, screen

    config = tmp_path / "config.json"
    config.write_text("{}\n")
    sessions, frames = [], []
    build, render = cli.build, screen.once_text
    cases = list(setup.view_cases())
    # Repeat the same real frame after all other cases to detect shared UI state.
    recording_flags = ["--record", str(tmp_path / "sessions.jsonl")] if recording else []
    monkeypatch.setattr(setup, "view_cases", lambda: iter(
        (name, [*options, *recording_flags]) for name, options in [*cases, cases[0]]))
    monkeypatch.setattr(clock, "now", lambda: 1700000000.)
    fake_init = cli.FakeBackend.__init__
    monkeypatch.setattr(cli.FakeBackend, "__init__", lambda self, user: fake_init(self, user, t0=1700000000., speed=0.))

    def tracked_build(*args, **kwargs):
        if sessions:
            previous = sessions[-1]
            assert previous.store._storage_cancel.is_set()
            assert previous.sampler.stop.is_set()
            assert previous.app.worker_scheduler._closed
        session = build(*args, **kwargs)
        assert not session.store.persist and session.store._state_owner is None
        assert not session.store._storage_cancel.is_set()
        sessions.append(session)
        return session

    def tracked_render(*args, **kwargs):
        result = render(*args, **kwargs)
        frames.append(result)
        return result

    monkeypatch.setattr(cli, "build", tracked_build)
    monkeypatch.setattr(screen, "once_text", tracked_render)
    initial_threads = set(threading.enumerate())
    initial_fds = len(list(Path('/proc/self/fd').iterdir())) if Path('/proc/self/fd').exists() else None
    def fd_targets():
        targets = {}
        for name in Path('/proc/self/fd').iterdir():
            try:
                targets[name.name] = os.readlink(name)
            except FileNotFoundError:
                pass
        return targets
    initial_targets = fd_targets() if initial_fds is not None else {}
    initial_env, initial_path, initial_argv = dict(os.environ), list(sys.path), list(sys.argv)
    initial_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    initial_streams = sys.stdout, sys.stderr
    initial_router = raven_execution.process_router()
    result = setup.validate_views(config)
    assert result == {"schema": "raven.setup-views/v1", "cases": [
        {"case": name, "exit_code": 0, "text_valid": True} for name, _ in [*cases, cases[0]]]}
    assert len(sessions) == len({id(item) for item in sessions}) == 49
    assert len({id(item.store) for item in sessions}) == 49
    assert len({id(item.app) for item in sessions}) == 49
    assert all(item.store._storage_cancel.is_set() and item.sampler.stop.is_set() for item in sessions)
    assert frames[0] == frames[-1]
    assert dict(os.environ) == initial_env and list(sys.path) == initial_path and list(sys.argv) == initial_argv
    assert (sys.stdout, sys.stderr) == initial_streams
    assert {sig: signal.getsignal(sig) for sig in initial_signals} == initial_signals
    assert raven_execution.process_router() is initial_router
    assert clock._source is time.time
    # Shutdown cancels pending work; allow already-running finite tasks to finish.
    # enumerate() omits exiting threads before their SQLite TLS cleanup ends.
    owned_threads = {thread for item in sessions
                     for thread in item.app.worker_scheduler._executor._threads}
    for thread in owned_threads | (set(threading.enumerate()) - initial_threads):
        thread.join(timeout=2)
        assert not thread.is_alive(), f"View validation leaked thread {thread.name}"
    if initial_fds is not None:
        final_fds = len(list(Path('/proc/self/fd').iterdir()))
        diagnostic = {'initial': initial_targets, 'final': fd_targets(),
                      'session_cache_paths': [str(getattr(getattr(item.backend, 'cache', None), 'path', '')) for item in sessions]}
        if final_fds > initial_fds:
            (tmp_path / 'fd-leak.json').write_text(json.dumps(diagnostic, indent=2))
        assert final_fds <= initial_fds, diagnostic


@pytest.mark.parametrize("code,text,error,expected", [
    (0, "", "", "clean ASCII"),
    (0, "snowman \u2603", "", "clean ASCII"),
    (0, "\x1b[0mtext", "", "clean ASCII"),
    (2, "", "configuration is unreadable", "configuration is unreadable"),
])
def test_batched_views_identify_the_exact_failed_case(tmp_path, monkeypatch, code, text, error, expected):
    from raven import raven_launch
    def failing(_arguments):
        print(text, end="")
        print(error, file=sys.stderr)
        return code
    monkeypatch.setattr(raven_launch, "light_main", failing)
    with pytest.raises(setup.SetupError, match=expected) as caught:
        setup.validate_views(tmp_path / "unused.json")
    assert "jobs/ascii" in str(caught.value)


def test_parent_rejects_incomplete_batch_before_publishing_report(tmp_path, monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(list(map(str, command)))
        if "--json" in command:
            return '{"jobs": [{"id": "1"}]}'
        assert "--_validate-views" in command
        return '{"schema": "raven.setup-views/v1", "cases": []}'
    monkeypatch.setattr(setup, "run", run)
    report = tmp_path / "report.txt"
    with pytest.raises(setup.SetupError, match="every expected ASCII/Unicode case"):
        setup.validate(sys.executable, "demo", "", report)
    assert len(calls) == 2 and not report.exists()


def test_setup_preserves_unrelated_paths(tmp_path):
    occupied = tmp_path / "important"
    occupied.mkdir()
    sentinel = occupied / "notes.txt"
    sentinel.write_text("keep this")
    with pytest.raises(setup.SetupError, match="Refusing to replace"):
        setup.prepare_venv(occupied)
    assert sentinel.read_text() == "keep this"
    with pytest.raises(setup.SetupError, match="dedicated"):
        setup.prepare_venv(ROOT)


def test_nested_venv_keeps_standard_library_and_reuses_existing_environment(tmp_path):
    """Exercise actual startup, including relocatable Python installations."""
    environment = tmp_path / "nested environment with spaces"
    python = setup.prepare_venv(environment)
    probe = subprocess.run([str(python), "-I", "-c",
        "import encodings, json, pathlib, sys; "
        "print(json.dumps({'prefix': sys.prefix, 'base': sys.base_prefix, "
        "'codec': encodings.__file__, 'isolation': sys.prefix != sys.base_prefix}))"],
        cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert probe.returncode == 0, probe.stderr
    data = json.loads(probe.stdout)
    assert Path(data['prefix']).resolve() == environment.resolve()
    assert Path(data['base']).resolve() == Path(sys.base_prefix).resolve()
    assert data['isolation'] and Path(data['codec']).is_file()
    config = (environment / 'pyvenv.cfg').read_bytes()
    assert setup.prepare_venv(environment) == python
    assert (environment / 'pyvenv.cfg').read_bytes() == config


def test_setup_preserves_existing_report_and_symlink(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("new report")
    report = tmp_path / "report.txt"
    report.write_text("important existing report")
    assert not setup.publish_report(source, report)
    assert report.read_text() == "important existing report"
    link = tmp_path / "link.txt"
    link.symlink_to(report)
    assert not setup.publish_report(source, link)
    assert report.read_text() == "important existing report"


def test_offline_setup_is_repeatable_and_ignores_personal_configuration(tmp_path):
    environment = tmp_path / "venv with spaces"
    report = tmp_path / "preview.txt"
    personal_config = tmp_path / "config"
    plugin_directory = personal_config / "raven" / "plugins"
    plugin_directory.mkdir(parents=True)
    marker = tmp_path / "plugin-was-executed"
    (plugin_directory / "untrusted.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
    )
    invalid_config = personal_config / "invalid.json"
    invalid_config.write_text("This personal config must never be loaded by setup.")
    state = tmp_path / "state" / "raven"
    state.mkdir(parents=True)
    saved_state = state / "ui.json"
    saved_state.write_text('{"tab": "history"}\n')
    env = dict(os.environ, RAVEN_CONFIG=str(invalid_config), XDG_CONFIG_HOME=str(personal_config), XDG_STATE_HOME=str(state.parent))
    command = [sys.executable, str(ROOT / "scripts" / "setup.py"), "--mode", "demo", "--venv", str(environment), "--report", str(report)]
    first = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "Ready (demo)" in first.stdout
    assert report.read_text().isascii()
    assert "JOBS" in report.read_text().upper()
    # Default setup must not require pip or contact a package registry.
    pip_probe = subprocess.run([str(environment / "bin" / "python"), "-m", "pip", "--version"], capture_output=True)
    assert pip_probe.returncode != 0
    report.write_text("user edited report")
    second = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "Existing report preserved" in second.stdout
    assert report.read_text() == "user edited report"
    assert saved_state.read_text() == '{"tab": "history"}\n'
    assert not marker.exists()
    assert invalid_config.read_text() == "This personal config must never be loaded by setup."


def test_source_launcher_works_outside_checkout(tmp_path):
    config = tmp_path / "empty.json"
    config.write_text("{}")
    result = subprocess.run(
        [str(ROOT / "scripts" / "raven"), "--fake", "--config", str(config), "--no-state", "--no-plugins", "--json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["jobs"]
