"""Installer ownership and maintenance dispatch stay independent of Slurm."""

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven import raven_launch as L
from raven import update as U


@pytest.fixture
def managed(tmp_path, monkeypatch):
    prefix = tmp_path / "releases"
    prefix.mkdir()
    release = prefix / "release-first"
    (release / "bin").mkdir(parents=True)
    bin_dir = tmp_path / "commands"
    bin_dir.mkdir()
    for name in U.COMMANDS:
        (release / "bin" / name).write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / name).symlink_to(prefix / "current/bin" / name)
    (prefix / "current").symlink_to(release.name)
    source = tmp_path / "source"
    source.mkdir()
    marker = {
        "schema": "raven.user-install/v1",
        "source": str(source),
        "prefix": str(prefix),
        "bin_dir": str(bin_dir),
        "terminal": False,
        "installed_commit": "a" * 40,
        "source_dirty": False,
    }
    (release / U.MARKER).write_text(json.dumps(marker))
    monkeypatch.setattr(U.sys, "prefix", str(release))
    monkeypatch.setattr(U.sys, "argv", [str(bin_dir / "raven")])
    return U.discover(), marker


def test_managed_discovery_remembers_paths_extras_and_source(managed):
    installation, marker = managed
    assert installation.kind == "managed"
    assert installation.source == Path(marker["source"])
    assert not installation.terminal
    assert installation.record()["release"] == str(installation.release)


def test_discovery_preserves_and_rejects_foreign_command(managed):
    installation, _ = managed
    command = installation.bin_dir / "ravenh"
    command.unlink()
    command.write_text("another owner")
    with pytest.raises(U.UpdateError, match="ownership changed"):
        U.discover()
    assert command.read_text() == "another owner"


def test_inactive_release_cannot_update(managed):
    installation, _ = managed
    (installation.prefix / "current").unlink()
    (installation.prefix / "current").symlink_to("release-someone-else")
    with pytest.raises(U.UpdateError, match="inactive"):
        U.discover()


def test_marker_symlink_and_duplicate_fields_rejected(managed):
    installation, _ = managed
    marker = installation.release / U.MARKER
    original = marker.read_text()
    marker.write_text('{"schema":"raven.user-install/v1","schema":"evil"}')
    with pytest.raises(ValueError, match="metadata"):
        U.discover()
    marker.unlink()
    other = installation.release / "other.json"
    other.write_text(original)
    marker.symlink_to(other)
    with pytest.raises((ValueError, OSError)):
        U.discover()


def test_legacy_marker_infers_owned_launcher_and_terminal_once(managed, monkeypatch):
    installation, marker = managed
    (installation.release / U.MARKER).write_text(
        json.dumps({key: marker[key] for key in ("schema", "source")})
    )
    monkeypatch.setattr(U, "_terminal_installed", lambda: True)
    detected = U.discover()
    assert detected.bin_dir == installation.bin_dir and detected.terminal
    assert detected.installed_commit == ""


def test_ambiguous_legacy_options_do_not_install(managed, monkeypatch):
    installation, marker = managed
    (installation.release / U.MARKER).write_text(
        json.dumps({key: marker[key] for key in ("schema", "source")})
    )

    def package(name):
        if name == "wcwidth":
            raise U.metadata.PackageNotFoundError(name)
        return object()

    monkeypatch.setattr(U.metadata, "distribution", package)
    with pytest.raises(U.UpdateError, match="ambiguous"):
        U.discover()


def test_update_plan_never_locks_installs_or_reads_configuration(
    managed, monkeypatch, capsys
):
    installation, _ = managed
    calls = []

    def source(path, **kwargs):
        calls.append((path, kwargs))
        return {"branch": "main", "status": "planned", "target_head": "b" * 40}

    monkeypatch.setitem(
        sys.modules, "raven.update_source", SimpleNamespace(update_source=source)
    )
    monkeypatch.setattr(
        U, "install_updated", lambda *args: pytest.fail("installed during plan")
    )
    monkeypatch.setattr(U, "_lock", lambda *args: pytest.fail("locked during plan"))
    assert (
        U.cli(
            ["update", "--plan", "--config", str(installation.source / "custom.json")]
        )
        == 0
    )
    assert calls[0][1]["mode"] == "plan"
    assert installation.source / "custom.json" in calls[0][1]["protected_paths"]
    assert "no network" in capsys.readouterr().out


def test_update_installs_advanced_source_and_keeps_core_only(managed, monkeypatch):
    installation, _ = managed
    calls = []

    def source(path, **kwargs):
        assert kwargs["overwrite"] and kwargs["backup_metadata"]["terminal"] is False
        return {
            "branch": "main",
            "changed": True,
            "target_head": "b" * 40,
            "backup": "/saved/backup",
        }

    monkeypatch.setitem(
        sys.modules, "raven.update_source", SimpleNamespace(update_source=source)
    )
    monkeypatch.setattr(
        U, "install_updated", lambda *args, **kwargs: calls.append(args)
    )
    assert U.cli(["update", "--overwrite"]) == 0
    assert calls == [(installation, installation.source, "b" * 40)]


def test_already_current_skips_build_unless_reinstall(managed, monkeypatch):
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "raven.update_source",
        SimpleNamespace(
            update_source=lambda *a, **k: {
                "branch": "main",
                "changed": False,
                "target_head": "a" * 40,
            }
        ),
    )
    monkeypatch.setattr(
        U, "install_updated", lambda *args, **kwargs: calls.append(args)
    )
    assert U.cli(["update"]) == 0 and not calls
    assert U.cli(["update", "--reinstall"]) == 0 and len(calls) == 1


def test_failed_build_reports_backup_and_preserves_active_release(
    managed, monkeypatch, capsys
):
    installation, _ = managed
    monkeypatch.setitem(
        sys.modules,
        "raven.update_source",
        SimpleNamespace(
            update_source=lambda *a, **k: {
                "branch": "main",
                "changed": True,
                "target_head": "b" * 40,
                "backup": "/saved/backup",
            }
        ),
    )
    monkeypatch.setattr(
        U,
        "install_updated",
        lambda *args, **kwargs: (_ for _ in ()).throw(U.UpdateError("build failed")),
    )
    assert U.cli(["update", "--overwrite"]) == 2
    out = capsys.readouterr()
    assert "raven revert /saved/backup" in out.out
    assert "previous managed release active" in out.err
    assert (installation.prefix / "current").resolve() == installation.release


def test_managed_install_command_preserves_options(managed, monkeypatch):
    installation, _ = managed
    (installation.source / "scripts").mkdir()
    (installation.source / "scripts/install.py").write_text("# fixture\n")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        new = installation.prefix / "release-second"
        new.mkdir()
        (new / U.MARKER).write_text(
            json.dumps(
                {
                    "schema": "raven.user-install/v1",
                    "source": str(installation.source),
                    "prefix": str(installation.prefix),
                    "bin_dir": str(installation.bin_dir),
                    "terminal": False,
                    "installed_commit": "b" * 40,
                }
            )
        )
        (installation.prefix / "current").unlink()
        (installation.prefix / "current").symlink_to(new.name)

    monkeypatch.setattr(U, "_run", run)
    U.install_updated(installation, installation.source, "b" * 40)
    assert "--without-terminal" in commands[0] and "--no-setup" in commands[0]
    assert commands[0][commands[0].index("--prefix") + 1] == str(installation.prefix)
    assert commands[0][commands[0].index("--bin-dir") + 1] == str(installation.bin_dir)


def test_revert_validates_installation_before_source_mutation(managed, monkeypatch):
    calls = []

    def revert(*a, **k):
        calls.append(k["mode"])
        return {"installation": {"kind": "pipx"}}

    monkeypatch.setitem(
        sys.modules, "raven.update_revert", SimpleNamespace(revert_source=revert)
    )
    assert U.cli(["revert", "/saved/backup"]) == 2
    assert calls == ["plan"]


def test_revert_atomically_selects_validated_retained_release(managed):
    installation, marker = managed
    old = installation.prefix / "release-old"
    (old / "bin").mkdir(parents=True)
    old_marker = dict(marker, installed_commit="9" * 40)
    (old / U.MARKER).write_text(json.dumps(old_marker))
    for name in U.COMMANDS:
        (old / "bin" / name).write_text("#!/bin/sh\nexit 0\n")
        (old / "bin" / name).chmod(0o700)
    (old / "bin/python").symlink_to(sys.executable)
    saved = dict(installation.record(), release=str(old), installed_commit="9" * 40)
    U.activate_retained(installation, saved)
    assert all(
        (installation.bin_dir / name).resolve() == old / "bin" / name
        for name in U.COMMANDS
    )
    assert installation.release.is_dir()


@pytest.mark.parametrize("entry", [L.light_main, L.heavy_main])
@pytest.mark.parametrize("command", ["update", "revert"])
def test_maintenance_dispatch_never_loads_config_or_allocates(
    entry, command, monkeypatch, tmp_path
):
    from raven import launch, raven_config

    monkeypatch.chdir(tmp_path)
    (tmp_path / command).write_text("#!/bin/sh\nexit 1\n")
    monkeypatch.setattr(
        launch, "main", lambda *a, **k: pytest.fail("launched a colliding script")
    )
    monkeypatch.setattr(
        raven_config,
        "resolve",
        lambda *a, **k: pytest.fail("loaded launch configuration"),
    )
    monkeypatch.setattr(
        L, "resolve", lambda *a, **k: pytest.fail("loaded scheduler configuration")
    )
    monkeypatch.setattr(
        L, "_allocation", lambda *a, **k: pytest.fail("requested allocation")
    )
    calls = []
    monkeypatch.setattr(U, "cli", lambda args: calls.append(args) or 17)
    assert entry([command, "--plan"]) == 17
    assert calls == [[command, "--plan"]]


@pytest.mark.parametrize("entry", [L.light_main, L.heavy_main])
def test_update_help_available_without_installation_or_slurm(
    entry, monkeypatch, capsys
):
    monkeypatch.setattr(
        U, "discover", lambda *a: pytest.fail("discovered install during help")
    )
    with pytest.raises(SystemExit) as stopped:
        entry(["update", "--help"])
    assert stopped.value.code == 0
    assert "--overwrite" in capsys.readouterr().out


def test_nested_config_flag_preserves_extra_file_without_loading_it(monkeypatch):
    calls = []
    monkeypatch.setattr(U, "cli", lambda args: calls.append(args) or 0)
    assert L.heavy_main(["--config", "broken.json", "update", "--plan"]) == 0
    assert calls == [["update", "--plan", "--config", "broken.json"]]


def test_update_rejects_scheduler_arguments(monkeypatch):
    monkeypatch.setattr(U, "cli", lambda args: pytest.fail("maintenance dispatched"))
    assert L.heavy_main(["--account", "lab", "update"]) == 2


def test_update_lock_is_exclusive_and_private(managed):
    installation, _ = managed
    with (
        U._lock(installation.control_directory, ".update.lock"),
        pytest.raises(U.UpdateError, match="in progress"),
        U._lock(installation.control_directory, ".update.lock"),
    ):
        pytest.fail("second writer")
    assert (
        installation.control_directory / ".update.lock"
    ).stat().st_mode & 0o777 == 0o600


@pytest.fixture
def pipx_install(tmp_path, monkeypatch):
    release = tmp_path / "pipx/venvs/raven"
    (release / "bin").mkdir(parents=True)
    source = tmp_path / "source with spaces"
    source.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in U.COMMANDS:
        (release / "bin" / name).write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / name).symlink_to(release / "bin" / name)
    spec = str(source) + "[terminal]"
    (release / "pipx_metadata.json").write_text(
        json.dumps({"main_package": {"package": "raven", "package_or_url": spec}})
    )
    monkeypatch.setattr(U.sys, "prefix", str(release))
    monkeypatch.setattr(U.sys, "argv", [str(bin_dir / "raven")])
    monkeypatch.setattr(
        U.shutil, "which", lambda name: "/tools/pipx" if name == "pipx" else None
    )
    return U.discover()


def test_pipx_local_source_and_extras_remain_owned(pipx_install, monkeypatch):
    installation = pipx_install
    assert installation.kind == "pipx" and installation.terminal
    assert (
        installation.control_directory
        == installation.prefix.parent.parent / "raven-updates"
    )
    calls = []
    monkeypatch.setattr(
        U, "_run", lambda command, **kwargs: calls.append((command, kwargs))
    )
    monkeypatch.setitem(
        sys.modules,
        "raven.update_source",
        SimpleNamespace(
            content_fingerprint=lambda *args, **kwargs: "same source snapshot",
            git=lambda source, *args: (
                ("b" * 40 + "\n").encode() if args[0] == "rev-parse" else b""
            ),
        ),
    )
    U.install_updated(installation, installation.source, "b" * 40)
    assert calls[0][0] == ["/tools/pipx", "reinstall", "raven"]
    assert calls[0][1]["env"]["PIPX_HOME"] == str(installation.prefix.parent.parent)
    assert calls[0][1]["env"]["PIPX_BIN_DIR"] == str(installation.bin_dir)
    assert U.discover().installed_commit == "b" * 40
    assert U.discover().source_dirty is False


def test_pipx_moved_source_is_not_silently_adopted(pipx_install, tmp_path):
    with pytest.raises(U.UpdateError, match="original source path"):
        U.discover(tmp_path / "another")


@pytest.mark.parametrize(
    "spec",
    [
        "raven",
        "https://example.invalid/raven.whl",
        "git+https://example.invalid/repo.git",
    ],
)
def test_pipx_nonlocal_sources_are_not_reinterpreted(pipx_install, spec):
    (pipx_install.prefix / "pipx_metadata.json").write_text(
        json.dumps({"main_package": {"package": "raven", "package_or_url": spec}})
    )
    with pytest.raises(U.UpdateError, match="absolute path"):
        U.discover()


def test_pipx_suffixed_environment_cannot_update_different_raven(
    pipx_install, monkeypatch
):
    old = pipx_install.prefix
    renamed = old.with_name("raven-preview")
    old.rename(renamed)
    monkeypatch.setattr(U.sys, "prefix", str(renamed))
    with pytest.raises(U.UpdateError, match="does not identify"):
        U.discover()


def test_dirty_installed_source_is_rebuilt_even_with_same_git_head(
    managed, monkeypatch
):
    installation, marker = managed
    marker["source_dirty"] = True
    (installation.release / U.MARKER).write_text(json.dumps(marker))
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "raven.update_source",
        SimpleNamespace(
            update_source=lambda *a, **k: {
                "branch": "main",
                "changed": False,
                "target_head": "a" * 40,
            }
        ),
    )
    monkeypatch.setattr(U, "install_updated", lambda *a, **k: calls.append(a))
    assert U.cli(["update"]) == 0
    assert len(calls) == 1


def test_unusable_retained_release_is_rejected_before_activation(managed):
    installation, _ = managed
    (installation.release / "bin/python").symlink_to(sys.executable)
    with pytest.raises(U.UpdateError, match="not executable"):
        U._revert_installation_plan(installation, installation.record())
    assert (installation.prefix / "current").resolve() == installation.release


def test_revert_prints_new_safety_backup_and_pipx_original_revision(
    pipx_install, monkeypatch, capsys
):
    calls = []

    def revert(*args, **kwargs):
        return {
            "installation": pipx_install.record(),
            "original_head": "a" * 40,
            "recovery_backup": "/saved/safety" if kwargs["mode"] == "revert" else None,
        }

    monkeypatch.setitem(
        sys.modules, "raven.update_revert", SimpleNamespace(revert_source=revert)
    )
    monkeypatch.setattr(
        U, "install_updated", lambda *args, **kwargs: calls.append(args)
    )
    assert U.cli(["revert", "/saved/original"]) == 0
    assert calls[0][2] == "a" * 40
    assert "raven revert /saved/safety" in capsys.readouterr().out


def test_interrupted_revert_prints_completed_safety_backup(
    managed, monkeypatch, capsys
):
    installation, _ = managed
    monkeypatch.setattr(
        U, "_revert_installation_plan", lambda *args: installation.release
    )

    def revert(*args, **kwargs):
        if kwargs["mode"] == "plan":
            return {"installation": installation.record()}
        error = U.MaintenanceInterrupted("cancelled")
        error.backup = "/saved/safety"
        raise error

    monkeypatch.setitem(
        sys.modules, "raven.update_revert", SimpleNamespace(revert_source=revert)
    )
    assert U.cli(["revert", "/saved/original"]) == 130
    assert "raven revert /saved/safety" in capsys.readouterr().out


def test_owned_installer_timeout_reaps_group_without_touching_unrelated_process(
    tmp_path,
):
    import os
    import subprocess
    import time

    if not hasattr(os, "waitid"):
        pytest.skip("WNOWAIT process ownership qualification requires waitid")
    pid_file = tmp_path / "helper.pid"
    child = (
        "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path("
        + repr(str(pid_file))
        + ").write_text(str(os.getpid())); time.sleep(20)"
    )
    leader = (
        "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
        + repr(child)
        + "]); time.sleep(20)"
    )
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            U._run([sys.executable, "-c", leader], cwd=tmp_path, timeout=0.6)
        assert unrelated.poll() is None
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                state = (
                    Path("/proc", str(pid), "stat")
                    .read_text()
                    .rsplit(")", 1)[1]
                    .split()[0]
                )
            except FileNotFoundError:
                break
            if state == "Z":
                break
            time.sleep(0.02)
        else:
            pytest.fail("owned TERM-ignoring helper remained live")
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=3)


def test_pipx_source_change_is_not_recorded_as_a_verified_update(
    pipx_install, monkeypatch
):
    fingerprints = iter(("before", "changed during build"))
    monkeypatch.setitem(
        sys.modules,
        "raven.update_source",
        SimpleNamespace(
            content_fingerprint=lambda *args, **kwargs: next(fingerprints),
            git=lambda *args: ("b" * 40 + "\n").encode(),
        ),
    )
    monkeypatch.setattr(U, "_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        U,
        "_write_receipt",
        lambda *args, **kwargs: pytest.fail("recorded an unverified install"),
    )
    with pytest.raises(U.UpdateError, match="Source changed"):
        U.install_updated(pipx_install, pipx_install.source, "b" * 40)


@pytest.mark.parametrize("kind", ["managed", "pipx"])
@pytest.mark.parametrize("field", ["prefix", "bin_dir"])
@pytest.mark.parametrize(
    "command", [["update", "--overwrite"], ["revert", "/saved/backup"]]
)
def test_source_contained_installation_paths_stop_before_any_mutation(
    managed, monkeypatch, capsys, kind, field, command
):
    installation, _ = managed
    # Include '..' to ensure lexical spelling cannot bypass containment.
    unsafe = installation.source / "nested/../owned-commands"
    altered = replace(installation, kind=kind, **{field: unsafe})
    monkeypatch.setattr(U, "discover", lambda *a: altered)
    monkeypatch.setattr(U, "_lock", lambda *a: pytest.fail("created maintenance lock"))
    monkeypatch.setattr(U, "_update", lambda *a: pytest.fail("changed source"))
    monkeypatch.setattr(U, "_revert", lambda *a: pytest.fail("restored source"))
    assert U.cli(command) == 2
    assert "outside the checkout" in capsys.readouterr().err
    assert all((installation.bin_dir / name).is_symlink() for name in U.COMMANDS)
    assert (installation.prefix / "current").resolve() == installation.release
