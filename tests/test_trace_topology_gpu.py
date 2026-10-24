"""GPU sample contracts; API fixtures do not qualify physical device execution."""

from __future__ import annotations

import builtins
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "trace-topology"


@pytest.fixture
def program(monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLE))
    specification = importlib.util.spec_from_file_location(
        "raven_topology_gpu", EXAMPLE / "gpu_workload.py"
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def framework(*, count=2, uuid=None):
    return SimpleNamespace(
        cuda=SimpleNamespace(
            device_count=lambda: count,
            get_device_properties=lambda ordinal: SimpleNamespace(
                name="API fixture partition",
                uuid=uuid,
                total_memory=12345,
            ),
        ),
    )


@pytest.mark.parametrize("size", [8, 16, 64])
@pytest.mark.parametrize("iterations", [1, 5, 32])
def test_closed_form_reference_matches_independent_iterative_stencil(
    program, size, iterations
):
    field = program.common.initial_field(size)
    for _ in range(iterations):
        field = program.common.advance(field, sweeps=1)
    assert program.spectral_reference(size, iterations) == pytest.approx(
        field, rel=1e-12, abs=1e-12
    )


def test_single_gpu_uses_exactly_one_visible_device(program):
    assert program.select_devices(framework(count=2), 1, multiple=False) == (0,)
    with pytest.raises(ValueError, match="exactly one"):
        program.select_devices(framework(), 2, multiple=False)


def test_multi_gpu_requires_actual_visible_device_count(program):
    assert program.select_devices(framework(count=4), 3, multiple=True) == (0, 1, 2)
    with pytest.raises(ValueError, match="2 or more"):
        program.select_devices(framework(), 1, multiple=True)
    with pytest.raises(RuntimeError, match="PyTorch sees 1"):
        program.select_devices(framework(count=1), 2, multiple=True)


@pytest.mark.parametrize("count", [0, 9, True, 2.5])
def test_device_count_is_strictly_bounded(program, count):
    with pytest.raises(ValueError, match="1 through 8"):
        program.select_devices(framework(count=8), count, multiple=True)


def test_partition_metadata_does_not_claim_two_physical_chips(program):
    actual = program.reported_device(framework(uuid="MIG-fixture"), 0)
    assert actual["reported_uuid"] == "MIG-fixture"
    assert "physical-chip independence is not inferred" in actual["identity_scope"]
    assert program.reported_device(framework(), 0)["reported_uuid"] is None


def test_missing_torch_is_actionable_without_creating_results(
    program, monkeypatch, tmp_path, capsys
):
    original = builtins.__import__

    def absent(name, *args, **kwargs):
        if name == "torch":
            raise ModuleNotFoundError("no application Torch", name="torch")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", absent)
    target = tmp_path / "results"
    assert program.main(["--output-dir", str(target), "--seconds", "0"]) == 2
    assert "GPU-enabled PyTorch" in capsys.readouterr().err
    assert not target.exists()


@pytest.mark.parametrize(
    "cuda,hip,available,message",
    [
        (None, None, False, "no CUDA or ROCm support"),
        ("fixture", None, False, "cannot access"),
        (None, "fixture", False, "cannot access"),
    ],
)
def test_unusable_backend_does_not_fall_back_to_cpu(
    program, monkeypatch, cuda, hip, available, message
):
    fake = SimpleNamespace(
        version=SimpleNamespace(cuda=cuda, hip=hip),
        cuda=SimpleNamespace(is_available=lambda: available),
    )
    monkeypatch.setitem(sys.modules, "torch", fake)
    with pytest.raises(RuntimeError, match=message):
        program.load_framework()


def test_missing_allocation_fails_before_output_creation(program, tmp_path):
    target = tmp_path / "results"
    args = program.common.parse_args(
        ["--output-dir", str(target), "--seconds", "0"], description="fixture"
    )
    with pytest.raises(RuntimeError, match="PyTorch sees 1"):
        program.run(args, multiple=True, framework=framework(count=1))
    assert not target.exists()


def test_final_reference_refuses_wrong_solution_despite_valid_invariants(program):
    initial = program.common.initial_field(16)
    final = program.common.advance(initial, sweeps=4)
    wrong = tuple(value * 0.9 for value in final)
    with pytest.raises(ArithmeticError, match="reference_agreement"):
        program.validate_device_result(initial, wrong, final)


def test_private_profile_wrapper_checkpoint_has_precedence(program, monkeypatch):
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "_raven_launch_runtime.tracing.activity_capture",
        SimpleNamespace(step=lambda: calls.append("owned-wrapper") or True),
    )
    assert program.activity_checkpoint() is True
    assert calls == ["owned-wrapper"]


def test_standalone_checkpoint_can_use_public_adapter(program, monkeypatch):
    from raven.tracing import activity_capture

    monkeypatch.delitem(
        sys.modules, "_raven_launch_runtime.tracing.activity_capture", raising=False
    )
    monkeypatch.setattr(activity_capture, "step", lambda: True)
    assert program.activity_checkpoint() is True


def test_submission_api_fixture_queues_all_devices_before_any_readback(
    program, monkeypatch, tmp_path
):
    """This verifies host orchestration ordering, not device overlap or capture."""
    fake = framework(count=2)
    fake.version = SimpleNamespace(hip=None)
    fake.__version__ = "explicit-api-fixture"
    events = []

    def initialize(framework, initial, ordinal):
        return initial, f"stream-{ordinal}"

    def enqueue(framework, field, stream, ordinal, sweeps, snapshot):
        events.append(("enqueue", ordinal))
        return program.common.advance(field, sweeps=sweeps), None

    def read(framework, field, pending, stream, ordinal, *, last):
        events.append(("read", ordinal))
        return field, {
            "energy": program.common.energy(field),
            "first_mode_amplitude": None,
            "host_submission": program.common.identity(),
            "final_field": field if last else None,
        }

    monkeypatch.setattr(program, "initialize_device", initialize)
    monkeypatch.setattr(program, "enqueue_device_iteration", enqueue)
    monkeypatch.setattr(program, "read_device_iteration", read)
    monkeypatch.setattr(program, "activity_checkpoint", lambda: False)
    args = program.common.parse_args(
        [
            "--output-dir",
            str(tmp_path / "api-order"),
            "--workers",
            "2",
            "--seconds",
            "0",
            "--steps",
            "3",
            "--size",
            "64",
            "--sweeps",
            "2",
        ],
        description="host orchestration fixture",
    )
    summary = program.run(args, multiple=True, framework=fake)
    assert events == [("enqueue", 0), ("enqueue", 1), ("read", 0), ("read", 1)] * 3
    assert summary["device_count"] == 2
    assert all(item["checks"]["reference_agreement"] for item in summary["devices"])
    assert "overlap is permitted, not measured" in summary["submission_model"]


def test_cpu_tensor_math_if_torch_is_installed_is_not_gpu_qualification(program):
    torch = pytest.importorskip("torch")
    initial = program.common.initial_field(64)
    field = torch.tensor(initial, dtype=torch.float64)
    final = program.advance_device(torch, field, 12)
    expected = program.spectral_reference(64, 12)
    assert final.tolist() == pytest.approx(expected, rel=1e-12, abs=1e-12)
    energy, amplitude = program.observe_device(torch, final, snapshot=True)
    assert energy == pytest.approx(program.common.energy(expected))
    assert amplitude > 0


@pytest.mark.parametrize("count", [1, 2])
def test_actual_allocated_gpu_execution_when_available(program, tmp_path, count):
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available() or torch.cuda.device_count() < count:
        pytest.skip(
            f"requires {count} actual CUDA/ROCm devices in this test allocation"
        )
    target = tmp_path / "gpu-results"
    args = program.common.parse_args(
        [
            "--output-dir",
            str(target),
            "--workers",
            str(count),
            "--seconds",
            "0",
            "--steps",
            "3",
            "--size",
            "64",
            "--sweeps",
            "2",
        ],
        description="actual device qualification",
    )
    summary = program.run(args, multiple=count > 1, framework=torch)
    stored = json.loads((target / "summary.json").read_text())
    assert stored["device_count"] == count
    assert {item["host_submission"]["native_tid"] for item in summary["devices"]} == {
        stored["coordinator"]["native_tid"]
    }
    assert all(item["checks"]["reference_agreement"] for item in stored["devices"])
    assert all(item["checks"]["energy_nonincreasing"] for item in stored["devices"])
    assert all(
        item["host_submission"]["pid"] == stored["coordinator"]["pid"]
        for item in stored["devices"]
    )
    with pytest.raises(FileExistsError):
        program.run(args, multiple=count > 1, framework=torch)
