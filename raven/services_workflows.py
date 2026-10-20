"""Read-only bounded native workflow adapters. The engine retains ownership."""
from __future__ import annotations

import csv
import io

from .operation_support import checkpoint, read_json

LIMIT = 2 * 1024 * 1024
MAX_NODES = 10000


def _text(value, field, limit=4096):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError(f"Workflow {field} must be text")
    text = str(value)
    if not text or len(text) > limit or any(ch in text for ch in ("\x00", "\r", "\n")):
        raise ValueError(f"Invalid workflow {field}")
    return text


def nextflow(ctx, source, workflow_id=""):
    """Read a stable bounded trace tail and its original header, not a full scan."""
    checkpoint(ctx)
    files = ctx.files
    before = files.snapshot_stat(source)
    size = before["size"]
    header_bytes = files.read(source, 0, min(size, 65536))
    if len(header_bytes) != min(size, 65536):
        raise ValueError("Workflow header read was incomplete")
    if b"\n" not in header_bytes:
        raise ValueError("Nextflow trace needs a complete TSV header within 64 KiB")
    header = header_bytes.split(b"\n", 1)[0]
    start = max(len(header) + 1, size - LIMIT)
    offset = max(len(header) + 1, start - 1)
    length = min(LIMIT + 1, size - offset)
    tail = files.read(source, offset, length)
    if len(tail) != length:
        raise ValueError("Workflow trace read was incomplete")
    after = files.snapshot_stat(source)
    if before != after:
        raise ValueError("Workflow trace changed during inspection; refresh to retry")
    if start > len(header) + 1:
        # Include the preceding byte so a boundary on a newline is kept exactly.
        if tail[:1] == b"\n":
            tail = tail[1:]
        elif b"\n" in tail:
            tail = tail.split(b"\n", 1)[1]
        else:
            tail = b""
    incomplete = bool(tail and not tail.endswith(b"\n"))
    if incomplete:
        tail = tail.rsplit(b"\n", 1)[0] + b"\n" if b"\n" in tail else b""
    try:
        fields = next(csv.reader([header.decode("utf-8").rstrip("\r")], delimiter="\t"))
        if len(fields) > 256 or len(fields) != len(set(fields)) or not {"task_id", "status"}.issubset(fields):
            raise ValueError("Nextflow trace needs unique task_id and status columns")
        reader = csv.DictReader(io.StringIO(tail.decode("utf-8")), fieldnames=fields, delimiter="\t")
        records = {}
        for item in reader:
            checkpoint(ctx)
            if None in item or any(value is None for value in item.values()):
                raise ValueError("Malformed Nextflow trace row")
            task = _text(item["task_id"], "task_id", 256)
            attempt = item.get("attempt") or "1"
            identity = (task, attempt, item.get("hash", ""))
            records[identity] = {"id": task, "attempt": attempt, "hash": item.get("hash", ""),
                                 "name": item.get("name", item.get("process", task)),
                                 "state": item["status"], "native_id": item.get("native_id", ""),
                                 "exit": item.get("exit", ""), "dependencies": [], "owner": "nextflow"}
            if len(records) > MAX_NODES:
                raise ValueError("Workflow trace exceeds 10000 retained task attempts")
    except (UnicodeError, csv.Error) as exc:
        raise ValueError("Nextflow trace must be bounded UTF-8 TSV") from exc
    return {"engine": "nextflow", "workflow_id": workflow_id or source, "source": source,
            "nodes": list(records.values()), "truncated": start > len(header) + 1,
            "incomplete_tail": incomplete, "ownership": "engine", "dag_available": False,
            "source_size": size}


def snakemake(ctx, source, workflow_id=""):
    """Accept Snakemake --d3dag JSON or an explicit runtime plugin snapshot."""
    value = read_json(ctx, source, limit=LIMIT)
    if not isinstance(value, dict):
        raise ValueError("Snakemake source must be a JSON object")
    envelope = value.get("schema") == "raven.workflow-engine/v1"
    if value.get("schema") and not envelope:
        raise ValueError("Unknown workflow-engine schema")
    if envelope and value.get("engine") != "snakemake":
        raise ValueError("The source belongs to a different workflow engine")
    if envelope:
        if set(value) - {"schema", "engine", "workflow_id", "nodes"}:
            raise ValueError("Unknown workflow-engine snapshot field")
        _text(value.get("workflow_id"), "workflow_id")
    raw = value.get("nodes")
    if not isinstance(raw, list) or len(raw) > MAX_NODES:
        raise ValueError("Snakemake nodes must be a list with at most 10000 entries")
    nodes = {}
    for item in raw:
        checkpoint(ctx)
        if not isinstance(item, dict):
            raise ValueError("Invalid Snakemake node")
        identity = _text(item.get("id"), "node id", 256)
        if identity in nodes:
            raise ValueError("Duplicate Snakemake node identity")
        details = item if envelope else item.get("value", {})
        if not isinstance(details, dict):
            raise ValueError("Invalid Snakemake node value")
        if envelope:
            if set(item) - {"id", "rule", "native_id", "state", "attempt", "dependencies"} or "dependencies" not in item:
                raise ValueError("Invalid workflow-engine node fields")
            for name, limit in (("rule", 4096), ("native_id", 256), ("state", 128), ("attempt", 128)):
                if name in item and item[name] != "":
                    _text(item[name], name, limit)
        dependencies = item.get("dependencies", []) if envelope else []
        if not isinstance(dependencies, list) or len(dependencies) > MAX_NODES:
            raise ValueError("Invalid Snakemake dependencies")
        nodes[identity] = {"id": identity, "name": str(details.get("rule", details.get("label", identity)))[:4096],
                           "native_id": str(details.get("native_id", ""))[:256] if envelope else "",
                           "state": str(details.get("state", "unknown"))[:128] if envelope else "unknown",
                           "attempt": str(details.get("attempt", ""))[:128] if envelope else "",
                           "dependencies": [_text(dep, "dependency", 256) for dep in dependencies],
                           "owner": "snakemake"}
    if not envelope:
        links = value.get("links", [])
        if not isinstance(links, list) or len(links) > 50000:
            raise ValueError("Invalid or oversized Snakemake links")
        for link in links:
            if not isinstance(link, dict):
                raise ValueError("Invalid Snakemake link")
            source_id, target_id = (_text(link.get(key), "link id", 256) for key in ("source", "target"))
            if source_id not in nodes or target_id not in nodes:
                raise ValueError("Snakemake link refers to an unknown node")
            nodes[target_id]["dependencies"].append(source_id)
    # Kahn's algorithm is linear, avoids recursion and rejects cyclic input.
    waiting, children = {}, {key: [] for key in nodes}
    edges = 0
    for key, node in nodes.items():
        deps = node["dependencies"]
        if len(set(deps)) != len(deps) or key in deps:
            raise ValueError("Duplicate or self-referencing workflow dependency")
        waiting[key] = len(deps)
        edges += len(deps)
        if edges > 50000:
            raise ValueError("Workflow exceeds 50000 edges")
        for dep in deps:
            if dep not in nodes:
                raise ValueError("Workflow dependency refers to an unknown node")
            children[dep].append(key)
    ready = [key for key, count in waiting.items() if count == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for child in children[current]:
            waiting[child] -= 1
            if waiting[child] == 0:
                ready.append(child)
    if visited != len(nodes):
        raise ValueError("Workflow DAG contains a cycle")
    return {"engine": "snakemake", "workflow_id": workflow_id or str(value.get("workflow_id", source)),
            "source": source, "nodes": list(nodes.values()), "truncated": False,
            "ownership": "engine", "dag_available": True, "runtime_available": envelope}
