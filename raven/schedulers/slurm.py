"""Slurm's structured native subset; existing dashboard adapters are unchanged."""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from .. import evidence as E, slurm_json as J
from ..transport_errors import CommandError
from .core import Scheduler, stdin_command, submission

_JOB = re.compile(r"[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?\Z")
_QUEUE_ID = re.compile(r"[0-9]+(?:_(?:[0-9]+|\[[0-9,:%-]+\]))?(?:\+[0-9]+)?\Z")
_STATE = {"CONFIGURING": "PENDING", "REQUEUED": "PENDING", "REQUEUE_FED": "PENDING", "REQUEUE_HOLD": "PENDING",
          "RESIZING": "RUNNING", "STAGE_OUT": "COMPLETING", "TIMEOUT": "FAILED", "OUT_OF_MEMORY": "FAILED",
          "NODE_FAIL": "FAILED", "BOOT_FAIL": "FAILED", "PREEMPTED": "FAILED", "DEADLINE": "FAILED"}


class SlurmScheduler(Scheduler):
    scheduler = "slurm"
    operations = ("queue", "history", "detail", "submit", "cancel", "hold", "release")

    def _argv(self, command, *arguments):
        argv = [command]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.scope["cluster"]):
            raise ValueError("One exact Slurm ClusterName is required; multi-cluster command output is unavailable")
        if self.scope.get("cluster") != "configured-default":
            argv += ["--clusters", self.scope["cluster"]]
        return [*argv, *arguments]

    def _document(self, argv, *, fresh=False, missing_ok=False):
        try:
            raw = self.read(argv, fresh=fresh)
            if raw.startswith("CLUSTER:"):
                prefix = "CLUSTER: " + self.scope["cluster"] + "\n"
                if self.scope["cluster"] == "configured-default" or not raw.startswith(prefix):
                    raise ValueError("Slurm JSON cluster header differs from the selected exact cluster")
                # --clusters emits this native header even for --json. Accept
                # exactly one verified cluster prefix; concatenated documents,
                # extra headers and arbitrary diagnostic preambles still fail.
                raw = raw[len(prefix):]
            value = E.loads(raw)
        except CommandError as exc:
            if missing_ok and "invalid job id" in str(exc).lower():
                return []
            raise
        if not isinstance(value, dict):
            raise ValueError("Slurm structured response must be an object")
        if value.get("errors"):
            errors = value["errors"]
            if (missing_ok and isinstance(errors, list)
                    and all(isinstance(item, dict) and "invalid job id" in str(item.get("description", "")).lower()
                            for item in errors)):
                return []
            raise ValueError("Slurm reported a structured scheduler error")
        if not isinstance(value.get("jobs"), list):
            raise ValueError("Slurm structured response has no jobs array")
        return self._bounded_records(value["jobs"])

    def _record(self, raw, *, historical=False, full_native=False):
        # Reuse qualified field parsers rather than inventing a second Slurm
        # sentinel, task-ID, timestamp, resource, and state interpretation.
        job = J.accounting_record(raw) if historical else J.queue_record(raw, time.time())
        if not _QUEUE_ID.fullmatch(job.id):
            raise ValueError("Native adapter requires a recognized Slurm job/task ID")
        if not job.submit:
            raise ValueError("Slurm attempt has no authoritative submission time")
        expected_cluster = self.scope["cluster"]
        if expected_cluster != "configured-default" and job.cluster != expected_cluster:
            raise ValueError("Slurm native cluster differs from the explicit connection scope")
        restarts = J.integer(raw.get("restart_cnt"))
        attempt = job.submit + "/restart:" + (str(restarts) if restarts is not None else "unknown")
        native = {"identity_verified": restarts is not None and bool(_JOB.fullmatch(job.id)) and bool(job.cluster),
                  "restart_cnt": restarts, "cluster": job.cluster,
                  "coverage": "normalized inventory fields; full native record is available in selected detail"}
        if full_native:
            native["job"] = raw
            native["coverage"] = "full native selected job record"
        state = _STATE.get(job.state, job.state)
        if state not in {"PENDING", "RUNNING", "COMPLETING", "COMPLETED", "FAILED", "CANCELLED", "SUSPENDED"}:
            state = "UNKNOWN"
        extra = {"native_cluster": job.cluster} if job.cluster else {}
        phase = "requested" if state == "PENDING" else "allocated" if job.start and state != "UNKNOWN" else "unknown"
        resource = {"cpus": job.cpus,
                    "cpu_unit": "Slurm CPU count; pending request / started allocation; site configured granularity",
                    "phase": phase, "nodes": job.nodes, "gpus": job.gpus}
        if historical:
            resource.update(memory={"requested": job.req_mem if job.req_mem else None, "unit": "B"},
                            cpu_time={"observed": job.cpu_time, "unit": "s"},
                            peak_task_rss={"observed": job.rss if job.rss else None, "unit": "B"})
        else:
            resource["memory"] = {"requested": job.mem_req or None, "unit": "native Slurm memory request"}
        return self.record(self.identity(job.id, attempt, **extra), name=job.name, user=job.user,
                           state=state, native_state=job.state, native=native,
                           resources=resource,
                           cpus=job.cpus, nodes=job.nodes, partition=job.partition, account=job.account,
                           submit=job.submit, start=job.start, end=job.end,
                           elapsed=job.elapsed, workdir=job.workdir, gpus=job.gpus,
                           exit_code=getattr(job, "exit", ""), cpu_time=getattr(job, "cpu_time", None),
                           rss=getattr(job, "rss", None) or None)

    def jobs(self, *, history=False, user=""):
        owner = user or self.scope["user"]
        if history:
            argv = self._argv("sacct", "--json", "--allocations", "--duplicates", "--starttime=now-2days", "--endtime=now", "--user", owner)
        else:
            argv = self._argv("squeue", "--json", "--array", "--user", owner)
        records = [self._record(raw, historical=history) for raw in self._document(argv)]
        if any(record["user"] != owner for record in records):
            raise ValueError("Slurm returned another owner's job in a user-scoped response")
        return self._bounded_records(records)

    def detail(self, identity):
        target = self._identity(identity)
        if not _JOB.fullmatch(target["native_id"]):
            raise ValueError("Detail requires an exact Slurm job/task ID")
        records = self._document(self._argv("scontrol", "show", "job", target["native_id"], "--json"), missing_ok=True)
        matching = [self._record(raw, full_native=True) for raw in records if J.identifier(raw) == target["native_id"]]
        if not matching:
            try:
                submitted = datetime.fromisoformat(target["attempt"].split("/restart:", 1)[0])
            except ValueError as exc:
                raise ValueError("Historical Slurm detail requires an authoritative submission timestamp") from exc
            # Four days around the UTC submission date also includes scheduler
            # local wall-clock timezone offsets, without an epoch-wide scan.
            first = (submitted - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00")
            last = (submitted + timedelta(days=2)).strftime("%Y-%m-%dT23:59:59")
            historical = self._document(self._argv("sacct", "--json", "--allocations", "--duplicates", "--jobs", target["native_id"],
                                                    "--starttime=" + first, "--endtime=" + last, "--user", self.scope["user"]))
            matching = [self._record(raw, historical=True, full_native=True) for raw in historical if J.accounting_record(raw).id == target["native_id"]]
        if len(matching) != 1 or matching[0]["identity"] != target:
            raise ValueError("Selected Slurm attempt is missing, ambiguous, or changed")
        return matching[0]

    def observe(self, native_id):
        """Resolve a current exact ID without accepting a caller's attempt token."""
        if not isinstance(native_id, str) or not _JOB.fullmatch(native_id):
            raise ValueError("Slurm observation requires an exact job/task ID")
        native = self._document(self._argv("scontrol", "show", "job", native_id, "--json"), fresh=True, missing_ok=True)
        records = [self._record(raw, full_native=True) for raw in native if J.identifier(raw) == native_id]
        if not records:
            native = self._document(self._argv("sacct", "--json", "--allocations", "--duplicates", "--jobs", native_id,
                                               "--starttime=now-2days", "--endtime=now", "--user", self.scope["user"]), fresh=True)
            records = [self._record(raw, historical=True, full_native=True) for raw in native if J.accounting_record(raw).id == native_id]
        if len(records) != 1 or records[0]["user"] != self.scope["user"] or not records[0]["native"]["identity_verified"]:
            raise ValueError("Slurm observation is missing, ambiguous, unowned, or lacks authoritative attempt fields")
        return records[0]

    def _submission(self, specification):
        if self.scope["cluster"] == "configured-default":
            raise ValueError("Pin the actual Slurm ClusterName in scope.cluster before a reviewed submission")
        spec = submission(specification)
        argv = self._argv("sbatch", "--parsable")
        mapping = {"account": "account", "partition": "partition", "cpus": "cpus-per-task",
                   "memory_mib": "mem", "nodes": "nodes", "name": "job-name", "gpus": "gpus"}
        for field, option in mapping.items():
            if field in spec:
                suffix = "M" if field == "memory_mib" else ""
                argv.append("--" + option + "=" + str(spec[field]) + suffix)
        if "cpus" in spec:
            nodes = spec.get("nodes", 1)
            if spec["cpus"] % nodes:
                raise ValueError("Total Slurm CPUs must divide evenly across the requested nodes")
            # Explicit portable CPUs mean total requested CPUs. Specify the task
            # layout too, rather than multiplying an inherited --ntasks value.
            argv = [item for item in argv if not item.startswith(("--cpus-per-task=", "--nodes="))]
            argv += ["--nodes=" + str(nodes), "--ntasks=" + str(nodes), "--ntasks-per-node=1",
                     "--cpus-per-task=" + str(spec["cpus"] // nodes)]
        if "wall_seconds" in spec:
            minutes, seconds = divmod(spec["wall_seconds"], 60)
            argv.append(f"--time={minutes}:{seconds:02d}")
        return [*argv, spec["script"]]

    def _submit_argv(self, specification, script_text):
        argv = stdin_command(self._submission(specification)[:-1], script_text)
        # Fixed shell program removes only scheduler-control environment names;
        # values never become code or tool output. Native script directives and
        # reviewed command-line overrides remain authoritative.
        argv[2] = ('for raven_name in $(env | sed -n "s/^\\(SBATCH_[A-Za-z0-9_]*\\)=.*/\\1/p"); '
                   'do unset "$raven_name"; done; ' + argv[2])
        return argv

    def _action(self, action, record):
        if self.scope["cluster"] == "configured-default":
            raise ValueError("Pin the actual Slurm ClusterName in scope.cluster before a reviewed action")
        if not record["native"].get("identity_verified"):
            raise ValueError("Slurm restart identity is unavailable; action requires a verified attempt")
        jid = record["identity"]["native_id"]
        if not _JOB.fullmatch(jid):
            raise ValueError("Slurm action requires an exact job ID")
        if record["state"] not in {"PENDING", "RUNNING", "SUSPENDED", "COMPLETING"}:
            raise ValueError("This Slurm attempt is no longer active")
        if action in {"hold", "release"} and record["state"] != "PENDING":
            raise ValueError("Only a pending Slurm job can be held or released")
        return self._argv("scancel", jid) if action == "cancel" else self._argv("scontrol", action, jid)

    def _parse_submission(self, raw):
        value = raw.strip().split(";", 1)[0]
        if not re.fullmatch(r"[0-9]+", value):
            raise ValueError("Unrecognized Slurm submission response; reconcile before retrying")
        return {"native_id": value}

    def capabilities(self):
        result = super().capabilities()
        result["notes"] = ["This modular adapter consumes Slurm JSON and reuses existing field normalizers; the established Slurm dashboard retains its legacy compatibility fallbacks.",
                           "Historical inventory is bounded to two days; selected old attempts use a small timestamp-anchored window. Missing restart counters disable mutations.",
                           "Mutations require an explicit native ClusterName; default endpoint inventory binds a returned native cluster in the job identity when available.",
                           "Explicit CPUs are a total request distributed as one task per requested node; site allocation granularity can round allocations. Inherited SBATCH_* controls are cleared before submission.",
                           "Inventory retains normalized identity and resource fields; full native JSON is retained for the selected detail, avoiding a full-document copy per listed job.",
                           "Slurm does not expose atomic attempt-conditioned cancel; revalidation narrows but cannot eliminate a scheduler-side identity race."]
        return result
