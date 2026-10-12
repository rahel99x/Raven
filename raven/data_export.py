"""Bounded retained-measurement exports; no scheduler or provider probes.

Plans freeze source byte watermarks. Append-only sources may grow after planning;
rotation, truncation, changed endpoints, and non-append mutations are reported.
Linux publication uses renameat2(RENAME_NOREPLACE). Other platforms fail closed
rather than describing an overwrite-prone rename as an atomic publication.
"""
from __future__ import annotations

from collections import OrderedDict
import csv
import ctypes
import datetime as dt
import errno
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import time
import uuid

FAMILIES = ("application", "gpu", "cpu-memory", "summary", "research", "trace", "logs", "artifacts")
DEFAULT_FAMILIES = FAMILIES[:6]
MAX_ATTEMPTS = 256
MAX_SOURCES = 4096
MAX_METADATA = 1 << 20
MAX_LINE = 1 << 20
MAX_METRICS = 1024
MAX_OPEN_FILES = 16
CHUNK = 64 << 10
MAX_ISSUES = 512
IDENTITY_COLUMNS = ("project", "project_id", "host", "run_id", "attempt", "cluster", "job_id", "array_parent", "array_task", "submission_token", "submit_time", "script_sha256")
CLOCK_COLUMNS = ("timestamp", "timestamp_utc", "source_time", "clock", "elapsed_seconds", "sequence")
APPLICATION_COLUMNS = IDENTITY_COLUMNS + CLOCK_COLUMNS + ("metric", "value", "unit", "phase", "step", "rank", "node", "source", "scope", "quality", "status", "reason")
GPU_COLUMNS = IDENTITY_COLUMNS + CLOCK_COLUMNS + ("node", "provider", "device_uuid", "device_index", "device_bdf", "partition", "utilization_percent", "memory_used_mib", "memory_total_mib", "source", "scope", "quality", "status", "reason")
CPU_COLUMNS = IDENTITY_COLUMNS + CLOCK_COLUMNS + ("node", "cpu_seconds", "rss_bytes", "process_count", "source", "scope", "quality", "status", "reason")
COLUMNS = {"application": APPLICATION_COLUMNS, "gpu": GPU_COLUMNS, "cpu-memory": CPU_COLUMNS}
OBSERVATIONS = {"gpu": ("utilization_percent", "memory_used_mib", "memory_total_mib"), "cpu-memory": ("cpu_seconds", "rss_bytes", "process_count")}
UNITS = {"utilization_percent": "percent", "memory_used_mib": "MiB", "memory_total_mib": "MiB", "cpu_seconds": "seconds", "rss_bytes": "bytes", "process_count": "processes"}


class ExportError(ValueError):
    """A request cannot be safely planned (CLI exit 2)."""


class ExportCancelled(Exception):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("nonfinite JSON number")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("invalid UTF-8 or nested JSON") from exc


def local_host():
    return socket.gethostname()


def _is_local(host):
    return not host or host in {"local", "localhost", local_host(), socket.getfqdn()}


def _text(value, field, limit=4096):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ExportError(f"{field} must be nonempty text without control characters (at most {limit} bytes)")
    return value


def _path(value, *, absolute=True):
    value = _text(str(value), "path")
    if "\\" in value or (absolute and not value.startswith("/")):
        raise ExportError("Path must be an absolute POSIX path without backslashes")
    if not absolute and value.startswith("/"):
        raise ExportError("Relative location must belong to the job's project folder")
    parts = value.split("/")
    if any(x == ".." for x in parts):
        raise ExportError("Path traversal is not allowed; choose an explicit absolute destination")
    if any(len(x.encode("utf-8")) > 255 for x in parts):
        raise ExportError("Path component exceeds 255 bytes")
    return os.path.normpath(value)


def _dir(path, *, create=False):
    """Walk from / using pinned dirfds; never follow any component symlink."""
    path = _path(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open("/", flags)
    try:
        for name in Path(path).parts[1:]:
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open(path):
    path = _path(path)
    parent = _dir(str(Path(path).parent))
    try:
        fd = os.open(Path(path).name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
    finally:
        os.close(parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ExportError("Source must be a regular file")
    return fd


def _signature(info):
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def _ends(fd, size):
    # Bounded optimistic append-only verification, never a full preview scan.
    return hashlib.sha256(os.pread(fd, min(CHUNK, size), 0) + os.pread(fd, min(CHUNK, size), max(0, size - CHUNK))).hexdigest()


def _read_metadata(path, limit=MAX_METADATA):
    fd = _open(path)
    try:
        info = os.fstat(fd)
        if info.st_size > limit:
            raise ExportError(f"Metadata exceeds {limit} bytes: {path}")
        raw = os.read(fd, limit + 1)
        if len(raw) > limit or _signature(info) != _signature(os.fstat(fd)):
            raise ExportError(f"Metadata changed during discovery: {path}")
        value = _decode(raw)
        if not isinstance(value, dict):
            raise ExportError(f"Metadata must be a JSON object: {path}")
        return value
    finally:
        os.close(fd)


def _under(root, relative):
    relative = _path(relative, absolute=False)
    if relative == ".":
        raise ExportError("Source path must name an exact file")
    return str(Path(root) / relative)


def device_id(row):
    """Fully qualified retained device identity; UUID/MIG slashes stay intact."""
    physical = row.get("device_uuid") or row.get("device_bdf")
    if not physical and row.get("device_index") is not None:
        physical = str(row["device_index"])
    identity = "/".join(str(row.get(key) or "") for key in ("node", "provider")) + "/" + str(physical or "")
    if row.get("partition"):
        identity += "/" + str(row["partition"])
    return identity


def _source(family, ident, path, *, schema="", coverage=None, append=False, **extra):
    result = dict(family=family, id=str(ident), path=str(path), schema=schema, coverage=coverage or {},
                  available=False, reason="Not recorded", metrics=[], devices=[], append=append, **extra)
    try:
        fd = _open(path)
        try:
            info = os.fstat(fd)
            result.update(available=True, reason="", size=info.st_size, watermark=_signature(info), endpoint_sha256=_ends(fd, info.st_size))
            if family in COLUMNS and info.st_size == 0:
                result.update(available=False, reason="Source exists, but no retained measurement records have been written yet")
        finally:
            os.close(fd)
    except (OSError, ValueError) as exc:
        result["reason"] = str(exc)
    return result


def _issue(code, message, **context):
    return dict(code=code, message=str(message), **context)


def _add_issue(issues, code, message, **context):
    if len(issues) < MAX_ISSUES:
        issues.append(_issue(code, message, **context))
    elif len(issues) == MAX_ISSUES:
        issues.append(_issue("issue-limit", "Additional source issues omitted after the bounded issue limit"))


def _selection(values, name, maximum=MAX_METRICS):
    if values is None:
        return []
    if not isinstance(values, (list, tuple)) or len(values) > maximum:
        raise ExportError(f"{name} must be a list of at most {maximum} entries")
    return list(dict.fromkeys(_text(value, name, 512) for value in values))


def _shared_anchor(project, run_id):
    """Verify a declared shared project anchor; never infer one from host names."""
    manifest = _read_metadata(str(Path(project) / "runs" / run_id / "run.json"))
    metadata = manifest.get("metadata") or {}
    automation = metadata.get("automation") or {} if isinstance(metadata, dict) else {}
    expected = automation.get("filesystem_id") if isinstance(automation, dict) else None
    if not expected:
        return None
    marker = _read_metadata(str(Path(project) / ".raven" / "filesystem.json"))
    if (marker.get("schema") != "raven.project-filesystem/v1" or marker.get("id") != expected
            or marker.get("origin_host") != automation.get("filesystem_host")
            or automation.get("project_root") != project
            or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{32}", expected)):
        raise ExportError("Shared project filesystem marker does not match this exact recorded execution")
    _text(marker["origin_host"], "shared project origin host", 255)
    return marker


def normalize_request(request):
    if not isinstance(request, dict):
        raise ExportError("Export request must be an object")
    # Also freezes caller-owned nested dictionaries and rejects non-JSON values.
    try:
        request = _decode(_json(request))
    except (ValueError, TypeError) as exc:
        raise ExportError(f"Request must contain finite JSON values: {exc}") from exc
    unknown = set(request) - {"schema", "project", "runs", "attempts", "include", "metrics", "devices", "reports", "start", "end", "layout", "destination", "preset", "range_policy", "sources", "all_available", "explicit_include"}
    if unknown:
        raise ExportError("Unknown export request fields: " + ", ".join(sorted(unknown)))
    subjects = request.get("attempts")
    if subjects is None:
        runs = request.get("runs", [])
        if not isinstance(runs, list):
            raise ExportError("runs must contain exact run IDs")
        subjects = [{"project": request.get("project"), "run_id": run} for run in runs]
    if not isinstance(subjects, list) or not 1 <= len(subjects) <= MAX_ATTEMPTS:
        raise ExportError(f"Select 1–{MAX_ATTEMPTS} exact run attempts")
    normalized, seen = [], set()
    for subject in subjects:
        if not isinstance(subject, dict):
            raise ExportError("Each attempt requires project and run_id")
        root = _path(subject.get("project", ""))
        run_id = _text(subject.get("run_id"), "run ID", 128)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
            raise ExportError("Run ID must name one exact safe attempt directory")
        host = subject.get("host", local_host())
        _text(host, "source filesystem host", 255)
        anchor = _shared_anchor(root, run_id)
        if not _is_local(host) and (not anchor or host != anchor["origin_host"]):
            raise ExportError(f"Source filesystem {host} requires a remote export adapter. Run raven export-data on {host}; no local fallback is used")
        origin = anchor["origin_host"] if anchor else local_host()
        key = (root, run_id, origin)
        if key not in seen:
            seen.add(key)
            normalized.append(dict(project=root, run_id=run_id, host=origin))
    include = _selection(request.get("include", list(DEFAULT_FAMILIES)), "families", 16)
    all_available = bool(request.get("all_available")) or "all" in include
    explicit_include = _selection(request.get("explicit_include", [x for x in include if x != "all"] if "all" in include else []), "explicit families", 16)
    if "all" in include:
        include = list(dict.fromkeys([*DEFAULT_FAMILIES, *(x for x in include if x != "all")]))
    if not include or set(include) - set(FAMILIES) or set(explicit_include) - set(FAMILIES):
        raise ExportError("Select one or more families: " + ", ".join(FAMILIES) + "; all excludes logs/artifacts")
    destination = request.get("destination") or {"mode": "project"}
    if not isinstance(destination, dict) or set(destination) - {"mode", "path", "host", "combined"}:
        raise ExportError("Destination requires mode, optional path, host, and combined")
    mode = destination.get("mode", "project")
    if mode not in {"project", "relative", "absolute"}:
        raise ExportError("Destination mode must be project, relative, or absolute")
    host = destination.get("host", local_host()) or local_host()
    _text(host, "destination filesystem host", 255)
    path = "exports" if mode == "project" else _path(destination.get("path", ""), absolute=mode == "absolute")
    anchors = [_shared_anchor(subject["project"], subject["run_id"]) for subject in normalized]
    within_projects = mode != "absolute" or all(path == subject["project"] or Path(subject["project"]) in Path(path).parents for subject in normalized)
    shared_destination = within_projects and all(anchor and anchor["origin_host"] == anchors[0]["origin_host"] for anchor in anchors)
    if not _is_local(host) and not (shared_destination and host == anchors[0]["origin_host"]):
        raise ExportError(f"Destination filesystem {host} has no verified export write adapter; run the export command on {host}")
    resolved_host = anchors[0]["origin_host"] if shared_destination else local_host()
    if destination.get("combined") and mode != "absolute":
        raise ExportError("A combined multi-project bundle requires an explicit absolute destination")
    layout = request.get("layout", "category")
    if layout not in {"category", "metric"}:
        raise ExportError("Layout must be category or metric")
    for bound in ("start", "end"):
        value = request.get(bound)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ExportError(f"{bound} must be a finite epoch timestamp")
    if request.get("start") is not None and request.get("end") is not None and request["start"] > request["end"]:
        raise ExportError("Time range starts after it ends")
    return dict(schema="raven.export-request/v1", attempts=normalized, include=include,
                all_available=all_available, explicit_include=explicit_include,
                metrics=_selection(request.get("metrics"), "metrics"), devices=_selection(request.get("devices"), "devices"),
                reports=_selection(request.get("reports"), "reports"), sources=_selection(request.get("sources"), "sources", MAX_SOURCES), start=request.get("start"), end=request.get("end"),
                layout=layout, destination=dict(mode=mode, path=path, host=resolved_host, combined=bool(destination.get("combined"))),
                preset=request.get("preset"), range_policy=request.get("range_policy", "full-recorded-run"))


def _identity(root, run_id, manifest):
    scheduler = manifest.get("scheduler") or {}
    metadata = manifest.get("metadata") or {}
    if not isinstance(scheduler, dict):
        scheduler = {}
    if not isinstance(metadata, dict):
        metadata = {}
    launch = metadata.get("raven_launch") or {}
    if isinstance(launch, dict):
        scheduler = dict(scheduler, **{k: v for k, v in launch.items() if k in {"cluster", "array_parent", "array_task"}})
    automation = metadata.get("automation") or {}
    plan = automation.get("plan") or {} if isinstance(automation, dict) else {}
    provenance = manifest.get("provenance") or {}
    identity = {"project": root, "project_id": manifest.get("project_id", ""), "host": automation.get("filesystem_host", local_host()) if automation.get("filesystem_id") else local_host(), "run_id": run_id,
                "attempt": manifest.get("attempt", 1), "cluster": manifest.get("cluster", scheduler.get("cluster", metadata.get("cluster", ""))),
                "job_id": manifest.get("job_id", ""), "array_parent": manifest.get("array_parent", launch.get("array_id", scheduler.get("array_parent", ""))),
                "array_task": manifest.get("array_task", launch.get("array_index", scheduler.get("array_task", ""))),
                "submission_token": launch.get("submission_token", plan.get("token", "")),
                "submit_time": manifest.get("submit"), "script_sha256": provenance.get("script_sha256", ""),
                "filesystem_id": automation.get("filesystem_id", "")}
    return identity


def _discover_attempt(subject):
    root, run_id = subject["project"], subject["run_id"]
    fd = _dir(root)
    os.close(fd)
    run = str(Path(root) / "runs" / run_id)
    manifest = _read_metadata(str(Path(run) / "run.json"))
    if manifest.get("schema") != "raven.run/v1" or manifest.get("run_id") != run_id:
        raise ExportError(f"Run manifest does not match exact selected attempt {run_id}")
    if type(manifest.get("attempt", 1)) is not int or not 1 <= manifest.get("attempt", 1) <= 2147483647:
        raise ExportError("Run attempt number must be a positive bounded integer")
    paths = manifest.get("paths") or {}
    if not isinstance(paths, dict):
        raise ExportError("Run paths must be an object")
    for field in ("metadata", "provenance", "scheduler"):
        if field in manifest and not isinstance(manifest[field], dict):
            raise ExportError("Run " + field + " must be an object")
    for field in ("automation", "raven_launch"):
        if field in manifest.get("metadata", {}) and not isinstance(manifest["metadata"][field], dict):
            raise ExportError("Run metadata." + field + " must be an object")
    identity = _identity(root, run_id, manifest)
    key = hashlib.sha256(_json(identity)).hexdigest()[:20]
    result = dict(subject, key=key, run_dir=run, name=manifest.get("name", run_id), job_id=identity["job_id"],
                  attempt=identity["attempt"], state=manifest.get("state", "UNKNOWN"), identity=identity,
                  provenance=manifest.get("provenance", {}), sources=[], issues=[])
    metadata = manifest.get("metadata") or {}
    automation = metadata.get("automation") or {} if isinstance(metadata, dict) else {}
    anchor = _shared_anchor(root, run_id)
    result["filesystem_anchor"] = anchor
    result["access_host_at_capture"] = local_host()
    if automation.get("filesystem_host") and not _is_local(automation["filesystem_host"]) and not anchor:
        raise ExportError("The recorded project belongs to filesystem host " + str(automation["filesystem_host"]) + "; run export there or configure an explicit verified remap")
    if automation.get("project_root") and automation["project_root"] != root:
        raise ExportError("The selected project differs from the recorded execution project; an explicit verified remap is required")
    result["application_state"] = manifest.get("state", "UNKNOWN")
    result["active_at_capture"] = manifest.get("state") in {"RUNNING", "PENDING", "STARTING"}
    result["scheduler_state"] = None
    result["metadata"] = metadata
    sources = result["sources"]
    def add(family, ident, relative, **options):
        try:
            sources.append(_source(family, ident, _under(run, relative), relative_path=relative, **options))
        except (ValueError, OSError) as exc:
            result["issues"].append(_issue("invalid-source", exc, family=family, source=str(relative)))
    add("summary", "run", "run.json", schema="raven.run/v1")
    add("application", "application", paths.get("metrics", "metrics.jsonl"), schema="raven.metrics/v1", append=True)
    add("summary", "summary", paths.get("summary", "summary.json"), schema="raven.summary/v1")
    binding = automation.get("metric_binding") or {}
    if binding:
        bound_path = _path(binding.get("path", ""))
        if Path(root) not in Path(bound_path).parents:
            raise ExportError("Declared application metric source must belong to the execution project")
        sources[1] = _source("application", "application", bound_path, schema=binding.get("format", "raven.metrics/v1"), append=True)
    scheduler_path = str(Path(run) / "scheduler-outcome.json")
    try:
        scheduler_outcome = _read_metadata(scheduler_path)
    except FileNotFoundError:
        pass
    else:
        observed_submit, recorded_submit = scheduler_outcome.get("submit_time"), manifest.get("submit")
        if (scheduler_outcome.get("schema") != "raven.scheduler-outcome/v1"
                or any(scheduler_outcome.get(k) != identity[k] for k in ("job_id", "cluster", "attempt"))
                or not _number(observed_submit) or not _number(recorded_submit)
                or abs(observed_submit - recorded_submit) >= 1):
            raise ExportError("Scheduler outcome does not match this exact attempt and submission time")
        result["scheduler_state"] = scheduler_outcome.get("state")
        sources.append(_source("summary", "scheduler-outcome", scheduler_path, schema="raven.scheduler-outcome/v1"))
    for view in ("planning", "predict", "forecast", "blockers", "tradeoffs", "scaling", "workflow", "submit", "array_manifest", "evidence", "operations", "passport"):
        if paths.get(view):
            add("research", view, paths[view])
    # Named telemetry/trace indexes are metadata, not directory discovery.
    for index_name, index_family in (("telemetry/index.json", None), ("trace/index.json", "trace")):
        try:
            index = _read_metadata(_under(run, index_name))
        except FileNotFoundError:
            continue
        except (ValueError, OSError) as exc:
            result["issues"].append(_issue("invalid-index", exc, source=index_name))
            continue
        expected_index = "raven.trace-index/v1" if index_family else "raven.telemetry-index/v1"
        if index.get("schema") != expected_index:
            result["issues"].append(_issue("invalid-index", "Unsupported retained source index schema", source=index_name, family=index_family or "gpu"))
            continue
        descriptors = index.get("sources", [])
        if not isinstance(descriptors, list) or len(descriptors) > MAX_SOURCES:
            raise ExportError("Source index exceeds its descriptor budget")
        if index_family:
            add("trace", "index", index_name, schema=index.get("schema", ""))
        for number, source in enumerate(descriptors):
            if not isinstance(source, dict):
                raise ExportError("Source descriptor must be an object")
            family = index_family or source.get("family")
            if family not in {"gpu", "cpu-memory", "trace"}:
                continue
            previous = len(sources)
            add(family, source.get("id", str(number)), source.get("path", ""), schema=source.get("schema", ""),
                coverage=source.get("coverage", {}), append=family in COLUMNS or str(source.get("path", "")).endswith(".jsonl"),
                recording_status=source.get("status", "unknown"), recording_reason=source.get("reason", ""))
            if len(sources) > previous:
                sources[-1]["metrics"] = _selection(source.get("metrics", list(OBSERVATIONS.get(family, ()))), "source metric names")
                devices = source.get("devices", [])
                if not isinstance(devices, list) or len(devices) > MAX_METRICS:
                    raise ExportError("Source device descriptors exceed their metadata budget")
                sources[-1]["devices"] = _selection([device_id(item) if isinstance(item, dict) else item for item in devices], "source device identifiers")
    try:
        logs = _read_metadata(_under(run, paths.get("log_index", "logs.json")))
        if logs.get("run_id", run_id) != run_id or logs.get("job_id", identity["job_id"]) != identity["job_id"]:
            raise ExportError("Log index belongs to a different attempt")
        entries = logs.get("logs", [])
        if not isinstance(entries, list) or len(entries) > MAX_SOURCES:
            raise ExportError("Log index exceeds descriptor budget")
        for entry in entries:
            path = _text(entry.get("path"), "declared log path")
            absolute = _path(path) if path.startswith("/") else _under(run, path)
            sources.append(_source("logs", entry.get("id", Path(path).name), absolute, append=True))
    except FileNotFoundError:
        pass
    except (ValueError, OSError) as exc:
        result["issues"].append(_issue("invalid-log-index", exc, family="logs"))
    if paths.get("contract"):
        try:
            contract = _read_metadata(_under(run, paths["contract"]))
            from .artifacts import _contract
            for entry in _contract(contract):
                add("artifacts", entry["path"], entry["path"], required=entry.get("required", True))
        except (ValueError, OSError) as exc:
            result["issues"].append(_issue("invalid-artifact-contract", exc, family="artifacts"))
    bindings = automation.get("research_bindings", [])
    if not isinstance(bindings, list) or len(bindings) > MAX_SOURCES:
        raise ExportError("Declared research bindings exceed the source metadata budget")
    for entry in bindings:
        if not isinstance(entry, dict):
            raise ExportError("Declared research binding must be an object")
        path = _text(entry.get("path"), "declared research source path")
        if path.startswith("/"):
            path = _path(path)
            if Path(root) not in Path(path).parents:
                raise ExportError("Declared research source must belong to the execution project")
        else:
            path = _under(run, path)
        sources.append(_source("research", _text(entry.get("id"), "research source ID", 128), path, schema=entry.get("schema", "")))
    for entry in automation.get("artifact_bindings", []):
        path = _path(entry.get("path", ""))
        if Path(root) not in Path(path).parents:
            raise ExportError("Declared artifact must belong to the execution project")
        sources.append(_source("artifacts", str(Path(path).relative_to(root)), path, required=entry.get("required", True)))
    if len(sources) > MAX_SOURCES:
        raise ExportError("Selected attempt exceeds source budget")
    _trace_watermarks(sources, identity, result["issues"])
    for source in sources:
        if source["family"] == "application" and source["available"]:
            source["metrics"] = _metric_hints(source)
            source["metrics_complete"] = False
    return result


def _trace_watermarks(sources, identity, issues):
    """Native committed byte watermarks take precedence over journal file size."""
    by_path = {source["path"]: source for source in sources if source["family"] == "trace"}
    for path, source in by_path.items():
        if not source["available"] or Path(path).name != "manifest.json":
            continue
        try:
            manifest = _read_metadata(path)
            if manifest.get("schema") != "raven.trace/v1":
                continue
            trace_identity = manifest.get("identity")
            if not isinstance(trace_identity, dict):
                raise ExportError("Native trace must declare exact run/attempt identity")
            if trace_identity.get("run_id", trace_identity.get("attempt")) != identity["run_id"]:
                raise ExportError("Native trace belongs to a different run attempt")
            if identity["job_id"] and trace_identity.get("job_id") != identity["job_id"]:
                raise ExportError("Native trace belongs to a different scheduler job")
            if manifest.get("journal") != "events.jsonl":
                raise ExportError("Native trace requires its declared sibling events.jsonl journal")
            journal = by_path.get(str(Path(path).parent / "events.jsonl"))
            if journal is None:
                raise ExportError("Native trace journal was not explicitly declared")
            cutoff, sequence = manifest.get("committed_bytes"), manifest.get("committed_seq")
            if type(cutoff) is not int or cutoff < 0 or type(sequence) is not int or sequence < 0:
                raise ExportError("Native trace committed byte/sequence watermark is invalid")
            if not journal["available"] or cutoff > journal["watermark"]["size"]:
                raise ExportError("Native trace journal is shorter than its committed watermark")
            fd = _open(journal["path"])
            try:
                journal["observed_size"] = journal["watermark"]["size"]
                journal["watermark"]["size"] = cutoff
                journal["endpoint_sha256"] = _ends(fd, cutoff)
            finally:
                os.close(fd)
            journal["committed_seq"] = sequence
            journal["coverage"] = manifest.get("coverage", {})
            if journal["observed_size"] > cutoff:
                _add_issue(issues, "uncommitted-trace-tail", "Native trace bytes beyond the committed watermark are excluded", family="trace", source=journal["path"], bytes=journal["observed_size"] - cutoff)
            if manifest.get("state") != "CLOSED" or manifest.get("dropped", 0):
                source.update(recording_status="partial", recording_reason="Native trace is open, incomplete, or reports dropped events")
                journal.update(recording_status="partial", recording_reason=source["recording_reason"])
        except (ValueError, OSError) as exc:
            source.update(available=False, reason=str(exc))
            journal_path = str(Path(path).parent / "events.jsonl")
            if journal_path in by_path:
                by_path[journal_path].update(available=False, reason=str(exc))
            _add_issue(issues, "invalid-trace", exc, family="trace", source=path)


def _metric_hints(source):
    """Two bounded windows, deliberately incomplete names, never a stream scan."""
    fd = _open(source["path"])
    names = set()
    try:
        size = source["watermark"]["size"]
        windows = [(0, min(CHUNK, size))]
        if size > CHUNK:
            windows.append((max(CHUNK, size - CHUNK), min(CHUNK, size - CHUNK)))
        for offset, length in windows:
            raw = os.pread(fd, length, offset)
            lines = raw.split(b"\n")
            if offset:
                lines = lines[1:]
            for line in lines[:-1]:
                try:
                    row = _decode(line)
                    metrics = row.get("metrics", {}) if isinstance(row, dict) else {}
                    if isinstance(metrics, dict):
                        for name in metrics:
                            if isinstance(name, str) and name and len(name) <= 512 and name.isprintable():
                                names.add(name)
                                if len(names) >= MAX_METRICS:
                                    return sorted(names)
                except (ValueError, TypeError):
                    pass
    finally:
        os.close(fd)
    return sorted(names)


def discover(request):
    """Read only selected attempt metadata and bounded source endpoints."""
    request = normalize_request(request)
    attempts = [_discover_attempt(subject) for subject in request["attempts"]]
    if sum(len(attempt["sources"]) for attempt in attempts) > MAX_SOURCES:
        raise ExportError(f"Selected attempts exceed {MAX_SOURCES} sources")
    availability = {family: dict(available=False, sources=0, reasons=[], metrics=[], devices=[], metrics_complete=False) for family in FAMILIES}
    issues = []
    for attempt in attempts:
        issues.extend(attempt["issues"])
        for source in attempt["sources"]:
            entry = availability[source["family"]]
            if source["available"]:
                entry["available"] = True
                entry["sources"] += 1
            elif source["reason"] not in entry["reasons"]:
                entry["reasons"].append(source["reason"])
            for field in ("metrics", "devices"):
                entry[field] = list(dict.fromkeys(entry[field] + [str(x) for x in source.get(field, [])]))[:MAX_METRICS]
    for family, entry in availability.items():
        if not entry["available"] and not entry["reasons"]:
            entry["reasons"].append("No retained source was declared for this attempt")
    return dict(schema="raven.export-discovery/v1", attempts=attempts, availability=availability, issues=issues)


def _safe_name(value, maximum=60):
    raw = re.sub(r"[^\w.-]+", "-", str(value), flags=re.UNICODE).strip(".-") or "run"
    encoded = raw.encode("utf-8")[:maximum]
    return encoded.decode("utf-8", errors="ignore") or "run"


def _attempt_name(attempt):
    ident = attempt["identity"]
    job = "job-" + _safe_name(ident["job_id"], 40) if ident["job_id"] else "local"
    return f"{_safe_name(attempt['name'])}__{job}__attempt-{ident['attempt']}__{attempt['key'][:10]}"


def _inspect_destination(path):
    """Read-only: reject existing unsafe ancestors, tolerate missing suffix."""
    current = Path("/")
    for part in Path(path).parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode):
            raise ExportError(f"Destination ancestor is not a directory or is a symlink: {current}")


def _plan_digest(plan):
    return hashlib.sha256(_json({key: value for key, value in plan.items() if key != "plan_digest"})).hexdigest()


def plan_export(request):
    request = normalize_request(request)
    found = discover(request)
    if request["all_available"]:
        request["include"] = list(dict.fromkeys([family for family in DEFAULT_FAMILIES if found["availability"][family]["available"]] + request["explicit_include"]))
    created = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
    export_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:16]
    attempts = found["attempts"]
    destination = request["destination"]
    groups = OrderedDict()
    for attempt in attempts:
        group = "combined" if destination["combined"] else attempt["project"]
        groups.setdefault(group, []).append(attempt)
    destinations = []
    for group, members in groups.items():
        parent = destination["path"] if destination["mode"] == "absolute" else _under(members[0]["project"], destination["path"])
        prefix = _attempt_name(members[0]) if len(members) == 1 else f"bundle-{len(members)}-attempts-{hashlib.sha256(group.encode()).hexdigest()[:10]}"
        path = str(Path(parent) / prefix / export_id)
        _path(path)
        _inspect_destination(str(Path(path).parent))
        destinations.append(dict(project=group, host=destination["host"], path=path, attempts=[x["run_id"] for x in members], attempt_keys=[x["key"] for x in members]))
    issues = [issue for issue in found["issues"] if not issue.get("family") or issue["family"] in request["include"]]
    for family in request["include"]:
        if not found["availability"][family]["available"]:
            _add_issue(issues, "unavailable-family", "; ".join(found["availability"][family]["reasons"]), family=family)
    available = any(found["availability"][family]["available"] for family in request["include"])
    plan = dict(schema="raven.export-plan/v1", export_id=export_id, created_at=created, request=request,
                attempts=attempts, destinations=destinations, issues=issues, valid=available,
                availability=found["availability"])
    plan["plan_digest"] = _plan_digest(plan)
    if len(_json(plan)) > 4 * MAX_METADATA:
        raise ExportError("Export plan exceeds 4 MiB; narrow the selected attempts/sources")
    return plan


def _cancelled(cancel):
    return bool(cancel and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()))


def _check_cancel(cancel):
    if _cancelled(cancel):
        raise ExportCancelled("Export cancelled; original measurements were not changed")


class _Writer:
    """Streaming hashes and an LRU descriptor cache, bounded by output count."""
    def __init__(self, stage, cancel, progress):
        self.stage, self.cancel, self.progress = stage, cancel, progress
        self.open = OrderedDict()
        self.files = OrderedDict()
        self.dirty = set()
        self.records = self.bytes = 0
        self.last_progress = 0.0

    def _handle(self, name):
        if name in self.open:
            self.open.move_to_end(name)
            return self.open[name]
        if len(self.open) >= MAX_OPEN_FILES:
            _, stream = self.open.popitem(last=False)
            # Closing flushes buffered bytes; sync each file once at publication,
            # not on every LRU eviction when metrics outnumber descriptors.
            stream.close()
        target = Path(self.stage) / name
        parent = _dir(str(target.parent), create=True)
        try:
            flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            flags |= os.O_EXCL | os.O_CREAT if name not in self.files else 0
            fd = os.open(target.name, flags, 0o600, dir_fd=parent)
        finally:
            os.close(parent)
        stream = os.fdopen(fd, "ab", buffering=CHUNK)
        self.open[name] = stream
        if name not in self.files:
            if len(self.files) >= 8192:
                stream.close()
                self.open.pop(name)
                raise ExportError("Export exceeds 8192 output files; narrow the selection")
            self.files[name] = dict(path=name, bytes=0, records=0, _hash=hashlib.sha256())
        return stream

    def write(self, name, raw, *, records=0, family=None, columns=None):
        _check_cancel(self.cancel)
        stream = self._handle(name)
        stream.write(raw)
        self.dirty.add(name)
        record = self.files[name]
        record["_hash"].update(raw)
        record["bytes"] += len(raw)
        record["records"] += records
        if family:
            record["family"] = family
        if columns:
            record["columns"] = list(columns)
        self.bytes += len(raw)
        self.records += records
        now = time.monotonic()
        if self.progress and now - self.last_progress >= .1:
            self.progress(dict(bytes=self.bytes, records=self.records, files=len(self.files)))
            self.last_progress = now

    def csv(self, name, columns, row, family):
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\n")
        if name not in self.files:
            writer.writerow(columns)
        writer.writerow([_cell(row.get(key)) for key in columns])
        self.write(name, buffer.getvalue().encode("utf-8"), records=1, family=family, columns=columns)

    def json(self, name, value, **options):
        self.write(name, _json(value) + b"\n", **options)

    def close(self):
        error = None
        for stream in self.open.values():
            try:
                stream.close()  # flush all active buffers before syncing dirty files
            except OSError as exc:
                error = error or exc
        self.open.clear()
        if error:
            raise error
        # Iteration follows output creation order, so payloads and manifest are
        # durable before the completion marker. Only one extra fd is needed.
        for name in self.files:
            if name not in self.dirty:
                continue
            fd = _open(str(Path(self.stage) / name))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            self.dirty.remove(name)

    def manifest_files(self):
        return [{**{key: value for key, value in info.items() if key != "_hash"}, "sha256": info["_hash"].hexdigest()} for info in self.files.values()]


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return _json(value).decode("utf-8")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Nonfinite measurement")
    return value


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and (not isinstance(value, float) or math.isfinite(value))


def _row_time(row):
    timestamp = row.get("timestamp", row.get("t"))
    if _number(timestamp):
        return timestamp
    text = row.get("timestamp_utc")
    if isinstance(text, str):
        try:
            value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if value.tzinfo is not None:
                return value.timestamp()
        except ValueError:
            pass
    return None


def _clock(row):
    result = {key: row.get(key) for key in CLOCK_COLUMNS}
    if "t" in row and "timestamp" not in row:
        result["timestamp"] = row["t"]
        result["clock"] = row.get("clock", "unix-epoch-seconds UTC")
        result["source_time"] = row.get("source_time", row["t"])
    return result


def _source_open(source):
    fd = _open(source["path"])
    try:
        info = os.fstat(fd)
        mark = source["watermark"]
        if info.st_ino != mark["inode"] or info.st_dev != mark["device"] or info.st_size < mark["size"]:
            raise ExportError("Source was replaced, rotated, or truncated after the preview")
        if not source.get("append") and _signature(info) != mark:
            raise ExportError("Structured source changed after the preview; refresh the export plan")
        if info.st_size == mark["size"] and (info.st_mtime_ns != mark["mtime_ns"] or info.st_ctime_ns != mark["ctime_ns"]):
            raise ExportError("Source was modified after the preview")
        if _ends(fd, mark["size"]) != source["endpoint_sha256"]:
            raise ExportError("Source content changed before its captured watermark")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _source_finish(fd, source):
    info, mark = os.fstat(fd), source["watermark"]
    if info.st_size < mark["size"] or _ends(fd, mark["size"]) != source["endpoint_sha256"]:
        raise ExportError("Source content changed while streaming; no bundle was published")
    if not source.get("append") and _signature(info) != mark:
        raise ExportError("Structured source changed while streaming; no bundle was published")
    if info.st_size == mark["size"] and (info.st_mtime_ns != mark["mtime_ns"] or info.st_ctime_ns != mark["ctime_ns"]):
        raise ExportError("Source was modified while streaming; no bundle was published")
    # Verify the original pathname still names the same retained object.
    named_fd = _open(source["path"])
    try:
        named = os.fstat(named_fd)
        if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
            raise ExportError("Source pathname rotated while streaming; refresh the export plan")
    finally:
        os.close(named_fd)


def _lines(fd, cutoff, cancel, counters):
    remaining = cutoff
    with os.fdopen(os.dup(fd), "rb", buffering=CHUNK) as stream:
        while remaining:
            _check_cancel(cancel)
            raw = stream.readline(min(MAX_LINE + 1, remaining))
            if not raw:
                raise ExportError("Source was truncated while exporting")
            remaining -= len(raw)
            if len(raw) > MAX_LINE:
                counters["oversized"] += 1
                while raw and not raw.endswith(b"\n") and remaining:
                    _check_cancel(cancel)
                    raw = stream.readline(min(MAX_LINE + 1, remaining))
                    remaining -= len(raw)
                continue
            if not raw.endswith(b"\n"):
                counters["incomplete_tail"] += 1
                continue
            if raw.strip():
                yield raw


def _metric_filename(metric):
    return _safe_name(metric, 70) + "--" + hashlib.sha256(metric.encode("utf-8")).hexdigest()[:16] + ".csv"


def _export_stream(writer, source, attempt, request, prefix, issues):
    family = source["family"]
    counters = dict(records=0, emitted=0, malformed=0, oversized=0, incomplete_tail=0, unknown_time=0, filtered=0)
    wanted = set(request["metrics"])
    device_filter = set(request["devices"])
    metric_names = set()
    observed_devices = set()
    fd = _source_open(source)
    try:
        for raw in _lines(fd, source["watermark"]["size"], writer.cancel, counters):
            counters["records"] += 1
            try:
                row = _decode(raw)
                if not isinstance(row, dict):
                    raise ValueError("measurement row is not an object")
                # Exact identity is immutable even when supplied redundantly by a producer.
                for key in ("run_id", "job_id"):
                    if key in row and row[key] != attempt["identity"].get(key):
                        raise ValueError("row belongs to a different exact attempt")
                if request["start"] is not None or request["end"] is not None:
                    timestamp = _row_time(row)
                    if timestamp is None:
                        counters["unknown_time"] += 1
                        continue
                    if (request["start"] is not None and timestamp < request["start"]) or (request["end"] is not None and timestamp > request["end"]):
                        counters["filtered"] += 1
                        continue
                base = dict(attempt["identity"], **_clock(row))
                if family == "application":
                    metrics = row.get("metrics")
                    if not isinstance(metrics, dict) or len(metrics) > MAX_METRICS:
                        raise ValueError("metrics must be a bounded object")
                    observations = []
                    for name, measurement in metrics.items():
                        _text(name, "metric name", 512)
                        if wanted and name not in wanted and "application." + name not in wanted:
                            continue
                        details = measurement if isinstance(measurement, dict) else {"value": measurement}
                        value = details.get("value")
                        if value is not None and not _number(value):
                            raise ValueError("metric value must be finite numeric or null")
                        units = row.get("units") or {}
                        if not isinstance(units, dict):
                            raise ValueError("units must be an object")
                        observation = dict(base, **{key: row.get(key) for key in ("phase", "step", "rank", "node", "source", "scope", "quality", "status", "reason")})
                        observation.update(metric=name, value=value, unit=details.get("unit") or units.get(name) or "unspecified")
                        observation["source"] = observation["source"] or source["path"]
                        observation["scope"] = observation["scope"] or "application-reported"
                        observation["status"] = details.get("status", observation["status"] or ("unavailable" if value is None else "observed"))
                        observation["reason"] = details.get("reason", observation["reason"])
                        observations.append((name, observation))
                    for name, observation in observations:
                        metric_names.add(name)
                        if len(metric_names) > MAX_METRICS:
                            raise ExportError(f"Source exceeds {MAX_METRICS} distinct selected metric names")
                        filename = "application-metrics.csv" if request["layout"] == "category" else "application/" + _metric_filename(name)
                        writer.csv(prefix + filename, APPLICATION_COLUMNS, observation, family)
                        counters["emitted"] += 1
                else:
                    if device_filter and family == "gpu":
                        identifiers = {str(row.get(key, "")) for key in ("device_uuid", "device_index", "device_bdf", "partition")}
                        identifiers.add(device_id(row))
                        matched = device_filter.intersection(identifiers)
                        if not matched:
                            counters["filtered"] += 1
                            continue
                        observed_devices.update(matched)
                    observation = dict(base, **{key: row.get(key) for key in COLUMNS[family] if key not in base})
                    observation["source"] = observation.get("source") or source["path"]
                    for name in OBSERVATIONS[family]:
                        if observation.get(name) is not None and not _number(observation[name]):
                            raise ValueError("telemetry counter must be finite numeric or null")
                    selected = [name for name in OBSERVATIONS[family] if name in wanted or family + "." + name in wanted]
                    if not selected and any(name.startswith(family + ".") for name in wanted):
                        counters["filtered"] += 1
                        continue
                    selected = selected or list(OBSERVATIONS[family])
                    if request["layout"] == "metric":
                        metadata = tuple(key for key in COLUMNS[family] if key not in OBSERVATIONS[family])
                        for name in selected:
                            writer.csv(prefix + family + "/" + _metric_filename(name), metadata + (name,), observation, family)
                            counters["emitted"] += 1
                    else:
                        columns = tuple(key for key in COLUMNS[family] if key not in OBSERVATIONS[family] or key in selected)
                        writer.csv(prefix + family + ".csv", columns, observation, family)
                        counters["emitted"] += 1
            except (ValueError, TypeError, OverflowError) as exc:
                if isinstance(exc, ExportError) and "distinct selected metric" in str(exc):
                    raise
                counters["malformed"] += 1
        _source_finish(fd, source)
    finally:
        os.close(fd)
    for kind in ("malformed", "oversized", "incomplete_tail", "unknown_time"):
        if counters[kind]:
            _add_issue(issues, kind, f"{counters[kind]} {kind.replace('_', ' ')} record(s)", run_id=attempt["run_id"], source=source["path"], family=family, count=counters[kind])
    if family == "gpu":
        counters["matched_devices"] = sorted(observed_devices)
    if family in OBSERVATIONS:
        for missing in sorted(name for name in wanted if name.startswith(family + ".") and name[len(family) + 1:] not in OBSERVATIONS[family]):
            _add_issue(issues, "unavailable-metric", "Unknown retained measurement: " + missing, metric=missing, family=family, run_id=attempt["run_id"])
    if family == "application" and wanted:
        selected_names = {name[12:] if name.startswith("application.") else name for name in wanted if "." not in name or name.startswith("application.")}
        for missing in sorted(selected_names - metric_names):
            _add_issue(issues, "unavailable-metric", "Selected application metric has no observations in this capture: " + missing, metric=missing, family=family, run_id=attempt["run_id"])
    return counters


def _copy_source(writer, source, prefix, issues, attempt):
    family = source["family"]
    if family == "summary":
        filename = {"run": "run.json", "summary": "summary.json", "scheduler-outcome": "scheduler-outcome.json"}[source["id"]]
    elif family == "trace":
        relative = source.get("relative_path") or str(Path(source["path"]).relative_to(attempt["run_dir"]))
        filename = "trace/index.json" if source["id"] == "index" else "trace/" + _path(relative, absolute=False)
    else:
        suffix = Path(source["path"]).suffix[:12]
        filename = family + "/" + _safe_name(source["id"], 90) + "--" + hashlib.sha256(source["path"].encode()).hexdigest()[:12] + suffix
    fd = _source_open(source)
    copied_before = writer.bytes
    try:
        remaining = source["watermark"]["size"]
        if not remaining:
            writer.write(prefix + filename, b"", family=family)
        # Native JSON is bounded and validated; bytes remain verbatim.
        if family in {"summary", "research"} or source["path"].endswith(".json") and family == "trace":
            if remaining > MAX_METADATA:
                raise ExportError("Native structured report exceeds metadata export limit")
            raw = os.read(fd, remaining + 1)
            if len(raw) != remaining:
                raise ExportError("Native report changed while reading")
            decoded = _decode(raw)
            if not isinstance(decoded, dict):
                raise ExportError("Native structured source must be a JSON object")
            if source.get("schema") and decoded.get("schema") != source["schema"]:
                raise ExportError("Native structured source schema differs from its declaration")
            writer.write(prefix + filename, raw, family=family)
        else:
            if family == "trace" and source["path"].endswith(".jsonl") and remaining:
                tail = os.pread(fd, min(MAX_LINE + 1, remaining), max(0, remaining - MAX_LINE - 1))
                if not tail.endswith(b"\n"):
                    last = tail.rfind(b"\n")
                    drop = len(tail) - last - 1 if last >= 0 else len(tail)
                    remaining -= drop
                    _add_issue(issues, "incomplete_tail", "Native trace's incomplete final record omitted", family=family, source=source["path"], count=1)
            while remaining:
                _check_cancel(writer.cancel)
                raw = os.read(fd, min(CHUNK, remaining))
                if not raw:
                    raise ExportError("Source truncated while copying")
                remaining -= len(raw)
                writer.write(prefix + filename, raw, family=family)
        _source_finish(fd, source)
    finally:
        os.close(fd)
    return dict(records=None, emitted=None, copied_bytes=writer.bytes - copied_before)


README = """Raven retained-data export (raven.export-manifest/v1)

Read manifest.json first: it identifies exact projects, runs, attempts and source
byte watermarks. Each source is an independent bounded capture, not a simultaneous
transaction across producers. Appended data after its watermark is excluded.

CSV files are UTF-8 and use standard quoting, including Unicode and multiline
strings. Application measurements are long-form: each row is one metric value.
An empty numeric cell means unavailable, never zero. Unknown units are explicitly
'unspecified'. GPU memory is MiB; RSS is bytes; CPU time is seconds; utilization
is percent. Allocations, process-tree observations and device counters are distinct.
Device utilization is not exclusive per-process GPU accounting. The source/scope/
quality/status/reason columns preserve those distinctions. Missing clock values
stay missing. Source time and clock labels are preserved; no timezone is inferred
for a naive timestamp. Cross-node clocks may be skewed. Per-source order is kept.

Identity columns include project, host, run_id, attempt, cluster and exact job_id.
A local run has no fabricated scheduler ID. Native research predictions and trace
evidence are separate files and are not relabeled observed application metrics.
No smoothing, interpolation, averaging, resampling or unit conversion is applied.
Application outcome, scheduler outcome and recording/export coverage are separate.
A partial export may contain useful observations from a failed or still-running job.

The manifest's files list records byte counts and SHA-256 checksums for every
payload file (including this README). manifest.json and COMPLETE.json are the
publication metadata and do not checksum themselves. Consumers should require
COMPLETE.json and a matching manifest export_id before accepting a bundle.
Linux local publication uses renameat2(RENAME_NOREPLACE), followed by directory
fsync. Remote export is unsupported. NFS durability depends on the server and
mount; successful local syscall results do not establish remote server durability.

Ordinary Python CSV import (no third-party package required):
    import csv
    with open('application-metrics.csv', encoding='utf-8', newline='') as source:
        for row in csv.DictReader(source):
            if row['metric'] == 'loss' and row['value'] != '':
                print(row['timestamp'], float(row['value']))

Plotting with an optional matplotlib installation:
    # Collect only the selected series/range needed for your plot.
    # plt.plot(times, values); plt.xlabel('UTC epoch seconds'); plt.ylabel('loss')
"""


def _publish(parent_fd, stage_name, final_name):
    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise ExportError("This platform lacks atomic no-overwrite directory publication (renameat2); export is unsupported") from exc
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    if function(parent_fd, os.fsencode(stage_name), parent_fd, os.fsencode(final_name), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), final_name)
    os.fsync(parent_fd)


def _validate_plan(plan):
    if not isinstance(plan, dict) or plan.get("schema") != "raven.export-plan/v1" or plan.get("plan_digest") != _plan_digest(plan):
        raise ExportError("Export plan changed or has an unsupported version; refresh the preview")
    request = normalize_request(plan["request"])
    if request != plan["request"]:
        raise ExportError("Export plan request is not normalized")
    # Public plans are serializable, but never allow external source path injection.
    if len(plan.get("attempts", [])) != len(request["attempts"]):
        raise ExportError("Export plan attempt set changed")
    for attempt, subject in zip(plan["attempts"], request["attempts"]):
        if any(attempt.get(key) != subject[key] for key in ("project", "run_id", "host")):
            raise ExportError("Export plan exact attempt identity changed")
        for source in attempt.get("sources", []):
            _path(source["path"])
            if source.get("family") not in FAMILIES:
                raise ExportError("Unknown source family in export plan")
    return plan


def _existing_bundle(plan, destination, cancel=None):
    path = destination["path"]
    try:
        manifest = _read_metadata(str(Path(path) / "manifest.json"), 8 * MAX_METADATA)
        marker = _read_metadata(str(Path(path) / "COMPLETE.json"))
    except FileNotFoundError:
        if Path(path).exists() or Path(path).is_symlink():
            raise ExportError("Destination already exists without this export's complete receipt")
        return None
    if (manifest.get("schema") != "raven.export-manifest/v1" or manifest.get("export_id") != plan["export_id"] or
            manifest.get("plan_digest") != plan["plan_digest"] or marker.get("export_id") != plan["export_id"] or
            marker.get("manifest_sha256") != hashlib.sha256(_json(manifest) + b"\n").hexdigest()):
        raise ExportError("Destination belongs to a different or incomplete export; it will not be overwritten")
    files = manifest.get("files", [])
    if not isinstance(files, list) or len(files) > 8192:
        raise ExportError("Existing export has an invalid file inventory")
    for entry in files:
        file_path = _under(path, entry["path"])
        fd = _open(file_path)
        digest, size = hashlib.sha256(), 0
        try:
            while True:
                _check_cancel(cancel)
                raw = os.read(fd, CHUNK)
                if not raw:
                    break
                digest.update(raw)
                size += len(raw)
        finally:
            os.close(fd)
        if size != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
            raise ExportError("Existing export payload differs from its immutable manifest")
    return manifest


def _receipt(plan, manifests, *, status=None, issues=None):
    collected = list(issues or [])
    for _, manifest in manifests:
        collected.extend(manifest.get("issues", []))
    status = status or ("partial" if collected or any(m.get("status") == "partial" for _, m in manifests) else "complete")
    codes = {"complete": 0, "partial": 3, "failed": 4, "cancelled": 130}
    return dict(schema="raven.export-receipt/v1", export_id=plan["export_id"], status=status,
                outputs=[path for path, _ in manifests], issues=collected[:MAX_ISSUES + 1],
                files=sum(len(manifest["files"]) + 2 for _, manifest in manifests), attempts=sum(len(manifest["attempts"]) for _, manifest in manifests), exit_code=codes[status])


def validate_existing(plan):
    """Recover an exactly matching published plan after a receipt-publication crash."""
    plan = _validate_plan(plan)
    manifests = []
    for destination in plan["destinations"]:
        manifest = _existing_bundle(plan, destination)
        if manifest is None:
            return None
        manifests.append((destination["path"], manifest))
    return _receipt(plan, manifests)


def _remove_tree(parent, name):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
    try:
        for entry in os.listdir(fd):
            info = os.stat(entry, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                _remove_tree(fd, entry)
            else:
                os.unlink(entry, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent)


def _export_bundle(plan, destination, cancel, progress):
    attempts = [attempt for attempt in plan["attempts"] if attempt["key"] in destination["attempt_keys"]]
    for attempt in attempts:
        current = _read_metadata(str(Path(attempt["run_dir"]) / "run.json"))
        if _identity(attempt["project"], attempt["run_id"], current) != attempt["identity"]:
            raise ExportError("Selected run identity changed after the export preview")
    parent_path = str(Path(destination["path"]).parent)
    parent = _dir(parent_path, create=True)
    stage_name = ".raven-stage-" + plan["export_id"] + "-" + uuid.uuid4().hex
    stage_path = str(Path(parent_path) / stage_name)
    writer = None
    stage_owned = False
    try:
        _check_cancel(cancel)
        os.mkdir(stage_name, 0o700, dir_fd=parent)
        stage_owned = True
        writer = _Writer(stage_path, cancel, progress)
        issues = list(plan["issues"])
        source_results, family_results = [], {}
        for attempt in attempts:
            prefix = "" if len(attempts) == 1 else "attempts/" + _attempt_name(attempt) + "/"
            seen_families = set()
            for source in attempt["sources"]:
                _check_cancel(cancel)
                family = source["family"]
                if family not in plan["request"]["include"]:
                    continue
                if family in {"research", "trace", "logs", "artifacts"} and plan["request"]["sources"] and source["id"] not in plan["request"]["sources"] and family + ":" + source["id"] not in plan["request"]["sources"]:
                    if not (family == "trace" and source["id"] == "index"):
                        continue
                if family == "research" and plan["request"]["reports"] and source["id"] not in plan["request"]["reports"]:
                    continue
                seen_families.add(family)
                record = {key: value for key, value in source.items() if key not in {"endpoint_sha256"}}
                record.update(run_id=attempt["run_id"], attempt_key=attempt["key"], status="complete")
                source_results.append(record)
                if not source["available"]:
                    _add_issue(issues, "unavailable-source", source["reason"], source=source["path"], family=family, run_id=attempt["run_id"])
                    record["status"] = "unavailable"
                    continue
                # Opening rejection leaves no misleading empty file and permits other families.
                try:
                    fd = _source_open(source)
                    os.close(fd)
                except (ValueError, OSError) as exc:
                    _add_issue(issues, "source-changed", exc, source=source["path"], family=family, run_id=attempt["run_id"])
                    record.update(status="partial", reason=str(exc))
                    continue
                before_issues = len(issues)
                before_bytes = writer.bytes
                try:
                    if family in COLUMNS:
                        record["counts"] = _export_stream(writer, source, attempt, plan["request"], prefix, issues)
                    else:
                        record["counts"] = _copy_source(writer, source, prefix, issues, attempt)
                except (ValueError, OSError) as exc:
                    if writer.bytes != before_bytes or isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT, errno.EIO}:
                        raise
                    _add_issue(issues, "invalid-source", exc, family=family, source=source["path"], run_id=attempt["run_id"])
                    record.update(status="partial", reason=str(exc))
                    continue
                coverage = source.get("coverage") or {}
                if source.get("recording_status") in {"partial", "failed", "unsupported", "unavailable", "recording"} or coverage.get("dropped") or coverage.get("missed_samples"):
                    _add_issue(issues, "partial-recording", source.get("recording_reason") or "Retained recording has incomplete or ongoing coverage", source=source["path"], family=family, run_id=attempt["run_id"])
                if len(issues) != before_issues:
                    record["status"] = "partial"
            if "gpu" in plan["request"]["include"] and plan["request"]["devices"]:
                matched_devices = set()
                for record in source_results:
                    if record["attempt_key"] == attempt["key"] and record["family"] == "gpu":
                        matched_devices.update(record.get("counts", {}).get("matched_devices", []))
                for missing in sorted(set(plan["request"]["devices"]) - matched_devices):
                    _add_issue(issues, "unavailable-device", "Selected device has no retained observations in this capture: " + missing, device=missing, family="gpu", run_id=attempt["run_id"])
            available_ids = {source["id"] for source in attempt["sources"] if source["family"] in plan["request"]["include"]}
            available_ids.update(source["family"] + ":" + source["id"] for source in attempt["sources"] if source["family"] in plan["request"]["include"])
            for missing in sorted(set(plan["request"]["sources"]) - available_ids):
                _add_issue(issues, "unavailable-selection", "Selected declared source was not found: " + missing, run_id=attempt["run_id"], source=missing)
            for missing in set(plan["request"]["include"]) - seen_families:
                _add_issue(issues, "unavailable-family", "No retained source declared for selected attempt", family=missing, run_id=attempt["run_id"])
        for family in plan["request"]["include"]:
            family_sources = [source for source in source_results if source["family"] == family]
            family_results[family] = dict(status="complete" if family_sources and all(source["status"] == "complete" for source in family_sources) and not any(issue.get("family") == family for issue in issues) else "partial", sources=len(family_sources))
        writer.write("README.txt", README.encode("utf-8"))
        writer.close()  # durable payloads before constructing publication metadata
        from . import __version__
        manifest = dict(schema="raven.export-manifest/v1", export_id=plan["export_id"], plan_digest=plan["plan_digest"],
                        created_at=plan["created_at"], raven_version=__version__, request=plan["request"],
                        attempts=[{key: value for key, value in attempt.items() if key not in {"sources", "issues"}} for attempt in attempts],
                        sources=source_results, files=writer.manifest_files(), families=family_results,
                        status="partial" if issues else "complete", issues=issues, transformations=[],
                        source_consistency="Per-source frozen byte watermarks; append-only contract with inode, size, metadata and endpoint validation; no global transaction",
                        publication="Linux renameat2 RENAME_NOREPLACE; local fsync; shared-filesystem server durability is site dependent",
                        column_contract="raven.export-columns/v1", units=UNITS, access_host_at_export=local_host(),
                        filesystem_host=destination["host"])
        encoded = _json(manifest) + b"\n"
        if len(encoded) > 8 * MAX_METADATA:
            raise ExportError("Export manifest exceeds the 8 MiB budget")
        writer.write("manifest.json", encoded)
        writer.json("COMPLETE.json", {"schema": "raven.export-complete/v1", "export_id": plan["export_id"], "manifest_sha256": hashlib.sha256(encoded).hexdigest()})
        writer.close()
        _check_cancel(cancel)
        stage_fd = _dir(stage_path)
        try:
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        named_parent = _dir(parent_path)
        try:
            pinned, named = os.fstat(parent), os.fstat(named_parent)
            if (pinned.st_dev, pinned.st_ino) != (named.st_dev, named.st_ino):
                raise ExportError("Destination directory moved or changed during export; no bundle was published")
        finally:
            os.close(named_parent)
        _publish(parent, stage_name, Path(destination["path"]).name)
        stage_owned = False
        return manifest
    finally:
        if writer is not None:
            try:
                writer.close()
            except OSError:
                # An earlier write/fsync failure still cleans exclusively owned staging.
                pass
        if stage_owned:
            # Stage is private, randomly named and belongs exclusively to this attempt.
            # Modern shutil.rmtree uses its symlink-safe fd implementation on POSIX.
            _remove_tree(parent, stage_name)
        os.close(parent)


def export_data(request_or_plan, cancel=None, progress=None):
    """Stream a frozen request/plan; recover owned publication, never overwrite.

    Invalid requests raise ExportError. Runtime failures return a structured
    receipt, preserving any already-published independent project bundles.
    """
    if not isinstance(request_or_plan, dict):
        raise ExportError("Export request or plan must be an object")
    plan = _validate_plan(request_or_plan) if request_or_plan.get("schema") == "raven.export-plan/v1" else plan_export(request_or_plan)
    if not plan["valid"]:
        raise ExportError("None of the selected sources is available; inspect plan availability and choose retained evidence")
    manifests = []
    try:
        for destination in plan["destinations"]:
            _check_cancel(cancel)
            existing = _existing_bundle(plan, destination, cancel)
            manifest = existing or _export_bundle(plan, destination, cancel, progress)
            manifests.append((destination["path"], manifest))
        return _receipt(plan, manifests)
    except ExportCancelled as exc:
        return _receipt(plan, manifests, status="cancelled", issues=[_issue("cancelled", exc)])
    except (OSError, ValueError, TypeError) as exc:
        return _receipt(plan, manifests, status="failed", issues=[_issue("export-failed", exc)])
