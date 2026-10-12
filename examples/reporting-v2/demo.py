#!/usr/bin/env python3
"""A small measured computation demonstrating language-neutral report commands."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=5)
    parser.add_argument("--terms-per-step", type=int, default=1000)
    args = parser.parse_args()
    if min(args.steps, args.terms_per_step) < 1 or args.steps * args.terms_per_step > 2_000_000:
        parser.error("choose positive counts with at most two million terms")
    if not os.environ.get("RAVEN_RUN_DIR"):
        parser.error("launch this example through run.sh")
    total = 0.0
    for step in range(1, args.steps + 1):
        for index in range((step - 1) * args.terms_per_step, step * args.terms_per_step):
            total += (1.0 if index % 2 == 0 else -1.0) / (2 * index + 1)
        estimate = 4 * total
        subprocess.run([os.environ["RAVEN_REPORT_PYTHON"], os.environ["RAVEN_REPORT_HELPER"],
                        "metric", "--value", f"pi_estimate={estimate}", "--value",
                        f"absolute_error={abs(math.pi - estimate)}", "--step", str(step),
                        "--completed", str(step), "--total", str(args.steps), "--unit", "steps"], check=True)
        print(f"step={step} pi={estimate:.12f}", flush=True)
    output = Path(os.environ["RAVEN_RUN_DIR"]) / "outputs/result.json"
    with output.open("x", encoding="utf-8") as stream:
        json.dump({"estimated_pi": estimate, "absolute_error": abs(math.pi - estimate),
                   "terms": args.steps * args.terms_per_step}, stream, allow_nan=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
