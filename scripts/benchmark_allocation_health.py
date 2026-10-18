#!/usr/bin/env python3
"""Measure the opt-in local health kernel's cost; never infer node efficacy."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CHILD = r'''
import json, os, resource, socket, sys, tempfile, time
from pathlib import Path
mode, tracked, mpi_prefix = sys.argv[1], sys.argv[2] == "tracked", sys.argv[3]
def io():
    return {key: int(value) for key, value in (line.split(":") for line in Path("/proc/self/io").read_text().splitlines())}
with tempfile.TemporaryDirectory(prefix="raven-health-cost-") as scratch:
    before = io(); child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = time.process_time(); wall = time.perf_counter()
    if tracked:
        import tracemalloc
        tracemalloc.start()
    receipt = None
    if mode != "disabled":
        from raven import allocation_health as H
        execution = {"site_kind": "desktop", "host": socket.gethostname(), "uid": os.getuid(), "pid": os.getpid(),
                     "verified": True, "verification": "explicit local kernel-cost fixture; not allocation qualification",
                     "verified_resources": {"cpus": 2, "memory_mib": 1024}}
        mpi = None
        if mode == "mpi":
            from raven import mpi_health as M
            mpi = M.review(execution, mpi_prefix)
        receipt = H.probe({"tests": mode, "bytes": 1 << 20, "repeats": 3, "warmup": 1, "seconds": 10},
                          execution, H.scratch_identity(scratch) if "scratch" in mode else None, mpi=mpi)
        assert receipt["status"] == "completed"
    allocations = tracemalloc.get_traced_memory()[1] if tracked else None
    elapsed, consumed = time.perf_counter() - wall, time.process_time() - cpu
    after = io(); child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    assert not list(Path(scratch).iterdir())
    print(json.dumps({"mode": mode, "tracked_python_allocations": tracked, "wall_seconds": elapsed, "cpu_seconds": consumed,
        "process_max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "reaped_children_cpu_seconds": child_after.ru_utime + child_after.ru_stime - child_before.ru_utime - child_before.ru_stime,
        "reaped_child_peak_rss_kib": child_after.ru_maxrss,
        "reaped_children_input_blocks": child_after.ru_inblock - child_before.ru_inblock,
        "reaped_children_output_blocks": child_after.ru_oublock - child_before.ru_oublock,
        "python_peak_allocated_bytes": allocations, "io_delta": {key: after[key] - before[key] for key in after},
        "receipt": receipt, "scratch_cleaned": True}))
'''

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mpi-prefix", default="", help="Explicit trusted Open MPI 5 development fixture; adds a same-host MPI observation")
    args = parser.parse_args()
    rows = []
    for tracked in (False, True):
        for trial in range(3 if not tracked else 1):
            for mode in ("disabled", "memory", "scratch", "memory,scratch") + (("mpi",) if args.mpi_prefix else ()):
                result = subprocess.run([sys.executable, "-c", CHILD, mode, "tracked" if tracked else "plain", args.mpi_prefix],
                    cwd=ROOT, capture_output=True, text=True, check=True, timeout=15)
                rows.append(dict(json.loads(result.stdout), trial=trial))
    report = {"schema": "raven.allocation-health-cost/v1", "rows": rows,
        "scope": "Equivalent fresh local Python processes, explicit fixed 1MiB/3-repeat/1-warmup kernel; imports included",
        "limits": ["Not scheduler, multi-node, GPU or sick-node efficacy qualification", "Scratch I/O includes page cache",
                   "Python allocation tracing uses separate runs and must not be conflated with process RSS",
                   "Process RSS includes interpreter startup; /proc I/O counters include metadata/imports",
                   "Child peak RSS is a maximum of reaped child observations, not simultaneous process-tree RSS; child CPU is separate from parent CPU",
                   "No scientific workload was running"],
        "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (ROOT / "raven/allocation_health.py", ROOT / "raven/mpi_health.py", ROOT / "raven/diagnostic_trials.py", Path(__file__).resolve())}}
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2); stream.write("\n")
    print(json.dumps({"output": str(args.output), "samples": len(rows)}))

if __name__ == "__main__":
    main()
