"""The user installer publishes checked releases and preserves other commands."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install.py"
SPEC = importlib.util.spec_from_file_location("raven_user_installer", SCRIPT)
I = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(I)


@pytest.fixture
def installer(tmp_path, monkeypatch):
    calls = []

    def create(path):
        (path / "bin").mkdir()
        for name in ("python", *I.COMMANDS):
            (path / "bin" / name).write_text("checked executable")

    monkeypatch.setattr(I.venv, "EnvBuilder", lambda **kwargs: SimpleNamespace(create=create))
    monkeypatch.setattr(I, "run", lambda command, **kwargs: calls.append(list(command)))
    monkeypatch.setattr(I, "source_metadata", lambda: dict(installed_commit="1" * 40,
        source_ref="main", source_remote="origin", source_dirty=False))
    monkeypatch.setattr(I, "source_snapshot", lambda *args: "2" * 64)
    monkeypatch.setattr(I, "_build_inputs", lambda: set())
    def build_root(stage, metadata):
        destination = stage / '.source'
        destination.mkdir()
        return destination
    monkeypatch.setattr(I, "_stage_source", build_root)
    return tmp_path / "releases", tmp_path / "bin", calls


def test_unrelated_command_preserved_before_install_mutates(installer):
    prefix, bin_dir, calls = installer
    bin_dir.mkdir()
    existing = bin_dir / "raven"
    existing.write_text("my existing program")
    with pytest.raises(I.InstallError, match="Existing command preserved"):
        I.install(prefix, bin_dir)
    assert existing.read_text() == "my existing program"
    assert not prefix.exists() and not calls


def test_pipx_link_is_preserved_even_when_broken(installer):
    prefix, bin_dir, _ = installer
    bin_dir.mkdir()
    existing = bin_dir / "ravenl"
    existing.symlink_to("/missing/pipx/venvs/raven/bin/ravenl")
    with pytest.raises(I.InstallError, match="original installer"):
        I.install(prefix, bin_dir)
    assert existing.is_symlink() and not prefix.exists()


def test_all_commands_follow_atomic_release_and_update_retains_previous(installer):
    prefix, bin_dir, calls = installer
    first = I.install(prefix, bin_dir)
    assert first["previous"] is None
    assert all((bin_dir / name).resolve() == first["release"] / "bin" / name for name in I.COMMANDS)
    assert len(calls) == 5
    second = I.install(prefix, bin_dir, terminal=False)
    assert second["previous"] == first["release"] and first["release"].is_dir()
    assert all((bin_dir / name).resolve() == second["release"] / "bin" / name for name in I.COMMANDS)
    assert calls[0][-1].endswith("[terminal]") and not calls[5][-1].endswith("[terminal]")
    assert json.loads((second["release"] / I.MARKER).read_text())["schema"] == "raven.user-install/v1"


def test_failed_validation_keeps_previous_release_and_removes_unpublished_stage(installer, monkeypatch):
    prefix, bin_dir, _ = installer
    first = I.install(prefix, bin_dir)
    before = set(prefix.iterdir())

    def fail(command, **kwargs):
        if str(command[0]).endswith("ravenh"):
            raise I.InstallError("broken Heavy launcher")

    monkeypatch.setattr(I, "run", fail)
    with pytest.raises(I.InstallError, match="broken Heavy"):
        I.install(prefix, bin_dir)
    assert set(prefix.iterdir()) == before
    assert (prefix / "current").resolve() == first["release"]
    assert (bin_dir / "ravenh").resolve() == first["release"] / "bin/ravenh"


def test_first_install_failure_publishes_no_launchers(installer, monkeypatch):
    prefix, bin_dir, _ = installer
    monkeypatch.setattr(I, "run", lambda *args, **kwargs: (_ for _ in ()).throw(I.InstallError("package unavailable")))
    with pytest.raises(I.InstallError):
        I.install(prefix, bin_dir)
    assert not list(bin_dir.iterdir())
    assert not (prefix / "current").exists()
    assert not list(prefix.glob("release-*"))


def test_install_plan_has_no_writes(installer):
    prefix, bin_dir, calls = installer
    assert I.main(["--plan", "--prefix", str(prefix), "--bin-dir", str(bin_dir)]) == 0
    assert not prefix.exists() and not bin_dir.exists() and not calls


def test_symlink_install_directory_is_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(I.InstallError, match="symbolic link"):
        I.install(link / "releases", tmp_path / "bin")


def test_update_marker_preserves_actual_destinations_preferences_and_prior_release(installer):
    prefix, bin_dir, _ = installer
    first = I.install(prefix, bin_dir)
    second = I.install(prefix, bin_dir, terminal=False)
    marker = I.read_marker(second["release"], prefix=prefix, bin_dir=bin_dir)
    assert marker == dict(schema="raven.user-install/v1", source=str(I.ROOT), prefix=str(prefix),
        bin_dir=str(bin_dir), terminal=False, installed_commit="1" * 40, source_ref="main",
        source_remote="origin", source_dirty=False, previous_release=str(first["release"]), source_snapshot_sha256="2" * 64)
    assert stat.S_IMODE((second["release"] / I.MARKER).stat().st_mode) == 0o600
    assert first["release"].is_dir()


@pytest.mark.parametrize("mask", [0, 0o777])
def test_new_marker_is_exactly_private_independent_of_umask(tmp_path, mask):
    previous = os.umask(mask)
    try:
        I._write_marker(tmp_path, dict(schema="raven.user-install/v1", source=str(I.ROOT)))
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tmp_path / I.MARKER).stat().st_mode) == 0o600


def test_legacy_integrity_safe_marker_updates_without_rewriting_previous_release(installer):
    prefix, bin_dir, _ = installer
    first = I.install(prefix, bin_dir)
    marker = first["release"] / I.MARKER
    legacy = json.dumps(dict(schema="raven.user-install/v1", source=str(I.ROOT)))
    marker.write_text(legacy); marker.chmod(0o644)
    second = I.install(prefix, bin_dir)
    assert second["previous"] == first["release"]
    assert marker.read_text() == legacy and stat.S_IMODE(marker.stat().st_mode) == 0o644
    assert I.read_marker(second["release"])["terminal"] is True


@pytest.mark.parametrize("change", ["group-writable", "new-public", "duplicate", "oversize", "deep",
    "not-object", "terminal-int", "terminal-null", "wrong-prefix", "wrong-bin", "previous-outside",
    "remote-url", "nonfinite", "symlink", "fifo"])
def test_malformed_or_untrusted_marker_never_replaces_existing_release(installer, change):
    prefix, bin_dir, calls = installer
    first = I.install(prefix, bin_dir)
    marker = first["release"] / I.MARKER
    data = json.loads(marker.read_text())
    if change == "group-writable": marker.chmod(0o660)
    elif change == "new-public": marker.chmod(0o644)
    elif change == "duplicate": marker.write_text('{"schema":"raven.user-install/v1","schema":"other"}')
    elif change == "oversize": marker.write_text(" " * (I.MARKER_LIMIT + 1))
    elif change == "deep": marker.write_text("[" * 1000 + "0" + "]" * 1000)
    elif change == "not-object": marker.write_text("[]")
    elif change in ("symlink", "fifo"):
        marker.unlink()
        if change == "symlink": marker.symlink_to(prefix / "foreign-marker")
        else: os.mkfifo(marker, 0o600)
    else:
        if change == "terminal-int": data["terminal"] = 1
        elif change == "terminal-null": data["terminal"] = None
        elif change == "wrong-prefix": data["prefix"] = str(prefix / "other")
        elif change == "wrong-bin": data["bin_dir"] = str(bin_dir / "other")
        elif change == "previous-outside": data["previous_release"] = str(prefix.parent / "release-other")
        elif change == "remote-url": data["source_remote"] = "https://user:secret@example.org/repo"
        elif change == "nonfinite": data["extension"] = float("inf")
        marker.write_text(json.dumps(data))
    before = set(prefix.iterdir()); count = len(calls)
    with pytest.raises((I.InstallError, OSError)):
        I.install(prefix, bin_dir)
    assert set(prefix.iterdir()) == before and len(calls) == count
    assert (prefix / "current").resolve() == first["release"]


@pytest.mark.parametrize("which", ["ancestor", "prefix", "bin"])
def test_world_writable_directories_are_refused_before_build(installer, which):
    prefix, bin_dir, calls = installer
    destination = prefix.parent if which == "ancestor" else prefix if which == "prefix" else bin_dir
    destination.mkdir(exist_ok=True); destination.chmod(0o777)
    with pytest.raises(I.InstallError, match="group/world writable"):
        I.install(prefix, bin_dir)
    assert not calls and not (prefix / "current").exists()


def test_competing_installer_does_not_build_or_change_current(installer):
    prefix, bin_dir, calls = installer
    first = I.install(prefix, bin_dir); count = len(calls)
    with (prefix / ".install.lock").open("rb") as lock:
        I.fcntl.flock(lock, I.fcntl.LOCK_EX | I.fcntl.LOCK_NB)
        with pytest.raises(I.InstallError, match="in progress"):
            I.install(prefix, bin_dir)
    assert len(calls) == count
    assert (prefix / "current").resolve() == first["release"]


def test_replaced_installer_lock_cannot_publish(installer, monkeypatch):
    prefix, bin_dir, _ = installer
    first = I.install(prefix, bin_dir)
    def replace_lock(command, **kwargs):
        if str(command[0]).endswith("ravenh"):
            lock = prefix / ".install.lock"
            lock.unlink(); lock.write_text("other lifetime"); lock.chmod(0o600)
    monkeypatch.setattr(I, "run", replace_lock)
    with pytest.raises(I.InstallError, match="lock changed"):
        I.install(prefix, bin_dir)
    assert list(prefix.glob("release-*")) == [first["release"]]
    assert (prefix / "current").resolve() == first["release"]


def test_changed_source_commit_during_build_cannot_publish(installer, monkeypatch):
    prefix, bin_dir, _ = installer
    first = I.install(prefix, bin_dir)
    versions = iter([dict(installed_commit=letter * 40, source_ref="main", source_remote="origin", source_dirty=False)
                     for letter in ("1", "2")])
    monkeypatch.setattr(I, "source_metadata", lambda: next(versions))
    with pytest.raises(I.InstallError, match="source checkout changed"):
        I.install(prefix, bin_dir)
    assert list(prefix.glob("release-*")) == [first["release"]]
    assert (prefix / "current").resolve() == first["release"]


def test_failed_marker_write_cleans_stage_and_preserves_previous(installer, monkeypatch):
    prefix, bin_dir, _ = installer
    first = I.install(prefix, bin_dir)
    monkeypatch.setattr(I, "_write_marker", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        I.install(prefix, bin_dir)
    assert list(prefix.glob("release-*")) == [first["release"]]
    assert (prefix / "current").resolve() == first["release"]


def test_failed_final_switch_cleans_only_new_links_and_stage(installer, monkeypatch):
    prefix, bin_dir, _ = installer
    monkeypatch.setattr(I.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("publish failed")))
    with pytest.raises(OSError, match="publish failed"):
        I.install(prefix, bin_dir)
    assert not list(bin_dir.iterdir()) and not list(prefix.glob("release-*"))
    assert not list(prefix.glob(".current-*"))


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_interrupt_preserves_previous_releases_lock_and_restores_signal_handler(installer, monkeypatch, signum):
    prefix, bin_dir, _ = installer
    first = I.install(prefix, bin_dir)
    previous_handlers = {value: signal.getsignal(value) for value in (signal.SIGTERM, signal.SIGINT)}
    def interrupt(command, **kwargs):
        os.kill(os.getpid(), signum)
    monkeypatch.setattr(I, "run", interrupt)
    assert I.main(["--prefix", str(prefix), "--bin-dir", str(bin_dir), "--no-setup"]) == 130
    assert all(signal.getsignal(value) == handler for value, handler in previous_handlers.items())
    assert (prefix / "current").resolve() == first["release"]
    assert list(prefix.glob("release-*")) == [first["release"]]
    monkeypatch.setattr(I, "run", lambda *args, **kwargs: None)
    assert I.install(prefix, bin_dir)["previous"] == first["release"]


def test_source_metadata_records_local_git_identity_without_remote_credentials(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], text=True, capture_output=True, check=True).stdout.strip()
    git("init", "-b", "main")
    git("-c", "user.name=Raven Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "fixture")
    git("remote", "add", "origin", "https://user:secret@example.invalid/raven")
    git("config", "branch.main.remote", "origin")
    monkeypatch.setattr(I, "ROOT", tmp_path)
    clean = I.source_metadata()
    assert clean == dict(installed_commit=git("rev-parse", "HEAD"), source_ref="main", source_remote="origin", source_dirty=False)
    assert "secret" not in json.dumps(clean)
    (tmp_path / "local-change").write_text("untracked source")
    assert I.source_metadata()["source_dirty"] is True
    git("checkout", "--detach")
    assert I.source_metadata()["source_ref"] is None


def test_source_without_git_records_unknown_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "ROOT", tmp_path)
    assert I.source_metadata() == dict(installed_commit=None, source_ref=None, source_remote=None, source_dirty=None)


def test_command_timeout_stops_owned_command_process(tmp_path):
    pid_path = tmp_path / "owned-pid"
    command = [sys.executable, "-c", "import os,pathlib,time; pathlib.Path(" + repr(str(pid_path)) + ").write_text(str(os.getpid())); time.sleep(60)"]
    with pytest.raises(I.InstallError, match="Could not run"):
        I.run(command, timeout=0.5)
    pid = int(pid_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_sigterm_reaps_actual_install_command_before_stage_cleanup(installer, tmp_path):
    prefix, bin_dir, _ = installer
    first = I.install(prefix, bin_dir)
    pid_path = tmp_path / "build-child.pid"
    driver = r'''
import importlib.util, pathlib, sys
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location('installer', sys.argv[1])
installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
def create(stage):
    (stage / 'bin').mkdir()
installer.venv.EnvBuilder = lambda **kwargs: SimpleNamespace(create=create)
installer.source_snapshot = lambda *args: '2' * 64
original_run = installer.run
child = 'import os,pathlib,time; pathlib.Path(' + repr(sys.argv[4]) + ').write_text(str(os.getpid())); time.sleep(60)'
installer.run = lambda command, **kwargs: original_run([sys.executable, '-c', child])
raise SystemExit(installer.main(['--prefix', sys.argv[2], '--bin-dir', sys.argv[3], '--no-setup']))
'''
    process = subprocess.Popen([sys.executable, "-c", driver, str(SCRIPT), str(prefix), str(bin_dir), str(pid_path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not pid_path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_path.exists(), "Owned installer command did not start"
        child_pid = int(pid_path.read_text())
        process.send_signal(signal.SIGTERM)
        _, error = process.communicate(timeout=5)
        assert process.returncode == 130 and "interrupted" in error
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert (prefix / "current").resolve() == first["release"]
        assert list(prefix.glob("release-*")) == [first["release"]]
        assert not list(prefix.glob(".current-*"))
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=5)


@pytest.mark.parametrize("expected", ["0" * 40, "HEAD", "not-a-commit"])
def test_commit_pin_mismatch_refuses_before_any_install_mutation(installer, expected):
    prefix, bin_dir, calls = installer
    with pytest.raises(I.InstallError, match="expected commit|expected-commit"):
        I.install(prefix, bin_dir, expected_commit=expected)
    assert not prefix.exists() and not bin_dir.exists() and not calls


def test_pinned_plan_checks_commit_without_writes(installer):
    prefix, bin_dir, calls = installer
    assert I.main(["--plan", "--prefix", str(prefix), "--bin-dir", str(bin_dir),
                   "--expected-commit", "0" * 40]) == 2
    assert not prefix.exists() and not bin_dir.exists() and not calls


def test_preserve_config_requires_pin_and_cannot_exempt_whole_checkout(installer):
    prefix, bin_dir, _ = installer
    with pytest.raises(I.InstallError, match="requires"):
        I.install(prefix, bin_dir, expected_protected_paths=[I.ROOT / "config.py"])
    with pytest.raises(I.InstallError, match="whole source checkout"):
        I.install(prefix, bin_dir, expected_commit="1" * 40, expected_protected_paths=[I.ROOT])


def test_pinned_update_keeps_literal_config_exceptions_and_truthful_dirty_metadata(installer, monkeypatch):
    prefix, bin_dir, _ = installer
    monkeypatch.setattr(I, "_git_paths", lambda *args: {Path("config.py"), Path(".raven/settings.json"), Path(".venv/local")})
    monkeypatch.setattr(I, "source_metadata", lambda: dict(installed_commit="1" * 40,
        source_ref="main", source_remote="origin", source_dirty=True))
    result = I.install(prefix, bin_dir, expected_commit="1" * 40,
                       expected_protected_paths=[I.ROOT / "config.py"])
    marker = I.read_marker(result["release"])
    assert marker["source_dirty"] is True and marker["installed_commit"] == "1" * 40


def test_protected_config_does_not_allow_neighboring_code_or_external_exemptions(installer, monkeypatch):
    prefix, bin_dir, calls = installer
    monkeypatch.setattr(I, "_git_paths", lambda *args: {Path("config.py.extra")})
    with pytest.raises(I.InstallError, match="outside preserved configuration"):
        I.install(prefix, bin_dir, expected_commit="1" * 40,
                  expected_protected_paths=[I.ROOT / "config.py", prefix.parent / "config.py.extra"])
    assert not calls and not prefix.exists()


def test_content_change_while_already_dirty_cannot_publish(installer, monkeypatch):
    prefix, bin_dir, _ = installer
    first = I.install(prefix, bin_dir)
    monkeypatch.setattr(I, "source_metadata", lambda: dict(installed_commit="1" * 40,
        source_ref="main", source_remote="origin", source_dirty=True))
    snapshots = iter(["2" * 64, "3" * 64])
    monkeypatch.setattr(I, "source_snapshot", lambda *args: next(snapshots))
    with pytest.raises(I.InstallError, match="source checkout changed"):
        I.install(prefix, bin_dir)
    assert list(prefix.glob("release-*")) == [first["release"]]
    assert (prefix / "current").resolve() == first["release"]


@pytest.mark.parametrize("existing", [False, True])
def test_interruption_after_atomic_switch_restores_previous_before_cleanup(installer, monkeypatch, existing):
    prefix, bin_dir, _ = installer
    first = I.install(prefix, bin_dir) if existing else None
    replace, interrupted = I.os.replace, [False]
    def interrupted_replace(source, destination):
        replace(source, destination)
        if not interrupted[0]:
            interrupted[0] = True
            os.kill(os.getpid(), signal.SIGTERM)
    monkeypatch.setattr(I.os, "replace", interrupted_replace)
    assert I.main(["--prefix", str(prefix), "--bin-dir", str(bin_dir), "--no-setup"]) == 130
    assert not list(prefix.glob(".current-*")) and not list(prefix.glob(".restore-*"))
    if first:
        assert (prefix / "current").resolve() == first["release"]
        assert list(prefix.glob("release-*")) == [first["release"]]
    else:
        assert not os.path.lexists(prefix / "current") and not list(bin_dir.iterdir())
        assert not list(prefix.glob("release-*"))


def test_source_snapshot_tracks_contents_untracked_files_and_explicit_ignored_config(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "ROOT", tmp_path)
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    git("init", "-b", "main")
    (tmp_path / ".gitignore").write_text(".raven/\n.venv/\nbuild/\n")
    code = tmp_path / "app.py"; code.write_text("old value")
    git("add", ".")
    git("-c", "user.name=Raven Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    initial = I.source_snapshot()
    code.write_text("new value")
    changed = I.source_snapshot()
    assert changed != initial
    code.write_text("third val")
    assert I.source_snapshot() != changed  # Both snapshots are already dirty.
    (tmp_path / "untracked.py").write_text("additional module")
    with_extra = I.source_snapshot()
    assert with_extra != changed
    (tmp_path / ".raven").mkdir()
    config = tmp_path / ".raven/config.json"; config.write_text('{"value":1}')
    assert I.source_snapshot() == with_extra  # Runtime stores are not build inputs.
    protected = I._protected_paths([config])
    with_config = I.source_snapshot(protected)
    config.write_text('{"value":2}')
    assert I.source_snapshot(protected) != with_config
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing-git"))
    assert I.source_metadata()["installed_commit"] is not None
    assert I.source_snapshot() == with_extra


def test_archive_snapshot_excludes_generated_outputs_and_enforces_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "ROOT", tmp_path)
    (tmp_path / "pyproject.toml").write_text("data")
    before = I.source_snapshot()
    (tmp_path / "build").mkdir(); (tmp_path / "build/generated").write_text("generated")
    assert I.source_snapshot() == before
    monkeypatch.setattr(I, "SOURCE_LIMIT", 3)
    with pytest.raises(I.InstallError, match="input bound"):
        I.source_snapshot()


def test_ignored_package_code_is_fingerprinted_and_requires_explicit_pin_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "ROOT", tmp_path)
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-b", "main")
    (tmp_path / "raven").mkdir()
    (tmp_path / "raven/__init__.py").write_text("# package\n")
    git("add", ".")
    git("-c", "user.name=Raven Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    commit = git("rev-parse", "HEAD")
    before = I.source_snapshot()
    (tmp_path / ".git/info/exclude").write_text("raven/hidden.py\nsetup.py\n")
    hidden = tmp_path / "raven/hidden.py"; hidden.write_text("# untracked code\n")
    assert git("status", "--porcelain") == ""
    metadata = I.source_metadata()
    assert metadata["source_dirty"] is True and I.source_snapshot() != before
    with pytest.raises(I.InstallError, match="outside preserved configuration"):
        I._check_pin(commit, metadata, I._protected_paths([]))
    I._check_pin(commit, metadata, I._protected_paths([hidden]))
    changed = I.source_snapshot()
    hidden.write_text("# modified while already dirty\n")
    assert I.source_snapshot() != changed
    (tmp_path / "setup.py").write_text("# additional ignored executable build input\n")
    with pytest.raises(I.InstallError, match="outside preserved configuration"):
        I._check_pin(commit, I.source_metadata(), I._protected_paths([hidden]))
