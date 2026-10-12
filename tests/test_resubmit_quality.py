"""A clone must preview the same job it will submit and preserve sbatch arguments."""
import shlex

import pytest

from raven.actions import Actions
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.resubmit import apply_overrides, build, flags_of, has_submission, parse_override_args, script_of
from raven.slurm import FakeBackend, Slurm


@pytest.fixture
def app():
    backend = FakeBackend("alex")
    slurm = Slurm(backend, "alex")
    store = Store(persist=False)
    store.apply_jobs(slurm.jobs())
    app = App(store, None, Actions(slurm, store), Config(), "alex", ascii_=True, interactive=False)
    app.selected_id = "12480001"
    return app


@pytest.mark.parametrize("switch", ["--no-kill", "--exclusive", "--get-user-env", "--nice", "-H", "-k", "-vv"])
def test_boolean_sbatch_options_leave_the_batch_script_intact(switch):
    argv = apply_overrides([switch, "job.sh", "script argument"], {"mem": "8G"})
    assert script_of(argv) == ["job.sh", "script argument"]
    assert flags_of(argv)["mem"] == "8G"
    assert has_submission(argv)


def test_export_file_and_option_terminator_keep_their_meaning():
    argv = apply_overrides(["--export-file", "env file", "--", "-job.sh", "--mem", "script value"], {"mem": "8G"})
    assert argv == ["--export-file=env file", "--mem=8G", "--", "-job.sh", "--mem", "script value"]
    assert script_of(argv) == ["-job.sh", "--mem", "script value"]
    assert flags_of(argv)["mem"] == "8G"


def test_memory_override_replaces_conflicting_memory_mode():
    assert apply_overrides(["--mem-per-cpu", "2G", "job.sh"], {"mem": "8G"}) == ["--mem=8G", "job.sh"]
    assert apply_overrides(["--mem=8G", "job.sh"], {"mem_per_cpu": "2G"}) == ["--mem-per-cpu=2G", "job.sh"]


def test_wrap_is_a_submission_and_can_be_replaced_with_a_script():
    clone = build("123", {"submit_line": "sbatch --wrap 'printf hello'"}, {}, overrides={"mem": "1G"})
    assert has_submission(clone.argv) and not script_of(clone.argv)
    assert flags_of(clone.argv)["wrap"] == "printf hello"
    clone = build("123", {"submit_line": "sbatch --wrap 'printf hello'"}, {}, overrides={"script": "new job.sh"})
    assert "wrap" not in flags_of(clone.argv)
    assert script_of(clone.argv) == ["new job.sh"]


@pytest.mark.parametrize("args", [["123", "--mem"], ["123", "--mem", "--time", "2:00"], ["123", "--typo=8G"], ["123", "--script"]])
def test_invalid_overrides_do_not_silently_submit_original_resources(args):
    with pytest.raises(ValueError):
        parse_override_args(args)


def test_clone_preview_uses_exact_arguments_and_workdir_before_confirmation(app, monkeypatch):
    slurm = app.actions.slurm
    workdir = "/work/project with spaces"
    original = ["--no-kill", "--export-file", "env file", "--", "-job.sh", "value; still one argument"]
    monkeypatch.setattr(slurm, "submit_info", lambda _: {"submit_line": shlex.join(["sbatch", *original]), "workdir": workdir})
    count = len(slurm.b.spec)
    app.run_command("resubmit 12480001 --mem 8G")
    assert app.command_ok and app.mode == "confirm"
    clone = app.confirm["clone"]
    prefix = "cd " + shlex.quote(workdir) + " && "
    assert slurm.b.calls[-1] == ["sh", "-c", prefix + shlex.join(["sbatch", "--test-only", *clone.argv])]
    assert len(slurm.b.spec) == count
    assert script_of(clone.argv) == original[4:]
    app.finish_confirm(True)
    assert slurm.b.calls[-1] == ["sh", "-c", prefix + shlex.join(["sbatch", *clone.argv])]
    assert app.command_ok and len(slurm.b.spec) == count + 1


def test_failed_preview_cannot_reach_confirmation_or_submit(app, monkeypatch):
    slurm = app.actions.slurm
    monkeypatch.setattr(slurm, "preview_submit", lambda *args: (False, "Unable to open file job.sh"))
    count = len(slurm.b.spec)
    app.run_command("resubmit 12480001")
    assert not app.command_ok and app.mode != "confirm" and not app.confirm
    assert "Unable to open file" in app.message
    assert len(slurm.b.spec) == count


def test_wrap_clone_reaches_confirmation_without_a_script(app, monkeypatch):
    monkeypatch.setattr(app.actions.slurm, "submit_info", lambda _: {"submit_line": "sbatch --wrap 'printf hello'"})
    app.run_command("resubmit 12480001")
    assert app.command_ok and app.mode == "confirm"
    assert app.confirm["clone"].argv == ["--wrap", "printf hello"]


@pytest.mark.parametrize("command", ["resubmit 999", "hold 12480001", "resubmit 12480001 --mem", "chain fly", "tab nonexistent", "export html"])
def test_command_failure_status_is_explicit(app, command):
    app.run_command(command)
    assert not app.command_ok
    app.run_command("note 12480001 failed unknown present")
    assert app.command_ok


def test_failed_confirmed_action_sets_failure_status(app, monkeypatch):
    monkeypatch.setattr(app.actions.slurm, "cancel", lambda _: (False, "Permission denied"))
    app.run_command("cancel 12480001")
    assert app.command_ok and app.mode == "confirm"
    app.finish_confirm(True)
    assert not app.command_ok and "Permission denied" in app.message
