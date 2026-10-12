"""Scale and infrastructure operations exposed by the terminal workbench."""
from __future__ import annotations

import re
import time

from . import operations as O
from . import scale_bottlenecks as B, scale_clusters as C, scale_energy as E, scale_incidents as I
from .scale_common import text, timestamp

SPECIFICATIONS = [
    {"key": "bottlenecks", "title": "Distributed bottleneck explorer", "group": "Scale", "proposal": "S02",
     "summary": "Inspect rank/phase skew, MPI time, and I/O evidence from bounded profiler exports.",
     "fields": [{"key": "path", "label": "Profiler export", "required": True},
                {"key": "format", "label": "Export format", "default": "json", "choices": ["json", "darshan"]},
                {"key": "job_id", "label": "Job ID"}, {"key": "cluster", "label": "Darshan cluster"},
                {"key": "attempt", "label": "Darshan attempt"}]},
    {"key": "clusters", "title": "Concurrent cluster workspace", "group": "Scale", "proposal": "S05",
     "summary": "Read queues and recent history from isolated cluster connections concurrently.",
     "fields": [{"key": "path", "label": "Workspace JSON (or profiles)"}, {"key": "user", "label": "Slurm user"},
                {"key": "history_hours", "label": "History hours", "default": "24"},
                {"key": "source", "label": "Inspect source name"}, {"key": "job_id", "label": "Inspect job ID"},
                {"key": "attempt", "label": "Inspect submit/start identity"}]},
    {"key": "incidents", "title": "Historical incident correlation", "group": "Scale", "proposal": "A17",
     "summary": "Match historical node events to one job's actual nodes and execution interval.",
     "fields": [{"key": "job_id", "label": "Job ID"}, {"key": "path", "label": "Events JSON (or Slurm events)"},
                {"key": "cluster", "label": "Cluster (if unknown)"}, {"key": "start", "label": "Start override (ISO)"},
                {"key": "end", "label": "End override (ISO)"}]},
    {"key": "energy", "title": "Energy per useful result", "group": "Scale", "proposal": "A18",
     "summary": "Account for successful and failed attempt energy with explicit scope and accepted useful work.",
     "fields": [{"key": "path", "label": "Energy campaign JSON", "required": True}]},
]


def _report(feature, summary, result):
    result = dict(result)
    rows, warnings = result.pop("rows", []), result.pop("warnings", [])
    return O.report(feature, summary, status="partial" if warnings else "ok", rows=rows, warnings=warnings, data=result)


def _job(ctx, job_id):
    candidates = [job for job in ctx.jobs + ctx.finished if str(job.get("id")) == job_id]
    if not candidates:
        raise ValueError("Select a known job in this connection before inspecting its incidents")
    # Live rows are authoritative only when their attempt identity agrees.
    keys = {(str(job.get("submit") or ""), str(job.get("start") or "")) for job in candidates}
    if len(keys) > 1:
        raise ValueError("Job ID is ambiguous across attempts; select an unambiguous job snapshot")
    return candidates[0]


def _incidents(params, ctx):
    job_id = str(params.get("job_id") or ctx.selected or "")
    job = _job(ctx, job_id)
    cluster = str(params.get("cluster") or job.get("cluster") or ctx.scope.get("cluster") or "")
    if not cluster:
        raw = O.command(ctx, ["scontrol", "show", "config"], timeout=4, limit=1 << 20)
        match = re.search(r"(?m)^\s*ClusterName\s*=\s*([A-Za-z0-9_.-]+)\s*$", raw)
        if match:
            cluster = match.group(1)
    text(cluster, "Cluster identity")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", cluster):
        raise ValueError("Invalid cluster identity")
    start = timestamp(params["start"]) if params.get("start") else I.scheduler_time(job.get("start"))
    running = any(row is job for row in ctx.jobs)
    end = timestamp(params["end"]) if params.get("end") else time.time() if running else I.scheduler_time(job.get("end"))
    if start is None or end is None:
        raise ValueError("Job start/end evidence is unknown; set explicit timestamps with time zones")
    hosts = job.get("hosts") or []
    if not hosts:
        nodelist = job.get("nodelist") or ""
        if not isinstance(nodelist, str) or not re.fullmatch(r"[A-Za-z0-9_.\[\],:-]{1,8192}", nodelist) or nodelist.startswith("-"):
            raise ValueError("Job allocation host list is unknown or invalid")
        hosts = O.command(ctx, ["scontrol", "show", "hostnames", nodelist], timeout=4, limit=256 << 10).split()
    if len(hosts) > 10000:
        raise ValueError("Node expansion exceeds 10000 hosts")
    if params.get("path"):
        events = I.normalize(O.read_json(ctx, params["path"]))
    else:
        from datetime import datetime
        # Slurm's query parser receives the local wall clock format it expects.
        begin, finish = (datetime.fromtimestamp(value).isoformat(timespec="seconds") for value in (start, end))
        raw = O.command(ctx, ["sacctmgr", "-n", "-P", "show", "events", "where", "Clusters=" + cluster,
                              "Start=" + begin, "End=" + finish, "Event=Node",
                              "format=Cluster,NodeName,Start,End,State,Reason"], timeout=8)
        events = I.parse_sacctmgr(raw, cluster=cluster)
    return I.correlate(events, cluster=cluster, job_id=job_id, hosts=hosts, start=start, end=end)


def run(feature, params, ctx):
    O.checkpoint(ctx)
    if feature == "bottlenecks":
        if params.get("format", "json") == "darshan":
            value = B.import_darshan(O.read_bytes(ctx, params.get("path", "")).decode("utf-8"),
                                    cluster=params.get("cluster") or ctx.scope.get("cluster", ""),
                                    job_id=params.get("job_id") or ctx.selected, attempt=params.get("attempt", ""))
        elif params.get("format", "json") == "json":
            value = O.read_json(ctx, params.get("path", ""))
        else:
            raise ValueError("Profiler format must be json or darshan")
        result = B.analyze(value, job_id=str(params.get("job_id") or ctx.selected or ""))
        known_cluster = ctx.scope.get("cluster")
        if known_cluster and result["cluster"] != known_cluster:
            raise ValueError("Profiler cluster does not match the selected connection")
        chosen = str(params.get("job_id") or ctx.selected or "")
        if chosen:
            candidates = [job for job in ctx.jobs + ctx.finished if str(job.get("id")) == chosen]
            identities = {(str(job.get("submit") or ""), str(job.get("start") or "")) for job in candidates}
            if len(identities) > 1:
                raise ValueError("Selected profiler job is ambiguous across attempts")
            if identities:
                submit, start = next(iter(identities))
                started = start not in ("", "Unknown", "None", "N/A", "0")
                expected_attempt = submit + "/" + start if started else submit
                if not submit or result["attempt"] != expected_attempt:
                    raise ValueError("Profiler attempt does not match the selected job submit/start identity")
                result["selected_attempt_verified"] = bool(known_cluster)
                if not known_cluster:
                    result["warnings"].append("Current cluster identity is unknown; the profiler cannot be fully verified against the selected connection")
            else:
                result["selected_attempt_verified"] = False
                result["warnings"].append("Profiler job is not in the captured queue/history; selected attempt cannot be verified")
        return _report(feature, "Distributed profiler evidence", result)
    if feature == "energy":
        return _report(feature, "Energy cost per accepted useful result", E.analyze(O.read_json(ctx, params.get("path", ""))))
    if feature == "incidents":
        return _report(feature, "Historical node-event overlap", _incidents(params, ctx))
    if feature == "clusters":
        value = O.read_json(ctx, params["path"]) if params.get("path") else None
        sources = C.configuration(value, ctx.cfg)
        try:
            hours = float(params.get("history_hours") or 24)
        except (ValueError, TypeError) as exc:
            raise ValueError("History hours must be numeric") from exc
        result = C.collect(ctx, sources, user=params.get("user", ""), history_hours=hours)
        if params.get("source") or params.get("job_id"):
            if not params.get("source") or not params.get("job_id"):
                raise ValueError("Inspection needs both source name and job ID")
            matches = [job for job in result["jobs"] if job["cluster_name"] == params["source"] and job["job_id"] == params["job_id"]
                       and (not params.get("attempt") or job["attempt"] == params["attempt"])]
            if len(matches) != 1:
                raise ValueError("Selected cross-cluster job is absent or ambiguous; specify its submit/start attempt identity")
            result["selected"] = matches[0]
            result["rows"] += ["Selected cross-cluster job (read-only):"] + [f"  {key}: {value}" for key, value in sorted(matches[0].items()) if not isinstance(value, (dict, list))]
        return _report(feature, "Concurrent cluster queues and recent history", result)
    raise ValueError("Unknown scale operation")


def apply(feature, plan, ctx):
    raise ValueError("Scale diagnostics are read-only; they do not change jobs or cluster connections")


# Managed Raven entry points also protect direct adapter callers.
from .operations import managed_run as _managed_run, managed_apply as _managed_apply
run = _managed_run(run)
apply = _managed_apply(apply)
