"""Bounded, opt-in command observations and reviewable terminal evidence.

Shell reports are observations, never scheduler authority. Output attached to a
command is a rendered screen observation: the independent hook and PTY channels
cannot promise byte-exact command boundaries.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict, deque
import copy
from dataclasses import asdict, is_dataclass
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import sys
import time
import textwrap
import uuid

from .terminal_bridge import Bridge
from .terminal_recording import private_file, read_recording

MAX_BLOCKS = 512
MAX_OUTPUT = 32768
MAX_TOTAL = 2 << 20
MAX_PINS = 64
_SECRET = re.compile(r"(?im)(\b(?:password|passwd|token|secret|api[_-]?key|authorization)\b\s*[=:]\s*)([^\s,;]+)")
_ENV_ALLOWED = frozenset(("PATH", "SHELL", "LANG", "LC_ALL", "TERM", "COLORTERM", "LOADEDMODULES", "MODULEPATH", "CONDA_DEFAULT_ENV", "VIRTUAL_ENV", "OMP_NUM_THREADS", "CUDA_VISIBLE_DEVICES", "SLURM_JOB_ID"))


def clean(text, limit=4096):
    return "".join(char if char.isprintable() or char in "\n\t" else " " for char in str(text)[:limit])


def redact(text, exclusions=()):
    value = _SECRET.sub(r"\1[REDACTED]", clean(text, MAX_OUTPUT))
    for excluded in tuple(exclusions)[:128]:
        if isinstance(excluded, str) and excluded:
            value = value.replace(excluded, "[EXCLUDED]")
    return value


def _record(value):
    return asdict(value) if is_dataclass(value) else dict(value) if isinstance(value, dict) else vars(value).copy()


def job_identity(value):
    value = _record(value)
    return {key: str(value.get(key, ""))[:256] for key in ("id", "submit", "start", "cluster", "user", "account")}


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
            self.sessions[session] = {"id": session, "host": clean(host or socket.gethostname(), 256),
                "cwd": clean(cwd), "current": None, "state": "unknown", "project": "", "jobs": [],
                "artifacts": [], "metadata": {key: clean(value, 256) for key, value in (metadata or {}).items()
                                             if key in ("kind", "container", "allocation", "node")}}
        return self.sessions[session]

    def observe(self, event, *, output="", jobs=(), visible=True):
        session = self.sessions.get(event.get("session"))
        if session is None:
            raise ValueError("Shell observation belongs to an unknown session.")
        kind = event.get("event")
        now, mono = event.get("received", time.time()), event.get("monotonic", time.monotonic())
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and abs(x) <= 1e15 and math.isfinite(x) for x in (now, mono)):
            raise ValueError("Invalid observation clock.")
        if "cwd" in event:
            session["cwd"] = clean(event["cwd"])
        if kind == "start":
            if session["current"]:
                old = self.blocks.get(session["current"])
                if old:
                    old["incomplete"] = "Another start arrived before completion."
            block_id = uuid.uuid4().hex[:16]
            block = {"id": block_id, "session": session["id"], "host": session["host"],
                "cwd": session["cwd"], "project": session["project"], "jobs": copy.deepcopy(session["jobs"]),
                "artifacts": copy.deepcopy(session["artifacts"]),
                "command": clean(event.get("command", ""), 2048), "started": now, "monotonic_start": mono,
                "ended": None, "duration": None, "status": None, "output": "", "incomplete": "", "output_truncated": False,
                "coverage": "rendered screen observation; shell/PTY boundaries are asynchronous",
                "clock": {"source": "Tower local arrival clock", "remote_skew": "unknown", "remote_clock": clean(event.get("clock", "unspecified"), 128)},
                "provenance": "authenticated shell observation; not scheduler authority"}
            self.blocks[block_id] = block
            session.update(current=block_id, state="running")
            self._trim()
            return copy.deepcopy(block)
        if kind == "end":
            status = event.get("status")
            if isinstance(status, bool) or not isinstance(status, int) or not -255 <= status <= 255:
                raise ValueError("Invalid command completion status.")
            block = self.blocks.get(session["current"])
            session.update(current=None, state="prompt")
            if block is None:
                return None
            observed = clean(output, MAX_OUTPUT)
            block.update(ended=now, duration=max(0.0, mono - block["monotonic_start"]), status=status, output=observed,
                         artifacts=copy.deepcopy(session["artifacts"]), output_truncated=len(str(output)) > MAX_OUTPUT)
            self.bytes += len(observed.encode("utf-8"))
            if not visible:
                self.notices.append({"session": session["id"], "block": block["id"], "status": status,
                                     "text": "A command completed in a hidden terminal."})
            self._trim()
            return copy.deepcopy(block)
        if kind == "prompt":
            session["state"] = "prompt"
        elif kind == "submission":
            # sbatch --parsable may include ;cluster. Match the live identity.
            reported = str(event.get("job", ""))
            jid, _, cluster = reported.partition(";")
            if not re.fullmatch(r"[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?", jid) or cluster and not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", cluster):
                raise ValueError("Submission report needs an exact Slurm job ID.")
            matches = [job_identity(job) for job in jobs if str(_record(job).get("id")) == jid]
            if cluster:
                matches = [job for job in matches if job["cluster"] == cluster]
            if len(matches) != 1:
                raise ValueError("Submission is not yet a unique job in Tower's current records; retry after refresh.")
            if matches[0] not in session["jobs"]:
                session["jobs"] = (session["jobs"] + matches)[-64:]
        elif kind in ("project", "artifact", "acceptance"):
            path = event.get("project") if kind == "project" else event.get("artifact") or event.get("manifest")
            if not isinstance(path, str) or not path or "\0" in path:
                raise ValueError("The report needs a declared path.")
            if not os.path.isabs(path):
                if not os.path.isabs(session["cwd"]):
                    raise ValueError("Relative handoff needs an observed absolute working directory.")
                path = os.path.normpath(os.path.join(session["cwd"], path))
            if kind == "project":
                session["project"] = path
            else:
                record = {"path": path, "host": session["host"], "kind": kind, "time": now,
                          "provenance": "declared path; not automatically opened or executed"}
                if record not in session["artifacts"]:
                    session["artifacts"] = (session["artifacts"] + [record])[-64:]
        elif kind == "environment":
            label = event.get("label") or session["id"] + ":" + str(int(now))
            self.capture_environment(label, event.get("environment", {}),
                source="explicit shell capture at " + session["host"] + " session " + session["id"],
                packages=event.get("packages", {}), modules=event.get("modules", {}))
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
            raise ValueError("Command block is missing or has expired from retained evidence.")
        return copy.deepcopy(self.blocks[block])

    def search(self, query="", *, host=None, project=None, job=None, status=None, limit=100):
        needle = clean(query, 1024).casefold()
        result = []
        for block in reversed(tuple(self.blocks.values())):
            if host and block["host"] != host or project and block["project"] != project:
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
        pin = {"id": uuid.uuid4().hex[:16], "label": clean(label, 256), "block": block,
               "metrics": json.loads(packed), "pinned": time.time(), "live": False}
        self.pins[pin["id"]] = pin
        while len(self.pins) > MAX_PINS:
            self.pins.popitem(last=False)
        return copy.deepcopy(pin)

    def timeline(self, *, job=None):
        return [{key: copy.deepcopy(block[key]) for key in
                 ("id", "session", "host", "jobs", "started", "ended", "duration", "status", "clock")}
                for block in self.blocks.values()
                if job is None or any(value["id"] == str(job) for value in block["jobs"])]

    def failures(self):
        return [{"block": block["id"], "status": block["status"], "jobs": copy.deepcopy(block["jobs"]),
                 "cause": "unknown; a nonzero shell status does not identify the cause",
                 "artifacts": copy.deepcopy(block.get("artifacts", []))}
                for block in self.blocks.values() if block["status"] not in (None, 0) and not block.get("dismissed")]

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
            raise ValueError("Choose shorter command blocks for an interactive comparison.")
        return list(difflib.unified_diff(a, b, fromfile=left, tofile=right, lineterm=""))[:4096]

    def capture_environment(self, label, values, *, source="explicit local capture", packages=None, modules=None):
        if not isinstance(values, dict) or len(values) > 256:
            raise ValueError("Environment summary is invalid.")
        summary = {key: redact(value)[:4096] for key, value in values.items() if key in _ENV_ALLOWED}
        for group, items in (("package", packages or {}), ("module", modules or {})):
            if not isinstance(items, dict) or len(items) > 64:
                raise ValueError("Explicit package/module summaries must be bounded dictionaries.")
            for name, version in items.items():
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.+/-]{1,128}", name) or not isinstance(version, str) or len(version) > 256:
                    raise ValueError("Invalid package/module name or version.")
                summary[group + "." + name] = redact(version)
        key = clean(label, 128)
        if not key:
            raise ValueError("Environment capture needs a name.")
        self.environments[key] = {"values": summary, "source": clean(source, 256), "time": time.time(),
                                  "coverage": "allowlisted environment; package/module versions are available only when explicitly supplied",
                                  "package_coverage": "supplied" if packages else "unavailable", "module_coverage": "supplied" if modules else "unavailable"}
        self.environments.move_to_end(key)
        while len(self.environments) > 32:
            self.environments.popitem(last=False)
        return copy.deepcopy(self.environments[key])

    def environment_diff(self, first, second):
        if first not in self.environments or second not in self.environments:
            raise ValueError("Both named environment captures are required.")
        a, b = self.environments[first]["values"], self.environments[second]["values"]
        return [{"key": key, "before": a.get(key), "after": b.get(key)}
                for key in sorted(a.keys() | b.keys()) if a.get(key) != b.get(key)]

    def bundle(self, blocks, *, exclusions=()):
        if not blocks or len(blocks) > 32:
            raise ValueError("Select between 1 and 32 command blocks.")
        selected = [self.get(key) for key in dict.fromkeys(blocks)]
        for block in selected:
            for key in ("command", "output", "cwd", "project"):
                block[key] = redact(block[key], exclusions)
        environments = copy.deepcopy(self.environments)
        for capture in environments.values():
            capture["values"] = {key: redact(value, exclusions) for key, value in capture["values"].items()}
        return {"schema": "tower.terminal-evidence/v1", "created": time.time(), "blocks": selected,
                "environment": environments,
                "limitations": ["Review before sharing: automatic redaction cannot identify every secret.",
                                "No input stream or files are included; commands are inert evidence.",
                                "Rendered observations do not reproduce every shell side effect."]}

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
    return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

def publish_share(path, text, *, session, exclusions=()):
    """Explicit same-uid read-only snapshot, not a network or input endpoint."""
    data = json.dumps({"schema": "tower.terminal-share/v1", "session": clean(session, 128),
                       "created": time.time(), "access": "same-user read-only file snapshot",
                       "text": redact(text, exclusions)}, ensure_ascii=True).encode()
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
    if not isinstance(value, dict) or value.get("schema") != "tower.terminal-share/v1" or not isinstance(value.get("text"), str):
        raise ValueError("Unsupported read-only snapshot.")
    return {"session": clean(value.get("session", ""), 128), "text": clean(value["text"], MAX_OUTPUT),
            "created": value.get("created"), "access": "read only"}


def initialize(app):
    state = getattr(app, "terminal_fusion_state", None)
    if state is None:
        state = {"store": FusionStore(), "bridge": None, "last_poll": 0.0, "rejected": 0,
                 "preview": None, "last_notice": None, "busy": False, "task_generation": 0, "last_result": None, "callback": None}
        app.terminal_fusion_state = state
    return state


def register_session(app, session_id, *, host=None, cwd="", metadata=None):
    state = initialize(app)
    if state["bridge"] is None:
        state["bridge"] = Bridge()
    state["store"].register(session_id, host=host, cwd=cwd, metadata=metadata)
    return state["bridge"].register(session_id)


def _terminal(app):
    from . import terminal_ui
    return terminal_ui.initialize(app)


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
            lines.append("".join(str(cell.get("text", cell.get("data", ""))) if isinstance(cell, dict)
                                 else str(cell[0]) if isinstance(cell, (tuple, list)) and cell else str(cell) for cell in row))
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
    from . import terminal_ui
    terminal = _terminal(app)
    active = getattr(terminal, "active", "")
    visible = (terminal_ui.showing(app) and getattr(app, "mode", "main") == "main"
               and terminal.body_rect is not None and not terminal.report and not terminal.dialog and not terminal.preview)
    for message in messages:
        try:
            _sid, snapshot = _service_snapshot(app, message["session"])
            state["store"].observe(message, output=_snapshot_text(snapshot), jobs=jobs,
                                    visible=visible and message["session"] == active)
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
        if hub is not None and state.get("callback") is not None and hasattr(hub, "cancel_task"):
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
    return next((copy.deepcopy(block) for block in reversed(tuple(state["store"].blocks.values()))
                 if block["session"] == session and block["ended"] is not None), None)


def _background(app, title, worker):
    """Use the bounded research lane; workers return data, never touch the UI."""
    state = initialize(app)
    if state["busy"]:
        raise ValueError("A terminal evidence operation is already running; wait for its result.")
    state["task_generation"] += 1
    generation = state["task_generation"]
    terminal = _terminal(app)
    owner = (terminal.active, terminal.focus_generation, getattr(app, "tab", None))
    def complete(value):
        if generation != state["task_generation"]:
            return
        state.update(busy=False, callback=None)
        if isinstance(value, Exception):
            if hasattr(app, "fail"):
                app.fail("Terminal evidence: " + clean(value))
            return
        state["last_result"] = {"title": title, "lines": list(value)[:4096]}
        current = _terminal(app)
        if owner == (current.active, current.focus_generation, getattr(app, "tab", None)):
            _show(app, value, title)
        elif hasattr(app, "say"):
            app.say(title + " finished; :terminal evidence-result opens the result.")
    if getattr(app, "interactive", False):
        hub = getattr(app, "research", None)
        if hub is None or not hub.start_task(worker, complete):
            raise ValueError("The background reader is busy; retry the terminal evidence operation shortly.")
        state.update(busy=True, callback=complete)
        if hasattr(app, "say"):
            app.say(title + " in the background.")
    else:
        state["busy"] = True
        try:
            value = worker()
        except Exception as exc:
            value = exc
        complete(value)
    return True


def command_names():
    return ["terminal"]


def actions():
    """Every evidence feature has a selectable Tools control and explicit input."""
    direct = [
        ("Command blocks", "blocks"), ("Command timeline", "timeline"),
        ("Command failures", "failures"), ("Evidence bookmarks", "pins"),
        ("Project/artifact handoffs", "handoffs"), ("Stop recording", "record off"),
        ("Scientific acceptance", "acceptance"), ("Last evidence result", "evidence-result"),
        ("Capability self-test", "selftest"), ("Terminal evidence help", "evidence-help"),
        ("Install optional Bash hooks", "hook bash"), ("Install optional Zsh hooks", "hook zsh"),
        ("Install optional Fish hooks", "hook fish"),
    ]
    prompts = [
        ("Open block: BLOCK_ID", "block"), ("Copy block: BLOCK_ID", "block-copy"),
        ("Search evidence: TEXT [job=ID]", "evidence-search"),
        ("Bookmark: BLOCK_ID LABEL", "bookmark"), ("Dismiss failure: BLOCK_ID", "dismiss"),
        ("Compare output: LEFT_ID RIGHT_ID", "compare"),
        ("Review export: BLOCK_ID... [exclude=TEXT]", "evidence-preview"),
        ("Export reviewed evidence: NEW_FILE", "evidence-export"),
        ("Record output: NEW_FILE", "record on"), ("Replay recording: FILE", "replay"),
        ("Capture Tower environment: NAME", "environment capture"),
        ("Compare environments: BEFORE AFTER", "environment diff"),
        ("Share read-only snapshot: NEW_FILE", "share"), ("View private snapshot: FILE", "share-read"),
        ("Open observed project: SESSION_ID", "handoff project"),
        ("Open declared artifact: SESSION_ID INDEX", "handoff artifact"),
        ("Open linked job logs: JOB_ID", "job-link"),
    ]
    return [(label, ["terminal", *shlex.split(command)]) for label, command in direct] + [
        (label, ["terminal", "prompt", *shlex.split(command)]) for label, command in prompts]


def _show(app, lines, title="Terminal evidence"):
    from . import terminal_ui
    terminal_ui.show_report(app, [clean(line) for line in lines][:4096], title=title)


def _rows(values):
    return json.dumps(values, indent=2, ensure_ascii=False, allow_nan=False).splitlines()


def run_command(app, args):
    if not args or args[0] != "terminal" or len(args) < 2:
        return False
    known = {"blocks", "block", "block-copy", "evidence-search", "bookmark", "pins", "timeline", "failures", "dismiss", "compare", "evidence-preview", "evidence-export", "environment", "share", "share-read", "replay", "record", "hook", "handoffs", "handoff", "job-link", "acceptance", "selftest", "evidence-help", "evidence-result"}
    action = args[1]
    if action not in known:
        return False
    state = initialize(app)
    store = state["store"]
    rest = args[2:]
    try:
        if action == "blocks":
            lines = ["Optional shell hooks establish command boundaries. No shell input is recorded.",
                     "Output is an observed screen; independent channels cannot guarantee exact command output."]
            lines += [f"{b['id']}  session={b['session']}  exit={b['status']}  {b['duration']}s  {b['command'] or '(command text not captured)'}" for b in store.search(limit=100)]
            lines.append(f"Evicted blocks: {store.evicted}. Open: :terminal block ID")
            _show(app, lines, "Command blocks")
        elif action == "block":
            if len(rest) != 1:
                raise ValueError("Usage: terminal block BLOCK_ID")
            _show(app, _rows(store.get(rest[0])))
        elif action == "block-copy":
            if len(rest) != 1:
                raise ValueError("Usage: terminal block-copy BLOCK_ID")
            block = store.get(rest[0])
            from . import clipboard
            text = (block["command"] + "\n" if block["command"] else "") + block["output"]
            if not text:
                raise ValueError("That command block has no captured command text or observed output.")
            message = clipboard.copy(text, getattr(app, "state_dir", None), **clipboard.options(app))
            if hasattr(app, "say"):
                app.say(message)
        elif action == "evidence-search":
            if not rest:
                raise ValueError("Usage: terminal evidence-search TEXT [host=HOST] [job=ID] [project=PATH] [status=N]")
            filters, query = {}, []
            for value in rest:
                key, sep, item = value.partition("=")
                if sep and key in ("host", "job", "project", "status"):
                    filters[key] = int(item) if key == "status" else item
                else:
                    query.append(value)
            _show(app, _rows(store.search(" ".join(query), **filters)), "Cross-session evidence search")
        elif action == "bookmark":
            if not rest:
                raise ValueError("Usage: terminal bookmark BLOCK_ID [LABEL]")
            _show(app, _rows(store.bookmark(rest[0], " ".join(rest[1:]), metrics=_metric_snapshot(app, store.get(rest[0])))), "Pinned evidence")
        elif action == "pins":
            _show(app, _rows(list(store.pins.values())), "Pinned evidence snapshots")
        elif action == "timeline":
            _show(app, ["Clock: Tower local arrival. Remote skew is unknown; do not infer microsecond causality."] + _rows(store.timeline(job=rest[0] if rest else None)), "Command timeline")
        elif action == "failures":
            _show(app, _rows(store.failures()), "Failure investigation cards")
        elif action == "dismiss":
            if len(rest) != 1:
                raise ValueError("Usage: terminal dismiss BLOCK_ID")
            store.dismiss(rest[0]); _show(app, ["Failure card dismissed."])
        elif action == "compare":
            if len(rest) not in (2, 3):
                raise ValueError("Usage: terminal compare LEFT_ID RIGHT_ID [none|trailing-space]")
            frozen = FusionStore()
            frozen.blocks = OrderedDict((key, store.get(key)) for key in rest[:2])
            _background(app, "Command output comparison", lambda: frozen.compare(*rest[:2], normalize=rest[2] if len(rest) == 3 else "none") or ["No output differences."])
        elif action == "evidence-preview":
            if not rest:
                raise ValueError("Usage: terminal evidence-preview BLOCK_ID... [exclude=TEXT]")
            ids = [value for value in rest if not value.startswith("exclude=")]
            exclusions = [value[8:] for value in rest if value.startswith("exclude=")]
            bundle = store.bundle(ids, exclusions=exclusions)
            lines = preview_lines(bundle)
            state["preview"] = {"ids": ids, "exclusions": exclusions, "digest": hashlib.sha256(json.dumps({key: bundle[key] for key in ("blocks", "environment")}, sort_keys=True).encode()).hexdigest()}
            _show(app, lines + ["Review this content. Export: :terminal evidence-export NEW_PRIVATE_FILE"], "Review redacted reproduction evidence")
        elif action == "evidence-export":
            if len(rest) != 1 or not state["preview"]:
                raise ValueError("First use terminal evidence-preview BLOCK_ID...; then terminal evidence-export NEW_FILE")
            preview = state["preview"]
            bundle = store.bundle(preview["ids"], exclusions=preview["exclusions"])
            if hashlib.sha256(json.dumps({key: bundle[key] for key in ("blocks", "environment")}, sort_keys=True).encode()).hexdigest() != preview["digest"]:
                state["preview"] = None
                raise ValueError("Evidence changed after preview. Review it again before exporting.")
            _background(app, "Evidence exported", lambda: _rows(export_bundle(rest[0], bundle)))
            state["preview"] = None
        elif action == "environment":
            if len(rest) == 2 and rest[0] == "capture":
                _show(app, _rows(store.capture_environment(rest[1], dict(os.environ))), "Tower process environment summary")
            elif len(rest) == 3 and rest[0] == "diff":
                _show(app, _rows(store.environment_diff(rest[1], rest[2])), "Environment differences")
            else:
                raise ValueError("Usage: terminal environment capture NAME | terminal environment diff BEFORE AFTER. Captures Tower's environment, not unknown child changes.")
        elif action == "share":
            if len(rest) != 1:
                raise ValueError("Usage: terminal share NEW_PRIVATE_FILE. Output may contain secrets; inspect before sharing.")
            sid, snapshot = _service_snapshot(app)
            if not sid or snapshot is None:
                raise ValueError("Open a terminal before publishing its output snapshot.")
            text = _snapshot_text(snapshot)
            _background(app, "Read-only local snapshot", lambda: _rows(publish_share(rest[0], text, session=sid)))
        elif action == "share-read":
            if len(rest) != 1:
                raise ValueError("Usage: terminal share-read PRIVATE_FILE")
            _background(app, "Read-only shared snapshot", lambda: _rows(read_share(rest[0])))
        elif action == "record":
            if not rest or rest[0] not in ("on", "off") or (rest[0] == "on" and len(rest) != 2):
                raise ValueError("Usage: terminal record on NEW_PRIVATE_FILE | terminal record off. Output can contain echoed secrets.")
            terminal = _terminal(app)
            if not terminal.active or terminal.service is None:
                raise ValueError("Open a terminal before recording.")
            terminal.service.request(terminal.active, "record_start" if rest[0] == "on" else "record_stop", **({"path": rest[1]} if rest[0] == "on" else {}))
            _show(app, ["Recording request sent. Output only, 16 MiB maximum; input events are never recorded.", "Child output may still contain secrets. Session health reports recording status."], "Terminal recording")
        elif action == "replay":
            if len(rest) != 1:
                raise ValueError("Usage: terminal replay PRIVATE_RECORDING")
            from . import terminal_playback
            terminal_playback.start(app, rest[0])
        elif action == "hook":
            if len(rest) != 1 or rest[0] not in ("bash", "zsh", "fish"):
                raise ValueError("Usage: terminal hook bash|zsh|fish")
            from . import terminal_ui
            from .terminal_bridge import helper_command
            _sid, snapshot = _service_snapshot(app)
            if not snapshot or snapshot.get("kind") != "local" or snapshot.get("metadata", {}).get("host") != socket.gethostname():
                raise ValueError("Install hooks in an explicit local Tower shell. Remote/allocation/container sessions need a helper installed on their own host.")
            from .terminal_workflows import shell_kind
            actual_shell = shell_kind(snapshot.get("metadata", {}).get("shell", ""))
            if actual_shell != rest[0]:
                raise ValueError("The requested hook shell differs from the selected terminal's known shell (" + actual_shell + ").")
            command = helper_command(rest[0], "hook", "--shell", rest[0])
            text = "source (" + command + " | psub)" if rest[0] == "fish" else "source <(" + command + ")"
            terminal_ui.prepare_input(app, text, label="Optional local shell hooks; confirm this session is at its shell prompt", helper_kind="shell-hook")
        elif action == "handoffs":
            _show(app, _rows(list(store.sessions.values())), "Declared project, job and artifact context")
        elif action == "job-link":
            if len(rest) != 1:
                raise ValueError("Usage: terminal job-link JOB_ID")
            linked = [identity for session in store.sessions.values() for identity in session["jobs"] if identity["id"] == rest[0]]
            with app.store.lock:
                current = [job_identity(job) for job in list(app.store.jobs) + list(app.store.finished) if str(_record(job).get("id")) == rest[0]]
            if len(current) != 1 or current[0] not in linked:
                raise ValueError("The observed job attempt is no longer current; refresh its submission link.")
            app.open_log(rest[0])
        elif action == "handoff":
            if len(rest) < 2 or rest[0] not in ("project", "artifact"):
                raise ValueError("Usage: terminal handoff project SESSION_ID | terminal handoff artifact SESSION_ID INDEX")
            source = store.sessions.get(rest[1])
            if not source:
                raise ValueError("Unknown terminal session.")
            if source["host"] != socket.gethostname() or getattr(getattr(app, "research", None), "files", None) and getattr(app.research.files, "remote", False):
                raise ValueError("This handoff requires the terminal and Research to use the same local host; remote paths are not local substitutes.")
            from . import project_ui
            if rest[0] == "project":
                if len(rest) != 2 or not source["project"]:
                    raise ValueError("Report a project path from that session first.")
                project_ui.run_command(app, ["project", source["project"]])
            else:
                if len(rest) != 3:
                    raise ValueError("Usage: terminal handoff artifact SESSION_ID INDEX (zero-based)")
                index = int(rest[2])
                if not 0 <= index < len(source["artifacts"]):
                    raise ValueError("Artifact index is outside the reported list.")
                selected = source["artifacts"][index]
                project = project_ui.initialize(app)
                tree = project.get("tree") or {}
                root = tree.get("root", "")
                node = next((node for node in tree.get("nodes", ()) if root and os.path.normpath(os.path.join(root, node["path"])) == selected["path"]), None)
                if node is None:
                    raise ValueError("Open this project's declared outputs first (:outputs). Only a verified declared artifact can be handed to Research.")
                project_ui._preview(app, node)
        elif action == "acceptance":
            from . import ops_ui
            ops_ui.run_command(app, ["ops", "open", "acceptance"])
            if len(rest) == 2:
                # Explicit paths populate the existing review form; no execution.
                ops_ui.initialize(app)["values"].update(manifest=rest[0], results=rest[1])
            elif rest:
                raise ValueError("Usage: terminal acceptance [POLICY_PATH RESULTS_PATH]")
        elif action == "selftest":
            from . import terminal_diagnostics
            terminal_diagnostics.start(app)
        elif action == "evidence-result":
            result = state.get("last_result")
            if result is None:
                raise ValueError("No completed terminal evidence operation is available.")
            _show(app, result["lines"], result["title"])
        elif action == "evidence-help":
            _show(app, [":terminal hook bash|zsh|fish — insert optional hook setup; press Enter deliberately.",
                "In shell: tower terminal report submission --job EXACT_SBATCH_PARSABLE_ID",
                "In shell: tower terminal report project --project PATH",
                "In shell: tower terminal report artifact --artifact PATH",
                "In shell: tower terminal report acceptance --manifest PATH",
                "In shell: tower terminal report environment --label NAME [--summary PACKAGE_MODULE_JSON]",
                ":terminal blocks | block ID | block-copy ID | evidence-search TEXT [job=ID] [status=N]",
                ":terminal bookmark ID LABEL | pins | timeline [JOB_ID] | failures | dismiss ID",
                ":terminal compare LEFT RIGHT [trailing-space]",
                ":terminal evidence-preview ID... [exclude=TEXT] → evidence-export NEW_FILE",
                ":terminal environment capture NAME | environment diff BEFORE AFTER",
                ":terminal record on NEW_FILE | record off | replay FILE",
                ":terminal share NEW_FILE | share-read FILE — private, read-only, same-user snapshots",
                ":terminal handoffs | handoff project SESSION | handoff artifact SESSION INDEX",
                ":terminal job-link ID | acceptance [POLICY RESULTS] | selftest",
                "No shell hook, command capture, recording, sharing or export is enabled automatically.",
                "All hook metadata is an observation. It cannot authorize scheduler changes."], "Terminal evidence controls")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        if hasattr(app, "fail"):
            app.fail("Terminal: " + clean(exc))
        else:
            _show(app, [str(exc)], "Terminal action failed")
    return True



def preview_lines(bundle):
    """Every exported field remains reviewable; reject oversized previews."""
    lines = list(bundle["limitations"])
    for block in bundle["blocks"]:
        lines.append("Command block " + block["id"])
        for key, value in block.items():
            rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
            lines.append(key + ":")
            for line in str(rendered).splitlines() or [""]:
                lines.extend(textwrap.wrap(line, width=4000, replace_whitespace=False, drop_whitespace=False) or [""])
    lines.append("Explicit environment summaries:")
    for line in _rows(bundle["environment"]):
        lines.extend(textwrap.wrap(line, width=4000, replace_whitespace=False, drop_whitespace=False) or [""])
    if len(lines) > 4000:
        raise ValueError("The complete preview exceeds 4000 lines. Select fewer command blocks before exporting.")
    return lines


def _metric_snapshot(app, block):
    """Pin only already-published local samples; never perform I/O or poll Slurm."""
    source = getattr(app, "store", None)
    if source is None or not hasattr(source, "series_publication"):
        return []
    result = []
    lower, upper = block["started"], block["ended"] or time.time()
    for identity in block["jobs"][:8]:
        values = source.series_publication(identity["id"], memory_only=True)
        for sample in values[-4000:]:
            stamp = sample.get("t")
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and math.isfinite(stamp) and lower <= stamp <= upper:
                record = {key: value for key, value in sample.items() if isinstance(value, (int, float, str)) and len(str(value)) <= 256}
                result.append({"job": identity["id"], "sample": record})
                if len(result) >= 128:
                    return result
    return result

def recording_text(events):
    """Safe plain-text replay summary. Strip complete escape/control sequences."""
    data = b"".join(event["data"] for event in events if event["event"] == "output")
    text = data.decode("utf-8", "replace")
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    clipped = len(text) > MAX_TOTAL
    value = clean(text, MAX_TOTAL)
    return value + ("\n[Replay text summary truncated at 2 MiB; the recording retains additional output.]" if clipped else "")


def cli(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("hook", "report"):
        from .terminal_bridge import cli as bridge_cli
        return bridge_cli(argv)
    if argv and argv[0] == "selftest":
        from . import terminal_diagnostics
        return terminal_diagnostics.cli(argv[1:])
    parser = argparse.ArgumentParser(prog="tower terminal", description="Terminal hooks, read-only recordings, and capability diagnostics.",
        epilog="hook --shell bash|zsh|fish: print optional integration. report EVENT: send an owned local observation. selftest [--json]: run bounded local PTY checks. UI controls: :terminal evidence-help.")
    parser.add_argument("action", choices=("hook", "report", "selftest", "inspect", "replay", "share-read"))
    parser.add_argument("path", nargs="?")
    args = parser.parse_args(argv)
    if not args.path:
        parser.error(args.action + " requires a path")
    try:
        if args.action == "share-read":
            print(read_share(args.path)["text"])
        else:
            if args.action == "replay" and sys.stdin.isatty() and sys.stdout.isatty():
                from . import terminal_playback
                return terminal_playback.cli([args.path])
            header, events = read_recording(args.path)
            if args.action == "inspect":
                print(json.dumps({"header": header, "events": len(events), "duration": events[-1]["t"] if events else 0,
                                  "execution": "none"}, indent=2))
            else:
                print(recording_text(events))
        return 0
    except (ValueError, OSError) as exc:
        print("tower terminal: " + clean(exc), file=sys.stderr)
        return 2
