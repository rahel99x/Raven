#!/usr/bin/env python3
"""Real finite-series calculation using Raven's optional metrics micro-API.

Launch through Raven, install Raven, or vendor raven.py beside this file first.
This example intentionally permits intermediate telemetry to be coalesced.
"""
from __future__ import annotations

import argparse
import math

import raven


@raven.measure("absolute_error", kind="error", step="step")
def measured_error(estimate, step):
    return abs(math.pi - estimate)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--terms-per-step", type=int, default=2000)
    args = parser.parse_args()
    if min(args.steps, args.terms_per_step) < 1 or args.steps * args.terms_per_step > 2_000_000:
        parser.error("choose positive counts with at most two million terms")
    total = 0.0
    with raven.phase("solve"):
        for step in range(1, args.steps + 1):
            first = (step - 1) * args.terms_per_step
            for index in range(first, first + args.terms_per_step):
                total += (1.0 if index % 2 == 0 else -1.0) / (2 * index + 1)
            estimate = 4 * total
            error = measured_error(estimate, step)
            raven.metric("pi_estimate", estimate, kind="gauge", step=step)
            raven.progress(step, args.steps, unit="steps")
            print(f"step={step} pi_estimate={estimate:.15g} absolute_error={error:.15g}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
