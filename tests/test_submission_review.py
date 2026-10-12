"""Independent regression cases for submission planning boundaries and races."""
from __future__ import annotations

import copy
import json
import os
from types import SimpleNamespace

import pytest

from raven import provenance, submission


@pytest.fixture(autouse=True)
def clear_sbatch_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith("SBATCH_"):
            monkeypatch.delenv(key)


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "batch.sh"
    path.write_text("#!/bin/bash\n#SBATCH --mem=8G\ntrue\n")
    return path


class Scheduler:
    def __init__(self):
        self.b = SimpleNamespace()
        self.calls = []

    def submit(self, argv, workdir):
        self.calls.append((list(argv), workdir))
        return True, "301", "301"

    def preview_submit(self, argv, workdir):
        self.calls.append((list(argv), workdir))
        return True, "validated"


def test_late_directive_diagnostics_are_bounded(script, tmp_path):
    script.write_text("#!/bin/bash\ntrue\n" + "#SBATCH --mem=1G\n" * 30000)
    plan = submission.prepare(script, tmp_path)
    assert len(plan["issues"]) <= 256
    assert any(issue["code"] == "ignored_directive" for issue in plan["issues"])
    assert "mem" not in plan["resources"]
    assert len(json.dumps(plan)) < submission.MAX_PLAN_BYTES


@pytest.mark.parametrize("flag", ["--output", "--error"])
@pytest.mark.parametrize("symlink", [False, True])
def test_stdin_log_collision_is_detected_without_declared_input(script, tmp_path, flag, symlink):
    source = tmp_path / "observations.txt"
    source.write_text("science data")
    destination = source
    if symlink:
        destination = tmp_path / "alias.txt"
        destination.symlink_to(source)
    plan = submission.prepare(script, tmp_path, ["--input", str(source), flag, str(destination)])
    assert not plan["valid"]
    assert any(issue["code"] == "output_collision" for issue in plan["issues"])
    scheduler = Scheduler()
    assert not submission.preview(plan, scheduler)["ok"] and not scheduler.calls
    assert source.read_text() == "science data"


def test_overly_deep_metadata_is_an_invalid_plan_instead_of_crashing(script, tmp_path):
    nested = {}
    for _ in range(1100):
        nested = {"nested": nested}
    plan = submission.prepare(script, tmp_path, parameters=nested)
    assert not plan["valid"]
    assert any(issue["code"] == "invalid_metadata" for issue in plan["issues"])


def test_cyclic_metadata_is_rejected_without_scheduler_calls(script, tmp_path):
    parameters = {"seed": 4}
    parameters["cycle"] = parameters
    plan = submission.prepare(script, tmp_path, parameters=parameters)
    assert not plan["valid"]
    scheduler = Scheduler()
    assert not submission.submit(plan, scheduler)["submitted"] and not scheduler.calls


def test_extremely_nested_saved_plan_raises_a_user_facing_value_error(tmp_path):
    path = tmp_path / "nested-plan.json"
    path.write_text("[" * 2000 + "0" + "]" * 2000)
    with pytest.raises(ValueError):
        submission.load(path)


def test_sbatch_environment_change_during_capture_prevents_submit(script, tmp_path, monkeypatch):
    plan = submission.prepare(script, tmp_path)
    scheduler = Scheduler()

    def capture(*args, **kwargs):
        monkeypatch.setenv("SBATCH_MEM", "999G")
        return {"capture": "synthetic"}

    monkeypatch.setattr(provenance, "capture", capture)
    result = submission.submit(plan, scheduler)
    assert result["submitted"] is False and not scheduler.calls


def test_missing_input_during_capture_prevents_submit(script, tmp_path, monkeypatch):
    source = tmp_path / "input.bin"
    source.write_bytes(b"observations")
    plan = submission.prepare(script, tmp_path, inputs=[str(source)])
    scheduler = Scheduler()

    def capture(*args, **kwargs):
        source.unlink()
        return {"capture": "synthetic"}

    monkeypatch.setattr(provenance, "capture", capture)
    result = submission.submit(plan, scheduler)
    assert result["submitted"] is False and not scheduler.calls


def test_mutated_display_fields_never_change_actual_submission(script, tmp_path, monkeypatch):
    plan = submission.prepare(script, tmp_path)
    modified = copy.deepcopy(plan)
    modified["command"] = "sbatch --wrap='touch SHOULD_NOT_EXIST'"
    modified["resources"]["mem"] = "999G"
    modified["issues"] = []
    scheduler = Scheduler()
    monkeypatch.setattr(provenance, "capture", lambda *args, **kwargs: {"capture": "synthetic"})
    result = submission.submit(modified, scheduler)
    # A stricter full-review digest may reject the mutation outright. Otherwise
    # the actual command and passport must come from revalidated execution data.
    if scheduler.calls:
        assert result["command"] == plan["command"]
        assert scheduler.calls == [(plan["argv"], str(tmp_path))]
    else:
        assert result["submitted"] is False


def test_nested_metadata_mutation_after_prepare_requires_review(script, tmp_path):
    parameters = {"training": {"seed": 4}}
    plan = submission.prepare(script, tmp_path, parameters=parameters)
    plan["parameters"]["training"]["seed"] = 999
    scheduler = Scheduler()
    result = submission.submit(plan, scheduler)
    assert not result["submitted"] and not scheduler.calls


def test_script_change_during_passport_capture_prevents_submit(script, tmp_path, monkeypatch):
    plan = submission.prepare(script, tmp_path)
    scheduler = Scheduler()

    def capture(*args, **kwargs):
        script.write_text("#!/bin/bash\n#SBATCH --mem=999G\ntrue\n")
        return {"capture": "synthetic"}

    monkeypatch.setattr(provenance, "capture", capture)
    result = submission.submit(plan, scheduler)
    assert result["submitted"] is False and not scheduler.calls
