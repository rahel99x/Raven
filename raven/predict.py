"""Bounded, evidence-backed predictions for repeated, compatible completed runs.

No allocation is changed here.  A split-conformal interval describes the next
observation under exchangeability, not a confidence interval for the mean and
not a guarantee that a requested limit is sufficient.  Censored failures and
ambiguous workload identities cause abstention rather than optimistic pooling.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from statistics import median

from .model import Finished, Job, mem_request_bytes

_METRICS = {"runtime_seconds": "seconds", "memory_bytes": "bytes", "cpu_seconds": "CPU seconds"}
_CORE = ("name", "partition", "cpus", "nodes", "gpus")
_OPTIONAL = ("gpu_type", "account", "qos", "mem_bytes", "time_seconds", "script_sha256", "input_size", "parameters")
_SCOPES = {"job_peak", "per_node_peak", "max_task_rss"}
_FIELDS = (*_CORE, *_OPTIONAL, "id", "state", "elapsed", "runtime_seconds", "cpu_time",
           "cpu_seconds", "rss", "max_rss", "memory_bytes", "memory_scope", "start", "end",
           "submit", "exit", "req_mem", "mem_req", "limit")
_ABSENT = object()


def _text(value, *, maximum=256, empty=True):
    if not isinstance(value, str) or len(value) > maximum or any(not c.isprintable() for c in value):
        raise ValueError("invalid text field")
    if not empty and not value:
        raise ValueError("missing identity field")
    return value


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(result) or result < 0 or result > 1e100 or (positive and result == 0):
        return None
    return result


def _count(value, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, int) or not (0 if zero else 1) <= value <= 10**9:
        raise ValueError("invalid allocation count")
    return value


def _parameters(value):
    """Canonicalize a small JSON parameter tree without walking unbounded data."""
    count = [0]

    def walk(item, depth):
        count[0] += 1
        if depth > 4 or count[0] > 128:
            raise ValueError("parameters exceed depth or entry budget")
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, str):
            return _text(item)
        if isinstance(item, (int, float)):
            numeric = _number(item)
            if numeric is None:
                # Negative parameters are valid, unlike measured resources.
                if isinstance(item, bool) or not math.isfinite(float(item)) or abs(item) > 1e100:
                    raise ValueError("invalid numeric parameter")
            return item
        if isinstance(item, list) and len(item) <= 64:
            return [walk(x, depth + 1) for x in item]
        if isinstance(item, Mapping) and len(item) <= 64:
            return {_text(k, maximum=64, empty=False): walk(v, depth + 1) for k, v in item.items()}
        raise ValueError("parameters must be bounded JSON data")

    if not isinstance(value, Mapping):
        raise ValueError("parameters must be an object")
    return json.dumps(walk(value, 0), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _mapping(value):
    if isinstance(value, Mapping):
        data = {key: value[key] for key in _FIELDS if key in value}
    elif isinstance(value, (Finished, Job)):
        data = {key: getattr(value, key) for key in _FIELDS if hasattr(value, key)}
        if isinstance(value, Job) and data.get("mem_bytes") == 0:
            # The model's zero default means unknown request, not zero memory.
            data.pop("mem_bytes", None)
    else:
        raise ValueError("record is not a job or mapping")
    if "mem_bytes" not in data:
        requested = _number(data.get("req_mem"), positive=True)
        if requested is None and isinstance(data.get("mem_req"), str) and len(data["mem_req"]) <= 64:
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?[KMGT]?[cn]?", data["mem_req"], re.IGNORECASE):
                requested = _number(mem_request_bytes(data["mem_req"], data.get("cpus", 0), data.get("nodes", 1)), positive=True)
        if requested is not None:
            data["mem_bytes"] = requested
    if "time_seconds" not in data and data.get("limit"):
        limit = _duration(data["limit"])
        if limit is not None and limit > 0:
            data["time_seconds"] = limit
    return data


def _identity(data, *, query=False):
    result = {}
    for key in _CORE:
        if key not in data:
            if query:
                continue
            raise ValueError("missing allocation identity")
        result[key] = _text(data[key], empty=False) if key in ("name", "partition") else _count(data[key], zero=key == "gpus")
    for key in _OPTIONAL:
        if key not in data or data[key] is None or data[key] == "":
            continue
        value = data[key]
        if key == "parameters":
            result[key] = _parameters(value)
        elif key in ("input_size", "mem_bytes", "time_seconds"):
            if _number(value) is None:
                raise ValueError("invalid workload size or allocation request")
            # Preserve exact integer sizes; avoid merging large integers through float conversion.
            result[key] = int(value) if isinstance(value, float) and value.is_integer() else value
        elif key == "script_sha256":
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
                raise ValueError("invalid script identity")
            result[key] = value.lower()
        else:
            result[key] = _text(value, maximum=128)
    return result


def _duration(value):
    if not isinstance(value, str):
        return _number(value)
    if len(value) > 64 or not re.fullmatch(r"(?:[0-9]+-)?[0-9]+(?::[0-9]+){0,2}(?:\.[0-9]+)?", value):
        return None
    days, _, rest = value.partition("-")
    if not rest:
        rest, days = days, "0"
    fields = rest.split(":")
    if len(fields) > 1 and any(float(x) >= 60 for x in fields[1:]):
        return None
    try:
        number = float(days) * 86400
        for i, part in enumerate(reversed(fields)):
            number += float(part) * 60**i
        return _number(number)
    except (ValueError, OverflowError):
        return None


def _timestamp(value):
    if isinstance(value, (int, float)):
        return _number(value)
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _record(value, position):
    data = _mapping(value)
    identity = _identity(data)
    state = _text(data.get("state", ""), maximum=64).split(" ", 1)[0].rstrip("+").upper()
    ineligible = state not in {"COMPLETED", "TIMEOUT", "OUT_OF_MEMORY"}
    if state == "COMPLETED" and data.get("exit") not in (None, "", "0", "0:0"):
        ineligible = True
    identity_id = data.get("id")
    if identity_id is not None:
        identity_id = _text(identity_id, maximum=160, empty=False)
        if re.fullmatch(r"[0-9]+(?:_[0-9]+)?\..+", identity_id):
            raise ValueError("job_step_not_independent_run")
    values = {
        "runtime_seconds": _number(data["runtime_seconds"]) if "runtime_seconds" in data else _duration(data.get("elapsed")),
        "cpu_seconds": _number(data["cpu_seconds"]) if "cpu_seconds" in data else _number(data.get("cpu_time")),
    }
    memory_scope = data.get("memory_scope")
    if "memory_bytes" in data:
        values["memory_bytes"] = _number(data["memory_bytes"])
        memory_scope = memory_scope if isinstance(memory_scope, str) and memory_scope in _SCOPES else None
    else:
        values["memory_bytes"] = _number(data.get("max_rss", data.get("rss")), positive=True)
        memory_scope = "max_task_rss" if values["memory_bytes"] is not None else None
    end = _timestamp(data.get("end"))
    if values["runtime_seconds"] is None and "runtime_seconds" not in data:
        start = _timestamp(data.get("start"))
        if end is not None and start is not None and end >= start:
            values["runtime_seconds"] = _number(end - start)
    if identity_id is None:
        digest = json.dumps([identity, state, values, memory_scope, end], sort_keys=True, separators=(",", ":"), allow_nan=False)
        identity_id = "anonymous:" + hashlib.sha256(digest.encode()).hexdigest()[:24]
    return {"id": identity_id, "identity": identity, "state": state, "values": values,
            "scope": memory_scope, "end": end, "position": position, "ineligible": ineligible}


def _empty_metric(unit):
    return {"estimate": None, "lower": None, "upper": None, "samples": 0,
            "method": "split_conformal_median", "calibration_samples": 0,
            "observed_coverage": None, "validation_samples": 0, "unit": unit,
            "status": "insufficient", "lower_bound": None, "censored_samples": 0}


def _fit(values, coverage, min_samples):
    result = _empty_metric("")
    result["samples"] = len(values)
    if len(values) < min_samples:
        return result
    # A genuine future holdout is separate from calibration.  With small cohorts
    # all available data serves training/calibration and coverage stays unknown.
    n_validation = len(values) // 5 if len(values) >= 20 else 0
    development = values[:-n_validation] if n_validation else values
    n_train = len(development) // 2
    training, calibration = development[:n_train], development[n_train:]
    estimate = float(median(training))
    rank = math.ceil((len(calibration) + 1) * coverage)
    result.update(estimate=estimate, training_samples=len(training), calibration_samples=len(calibration))
    if rank > len(calibration):
        result["status"] = "insufficient_calibration"
        return result
    radius = sorted(abs(x - estimate) for x in calibration)[rank - 1]
    lower, upper = max(0.0, estimate - radius), estimate + radius
    result.update(lower=lower, upper=upper, status="ok", calibration_rank=rank,
                  target_coverage=coverage, validation_samples=n_validation)
    if n_validation:
        validation = values[-n_validation:]
        result["observed_coverage"] = sum(lower <= x <= upper for x in validation) / n_validation
        if result["observed_coverage"] < coverage:
            result["status"] = "poor_validation"
    return result


def predict(history, query=None, *, coverage=.8, min_samples=10, max_records=10000):
    """Predict measurements for one exact workload/allocation cohort.

    ``memory_bytes`` mappings require ``memory_scope``.  ``rss``/``max_rss`` and
    :class:`Finished` account for maximum task RSS, never total job memory.
    Unspecified optional workload identities can only resolve when unambiguous.
    The returned dictionaries contain JSON data only; unknown values are null.
    """
    result = {"status": "insufficient", "cohort": None,
              "metrics": {key: _empty_metric(unit) for key, unit in _METRICS.items()},
              "evidence": [], "censored": [], "exclusions": {}, "limitations": [],
              "records_considered": 0, "truncated": False, "target_coverage": None,
              "coverage_assumption": "Exchangeable future runs within the exact cohort; changes and temporal drift can invalidate coverage."}
    if (_number(coverage, positive=True) is None or coverage >= 1
            or isinstance(min_samples, bool) or not isinstance(min_samples, int) or not 2 <= min_samples <= 100000
            or isinstance(max_records, bool) or not isinstance(max_records, int) or not 1 <= max_records <= 100000):
        result.update(status="error", limitations=["Invalid coverage, sample count, or record budget."])
        return result
    result["target_coverage"] = float(coverage)
    try:
        query_data = _mapping(query) if query is not None else {}
        wanted = _identity(query_data, query=True)
        wanted_scope = query_data.get("memory_scope")
        if wanted_scope is not None and (not isinstance(wanted_scope, str) or wanted_scope not in _SCOPES):
            raise ValueError("invalid query memory scope")
        if not isinstance(history, (list, tuple)) and isinstance(history, (str, bytes, Mapping)):
            raise ValueError("history must be an iterable of records")
        source = iter(history)
    except (TypeError, ValueError, OverflowError):
        result.update(status="error", limitations=["Invalid query identity or history."])
        return result
    exclusions = Counter()
    unproven = Counter()
    unique = {}
    try:
        for position in range(max_records):
            try:
                value = next(source)
            except StopIteration:
                break
            result["records_considered"] += 1
            try:
                row = _record(value, position)
            except (ValueError, TypeError, OverflowError, RecursionError):
                exclusions["invalid_or_ineligible_record"] += 1
                continue
            if any(row["identity"].get(key, _ABSENT) != value for key, value in wanted.items()):
                exclusions["different_workload_or_allocation"] += 1
                unproven.update(key for key in wanted if key not in row["identity"])
                continue
            prior = unique.get(row["id"])
            if prior is not None:
                exclusions["duplicate_id"] += 1
                # Prefer dated latest evidence; conflicting undated rows are
                # quarantined rather than cherry-picked by ingestion order.
                if row == prior or (row["identity"] == prior["identity"] and row["state"] == prior["state"]
                                    and row["values"] == prior["values"] and row["scope"] == prior["scope"] and row["end"] == prior["end"]
                                    and row["ineligible"] == prior["ineligible"]):
                    continue
                if row["end"] is not None and prior["end"] is not None and row["end"] != prior["end"]:
                    if row["end"] > prior["end"]:
                        unique[row["id"]] = row
                else:
                    unique[row["id"]] = {**prior, "conflict": True}
                continue
            unique[row["id"]] = row
        else:
            # Do not consume an extra record from potentially expensive sources.
            result["truncated"] = not isinstance(history, (list, tuple)) or len(history) > max_records
    except Exception:
        result.update(status="error", limitations=["History iteration failed; no partial prediction was published."])
        return result
    rows = [row for row in unique.values() if not row.get("conflict") and not row["ineligible"]]
    exclusions["invalid_or_ineligible_record"] += sum(row["ineligible"] for row in unique.values())
    exclusions["conflicting_duplicate_id"] += sum(bool(row.get("conflict")) for row in unique.values())
    result["exclusions"] = dict(exclusions)
    if unproven:
        result["unproven_query_fields"] = dict(unproven)
        result["limitations"].append("Some history records do not prove the requested identity fields: " + ", ".join(sorted(unproven)) + ".")
    if exclusions["conflicting_duplicate_id"]:
        result["limitations"].append("Conflicting undated or equally dated observations share a run ID; resolve their provenance before predicting.")
        return result
    signatures = {}
    for row in rows:
        key = json.dumps(row["identity"], sort_keys=True, separators=(",", ":"), allow_nan=False)
        signatures[key] = row["identity"]
    if len(signatures) != 1:
        result["limitations"].append("No unique compatible cohort; specify name, partition, allocation and any differing workload identities.")
        result["available_cohorts"] = list(signatures.values())[:32]
        result["cohorts_found"] = len(signatures)
        return result
    result["cohort"] = next(iter(signatures.values())).copy()
    if wanted_scope is not None:
        result["cohort"]["memory_scope"] = wanted_scope
    if "parameters" in result["cohort"]:
        result["cohort"]["parameters"] = json.loads(result["cohort"]["parameters"])
    if "script_sha256" not in result["cohort"]:
        result["limitations"].append("Script identity was not captured; workload equivalence relies on the recorded name and allocation.")
    if "input_size" not in result["cohort"] and "parameters" not in result["cohort"]:
        result["limitations"].append("Input size and parameters were not captured; changed scientific inputs can invalidate predictions.")
    if result["cohort"].get("gpus") and "gpu_type" not in result["cohort"]:
        result["limitations"].append("GPU type was not captured; partition and GPU count alone do not establish identical hardware.")
    chronological = all(row["end"] is not None for row in rows)
    rows.sort(key=lambda row: (row["end"], row["id"]) if chronological else (row["position"], row["id"]))
    result["ordering"] = "end_time" if chronological else "input_order"
    if not chronological:
        result["limitations"].append("Missing end times: train/calibration/holdout follow input order, not verified chronology.")
    if result["truncated"]:
        result["limitations"].append("History exceeded the record budget; this is a bounded subset.")
    result["evidence"] = [row["id"] for row in rows][:512]
    result["evidence_count"] = len(rows)
    result["evidence_truncated"] = len(rows) > 512
    censored = [row for row in rows if row["state"] != "COMPLETED"]
    result["censored"] = [{"id": row["id"], "state": row["state"], "lower_bounds": row["values"],
                           "memory_scope": row["scope"]} for row in censored[:128]]
    result["censored_count"] = len(censored)
    result["censored_truncated"] = len(censored) > 128
    completed = [row for row in rows if row["state"] == "COMPLETED"]
    for key, unit in _METRICS.items():
        candidates = completed
        scope = None
        if key == "memory_bytes":
            scoped_rows = [row for row in rows if wanted_scope is None or row["scope"] == wanted_scope]
            scopes = {row["scope"] for row in scoped_rows if row["values"][key] is not None}
            if not scopes:
                if wanted_scope is not None and any(row["values"][key] is not None for row in rows):
                    result["metrics"][key].update(status="scope_mismatch", scope=wanted_scope)
                    result["limitations"].append("History does not establish the requested memory measurement scope.")
                    continue
                result["limitations"].append("Memory measurements are unavailable.")
                continue
            if None in scopes or len(scopes) != 1:
                result["metrics"][key].update(status="unknown_scope" if None in scopes else "mixed_scopes")
                result["limitations"].append("Memory scope is unknown or mixed; no total-job memory estimate can be inferred.")
                continue
            scope = next(iter(scopes))
            candidates = [row for row in completed if row["scope"] == scope]
            if wanted_scope is not None and len(scoped_rows) != len(rows):
                result["limitations"].append("Memory observations with other or unknown scopes were excluded from this metric.")
            if scope == "max_task_rss":
                result["limitations"].append("Memory is maximum task RSS, not the sum across ranks or a safe --mem request.")
        values = [row["values"][key] for row in candidates if row["values"][key] is not None]
        fitted = _fit(values, float(coverage), min_samples)
        fitted["unit"] = unit
        if key == "memory_bytes":
            fitted["scope"] = scope
        if censored:
            bounds = [row["values"][key] for row in censored if row["values"][key] is not None
                      and (key != "memory_bytes" or row["scope"] == scope)]
            fitted.update(completed_estimate=fitted["estimate"], estimate=None, lower=None, upper=None,
                          status="censored", lower_bound=max(bounds) if bounds else None,
                          censored_samples=len(censored), observed_coverage=None)
        elif fitted["status"] == "poor_validation":
            result["limitations"].append(f"{key}: recent held-out coverage fell below the target; use caution with drift or changed inputs.")
        elif fitted["status"] == "insufficient_calibration":
            result["limitations"].append(f"{key}: the finite-sample coverage rank exceeds the calibration sample count.")
        result["metrics"][key] = fitted
    if censored:
        result["limitations"].append("TIMEOUT/OOM runs are censored lower-bound evidence; completed-only predictions would be biased. Predictive intervals are withheld.")
    statuses = [metric["status"] for metric in result["metrics"].values()]
    result["status"] = "ok" if all(status == "ok" for status in statuses) else "partial" if any(status in {"ok", "poor_validation", "censored"} for status in statuses) else "insufficient"
    return result
