#!/usr/bin/env python3
"""Fixed-work optional Torch CPU capture overhead against disabled control.

Every sample uses a fresh application interpreter. Import/warmup, adapter setup,
scientific operators, checkpoint export/import, teardown and total process time
are reported separately. This is a new-adapter overhead measurement, not a
before/after optimization or physical GPU qualification. No packages are installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCHEMA = "raven.activity-capture-benchmark/v1"
ROOT = Path(__file__).resolve().parents[1]


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rss():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def _io():
    try:
        raw = Path("/proc/self/io").read_text()
    except OSError:
        return None
    return {
        key: int(value)
        for line in raw.splitlines()
        for key, value in (line.split(":", 1),)
        if key in {"rchar", "wchar", "read_bytes", "write_bytes"}
    }


def _delta(before, after):
    return (
        {key: after[key] - value for key, value in before.items()}
        if before and after
        else None
    )


def _fixture(torch, size):
    """Deterministic float64 inputs; no random, pacing or data-dependent work."""
    values = torch.arange(size * size, dtype=torch.float64).reshape(size, size)
    weights = ((values.remainder(19) - 9) / (9 * math.sqrt(size))).contiguous()
    initial = ((values.remainder(13) - 6) / 13).contiguous()
    bias = (torch.arange(size, dtype=torch.float64).remainder(7) / 70).reshape(1, size)
    return initial, weights, bias


def _workload(torch, initial, weights, bias, *, steps, sweeps, checkpoint):
    state = initial.clone()
    work_wall = work_cpu = checkpoint_wall = checkpoint_cpu = 0.0
    wall, cpu = time.perf_counter(), time.process_time()
    with torch.no_grad():
        for _ in range(steps):
            started, used = time.perf_counter(), time.process_time()
            for _ in range(sweeps):
                state = torch.tanh(torch.mm(state, weights) + bias)
            work_wall += time.perf_counter() - started
            work_cpu += time.process_time() - used
            started, used = time.perf_counter(), time.process_time()
            checkpoint()
            checkpoint_wall += time.perf_counter() - started
            checkpoint_cpu += time.process_time() - used
    elapsed, consumed = time.perf_counter() - wall, time.process_time() - cpu
    # Read after timing; conversion has its own small profiler-visible cost.
    answer = {"sum": float(state.sum()), "squared_sum": float((state * state).sum())}
    if not all(math.isfinite(value) for value in answer.values()):
        raise ValueError("Numerical benchmark fixture produced a nonfinite checksum")
    return {
        "checksum_hex": {key: value.hex() for key, value in answer.items()},
        "scientific_work_wall_seconds": work_wall,
        "scientific_work_cpu_seconds": work_cpu,
        "checkpoint_wall_seconds": checkpoint_wall,
        "checkpoint_cpu_seconds": checkpoint_cpu,
        "work_and_checkpoints_wall_seconds": elapsed,
        "work_and_checkpoints_cpu_seconds": consumed,
        "matrix_multiplications": steps * sweeps,
    }


def _capture_evidence(receipt):
    from raven.tracing.protocol import read_json

    reports = receipt.get("reports", [])
    source_states, raw_bytes, journal_bytes, dropped, registered = [], 0, 0, 0, True
    hashes = []
    for report in reports:
        manifest = read_json(report["manifest"])
        path = Path(report["manifest"])
        raw = Path(report["producer_report"])
        journal = path.parent / manifest["journal"]
        source_states.append(manifest["state"])
        dropped += manifest.get("dropped", 0)
        registered = registered and report.get("registered", False)
        raw_bytes += raw.stat().st_size
        journal_bytes += journal.stat().st_size
        hashes.append(
            {
                "manifest_sha256": _hash(path),
                "journal_sha256": _hash(journal),
                "producer_report_sha256": _hash(raw),
                "status": report.get("status"),
                "committed_events": manifest["committed_seq"],
            }
        )
    complete = (
        receipt.get("status") == "complete"
        and bool(reports)
        and registered
        and not dropped
        and all(state == "CLOSED" for state in source_states)
        and not receipt.get("errors")
    )
    return {
        "status": receipt.get("status"),
        "application_status": receipt.get("application_status"),
        "reports": len(reports),
        "raw_report_bytes": raw_bytes,
        "journal_bytes": journal_bytes,
        "retained_artifact_bytes": receipt.get("stored_bytes", 0),
        "source_states": source_states,
        "dropped": dropped,
        "all_registered": registered,
        "final_capture_complete": complete,
        "errors": receipt.get("errors", []),
        "limit_reason": receipt.get("limit_reason"),
        "source_hashes": hashes,
        "fidelity": "Framework CPU activities only; native call stacks, GPU capture and continuous scheduling are unqualified",
        "buffer_bound": receipt.get("buffer_bound"),
        "artifact_budget_bytes": receipt.get("artifact_budget_bytes"),
    }


def _child(args):
    sys.path.insert(0, str(ROOT))
    sample = {
        "schema": SCHEMA,
        "variant": args.variant,
        "measurement": args.measurement,
        "python": sys.version,
        "interpreter": sys.executable,
        "process_id": os.getpid(),
        "status": "preparing",
        "python_traced_peak_bytes": None,
        "process_peak_rss_bytes": None,
    }
    started, used = time.perf_counter(), time.process_time()
    try:
        import torch
    except (ImportError, OSError) as error:
        sample.update(
            status="unavailable",
            reason="Optional Torch interpreter dependency unavailable: " + str(error),
        )
        return sample
    sample.update(
        torch_version=str(torch.__version__),
        torch_cuda_version=getattr(torch.version, "cuda", None),
        torch_hip_version=getattr(torch.version, "hip", None),
    )
    torch.set_num_threads(1)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass  # Fresh interpreters normally allow it; actual thread setting is below.
    sample["torch_threads"] = torch.get_num_threads()
    sample["torch_interop_threads"] = torch.get_num_interop_threads()
    from raven.tracing import activity_capture

    initial, weights, bias = _fixture(torch, args.size)
    torch.tanh(
        torch.mm(initial, weights) + bias
    )  # Warm lazy kernels once in both variants.
    sample.update(
        framework_import_warmup_wall_seconds=time.perf_counter() - started,
        framework_import_warmup_cpu_seconds=time.process_time() - used,
        process_peak_rss_before_capture_bytes=_rss(),
    )
    recorder = None
    before_io = _io()
    if args.measurement == "allocation":
        import tracemalloc

        tracemalloc.start()
    try:
        started, used = time.perf_counter(), time.process_time()
        if args.variant == "torch-cpu":
            recorder = activity_capture.TorchActivityRecorder(
                args.project,
                job_id="activity-benchmark",
                attempt="fixed-work",
                gpu=False,
                required=False,
                with_stack=args.with_stack,
                active_steps=args.window_steps,
                max_windows=min(128, math.ceil(args.steps / args.window_steps) + 1),
                max_bytes=args.max_mib << 20,
                max_total_bytes=args.artifact_mib << 20,
            )
            recorder.__enter__()
        sample.update(
            adapter_setup_wall_seconds=time.perf_counter() - started,
            adapter_setup_cpu_seconds=time.process_time() - used,
        )
        try:
            sample.update(
                _workload(
                    torch,
                    initial,
                    weights,
                    bias,
                    steps=args.steps,
                    sweeps=args.sweeps,
                    checkpoint=activity_capture.step,
                )
            )
        finally:
            started, used = time.perf_counter(), time.process_time()
            if recorder is not None:
                recorder.__exit__(*sys.exc_info())
            sample.update(
                adapter_teardown_wall_seconds=time.perf_counter() - started,
                adapter_teardown_cpu_seconds=time.process_time() - used,
            )
        started, used = time.perf_counter(), time.process_time()
        sample["capture"] = (
            _capture_evidence(recorder.receipt)
            if recorder is not None
            else {
                "status": "disabled",
                "final_capture_complete": None,
                "raw_report_bytes": 0,
                "journal_bytes": 0,
                "retained_artifact_bytes": 0,
            }
        )
        sample.update(
            harness_verification_wall_seconds=time.perf_counter() - started,
            harness_verification_cpu_seconds=time.process_time() - used,
        )
        sample["status"] = (
            "complete"
            if recorder is None or sample["capture"]["final_capture_complete"]
            else "capture-incomplete"
        )
    finally:
        sample["io_delta"] = _delta(before_io, _io())
        sample["process_peak_rss_bytes"] = _rss()
        if args.measurement == "allocation":
            sample["python_traced_peak_bytes"] = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
    return sample


def _sample(args, *, variant, measurement, project, result_path):
    command = [
        args.python,
        str(Path(__file__).resolve()),
        "--child",
        "--variant",
        variant,
        "--measurement",
        measurement,
        "--project",
        str(project),
        "--output",
        str(result_path),
        "--size",
        str(args.size),
        "--steps",
        str(args.steps),
        "--sweeps",
        str(args.sweeps),
        "--window-steps",
        str(args.window_steps),
        "--max-mib",
        str(args.max_mib),
        "--artifact-mib",
        str(args.artifact_mib),
    ]
    if args.with_stack:
        command.append("--with-stack")
    started = time.perf_counter()
    with tempfile.TemporaryFile() as diagnostic:
        child = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=diagnostic)
        try:
            child.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        diagnostic.seek(0, os.SEEK_END)
        size = diagnostic.tell()
        diagnostic.seek(max(0, size - 8192))
        tail = diagnostic.read(8192).decode("utf-8", "replace")
    elapsed = time.perf_counter() - started
    if not result_path.exists():
        return {
            "variant": variant,
            "measurement": measurement,
            "status": "child-error",
            "process_wall_seconds": elapsed,
            "child_exit": child.returncode,
            "diagnostics": tail,
            "reason": "Child failed or exceeded the explicit timeout before reporting",
        }
    if result_path.stat().st_size > 1 << 20:
        raise ValueError("Activity benchmark child result exceeds 1 MiB")
    sample = json.loads(result_path.read_text())
    sample.update(
        process_wall_seconds=elapsed,
        child_exit=child.returncode,
        diagnostics=tail,
        diagnostic_bytes=size,
    )
    return sample


def _summary(samples):
    timings = [row for row in samples if row["measurement"] == "timing"]
    controls = [row for row in timings if row["variant"] == "disabled"]
    captured_samples = [row for row in timings if row["variant"] == "torch-cpu"]
    complete = (
        bool(controls)
        and len(controls) == len(captured_samples)
        and all(
            row.get("status") == "complete" and row.get("child_exit", 0) == 0
            for row in timings
        )
    )
    checksums = [row.get("checksum_hex") for row in timings]
    equivalent = bool(checksums) and all(
        value is not None and value == checksums[0] for value in checksums
    )
    summaries = {}
    for variant in ("disabled", "torch-cpu"):
        selected = [row for row in timings if row["variant"] == variant]
        fields = {}
        for name in (
            "scientific_work_wall_seconds",
            "scientific_work_cpu_seconds",
            "checkpoint_wall_seconds",
            "adapter_setup_wall_seconds",
            "adapter_teardown_wall_seconds",
            "work_and_checkpoints_wall_seconds",
            "process_wall_seconds",
            "process_peak_rss_bytes",
        ):
            values = [row[name] for row in selected if row.get(name) is not None]
            fields[name] = statistics.median(values) if values else None
        summaries[variant] = {"samples": len(selected), "median": fields}
    overhead = None
    if complete and equivalent:
        overhead = {}
        for name in (
            "scientific_work_wall_seconds",
            "scientific_work_cpu_seconds",
            "work_and_checkpoints_wall_seconds",
            "process_wall_seconds",
        ):
            control, captured = (
                summaries[variant]["median"][name]
                for variant in ("disabled", "torch-cpu")
            )
            overhead[name] = {
                "ratio": captured / control if control else None,
                "percent": (captured / control - 1) * 100 if control else None,
                "extra_seconds": captured - control,
            }
    return {
        "all_timing_samples_complete": complete,
        "identical_numerical_checksums": equivalent,
        "variants": summaries,
        "overhead_vs_disabled": overhead,
        "qualification": "New Torch CPU adapter overhead against disabled control; not an old-adapter speedup or GPU qualification",
        "reason": None
        if complete and equivalent
        else "Overhead comparison withheld: a capture/work sample was unavailable, incomplete, failed, or numerically different",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Application interpreter; optional Torch must already be installed",
    )
    parser.add_argument("--size", type=int, default=192)
    parser.add_argument("--steps", type=int, default=15)
    parser.add_argument("--sweeps", type=int, default=32)
    parser.add_argument("--window-steps", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-mib", type=int, default=32)
    parser.add_argument("--artifact-mib", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--with-stack", action="store_true")
    parser.add_argument(
        "--allocations",
        action="store_true",
        help="Additional separate child runs; Python allocations exclude native Torch memory",
    )
    parser.add_argument(
        "--artifacts",
        type=Path,
        help="Retain per-sample raw reports, journals and child results here",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--variant", choices=("disabled", "torch-cpu"), help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--measurement",
        choices=("timing", "allocation"),
        default="timing",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--project", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not (
        16 <= args.size <= 512
        and 1 <= args.steps <= 128
        and 1 <= args.sweeps <= 128
        and 1 <= args.window_steps <= 128
        and 1 <= args.repeats <= 7
        and 1 <= args.max_mib <= 128
        and 1 <= args.artifact_mib <= 1024
        and math.isfinite(args.timeout)
        and 1 <= args.timeout <= 600
    ):
        parser.error(
            "Choose size16–512, steps/sweeps/window1–128, repeats1–7, report1–128MiB, artifacts1–1024MiB, timeout1–600s"
        )
    if args.child:
        if args.output is None or args.project is None or args.variant is None:
            parser.error("Internal child requires output, project and variant")
        args.output.write_text(json.dumps(_child(args), indent=2, allow_nan=False))
        return 0
    temporary = (
        tempfile.TemporaryDirectory(prefix="raven-activity-benchmark-")
        if args.artifacts is None
        else None
    )
    directory = (
        Path(temporary.name) if temporary is not None else args.artifacts.absolute()
    )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    samples = []
    try:
        for measurement in ["timing", "allocation"] if args.allocations else ["timing"]:
            for repeat in range(args.repeats if measurement == "timing" else 1):
                # Alternate order to reduce monotonic environment/thermal bias.
                variants = (
                    ("disabled", "torch-cpu")
                    if repeat % 2 == 0
                    else ("torch-cpu", "disabled")
                )
                for variant in variants:
                    project = directory / f"{measurement}-{repeat}-{variant}"
                    project.mkdir(mode=0o700)
                    samples.append(
                        _sample(
                            args,
                            variant=variant,
                            measurement=measurement,
                            project=project,
                            result_path=project / "result.json",
                        )
                    )
        paths = [Path(__file__).resolve(), *(ROOT / "raven/tracing").glob("*.py")]
        try:
            revision = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            dirty = bool(
                subprocess.check_output(
                    ["git", "status", "--porcelain"], cwd=ROOT, text=True
                )
            )
        except (OSError, subprocess.CalledProcessError):
            revision, dirty = None, None
        report = {
            "schema": SCHEMA,
            "revision": revision,
            "working_tree_dirty": dirty,
            "source_sha256": {
                str(path.relative_to(ROOT)): _hash(path) for path in paths
            },
            "artifact_retention": "retained"
            if args.artifacts
            else "temporary artifacts removed after report",
            "artifact_directory": str(directory) if args.artifacts else None,
            "environment": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "logical_cpus": os.cpu_count(),
                "cpu_affinity": sorted(os.sched_getaffinity(0))
                if hasattr(os, "sched_getaffinity")
                else None,
                "isolation": "Not enforced; root must schedule without competing heavy work",
            },
            "fixture": {
                "size": args.size,
                "steps": args.steps,
                "sweeps": args.sweeps,
                "window_steps": args.window_steps,
                "with_stack": args.with_stack,
                "torch_threads": 1,
                "dtype": "float64",
                "pacing": "none; fixed work",
            },
            "samples": samples,
            "summary": _summary(samples),
            "limitations": [
                "CPU-only framework activity; no physical GPU, Slurm or remote qualification.",
                "Process RSS includes the interpreter, Torch import and native allocations; it is not Python allocation peak.",
                "Optional allocation runs are separate and excluded from timing summaries; tracemalloc excludes most native Torch allocations.",
                "Total process wall time includes framework startup, benchmark verification and result publication; scientific work and adapter/checkpoint/verification costs are reported separately.",
                "Profiler internal buffers and temporary export are tool-dependent; retained artifacts have explicit separate limits.",
                "Complete capture means the adapter's selected framework reports were committed; it is not complete observation of arbitrary application execution.",
                "No scheduler API/command is invoked by this fixture; scheduler load is not benchmarked.",
            ],
        }
        encoded = json.dumps(report, indent=2, allow_nan=False)
        if args.output:
            args.output.write_text(encoded + "\n")
        else:
            print(encoded)
    finally:
        if temporary is not None:
            temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
