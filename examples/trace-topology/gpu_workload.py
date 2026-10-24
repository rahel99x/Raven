"""One- and multiple-device diffusion using actual CUDA/ROCm execution.

Each process-visible device gets its own stream. One host thread queues every
device's work before reading results, preserving framework-profiler visibility
while allowing device execution to overlap. The two-mode
periodic initial condition has an independent closed-form diffusion solution,
so validation is O(cells), even when a longer experiment performs many sweeps.
Visible ordinals may describe GPU partitions; they do not prove physical chips.
"""

from __future__ import annotations

import math
import sys
import time

import common
import raven


ALPHA = 0.15


def load_framework():
    """Require a usable device backend without substituting CPU computation."""
    try:
        import torch
    except ModuleNotFoundError as error:
        if error.name != "torch":
            raise
        raise RuntimeError(
            "GPU samples require GPU-enabled PyTorch in the application's Python "
            "environment. Load the site's CUDA or ROCm PyTorch module, or choose "
            "the corresponding --python environment. No CPU fallback is used."
        ) from error
    if not (
        getattr(torch.version, "cuda", None) or getattr(torch.version, "hip", None)
    ):
        raise RuntimeError(
            "This PyTorch build has no CUDA or ROCm support; install/load a "
            "GPU-enabled application environment. No CPU fallback is used."
        )
    if not torch.cuda.is_available():
        raise RuntimeError(
            "PyTorch cannot access a CUDA/ROCm device. Run inside the requested "
            "GPU allocation and check the driver and device visibility. "
            "No CPU fallback is used."
        )
    return torch


def select_devices(framework, count, *, multiple):
    if type(count) is not int or not 1 <= count <= 8:
        raise ValueError("GPU samples support 1 through 8 visible devices")
    if multiple and count < 2:
        raise ValueError("multi_gpu.py requires --workers 2 or more visible devices")
    if not multiple and count != 1:
        raise ValueError("single_gpu.py uses exactly one device; select --workers 1")
    available = framework.cuda.device_count()
    if type(available) is not int or available < count:
        raise RuntimeError(
            f"Requested {count} process-visible GPU devices, but PyTorch sees "
            f"{available}. Request the matching GPUs in Slurm; do not expose "
            "devices outside the allocation. No device is emulated."
        )
    return tuple(range(count))


def reported_device(framework, ordinal):
    properties = framework.cuda.get_device_properties(ordinal)
    reported_uuid = getattr(properties, "uuid", None)
    if reported_uuid is not None:
        reported_uuid = str(reported_uuid)[:1024]
        if not reported_uuid.isprintable():
            reported_uuid = None
    return {
        "visible_ordinal": ordinal,
        "name": str(properties.name)[:256],
        "reported_uuid": reported_uuid,
        "reported_total_memory_bytes": int(properties.total_memory),
        "identity_scope": "Process-visible device or partition; physical-chip independence is not inferred",
    }


def spectral_reference(size, iterations):
    """Exact discrete-stencil eigenmodes, apart from ordinary float rounding."""
    first = (1 - 2 * ALPHA + 2 * ALPHA * math.cos(2 * math.pi / size)) ** iterations
    second = (1 - 2 * ALPHA + 2 * ALPHA * math.cos(4 * math.pi / size)) ** iterations
    return tuple(
        first * math.sin(2 * math.pi * index / size)
        + 0.25 * second * math.cos(4 * math.pi * index / size)
        for index in range(size)
    )


def periodic_neighbors(framework, field):
    return framework.roll(field, 1) + framework.roll(field, -1)


def diffuse_device(framework, previous):
    neighbors = periodic_neighbors(framework, previous)
    return (1 - 2 * ALPHA) * previous + ALPHA * neighbors


def advance_device(framework, field, sweeps):
    for _ in range(sweeps):
        field = diffuse_device(framework, field)
    return field


def spectral_amplitude(framework, field):
    spectrum = framework.fft.rfft(field)
    return spectrum[1].abs() / field.numel()


def enqueue_observations(framework, field, *, snapshot):
    return (
        framework.mean(field * field),
        spectral_amplitude(framework, field) if snapshot else None,
    )


def read_observations(pending):
    energy, amplitude = pending
    value = energy.item()
    if not math.isfinite(value):
        raise ArithmeticError("GPU diffusion produced nonfinite energy")
    return value, amplitude.item() if amplitude is not None else None


def observe_device(framework, field, *, snapshot):
    # Scalar conversion fences these result reductions on this stream. There
    # is no device-wide synchronization added only for a tracing display.
    return read_observations(enqueue_observations(framework, field, snapshot=snapshot))


def initialize_device(framework, initial, ordinal):
    with framework.cuda.device(ordinal):
        stream = framework.cuda.Stream(device=ordinal)
        with framework.cuda.stream(stream):
            field = framework.tensor(
                initial, dtype=framework.float64, device=f"cuda:{ordinal}"
            )
    return field, stream


def enqueue_device_iteration(framework, field, stream, ordinal, sweeps, snapshot):
    """Enqueue one device without synchronizing or reading a scalar result."""
    with framework.cuda.device(ordinal), framework.cuda.stream(stream):
        with raven.phase(f"gpu_{ordinal}_diffusion"):
            field = advance_device(framework, field, sweeps)
        with raven.phase(f"gpu_{ordinal}_measurements"):
            pending = enqueue_observations(framework, field, snapshot=snapshot)
    return field, pending


def read_device_iteration(framework, field, pending, stream, ordinal, *, last):
    """Read a device only after every device has received its queued step."""
    with framework.cuda.device(ordinal), framework.cuda.stream(stream):
        with raven.phase(f"gpu_{ordinal}_result_check"):
            value, amplitude = read_observations(pending)
            final = field.detach().cpu().tolist() if last else None
    return field, {
        "energy": value,
        "first_mode_amplitude": amplitude,
        "host_submission": common.identity(),
        "final_field": final,
    }


def activity_checkpoint():
    """An optional profiler wrapper owns the main-thread window clock."""
    active = sys.modules.get("_raven_launch_runtime.tracing.activity_capture")
    if active is not None:
        return bool(active.step())
    try:
        from raven.tracing.activity_capture import step
    except ModuleNotFoundError as error:
        if error.name not in {"raven.tracing", "raven.tracing.activity_capture"}:
            raise
        # Automatic Python tracing can ship only Raven's small metrics client.
        return False
    return bool(step())


def validate_device_result(initial, final, expected):
    checks = common.check_field(initial, final)
    if len(final) != len(expected):
        raise ArithmeticError("GPU result length differs from the reference")
    maximum_error = max(
        abs(actual - reference) for actual, reference in zip(final, expected)
    )
    tolerance = 2e-10
    checks.update(
        reference="Closed-form discrete periodic diffusion eigenmodes",
        reference_maximum_absolute_error=maximum_error,
        reference_absolute_tolerance=tolerance,
        reference_agreement=math.isfinite(maximum_error) and maximum_error <= tolerance,
    )
    if not all(
        checks[name]
        for name in (
            "finite",
            "mean_conserved",
            "energy_nonincreasing",
            "reference_agreement",
        )
    ):
        raise ArithmeticError(f"GPU diffusion failed numerical checks: {checks}")
    return checks


def run(args, *, multiple, framework=None):
    # Validate topology mode before importing a possibly large optional runtime.
    if multiple and args.workers < 2:
        raise ValueError("multi_gpu.py requires --workers 2 or more visible devices")
    if not multiple and args.workers != 1:
        raise ValueError("single_gpu.py uses exactly one device; select --workers 1")
    framework = framework or load_framework()
    ordinals = select_devices(framework, args.workers, multiple=multiple)
    devices = [reported_device(framework, ordinal) for ordinal in ordinals]
    common.prepare_output(args)
    initial = common.initial_field(args.size)
    fields_and_streams = [
        initialize_device(framework, initial, ordinal) for ordinal in ordinals
    ]
    initial_energy = common.energy(initial)
    started, active_windows = time.monotonic(), 0
    observations = []
    for number in range(1, args.steps + 1):
        snapshot = (
            number == 1 or number == args.steps or number % max(1, args.steps // 8) == 0
        )
        with raven.phase("gpu_iteration"):
            # No .item(), .cpu() or device-wide synchronization occurs in this
            # first loop. Each independent stream receives its work first.
            pending = [
                enqueue_device_iteration(
                    framework, field, stream, ordinal, args.sweeps, snapshot
                )
                for ordinal, (field, stream) in zip(ordinals, fields_and_streams)
            ]
            results = [
                read_device_iteration(
                    framework, field, values, stream, ordinal, last=number == args.steps
                )
                for ordinal, (field, values), (_, stream) in zip(
                    ordinals, pending, fields_and_streams
                )
            ]
        fields_and_streams = [
            (field, previous[1])
            for (field, _), previous in zip(results, fields_and_streams)
        ]
        observations = [observation for _, observation in results]
        if any(item["energy"] > initial_energy + 1e-10 for item in observations):
            raise ArithmeticError("GPU diffusion energy exceeded the initial field")
        for ordinal, observation in zip(ordinals, observations):
            raven.metric(
                f"gpu_{ordinal}_energy",
                observation["energy"],
                unit="a.u.^2",
                step=number,
            )
        common.report_step(
            number,
            args,
            sum(item["energy"] for item in observations) / len(observations),
        )
        active_windows += activity_checkpoint()
        common.pace(started, number, args)
    expected = spectral_reference(args.size, args.steps * args.sweeps)
    rows = []
    for device, observation in zip(devices, observations):
        final = observation.pop("final_field")
        rows.append(
            dict(
                device,
                **observation,
                checks=validate_device_result(initial, final, expected),
            )
        )
    summary = {
        "variant": "multi_gpu" if multiple else "single_gpu",
        "execution": "Actual CUDA/ROCm tensors and per-device streams; no CPU fallback",
        "submission_model": "One host thread submits all device work before any result readback; overlap is permitted, not measured here",
        "device_count": len(devices),
        "device_identity_limit": "Distinct visible ordinals may be partitions of one physical GPU",
        "backend": "rocm" if getattr(framework.version, "hip", None) else "cuda",
        "framework_version": str(framework.__version__),
        "coordinator": common.identity(),
        "devices": rows,
        "active_activity_checkpoints": active_windows,
        "elapsed_seconds": time.monotonic() - started,
        "recording_scope": "Python tracing records host calls; framework device activities require the explicit profiling wrapper",
    }
    common.write_summary(args, summary)
    return summary


def main(argv=None, *, multiple=False):
    args = common.parse_args(
        argv,
        description="Inspect actual concurrent GPU devices/partitions"
        if multiple
        else "Inspect one actual CUDA/ROCm device",
        default_workers=2 if multiple else 1,
    )
    try:
        run(args, multiple=multiple)
    except (OSError, RuntimeError, ValueError, ArithmeticError) as error:
        print("GPU sample: " + str(error), file=sys.stderr)
        return 2
    return 0
