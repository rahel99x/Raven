"""Flux native job-list evidence scoped to one explicit broker instance URI."""

from __future__ import annotations

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

_ID = r"(?:[0-9]+|[fƒ][1-9A-HJ-NP-Za-km-z]+)"
_STATES = {
    "NEW": "PENDING",
    "DEPEND": "PENDING",
    "PRIORITY": "PENDING",
    "SCHED": "PENDING",
    "RUN": "RUNNING",
    "CLEANUP": "COMPLETING",
    "INACTIVE": "UNKNOWN",
}
_RESULTS = {
    "COMPLETED": "COMPLETED",
    "FAILED": "FAILED",
    "CANCELED": "CANCELLED",
    "TIMEOUT": "FAILED",
}
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def canonical_job_id(value):
    """Flux RFC 19 F58/decimal aliases share one lossless unsigned identity."""
    value = native_checked_id(value, _ID, "Flux job ID")
    if value[0] in {"f", "ƒ"}:
        result = 0
        for digit in value[1:]:
            result = result * 58 + _BASE58.index(digit)
    else:
        result = int(value)
    if result > (1 << 64) - 1:
        raise ValueError("Flux job ID exceeds the unsigned 64-bit bound")
    return str(result)


class FluxScheduler(Scheduler):
    scheduler = "flux"

    def capabilities(self):
        result = {
            **super().capabilities(),
            "native_format": "flux jobs --json",
            "notes": [
                "scope.uri pins one concrete local:// or ssh:// instance; nested Flux instances are distinct scopes.",
                "Absent job-list attributes stay unavailable, including GPU and memory counters.",
                "Flux ncores is a native job core count; exclusive allocation and physical/logical CPU equivalence are not inferred.",
                "Bank is recorded natively; charging requires the site's accounting plugin.",
                "Urgency hold/release is not exposed as a portable job hold because it changes native urgency.",
                "No live Flux instance has been qualified on this host.",
            ],
        }
        if not self.scope.get("uri"):
            result["operations"] = ["queue", "history", "detail"]
            result["unavailable"] = {
                "submit": "Pin the concrete Flux instance URI first",
                "cancel": "Pin the concrete Flux instance URI first",
            }
        return result

    def _command(self, *args):
        uri = self.scope.get("uri")
        if uri is not None:
            native_text(uri, "Flux instance URI")
            if not uri.startswith(("local://", "ssh://")):
                raise ValueError(
                    "Flux scope must pin a concrete local:// or ssh:// instance URI, not a relative job ID"
                )
            return ["flux", "proxy", uri, "flux", *args]
        return ["flux", *args]

    def _parse(self, document, *, single=False):
        if not isinstance(document, dict):
            raise ValueError("Expected flux jobs native JSON object")
        rows = [document] if single else document.get("jobs")
        result = []
        for item in native_records(rows, self.max_records, "Flux job records"):
            # Prefer the JSON integer ID: it is lossless in Python, whereas
            # browser implementations may round the unsigned 64-bit value.
            job_id = canonical_job_id(item.get("id", item.get("jobid")))
            if (
                item.get("jobid") is not None
                and canonical_job_id(item["jobid"]) != job_id
            ):
                raise ValueError("Flux numeric id and F58 jobid disagree")
            submit_value = item.get("t_submit")
            if isinstance(submit_value, bool) or not isinstance(
                submit_value, (int, float)
            ):
                raise ValueError(
                    "Flux t_submit must be an authoritative native numeric timestamp"
                )
            created = native_timestamp(submit_value, "Flux t_submit")
            native_state = native_text(item.get("state"), "Flux state")
            native_result = item.get("result")
            state = _RESULTS.get(native_result, _STATES.get(native_state, "UNKNOWN"))
            user = item.get("username")
            userid = native_integer(item.get("userid"), "Flux userid")
            if not isinstance(user, str) or not user:
                # A numeric ID is not a verified login name. Keep it as such;
                # owner verification will refuse a mutation for another name.
                user = str(userid)
            metrics, diagnostics = native_metrics(
                item,
                {
                    "ncores": native_optional_count,
                    "nnodes": native_optional_count,
                    "ngpus": native_optional_count,
                },
                source="Flux",
            )
            cpus, nodes, gpus = (metrics[key] for key in ("ncores", "nnodes", "ngpus"))
            identity = self.identity(job_id, f"t_submit={created};userid={userid}")
            result.append(
                self.record(
                    identity,
                    name=native_text(item.get("name", job_id), "Flux job name"),
                    user=user,
                    state=state,
                    native_state=native_state,
                    cpus=cpus,
                    nodes=nodes,
                    gpus=gpus,
                    account=item.get("bank", ""),
                    partition=item.get("queue", ""),
                    submit=created,
                    resources={
                        "cpu": {"reported": cpus, "unit": "flux-core"},
                        "gpu": {"reported": gpus, "unit": "device"},
                        "memory": {"reported": None, "unit": "B"},
                    },
                    native={
                        "identity_verified": bool(self.scope.get("uri")),
                        "userid": userid,
                        "t_submit": created,
                        "result": native_result,
                        "returncode": item.get("returncode"),
                        "runtime": item.get("runtime"),
                        "urgency": item.get("urgency"),
                        "nodelist": item.get("nodelist"),
                        "exception": item.get("exception"),
                        "jobid": item.get("jobid"),
                        "resource_diagnostics": diagnostics,
                    },
                    workdir=item.get("cwd", ""),
                    exit_code=item.get("returncode"),
                )
            )
        return self._bounded_records(result)

    def jobs(self, *, history=False, user=""):
        user = user or self.scope["user"]
        native_text(user, "Flux user")
        args = ["jobs", "--json", f"--count={self.max_records}", f"--user={user}"]
        if history:
            args.append("--filter=inactive")
        return [
            row
            for row in self._parse(self.json(self._command(*args)))
            if row["user"] == user
        ]

    def detail(self, identity):
        identity = self._identity(identity)
        job_id = native_checked_id(identity["native_id"], _ID, "Flux job ID")
        return native_one(
            self._parse(
                self.json(self._command("jobs", "--json", job_id)), single=True
            ),
            identity,
        )

    def _require_instance(self):
        if not self.scope.get("uri"):
            raise ValueError(
                "Pin the concrete Flux instance URI before submitting or changing jobs"
            )

    def observe(self, native_id):
        job_id = canonical_job_id(native_id)
        rows = self._parse(
            self.json(self._command("jobs", "--json", job_id), fresh=True), single=True
        )
        return native_observed(rows, job_id, self)

    def _submission(self, spec):
        self._require_instance()
        script = native_submission(spec, unsupported=("memory_mib",))
        args = ["batch"]
        nodes = spec.get("nodes", 1)
        cpus, gpus = spec.get("cpus"), spec.get("gpus")
        if any(value is not None and value % nodes for value in (cpus, gpus)):
            raise ValueError(
                "Flux total cores/GPUs must divide across explicit node slots; use native directives for other topologies"
            )
        if any(spec.get(key) is not None for key in ("nodes", "cpus", "gpus")):
            args.extend([f"--nslots={nodes}"])
        if spec.get("nodes") is not None:
            args.append(f"--nodes={nodes}")
        if cpus is not None:
            args.append(f"--cores-per-slot={cpus // nodes}")
        if gpus is not None:
            args.append(f"--gpus-per-slot={gpus // nodes}")
        for key, option in (
            ("account", "--bank"),
            ("partition", "--queue"),
            ("name", "--job-name"),
        ):
            if spec.get(key) is not None:
                args.append(option + "=" + spec[key])
        if spec.get("wall_seconds") is not None:
            args.append(f"--time-limit={spec['wall_seconds']}s")
        return self._command(*args, script)

    def _submit_argv(self, spec, script_text):
        # flux batch reads its script from stdin when no SCRIPT is supplied.
        return native_stdin_submission(self._submission(spec)[:-1], script_text)

    def _parse_submission(self, raw):
        return {"native_id": canonical_job_id(raw.strip())}

    def _action(self, action, record):
        self._require_instance()
        native_action_owner(self, record)
        if action != "cancel":
            raise ValueError(
                "Flux urgency changes are not portable hold/release actions"
            )
        job_id = native_checked_id(record["identity"]["native_id"], _ID, "Flux job ID")
        return self._command("cancel", job_id)
