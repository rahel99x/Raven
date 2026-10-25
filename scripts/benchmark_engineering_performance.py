#!/usr/bin/env python3
"""Measure investigation composition against the same existing trace analyses.

Run each mode in a fresh process. Timing excludes tracemalloc; allocation peaks
are measured in a separate invocation after timing. No scheduler is contacted.
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raven import evidence  # noqa: E402
from raven.engineering import performance  # noqa: E402
from raven.tracing import workspace_analysis as analysis  # noqa: E402
from raven.tracing import workspace_model as model  # noqa: E402


def fixture(count):
    identity = {"cluster": "fixture", "job_id": "42", "attempt": "bench-1"}
    rows = []
    for number in range(count):
        common = {
            "source_id": "s",
            "clock_id": "c",
            "start_ns": number * 1000,
            "end_ns": number * 1000 + 900,
            "duration_ns": 900,
            "thread": str(number % 8),
            "depth": 0,
        }
        rows.append(
            {
                **common,
                "id": "call-" + str(number),
                "name": "solve-" + str(number % 16),
                "kind": "python",
                "data": {},
                "self_ns": 900,
            }
        )
        rows.append(
            {
                **common,
                "id": "cpu-" + str(number),
                "name": "running",
                "kind": "scheduling",
                "data": {"state": "running", "logical_cpu": number % 8},
            }
        )
    return {
        "identity": identity,
        "intervals": rows,
        "markers": [],
        "caught_up": True,
        "sources": [
            {
                "id": "s",
                "identity": {
                    **identity,
                    "node": "n",
                    "boot_id": "boot",
                    "pid": 1,
                    "process_start": "p",
                },
                "clock": {"id": "c"},
                "validated": True,
                "status": "COMPLETE",
            }
        ],
    }


def baseline(query):
    return {
        "resources": model.resource_metrics(query),
        "functions": model.function_statistics(query, limit=100),
        "hints": analysis.bottleneck_hints(query),
        "counters": analysis.counter_lens(query),
        "flows": model.explicit_flows(query, limit=2000),
    }


def duplicate_validation(query):
    """Reproduce the first implementation's redundant full serialization."""
    evidence.canonical(query)
    return performance.analyze(query)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("baseline", "duplicate-validation", "service"))
    parser.add_argument("--calls", type=int, default=1000)
    parser.add_argument("--iterations", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.calls <= 2000 or not 3 <= args.iterations <= 200:
        parser.error("Choose 1–2000 calls and 3–200 iterations")
    query = fixture(args.calls)
    reference = baseline(query)
    delivered = performance.analyze(query)
    assert all(reference[key] == delivered[key] for key in reference), "Analysis fidelity differs"
    callback = {
        "baseline": baseline,
        "service": performance.analyze,
        "duplicate-validation": duplicate_validation,
    }[args.mode]
    callback(query)
    durations = []
    started = time.process_time()
    for _ in range(args.iterations):
        before = time.perf_counter()
        callback(query)
        durations.append((time.perf_counter() - before) * 1000)
    cpu_ms = (time.process_time() - started) * 1000 / args.iterations
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mib = rss / (1 << 20) if sys.platform == "darwin" else rss / 1024
    tracemalloc.start()
    callback(query)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    durations.sort()
    print(
        json.dumps(
            {
                "mode": args.mode,
                "fixture_calls": args.calls,
                "fixture_events": 2 * args.calls,
                "iterations": args.iterations,
                "wall_median_ms": statistics.median(durations),
                "wall_p95_ms": durations[min(len(durations) - 1, int(0.95 * len(durations)))],
                "cpu_mean_ms": cpu_ms,
                "process_high_water_rss_mib": rss_mib,
                "python_peak_allocated_mib": peak / (1 << 20),
                "scheduler_commands": 0,
                "fidelity": "all five pre-existing analysis blocks identical",
                "limits": "Local synthetic observations, no live cluster, terminal rendering, device or remote I/O; RSS includes imports, fixture and fidelity checks; tracemalloc peak measured separately from timing.",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
