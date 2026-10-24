"""Shared numerical and artifact contracts for the topology examples."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import socket
import threading
import time


def argument_parser(description, *, default_workers=2, include_workers=True):
    parser = argparse.ArgumentParser(description=description, allow_abbrev=False)
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="new results directory; existing directories are refused",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=120,
        help="minimum paced computation duration, seconds (default: 120)",
    )
    parser.add_argument("--steps", type=int, default=80)
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument("--sweeps", type=int, default=4)
    if include_workers:
        parser.add_argument("--workers", type=int, default=default_workers)
    return parser


def validate_args(parser, args):
    if not math.isfinite(args.seconds) or not 0 <= args.seconds <= 3600:
        parser.error("seconds must be finite and between 0 and 3600")
    for value, name, low, high in (
        (args.steps, "steps", 1, 10000),
        (args.size, "size", 8, 262144),
        (args.sweeps, "sweeps", 1, 100),
    ):
        if not low <= value <= high:
            parser.error(f"{name} must be between {low} and {high}")
    if hasattr(args, "workers") and not 1 <= args.workers <= 8:
        parser.error("workers must be between 1 and 8")
    if not str(args.output_dir).isprintable():
        parser.error("output directory must not contain control characters")
    args.output_dir = args.output_dir.expanduser().absolute()
    return args


def parse_args(argv=None, *, description, default_workers=2):
    parser = argument_parser(description, default_workers=default_workers)
    return validate_args(parser, parser.parse_args(argv))


def initial_field(size):
    return tuple(
        [
            math.sin(2 * math.pi * index / size)
            + 0.25 * math.cos(4 * math.pi * index / size)
            for index in range(size)
        ]
    )


def diffuse_partition(field, begin, end):
    """One stable periodic heat stencil over an explicit immutable partition."""
    alpha = 0.15
    return tuple(
        [
            (1 - 2 * alpha) * field[index]
            + alpha * (field[index - 1] + field[(index + 1) % len(field)])
            for index in range(begin, end)
        ]
    )


def diffuse(field):
    return diffuse_partition(field, 0, len(field))


def advance(field, sweeps=4):
    for _ in range(sweeps):
        field = diffuse(field)
    return field


def energy(field):
    return math.fsum([value * value for value in field]) / len(field)


def check_field(initial, final):
    if len(initial) != len(final) or not initial:
        raise ArithmeticError("Diffusion changed the field size")
    finite = all([math.isfinite(value) for value in final])
    initial_mean = math.fsum(initial) / len(initial)
    final_mean = math.fsum(final) / len(final)
    initial_energy, final_energy = energy(initial), energy(final)
    checks = {
        "finite": finite,
        "mean_conserved": abs(initial_mean - final_mean) <= 1e-11,
        "energy_nonincreasing": final_energy <= initial_energy * (1 + 1e-10),
        "initial_energy": initial_energy,
        "final_energy": final_energy,
        "initial_mean": initial_mean,
        "final_mean": final_mean,
    }
    if not all(
        checks[key] for key in ("finite", "mean_conserved", "energy_nonincreasing")
    ):
        raise ArithmeticError("Diffusion numerical validation failed: " + str(checks))
    return checks


def identity():
    return {
        "pid": os.getpid(),
        "native_tid": threading.get_native_id(),
        "thread": threading.current_thread().name,
        "node": socket.gethostname(),
        "allowed_cpu_ids": sorted(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
    }


def pace(started, step, args):
    time.sleep(max(0, started + args.seconds * step / args.steps - time.monotonic()))


def report_step(step, args, value):
    import raven

    raven.metric("diffusion_energy", value, kind="objective", unit="a.u.^2", step=step)
    raven.progress(step, args.steps, unit="steps", step=step)
    if step == 1 or step == args.steps or step % 10 == 0:
        print(f"step={step}/{args.steps} energy={value:.10g}", flush=True)


def prepare_output(args):
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    args._prepared_output = True
    return args.output_dir


def write_summary(args, payload):
    if not getattr(args, "_prepared_output", False):
        prepare_output(args)
    value = dict(payload)
    value.update(
        schema="raven.trace-topology/v1",
        parameters={
            key: getattr(args, key)
            for key in ("seconds", "steps", "size", "sweeps", "workers")
            if hasattr(args, key)
        },
    )
    encoded = json.dumps(value, allow_nan=False, ensure_ascii=True, indent=2) + "\n"
    if len(encoded.encode("utf-8")) > 1 << 20:
        raise ValueError("Example summary exceeds its 1 MiB artifact budget")
    path = args.output_dir / "summary.json"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(encoded)
    print("Summary: " + str(path), flush=True)
    return path
