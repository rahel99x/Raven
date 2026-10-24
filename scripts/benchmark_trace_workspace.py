#!/usr/bin/env python3
"""Compare indexed workspace queries with the existing retained timeline.

Both variants consume the same verified synthetic journals. The legacy variant
is only whole-recording comparable while every source fits its retained window.
Each timed or allocation-tracked sample runs in a fresh child process. Results
are local reader/index costs, not capture overhead or live scheduler/GPU efficacy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc

SCHEMA = "raven.trace-workspace-benchmark/v1"


def _digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1 << 20), b""):
            checksum.update(data)
    return checksum.hexdigest()


def _rss_bytes():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def _io():
    try:
        text = Path("/proc/self/io").read_text()
    except OSError:
        return None
    result = {}
    for line in text.splitlines():
        key, value = line.split(":", 1)
        if key in {"rchar", "wchar", "read_bytes", "write_bytes"}:
            result[key] = int(value.strip())
    return result


def _environment():
    limits = {}
    for name in ("cpu.max", "memory.max"):
        try:
            with (Path("/sys/fs/cgroup") / name).open() as stream:
                limits[name] = stream.read(4096).strip()
        except OSError:
            limits[name] = None
    return {
        "machine": platform.machine(),
        "logical_cpu_count": os.cpu_count(),
        "cpu_affinity": sorted(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
        "cgroup_v2_limits": limits,
        "isolation": "Not enforced by this harness; run without competing heavy work for controlled measurements",
    }


def _source(root):
    paths = sorted((root / "raven/tracing").glob("*.py"))
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain", "--", "raven/tracing"], cwd=root
            )
        )
    except (OSError, subprocess.CalledProcessError):
        revision, dirty = None, None
    return {
        "root": str(root),
        "revision": revision,
        "tracing_dirty": dirty,
        "sha256": {str(path.relative_to(root)): _digest(path) for path in paths},
    }


def _fixtures(root, intervals, streams, threads, nodes):
    from raven.tracing.capture import Recorder
    from raven.tracing.protocol import read_json

    paths, records = [], []
    base = time.monotonic_ns()
    for source in range(streams):
        with Recorder(
            root,
            job_id="workspace-benchmark",
            attempt="deterministic-fixture",
            cluster="fixture",
            stream_id=f"workspacebench{source:04d}",
            identity_extra={
                "node": f"fixture-node-{source % nodes}",
                "boot_id": f"fixture-boot-{source % nodes}",
            },
            coverage={
                "adapter": "synthetic-workspace-benchmark",
                "scope": "Deterministic software fixture; no actual workload or topology observation",
            },
            max_bytes=max(64 << 20, intervals * 2048),
            flush_events=1024,
            flush_interval=60,
            background_flush=False,
            journal_format="blocks",
            initial_mono_ns=base,
            register=False,
        ) as recorder:
            recorder.emit(
                "call",
                {"function": "outer", "frame": "outer"},
                thread="thread-0",
                mono_ns=base + 1,
            )
            for number in range(intervals):
                start = base + 100 + number * 200
                thread = f"thread-{number % threads}"
                frame = f"phase-{number % 16}"
                recorder.emit(
                    "call",
                    {"function": frame, "frame": frame},
                    thread=thread,
                    mono_ns=start,
                )
                recorder.emit(
                    "return",
                    {"function": frame, "frame": frame},
                    thread=thread,
                    mono_ns=start + 1 + number % 37,
                )
            recorder.emit(
                "return",
                {"function": "outer", "frame": "outer"},
                thread="thread-0",
                mono_ns=base + intervals * 200 + 100,
            )
            paths.append(str(recorder.path))
        manifest = read_json(recorder.path)
        if manifest["state"] != "CLOSED" or manifest.get("dropped", 0):
            raise RuntimeError("The benchmark fixture did not commit completely")
        records.append(
            {
                "path": str(recorder.path),
                "identity": manifest["identity"],
                "committed_events": manifest["committed_seq"],
                "journal_bytes": manifest["committed_bytes"],
                "manifest_sha256": _digest(recorder.path),
                "journal_sha256": _digest(recorder.directory / "events.jsonl"),
            }
        )
    return {
        "paths": paths,
        "sources": records,
        "base_ns": base,
        "intervals_per_source": intervals,
        "streams": streams,
        "threads_per_source": threads,
        "synthetic_nodes": nodes,
        "expected_intervals": streams * (intervals + 1),
    }


def _queries(fixture, count):
    length = fixture["intervals_per_source"] * 200
    width = min(20_000, max(1, length // 32))
    room = max(1, length - width)
    return [
        (
            fixture["base_ns"] + (index * 104729 % room),
            fixture["base_ns"] + (index * 104729 % room) + width,
        )
        for index in range(count)
    ]


def _canonical(rows, sources):
    names = {row["id"]: row["identity"]["stream"] for row in sources}
    result = []
    for row in rows:
        stream = row.get("stream") or names.get(row.get("source_id"))
        if stream is None:
            raise RuntimeError("Query response omitted its source identity")
        result.append(
            (
                stream,
                row["first_seq"],
                row["last_seq"],
                row["kind"],
                row["thread"],
                row["name"],
                row["start_ns"],
                row["end_ns"],
                row["duration_ns"],
            )
        )
    return hashlib.sha256(
        json.dumps(sorted(result), separators=(",", ":")).encode()
    ).hexdigest()


def _legacy(paths):
    from raven.tracing.protocol import TraceReader
    from raven.tracing.timeline import build

    models = []
    for path in paths:
        reader = TraceReader(path)
        snapshot = None
        for snapshot in reader.iter_pages(max_events=512, max_bytes=256 << 10):
            if snapshot["status"] == "INVALID":
                raise ValueError("Invalid benchmark source")
        model = build(snapshot)
        models.append(model)
    comparable = all(model["evicted_events"] == 0 for model in models)
    return models, comparable


def _legacy_query(models, start, end):
    rows = []
    for model in models:
        for value in model["intervals"]:
            left, right = (
                value["start_ns"] + model["origin_ns"],
                value["end_ns"] + model["origin_ns"],
            )
            # Both variants answer the same inclusive overlap predicate. A
            # call ending exactly at the selected start remains inspectable;
            # comparing a half-open scan to an inclusive index is unequal work.
            if left <= end and right >= start:
                rows.append(
                    dict(
                        value,
                        start_ns=left,
                        end_ns=right,
                        stream=model["identity"]["stream"],
                    )
                )
    return rows


def _child(args):
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    fixture = json.loads(args.fixtures.read_text())
    if args.measurement == "allocation":
        tracemalloc.start()
    before_io = _io()
    wall, cpu = time.perf_counter(), time.process_time()
    index, models = None, None
    index_directory = tempfile.TemporaryDirectory(prefix="raven-workspace-bench-index-")
    try:
        ingest_wall, ingest_cpu = time.perf_counter(), time.process_time()
        if args.variant == "legacy-retained":
            models, comparable = _legacy(fixture["paths"])
            catalog = {"sources": []}
            count = sum(len(model["intervals"]) for model in models)
        else:
            from raven.tracing.workspace_index import TraceIndex

            index = TraceIndex(
                Path(index_directory.name) / "workspace.sqlite",
                max_bytes=args.index_mib << 20,
            )
            deadline = time.monotonic() + 300
            while True:
                catalog = index.refresh(
                    fixture["paths"],
                    expected={
                        "job_id": "workspace-benchmark",
                        "attempt": "deterministic-fixture",
                        "cluster": "fixture",
                    },
                    max_events=4096,
                    max_seconds=0.25,
                )
                if catalog["caught_up"]:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Index fixture ingestion exceeded 300 seconds")
            comparable, count = True, None
        ingest = {
            "wall_seconds": time.perf_counter() - ingest_wall,
            "cpu_seconds": time.process_time() - ingest_cpu,
        }
        if index is not None:
            proof = index.functions(limit=1000)
            if proof.get("truncated"):
                raise RuntimeError("Benchmark function proof exceeded its group budget")
            count = sum(row["count"] for row in proof["functions"])
        if comparable and count != fixture["expected_intervals"]:
            raise RuntimeError(
                "Whole-fixture ingestion omitted or duplicated matched intervals"
            )
        query_samples, results = [], []
        for start, end in _queries(fixture, args.queries):
            began, used = time.perf_counter(), time.process_time()
            if index is None:
                rows = _legacy_query(models, start, end)
            else:
                response = index.query(start_ns=start, end_ns=end, limit=2000)
                if response.get("truncated"):
                    raise RuntimeError(
                        "Benchmark query exceeded its recorded result budget"
                    )
                rows = response["intervals"]
            query_samples.append(
                {
                    "wall_seconds": time.perf_counter() - began,
                    "cpu_seconds": time.process_time() - used,
                }
            )
            results.append(
                {
                    "start_ns": start,
                    "end_ns": end,
                    "count": len(rows),
                    "sha256": _canonical(rows, catalog["sources"]),
                }
            )
        overview = None
        if index is not None:
            began, used = time.perf_counter(), time.process_time()
            overview_result = index.overview(bins=256)
            overview = {
                "wall_seconds": time.perf_counter() - began,
                "cpu_seconds": time.process_time() - used,
                "response_bytes": len(json.dumps(overview_result)),
            }
            index.close()
        after_io = _io()
        python_current = python_peak = None
        if args.measurement == "allocation":
            python_current, python_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        operation_wall = time.perf_counter() - wall
        operation_cpu = time.process_time() - cpu
        result = {
            "variant": args.variant,
            "measurement": args.measurement,
            "timing_eligible": args.measurement == "time",
            "source": _source(root),
            "whole_fixture_comparable": comparable,
            "matched_interval_count": count,
            "expected_interval_count": fixture["expected_intervals"],
            "ingest": ingest,
            "queries": query_samples,
            "query_answers": results,
            "overview": overview,
            "whole_operation_wall_seconds": operation_wall,
            "whole_operation_cpu_seconds": operation_cpu,
            "process_peak_rss_bytes": _rss_bytes(),
            "python_traced_current_bytes": python_current,
            "python_traced_peak_bytes": python_peak,
            "index_disk_bytes": sum(
                path.stat().st_size
                for path in Path(index_directory.name).glob("*")
                if path.is_file()
            ),
            "process_io_bytes": None
            if before_io is None or after_io is None
            else {key: after_io[key] - before_io[key] for key in before_io},
            "scope": "Whole verified fixture"
            if comparable
            else "Retained suffix only; unsupported as a whole-recording baseline",
        }
        print(json.dumps(result))
    finally:
        if index is not None:
            index.close()
        index_directory.cleanup()


def _summary(samples):
    result = {}
    for variant in ("legacy-retained", "indexed-workspace"):
        timed = [
            sample
            for sample in samples
            if sample["variant"] == variant and sample["measurement"] == "time"
        ]
        query = [row["wall_seconds"] for sample in timed for row in sample["queries"]]
        ordered = sorted(query)
        result[variant] = {
            "whole_fixture_comparable": all(
                x["whole_fixture_comparable"] for x in timed
            ),
            "ingest_median_seconds": statistics.median(
                x["ingest"]["wall_seconds"] for x in timed
            ),
            "query_median_seconds": statistics.median(query),
            "query_p95_seconds": ordered[math.ceil(len(ordered) * 0.95) - 1],
            "process_peak_rss_bytes": max(x["process_peak_rss_bytes"] for x in timed),
            "python_allocation_peak_bytes": next(
                x["python_traced_peak_bytes"]
                for x in samples
                if x["variant"] == variant and x["measurement"] == "allocation"
            ),
        }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--intervals", type=int, default=5000)
    parser.add_argument("--streams", type=int, default=1)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--nodes", type=int, default=1)
    parser.add_argument("--queries", type=int, default=25)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--index-mib", type=int, default=512)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fixtures", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--variant",
        choices=("legacy-retained", "indexed-workspace"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--measurement", choices=("time", "allocation"), help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    if not (
        1 <= args.intervals <= 500_000
        and 1 <= args.streams <= 16
        and args.intervals * args.streams <= 1_000_000
        and 1 <= args.threads <= 32
        and 1 <= args.nodes <= args.streams
        and 1 <= args.queries <= 1000
        and 1 <= args.repeats <= 20
        and 1 <= args.index_mib <= 8192
    ):
        parser.error(
            "Fixture/sample limits exceeded; use --help for supported settings"
        )
    if args.child:
        if args.fixtures is None or args.variant is None or args.measurement is None:
            parser.error("Incomplete private child invocation")
        _child(args)
        return 0
    args.source_root = args.source_root.resolve()
    baseline = (args.baseline_root or args.source_root).resolve()
    sys.path.insert(0, str(args.source_root))
    samples = []
    with tempfile.TemporaryDirectory(prefix="raven-workspace-benchmark-") as temporary:
        directory = Path(temporary)
        fixture = _fixtures(
            directory, args.intervals, args.streams, args.threads, args.nodes
        )
        fixture_file = directory / "fixtures.json"
        fixture_file.write_text(json.dumps(fixture))
        for measurement, repeats in (("time", args.repeats), ("allocation", 1)):
            for repetition in range(repeats):
                variants = [
                    ("legacy-retained", baseline),
                    ("indexed-workspace", args.source_root),
                ]
                for variant, source_root in (
                    variants if repetition % 2 == 0 else list(reversed(variants))
                ):
                    command = [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--child",
                        "--fixtures",
                        str(fixture_file),
                        "--variant",
                        variant,
                        "--measurement",
                        measurement,
                        "--source-root",
                        str(source_root),
                        "--queries",
                        str(args.queries),
                        "--index-mib",
                        str(args.index_mib),
                    ]
                    completed = subprocess.run(
                        command, capture_output=True, text=True, timeout=360
                    )
                    if completed.returncode:
                        raise RuntimeError(
                            f"{variant} qualification failed: {completed.stderr[-4000:]}"
                        )
                    sample = json.loads(completed.stdout)
                    sample["repetition"] = repetition
                    samples.append(sample)
        reference = [sample for sample in samples if sample["whole_fixture_comparable"]]
        if any(
            sample["query_answers"] != reference[0]["query_answers"]
            for sample in reference
        ):
            raise RuntimeError(
                "Equivalent whole-fixture query answers differ between variants"
            )
        fixture.pop("paths")
        report = {
            "schema": SCHEMA,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "environment": _environment(),
            "harness_sha256": _digest(Path(__file__).resolve()),
            "fixture": fixture,
            "samples": samples,
            "summary": _summary(samples),
            "equivalent_whole_fixture_query_answers": True
            if len({sample["variant"] for sample in reference}) == 2
            else None,
            "within_supported_variant_repeat_answers_consistent": True,
            "comparison_scope": "Both variants represent the whole fixture"
            if len({sample["variant"] for sample in reference}) == 2
            else "Index repeat consistency only; legacy whole-fixture representation is unsupported",
            "limits": [
                "Synthetic local journals; no live scheduler, GPU, remote-node or capture-overhead qualification.",
                "Peak process RSS includes native/interpreter memory; tracemalloc reports Python allocations in separate samples excluded from timed summaries.",
                "Legacy retained suffix is not a comparable whole-run baseline after eviction; no speedup is inferred from it.",
                "Read/write byte counters depend on platform and cache state; zero read_bytes does not mean no file reads.",
                "Overview costs describe each supported implementation; no legacy full-recording overview is invented.",
            ],
        }
        raw = json.dumps(report, indent=2) + "\n"
        if args.output:
            descriptor = os.open(
                args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w") as output:
                output.write(raw)
        print(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
