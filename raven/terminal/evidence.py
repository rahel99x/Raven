"""Bounded shell observations and session evidence, without terminal UI imports."""

from __future__ import annotations
from collections import OrderedDict, deque
import copy
from dataclasses import asdict, is_dataclass
import difflib
import hashlib
import json
import math
import os
import re
import socket
import time
import uuid
from .bridge import Bridge
from .recording import private_file
from .text import clean as clean

MAX_BLOCKS = 512
MAX_OUTPUT = 32768
MAX_TOTAL = 2 << 20
MAX_PINS = 64
_ENV_ALLOWED = frozenset(
    (
        "PATH",
        "SHELL",
        "LANG",
        "LC_ALL",
        "TERM",
        "COLORTERM",
        "LOADEDMODULES",
        "MODULEPATH",
        "CONDA_DEFAULT_ENV",
        "VIRTUAL_ENV",
        "OMP_NUM_THREADS",
        "CUDA_VISIBLE_DEVICES",
        "SLURM_JOB_ID",
    )
)


def redact(text, exclusions=()):
    from ..redaction import redact as redact_credentials

    value = redact_credentials(clean(text, MAX_OUTPUT))
    for excluded in tuple(exclusions)[:128]:
        if isinstance(excluded, str) and excluded:
            value = value.replace(excluded, "[EXCLUDED]")
    return value


def _record(value):
    return (
        asdict(value)
        if is_dataclass(value)
        else dict(value)
        if isinstance(value, dict)
        else vars(value).copy()
    )


def job_identity(value):
    value = _record(value)
    return {
        key: str(value.get(key, ""))[:256]
        for key in ("id", "submit", "start", "cluster", "user", "account")
    }


class FusionStore:
    """In-memory, bounded evidence; never stores terminal input automatically."""

    def __init__(self, *, max_blocks=MAX_BLOCKS, max_bytes=MAX_TOTAL):
        if not 1 <= max_blocks <= MAX_BLOCKS or not 1024 <= max_bytes <= MAX_TOTAL:
            raise ValueError("Invalid evidence budget.")
        self.max_blocks, self.max_bytes = max_blocks, max_bytes
        self.blocks = OrderedDict()
        self.sessions = {}
        self.pins = OrderedDict()
        self.environments = OrderedDict()
        self.notices = deque(maxlen=64)
        self.evicted = 0
        self.bytes = 0

    def register(self, session, *, host=None, cwd="", metadata=None):
        session = str(session)[:128]
        if not session:
            raise ValueError("A session identity is required.")
        if session not in self.sessions and len(self.sessions) >= 32:
            raise ValueError("Evidence session limit reached.")
        existing = self.sessions.get(session)
        if existing is None:
            self.sessions[session] = {
                "id": session,
                "host": clean(host or socket.gethostname(), 256),
                "cwd": clean(cwd),
                "current": None,
                "state": "unknown",
                "project": "",
                "jobs": [],
                "artifacts": [],
                "metadata": {
                    key: clean(value, 256)
                    for key, value in (metadata or {}).items()
                    if key in ("kind", "container", "allocation", "node")
                },
            }
        return self.sessions[session]

    def observe(self, event, *, output="", jobs=(), visible=True):
        session = self.sessions.get(event.get("session"))
        if session is None:
            raise ValueError("Shell observation belongs to an unknown session.")
        kind = event.get("event")
        now, mono = (
            event.get("received", time.time()),
            event.get("monotonic", time.monotonic()),
        )
        if not all(
            isinstance(x, (int, float))
            and not isinstance(x, bool)
            and abs(x) <= 1e15
            and math.isfinite(x)
            for x in (now, mono)
        ):
            raise ValueError("Invalid observation clock.")
        if "cwd" in event:
            session["cwd"] = clean(event["cwd"])
        if kind == "start":
            if session["current"]:
                old = self.blocks.get(session["current"])
                if old:
                    old["incomplete"] = "Another start arrived before completion."
            block_id = uuid.uuid4().hex[:16]
            block = {
                "id": block_id,
                "session": session["id"],
                "host": session["host"],
                "cwd": session["cwd"],
                "project": session["project"],
                "jobs": copy.deepcopy(session["jobs"]),
                "artifacts": copy.deepcopy(session["artifacts"]),
                "command": clean(event.get("command", ""), 2048),
                "started": now,
                "monotonic_start": mono,
                "ended": None,
                "duration": None,
                "status": None,
                "output": "",
                "incomplete": "",
                "output_truncated": False,
                "coverage": "rendered screen observation; shell/PTY boundaries are asynchronous",
                "clock": {
                    "source": "Raven local arrival clock",
                    "remote_skew": "unknown",
                    "remote_clock": clean(event.get("clock", "unspecified"), 128),
                },
                "provenance": "authenticated shell observation; not scheduler authority",
            }
            self.blocks[block_id] = block
            session.update(current=block_id, state="running")
            self._trim()
            return copy.deepcopy(block)
        if kind == "end":
            status = event.get("status")
            if (
                isinstance(status, bool)
                or not isinstance(status, int)
                or not -255 <= status <= 255
            ):
                raise ValueError("Invalid command completion status.")
            block = self.blocks.get(session["current"])
            session.update(current=None, state="prompt")
            if block is None:
                return None
            observed = clean(output, MAX_OUTPUT)
            block.update(
                ended=now,
                duration=max(0.0, mono - block["monotonic_start"]),
                status=status,
                output=observed,
                artifacts=copy.deepcopy(session["artifacts"]),
                output_truncated=len(str(output)) > MAX_OUTPUT,
            )
            self.bytes += len(observed.encode("utf-8"))
            if not visible:
                self.notices.append(
                    {
                        "session": session["id"],
                        "block": block["id"],
                        "status": status,
                        "text": "A command completed in a hidden terminal.",
                    }
                )
            self._trim()
            return copy.deepcopy(block)
        if kind == "prompt":
            session["state"] = "prompt"
        elif kind == "submission":
            # sbatch --parsable may include ;cluster. Match the live identity.
            reported = str(event.get("job", ""))
            jid, _, cluster = reported.partition(";")
            if (
                not re.fullmatch(r"[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?", jid)
                or cluster
                and not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", cluster)
            ):
                raise ValueError("Submission report needs an exact Slurm job ID.")
            matches = [
                job_identity(job) for job in jobs if str(_record(job).get("id")) == jid
            ]
            if cluster:
                matches = [job for job in matches if job["cluster"] == cluster]
            if len(matches) != 1:
                raise ValueError(
                    "Submission is not yet a unique job in Raven's current records; retry after refresh."
                )
            if matches[0] not in session["jobs"]:
                session["jobs"] = (session["jobs"] + matches)[-64:]
        elif kind in ("project", "artifact", "acceptance"):
            path = (
                event.get("project")
                if kind == "project"
                else event.get("artifact") or event.get("manifest")
            )
            if not isinstance(path, str) or not path or "\0" in path:
                raise ValueError("The report needs a declared path.")
            if not os.path.isabs(path):
                if not os.path.isabs(session["cwd"]):
                    raise ValueError(
                        "Relative handoff needs an observed absolute working directory."
                    )
                path = os.path.normpath(os.path.join(session["cwd"], path))
            if kind == "project":
                session["project"] = path
            else:
                record = {
                    "path": path,
                    "host": session["host"],
                    "kind": kind,
                    "time": now,
                    "provenance": "declared path; not automatically opened or executed",
                }
                if record not in session["artifacts"]:
                    session["artifacts"] = (session["artifacts"] + [record])[-64:]
        elif kind == "environment":
            label = event.get("label") or session["id"] + ":" + str(int(now))
            self.capture_environment(
                label,
                event.get("environment", {}),
                source="explicit shell capture at "
                + session["host"]
                + " session "
                + session["id"],
                packages=event.get("packages", {}),
                modules=event.get("modules", {}),
            )
        return None

    def _trim(self):
        while len(self.blocks) > self.max_blocks or self.bytes > self.max_bytes:
            key, value = self.blocks.popitem(last=False)
            self.bytes -= len(value["output"].encode("utf-8"))
            self.evicted += 1
            for session in self.sessions.values():
                if session["current"] == key:
                    session["current"] = None

    def get(self, block):
        if block not in self.blocks:
            raise ValueError(
                "Command block is missing or has expired from retained evidence."
            )
        return copy.deepcopy(self.blocks[block])

    def search(
        self, query="", *, host=None, project=None, job=None, status=None, limit=100
    ):
        needle = clean(query, 1024).casefold()
        result = []
        for block in reversed(tuple(self.blocks.values())):
            if (
                host
                and block["host"] != host
                or project
                and block["project"] != project
            ):
                continue
            if job and not any(value["id"] == str(job) for value in block["jobs"]):
                continue
            if status is not None and block["status"] != status:
                continue
            if needle not in (block["command"] + "\n" + block["output"]).casefold():
                continue
            result.append(copy.deepcopy(block))
            if len(result) >= min(max(1, limit), 100):
                break
        return result

    def bookmark(self, block_id, label="", *, metrics=()):
        block = self.get(block_id)
        metrics = list(metrics)[:256]
        packed = json.dumps(metrics, allow_nan=False)
        if len(packed.encode()) > MAX_OUTPUT:
            raise ValueError("Pinned metric evidence exceeds its budget.")
        pin = {
            "id": uuid.uuid4().hex[:16],
            "label": clean(label, 256),
            "block": block,
            "metrics": json.loads(packed),
            "pinned": time.time(),
            "live": False,
        }
        self.pins[pin["id"]] = pin
        while len(self.pins) > MAX_PINS:
            self.pins.popitem(last=False)
        return copy.deepcopy(pin)

    def timeline(self, *, job=None):
        return [
            {
                key: copy.deepcopy(block[key])
                for key in (
                    "id",
                    "session",
                    "host",
                    "jobs",
                    "started",
                    "ended",
                    "duration",
                    "status",
                    "clock",
                )
            }
            for block in self.blocks.values()
            if job is None or any(value["id"] == str(job) for value in block["jobs"])
        ]

    def failures(self):
        return [
            {
                "block": block["id"],
                "status": block["status"],
                "jobs": copy.deepcopy(block["jobs"]),
                "cause": "unknown; a nonzero shell status does not identify the cause",
                "artifacts": copy.deepcopy(block.get("artifacts", [])),
            }
            for block in self.blocks.values()
            if block["status"] not in (None, 0) and not block.get("dismissed")
        ]

    def dismiss(self, block_id):
        self.get(block_id)
        self.blocks[block_id]["dismissed"] = True

    def compare(self, left, right, *, normalize="none"):
        if normalize not in ("none", "trailing-space"):
            raise ValueError("Normalization must be none or trailing-space.")
        first, second = self.get(left), self.get(right)
        a, b = first["output"].splitlines(), second["output"].splitlines()
        if normalize == "trailing-space":
            a, b = [line.rstrip() for line in a], [line.rstrip() for line in b]
        # Bound line length and line counts before difflib's quadratic matching.
        if len(a) > 1024 or len(b) > 1024 or any(len(line) > 4096 for line in a + b):
            raise ValueError(
                "Choose shorter command blocks for an interactive comparison."
            )
        return list(
            difflib.unified_diff(a, b, fromfile=left, tofile=right, lineterm="")
        )[:4096]

    def capture_environment(
        self,
        label,
        values,
        *,
        source="explicit local capture",
        packages=None,
        modules=None,
    ):
        if not isinstance(values, dict) or len(values) > 256:
            raise ValueError("Environment summary is invalid.")
        summary = {
            key: redact(value)[:4096]
            for key, value in values.items()
            if key in _ENV_ALLOWED
        }
        for group, items in (("package", packages or {}), ("module", modules or {})):
            if not isinstance(items, dict) or len(items) > 64:
                raise ValueError(
                    "Explicit package/module summaries must be bounded dictionaries."
                )
            for name, version in items.items():
                if (
                    not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.+/-]{1,128}", name)
                    or not isinstance(version, str)
                    or len(version) > 256
                ):
                    raise ValueError("Invalid package/module name or version.")
                summary[group + "." + name] = redact(version)
        key = clean(label, 128)
        if not key:
            raise ValueError("Environment capture needs a name.")
        self.environments[key] = {
            "values": summary,
            "source": clean(source, 256),
            "time": time.time(),
            "coverage": "allowlisted environment; package/module versions are available only when explicitly supplied",
            "package_coverage": "supplied" if packages else "unavailable",
            "module_coverage": "supplied" if modules else "unavailable",
        }
        self.environments.move_to_end(key)
        while len(self.environments) > 32:
            self.environments.popitem(last=False)
        return copy.deepcopy(self.environments[key])

    def environment_diff(self, first, second):
        if first not in self.environments or second not in self.environments:
            raise ValueError("Both named environment captures are required.")
        a, b = self.environments[first]["values"], self.environments[second]["values"]
        return [
            {"key": key, "before": a.get(key), "after": b.get(key)}
            for key in sorted(a.keys() | b.keys())
            if a.get(key) != b.get(key)
        ]

    def bundle(self, blocks, *, exclusions=()):
        if not blocks or len(blocks) > 32:
            raise ValueError("Select between 1 and 32 command blocks.")
        selected = [self.get(key) for key in dict.fromkeys(blocks)]
        for block in selected:
            for key in ("command", "output", "cwd", "project"):
                block[key] = redact(block[key], exclusions)
        environments = copy.deepcopy(self.environments)
        for capture in environments.values():
            capture["values"] = {
                key: redact(value, exclusions)
                for key, value in capture["values"].items()
            }
        return {
            "schema": "raven.terminal-evidence/v1",
            "created": time.time(),
            "blocks": selected,
            "environment": environments,
            "limitations": [
                "Review before sharing: automatic redaction cannot identify every secret.",
                "No input stream or files are included; commands are inert evidence.",
                "Rendered observations do not reproduce every shell side effect.",
            ],
        }

    def export(self, path, blocks, *, exclusions=()):
        return export_bundle(path, self.bundle(blocks, exclusions=exclusions))


def export_bundle(path, bundle):
    data = json.dumps(bundle, ensure_ascii=True, allow_nan=False, indent=2).encode()
    if len(data) > MAX_TOTAL:
        raise ValueError("Evidence export exceeds its byte budget.")
    fd = private_file(path, create=True)
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
    finally:
        if fd >= 0:
            os.close(fd)
    return {
        "path": str(path),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def publish_share(path, text, *, session, exclusions=()):
    """Explicit same-uid read-only snapshot, not a network or input endpoint."""
    data = json.dumps(
        {
            "schema": "raven.terminal-share/v1",
            "session": clean(session, 128),
            "created": time.time(),
            "access": "same-user read-only file snapshot",
            "text": redact(text, exclusions),
        },
        ensure_ascii=True,
    ).encode()
    fd = private_file(path, create=True)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    return {"path": str(path), "bytes": len(data), "access": "read only; mode 0600"}


def read_share(path):
    fd = private_file(path)
    with os.fdopen(fd, "rb") as stream:
        data = stream.read(MAX_OUTPUT * 8 + 1)
    if len(data) > MAX_OUTPUT * 8:
        raise ValueError("Shared snapshot exceeds its byte budget.")
    value = json.loads(data)
    if (
        not isinstance(value, dict)
        or value.get("schema") != "raven.terminal-share/v1"
        or not isinstance(value.get("text"), str)
    ):
        raise ValueError("Unsupported read-only snapshot.")
    return {
        "session": clean(value.get("session", ""), 128),
        "text": clean(value["text"], MAX_OUTPUT),
        "created": value.get("created"),
        "access": "read only",
    }


def initialize(app):
    state = getattr(app, "terminal_fusion_state", None)
    if state is None:
        state = {
            "store": FusionStore(),
            "bridge": None,
            "last_poll": 0.0,
            "rejected": 0,
            "preview": None,
            "last_notice": None,
            "busy": False,
            "task_generation": 0,
            "last_result": None,
            "callback": None,
        }
        app.terminal_fusion_state = state
    return state


def register_session(app, session_id, *, host=None, cwd="", metadata=None):
    state = initialize(app)
    if state["bridge"] is None:
        state["bridge"] = Bridge()
    state["store"].register(session_id, host=host, cwd=cwd, metadata=metadata)
    return state["bridge"].register(session_id)


def _terminal(app):
    from .session_state import initialize as session_state

    return session_state(app)


def _service_snapshot(app, session=None):
    state = _terminal(app)
    sid = session or getattr(state, "active", "")
    service = getattr(state, "service", None)
    if service is None or not sid:
        return sid, None
    accessor = getattr(service, "snapshot", None)
    return sid, accessor(sid) if callable(accessor) else None


def _snapshot_text(snapshot):
    if snapshot is None:
        return ""
    if is_dataclass(snapshot):
        snapshot = asdict(snapshot)
    if not isinstance(snapshot, dict):
        return ""
    raw = snapshot.get("text") or snapshot.get("lines") or snapshot.get("screen", [])
    if isinstance(raw, str):
        return clean(raw, MAX_OUTPUT)
    lines = []
    for row in list(raw)[:512]:
        if isinstance(row, str):
            lines.append(row)
        elif isinstance(row, dict):
            lines.append(str(row.get("text", "")))
        elif isinstance(row, (list, tuple)):
            lines.append(
                "".join(
                    str(cell.get("text", cell.get("data", "")))
                    if isinstance(cell, dict)
                    else str(cell[0])
                    if isinstance(cell, (tuple, list)) and cell
                    else str(cell)
                    for cell in row
                )
            )
    return clean("\n".join(lines), MAX_OUTPUT)


def tick(app):
    state = getattr(app, "terminal_fusion_state", None)
    if state is None or state["bridge"] is None:
        return False
    now = time.monotonic()
    if now - state["last_poll"] < 0.025:
        return False
    state["last_poll"] = now
    messages = state["bridge"].poll(32)
    if not messages:
        return False
    jobs = []
    store = getattr(app, "store", None)
    if any(value["event"] == "submission" for value in messages) and store is not None:
        with store.lock:
            jobs = list(store.jobs) + list(store.finished)
    from .session_state import showing

    terminal = _terminal(app)
    active = getattr(terminal, "active", "")
    visible = (
        showing(app)
        and getattr(app, "mode", "main") == "main"
        and terminal.body_rect is not None
        and not terminal.report
        and not terminal.dialog
        and not terminal.preview
    )
    for message in messages:
        try:
            _sid, snapshot = _service_snapshot(app, message["session"])
            state["store"].observe(
                message,
                output=_snapshot_text(snapshot),
                jobs=jobs,
                visible=visible and message["session"] == active,
            )
        except (ValueError, OSError, TypeError):
            state["rejected"] += 1
    if state["store"].notices:
        notice = state["store"].notices[-1]
        if state["last_notice"] != notice["block"]:
            state["last_notice"] = notice["block"]
            if hasattr(app, "say"):
                app.say(notice["text"] + " Exit status " + str(notice["status"]) + ".")
    return True


def close(app):
    state = getattr(app, "terminal_fusion_state", None)
    if state:
        state["task_generation"] += 1
        hub = getattr(app, "research", None)
        if (
            hub is not None
            and state.get("callback") is not None
            and hasattr(hub, "cancel_task")
        ):
            hub.cancel_task(state["callback"])
        if state["bridge"]:
            state["bridge"].close()
            state["bridge"] = None


def unregister_session(app, session_id):
    state = getattr(app, "terminal_fusion_state", None)
    if state:
        if state["bridge"]:
            state["bridge"].unregister(session_id)
        state["store"].sessions.pop(session_id, None)


def latest_completed(app, session):
    state = getattr(app, "terminal_fusion_state", None)
    if not state:
        return None
    return next(
        (
            copy.deepcopy(block)
            for block in reversed(tuple(state["store"].blocks.values()))
            if block["session"] == session and block["ended"] is not None
        ),
        None,
    )
