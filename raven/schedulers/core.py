"""Native scheduler contracts above Raven's bounded, cancellable transport.

Adapters preserve native identity and resource units. No adapter is discovered by
importing project code. An explicit digest review and a durable claim precede
every mutation; uncertain outcomes are returned for reconciliation, never replayed.
"""
from __future__ import annotations

import copy
import getpass
import hashlib
import os
import re
import threading
import time
import uuid

from .. import evidence as E
from ..bounded_command import execute
from ..transport_errors import CommandError

SCHEDULERS = frozenset({"slurm", "kubernetes", "lsf", "openpbs", "pbspro", "htcondor", "flux"})
MAX_INPUT_BYTES = 96 << 10
STATES = frozenset({"PENDING", "RUNNING", "COMPLETING", "COMPLETED", "FAILED", "CANCELLED", "SUSPENDED", "UNKNOWN"})
_SCOPE_FIELDS = {"cluster", "user", "account", "context", "namespace", "uri", "host", "schedd", "pool"}
SUBMISSION_FIELDS = {"script", "account", "partition", "cpus", "memory_mib", "wall_seconds", "name", "nodes", "gpus", "native_options"}


def validate_identity(value):
    """Detach an exact native attempt; a name or recycled number is insufficient."""
    E.keys(value, {"scheduler", "scope", "native_id", "attempt"}, {"namespace", "uid", "native_cluster"}, "scheduler identity")
    if not isinstance(value["scheduler"], str) or value["scheduler"] not in SCHEDULERS:
        raise ValueError("Unknown scheduler identity")
    for key, item in value.items():
        E.text(item, "identity " + key, 1024)
    return copy.deepcopy(value)


def normalize_scope(value=None):
    if value is not None and not isinstance(value, dict):
        raise ValueError("Scheduler connection scope must be an object")
    result = dict(value or {})
    if result.keys() - _SCOPE_FIELDS:
        raise ValueError("Unsupported scheduler connection scope fields")
    result.setdefault("cluster", "configured-default")
    result.setdefault("user", getpass.getuser())
    for key, item in result.items():
        E.text(item, "scope " + key, 4096)
    return result


def scope_digest(value=None):
    # Billing defaults are properties of a submission, not the native job's
    # endpoint identity. Two lab profiles must not double-count the same job.
    normalized = normalize_scope(value)
    return E.digest({key: item for key, item in normalized.items() if key != "account"})


def submission(value):
    if not isinstance(value, dict) or value.keys() - SUBMISSION_FIELDS or "script" not in value:
        raise ValueError("Submission requires a script and only declared resource fields")
    path = E.text(value["script"], "script", 4096)
    if not path.startswith("/") or path != os.path.normpath(path):
        raise ValueError("Submission script must be an absolute normalized path on the scheduler host")
    for key in ("account", "partition", "name"):
        if key in value:
            text = E.text(value[key], key, 256)
            if text.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@/+:-]+", text):
                raise ValueError("Invalid native submission " + key)
    for key in ("cpus", "memory_mib", "wall_seconds", "nodes", "gpus"):
        if key in value:
            E.integer(value[key], key, 10**9)
            if value[key] < (0 if key == "gpus" else 1):
                raise ValueError(key + " is outside its resource bound")
    if value.get("native_options"):
        raise ValueError("Unchecked native options are unavailable; use explicit supported resources")
    return copy.deepcopy(value)


class Scheduler:
    scheduler = ""
    operations = ("queue", "history", "detail", "submit", "cancel")

    def __init__(self, backend, scope=None, *, timeout=8, max_bytes=4 << 20,
                 max_records=10000, cancel=None, journal_dir=None, min_interval=1):
        if self.scheduler not in SCHEDULERS:
            raise ValueError("Use a registered native scheduler adapter")
        self.backend = backend
        self._bound_backend = backend
        self.scope = normalize_scope(scope)
        self.scope_key = scope_digest(self.scope)
        self._scope_snapshot = copy.deepcopy(self.scope)
        self._transport_snapshot = self._transport_binding()
        self.timeout = E.finite(timeout, "scheduler timeout", .001)
        if timeout > 120:
            raise ValueError("Scheduler timeout exceeds 120 seconds")
        E.integer(max_bytes, "output bound", 8 << 20)
        E.integer(max_records, "record bound", 20000)
        if not max_bytes or not max_records:
            raise ValueError("Scheduler output and record bounds must be positive")
        self.max_bytes, self.max_records = max_bytes, max_records
        self.min_interval = E.finite(min_interval, "scheduler read interval", .01)
        self.cancel, self.journal_dir = cancel, journal_dir
        self._lock = threading.RLock()
        self._cache = {}
        self._in_apply = False
        self.stats = {"requests": 0, "failed_requests": 0, "cache_hits": 0, "bytes": 0, "last_read": None}

    def identity(self, native_id, attempt, **extra):
        self._check_scope()
        return validate_identity(dict(scheduler=self.scheduler, scope=self.scope_key,
                                      native_id=str(native_id), attempt=str(attempt), **extra))

    def _check_scope(self):
        if self.scope != self._scope_snapshot:
            raise ValueError("Scheduler connection scope changed; create a new connection before reading or changing jobs")
        if self.backend is not self._bound_backend or self._transport_binding() != self._transport_snapshot:
            raise ValueError("Scheduler transport changed; create a new connection before reading or changing jobs")

    def _transport_binding(self):
        """Pin mutable transport endpoint fields without exposing SSH options."""
        backend = self.backend
        result = {"adapter": type(backend).__module__ + "." + type(backend).__qualname__}
        for field in ("host", "user", "target"):
            value = getattr(backend, field, None)
            if value is not None:
                result[field] = E.text(value, "transport " + field, 4096, empty=True)
        options = getattr(backend, "opts", None)
        if options is not None:
            if not isinstance(options, (tuple, list)) or any(not isinstance(item, str) for item in options):
                raise ValueError("Transport options must be a bounded native argument vector")
            result["options_sha256"] = E.digest(options)
        return result

    def _identity(self, value):
        self._check_scope()
        result = validate_identity(value)
        if result["scheduler"] != self.scheduler or result["scope"] != self.scope_key:
            raise ValueError("Identity belongs to a different scheduler connection scope")
        return result

    def capabilities(self):
        self._check_scope()
        return {"scheduler": self.scheduler, "operations": list(self.operations),
                "qualification": "fixture-tested; live site remains unqualified",
                "resources": "native units preserved; unavailable counters remain unknown",
                "scope": copy.deepcopy(self.scope), "scope_key": self.scope_key,
                "limits": {"timeout_seconds": self.timeout, "output_bytes": self.max_bytes,
                           "records": self.max_records, "min_read_interval_seconds": self.min_interval,
                           "max_inflight": 1}, "read_stats": dict(self.stats),
                "read_stats_unit": "bounded native command invocations, not an inferred count of scheduler RPCs or authentication requests"}

    def read(self, argv, *, fresh=False):
        if not isinstance(argv, (tuple, list)) or not argv or any(not isinstance(item, str) or "\0" in item for item in argv):
            raise ValueError("Native command must be an argument vector")
        key = tuple(argv)
        with self._lock:
            self._check_scope()
            now = time.monotonic()
            cached = self._cache.get(key)
            if not fresh and not self._in_apply and cached and now - cached[0] < self.min_interval:
                self.stats["cache_hits"] += 1
                return cached[1]
            self.stats["requests"] += 1
            try:
                raw = execute(self.backend, list(argv), self.timeout, self.max_bytes, self.cancel)
            except CommandError:
                self.stats["failed_requests"] += 1
                raise
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > self.max_bytes:
                self.stats["failed_requests"] += 1
                raise CommandError("Native scheduler transport violated its bounded text contract")
            self.stats["bytes"] += len(raw.encode("utf-8"))
            self.stats["last_read"] = time.time()
            if len(self._cache) >= 16:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (time.monotonic(), raw)
            return raw

    def json(self, argv, *, fresh=False):
        return E.loads(self.read(argv, fresh=fresh))

    def fresh_detail(self, identity):
        """Reconcile an attempt without publishing a cached scheduler state."""
        with self._lock:
            self._cache.clear()
            try:
                return self.detail(identity)
            finally:
                self._cache.clear()

    def record(self, identity, *, name="", user="", state="UNKNOWN", native_state="", resources=None, native=None, **viewfields):
        identity = self._identity(identity)
        if not isinstance(state, str) or state not in STATES:
            raise ValueError("Unknown normalized scheduler state")
        for label, value in (("name", name), ("user", user), ("native state", native_state)):
            E.text(str(value), label, 4096, empty=True)
        result = dict(id=identity["native_id"], identity=identity, name=str(name), user=str(user),
                      state=state, native_state=str(native_state), resources=copy.deepcopy(resources or {}),
                      native=copy.deepcopy(native or {}), cpus=None, nodes=None, partition="", account="")
        result.update(copy.deepcopy(viewfields))
        E.canonical(result)
        return result

    def _bounded_records(self, records):
        if not isinstance(records, list) or len(records) > self.max_records:
            raise ValueError("Scheduler response exceeds the native record bound")
        seen = set()
        for record in records:
            if isinstance(record, dict) and isinstance(record.get("identity"), dict):
                key = E.digest(self._identity(record["identity"]))
                if key in seen:
                    raise ValueError("Scheduler returned a duplicate exact native attempt")
                seen.add(key)
        return records

    def _script(self, specification):
        if "script" not in specification:
            return None
        # Fixed positional shell program works through local and SSH transports.
        # No project text is interpreted as shell syntax. The transport bounds the
        # read, and apply streams the reviewed bytes rather than re-opening a path.
        command = ["sh", "-c", 'test -f "$1" && test ! -L "$1" && exec cat -- "$1"', "raven-script", specification["script"]]
        text = self.read(command, fresh=True)
        if "\0" in text or "\ufffd" in text or len(text.encode()) > MAX_INPUT_BYTES:
            raise ValueError("Submission script must be unambiguous UTF-8, NUL-free and at most 96 KiB")
        return text

    def _submission(self, specification):
        raise NotImplementedError

    def _submit_argv(self, specification, script_text):
        if script_text is not None:
            raise ValueError("This adapter cannot submit the reviewed script bytes safely")
        return self._submission(specification)

    def _action(self, action, record):
        raise NotImplementedError

    def _parse_submission(self, raw):
        return {"output": raw}

    def _submission_result(self, raw, specification):
        return self._parse_submission(raw)

    def review(self, action, identity=None, submission=None):
        self._check_scope()
        if action not in self.operations or action not in {"submit", "cancel", "hold", "release"}:
            raise ValueError("Action is unavailable for this native scheduler")
        with self._lock:
            plan = {"schema": "raven.scheduler-plan/v1", "id": uuid.uuid4().hex,
                    "scheduler": self.scheduler, "scope": copy.deepcopy(self.scope),
                    "transport": copy.deepcopy(self._transport_snapshot),
                    "action": action, "created": time.time(), "expires_seconds": 300}
            if action == "submit":
                if identity is not None:
                    raise ValueError("Submission does not accept an existing job identity")
                spec = copy.deepcopy(submission)
                argv = self._submission(spec)
                script = self._script(spec)
                plan.update(submission=spec, argv=argv,
                            script_sha256=hashlib.sha256(script.encode()).hexdigest() if script is not None else None)
            else:
                if submission is not None:
                    raise ValueError("An existing job action does not accept submission fields")
                target = self._identity(identity)
                self._cache.clear()
                record = self.detail(target)
                if record["identity"] != target:
                    raise ValueError("Native job was replaced or its attempt changed")
                if record.get("user") != self.scope["user"]:
                    raise ValueError("Only an exact job owned by the selected login can be changed")
                plan.update(identity=target, target_user=record["user"], argv=self._action(action, record))
            E.canonical(plan)
            plan["digest"] = E.digest(plan)
            return plan

    def _claim(self, plan):
        if not self.journal_dir:
            raise ValueError("A private durable action journal is required before scheduler mutation")
        from ..config_trust import directory_fd
        with directory_fd(self.journal_dir, create=True, private=True) as parent:
            name = plan["digest"] + ".claimed"
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            except FileExistsError as exc:
                raise ValueError("This reviewed action was already attempted; reconcile native state before preparing a new plan") from exc
            try:
                os.write(fd, E.canonical({"id": plan["id"], "digest": plan["digest"], "time": time.time(),
                                          "outcome": "unobserved; reconcile native state before retrying"}))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(parent)

    def _save_receipt(self, plan, receipt):
        from ..config_trust import directory_fd
        raw = E.canonical(receipt)
        with directory_fd(self.journal_dir, private=True) as parent:
            name = plan["digest"] + ".receipt.json"
            temporary = "." + name + "." + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def apply(self, plan, review_digest):
        with self._lock:
            self._check_scope()
            E.keys(plan, {"schema", "id", "scheduler", "scope", "transport", "action", "created", "expires_seconds", "argv", "digest"},
                   {"submission", "script_sha256", "identity", "target_user"}, "scheduler review")
            unsigned = {key: value for key, value in plan.items() if key != "digest"}
            if plan["schema"] != "raven.scheduler-plan/v1" or E.digest(unsigned) != plan["digest"] or review_digest != plan["digest"]:
                raise ValueError("Explicit review digest does not match the scheduler plan")
            if plan["scheduler"] != self.scheduler or plan["scope"] != self.scope:
                raise ValueError("Reviewed scheduler connection changed")
            if plan["transport"] != self._transport_snapshot:
                raise ValueError("Reviewed scheduler transport changed")
            if not isinstance(plan["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", plan["id"]):
                raise ValueError("Invalid scheduler action token")
            E.finite(plan["created"], "plan creation time", 0)
            if plan["expires_seconds"] != 300 or not 0 <= time.time() - plan["created"] <= 300:
                raise ValueError("Scheduler plan expired; prepare a fresh review")
            action = plan["action"]
            if action not in self.operations or action not in {"submit", "cancel", "hold", "release"}:
                raise ValueError("Native action is unsupported")
            self._in_apply = True
            try:
                self._cache.clear()
                if action == "submit":
                    spec = plan.get("submission")
                    argv = self._submission(spec)
                    script = self._script(spec)
                    digest = hashlib.sha256(script.encode()).hexdigest() if script is not None else None
                    if digest != plan.get("script_sha256"):
                        raise ValueError("Reviewed submission script changed")
                    execute_argv = self._submit_argv(spec, script)
                else:
                    target = self._identity(plan.get("identity"))
                    record = self.detail(target)
                    if record["identity"] != target or record.get("user") != plan.get("target_user") or record.get("user") != self.scope["user"]:
                        raise ValueError("Reviewed native identity or job ownership changed")
                    argv = self._action(action, record)
                    execute_argv = argv
                if argv != plan["argv"]:
                    raise ValueError("Reviewed native command changed")
                self._claim(plan)
                receipt = {"schema": "raven.scheduler-receipt/v1", "scheduler": self.scheduler,
                           "scope": copy.deepcopy(self.scope), "transport": copy.deepcopy(self._transport_snapshot),
                           "action": action, "review_digest": plan["digest"]}
                try:
                    raw = execute(self.backend, execute_argv, self.timeout, self.max_bytes, self.cancel)
                    receipt.update(status="accepted", output=raw[:16384],
                                   output_bytes=len(raw.encode()), output_truncated=len(raw) > 16384)
                    if action == "submit":
                        receipt.update(self._submission_result(raw, spec))
                    else:
                        receipt["identity"] = target
                except (CommandError, ValueError) as exc:
                    receipt.update(status="unknown", output="", error=str(exc),
                                   recovery="Inspect and reconcile native scheduler state before preparing another reviewed action; this token is consumed.")
                try:
                    self._save_receipt(plan, receipt)
                except (OSError, ValueError) as exc:
                    receipt["journal_warning"] = "Native outcome observed but receipt persistence failed: " + str(exc)[:1024]
                return receipt
            finally:
                self._cache.clear()
                self._in_apply = False


def stdin_command(argv, text):
    """Feed reviewed UTF-8 bytes to a native command without a file-path race."""
    if not isinstance(text, str) or "\0" in text or len(text.encode()) > MAX_INPUT_BYTES:
        raise ValueError("Reviewed native input must be NUL-free and at most 96 KiB")
    return ["sh", "-c", 'raven_payload=$1; shift; printf %s "$raven_payload" | "$@"', "raven-input", text, *argv]
