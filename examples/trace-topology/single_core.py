#!/usr/bin/env python3
"""Multiple threads confined to one allowed logical CPU on one physical core."""

from cpu_workload import main

if __name__ == "__main__":
    raise SystemExit(main("single-core"))
