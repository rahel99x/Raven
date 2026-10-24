#!/usr/bin/env python3
"""Launch, read and retain actual traces in the owned disposable Slurm fixture.

The fixture uses real Slurm submission/accounting, not mocked scheduler output.
It does not qualify physical GPU, MPI, multi-node clock alignment, production
site policy or task/cgroup enforcement (this fixture explicitly uses task/none).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(os.environ.get("RAVEN_QUALIFICATION_ROOT", "/raven"))
sys.path.insert(0, str(SOURCE))

SCIENCE = '''"""Two independently recorded worker threads with nested scientific phases."""
import json
from pathlib import Path
import threading
import time


def leaf(value):
    total = 0
    for index in range(80):
        total += (value + index) % 97
    return total


def reduction(value, depth=3):
    return leaf(value) if depth == 0 else reduction(value + 1, depth - 1)


def work_unit(value):
    return reduction(value) + reduction(value + 2)


def science_worker(number, values):
    deadline = time.monotonic() + 8
    total, count = 0, 0
    while time.monotonic() < deadline:
        total += work_unit(number + count)
        count += 1
        time.sleep(.08)
    values[number] = {"iterations": count, "result": total, "thread": threading.get_ident()}


values = {}
started = time.monotonic()
workers = [threading.Thread(target=science_worker, args=(number, values), name="science-" + str(number)) for number in range(2)]
for worker in workers:
    worker.start()
for worker in workers:
    worker.join()
Path(__file__).with_name("science-result.json").write_text(json.dumps({"threads": values, "elapsed_seconds": time.monotonic() - started}))
print("Scientific job completed with two worker threads", flush=True)
'''


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def source_evidence():
    paths = sorted((SOURCE / "raven").rglob("*.py")) + [
        SOURCE / "scripts/slurm/trace_workspace.py",
        SOURCE / "scripts/slurm/entrypoint.sh",
    ]
    hashes = {
        str(path.relative_to(SOURCE)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }
    return {
        "files": hashes,
        "tree_sha256": hashlib.sha256(
            json.dumps(hashes, sort_keys=True).encode()
        ).hexdigest(),
    }


def worker_probe():
    from raven.raven_worker_client import WorkerClient

    client = WorkerClient(
        {
            "site_kind": "slurm",
            "context": {"account": "labx", "partition": "local"},
            "resources": {
                "cpus": 1,
                "memory_mib": 256,
                "max_tasks": 1,
                "wall_seconds": 60,
            },
        },
        launch_argv=[sys.executable, "-m", "raven.raven_worker"],
    )
    result = {}
    try:
        client.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            client.poll(0.1)
            if client.placement or client.status == "failed":
                break
        result.update(
            status=client.status, error=client.error, placement=client.placement
        )
    finally:
        client.close()
        result["stderr"] = client.diagnostics
    print(json.dumps(result))
    return int(result.get("status") != "placement" or bool(result.get("error")))


def qualify(root, revision):
    from raven import layout as L
    from raven.slurm import parse_kv
    from raven.tracing import workspace_ui as UI
    from raven.tracing.protocol import read_json
    from raven.tracing.workspace_session import WorkspaceSession

    if os.getuid() != 1100:
        raise ValueError(
            "Scientific qualification must execute as the fixture's raven user"
        )
    project, output = root / "project", root / "results"
    project.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    report = {
        "schema": "raven.trace-workspace-slurm-qualification/v1",
        "source_revision": revision,
        "source_evidence": source_evidence(),
        "scope": "Real one-node disposable Slurm allocation, labx billing account, raven login",
        "commands": [],
        "checks": [],
        "production_qualified": False,
        "unqualified": [
            "physical GPU",
            "multi-node",
            "MPI",
            "production SSH/site policy",
            "Slurm task/cgroup enforcement",
            "fully verified Heavy readiness/analysis",
        ],
    }
    started, owned, session = time.monotonic(), [], None

    def check(name, condition, **evidence):
        report["checks"].append(dict(name=name, passed=bool(condition), **evidence))
        if not condition:
            raise ValueError("Qualification failed: " + name)

    def command(label, argv, *, timeout=30):
        begin = time.monotonic()
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=dict(os.environ, PYTHONPATH=str(SOURCE)),
            cwd=project,
        )
        record = dict(
            label=label,
            argv=argv,
            exit_code=result.returncode,
            duration_seconds=time.monotonic() - begin,
        )
        for stream in ("stdout", "stderr"):
            value = getattr(result, stream)
            if len(value.encode()) > 2 << 20:
                raise ValueError(
                    "Qualification command output exceeded its 2 MiB bound"
                )
            path = output / (label + "." + stream + ".txt")
            path.write_text(value)
            path.chmod(0o600)
            record[stream] = {
                "file": path.name,
                "sha256": hashlib.sha256(value.encode()).hexdigest(),
            }
        report["commands"].append(record)
        write_json(output / "commands.json", report["commands"])
        if result.returncode:
            raise ValueError(
                label
                + " exited "
                + str(result.returncode)
                + ": "
                + result.stderr[-1500:]
            )
        return result.stdout

    try:
        report["version"] = command("version", ["scontrol", "--version"]).strip()
        report["python"] = command(
            "python-version", [sys.executable, "--version"]
        ).strip()
        command("scheduler-config", ["scontrol", "show", "config"])
        command("nodes", ["scontrol", "show", "nodes", "-o"])
        command(
            "associations",
            [
                "sacctmgr",
                "-n",
                "-P",
                "show",
                "assoc",
                "where",
                "user=raven",
                "format=Cluster,Account,User,Partition",
            ],
        )
        science = project / "science.py"
        science.write_text(SCIENCE)
        script = project / "job with spaces.sh"
        script.write_text(
            "#!/bin/bash\nset -euo pipefail\nexec python3 "
            + __import__("shlex").quote(str(science))
            + "\n"
        )
        before = hashlib.sha256(script.read_bytes()).hexdigest()
        receipt_path = output / "launch-receipt.json"
        launch = [
            sys.executable,
            "-m",
            "raven",
            "launch",
            str(script),
            "--project",
            str(project),
            "--trace-profile",
            "pilot",
            "--trace-adapter",
            "python-legacy-profile",
            "--trace-cpu-placement",
            "--sbatch=--account=labx",
            "--sbatch=--partition=local",
            "--sbatch=--nodes=1",
            "--sbatch=--ntasks=1",
            "--sbatch=--cpus-per-task=2",
            "--sbatch=--mem=64M",
            "--sbatch=--time=00:02:00",
            "--json",
            "--receipt-file",
            str(receipt_path),
        ]
        receipt = json.loads(command("launch", launch))
        job = receipt["job_id"]
        check(
            "exact numeric Slurm job receipt", isinstance(job, str) and job.isdecimal()
        )
        owned.append(job)
        check(
            "original scientific Bash script preserved",
            hashlib.sha256(script.read_bytes()).hexdigest() == before,
        )
        check(
            "durable receipt matches printed receipt",
            json.loads(receipt_path.read_text()) == receipt,
        )
        run = Path(receipt["run_dir"])
        expected = {"job_id": job, "attempt": receipt["run_id"], "cluster": "raven-ci"}
        report["run_identity"] = expected
        paths, live = [], None
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            index = run / "trace/index.json"
            if index.exists():
                document = read_json(index)
                paths = [
                    run / entry["path"]
                    for entry in document["sources"]
                    if entry.get("schema") == "raven.trace/v1"
                ]
            if paths:
                if session is None:
                    session = WorkspaceSession(
                        paths, expected=expected, index_path=output / "run-index.sqlite"
                    )
                session.paths = tuple(map(str, paths))
                live = session.refresh(
                    {
                        "mode": "live",
                        "delta_ns": 2_000_000_000,
                        "functions": True,
                        "overview": True,
                    }
                )
                names = {row["name"] for row in live["intervals"]}
                if (
                    live["caught_up"]
                    and any("work_unit" in name for name in names)
                    and any(source["status"] == "LIVE" for source in live["sources"])
                ):
                    break
            time.sleep(0.1)
        if live is not None:
            write_json(output / "last-live-query.json", live)
        check(
            "verified committed intervals visible while actual job runs",
            live is not None
            and bool(live["intervals"])
            and any(source["status"] == "LIVE" for source in live["sources"]),
        )
        detail = parse_kv(
            command("running-details", ["scontrol", "show", "job", job, "-o"])
        )
        check(
            "actual lab account separate from login",
            detail.get("Account") == "labx"
            and detail.get("UserId", "").startswith("raven("),
        )
        check(
            "scientific allocation requests two CPUs",
            int(detail.get("NumCPUs", "0")) == 2,
        )
        selected = next(
            row["id"] for row in live["intervals"] if "work_unit" in row["name"]
        )
        state = {}
        UI.publish(state, live)
        state["workspace_view"].update(selected=selected, selected_detail_id=selected)
        rows, hits = UI.render(SimpleNamespace(g=L.Glyphs(True)), state, job, 80)
        terminal = "\n".join(L.row_text(row) for row in rows)
        (output / "live-terminal.txt").write_text(terminal)
        check(
            "80-column terminal workspace has real events and controls",
            "work_unit" in terminal
            and bool(hits)
            and all(L.vlen(L.row_text(row)) <= 80 for row in rows),
        )
        write_json(output / "live-query.json", live)
        stable = selected
        deadline = time.monotonic() + 70
        accounting = ""
        while time.monotonic() < deadline:
            result = subprocess.run(
                [
                    "sacct",
                    "-n",
                    "-X",
                    "-P",
                    "-j",
                    job,
                    "-o",
                    "JobID,State,Account,AllocCPUS,Submit,Start,End",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            accounting = result.stdout
            if result.returncode == 0 and any(
                "|COMPLETED|labx|2|" in row for row in accounting.splitlines()
            ):
                break
            time.sleep(0.25)
        (output / "completed-accounting.txt").write_text(accounting)
        check(
            "actual accounting completes exact job on lab allocation",
            any(
                row.startswith(job + "|COMPLETED|labx|2|")
                for row in accounting.splitlines()
            ),
        )
        for _ in range(200):
            retained = session.refresh(
                {
                    "mode": "explore",
                    "functions": True,
                    "overview": True,
                    "selected": stable,
                }
            )
            if retained["caught_up"] and all(
                source["status"] == "COMPLETE" for source in retained["sources"]
            ):
                break
            time.sleep(0.02)
        check(
            "completed recording is fully verified",
            retained["caught_up"]
            and all(source["status"] == "COMPLETE" for source in retained["sources"]),
        )
        check(
            "completed run preserves exact selected event",
            retained["selected_detail"] is not None
            and retained["selected_detail"]["id"] == stable,
        )
        UI.publish(state, retained)
        check(
            "completion preserves terminal selection and enters Explore",
            state["workspace_view"]["selected"] == stable
            and state["workspace_view"]["mode"] == "explore",
        )
        threads = {
            row["thread"] for row in retained["intervals"] if "work_unit" in row["name"]
        }
        check(
            "two live scientific threads remain independently inspectable",
            len(threads) == 2,
            threads=sorted(threads),
        )
        cpu = session.refresh({"mode": "explore", "perspective": "cpu"})
        cpu_supported = any(
            source["coverage"].get("cpu_placement", {}).get("status") == "available"
            for source in retained["sources"]
        )
        report["cpu_placement_supported"] = cpu_supported
        if cpu_supported:
            check(
                "actual sched_getcpu evidence is a point, never inferred residency",
                bool(cpu["workspace_projection"]["intervals"])
                and all(
                    row.get("point") and row["kind"] == "cpu_observation"
                    for row in cpu["workspace_projection"]["intervals"]
                ),
            )
        write_json(output / "completed-query.json", retained)
        write_json(output / "cpu-query.json", cpu)
        science_result = json.loads((project / "science-result.json").read_text())
        check(
            "science ran long enough for live inspection",
            science_result["elapsed_seconds"] >= 7.5
            and len(science_result["threads"]) == 2,
        )
        report["science"] = science_result
        check(
            "scientific script still preserved after completion",
            hashlib.sha256(script.read_bytes()).hexdigest() == before,
        )
        command(
            "completed-step-accounting",
            [
                "sacct",
                "-n",
                "-P",
                "-j",
                job,
                "-o",
                "JobID,State,AllocTRES,Elapsed,TotalCPU,MaxRSS",
            ],
        )
        report["worker_startup"] = json.loads(
            command(
                "worker-startup",
                [
                    "srun",
                    "--account=labx",
                    "--partition=local",
                    "--nodes=1",
                    "--ntasks=1",
                    "--cpus-per-task=1",
                    "--mem=256M",
                    "--time=00:02:00",
                    "--job-name=raven-workspace-startup-check",
                    sys.executable,
                    str(SOURCE / "scripts/slurm/trace_workspace.py"),
                    "--worker-probe",
                ],
                timeout=60,
            )
        )
        check(
            "owned Slurm Heavy worker reaches placement protocol without startup error",
            report["worker_startup"].get("status") == "placement"
            and not report["worker_startup"].get("error"),
        )
        report["status"] = "passed-for-recorded-checks"
    except Exception as exc:
        report.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
    finally:
        if session is not None:
            session.close()
        for job in owned:
            subprocess.run(["scancel", job], capture_output=True, timeout=10)
        report["elapsed_seconds"] = time.monotonic() - started
        write_json(output / "manifest.json", report)
    print(
        json.dumps(
            {
                key: report.get(key)
                for key in ("status", "version", "error", "elapsed_seconds")
            }
        )
    )
    return int(report["status"] != "passed-for-recorded-checks")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--child", type=Path)
    parser.add_argument("--worker-probe", action="store_true")
    args = parser.parse_args(argv)
    if args.worker_probe:
        return worker_probe()
    if args.child:
        return qualify(args.child, args.revision)
    if os.getuid() != 0 or not Path("/tmp/raven-services-started").is_file():
        parser.error("Run only through qualify_slurm.py's owned disposable service")
    if args.output is None or not args.revision:
        parser.error("--output and --revision are required")
    root = Path("/home/raven/trace workspace qualification")
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
        str(SOURCE / "scripts/slurm/trace_workspace.py"),
        "--child",
        str(root),
        "--revision",
        args.revision,
    ]
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=220)
        for stream in ("stdout", "stderr"):
            (args.output / ("driver." + stream + ".txt")).write_text(
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
