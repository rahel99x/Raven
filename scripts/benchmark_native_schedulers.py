#!/usr/bin/env python3
"""Measure native JSON normalization without contacting a scheduler.

Uses the same source-format fixtures as the regression suite. Run in a source
checkout with development dependencies. Timing and allocation passes are
separate; whole-process peak RSS is not attributed to individual adapters.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import platform
import resource
import runpy
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fixture(original, name, count):
    if name in {"openpbs", "pbspro"}:
        sample = next(iter(original["Jobs"].values()))
        return {
            **original,
            "Jobs": {
                f"{index}.pbs.example": copy.deepcopy(sample)
                for index in range(1, count + 1)
            },
        }
    if name == "htcondor":
        return [
            {
                **original[0],
                "ClusterId": index,
                "GlobalJobId": f"schedd.example#{index}.4#1792929501",
            }
            for index in range(count)
        ]
    if name == "lsf":
        return {
            **original,
            "RECORDS": [
                {**original["RECORDS"][0], "JOBID": str(index)}
                for index in range(1, count + 1)
            ],
        }
    return {
        "jobs": [
            {
                key: value
                for key, value in {**original["jobs"][0], "id": index}.items()
                if key != "jobid"
            }
            for index in range(1, count + 1)
        ]
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args(argv)
    if not 1 <= args.records <= 1000 or not 3 <= args.repeats <= 100:
        parser.error("Use 1–1000 records and 3–100 repetitions")
    fixtures = runpy.run_path(str(ROOT / "tests/test_scheduler_native_batch.py"))
    results = []
    for name, original in fixtures["DOCUMENTS"].items():
        scheduler, _ = fixtures["adapter"](name)
        document = fixture(original, name, args.records)
        raw = json.dumps(document)
        decode_cpu, normalize_cpu, wall = [], [], []
        for _ in range(args.repeats):
            started = time.process_time_ns()
            parsed = json.loads(raw)
            decode_cpu.append((time.process_time_ns() - started) / 1e6)
            started_cpu, started_wall = time.process_time_ns(), time.perf_counter_ns()
            normalized = scheduler._parse(parsed)
            wall.append((time.perf_counter_ns() - started_wall) / 1e6)
            normalize_cpu.append((time.process_time_ns() - started_cpu) / 1e6)
            if len(normalized) != args.records:
                raise ValueError("Benchmark fixture lost native records")
        tracemalloc.start()
        scheduler._parse(json.loads(raw))
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        results.append(
            dict(
                scheduler=name,
                records=args.records,
                fixture_bytes=len(raw.encode()),
                repeats=args.repeats,
                json_decode_cpu_median_ms=statistics.median(decode_cpu),
                normalize_cpu_median_ms=statistics.median(normalize_cpu),
                normalize_wall_median_ms=statistics.median(wall),
                python_allocations=dict(retained_bytes=retained, peak_bytes=peak),
                scheduler_requests=scheduler.stats["requests"],
            )
        )
    source_paths = [
        ROOT / "raven/schedulers" / name
        for name in (
            "core.py",
            "batch_common.py",
            "pbs.py",
            "lsf.py",
            "htcondor.py",
            "flux.py",
        )
    ]
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = dict(
        schema="raven.native-batch-benchmark/v1",
        results=results,
        source_sha256={
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in source_paths
        },
        environment=dict(
            python=platform.python_version(), platform=platform.platform()
        ),
        process_peak_rss_bytes=rss * (1 if sys.platform == "darwin" else 1024),
        limitations=[
            "No equivalent native adapter existed before this change; no before/after gain is claimed.",
            "JSON decode is a reference cost, not equivalent previous behavior.",
            "Equivalent in-memory fixtures; timed regions contain no scheduler or disk I/O.",
            "Python allocation tracing is a separate untimed pass; process peak RSS includes all adapters, fixtures, and imports.",
            "Single shared cloud host; native fixture cost does not qualify live scheduler latency or production load.",
        ],
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
