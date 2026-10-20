"""Reproduce bounded Python tracing overhead in isolated fresh processes.

Prepare the baseline without touching a checkout::

    mkdir /tmp/raven-before
    git archive BASE_REVISION raven | tar -x -C /tmp/raven-before
    python scripts/benchmark-injob.py --before /tmp/raven-before --output results.json

Run with peer builds/tests stopped. RSS is Linux ru_maxrss KiB; block output
counts are not scheduler RPCs. No live Slurm or GPU qualification is performed.
"""

import argparse
import json
import os
import resource
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def case(package, workload):
    sys.path.insert(0, package)
    from raven.trace_recorder import PythonCapture, Recorder

    root = Path(tempfile.mkdtemp(prefix="raven-trace-bench-"))
    source = "def f(): return 7\ndef fib(n): return n if n < 2 else fib(n-1)+fib(n-2)\n"
    scope = {}
    exec(compile(source, str(root / "work.py"), "exec"), scope)
    pubs = [0]
    original = Recorder._publish

    def publish(self):
        pubs[0] += 1
        return original(self)

    Recorder._publish = publish
    r = Recorder(
        root,
        journal_format="blocks",
        max_bytes=512 << 20,
        coverage={"adapter": "python-sys-monitoring", "scope": "project fixture"},
    )
    c = PythonCapture(r, scope="project", roots=(root,))
    c.start()
    cpu = time.process_time()
    wall = time.perf_counter()
    if workload == "calls":
        for _ in range(10000):
            scope["f"]()
    else:
        assert scope["fib"](18) == 2584
    app = time.perf_counter() - wall
    c.stop()
    r.close()
    result = {
        "application_s": app,
        "complete_s": time.perf_counter() - wall,
        "cpu_s": time.process_time() - cpu,
        "journal_bytes": r.bytes,
        "events": r.committed_seq,
        "state": r.state,
        "dropped": r.dropped,
        "manifest_publications": pubs[0],
        "rss_peak_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "block_output_ops": resource.getrusage(resource.RUSAGE_SELF).ru_oublock,
    }
    print(json.dumps(result))


def startup(package, traced):
    sys.path.insert(0, package)
    from raven.launch import resolve_plan
    from raven.launch_runtime import execute_plan

    root = Path(tempfile.mkdtemp(prefix="raven-startup-bench-"))
    script = root / "startup.py"
    script.write_text(
        'import subprocess,sys,time,json,statistics\nvalues=[]\nfor i in range(10):\n t=time.perf_counter();subprocess.run([sys.executable,"-c","pass"],check=True);values.append(time.perf_counter()-t)\nopen("timings.json","w").write(json.dumps(values))\n'
    )
    job = root / "job.sh"
    job.write_text("#!/bin/sh\n" + shlex.join([sys.executable, str(script)]) + "\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("RAVEN_")}
    env.pop("PYTHONPATH", None)
    args = [
        str(job),
        "--project",
        str(root),
        "--local",
        "--trace" if traced else "--no-trace",
        "--no-cpu-memory",
        "--no-gpu",
        "--no-export-on-finish",
    ]
    result = execute_plan(resolve_plan(args, cwd=root, environ=env), environ=env)
    assert result["exit_code"] == 0, result
    samples = json.loads((root / "timings.json").read_text())
    print(json.dumps({"median_s": statistics.median(samples), "samples_s": samples}))


def main():
    # Private child modes keep each measured import/runtime out of this driver.
    if len(sys.argv) > 1 and sys.argv[1] == "_case":
        case(sys.argv[2], sys.argv[3])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "_startup":
        startup(sys.argv[2], sys.argv[3] == "1")
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--before",
        required=True,
        type=Path,
        help="Parent containing baseline raven/ package",
    )
    parser.add_argument(
        "--after", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 10:
        parser.error("--repeat must be 1–10")
    for package in (args.before, args.after):
        if not (package / "raven/trace_recorder.py").is_file():
            parser.error("Each package parent must contain raven/trace_recorder.py")
    results = {
        "environment": {
            "python": sys.version,
            "os": os.uname().sysname,
            "machine": os.uname().machine,
            "notes": "Serial fresh processes; stop peer builds/tests for controlled measurements. Linux RSS is KiB; block output operations are not scheduler RPCs.",
        },
        "tracing": {},
        "startup": {},
    }
    for name, package in (
        ("before", args.before.resolve()),
        ("after", args.after.resolve()),
    ):
        results["tracing"][name] = {}
        for workload in ("calls", "fib"):
            runs = []
            for _ in range(args.repeat):
                output = subprocess.run(
                    [sys.executable, __file__, "_case", str(package), workload],
                    text=True,
                    capture_output=True,
                    check=True,
                    timeout=60,
                )
                runs.append(json.loads(output.stdout))
            results["tracing"][name][workload] = {
                "runs": runs,
                "median": {
                    key: statistics.median(run[key] for run in runs)
                    for key in runs[0]
                    if isinstance(runs[0][key], (int, float))
                },
            }
        results["startup"][name] = {}
        for traced in (False, True):
            output = subprocess.run(
                [sys.executable, __file__, "_startup", str(package), str(int(traced))],
                text=True,
                capture_output=True,
                check=True,
                timeout=120,
            )
            results["startup"][name]["traced" if traced else "untraced"] = json.loads(
                output.stdout.splitlines()[-1]
            )
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(str(args.output))


if __name__ == "__main__":
    main()
