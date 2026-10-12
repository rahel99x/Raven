#!/usr/bin/env python3
"""Measure retained-data export time, bounded memory, correctness, and cancellation.

Each size runs in a fresh child. Generated input is explicitly synthetic and is
excluded from timing/tracemalloc. Outputs are validated by Python's csv, json,
and hashlib modules, independently of Raven's writers. No scheduler or device
commands are used. Run from an installed Raven environment or the source tree.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import platform
import resource
import subprocess
import sys
import tempfile
import time
import tracemalloc

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if (SOURCE_ROOT / "raven").is_dir():
    sys.path.insert(0, str(SOURCE_ROOT))


def fixture(root: Path, rows: int, metrics: int) -> dict:
    from raven.reporting import begin_run, finish_run

    (root / "synthetic.py").write_text("# Generated export benchmark; no scientific claims.\n", encoding="utf-8")
    run = begin_run(root, "synthetic-benchmark", name="synthetic export benchmark", script="synthetic.py")
    with (run / "metrics.jsonl").open("w", encoding="utf-8") as stream:
        for step in range(rows):
            values = {f"measurement_{metric}": (step + metric) / 17.0 for metric in range(metrics)}
            row = {"t": 1_700_000_000.0 + step / 8.0, "step": step,
                   "phase": "synthetic", "metrics": values}
            stream.write(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n")
    finish_run(run, state="COMPLETED", runtime_seconds=1.0, exit_code=0)
    return {"project": str(root), "runs": [run.name], "include": ["application"], "layout": "category"}


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(receipt: dict, rows: int, metrics: int) -> dict:
    actual_rows = 0
    files_checked = 0
    byte_count = 0
    for output in receipt["outputs"]:
        root = Path(output)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["files"]:
            path = root / entry["path"]
            if checksum(path) != entry["sha256"]:
                raise RuntimeError(f"Checksum disagreement for {path}")
            if path.stat().st_size != entry["bytes"]:
                raise RuntimeError(f"Byte count disagreement for {path}")
            files_checked += 1
            byte_count += path.stat().st_size
            if path.suffix == ".csv":
                with path.open(newline="", encoding="utf-8") as stream:
                    for row in csv.DictReader(stream):
                        expected_metric = int(row["metric"].removeprefix("measurement_"))
                        if row["run_id"] != "synthetic-benchmark":
                            raise RuntimeError("Export changed run identity")
                        if float(row["value"]) != (int(row["step"]) + expected_metric) / 17.0:
                            raise RuntimeError("Export changed measurement precision or value")
                        actual_rows += 1
    if actual_rows != rows * metrics:
        raise RuntimeError(f"Expected {rows * metrics} CSV observations, received {actual_rows}")
    return {"csv_rows_verified": actual_rows, "files_verified": files_checked, "export_bytes": byte_count}


def measure(rows: int, metrics: int, layout: str = "category") -> dict:
    from raven.data_export import export_data, plan_export

    with tempfile.TemporaryDirectory(prefix="raven-export-benchmark-") as temp:
        root = Path(temp)
        request = fixture(root, rows, metrics)
        request["layout"] = layout
        source_bytes = (root / "runs/synthetic-benchmark/metrics.jsonl").stat().st_size
        tracemalloc.start()
        started = time.perf_counter()
        plan = plan_export(request)
        planned = time.perf_counter()
        receipt = export_data(plan)
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        if receipt["status"] != "complete":
            raise RuntimeError(f"Benchmark export was {receipt['status']}: {receipt.get('issues')}")
        verified = validate(receipt, rows, metrics)
        probes = 0
        cancelled_at = None

        def cancel():
            nonlocal probes, cancelled_at
            probes += 1
            if probes >= 20:
                if cancelled_at is None:
                    cancelled_at = time.perf_counter()
                return True
            return False

        cancellation_started = time.perf_counter()
        cancelled = export_data(request, cancel=cancel)
        cancellation_finished = time.perf_counter()
        if cancelled_at is None or cancelled["status"] != "cancelled":
            raise RuntimeError("Export did not observe the requested cancellation")
        return {"input_rows": rows, "metrics_per_record": metrics, "layout": layout, "source_bytes": source_bytes,
                "plan_seconds": planned - started, "elapsed_seconds": elapsed,
                "observations_per_second": rows * metrics / elapsed,
                "tracemalloc_peak_bytes": peak,
                "process_max_rss_platform_units": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "cancellation_seconds_after_observed_request": cancellation_finished - cancelled_at,
                "cancellation_total_seconds": cancellation_finished - cancellation_started,
                "cancellation_checks": probes, **verified}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, nargs="+", default=[1000, 100000])
    parser.add_argument("--metrics", type=int, default=2)
    parser.add_argument("--layout", choices=("category", "metric"), default="category")
    parser.add_argument("--output", type=Path, help="new JSON report path; never overwrite an existing file")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if any(value < 100 for value in args.rows) or not 1 <= args.metrics <= 64:
        parser.error("choose at least 100 rows and 1–64 metric names per record")
    if args.child:
        print(json.dumps(measure(args.rows[0], args.metrics, args.layout), allow_nan=False))
        return 0
    measurements = []
    for rows in args.rows:
        command = [sys.executable, str(Path(__file__).resolve()), "--child", "--rows", str(rows),
                   "--metrics", str(args.metrics), "--layout", args.layout]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        measurements.append(json.loads(result.stdout))
    from raven import data_export
    report = {"schema": "raven.export-benchmark/v1", "python": platform.python_version(),
              "exporter_sha256": checksum(Path(data_export.__file__)),
              "platform": platform.platform(), "measurements": measurements,
              "notes": ["Synthetic application records; no Slurm or GPU measurements.",
                        "Tracemalloc includes planning/export, excludes input generation and validation.",
                        "Max RSS includes the entire fresh child; its units depend on the platform.",
                        "Measurements describe this machine and filesystem only."]}
    rendered = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
