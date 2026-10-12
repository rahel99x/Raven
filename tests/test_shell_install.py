"""Exercise actual Bash expansion and preservation of personal configuration."""
import importlib.util
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("raven_install_shell", ROOT / "scripts/install_shell.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def shell_env(**values):
    """Do not let an inherited user profile/account affect alias tests."""
    env = dict(os.environ)
    for key in ("RAVEN_CONFIG", "RAVEN_PROFILE", "RAVEN_ACCOUNT", "CARC_ACCOUNT"):
        env.pop(key, None)
    env.update(values)
    return env


def recording_checkout(tmp_path):
    root = tmp_path / "checkout with 'quotes'"
    (root / "scripts").mkdir(parents=True)
    launcher = root / "scripts/raven"
    launcher.write_text('#!/bin/sh\nprintf "%s\\0" "$@"\n')
    launcher.chmod(0o755)
    return root


def run_alias(block, env, *args):
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", '''shopt -s expand_aliases
source "$1"
shift
raven "$@"
''', "bash", str(block), *args], capture_output=True, text=True, env=env, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split("\0")[:-1]


def test_alias_preserves_argument_boundaries_and_reads_current_account(tmp_path):
    root = recording_checkout(tmp_path)
    block = tmp_path / "shell block"
    block.write_text(installer.shell_block(root))
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", '''shopt -s expand_aliases
raven() { return 99; }
dash2() { return 99; }
alias raven='false'
alias dash='false'
source "$1"
alias dash >/dev/null 2>&1 && exit 90
declare -F dash2 >/dev/null && exit 91
CARC_ACCOUNT="current account"
raven --once --tab history "argument with spaces"
''', "bash", str(block)], capture_output=True, text=True,
        env=shell_env(RAVEN_CONFIG="", RAVEN_PROFILE=""),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\0")[:-1] == [
        "--config", str(root / "docs/config.example.json"), "--profile", "carc", "--unicode",
        "--account", "current account", "--once", "--tab", "history", "argument with spaces",
    ]


def test_real_alias_uses_the_project_launcher_from_another_directory(tmp_path):
    block = tmp_path / "alias.bash"
    block.write_text(installer.shell_block(ROOT))
    result = subprocess.run(["bash", "--noprofile", "--norc", "-c", '''shopt -s expand_aliases
source "$1"
raven --fake --doctor
''', "bash", str(block)], cwd=tmp_path, capture_output=True, text=True,
        env=shell_env(CARC_ACCOUNT="", RAVEN_CONFIG="", RAVEN_PROFILE=""), timeout=20)
    assert result.returncode == 0, result.stderr
    assert "Prerequisites ready" in result.stdout


def test_update_backs_up_and_preserves_unrelated_settings_and_is_repeatable(tmp_path):
    path = tmp_path / ".bashrc"
    original = b"export KEEP='unchanged'\nalias dash='old-dashboard'\nalias raven='old-raven'\nalias other='echo keep'\n"
    path.write_bytes(original)
    path.chmod(0o640)
    assert installer.main(["--bashrc", str(path), "--apply"]) == 0
    result = path.read_text()
    assert result.startswith(original.decode())
    backups = list(tmp_path.glob(".bashrc.raven-*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert path.stat().st_mode & 0o777 == 0o640
    assert installer.main(["--bashrc", str(path), "--apply"]) == 0
    assert path.read_text() == result and list(tmp_path.glob(".bashrc.raven-*.bak")) == backups


def test_preview_never_replaces_original_or_existing_output(tmp_path):
    path = tmp_path / ".bashrc"
    path.write_text("export KEEP=yes\n")
    preview = tmp_path / "preview"
    assert installer.main(["--bashrc", str(path), "--output", str(preview)]) == 0
    assert path.read_text() == "export KEEP=yes\n"
    assert preview.stat().st_mode & 0o777 == 0o600
    assert installer.main(["--bashrc", str(path), "--output", str(preview)]) == 1


def test_invalid_shell_and_malformed_managed_blocks_leave_config_untouched(tmp_path):
    path = tmp_path / ".bashrc"
    for original in ["if then\n", installer.BEGIN + "\nunfinished\n"]:
        path.write_text(original)
        assert installer.main(["--bashrc", str(path), "--apply"]) == 1
        assert path.read_text() == original
        assert not list(tmp_path.glob("*.bak"))


def test_apply_preserves_a_bashrc_symlink(tmp_path):
    target = tmp_path / "dotfiles/bashrc"
    target.parent.mkdir()
    target.write_text("export KEEP=yes\n")
    link = tmp_path / ".bashrc"
    link.symlink_to(target)
    assert installer.main(["--bashrc", str(link), "--apply"]) == 0
    assert link.is_symlink() and installer.BEGIN in target.read_text()



@pytest.mark.parametrize("default_profile,values,profile,account", [
    ("desktop", {"CARC_ACCOUNT": "old cluster account"}, "desktop", None),
    ("carc", {"CARC_ACCOUNT": "legacy account"}, "carc", "legacy account"),
    ("carc", {"RAVEN_PROFILE": "desktop", "CARC_ACCOUNT": "old cluster account"}, "desktop", None),
    ("desktop", {"RAVEN_PROFILE": "carc", "CARC_ACCOUNT": "legacy account"}, "carc", "legacy account"),
    ("desktop", {"RAVEN_PROFILE": "another cluster", "CARC_ACCOUNT": "old cluster account"}, "another cluster", None),
    ("desktop", {"RAVEN_ACCOUNT": "local account", "CARC_ACCOUNT": "old cluster account"}, "desktop", "local account"),
    ("carc", {"RAVEN_ACCOUNT": "new account", "CARC_ACCOUNT": "legacy account"}, "carc", "new account"),
    ("carc", {"RAVEN_ACCOUNT": "", "CARC_ACCOUNT": "legacy account"}, "carc", None),
    ("desktop", {"RAVEN_PROFILE": "", "CARC_ACCOUNT": "old cluster account"}, "desktop", None),
])
def test_profile_and_account_precedence_in_actual_bash(tmp_path, default_profile, values, profile, account):
    root = recording_checkout(tmp_path)
    block = tmp_path / "alias.bash"
    block.write_text(installer.shell_block(root, default_profile))
    expected = ["--config", str(root / "docs/config.example.json"), "--profile", profile, "--unicode"]
    if account is not None:
        expected += ["--account", account]
    assert run_alias(block, shell_env(**values), "--once") == expected + ["--once"]


def test_quoted_profile_default_and_overrides_cannot_execute_shell_text(tmp_path):
    root = recording_checkout(tmp_path)
    marker = tmp_path / "must not be created"
    profile = "desktop's $(touch '" + str(marker) + "') \x60echo unsafe\x60\nnext line"
    block = tmp_path / "alias.bash"
    block.write_text(installer.shell_block(root, profile))
    installer.validate(block.read_text())
    config = str(tmp_path / "custom config with 'quotes' 界.json")
    account = "account $(touch '" + str(marker) + "') with spaces"
    args = ("argument with spaces", "", "a\nb", "literal * ; $(touch) 'quotes' 界")
    expected = ["--config", config, "--unicode", "--account", account, *args]
    assert run_alias(block, shell_env(RAVEN_CONFIG=config, RAVEN_ACCOUNT=account), *args) == expected
    override = "override's $(touch '" + str(marker) + "')"
    expected[2:2] = ["--site", override]
    assert run_alias(block, shell_env(RAVEN_CONFIG=config, RAVEN_ACCOUNT=account, RAVEN_PROFILE=override), *args) == expected
    assert not marker.exists()


def test_account_and_profile_overrides_are_read_at_each_invocation(tmp_path):
    root = recording_checkout(tmp_path)
    block = tmp_path / "alias.bash"
    block.write_text(installer.shell_block(root, "desktop"))
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", '''shopt -s expand_aliases
source "$1"
CARC_ACCOUNT="legacy account"
raven first
RAVEN_PROFILE=carc
raven second
RAVEN_ACCOUNT="new account"
raven third
RAVEN_PROFILE=desktop
RAVEN_ACCOUNT=
raven fourth
''', "bash", str(block)], capture_output=True, text=True, env=shell_env(), timeout=10,
    )
    assert result.returncode == 0, result.stderr
    base = ["--config", str(root / "docs/config.example.json"), "--profile"]
    assert result.stdout.split("\0")[:-1] == (
        base + ["desktop", "--unicode", "first"]
        + base + ["carc", "--unicode", "--account", "legacy account", "second"]
        + base + ["carc", "--unicode", "--account", "new account", "third"]
        + base + ["desktop", "--unicode", "fourth"]
    )


def test_desktop_cli_install_replaces_legacy_block_and_is_repeatable(tmp_path, monkeypatch):
    root = recording_checkout(tmp_path)
    monkeypatch.setattr(installer, "ROOT", root)
    path = tmp_path / ".bashrc"
    original = "export KEEP='unchanged'\n\n" + installer.shell_block(root) + "\n"
    path.write_text(original)
    path.chmod(0o640)
    assert installer.main(["--bashrc", str(path), "--profile", "desktop", "--apply"]) == 0
    updated = path.read_text()
    assert updated.startswith("export KEEP='unchanged'\n")
    assert updated.count(installer.BEGIN) == updated.count(installer.END) == 1
    backups = list(tmp_path.glob(".bashrc.raven-*.bak"))
    assert len(backups) == 1 and backups[0].read_text() == original
    assert path.stat().st_mode & 0o777 == 0o640
    assert run_alias(path, shell_env(CARC_ACCOUNT="must not leak"), "--once") == [
        "--config", str(root / "docs/config.example.json"), "--profile", "desktop", "--unicode", "--once",
    ]
    assert installer.main(["--bashrc", str(path), "--profile", "desktop", "--apply"]) == 0
    assert path.read_text() == updated and list(tmp_path.glob(".bashrc.raven-*.bak")) == backups


def test_cli_prints_selected_profile_without_modifying_configuration(tmp_path, monkeypatch, capsys):
    root = recording_checkout(tmp_path)
    monkeypatch.setattr(installer, "ROOT", root)
    path = tmp_path / ".bashrc"
    path.write_text("export KEEP=yes\n")
    assert installer.main(["--bashrc", str(path), "--profile", "desktop"]) == 0
    block = tmp_path / "printed.bash"
    block.write_text(capsys.readouterr().out)
    assert run_alias(block, shell_env(CARC_ACCOUNT="must not leak"))[3] == "desktop"
    assert path.read_text() == "export KEEP=yes\n"
    assert not list(tmp_path.glob("*.bak"))


def test_new_config_has_precedence_without_implicit_legacy_profile_or_account(tmp_path):
    root = recording_checkout(tmp_path)
    block = tmp_path / "alias.bash"
    block.write_text(installer.shell_block(root))
    env = shell_env(RAVEN_CONFIG="/new/raven.json", CARC_ACCOUNT="old-account")
    assert run_alias(block, env, "--once") == ["--config", "/new/raven.json", "--unicode", "--once"]
    env["RAVEN_PROFILE"] = "lab"
    assert run_alias(block, env) == ["--config", "/new/raven.json", "--site", "lab", "--unicode"]
