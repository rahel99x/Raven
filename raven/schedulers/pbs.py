"""OpenPBS/PBS Professional native JSON; identifiers retain the server suffix."""

from __future__ import annotations

import re
from datetime import datetime

from .batch_common import (
    action_owner as native_action_owner,
)
from .batch_common import (
    checked_id as native_checked_id,
)
from .batch_common import (
    hms as native_hms,
)
from .batch_common import (
    integer as native_integer,
)
from .batch_common import (
    memory_quantity as native_memory_quantity,
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
    stdin_submission as native_stdin_submission,
)
from .batch_common import (
    submission as native_submission,
)
from .batch_common import (
    text as native_text,
)
from .core import Scheduler

_ID = r"[0-9]+(?:\[[0-9]+\])?(?:\.[A-Za-z0-9_.-]+)?(?:@[A-Za-z0-9_.-]+)?"
_READ_ID = r"[0-9]+(?:\[[0-9]*\])?(?:\.[A-Za-z0-9_.-]+)?(?:@[A-Za-z0-9_.-]+)?"
_STATE = {
    "Q": "PENDING",
    "W": "PENDING",
    "H": "PENDING",
    "R": "RUNNING",
    "E": "COMPLETING",
    "B": "RUNNING",
    "S": "SUSPENDED",
    "U": "SUSPENDED",
    "T": "PENDING",
    "F": "COMPLETED",
    "X": "COMPLETED",
}
_MONTHS = {
    name: index + 1
    for index, name in enumerate(
        (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        )
    )
}


def submission_time(value):
    """qstat converts epoch attributes to ctime strings, even in JSON output.

    Keep that native timezone-unspecified value rather than guessing an epoch
    using Raven's timezone. Numeric native variants are also preserved.
    """
    if type(value) is int or isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        stamp = native_integer(value, "PBS qtime")
        if stamp < 1:
            raise ValueError("PBS qtime must be a positive native epoch")
        return str(stamp)
    value = native_text(value, "PBS qtime")
    match = re.fullmatch(
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([A-Za-z]{3}) +([0-9]{1,2}) ([0-9]{2}):([0-9]{2}):([0-9]{2}) ([0-9]{4})",
        value,
    )
    if not match or match[1] not in _MONTHS:
        raise ValueError(
            "PBS qtime needs native seconds and year; partial or timezone-guessed timestamps are unavailable"
        )
    datetime(
        int(match[6]),
        _MONTHS[match[1]],
        int(match[2]),
        int(match[3]),
        int(match[4]),
        int(match[5]),
    )
    return value


class PBSScheduler(Scheduler):
    scheduler = "openpbs"
    operations = ("queue", "history", "detail", "submit", "cancel", "hold", "release")

    def capabilities(self):
        return {
            **super().capabilities(),
            "native_format": "qstat full JSON (-F json)",
            "notes": [
                "Completed jobs require server job_history_enable and retained history.",
                "Native select/place directives stay in the submission file; cpus/memory overrides use one explicit chunk.",
                "CPU ncpus and memory requests are not physical-core measurements.",
                "Array parents are excluded when -t expands subjobs, preventing duplicate job/resource totals.",
                "Missing run_count remains an unknown restart generation and disables mutation.",
                "qselect -u selects the login's job IDs before bounded qstat JSON chunks; no all-user JSON polling.",
                "Native ctime qtime strings retain their unspecified timezone; no local epoch conversion is inferred.",
                "No live PBS site has been qualified on this host.",
            ],
        }

    def _parse(self, document):
        if not isinstance(document, dict) or not isinstance(document.get("Jobs"), dict):
            raise ValueError("Expected qstat -f -F json Jobs object")
        jobs = document["Jobs"]
        if len(jobs) > self.max_records:
            raise ValueError("PBS job record bound exceeded")
        result = []
        for job_id, item in jobs.items():
            native_checked_id(job_id, _READ_ID, "PBS job ID")
            if not isinstance(item, dict):
                raise ValueError("PBS job records must be objects")
            if "[]" in job_id:
                continue
            # A full server-qualified identity prevents equal numeric IDs from
            # separate servers being confused, including array subjobs.
            if "." not in job_id:
                raise ValueError(
                    "PBS JSON must include the native server-qualified Job_Id"
                )
            if item.get("Job_Id", job_id) != job_id:
                raise ValueError("PBS record key and Job_Id disagree")
            owner = native_text(item.get("Job_Owner"), "PBS Job_Owner")
            user = owner.split("@", 1)[0]
            native_text(user, "PBS login owner")
            created = submission_time(item.get("qtime"))
            restart = native_integer(
                item.get("run_count", item.get("Run_Count")),
                "PBS run_count",
                optional=True,
            )
            native_state = native_text(item.get("job_state"), "PBS job_state")
            state = _STATE.get(native_state, "UNKNOWN")
            exit_status = item.get("Exit_status")
            if exit_status is not None:
                if type(exit_status) is not int:
                    raise ValueError("PBS Exit_status must be a native integer")
                if native_state in {"F", "X"} and exit_status:
                    state = "FAILED"
            resource = item.get("Resource_List", {})
            metrics, diagnostics = native_metrics(
                resource,
                {
                    "ncpus": native_optional_count,
                    "nodect": native_optional_count,
                    "ngpus": native_optional_count,
                    "mem": native_memory_quantity,
                },
                source="PBS Resource_List",
            )
            cpus, nodes, gpus, memory = (
                metrics[key] for key in ("ncpus", "nodect", "ngpus", "mem")
            )
            account = item.get("Account_Name", "")
            partition = item.get("queue", "")
            identity = self.identity(
                job_id,
                f"qtime={created};run_count={restart if restart is not None else 'unknown'}",
            )
            result.append(
                self.record(
                    identity,
                    name=native_text(item.get("Job_Name", job_id), "PBS Job_Name"),
                    user=user,
                    state=state,
                    native_state=native_state,
                    cpus=cpus,
                    nodes=nodes,
                    gpus=gpus,
                    account=account,
                    partition=partition,
                    submit=str(created),
                    resources={
                        "cpu": {"requested": cpus, "unit": "pbs-ncpus"},
                        "memory": {"requested": memory, "unit": "B"},
                        "gpu": {"requested": gpus, "unit": "device"},
                        "native": resource,
                    },
                    native={
                        "identity_verified": restart is not None,
                        "owner": owner,
                        "qtime": created,
                        "run_count": restart,
                        "Exit_status": exit_status,
                        "server": item.get("server"),
                        "resource_list": resource,
                        "resource_diagnostics": diagnostics,
                        "resources_used": item.get("resources_used", {}),
                        "comment": item.get("comment", ""),
                    },
                    workdir=item.get("init_work_dir", ""),
                    exit_code=exit_status,
                )
            )
        return self._bounded_records(result)

    def jobs(self, *, history=False, user=""):
        user = user or self.scope["user"]
        native_text(user, "PBS user")
        # qstat's -u alternate format conflicts with -F json in upstream PBS.
        # Select on the server first, then request only bounded exact IDs.
        select = ["qselect", "-u", user, "-T"]
        if history:
            select.append("-x")
        selected = self.read(select).splitlines()
        if len(selected) > self.max_records:
            raise ValueError("PBS selected job record bound exceeded")
        ids = [
            native_checked_id(item.strip(), _READ_ID, "PBS selected job ID")
            for item in selected
        ]
        if len(set(ids)) != len(ids):
            raise ValueError("PBS selection contains duplicate exact job IDs")
        rows = []
        for offset in range(0, len(ids), 256):
            argv = ["qstat", "-t", "-f", "-F", "json"]
            if history:
                argv.append("-x")
            rows.extend(self._parse(self.json([*argv, *ids[offset : offset + 256]])))
            self._bounded_records(rows)
        return self._bounded_records(
            [
                row
                for row in rows
                if row["user"] == user
                and (not history or row["native_state"] in {"F", "X", "M"})
            ]
        )

    def detail(self, identity):
        identity = self._identity(identity)
        job_id = native_checked_id(identity["native_id"], _ID, "PBS job ID")
        rows = self._parse(self.json(["qstat", "-f", "-x", "-F", "json", job_id]))
        return native_one(rows, identity)

    def observe(self, native_id):
        job_id = native_checked_id(native_id, _ID, "PBS job ID")
        rows = self._parse(
            self.json(["qstat", "-f", "-x", "-F", "json", job_id], fresh=True)
        )
        return native_observed(rows, job_id, self)

    def _submission(self, spec):
        script = native_submission(spec)
        argv = ["qsub"]
        for key, option in (("account", "-A"), ("partition", "-q"), ("name", "-N")):
            if spec.get(key) is not None:
                argv.extend([option, spec[key]])
        # select is deliberately one chunk unless the user requests nodes;
        # ncpus is per chunk and total cpus must divide across those nodes.
        if any(
            spec.get(key) is not None for key in ("cpus", "nodes", "memory_mib", "gpus")
        ):
            nodes = spec.get("nodes", 1)
            cpus = spec.get("cpus")
            gpus = spec.get("gpus")
            memory = spec.get("memory_mib")
            if any(
                value is not None and value % nodes for value in (cpus, gpus, memory)
            ):
                raise ValueError(
                    "PBS total CPU/GPU/memory overrides must divide evenly across requested chunks; use native select for heterogeneous requests"
                )
            chunks = [str(nodes)]
            if cpus is not None:
                chunks.append(f"ncpus={cpus // nodes}")
            if memory is not None:
                chunks.append(f"mem={memory // nodes}mb")
            if gpus is not None:
                chunks.append(f"ngpus={gpus // nodes}")
            argv.extend(["-l", "select=" + ":".join(chunks)])
        if spec.get("wall_seconds") is not None:
            argv.extend(["-l", "walltime=" + native_hms(spec["wall_seconds"])])
        return [*argv, script]

    def _submit_argv(self, spec, script_text):
        return native_stdin_submission(self._submission(spec)[:-1], script_text)

    def _parse_submission(self, raw):
        job_id = native_checked_id(raw.strip(), _ID, "PBS submitted job ID")
        return {"native_id": job_id}

    def _action(self, action, record):
        native_action_owner(self, record)
        job_id = native_checked_id(record["identity"]["native_id"], _ID, "PBS job ID")
        command = {"cancel": "qdel", "hold": "qhold", "release": "qrls"}.get(action)
        if command is None:
            raise ValueError("Unsupported PBS action")
        return [command, job_id]


class OpenPBSScheduler(PBSScheduler):
    scheduler = "openpbs"


class PBSProfessionalScheduler(PBSScheduler):
    scheduler = "pbspro"
