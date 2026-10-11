#!/usr/bin/env python3
"""Copyable stdlib producer for the Tower project interchange convention.

One coordinator writes each run. This example is instrumentation, not a job
launcher or a distributed logging service. Unknown measurements stay unknown.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time

MAX_JSON = 1 << 20
MAX_RUNS = 256
MAX_AGGREGATE = 1 << 20
MAX_SCRIPT = 8 << 20
MAX_LOGS = 256
MAX_LOG_JSON = 256 << 10
PLANNING_VIEWS = {"predict", "forecast", "blockers", "tradeoffs", "scaling", "workflow"}
REPORT_VIEWS = PLANNING_VIEWS | {"planning", "submit"}
PLAN_FIELDS = ("schema", "script", "workdir", "argv", "command", "overrides", "script_sha256",
               "parameters", "inputs", "outputs", "resources", "directives", "issues", "valid")
PLAN_WORKFLOW_FIELDS = ("workflow_node_id", "symbolic_dependencies", "workflow_orchestration",
                        "requires_workflow_orchestration", "submittable")
RESOURCE_KEYS = {"partition", "cpus", "nodes", "gpus", "gpu_type", "account", "qos",
                 "mem_bytes", "time_seconds"}
QUERY_KEYS = RESOURCE_KEYS | {"name", "script_sha256", "parameters", "input_size", "memory_scope"}
SUMMARY_KEYS = RESOURCE_KEYS | {"schema", "id", "name", "state", "memory_scope", "runtime_seconds",
    "memory_bytes", "cpu_seconds", "script_sha256", "input_size", "start", "end", "submit", "job_id",
    "experiment_id", "attempt", "project_id", "exit_code", "workers", "problem_size", "repeat",
    "fingerprint", "work_units", "parameters", "results", "metadata"}
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
            "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED", "UNKNOWN", "INTERRUPTED"}


def _text(value, name, limit=128):
    if not isinstance(value, str) or not value or len(value) > limit or not value.isprintable():
        raise ValueError(f"{name} must be a nonempty printable string of at most {limit} characters")
    return value


def _number(value, name, *, minimum=0, maximum=1e100, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        valid = math.isfinite(value) and minimum <= value <= maximum
    except OverflowError:
        valid = False
    if not valid or integer and (not isinstance(value, int) or value > (1 << 63) - 1):
        raise ValueError(f"{name} must be a finite {'integer ' if integer else ''}number >= {minimum}")
    return value


def _directory(path):
    path = Path(path).absolute()
    for component in reversed((path, *path.parents)):
        if not stat.S_ISDIR(component.lstat().st_mode):
            raise ValueError(f"directory required; symlinks refused: {component}")
    return path


def _under(root, relative, *, create_parent=False):
    root = _directory(root)
    raw_input = str(relative)
    if (len(raw_input) > 4096 or "\\" in raw_input or any(c in raw_input for c in "*?[]") or not raw_input.isprintable()
            or any(p in {".", ".."} for p in raw_input.split("/"))
            or "//" in raw_input or raw_input.endswith("/")):
        raise ValueError("path must be exact, printable, and without traversal, globs, or empty components")
    path = Path(relative)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError as exc:
            raise ValueError("path must be inside the selected project") from exc
    raw = str(path)
    if raw in {"", "."} or "\\" in raw or any(p in {"", ".", ".."} for p in raw.split("/")):
        raise ValueError("path must be an exact project-relative path without traversal")
    parent = root
    for component in path.parts[:-1]:
        parent /= component
        if create_parent:
            parent.mkdir(mode=0o700, exist_ok=True)
        _directory(parent)
    return root / path


def _encoded(value, limit=MAX_JSON):
    stack, count, text_chars = [(value, 0)], 0, 0
    while stack:
        current, depth = stack.pop()
        count += 1
        if depth > 32 or count > 100000:
            raise ValueError("JSON exceeds the depth or value budget")
        if isinstance(current, dict):
            if any(not isinstance(key, str) for key in current):
                raise ValueError("JSON object keys must be strings")
            if len(current) + len(stack) + count > 100000:
                raise ValueError("JSON exceeds the value budget")
            text_chars += sum(len(key) for key in current)
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            if len(current) + len(stack) + count > 100000:
                raise ValueError("JSON exceeds the value budget")
            stack.extend((child, depth + 1) for child in current)
        elif isinstance(current, str):
            text_chars += len(current)
        elif isinstance(current, int) and not isinstance(current, bool) and current.bit_length() > 256:
            raise ValueError("JSON integer exceeds the bounded reader profile")
        if text_chars > limit:
            raise ValueError(f"JSON text exceeds the {limit}-byte producer budget")
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > limit:
        raise ValueError(f"JSON exceeds the {limit}-byte producer budget")
    return raw


def _read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_JSON:
            raise ValueError("JSON must be a regular file of at most 1 MiB")
        chunks, remaining = [], MAX_JSON + 1
        while remaining:
            part = os.read(fd, min(65536, remaining))
            if not part:
                break
            chunks.append(part)
            remaining -= len(part)
        raw = b"".join(chunks)
        after, named = os.fstat(fd), os.stat(path, follow_symlinks=False)
        signature = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                   value.st_mtime_ns, value.st_ctime_ns)
        if len(raw) > MAX_JSON or len(raw) != before.st_size or signature(before) != signature(after) or signature(after) != signature(named):
            raise ValueError("JSON changed while being read or exceeds the byte budget")
    finally:
        os.close(fd)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON keys are refused")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda token: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    except (UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError("invalid bounded UTF-8 JSON") from exc
    _encoded(value)  # rejects exponent overflow and out-of-budget values
    return value


def _atomic_json(path, value, *, replace=False):
    path = Path(path)
    _directory(path.parent)
    raw = _encoded(value)
    if replace and os.path.lexists(path) and not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("refusing to replace a nonregular file")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)  # exclusive publication: existing files survive
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _resources(value):
    if not isinstance(value, dict) or set(value) - RESOURCE_KEYS:
        raise ValueError("resources must contain only documented request/allocation fields")
    out = {}
    for key, item in value.items():
        if item is None:
            continue
        if key in {"cpus", "nodes", "gpus", "mem_bytes"}:
            out[key] = _number(item, key, minimum=0 if key == "gpus" else 1,
                               maximum=(1 << 63) - 1 if key == "mem_bytes" else 1_000_000_000, integer=True)
        elif key == "time_seconds":
            out[key] = _number(item, key, minimum=1e-9, maximum=3_162_240_000)
        else:
            out[key] = "" if key == "gpu_type" and item == "" else _text(item, key)
    return out


def _parameters(value):
    if not isinstance(value, dict) or len(value) > 64:
        raise ValueError("parameters must be an object with at most 64 entries")
    stack, count = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 4 or count > 128:
            raise ValueError("parameters exceed four nesting levels or 128 total values")
        if isinstance(item, dict):
            if len(item) > 64:
                raise ValueError("parameters object exceeds 64 entries")
            for key, child in item.items():
                _text(key, "parameter name", 64)
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            if len(item) > 64:
                raise ValueError("parameter list exceeds 64 entries")
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if item != "":
                _text(item, "parameter value", 256)
        elif item is None or isinstance(item, bool):
            pass
        else:
            _number(item, "parameter value", minimum=-1e100)
    _encoded(value, 16384)
    return value


def _log_path(run_dir, value):
    """Validate an exact location without opening, scanning, or creating logs."""
    raw = _text(value, "log path", 4096)
    if "\\" in raw or any(c in raw for c in "*?[]") or raw.endswith("/") or raw.startswith("~"):
        raise ValueError("log path must name an exact file without globs or backslashes")
    supplied = Path(raw)
    path = Path(os.path.normpath(supplied if supplied.is_absolute() else run_dir / supplied))
    if len(str(path)) > 4096:
        raise ValueError("resolved log path exceeds 4096 characters")
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue  # a registered log may be created later by its actual writer
        if component == path:
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("registered log must be a regular file when present")
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("log path ancestors must be directories; symlinks refused")
    return str(path)


def _log_index(run_dir, value, manifest=None):
    if (not isinstance(value, dict) or value.get("schema") != "tower.logs/v1"
            or set(value) - {"schema", "logs", "run_id", "job_id"}
            or not isinstance(value.get("logs"), list) or len(value["logs"]) > MAX_LOGS):
        raise ValueError("log index must be a bounded tower.logs/v1 object")
    if "run_id" in value and value["run_id"] != run_dir.name:
        raise ValueError("log index run_id must describe the selected run")
    if "job_id" in value:
        _text(value["job_id"], "log index job_id")
        if manifest is not None and value["job_id"] != manifest.get("job_id"):
            raise ValueError("log index job_id must match the actual run job_id")
    ids, paths = set(), set()
    for entry in value["logs"]:
        if not isinstance(entry, dict) or set(entry) - {"id", "path", "label", "group", "description"}:
            raise ValueError("log entries use id, path, label, group, and description only")
        ident = entry.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", ident):
            raise ValueError("log id must be a safe stable ID of at most 128 characters")
        path = _log_path(run_dir, entry.get("path"))
        if ident in ids or path in paths:
            raise ValueError("log IDs and resolved paths must be unique")
        ids.add(ident)
        paths.add(path)
        for key, limit in (("label", 160), ("group", 160), ("description", 512)):
            if key in entry and (entry[key] != "" or key != "description"):
                _text(entry[key], f"log {key}", limit)
    _encoded(value, MAX_LOG_JSON)
    return value


def register_log(run_dir, id, path, *, label="", group="", description=""):
    """Atomically register another exact log location for one run coordinator.

    Relative paths, including explicit sibling locations, resolve to this run
    directory. Absolute paths can name
    explicit external logs; they must be rebound when a project is relocated.
    Logs may not exist yet. This helper never opens their contents or scans a
    directory, and it refuses duplicate IDs/paths and symlinks when present.
    """
    run_dir = _directory(run_dir)
    manifest = _read_json(run_dir / "run.json")
    if (not isinstance(manifest, dict) or manifest.get("schema") != "tower.run/v1"
            or manifest.get("run_id") != run_dir.name):
        raise ValueError("run manifest must describe the selected run")
    index_path = run_dir / "logs.json"
    index = _log_index(run_dir, _read_json(index_path), manifest)
    entry = {"id": id, "path": str(path)}
    for key, value in (("label", label), ("group", group), ("description", description)):
        if value != "":
            entry[key] = value
    index["logs"].append(entry)
    _log_index(run_dir, index, manifest)
    _atomic_json(index_path, index, replace=True)
    return entry


def _summary(row):
    """Check the shared producer profile without adding a schema dependency."""
    if (not isinstance(row, dict) or row.get("schema") != "tower.summary/v1"
            or not isinstance(row.get("state"), str) or row["state"] not in TERMINAL):
        raise ValueError("each selected run must have a terminal tower.summary/v1 object")
    if set(row) - SUMMARY_KEYS:
        raise ValueError("unknown summary fields; place project extensions in results or metadata")
    ident = row.get("id")
    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", ident):
        raise ValueError("summary.id must be a safe unique attempt ID of at most 128 characters")
    _text(row.get("name"), "summary.name")
    _resources({key: value for key, value in row.items() if key in RESOURCE_KEYS})
    if "parameters" in row:
        _parameters(row["parameters"])
    if row.get("script_sha256") is not None and (not isinstance(row["script_sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", row["script_sha256"])):
        raise ValueError("script_sha256 must be a real 64-character hexadecimal digest")
    for key in ("job_id", "experiment_id", "project_id"):
        if row.get(key) is not None:
            _text(row[key], key)
    if row.get("fingerprint") is not None:
        _text(row["fingerprint"], "fingerprint", 256)
    if row.get("attempt") is not None:
        _number(row["attempt"], "attempt", minimum=1, maximum=2_147_483_647, integer=True)
    for key in ("start", "end", "submit"):
        if row.get(key) is not None:
            _number(row[key], key, maximum=253402300799)
    if row.get("start") is not None and row.get("end") is not None and row["end"] < row["start"]:
        raise ValueError("end must not precede start")
    for key in ("runtime_seconds", "cpu_seconds", "memory_bytes", "input_size"):
        if row.get(key) is not None:
            _number(row[key], key)
    if row.get("memory_scope") is not None and (not isinstance(row["memory_scope"], str) or row["memory_scope"] not in {"job_peak", "per_node_peak", "max_task_rss"}):
        raise ValueError("invalid memory_scope")
    if row.get("memory_bytes") is not None and row.get("memory_scope") is None:
        raise ValueError("observed memory requires memory_scope")
    for key, maximum in (("workers", 1_000_000), ("repeat", 10_000)):
        if row.get(key) is not None:
            _number(row[key], key, minimum=1, maximum=maximum, integer=True)
    for key in ("problem_size", "work_units"):
        if row.get(key) is not None:
            _number(row[key], key, minimum=1e-300, maximum=1e300)
    if row.get("exit_code") is not None:
        _number(row["exit_code"], "exit_code", minimum=-2_147_483_648, maximum=2_147_483_647, integer=True)
        if row["state"] == "COMPLETED" and row["exit_code"] != 0:
            raise ValueError("COMPLETED requires exit_code zero when recorded")
    for key in ("results", "metadata"):
        if key in row and not isinstance(row[key], dict):
            raise ValueError(f"{key} must be an object")
    _encoded(row)
    return row


def begin_run(root, run_id, *, name, script, experiment_id=None, attempt=1,
              resources=None, parameters=None, input_size=None, job_id=None):
    """Create an exclusive attempt directory; return its absolute Path.

    Supply actual allocation/request fields only when known. ``script`` is the
    scientific source entry point relative to root, not merely a batch wrapper.
    Include other source/input fingerprints in parameters for multi-file work.
    """
    root = _directory(root)
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise ValueError("run_id must be a safe directory name of at most 128 characters")
    _text(name, "name")
    experiment_id = _text(experiment_id or name, "experiment_id")
    _number(attempt, "attempt", minimum=1, maximum=2_147_483_647, integer=True)
    parameters = _parameters({} if parameters is None else parameters)
    resource_data = _resources(resources or {})
    if input_size is not None:
        _number(input_size, "input_size")
    if job_id is not None:
        _text(job_id, "job_id", 128)
    script_path = _under(root, script)
    fd = os.open(script_path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SCRIPT:
            raise ValueError("scientific source must be a regular file of at most 8 MiB")
        digest, remaining = hashlib.sha256(), MAX_SCRIPT + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining == 0:
            raise ValueError("scientific source exceeded its byte budget")
        final, named = os.fstat(fd), os.stat(script_path, follow_symlinks=False)
        signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if signature(info) != signature(final) or signature(final) != signature(named):
            raise ValueError("scientific source changed while hashing")
    finally:
        os.close(fd)
    runs = _under(root, "runs/.unused", create_parent=True).parent
    run_dir = runs / run_id
    run_dir.mkdir(mode=0o700)  # no exist_ok: retries must have a new unique run ID
    for child in ("outputs", "logs", "passports", "reports"):
        (run_dir / child).mkdir(mode=0o700)
    for filename in ("metrics.jsonl", "logs/stdout.log", "logs/stderr.log"):
        (run_dir / filename).touch(mode=0o600, exist_ok=False)
    manifest = {"schema": "tower.run/v1", "run_id": run_id, "experiment_id": experiment_id,
                "attempt": attempt, "name": name, "state": "RUNNING", "start": time.time(),
                "parameters": parameters, "resources": resource_data,
                "provenance": {"script": str(script_path.relative_to(root)), "script_sha256": digest.hexdigest()},
                "paths": {"metrics": "metrics.jsonl", "summary": "summary.json", "outputs": "outputs",
                          "logs": "logs", "stdout": "logs/stdout.log", "stderr": "logs/stderr.log",
                          "log_index": "logs.json", "passports": "passports",
                          "planning": "reports/planning.json", "submit": "reports/submit.json"}}
    if job_id is not None:
        manifest["job_id"] = job_id
    if input_size is not None:
        manifest["input_size"] = input_size
    log_index = {"schema": "tower.logs/v1", "run_id": run_id, "logs": [
        {"id": "application.stdout", "path": "logs/stdout.log", "label": "Application stdout", "group": "Application"},
        {"id": "application.stderr", "path": "logs/stderr.log", "label": "Application stderr", "group": "Application"}]}
    if job_id is not None:
        log_index["job_id"] = job_id
    _atomic_json(run_dir / "logs.json", _log_index(run_dir, log_index, manifest))
    _atomic_json(run_dir / "run.json", manifest)
    return run_dir


def write_metric(run_dir, metrics, *, step=None, phase=None, progress=None, t=None):
    """Append one native Tower JSONL row; use one coordinator per run."""
    run_dir = _directory(run_dir)
    if not isinstance(metrics, dict) or len(metrics) > 64:
        raise ValueError("metrics must be an object with at most 64 entries")
    row = {"t": _number(time.time() if t is None else t, "t"),
           "metrics": {_text(key, "metric name", 96): _number(value, "metric value", minimum=-math.inf)
                       for key, value in metrics.items()}}
    if step is not None:
        row["step"] = _number(step, "step", maximum=(1 << 63) - 1, integer=True)
    if phase is not None:
        row["phase"] = "" if phase == "" else _text(phase, "phase", 160)
    if progress is not None:
        if not isinstance(progress, dict) or set(progress) - {"completed", "total", "unit"}:
            raise ValueError("progress must contain completed, total, and optional unit")
        completed = _number(progress.get("completed"), "progress.completed")
        total = _number(progress.get("total"), "progress.total", minimum=1e-300)
        if completed > total:
            raise ValueError("progress.completed must not exceed total")
        unit = progress.get("unit", "")
        row["progress"] = {"completed": completed, "total": total,
                           "unit": "" if unit == "" else _text(unit, "progress.unit", 64)}
    raw = _encoded(row, 65536)
    fd = os.open(run_dir / "metrics.jsonl", os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("metrics must be a regular file")
        if os.write(fd, raw) != len(raw):
            raise OSError("incomplete metric append; the run coordinator must stop")
    finally:
        os.close(fd)


def finish_run(run_dir, *, state, runtime_seconds=None, cpu_seconds=None,
               memory_bytes=None, memory_scope=None, results=None, metadata=None, exit_code=None, scaling=None):
    """Publish a terminal summary without inventing scheduler or memory data.

    Measure wall time using perf_counter in the application. CPU seconds must
    name their measurement scope in project documentation (the demo uses only
    its coordinator process). A timeout/OOM runtime is a censored observation.
    """
    run_dir = _directory(run_dir)
    if not isinstance(state, str) or state not in TERMINAL:
        raise ValueError("finish_run requires an explicit uppercase terminal state")
    manifest = _read_json(run_dir / "run.json")
    if (not isinstance(manifest, dict) or manifest.get("schema") != "tower.run/v1" or manifest.get("state") != "RUNNING"
            or not {"run_id", "name", "experiment_id", "attempt", "start", "parameters", "provenance"} <= manifest.keys()
            or not isinstance(manifest.get("provenance"), dict) or "script_sha256" not in manifest["provenance"]
            or manifest.get("run_id") != run_dir.name):
        raise ValueError("run manifest must describe an unfinished Tower run")
    summary = {"schema": "tower.summary/v1", "id": manifest["run_id"], "name": manifest["name"],
               "experiment_id": manifest["experiment_id"], "attempt": manifest["attempt"], "state": state,
               "start": manifest["start"], "end": time.time(), "parameters": manifest["parameters"],
               "script_sha256": manifest["provenance"]["script_sha256"], **_resources(manifest.get("resources", {}))}
    for key in ("job_id", "input_size"):
        if key in manifest:
            summary[key] = manifest[key]
    for key, value in (("runtime_seconds", runtime_seconds), ("cpu_seconds", cpu_seconds), ("memory_bytes", memory_bytes)):
        if value is not None:
            summary[key] = _number(value, key)
    if memory_bytes is not None:
        if not isinstance(memory_scope, str) or memory_scope not in {"job_peak", "per_node_peak", "max_task_rss"}:
            raise ValueError("observed memory requires an explicit memory_scope")
        summary["memory_scope"] = memory_scope
    elif memory_scope is not None:
        raise ValueError("memory_scope requires observed memory_bytes")
    for key, value in (("results", results), ("metadata", metadata)):
        if value is not None:
            if not isinstance(value, dict):
                raise ValueError(f"{key} must be an object")
            summary[key] = value
    if exit_code is not None:
        if isinstance(exit_code, bool) or not isinstance(exit_code, int) or not -2_147_483_648 <= exit_code <= 2_147_483_647:
            raise ValueError("exit_code must be an integer")
        summary["exit_code"] = exit_code
        if state == "COMPLETED" and exit_code != 0:
            raise ValueError("COMPLETED requires exit_code zero when recorded")
    if scaling is not None:
        if not isinstance(scaling, dict) or set(scaling) != {"workers", "problem_size", "repeat"}:
            raise ValueError("scaling requires exactly workers, problem_size, and repeat")
        summary.update(workers=_number(scaling["workers"], "workers", minimum=1, maximum=1_000_000, integer=True),
                       problem_size=_number(scaling["problem_size"], "problem_size", minimum=1e-9, maximum=1e300),
                       repeat=_number(scaling["repeat"], "repeat", minimum=1, maximum=10_000, integer=True))
    _summary(summary)
    _atomic_json(run_dir / "summary.json", summary)
    manifest.update(state=state, end=summary["end"])
    _atomic_json(run_dir / "run.json", manifest, replace=True)
    return summary


def export_planning(root, run_dirs, *, reference_run=None, output="reports/planning.json"):
    """Aggregate at most 256 explicit attempt paths; never scan or launch jobs.

    All terminal states are preserved. Reference query identity is explicit;
    missing resources remain missing and Tower will withhold unsupported fits.
    """
    root = _directory(root)
    if isinstance(run_dirs, (str, bytes, Path)):
        raise ValueError("run_dirs must be an iterable of explicit run paths, not a single string")
    rows, seen, paths, consumed = [], set(), {}, 0
    for index, value in enumerate(run_dirs):
        if index >= MAX_RUNS:
            raise ValueError("at most 256 explicit run directories may be aggregated")
        run_dir = _directory(_under(root, value))
        row = _summary(_read_json(run_dir / "summary.json"))
        manifest = _read_json(run_dir / "run.json")
        ident = row["id"]
        if (not isinstance(manifest, dict) or manifest.get("schema") != "tower.run/v1"
                or manifest.get("run_id") != ident or ident != run_dir.name or manifest.get("state") != row["state"]):
            raise ValueError("run directory, manifest, and terminal summary must agree")
        if ident in seen:
            raise ValueError("duplicate run identity in aggregate")
        seen.add(ident)
        consumed += len(_encoded(row))
        if consumed > MAX_AGGREGATE:
            raise ValueError("selected summaries exceed the 1 MiB aggregate budget")
        rows.append(row)
        paths[run_dir] = row
    if not rows:
        raise ValueError("choose at least one explicit run directory")
    bundle = {"version": 1, "kind": "tower.planning", "history": rows}
    if reference_run is not None:
        reference = _directory(_under(root, reference_run))
        if reference not in paths:
            raise ValueError("reference run must be one of the explicitly selected runs")
        bundle["query"] = {key: value for key, value in paths[reference].items() if key in QUERY_KEYS and value is not None}
    scaling_rows = [row for row in rows if all(row.get(key) is not None for key in ("workers", "problem_size", "repeat"))]
    if scaling_rows:
        bundle["scaling"] = scaling_rows
    _encoded(bundle, MAX_AGGREGATE)
    target = _under(root, output, create_parent=True)
    if target.exists() or target.is_symlink():
        existing = _read_json(target)
        if not isinstance(existing, dict) or existing.get("version") != 1 or existing.get("kind") != "tower.planning":
            raise ValueError("refusing to overwrite a file that is not a Tower planning bundle")
    _atomic_json(target, bundle, replace=True)
    return bundle


def publish_research(run_dir, view, document):
    """Publish one actual native source and bind its exact per-run location.

    This stdlib helper checks the interchange header, finite bounded JSON and
    selected job identity. Tower's native analysis performs scientific checks;
    this helper does not create observations, launch jobs, or execute plans.
    ``submit`` accepts only an intact native ``tower run prepare`` result and
    remains a read-only report in the dashboard. Use one run coordinator.
    """
    if not isinstance(view, str) or view not in REPORT_VIEWS:
        raise ValueError("report view must be planning, predict, forecast, blockers, tradeoffs, scaling, workflow, or submit")
    run_dir = _directory(run_dir)
    manifest = _read_json(run_dir / "run.json")
    if (not isinstance(manifest, dict) or manifest.get("schema") != "tower.run/v1"
            or manifest.get("run_id") != run_dir.name or not isinstance(manifest.get("paths", {}), dict)):
        raise ValueError("run manifest must describe the selected run")
    if not isinstance(document, dict):
        raise ValueError("report must be a native JSON object")
    _encoded(document, MAX_AGGREGATE)
    if view == "submit":
        if (document.get("schema") != "tower.submission-plan/v1"
                or not set(PLAN_FIELDS) <= document.keys() or not isinstance(document.get("valid"), bool)
                or not isinstance(document.get("resources"), dict) or not isinstance(document.get("issues"), list)):
            raise ValueError("submit report must be an intact native tower.submission-plan/v1 preflight")
        fields = {key: document[key] for key in PLAN_FIELDS}
        fields.update({key: document[key] for key in PLAN_WORKFLOW_FIELDS if key in document})
        digest = hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=True,
                                           allow_nan=False).encode()).hexdigest()
        if document.get("plan_id") != digest:
            raise ValueError("preflight plan_id does not match its captured review fields")
    else:
        kinds = {"tower.planning"}
        if view in {"workflow", "scaling"}:
            kinds.add("tower." + view)
        if (type(document.get("version")) is not int or document["version"] != 1
                or document.get("kind") not in kinds):
            raise ValueError("planning reports require their native version-1 planning/workflow/scaling header")
        if "job_id" in document and document["job_id"] != manifest.get("job_id"):
            raise ValueError("report job_id must match this run's actual scheduler identity")
    relative = f"reports/{view}.json"
    target = _under(run_dir, relative, create_parent=True)
    updated = dict(manifest, paths=dict(manifest.get("paths", {}), **{view: relative}))
    _encoded(updated, 65536)
    _atomic_json(target, document, replace=True)
    _atomic_json(run_dir / "run.json", updated, replace=True)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="aggregate explicit completed/failed attempt directories")
    export.add_argument("runs", nargs="+")
    export.add_argument("--root", default=".")
    export.add_argument("--reference", help="one selected run supplies the prediction query")
    export.add_argument("--output", default="reports/planning.json")
    report = commands.add_parser("report", help="atomically attach an actual native per-view research source")
    report.add_argument("view", choices=sorted(REPORT_VIEWS))
    report.add_argument("source", help="existing native JSON source; never executed")
    report.add_argument("--run", required=True, help="exact attempt directory containing run.json")
    args = parser.parse_args(argv)
    try:
        if args.command == "report":
            target = publish_research(args.run, args.view, _read_json(args.source))
            print(f"Published {args.view} source: {target}; no scheduler action.")
            return 0
        output = export_planning(args.root, args.runs, reference_run=args.reference, output=args.output)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"reporting: {exc}\n")
    print(f"Wrote {args.output}: {len(output['history'])} attempts; failures retained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
