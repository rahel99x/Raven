"""Bounded observational request alternatives and chronological forecast audit.

Recorded start outcomes calibrate queue-model residuals. Counterfactual changes
remain advisory; this never edits requests or treats simulation as observation.
"""
from __future__ import annotations

import copy
import math
import statistics

from . import forecast_model as F
from .evidence import bounded, digest
from .runtime_contracts import identifier, number, object_, sequence

MODEL = "observable-queue-residual/1"
MAX_OBSERVATIONS = 1000
MAX_EVALUATIONS = 100
_REQUIRED = {"name", "partition", "account", "qos", "cpus", "nodes", "gpus", "mem_bytes", "time_seconds"}
_LIMITS = [
    "Request alternatives are observational scenarios, not causal guarantees of earlier starts.",
    "Hidden demand, invisible reservations and unreported policy can invalidate both model points and historical intervals.",
    "Runtime evidence must describe the same application and resources; changing CPUs, nodes or memory can change scientific runtime.",
    "Recorded and synthetic outcome audits remain separate; a simulator cannot supply independent outcome ground truth.",
    "No job request, checkpoint or submission is changed by this analysis.",
]


def _request(value):
    object_(value, _REQUIRED, {"gpu_type"}, "forecast request")
    result = F._cohort(value)
    if not _REQUIRED <= result.keys() or result["cpus"] < 1 or result["nodes"] < 1 or (result["gpus"] and "gpu_type" not in result):
        raise ValueError("Request needs concrete application, account/partition/QOS and CPU/node/GPU/memory/walltime values")
    return result


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise ValueError("Forecast scenario analysis cancelled")


def _context(value):
    object_(value, {"policy_digest", "reservations_visible", "policy_complete"}, {"queue_depth", "idle_cpus"}, "forecast context")
    identifier(value["policy_digest"], "policy identity")
    for key in ("reservations_visible", "policy_complete"):
        if type(value[key]) is not bool:
            raise ValueError("Context visibility must be an explicit boolean")
    for key in ("queue_depth", "idle_cpus"):
        if key in value:
            number(value[key], key, high=1e9)
    return value


def _validate(document):
    bounded(document)
    object_(document, {"schema", "now", "request", "alternatives", "context", "observations"},
            {"coverage", "wait_threshold_seconds", "maximum_overrun_probability", "queue_model", "target_job_id", "queue_model_issued_at", "model_wait_seconds", "scheduler_wait_seconds", "metadata"}, "forecast scenarios")
    if document["schema"] != "raven.forecast-scenarios/v1":
        raise ValueError("Expected raven.forecast-scenarios/v1")
    now = number(document["now"], "observation clock", high=1e12)
    if "queue_model" in document:
        if "queue_model_issued_at" not in document:
            raise ValueError("Queue scenarios require queue_model_issued_at in the model's clock domain")
        number(document["queue_model_issued_at"], "queue model forecast origin", high=1e12)
    elif "queue_model_issued_at" in document or "target_job_id" in document:
        raise ValueError("Queue model origin and target require an explicit queue_model")
    _request(document["request"])
    _context(document["context"])
    for key in ("model_wait_seconds", "scheduler_wait_seconds"):
        if key in document:
            number(document[key], key, high=1e9)
    alternatives = sequence(document["alternatives"], "request alternatives", 8)
    seen = {"current"}
    for item in alternatives:
        object_(item, {"id", "request"}, {"model_wait_seconds", "scheduler_wait_seconds"}, "alternative")
        key = identifier(item["id"], "alternative identity")
        if key in seen:
            raise ValueError("Alternative identities must be unique and cannot be current")
        seen.add(key)
        request = _request(item["request"])
        if request["name"] != document["request"]["name"]:
            raise ValueError("Request alternatives must refer to the same scientific application")
        for field in ("model_wait_seconds", "scheduler_wait_seconds"):
            if field in item:
                number(item[field], field, high=1e9)
    observations = sequence(document["observations"], "forecast outcomes", MAX_OBSERVATIONS)
    attempts = set()
    for row in observations:
        object_(row, {"id", "request", "context", "origin", "submit", "issued_at", "actual_start", "actual_end", "state"},
                {"model_wait_seconds", "scheduler_wait_seconds", "metadata"}, "forecast outcome")
        key = identifier(row["id"], "attempt identity")
        if key in attempts:
            raise ValueError("Outcome attempt identities must be unique")
        attempts.add(key)
        _request(row["request"])
        _context(row["context"])
        if row["origin"] not in {"recorded", "synthetic"} or row["state"] not in {"completed", "timeout"}:
            raise ValueError("Outcomes require recorded/synthetic origin and completed/timeout state")
        values = [number(row[k], k, high=1e12) for k in ("submit", "issued_at", "actual_start", "actual_end")]
        if not values[0] <= values[1] < values[2] < values[3] <= now:
            raise ValueError("Outcome predictions must precede actual starts and completed observations must precede now")
        for field in ("model_wait_seconds", "scheduler_wait_seconds"):
            if field in row:
                number(row[field], field, high=1e9)
    coverage = number(document.get("coverage", .8), "coverage", low=.01, high=.99)
    threshold = number(document.get("wait_threshold_seconds", 300), "wait event threshold", high=1e9)
    risk = number(document.get("maximum_overrun_probability", .1), "maximum runtime risk", low=.001, high=.5)
    return now, coverage, threshold, risk


def _same(a, b, *, runtime=False):
    ignored = {"time_seconds"} if runtime else set()
    return all(a.get(k) == b.get(k) for k in (a.keys() | b.keys()) - ignored)


def _eligible(observations, request, context):
    return [row for row in observations if _same(row["request"], request)
            and row["context"]["policy_digest"] == context["policy_digest"]
            and row["context"]["reservations_visible"] == context["reservations_visible"]
            and row["context"]["policy_complete"] == context["policy_complete"]]


def _fit(rows, point, coverage, threshold, *, residual):
    values = [(row["actual_start"] - row["issued_at"]) - (row.get("model_wait_seconds", 0) if residual else 0)
              for row in rows if not residual or "model_wait_seconds" in row]
    if len(values) < 40 or residual and point is None:
        return {"status": "abstain", "reason": "At least 40 comparable earlier outcomes and an explicit model point are required.", "samples": len(values)}
    split = len(values) // 2
    center = statistics.median(values[:split])
    errors = [abs(v - center) for v in values[split:]]
    radius = F._quantile(errors, coverage)
    if radius is None:
        return {"status": "abstain", "reason": "Requested interval coverage exceeds available calibration evidence.", "samples": len(values)}
    early = statistics.median(values[:split])
    recent = statistics.median(values[-20:])
    scale = max(1., statistics.median([abs(v - early) for v in values[:split]]))
    if abs(recent - early) > 4 * scale:
        return {"status": "abstain", "reason": "Chronological residual drift exceeds the declared robust threshold.", "samples": len(values)}
    base = point if residual else 0
    prediction = max(0., base + center)
    # Empirical event probability receives Jeffreys smoothing, avoiding log(0).
    successes = sum(base + v <= threshold for v in values[split:])
    probability = (successes + .5) / (len(values[split:]) + 1)
    return {"status": "forecast", "wait_seconds": prediction, "lower_wait_seconds": max(0., prediction - radius),
            "upper_wait_seconds": prediction + radius, "probability_wait_within_threshold": probability,
            "samples": len(values), "calibration_samples": len(errors), "coverage_target": coverage,
            "interval_method": "chronological split-conformal absolute residual", "probability_method": "empirical held-out residual CDF with Jeffreys smoothing"}


def _runtime_risk(rows, request):
    matches = [r for r in rows if _same(r["request"], request, runtime=True)]
    over = 0
    unknown = 0
    for row in matches:
        duration = row["actual_end"] - row["actual_start"]
        if duration > request["time_seconds"]:
            over += 1
        elif row["state"] == "timeout":
            unknown += 1
    n = len(matches)
    if not n or unknown:
        return {"status": "unknown", "samples": n, "censored_unresolved": unknown, "upper_probability": None}
    # Wilson 95% upper bound; finite samples with zero overruns remain uncertain.
    z, p = 1.959963984540054, over / n
    upper = (p + z*z/(2*n) + z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / (1+z*z/n)
    return {"status": "estimated" if n >= 20 else "sparse", "samples": n, "observed_overruns": over,
            "probability": p, "upper_probability": upper, "method": "95% Wilson upper bound; unresolved timeout censoring abstains"}


def _current(rows, item, now, coverage, threshold):
    history = [dict(r["request"], id=r["id"], submit=r["submit"], start=r["actual_start"]) for r in rows]
    observed = [dict(r["request"], id=r["id"], submit=r["submit"], issued_at=r["issued_at"],
                     predicted_start=r["issued_at"] + r["scheduler_wait_seconds"], actual_start=r["actual_start"])
                for r in rows if "scheduler_wait_seconds" in r]
    target = dict(item["request"], id="evaluation-target", submit=item.get("submit", now), state="PENDING")
    if "scheduler_wait_seconds" in item:
        target["est_start"] = now + item["scheduler_wait_seconds"]
    result = F.forecast(target, history, observations=observed, now=now, coverage=coverage)
    if result["predicted_start"] is None:
        return {"status": "abstain", "reason": "; ".join(result["limitations"]), "samples": result["samples"]}
    # Probability is independently estimated from earlier absolute prediction
    # residuals; the original predictor does not claim this probability itself.
    residuals = [(r["actual_start"] - r["issued_at"]) - r["scheduler_wait_seconds"] for r in rows if "scheduler_wait_seconds" in r]
    probability = None
    point = result["predicted_start"] - now
    if "scheduler_wait_seconds" in item and len(residuals) >= 20:
        probability = (sum(point + v <= threshold for v in residuals) + .5) / (len(residuals) + 1)
    elif "scheduler_wait_seconds" not in item and len(rows) >= 20:
        queued = now-target["submit"]
        remaining = [r["actual_start"]-r["submit"]-queued for r in rows if r["actual_start"]-r["submit"] > queued]
        if len(remaining) >= 20:
            probability = (sum(wait <= threshold for wait in remaining) + .5) / (len(remaining) + 1)
    return {"status": "forecast", "wait_seconds": point,
            "lower_wait_seconds": None if result["lower_start"] is None else result["lower_start"] - now,
            "upper_wait_seconds": None if result["upper_start"] is None else result["upper_start"] - now,
            "probability_wait_within_threshold": probability, "method": result["method"], "samples": result["samples"]}


def _audit(observations, coverage, threshold, cancel):
    audits = []
    for origin in ("recorded", "synthetic"):
        rows = sorted([r for r in observations if r["origin"] == origin], key=lambda r: (r["issued_at"], r["id"]))
        evaluation = rows[-MAX_EVALUATIONS:]
        results = {name: [] for name in ("simple_cohort", "current_predictor", "queue_residual")}
        for target in evaluation:
            _check(cancel)
            previous = [r for r in rows if r["actual_end"] < target["issued_at"]]
            comparable = _eligible(previous, target["request"], target["context"])
            values = {"simple_cohort": _fit(comparable, None, coverage, threshold, residual=False),
                      "current_predictor": _current(comparable, target, target["issued_at"], coverage, threshold),
                      "queue_residual": _fit(comparable, target.get("model_wait_seconds"), coverage, threshold, residual=True)}
            actual = target["actual_start"] - target["issued_at"]
            for name, value in values.items():
                if value["status"] == "forecast":
                    lower, upper = value.get("lower_wait_seconds"), value.get("upper_wait_seconds")
                    prob = value.get("probability_wait_within_threshold")
                    event = actual <= threshold
                    results[name].append({"absolute_error": abs(actual-value["wait_seconds"]),
                        "covered": None if lower is None or upper is None else lower <= actual <= upper,
                        "brier": None if prob is None else (prob-int(event))**2,
                        "log_score": None if prob is None else -math.log(prob if event else 1-prob)})
        for name, values in results.items():
            if not rows:
                continue
            errors = sorted(v["absolute_error"] for v in values)
            metric = lambda field: statistics.mean([v[field] for v in values if v[field] is not None]) if any(v[field] is not None for v in values) else None
            audits.append({"origin": origin, "model": name, "eligible_attempts": len(evaluation), "predictions": len(values),
                           "prediction_coverage": len(values)/len(evaluation), "mae_seconds": statistics.mean(errors) if errors else None,
                           "p95_absolute_error_seconds": errors[math.ceil(.95*len(errors))-1] if errors else None,
                           "interval_coverage": metric("covered"), "brier_score": metric("brier"), "log_score": metric("log_score"),
                           "interval_samples": sum(v["covered"] is not None for v in values),
                           "probability_samples": sum(v["brier"] is not None for v in values)})
    return audits


def analyze(document, *, cancel=None):
    now, coverage, threshold, max_risk = _validate(document)
    rows = sorted(document["observations"], key=lambda r: (r["actual_end"], r["id"]))
    # Never combine synthetic outcomes into the live/recorded inference cohort.
    recorded = [r for r in rows if r["origin"] == "recorded"]
    current = {"id": "current", "request": document["request"]}
    current.update({key: document[key] for key in ("model_wait_seconds", "scheduler_wait_seconds") if key in document})
    items = [current] + copy.deepcopy(document["alternatives"])
    if "queue_model" in document:
        from .slurm_simulator import simulate, validate
        model = validate(document["queue_model"])
        if len(model.get("jobs", [])) > 256:
            raise ValueError("Forecast alternatives limit queue scenarios to 256 jobs")
        target = document.get("target_job_id")
        if not isinstance(target, str) or sum(j.get("id") == target for j in model.get("jobs", [])) != 1:
            raise ValueError("Queue scenarios require one exact target_job_id")
        for item in items:
            _check(cancel)
            scenario = copy.deepcopy(model)
            job = next(j for j in scenario["jobs"] if j["id"] == target)
            request = item["request"]
            if (request["partition"] != document["request"]["partition"] or
                    request.get("gpu_type") != document["request"].get("gpu_type")):
                item["model_error"] = "This inventory cannot represent another partition or GPU type; supply an independently observed scenario point without queue_model."
                continue
            if request["cpus"] % request["nodes"] or request["gpus"] % request["nodes"]:
                raise ValueError("Homogeneous queue scenarios require divisible per-node CPU/GPU requests")
            job.update(nodes=request["nodes"], cpus_per_node=request["cpus"]//request["nodes"],
                       gpus_per_node=request["gpus"]//request["nodes"],
                       memory_mb_per_node=math.ceil(request["mem_bytes"]/(1048576*request["nodes"])),
                       wall_limit=request["time_seconds"], account=request["account"], qos=request["qos"])
            if job["duration"] > job["wall_limit"]:
                item["model_error"] = "Declared application duration exceeds this wall limit; scenario refused."
                continue
            result = simulate(scenario, cancel=cancel)
            predicted = next((r for r in result["timeline"] if r["id"] == target), None)
            origin = document["queue_model_issued_at"]
            item["queue_model_receipt"] = {"model": result["model"], "input_sha256": result["input_sha256"], "production_qualified": False,
                                          "issued_at_model_clock": origin, "predicted_start_model_clock": predicted["start"] if predicted else None,
                                          "wait_basis": "Predicted start minus explicit forecast origin in the model clock"}
            if predicted is not None and predicted["start"] >= origin:
                item["model_wait_seconds"] = predicted["start"] - origin
            elif predicted is not None:
                item["model_error"] = "Modeled target already started before the forecast origin; a pending-request forecast is unavailable."

            else:
                item["model_error"] = result["blocked"].get(target, "Scenario target did not start")
    context = document["context"]
    for item in items:
        _check(cancel)
        comparable = _eligible(recorded, item["request"], context)
        item["forecast"] = _fit(comparable, item.get("model_wait_seconds"), coverage, threshold, residual=True)
        item["runtime_risk"] = _runtime_risk(recorded, item["request"])
        reasons = []
        if not context["reservations_visible"] or not context["policy_complete"]:
            reasons.append("Reservations or scheduler policies are not completely visible")
        if item["forecast"]["status"] != "forecast":
            reasons.append(item["forecast"]["reason"])
        if item["runtime_risk"]["status"] != "estimated" or item["runtime_risk"]["upper_probability"] > max_risk:
            reasons.append("Runtime-overrun uncertainty exceeds the declared risk tolerance")
        if item.get("model_error"):
            reasons.append(item["model_error"])
        item["eligible_for_comparison"] = not reasons
        item["warnings"] = reasons
    eligible = [i for i in items if i["eligible_for_comparison"]]
    best = min(eligible, key=lambda i: i["forecast"]["wait_seconds"]) if eligible else None
    return {"schema": "raven.forecast-scenarios-result/v1", "model": MODEL, "input_sha256": digest(document),
            "status": "advisory" if best else "abstain", "experimental": True, "production_qualified": False,
            "summary": "Review observational request alternatives and runtime risk; no requests have been changed.",
            "alternatives": items, "preferred_scenario": best["id"] if best else None,
            "evaluation": _audit(rows, coverage, threshold, cancel), "limits": list(_LIMITS),
            "parameters": {"coverage": coverage, "wait_threshold_seconds": threshold, "maximum_overrun_probability": max_risk,
                           "max_outcomes": MAX_OBSERVATIONS, "max_evaluations_per_origin": MAX_EVALUATIONS}}


def lines(result):
    rows = [result["summary"]]
    for item in result["alternatives"]:
        f, risk = item["forecast"], item["runtime_risk"]
        rows.append(f"{item['id']}: wall {item['request']['time_seconds']:g} s; forecast {f['status']}; wait {f.get('wait_seconds')} s; runtime risk upper {risk.get('upper_probability')}")
        if f["status"] == "forecast":
            rows.append(f"  Interval {f['lower_wait_seconds']:.3f}..{f['upper_wait_seconds']:.3f} s; {f['samples']} recorded observations")
        rows.extend("  " + text for text in item["warnings"])
    rows.extend(f"{r['origin']} / {r['model']}: MAE {r['mae_seconds']} s; p95 {r['p95_absolute_error_seconds']} s; prediction coverage {r['prediction_coverage']:.1%}; interval coverage {r['interval_coverage']}; Brier {r['brier_score']}; log score {r['log_score']}" for r in result["evaluation"])
    return rows + ["Limit: " + text for text in result["limits"]]
