"""Compare explicit submission choices without inventing resource speedups.

CPU and GPU quantities are totals for the allocation, not counts per node.
``mem_bytes`` is the requested allocation total. Reservation hours describe
requested walltime; they are neither actual consumption nor a billing model.
Comparisons use exact-work cohorts and remain conditional on their interval
bounds, especially when a caller supplies an unvalidated runtime estimate.
Preparing a choice reads its batch script but never contacts Slurm or runs it.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import itertools
import json
import math
import re

from . import clock

SCHEMA = "raven.submission-tradeoffs/v1"
MAX_CANDIDATES = 64
MAX_RECORDS = 10000
MAX_TEXT = 512
RESOURCE_FIELDS = ("cpus", "nodes", "gpus", "gpu_type", "mem_bytes", "time_seconds",
                   "partition", "account", "qos")
INTEGER_FIELDS = {"cpus": (1, 2147483647), "nodes": (1, 2147483647),
                  "gpus": (0, 2147483647), "mem_bytes": (1, (1 << 63) - 1)}
TEXT_FIELDS = {"label", "name", "gpu_type", "partition", "account", "qos"}
IDENTITY_FIELDS = ("name", "script_sha256", "input_size", "parameters")
BASE_OBJECTIVES = ("runtime_seconds", "reserved_core_hours", "reserved_gpu_hours")
INDEX_FIELDS = ("name", "partition", "cpus", "nodes", "gpus")


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (value <= 0 if positive else value < 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return value


def _text(value, name):
    limit = 128 if name == "name" else MAX_TEXT
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
        raise ValueError(f"{name} must be a nonempty string of at most {limit} UTF-8 bytes")
    if not all(char.isprintable() for char in value):
        raise ValueError(f"{name} must not contain terminal control characters")
    return value


def _parameters(value):
    if not isinstance(value, Mapping):
        raise ValueError("parameters must be a finite JSON object")
    visited = [0]

    def visit(item, depth):
        visited[0] += 1
        if depth > 12 or visited[0] > 4096:
            raise ValueError("parameters exceed the bounded depth or item limit")
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, (int, float)):
            if not math.isfinite(item):
                raise ValueError("parameters contain a nonfinite number")
            return item
        if isinstance(item, str):
            if len(item.encode("utf-8")) > 65536 or not all(c.isprintable() for c in item):
                raise ValueError("parameters contain an oversized or nonprintable string")
            return item
        if isinstance(item, Mapping):
            if len(item) > 1024 or any(not isinstance(k, str) for k in item):
                raise ValueError("parameters need bounded string-keyed JSON objects")
            if any(len(k.encode("utf-8")) > MAX_TEXT or not all(c.isprintable() for c in k) for k in item):
                raise ValueError("parameter names must be bounded and printable")
            return {k: visit(v, depth + 1) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            if len(item) > 1024:
                raise ValueError("parameters contain too many list entries")
            return [visit(v, depth + 1) for v in item]
        raise ValueError("parameters must contain only JSON data")

    result = visit(value, 0)
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("parameters exceed 64 KiB")
    return result


def _candidate(raw, index):
    if not isinstance(raw, Mapping):
        raise ValueError("candidate must be an object")
    if len(raw) > 128:
        raise ValueError("candidate contains too many fields")
    result = {"label": f"Choice {index + 1}"}
    for key in TEXT_FIELDS:
        if key in raw and raw[key] is not None:
            result[key] = "" if key == "gpu_type" and raw[key] == "" else _text(raw[key], key)
    for key, (low, high) in INTEGER_FIELDS.items():
        if key in raw and raw[key] is not None:
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{key} must be an integer between {low} and {high}")
            result[key] = value
    for key in ("time_seconds", "estimated_runtime_seconds"):
        if key in raw and raw[key] is not None:
            value = _number(raw[key], key, positive=True)
            if value > 100 * 366 * 86400:
                raise ValueError(f"{key} exceeds the bounded 100-year range")
            result[key] = value
    if "input_size" in raw and raw["input_size"] is not None:
        result["input_size"] = _number(raw["input_size"], "input_size")
    if "script_sha256" in raw and raw["script_sha256"] is not None:
        value = raw["script_sha256"]
        if not isinstance(value, str) or not re.fullmatch("[0-9a-fA-F]{64}", value):
            raise ValueError("script_sha256 must be a 64-digit hexadecimal digest")
        result["script_sha256"] = value.lower()
    if "parameters" in raw and raw["parameters"] is not None:
        result["parameters"] = _parameters(raw["parameters"])
    if result.get("nodes", 1) > result.get("cpus", 2147483647):
        raise ValueError("total cpus must provide at least one CPU per requested node")
    return result


def _work(candidate):
    if not candidate.get("name") and not candidate.get("script_sha256"):
        return None
    identity = {key: candidate[key] for key in IDENTITY_FIELDS if key in candidate}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _metric(unit, scope, *, estimate=None, lower=None, upper=None, status="unavailable",
            basis="unavailable", samples=0):
    return {"estimate": estimate, "lower": lower, "upper": upper, "unit": unit,
            "scope": scope, "status": status, "basis": basis, "samples": samples}


def _point(value, unit, scope, *, status="calculated", basis="requested_reservation"):
    return _metric(unit, scope, estimate=value, lower=value, upper=value, status=status, basis=basis)


def _interval(metric):
    lower, upper = metric.get("lower"), metric.get("upper")
    if isinstance(lower, bool) or isinstance(upper, bool):
        return None
    if not isinstance(lower, (int, float)) or not isinstance(upper, (int, float)):
        return None
    try:
        finite = math.isfinite(lower) and math.isfinite(upper)
    except OverflowError:
        finite = False
    if not finite or not 0 <= lower <= upper:
        return None
    return lower, upper


def _usable_interval(metric):
    if metric.get("validation_status") in {"poor_validation", "insufficient", "insufficient_calibration", "censored"}:
        return None
    return _interval(metric)


def _bounded_records(records, name):
    if isinstance(records, (str, bytes, Mapping)) or not isinstance(records, Iterable):
        raise ValueError(f"{name} must be an iterable of records")
    values = list(itertools.islice(records, MAX_RECORDS + 1))
    return values[:MAX_RECORDS], len(values) > MAX_RECORDS


def _record_index(records):
    """Partition unambiguous core fields once; model modules validate each row.

    These fields have the same direct representation in Job/Finished and model
    mapping inputs. Optional aliases remain the responsibility of the model.
    Partial requests fall back to the original bounded history instead of
    guessing a cohort. This avoids normalizing all 10,000 records 64 times.
    """
    groups = {}
    for record in records:
        if isinstance(record, Mapping):
            key = tuple(record.get(field) for field in INDEX_FIELDS)
        else:
            key = tuple(getattr(record, field, None) for field in INDEX_FIELDS)
        try:
            groups.setdefault(key, []).append(record)
        except TypeError:
            # A list/dict core field is invalid in both model implementations.
            continue
    return groups


def _indexed_history(candidate, records, index):
    if all(field in candidate for field in INDEX_FIELDS):
        return index.get(tuple(candidate[field] for field in INDEX_FIELDS), [])
    return records


def _measure(candidate, history, queue_history, coverage, timestamp, prediction_cache, queue_cache,
             history_index, queue_index):
    work = _work(candidate)
    metrics = {"runtime_seconds": _metric("seconds", work),
               "wait_seconds": _metric("seconds", "submitted_now"),
               "completion_seconds": _metric("seconds", work),
               "reserved_core_hours": _metric("cpu_core_hour", "allocated_cpu_total"),
               "reserved_gpu_hours": _metric("gpu_hour", candidate.get("gpu_type") or None),
               "reserved_memory_gib_hours": _metric("gib_hour", "allocation_total")}
    evidence, assumptions, limitations, risks = [], [], [], []
    runtime = candidate.get("estimated_runtime_seconds")
    prediction = None
    if runtime is not None:
        metrics["runtime_seconds"] = _point(runtime, "seconds", work, status="assumed",
                                             basis="declared_runtime_estimate")
        assumptions.append("Runtime is the caller's explicit estimate; its point bounds have no empirical coverage.")
    elif history and work:
        from .predict import predict
        query = {key: value for key, value in candidate.items() if key in RESOURCE_FIELDS + IDENTITY_FIELDS}
        token = json.dumps(query, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if token not in prediction_cache:
            prediction_cache[token] = predict(_indexed_history(candidate, history, history_index), query, coverage=coverage)
        prediction = prediction_cache[token]
        source = prediction.get("metrics", {}).get("runtime_seconds", {})
        if _interval(source):
            metrics["runtime_seconds"] = _metric("seconds", work, estimate=source.get("estimate"),
                                                   lower=source["lower"], upper=source["upper"],
                                                   status="poor_validation" if source.get("status") == "poor_validation" else "predicted",
                                                   basis=source.get("method", "exact_cohort"),
                                                   samples=source.get("samples", 0))
            metrics["runtime_seconds"].update({"validation_status": source.get("status", "unknown"),
                                               "calibration_samples": source.get("calibration_samples"),
                                               "validation_samples": source.get("validation_samples"),
                                               "observed_coverage": source.get("observed_coverage"),
                                               "requested_coverage": coverage})
            if source.get("status") == "poor_validation":
                risks.append("Recent runtime validation failed the coverage target; the displayed interval cannot establish Pareto dominance.")
        elif source.get("status") == "censored":
            metrics["runtime_seconds"]["status"] = "censored"
        evidence.extend(prediction.get("evidence", [])[:24])
        limitations.extend(prediction.get("limitations", [])[:24])
    if not _interval(metrics["runtime_seconds"]):
        limitations.append("No usable runtime interval for this exact work and resource choice; no resource scaling is assumed.")
    if queue_history and work:
        from .forecast import forecast
        # The forecast implementation understands Finished/Job memory and limit
        # aliases and requires exact matching when these request fields are set.
        # Omitting them here would silently weaken an alternative's cohort.
        if "mem_bytes" in candidate and "time_seconds" in candidate:
            query = {key: value for key, value in candidate.items() if key in RESOURCE_FIELDS + IDENTITY_FIELDS}
            token = json.dumps(query, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if token not in queue_cache:
                query.update({"state": "PENDING", "submit": timestamp, "reason": "", "est_start": ""})
                queue_cache[token] = forecast(query, _indexed_history(candidate, queue_history, queue_index), now=timestamp, coverage=coverage)
            estimate = queue_cache[token]
            lower, upper = estimate.get("lower_start"), estimate.get("upper_start")
            calibrated = estimate.get("calibration", {}).get("valid") is True
            if (calibrated and isinstance(lower, (int, float)) and not isinstance(lower, bool)
                    and isinstance(upper, (int, float)) and not isinstance(upper, bool)
                    and math.isfinite(lower) and math.isfinite(upper) and timestamp <= lower <= upper):
                start = estimate.get("predicted_start")
                point = max(0.0, start - timestamp) if isinstance(start, (int, float)) and math.isfinite(start) else None
                metrics["wait_seconds"] = _metric("seconds", "submitted_now", estimate=point,
                                                    lower=lower - timestamp, upper=upper - timestamp,
                                                    status="predicted", basis=estimate.get("method", "exact_cohort"),
                                                    samples=estimate.get("samples", 0))
                assumptions.append("Queue interval describes a hypothetical submission now with this exact request.")
            evidence.extend(estimate.get("evidence", [])[:24])
            limitations.extend(estimate.get("limitations", [])[:24])
        else:
            limitations.append("Queue history does not prove matching requested memory and walltime for this choice.")
    if not _interval(metrics["wait_seconds"]):
        limitations.append("Queue delay is unavailable without a calibrated matching cohort; scheduler points are not treated as guarantees.")
    runtime_bounds, wait_bounds = _usable_interval(metrics["runtime_seconds"]), _usable_interval(metrics["wait_seconds"])
    if runtime_bounds and wait_bounds:
        runtime_point, wait_point = metrics["runtime_seconds"]["estimate"], metrics["wait_seconds"]["estimate"]
        total = runtime_point + wait_point if runtime_point is not None and wait_point is not None else None
        metrics["completion_seconds"] = _metric("seconds", work, estimate=total,
                                                  lower=runtime_bounds[0] + wait_bounds[0],
                                                  upper=runtime_bounds[1] + wait_bounds[1], status="derived",
                                                  basis="sum_of_component_bounds")
        assumptions.append("Completion bounds sum runtime and queue bounds conservatively; no joint coverage is claimed.")
    walltime = candidate.get("time_seconds")
    if walltime is not None:
        for resource, key, unit, scope, divisor in (
                ("cpus", "reserved_core_hours", "cpu_core_hour", "allocated_cpu_total", 1),
                ("gpus", "reserved_gpu_hours", "gpu_hour", candidate.get("gpu_type") or None, 1),
                ("mem_bytes", "reserved_memory_gib_hours", "gib_hour", "allocation_total", 1 << 30)):
            if resource in candidate:
                value = candidate[resource] / divisor * walltime / 3600
                if resource == "gpus" and candidate[resource] == 0:
                    scope = "no_gpu_allocation"
                metrics[key] = _point(value, unit, scope)
        if runtime_bounds and walltime < runtime_bounds[1]:
            risks.append("Requested walltime is below the runtime upper bound; time-limit failure remains possible.")
        elif not runtime_bounds:
            risks.append("Walltime adequacy cannot be assessed without a runtime interval.")
    else:
        limitations.append("Requested walltime is missing, so reservation-hour quantities are unavailable.")
    if "gpus" in candidate and candidate["gpus"] and not candidate.get("gpu_type"):
        limitations.append("GPU type is unspecified; positive GPU-hour quantities cannot establish compatible hardware scope.")
    assumptions.append("Reservation hours use total CPUs/GPUs times requested walltime, without multiplying by node count.")
    assumptions.append("Memory GiB-hours describe an allocation proxy and are excluded from the Pareto objectives; site billing is unknown.")
    assumptions.append("Preparing a choice uses one task per node and rounds walltime up to seconds and total memory up to MiB per node.")
    if candidate.get("cpus") and candidate.get("nodes") and candidate["cpus"] % candidate["nodes"]:
        limitations.append("The observed CPU total does not divide evenly across nodes; the explicit preparation topology cannot represent it.")
    return metrics, evidence, assumptions, limitations, risks


def _compatible(left, right):
    if left.get("unit") != right.get("unit") or not _usable_interval(left) or not _usable_interval(right):
        return False
    left_scope, right_scope = left.get("scope"), right.get("scope")
    if left.get("unit") == "gpu_hour" and (left_scope == "no_gpu_allocation" or right_scope == "no_gpu_allocation"):
        return True
    return left_scope is not None and left_scope == right_scope


def _dominance(left, right, objectives):
    if not left.get("work_id") or left["work_id"] != right.get("work_id"):
        return None
    pairs = [(left["metrics"][name], right["metrics"][name]) for name in objectives]
    if not all(_compatible(a, b) for a, b in pairs):
        return None
    certain = all(a["upper"] <= b["lower"] for a, b in pairs) and any(a["upper"] < b["lower"] for a, b in pairs)
    possible = all(a["lower"] <= b["upper"] for a, b in pairs) and any(a["lower"] < b["upper"] for a, b in pairs)
    return "certain" if certain else "possible" if possible else "none"


def compare(candidates, *, history=(), queue_history=(), coverage=.8, max_candidates=MAX_CANDIDATES):
    """Return bounded alternatives, interval Pareto relations, and honest gaps.

    Missing measurements stay unknown. Candidates with different work identities
    never dominate each other. A ``certain`` relation means separated supplied
    bounds, conditional on assumptions; it does not mean guaranteed job results.
    Malformed candidates are isolated as invalid rows rather than hiding valid
    alternatives. Duplicate labels retain their stable original indices.
    """
    try:
        valid_coverage = (not isinstance(coverage, bool) and isinstance(coverage, (int, float))
                          and math.isfinite(coverage) and 0 < coverage < 1)
    except OverflowError:
        valid_coverage = False
    if not valid_coverage:
        raise ValueError("coverage must be a finite number strictly between zero and one")
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 1 <= max_candidates <= MAX_CANDIDATES:
        raise ValueError(f"max_candidates must be an integer between 1 and {MAX_CANDIDATES}")
    if isinstance(candidates, (str, bytes, Mapping)) or not isinstance(candidates, Iterable):
        raise ValueError("candidates must be an iterable of objects")
    raw = list(itertools.islice(candidates, max_candidates + 1))
    records, history_truncated = _bounded_records(history, "history")
    queue, queue_truncated = _bounded_records(queue_history, "queue_history")
    history_index, queue_index = _record_index(records), _record_index(queue)
    timestamp = _number(clock.now(), "comparison clock")
    rows, prediction_cache, queue_cache = [], {}, {}
    for index, item in enumerate(raw[:max_candidates]):
        try:
            candidate = _candidate(item, index)
        except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError) as exc:
            rows.append({"index": index, "label": f"Choice {index + 1}", "resources": {}, "metrics": {},
                         "work_id": None, "status": "invalid", "evidence": [], "assumptions": [],
                         "limitations": [str(exc)], "risks": [], "definition": None})
            continue
        metrics, evidence, assumptions, limitations, risks = _measure(candidate, records, queue, coverage,
                                                                       timestamp, prediction_cache, queue_cache,
                                                                       history_index, queue_index)
        work = _work(candidate)
        if not work:
            limitations.append("A shared work name or script digest is required for comparisons; labels do not establish work identity.")
        known = bool(_usable_interval(metrics["runtime_seconds"]))
        status = "assumed" if work and known and metrics["runtime_seconds"]["status"] == "assumed" else "ok" if work and known else "incomplete"
        rows.append({"index": index, "label": candidate["label"], "resources": {key: candidate[key] for key in RESOURCE_FIELDS if key in candidate},
                     "metrics": metrics, "work_id": work, "status": status, "evidence": evidence[:48],
                     "assumptions": list(dict.fromkeys(assumptions)), "limitations": list(dict.fromkeys(limitations)),
                     "risks": risks, "definition": candidate})
    valid = [row for row in rows if row["status"] != "invalid"]
    objectives = list(BASE_OBJECTIVES)
    if valid and all(_interval(row["metrics"]["completion_seconds"]) for row in valid):
        objectives.append("completion_seconds")
    frontier = []
    for row in rows:
        certain, possible, compared = [], [], []
        if row["status"] != "invalid":
            for other in valid:
                if other is row:
                    continue
                relation = _dominance(other, row, objectives)
                if relation is not None:
                    compared.append(other["index"])
                if relation == "certain":
                    certain.append(other["index"])
                elif relation == "possible":
                    possible.append(other["index"])
        eligible = bool(row.get("work_id")) and row["status"] != "invalid"
        on_frontier = eligible and not certain
        if on_frontier:
            frontier.append(row["index"])
        row["pareto"] = {"frontier": on_frontier, "certainly_dominated_by": certain,
                         "possibly_dominated_by": possible, "compared_with": compared,
                         "status": "dominated" if certain else "frontier" if compared else "unavailable",
                         "certainty": "conditional_on_supplied_bounds_and_assumptions"}
    limitations = ["Pareto relations compare matching work definitions and compatible known objective scopes only.",
                   "Interval separation is conditional on supplied bounds and assumptions, not a guarantee.",
                   "Unknown dimensions never establish dominance, and no inverse-linear resource speedup is assumed.",
                   "Reservation quantities are requests, not actual resource use or a site billing estimate."]
    if len(raw) > max_candidates:
        limitations.append(f"Candidate input truncated to {max_candidates} choices.")
    if history_truncated or queue_truncated:
        limitations.append(f"History input is bounded to the first {MAX_RECORDS} records per source.")
    if len({row["work_id"] for row in valid if row["work_id"]}) > 1:
        limitations.append("Different or incompletely matching work definitions form separate comparison groups.")
    status = "empty" if not rows else "error" if not valid else "ok" if all(row["status"] in {"ok", "assumed"} for row in rows) else "partial"
    return {"schema": SCHEMA, "status": status, "candidates": rows, "frontier": frontier,
            "objectives": objectives, "coverage": coverage, "as_of": timestamp, "limitations": limitations}


def _slurm_time(seconds):
    value = math.ceil(seconds)
    days, rest = divmod(value, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{days}-{hours:02d}:{minutes:02d}:{seconds:02d}" if days else f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def prepare_choice(result, index, script, *, workdir=None):
    """Prepare a chosen request with one task per node and explicit totals.

    The resource topology is deliberately explicit: total CPUs must divide
    evenly across nodes; total memory is rounded up to MiB per node. Inherited
    directives that alter that interpretation require editing the batch script.
    This function does not submit, test-submit, inspect the queue, or execute code.
    """
    if not isinstance(result, Mapping) or result.get("schema") != SCHEMA:
        raise ValueError("not a Raven submission tradeoff result")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("choice index must be a nonnegative integer")
    rows = result.get("candidates")
    if not isinstance(rows, list) or len(rows) > MAX_CANDIDATES:
        raise ValueError("invalid tradeoff candidate list")
    choices = [row for row in rows if isinstance(row, Mapping) and row.get("index") == index]
    if len(choices) != 1 or choices[0].get("status") == "invalid":
        raise ValueError("choice index does not identify one valid candidate")
    row = choices[0]
    candidate = _candidate(row.get("definition"), index)
    required = {"cpus", "nodes", "gpus", "mem_bytes", "time_seconds"}
    missing = sorted(required - candidate.keys())
    if missing:
        raise ValueError("choice preparation needs explicit " + ", ".join(missing))
    cpus, nodes, gpus = candidate["cpus"], candidate["nodes"], candidate["gpus"]
    if cpus % nodes:
        raise ValueError("total cpus must divide evenly across nodes for the explicit one-task-per-node plan")
    memory_mib = (candidate["mem_bytes"] + nodes * (1 << 20) - 1) // (nodes * (1 << 20))
    overrides = [f"--nodes={nodes}", f"--ntasks={nodes}", f"--cpus-per-task={cpus // nodes}",
                 f"--mem={memory_mib}M", "--time=" + _slurm_time(candidate["time_seconds"])]
    for key in ("partition", "account", "qos", "name"):
        if key in candidate:
            overrides.append("--" + ("job-name" if key == "name" else key) + "=" + candidate[key])
    if gpus:
        prefix = candidate.get("gpu_type", "")
        if prefix and not re.fullmatch(r"[A-Za-z0-9_.-]+", prefix):
            raise ValueError("GPU type needs a valid Slurm resource token")
        overrides.append("--gpus=" + (prefix + ":" if prefix else "") + str(gpus))
    from .submission import prepare
    plan = prepare(script, workdir=workdir, overrides=overrides, parameters=candidate.get("parameters"))
    actual = plan["resources"]
    conflicts = {"cpus_per_gpu", "ntasks_per_core", "ntasks_per_gpu", "ntasks_per_socket", "ntasks_per_node",
                 "threads_per_core", "mem_per_cpu", "mem_per_gpu", "gpus_per_node", "gpus_per_socket", "gpus_per_task"} & actual.keys()
    if "gres" in actual and "gpu" in actual["gres"].lower():
        conflicts.add("gres")
    if not gpus and "gpus" in actual:
        conflicts.add("gpus")
    if conflicts:
        raise ValueError("batch directives conflict with chosen resource-total semantics; remove or reconcile: " + ", ".join(sorted(conflicts)))
    if candidate.get("script_sha256") and candidate["script_sha256"] != plan["script_sha256"]:
        raise ValueError("batch script digest does not match the work identity used in the comparison")
    # Advisory metadata must be outside the sealed submission plan: adding fields
    # would invalidate submission's tamper-resistant digest. Its exact flags carry
    # the chosen request, and callers already retain the comparison assumptions.
    return plan
