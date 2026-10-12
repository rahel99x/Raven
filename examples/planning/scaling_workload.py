#!/usr/bin/env python3
"""Bounded CPU demonstration and explicit-file scaling record collection.

Examples, from the checkout:
  .venv/bin/python examples/planning/scaling_workload.py local --workers 1 --output one.json
  .venv/bin/python examples/planning/scaling_workload.py combine one.json two.json --output measurements.json
  raven run scaling analyze measurements.json --mode strong --baseline 1

The workload is intentionally small and synthetic. It measures actual elapsed
time including process startup; it does not predict production application speed.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

# Allow the documented direct invocation with either an installed Raven or a
# source checkout, without launching setup or installing dependencies.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from raven.planning_io import load_json

MAX_WORKERS = 8
MAX_PROBLEM = 1000000
MAX_BYTES = 8 << 20


def integer(value, label, low, high):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    text = str(value)
    if not re.fullmatch(r"[0-9]{1,10}", text) or not low <= int(text) <= high:
        raise ValueError(f"{label} must be in {low}..{high}")
    return int(text)


def number(value, label):
    parsed = float(value)
    if not math.isfinite(parsed) or not 0 < parsed <= 1:
        raise ValueError(f"{label} must be finite in (0, 1]")
    return parsed


def digest_script(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 256 << 10:
            raise ValueError("example script must be a regular file of at most 256 KiB")
        data = handle.read((256 << 10) + 1)
        after = os.fstat(handle.fileno())
    signature = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    if len(data) != before.st_size or signature(before) != signature(after):
        raise ValueError("example script changed during fingerprinting")
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    data = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    if len(data) > MAX_BYTES:
        raise ValueError("record output exceeds 8 MiB")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def kernel(task):
    start, stop, seed, terms, scale = task
    total = 0.0
    for index in range(start, stop):
        # The same global problem is partitioned across workers. Seed changes
        # between repeats are paired across worker configurations by the recipe.
        angle = index * scale + seed
        for term in range(terms):
            total += math.sin(angle + term) * math.cos(angle / (term + 1))
    return total


def observed_resources():
    cpus = os.environ.get("SLURM_JOB_CPUS_PER_NODE", "")
    match = re.fullmatch(r"([0-9]{1,10})(?:\(x1\))?", cpus)
    allocated_cpus = int(match[1]) if match and 0 < int(match[1]) <= 2147483647 else None
    gpus = os.environ.get("SLURM_GPUS_ON_NODE", "")
    allocated_gpus = int(gpus) if re.fullmatch(r"[0-9]{1,10}", gpus) and int(gpus) <= 2147483647 else None
    return allocated_cpus, allocated_gpus


def run(args):
    if not args.output or not args.output.isprintable() or os.path.lexists(args.output):
        raise ValueError("output must be a new printable path; existing records are never overwritten")
    if not Path(args.output).parent.is_dir():
        raise ValueError("output parent directory must already exist")
    if args.command == "run":
        if os.environ.get("SLURM_JOB_NUM_NODES", "1") != "1":
            raise ValueError("the demonstration requires exactly one allocated node")
        workers = integer(os.environ.get("RAVEN_SCALING_WORKERS", ""), "workers", 1, MAX_WORKERS)
        raw_problem = os.environ.get("RAVEN_SCALING_PROBLEM_SIZE", "")
        problem_number = float(raw_problem)
        if not math.isfinite(problem_number) or not problem_number.is_integer():
            raise ValueError("this demonstration requires an integer problem_size")
        problem = integer(int(problem_number), "problem_size", 1, MAX_PROBLEM)
        repeat = integer(os.environ.get("RAVEN_SCALING_REPEAT", ""), "repeat", 1, 20)
        seed = integer(os.environ.get("RAVEN_SCALING_SEED", ""), "seed", 0, 2147483647)
        terms = integer(os.environ.get("RAVEN_PARAM_KERNEL_TERMS", ""), "kernel_terms", 1, 64)
        scale = number(os.environ.get("RAVEN_PARAM_SCALE", ""), "scale")
        unexpected = set(name for name in os.environ if name.startswith("RAVEN_PARAM_")) - {"RAVEN_PARAM_KERNEL_TERMS", "RAVEN_PARAM_SCALE"}
        if unexpected:
            raise ValueError("this example does not consume all exported user parameters")
        job_id = os.environ.get("SLURM_JOB_ID", "")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", job_id):
            raise ValueError("run requires a valid Slurm job environment; use local for explicit local execution")
        experiment_id = os.environ.get("RAVEN_SCALING_EXPERIMENT_ID", "")
        if not re.fullmatch(r"[a-f0-9]{64}", experiment_id):
            raise ValueError("run requires the reviewed experiment identity")
        cpus, gpus = observed_resources()
        if cpus is not None and cpus < workers:
            raise ValueError("allocated CPUs are fewer than the controlled worker count")
        context = "slurm"
    else:
        workers = integer(args.workers, "workers", 1, MAX_WORKERS)
        problem = integer(args.problem_size, "problem_size", 1, MAX_PROBLEM)
        repeat = integer(args.repeat, "repeat", 1, 20)
        seed = integer(args.seed, "seed", 0, 2147483647)
        terms = integer(args.kernel_terms, "kernel_terms", 1, 64)
        scale = number(args.scale, "scale")
        job_id, experiment_id, cpus, gpus, context = "local-" + uuid.uuid4().hex, None, None, None, "local"
    script_hash = digest_script(args.script or __file__)
    if hasattr(os, "sched_getaffinity") and len(os.sched_getaffinity(0)) < workers:
        raise ValueError("CPU affinity exposes fewer CPUs than the requested workers")
    workload_hash = digest_script(__file__)
    fingerprint = hashlib.sha256(json.dumps({"experiment_id": experiment_id, "workload_sha256": workload_hash,
                                             "measurement_context": context}, sort_keys=True).encode()).hexdigest()
    start = time.perf_counter()
    partitions = [(problem * i // workers, problem * (i + 1) // workers, seed, terms, scale) for i in range(workers)]
    with ProcessPoolExecutor(max_workers=workers) as executor:
        checksum = math.fsum(executor.map(kernel, partitions))
    elapsed = time.perf_counter() - start
    if digest_script(args.script or __file__) != script_hash or digest_script(__file__) != workload_hash:
        raise ValueError("script or workload changed during measurement; no comparable record is written")
    record = {"workers": workers, "runtime_seconds": elapsed, "problem_size": problem, "work_units": problem,
              "state": "COMPLETED", "repeat": repeat, "job_id": job_id, "seed": seed, "script_sha256": script_hash,
              "parameters": {"kernel_terms": terms, "scale": scale}, "cpus": cpus, "gpus": gpus,
              "measurement_context": context, "checksum": checksum, "workload_sha256": workload_hash, "fingerprint": fingerprint,
              "notes": "synthetic single-node CPU example; elapsed time includes process startup"}
    write_json(args.output, record)
    print(f"Measured {workers} workers in {elapsed:.6f}s; wrote {args.output}")


def combine(args):
    if not 1 <= len(args.paths) <= 1000:
        raise ValueError("combine accepts 1..1000 explicit record paths")
    records, consumed = [], 0
    for path in args.paths:
        before = os.stat(path, follow_symlinks=False)
        remaining = MAX_BYTES - consumed
        if not stat.S_ISREG(before.st_mode) or before.st_size > min(65536, remaining) or remaining <= 0:
            raise ValueError("combined input exceeds a regular-file or byte budget (64 KiB per file, 8 MiB aggregate)")
        record = load_json(path, max_bytes=min(65536, remaining), max_depth=16)
        after = os.stat(path, follow_symlinks=False)
        signature = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if signature(before) != signature(after):
            raise ValueError("record changed during collection")
        if not isinstance(record, dict) or "workers" not in record or "state" not in record:
            raise ValueError("combine inputs must be individual measured run records")
        consumed += after.st_size
        if consumed > MAX_BYTES:
            raise ValueError("combined input exceeds the 8 MiB read budget")
        records.append(record)
    write_json(args.output, {"version": 1, "kind": "raven.scaling-records", "records": records})
    print(f"Collected {len(records)} explicit records in {args.output}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    real = commands.add_parser("run", help="execute a reviewed Slurm workload using exported controls")
    local = commands.add_parser("local", help="explicitly execute a bounded local synthetic measurement")
    for command in (real, local):
        command.add_argument("--output", required=True)
        command.add_argument("--script", default=None)
    local.add_argument("--workers", type=int, default=1)
    local.add_argument("--problem-size", type=int, default=10000)
    local.add_argument("--repeat", type=int, default=1)
    local.add_argument("--seed", type=int, default=7)
    local.add_argument("--kernel-terms", type=int, default=8)
    local.add_argument("--scale", type=float, default=.001)
    collect = commands.add_parser("combine", help="combine explicit individual JSON paths; no directory scan")
    collect.add_argument("paths", nargs="+")
    collect.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        combine(args) if args.command == "combine" else run(args)
    except (OSError, ValueError, OverflowError) as exc:
        parser.exit(2, f"scaling example: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
