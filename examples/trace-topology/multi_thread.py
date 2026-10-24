#!/usr/bin/env python3
"""Concurrent Python threads; CPython's GIL does not imply CPU parallelism."""

from cpu_workload import main

if __name__ == "__main__":
    raise SystemExit(main("multi-thread"))
