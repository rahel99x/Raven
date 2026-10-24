#!/usr/bin/env python3
"""Spawned Python processes pinned to distinct proven physical CPU cores."""

from cpu_workload import main

if __name__ == "__main__":
    raise SystemExit(main("multi-core"))
