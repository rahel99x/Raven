"""The user installer publishes checked releases and preserves other commands."""
import importlib.util
import json
from pathlib import Path
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
