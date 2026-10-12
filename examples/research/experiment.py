#!/usr/bin/env python3
"""A small, reproducible regression experiment using only Python's standard library.

Raven is also dependency-free. Install the checkout with scripts/setup.py first
so this example can import its optional reporter. Each execution creates a new
output directory and never overwrites another run. Delay is solely for making
the terminal demo easy to watch; it is not part of the optimization algorithm.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import random
import time

from raven.metrics import write_metric


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new output directory")
    parser.add_argument("--steps", type=int, default=64)
    parser.add_argument("--samples", type=int, default=512)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--learning-rate", type=float, default=0.15)
    parser.add_argument("--delay", type=float, default=0.05, help="demo pacing in seconds per step")
    args = parser.parse_args()
    if not 1 <= args.steps <= 10000 or not 4 <= args.samples <= 100000:
        parser.error("steps must be 1..10000 and samples must be 4..100000")
    if not math.isfinite(args.learning_rate) or not 0 < args.learning_rate <= 0.5:
        parser.error("learning-rate must be finite and in (0, 0.5]")
    if not math.isfinite(args.delay) or not 0 <= args.delay <= 1:
        parser.error("delay must be finite and in [0, 1]")
    try:
        args.output.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        parser.error(f"output must be a new directory: {exc}")
    rng = random.Random(args.seed)
    data = []
    for _ in range(args.samples):
        x = rng.uniform(-1, 1)
        data.append((x, 2.5 * x - 0.75 + rng.gauss(0, 0.05)))
    dataset_path = args.output / "dataset.csv"
    with dataset_path.open("x", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["x", "y"])
        writer.writerows(data)
    os.chmod(dataset_path, 0o600)

    # These sufficient statistics produce the same full-batch gradients without
    # rescanning the dataset each step: O(samples + steps), not O(samples*steps).
    mean_x = math.fsum(x for x, _ in data) / args.samples
    mean_y = math.fsum(y for _, y in data) / args.samples
    mean_xx = math.fsum(x * x for x, _ in data) / args.samples
    mean_xy = math.fsum(x * y for x, y in data) / args.samples
    mean_yy = math.fsum(y * y for _, y in data) / args.samples
    slope, intercept = 0.0, 0.0
    metrics_path = args.output / "metrics.jsonl"
    write_metric(metrics_path, {}, step=0, phase="prepare", completed=0, total=args.steps, unit="steps")
    curve_path = args.output / "curve.csv"
    with curve_path.open("x", encoding="utf-8", newline="") as curve:
        os.chmod(curve_path, 0o600)
        writer = csv.writer(curve)
        writer.writerow(["step", "loss", "rmse", "slope", "intercept"])
        for step in range(1, args.steps + 1):
            ds = 2 * (slope * mean_xx + intercept * mean_x - mean_xy)
            di = 2 * (slope * mean_x + intercept - mean_y)
            slope -= args.learning_rate * ds
            intercept -= args.learning_rate * di
            loss = max(0.0, slope * slope * mean_xx + intercept * intercept + mean_yy
                       + 2 * slope * intercept * mean_x - 2 * slope * mean_xy - 2 * intercept * mean_y)
            rmse = math.sqrt(loss)
            writer.writerow([step, loss, rmse, slope, intercept])
            curve.flush()
            write_metric(metrics_path, {"loss": loss, "rmse": rmse,
                         "coefficient_error": math.hypot(slope - 2.5, intercept + 0.75)},
                         step=step, phase="fit", completed=step, total=args.steps, unit="steps")
            if args.delay:
                time.sleep(args.delay)
    summary = {"schema": "raven.research-demo", "version": 1, "seed": args.seed,
               "steps": args.steps, "samples": args.samples, "learning_rate": args.learning_rate,
               "final_loss": loss, "rmse": rmse, "coefficients": {"slope": slope, "intercept": intercept},
               "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest()}
    summary_path = args.output / "summary.json"
    with summary_path.open("x", encoding="utf-8") as output:
        os.chmod(summary_path, 0o600)
        json.dump(summary, output, sort_keys=True, indent=2, allow_nan=False)
        output.write("\n")
    write_metric(metrics_path, {"loss": loss, "rmse": rmse}, step=args.steps, phase="complete",
                 completed=args.steps, total=args.steps, unit="steps")
    print(f"Completed {args.steps} steps; loss={loss:.6g}; outputs={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
