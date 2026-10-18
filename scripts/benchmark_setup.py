#!/usr/bin/env python3
"""Compare preserved and current setup drivers against identical Raven sources.

Run only in a quiet window. Every sample creates a new pip-free environment,
renders the same 48 frames, validates JSON/report/doctor, and preserves private
configuration. RSS is Linux process RSS, separately from Python allocations.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import tempfile
import time

import profile_test_suite as profiler

ROOT = Path(__file__).resolve().parents[1]


def worker(driver, folder, record):
    loader = importlib.machinery.SourceFileLoader("raven_setup_measured", str(driver))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    module.ROOT = ROOT
    commands = []
    original = module.run

    def counted(command, **kwargs):
        commands.append(list(map(str, command)))
        return original(command, **kwargs)

    module.run = counted
    config = folder / "personal.json"
    config.write_text("Invalid personal configuration must be ignored.\n")
    os.environ.update(RAVEN_CONFIG=str(config), XDG_CONFIG_HOME=str(folder / "config"),
                      XDG_STATE_HOME=str(folder / "state"))
    code = module.main(["--mode", "demo", "--venv", str(folder / "environment with spaces"),
                        "--report", str(folder / "report.txt")])
    report = folder / "report.txt"
    personal_preserved = config.read_text() == "Invalid personal configuration must be ignored.\n"
    valid_report = report.exists() and report.read_text().isascii() and "JOBS" in report.read_text().upper()
    result = {"exit_code": code, "direct_setup_subprocesses": len(commands), "commands": commands,
              "personal_config_preserved": personal_preserved, "ascii_report_complete": valid_report,
              "worker_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "waited_child_maxrss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss}
    record.write_text(json.dumps(result, indent=2) + "\n")
    return code if code else 0 if personal_preserved and valid_report else 1


def measure(driver, folder, label, output):
    record = output.with_name(output.stem + "-" + label + ".worker.json")
    log = output.with_name(output.stem + "-" + label + ".log")
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(driver),
               "--folder", str(folder), "--record", str(record)]
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    peak_rss = peak_individual = peak_processes = samples = 0
    with log.open("xb") as stream:
        child = subprocess.Popen(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        initial = profiler.process(child.pid)
        expected_start = initial[0][1] if initial else None
        try:
            while child.poll() is None:
                rows = profiler.sample(child.pid, expected_start) if expected_start is not None else []
                peak_rss = max(peak_rss, sum(rss for _, rss, _ in rows))
                peak_individual = max(peak_individual, max((rss for _, rss, _ in rows), default=0))
                peak_processes = max(peak_processes, len(rows))
                samples += 1
                if time.monotonic() - started > 180:
                    raise TimeoutError("Setup comparison exceeded 180s")
                time.sleep(.1)
        finally:
            if child.poll() is None:
                current = profiler.process(child.pid)
                if current is not None and current[0][1] == expected_start:
                    os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                current = profiler.process(child.pid)
                if current is not None and current[0][1] == expected_start:
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {"label": label, "driver": str(driver), "command": command, "exit_code": child.returncode,
            "wall_seconds": time.monotonic() - started,
            "user_cpu_seconds": after.ru_utime - before.ru_utime,
            "system_cpu_seconds": after.ru_stime - before.ru_stime,
            "input_blocks": after.ru_inblock - before.ru_inblock,
            "output_blocks": after.ru_oublock - before.ru_oublock,
            "peak_aggregate_live_rss_kib": peak_rss, "peak_individual_sampled_rss_kib": peak_individual,
            "peak_live_process_count": peak_processes, "samples": samples,
            "worker": json.loads(record.read_text()) if record.exists() else {"missing": True}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--folder", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--record", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker, args.folder, args.record)
    if not args.baseline or not args.output or not 1 <= args.repeats <= 5:
        parser.error("Provide --baseline, --output and 1–5 repeats")
    output = args.output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or any(output.parent.glob(output.stem + "-*")):
        parser.error("Evidence paths must be new")
    before = profiler.fingerprint()
    started = datetime.now(timezone.utc).isoformat()
    runs = []
    drivers = {"baseline": args.baseline.absolute(), "batched": ROOT / "scripts/setup.py"}
    with tempfile.TemporaryDirectory(prefix="raven-setup-benchmark-") as temporary:
        for repeat in range(args.repeats):
            for variant in (("baseline", "batched") if repeat % 2 == 0 else ("batched", "baseline")):
                label = f"{variant}-{repeat + 1}"
                folder = Path(temporary) / label
                folder.mkdir()
                runs.append(measure(drivers[variant], folder, label, output))
    after = profiler.fingerprint()
    result = {"schema": "raven.setup-performance/v1", "started": started,
              "ended": datetime.now(timezone.utc).isoformat(), "runs": runs,
              "source_before": before["sha256"], "source_after": after["sha256"],
              "source_stable": before == after, "sources": before,
              "drivers": {key: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                          for key, path in drivers.items()},
              "environment": {"python": sys.version, "platform": sys.platform, "cpu_count": os.cpu_count()},
              "fixture": "Fresh pip-free venv, demo JSON,48 ASCII/Unicode cases, ASCII report, doctor; identical Raven modules and inherited environment.",
              "limits": "RSS samples sum live processes including shared pages (not PSS/Python allocations); 100ms sampling can miss short peaks. CPU/block I/O are waited process-tree resource counters. Direct subprocess counts instrument setup.run, not all descendant execs. Wall time includes up to 100ms exit-detection delay. Batching shares imports only; every view uses a fresh full launcher session."}
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"source_stable": result["source_stable"], "runs": len(runs), "output": str(output)}))
    return 0 if result["source_stable"] and all(row["exit_code"] == 0 for row in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
