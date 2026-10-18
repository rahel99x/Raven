#!/usr/bin/env python3
"""Reproducible synthetic held-out evaluation, never a live deployment claim."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import random
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from raven import measurement_series as M, phase_alignment as P, stream_anomalies as A


def capture(values, *, metric="gpu", unit="%", attempt="synthetic", stamps=None):
    return {"schema": M.SCHEMA, "series": [{"identity": {"scope": "declared-synthetic", "job": attempt, "attempt": attempt, "verified": True},
            "metric": metric, "unit": unit, "samples": [{"t": float(t), "value": value} for t, value in zip(stamps or range(len(values)), values)]}]}


def anomaly_trials():
    records = []
    # Thresholds/window are fixed in production before these held-out seeds.
    for kind in ("step", "leak", "stall", "stationary", "noisy"):
        for seed in range(1000, 1020):
            rng = random.Random(seed)
            values = []
            for i in range(128):
                base = 70. + rng.gauss(0, .6)
                if kind == "step" and i >= 64: base += 20
                if kind == "stall" and i >= 64: base = 2 + rng.gauss(0, .2)
                if kind == "leak": base = 100 + max(0, i - 64) * 2 + rng.gauss(0, .05)
                if kind == "noisy": base = rng.uniform(0, 100)
                values.append(base * 2**20 if kind == "leak" else base)
            document = capture(values, metric="rss" if kind == "leak" else "gpu", unit="B" if kind == "leak" else "%", attempt=str(seed))
            result = A.analyze(document)
            after = [event for event in result["events"] if event["timestamp"] >= 64]
            positive = kind in {"step", "leak", "stall"}
            records.append({"kind": kind, "seed": seed, "positive": positive,
                "detected": bool(after) if positive else False,
                "delay_seconds": after[0]["timestamp"] - 64 if positive and after else None,
                "false_alerts": sum(event["timestamp"] < 64 or not positive for event in result["events"]),
                "samples": len(values), "ending_state": result["series"][0]["state"]})
    return {"trials": records, "recall": sum(row["detected"] for row in records if row["positive"]) / 60,
            "false_alerts_per_observation": sum(row["false_alerts"] for row in records) / sum(row["samples"] for row in records),
            "median_delay_seconds": statistics.median(row["delay_seconds"] for row in records if row["delay_seconds"] is not None)}


def phases(lengths, levels, seed, scale=1.):
    rng, values, labels, progress = random.Random(seed), [], [], []
    for phase, (size, level) in enumerate(zip(lengths, levels)):
        for i in range(size):
            values.append(level + 2 * math.sin(2 * math.pi * i / size) + rng.gauss(0, .1))
            labels.append(phase); progress.append(i / max(1, size - 1))
    return capture(values, metric="synthetic-signal", unit="arbitrary", attempt=str(seed), stamps=[i * scale for i in range(len(values))]), labels, progress


def phase_trials():
    records = []
    for seed in range(2000, 2020):
        rng = random.Random(seed)
        sizes = [rng.randint(24, 48) for _ in range(3)]
        a, al, ap = phases((32, 32, 32), (20, 80, 40), seed)
        b, bl, bp = phases(sizes, (20, 80, 40), seed + 100, scale=1.5)
        result = P.analyze({"schema": "raven.phase-comparison/v1", "reference": a, "candidate": b})
        expected = [sizes[0], sizes[0] + sizes[1]]
        observed = [item["start"] for item in result["candidate_segments"]][1:]
        boundary_error = sum(min(abs(value - target) for value in observed) if observed else len(bl) for target in expected) / len(expected)
        baseline_error = sum(min(abs(value - target) for value in range(32, len(bl), 32)) for target in expected) / len(expected)
        aligned = result["aligned_indices"]
        false = sum(al[pair["reference_index"]] != bl[pair["candidate_index"]] for pair in aligned)
        progress_error = statistics.mean(abs(ap[pair["reference_index"]] - bp[pair["candidate_index"]]) for pair in aligned) if aligned else None
        simple_pairs = [(i, round(i * (len(bl) - 1) / (len(al) - 1))) for i in range(len(al))]
        baseline_false = sum(al[i] != bl[j] for i, j in simple_pairs) / len(simple_pairs)
        records.append({"seed": seed, "candidate_lengths": sizes, "boundary_mean_absolute_error_samples": boundary_error,
            "fixed_window_boundary_mae_samples": baseline_error, "alignment_phase_progress_mae": progress_error,
            "false_correspondence_fraction": false / max(1, len(aligned)), "simple_time_alignment_false_fraction": baseline_false,
            "dtw_cells": result["dtw_cells"], "work_units": result["work_units"]})
    unrelated, _, _ = phases((32, 32, 32), (500, 600, 700), 9000)
    rejection = P.analyze({"schema": "raven.phase-comparison/v1", "reference": a, "candidate": unrelated})
    return {"trials": records, "unrelated_aligned_pairs": len(rejection["aligned_indices"])}


def reporting_example():
    # Execute the real example's numerical/reporting code with a capture adapter.
    # Wall times are measured; the example has one declared solve phase. This is
    # a local example workload, not labeled hardware telemetry or real HPC data.
    path = ROOT / "examples/automated-reporting/solve.py"
    spec = importlib.util.spec_from_file_location("reporting_solve", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    samples = []
    start = time.perf_counter()
    def record(*args):
        for i, arg in enumerate(args):
            if arg == "--value" and args[i + 1].startswith("absolute_error="):
                samples.append({"t": time.perf_counter() - start, "value": float(args[i + 1].split("=", 1)[1])})
    module.report = record
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        status = module.main(["--steps", "32", "--terms-per-step", "100"])
    doc = capture([], metric="absolute_error", unit="ratio", attempt="reporting-example")
    doc["series"][0]["samples"] = samples
    result = P.analyze({"schema": "raven.phase-comparison/v1", "reference": doc, "candidate": doc})
    return {"program": str(path.relative_to(ROOT)), "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "exit_status": status, "samples": len(samples), "declared_scientific_phases": 1,
            "statistical_segments": len(result["reference_segments"]),
            "meaning": "Numerical convergence can create statistical changes within one scientific phase; no hidden phase ground truth claimed",
            "input": doc}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = {"schema": "raven.signal-evaluation/v1", "qualification": "Synthetic held-out seeds and a local reporting example; deployment efficacy remains unproved",
        "parameters": {"window": A.WINDOW, "warmup": A.WARMUP, "threshold": 6, "phase_penalty": 4, "phase_minimum": 8},
        "development_seeds": [1, 2, 3, 4, 5], "heldout_seeds": {"anomalies": [1000, 1019], "phases": [2000, 2019]},
        "source_sha256": {name: hashlib.sha256((ROOT / "raven" / name).read_bytes()).hexdigest() for name in ("measurement_series.py", "stream_anomalies.py", "phase_alignment.py")},
        "anomalies": anomaly_trials(), "phases": phase_trials(), "reporting_example": reporting_example()}
    text = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output: args.output.write_text(text)
    else: print(text, end="")


if __name__ == "__main__":
    main()
