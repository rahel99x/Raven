"""Bounded, explicit timestamped measurements shared by experimental analyses."""
from __future__ import annotations

import hashlib
import json
import math

SCHEMA = "raven.measurement-series/v1"
MAX_SERIES = 16
MAX_SAMPLES = 4096


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def identity(value):
    if not isinstance(value, dict) or value.get("verified") is not True:
        raise ValueError("Analysis requires a verified attempt identity; job numbers alone are insufficient")
    if set(value) != {"scope", "attempt", "job", "verified"}:
        raise ValueError("Measurement identity needs scope, attempt, job and verified only")
    for key in ("scope", "attempt", "job"):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 256:
            raise ValueError("Measurement identity fields must be bounded nonempty strings")
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate(document):
    if not isinstance(document, dict) or document.get("schema") != SCHEMA or set(document) != {"schema", "series"}:
        raise ValueError("Use a raven.measurement-series/v1 document with schema and series")
    series = document["series"]
    if not isinstance(series, list) or not 1 <= len(series) <= MAX_SERIES:
        raise ValueError("Analysis accepts 1..16 explicit series")
    seen = set()
    for item in series:
        if not isinstance(item, dict) or set(item) - {"identity", "metric", "unit", "samples", "limit", "source"}:
            raise ValueError("Unknown measurement series fields")
        token = identity(item.get("identity"))
        for field in ("metric", "unit"):
            if not isinstance(item.get(field), str) or not 1 <= len(item[field]) <= 128:
                raise ValueError("Each series needs a bounded metric name and explicit unit")
        key = token, item["metric"]
        if key in seen:
            raise ValueError("Duplicate attempt/metric series; select one authoritative recording")
        seen.add(key)
        limit = item.get("limit")
        if limit is not None and (not finite(limit) or limit <= 0):
            raise ValueError("A resource limit must be finite, positive and use the series unit")
        samples = item.get("samples")
        if not isinstance(samples, list) or len(samples) > MAX_SAMPLES:
            raise ValueError("Each series accepts at most 4096 observations")
        source = item.get("source")
        if source is not None:
            required = {"capture", "sha256", "path", "source_id", "source_rows", "selector", "metric", "unit"}
            if not isinstance(source, dict) or set(source) != required:
                raise ValueError("Source provenance fields are incomplete or unknown")
            if any(not isinstance(source[key], str) or not 1 <= len(source[key]) <= 4096 for key in ("capture", "sha256", "path", "metric", "unit")):
                raise ValueError("Source provenance strings must be bounded")
            if any(len(source[key]) != 64 or any(c not in "0123456789abcdef" for c in source[key]) for key in ("capture", "sha256")):
                raise ValueError("Source provenance requires exact SHA-256 digests")
            if type(source["source_id"]) is not int or source["source_id"] <= 0 or not isinstance(source["source_rows"], list) or len(source["source_rows"]) != len(samples) or any(type(row) is not int or row < 2 for row in source["source_rows"]):
                raise ValueError("Source provenance needs one positive CSV row per observation")
            if not isinstance(source["selector"], dict) or len(source["selector"]) > 16 or any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 128 or len(v) > 4096 for k, v in source["selector"].items()):
                raise ValueError("Source stream dimensions must be bounded strings")
        previous = None
        for sample in samples:
            if not isinstance(sample, dict) or set(sample) - {"t", "value", "phase"} or not {"t", "value"} <= set(sample):
                raise ValueError("Each sample needs t and value, with optional declared phase")
            stamp, value = sample["t"], sample["value"]
            if not finite(stamp) or value is not None and not finite(value):
                raise ValueError("Samples need finite seconds and finite values or explicit null")
            if previous is not None and stamp <= previous:
                raise ValueError("Timestamps must increase strictly; resolve duplicate/reordered recordings explicitly")
            previous = stamp
            phase = sample.get("phase", "")
            if not isinstance(phase, str) or len(phase) > 128:
                raise ValueError("Declared phase labels must be bounded strings")
    return document
