#!/usr/bin/env python3
"""Measure validated experiment summaries without scheduler or filesystem I/O.

Timing excludes tracing allocations. The separate tracemalloc sample reports
Python allocations; process peak RSS includes imports and the fixture. This is
a new-capability baseline, not an invented comparison with a prior feature.
"""

from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
import platform
import resource
import statistics
import sys
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raven.engineering import _storage as S, experiments as X  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=15)
    args = parser.parse_args(argv)
    if not 1 <= args.attempts <= 1000 or not 3 <= args.repeats <= 100:
        parser.error("Use 1–1000 attempts and 3–100 repetitions")
    attempts = [X._attempt(None, dict(scheduler="slurm", scope="fixture-site", native_id=str(index),
                                    attempt="2026-10-25/0"),
                           dict(cpu_seconds=12, allocated_cpu_seconds=24, source="fixture allocation summary",
                                scope="allocation", complete=True), "submission", None)
                for index in range(args.attempts)]
    for attempt in attempts:
        attempt["observed"] = 1.
    document = dict(schema=X.SCHEMA, experiment_id="fixed-fixture", title="Fixture", revision=1,
                    created=1., updated=1., attempts=attempts)
    raw = S.encoded(document)
    samples = []
    cpu_start = time.process_time()
    for _ in range(args.repeats):
        start = time.perf_counter()
        result = X.summarize(document)
        samples.append((time.perf_counter() - start) * 1000)
    cpu_seconds = time.process_time() - cpu_start
    gc.collect()
    tracemalloc.start()
    X.summarize(document)
    retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_native = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = dict(schema="raven.engineering-lineage-benchmark/v1",
                  fixture=dict(attempts=args.attempts, json_bytes=len(raw), repeats=args.repeats,
                               known_cpu_seconds=result["totals"]["cpu_seconds"]["known_sum"]),
                  summary_ms=dict(median=statistics.median(samples), minimum=min(samples), maximum=max(samples)),
                  total_process_cpu_seconds=cpu_seconds,
                  python_allocations=dict(retained_bytes=retained, peak_bytes=peak),
                  process_peak_rss_bytes=rss_native * (1 if sys.platform == "darwin" else 1024),
                  environment=dict(python=platform.python_version(), platform=platform.platform()),
                  scheduler_requests=0,
                  limitations=["New capability: no equivalent previous implementation; no before/after gain claimed.",
                               "Warm in-process validated summary; no disk or scheduler latency.",
                               "Process peak RSS includes interpreter/imports/fixture and is separate from Python allocation peak.",
                               "Single cloud host; not interactive-terminal or production-cluster qualification."])
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
