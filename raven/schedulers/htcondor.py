"""HTCondor ClassAd JSON, schedd-qualified GlobalJobId, and native submit files."""

from __future__ import annotations

import json
import re

from .batch_common import (
    action_owner as native_action_owner,
)
from .batch_common import (
    checked_id as native_checked_id,
)
from .batch_common import (
    integer as native_integer,
)
from .batch_common import (
    metrics as native_metrics,
)
from .batch_common import (
    observed as native_observed,
)
from .batch_common import (
    one as native_one,
)
from .batch_common import (
    optional_count as native_optional_count,
)
from .batch_common import (
    quantity as native_quantity,
)
from .batch_common import (
    records as native_records,
)
from .batch_common import (
    stdin_submission as native_stdin_submission,
)
from .batch_common import (
    submission as native_submission,
)
from .batch_common import (
    text as native_text,
)
from .core import Scheduler

_ID = r"[0-9]+\.[0-9]+"
_ATTRS = ",".join(
    (
        "ClusterId",
        "ProcId",
        "GlobalJobId",
        "Owner",
        "QDate",
        "NumJobStarts",
        "JobStatus",
        "JobBatchName",
        "Cmd",
        "RequestCpus",
        "RequestMemory",
        "RequestGpus",
        "AccountingGroup",
        "RemoteHost",
        "ExitCode",
        "ExitBySignal",
        "HoldReason",
        "JobStartDate",
        "CompletionDate",
        "RemoteWallClockTime",
        "MemoryUsage",
    )
)
_STATES = {
    1: "PENDING",
    2: "RUNNING",
    3: "CANCELLED",
    4: "COMPLETED",
    5: "PENDING",
    6: "COMPLETING",
    7: "SUSPENDED",
}


class HTCondorScheduler(Scheduler):
    scheduler = "htcondor"
    operations = ("queue", "history", "detail", "submit", "cancel", "hold", "release")

    def capabilities(self):
        return {
            **super().capabilities(),
            "native_format": "condor_q/condor_history ClassAd JSON",
            "notes": [
                "Submission file is an HTCondor submit description, not a Bash script.",
                "scope.schedd and scope.pool pin the native -name/-pool targets; scope.host remains the SSH transport host.",
                "One native submit may create a reviewed cluster of jobs, capped by max_records; the receipt retains its range.",
                "RequestCpus, RequestMemory (MiB), and RequestGpus are requests, not observed usage.",
                "History and counters depend on schedd retention and published ClassAds.",
                "No live HTCondor site has been qualified on this host.",
            ],
        }

    def _command(self, name):
        argv = [name]
        if self.scope.get("schedd"):
            argv.extend(["-name", native_text(self.scope["schedd"], "HTCondor schedd")])
        if self.scope.get("pool"):
            argv.extend(["-pool", native_text(self.scope["pool"], "HTCondor pool")])
        return argv

    def _parse(self, document):
        result = []
        for item in native_records(document, self.max_records, "HTCondor ClassAds"):
            cluster = native_integer(item.get("ClusterId"), "HTCondor ClusterId")
            process = native_integer(item.get("ProcId"), "HTCondor ProcId")
            job_id = f"{cluster}.{process}"
            global_id = native_text(item.get("GlobalJobId"), "HTCondor GlobalJobId")
            if (
                not re.fullmatch(r"[^#]+#[0-9]+\.[0-9]+#[0-9]+", global_id)
                or global_id.split("#")[1] != job_id
            ):
                raise ValueError(
                    "HTCondor GlobalJobId does not identify the exact ClusterId.ProcId"
                )
            created = native_integer(item.get("QDate"), "HTCondor QDate")
            if created < 1:
                raise ValueError("HTCondor QDate must be a positive native epoch")
            starts = native_integer(
                item.get("NumJobStarts"), "HTCondor NumJobStarts", optional=True
            )
            user = native_text(item.get("Owner"), "HTCondor Owner")
            native_state = native_integer(item.get("JobStatus"), "HTCondor JobStatus")
            state = _STATES.get(native_state, "UNKNOWN")
            exit_code = item.get("ExitCode")
            if exit_code is not None and type(exit_code) is not int:
                raise ValueError("HTCondor ExitCode must be an integer")
            by_signal = item.get("ExitBySignal")
            if by_signal is not None and type(by_signal) is not bool:
                raise ValueError("HTCondor ExitBySignal must be boolean")
            if native_state == 4 and (exit_code not in (None, 0) or by_signal):
                state = "FAILED"
            metrics, diagnostics = native_metrics(
                item,
                {
                    "RequestCpus": native_quantity,
                    "RequestMemory": native_quantity,
                    "RequestGpus": native_optional_count,
                },
                source="HTCondor",
            )
            cpus, memory, gpus = (
                metrics[key] for key in ("RequestCpus", "RequestMemory", "RequestGpus")
            )
            identity = self.identity(
                job_id,
                f"{global_id};qdate={created};starts={starts if starts is not None else 'unknown'}",
            )
            result.append(
                self.record(
                    identity,
                    name=native_text(
                        item.get("JobBatchName", item.get("Cmd", job_id)),
                        "HTCondor job name",
                    ),
                    user=user,
                    state=state,
                    native_state=str(native_state),
                    cpus=cpus,
                    gpus=gpus,
                    account=item.get("AccountingGroup", ""),
                    submit=str(created),
                    exit_code=exit_code,
                    resources={
                        "cpu": {"requested": cpus, "unit": "htcondor-request-cpus"},
                        "memory": {"requested": memory, "unit": "MiB"},
                        "gpu": {"requested": gpus, "unit": "device"},
                    },
                    native={
                        "identity_verified": starts is not None,
                        "GlobalJobId": global_id,
                        "QDate": created,
                        "NumJobStarts": starts,
                        "JobStatus": native_state,
                        "ExitCode": exit_code,
                        "ExitBySignal": by_signal,
                        "RemoteHost": item.get("RemoteHost"),
                        "HoldReason": item.get("HoldReason"),
                        "resource_diagnostics": diagnostics,
                        "MemoryUsage": item.get("MemoryUsage"),
                        "RemoteWallClockTime": item.get("RemoteWallClockTime"),
                    },
                )
            )
        return self._bounded_records(result)

    def jobs(self, *, history=False, user=""):
        user = user or self.scope["user"]
        native_text(user, "HTCondor owner")
        argv = [
            *self._command("condor_history" if history else "condor_q"),
            "-json",
            "-attributes",
            _ATTRS,
            "-limit",
            str(self.max_records),
            "-constraint",
            "Owner == " + json.dumps(user, ensure_ascii=True),
        ]
        return [row for row in self._parse(self.json(argv)) if row["user"] == user]

    def detail(self, identity):
        identity = self._identity(identity)
        job_id = native_checked_id(identity["native_id"], _ID, "HTCondor job ID")
        for name in ("condor_q", "condor_history"):
            rows = self._parse(
                self.json(
                    [
                        *self._command(name),
                        "-json",
                        "-attributes",
                        _ATTRS,
                        "-limit",
                        "2",
                        job_id,
                    ]
                )
            )
            if rows:
                return native_one(rows, identity)
        raise ValueError(
            "Exact HTCondor job attempt is absent from queue and retained history"
        )

    def _submission(self, spec):
        script = native_submission(
            spec, unsupported=("partition", "nodes", "wall_seconds")
        )
        argv = [
            *self._command("condor_submit"),
            "-terse",
            "-single-cluster",
            "-maxjobs",
            str(self.max_records),
        ]
        for key, field in (
            ("cpus", "request_cpus"),
            ("memory_mib", "request_memory"),
            ("gpus", "request_gpus"),
        ):
            if spec.get(key) is not None:
                suffix = " MB" if key == "memory_mib" else ""
                argv.extend(["-append", f"{field} = {spec[key]}{suffix}"])
        if spec.get("account") is not None:
            argv.extend(["-append", "accounting_group = " + spec["account"]])
        if spec.get("name") is not None:
            argv.extend(["-batch-name", spec["name"]])
        return [*argv, script]

    def observe(self, native_id):
        job_id = native_checked_id(native_id, _ID, "HTCondor job ID")
        for name in ("condor_q", "condor_history"):
            rows = self._parse(
                self.json(
                    [
                        *self._command(name),
                        "-json",
                        "-attributes",
                        _ATTRS,
                        "-limit",
                        "2",
                        job_id,
                    ],
                    fresh=True,
                )
            )
            if rows:
                return native_observed(rows, job_id, self)
        raise ValueError("Exact HTCondor job is absent from queue and retained history")

    def _submit_argv(self, spec, script_text):
        return native_stdin_submission([*self._submission(spec)[:-1], "-"], script_text)

    def _parse_submission(self, raw):
        match = re.fullmatch(r"\s*([0-9]+\.[0-9]+)(?:\s+-\s+([0-9]+\.[0-9]+))?\s*", raw)
        if not match:
            raise ValueError(
                "HTCondor submitted IDs are unavailable; reconcile condor_q before retrying"
            )
        first, last = match[1], match[2] or match[1]
        first_cluster, first_proc = map(int, first.split("."))
        last_cluster, last_proc = map(int, last.split("."))
        if (
            first_cluster != last_cluster
            or not 0 <= last_proc - first_proc < self.max_records
        ):
            raise ValueError(
                "HTCondor submission range exceeds the reviewed single-cluster bound"
            )
        return {
            "native_id": first,
            "native_range": {"first": first, "last": last},
            "jobs": last_proc - first_proc + 1,
        }

    def _action(self, action, record):
        native_action_owner(self, record)
        job_id = native_checked_id(
            record["identity"]["native_id"], _ID, "HTCondor job ID"
        )
        cluster, process = map(int, job_id.split("."))
        native = record["native"]
        command = {
            "cancel": "condor_rm",
            "hold": "condor_hold",
            "release": "condor_release",
        }.get(action)
        if command is None:
            raise ValueError("Unsupported HTCondor action")
        # Let the schedd atomically evaluate the reviewed owner and generation,
        # rather than mutate every process in a recycled cluster number.
        constraint = (
            f"ClusterId == {cluster} && ProcId == {process} && "
            f"GlobalJobId == {json.dumps(native['GlobalJobId'], ensure_ascii=True)} && "
            f"QDate == {native['QDate']} && NumJobStarts == {native['NumJobStarts']} && "
            f"Owner == {json.dumps(record['user'], ensure_ascii=True)}"
        )
        return [*self._command(command), "-constraint", constraint]
