"""Historical node-event overlap, without attributing causality."""
from __future__ import annotations

from datetime import datetime

from .scale_common import document, items, text, timestamp, iso


def scheduler_time(value):
    """Slurm dates are interpreted in this process's configured local timezone."""
    if value in (None, "", "Unknown", "None", "N/A", "0"):
        return None
    try:
        if isinstance(value, (int, float)):
            return timestamp(value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Invalid Slurm accounting timestamp") from exc


def normalize(value):
    document(value, "raven.incidents/v1")
    events, seen = [], set()
    for item in items(value.get("events"), "events"):
        cluster = text(item.get("cluster"), "cluster")
        nodes = item.get("nodes")
        if not isinstance(nodes, list) or not nodes or len(nodes) > 10000:
            raise ValueError("incident nodes must be a nonempty list of at most 10000 exact hostnames")
        nodes = sorted(set(text(node, "node") for node in nodes))
        if any("[" in node or "]" in node or "," in node for node in nodes):
            raise ValueError("Incident nodes must be expanded hostnames, not hostlist expressions")
        start = timestamp(item.get("start"), "incident start")
        end = timestamp(item["end"], "incident end") if item.get("end") is not None else None
        if end is not None and end <= start:
            raise ValueError("incident end must be after start")
        reason = text(item.get("reason", "unknown"), "reason", limit=2048)
        state = text(item.get("state", "unknown"), "state")
        key = (cluster, tuple(nodes), start, end, reason, state)
        if key in seen:
            continue
        seen.add(key)
        events.append(dict(cluster=cluster, nodes=nodes, start=start, end=end, reason=reason, state=state))
    return events


def parse_sacctmgr(raw, *, cluster):
    if not isinstance(raw, str) or len(raw.encode()) > 1024 * 1024:
        raise ValueError("Node event output exceeds 1 MiB")
    events = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        fields = line.split("|")
        if len(fields) == 7 and not fields[-1]:
            fields.pop()
        if len(fields) != 6:
            raise ValueError("Malformed sacctmgr node event output")
        source_cluster, node, start, end, state, reason = fields
        if source_cluster != cluster:
            continue
        ts = scheduler_time(start)
        te = scheduler_time(end)
        if ts is None:
            raise ValueError("Node event has no start time")
        events.append(dict(cluster=source_cluster, nodes=[node], start=ts, end=te, state=state or "unknown", reason=reason or "unknown"))
    return normalize({"schema": "raven.incidents/v1", "events": events})


def correlate(events, *, cluster, job_id, hosts, start, end):
    start, end = timestamp(start), timestamp(end)
    if end <= start:
        raise ValueError("Job interval must end after it starts")
    if not hosts:
        raise ValueError("Job node allocation is unknown; node events cannot be attributed")
    hosts = set(text(host, "job hostname") for host in hosts)
    result, rows = [], []
    for event in events:
        if event["cluster"] != cluster:
            continue
        nodes = sorted(hosts.intersection(event["nodes"]))
        overlap_start, overlap_end = max(start, event["start"]), min(end, event["end"] or end)
        if not nodes or overlap_start >= overlap_end:
            continue
        item = dict(event, matched_nodes=nodes, overlap_start=overlap_start, overlap_end=overlap_end, overlap_s=overlap_end-overlap_start)
        result.append(item)
        rows.append(f"{iso(overlap_start)}..{iso(overlap_end)} | {','.join(nodes)} | {event['state']} | {event['reason']}")
    result.sort(key=lambda r: (r["overlap_start"], r["matched_nodes"]))
    if not rows:
        rows = ["No overlapping events in the available source. This does not prove infrastructure health."]
    return {"job_id": job_id, "cluster": cluster, "start": start, "end": end, "events": result,
            "rows": rows, "warnings": ["Time and node overlap is correlation, not proof of a job failure cause.",
                                       "Event retention and permissions limit the available history. Slurm dates without an offset use Raven's local timezone."]}
