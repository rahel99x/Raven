"""Versioned, bounded evidence receipts. A digest proves integrity, not truth.

Clock domains, source coverage and losses remain explicit; ingestion never
promotes sampled observations to a complete execution trace.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

SCHEMA = "tower.evidence/v1"
MAX_BYTES = 8 << 20
MAX_RECORDS = 20000
HASH = re.compile(r"[0-9a-f]{64}\Z")
KINDS = {"metric", "span", "flow", "artifact", "run", "annotation"}
UNITS = {"s", "ms", "us", "ns", "B", "KiB", "MiB", "GiB", "count", "percent", "ratio", "J", "W", "Hz", "B/s", "count/s", "unknown"}


def text(value, label="text", limit=4096, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not value and not empty) or any(not c.isprintable() for c in value):
        raise ValueError(f"{label} must be bounded printable text")
    return value


def finite(value, label="number", minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > 1e100 or not math.isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError(f"{label} must be a finite number" + (f" >= {minimum}" if minimum is not None else ""))
    return value


def integer(value, label="count", maximum=2**63-1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{label} must be an integer in 0..{maximum}")
    return value


def bounded(value, *, max_nodes=200000):
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > max_nodes or depth > 24:
            raise ValueError("Evidence nesting or item count exceeds its bound")
        if isinstance(item, dict):
            for key, child in item.items():
                text(key, "JSON key", 256)
                pending.append((child, depth + 1))
        elif isinstance(item, (list, tuple)):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > 65536 or any(ord(c) < 32 and c not in "\n\t\r" for c in item):
                raise ValueError("Evidence strings contain controls or exceed their bound")
        elif item is not None and type(item) is not bool:
            finite(item)
    return value


def canonical(value):
    bounded(value)
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    if len(data) > MAX_BYTES:
        raise ValueError("Evidence exceeds the 8 MiB document bound")
    return data


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def loads(raw):
    if not isinstance(raw, (str, bytes)) or len(raw) > MAX_BYTES:
        raise ValueError("Use a bounded JSON document")
    def pairs(values):
        out = {}
        for key, value in values:
            if key in out:
                raise ValueError("Duplicate JSON key: " + str(key)[:80])
            out[key] = value
        return out
    def reject(value):
        raise ValueError("Nonfinite JSON number: " + value)
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)
        canonical(value)
        return value
    except (UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError("Invalid bounded UTF-8 JSON") from exc


def keys(value, required, optional=(), label="object"):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError(f"Invalid {label} fields; requires {', '.join(sorted(required))}")
    return value


def identity(value):
    keys(value, {"cluster", "job_id", "attempt"}, {"node", "process_start", "pid", "rank", "thread", "task"}, "execution identity")
    for name, item in value.items():
        if name in {"pid", "rank", "thread"}:
            integer(item, name)
        else:
            text(item, name, 512)
    if not re.fullmatch(r"[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?(?:\.[A-Za-z0-9_-]+)?", value["job_id"]):
        raise ValueError("Execution job_id must be an exact Slurm job/task/step ID")
    if "pid" in value and not {"node", "process_start"} <= value.keys():
        raise ValueError("Process identity requires node and process_start to prevent PID reuse")
    return copy.deepcopy(value)


def validate(value):
    canonical(value)
    keys(value, {"schema", "identity", "source", "clock", "coverage", "loss", "records", "transforms"}, {"digest", "metadata"}, "evidence")
    if value["schema"] != SCHEMA:
        raise ValueError("Expected " + SCHEMA)
    identity(value["identity"])
    source = keys(value["source"], {"id", "format", "version", "sha256"}, {"path", "producer"}, "source")
    for key, item in source.items():
        text(item, "source " + key, 4096)
    if not HASH.fullmatch(source["sha256"]):
        raise ValueError("Source sha256 must be a lowercase SHA256 digest")
    clock = keys(value["clock"], {"domain", "unit", "uncertainty"}, {"origin"}, "clock")
    text(clock["domain"], "clock domain", 512)
    if not isinstance(clock["unit"], str) or clock["unit"] not in {"s", "ms", "us", "ns", "unknown"}:
        raise ValueError("Clock unit must be s, ms, us, ns or unknown")
    if clock["uncertainty"] is not None:
        finite(clock["uncertainty"], "clock uncertainty", 0)
    if "origin" in clock:
        text(clock["origin"], "clock origin")
    coverage = keys(value["coverage"], {"mode", "complete", "scope", "gaps", "omitted"}, label="coverage")
    if not isinstance(coverage["mode"], str) or coverage["mode"] not in {"events", "samples", "aggregates", "provenance", "mixed"} or coverage["complete"] not in (None, True, False) or (coverage["complete"] is not None and type(coverage["complete"]) is not bool):
        raise ValueError("Invalid evidence coverage mode or completeness")
    if not isinstance(coverage["scope"], list) or len(coverage["scope"]) > 256:
        raise ValueError("Coverage scope must be a bounded list")
    for item in coverage["scope"]:
        text(item, "coverage scope")
    integer(coverage["omitted"], "omitted records")
    if not isinstance(coverage["gaps"], list) or len(coverage["gaps"]) > 4096:
        raise ValueError("Coverage gaps must be a bounded list")
    for gap in coverage["gaps"]:
        keys(gap, {"start", "end", "reason"}, label="gap")
        finite(gap["start"], "gap start")
        finite(gap["end"], "gap end")
        if gap["end"] < gap["start"]:
            raise ValueError("Gap end precedes start")
        text(gap["reason"], "gap reason")
    loss = keys(value["loss"], {"records", "reason"}, label="loss")
    if loss["records"] is not None:
        integer(loss["records"], "lost records")
    text(loss["reason"], "loss reason")
    if coverage["complete"] is True and (coverage["omitted"] or coverage["gaps"] or loss["records"] != 0):
        raise ValueError("Complete coverage cannot contain omitted, missing or unknown-loss records")
    records, seen = value["records"], set()
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise ValueError("Evidence records exceed their 20000 record bound")
    for record in records:
        keys(record, {"id", "kind", "data"}, label="record")
        token = text(record["id"], "record id", 512)
        if token in seen:
            raise ValueError("Duplicate record identity: " + token)
        seen.add(token)
        if not isinstance(record["kind"], str) or record["kind"] not in KINDS or not isinstance(record["data"], dict):
            raise ValueError("Unsupported record kind or data")
        data = record["data"]
        if record["kind"] == "metric":
            keys(data, {"name", "value", "unit"}, {"time", "attributes"}, "metric")
            text(data["name"], "metric name")
            if data["value"] is not None:
                finite(data["value"], "metric value")
            if not isinstance(data["unit"], str) or data["unit"] not in UNITS:
                raise ValueError("Unrecognized metric unit; use unknown and retain original attributes")
            if "time" in data:
                finite(data["time"], "metric timestamp")
        if record["kind"] == "span":
            keys(data, {"name", "start", "end"}, {"pid", "tid", "attributes"}, "span")
            text(data["name"], "span name")
            finite(data["start"], "span start")
            if data["end"] is not None:
                finite(data["end"], "span end")
                if data["end"] < data["start"]:
                    raise ValueError("Span end precedes start")
    transforms = value["transforms"]
    if not isinstance(transforms, list) or len(transforms) > 64:
        raise ValueError("Transforms must be a bounded list")
    for transform in transforms:
        keys(transform, {"name", "version", "parameters"}, label="transform")
        text(transform["name"], "transform name")
        text(transform["version"], "transform version")
        if not isinstance(transform["parameters"], dict):
            raise ValueError("Transform parameters must be an object")
    if "metadata" in value and not isinstance(value["metadata"], dict):
        raise ValueError("Evidence metadata must be an object")
    if "digest" in value and value["digest"] != digest({k: v for k, v in value.items() if k != "digest"}):
        raise ValueError("Evidence receipt digest does not match")
    return copy.deepcopy(value)


def receipt(identity, source, records, *, clock=None, coverage=None, loss=None, transforms=(), metadata=None):
    value = {"schema": SCHEMA, "identity": identity, "source": source,
             "clock": clock or {"domain": "unknown", "unit": "unknown", "uncertainty": None},
             "coverage": coverage or {"mode": "mixed", "complete": None, "scope": [], "gaps": [], "omitted": 0},
             "loss": loss or {"records": None, "reason": "Source does not report loss accounting"},
             "records": records, "transforms": list(transforms)}
    if metadata is not None:
        value["metadata"] = metadata
    value = validate(value)
    value["digest"] = digest(value)
    return value


def verify_receipt(value):
    if not isinstance(value, dict) or "digest" not in value:
        raise ValueError("Evidence receipt has no digest")
    return validate(value)
