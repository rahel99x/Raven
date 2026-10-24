#!/usr/bin/env python3
"""Equivalent fresh-process tracing/client fixtures, without scheduler or GPU claims.

Timing and tracemalloc run in separate processes. Imports/framework setup are
outside the timed interval. Recorder setup, durable flush, and close are inside.
The 10-million-call case deliberately exposes the default recording budget;
budget-exhausted results cannot qualify an overhead target at full fidelity.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import math
import os
import platform
import resource
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path

WORKLOAD = '''
def call(value):
    return (value * 17 + 3) % 101

def calls(count):
    answer = 0
    for value in range(count):
        answer += call(value)
    return answer

def numpy_work(count, np):
    left = np.arange(256 * 256, dtype=float).reshape(256, 256) / 65536
    right = np.eye(256) * 0.5
    for _ in range(count):
        left = np.matmul(left, right) + 0.125
    return float(left.sum())

def torch_work(count, torch):
    torch.manual_seed(7)
    model = torch.nn.Linear(128, 128)
    batch = torch.randn(64, 128)
    for _ in range(count):
        loss = model(batch).square().mean()
        loss.backward()
        with torch.no_grad():
            for parameter in model.parameters():
                parameter -= 0.001 * parameter.grad
        model.zero_grad(set_to_none=True)
    return loss.item()

def mpi_work(count, comm, MPI):
    answer = 0.0
    for _ in range(count):
        answer = comm.allreduce(float(comm.rank + 1), op=MPI.SUM)
    return answer
'''


def _rss():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)


def _manifest(recorder):
    if recorder is None:
        return {}
    return json.loads(recorder.path.read_text())


def _drain_client(client):
    # Public API intentionally does not require application shutdown plumbing;
    # benchmark-only synchronization must wait for the actual writer to finish.
    if not client._get_client().close(timeout=10):
        raise RuntimeError("Client did not drain successfully within the benchmark deadline")


def child(args):
    sys.path.insert(0, str(args.source_root.resolve()))
    measured_files = ["raven/__init__.py", "raven/client.py"] if args.mode in {"client", "noop"} else ["raven/trace_recorder.py", "raven/trace_protocol.py"]
    measured_files += [name for name in ("raven/tracing/capture.py", "raven/tracing/protocol.py", "raven/tracing/metrics_overlay.py")
                       if (args.source_root / name).is_file()]
    before_hashes = {name: hashlib.sha256((args.source_root / name).read_bytes()).hexdigest()
                     for name in measured_files if (args.source_root / name).exists()}
    framework = None
    if args.case == "numpy":
        import numpy as framework
    elif args.case == "torch":
        import torch as framework
        framework.set_num_threads(1)
    elif args.case == "mpi":
        from mpi4py import MPI as framework
    with tempfile.TemporaryDirectory(prefix="raven-instrumentation-") as temporary:
        directory = Path(temporary)
        target = directory / "workload.py"
        target.write_text(WORKLOAD)
        namespace = {}
        exec(compile(WORKLOAD, str(target), "exec"), namespace)  # noqa: S102 - fixed benchmark fixture only
        client = capture = recorder = None
        result = None
        if args.mode == "client":
            from raven import client
            client.configure(path=str(directory / "metrics.jsonl"))
        elif args.mode == "noop":
            from raven import client
        elif args.mode != "disabled":
            from raven.trace_recorder import PythonCapture, Recorder
            recorder_args = {"max_bytes": args.max_bytes, "flush_events": 64, "flush_interval": 0.05}
            capture_args = {"legacy": not hasattr(sys, "monitoring")}
            if "journal_format" in inspect.signature(Recorder).parameters:
                recorder_args["journal_format"] = "blocks"
            if "scope" in inspect.signature(PythonCapture).parameters:
                capture_args.update(scope="all" if args.mode == "all" else "project", roots=[directory])
        if args.case == "mpi":
            framework.COMM_WORLD.Barrier()
        if args.allocations:
            tracemalloc.start()
        usage_before = resource.getrusage(resource.RUSAGE_SELF)
        started, cpu = time.perf_counter(), time.process_time()
        if args.mode not in {"disabled", "client", "noop"}:
            recorder = Recorder(directory, **recorder_args)
            capture = PythonCapture(recorder, **capture_args)
            capture.start()
        try:
            if args.case in {"calls", "calls-10m"}:
                result = namespace["calls"](args.iterations)
            elif args.case in {"numpy", "torch"}:
                result = namespace[args.case + "_work"](args.iterations, framework)
            elif args.case == "mpi":
                result = namespace["mpi_work"](args.iterations, framework.COMM_WORLD, framework)
            elif args.case == "noop":
                function = namespace["call"]
                if client is not None:
                    function = client.measure("fixture")(function)
                result = sum(function(index) for index in range(args.iterations))
            elif args.case in {"metrics", "metric-threads"}:
                def report(name, count):
                    for index in range(count):
                        if client is not None:
                            client.metric(name, index, step=index)
                if args.case == "metrics":
                    report("fixture", args.iterations)
                else:
                    workers = [threading.Thread(target=report, args=("thread-" + str(index), args.iterations)) for index in range(8)]
                    for worker in workers:
                        worker.start()
                    for worker in workers:
                        worker.join()
                result = args.iterations - 1
        finally:
            if capture is not None:
                capture.stop()
                recorder.close()
            enqueue_elapsed = time.perf_counter() - started
            if args.mode == "client":
                _drain_client(client)
        elapsed, used = time.perf_counter() - started, time.process_time() - cpu
        usage_after = resource.getrusage(resource.RUSAGE_SELF)
        peak_rss = _rss()
        python_peak = tracemalloc.get_traced_memory()[1] if args.allocations else None
        if args.allocations:
            tracemalloc.stop()
        manifest = _manifest(recorder)
        metric_file = directory / "metrics.jsonl"
        rows = [json.loads(line) for line in metric_file.read_text().splitlines()] if metric_file.exists() else []
        meta_file = directory / "metrics.meta.json"
        meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
        latest = {}
        for row in rows:
            latest.update(row.get("metrics", {}))
        value = {"case": args.case, "mode": args.mode, "iterations": args.iterations, "allocations": args.allocations,
                     "recorder_budget_bytes": args.max_bytes, "flush_events": 64, "flush_interval_seconds": 0.05,
                     "wall_seconds": elapsed, "enqueue_seconds": enqueue_elapsed, "cpu_seconds": used,
                     "filesystem_input_blocks": usage_after.ru_inblock - usage_before.ru_inblock,
                     "filesystem_output_blocks": usage_after.ru_oublock - usage_before.ru_oublock,
                     "peak_process_rss_bytes": peak_rss, "peak_python_allocated_bytes": python_peak,
                     "journal_bytes": manifest.get("committed_bytes", 0), "events": manifest.get("committed_seq", 0),
                     "dropped": manifest.get("dropped", 0), "state": manifest.get("state"), "result": result,
                     "metric_records": len(rows), "metric_bytes": metric_file.stat().st_size if metric_file.exists() else 0,
                     "metric_latest": latest, "metric_metadata": meta,
                     "source_root": str(args.source_root), "python": sys.version.split()[0], "source_sha256": before_hashes}
        if recorder is not None:
            from raven.trace_protocol import TraceReader
            reader = TraceReader(recorder.path, retain=20000)
            calls = returns = previous = 0
            while True:
                snapshot = reader.refresh(max_events=20000, max_bytes=8 << 20)
                fresh = [event for event in snapshot["events"] if event["seq"] > previous]
                calls += sum(event["kind"] == "call" and event.get("data", {}).get("function") == "call" for event in fresh)
                returns += sum(event["kind"] == "return" and event.get("data", {}).get("function") == "call" for event in fresh)
                if snapshot.get("caught_up") or snapshot.get("status") == "INVALID":
                    break
                if snapshot["read_seq"] <= previous:
                    raise RuntimeError("Trace verification stopped making progress")
                previous = snapshot["read_seq"]
            value["verification"] = {key: snapshot.get(key) for key in ("status", "read_seq", "complete", "warnings")}
            value["fixture_calls_recorded"], value["fixture_returns_recorded"] = calls, returns
            if args.case.startswith("calls"):
                value["fixture_call_coverage_fraction"] = calls / args.iterations
                value["full_fidelity_overhead_eligible"] = bool(snapshot["complete"] and calls == returns == args.iterations)
            if snapshot["status"] == "INVALID":
                raise RuntimeError("Produced journal failed integrity verification: " + str(snapshot["warnings"]))
            if args.case.startswith("calls") and manifest.get("state") == "CLOSED" and not manifest.get("dropped"):
                assert calls == returns == args.iterations, (calls, returns, args.iterations)
        if args.case in {"calls", "calls-10m", "noop"}:
            quotient, remainder = divmod(args.iterations, 101)
            assert result == quotient * 5050 + sum((index * 17 + 3) % 101 for index in range(remainder))
        if args.mode == "client":
            names = ["fixture"] if args.case == "metrics" else ["thread-" + str(index) for index in range(8)]
            assert latest == {name: args.iterations - 1 for name in names}, latest
            assert meta["counts"]["accepted"] == args.iterations * len(names), meta
            assert sum(meta["coalesced"].values()) + len(rows) == args.iterations * len(names), meta
            assert len(rows) <= len(names) * (20 * math.ceil(elapsed) + 1), (len(rows), elapsed)
        elif args.mode == "noop":
            assert not rows and not meta
        if args.case == "mpi":
            value["rank"] = framework.COMM_WORLD.rank
            value["world_size"] = framework.COMM_WORLD.size
        assert before_hashes == {name: hashlib.sha256((args.source_root / name).read_bytes()).hexdigest()
                                 for name in before_hashes}, "Measured sources changed during this sample; rerun it"
        print(json.dumps(value, sort_keys=True))


def _revision(root):
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False, timeout=10)
    return result.stdout.strip() if result.returncode == 0 else "archive: supply --baseline-revision"


def _run(command, environment, timeout):
    process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        output, errors = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, output, errors)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, errors = process.communicate(timeout=10)
        return subprocess.CompletedProcess(command, 124, output, "Owned fixture timed out; process group stopped.\n" + errors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--baseline-revision", default="unknown")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--cases", default="calls,calls-10m,numpy,torch,mpi,noop,metrics,metric-threads")
    parser.add_argument("--modes", default="disabled,project,all,noop,client")
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--mpi-iterations", type=int, default=2000)
    parser.add_argument("--max-bytes", type=int, default=64 << 20)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--allocations", action="store_true")
    parser.add_argument("--case", default="calls")
    parser.add_argument("--mode", default="disabled", choices=("disabled", "project", "all", "noop", "client"))
    parser.add_argument("--memory-pass", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10 or not 1 <= args.iterations <= 10_000_000:
        parser.error("repeats must be 1..10 and iterations 1..10000000")
    if not 1 <= args.mpi_iterations <= 1_000_000:
        parser.error("mpi-iterations must be 1..1000000")
    if set(args.cases.split(",")) - {"calls", "calls-10m", "numpy", "torch", "mpi", "noop", "metrics", "metric-threads"}:
        parser.error("cases contains an unknown fixture")
    if set(args.modes.split(",")) - {"disabled", "project", "all", "noop", "client"}:
        parser.error("modes contains an unknown capture mode")
    if args.child:
        child(args)
        return
    modules = {name: importlib.util.find_spec(name) is not None for name in ("numpy", "torch", "mpi4py")}
    samples, skipped, failures = [], [], []
    cases = args.cases.split(",")
    for case in cases:
        dependency = {"numpy": "numpy", "torch": "torch", "mpi": "mpi4py"}.get(case)
        if dependency and (not modules[dependency] or case == "mpi" and not shutil.which("mpirun")):
            skipped.append({"case": case, "reason": "Optional installed dependency/launcher unavailable"})
            continue
        counts = {"calls-10m": 10_000_000, "numpy": 200, "torch": 100, "mpi": args.mpi_iterations,
                  "noop": 100_000, "metrics": 100_000, "metric-threads": 10_000}
        iterations = counts.get(case, args.iterations)
        if case in {"noop", "metrics", "metric-threads"}:
            variants = [(args.source_root, "disabled"), (args.source_root, "noop" if case == "noop" else "client")]
        else:
            variants = [(args.source_root, "disabled"), (args.source_root, "project"), (args.source_root, "all")]
            if args.baseline_root:
                variants.insert(1, (args.baseline_root, "all"))
        variants = [(root, mode) for root, mode in variants if mode in args.modes.split(",")]
        for repetition in range(args.repeats + int(args.memory_pass)):
            allocations = repetition == args.repeats
            if allocations and case == "calls-10m":
                # The separate fully retained fixture measures allocations;
                # tracing ten million already-budget-dropped callbacks changes
                # their timing enormously without measuring useful retention.
                continue
            for root, mode in variants if repetition % 2 == 0 else reversed(variants):
                command = [sys.executable, str(Path(__file__).resolve()), "--child", "--source-root", str(root),
                           "--case", case, "--mode", mode, "--iterations", str(iterations), "--max-bytes", str(args.max_bytes)]
                if allocations:
                    command.append("--allocations")
                if case == "mpi":
                    command = [shutil.which("mpirun"), "-np", "2", *command]
                environment = {key: value for key, value in os.environ.items() if not key.startswith("RAVEN_")}
                environment.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
                result = _run(command, environment, args.timeout)
                if result.returncode:
                    failures.append({"case": case, "mode": mode, "source_root": str(root), "returncode": result.returncode,
                                         "stderr": result.stderr[-4000:]})
                    continue
                for line in result.stdout.splitlines():
                    samples.append(dict(json.loads(line), repetition=repetition))
    summaries = []
    for case, mode, root in sorted({(s["case"], s["mode"], s["source_root"]) for s in samples}):
        timing = [s for s in samples if (s["case"], s["mode"], s["source_root"]) == (case, mode, root) and not s["allocations"]]
        if timing:
            wall = [max(sample["wall_seconds"] for sample in timing if sample["repetition"] == repetition)
                    for repetition in sorted({sample["repetition"] for sample in timing})]
            cpu = [sum(sample["cpu_seconds"] for sample in timing if sample["repetition"] == repetition)
                   for repetition in sorted({sample["repetition"] for sample in timing})]
            summaries.append({"case": case, "mode": mode, "source_root": root,
                "wall_seconds_median": statistics.median(wall), "wall_seconds_min": min(wall), "wall_seconds_max": max(wall),
                "cpu_seconds_median": statistics.median(cpu),
                "timing_scope": "Slowest-rank wall and summed rank CPU per launch" if case == "mpi" else "Single process",
                "peak_process_rss_bytes_max": max(s["peak_process_rss_bytes"] for s in timing),
                "events_min": min(s["events"] for s in timing), "events_max": max(s["events"] for s in timing),
                "dropped_max": max(s["dropped"] for s in timing)})
    files = ("raven/trace_recorder.py", "raven/trace_protocol.py", "raven/client.py", "scripts/benchmark_auto_instrumentation.py")
    report = {"schema": "raven.automatic-instrumentation-benchmark/v1", "revision": _revision(args.source_root),
                  "source_state": "Working-tree bytes identified by per-sample source_sha256; revision alone is not the implementation identity",
                  "baseline_revision": args.baseline_revision, "platform": platform.platform(), "python": sys.version,
                  "logical_cpus": os.cpu_count(),
                  "affinity_cpus": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
                  "source_sha256": {name: hashlib.sha256((args.source_root / name).read_bytes()).hexdigest() for name in files if (args.source_root / name).exists()},
                  "repeats": args.repeats, "optional_modules": modules, "cases": cases, "samples": samples, "summaries": summaries,
                  "skipped": skipped, "failures": failures,
                  "scope": "Local CPU/filesystem and optional two local MPI ranks; no Slurm, GPU, remote cluster, or cross-node qualification. RSS is process high-water; Python allocations come from separate instrumented passes. Native results include Python control loops. Budget-exhausted samples do not qualify full-fidelity overhead."}
    raw = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(raw)
    print(raw)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
