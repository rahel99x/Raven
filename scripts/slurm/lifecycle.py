#!/usr/bin/env python3
"""Real Slurm lifecycle inside the disposable qualification container only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, "/raven")
from raven.slurm import JOB_FMT, SACCT_FIELDS, SACCT_LOG_FIELDS, SACCT_STEP_FIELDS, Slurm, CommandError, parse_jobs, parse_kv


def execute(argv, *, user=True, timeout=20):
    command = ["runuser", "-u", "raven", "--", *argv] if user else argv
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=timeout)
    if len(result.stdout) + len(result.stderr) > 2 << 20:
        raise ValueError("Qualification command output exceeds 2 MiB")
    return result


class LocalScheduler:
    probe_json = True

    def run(self, command, timeout=20):
        begin = time.monotonic()
        result = execute(command, timeout=timeout)
        if result.returncode:
            raise CommandError(result.stderr)
        return result.stdout, time.monotonic() - begin


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args(argv)
    if os.getuid() != 0 or not Path("/tmp/raven-services-started").is_file():
        parser.error("Run only inside the disposable Raven qualification container")
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"schema": "raven.slurm-qualification/v1", "source_revision": args.revision,
              "source_sha256": {path: hashlib.sha256((Path('/raven') / path).read_bytes()).hexdigest()
                                for path in ("raven/slurm.py", "raven/slurm_json.py", "scripts/slurm/lifecycle.py", "scripts/slurm/entrypoint.sh")},
              "environment": "real Slurm, one disposable Linux container",
              "commands": [], "checks": [], "production_qualified": False,
              "unqualified": ["physical GPU", "multi-node", "production SSH", "site policy",
                              "heterogeneous allocations"]}
    started = time.monotonic()
    owned = []

    def capture(label, command, *, required=True, user=True):
        begin = time.monotonic()
        result = execute(command, user=user)
        record = {"label": label, "argv": command, "exit_code": result.returncode,
                  "duration_seconds": time.monotonic() - begin}
        for stream in ("stdout", "stderr"):
            raw = getattr(result, stream)
            name = label + "." + stream + ".txt"
            (args.output / name).write_text(raw)
            record[stream] = {"file": name, "sha256": hashlib.sha256(raw.encode()).hexdigest()}
        report["commands"].append(record)
        if required and result.returncode:
            raise ValueError(label + ": " + result.stderr[-1500:])
        return result.stdout

    def check(name, condition):
        report["checks"].append({"name": name, "passed": bool(condition)})
        if not condition:
            raise ValueError("Qualification failed: " + name)

    def wait_state(job, wanted, *, accounting=False, timeout=60):
        until = time.monotonic() + timeout
        attempts = 0
        while time.monotonic() < until:
            command = (["sacct", "-n", "-X", "-P", "-j", job, "-o", "State"] if accounting else
                       ["squeue", "-h", "-j", job, "-o", "%T"])
            result = execute(command)
            attempts += 1
            if result.returncode == 0 and any(s.split()[0].split("+")[0] in wanted for s in result.stdout.splitlines() if s.strip()):
                report["checks"].append({"name": "state " + job, "states": sorted(wanted),
                                         "passed": True, "polls": attempts,
                                         "accounting": accounting})
                return
            time.sleep(.5)
        raise ValueError("State deadline: " + job + " -> " + str(wanted))

    def submit(label, script, *options):
        raw = capture(label, ["sbatch", "--parsable", "--account=labx", "--time=00:02:00",
                              "--mem=32M", "--chdir=/home/raven/work with spaces",
                              "--output=/home/raven/work with spaces/%j.log", *options, str(script)])
        job = raw.strip().split(";", 1)[0]
        if not re.fullmatch(r"[1-9][0-9]*", job):
            raise ValueError("Unexpected sbatch identity")
        owned.append(job)
        return job

    def snapshot(label, job):
        raw = capture(label + "-squeue", ["squeue", "-h", "-u", "raven", "-o", JOB_FMT])
        check(label + " legacy rows", bool(parse_jobs(raw)))
        capture(label + "-squeue-json", ["squeue", "--json", "-u", "raven"], required=False)
        raw = capture(label + "-scontrol", ["scontrol", "show", "job", job, "-o"])
        check(label + " account", bool(raw.strip()) and all(
            parse_kv(line).get("Account") == "labx" for line in raw.splitlines() if line.strip()))
        capture(label + "-scontrol-json", ["scontrol", "--json", "show", "job", job], required=False)

    try:
        report["slurm_version"] = capture("version", ["squeue", "--version"]).strip()
        for tool in ("squeue", "sacct", "scontrol"):
            capture(tool + "-help", [tool, "--help"])
        capture("config", ["scontrol", "show", "config"])
        capture("slurm-conf", ["cat", "/etc/slurm/slurm.conf"])
        folder = Path("/home/raven/work with spaces")
        folder.mkdir(exist_ok=True)
        os.chown(folder, 1100, 1100)
        ordinary = folder / "job with spaces.sh"
        ordinary.write_text('#!/bin/bash\nprintf "literal argument=x y\\n"\nsleep 6\n')
        requeue = folder / "requeue.sh"
        requeue.write_text('#!/bin/bash\nprintf "restart=%s\\n" "${SLURM_RESTART_COUNT:-0}"\nif [ "${SLURM_RESTART_COUNT:-0}" = 0 ]; then sleep 30; fi\n')
        job = submit("submit", ordinary, "--hold", "--job-name=Raven spaces = sample")
        wait_state(job, {"PENDING"})
        snapshot("pending", job)
        capture("release", ["scontrol", "release", job])
        wait_state(job, {"RUNNING"})
        snapshot("running", job)
        scheduler = Slurm(LocalScheduler(), "raven")
        jobs = scheduler.jobs()
        check("Raven scheduler reader exact running job", any(row.id == job and row.account == "labx" for row in jobs))
        detail = scheduler.details(job)
        check("Raven scheduler reader path with spaces", detail.get("WorkDir") == str(folder))
        capture("sprio", ["sprio", "-j", job], required=False)
        capture("sshare", ["sshare", "-u", "raven", "-P"], required=False)
        wait_state(job, {"COMPLETED"}, accounting=True)
        finished = scheduler.finished(1)
        check("Raven accounting reader exact completed job", any(row.id == job and row.state == "COMPLETED" for row in finished))
        retained = next(row for row in finished if row.id == job and row.state == "COMPLETED")
        historical = scheduler.record_details(retained, historical=True)
        if historical.get("LogPathError") == "this Slurm version does not expose historical stdout/stderr paths through sacct":
            report["historical_log_paths"] = historical["LogPathError"]
        else:
            check("Raven historical details preserve completed path", historical.get("WorkDir") == str(folder))
        submission = scheduler.record_submit_info(retained)
        check("Raven exact submission retains account and name", submission.get("account") == "labx" and submission.get("name") == "Raven spaces = sample")
        capture("accounting-steps", ["sacct", "-n", "-P", "--duplicates", "-j", job, "-S", "now-1hour", "-o", SACCT_STEP_FIELDS])
        steps = scheduler.record_fin_steps(retained)
        report["raven_steps"] = [row.id for row in steps]
        check("Raven exact steps retain completed batch", any(row.id == job + ".batch" for row in steps))
        report["raven_reader_status"] = scheduler.reader_status
        log = capture("completed-log", ["cat", str(folder / (job + ".log"))])
        check("log bytes preserve spaces and equals", "literal argument=x y\n" in log)
        array = submit("array-submit", ordinary, "--array=1-2%1", "--hold")
        snapshot("array-pending", array)
        capture("array-release", ["scontrol", "release", array])
        wait_state(array, {"RUNNING"})
        snapshot("array-running", array)
        capture("array-cancel", ["scancel", array])
        wait_state(array, {"CANCELLED"}, accounting=True)
        again = submit("requeue-submit", requeue)
        wait_state(again, {"RUNNING"})
        before = parse_kv(capture("before-requeue", ["scontrol", "show", "job", again, "-o"]))
        time.sleep(1.1)
        capture("requeue", ["scontrol", "requeue", again])
        wait_state(again, {"COMPLETED"}, accounting=True, timeout=180)
        after = parse_kv(capture("after-requeue", ["scontrol", "show", "job", again, "-o"]))
        check("requeue retains id and increments restart", before.get("JobId") == after.get("JobId") and int(after.get("Restarts", "0")) >= 1)
        if not report["slurm_version"].startswith("slurm 23.02."):
            name = "raven-heavy-0123456789abcdef0123456789abcdef"
            guard = submit("guard-submit", ordinary, "--hold", "--job-name=" + name)
            capture("guard-wrong-name", ["scancel", "--ctld", "--name=" + name + "-wrong", "--user=1100", guard], required=False)
            wait_state(guard, {"PENDING"})
            capture("guard-wrong-user", ["scancel", "--ctld", "--name=" + name, "--user=0", guard], required=False)
            wait_state(guard, {"PENDING"})
            capture("guard-exact-cancel", ["scancel", "--ctld", "--name=" + name, "--user=1100", guard])
            wait_state(guard, {"CANCELLED"}, accounting=True)
            report["atomic_cancel_filters"] = "name and uid mismatches preserved held job; matching controller request cancelled it"
        else:
            report["atomic_cancel_filters"] = "unsupported: 23.02 client lacks controller-side REQUEST_KILL_JOBS; automatic cancellation must remain disabled"
        capture("accounting", ["sacct", "-n", "-P", "--duplicates", "-u", "raven", "-S", "now-1hour", "-o", SACCT_FIELDS])
        capture("accounting-identity", ["sacct", "-n", "-P", "--duplicates", "-j", job, "-S", "now-1hour", "-o", SACCT_LOG_FIELDS],
                required="historical_log_paths" not in report)
        capture("accounting-json", ["sacct", "--json", "--duplicates", "-u", "raven", "-S", "now-1hour"], required=False)
        # Reconnect is a real controller restart with retained controller state.
        pid = int(Path("/tmp/slurmctld.pid").read_text())
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if not Path("/tmp/slurmctld.pid").exists():
                break
            time.sleep(.1)
        capture("controller-restart", ["slurmctld"], user=False)
        for _ in range(100):
            result = execute(["scontrol", "ping"])
            if result.returncode == 0 and "UP" in result.stdout:
                break
            time.sleep(.2)
        check("controller reconnect", result.returncode == 0 and "UP" in result.stdout)
        capture("reconnected-accounting", ["sacct", "-n", "-X", "-P", "-j", job, "-o", "JobID,State,Account,AllocCPUS,Submit,Start,End"])
        # Deliberately recycle the original number in this disposable controller
        # only. Accounting persists; a clean controller generation drops its
        # completed-job state. Production controllers must never use this step.
        pid = int(Path("/tmp/slurmctld.pid").read_text())
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if not Path("/tmp/slurmctld.pid").exists():
                break
            time.sleep(.1)
        configuration = Path("/etc/slurm/slurm.conf")
        # -c drops jobs but older Slurm still restores its saved ID sequence.
        # A genuinely empty owned state directory is required for numeric reuse.
        recycled_state = Path("/var/spool/slurmctld-recycled")
        recycled_state.mkdir(mode=0o700)
        original_state = Path("/var/spool/slurmctld").stat()
        os.chown(recycled_state, original_state.st_uid, original_state.st_gid)
        configured = configuration.read_text().replace("StateSaveLocation=/var/spool/slurmctld\n",
                                                       "StateSaveLocation=/var/spool/slurmctld-recycled\n")
        configuration.write_text(configured + "\nFirstJobId=" + job + "\n")
        capture("recycle-controller-generation", ["slurmctld", "-c"], user=False)
        for _ in range(100):
            result = execute(["scontrol", "ping"])
            if result.returncode == 0 and "UP" in result.stdout:
                break
            time.sleep(.2)
        recycled = submit("recycled-submit", ordinary, "--hold", "--job-name=Raven recycled identity")
        check("actual numeric job id reused", recycled == job)
        new = parse_kv(capture("recycled-details", ["scontrol", "show", "job", recycled, "-o"]))
        check("recycled id has new submission", new.get("SubmitTime") != before.get("SubmitTime"))
        old = scheduler.record_details(retained, historical=True)
        if "historical_log_paths" in report:
            check("Raven unsupported historical paths stay unavailable after reuse", bool(old.get("LogPathError")) and not old.get("WorkDir"))
        else:
            check("Raven old attempt remains attributable after id reuse", old.get("WorkDir") == str(folder) and old.get("JobName") == "Raven spaces = sample")
        old_submission = scheduler.record_submit_info(retained)
        check("Raven retained submission excludes recycled job", old_submission.get("name") == "Raven spaces = sample")
        capture("recycled-cancel", ["scancel", recycled])
        wait_state(recycled, {"CANCELLED"}, accounting=True)
        capture("recycled-accounting", ["sacct", "-n", "-P", "-D", "-j", job, "-S", "now-1hour", "-o", "JobID,JobName,Submit,Start,End,State,Account"])
        report["raven_reader_status"] = scheduler.reader_status
        capture("service-processes", ["ps", "-eo", "comm,rss,time"], user=False)
        report["status"] = "passed-for-recorded-checks"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
    finally:
        for job in owned:
            execute(["scancel", job])
        report["elapsed_seconds"] = time.monotonic() - started
        (args.output / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report.get(key) for key in ("status", "slurm_version", "error", "elapsed_seconds")}))
    return 0 if report["status"] == "passed-for-recorded-checks" else 1


if __name__ == "__main__":
    raise SystemExit(main())
