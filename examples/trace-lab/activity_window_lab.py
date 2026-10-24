#!/usr/bin/env python3
"""Two-minute nested CPU/GPU workload with one cooperative activity-window hook.

Use Raven's ``trace profile --python APP_PYTHON --window-steps 5`` wrapper to
make its tracing module available in the application's interpreter. Choose the
torch or cuda backend to produce framework operator/device activities; python
is a standard-library workload that still runs when optional profiling fails.
The default 120 seconds includes deliberate pacing, not a utilization benchmark.
"""

from __future__ import annotations

import argparse
import json
import math
import time

from raven.tracing.activity_capture import step as activity_step


def initialize(size, backend):
    values = [math.sin(2 * math.pi * index / size) for index in range(size)]
    if backend == "python":
        return values, None
    try:
        import torch
    except ImportError as error:
        raise RuntimeError(
            "The selected backend requires PyTorch in the application interpreter"
        ) from error
    if backend == "cuda" and not torch.cuda.is_available():
        raise RuntimeError(
            "The cuda backend requires an accessible CUDA/ROCm device; it does not substitute CPU computation"
        )
    device = "cuda:0" if backend == "cuda" else "cpu"
    return torch.tensor(values, dtype=torch.float64, device=device), torch


def periodic_neighbors(field, framework):
    if framework is None:
        return [
            field[index - 1] + field[(index + 1) % len(field)]
            for index in range(len(field))
        ]
    return framework.roll(field, 1) + framework.roll(field, -1)


def diffuse(field, framework, alpha):
    neighbors = periodic_neighbors(field, framework)
    if framework is None:
        return [
            (1 - 2 * alpha) * value + alpha * neighbors[index]
            for index, value in enumerate(field)
        ]
    return (1 - 2 * alpha) * field + alpha * neighbors


def advance(field, framework, sweeps):
    for _ in range(sweeps):
        field = diffuse(field, framework, 0.15)
    return field


def energy(field, framework):
    # Scalar conversion is a numerical result check in this workload. The
    # tracing adapter itself never adds a device-wide synchronize operation.
    if framework is None:
        return sum(value * value for value in field) / len(field)
    return framework.mean(field * field).item()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend", choices=("python", "torch", "cuda"), default="python"
    )
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--steps", type=int, default=80)
    parser.add_argument("--size", type=int, default=16384)
    parser.add_argument("--sweeps", type=int, default=8)
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or not 0 <= args.seconds <= 3600:
        parser.error("seconds must be finite and between 0 and 3600")
    if (
        not 1 <= args.steps <= 10000
        or not 8 <= args.size <= 1_000_000
        or not 1 <= args.sweeps <= 100
    ):
        parser.error("steps, size or sweeps exceeds the example's bounds")
    field, framework = initialize(args.size, args.backend)
    initial = energy(field, framework)
    started, windows = time.monotonic(), 0
    for number in range(1, args.steps + 1):
        field = advance(field, framework, args.sweeps)
        value = energy(field, framework)
        if not math.isfinite(value) or value > initial * (1 + 1e-9):
            raise ArithmeticError(
                "Diffusion energy failed its finite/nonincreasing check"
            )
        windows += bool(activity_step())
        if number == 1 or number == args.steps or number % 10 == 0:
            print(f"step={number}/{args.steps} energy={value:.10g}", flush=True)
        target = started + args.seconds * number / args.steps
        time.sleep(max(0, target - time.monotonic()))
    print(
        json.dumps(
            {
                "backend": args.backend,
                "steps": args.steps,
                "energy": value,
                "initial_energy": initial,
                "elapsed_seconds": time.monotonic() - started,
                "active_hook_steps": windows,
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
