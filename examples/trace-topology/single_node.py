#!/usr/bin/env python3
"""Run at least two MPI ranks on one actual node, with two-minute live pacing.

Example: srun --nodes=1 --ntasks=2 python single_node.py --output-dir RESULTS
Use the Python/MPI environment supported by your site; ordinary Python startup
does not create an MPI allocation or replace its launcher.
"""

from node_workload import main

if __name__ == "__main__":
    raise SystemExit(main("single_node"))
