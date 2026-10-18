"""Advisory finite-work Young model with explicit exposure and censoring.

This never derives checkpoint duration from file metadata or changes a job.
The approximation excludes interruptions during writes/restarts and repeated
loss of replayed work; its rare-interruption domain is checked explicitly.
"""
from __future__ import annotations

import math
import random
import statistics

from .evidence import bounded, digest
from .runtime_contracts import integer, number, object_, sequence, text

MODEL = "young-finite-work/1"
MIN_SECONDS = 1e-9  # Explicit numerical resolution, not inferred timing precision.
ASSUMPTIONS = [
    "Allocation-level stationary Poisson node-failure hazard within the declared cohort; censoring is independent.",
    "Equal useful-work segments; first-order loss ignores failures during checkpoint writes, restart and repeated replay.",
    "Checkpoint duration is measured elapsed time or an explicitly labelled estimate, never file size or mtime.",
    "Preemption and wall-time expiry are separate policy events; neither is counted as node failure.",
    "Advice does not configure periodic checkpointing, submit, signal, restart or requeue a job.",
]


def _duration(value, name):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(name + " requires measured samples or an explicit estimated range")
    if "samples_seconds" in value:
        object_(value, {"samples_seconds", "source"}, name=name)
        samples = sequence(value["samples_seconds"], name + " samples", 1000, 1)
        values = [number(v, name, low=MIN_SECONDS, high=1e9, positive=True) for v in samples]
        result = {"lower": min(values), "estimate": statistics.median(values), "upper": max(values),
                  "samples": len(values), "kind": "measured envelope; not a confidence interval"}
    else:
        object_(value, {"lower_seconds", "upper_seconds", "source"}, name=name)
        low = number(value["lower_seconds"], name + " lower", low=MIN_SECONDS, high=1e9, positive=True)
        high = number(value["upper_seconds"], name + " upper", low=MIN_SECONDS, high=1e9, positive=True)
        if low > high:
            raise ValueError(name + " range is reversed")
        result = {"lower": low, "estimate": math.sqrt(low * high), "upper": high,
                  "samples": 0, "kind": "user-provided estimate"}
    result["source"] = text(value["source"], name + " evidence", 1024)
    result["unit"] = "seconds"
    return result


def _poisson_cdf(k, mean):
    term = math.exp(-mean)
    total = term
    for index in range(1, k + 1):
        term *= mean / index
        total += term
    return min(1., total)


def _poisson_mean(k, probability):
    low, high = 0., max(10., 2 * k + 20.)
    for _ in range(64):
        middle = (low + high) / 2
        if _poisson_cdf(k, middle) > probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def failure_model(value):
    object_(value, set(), {"observations", "stationary", "representative", "allocation_mtbf"}, "failure model")
    records = sequence(value.get("observations", []), "exposure observations", 10000)
    counts = dict(node_failure=0, preempted=0, timeout=0, completed=0, censored=0)
    exposure, ids = 0., set()
    for row in records:
        object_(row, {"id", "exposure_seconds", "outcome"}, name="exposure observation")
        ident = text(row["id"], "attempt identity", 256)
        if ident in ids:
            raise ValueError("Exposure contains a duplicate attempt identity")
        ids.add(ident)
        if not isinstance(row["outcome"], str) or row["outcome"] not in counts:
            raise ValueError("Outcome must separate node_failure, preempted, timeout, completed and censored")
        exposure += number(row["exposure_seconds"], "allocation exposure", low=MIN_SECONDS, high=1e9, positive=True)
        counts[row["outcome"]] += 1
    result = {"counts": counts, "allocation_exposure_seconds": exposure,
              "censored_observations": len(records) - counts["node_failure"]}
    if isinstance(value.get("allocation_mtbf"), dict) and "samples_seconds" in value["allocation_mtbf"]:
        raise ValueError("Allocation MTBF must be an explicit estimated range; measured attempts require exposure and censoring observations")
    explicit = _duration(value.get("allocation_mtbf"), "allocation MTBF")
    if explicit is not None:
        result.update(rate_lower=1 / explicit["upper"], rate_estimate=1 / explicit["estimate"],
                      rate_upper=1 / explicit["lower"], method="explicit allocation MTBF estimate", input=explicit)
        return result
    if any(value.get(key) is not True for key in ("stationary", "representative")):
        return dict(result, unavailable="History needs explicit stationary and representative cohort declarations, or an independent MTBF estimate.")
    if not exposure:
        return dict(result, unavailable="No allocation exposure was supplied.")
    k = counts["node_failure"]
    if k == 0:
        return dict(result, rate_lower=0., rate_upper=-math.log(.05) / exposure,
                    method="95% one-sided zero-event Poisson bound",
                    unavailable="Zero observed node failures establish an upper hazard bound, not infinite MTBF or a finite optimal interval.")
    if k < 100:
        low, high = _poisson_mean(k - 1, .975), _poisson_mean(k, .025)
        method = "95% exact Poisson exposure interval"
    else:
        # Wilson-Hilferty approximation to the two chi-square quantiles.
        low = k * (1 - 1 / (9 * k) - 1.95996398454 / (3 * math.sqrt(k))) ** 3
        high = (k + 1) * (1 - 1 / (9 * (k + 1)) + 1.95996398454 / (3 * math.sqrt(k + 1))) ** 3
        method = "95% approximate Poisson exposure interval (Wilson-Hilferty)"
    return dict(result, rate_lower=low / exposure, rate_estimate=k / exposure,
                rate_upper=high / exposure, method=method)


def expected_cost(useful_seconds, interval_seconds, checkpoint_seconds, restart_seconds, failure_rate):
    """First-order renewal loss for fixed equal-period checkpoints and a tail."""
    count = max(0, math.ceil(math.nextafter(useful_seconds / interval_seconds, -math.inf)) - 1) if interval_seconds else 0
    if count > 10000:
        raise ValueError("Checkpoint schedule exceeds 10,000 writes")
    tail = useful_seconds - count * interval_seconds if interval_seconds else useful_seconds
    square_sum = count * interval_seconds ** 2 + tail ** 2 if interval_seconds else useful_seconds ** 2
    write = count * checkpoint_seconds
    lost = failure_rate * square_sum / 2
    restart = failure_rate * useful_seconds * restart_seconds
    return {"writes": count, "write_seconds": write, "lost_work_seconds": lost,
            "restart_seconds": restart, "overhead_seconds": write + lost + restart}


def _best(useful, cost, restart, rate):
    young = math.sqrt(2 * cost / rate)
    continuous = useful / young
    candidates = {1, max(1, min(10001, math.floor(continuous))), max(1, min(10001, math.ceil(continuous)))}
    n = min(candidates, key=lambda n: ((n - 1) * cost + rate * useful * useful / (2 * n), n))
    return {"interval_seconds": useful / n if n > 1 else None, "segments": n,
            "young_interval_seconds": young,
            "cost": expected_cost(useful, useful / n if n > 1 else None, cost, restart, rate)}


def advise(document):
    bounded(document, max_nodes=100000)
    object_(document, {"schema", "scope", "support", "useful_seconds", "failure_model"},
            {"checkpoint_seconds", "restart_seconds", "fixed_interval_seconds", "preemption_notice_seconds",
             "remaining_wall_seconds", "metadata"}, "checkpoint advice")
    if document["schema"] != "raven.checkpoint-advice/v1":
        raise ValueError("Expected raven.checkpoint-advice/v1")
    scope = document["scope"]
    object_(scope, {"partition", "nodes", "application"}, {"cluster"}, "allocation cohort")
    integer(scope["nodes"], "cohort nodes", 1, 100000)
    for key in ("partition", "application", "cluster"):
        if key in scope:
            text(scope[key], "cohort " + key, 256)
    support = document["support"]
    object_(support, {"compatible", "restart_verified"}, {"evidence"}, "checkpoint support")
    if any(type(support[key]) is not bool for key in ("compatible", "restart_verified")):
        raise ValueError("Checkpoint support declarations must be true or false")
    useful = number(document["useful_seconds"], "remaining useful work", low=MIN_SECONDS, high=1e9, positive=True)
    checkpoint = _duration(document.get("checkpoint_seconds"), "checkpoint duration")
    restart = _duration(document.get("restart_seconds"), "restart duration")
    hazard = failure_model(document["failure_model"])
    receipt = {"schema": "raven.checkpoint-advice-result/v1", "model": MODEL, "input_sha256": digest(document),
               "status": "abstain", "experimental": True, "scope": scope, "assumptions": list(ASSUMPTIONS),
               "failure_model": hazard, "durations": {"checkpoint": checkpoint, "restart": restart},
               "useful_seconds": useful, "warnings": [], "policy_events": {}}
    if not all(support[key] for key in ("compatible", "restart_verified")):
        receipt["summary"] = "No interval advised: application checkpoint compatibility and restart support must be verified."
        return receipt
    if checkpoint is None or restart is None:
        receipt["summary"] = "No interval advised: measure checkpoint/restart elapsed durations or provide labelled estimated ranges."
        return receipt
    for field, label in (("preemption_notice_seconds", "preemption"), ("remaining_wall_seconds", "wall_time")):
        if field in document:
            seconds = number(document[field], field, high=1e9)
            receipt["policy_events"][label] = {"seconds": seconds, "checkpoint_fits_estimated_budget": checkpoint["upper"] < seconds,
                "advice": "Review the application's checkpoint-on-notice/deadline hook separately; advance notice and delivery are not guaranteed."}
    if hazard.get("unavailable"):
        receipt["summary"] = hazard["unavailable"]
        return receipt
    if any(item["samples"] == 1 for item in (checkpoint, restart)):
        receipt["warnings"].append("A single measured duration cannot quantify variability; sensitivity is conditional on that value.")
    point = _best(useful, checkpoint["estimate"], restart["estimate"], hazard["rate_estimate"])
    sensitivity = []
    for cost in sorted({checkpoint["lower"], checkpoint["upper"]}):
        for rate in sorted({hazard["rate_lower"], hazard["rate_upper"]}):
            scenario = _best(useful, cost, restart["estimate"], rate)
            sensitivity.append(dict(scenario, checkpoint_seconds=cost, failure_rate=rate))
    # Every supported sensitivity scenario must remain in the stated rare-loss
    # approximation. Otherwise a plausible interval would disguise model failure.
    if any(row["failure_rate"] * ((row["interval_seconds"] or useful) + row["checkpoint_seconds"] + restart["upper"]) > .1
           for row in sensitivity):
        return dict(receipt, summary="Abstaining: plausible interruption probability exceeds the 0.1 per-segment model limit.", sensitivity=sensitivity)
    fixed = number(document.get("fixed_interval_seconds", min(3600., useful)), "fixed baseline interval", low=MIN_SECONDS, high=1e9, positive=True)
    scenarios = []
    for name, interval in (("advised", point["interval_seconds"]), ("fixed", fixed), ("none", None)):
        costs = [expected_cost(useful, interval, cost, rest, rate)
                 for cost in {checkpoint["lower"], checkpoint["upper"]}
                 for rest in {restart["lower"], restart["upper"]}
                 for rate in {hazard["rate_lower"], hazard["rate_upper"]}]
        center = expected_cost(useful, interval, checkpoint["estimate"], restart["estimate"], hazard["rate_estimate"])
        within_domain = hazard["rate_upper"] * (min(interval or useful, useful) + checkpoint["upper"] + restart["upper"]) <= .1
        if not within_domain:
            receipt["warnings"].append(name + " baseline exceeds the rare-interruption approximation; its overhead is illustrative extrapolation.")
        scenarios.append(dict(name=name, interval_seconds=interval, within_model_domain=within_domain, **center,
                              overhead_lower_seconds=min(v["overhead_seconds"] for v in costs),
                              overhead_upper_seconds=max(v["overhead_seconds"] for v in costs)))
    finite_intervals = [row["interval_seconds"] for row in sensitivity if row["interval_seconds"] is not None]
    receipt.update(status="advisory", recommendation=point, sensitivity=sensitivity, baselines=scenarios,
                   interval_range_seconds=[min(finite_intervals), max(finite_intervals)] if finite_intervals else None,
                   no_checkpoint_plausible=any(row["interval_seconds"] is None for row in sensitivity),
                   summary=("No periodic write is beneficial under the central finite-work model." if point["interval_seconds"] is None else
                            f"Review a useful-work checkpoint interval of {point['interval_seconds']:.1f} seconds; sensitivity is conditional, not a guaranteed optimum."))
    return receipt


def review_lines(receipt):
    rows = ["Checkpoint advice (experimental): " + receipt["summary"]]
    interval = receipt.get("interval_range_seconds")
    if interval:
        rows.append(f"Sensitivity interval: {interval[0]:.1f}..{interval[1]:.1f} seconds of useful work" +
                    ("; no periodic checkpoint is also plausible" if receipt.get("no_checkpoint_plausible") else ""))
    rows.extend("Assumption: " + item for item in receipt["assumptions"])
    rows.extend("Caution: " + item for item in receipt.get("warnings", ()))
    for name, item in receipt.get("policy_events", {}).items():
        rows.append(f"{name}: {item['seconds']:g}s; checkpoint fits estimated budget: {item['checkpoint_fits_estimated_budget']}. {item['advice']}")
    return rows


def simulate_first_order(useful, interval, checkpoint, restart, rate, *, seed=0, trials=1000):
    """Independent Monte Carlo check of this approximation, not live efficacy."""
    for name, value in (("useful", useful), ("checkpoint", checkpoint), ("restart", restart), ("rate", rate)):
        number(value, name, positive=True)
    integer(trials, "simulation trials", 1, 10000)
    integer(seed, "simulation seed", 0, 2**32 - 1)
    rng, results, events = random.Random(seed), [], 0
    deterministic = expected_cost(useful, interval, checkpoint, restart, rate)["write_seconds"]
    for _ in range(trials):
        at, lost = rng.expovariate(rate), 0.
        while at < useful:
            events += 1
            if events > 100000:
                raise ValueError("Simulation exceeds 100,000-event budget")
            lost += (at % interval if interval else at) + restart
            at += rng.expovariate(rate)
        results.append(deterministic + lost)
    return {"trials": trials, "seed": seed, "events": events, "mean_overhead_seconds": statistics.mean(results),
            "standard_error_seconds": statistics.stdev(results) / math.sqrt(trials) if trials > 1 else None,
            "scope": "synthetic first-order model validation; no live workload efficacy claim"}
