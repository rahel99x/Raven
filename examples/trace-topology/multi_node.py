#!/usr/bin/env python3
"""Run MPI diffusion across at least two distinct observed compute nodes.

Example inside a suitable allocation:
  srun --nodes=2 --ntasks=2 --ntasks-per-node=1 python multi_node.py --output-dir RESULTS

Two local ranks are rejected; the example never relabels local processes as
multiple nodes. Shared project/output storage and site-approved MPI are required.
"""

from node_workload import main

if __name__ == "__main__":
    raise SystemExit(main("multi_node"))
