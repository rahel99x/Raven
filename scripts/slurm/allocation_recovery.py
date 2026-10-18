#!/usr/bin/env python3
"""Actual governed orphan reconciliation in the disposable Slurm fixture only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

SOURCE = Path(os.environ.get("RAVEN_QUALIFICATION_ROOT", "/raven"))
sys.path.insert(0, str(SOURCE))


def child(job, token):
    from raven.raven_allocation_adapters import prepare
    from raven.raven_allocation_helper import OwnershipRecord
    from raven.raven_allocation_recovery import connection_binding, reconcile, receipt_path
    from raven.platform_session import wrap_backend
    from raven.slurm import Backend
    assert os.getuid() == 1100
    state = Path("/home/raven") / ("recovery-" + token)
    state.mkdir(mode=0o700)
    plan = prepare({"site": {"kind": "slurm", "cluster_id": "raven-ci"}, "connection": {"transport": "local"},
                    "execution": {"heavy_target": "slurm", "heavy_requires_allocation": True},
        "allocation": {"cpus_per_task": 1, "memory_mib": 128, "walltime_seconds": 60}})
    owner = OwnershipRecord(state, plan, token)
    owner.write({"request_id": token, "request_name": "raven-heavy-" + token, "job_id": job,
                 "ownership": "dedicated-allocation", "connection_binding": connection_binding(plan),
                 "created_at": time.time(), "cleanup_on_loss": True, "identity": {},
                 "state": "pending", "cleanup_uncertain": True})
    owner.close()
    backend = wrap_backend(Backend(), {"raven": {"collection": {}}, "collection_cache_dir": str(state / "cache")},
                           {"cluster": "raven-ci", "user": "raven"}, str(state))
    assert backend.ledger is not None
    first = reconcile(state, plan, backend)
    assert first["cleanup_uncertain"] and first["recovery"]["phase"] == "confirming", first
    until = first["recovery"]["next_retry"] + .1
    assert 0 <= until - time.time() <= 30
    while time.time() < until:
        time.sleep(min(.2, until - time.time()))
    second = reconcile(state, plan, backend)
    assert second["state"] == "light" and not second["cleanup_uncertain"], second
    durable = json.loads(receipt_path(state, plan).read_text())
    assert not durable["cleanup_uncertain"]
    print(json.dumps({"first": first, "second": second, "durable": durable,
                      "ledger_counts": backend.ledger.counts, "actual_owner_uid": os.getuid()}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--child", nargs=2, metavar=("JOB", "TOKEN"))
    args = parser.parse_args(argv)
    if args.child:
        child(*args.child)
        return 0
    if os.getuid() != 0 or not Path("/tmp/raven-services-started").is_file():
        parser.error("Run only in the owned disposable Slurm container")
    args.output.mkdir(parents=True, exist_ok=True)
    records = []
    owned = []
    def command(argv, timeout=20):
        result = subprocess.run(["runuser", "-u", "raven", "--", *argv], capture_output=True, text=True, timeout=timeout)
        records.append({"argv": argv, "exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            raise ValueError(result.stderr)
        return result.stdout
    manifest = {"schema": "raven.real-allocation-recovery/v1", "source_revision": args.revision,
                "source_sha256": {p: hashlib.sha256((SOURCE / p).read_bytes()).hexdigest()
                                  for p in ("raven/raven_allocation_recovery.py", "raven/platform_session.py", "scripts/slurm/allocation_recovery.py")},
                "scope": "real disposable Slurm; production governed backend; no production site claim"}
    try:
        manifest["version"] = command(["scontrol", "--version"]).strip()
        token = uuid.uuid4().hex
        for name in ("raven-heavy-" + token, "unrelated-owned-sentinel-" + token):
            job = command(["sbatch", "--parsable", "--hold", "--account=labx", "--mem=32M", "--time=00:01:00", "--job-name=" + name, "--wrap=true"]).strip().split(";")[0]
            assert job.isdecimal()
            owned.append(job)
        raw = command(["python3", str(SOURCE / "scripts/slurm/allocation_recovery.py"), "--child", owned[0], token], timeout=45)
        manifest["recovery"] = json.loads(raw)
        sentinel = command(["squeue", "-h", "-j", owned[1], "-o", "%T"]).strip()
        assert sentinel == "PENDING", sentinel
        manifest["unrelated_sentinel_preserved"] = True
        manifest["status"] = "passed"
    except Exception as exc:
        manifest.update(status="failed", error=str(exc))
    finally:
        for job in owned:
            subprocess.run(["runuser", "-u", "raven", "--", "scancel", job], capture_output=True, timeout=10)
        (args.output / "commands.json").write_text(json.dumps(records, indent=2) + "\n")
        (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({key: manifest.get(key) for key in ("status", "version", "error")}))
    return int(manifest["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
