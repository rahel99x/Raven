"""Real threads or affinity-constrained processes over the same heat stencil."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import multiprocessing
import os
from pathlib import Path
import threading
import time

import common
import raven

_ready = None
_local = threading.local()


def physical_cores(allowed, *, sysfs=Path("/sys/devices/system/cpu")):
    """Select one allowed logical CPU per proven physical core, never siblings."""
    representatives = {}
    for cpu in sorted(allowed):
        folder = sysfs / ("cpu" + str(cpu)) / "topology"
        try:
            socket = int((folder / "physical_package_id").read_text())
            core = int((folder / "core_id").read_text())
            die_path = folder / "die_id"
            die = int(die_path.read_text()) if die_path.exists() else None
        except (OSError, ValueError) as error:
            raise RuntimeError(
                "Cannot prove physical-core topology inside the allowed CPU set; "
                "this Linux example needs readable sysfs topology"
            ) from error
        if socket < 0 or core < 0 or die is not None and die < 0:
            raise RuntimeError("Physical-core topology contains unknown identifiers")
        key = socket, die, core
        representatives.setdefault(
            key,
            {"logical_cpu": cpu, "socket": socket, "die": die, "physical_core": core},
        )
    return list(representatives.values())


def _thread_ready(barrier):
    global _ready
    _ready = barrier


def _process_ready(cpus, barrier):
    global _ready
    cpu = cpus.get(timeout=15)
    os.sched_setaffinity(0, {cpu})
    if os.sched_getaffinity(0) != {cpu}:
        raise RuntimeError("Worker affinity did not retain its assigned logical CPU")
    _ready = barrier


def _partition(field, begin, end):
    if not getattr(_local, "ready", False):
        _local.ready = True
        _ready.wait(timeout=15)
    values = common.diffuse_partition(field, begin, end)
    return values, common.identity()


def _parallel_step(pool, field, workers, identities):
    pending = [
        pool.submit(
            _partition,
            field,
            index * len(field) // workers,
            (index + 1) * len(field) // workers,
        )
        for index in range(workers)
    ]
    pieces = []
    for future in pending:
        values, owner = future.result(timeout=30)
        identities[(owner["pid"], owner["native_tid"])] = owner
        pieces.append(values)
    return tuple([value for piece in pieces for value in piece])


def evolve(args, pool, initial, identities):
    field = initial
    started = time.monotonic()
    for step in range(1, args.steps + 1):
        with raven.phase("topology_diffusion"):
            if pool is None:
                field = common.advance(field, args.sweeps)
                owner = common.identity()
                identities[(owner["pid"], owner["native_tid"])] = owner
            else:
                for _ in range(args.sweeps):
                    field = _parallel_step(pool, field, args.workers, identities)
            common.report_step(step, args, common.energy(field))
        common.pace(started, step, args)
    return field, time.monotonic() - started


def main(variant, argv=None):
    args = common.parse_args(
        argv,
        description=__doc__,
        default_workers=1 if variant == "single-thread" else 2,
    )
    if variant == "single-thread" and args.workers != 1:
        raise ValueError("The single-thread example requires --workers 1")
    if variant != "single-thread" and args.workers < 2:
        raise ValueError(
            "This example needs --workers 2..8 to show distinct execution lanes"
        )
    original_affinity, core_plan = None, []
    if variant in {"single-core", "multi-core"}:
        if not hasattr(os, "sched_getaffinity") or not hasattr(os, "sched_setaffinity"):
            raise RuntimeError(
                "CPU-core examples need Linux sched_getaffinity/sched_setaffinity"
            )
        original_affinity = os.sched_getaffinity(0)
        if not original_affinity:
            raise RuntimeError("No CPUs are allowed by this process's cpuset")
        if variant == "single-core":
            core_plan = [{"logical_cpu": min(original_affinity)}]
        else:
            core_plan = physical_cores(original_affinity)
            if len(core_plan) < args.workers:
                raise RuntimeError(
                    f"Multi-core needs {args.workers} distinct allowed physical cores; "
                    f"only {len(core_plan)} are proven. SMT siblings are not separate physical cores."
                )
            core_plan = core_plan[: args.workers]
    common.prepare_output(args)
    initial, identities = common.initial_field(args.size), {}
    try:
        if variant == "single-thread":
            final, elapsed = evolve(args, None, initial, identities)
        elif variant in {"multi-thread", "single-core"}:
            if variant == "single-core":
                os.sched_setaffinity(0, {core_plan[0]["logical_cpu"]})
            barrier = threading.Barrier(args.workers)
            with ThreadPoolExecutor(
                max_workers=args.workers,
                thread_name_prefix="raven-science",
                initializer=_thread_ready,
                initargs=(barrier,),
            ) as pool:
                final, elapsed = evolve(args, pool, initial, identities)
        elif variant == "multi-core":
            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            try:
                for core in core_plan:
                    queue.put(core["logical_cpu"])
                barrier = context.Barrier(args.workers)
                with ProcessPoolExecutor(
                    max_workers=args.workers,
                    mp_context=context,
                    initializer=_process_ready,
                    initargs=(queue, barrier),
                ) as pool:
                    final, elapsed = evolve(args, pool, initial, identities)
            finally:
                queue.close()
                queue.join_thread()
        else:
            raise ValueError("Unknown CPU example variant: " + variant)
        expected = initial
        for _ in range(args.steps):
            expected = common.advance(expected, args.sweeps)
        if final != expected:
            raise ArithmeticError(
                "Partitioned diffusion differs from the serial reference"
            )
        common.write_summary(
            args,
            {
                "variant": variant,
                "elapsed_seconds": elapsed,
                "initial_energy": common.energy(initial),
                "final_energy": common.energy(final),
                "checks": dict(
                    common.check_field(initial, final), serial_equivalent=True
                ),
                "execution": list(identities.values()),
                "cpu_plan": core_plan,
                "coverage": "Actual execution identities and affinity; point observations do not establish continuous CPU residency",
            },
        )
    finally:
        if original_affinity is not None:
            os.sched_setaffinity(0, original_affinity)
    return 0
