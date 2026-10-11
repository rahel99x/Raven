"""Shared contracts for bounded research operations and reviewed mutations.

Adapters never receive the UI object. Inspection runs in the existing research
lane; a plan binds an explicit action to one connection and a short review
window. Each adapter must additionally revalidate its live scheduler/file
evidence immediately before changing it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import copy
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import tempfile
import time
import uuid

from .remote import LocalFiles

MODULES = ("ops_cluster", "ops_science", "ops_campaigns", "ops_services", "ops_scale", "ops_queue_order", "ops_workspace", "ops_investigation", "ops_platform", "ops_runtime", "ops_trace")
PLAN_SCHEMA = "tower.operation-plan/v1"
REPORT_SCHEMA = "tower.operation/v1"
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


def cancelled(ctx):
    return bool(ctx.cancel is not None and ctx.cancel.is_set())


def checkpoint(ctx):
    if cancelled(ctx):
        raise ValueError("Operation cancelled; prepare a new review before retrying.")


def canonical(value):
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("Operation data must be finite, bounded JSON.") from exc
    if len(data) > 16 << 20:
        raise ValueError("Operation data exceeds the 16 MiB limit.")
    return data


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def clean(value, limit=4096):
    return "".join(c if c.isprintable() else " " for c in str(value)[:limit])


def report(feature, summary, *, status="ok", rows=(), data=None, warnings=(), plan=None):
    value = {"schema": REPORT_SCHEMA, "feature": feature, "status": status,
             "summary": str(summary), "rows": list(rows), "data": data or {},
             "warnings": list(warnings)}
    if plan is not None:
        value["plan"] = plan
    canonical(value)
    return value


def _identity(record):
    keys = ("id", "submit", "start", "cluster", "account", "user")
    return {key: record.get(key, "") for key in keys}


def prepare_plan(ctx, feature, params, payload):
    checkpoint(ctx)
    if ctx.replay:
        raise ValueError("Recorded sessions cannot prepare live changes.")
    if not ctx.scope:
        raise ValueError("Connection identity is unavailable; a reviewed action needs an explicit scope.")
    job_id = str(params.get("job_id") or "")
    job = next((row for row in ctx.jobs + ctx.finished if str(row.get("id")) == job_id), None)
    value = {"schema": PLAN_SCHEMA, "feature": feature, "params": copy.deepcopy(params),
             "payload": copy.deepcopy(payload), "scope": copy.deepcopy(ctx.scope),
             "created": time.time(), "expires_in": PLAN_LIFETIME,
             "nonce": uuid.uuid4().hex,
             "job_identity": _identity(job) if job is not None else None}
    value["digest"] = digest(value)
    return value


def validate_plan(plan, ctx, feature):
    checkpoint(ctx)
    if ctx.replay:
        raise ValueError("Recorded sessions cannot apply live changes.")
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA or plan.get("feature") != feature:
        raise ValueError("This action requires its own reviewed operation plan.")
    content = {key: value for key, value in plan.items() if key != "digest"}
    if plan.get("digest") != digest(content):
        raise ValueError("The reviewed plan changed; inspect it again.")
    if not ctx.scope or plan.get("scope") != ctx.scope:
        raise ValueError("The connection changed; prepare a new review.")
    created, lifetime = plan.get("created"), plan.get("expires_in")
    if (isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(created)
            or lifetime != PLAN_LIFETIME or not -1 <= time.time() - created <= PLAN_LIFETIME):
        raise ValueError("The review expired; inspect the current evidence again.")
    identity = plan.get("job_identity")
    if identity:
        current = next((row for row in ctx.jobs + ctx.finished if str(row.get("id")) == str(identity.get("id"))), None)
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
        raise ValueError("Run Tower on the target host for this local operation; SSH views cannot write local substitutes.")


def _process(argv, timeout, limit, cancel=None):
    """Bound process output while it is produced and reap the whole group."""
    from .slurm import CommandError
    process = None
    buffers = {"out": bytearray(), "err": bytearray()}
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, "out"), (process.stderr, "err")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                if cancel is not None and cancel.is_set():
                    raise CommandError("Operation cancelled; command outcome can require reconciliation.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CommandError("Command timed out; inspect current state before retrying an action.")
                for key, _ in selector.select(min(.05, remaining)):
                    chunk = os.read(key.fileobj.fileno(), min(16384, limit + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        if sum(map(len, buffers.values())) + len(chunk) > limit:
                            raise CommandError("Command output exceeded the inspection limit.")
                        buffers[key.data].extend(chunk)
        process.wait(timeout=max(.01, deadline - time.monotonic()))
        out = buffers["out"].decode("utf-8", "replace")
        if process.returncode:
            error = buffers["err"].decode("utf-8", "replace") or out
            raise CommandError(f"{argv[0]} exit {process.returncode}: " + clean(error, 400))
        return out
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandError(clean(exc)) from exc
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait(timeout=2)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def _backend_command(backend, argv, timeout, limit, cancel, depth=0):
    from .slurm import Backend
    from .remote import SshBackend
    from .record import RecordingBackend
    from .platform_session import GovernedBackend
    if depth > 8:
        raise ValueError("The connection wrapper depth exceeds its bound.")
    if type(backend) is GovernedBackend:
        # Explicit inspections and action validation require a new bounded,
        # cancellable observation, not the sampler's cached queue snapshot.
        return backend.command(argv, timeout, limit, cancel)
    if type(backend) is RecordingBackend:
        started = time.time()
        try:
            out = _backend_command(backend.inner, argv, timeout, limit, cancel, depth + 1)
        except Exception as exc:
            backend._write({"t": started, "cmd": argv, "err": clean(exc)})
            raise
        backend._write({"t": started, "cmd": argv, "out": out, "dt": time.time() - started})
        return out
    if type(backend) is Backend:
        return _process(argv, timeout, limit, cancel)
    if type(backend) is SshBackend and backend.runner is SshBackend._run:
        return _process(backend.argv(argv), timeout + 5, limit, cancel)
    # Injected/simulated backends own their transport; keep their contract.
    return backend.run(argv, timeout)[0]


def command(ctx, argv, timeout=8, limit=1 << 20):
    """Run one explicit argv on the captured connection, outside rendering."""
    checkpoint(ctx)
    if ctx.replay:
        raise ValueError("Live command inspection is unavailable in a recorded session.")
    if not isinstance(argv, (list, tuple)) or not argv or len(argv) > 2048:
        raise ValueError("Command arguments are invalid.")
    if any(not isinstance(arg, str) or "\x00" in arg or len(arg) > 1 << 20 for arg in argv):
        raise ValueError("Command arguments must be bounded text without NUL bytes.")
    if not isinstance(timeout, (float, int)) or isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValueError("Command timeout must be between zero and 120 seconds.")
    backend = getattr(ctx.slurm, "b", None)
    if backend is None:
        raise ValueError("No scheduler connection is available.")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 64 << 20:
        raise ValueError("Command output limit is invalid.")
    out = _backend_command(backend, list(argv), timeout, limit, ctx.cancel)
    if not isinstance(out, str) or len(out) > limit or len(out.encode("utf-8")) > limit:
        raise ValueError("Command output exceeds the inspection limit.")
    return out


def read_bytes(ctx, path, limit=MAX_DOCUMENT):
    checkpoint(ctx)
    if not isinstance(path, (str, os.PathLike)) or not str(path) or any(c in str(path) for c in ("\x00", "\r", "\n")):
        raise ValueError("Choose an explicit file path without control characters.")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 64 << 20:
        raise ValueError("File inspection limit is invalid.")
    if getattr(ctx.files, "remote", False):
        before = ctx.files.snapshot_stat(str(path))
        if before["size"] > limit:
            raise ValueError("File exceeds the inspection limit.")
        data = ctx.files.read(str(path), 0, limit + 1)
        after = ctx.files.snapshot_stat(str(path))
        if before != after or len(data) != before["size"]:
            raise ValueError("The file changed during inspection; retry after its writer finishes.")
    else:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise ValueError("Choose a regular file within the inspection limit.")
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            if identity(before) != identity(after) or identity(after) != identity(os.stat(path)):
                raise ValueError("The file changed during inspection; retry after its writer finishes.")
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
        value = json.loads(read_bytes(ctx, path, limit).decode("utf-8"),
                           object_pairs_hook=unique, parse_constant=finite,
                           parse_float=_float, parse_int=_integer)
        _bounded(value, 32)
        return value
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Use a bounded UTF-8 JSON document.") from exc


def atomic_json(path, value, overwrite=False):
    """Publish a local JSON document without following or clobbering a target."""
    path = Path(path)
    data = canonical(value) + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=".tower-", dir=str(path.parent))
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


@lru_cache(maxsize=1)
def _registry():
    entries = {}
    for name in MODULES:
        module = importlib.import_module("tower." + name)
        for specification in module.SPECIFICATIONS:
            key = specification["key"]
            if key in entries:
                raise ValueError("Duplicate operation: " + key)
            entries[key] = (module, specification)
    return entries


def catalog():
    return [copy.deepcopy(spec) for _, spec in _registry().values()]


def specification(feature):
    try:
        return copy.deepcopy(_registry()[feature][1])
    except KeyError as exc:
        raise ValueError("Unknown operation: " + clean(feature)) from exc


def parameters(feature, supplied):
    spec = specification(feature)
    definitions = {item["key"]: item for item in spec.get("fields", ())}
    if not isinstance(supplied, dict) or set(supplied) - set(definitions):
        raise ValueError("Unknown fields for " + feature + ".")
    values = {key: supplied.get(key, item.get("default", "")) for key, item in definitions.items()}
    for key, value in values.items():
        if not isinstance(value, str) or len(value) > 8192 or any(c in value for c in ("\x00", "\r", "\n")):
            raise ValueError("Operation fields must be bounded single-line text.")
        definition = definitions[key]
        if definition.get("required") and not value.strip():
            raise ValueError(definition.get("label", key) + " is required.")
        choices = definition.get("choices")
        if choices and value not in choices:
            raise ValueError(definition.get("label", key) + ": choose " + ", ".join(choices))
    return values


def run(feature, params, ctx):
    checkpoint(ctx)
    values = parameters(feature, params)
    return _registry()[feature][0].run(feature, values, ctx)


def apply(feature, plan, ctx):
    validate_plan(plan, ctx, feature)
    callback = getattr(_registry()[feature][0], "apply", None)
    if callback is None:
        raise ValueError("This operation is read-only.")
    receipt = None
    if ctx.state_dir:
        from .services_io import private_directory
        directory = private_directory(Path(ctx.state_dir) / "operations-receipts")
        receipt = directory / (plan["digest"] + ".result.json")
        try:
            atomic_json(directory / (plan["digest"] + ".intent.json"), {
                "schema": "tower.operation-receipt/v1", "state": "started",
                "feature": feature, "scope": ctx.scope, "plan_digest": plan["digest"],
                "started": time.time(), "result_path": str(receipt)})
        except FileExistsError as exc:
            raise ValueError("This plan was already attempted. Read its receipt and inspect current state before retrying.") from exc
    try:
        value = callback(feature, copy.deepcopy(plan), ctx)
        if not isinstance(value, dict) or value.get("schema") != REPORT_SCHEMA or value.get("feature") != feature:
            raise ValueError("The adapter did not return a valid action receipt; inspect current state.")
        canonical(value)
    except Exception as exc:
        if receipt is not None:
            try:
                atomic_json(receipt, {"schema": "tower.operation-receipt/v1", "state": "error",
                    "feature": feature, "scope": ctx.scope, "plan_digest": plan["digest"],
                    "finished": time.time(), "error": clean(exc),
                    "reconciliation": "The action may have partially completed. Inspect current state before a new review."})
            except (OSError, ValueError):
                pass  # The durable intent still identifies the unresolved attempt.
        raise
    if receipt is not None:
        try:
            atomic_json(receipt, {"schema": "tower.operation-receipt/v1", "state": "finished",
                "feature": feature, "scope": ctx.scope, "plan_digest": plan["digest"],
                "finished": time.time(), "result": value})
            value["receipt_path"] = str(receipt)
        except (OSError, ValueError) as exc:
            value.setdefault("warnings", []).append("Action result could not be saved: " + clean(exc)
                + ". Its durable intent remains; copy this report before closing.")
    else:
        value.setdefault("warnings", []).append("State storage is disabled; copy this action report before closing.")
    return value
