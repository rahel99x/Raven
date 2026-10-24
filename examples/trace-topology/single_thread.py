#!/usr/bin/env python3
"""One scientific thread; the scheduler may move it between allowed CPUs."""

from cpu_workload import main

if __name__ == "__main__":
    raise SystemExit(main("single-thread"))
