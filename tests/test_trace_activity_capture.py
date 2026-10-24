"""Framework adapter control, journal integration and application isolation.

The fake framework is an API fixture, not physical GPU qualification. Imported
reports use the real bounded parser, integrity writer and reader.
"""

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from raven.tracing import activity_capture as C
from raven.tracing.protocol import read_json, read_trace


class FrameworkProfile:
    def __init__(self, framework, options):
        self.framework, self.options = framework, options
        self.steps = self.exports = self.exits = 0

    def __enter__(self):
        self.framework.enters += 1
        return self

    def __exit__(self, *exception):
        self.exits += 1
        if (
            "schedule" not in self.options
            or not self.steps
            or self.steps % self.options["schedule"]["active"]
        ):
            self.options["on_trace_ready"](self)
        return True  # Raven must not let a provider swallow application errors.

    def step(self):
        self.steps += 1
        if (
            "schedule" in self.options
            and self.steps % self.options["schedule"]["active"] == 0
        ):
            self.options["on_trace_ready"](self)

    def export_chrome_trace(self, destination):
        self.exports += 1
        if self.framework.export_failure:
            raise RuntimeError("deliberate export failure")
        category = "kernel" if "CUDA" in self.options["activities"] else "cpu_op"
        Path(destination).write_text(
            json.dumps(
                {
                    "traceEvents": [
                        {
                            "ph": "X",
                            "ts": 10 * self.exports,
                            "dur": 2,
                            "name": "solve",
                            "cat": category,
                            "pid": 123,
                            "tid": 8,
                            "args": {"device": 0, "context": 1, "stream": 7},
                        }
                    ]
                }
            )
        )


class Framework:
    def __init__(self, *, gpu=True, active=False):
        self.profiles, self.enters, self.export_failure = [], 0, False
        self.__version__ = "fixture-1"
        self.version = SimpleNamespace(hip=None, cuda="fixture")
        self.autograd = SimpleNamespace(_profiler_enabled=lambda: active)
        self.profiler = SimpleNamespace(
            ProfilerActivity=SimpleNamespace(CPU="CPU", CUDA="CUDA"),
            schedule=lambda **kwargs: kwargs,
            profile=self.profile,
        )
        self.cuda = SimpleNamespace(
            is_available=lambda: gpu,
            device_count=lambda: 1,
            get_device_properties=lambda index: SimpleNamespace(
                name="Fixture GPU", uuid="GPU-A"
            ),
            synchronize=lambda *args: pytest.fail(
                "adapter inserted device synchronization"
            ),
        )

    def profile(self, **options):
        result = FrameworkProfile(self, options)
        self.profiles.append(result)
        return result


def install(monkeypatch, framework):
    monkeypatch.setattr(C, "_load_torch", lambda: framework)
    return framework


def test_cooperative_live_windows_are_registered_before_application_finishes(
    tmp_path, monkeypatch
):
    framework = install(monkeypatch, Framework())
    collector = C.TorchActivityRecorder(
        tmp_path,
        job_id="42",
        attempt="one",
        gpu=True,
        node="node-a",
        active_steps=2,
        max_windows=2,
    )
    with collector:
        collector.step()
        assert not collector.receipt["reports"]
        collector.step()
        assert len(collector.receipt["reports"]) == 1
        saved = read_json(collector.receipt_path)
        assert saved["status"] == "recording"
        assert saved["reports"][0]["manifest"]
        collector.step()
        collector.step()
        collector.step()  # Application can continue after the capture budget.
        assert collector.receipt["status"] == "limit"
    assert collector.receipt["steps"] == 5
    assert len(collector.receipt["reports"]) == 2
    assert framework.profiles[0].exits == 1
    assert collector.receipt["capture_qualified"] is False
    for result in collector.receipt["reports"]:
        snapshot = read_trace(result["manifest"])
        assert snapshot["status"] == "COMPLETE"
        activity = next(
            row["data"] for row in snapshot["events"] if row["kind"] == "activity"
        )
        assert activity["device"]["uuid"] == "GPU-A"
        assert activity["device"]["node"] == "node-a"
        assert activity["device"]["vendor"] == "nvidia"
        assert Path(result["producer_report"]).is_file()
        assert Path(result["producer_report"]).stat().st_mode & 0o777 == 0o600
    assert collector.receipt["stored_bytes"] <= collector.max_total_bytes
    assert collector.receipt["interpreter"] == sys.executable
    assert collector.receipt["torch_version"] == "fixture-1"


def test_generic_script_profile_exports_at_completion_and_restores_arguments(
    tmp_path, monkeypatch
):
    framework = install(monkeypatch, Framework())
    target = tmp_path / "app.py"
    target.write_text(
        "import json,sys\nfrom pathlib import Path\nPath('result.json').write_text(json.dumps(sys.argv))\n"
    )
    monkeypatch.chdir(tmp_path)
    original_argv, original_path = sys.argv, list(sys.path)
    result = C.profile_script(
        [str(target), "literal $HOME"],
        project=tmp_path,
        job_id="42",
        attempt="one",
        gpu=True,
    )
    assert json.loads((tmp_path / "result.json").read_text()) == [
        str(target),
        "literal $HOME",
    ]
    assert sys.argv is original_argv and sys.path == original_path
    assert result["status"] == "complete"
    assert result["live"] is False and len(result["reports"]) == 1
    assert "schedule" not in framework.profiles[0].options
    assert read_json(result["receipt"])["application_status"] == "completed"


def test_missing_optional_framework_keeps_original_application_working(
    tmp_path, monkeypatch
):
    def missing():
        raise ModuleNotFoundError("torch unavailable in this interpreter")

    monkeypatch.setattr(C, "_load_torch", missing)
    target = tmp_path / "app.py"
    target.write_text(
        "from pathlib import Path\nPath('science-result').write_text('done')\n"
    )
    monkeypatch.chdir(tmp_path)
    result = C.profile_script(
        [str(target)], project=tmp_path, job_id="42", attempt="one", gpu=True
    )
    assert (tmp_path / "science-result").read_text() == "done"
    assert (
        result["status"] == "unavailable"
        and result["application_status"] == "completed"
    )
    assert "torch unavailable" in result["errors"][0]
    assert not result["reports"]
    assert read_json(result["receipt"])["errors"] == result["errors"]


def test_required_gpu_failure_prevents_launch_and_never_claims_cpu_fallback(
    tmp_path, monkeypatch
):
    framework = install(monkeypatch, Framework(gpu=False))
    target = tmp_path / "app.py"
    target.write_text("raise AssertionError('application must not start')\n")
    with pytest.raises(RuntimeError, match="Required activity capture could not start"):
        C.profile_script(
            [str(target)],
            project=tmp_path,
            job_id="42",
            attempt="one",
            gpu=True,
            required=True,
        )
    assert framework.profiles == []
    receipt = read_json(
        next((tmp_path / ".raven" / "activity-captures").glob("*/receipt.json"))
    )
    assert receipt["status"] == "unavailable" and receipt["gpu_requested"]
    assert receipt["application_status"] == "not-started"


def test_existing_framework_profiler_is_not_replaced(tmp_path, monkeypatch):
    framework = install(monkeypatch, Framework(active=True))
    with C.TorchActivityRecorder(tmp_path, job_id="42", attempt="one") as collector:
        assert collector.receipt["status"] == "unavailable"
        assert "not replace" in collector.receipt["errors"][0]
    assert framework.profiles == []


def test_original_application_exception_survives_provider_exit_and_artifacts_remain(
    tmp_path, monkeypatch
):
    install(monkeypatch, Framework())
    target = tmp_path / "app.py"
    target.write_text("raise LookupError('original scientific failure')\n")
    original_argv, original_path = sys.argv, list(sys.path)
    with pytest.raises(LookupError, match="original scientific failure"):
        C.profile_script([str(target)], project=tmp_path, job_id="42", attempt="one")
    assert sys.argv is original_argv and sys.path == original_path
    receipt = read_json(
        next((tmp_path / ".raven" / "activity-captures").glob("*/receipt.json"))
    )
    assert receipt["application_status"] == "failed"
    assert len(receipt["reports"]) == 1
    assert read_trace(receipt["reports"][0]["manifest"])["status"] == "COMPLETE"


def test_export_error_stops_capture_and_preserves_application_execution(
    tmp_path, monkeypatch
):
    framework = install(monkeypatch, Framework())
    framework.export_failure = True
    with C.TorchActivityRecorder(
        tmp_path, job_id="42", attempt="one", active_steps=1
    ) as collector:
        collector.step()
        collector.step()
        assert collector.receipt["steps"] == 2
        assert collector.receipt["status"] == "error"
        assert collector._profiler is None
    assert framework.profiles[0].exits == 1
    assert "deliberate export failure" in collector.receipt["errors"][0]
    assert collector.receipt["application_status"] == "completed"
    assert not collector.receipt["reports"]


def test_artifact_budget_failure_keeps_scientific_exception_semantics(
    tmp_path, monkeypatch
):
    install(monkeypatch, Framework())
    with C.TorchActivityRecorder(
        tmp_path, job_id="42", attempt="one", active_steps=1, max_total_bytes=8192
    ) as collector:
        collector.step()
        assert collector.receipt["status"] == "error"
        assert "artifact budget" in collector.receipt["errors"][0]
    assert collector.receipt["reports"] == []


def test_strict_capture_failure_surfaces_after_successful_application(
    tmp_path, monkeypatch
):
    framework = install(monkeypatch, Framework())
    framework.export_failure = True
    with pytest.raises(
        RuntimeError, match="Required activity capture did not complete"
    ):
        with C.TorchActivityRecorder(
            tmp_path, job_id="42", attempt="one", required=True
        ):
            pass


def test_step_lifecycle_is_explicit_and_close_is_idempotent(tmp_path, monkeypatch):
    install(monkeypatch, Framework())
    collector = C.TorchActivityRecorder(tmp_path, job_id="42", attempt="one")
    with pytest.raises(RuntimeError, match="entered"):
        collector.step()
    with collector:
        collector.step()
    frozen = json.dumps(collector.receipt)
    collector.__exit__(None, None, None)
    assert json.dumps(collector.receipt) == frozen
    with pytest.raises(RuntimeError, match="entered"):
        collector.step()
    with pytest.raises(RuntimeError, match="only once"):
        collector.__enter__()


def test_prerequisite_report_does_not_import_torch_or_probe_a_gpu(monkeypatch):
    monkeypatch.setattr(C.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(
        C, "_load_torch", lambda: pytest.fail("capability report imported Torch")
    )
    result = C.capabilities()
    assert result["status"] == "prerequisite-present" and result["qualified"] is False
    assert "tool-dependent" in result["buffer_bound"]


def test_wrapper_step_hook_publishes_live_windows_and_restores_context(
    tmp_path, monkeypatch
):
    install(monkeypatch, Framework())
    target = tmp_path / "app.py"
    target.write_text(
        "from raven.tracing.activity_capture import step\nfor _ in range(4): step()\n"
    )
    assert C.step() is False
    result = C.profile_script(
        [str(target)],
        project=tmp_path,
        job_id="42",
        attempt="one",
        active_steps=2,
        max_windows=3,
    )
    assert result["live"] and result["live_observed"]
    assert result["steps"] == 4 and len(result["reports"]) == 2
    assert result["status"] == "complete"
    assert C.step() is False


def test_no_step_calls_do_not_claim_a_live_recording(tmp_path, monkeypatch):
    install(monkeypatch, Framework())
    target = tmp_path / "app.py"
    target.write_text("result = 1 + 1\n")
    result = C.profile_script(
        [str(target)], project=tmp_path, job_id="42", attempt="one", active_steps=2
    )
    assert result["live"] and result["live_observed"] is False
    assert "No cooperative step()" in result["live_reason"]
    assert len(result["reports"]) == 1


def test_nested_capture_keeps_the_outer_owner_and_step_hook(tmp_path, monkeypatch):
    install(monkeypatch, Framework())
    with C.TorchActivityRecorder(
        tmp_path, job_id="42", attempt="outer", active_steps=1
    ) as outer:
        with C.TorchActivityRecorder(tmp_path, job_id="42", attempt="inner") as inner:
            assert inner.receipt["status"] == "unavailable"
            assert C.step() is True
        assert outer.receipt["steps"] == 1
        assert len(outer.receipt["reports"]) == 1
    assert C.step() is False


@pytest.mark.parametrize("exit_code", [0, 7])
def test_script_exit_codes_preserve_success_or_failure_and_durable_receipt(
    tmp_path, monkeypatch, exit_code
):
    install(monkeypatch, Framework())
    target = tmp_path / "app.py"
    target.write_text(f"raise SystemExit({exit_code})\n")
    if exit_code:
        with pytest.raises(SystemExit) as caught:
            C.profile_script(
                [str(target)], project=tmp_path, job_id="42", attempt="one"
            )
        assert caught.value.code == exit_code
    else:
        assert (
            C.profile_script(
                [str(target)], project=tmp_path, job_id="42", attempt="one"
            )["status"]
            == "complete"
        )
    receipt = read_json(
        next((tmp_path / ".raven" / "activity-captures").glob("*/receipt.json"))
    )
    assert receipt["application_exit_code"] == exit_code
    assert receipt["application_status"] == ("failed" if exit_code else "completed")


def test_copied_context_cannot_advance_profiler_on_a_foreign_thread(
    tmp_path, monkeypatch
):
    from contextvars import copy_context
    from threading import Thread

    install(monkeypatch, Framework())
    observed = []
    with C.TorchActivityRecorder(tmp_path, job_id="42", attempt="one") as recorder:
        context = copy_context()
        thread = Thread(target=lambda: observed.append(context.run(C.step)))
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert observed == [False] and recorder.receipt["steps"] == 0


def test_two_minute_example_has_a_fast_real_standard_library_smoke_path(
    tmp_path, monkeypatch, capsys
):
    install(monkeypatch, Framework())
    script = (
        Path(__file__).resolve().parents[1]
        / "examples/trace-lab/activity_window_lab.py"
    )
    receipt = C.profile_script(
        [str(script), "--seconds", "0", "--steps", "3", "--size", "8", "--sweeps", "1"],
        project=tmp_path,
        job_id="42",
        attempt="one",
        active_steps=2,
    )
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["backend"] == "python" and result["steps"] == 3
    assert result["energy"] <= result["initial_energy"]
    assert result["active_hook_steps"] == 3
    assert receipt["status"] == "complete" and receipt["live_observed"]


def test_framework_python_replay_stacks_are_an_explicit_separate_opt_in(
    tmp_path, monkeypatch
):
    framework = install(monkeypatch, Framework())
    with C.TorchActivityRecorder(tmp_path, job_id="42", attempt="default") as recorder:
        assert framework.profiles[-1].options["with_stack"] is False
        assert recorder.receipt["framework_python_stacks"] is False
        assert "Operator/activity names only" in recorder.receipt["stack_scope"]
    with C.TorchActivityRecorder(
        tmp_path, job_id="42", attempt="stack", with_stack=True
    ) as recorder:
        assert framework.profiles[-1].options["with_stack"] is True
        assert recorder.receipt["framework_python_stacks"] is True
        assert "not native or device" in recorder.receipt["stack_scope"]
