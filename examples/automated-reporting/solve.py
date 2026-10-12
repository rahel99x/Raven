#!/usr/bin/env python3
"""A small real calculation with optional, language-neutral Raven reporting.

No Raven import or application-environment installation is required. Run directly
to compute without reporting, or through ``raven launch`` to retain observations.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys


def report(*args: str) -> None:
    """Use the pinned launch helper, when present, without hiding workload output."""
    python = os.environ.get("RAVEN_REPORT_PYTHON")
    helper = os.environ.get("RAVEN_REPORT_HELPER")
    if not (python and helper and os.environ.get("RAVEN_RUN_DIR")):
        return
    try:
        subprocess.run([python, "-I", helper, *args], check=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        # Scientific computation does not become a failure merely because an
        # optional reporting command is unavailable or its storage is full.
        print(f"Optional reporting unavailable: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--terms-per-step", type=int, default=2000)
    parser.add_argument("--fail-after", type=int, help="return 7 after this measured step")
    args = parser.parse_args(argv)
    if min(args.steps, args.terms_per_step) < 1 or args.steps * args.terms_per_step > 2_000_000:
        parser.error("choose positive counts with at most two million terms")
    if args.fail_after is not None and not 1 <= args.fail_after <= args.steps:
        parser.error("--fail-after must be between 1 and --steps")
    total = 0.0
    for step in range(1, args.steps + 1):
        first = (step - 1) * args.terms_per_step
        for index in range(first, first + args.terms_per_step):
            total += (1.0 if index % 2 == 0 else -1.0) / (2 * index + 1)
        estimate = 4 * total
        error = abs(math.pi - estimate)
        print(f"step={step} pi_estimate={estimate:.15g} absolute_error={error:.15g}", flush=True)
        report("metric", "--value", f"pi_estimate={estimate!r}", "--value", f"absolute_error={error!r}",
               "--step", str(step), "--phase", "solve", "--completed", str(step),
               "--total", str(args.steps), "--unit", "steps")
        if step == args.fail_after:
            print("Requested demonstration failure after a retained observation.", file=sys.stderr)
            return 7
    run = os.environ.get("RAVEN_RUN_DIR")
    if run:
        output = Path(run) / "outputs" / "result.json"
        try:
            with output.open("x", encoding="utf-8") as stream:
                json.dump({"pi_estimate": estimate, "absolute_error": error,
                           "terms": args.steps * args.terms_per_step}, stream, allow_nan=False, indent=2)
                stream.write("\n")
            report("artifact", "outputs/result.json", "--format", "json")
        except OSError as exc:
            print(f"Optional result file unavailable: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
