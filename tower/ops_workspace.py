"""Project onboarding and timestamp-aware investigation timelines.

All discovery is explicit and bounded. This adapter never crawls a project's
descendants, executes project code, or infers a scientific measurement.
"""
from __future__ import annotations

import math
import hashlib
import os
import re
from datetime import datetime

from . import operations as O


SPECIFICATIONS = [
    {"key": "project-guide", "title": "Connect and validate a research project", "group": "Workspace", "proposal": "F04",
     "summary": "Inspect the reporting contract, locate missing evidence, and prepare a minimal Bash integration.",
     "fields": [{"key": "root", "label": "Project directory", "required": True},
                {"key": "run_id", "label": "Sample run ID (required for a selected scheduler job)"},
                {"key": "script", "label": "Batch script relative to project (optional)"},
                {"key": "experiment", "label": "Experiment label (optional)"},
                {"key": "initialize", "label": "Prepare project descriptor", "default": "no", "choices": ["no", "yes"]}]},
    {"key": "investigation-timeline", "title": "Join investigation events", "group": "Workspace", "proposal": "F05",
     "summary": "Join declared log, runtime and scheduler events while retaining clock uncertainty and source identity.",
     "fields": [{"key": "path", "label": "Optional timeline manifest; blank collects the selected job"},
                {"key": "job_id", "label": "Exact job ID"},
                {"key": "root", "label": "Optional registered project root"}]},
]

REQUIRED_SOURCES = {
    "Experiment": ("metrics", "summary"), "Arrays": ("array_manifest",),
    "Logs": ("logs", "stdout", "stderr", "log_index"), "Artifacts": ("outputs", "contract"),
    "Passport": ("passports",), "Resources": ("predict", "planning"),
    "Forecast": ("forecast",), "Blockers": ("blockers",),
    "Tradeoffs": ("tradeoffs",), "Scaling": ("scaling",),
    "Workflow": ("workflow",), "Submit": ("submit",),
}


def _project(root, ctx, params=None):
    from .projects import discover_project
    from .project_validation import hooks, inspect_sample
    params = params or {}
    result = discover_project(root, files=ctx.files)
    coverage = []
    for run in result["runs"]:
        O.checkpoint(ctx)
        paths = run["inventory"].get("paths", {})
        coverage.append({"run_id": run["run_id"], "job_id": run.get("job_id"),
                         "declared": sorted(paths), "missing_views": [name for name, keys in REQUIRED_SOURCES.items()
                             if not any(paths.get(key) for key in keys)]})
    run_id = params.get("run_id", "")
    # Native inventories record reporter wall times, not the scheduler's exact
    # submit/start identity. Job ID, cluster and restart count alone cannot
    # distinguish a reused ID. Let an explicit run choose its sample instead
    # of associating an old attempt with the current scheduler selection.
    candidates = [run for run in result["runs"] if run["run_id"] == run_id] if run_id else []
    if not run_id and not ctx.selected and len(result["runs"]) == 1:
        candidates = result["runs"]
    if run_id and len(candidates) != 1:
        raise ValueError("Choose a sample run ID from the bounded project inventory; no other attempt is substituted.")
    selected = candidates[0] if len(candidates) == 1 else None
    sample = inspect_sample(result["root"], selected["run_id"], ctx) if selected else {
        "status": "not-selected", "checks": [], "warnings": [],
        "scope": "Choose an exact sample run ID; no attempt was selected automatically."}
    inventory = selected["inventory"] if selected else {}
    snippets = hooks(result["root"], script=params.get("script") or "tower-batch.sh",
                     experiment=params.get("experiment") or inventory.get("experiment_id") or "experiment")
    return dict(result, coverage=coverage, required_sources=REQUIRED_SOURCES, sample=sample, hooks=snippets,
                bash=snippets["batch"])


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 253402300799 or not math.isfinite(value):
        raise ValueError(name + " must be finite nonnegative seconds.")
    return float(value)


def timeline(value, selected="", cluster=""):
    if not isinstance(value, dict) or value.get("schema") != "tower.timeline/v1":
        raise ValueError("Expected tower.timeline/v1.")
    events = value.get("events")
    if not isinstance(events, list) or len(events) > 20000:
        raise ValueError("Timeline requires at most 20000 events.")
    seen, result = set(), []
    for record in events:
        if not isinstance(record, dict):
            raise ValueError("Timeline events must be objects.")
        for key in ("id", "source", "clock", "kind", "message", "job_id", "attempt"):
            item = record.get(key)
            if not isinstance(item, str) or not item or len(item) > (4096 if key == "message" else 512) or any(not c.isprintable() for c in item):
                raise ValueError("Timeline " + key + " must be bounded printable text.")
        namespace = record.get("cluster", value.get("cluster", "unspecified"))
        if not isinstance(namespace, str) or not namespace or len(namespace) > 512 or not namespace.isprintable():
            raise ValueError("Timeline cluster must be bounded printable text.")
        identity = (namespace, record["attempt"], record["source"], record["id"])
        if identity in seen:
            raise ValueError("Duplicate timeline event identity.")
        seen.add(identity)
        if "timestamp" not in record:
            raise ValueError("Timeline events must declare a timestamp or explicit null.")
        timestamp = record["timestamp"]
        if "ordinal" in record and (type(record["ordinal"]) is not int or not 1 <= record["ordinal"] <= 1000000000):
            raise ValueError("Source ordinal must be a positive bounded integer.")
        textual = record.get("timestamp_text", "")
        if not isinstance(textual, str) or len(textual) > 128 or any(not c.isprintable() for c in textual):
            raise ValueError("Textual event time must be bounded printable text.")
        if timestamp is None:
            if type(record.get("ordinal")) is not int or not 1 <= record["ordinal"] <= 1000000000:
                raise ValueError("Events without timestamps require a positive source ordinal.")
        else:
            timestamp = _finite(timestamp, "Event timestamp")
        uncertainty = _finite(record.get("uncertainty_s", 0), "Clock uncertainty")
        if selected and record["job_id"] != selected:
            continue
        result.append(dict(record, cluster=namespace, timestamp=timestamp, uncertainty_s=uncertainty))
    # Different clock domains have no asserted global order. Even a shared
    # clock's overlapping uncertainty intervals do not establish causality.
    result.sort(key=lambda item: (item["cluster"], item["clock"], item["timestamp"] is None,
                                 item["timestamp"] or 0, item["source"], item.get("ordinal", 0), item["id"]))
    domains = sorted({item["cluster"] + "/" + item["clock"] for item in result})
    attempts = sorted({item["attempt"] for item in result})
    clusters = sorted({item["cluster"] for item in result})
    if selected and len(attempts) > 1:
        raise ValueError("Timeline job ID spans multiple attempts; supply a single-attempt manifest.")
    if selected and len(clusters) > 1:
        raise ValueError("Timeline job ID spans multiple clusters; supply a single-cluster manifest.")
    if selected and cluster and any(name not in (cluster, "unspecified") for name in clusters):
        raise ValueError("Timeline cluster does not match the selected connection.")
    warnings = ["Timestamp order is not proof of causation; retain message/task dependency evidence."]
    if len(domains) > 1:
        warnings.append("Clock domains are displayed separately; no cross-domain order is inferred.")
    if "unspecified" in clusters:
        warnings.append("Cluster identity was not declared by this manifest; it is not automatically associated with a connection.")
    if any(item["timestamp"] is None for item in result):
        warnings.append("Unknown timestamps retain source order; textual local times have no inferred UTC offset.")
    rows = []
    for item in result:
        when = (f"{item['timestamp']:.6f} +/-{item['uncertainty_s']:g}s" if item["timestamp"] is not None else
                f"{item.get('timestamp_text') or 'time unknown'} [source item {item['ordinal']}]")
        rows.append(f"{item['cluster']}/{item['clock']} {when}  {item['kind']}  {item['source']}  {item['message']}")
    return {"events": result, "domains": domains, "clusters": clusters, "warnings": warnings, "rows": rows}


_NULL = {"", "unknown", "none", "n/a", "(null)", "unset"}
_WALL = re.compile(r"^\[?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")


def _wall(text):
    """Only offset-bearing ISO timestamps establish epoch seconds."""
    match = _WALL.match(str(text))
    if match is None:
        return None, ""
    raw = match[1]
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        seconds = stamp.timestamp() if stamp.utcoffset() is not None else None
        if seconds is not None:
            _finite(seconds, "Source timestamp")
        return seconds, raw
    except (ValueError, OverflowError, OSError):
        return None, raw


def _tail(ctx, path, maximum=65536):
    """Read a stable bounded tail through the selected connection only."""
    O.checkpoint(ctx)
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 4096 or not path.isprintable():
        raise ValueError("Scheduler log path must be exact, printable and absolute.")
    if any(token in path for token in ("%", "*", "?", "[", "]")):
        raise ValueError("Scheduler log path is unresolved; open its resolved task output explicitly.")
    if getattr(ctx.files, "remote", False):
        before = ctx.files.snapshot_stat(path)
        offset = max(0, before["size"] - maximum)
        data = ctx.files.read(path, offset, maximum)
        if before != ctx.files.snapshot_stat(path) or len(data) != before["size"] - offset:
            raise ValueError("Log changed during timeline inspection; retry.")
        identity = before
    else:
        from .artifacts import _open_local, _signature
        root = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fd = parent = None
        try:
            normalized = os.path.normpath(path)
            fd, parent, name = _open_local(root, normalized.lstrip("/"))
            before = os.fstat(fd)
            offset = max(0, before.st_size - maximum)
            data = os.pread(fd, maximum, offset)
            if (len(data) != before.st_size - offset or _signature(before) != _signature(os.fstat(fd))
                    or _signature(before) != _signature(os.stat(name, dir_fd=parent, follow_symlinks=False))):
                raise ValueError("Log changed during timeline inspection; retry.")
            identity = {"size": before.st_size, "device": before.st_dev, "inode": before.st_ino,
                        "mtime_ns": before.st_mtime_ns, "ctime_ns": before.st_ctime_ns}
        finally:
            if fd is not None:
                os.close(fd)
            if parent is not None:
                os.close(parent)
            os.close(root)
    O.checkpoint(ctx)
    if not isinstance(data, bytes) or len(data) > maximum:
        raise ValueError("Log reader exceeded its bounded tail contract.")
    if offset:
        # The first fragment may begin within a UTF-8 sequence or log line.
        split = data.find(b"\n")
        data = data[split + 1:] if split >= 0 else b""
    return data, offset, identity


def automatic_timeline(params, ctx):
    """Join supported evidence for a fresh, exact scheduler execution."""
    from . import ops_cluster as C, projects, trace_protocol as T
    from .log_catalog import _JOB
    job_id = str(params.get("job_id") or ctx.selected)
    if not _JOB.fullmatch(job_id):
        raise ValueError("Choose one exact job ID or array task, without ranges or wildcards.")
    candidates = [record for record in ctx.jobs + ctx.finished if str(record.get("id")) == job_id]
    if len(candidates) != 1:
        raise ValueError("Select an unambiguous job attempt from the current connection.")
    selected = candidates[0]
    origin, detail = C._retained_identity(ctx, job_id)
    O.checkpoint(ctx)
    cluster = str(detail.get("Cluster") or ctx.scope.get("cluster") or selected.get("cluster") or "")
    if not cluster:
        raise ValueError("Set the connection cluster name before automatically associating timeline evidence.")
    if any(value and value != cluster for value in (ctx.scope.get("cluster"), selected.get("cluster"))):
        raise ValueError("The scheduler cluster changed; refresh the selected job.")
    for before, after in (("submit", "SubmitTime"), ("start", "StartTime")):
        expected, current = str(selected.get(before) or ""), str(detail.get(after) or "")
        if before == "submit" and expected.lower() in _NULL:
            raise ValueError("The selected job lacks a stable submit identity; refresh before joining evidence.")
        if expected.lower() not in _NULL and current != expected:
            raise ValueError("The selected job attempt changed; refresh before joining evidence.")
    owner = detail.get("UserId", "").split("(", 1)[0]
    if selected.get("user") and selected["user"] != owner:
        raise ValueError("The selected job owner changed; refresh before joining evidence.")
    submit, start = detail.get("SubmitTime", ""), detail.get("StartTime", "")
    state = detail.get("JobState", "").split("+", 1)[0]
    if start.lower() in _NULL or state == "PENDING":
        start = ""
    attempt = submit + ("/" + start if start else "")
    base = {"cluster": cluster, "job_id": job_id, "attempt": attempt}
    events, warnings, sources = [], [], []
    for ordinal, (key, kind) in enumerate((("SubmitTime", "submitted"), ("StartTime", "started"), ("EndTime", "ended")), 1):
        raw = detail.get(key, "")
        event_source = "scheduler." + origin
        if key == "EndTime" and origin == "accounting" and not raw and selected.get("end"):
            raw = str(selected["end"])
            event_source = "scheduler.accounting-published"
        if key == "StartTime" and not start:
            continue
        if key == "EndTime" and detail.get("JobState", "").split("+", 1)[0] in {"RUNNING", "PENDING", "CONFIGURING", "COMPLETING", "SUSPENDED"}:
            continue  # Active-job EndTime is a forecast, not a transition.
        if not raw or raw.lower() in _NULL:
            continue
        stamp, textual = _wall(raw)
        events.append(dict(base, id=key, source=event_source, kind=kind,
                           clock="unix" if stamp is not None else "slurm-wall", timestamp=stamp,
                           timestamp_text=textual or O.clean(raw, 128), ordinal=ordinal,
                           message=f"Job {job_id}: {kind}"))
    events.append(dict(base, id="observed-state", source="scheduler." + origin, kind="state-observation",
                       clock="source-order:scheduler", timestamp=None, ordinal=4,
                       message="Current observed state: " + O.clean(detail.get("JobState", "unknown"))))
    sources.append({"source": "scheduler." + origin, "identity": base, "state": detail.get("JobState", "unknown")})
    seen_paths = set()
    for key in ("StdOut", "StdErr"):
        O.checkpoint(ctx)
        path = detail.get(key, "")
        if path.lower() in _NULL:
            warnings.append(key + " path is not retained by the verified scheduler record.")
            continue
        if path in seen_paths:
            continue
        seen_paths.add(path)
        try:
            data, offset, identity = _tail(ctx, path)
            source = key.lower() + ":" + hashlib.sha256(path.encode()).hexdigest()[:16]
            lines = data.decode("utf-8", "replace").splitlines()
            omitted_lines = len(lines) > 256
            lines = lines[-256:]
            sources.append({"source": source, "path": path, "file_identity": identity, "byte_offset": offset,
                            "scope": "up to 256 lines from a 64 KiB tail; ordinals are relative to this window",
                            "association": "verified scheduler path; individual line attempt is not independently verified"})
            for ordinal, line in enumerate(lines, 1):
                stamp, textual = _wall(line)
                events.append(dict(base, id=str(ordinal), source=source, kind="log", message=O.clean(line, 4096) or " ",
                                   clock="unix" if stamp is not None else "source-order:" + source,
                                   timestamp=stamp, timestamp_text=textual, ordinal=ordinal, path=path,
                                   producer_attempt_verified=False))
            if offset or omitted_lines:
                warnings.append(key + " is a bounded tail; earlier lines were not collected.")
        except (OSError, RuntimeError, ValueError) as exc:
            O.checkpoint(ctx)
            warnings.append(key + ": " + O.clean(exc, 240))
    if any(event["kind"] == "log" for event in events):
        warnings.append("Log paths match the current scheduler record. Appended/reused files can contain earlier attempts; individual line identities are not verified.")
    roots = projects.job_project_roots(detail.get("WorkDir") or selected.get("workdir"), params.get("root", ""))
    if getattr(ctx.files, "remote", False):
        warnings.append("Registered trace journals require Tower on their host; no local files were substituted for the SSH connection.")
    else:
        remaining, remaining_bytes, scanned, seen_traces = 3000, 2 << 20, 0, set()
        for root in roots:
            try:
                paths = T.discover_trace(root, job_id, attempt)
            except (OSError, RuntimeError, ValueError) as exc:
                O.checkpoint(ctx)
                warnings.append("Trace registry: " + O.clean(exc, 240))
                continue
            for path in paths:
                if path in seen_traces:
                    continue
                if scanned >= 8 or remaining <= 0 or remaining_bytes <= 0:
                    warnings.append("Trace timeline reached its eight-stream/3000-event/2 MiB budget; additional evidence remains in the journals.")
                    break
                seen_traces.add(path)
                scanned += 1
                O.checkpoint(ctx)
                try:
                    manifest = T.read_json(path)
                    expected = manifest.get("identity", {})
                    if any(expected.get(key) != base[key] for key in ("cluster", "job_id", "attempt")):
                        continue
                    reader = T.TraceReader(path)
                    snapshot = reader.refresh(max_events=min(1500, remaining), max_bytes=min(512 << 10, remaining_bytes), cancel=ctx.cancel)
                    remaining_bytes -= reader.offset
                    O.checkpoint(ctx)
                    identity = snapshot["identity"]
                    if identity != expected:
                        raise ValueError("Trace identity changed during inspection.")
                    source = "trace:" + identity["stream"]
                    sources.append({"source": source, "path": str(path), "identity": identity, "sha256": snapshot["sha256"],
                                    "complete": snapshot["complete"], "scope": "bounded committed journal prefix"})
                    warnings += ["Trace: " + O.clean(message, 240) for message in snapshot["warnings"][:8]]
                    for event in snapshot["events"]:
                        events.append(dict(base, id=str(event["seq"]), source=source, kind=event["kind"],
                                           clock="monotonic:" + identity["node"] + ":" + identity["boot_id"],
                                           timestamp=event["mono_ns"] / 1000000000, ordinal=event["seq"],
                                           message=O.clean(str(event["data"]), 4096) or " ", thread=event["thread"]))
                    remaining -= len(snapshot["events"])
                except (OSError, RuntimeError, ValueError) as exc:
                    O.checkpoint(ctx)
                    warnings.append("Trace source: " + O.clean(exc, 240))
    O.checkpoint(ctx)
    result = timeline({"schema": "tower.timeline/v1", "events": events}, job_id, cluster)
    result.update(sources=sources, identity=base, collection="automatic")
    result["warnings"] = list(dict.fromkeys(result["warnings"] + warnings))
    return result


def run(feature, params, ctx):
    if feature == "investigation-timeline":
        result = (timeline(O.read_json(ctx, params["path"]), params.get("job_id") or ctx.selected, ctx.scope.get("cluster", ""))
                  if params.get("path") else automatic_timeline(params, ctx))
        return O.report(feature, f"{len(result['events'])} joined events", rows=result["rows"], data=result, warnings=result["warnings"])
    if feature != "project-guide":
        raise ValueError("Unknown workspace operation.")
    result = _project(params["root"], ctx, params)
    rows = [result["summary"], "Declared paths describe coverage; actual values still require valid source files."]
    rows += [f"{item['run_id']}: job {item['job_id'] or 'not submitted'}; missing views: " + (", ".join(item["missing_views"]) or "none") for item in result["coverage"]]
    rows += ["", "Sample validation: " + result["sample"]["status"]]
    rows += [item["source"] + ": " + item["status"] + " — " + item["detail"] for item in result["sample"]["checks"]]
    rows += [result["sample"]["scope"], "", "Batch hook (report exec owns start and finish):"] + result["hooks"]["batch"]
    rows += ["", "Application metric/output hooks:"] + result["hooks"]["metrics"]
    rows += ["", "Manual lifecycle alternative:"] + result["hooks"]["manual"] + result["hooks"]["notes"]
    plan = None
    if params.get("initialize") == "yes":
        O.local_only(ctx)
        import os
        info = os.stat(result["root"], follow_symlinks=False)
        plan = O.prepare_plan(ctx, feature, params, {"root": result["root"], "device": info.st_dev, "inode": info.st_ino})
    return O.report(feature, "Project reporting readiness", status="warning" if result["sample"]["status"] != "ok" else "ok",
                    rows=rows, data=result, warnings=result["warnings"] + result["sample"]["warnings"], plan=plan)


def apply(feature, plan, ctx):
    if feature != "project-guide":
        raise ValueError("Timeline inspection is read-only.")
    O.local_only(ctx)
    value = O.validate_plan(plan, ctx, feature)
    # Re-run confined discovery immediately before descriptor creation.
    _project(value["root"], ctx)
    import os
    info = os.stat(value["root"], follow_symlinks=False)
    if (info.st_dev, info.st_ino) != (value["device"], value["inode"]):
        raise ValueError("Project directory changed; prepare a new review.")
    from .research_reporting import initialize
    result = initialize(value["root"])
    return O.report(feature, "Project descriptor is ready; existing data was preserved.", data=result)
