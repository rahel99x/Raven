"""Bounded, evidence-based explanations for an explicitly selected job.

This module never opens files, invokes commands, or takes corrective actions.  The
caller supplies scheduler records and selected log excerpts.  Hypotheses describe
observations, not a proven root cause, and retain links back to their evidence.
"""
from __future__ import annotations

import itertools
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any


MAX_LOG_BYTES = 128 * 1024
MAX_LOG_LINES = 1200
MAX_EVIDENCE = 80
MAX_EVENTS = 200
MAX_EVENT_INPUT = 2000
MAX_EXCERPT_CHARS = 900
MAX_LOG_SOURCES = 32
MAX_TOTAL_LOG_BYTES = 1024 * 1024
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[ -/]*[@-~]")
_LOG_TIMESTAMP = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2}))(?!\d)")
_FAILURE_STATES = {"FAILED", "OUT_OF_MEMORY", "TIMEOUT", "NODE_FAIL", "BOOT_FAIL", "PREEMPTED", "CANCELLED", "DEADLINE"}
_RULES = (
    ("gpu_oom", re.compile(r"(?:CUDA|HIP|GPU|cuDNN).{0,80}(?:out[ -]of[ -]memory|memory allocation fail)|(?:torch\.cuda\.OutOfMemoryError|CUDA_ERROR_OUT_OF_MEMORY)", re.I)),
    ("host_oom", re.compile(r"oom[-_ ]kill|(?:memory|memcg) cgroup out of memory|exceeded (?:job )?memory limit|(?:slurmstepd|slurmd).{0,100}out of memory", re.I)),
    ("host_app_oom", re.compile(r"\bMemoryError\b|std::bad_alloc|cannot allocate memory", re.I)),
    ("timeout", re.compile(r"(?:DUE TO|exceeded).{0,40}(?:TIME LIMIT|wall[ -]?time)|job.{0,50}time limit", re.I)),
    ("quota", re.compile(r"disk quota exceeded|\bEDQUOT\b", re.I)),
    ("storage", re.compile(r"no space left on device|\bENOSPC\b", re.I)),
    ("permission", re.compile(r"permission denied|\bEACCES\b|operation not permitted", re.I)),
    ("application", re.compile(r"Traceback \(most recent call last\)|(?:^|\s)(?:[\w.]+(?:Error|Exception))(?::|\s)|segmentation fault|\bSIGSEGV\b|floating point exception", re.I)),
    ("communication", re.compile(r"NCCL.{0,120}(?:error|timed? out|unhandled|abort)|(?:MPI|PMIx|UCX).{0,120}(?:error|abort|connection|timed? out)|connection reset by peer|connection refused|collective.{0,40}timed? out", re.I)),
    ("node", re.compile(r"(?:node|slurmd).{0,60}(?:failure|unresponsive|not responding)|\bNODE_FAIL\b", re.I)),
    ("preempt", re.compile(r"\bpreempt(?:ed|ion)\b", re.I)),
    ("generic", re.compile(r"\b(?:failed|fatal|error)\b", re.I)),
)
_DESCRIPTIONS = {
    "gpu_oom": ("GPU memory exhaustion", "strong", ["Check the complete GPU exception and device memory measurements.", "Compare batch size, model/input size, and allocation with a comparable successful run; Slurm --mem changes host memory, not GPU capacity."]),
    "host_oom": ("Host memory limit or OOM termination", "strong", ["Inspect sacct job and step states and MaxRSS; sampled peaks can miss the final spike.", "Check the allocation's memory limit and cgroup OOM record before considering a host-memory request change."]),
    "host_app_oom": ("Application host-memory allocation failure", "moderate", ["Inspect the allocation exception, host memory limit, and process resource limits.", "Compare input size and peak memory with a successful run; this exception alone does not prove Slurm killed the job."]),
    "timeout": ("Wall-time limit reached", "strong", ["Compare elapsed time, Timelimit, and scheduler termination messages.", "Check the latest valid checkpoint and measured runtime before preparing a longer or resumed run."]),
    "quota": ("Storage quota exceeded", "strong", ["Check the quota for the exact output filesystem and user/project.", "Inspect which write failed and whether expected outputs are complete."]),
    "storage": ("Output filesystem capacity exhausted", "strong", ["Check free space and inode availability on the named filesystem.", "Validate partial outputs and application write errors before rerunning."]),
    "permission": ("Access or execution permission failure", "moderate", ["Inspect the path or operation named in the error and its ownership/permissions.", "Check the job's modules, mount availability, and execution identity."]),
    "application": ("Application exception or unsuccessful exit", "moderate", ["Read the first exception and its surrounding stack trace; wrapper exit codes can hide child failures.", "Compare script, environment, inputs, and parameters with a comparable successful run."]),
    "communication": ("Distributed communication failure", "moderate", ["Inspect the earliest rank-specific error and compare timestamps across selected rank logs.", "Check for a rank exception or node loss before attributing the failure to the network."]),
    "node": ("Node or launch infrastructure failure", "strong", ["Inspect scheduler reason, job/step states, and the named node.", "Compare rank/node logs and visible node state; ask site support about infrastructure events if necessary."]),
    "preempt": ("Scheduler preemption", "strong", ["Inspect the scheduler preemption state/reason and applicable partition/QOS policy.", "Check the most recent application-validated checkpoint and the job's requeue policy."]),
    "cancel": ("Job cancellation or deadline termination", "strong", ["Inspect sacct state/reason and the recorded cancellation/deadline event.", "Check whether the application wrote a complete result or valid checkpoint before stopping."]),
    "signal": ("Process terminated by a signal", "strong", ["Inspect the scheduler's exit-code:signal value and individual job steps.", "Find the preceding application/scheduler message; a signal identifies how the process stopped, not why."]),
    "generic": ("Unclassified error or failure message", "weak", ["Read surrounding lines to identify the failing component and whether recovery followed.", "Compare scheduler job/step status and exit codes with application output validation."]),
    "artifacts": ("Declared output validation failed", "strong", ["Inspect the failing output contract and the exact declared path.", "Check producer logs and validate whether the output is absent, partial, or malformed."]),
    "artifact_unverified": ("Declared output inspection is incomplete", "weak", ["Inspect output-check errors, file stability, and the available validation budget.", "Repeat bounded validation when the output is stable and accessible; an unavailable check does not establish an invalid result."]),
    "pressure": ("High recorded host-memory pressure", "weak", ["Compare the measured peak with the allocation limit and job step records.", "Inspect explicit allocation/OOM errors; a high sampled peak alone does not establish a memory failure."]),
}


def sanitize_text(value: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    """Remove terminal escapes and invisible controls, preserving readable Unicode."""
    if not isinstance(value, str):
        return ""
    value = _ANSI.sub("", value[:max(0, limit)])
    return "".join("    " if c == "\t" else c for c in value
                   if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf", "Cs"})


def _field(record: Any, *keys: str) -> Any:
    for key in keys:
        try:
            value = record.get(key) if isinstance(record, Mapping) else getattr(record, key, None)
        except (AttributeError, TypeError, ValueError, OverflowError):
            # Typed model properties can encounter malformed fixture/replay data.
            continue
        if value is not None:
            return value
    return None


def _text(value: Any, limit: int = MAX_EXCERPT_CHARS) -> str:
    if isinstance(value, str):
        return sanitize_text(value, limit)
    if isinstance(value, int) and not isinstance(value, bool):
        if value.bit_length() > 4096:
            return ""
        return str(value)[:limit]
    return ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _timestamp(value: Any) -> str | float | int | None:
    if isinstance(value, str):
        return sanitize_text(value, 80) or None
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _epoch(value: Any) -> float | None:
    """Order comparable timestamps without guessing a Slurm site's timezone."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except OverflowError:
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _log_excerpt(value: Any, source: str) -> tuple[list[str], dict]:
    """Bound before decoding/splitting; unknown offsets remain explicitly relative."""
    meta = value if isinstance(value, Mapping) else {}
    text = meta.get("text", "") if meta else value
    if text is None:
        text = ""
    if not isinstance(text, (str, bytes)):
        raise TypeError(f"{source} must be text, bytes, or a mapping containing text")
    first_line = meta.get("first_line")
    if not isinstance(first_line, int) or isinstance(first_line, bool) or not 1 <= first_line <= 10 ** 12:
        first_line = None
    path = _text(meta.get("path"), 4096)
    truncated = meta.get("truncated") is True
    # A UTF-8 character occupies at least one byte: taking a bounded suffix first
    # avoids allocating an encoded copy of an arbitrarily large caller string.
    char_cut = len(text) > MAX_LOG_BYTES
    bounded = text[-MAX_LOG_BYTES:] if char_cut else text
    raw = bounded.encode("utf-8", "replace") if isinstance(bounded, str) else bounded
    byte_cut = len(raw) > MAX_LOG_BYTES
    raw = raw[-MAX_LOG_BYTES:]
    if char_cut or byte_cut:
        first_line = None
        truncated = True
        newline = raw.find(b"\n")
        if newline >= 0:
            raw = raw[newline + 1:]
    decoded = raw.decode("utf-8", "replace")
    parts = decoded.rsplit("\n", MAX_LOG_LINES)
    if len(parts) > MAX_LOG_LINES:
        skipped = parts.pop(0)
        if first_line is not None:
            first_line += skipped.count("\n") + 1
        truncated = True
    if parts and parts[-1] == "":
        parts.pop()
    identity = meta.get("file_identity")
    if not isinstance(identity, Mapping):
        identity = None
    else:
        identity = {key: list(value) if isinstance(value, tuple) else value
                    for key, value in identity.items() if key in {"size", "ident", "updated"}
                    and (type(value) is int or isinstance(value, (list, tuple))
                         and len(value) <= 6 and all(type(item) is int or isinstance(item, str) and len(item) <= 200 for item in value))}
    return parts, {"source": source, "path": path, "first_line": first_line,
                   "line_basis": "original" if first_line is not None else "tail-relative",
                   "truncated": truncated, "bytes_examined": len(raw), "lines_examined": len(parts),
                   "present": bool(raw), "file_identity": identity,
                   "label": _text(meta.get("label"), 160), "group": _text(meta.get("group"), 160)}


def investigate(job: Any, *, details: Mapping | None = None, live: Any = None,
                events: Iterable = (), stdout: Any = "", stderr: Any = "",
                artifact_results: Any = None, logs: Iterable = ()) -> dict:
    """Build a JSON-safe investigation with ranked, evidence-linked hypotheses.

    ``job`` accepts Raven Job/Finished records or a mapping with ``id``/``JobId``.
    Log arguments accept text/bytes or ``{text, path, first_line, truncated}``.
    ``first_line`` is the 1-based original line number of the supplied excerpt.
    Without it, locations explicitly use tail-relative numbering.  Only events
    scoped to this job (or its dot-separated steps) contribute evidence.  Empty
    logs mean no supplied log evidence; they never establish application health.
    """
    if isinstance(job, (str, bytes, list, tuple, int, float, bool)) or job is None:
        raise TypeError("job must be a job record or mapping")
    raw_id = _field(job, "id", "JobId", "JobID", "job_id")
    if isinstance(raw_id, str) and (len(raw_id) > 128 or sanitize_text(raw_id, 128) != raw_id):
        raise ValueError("job id must be bounded and contain no terminal controls")
    job_id = _text(raw_id, 128).strip()
    if not job_id:
        raise ValueError("job must have a nonempty id")
    if details is not None and not isinstance(details, Mapping):
        raise TypeError("details must be a mapping or None")
    if events is None or isinstance(events, (str, bytes, Mapping)) or not isinstance(events, Iterable):
        raise TypeError("events must be an iterable of event records")
    details = details or {}
    evidence: list[dict] = []
    chronology: list[dict] = []
    support: dict[str, list[str]] = {}
    limitations: list[str] = []
    # An unrelated details result must not contaminate the selected case.
    detail_id = _text(_field(details, "JobId", "JobID", "id"), 128)
    if detail_id and detail_id != job_id:
        limitations.append("Scheduler details belong to another job and were ignored.")
        details = {}
    raw_state = _text(_field(job, "state", "JobState", "State"), 120) or _text(_field(details, "JobState", "State", "state"), 120)
    state = raw_state.upper().split(" ", 1)[0].rstrip("+") or "UNKNOWN"
    completed = state == "COMPLETED"
    failed = state in _FAILURE_STATES

    def add(source: str, location: str, text: str, *, kind: str = "observation", timestamp: Any = None, **extra: Any) -> str | None:
        if len(evidence) >= MAX_EVIDENCE:
            return None
        eid = f"E{len(evidence) + 1}"
        item = {"id": eid, "source": source, "location": sanitize_text(location, 700),
                "text": sanitize_text(text), "kind": kind, **extra}
        stamp = _timestamp(timestamp)
        if stamp is not None:
            item["timestamp"] = stamp
            chronology.append({"evidence_id": eid, "timestamp": stamp, "source": source})
        evidence.append(item)
        return eid

    def link(key: str, eid: str | None) -> None:
        if eid is not None and len(support.setdefault(key, [])) < 6:
            support[key].append(eid)

    state_id = add("scheduler", f"job {job_id} state", f"Slurm state: {raw_state or 'unknown'}", timestamp=_field(job, "end", "EndTime"))
    state_rules = {"OUT_OF_MEMORY": "host_oom", "TIMEOUT": "timeout", "DEADLINE": "cancel",
                   "NODE_FAIL": "node", "BOOT_FAIL": "node", "PREEMPTED": "preempt", "CANCELLED": "cancel", "FAILED": "application"}
    if state in state_rules:
        link(state_rules[state], state_id)
    reason = _text(_field(details, "Reason", "reason") or _field(job, "reason"), 500)
    if reason and reason not in {"None", "NONE", "(null)"}:
        reason_id = add("scheduler", f"job {job_id} reason", f"Scheduler reason: {reason}")
        for key, pattern in _RULES:
            if pattern.search(reason):
                link(key, reason_id)

    exit_text = _text(_field(job, "exit", "ExitCode", "exit_code") or _field(details, "ExitCode", "exit_code"), 80)
    exit_id = None
    if exit_text:
        exit_id = add("scheduler", f"job {job_id} ExitCode", f"ExitCode: {exit_text}")
        match = re.fullmatch(r"(\d{1,8})(?::(\d{1,5}))?", exit_text.strip())
        if match:
            code, signal = int(match[1]), int(match[2] or 0)
            if signal:
                link("signal", exit_id)
            if code:
                link("application", exit_id)
        else:
            limitations.append("The supplied exit code is not a recognized Slurm code:signal value.")
    detail_state = _text(_field(details, "JobState", "State", "state"), 120)
    detail_state_id = None
    if detail_state and detail_state.upper().split(" ", 1)[0].rstrip("+") != state:
        detail_state_id = add("scheduler", f"job {job_id} details state", f"Details state differs from selected record: {detail_state}")
        limitations.append("Scheduler records disagree on state; verify their timestamps and refresh the selected job.")

    # Evidence order prioritizes scheduler records, then the earliest recognized
    # error in the selected stderr excerpt, then stdout, then event history.
    log_sources = []
    first_error = None
    if isinstance(logs, (str, bytes, Mapping)) or not isinstance(logs, Iterable):
        raise TypeError("logs must be an iterable of excerpt records")
    supplied_logs = [("stderr", stderr), ("stdout", stdout)]
    additional = list(itertools.islice(logs, MAX_LOG_SOURCES + 1))
    if len(additional) > MAX_LOG_SOURCES:
        limitations.append(f"Additional log inspection reached the {MAX_LOG_SOURCES}-source limit; remaining sources were omitted.")
    for index, value in enumerate(additional[:MAX_LOG_SOURCES]):
        if not isinstance(value, Mapping):
            raise TypeError("additional logs must contain excerpt mappings")
        supplied_logs.append((_text(value.get("source"), 160) or f"log:{index + 1}", value))
    log_bytes = 0
    for source, supplied in supplied_logs:
        if log_bytes >= MAX_TOTAL_LOG_BYTES:
            limitations.append("Combined log inspection reached its 1 MiB budget; remaining sources were omitted.")
            break
        lines, meta = _log_excerpt(supplied, source)
        remaining = MAX_TOTAL_LOG_BYTES - log_bytes
        if meta["bytes_examined"] > remaining:
            # Keep the budget exact without attributing a shortened window to
            # its former original line numbers.
            raw = "\n".join(lines).encode("utf-8", "replace")[-remaining:]
            newline = raw.find(b"\n")
            if newline >= 0:
                raw = raw[newline + 1:]
            lines, meta = _log_excerpt(dict(text=raw, path=meta["path"], truncated=True,
                                           file_identity=meta["file_identity"], label=meta["label"], group=meta["group"]), source)
        log_bytes += meta["bytes_examined"]
        log_sources.append(meta)
        if not meta["present"]:
            limitations.append(f"No {source} excerpt was supplied; application health is unverified.")
        if meta["truncated"]:
            limitations.append(f"Only a bounded {source} tail was examined; earlier errors may be absent.")
        for index, raw_line in enumerate(lines):
            # Examine the already byte-bounded line, then retain context around
            # its first recognized error. Long JSON/debug prefixes must not
            # hide a real exception beyond the display excerpt length.
            line = sanitize_text(raw_line, MAX_LOG_BYTES)
            matches = [(key, found) for key, pattern in _RULES if (found := pattern.search(line))]
            keys = [key for key, _ in matches]
            if not keys:
                continue
            # Specific exception rules supersede a bare generic 'error' token.
            if len(keys) > 1 and "generic" in keys:
                keys.remove("generic")
            number = meta["first_line"] + index if meta["first_line"] is not None else index + 1
            location = f"{meta['path'] or source}:{number}" if meta["line_basis"] == "original" else f"{meta['path'] or source} (tail-relative line {number})"
            start = max(0, min(found.start() for key, found in matches if key in keys) - 120)
            excerpt = ("... " if start else "") + line[start:start + MAX_EXCERPT_CHARS - (4 if start else 0)]
            stamp_match = _LOG_TIMESTAMP.search(line[:160])
            log_stamp = _epoch(stamp_match.group(1)) if stamp_match else None
            timestamp_fields = {"timestamp": log_stamp, "t": log_stamp} if log_stamp is not None else {}
            eid = add(source, location, excerpt, line=number, line_basis=meta["line_basis"], excerpt_offset=start,
                      path=meta["path"], file_identity=meta["file_identity"],
                      excerpt_line=line[:MAX_EXCERPT_CHARS], first_line=meta["first_line"],
                      tail_distance=len(lines) - index - 1, job=job_id, **timestamp_fields)
            if eid is None:
                break
            if first_error is None:
                first_error = eid
            for key in keys:
                link(key, eid)

    # Reverse bounded event windows when the container supports it.  A generator
    # is consumed at most MAX_EVENT_INPUT times, including nonmatching records.
    try:
        window = list(itertools.islice(reversed(events), MAX_EVENT_INPUT))
        window.reverse()
    except TypeError:
        window = list(itertools.islice(iter(events), MAX_EVENT_INPUT))
    selected_events = []
    for event in window:
        if not isinstance(event, Mapping):
            continue
        scoped = _text(_field(event, "job", "job_id", "JobId", "JobID"), 128)
        if scoped == job_id or scoped.startswith(job_id + "."):
            selected_events.append(event)
    for event in selected_events[-MAX_EVENTS:]:
        text = _text(_field(event, "text", "message"))
        if not text:
            continue
        kind = _text(_field(event, "kind", "type"), 100) or "event"
        eid = add("event", f"job {job_id} event ({kind})", text, timestamp=_field(event, "t", "timestamp", "time"))
        for key, pattern in _RULES:
            if pattern.search(text):
                link(key, eid)

    rss = _number(_field(live, "rss", "MaxRSS") if live is not None else _field(job, "rss", "MaxRSS"))
    requested = _number(_field(job, "req_mem", "mem_bytes", "memory_limit_bytes"))
    if rss is not None and requested and rss / requested >= 0.9:
        eid = add("metrics", f"job {job_id} sampled memory", f"Recorded host-memory peak {rss:.0f} bytes; requested {requested:.0f} bytes ({rss / requested:.0%}).")
        link("pressure", eid)

    artifact_meta = artifact_results if isinstance(artifact_results, Mapping) else {}
    results = artifact_meta.get("outputs", artifact_meta.get("results", [])) if artifact_meta else artifact_results
    if isinstance(results, (list, tuple)):
        for result in results[:100]:
            if not isinstance(result, Mapping):
                continue
            result_job = _text(_field(result, "job_id", "job"), 128)
            if result_job and result_job != job_id:
                continue
            status = _text(result.get("status"), 80).lower()
            if status in {"not_checked", "incomplete", "error"}:
                path = _text(_field(result, "path", "name"), 500) or "declared output"
                message = _text(_field(result, "message", "error", "reason")) or f"Output could not be fully checked (status: {status})."
                eid = add("artifact", path, message, timestamp=artifact_meta.get("observed_at"))
                link("artifact_unverified", eid)
            elif result.get("ok") is False or result.get("valid") is False or status in {"missing", "invalid", "failed", "mismatch"}:
                path = _text(_field(result, "path", "name"), 500) or "declared output"
                checks = result.get("checks", [])
                failures = [c for c in checks[:20] if isinstance(c, Mapping) and c.get("status") == "fail"] if isinstance(checks, (list, tuple)) else []
                message = _text(_field(result, "message", "error", "reason"))
                if not message:
                    message = "; ".join(_text(c.get("message"), 300) for c in failures[:3]) or f"Output validation status: {status or 'failed'}"
                eid = add("artifact", path, message, timestamp=artifact_meta.get("observed_at"))
                link("artifacts", eid)
    artifact_status = _text(artifact_meta.get("status"), 80).lower()
    outputs_verified = artifact_status == "valid" and artifact_meta.get("valid") is True and not (support.get("artifacts") or support.get("artifact_unverified"))
    if outputs_verified:
        add("artifact", "declared output validation", _text(artifact_meta.get("summary")) or "Declared output contract checks passed; scientific correctness is not established.", timestamp=artifact_meta.get("observed_at"))
    if artifact_status in {"incomplete", "error"} and not support.get("artifact_unverified"):
        eid = add("artifact", "declared output validation", _text(artifact_meta.get("summary")) or f"Output inspection is {artifact_status}.", timestamp=artifact_meta.get("observed_at"))
        link("artifact_unverified", eid)

    hypotheses = []
    for key, ids in support.items():
        name, confidence, next_checks = _DESCRIPTIONS[key]
        contradictions = []
        if completed and key not in {"artifacts", "artifact_unverified", "pressure"}:
            if state_id:
                contradictions.append(state_id)
            confidence = "weak"
        if detail_state_id:
            contradictions.append(detail_state_id)
        hypotheses.append({"key": key, "name": name, "confidence": confidence,
                           "support": ids, "contradictions": contradictions,
                           "next_checks": list(next_checks),
                           "interpretation": "Historical or recovered log warning; the selected job is completed." if completed and key not in {"artifacts", "artifact_unverified", "pressure"} else "Evidence suggests this explanation; causal order and the underlying cause require confirmation."})
    order = {"strong": 0, "moderate": 1, "weak": 2}
    hypotheses.sort(key=lambda h: (order[h["confidence"]], h["key"] == "generic", -len(h["support"])))
    if not hypotheses:
        hypotheses.append({"key": "unknown", "name": "Insufficient failure evidence", "confidence": "weak",
                           "support": [], "contradictions": [],
                           "next_checks": ["Retrieve the selected job's sacct job and step states and exit codes.", "Inspect explicitly selected stderr/stdout and validate the expected application outputs."],
                           "interpretation": "Missing error evidence does not establish a healthy application."})
    if failed:
        status = "failure"
        summary = f"Slurm reports {state}. {hypotheses[0]['name']}; verify the linked evidence before changing or rerunning the job."
    elif completed:
        status = "warning" if support else "completed"
        summary = "Slurm reports COMPLETED. " + ("Selected evidence contains warnings or output-validation failures; these do not change the recorded scheduler state." if support else "Declared output contract checks passed; scientific correctness remains unverified." if outputs_verified else "No failure was identified in the supplied evidence; application outputs remain unverified.")
    elif support:
        status = "warning"
        summary = f"Selected job state is {state}. {hypotheses[0]['name']} is a candidate explanation, not a confirmed terminal failure."
    else:
        status = "unknown"
        summary = f"Selected job state is {state}; there is insufficient supplied evidence to explain a failure."
    if len(evidence) >= MAX_EVIDENCE:
        limitations.append("Evidence count reached its bound; review full selected logs for additional context.")
    limitations.append("Log excerpts can contain messages from recovered errors or previous attempts; chronology must be confirmed before assigning a root cause.")
    # Preserve the timestamp itself and evidence identity.  Naive Slurm times
    # cannot safely be compared with epochs without knowing the site's zone.
    for entry in chronology:
        entry["order_basis"] = "timestamp" if _epoch(entry["timestamp"]) is not None else "source-order"
    chronology.sort(key=lambda entry: (_epoch(entry["timestamp"]) is None, _epoch(entry["timestamp"]) or 0))
    return {"job_id": job_id, "state": state, "status": status, "summary": summary,
            "evidence": evidence, "hypotheses": hypotheses, "chronology": chronology,
            "outputs_verified": outputs_verified,
            "chronology_order": "Comparable epoch/explicit-timezone timestamps ascending; other timestamps retain source order.",
            "first_error": first_error, "log_sources": log_sources, "limitations": limitations}
