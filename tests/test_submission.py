"""Submission preparation must remain local, bounded, and faithful to argv."""
from __future__ import annotations

import copy
import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven import submission
from raven.remote import SshBackend
from raven.slurm import FakeBackend, Slurm


@pytest.fixture(autouse=True)
def clear_sbatch_environment(monkeypatch):
    for name in os.environ:
        if name.startswith("SBATCH_"):
            monkeypatch.delenv(name)


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "training job's script.sh"
    path.write_text("#!/bin/bash\n#SBATCH --cpus-per-task=4\n#SBATCH --mem=8G\n#SBATCH --time=01:00:00\npython train.py\n")
    return path


def codes(plan):
    return {issue["code"] for issue in plan["issues"]}


class Scheduler:
    def __init__(self, response=(True, "Submitted batch job 10001", "10001")):
        self.b = SimpleNamespace()
        self.calls = []
        self.response = response

    def preview_submit(self, argv, workdir):
        self.calls.append(("preview", list(argv), workdir))
        return True, "test-only validated"

    def submit(self, argv, workdir):
        self.calls.append(("submit", list(argv), workdir))
        return self.response


def test_prepare_never_runs_script_or_scheduler(script, tmp_path, monkeypatch):
    marker = tmp_path / "DO_NOT_CREATE"
    script.write_text(f"#!/bin/bash\ntouch {shlex.quote(str(marker))}\n")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("preflight executed a subprocess"))
    plan = submission.prepare(script, workdir=tmp_path)
    assert plan["valid"]
    assert not marker.exists()
    assert json.loads(json.dumps(plan)) == plan


def test_exact_argv_quotes_and_options_override_directives(script, tmp_path):
    parameters = {"learning_rate": 0.001, "seed": 17}
    overrides = ["-c8", "--job-name", "a 'quoted'; $(touch NOT_CREATED) name", "--output=slurm-%j.out"]
    plan = submission.prepare(script, tmp_path, overrides, parameters)
    assert plan["valid"], plan["issues"]
    assert plan["resources"]["cpus_per_task"] == "8"
    assert plan["parameters"] == parameters
    assert plan["argv"] == ["--parsable", *overrides, "--chdir=" + str(tmp_path), str(script)]
    assert shlex.split(plan["command"]) == ["sbatch", *plan["argv"]]
    assert "option_override" in codes(plan)


def test_directives_stop_at_first_command_and_comments_are_accepted(script, tmp_path):
    script.write_text("#!/bin/bash\n# comment\n\n  #SBATCH -c 3 -J 'two words' # trailing explanation\ntrue\n#SBATCH --mem=999G\n")
    plan = submission.prepare(script, tmp_path)
    assert plan["valid"]
    assert plan["resources"]["cpus_per_task"] == "3"
    assert plan["resources"]["job_name"] == "two words"
    assert "mem" not in plan["resources"]
    assert "ignored_directive" in codes(plan)


def test_invalid_directive_quotes(script, tmp_path):
    script.write_text("#!/bin/bash\n#SBATCH --job-name='unclosed\ntrue\n")
    assert "directive_syntax" in codes(submission.prepare(script, tmp_path))


@pytest.mark.parametrize("flags,code", [
    (["--mem-per-cpu=2G"], "memory_conflict"),
    (["--cpus-per-gpu=4"], "cpu_conflict"),
    (["--ntasks-per-gpu=2", "--gpus-per-task=1"], "task_gpu_conflict"),
    (["--gres=gpu:a100:1", "--gpus=2"], "gpu_conflict"),
    (["-c0"], "invalid_resource"),
    (["-c-1"], "invalid_resource"),
    (["--nodes=4-2"], "invalid_nodes"),
    (["--nodes=0"], "invalid_nodes"),
    (["--mem=NaNG"], "invalid_memory"),
    (["--time=01:70:00"], "invalid_time"),
    (["--time=1-24:00:00"], "invalid_time"),
    (["--gpus-per-task=oops"], "invalid_gpu"),
    (["--array=9-0"], "invalid_array"),
    (["--array=0-9:0"], "invalid_array"),
    (["--array=1,2%0"], "invalid_array"),
    (["--array=-1"], "invalid_array"),
    (["--open-mode=destroy"], "invalid_open_mode"),
    (["--job-name=" + "n" * 129], "invalid_job_name"),
    (["--job-name=line\nbreak"], "invalid_option"),
    (["--job-name=$UNSET"], "unexpanded_placeholder"),
    (["--output={{LOG_DIRECTORY}}/result"], "unexpanded_placeholder"),
    (["--wrap", "echo hello"], "unsupported_option"),
    (["--site-option=value"], "unsupported_option"),
    (["--quiet"], "unsupported_option"),
    (["--test-only"], "unsupported_option"),
    (["another-script.sh"], "unexpected_argument"),
    ([";", "touch", "bad"], "unexpected_argument"),
    (["--", "another-script.sh"], "unsupported_option"),
    (["--mem"], "missing_value"),
    (["-c"], "missing_value"),
])
def test_preflight_rejects_invalid_or_unsupported_flags(script, tmp_path, flags, code):
    plan = submission.prepare(script, tmp_path, flags)
    assert not plan["valid"]
    assert code in codes(plan)


@pytest.mark.parametrize("value", ["0", "120", "01:30", "03:00:00", "2-04", "2-04:30", "2-04:30:10", "infinite"])
def test_valid_slurm_time_formats(script, tmp_path, value):
    assert submission.prepare(script, tmp_path, ["--time=" + value])["valid"]


def test_huge_array_is_arithmetic_and_bounded(script, tmp_path):
    plan = submission.prepare(script, tmp_path, ["--array=0-2147483647:2%12"])
    assert plan["valid"]
    assert plan["resources"]["array_task_count_upper_bound"] == 1073741824
    assert len(json.dumps(plan)) < 4096
    plan = submission.prepare(script, tmp_path, ["--array=" + ",".join("1" for _ in range(1025))])
    assert "invalid_array" in codes(plan)


@pytest.mark.parametrize("flag", ["--cpus-per-task", "--nodes", "--time", "--array", "--gpus"])
def test_huge_integer_values_do_not_crash(script, tmp_path, flag):
    plan = submission.prepare(script, tmp_path, [flag + "=" + "9" * 10000])
    assert not plan["valid"]


def test_workdir_from_directive_and_explicit_override(script, tmp_path, monkeypatch):
    directory = tmp_path / "working directory"
    directory.mkdir()
    script.write_text("#!/bin/bash\n#SBATCH --chdir='working directory'\ntrue\n")
    monkeypatch.chdir(tmp_path)
    plan = submission.prepare(script)
    assert plan["valid"]
    assert plan["workdir"] == str(directory)
    plan = submission.prepare(script, tmp_path, ["--chdir=missing"])
    assert plan["valid"]
    assert plan["argv"][-2] == "--chdir=" + str(tmp_path)
    assert not submission.prepare(script, tmp_path / "absent")["valid"]


def test_declared_inputs_outputs_and_symlink_collisions(script, tmp_path):
    source = tmp_path / "input.dat"
    source.write_text("data")
    link = tmp_path / "same-input"
    link.symlink_to(source)
    plan = submission.prepare(script, tmp_path, inputs=["input.dat"], outputs=["same-input"])
    assert not plan["valid"] and "output_collision" in codes(plan)
    assert "missing_input" in codes(submission.prepare(script, tmp_path, inputs=["missing"]))
    assert "output_collision" in codes(submission.prepare(script, tmp_path, outputs=[str(script)]))
    assert "output_collision" in codes(submission.prepare(script, tmp_path, outputs=["new", "new"]))
    plan = submission.prepare(script, tmp_path, outputs=["not-created/result.json"])
    assert plan["valid"] and "missing_output_parent" in codes(plan)
    assert not (tmp_path / "not-created").exists()


def test_declared_inputs_match_passport_capture_constraints(script, tmp_path):
    source = tmp_path / "input"
    source.write_text("input")
    link = tmp_path / "linked-input"
    link.symlink_to(source)
    assert submission.prepare(script, tmp_path, inputs=[str(link)])["valid"]
    assert "invalid_input" in codes(submission.prepare(script, tmp_path, inputs=[str(tmp_path)]))
    for declaration in ({"path": str(source), "hash": "true"}, {"path": str(source), "max_bytes": True},
                        {"path": str(source), "max_bytes": 65 << 20}, {"path": str(source), "unknown": 1}):
        assert "invalid_input_declaration" in codes(submission.prepare(script, tmp_path, inputs=[declaration]))
    assert submission.prepare(script, tmp_path, inputs=[{"path": str(source), "hash": True, "max_bytes": 64}])["valid"]
    assert "input_hash_budget" in codes(submission.prepare(script, tmp_path, inputs=[{"path": str(source), "hash": True, "max_bytes": 1}]))
    assert "invalid_metadata" in codes(submission.prepare(script, tmp_path, parameters={"password": "private"}))


def test_array_log_patterns_and_missing_log_parent(script, tmp_path):
    assert "array_log_collision" in codes(submission.prepare(script, tmp_path, ["--array=0-9", "--output=log-%A.txt"]))
    for pattern in ("log-%a.txt", "log-%j.txt", "log-%04a.txt"):
        assert "array_log_collision" not in codes(submission.prepare(script, tmp_path, ["--array=0-9", "--output=" + pattern]))
    assert "array_log_collision" in codes(submission.prepare(script, tmp_path, ["--array=0-9", "--output=log-%%a.txt"]))
    assert "missing_log_parent" in codes(submission.prepare(script, tmp_path, ["--output=missing/%j.txt"]))
    assert "output_collision" in codes(submission.prepare(script, tmp_path, ["--output=" + str(script)]))


def test_inherited_sbatch_options_are_explicit_and_do_not_leak_values(script, tmp_path, monkeypatch):
    monkeypatch.setenv("SBATCH_EXPORT", "SECRET=do-not-print")
    plan = submission.prepare(script, tmp_path)
    assert not plan["valid"]
    assert "inherited_sbatch_options" in codes(plan)
    assert "SBATCH_EXPORT" in json.dumps(plan)
    assert "do-not-print" not in json.dumps(plan)


@pytest.mark.parametrize("content,code", [(b"true\n", "missing_shebang"), (b"#!\ntrue\n", "missing_shebang"),
                                          (b"#!/bin/bash\r\ntrue\r\n", "script_encoding"),
                                          (b"#!/bin/bash\n\x00", "script_encoding"), (b"\xff", "script_unreadable")])
def test_invalid_scripts_are_actionable(script, tmp_path, content, code):
    script.write_bytes(content)
    plan = submission.prepare(script, tmp_path)
    assert not plan["valid"]
    assert code in codes(plan)


def test_script_read_bounds_regular_files_and_fifos(script, tmp_path, monkeypatch):
    monkeypatch.setattr(submission, "MAX_SCRIPT_BYTES", 16)
    assert "script_unreadable" in codes(submission.prepare(script, tmp_path))
    assert "script_unreadable" in codes(submission.prepare(tmp_path / "missing", tmp_path))
    assert "script_unreadable" in codes(submission.prepare(tmp_path, tmp_path))
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert "script_unreadable" in codes(submission.prepare(fifo, tmp_path))


def test_cyclic_symlink_and_control_paths_are_preflight_errors(script, tmp_path):
    cycle = tmp_path / "cycle"
    cycle.symlink_to(cycle)
    assert "script_unreadable" in codes(submission.prepare(cycle, tmp_path))
    assert "invalid_workdir" in codes(submission.prepare(script, cycle))
    newline_directory = tmp_path / "line\nbreak"
    newline_directory.mkdir()
    assert "invalid_path" in codes(submission.prepare(script, newline_directory))


@pytest.mark.parametrize("kwargs", [{"overrides": "--mem=8G"}, {"overrides": [42]}, {"inputs": "one-path"},
                                    {"outputs": [None]}, {"parameters": [1]}, {"parameters": {"bad": float("nan")}}])
def test_invalid_api_metadata_is_rejected(script, tmp_path, kwargs):
    plan = submission.prepare(script, tmp_path, **kwargs)
    assert not plan["valid"]
    json.dumps(plan, allow_nan=False)


def test_preview_is_explicit_and_exact(script, tmp_path):
    scheduler = Scheduler()
    plan = submission.prepare(script, tmp_path, ["--job-name=spaces; $()"])
    assert scheduler.calls == []
    result = submission.preview(plan, scheduler)
    assert result["ok"]
    assert scheduler.calls == [("preview", plan["argv"], str(tmp_path))]
    assert shlex.split(result["command"]) == ["sbatch", "--test-only", *plan["argv"]]


def test_changed_script_or_modified_plan_prevents_scheduler_calls(script, tmp_path):
    scheduler = Scheduler()
    plan = submission.prepare(script, tmp_path)
    altered = copy.deepcopy(plan)
    altered["argv"].insert(0, "--mem=999G")
    assert "changed" in submission.preview(altered, scheduler)["error"]
    script.write_text(script.read_text() + "echo change\n")
    assert "script changed" in submission.preview(plan, scheduler)["error"]
    assert not submission.submit(plan, scheduler)["ok"]
    assert scheduler.calls == []


@pytest.mark.parametrize("field,value", [("resources", {"mem": "999G"}), ("command", "false"),
                                         ("valid", False), ("issues", []), ("directives", [])])
def test_review_fields_cannot_be_tampered(script, tmp_path, field, value):
    scheduler = Scheduler()
    plan = submission.prepare(script, tmp_path, ["-c8"])
    plan[field] = value
    assert "changed" in submission.preview(plan, scheduler)["error"]
    assert scheduler.calls == []


def test_preflight_rechecks_declared_input_and_environment(script, tmp_path, monkeypatch):
    source = tmp_path / "data"
    source.write_text("input")
    plan = submission.prepare(script, tmp_path, inputs=[str(source)])
    source.unlink()
    scheduler = Scheduler()
    assert "input" in submission.preview(plan, scheduler)["error"]
    source.write_text("input")
    monkeypatch.setenv("SBATCH_MEM_PER_CPU", "5000")
    assert "inherited" in submission.preview(plan, scheduler)["error"]
    assert scheduler.calls == []


def test_remote_and_replay_wrapped_backend_are_rejected(script, tmp_path):
    scheduler = Scheduler()
    scheduler.b = SimpleNamespace(inner=SshBackend("cluster", runner=lambda *a: pytest.fail("SSH executed")))
    plan = submission.prepare(script, tmp_path)
    assert "login node" in submission.preview(plan, scheduler)["error"]
    assert "login node" in submission.submit(plan, scheduler)["error"]
    assert scheduler.calls == []


def test_save_load_preserves_plan_and_refuses_overwrite_or_tampering(script, tmp_path):
    plan = submission.prepare(script, tmp_path)
    path = submission.save(plan, tmp_path / "plan.json")
    assert submission.load(path) == plan
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        submission.save(plan, path)
    modified = copy.deepcopy(plan)
    modified["workdir"] = "/unexpected"
    path.write_text(json.dumps(modified))
    with pytest.raises(ValueError, match="modified"):
        submission.load(path)
    path.write_text('{"schema":"raven.submission-plan/v1"}')
    with pytest.raises(ValueError, match="incomplete"):
        submission.load(path)


def test_plan_read_bounds_and_nonregular_files(script, tmp_path, monkeypatch):
    plan = submission.prepare(script, tmp_path)
    path = submission.save(plan, tmp_path / "plan.json")
    monkeypatch.setattr(submission, "MAX_PLAN_BYTES", 16)
    with pytest.raises(ValueError, match="size limit"):
        submission.load(path)
    with pytest.raises(ValueError, match="size limit"):
        submission.save(plan, tmp_path / "another.json")
    fifo = tmp_path / "plan-fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular file"):
        submission.load(fifo)


@pytest.mark.parametrize("ok,output,expected,state", [
    (True, "10001", "10001", "accepted"),
    (True, "10001;cluster", "10001", "accepted"),
    (True, "Submitted batch job 10001", "10001", "accepted"),
    (True, "warning: allocation 999\nSubmitted batch job 10001", "10001", "accepted"),
    (True, "Submitted batch job 10001\nSubmitted batch job 10002", None, "unknown"),
    (True, "accepted without a receipt", None, "unknown"),
    (False, "invalid partition", None, "rejected"),
    (False, "Command timed out after 30 seconds", None, "unknown"),
])
def test_submit_receipt_states_and_passport_capture(script, tmp_path, ok, output, expected, state):
    pytest.importorskip("raven.provenance")
    scheduler = Scheduler((ok, output, "999999"))  # Do not trust the old permissive ID parser.
    plan = submission.prepare(script, tmp_path)
    result = submission.submit(plan, scheduler, tmp_path / "passports")
    assert result["job_id"] == expected, result
    assert result["state"] == state, result
    assert result["ok"] is bool(ok and expected)
    assert Path(result["passport_path"]).is_file()
    assert scheduler.calls == [("submit", plan["argv"], str(tmp_path))]


def test_passport_write_failure_prevents_submission(script, tmp_path):
    pytest.importorskip("raven.provenance")
    blocker = tmp_path / "not-directory"
    blocker.write_text("block")
    scheduler = Scheduler()
    result = submission.submit(submission.prepare(script, tmp_path), scheduler, blocker)
    assert not result["ok"] and result["submitted"] is False
    assert scheduler.calls == []


def test_scheduler_exception_after_contact_has_unknown_outcome(script, tmp_path):
    pytest.importorskip("raven.provenance")
    scheduler = Scheduler()

    def failed_receipt(argv, workdir):
        scheduler.calls.append(("submit", list(argv), workdir))
        raise OSError("recording write failed after scheduler call")

    scheduler.submit = failed_receipt
    result = submission.submit(submission.prepare(script, tmp_path), scheduler, tmp_path / "passports")
    assert result["state"] == "unknown"
    assert result["submitted"] is None
    assert result["job_id"] is None
    assert "inspect the queue" in result["error"]
    assert Path(result["passport_path"]).is_file()
    assert len(scheduler.calls) == 1


def test_fake_backend_end_to_end_submit_and_preview(script, tmp_path):
    pytest.importorskip("raven.provenance")
    backend = FakeBackend()
    scheduler = Slurm(backend, "alex")
    plan = submission.prepare(script, tmp_path, ["--gres=gpu:a100:1", "--array=0-9%2"])
    initial_jobs = len(backend.spec)
    assert submission.preview(plan, scheduler)["ok"]
    assert len(backend.spec) == initial_jobs
    result = submission.submit(plan, scheduler, tmp_path / "passports")
    assert result["ok"], result
    assert len(backend.spec) == initial_jobs + 1
    assert backend.spec[-1]["id"] == result["job_id"]
