#!/usr/bin/env python3
"""Evolve a periodic heat field and report real measurements to Raven.

The default two-minute run is deliberately paced for live inspection. Use
``--pause 0`` for a fast calculation. Python calls form a shallow numerical
pipeline, with optional spawned processes analyzing immutable snapshots.
Only the parent process reports metrics; helper processes return observations.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import multiprocessing
import os
import random
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
from pathlib import Path

try:
    import raven
except ModuleNotFoundError as exc:
    if exc.name != "raven":
        raise
    raise SystemExit(
        "Raven's metrics client is required. Launch job.sh through Raven, "
        "or install Raven in this Python environment."
    ) from exc


def initialize_field(size, seed):
    """Build three reproducible Gaussian hotspots on a periodic square."""
    rng = random.Random(seed)
    spots = [
        (rng.random(), rng.random(), rng.uniform(0.07, 0.16), rng.uniform(0.8, 1.5))
        for _ in range(3)
    ]
    field = []
    for row in range(size):
        values = []
        for column in range(size):
            value = 0.1
            for x, y, width, height in spots:
                dx = abs(column / size - x)
                dy = abs(row / size - y)
                distance_squared = min(dx, 1 - dx) ** 2 + min(dy, 1 - dy) ** 2
                value += height * math.exp(-distance_squared / (2 * width * width))
            values.append(value)
        field.append(tuple(values))
    return tuple(field)


def update_row(previous, row, alpha):
    """A convex five-point stencil; periodic edges exchange heat."""
    row_count = len(previous)
    above, center, below = previous[(row - 1) % row_count], previous[row], previous[(row + 1) % row_count]
    size = len(center)
    updated = []
    for column in range(size):
        value = (1 - 4 * alpha) * center[column] + alpha * (
            above[column] + below[column]
            + center[(column - 1) % size] + center[(column + 1) % size]
        )
        updated.append(value)
    return tuple(updated)


def advance_step(previous, alpha):
    # Eager row assembly avoids generator resume/yield noise in the trace.
    return tuple([update_row(previous, row, alpha) for row in range(len(previous))])


def rms_difference(previous, current):
    squared = math.fsum([
        (after - before) ** 2
        for old_row, new_row in zip(previous, current)
        for before, after in zip(old_row, new_row)
    ])
    return math.sqrt(squared / (len(current) ** 2))


@raven.measure("residual", kind="residual", step="step", unit="a.u.")
def measured_residual(previous, current, step):
    return rms_difference(previous, current)


@raven.measure("peak_temperature", kind="gauge", step="step", unit="a.u.")
def measured_peak(field, step):
    return max([max(row) for row in field])


@raven.measure("total_heat", kind="conserved", step="step", unit="a.u. * cells")
def measured_heat(field, step):
    return math.fsum([value for row in field for value in row])


@raven.measure("thermal_variance", kind="objective", step="step", unit="a.u.^2")
def measured_variance(field, step):
    values = [value for row in field for value in row]
    mean = math.fsum(values) / len(values)
    return math.fsum([(value - mean) ** 2 for value in values]) / len(values)


def report_step(previous, current, step, total_steps):
    values = {
        "residual": measured_residual(previous, current, step),
        "peak_temperature": measured_peak(current, step),
        "total_heat": measured_heat(current, step),
        "thermal_variance": measured_variance(current, step),
    }
    raven.progress(step, total_steps, unit="steps", step=step)
    print(
        f"step={step:4d}/{total_steps} residual={values['residual']:.6g} "
        f"peak={values['peak_temperature']:.6g} heat={values['total_heat']:.12g}",
        flush=True,
    )
    return values


def histogram(values, low, high, bins=12):
    """Use fixed initial bounds so histograms remain comparable over time."""
    counts = [0] * bins
    for value in values:
        index = int((value - low) * bins / (high - low)) if high > low else 0
        counts[min(bins - 1, max(0, index))] += 1
    return counts


def spatial_modes(field, start, stop):
    """Return partial first Fourier modes, without a numerical dependency."""
    size = len(field)
    x_cos, x_sin, y_cos, y_sin = [], [], [], []
    for row in range(start, stop):
        row_phase = 2 * math.pi * row / size
        for column, value in enumerate(field[row]):
            column_phase = 2 * math.pi * column / size
            x_cos.append(value * math.cos(column_phase))
            x_sin.append(value * math.sin(column_phase))
            y_cos.append(value * math.cos(row_phase))
            y_sin.append(value * math.sin(row_phase))
    return [math.fsum(values) for values in (x_cos, x_sin, y_cos, y_sin)]


def summarize_region(field, start, stop, bounds):
    values = [value for row in field[start:stop] for value in row]
    return {
        "count": len(values),
        "minimum": min(values),
        "maximum": max(values),
        "sum": math.fsum(values),
        "histogram": histogram(values, *bounds),
        "modes": spatial_modes(field, start, stop),
    }


def analyze_region(field, start, stop, bounds):
    """Worker entry point: nested analysis, no metric writes or mutation."""
    result = summarize_region(field, start, stop, bounds)
    result["pid"] = os.getpid()
    return result


def analyze_snapshot(field, step, bounds, pool, workers):
    size = len(field)
    parts = max(1, workers)
    arguments = [(field, part * size // parts, (part + 1) * size // parts, bounds) for part in range(parts)]
    if pool is None:
        regions = [analyze_region(*arguments[0])]
    else:
        futures = [pool.submit(analyze_region, *argument) for argument in arguments]
        regions = [future.result() for future in futures]
    modes = [math.fsum(region["modes"][index] for region in regions) for index in range(4)]
    return {
        "step": step,
        "cells": sum(region["count"] for region in regions),
        "minimum": min(region["minimum"] for region in regions),
        "maximum": max(region["maximum"] for region in regions),
        "mean": math.fsum(region["sum"] for region in regions) / (size * size),
        "first_mode_amplitude": math.sqrt(math.fsum(value * value for value in modes)) / (size * size),
        "histogram": [sum(region["histogram"][index] for region in regions) for index in range(12)],
        "analysis_pids": sorted({region["pid"] for region in regions}),
    }


def evolve(initial, args, pool):
    field = initial
    bounds = (min([min(row) for row in initial]), max([max(row) for row in initial]))
    interval = max(1, (args.steps + 7) // 8)
    snapshots = [analyze_snapshot(field, 0, bounds, pool, args.workers)]
    report_step(field, field, 0, args.steps)
    for step in range(1, args.steps + 1):
        previous = field
        with raven.phase("diffusion"):
            field = advance_step(previous, args.alpha)
            metrics = report_step(previous, field, step, args.steps)
        if step % interval == 0 or step == args.steps:
            with raven.phase("snapshot_analysis"):
                snapshot = analyze_snapshot(field, step, bounds, pool, args.workers)
                snapshots.append(snapshot)
                raven.metric("first_mode_amplitude", snapshot["first_mode_amplitude"], step=step, unit="a.u.")
        if step == args.fail_at_step:
            raise RuntimeError(f"Requested demonstration failure after step {step}; earlier recordings remain available.")
        if args.pause:
            time.sleep(args.pause)
    return field, metrics, snapshots, bounds


def numerical_checks(initial, final):
    initial_heat = math.fsum([value for row in initial for value in row])
    final_heat = math.fsum([value for row in final for value in row])
    low, high = min([min(row) for row in initial]), max([max(row) for row in initial])
    final_low, final_high = min([min(row) for row in final]), max([max(row) for row in final])
    tolerance = max(1.0, abs(initial_heat)) * 1e-11
    return {
        "initial_heat": initial_heat,
        "final_heat": final_heat,
        "absolute_heat_drift": abs(final_heat - initial_heat),
        "heat_drift_tolerance": tolerance,
        "heat_conserved": abs(final_heat - initial_heat) <= tolerance,
        "initial_range": [low, high],
        "final_range": [final_low, final_high],
        "maximum_principle": final_low >= low - 1e-12 and final_high <= high + 1e-12,
    }


def write_atomic(target, write):
    """Publish one complete artifact without replacing an existing result."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    finally:
        os.unlink(temporary)


def write_results(output, field, summary):
    # The summary is published last: its existence means both files are complete.
    write_atomic(output / "final-field.csv", lambda handle: csv.writer(handle).writerows(field))
    write_atomic(output / "summary.json", lambda handle: json.dump(summary, handle, indent=2, allow_nan=False))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="artifact directory; existing result files are never replaced")
    parser.add_argument("--grid", type=int, default=32, help="cells on each edge (8..128; default: 32)")
    parser.add_argument("--steps", type=int, default=80, help="diffusion steps (1..2000; default: 80)")
    parser.add_argument("--pause", type=float, default=1.5, help="deliberate live-view delay per step, seconds (0..5; default: 1.5)")
    parser.add_argument("--workers", type=int, default=2, help="spawned analysis workers (0..4; 0 runs analysis in the parent)")
    parser.add_argument("--seed", type=int, default=7, help="reproducible initial field seed (default: 7)")
    parser.add_argument("--alpha", type=float, default=0.20, help="stable diffusion coefficient (0 < alpha <= 0.25)")
    parser.add_argument("--fail-at-step", type=int, help="raise a deliberate error after this step's measurements")
    return validate_args(parser, parser.parse_args(argv))


def validate_args(parser, args):
    """Share finite workload limits with the MPI and optional CUDA variants."""
    if not 8 <= args.grid <= 128 or not 1 <= args.steps <= 2000:
        parser.error("choose --grid 8..128 and --steps 1..2000")
    if args.grid * args.grid * args.steps > 8_000_000:
        parser.error("choose at most 8,000,000 cell updates (grid * grid * steps)")
    if not math.isfinite(args.pause) or not 0 <= args.pause <= 5 or args.pause * args.steps > 3600:
        parser.error("--pause must be finite and 0..5 seconds, with at most one hour of total delay")
    if hasattr(args, "workers") and not 0 <= args.workers <= 4:
        parser.error("--workers must be 0..4")
    if not math.isfinite(args.alpha) or not 0 < args.alpha <= 0.25:
        parser.error("--alpha must be finite and 0 < alpha <= 0.25 for a stable convex stencil")
    if args.fail_at_step is not None and not 1 <= args.fail_at_step <= args.steps:
        parser.error("--fail-at-step must be between 1 and --steps")
    return args


def main(argv=None):
    args = parse_args(argv)
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name in ("summary.json", "final-field.csv"):
        if os.path.lexists(output / name):
            raise FileExistsError(f"Refusing to replace {output / name}; choose a new --output-dir.")
    started = time.monotonic()
    with raven.phase("initialize"):
        initial = initialize_field(args.grid, args.seed)
    context = (
        ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context("spawn"))
        if args.workers else nullcontext(None)
    )
    with context as pool:
        field, metrics, snapshots, bounds = evolve(initial, args, pool)
    checks = numerical_checks(initial, field)
    if not checks["heat_conserved"] or not checks["maximum_principle"]:
        raise ArithmeticError(f"Numerical validation failed: {checks}")
    summary = {
        "schema": "raven.trace-lab/v1",
        "model": "periodic two-dimensional heat diffusion; arbitrary temperature units",
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
        print(f"Trace lab: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
