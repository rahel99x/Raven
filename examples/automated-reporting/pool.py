#!/usr/bin/env python3
"""Workers calculate; only the parent reports aggregate observations by default."""
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor

import raven


def calculate(start):
    value = sum(index * index for index in range(start, start + 10000))
    raven.metric("helper_value", value)  # Suppressed in helper processes.
    return value


def main():
    started = time.monotonic()
    # Spawn exercises fresh application interpreters and portable main guarding.
    with raven.phase("pool"), ProcessPoolExecutor(
            max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(calculate, range(0, 80000, 10000)))
    raven.metric("sum_of_squares", sum(results), kind="gauge")
    raven.metric("items_per_second", 80000 / (time.monotonic() - started),
                 kind="throughput", unit="items/s")
    raven.progress(8, 8, unit="chunks")
    print(f"sum_of_squares={sum(results)}", flush=True)


if __name__ == "__main__":
    main()
