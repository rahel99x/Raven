#!/usr/bin/env python3
"""Diffuse a heat field on a real CUDA GPU and retain Python host-call traces.

The default run includes 120 seconds of deliberate pacing for live inspection.
This is a numerical demonstration, not a GPU utilization benchmark. Python
tracing shows kernel submission and synchronization calls, not device kernels.
PyTorch with CUDA support is an optional dependency of this example only.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path

from heat_lab import initialize_field, numerical_checks, raven, write_results


def require_cuda():
    """Fail before creating artifacts if this Python cannot use CUDA."""
    try:
        import torch
    except ModuleNotFoundError as exc:
        if exc.name != "torch":
            raise
        raise RuntimeError(
            "The CUDA example requires a CUDA-enabled PyTorch installation in "
            "RAVEN_EXAMPLE_PYTHON. Use your site's PyTorch module or environment."
        ) from exc
    if not torch.cuda.is_available():
        raise RuntimeError(
            "PyTorch cannot access CUDA. Run inside a GPU allocation, verify "
            "CUDA_VISIBLE_DEVICES and the driver, and use CUDA-enabled PyTorch. "
            "This example does not fall back to CPU computation."
        )
    return torch


def upload_field(torch, initial):
    """Transfer one reproducible initial state to the first visible GPU."""
    field = torch.tensor(initial, dtype=torch.float64, device="cuda:0")
    torch.cuda.synchronize(field.device)
    return field


def periodic_neighbors(torch, field):
    return (
        torch.roll(field, 1, 0) + torch.roll(field, -1, 0)
        + torch.roll(field, 1, 1) + torch.roll(field, -1, 1)
    )


def advance_cuda_step(torch, previous, alpha):
    """Enqueue the same convex periodic stencil used by the CPU example."""
    return (1 - 4 * alpha) * previous + alpha * periodic_neighbors(torch, previous)


@raven.measure("residual", kind="residual", step="step", unit="a.u.")
def measured_residual(torch, previous, current, step):
    return torch.sqrt(torch.mean((current - previous) ** 2)).item()


@raven.measure("peak_temperature", kind="gauge", step="step", unit="a.u.")
def measured_peak(field, step):
    return field.max().item()


@raven.measure("total_heat", kind="conserved", step="step", unit="a.u. * cells")
def measured_heat(field, step):
    return field.sum().item()


@raven.measure("thermal_variance", kind="objective", step="step", unit="a.u.^2")
def measured_variance(torch, field, step):
    # Population variance agrees with the CPU and MPI implementations.
    return torch.mean((field - field.mean()) ** 2).item()


def report_cuda_step(torch, previous, current, step, total_steps):
    # Wall time before this fence can include asynchronous device work. Each
    # scalar .item() below also waits for its corresponding reduction result.
    torch.cuda.synchronize(current.device)
    metrics = {
        "residual": measured_residual(torch, previous, current, step),
        "peak_temperature": measured_peak(current, step),
        "total_heat": measured_heat(current, step),
        "thermal_variance": measured_variance(torch, current, step),
    }
    raven.progress(step, total_steps, unit="steps", step=step)
    print(
        f"CUDA step={step:4d}/{total_steps} residual={metrics['residual']:.6g} "
        f"peak={metrics['peak_temperature']:.6g} heat={metrics['total_heat']:.12g}",
        flush=True,
    )
    return metrics


def spectral_amplitude(torch, field):
    """Measure the first horizontal and vertical Fourier modes on the GPU."""
    spectrum = torch.fft.rfft2(field)
    magnitude_squared = spectrum[0, 1].abs() ** 2 + spectrum[1, 0].abs() ** 2
    return (torch.sqrt(magnitude_squared) / field.numel()).item()


def analyze_cuda_snapshot(torch, field, step, bounds):
    counts = torch.histc(field, bins=12, min=bounds[0], max=bounds[1])
    return {
        "step": step,
        "cells": field.numel(),
        "minimum": field.min().item(),
        "maximum": field.max().item(),
        "mean": field.mean().item(),
        "first_mode_amplitude": spectral_amplitude(torch, field),
        "histogram": [int(count) for count in counts.cpu().tolist()],
        "analysis_pids": [os.getpid()],
    }


def evolve_cuda(torch, initial, args):
    with raven.phase("upload_to_gpu"):
        field = upload_field(torch, initial)
    bounds = (min([min(row) for row in initial]), max([max(row) for row in initial]))
    interval = max(1, (args.steps + 7) // 8)
    snapshots = [analyze_cuda_snapshot(torch, field, 0, bounds)]
    report_cuda_step(torch, field, field, 0, args.steps)
    for step in range(1, args.steps + 1):
        previous = field
        with raven.phase("diffusion"):
            field = advance_cuda_step(torch, previous, args.alpha)
            metrics = report_cuda_step(torch, previous, field, step, args.steps)
        if step % interval == 0 or step == args.steps:
            with raven.phase("snapshot_analysis"):
                snapshot = analyze_cuda_snapshot(torch, field, step, bounds)
                snapshots.append(snapshot)
                raven.metric("first_mode_amplitude", snapshot["first_mode_amplitude"], step=step, unit="a.u.")
        if step == args.fail_at_step:
            raise RuntimeError(
                f"Requested demonstration failure after step {step}; earlier recordings remain available."
            )
        if args.pause:
            time.sleep(args.pause)
    with raven.phase("download_from_gpu"):
        final = field.cpu().tolist()
    return final, metrics, snapshots, bounds


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="artifact directory; existing results are never replaced")
    parser.add_argument("--grid", type=int, default=128, help="cells on each edge (8..1024; default: 128)")
    parser.add_argument("--steps", type=int, default=80, help="diffusion steps (1..2000; default: 80)")
    parser.add_argument("--pause", type=float, default=1.5, help="deliberate delay per step, seconds (0..5; default: 1.5)")
    parser.add_argument("--seed", type=int, default=7, help="reproducible initial field seed (default: 7)")
    parser.add_argument("--alpha", type=float, default=0.20, help="stable diffusion coefficient (0 < alpha <= 0.25)")
    parser.add_argument("--fail-at-step", type=int, help="raise a deliberate error after this step's measurements")
    args = parser.parse_args(argv)
    if not 8 <= args.grid <= 1024 or not 1 <= args.steps <= 2000:
        parser.error("choose --grid 8..1024 and --steps 1..2000")
    if args.grid * args.grid * args.steps > 134_217_728:
        parser.error("choose at most 134,217,728 cell updates (grid * grid * steps)")
    if not math.isfinite(args.pause) or not 0 <= args.pause <= 5 or args.pause * args.steps > 3600:
        parser.error("--pause must be finite and 0..5 seconds, with at most one hour of total delay")
    if not math.isfinite(args.alpha) or not 0 < args.alpha <= 0.25:
        parser.error("--alpha must be finite and 0 < alpha <= 0.25 for a stable convex stencil")
    if args.fail_at_step is not None and not 1 <= args.fail_at_step <= args.steps:
        parser.error("--fail-at-step must be between 1 and --steps")
    return args


def main(argv=None):
    args = parse_args(argv)
    output = args.output_dir.expanduser().resolve()
    for name in ("summary.json", "final-field.csv"):
        if os.path.lexists(output / name):
            raise FileExistsError(f"Refusing to replace {output / name}; choose a new --output-dir.")
    torch = require_cuda()
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with raven.phase("initialize"):
        initial = initialize_field(args.grid, args.seed)
    field, metrics, snapshots, bounds = evolve_cuda(torch, initial, args)
    checks = numerical_checks(initial, field)
    if not checks["heat_conserved"] or not checks["maximum_principle"]:
        raise ArithmeticError(f"Numerical validation failed: {checks}")
    summary = {
        "schema": "raven.trace-lab/v1",
        "model": "periodic two-dimensional heat diffusion; arbitrary temperature units",
        "backend": "cuda",
        "device": torch.cuda.get_device_name(0),
        "dtype": "float64",
        "torch_version": str(torch.__version__),
        "trace_scope": "Python host calls; device kernel execution is not captured",
        "parameters": {key: value for key, value in vars(args).items() if key != "output_dir"},
        "parent_pid": os.getpid(),
        "elapsed_seconds": time.monotonic() - started,
        "deliberate_pause_seconds": args.steps * args.pause,
        "final_metrics": metrics,
        "checks": checks,
        "histogram_bounds": list(bounds),
        "snapshots": snapshots,
        "field_csv": {"path": "final-field.csv", "rows": args.grid, "columns": args.grid, "header": False},
    }
    with raven.phase("write_results"):
        write_results(output, field, summary)
    raven.progress(args.steps, args.steps, unit="steps", step=args.steps)
    print(f"Completed: {output / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ArithmeticError) as exc:
        print(f"CUDA trace lab: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
