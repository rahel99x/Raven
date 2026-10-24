"""The example wrapper reuses frozen ownership and preserves scientific exits."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "topology_profile_runner", ROOT / "examples/trace-topology/profile_runner.py"
)
PROGRAM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROGRAM)


def test_profile_requires_a_frozen_job_runtime_before_recording(monkeypatch):
    monkeypatch.delitem(sys.modules, "_raven_launch_runtime", raising=False)
    monkeypatch.delenv("RAVEN_RUNTIME_DIRECTORY", raising=False)
    monkeypatch.delenv("RAVEN_RUNTIME_BUNDLE", raising=False)
    with pytest.raises(ValueError, match="run.sh launcher"):
        PROGRAM.runtime_package()
    assert "_raven_launch_runtime" not in sys.modules


def test_context_uses_verified_run_identity_and_owned_window_limits(
    monkeypatch, tmp_path
):
    run = tmp_path / "runs" / "attempt-1"
    plan = {"fixture": "frozen plan", "project_root": str(tmp_path)}
    observed = []
    activity = SimpleNamespace()
    modules = {
        "fixture.reporting": SimpleNamespace(
            _read_json=lambda path: plan, _directory=Path
        ),
        "fixture.launching.autotrace": SimpleNamespace(
            verified_run=lambda selected, config: (
                observed.append((selected, config)) or (run, {"job_id": "42"})
            )
        ),
        "fixture.tracing.activity_capture": activity,
    }
    monkeypatch.setattr(PROGRAM, "runtime_package", lambda: "fixture")
    monkeypatch.setattr(PROGRAM.importlib, "import_module", modules.__getitem__)
    monkeypatch.setenv("RAVEN_RUN_DIR", str(run))
    monkeypatch.setenv("RAVEN_LAUNCH_PLAN", str(tmp_path / "plan.json"))
    monkeypatch.setenv("SLURM_CLUSTER_NAME", "LabCluster")
    recorder, context = PROGRAM.recording_context()
    assert recorder is activity and observed == [(run, plan)]
    assert context == dict(
        project=tmp_path,
        job_id="42",
        attempt="attempt-1",
        cluster="LabCluster",
        gpu=True,
        required=False,
        active_steps=2,
        max_windows=64,
        max_bytes=8 << 20,
    )
    monkeypatch.setenv("RAVEN_EXAMPLE_GPU", "unexpected")
    with pytest.raises(ValueError, match="must be 0 or 1"):
        PROGRAM.recording_context()


@pytest.mark.parametrize("failure", [LookupError("scientific failure"), SystemExit(7)])
def test_profile_does_not_rerun_or_replace_scientific_failure(monkeypatch, failure):
    calls = []

    def profile(arguments, **context):
        calls.append((arguments, context))
        raise failure

    monkeypatch.setattr(
        PROGRAM,
        "recording_context",
        lambda: (SimpleNamespace(profile_script=profile), {"gpu": True}),
    )
    arguments = ["single_gpu.py", "--steps", "3"]
    with pytest.raises(type(failure)) as caught:
        PROGRAM.main(arguments)
    assert caught.value is failure and calls == [(arguments, {"gpu": True})]


def test_optional_activity_unavailability_preserves_success(monkeypatch, capsys):
    monkeypatch.setattr(
        PROGRAM,
        "recording_context",
        lambda: (
            SimpleNamespace(
                profile_script=lambda *args, **kwargs: {
                    "status": "unavailable",
                    "reports": [],
                }
            ),
            {},
        ),
    )
    assert PROGRAM.main(["single_gpu.py"]) == 0
    assert "Activity recording: unavailable" in capsys.readouterr().out
