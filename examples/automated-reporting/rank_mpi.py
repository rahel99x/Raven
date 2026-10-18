#!/usr/bin/env python3
"""Real mpi4py exchange and explicitly collective metric reporting.

Requires mpi4py built against the MPI runtime used by the launch command.
"""
from mpi4py import MPI

import raven


def main():
    comm = MPI.COMM_WORLD
    rank, size = comm.Get_rank(), comm.Get_size()
    for step in range(4):
        with raven.phase("halo-exchange"):
            incoming = comm.sendrecv(float(rank + 1), dest=(rank + 1) % size,
                                     source=(rank - 1) % size)
        # Every rank calls the same collective in the same iteration order.
        raven.metric("energy", incoming * incoming, kind="conserved", step=step,
                     unit="arbitrary", reduce="sum")
        raven.progress(step + 1, 4, unit="exchanges")
    if rank == 0:
        print(f"Completed real exchange across {size} MPI ranks.", flush=True)


if __name__ == "__main__":
    main()
