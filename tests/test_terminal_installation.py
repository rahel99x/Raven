"""Optional terminal packaging and application-colour ownership contracts."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from raven import palette
from raven import terminal_diagnostics as D


@pytest.mark.parametrize("theme", palette.THEME_NAMES)
def test_child_explicit_rgb_is_not_a_raven_theme_alias(theme):
    styled = palette.cell_style("terminal+fg:#22d3ee+bg:#164e63+bold", theme)
    result = palette.resolve(styled, theme)
    if theme in ("mono", "reader"):
        assert result.foreground is None and result.background is None
    else:
        assert result.foreground == (34, 211, 238)
        assert result.background == (22, 78, 99)
    assert "bold" in result.flags


@pytest.mark.parametrize("theme", palette.THEME_NAMES)
def test_child_default_cells_inherit_the_raven_theme(theme):
    assert palette.resolve(palette.cell_style("terminal", theme), theme) == palette.resolve(
        palette.cell_style("", theme), theme)


def test_terminal_marker_cannot_inject_escape_sequences(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    for style in ("terminal+fg:#22d3ee", "terminal+fg:\x1b[2J", "terminal+bg:#ffffff\x07"):
        codes = palette.ansi_codes(style, color_depth=24)
        assert all(char in "0123456789;" for char in codes)


def test_setup_terminal_is_explicit_and_offline_default_remains():
    source = Path(__file__).resolve().parents[1] / "scripts" / "setup.py"
    spec = importlib.util.spec_from_file_location("raven_terminal_setup_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parser().parse_args([]).terminal is False
    assert module.parser().parse_args(["--terminal"]).terminal is True


def test_core_import_without_site_packages_does_not_require_terminal_engine():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-S", "-c",
                             "from raven.config import Config; from raven.controller import App; "
                             "from raven.model import Store; App(Store(persist=False), None, None, Config(), 'test')"],
                            cwd=root, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_local_capability_probe_runs_real_controlling_pty_without_slurm():
    pytest.importorskip("pyte")
    from raven.terminal_diagnostics import run_probe
    result = run_probe()
    assert result["status"] == "passed", result
    assert result["scheduler_contacted"] is False
    assert {check["name"] for check in result["checks"]} == {
        "alternate-buffer", "tty", "foreground", "input", "resize", "output-and-exit"}


def test_selftest_outside_checkout_keeps_source_launcher_support(tmp_path):
    pytest.importorskip("pyte")
    import json
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "raven" / "terminal_diagnostics.py"), "--json"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)["status"] == "passed"


@pytest.fixture
def profile_probe():
    return {"status": "passed", "checks": [{"name": name, "passed": True} for name in sorted(D.CHECK_NAMES)],
            "scheduler_contacted": False}


def test_private_profile_roundtrip_distinguishes_tools_from_tested_features(tmp_path, profile_probe, monkeypatch):
    monkeypatch.setenv("RAVEN_PROFILE_SECRET", "never-capture-this-value")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(D.shutil, "which", lambda name: "/private/path/" + name)
    path = tmp_path / "profile.json"
    value = D.save_profile(path, profile_probe)
    assert path.stat().st_mode & 0o777 == 0o600
    assert D.read_profile(path) == value
    assert set(value["tested_features"]) == D.CHECK_NAMES
    assert all(item == {"available": True, "tested": False} for item in value["tools"].values())
    raw = path.read_text()
    assert "never-capture-this-value" not in raw and "/private/path" not in raw
    assert "TERM identifies" in raw


def test_profile_save_never_overwrites_existing_file(tmp_path, profile_probe):
    path = tmp_path / "existing"
    path.write_text("preserve")
    with pytest.raises(FileExistsError):
        D.save_profile(path, profile_probe)
    assert path.read_text() == "preserve"


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "public", "oversized"])
def test_profile_rejects_unsafe_or_unbounded_sources(tmp_path, profile_probe, kind):
    path = tmp_path / "profile"
    if kind == "symlink":
        target = tmp_path / "target"
        D.save_profile(target, profile_probe)
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "directory":
        path.mkdir()
    elif kind == "public":
        D.save_profile(path, profile_probe)
        path.chmod(0o644)
    else:
        path.write_bytes(b"x" * (D.PROFILE_LIMIT + 1))
        path.chmod(0o600)
    with pytest.raises((OSError, ValueError)):
        D.read_profile(path)


@pytest.mark.parametrize("mutation", ["schema", "runtime", "control", "claims", "tools", "duplicate", "missing", "bool", "status"])
def test_profile_rejects_forged_or_inconsistent_capability_claims(tmp_path, profile_probe, mutation):
    value = D.make_profile(profile_probe)
    if mutation == "schema":
        value["schema"] = "unrecognized"
    elif mutation == "runtime":
        value["runtime"]["private_environment"] = "must-not-be-here"
    elif mutation == "control":
        value["runtime"]["term"] = "\x1b[2J"
    elif mutation == "claims":
        value["tested_features"].append("vim-certified")
    elif mutation == "tools":
        value["tools"]["vim"]["tested"] = True
    elif mutation == "duplicate":
        value["probe"]["checks"][1] = value["probe"]["checks"][0]
    elif mutation == "missing":
        value["probe"]["checks"].pop()
    elif mutation == "bool":
        value["probe"]["checks"][0]["passed"] = "yes"
    else:
        value["probe"]["status"] = "certified"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        D.read_profile(path)


def test_imported_profile_does_not_supply_command_guidance(tmp_path, profile_probe):
    value = D.make_profile(profile_probe)
    value["fullscreen_guidance"] = "execute unrelated command"
    value["limits"] = ["everything certified"]
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    loaded = D.read_profile(path)
    assert loaded["fullscreen_guidance"] == D.FULLSCREEN_GUIDANCE
    assert loaded["limits"] == list(D.PROFILE_LIMITS)


def test_comparison_reports_metadata_changes_without_certification(profile_probe, monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    value = D.make_profile(profile_probe)
    value["runtime"]["engine_version"] = "old-version"
    value["runtime"]["term"] = "old-term"
    value["tools"]["vim"]["available"] = not value["tools"]["vim"]["available"]
    result = D.compare_profile(value, profile_probe)
    assert result["status"] == "changed"
    assert {item["field"] for item in result["runtime_changes"]} == {"term", "engine_version"}
    assert result["tool_availability_changes"] == ["vim"]
    assert "not application" in result["notice"]
    matching = D.compare_profile(D.make_profile(profile_probe), profile_probe)
    assert matching["status"] == "metadata-matched"
    assert "certification" in matching["notice"]


def test_failed_probe_can_be_saved_without_claiming_success(tmp_path, profile_probe):
    profile_probe["status"] = "failed"
    profile_probe["checks"][0]["passed"] = False
    value = D.save_profile(tmp_path / "failed.json", profile_probe)
    assert value["probe"]["status"] == "failed"
    assert profile_probe["checks"][0]["name"] not in value["tested_features"]


def test_cli_can_save_and_compare_profile_without_changing_plain_probe(monkeypatch, tmp_path, capsys, profile_probe):
    monkeypatch.setattr(D, "run_probe", lambda: {**profile_probe, "limits": D.PROFILE_LIMITS[0]})
    path = tmp_path / "profile.json"
    assert D.cli(["--json", "--save-profile", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["profile_saved"] == str(path)
    assert D.cli(["--json", "--profile", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["profile_comparison"]["status"] == "metadata-matched"
    assert D.cli([]) == 0
    assert "Terminal display to fullscreen" in capsys.readouterr().out


def test_cli_invalid_profile_never_runs_probe(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(D, "run_probe", lambda: pytest.fail("Invalid profile must be rejected before launching a child"))
    assert D.cli(["--json", "--profile", str(tmp_path / "absent")]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_runtime_metadata_sanitizes_term_and_never_executes_tool_checks(monkeypatch, profile_probe):
    monkeypatch.setenv("TERM", "\x1b[2J")
    monkeypatch.setattr(D.shutil, "which", lambda name: None)
    profile = D.make_profile(profile_probe)
    assert profile["runtime"]["term"] == "unrecognized"
    assert all(not item["available"] and not item["tested"] for item in profile["tools"].values())
