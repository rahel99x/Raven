#!/usr/bin/env python3
"""Native scheduler and project services on an owned real Slurm endpoint.

Run through qualify_native_slurm.py, never directly against a personal site.
All jobs have a bounded time/resource request and a reviewed exact owner/attempt.
The fixture has task/none; this does not qualify cgroup or physical GPU behavior.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

SOURCE = Path(os.environ.get("RAVEN_QUALIFICATION_ROOT", "/raven"))
sys.path.insert(0, str(SOURCE))


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    path.chmod(0o600)


def source_evidence():
    files = sorted((SOURCE / "raven").rglob("*.py")) + [
        SOURCE / "scripts/slurm/native_scheduler.py",
        SOURCE / "scripts/slurm/entrypoint.sh",
    ]
    hashes = {
        str(path.relative_to(SOURCE)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }
    return dict(
        files=hashes,
        tree_sha256=hashlib.sha256(
            json.dumps(hashes, sort_keys=True).encode()
        ).hexdigest(),
    )


def history_until(scheduler, expected, *, timeout=20):
    """Controller completion precedes slurmdbd publication; wait with a bound."""
    deadline = time.monotonic() + timeout
    while True:
        history = scheduler.jobs(history=True)
        if expected.issubset({row["id"] for row in history}):
            return history
        if time.monotonic() >= deadline:
            raise ValueError(
                "Owned completed attempts did not reach bounded native accounting before its deadline"
            )
        time.sleep(1)


def qualify(root, revision):
    from raven.engineering import experiments, qualification
    from raven.schedulers import create
    from raven.slurm import Backend

    if os.getuid() != 1100 or not Path("/tmp/raven-services-started").is_file():
        raise ValueError(
            "Native qualification requires the disposable fixture's raven login"
        )
    project, output = root / "project", root / "results"
    project.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    os.chdir(project)  # Native default stdout must be writable by this login.
    report = dict(
        schema="raven.native-slurm-qualification/v1",
        source_revision=revision,
        source_evidence=source_evidence(),
        environment="real Slurm, one owned disposable Linux container",
        scope=dict(cluster="raven-ci", user="raven", account="labx"),
        checks=[],
        commands=[],
        production_qualified=False,
        unqualified=[
            "production site permissions and deployment",
            "SSH",
            "multi-node",
            "physical GPU",
            "Slurm task/cgroup enforcement",
            "non-Slurm schedulers",
        ],
    )
    started, owned, terminal_attempts = time.monotonic(), [], set()

    class RecordingBackend(Backend):
        def inspect_command(self, argv, timeout, limit, cancel=None, *, depth=0):
            before = time.monotonic()
            entry = dict(
                argv=list(argv), timeout_seconds=timeout, output_limit_bytes=limit
            )
            try:
                raw = super().inspect_command(argv, timeout, limit, cancel, depth=depth)
                name = f"native-command-{len(report['commands']):04d}.stdout.txt"
                (output / name).write_text(raw)
                (output / name).chmod(0o600)
                entry.update(
                    status="success",
                    stdout=dict(
                        file=name,
                        sha256=hashlib.sha256(raw.encode()).hexdigest(),
                        bytes=len(raw.encode()),
                    ),
                )
                return raw
            except Exception as exc:
                entry.update(status="error", error=type(exc).__name__ + ": " + str(exc))
                raise
            finally:
                entry["seconds"] = time.monotonic() - before
                report["commands"].append(entry)
                write_json(output / "commands.json", report["commands"])

    scheduler = create(
        "slurm",
        RecordingBackend(),
        report["scope"],
        journal_dir=str(root / "actions"),
        timeout=8,
        max_bytes=4 << 20,
        max_records=1000,
        min_interval=0.2,
    )

    def check(label, condition, **evidence):
        report["checks"].append(dict(name=label, passed=bool(condition), **evidence))
        if not condition:
            raise ValueError("Native qualification failed: " + label)

    def wait_observed(identifier, states, *, timeout=30):
        until, errors = time.monotonic() + timeout, []
        while time.monotonic() < until:
            try:
                record = scheduler.observe(identifier)
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append(str(exc))
            else:
                if record["native_state"] in states or record["state"] in states:
                    if record["state"] in {"COMPLETED", "CANCELLED", "FAILED"}:
                        terminal_attempts.add(
                            json.dumps(record["identity"], sort_keys=True)
                        )
                    return record
                if record["state"] in {"COMPLETED", "CANCELLED", "FAILED"}:
                    write_json(
                        output / ("unexpected-terminal-" + identifier + ".json"), record
                    )
                    raise ValueError(
                        "Native job "
                        + identifier
                        + " reached "
                        + record["native_state"]
                        + " while waiting for "
                        + str(sorted(states))
                        + "; inspect the retained terminal record and fixture log"
                    )
            time.sleep(0.5)
        raise ValueError(
            "Native observation timed out for "
            + identifier
            + ": "
            + "; ".join(errors[-3:])
        )

    def submit(path, name):
        spec = dict(
            script=str(path),
            account="labx",
            partition="local",
            cpus=1,
            nodes=1,
            memory_mib=64,
            wall_seconds=90,
            name=name,
        )
        plan = scheduler.review("submit", submission=spec)
        write_json(output / (name + "-plan.json"), plan)
        receipt = scheduler.apply(plan, plan["digest"])
        write_json(output / (name + "-receipt.json"), receipt)
        check(
            name + " native submission accepted",
            receipt.get("status") == "accepted" and bool(receipt.get("native_id")),
        )
        identifier = receipt["native_id"]
        # Never promote a bare acknowledgement ID to cancellation authority.
        # The fresh native read must verify owner, cluster and exact attempt.
        record = wait_observed(identifier, {"PENDING", "RUNNING"})
        owned.append(record["identity"])
        check(
            name + " exact owner/account/cluster",
            record["user"] == "raven"
            and record["account"] == "labx"
            and record["identity"].get("native_cluster") == "raven-ci",
        )
        return record, plan, receipt

    def apply_action(action, identity):
        plan = scheduler.review(action, identity=identity)
        receipt = scheduler.apply(plan, plan["digest"])
        check(
            action + " exact reviewed action accepted",
            receipt.get("status") == "accepted",
        )
        return receipt

    try:
        report["version"] = scheduler.read(["scontrol", "--version"]).strip()
        report["capabilities"] = scheduler.capabilities()
        initial = scheduler.jobs()
        check(
            "native initial inventory is user scoped",
            all(row["user"] == "raven" for row in initial),
        )
        main_script = project / "ordinary script.sh"
        main_script.write_text(
            "#!/bin/sh\n#SBATCH --output="
            + shlex.quote(str(project / "ordinary.log"))
            + "\nprintf '%s\\n' 'native literal=x y'\nsleep 30\n"
        )
        record, plan, receipt = submit(main_script, "raven-native-qualification")
        identity = record["identity"]
        queued = scheduler.jobs()
        check(
            "submitted job appears in native queue",
            any(row["identity"] == identity for row in queued),
        )
        selected = scheduler.fresh_detail(identity)
        check(
            "selected native detail preserves identity",
            selected["identity"] == identity and selected["native"].get("job"),
        )
        wrong = copy.deepcopy(identity)
        wrong["attempt"] += "-different"
        try:
            scheduler.review("cancel", identity=wrong)
        except ValueError:
            check("changed attempt cannot be reviewed", True)
        else:
            check("changed attempt cannot be reviewed", False)
        apply_action("cancel", identity)
        terminal = wait_observed(identity["native_id"], {"CANCELLED"})
        check(
            "cancelled job retains exact native attempt",
            terminal["identity"] == identity,
        )
        check(
            "completed detail reconciles queued identity",
            scheduler.fresh_detail(identity)["identity"] == identity,
        )
        replay_rejected = False
        try:
            scheduler.apply(plan, plan["digest"])
        except ValueError:
            replay_rejected = True
        check("durable action cannot be replayed", replay_rejected)

        held_script = project / "held script.sh"
        held_script.write_text(
            "#!/bin/sh\n#SBATCH --hold\n#SBATCH --output="
            + shlex.quote(str(project / "held.log"))
            + "\nprintf '%s\\n' completed-native-held\nsleep 1\n"
        )
        held, _, _ = submit(held_script, "raven-native-held")
        check("native script hold directive preserved", held["state"] == "PENDING")
        apply_action("hold", held["identity"])
        apply_action("release", held["identity"])
        finished = wait_observed(held["id"], {"COMPLETED"}, timeout=45)
        check("held then released job completes", finished["state"] == "COMPLETED")
        history = history_until(scheduler, {identity["native_id"], held["id"]})
        check(
            "bounded native accounting includes owned attempts",
            {identity["native_id"], held["id"]}.issubset(
                {row["id"] for row in history}
            ),
        )
        write_json(output / "native-history.json", history)

        experiments.create(
            str(project),
            "native-qualification",
            title="Owned native scheduler lifecycle",
        )
        experiments.link(
            str(project), "native-qualification", scheduler_identity=identity
        )
        experiments.link(
            str(project), "native-qualification", scheduler_identity=held["identity"]
        )
        summary = experiments.summarize(
            project=str(project), experiment_id="native-qualification"
        )
        check(
            "real native attempts link once without manufactured accounting",
            summary["attempts"] == 2
            and all(not row["complete"] for row in summary["totals"].values()),
        )
        write_json(output / "experiment-summary.json", summary)

        probe = qualification.prepare(
            str(project),
            scheduler,
            {
                "python": sys.executable,
                "scratch": str(project),
                "imports": ["raven"],
                "worker": True,
                "trace": True,
                "gpu": False,
                "resources": {
                    "account": "labx",
                    "partition": "local",
                    "cpus": 1,
                    "memory_mib": 512,
                    "wall_seconds": 120,
                },
            },
        )
        native = probe["review"]
        probe_receipt = scheduler.apply(native, native["digest"])
        check(
            "compute probe submitted through reviewed native adapter",
            probe_receipt.get("status") == "accepted",
        )
        probe_record = wait_observed(
            probe_receipt["native_id"], {"PENDING", "RUNNING", "COMPLETED"}
        )
        owned.append(probe_record["identity"])
        wait_observed(probe_record["id"], {"COMPLETED"}, timeout=90)
        probe_result = qualification.collect(
            str(project), probe["id"], scheduler, probe_record["identity"]
        )
        write_json(output / "compute-qualification.json", probe_result)
        check(
            "compute probe exact attempt verified",
            probe_result.get("identity_verified") is True,
        )
        checks = probe_result.get(
            "checks", probe_result.get("result", {}).get("checks", [])
        )
        required = {"import:raven", "filesystem", "metrics-recording"}
        check(
            "compute imports filesystem and recording passed",
            required.issubset(
                {row["name"] for row in checks if row["status"] == "passed"}
            ),
        )
        report["status"] = "passed-for-recorded-checks"
    except Exception as exc:
        report.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
    finally:
        cleanup = []
        for identity in owned:
            if json.dumps(identity, sort_keys=True) in terminal_attempts:
                continue  # Already observed terminal; do not re-open a purged ID.
            try:
                current = scheduler.fresh_detail(identity)
                if current["identity"] != identity or current["user"] != "raven":
                    raise ValueError(
                        "Cleanup identity/owner changed; nothing cancelled"
                    )
                if current["state"] in {
                    "PENDING",
                    "RUNNING",
                    "COMPLETING",
                    "SUSPENDED",
                }:
                    cleanup.append(apply_action("cancel", identity))
            except Exception as exc:
                cleanup.append(
                    dict(
                        identity=identity,
                        status="unverified",
                        error=str(exc),
                        recovery="The reviewed job time limit remains bounded; no numeric-ID fallback was used.",
                    )
                )
        report["cleanup"] = cleanup
        if any(row.get("status") == "unverified" for row in cleanup):
            report.update(
                status="failed",
                cleanup_error="An owned attempt could not be reconciled during cleanup; inspect the bounded fixture evidence",
            )
        report["read_stats"] = scheduler.capabilities()["read_stats"]
        report["seconds"] = time.monotonic() - started
        write_json(output / "manifest.json", report)
    print(
        json.dumps(
            {key: report.get(key) for key in ("status", "version", "error", "seconds")}
        )
    )
    return int(report["status"] != "passed-for-recorded-checks")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--child", type=Path)
    args = parser.parse_args(argv)
    if args.child:
        if not args.revision:
            parser.error("--revision is required for the child report")
        return qualify(args.child, args.revision)
    if os.getuid() != 0 or not Path("/tmp/raven-services-started").is_file():
        parser.error("Run only through qualify_native_slurm.py's disposable service")
    if args.output is None or not args.revision:
        parser.error("--output and --revision are required")
    root = Path("/home/raven/native scheduler qualification")
    root.mkdir(mode=0o700)
    os.chown(root, 1100, 1100)
    command = [
        "runuser",
        "-u",
        "raven",
        "--",
        "env",
        "PYTHONPATH=" + str(SOURCE),
        "RAVEN_QUALIFICATION_ROOT=" + str(SOURCE),
        "python3",
        str(SOURCE / "scripts/slurm/native_scheduler.py"),
        "--child",
        str(root),
        "--revision",
        args.revision,
    ]
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=260)
        for stream in ("stdout", "stderr"):
            (args.output / ("native-driver." + stream + ".txt")).write_text(
                getattr(result, stream)
            )
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        return result.returncode
    finally:
        if root.exists():
            copied = args.output / "run-artifacts"
            shutil.copytree(root, copied, symlinks=True)
            owner = args.output.stat()
            for path in [copied, *copied.rglob("*")]:
                os.chown(path, owner.st_uid, owner.st_gid, follow_symlinks=False)
                if not path.is_symlink():
                    path.chmod(0o700 if path.is_dir() else 0o600)


if __name__ == "__main__":
    raise SystemExit(main())
