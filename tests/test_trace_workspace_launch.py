"""Capture presets reach saved plans and the existing reviewed launch workflow."""

import pytest

from raven.entry.workload import resolve_plan
from raven.launching.wizard import review
from raven.tracing.profiles import preview


def test_profile_expands_before_explicit_overrides_without_editing_script(tmp_path):
    source = tmp_path / "job.sh"
    source.write_text("#!/bin/sh\nprintf 'science\\n'\n")
    original = source.read_bytes()
    plan = resolve_plan(
        [
            str(source),
            "--project",
            str(tmp_path),
            "--local",
            "--trace-profile",
            "pilot",
            "--trace-processes",
            "3",
            "--trace-cpu-placement",
        ],
        cwd=tmp_path,
        environ={},
    )
    assert plan["trace"]
    assert plan["settings"]["trace_processes"] == 3
    assert plan["settings"]["trace_process_bytes"] == 4 << 20
    assert plan["settings"]["trace_run_bytes"] == 32 << 20
    assert plan["trace_budget"]["retained_byte_upper_bound"] == 12 << 20
    assert plan["trace_budget"]["cpu_placement_points"]
    assert plan["setting_sources"]["trace_process_bytes"] == "trace profile pilot"
    assert plan["setting_sources"]["trace_processes"] == "command line"
    assert source.read_bytes() == original
    assert not (tmp_path / ".raven").exists()


def test_recordings_form_preset_reproducible_and_workload_account_separate(tmp_path):
    source = tmp_path / "job.sh"
    source.write_text("#!/bin/sh\n#SBATCH --account=LabX\ntrue\n")
    reviewed = review(
        dict(
            project=str(tmp_path),
            script=source.name,
            trace=True,
            trace_profile="topology",
            execution="slurm",
        ),
        environ={},
    )
    plan = reviewed["plan"]
    assert "--trace-profile topology" in reviewed["command"]
    assert plan["account"] == "LabX"
    assert plan["settings"]["trace_cpu_placement"]
    assert "allocation" not in plan["settings"]
    assert "No allocation" in plan["trace_budget"]["resources"]


def test_disabled_capture_and_conflicting_profile(tmp_path):
    source = tmp_path / "job.sh"
    source.write_text("#!/bin/sh\ntrue\n")
    with pytest.raises(ValueError, match="conflicts with --no-trace"):
        resolve_plan(
            [str(source), "--trace-profile", "pilot", "--no-trace"],
            cwd=tmp_path,
            environ={},
        )
    assert preview({"trace": False})["retained_byte_upper_bound"] == 0
