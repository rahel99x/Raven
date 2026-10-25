"""IBM Spectrum LSF JSON and exact array-element identity; no unit guesses."""

from __future__ import annotations

import math
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
from .batch_common import (
    timestamp as native_timestamp,
)
from .core import Scheduler

_ID = r"[0-9]+(?:\[[0-9]+\])?"
_FORMAT = "jobid jobindex user stat queue job_name submit_time start_time finish_time exec_host nalloc_slot proj_name"
_STATES = {
    "PEND": "PENDING",
    "RUN": "RUNNING",
    "DONE": "COMPLETED",
    "EXIT": "FAILED",
    "WAIT": "PENDING",
    "PSUSP": "PENDING",
    "USUSP": "SUSPENDED",
    "SSUSP": "SUSPENDED",
    "UNKWN": "UNKNOWN",
    "ZOMBI": "UNKNOWN",
}


def _precise_submit(value):
    # Some releases/sites return only 'Oct 24 12:29'. Preserve this for display
    # but refuse mutation: guessing the current year does not prove identity.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return math.isfinite(value) and value > 0
    if not isinstance(value, str):
        return False
    return bool(
        re.fullmatch(r"[0-9]{10,}(?:\.[0-9]+)?", value)
        or (
            re.search(r"\b(?:19|20|21)[0-9]{2}\b", value)
            and re.search(r"\b[0-9]{2}:[0-9]{2}:[0-9]{2}\b", value)
        )
    )


class LSFScheduler(Scheduler):
    scheduler = "lsf"
    operations = ("queue", "history", "detail", "submit", "cancel", "hold", "release")

    def capabilities(self):
        return {
            **super().capabilities(),
            "native_format": "bjobs -json explicit output columns",
            "notes": [
                "Queue history uses retained bjobs -a records; archived bhist event logs are not silently substituted.",
                "Mutations require submit-time evidence with seconds and year (or native epoch); low-precision site output stays read-only.",
                "nalloc_slot is an allocated LSF slot count, not measured physical CPU cores.",
                "Portable memory overrides are unavailable because LSF reservation units and per-task/per-host semantics are site-dependent.",
                "Portable total-GPU overrides are unavailable: LSF num=N normally requests GPUs per host, so retain explicit native GPU directives.",
                "Hold/release is limited to pending LSF jobs (bstop/bresume); running-job suspension is not a portable hold.",
                "No live IBM Spectrum LSF site has been qualified on this host.",
            ],
        }

    def _parse(self, document):
        if not isinstance(document, dict):
            raise ValueError("Expected LSF JSON object")
        result = []
        for item in native_records(
            document.get("RECORDS"), self.max_records, "LSF job records"
        ):
            job_id = native_checked_id(item.get("JOBID"), _ID, "LSF job ID")
            index = native_integer(item.get("JOBINDEX"), "LSF JOBINDEX", optional=True)
            if index:
                expected = f"{job_id.split('[', 1)[0]}[{index}]"
                if "[" in job_id and job_id != expected:
                    raise ValueError(
                        "LSF JOBID and JOBINDEX identify different array elements"
                    )
                job_id = expected
            user = native_text(item.get("USER"), "LSF user")
            created = native_timestamp(item.get("SUBMIT_TIME"), "LSF SUBMIT_TIME")
            state = native_text(item.get("STAT"), "LSF STAT")
            metrics, diagnostics = native_metrics(
                item, {"NALLOC_SLOT": native_optional_count}, source="LSF"
            )
            cpus = metrics["NALLOC_SLOT"]
            identity = self.identity(job_id, "submit=" + created)
            result.append(
                self.record(
                    identity,
                    name=native_text(item.get("JOB_NAME", job_id), "LSF job name"),
                    user=user,
                    state=_STATES.get(state, "UNKNOWN"),
                    native_state=state,
                    cpus=cpus,
                    account=item.get("PROJ_NAME", ""),
                    partition=item.get("QUEUE", ""),
                    submit=created,
                    resources={
                        "cpu": {"allocated": cpus, "unit": "lsf-slot"},
                        "memory": {"requested": None, "unit": "site-native"},
                        "gpu": {"requested": None, "unit": "device"},
                    },
                    native={
                        "identity_verified": _precise_submit(item.get("SUBMIT_TIME")),
                        "submit_time": created,
                        "jobindex": index,
                        "resource_diagnostics": diagnostics,
                        "exec_host": item.get("EXEC_HOST"),
                        "start_time": item.get("START_TIME"),
                        "finish_time": item.get("FINISH_TIME"),
                    },
                )
            )
        return self._bounded_records(result)

    def jobs(self, *, history=False, user=""):
        user = user or self.scope["user"]
        native_text(user, "LSF user")
        argv = ["bjobs", "-json", "-a", "-u", user, "-o", _FORMAT]
        rows = [row for row in self._parse(self.json(argv)) if row["user"] == user]
        if history:
            return [
                row
                for row in rows
                if row["state"] in {"COMPLETED", "FAILED", "CANCELLED"}
            ]
        return [
            row
            for row in rows
            if row["state"] not in {"COMPLETED", "FAILED", "CANCELLED"}
        ]

    def detail(self, identity):
        identity = self._identity(identity)
        job_id = native_checked_id(identity["native_id"], _ID, "LSF job ID")
        return native_one(
            self._parse(self.json(["bjobs", "-json", "-a", "-o", _FORMAT, job_id])),
            identity,
        )

    def _submission(self, spec):
        script = native_submission(spec, unsupported=("memory_mib", "nodes", "gpus"))
        argv = ["bsub"]
        for key, option in (
            ("account", "-P"),
            ("partition", "-q"),
            ("name", "-J"),
            ("cpus", "-n"),
        ):
            if spec.get(key) is not None:
                argv.extend([option, str(spec[key])])
        if spec.get("wall_seconds") is not None:
            # LSF -W has minute granularity: round up and make the effective
            # request visible in the reviewed native command.
            minutes = (spec["wall_seconds"] + 59) // 60
            argv.extend(["-W", f"{minutes // 60}:{minutes % 60:02}"])
        return [*argv, "<", script]

    def observe(self, native_id):
        job_id = native_checked_id(native_id, _ID, "LSF job ID")
        rows = self._parse(
            self.json(["bjobs", "-json", "-a", "-o", _FORMAT, job_id], fresh=True)
        )
        return native_observed(rows, job_id, self)

    def _submit_argv(self, spec, script_text):
        # bsub reads #BSUB directives from stdin. Passing a script as COMMAND
        # instead would silently discard those resource/account directives.
        return native_stdin_submission(self._submission(spec)[:-2], script_text)

    def _parse_submission(self, raw):
        match = re.fullmatch(
            r"\s*Job <([0-9]+)> is submitted(?: to (?:default )?queue <[^<>\r\n]+>)?\.\s*",
            raw,
        )
        if not match:
            raise ValueError(
                "LSF submitted ID is unverified; inspect bjobs before retrying"
            )
        return {"native_id": match[1]}

    def _action(self, action, record):
        native_action_owner(self, record)
        job_id = native_checked_id(record["identity"]["native_id"], _ID, "LSF job ID")
        if action == "cancel":
            return ["bkill", job_id]
        if action == "hold" and record["native_state"] == "PEND":
            return ["bstop", job_id]
        if action == "release" and record["native_state"] == "PSUSP":
            return ["bresume", job_id]
        raise ValueError(
            "LSF hold/release applies only to a pending job or its explicit PSUSP hold"
        )
