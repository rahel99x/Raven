"""Private, bounded incident evidence using existing recording and publication.

An evidence export does not attach a debugger or start a sampler. Applications
that want pre-trigger retention use the existing cooperative TriggeredCapture.
"""

from __future__ import annotations

import copy
import hashlib
import os
import stat
from pathlib import Path

from .. import evidence as E
from .. import science_files
from ..redaction import redact
from .performance import _bind, _cancel, trace_identity

KINDS = frozenset(
    {
        "oom",
        "node-failure",
        "preemption",
        "timeout",
        "application-error",
        "rank-stall",
        "gpu-error",
        "slowdown",
        "manual",
    }
)


def _budget(value):
    if value is not None and not isinstance(value, dict):
        raise ValueError("Incident budget must be an object")
    result = {
        "max_bytes": 4 << 20,
        "max_events": 4096,
        "max_log_bytes": 256 << 10,
        "pre_seconds": 5.0,
        "post_seconds": 5.0,
        **(value or {}),
    }
    E.keys(
        result,
        {"max_bytes", "max_events", "max_log_bytes", "pre_seconds", "post_seconds"},
        label="incident budget",
    )
    for key, minimum, maximum in (
        ("max_bytes", 64 << 10, 8 << 20),
        ("max_events", 1, 20000),
        ("max_log_bytes", 1, 1 << 20),
    ):
        E.integer(result[key], key, maximum)
        if result[key] < minimum:
            raise ValueError(key + " is below the minimum incident budget")
    for key in ("pre_seconds", "post_seconds"):
        E.finite(result[key], key, 0)
        if result[key] > 3600:
            raise ValueError("Incident windows are limited to one hour per side")
    return result


def _redacted(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _redacted(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redacted(item) for item in value]
    return value


def capture(
    destination,
    *,
    identity,
    trigger,
    query=None,
    scheduler=None,
    logs=(),
    samples=(),
    budget=None,
    cancel=None,
):
    """Save selected retained evidence for one attempt in a new private directory.

    ``trigger`` requires kind, clock_id, time_ns, and an evidence description.
    Logs are explicit ``{path, identity}`` selections; samples require identity,
    clock_id and time_ns. No paths are discovered and no ambient environment is
    collected. Untimed logs remain excerpts with unverified chronology.
    """
    _cancel(cancel)
    identity = trace_identity(identity)
    budget = _budget(budget)
    E.canonical(trigger)
    E.keys(trigger, {"kind", "clock_id", "time_ns", "evidence"}, label="incident trigger")
    if not isinstance(trigger["kind"], str) or trigger["kind"] not in KINDS:
        raise ValueError("Unknown incident trigger kind")
    E.text(trigger["clock_id"], "trigger clock", 1024)
    E.text(trigger["evidence"], "trigger evidence", 2048)
    E.integer(trigger["time_ns"], "trigger timestamp", (1 << 64) - 1)
    start = max(0, trigger["time_ns"] - int(budget["pre_seconds"] * 1e9))
    end = min((1 << 64) - 1, trigger["time_ns"] + int(budget["post_seconds"] * 1e9))
    if not isinstance(logs, (list, tuple)) or len(logs) > 16:
        raise ValueError("Select at most 16 explicit incident log files")
    if not isinstance(samples, (list, tuple)) or len(samples) > 20000:
        raise ValueError("Incident samples exceed their 20000-record bound")
    E.canonical(samples)
    # Reserve fixed metadata/receipt space first. Each accepted source/row is
    # serialized once for admission; final serialization verifies the hard cap.
    remaining = budget["max_bytes"] - (32 << 10)
    errors = []

    def problem(source, message):
        if len(errors) < 16:
            errors.append(
                {"source": redact(str(source))[:256], "read_error": redact(str(message))[:512]}
            )

    scheduler_doc = None
    if scheduler is not None:
        E.canonical(scheduler)
        if (
            not isinstance(scheduler, dict)
            or trace_identity(scheduler.get("identity", {})) != identity
        ):
            raise ValueError("Scheduler incident evidence identifies a different attempt")
        candidate = _redacted(scheduler)
        size = len(E.canonical(candidate))
        if size <= remaining:
            scheduler_doc, remaining = candidate, remaining - size
        else:
            problem("scheduler", "Scheduler evidence exceeds the remaining incident byte budget")

    retained, markers, source_docs, prepared_sources = [], [], {}, {}
    omitted, other_clocks, outside = 0, 0, 0
    query_digest = None
    query_partial = None
    source_errors = []
    if query is not None:
        query_raw = E.canonical(query)
        _, rows, sources = _bind(query, identity)
        from ..tracing.workspace_analysis_common import partial

        query_partial = partial(query)
        query_digest = hashlib.sha256(query_raw).hexdigest()
        query_errors = query.get("discovery_errors", [])
        if not isinstance(query_errors, (list, tuple)):
            raise ValueError("Recording discovery errors must be a bounded array")
        for error in query_errors[:16]:
            if isinstance(error, dict):
                source_errors.append(
                    {
                        "path": redact(str(error.get("path", "")))[:256],
                        "read_error": redact(
                            str(error.get("read_error", "Unknown retained source error"))
                        )[:512],
                    }
                )
        candidates = []
        marker_ids = {row["id"] for row in query.get("markers", [])}
        for row in rows.values():
            _cancel(cancel)
            if row["clock_id"] != trigger["clock_id"]:
                other_clocks += 1
            elif row["start_ns"] > end or row.get("end_ns", row["start_ns"]) < start:
                outside += 1
            else:
                distance = min(
                    abs(row["start_ns"] - trigger["time_ns"]),
                    abs(row.get("end_ns", row["start_ns"]) - trigger["time_ns"]),
                )
                candidates.append((distance, row["start_ns"], row["id"], row))
        for _, _, _, row in sorted(candidates, key=lambda item: item[:3]):
            _cancel(cancel)
            source_id = row["source_id"]
            if source_id not in prepared_sources:
                source = _redacted(sources[source_id])
                source["identity"] = copy.deepcopy(sources[source_id]["identity"])
                prepared_sources[source_id] = source, len(E.canonical(source))
            source, source_size = prepared_sources[source_id]
            candidate = _redacted(row)
            size = (
                len(E.canonical(candidate))
                + (source_size if source_id not in source_docs else 0)
                + 8
            )
            if len(retained) + len(markers) >= budget["max_events"] or size > remaining:
                omitted += 1
                continue
            remaining -= size
            source_docs[source_id] = source
            (markers if row["id"] in marker_ids else retained).append(candidate)
    sample_rows = []
    omitted_samples = 0
    for sample in samples:
        _cancel(cancel)
        if not isinstance(sample, dict) or trace_identity(sample.get("identity", {})) != identity:
            raise ValueError("Incident sample identifies a different attempt")
        E.text(sample.get("clock_id"), "sample clock", 1024)
        E.integer(sample.get("time_ns"), "sample timestamp", (1 << 64) - 1)
        candidate = _redacted(sample)
        size = len(E.canonical(candidate)) + 2
        if (
            sample["clock_id"] != trigger["clock_id"]
            or not start <= sample["time_ns"] <= end
            or size > remaining
        ):
            omitted_samples += 1
        else:
            sample_rows.append(candidate)
            remaining -= size
    log_rows, log_records = [], []
    for selection in logs:
        _cancel(cancel)
        E.keys(selection, {"path", "identity"}, label="incident log selection")
        if trace_identity(selection["identity"]) != identity:
            raise ValueError("Selected incident log identifies a different attempt")
        E.text(selection["path"], "incident log path", 4096)
        path = str(Path(selection["path"]).expanduser().absolute())
        limit = min(budget["max_log_bytes"], max(1, remaining // 2))
        try:
            record, raw = science_files.snapshot(path, cancel=cancel, limit=limit, keep=True)
        except (OSError, ValueError) as exc:
            _cancel(cancel)
            problem(path, exc)
            continue
        candidate = {
            "path": path,
            "original_sha256": record["sha256"],
            "original_bytes": record["size"],
            "text": redact(raw.decode("utf-8", errors="replace")),
            "scope": "Explicitly selected bounded file; chronology relative to trigger is unverified",
        }
        size = len(E.canonical(candidate)) + 2
        if size > remaining:
            problem(path, "Redacted log exceeds the remaining incident byte budget")
            continue
        remaining -= size
        log_rows.append(candidate)
        log_records.append(record)
    document = {
        "schema": "raven.incident-evidence/v1",
        "identity": identity,
        "trigger": _redacted(trigger),
        "window": {"clock_id": trigger["clock_id"], "start_ns": start, "end_ns": end},
        "budget": budget,
        "capture_mode": "retained evidence export; no sampler was started",
        "trace": {
            "identity": identity,
            "sources": list(source_docs.values()),
            "intervals": sorted(retained, key=lambda row: (row["start_ns"], row["id"])),
            "markers": sorted(markers, key=lambda row: (row["start_ns"], row["id"])),
            "truncated": bool(omitted or query_partial),
            "caught_up": query.get("caught_up") if query is not None else None,
        },
        "scheduler": scheduler_doc,
        "samples": sample_rows,
        "logs": log_rows,
        "coverage": {
            "retained_events": len(retained) + len(markers),
            "omitted_window_events": omitted,
            "other_clock_events": other_clocks,
            "outside_window_events": outside,
            "retained_samples": len(sample_rows),
            "omitted_samples": omitted_samples,
            "retained_logs": len(log_rows),
            "requested_logs": len(logs),
            "source_query_partial": query_partial,
            "source_query_digest": query_digest,
            "source_read_errors": source_errors,
            "omitted_source_errors": max(0, len(query.get("discovery_errors", [])) - 16)
            if query is not None
            else 0,
            "read_errors": errors,
        },
        "readiness": {
            "trace": "available" if retained or markers else "missing",
            "scheduler": "available" if scheduler_doc is not None else "missing",
            "samples": "available" if sample_rows else "missing",
            "logs": "available" if log_rows else "missing",
        },
        "limitations": [
            "The trigger records a supplied observation; correlated evidence does not establish its cause.",
            "The byte/event budgets bound this evidence export. They do not certify a runtime CPU-overhead percentage for a recording adapter.",
            "The trace window preserves original full intervals overlapping the window; inclusive duration can extend outside the selected window.",
            "Other clock domains are omitted rather than assigned an unverified cross-node alignment.",
            "No pre/post evidence exists unless recording began before the event. Missing sources are unknown, not healthy.",
            "Common credential forms are redacted. Review this private bundle before sharing; redaction is not proof that all secrets are absent.",
        ],
    }
    raw = E.canonical(document)
    digest = hashlib.sha256(raw).hexdigest()
    receipt = {
        "schema": "raven.incident-receipt/v1",
        "identity": identity,
        "files": [{"path": "incident.json", "sha256": digest, "bytes": len(raw)}],
        "budget_bytes": budget["max_bytes"],
    }
    # science_files.publish uses indented JSON for its completion receipt.
    import json

    receipt_bytes = len(
        (json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    )
    if len(raw) + receipt_bytes > budget["max_bytes"]:
        raise ValueError("Incident metadata exceeds the byte budget; use a larger explicit budget")

    def final_check():
        for record in log_records:
            science_files.verify(record, cancel=cancel)

    _cancel(cancel)
    published = science_files.publish(
        destination,
        files=[("incident.json", raw, False)],
        receipt=receipt,
        cancel=cancel,
        final_check=final_check,
    )
    return {
        "schema": "raven.incident-export/v1",
        "path": published,
        "identity": identity,
        "sha256": digest,
        "stored_bytes": len(raw) + receipt_bytes,
        "coverage": copy.deepcopy(document["coverage"]),
        "status": "saved with coverage limits"
        if omitted or other_clocks or omitted_samples or errors or source_errors or query_partial
        else "saved; cause remains unproved",
    }


def recorder(project, *, identity, budget=None):
    """Configure the existing opt-in rolling recorder; callers arm/trigger it.

    No events are sampled automatically. The recorder receipt reports actual
    eviction, post-window termination, quota errors, and qualification status.
    """
    from ..tracing.activity_trigger import TriggeredCapture

    identity = trace_identity(identity)
    budget = _budget(budget)
    if not budget["pre_seconds"] or not budget["post_seconds"]:
        raise ValueError("An armed rolling recorder needs positive pre/post durations")
    return TriggeredCapture(
        project,
        job_id=identity["job_id"],
        attempt=identity["attempt"],
        cluster=identity["cluster"],
        pre_seconds=budget["pre_seconds"],
        post_seconds=budget["post_seconds"],
        pre_events=min(budget["max_events"], 65536),
        post_events=budget["max_events"],
        pre_bytes=min(budget["max_bytes"] // 4, 64 << 20),
        max_bytes=budget["max_bytes"],
    )


def load(path, *, identity=None, cancel=None):
    """Reopen an immutable incident only after anchored integrity verification."""
    from .. import projects

    _cancel(cancel)
    expected = trace_identity(identity) if identity is not None else None
    root, fd = projects._root(str(path))
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Incident directory must remain private and user-owned")
        with os.scandir(fd) as entries:
            names = set()
            for entry in entries:
                names.add(entry.name)
                if len(names) > 2:
                    raise ValueError("Unexpected files or incomplete incident publication")
        if names != {"incident.json", ".raven-receipt.json"}:
            raise ValueError("Incident publication is incomplete or has unexpected files")
        for name in sorted(names):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise ValueError("Incident files must remain private user-owned regular files")
        receipt_raw, _ = projects._read(fd, ".raven-receipt.json", 65536)
        receipt = E.loads(receipt_raw)
        E.keys(receipt, {"schema", "identity", "files", "budget_bytes"}, label="incident receipt")
        if receipt["schema"] != "raven.incident-receipt/v1":
            raise ValueError("Unsupported incident receipt")
        binding = trace_identity(receipt["identity"])
        if expected is not None and binding != expected:
            raise ValueError("Incident identifies a different recording attempt")
        E.integer(receipt["budget_bytes"], "incident byte budget", 8 << 20)
        if not isinstance(receipt["files"], list) or len(receipt["files"]) != 1:
            raise ValueError("Incident receipt has an invalid file inventory")
        item = receipt["files"][0]
        E.keys(item, {"path", "sha256", "bytes"}, label="incident file")
        if (
            item["path"] != "incident.json"
            or not isinstance(item["sha256"], str)
            or not E.HASH.fullmatch(item["sha256"])
        ):
            raise ValueError("Invalid incident file identity")
        E.integer(item["bytes"], "incident file bytes", 8 << 20)
        raw, _ = projects._read(fd, "incident.json", item["bytes"])
        _cancel(cancel)
        if len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("Incident bytes do not match the completion receipt")
        if len(raw) + len(receipt_raw) > receipt["budget_bytes"]:
            raise ValueError("Incident exceeds its recorded byte budget")
        document = E.loads(raw)
        if (
            not isinstance(document, dict)
            or document.get("schema") != "raven.incident-evidence/v1"
            or trace_identity(document.get("identity", {})) != binding
        ):
            raise ValueError("Incident manifest and receipt identities disagree")
        if _budget(document.get("budget"))["max_bytes"] != receipt["budget_bytes"]:
            raise ValueError("Incident manifest and receipt budgets disagree")
        # Reading a report grants no scheduler or filesystem mutation authority.
        return {
            "schema": "raven.incident-inspection/v1",
            "path": str(root),
            "identity": binding,
            "document": document,
            "integrity": "Completion receipt and SHA256 match; source truth and cause remain unproved.",
        }
    finally:
        os.close(fd)
