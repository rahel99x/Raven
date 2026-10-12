"""Pure bounded telemetry reader contracts, shared with the confined SSH helper."""
from __future__ import annotations

import collections
import json
import math

INDEX_SCHEMA = "raven.telemetry-index/v1"
GPU_SCHEMA = "raven.gpu-sample/v1"
CPU_SCHEMA = "raven.cpu-memory-sample/v1"
MAX_INDEX_BYTES = 256 * 1024
MAX_READ_BYTES = 8 * 1024 * 1024
MAX_READ_ROWS = 20000


def validate_index(value, run_id):
    if not isinstance(value, dict) or value.get("schema") != INDEX_SCHEMA or value.get("run_id") != run_id:
        raise ValueError("retained telemetry index does not match the exact run")
    sources = value.get("sources")
    if not isinstance(sources, list) or len(sources) > 16:
        raise ValueError("invalid telemetry source list")
    seen = set()
    for source in sources:
        if not isinstance(source, dict) or source.get("family") not in {"gpu", "cpu-memory"}:
            raise ValueError("unsupported retained telemetry family")
        if source.get("status") not in {"recording", "complete", "partial", "unavailable", "failed"}:
            raise ValueError("invalid telemetry source status")
        if not isinstance(source.get("reason", ""), str) or not isinstance(source.get("coverage", {}), dict):
            raise ValueError("invalid telemetry source coverage")
        for key, maximum in (("devices", 256), ("metrics", 64)):
            values = source.get(key, [])
            if not isinstance(values, list) or len(values) > maximum:
                raise ValueError("invalid bounded telemetry " + key)
        family = source["family"]
        if family in seen or source.get("path") != "telemetry/" + family + ".jsonl":
            raise ValueError("invalid or duplicate retained telemetry path")
        seen.add(family)
        if source.get("schema") != (GPU_SCHEMA if family == "gpu" else CPU_SCHEMA):
            raise ValueError("unsupported retained telemetry stream schema")
    return value


def parse_rows(data, source, run_id, *, max_rows=MAX_READ_ROWS):
    """Bounded dashboard tail. Export uses the original full retained streams."""
    rows = collections.deque(maxlen=max_rows)
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n") or len(line) > 65536:
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if not isinstance(row, dict) or row.get("schema") != source["schema"] or row.get("run_id") != run_id:
            continue
        stamp = row.get("timestamp")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            continue
        rows.append(row)
    return list(rows)
