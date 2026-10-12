"""Argument boundaries must survive CLI-to-palette transport."""
import subprocess
import sys

import pytest

from raven.cli import build, parse, scripted
from raven.config import Config


def test_resubmit_preview_preserves_script_paths_and_job_names_with_spaces():
    result = subprocess.run(
        [sys.executable, "-m", "raven", "--fake", "--no-state", "--no-plugins",
         "run", "resubmit", "12480001", "--script", "jobs/train model.sbatch", "--job-name", "training model"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 3
    assert "'jobs/train model.sbatch'" in result.stdout
    assert "'--job-name=training model'" in result.stdout
    assert "add --yes" in result.stdout


def test_palette_quoting_error_never_submits_or_confirms():
    session = build(parse(["--fake", "--no-plugins"]), Config())
    try:
        session.app.run_command('resubmit 12480001 --script "unterminated')
        assert session.app.mode != "confirm"
        assert "command failed" in session.app.message
    finally:
        session.close()


def test_eval_retains_quoted_strings_and_spaces(capsys):
    args = parse(["--fake", "--no-plugins", "run", "eval", "'two  spaces'.count(' ')"])
    session = build(args, Config())
    try:
        assert scripted(args, session) == 0
        assert capsys.readouterr().out.strip() == "2"
    finally:
        session.close()


def test_resubmit_account_override_belongs_to_submission():
    args = parse(["--account", "monitor-account", "run", "resubmit", "12480001", "--account", "submit-account", "--fake"])
    assert args.account == "monitor-account"
    assert args.run == "resubmit 12480001 --account submit-account"
    result = subprocess.run(
        [sys.executable, "-m", "raven", "--fake", "--no-state", "--no-plugins",
         "run", "resubmit", "12480001", "--account", "submit-account"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 3
    assert "--account=submit-account" in result.stdout


def test_empty_run_fails_without_starting_an_interactive_session():
    with pytest.raises(SystemExit) as error:
        parse(["run", "--fake"])
    assert error.value.code == 2
