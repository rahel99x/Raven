#!/usr/bin/env python3
"""Two-minute cooperative flight-recorder example, requiring only Raven/Python.

Pass the same --job-id/--attempt as the job's Raven run for automatic discovery.
An intentionally injected model perturbation triggers a bounded incident slice;
the numerical program continues after capture stops. No device is sampled or
attached, and elapsed spans do not imply consumed CPU time.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import time
import uuid

from raven.tracing.activity_trigger import TriggeredCapture


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument(
        "--job-id", default=os.environ.get("SLURM_JOB_ID", "local-trigger")
    )
    parser.add_argument(
        "--attempt", default=os.environ.get("RAVEN_RUN_ID") or uuid.uuid4().hex
    )
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--steps", type=int, default=240)
    parser.add_argument("--size", type=int, default=2048)
    parser.add_argument("--trigger-step", type=int)
    args = parser.parse_args(argv)
    if (
        not math.isfinite(args.seconds)
        or not 0 <= args.seconds <= 3600
        or not 4 <= args.steps <= 10000
        or not 8 <= args.size <= 100000
    ):
        parser.error("Choose 0–3600 seconds, 4–10,000 steps and 8–100,000 field points")
    incident = args.trigger_step if args.trigger_step is not None else args.steps // 2
    if not 1 <= incident <= args.steps:
        parser.error("--trigger-step must identify one of the program's steps")
    args.project.mkdir(mode=0o700, parents=True, exist_ok=True)
    field = [math.sin(2 * math.pi * index / args.size) for index in range(args.size)]
    recorder = TriggeredCapture(
        args.project,
        job_id=args.job_id,
        attempt=args.attempt,
        pre_seconds=5,
        post_seconds=5,
    )
    started = time.monotonic()
    with recorder:
        print("Incident receipt: " + str(recorder.receipt_path), flush=True)
        for step in range(1, args.steps + 1):
            with recorder.span("diffusion step"):
                if step == incident:
                    field[len(field) // 2] += 0.5
                field = [
                    value
                    + 0.2
                    * (
                        field[(index - 1) % args.size]
                        + field[(index + 1) % args.size]
                        - 2 * value
                    )
                    for index, value in enumerate(field)
                ]
                energy = sum(value * value for value in field) / len(field)
                if not math.isfinite(energy):
                    raise ArithmeticError(
                        "The numerical model produced a nonfinite result"
                    )
                recorder.emit(
                    "counter",
                    {
                        "name": "model energy",
                        "values": {"mean_squared_field": energy},
                        "units": "dimensionless",
                    },
                )
            if step == incident:
                recorder.trigger(f"Injected model perturbation at step {step}")
                print(
                    "Trace manifest: " + str(recorder.receipt["manifest"]), flush=True
                )
            if step == 1 or step == args.steps or step % max(1, args.steps // 10) == 0:
                print(
                    f"step={step}/{args.steps} energy={energy:.9g} capture={recorder.receipt['status']}",
                    flush=True,
                )
            target = started + args.seconds * step / args.steps
            time.sleep(max(0, target - time.monotonic()))
    print(
        json.dumps(
            {
                "job_id": args.job_id,
                "attempt": args.attempt,
                "steps": args.steps,
                "energy": energy,
                "elapsed_seconds": time.monotonic() - started,
                "receipt": str(recorder.receipt_path),
                "capture": recorder.receipt,
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
