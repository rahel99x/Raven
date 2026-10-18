#!/usr/bin/env python3
"""Trace a row-decomposed heat calculation across actual MPI processes.

Each rank exchanges periodic halo rows and advances its own part of the grid.
Rank zero publishes global scientific metrics and final artifacts. The default
two-minute run is deliberately paced; --pause 0 removes the demonstration delay.
mpi4py must be built for the MPI implementation selected by the site's launcher.
"""
from __future__ import annotations

import argparse
import math
import os
import socket
import sys
import time
from array import array
from pathlib import Path

import heat_lab

import raven


class CoordinatedFailure(RuntimeError):
    """Every rank deliberately leaves the same completed collective step."""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="shared artifact directory; result files are never replaced")
    parser.add_argument("--grid", type=int, default=32, help="cells per edge (8..128; must be at least the MPI rank count)")
    parser.add_argument("--steps", type=int, default=80, help="diffusion steps (1..2000; default: 80)")
    parser.add_argument("--pause", type=float, default=1.5, help="deliberate per-step delay on every rank, seconds (0..5; default: 1.5)")
    parser.add_argument("--seed", type=int, default=7, help="reproducible initial field seed (default: 7)")
    parser.add_argument("--alpha", type=float, default=0.20, help="stable diffusion coefficient (0 < alpha <= 0.25)")
    parser.add_argument("--fail-at-step", type=int, help="all ranks fail after this step's measurements")
    return heat_lab.validate_args(parser, parser.parse_args(argv))


def exchange_halos(partition, communicator):
    """Match both directions explicitly; Sendrecv also works for one rank."""
    rank, size = communicator.Get_rank(), communicator.Get_size()
    previous_rank, next_rank = (rank - 1) % size, (rank + 1) % size
    above = communicator.sendrecv(partition[-1], dest=next_rank, sendtag=11,
                                  source=previous_rank, recvtag=11)
    below = communicator.sendrecv(partition[0], dest=previous_rank, sendtag=12,
                                  source=next_rank, recvtag=12)
    return above, below


def advance_partition(partition, above, below, alpha):
    """Reuse the CPU stencil with two received halo rows, without padding cells."""
    padded = (above, *partition, below)
    return tuple([heat_lab.update_row(padded, row + 1, alpha) for row in range(len(partition))])


def collective_measurements(previous, current, total_cells, communicator, mpi):
    """Every rank enters the same collectives; only root receives the report."""
    values = [value for row in current for value in row]
    delta_squared = math.fsum([
        (after - before) ** 2
        for old_row, new_row in zip(previous, current)
        for before, after in zip(old_row, new_row)
    ])
    local = array("d", [math.fsum(values), delta_squared])
    totals = array("d", [0.0, 0.0])
    communicator.Allreduce([local, mpi.DOUBLE], [totals, mpi.DOUBLE], op=mpi.SUM)
    mean = totals[0] / total_cells
    # Two-pass variance avoids subtracting nearly equal squared quantities.
    local_variance = math.fsum([(value - mean) ** 2 for value in values])
    variance = communicator.reduce(local_variance, op=mpi.SUM, root=0)
    peak = communicator.reduce(max(values), op=mpi.MAX, root=0)
    if communicator.Get_rank() == 0:
        return {"residual_squared": totals[1] / total_cells,
                "peak_temperature": peak, "total_heat": totals[0],
                "thermal_variance": variance / total_cells}
    return None


@raven.measure("residual", kind="residual", step="step", unit="a.u.")
def measured_global_residual(residual_squared, step):
    return math.sqrt(residual_squared)


def report_global_step(observations, step, steps):
    """Called by rank zero only; other ranks never append scientific JSONL."""
    metrics = {"residual": measured_global_residual(observations["residual_squared"], step),
               **{key: observations[key] for key in ("peak_temperature", "total_heat", "thermal_variance")}}
    for name, kind, unit in (("peak_temperature", "gauge", "a.u."),
                             ("total_heat", "conserved", "a.u. * cells"),
                             ("thermal_variance", "objective", "a.u.^2")):
        raven.metric(name, metrics[name], kind=kind, unit=unit, step=step)
    raven.progress(step, steps, unit="steps", step=step)
    print(f"step={step:4d}/{steps} residual={metrics['residual']:.6g} "
          f"peak={metrics['peak_temperature']:.6g} heat={metrics['total_heat']:.12g}", flush=True)
    return metrics


def gather_snapshot(partition, step, bounds, communicator):
    parts = communicator.gather(partition, root=0)
    if communicator.Get_rank() != 0:
        return None, None
    field = tuple([row for part in parts for row in part])
    snapshot = heat_lab.analyze_snapshot(field, step, bounds, pool=None, workers=0)
    return field, snapshot


def evolve_distributed(partition, bounds, args, communicator, mpi):
    rank = communicator.Get_rank()
    interval = max(1, (args.steps + 7) // 8)
    final_field, initial_snapshot = gather_snapshot(partition, 0, bounds, communicator)
    snapshots = [initial_snapshot] if rank == 0 else []
    observations = collective_measurements(partition, partition, args.grid ** 2, communicator, mpi)
    if rank == 0:
        report_global_step(observations, 0, args.steps)
    metrics = None
    for step in range(1, args.steps + 1):
        previous = partition
        with raven.phase("mpi_diffusion"):
            above, below = exchange_halos(previous, communicator)
            partition = advance_partition(previous, above, below, args.alpha)
            observations = collective_measurements(previous, partition, args.grid ** 2, communicator, mpi)
            if rank == 0:
                metrics = report_global_step(observations, step, args.steps)
        if step % interval == 0 or step == args.steps:
            with raven.phase("mpi_snapshot_analysis"):
                final_field, snapshot = gather_snapshot(partition, step, bounds, communicator)
                if rank == 0:
                    snapshots.append(snapshot)
                    raven.metric("first_mode_amplitude", snapshot["first_mode_amplitude"], step=step, unit="a.u.")
        if step == args.fail_at_step:
            # All ranks finished this step and know the same requested failure.
            # A normal coordinated exit allows the metrics writer to drain.
            communicator.Barrier()
            raise CoordinatedFailure(f"Requested demonstration failure after step {step}; earlier recordings remain available.")
        if args.pause:
            time.sleep(args.pause)
    return final_field, metrics, snapshots


def prepare_root(args, ranks):
    if ranks > args.grid:
        raise ValueError(f"The grid has {args.grid} rows but MPI started {ranks} ranks; choose at most one rank per row.")
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name in ("summary.json", "final-field.csv"):
        if os.path.lexists(output / name):
            raise FileExistsError(f"Refusing to replace {output / name}; choose a new --output-dir.")
    initial = heat_lab.initialize_field(args.grid, args.seed)
    parts = [initial[rank * args.grid // ranks:(rank + 1) * args.grid // ranks] for rank in range(ranks)]
    bounds = (min([min(row) for row in initial]), max([max(row) for row in initial]))
    return output, initial, parts, bounds


def publish_root(args, output, initial, final, metrics, snapshots, ranks, mpi, elapsed):
    checks = heat_lab.numerical_checks(initial, final)
    if not checks["heat_conserved"] or not checks["maximum_principle"]:
        raise ArithmeticError(f"Numerical validation failed: {checks}")
    summary = {
        "schema": "raven.trace-lab/v1", "backend": "mpi",
        "model": "periodic two-dimensional heat diffusion; arbitrary temperature units",
        "parameters": {key: value for key, value in vars(args).items() if key != "output_dir"},
        "parent_pid": os.getpid(), "elapsed_seconds": elapsed,
        "deliberate_pause_seconds": args.steps * args.pause,
        "mpi": {"world_size": len(ranks), "ranks": ranks, "library_version": mpi.Get_library_version().strip(),
                "metrics_writer_rank": 0, "decomposition": "contiguous rows with periodic halo exchange"},
        "final_metrics": metrics, "checks": checks,
        "histogram_bounds": [min([min(row) for row in initial]), max([max(row) for row in initial])],
        "snapshots": snapshots,
        "field_csv": {"path": "final-field.csv", "rows": args.grid, "columns": args.grid, "header": False},
    }
    with raven.phase("write_results"):
        heat_lab.write_results(output, final, summary)
    raven.progress(args.steps, args.steps, unit="steps", step=args.steps)
    print(f"Completed: {output / 'summary.json'}", flush=True)


def run_world(communicator, mpi, argv):
    rank, ranks = communicator.Get_rank(), communicator.Get_size()
    parsed = None
    if rank == 0:
        try:
            parsed = (parse_args(argv), 0)
        except SystemExit as exc:
            parsed = (None, int(exc.code or 0))
    args, exit_code = communicator.bcast(parsed, root=0)
    if args is None:
        return exit_code
    output = initial = parts = bounds = None
    preparation_error = None
    if rank == 0:
        try:
            with raven.phase("initialize"):
                output, initial, parts, bounds = prepare_root(args, ranks)
        except (OSError, ValueError) as exc:
            preparation_error = str(exc)
    preparation_error = communicator.bcast(preparation_error, root=0)
    if preparation_error:
        if rank == 0:
            print("Trace lab MPI: " + preparation_error, file=sys.stderr, flush=True)
        return 2
    partition = communicator.scatter(parts, root=0)
    bounds = communicator.bcast(bounds, root=0)
    rank_details = communicator.gather({
        "rank": rank, "pid": os.getpid(), "node": socket.gethostname(),
        "row_start": rank * args.grid // ranks, "row_stop": (rank + 1) * args.grid // ranks,
    }, root=0)
    started = time.monotonic()
    try:
        final, metrics, snapshots = evolve_distributed(partition, bounds, args, communicator, mpi)
    except CoordinatedFailure as exc:
        if rank == 0:
            print("Trace lab MPI: " + str(exc), file=sys.stderr, flush=True)
        return 1
    publication_error = None
    if rank == 0:
        try:
            publish_root(args, output, initial, final, metrics, snapshots, rank_details, mpi, time.monotonic() - started)
        except (OSError, ArithmeticError) as exc:
            publication_error = str(exc)
    publication_error = communicator.bcast(publication_error, root=0)
    if publication_error:
        if rank == 0:
            print("Trace lab MPI: " + publication_error, file=sys.stderr, flush=True)
        return 1
    return 0


def main(argv=None):
    try:
        from mpi4py import MPI
    except (ImportError, RuntimeError) as exc:
        print("Trace lab MPI: install mpi4py for the active MPI/Python environment and use its matching mpiexec. "
              f"The MPI binding could not load: {exc}", file=sys.stderr)
        return 2
    communicator = MPI.COMM_WORLD
    try:
        return run_world(communicator, MPI, argv)
    except BaseException as exc:  # noqa: BLE001 - any lost rank can strand peers
        # A rank-local unexpected failure must not leave peers in a collective.
        # Aborting can lose buffered recordings; the coordinated failure option
        # above instead exercises a clean failure with retained prior evidence.
        print(f"Trace lab MPI rank {communicator.Get_rank()}: {type(exc).__name__}: {exc}; aborting this MPI world.",
              file=sys.stderr, flush=True)
        communicator.Abort(1)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
