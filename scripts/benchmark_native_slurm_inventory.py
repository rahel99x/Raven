#!/usr/bin/env python3
"""Compare native Slurm inventory envelopes with equivalent recorded fixtures.

Run without competing workloads:
    python scripts/benchmark_native_slurm_inventory.py \
        --output docs/benchmarks/native-slurm-inventory.json

This is an offline parser microbenchmark, not scheduler, network or UI
qualification. The full-record case represents the rejected inventory design;
the existing field normalizer is also reported so the added contract's cost is
visible. Complete raw records remain available for a selected job's detail.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import time
import tracemalloc
from types import SimpleNamespace

from benchmark_source import load_module, package_directory


QUEUE_FIELDS = (
    "id", "name", "user", "state", "cpus", "nodes", "gpus", "partition",
    "account", "submit", "start", "end", "elapsed", "workdir",
)
FIXTURE_ROOT = Path("tests/fixtures/slurm/recorded/26.05.5/final")


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes(root):
    return {
        str(path.relative_to(root)): file_hash(path)
        for path in sorted(package_directory(root).rglob("*.py"))
    }


def revision(root):
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True,
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def measured(function, rows, repeats):
    wall, cpu = [], []
    for _ in range(repeats):
        wall_start, cpu_start = time.perf_counter(), time.process_time()
        output = [function(row) for row in rows]
        wall.append((time.perf_counter() - wall_start) * 1000)
        cpu.append((time.process_time() - cpu_start) * 1000)
        del output
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        output = [function(row) for row in rows]
        current, peak = tracemalloc.get_traced_memory()
        del output
    finally:
        tracemalloc.stop()
    return {
        "records": len(rows),
        "repeats": repeats,
        "median_wall_ms": statistics.median(wall),
        "median_cpu_ms": statistics.median(cpu),
        "wall_ms_samples": wall,
        "cpu_ms_samples": cpu,
        "python_retained_bytes": current - before,
        "python_peak_additional_bytes": peak - before,
    }


def benchmark(root, records=500, repeats=5):
    root = Path(root).resolve()
    initial_sources = source_hashes(root)
    normalizer = load_module(root, "slurm_json")
    adapter = load_module(root, "schedulers.slurm")
    queue_path = root / FIXTURE_ROOT / "running-squeue-json.stdout.txt"
    history_path = root / FIXTURE_ROOT / "accounting-json.stdout.txt"
    source = json.loads(queue_path.read_text())["jobs"][0]
    client = adapter.SlurmScheduler(
        object(), {"cluster": source["cluster"], "user": source["user_name"]},
    )
    observed = time.time()
    original_clock = adapter.time
    # Freeze only the adapter's observation clock. The benchmark's wall/CPU
    # clocks and Python's process-wide time module remain unchanged.
    adapter.time = SimpleNamespace(time=lambda: observed)
    try:
        rows = []
        for index in range(records):
            row = copy.deepcopy(source)
            row["job_id"] = 1000 + index
            rows.append(row)
        for row in rows:
            baseline = normalizer.queue_record(row, observed)
            compact = client._record(row)
            for field in QUEUE_FIELDS:
                if compact[field] != getattr(baseline, field):
                    raise AssertionError("Normalized queue fidelity changed: " + field)
            if compact["resources"]["memory"]["requested"] != (baseline.mem_req or None):
                raise AssertionError("Native queue memory request changed")
            if client._record(row, full_native=True)["native"]["job"] != row:
                raise AssertionError("Selected detail changed the full native record")

        history = json.loads(history_path.read_text())["jobs"]
        for row in history:
            baseline = normalizer.accounting_record(row)
            compact = client._record(row, historical=True)
            if compact["resources"]["memory"]["requested"] != (baseline.req_mem or None):
                raise AssertionError("Historical requested memory changed")
            if compact["cpu_time"] != baseline.cpu_time or compact["exit_code"] != baseline.exit:
                raise AssertionError("Historical CPU time or exit code changed")
            if compact["rss"] != (baseline.rss or None):
                raise AssertionError("Historical peak RSS changed")

        scenarios = {
            "existing_field_normalizer": measured(
                lambda row: normalizer.queue_record(row, observed), rows, repeats,
            ),
            "full_native_inventory_candidate": measured(
                lambda row: client._record(row, full_native=True), rows, repeats,
            ),
            "compact_native_inventory": measured(client._record, rows, repeats),
        }
    finally:
        adapter.time = original_clock

    if source_hashes(root) != initial_sources:
        raise RuntimeError("Runtime source changed during the benchmark; discard this run")
    compact = scenarios["compact_native_inventory"]
    full = scenarios["full_native_inventory_candidate"]
    original = scenarios["existing_field_normalizer"]
    rss_unit = "KiB" if sys.platform.startswith("linux") else "B" if sys.platform == "darwin" else "platform native"
    return {
        "schema": "raven.native-slurm-inventory-benchmark/v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "revision": revision(root),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "fixture": {
            "slurm_version": "26.05.5", "records": records, "repeats": repeats,
            "observed_epoch": observed,
            "queue": {"path": str(queue_path.relative_to(root)), "sha256": file_hash(queue_path)},
            "history": {"path": str(history_path.relative_to(root)), "sha256": file_hash(history_path)},
        },
        "equivalence": {
            "queue_records_checked": records,
            "queue_fields": list(QUEUE_FIELDS),
            "queue_memory_native_request_checked": True,
            "selected_full_native_json_exact": True,
            "history_records_checked": len(history),
            "history_memory_cpu_rss_exit_checked": True,
            "unknown_memory_or_rss": "missing values remain unknown rather than invented zero",
        },
        "scenarios": scenarios,
        "comparisons": {
            "compact_wall_reduction_percent_vs_full_candidate": 100 * (1 - compact["median_wall_ms"] / full["median_wall_ms"]),
            "compact_python_retained_reduction_percent_vs_full_candidate": 100 * (1 - compact["python_retained_bytes"] / full["python_retained_bytes"]),
            "compact_wall_ratio_vs_existing_normalizer": compact["median_wall_ms"] / original["median_wall_ms"],
            "compact_python_retained_ratio_vs_existing_normalizer": compact["python_retained_bytes"] / original["python_retained_bytes"],
        },
        "process_rss": {
            "peak": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "unit": rss_unit,
            "scope": "Whole-process high water shared by all scenarios; cannot establish per-scenario RSS.",
        },
        "source_sha256": initial_sources,
        "harness_sha256": {
            "scripts/benchmark_native_slurm_inventory.py": file_hash(Path(__file__).resolve()),
            "scripts/benchmark_source.py": file_hash(Path(__file__).resolve().with_name("benchmark_source.py")),
        },
        "limits": [
            "Offline parser microbenchmark only; no scheduler, network, live wait, GPU, or UI measurement.",
            "Python allocations come from tracemalloc and are distinct from whole-process RSS.",
            "Full native inventory is the rejected design comparison, not a previous Raven release benchmark.",
            "The richer compact envelope costs more than the existing field normalizer, which the established Slurm workflow continues using.",
            "Run without competing workloads; results depend on this Python build and host.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--records", type=int, default=500)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.records <= 2000 or not 3 <= args.repeats <= 20:
        parser.error("Use 1..2000 records and 3..20 repeats")
    result = benchmark(args.source, args.records, args.repeats)
    encoded = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
        print(json.dumps({"output": str(args.output), "comparisons": result["comparisons"]}, indent=2))
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
