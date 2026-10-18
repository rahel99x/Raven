"""Equivalent idle manager fixture; no scheduler commands or test workloads.

Usage: python scripts/benchmark_allocation_recovery.py /absolute/source/root
Run each revision in a separate, otherwise quiet process.
"""
import json
import resource
import statistics
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, sys.argv[1])
from raven.raven_allocation import AllocationManager

config = {"site": {"kind": "slurm", "cluster_id": "benchmark"},
          "connection": {"transport": "local"},
          "execution": {"heavy_target": "slurm", "heavy_requires_allocation": True}}
with tempfile.TemporaryDirectory(prefix="raven-recovery-bench-") as path:
    manager = AllocationManager(config, state_dir=path)
    timings = []
    cpu = time.process_time()
    for _ in range(30000):
        start = time.perf_counter_ns()
        manager.tick()
        timings.append((time.perf_counter_ns() - start) / 1000.)
    cpu = time.process_time() - cpu
    tracemalloc.start()
    for _ in range(10000):
        manager.tick()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    manager.shutdown()
    print(json.dumps({"source": sys.argv[1], "fixture": "idle allocation manager, no backend, no scheduler commands", "count": len(timings),
                      "tick_median_us": statistics.median(timings), "tick_p95_us": sorted(timings)[int(.95 * (len(timings)-1))],
                      "loop_cpu_seconds": cpu, "python_peak_10000_ticks_bytes": peak,
                      "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}, indent=2))
