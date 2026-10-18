#!/usr/bin/env python3
"""Independent pre-start forecast points on an owned isolated Slurm fixture."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(os.environ.get("RAVEN_QUALIFICATION_ROOT", "/raven"))
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    if not Path("/tmp/raven-services-started").exists():
        raise SystemExit("Only the owned isolated Slurm qualification fixture is supported")
    os.environ["TZ"] = "UTC"
    time.tzset()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    calls, owned, observations, refused = [], [], [], []
    deadline = time.monotonic() + 420
    def run(argv):
        start = time.time()
        result = subprocess.run(["runuser", "-u", "raven", "--", *argv], capture_output=True, text=True, timeout=10)
        calls.append(dict(argv=argv, start=start, seconds=time.time()-start, returncode=result.returncode, stdout=result.stdout, stderr=result.stderr))
        if result.returncode:
            raise RuntimeError(str(argv) + ": " + result.stderr)
        return result.stdout.strip()
    def stamp(value):
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()
    def check():
        if time.monotonic() >= deadline:
            raise RuntimeError("Forecast capture exceeded its 420-second budget")
    try:
        version = run(["scontrol", "--version"])
        config = run(["scontrol", "show", "config"])
        policy = hashlib.sha256(config.encode()).hexdigest()
        context = dict(policy_digest=policy, reservations_visible=True, policy_complete=True)
        request = dict(name="owned-sleep-one-second-v1", partition="local", account="labx", qos="normal",
                       cpus=1, nodes=1, gpus=0, mem_bytes=32*1048576, time_seconds=60)
        folder = "/home/raven/forecast-" + uuid.uuid4().hex
        run(["mkdir", "-p", folder])
        for index in range(65):
            check()
            common = ["sbatch", "--parsable", "--account=labx", "--partition=local", "--nodes=1", "--mem=32M", "--time=00:01:00", "--output=" + folder + "/%j.log"]
            holder = run([*common, "--cpus-per-task=2", "--job-name=raven-forecast-holder", "--wrap=sleep 2"]).split(";", 1)[0]
            if not holder.isdecimal(): raise RuntimeError("Invalid owned allocation identity")
            owned.append(holder)
            while "RUNNING" not in run(["squeue", "-h", "-j", holder, "-o", "%T"]):
                check()
                time.sleep(.05)
            target = run([*common, "--hold", "--cpus-per-task=1", "--job-name=raven-forecast-target", "--wrap=sleep 1"]).split(";", 1)[0]
            if not target.isdecimal(): raise RuntimeError("Invalid owned target identity")
            owned.append(target)
            snapshot = run(["squeue", "--start", "--noheader", "--jobs=" + holder + "," + target, "--format=%i|%T|%V|%S|%M|%R"])
            # Known command duration supplies a prospective point. It is not
            # corrected using the subsequently observed start or completion.
            issued = time.time()
            point = dict(id=target, request=request, context=context, origin="recorded", issued_at=issued,
                         model_wait_seconds=2., metadata={"capture_index": index, "snapshot": snapshot,
                         "model_basis": "Prospectively declared 2-second holder program; includes unknown startup/scheduler timing",
                         "qualification_limit": "Owned two-CPU fixture with known demand, not a production-site cohort"})
            with (output / "issued.jsonl").open("a") as handle:
                handle.write(json.dumps(point) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            run(["scontrol", "release", target])
            while run(["squeue", "-h", "--jobs=" + holder + "," + target, "-o", "%i"]):
                check()
                time.sleep(.1)
            while True:
                raw = run(["sacct", "-n", "-P", "-X", "--duplicates", "--jobs=" + target, "--user=raven", "--starttime=now-1hours", "--endtime=now", "--format=JobID,State,Submit,Start,End,AllocCPUS,NNodes"])
                rows = [line.split("|") for line in raw.splitlines() if line]
                if len(rows) == 1 and len(rows[0]) >= 7 and rows[0][1] == "COMPLETED" and rows[0][4] not in ("", "Unknown"):
                    row = rows[0]
                    break
                check()
                time.sleep(.1)
            if row[0] != target or row[5:7] != ["1", "1"]:
                raise RuntimeError("Actual target identity or allocation differs")
            point.update(submit=stamp(row[2]), actual_start=stamp(row[3]), actual_end=stamp(row[4]), state="completed")
            if not point["submit"] <= issued < point["actual_start"] < point["actual_end"]:
                refused.append(dict(id=target, reason="Coarse scheduler timestamp cannot prove strict pre-start ordering", observed=point))
            else:
                observations.append(point)
            (output / "observations.json").write_text(json.dumps(observations, indent=2)+"\n")
        from raven.forecast import scenarios
        document = dict(schema="raven.forecast-scenarios/v1", now=time.time(), request=request, alternatives=[],
                        context=context, observations=observations, model_wait_seconds=2., wait_threshold_seconds=3.,
                        metadata={"version": version, "revision": args.revision, "refused": refused,
                                  "implementation_sha256": {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                                                           ("raven/forecast.py", "raven/forecast_scenarios.py", "scripts/slurm/forecast_observations.py")}})
        result = scenarios(document)
        (output / "model.json").write_text(json.dumps(document, indent=2)+"\n")
        (output / "result.json").write_text(json.dumps(result, indent=2)+"\n")
        (output / "manifest.json").write_text(json.dumps(dict(revision=args.revision, version=version, accepted=len(observations), refused=refused, evaluation=result["evaluation"]), indent=2)+"\n")
        print(json.dumps(dict(status="captured", samples=len(observations), refused=len(refused), evaluation=result["evaluation"])))
    finally:
        if owned:
            subprocess.run(["runuser", "-u", "raven", "--", "scancel", *owned], capture_output=True, timeout=10)
        (output / "commands.json").write_text(json.dumps(calls, indent=2)+"\n")


if __name__ == "__main__":
    main()
