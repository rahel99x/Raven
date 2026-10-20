"""Lower contracts for bounded research operations and reviewed mutations.

Adapters never receive the UI object. Inspection runs in the existing research
lane; a plan binds an explicit action to one connection and a short review
window. Each adapter must additionally revalidate its live scheduler/file
evidence immediately before changing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
import copy
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid

from .local_files import LocalFiles

PLAN_SCHEMA = "raven.operation-plan/v1"
REPORT_SCHEMA = "raven.operation/v1"
MAX_DOCUMENT = 4 << 20
PLAN_LIFETIME = 300


@dataclass(frozen=True)
class Context:
    slurm: object = None
    files: object = field(default_factory=LocalFiles)
    cfg: dict = field(default_factory=dict)
    jobs: tuple = ()
    finished: tuple = ()
    selected: str = ""
    scope: dict = field(default_factory=dict)
    cancel: object = None
    state_dir: str = ""
    replay: bool = False
    ui_state_status: dict = field(default_factory=dict)
    execution: object = None
    verified_execution: dict = field(default_factory=dict)


from .operation_context import cancelled as cancelled, checkpoint, _identity


from .operation_json import canonical, digest


def clean(value, limit=4096):
    return "".join(c if c.isprintable() else " " for c in str(value)[:limit])


def report(
    feature, summary, *, status="ok", rows=(), data=None, warnings=(), plan=None
):
    value = {
        "schema": REPORT_SCHEMA,
        "feature": feature,
        "status": status,
        "summary": str(summary),
        "rows": list(rows),
        "data": data or {},
        "warnings": list(warnings),
    }
    if plan is not None:
        value["plan"] = plan
    canonical(value)
    return value


def prepare_plan(ctx, feature, params, payload):
    checkpoint(ctx)
    if ctx.replay:
        raise ValueError("Recorded sessions cannot prepare live changes.")
    if not ctx.scope:
        raise ValueError(
            "Connection identity is unavailable; a reviewed action needs an explicit scope."
        )
    job_id = str(params.get("job_id") or "")
    job = next(
        (row for row in ctx.jobs + ctx.finished if str(row.get("id")) == job_id), None
    )
    value = {
        "schema": PLAN_SCHEMA,
        "feature": feature,
        "params": copy.deepcopy(params),
        "payload": copy.deepcopy(payload),
        "scope": copy.deepcopy(ctx.scope),
        "created": time.time(),
        "expires_in": PLAN_LIFETIME,
        "nonce": uuid.uuid4().hex,
        "job_identity": _identity(job) if job is not None else None,
    }
    value["digest"] = digest(value)
    return value


def validate_plan(plan, ctx, feature):
    checkpoint(ctx)
    if ctx.replay:
        raise ValueError("Recorded sessions cannot apply live changes.")
    if (
        not isinstance(plan, dict)
        or plan.get("schema") != PLAN_SCHEMA
        or plan.get("feature") != feature
    ):
        raise ValueError("This action requires its own reviewed operation plan.")
    content = {key: value for key, value in plan.items() if key != "digest"}
    if plan.get("digest") != digest(content):
        raise ValueError("The reviewed plan changed; inspect it again.")
    if not ctx.scope or plan.get("scope") != ctx.scope:
        raise ValueError("The connection changed; prepare a new review.")
    created, lifetime = plan.get("created"), plan.get("expires_in")
    if (
        isinstance(created, bool)
        or not isinstance(created, (int, float))
        or not math.isfinite(created)
        or lifetime != PLAN_LIFETIME
        or not -1 <= time.time() - created <= PLAN_LIFETIME
    ):
        raise ValueError("The review expired; inspect the current evidence again.")
    identity = plan.get("job_identity")
    if identity:
        current = next(
            (
                row
                for row in ctx.jobs + ctx.finished
                if str(row.get("id")) == str(identity.get("id"))
            ),
            None,
        )
        if current is None or _identity(current) != identity:
            raise ValueError("The selected job attempt changed; prepare a new review.")
    payload = plan.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("The reviewed action payload is invalid.")
    return copy.deepcopy(payload)


def local_only(ctx):
    if ctx.replay:
        raise ValueError("This operation is unavailable in a recorded session.")
    backend = getattr(ctx.slurm, "b", None)
    seen = set()
    while backend is not None and hasattr(backend, "inner"):
        if id(backend) in seen or len(seen) >= 8:
            raise ValueError("The connection backend is ambiguous.")
        seen.add(id(backend))
        backend = backend.inner
    if getattr(ctx.files, "remote", False) or getattr(backend, "host", None):
        raise ValueError(
            "Run Raven on the target host for this local operation; SSH views cannot write local substitutes."
        )


from .bounded_command import _process as _process, execute as _backend_command


def command(ctx, argv, timeout=8, limit=1 << 20):
    """Run one explicit argv on the captured connection, outside rendering."""
    checkpoint(ctx)
    if ctx.replay:
        raise ValueError(
            "Live command inspection is unavailable in a recorded session."
        )
    if not isinstance(argv, (list, tuple)) or not argv or len(argv) > 2048:
        raise ValueError("Command arguments are invalid.")
    if any(
        not isinstance(arg, str) or "\x00" in arg or len(arg) > 1 << 20 for arg in argv
    ):
        raise ValueError("Command arguments must be bounded text without NUL bytes.")
    if (
        not isinstance(timeout, (float, int))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or not 0 < timeout <= 120
    ):
        raise ValueError("Command timeout must be between zero and 120 seconds.")
    backend = getattr(ctx.slurm, "b", None)
    if backend is None:
        raise ValueError("No scheduler connection is available.")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 64 << 20
    ):
        raise ValueError("Command output limit is invalid.")
    out = _backend_command(backend, list(argv), timeout, limit, ctx.cancel)
    if not isinstance(out, str) or len(out) > limit or len(out.encode("utf-8")) > limit:
        raise ValueError("Command output exceeds the inspection limit.")
    return out


def read_bytes(ctx, path, limit=MAX_DOCUMENT):
    checkpoint(ctx)
    from .source_budget import source_limit, source_read

    limit = source_limit(limit)
    if (
        not isinstance(path, (str, os.PathLike))
        or not str(path)
        or any(c in str(path) for c in ("\x00", "\r", "\n"))
    ):
        raise ValueError("Choose an explicit file path without control characters.")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 64 << 20
    ):
        raise ValueError("File inspection limit is invalid.")
    if getattr(ctx.files, "remote", False):
        before = ctx.files.snapshot_stat(str(path))
        if before["size"] > limit:
            raise ValueError("File exceeds the inspection limit.")
        source_read(before["size"])
        data = ctx.files.read(str(path), 0, limit + 1)
        after = ctx.files.snapshot_stat(str(path))
        if before != after or len(data) != before["size"]:
            raise ValueError(
                "The file changed during inspection; retry after its writer finishes."
            )
    else:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise ValueError("Choose a regular file within the inspection limit.")
            source_read(before.st_size)
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            identity = lambda value: (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
            if identity(before) != identity(after) or identity(after) != identity(
                os.stat(path)
            ):
                raise ValueError(
                    "The file changed during inspection; retry after its writer finishes."
                )
    if len(data) > limit:
        raise ValueError("File exceeds the inspection limit.")
    checkpoint(ctx)
    return data


def read_json(ctx, path, limit=MAX_DOCUMENT):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("JSON contains a duplicate key: " + clean(key))
            value[key] = item
        return value

    def finite(value):
        raise ValueError("JSON must not contain non-finite numbers.")

    try:
        from .planning_io import _bounded, _float, _integer

        value = json.loads(
            read_bytes(ctx, path, limit).decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=finite,
            parse_float=_float,
            parse_int=_integer,
        )
        _bounded(value, 32)
        return value
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Use a bounded UTF-8 JSON document.") from exc


def atomic_json(path, value, overwrite=False):
    """Publish a local JSON document without following or clobbering a target."""
    path = Path(path)
    data = canonical(value) + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=".raven-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError("Refusing to replace a non-regular target.")
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
            os.unlink(temporary)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return str(path)


def parameters_for(feature, spec, supplied):
    definitions = {item["key"]: item for item in spec.get("fields", ())}
    if not isinstance(supplied, dict) or set(supplied) - set(definitions):
        raise ValueError("Unknown fields for " + feature + ".")
    values = {
        key: supplied.get(key, item.get("default", ""))
        for key, item in definitions.items()
    }
    for key, value in values.items():
        if (
            not isinstance(value, str)
            or len(value) > 8192
            or any(c in value for c in ("\x00", "\r", "\n"))
        ):
            raise ValueError("Operation fields must be bounded single-line text.")
        definition = definitions[key]
        if definition.get("required") and not value.strip():
            raise ValueError(definition.get("label", key) + " is required.")
        choices = definition.get("choices")
        if choices and value not in choices:
            raise ValueError(
                definition.get("label", key) + ": choose " + ", ".join(choices)
            )
    return values


def action_services():
    from .action_lifecycle import Services

    return Services(validate_plan, atomic_json, canonical, clean, REPORT_SCHEMA)


def managed_run(function, *, complete=None):
    """Protect a direct adapter without importing the composition registry."""

    @wraps(function)
    def guarded(feature, params, ctx):
        from .execution.admission import operation_route

        routed = operation_route(feature, params, ctx)
        if routed is not None:
            return (
                complete(routed, ctx)
                if complete is not None and routed.get("status") != "deferred"
                else routed
            )
        return function(feature, params, ctx)

    return guarded


def managed_apply(function):
    from functools import wraps

    @wraps(function)
    def guarded(feature, plan, ctx):
        from .execution.admission import apply_admission

        routed = apply_admission(feature, plan, ctx)
        if routed is not None:
            from .action_lifecycle import execute

            return execute(feature, plan, ctx, routed, services=action_services())
        return function(feature, plan, ctx)

    return guarded


def apply_adapter(feature, plan, ctx, callback):
    """Apply a known adapter through the same admission and durable lifecycle."""
    from .action_lifecycle import execute
    from .execution.admission import apply_admission

    routed = apply_admission(feature, plan, ctx)
    return execute(feature, plan, ctx, routed or callback, services=action_services())
