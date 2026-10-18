#!/usr/bin/env python3
"""Capture two independent chronological queues inside the isolated Slurm image.

Not a production-cluster tool: requires the qualification-container sentinel,
the dedicated raven login, labx/local allocation, and writes only its own IDs.
"""
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

sys.path.insert(0, "/raven")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    os.environ["TZ"] = "UTC"
    time.tzset()
    if not Path("/tmp/raven-services-started").exists():
        raise SystemExit("This capture requires the isolated Slurm qualification fixture")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    records, owned = [], []
    def run(argv, *, owner=True):
        started = time.time()
        command = ["runuser", "-u", "raven", "--", *argv] if owner else argv
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        records.append({"argv": argv, "start": started, "seconds": time.time() - started,
                        "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            raise RuntimeError(str(argv) + ": " + result.stderr)
        return result.stdout.strip()
    def epoch(value):
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()
    try:
        version = run(["scontrol", "--version"])
        configuration = run(["scontrol", "show", "config"])
        nodes = run(["scontrol", "show", "nodes", "-o"])
        from raven.slurm import parse_kv
        node = parse_kv(nodes.splitlines()[0])
        cpus = int(node.get("CPUEfctv") or node["CPUTot"])
        if cpus < 2:
            raise RuntimeError("Queue fixture requires at least two configured CPUs")
        folder = "/home/raven/queue-model-" + uuid.uuid4().hex
        run(["mkdir", "-p", folder])
        traces = []
        workloads = [(("a", cpus, 2, None), ("b", 1, 2, None), ("c", 1, 2, "b")),
                     (("d", 1, 4, None), ("e", cpus, 2, None), ("f", 1, 2, None), ("g", 1, 2, "e"))]
        for phase, workload in enumerate(workloads):
            identifiers = {}
            for name, requested_cpus, seconds, dependency in workload:
                argv = ["sbatch", "--parsable", "--hold", "--account=labx", "--partition=local", "--nodes=1",
                        "--cpus-per-task=" + str(requested_cpus), "--mem=32M", "--time=00:01:00",
                        "--job-name=raven-queue-model-" + name, "--output=" + folder + "/%j.log"]
                if dependency:
                    argv.append("--dependency=afterok:" + identifiers[dependency])
                jid = run([*argv, "--wrap=sleep " + str(seconds)]).split(";", 1)[0]
                if not jid.isdecimal():
                    raise RuntimeError("Scheduler did not return one exact job ID")
                identifiers[name] = jid
                owned.append(jid)
            released = int(time.time())
            run(["scontrol", "release", ",".join(identifiers.values())])
            deadline = time.monotonic() + 60
            while run(["squeue", "--noheader", "--jobs=" + ",".join(identifiers.values()), "--format=%i|%T"]):
                if time.monotonic() > deadline:
                    raise RuntimeError("Owned queue did not complete within the qualification budget")
                time.sleep(.25)
            captured = []
            while len(captured) != len(workload):
                raw = run(["sacct", "--noheader", "--parsable2", "--duplicates", "--allocations",
                           "--jobs=" + ",".join(identifiers.values()), "--starttime=now-1hours", "--endtime=now",
                           "--format=JobID,State,Start,End,AllocCPUS,NNodes,Submit,Eligible"])
                rows = {row.split("|")[0]: row.split("|") for row in raw.splitlines() if row}
                captured = []
                for name, requested_cpus, seconds, dependency in workload:
                    fields = rows.get(identifiers[name], [])
                    if len(fields) < 8 or fields[1] != "COMPLETED" or fields[3] in ("Unknown", ""):
                        continue
                    if int(fields[4]) != requested_cpus or int(fields[5]) != 1:
                        raise RuntimeError("Actual resource allocation differs from the declared homogeneous model subset")
                    captured.append({"id": identifiers[name], "submit": released, "duration": seconds, "wall_limit": 60,
                        "nodes": 1, "cpus_per_node": requested_cpus, "memory_mb_per_node": 32, "gpus_per_node": 0,
                        "priority": 1, "account": "labx", "qos": "normal", "depends": [identifiers[dependency]] if dependency else [],
                        "actual_start": epoch(fields[2]), "actual_end": epoch(fields[3]),
                        "metadata": {"submitted_at": fields[6], "eligible_at": fields[7], "program": "sleep " + str(seconds),
                                     "arrival_semantics": "explicitly observed release of owned held jobs; dependency outcomes remain modeled"}})
                if len(captured) != len(workload):
                    if time.monotonic() > deadline:
                        raise RuntimeError("Completed accounting remained incomplete")
                    time.sleep(.25)
            traces.append({"id": "queue-" + str(phase), "role": "calibration" if phase == 0 else "validation",
                           "origin": "recorded", "jobs": captured,
                           "source_sha256": hashlib.sha256(raw.encode()).hexdigest()})
        model = {"schema": "raven.queue-model/v1", "id": "slurm-independent-queue-holdout", "seed": 0,
                 "nodes": [{"id": node["NodeName"], "cpus": cpus, "memory_mb": int(node["RealMemory"]), "gpus": 0}],
                 "policy": {"backfill": "sched/backfill" in configuration, "fairshare_weight": 0, "usage_half_life_seconds": 86400,
                            "accounts": {"labx": {"weight": 1, "usage_cpu_seconds": 0}},
                            "qos": {"normal": {"max_running": 1000, "max_cpus": 100000, "max_wall_seconds": 86400}}},
                 "jobs": traces[1]["jobs"], "traces": traces, "hidden_delay_seconds": 10,
                 "metadata": {"version": version, "revision": args.revision, "environment": "isolated single-node real Slurm container",
                              "implementation_sha256": {name: hashlib.sha256(Path("/raven", name).read_bytes()).hexdigest()
                                                       for name in ("raven/slurm_simulator.py", "scripts/slurm/queue_observations.py")},
                              "qualification_limit": "Two tiny independent workloads; not site-scale efficacy or hidden-state fidelity",
                              "policy_basis": "Dedicated qualification fixture; no preemption, no reservations, labx/normal defaults"}}
        from raven.slurm_simulator import simulate
        result = simulate(model)
        (output / "model.json").write_text(json.dumps(model, indent=2) + "\n")
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        (output / "manifest.json").write_text(json.dumps({"revision": args.revision, "version": version, "commands": records}, indent=2) + "\n")
        print(json.dumps({"status": "captured", "output": str(output), "validation": result["validation"]}))
    finally:
        if owned:
            subprocess.run(["runuser", "-u", "raven", "--", "scancel", *owned], capture_output=True, timeout=10)
        (output / "commands.json").write_text(json.dumps(records, indent=2) + "\n")


if __name__ == "__main__":
    main()
