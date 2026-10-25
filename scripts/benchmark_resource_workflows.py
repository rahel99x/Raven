#!/usr/bin/env python3
"""Equivalent naive/shared source-inspection fixture; no scheduler is contacted."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import resource
import statistics
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from raven import science_files
from raven.engineering import optimization as O


def run(repeats=30):
    with tempfile.TemporaryDirectory(prefix="raven-resource-benchmark-") as directory:
        script = Path(directory) / "work.sh"
        script.write_text("#!/bin/bash\n#" + "x" * 65500 + "\necho science\n")
        variants = [dict(script=str(script), cpus=i + 1, memory_mib=64, wall_seconds=10) for i in range(8)]

        def naive():
            results = []
            for item in variants:
                reservation = O.reservation(item)
                workload = O._workload(item)
                record, _ = science_files.snapshot(item["script"], limit=1 << 20)
                results.append((reservation, workload, record))
            return results

        def shared():
            record, raw = science_files.snapshot(str(script), limit=1 << 20, keep=True)
            return [(O.reservation(item, _script_bytes=raw), O._workload(item, _source=record), record) for item in variants]

        assert naive() == shared(), "Inspection optimizations must preserve every result and source identity"
        def measure(callback, reads):
            for _ in range(3):
                callback()
            walls, cpus = [], []
            for _ in range(repeats):
                started, cpu = time.perf_counter(), time.process_time()
                callback()
                walls.append((time.perf_counter() - started) * 1000)
                cpus.append((time.process_time() - cpu) * 1000)
            tracemalloc.start()
            callback()
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            return dict(wall_median_ms=statistics.median(walls), cpu_median_ms=statistics.median(cpus),
                        source_opens=reads, source_bytes=reads * script.stat().st_size,
                        python_allocation_peak_bytes=peak)
        return dict(schema="raven.resource-workflow-benchmark/v1", fixture=dict(variants=8,
                    distinct_sources=1, source_bytes=script.stat().st_size, repetitions=repeats),
                    naive=measure(naive, 24), shared=measure(shared, 1),
                    process_rss_highwater_native_units=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    rss_unit="KiB on Linux; bytes on macOS; aggregate process high-water mark, not per algorithm",
                    qualification="Warm-cache local source inspection only; equivalent resource/source results asserted. No scheduler, SSH, application, billing, concurrency or hardware efficacy measurement.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--output")
    args = parser.parse_args()
    if not 3 <= args.repeats <= 1000:
        parser.error("repeats must be within 3..1000")
    document = run(args.repeats)
    raw = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(raw)
    print(raw, end="")


if __name__ == "__main__":
    main()
