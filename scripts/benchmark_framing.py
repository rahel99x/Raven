#!/usr/bin/env python3
"""Controlled in-memory framing comparison; no scheduler or terminal latency claim.

Run each revision/channel in a fresh process, without competing tests. Timings
exclude tracemalloc; Python allocation peaks and process RSS are separate fields.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import resource
import statistics
import sys
import time
import tracemalloc

from benchmark_source import load_module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--channel", choices=("raven_protocol", "raven_allocation", "terminal_service"), required=True)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--fixture", choices=("common", "terminal-grid"), default="common")
    args = parser.parse_args()
    if not 1 <= args.iterations <= 10000 or not 1 <= args.repeats <= 20:
        parser.error("iterations/repeats exceed benchmark bounds")
    imported_at = time.perf_counter()
    module = load_module(args.source, args.channel)
    import_seconds = time.perf_counter() - imported_at
    encode = getattr(module, "encode", None) or module.frame
    documents = [{"op": "snapshot", "id": str(i), "generation": 2,
                  "rows": [[j, "sample", j / 10] for j in range(40)]} for i in range(32)]
    if args.fixture == "terminal-grid":
        if args.channel != "terminal_service":
            parser.error("terminal-grid is a terminal channel fixture")
        # Worst-style fragmentation for an ordinary 80-column terminal: one
        # segment per cell. This is transport-only, not a renderer benchmark.
        documents = [{"op": "snapshot", "id": str(i), "state": {
            "rows": 24, "cols": 80, "lines": [[["x", "red" if col % 2 else "blue"] for col in range(80)] for row in range(24)],
            "cursor": {"x": 0, "y": 0, "visible": True}, "history": [], "seq": i + 1}} for i in range(8)]
    wire = b"".join(encode(value) for value in documents)

    def operation():
        encoded = b"".join(encode(value) for value in documents)
        decoder = module.Decoder()
        for offset in range(0, len(encoded), 8192):
            decoder.feed(encoded[offset:offset + 8192])
        assert decoder.take(32) == documents

    operation()
    walls, cpus = [], []
    io_before = resource.getrusage(resource.RUSAGE_SELF)
    for _ in range(args.repeats):
        wall, cpu = time.perf_counter(), time.process_time()
        for _ in range(args.iterations):
            operation()
        walls.append(time.perf_counter() - wall)
        cpus.append(time.process_time() - cpu)
    tracemalloc.start()
    operation()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    package = Path(module.__file__).resolve().parent
    sources = [Path(module.__file__).resolve(), package / "framing.py", package / "transport/framing.py"]
    print(json.dumps({"channel": args.channel, "fixture": args.fixture, "source": str(args.source.resolve()), "frames_per_iteration": len(documents),
                      "source_sha256": {str(path.relative_to(args.source.resolve())): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources if path.exists()},
                      "wire_bytes_per_iteration": len(wire), "iterations": args.iterations, "repeats": args.repeats,
                      "wall_seconds": walls, "cpu_seconds": cpus,
                      "verified_source_import_seconds": import_seconds,
                      "frames_per_second_median": args.iterations * len(documents) / statistics.median(walls),
                      "python_peak_allocated_bytes": peak, "process_peak_rss_kib": usage.ru_maxrss,
                      "block_reads": usage.ru_inblock - io_before.ru_inblock, "block_writes": usage.ru_oublock - io_before.ru_oublock,
                      "limits": "In-memory transport fixture; no scheduler, GPU, SSH or end-user terminal latency measurement"}, indent=2))


if __name__ == "__main__":
    main()
