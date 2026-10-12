#!/usr/bin/env python3
"""Portable command reporting; copy the canonical reporting.py beside this file.

This application-owned wrapper submits no jobs and makes no scheduler queries.
On a cluster, invoke it only inside the compute allocation that runs the work.
Use one coordinator per command, never one wrapper per MPI rank.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import re
import signal
import socket
import subprocess
import sys
import time
import uuid

try:
    import reporting as R
except ImportError as exc:
    raise SystemExit("Copy examples/project-template/reporting.py beside portable_run.py first.") from exc


def scheduler_context(env):
    """Retain declared Slurm identity/resources; unknown values stay absent."""
    job = env.get("SLURM_JOB_ID", "")
    if not job:
        return None, 1, {}, {}
    if not re.fullmatch(r"[1-9][0-9]{0,18}", job):
        raise ValueError("invalid SLURM_JOB_ID")
    parent, task = env.get("SLURM_ARRAY_JOB_ID", ""), env.get("SLURM_ARRAY_TASK_ID", "")
    if bool(parent) != bool(task):
        raise ValueError("array reporting requires both Slurm array identifiers")
    if parent and (not re.fullmatch(r"[1-9][0-9]{0,18}", parent)
                   or not re.fullmatch(r"[0-9]{1,19}", task)):
        raise ValueError("invalid Slurm array identity")
    restart = env.get("SLURM_RESTART_COUNT", "0")
    if not re.fullmatch(r"[0-9]{1,10}", restart) or int(restart) > 2_147_483_646:
        raise ValueError("invalid SLURM_RESTART_COUNT")
    cluster = env.get("SLURM_CLUSTER_NAME", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{0,128}", cluster):
        raise ValueError("invalid SLURM_CLUSTER_NAME")
    resources = {}
    for source, target in (("SLURM_JOB_PARTITION", "partition"),
                           ("SLURM_JOB_ACCOUNT", "account"), ("SLURM_JOB_QOS", "qos")):
        if env.get(source):
            resources[target] = env[source]
    nodes = env.get("SLURM_JOB_NUM_NODES", env.get("SLURM_NNODES", ""))
    if re.fullmatch(r"[1-9][0-9]{0,8}", nodes):
        resources["nodes"] = int(nodes)
    cpu_layout = env.get("SLURM_JOB_CPUS_PER_NODE", "")
    if cpu_layout and len(cpu_layout) <= 1024:
        count = cpus = 0
        for piece in cpu_layout.split(","):
            match = re.fullmatch(r"([1-9][0-9]{0,8})(?:\(x([1-9][0-9]{0,8})\))?", piece)
            if match is None:
                break
            repetitions = int(match.group(2) or 1)
            count += repetitions
            cpus += int(match.group(1)) * repetitions
        else:
            if cpus <= 1_000_000_000 and count <= 1_000_000_000 and resources.get("nodes", count) == count:
                resources.update(cpus=cpus, nodes=count)
    gpu = env.get("SLURM_GPUS", "")
    if re.fullmatch(r"[0-9]{1,9}", gpu):
        resources["gpus"] = int(gpu)
    per_node, per_cpu = env.get("SLURM_MEM_PER_NODE", ""), env.get("SLURM_MEM_PER_CPU", "")
    if not (per_node and per_cpu):
        for raw, count in ((per_node, resources.get("nodes")), (per_cpu, resources.get("cpus"))):
            if count and re.fullmatch(r"[1-9][0-9]{0,12}", raw):
                total = int(raw) * count * 1024**2
                if total <= (1 << 63) - 1:
                    resources["mem_bytes"] = total
    launch = {"cluster": cluster, "restart_count": int(restart), "array_id": parent,
              "array_index": int(task) if parent else None}
    return f"{parent}_{task}" if parent else job, int(restart) + 1, resources, launch


def run_command(args):
    command = args.args[1:] if args.args[:1] == ["--"] else args.args
    if not command:
        raise ValueError("run requires a command after --")
    root = Path(args.root).absolute()
    parameters = json.loads(args.parameters)
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be a JSON object")
    job, attempt, resources, launch = scheduler_context(os.environ)
    run_id = args.run_id or "run-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:12]
    run = R.begin_run(root, run_id, name=args.experiment, experiment_id=args.experiment,
                      script=args.script, parameters=parameters, job_id=job,
                      attempt=attempt, resources=resources)
    identity = {"user": pwd.getpwuid(os.getuid()).pw_name, "uid": os.getuid(),
                "host": socket.gethostname(), "mode": "slurm" if job else "local",
                "resource_source": "Slurm environment declarations" if job else "none",
                "runtime_seconds_source": "time.monotonic",
                "runtime_seconds_scope": "wrapped command including its launch and shutdown"}
    metadata = {"reporting_v2": identity}
    if job:
        metadata["raven_launch"] = launch
    # The copyable SDK owns bounded JSON validation and atomic publications.
    manifest = R._read_json(run / "run.json")
    manifest["metadata"] = metadata
    R._atomic_json(run / "run.json", manifest, replace=True)
    print(run, flush=True)
    env = dict(os.environ, RAVEN_RUN_DIR=str(run), RAVEN_PROJECT_ROOT=str(root),
               RAVEN_REPORT_HELPER=str(Path(__file__).absolute()), RAVEN_REPORT_PYTHON=sys.executable)
    process = None
    received = []

    def forward(signum, _frame):
        received.append(signum)
        if process is not None:
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass

    handlers = {sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM)}
    started = time.monotonic()
    try:
        with (run / "logs/stdout.log").open("ab") as stdout, (run / "logs/stderr.log").open("ab") as stderr:
            process = subprocess.Popen(command, cwd=root, env=env, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            for signum in received:
                try:
                    os.killpg(process.pid, signum)
                except ProcessLookupError:
                    pass
            result = process.wait()
        code = 128 - result if result < 0 else result
        interrupted = received or result in (-signal.SIGINT, -signal.SIGTERM)
        state = "INTERRUPTED" if interrupted else "COMPLETED" if code == 0 else "FAILED"
    except OSError as exc:
        code, state = 127, "FAILED"
        print(f"Raven reporting: could not start command: {exc}", file=sys.stderr)
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    try:
        R.finish_run(run, state=state, exit_code=code, runtime_seconds=time.monotonic() - started,
                     metadata=metadata)
    except (OSError, ValueError) as exc:
        # A publication error must not turn an application's failure into success,
        # or replace the application's result with a reporting-specific exit code.
        print(f"Raven reporting: command exited {code}; final report could not be saved: {exc}", file=sys.stderr)
    else:
        print(f"Raven reporting: {state} (exit {code})", file=sys.stderr)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="record one command on this machine; never allocate compute")
    run.add_argument("--root", default=os.environ.get("RAVEN_PROJECT_ROOT", "."))
    run.add_argument("--experiment", required=True)
    run.add_argument("--script", required=True, help="scientific source file relative to project root")
    run.add_argument("--parameters", default="{}", help="JSON object of scientific parameters")
    run.add_argument("--run-id", help="unique attempt name; generated by default")
    run.add_argument("args", nargs=argparse.REMAINDER)
    metric = commands.add_parser("metric", help="append actual measurements from one run coordinator")
    metric.add_argument("--run", default=os.environ.get("RAVEN_RUN_DIR"), required=not os.environ.get("RAVEN_RUN_DIR"))
    metric.add_argument("--value", action="append", default=[], help="NAME=NUMBER; repeat for multiple measurements")
    metric.add_argument("--step", type=int)
    metric.add_argument("--phase")
    metric.add_argument("--completed", type=float)
    metric.add_argument("--total", type=float)
    metric.add_argument("--unit", default="")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run_command(args)
        values = {}
        for declaration in args.value:
            name, equal, number = declaration.partition("=")
            if not equal or name in values:
                raise ValueError("each metric needs a unique NAME=NUMBER")
            values[name] = float(number)
        progress = None
        if args.completed is not None or args.total is not None:
            progress = {"completed": args.completed, "total": args.total, "unit": args.unit}
        R.write_metric(args.run, values, step=args.step, phase=args.phase, progress=progress)
        return 0
    except (OSError, ValueError) as exc:
        print(f"Raven reporting: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
