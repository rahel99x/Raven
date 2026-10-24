"""Actual MPI node examples with observed placement and checked diffusion.

Ranks own contiguous parts of one periodic field. Halo exchanges are real MPI
messages; gathering the modest demonstration field makes the numerical checks
and rank-zero reporting easy to inspect. This is not an MPI scaling benchmark.
"""

from __future__ import annotations

import math
import sys
import time

import common


class CoordinatedFailure(RuntimeError):
    """All ranks have observed the same validation or root-operation failure."""


def partition_bounds(size, rank, ranks):
    if (
        any(type(value) is not int for value in (size, rank, ranks))
        or not 1 <= ranks <= 128
        or size < ranks
        or not 0 <= rank < ranks
    ):
        raise ValueError("Choose 1–128 ranks with at least one field cell per rank")
    return rank * size // ranks, (rank + 1) * size // ranks


def advance_partition(partition, left_halo, right_halo):
    """Match common.advance's convex periodic stencil, including one-cell ranks."""
    if not partition:
        raise ValueError("Every MPI rank must own at least one field cell")
    padded = (left_halo, *partition, right_halo)
    return tuple(
        (1 - 2 * 0.15) * value + 0.15 * (padded[index] + padded[index + 2])
        for index, value in enumerate(partition)
    )


def exchange_halos(partition, communicator):
    rank, ranks = communicator.Get_rank(), communicator.Get_size()
    previous_rank, next_rank = (rank - 1) % ranks, (rank + 1) % ranks
    left = communicator.sendrecv(
        partition[-1],
        dest=next_rank,
        sendtag=11,
        source=previous_rank,
        recvtag=11,
    )
    right = communicator.sendrecv(
        partition[0],
        dest=previous_rank,
        sendtag=12,
        source=next_rank,
        recvtag=12,
    )
    return left, right


def advance_distributed(partition, communicator, sweeps):
    for _ in range(sweeps):
        left, right = exchange_halos(partition, communicator)
        partition = advance_partition(partition, left, right)
    return partition


def validate_world(placements, *, variant, field_size):
    """Check observed hosts, not requested node counts or launcher hints."""
    ranks = len(placements)
    if not 2 <= ranks <= 128 or field_size < ranks:
        raise CoordinatedFailure(
            "Node examples require 2–128 MPI ranks and at least one cell per rank; "
            "select the rank count with the site's MPI launcher, not --workers"
        )
    nodes = sorted({row["node"] for row in placements})
    if variant == "single_node" and len(nodes) != 1:
        raise CoordinatedFailure(
            "single_node requires all ranks on one observed hostname; "
            "request one node or choose multi_node.py"
        )
    if variant == "multi_node" and len(nodes) < 2:
        raise CoordinatedFailure(
            "multi_node requires at least two distinct observed hostnames. "
            "Two ranks on one workstation do not demonstrate multiple nodes. "
            "Use the cluster's supported launcher and a two-node allocation"
        )
    if variant not in {"single_node", "multi_node"}:
        raise CoordinatedFailure("Choose the single_node or multi_node entry point")
    return nodes


def root_operation(communicator, callback, label):
    """Propagate root failures before another rank enters its next collective."""
    value, error = None, None
    if communicator.Get_rank() == 0:
        try:
            value = callback()
        except Exception as exc:
            error = f"{label}: {type(exc).__name__}: {exc}"[:2048]
    error = communicator.bcast(error, root=0)
    if error is not None:
        raise CoordinatedFailure(error)
    return value


def evolve_partition(args, communicator):
    rank, ranks = communicator.Get_rank(), communicator.Get_size()
    initial = root_operation(
        communicator, lambda: common.initial_field(args.size), "Initial field"
    )
    parts = (
        [
            initial[slice(*partition_bounds(args.size, number, ranks))]
            for number in range(ranks)
        ]
        if rank == 0
        else None
    )
    local = communicator.scatter(parts, root=0)
    initial_energy = common.energy(initial) if rank == 0 else None
    final = None
    communicator.Barrier()
    started = time.monotonic()
    for step in range(1, args.steps + 1):
        local = advance_distributed(local, communicator, args.sweeps)
        gathered = communicator.gather(local, root=0)
        final = (
            tuple(value for part in gathered for value in part) if rank == 0 else None
        )

        def report():
            value = common.energy(final)
            if not math.isfinite(value) or value > initial_energy * (1 + 1e-12):
                raise ArithmeticError(
                    "Distributed diffusion energy increased or is not finite"
                )
            common.report_step(step, args, value)

        root_operation(communicator, report, "Scientific measurement")
        common.pace(started, step, args)
    communicator.Barrier()
    return initial, final, time.monotonic() - started


def numerical_validation(initial, final, args):
    reference = initial
    for _ in range(args.steps):
        reference = common.advance(reference, sweeps=args.sweeps)
    maximum_error = max(
        abs(actual - expected) for actual, expected in zip(final, reference)
    )
    checks = common.check_field(initial, final)
    if maximum_error > 1e-12:
        raise ArithmeticError("Distributed field differs from the sequential reference")
    return dict(
        checks,
        serial_reference_max_error=maximum_error,
        serial_reference_equivalent=True,
    )


def run(args, communicator, mpi, variant):
    rank, ranks = communicator.Get_rank(), communicator.Get_size()
    placement = dict(
        common.identity(), rank=rank, mpi_processor_name=mpi.Get_processor_name()
    )
    placements = communicator.allgather(placement)
    # A mismatched multi-program launch must fail collectively before halo work.
    configurations = communicator.allgather(
        (variant, args.seconds, args.steps, args.size, args.sweeps)
    )
    if len(set(configurations)) != 1:
        raise CoordinatedFailure(
            "Every MPI rank must use the same example and parameters"
        )
    nodes = validate_world(placements, variant=variant, field_size=args.size)
    args.workers = ranks
    root_operation(
        communicator, lambda: common.prepare_output(args), "Output directory"
    )
    initial, final, elapsed = evolve_partition(args, communicator)

    def publish():
        summary = {
            "variant": variant,
            "execution": "MPI ranks with real halo exchange",
            "ranks": ranks,
            "observed_nodes": nodes,
            "placements": placements,
            "placement_scope": "OS hostnames and MPI processor-name observations; site allocation enforcement is separate",
            "mpi_version": mpi.Get_library_version().replace("\x00", "").strip(),
            "elapsed_seconds": elapsed,
            "initial_energy": common.energy(initial),
            "final_energy": common.energy(final),
            "numerical_checks": numerical_validation(initial, final, args),
        }
        common.write_summary(args, summary)

    root_operation(communicator, publish, "Final artifacts")
    return 0


def main(variant, argv=None):
    parser = common.argument_parser(
        f"{variant.replace('_', ' ')}: actual MPI placement and two-minute checked diffusion",
        include_workers=False,
    )
    args = common.validate_args(parser, parser.parse_args(argv))
    try:
        from mpi4py import MPI
    except (ImportError, RuntimeError) as exc:
        parser.error(
            "Node examples require mpi4py and a compatible MPI runtime in the "
            f"application interpreter; load the site's environment: {exc}"
        )
    communicator = MPI.COMM_WORLD
    try:
        return run(args, communicator, MPI, variant)
    except CoordinatedFailure as exc:
        if communicator.Get_rank() == 0:
            print(f"{variant}: {exc}", file=sys.stderr, flush=True)
        return 2
    except Exception as exc:
        print(
            f"{variant}: rank {communicator.Get_rank()} failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
        # An unexpected rank-local failure must not strand peers in collectives.
        if communicator.Get_size() > 1:
            communicator.Abort(1)
        return 1
