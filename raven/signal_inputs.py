"""Read-only, provenance-preserving conversion from one indexed evidence stream."""
from __future__ import annotations

import json

from . import evidence_index as I, measurement_series as M


def from_index(index, *, source_id, attempt, metric, selector=None, limit=None, cancel=None):
    selected = I.series(index, source_id=source_id, attempt=attempt, metric=metric, selector=selector, cancel=cancel)
    if len(selected["samples"]) > M.MAX_SAMPLES:
        raise ValueError("Select a source with at most 4096 observations; no implicit downsampling")
    result = I.query(index, "SELECT DISTINCT sources.capture,sources.path,sources.sha256,identity_json FROM measurements JOIN sources ON source_id=sources.id WHERE source_id=? AND attempt_key=? AND metric=?",
                     parameters=(source_id, attempt, metric), max_rows=2, cancel=cancel)
    if result["status"] != "complete" or len(result["rows"]) != 1:
        raise ValueError("Measurement provenance is missing or ambiguous; rebuild the evidence index")
    capture, path, digest, raw_identity = result["rows"][0]
    declared = json.loads(raw_identity)
    mapped_metric = "rss" if metric == "rss_bytes" else metric
    unit = {"bytes": "B", "percent": "%", "seconds": "s"}.get(selected["unit"], selected["unit"])
    item = {"identity": {"scope": "capture:" + capture, "attempt": attempt,
                         "job": declared["job_id"] or declared["run_id"], "verified": True},
            "metric": mapped_metric, "unit": unit,
            "samples": [{"t": stamp, "value": value} for stamp, value, _ in selected["samples"]],
            "source": {"capture": capture, "sha256": digest, "path": path, "source_id": source_id,
                       "source_rows": [row for _, _, row in selected["samples"]],
                       "selector": selected["dimensions"], "metric": metric, "unit": selected["unit"]}}
    if limit is not None:
        item["limit"] = limit
    return M.validate({"schema": M.SCHEMA, "series": [item]})
