#!/usr/bin/env python3
"""Bounded signal scaling comparison; run cases in isolated fresh processes."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import resource
import statistics
import sys
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from raven import measurement_series as M, phase_alignment as P, stream_anomalies as A


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=512)
    parser.add_argument("--features", type=int, default=4)
    args = parser.parse_args()
    if not 32 <= args.samples <= M.MAX_SAMPLES or not 1 <= args.features <= 8:
        parser.error("samples/features outside analysis bounds")
    document = {"schema": M.SCHEMA, "series": [{"identity": {"scope": "synthetic", "attempt": "benchmark", "job": "7", "verified": True},
        "metric": f"feature-{f}", "unit": "count", "samples": [{"t": i, "value": (i // max(1, args.samples // 4)) * 30 + math.sin(i / 10) + f} for i in range(args.samples)]} for f in range(args.features)]}
    comparison = {"schema": "raven.phase-comparison/v1", "reference": document, "candidate": document}
    def fixed_windows():
        # Transparent cheap baseline: fixed equal-size windows, identity timing.
        width = max(8, args.samples // 4)
        return [(i, min(args.samples, i + width)) for i in range(0, args.samples, width)]
    functions = {"stream_robust_detector": lambda: A.analyze(document), "segmentation_alignment": lambda: P.analyze(comparison), "fixed_window_baseline": fixed_windows}
    result = {"samples_per_feature": args.samples, "features": args.features, "repeats": 3,
              "source_sha256": {name: hashlib.sha256((ROOT / "raven" / name).read_bytes()).hexdigest() for name in ("measurement_series.py", "stream_anomalies.py", "phase_alignment.py")}}
    for name, operation in functions.items():
        wall, cpu = [], []
        for _ in range(3):
            w, c = time.perf_counter(), time.process_time(); operation()
            wall.append(time.perf_counter() - w); cpu.append(time.process_time() - c)
        tracemalloc.start(); output = operation(); _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
        result[name] = {"wall_seconds": wall, "cpu_seconds": cpu, "python_peak_bytes": peak}
        if name == "segmentation_alignment": result[name].update(work_units=output["work_units"], dtw_cells=output["dtw_cells"])
    result["process_peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result["limits"] = "In-memory synthetic fixture; fixed windows are cheaper but are not change detection. Process RSS includes fixture and all algorithms; Python allocations measured separately. No scheduler/GPU/deployment measurement."
    print(json.dumps(result, indent=2))


if __name__ == "__main__": main()
