#!/usr/bin/env python3
"""Measure bounded metric snapshots on an explicit checkout without a scheduler.

Run each checkout/mode in a separate process. The fixed fixture contains 64
series with 600 observations each; source creation and the initial read are
outside timing. This is a diagnostic, not a pass/fail performance threshold.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path

from benchmark_source import load_module


def repetitions(value):
    number = int(value)
    if not 1 <= number <= 10000:
        raise argparse.ArgumentTypeError("repeats must be between 1 and 10000")
    return number


def run(source_root, mode, repeats=60):
    source_root = Path(source_root).resolve()
    module = load_module(source_root, "metrics")
    if mode == "sidecar" and not hasattr(module, "read_meta"):
        raise ValueError("the selected checkout does not support metric metadata")
    with tempfile.TemporaryDirectory(prefix="raven-metric-benchmark-") as directory:
        path = Path(directory) / "metrics.jsonl"
        path.write_text("".join(json.dumps({
            "t": i + 1, "metrics": {f"m{j}": 1 / (i + 1) for j in range(64)}
        }) + "\n" for i in range(600)), encoding="utf-8")
        if mode == "sidecar":
            path.with_suffix(".meta.json").write_text(json.dumps({
                "schema": "raven.metrics-meta/v1", "client": {"api_version": 1},
                "kinds": {f"m{j}": "residual" for j in range(64)},
                "coalesced": {}, "rank_policy": {"mode": "coordinator", "rank": 0},
            }), encoding="utf-8")
        reader = module.MetricReader(max_bytes=8 << 20)
        reader.read(path)
        times = []
        cpu_start = time.process_time()
        for _ in range(repeats):
            started = time.perf_counter()
            result = reader.read(path)
            times.append((time.perf_counter() - started) * 1000)
        median = statistics.median(times)
        p95 = sorted(times)[math.ceil(.95 * repeats) - 1]
        cpu_total = (time.process_time() - cpu_start) * 1000
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KiB; macOS reports bytes. Neither is Python-only memory.
        peak_rss_kib = peak_rss / 1024 if sys.platform == "darwin" else peak_rss
        retained = sum(map(len, result["series"].values()))
        if retained != 38400 or result.get("status") != "ok":
            raise ValueError("benchmark did not preserve the complete metric fixture")
        if mode == "sidecar" and len(result.get("kinds", {})) != 64:
            raise ValueError("benchmark did not load all 64 metric kind declarations")
    # Hash after measurement so provenance collection cannot inflate timing.
    source_hashes = {}
    for name in ("metrics.py", "projects.py", "remote.py"):
        file = Path(module.__file__).parent / name
        if file.is_file():
            source_hashes["raven/" + name] = hashlib.sha256(file.read_bytes()).hexdigest()
    return {
        "source": str(source_root), "mode": mode, "median_ms": median,
        "p95_ms": p95, "cpu_total_ms": cpu_total,
        "process_peak_rss_kib": peak_rss_kib,
        "fixture": f"64series*600points;unchangedsource;{repeats}reads",
        "retained": retained, "repeats": repeats,
        "python": sys.version, "platform": platform.platform(),
        "source_sha256": source_hashes,
        "measurement": "warm unchanged-stream snapshots; elapsed time and process CPU in milliseconds",
        "limits": "local temporary filesystem; process peak RSS includes imports, fixture setup and native memory; Python allocation peaks are not measured",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path, help="checkout whose raven package is measured")
    parser.add_argument("mode", choices=("legacy", "sidecar"), help="omit or include residual-kind metadata")
    parser.add_argument("--repeats", type=repetitions, default=60)
    parser.add_argument("--output", type=Path, help="also save the JSON report to this file")
    args = parser.parse_args(argv)
    try:
        result = run(args.source_root, args.mode, args.repeats)
    except (OSError, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")
    rendered = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
