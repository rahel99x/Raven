"""Measured numerical robustness and noise-aware regression investigations."""
from __future__ import annotations

import math
import statistics

from .science_contracts import canonical, number, obj, rows, text
from .science_stats import compare


def numerical(document):
    canonical(document)
    obj(document, {"schema", "metrics", "runs"})
    if document["schema"] != "raven.numerical-stability/v1":
        raise ValueError("Expected raven.numerical-stability/v1")
    policies = {}
    for policy in rows(document["metrics"], "metric policies", 128, 1):
        obj(policy, {"name", "reference", "absolute", "relative"}, label="metric policy")
        key = text(policy["name"])
        if key in policies:
            raise ValueError("Duplicate metric policy")
        policies[key] = {"reference": number(policy["reference"]),
                         "absolute": number(policy["absolute"], "absolute tolerance", 0),
                         "relative": number(policy["relative"], "relative tolerance", 0)}
    groups, seen = {}, set()
    for run in rows(document["runs"], "independent runs", 10000, 1):
        obj(run, {"id", "configuration", "metrics"}, label="run")
        key, config = text(run["id"]), text(run["configuration"])
        if key in seen or not isinstance(run["metrics"], dict) or set(run["metrics"]) - policies.keys():
            raise ValueError("Run IDs must be unique and metrics declared")
        seen.add(key)
        for value in run["metrics"].values():
            number(value, "measured metric")
        groups.setdefault(config, []).append(run)
    output = []
    for config, runs in sorted(groups.items()):
        for name, policy in policies.items():
            values = [number(run["metrics"][name]) for run in runs if name in run["metrics"]]
            missing = [run["id"] for run in runs if name not in run["metrics"]]
            tolerance = max(policy["absolute"], policy["relative"]*abs(policy["reference"]))
            if not math.isfinite(tolerance):
                raise ValueError("Tolerance overflow")
            violations = [run["id"] for run in runs if name in run["metrics"] and abs(run["metrics"][name]-policy["reference"]) > tolerance]
            output.append({"configuration": config, "metric": name, "n": len(values),
                           "minimum": min(values) if values else None, "maximum": max(values) if values else None,
                           "mean": statistics.fmean(values) if values else None,
                           "standard_deviation": statistics.stdev(values) if len(values) > 1 else None,
                           "max_absolute_error": max((abs(v-policy["reference"]) for v in values), default=None),
                           "tolerance": tolerance, "missing": missing, "violations": violations,
                           "state": "failed" if violations else "unknown" if missing else "observed-pass"})
    return {"measurements": output, "state": "failed" if any(r["violations"] for r in output) else "unknown" if any(r["missing"] for r in output) else "observed-pass",
            "limit": "Observed checks do not prove numerical stability outside these inputs/configurations. Seeds and experimental independence must be established by the producer."}


def regression(document):
    canonical(document)
    obj(document, {"schema", "unit", "design", "baseline", "versions", "minimum_change"}, {"confidence"})
    if document["schema"] != "raven.performance-investigation/v1":
        raise ValueError("Expected raven.performance-investigation/v1")
    text(document["unit"])
    if document["design"] not in {"paired", "independent"}:
        raise ValueError("Comparison design must be paired or independent")
    threshold = number(document["minimum_change"], "practical change threshold", 0)
    confidence = number(document.get("confidence", .95), "family confidence", .8, .95)
    versions = rows(document["versions"], "ordered versions", 32, 2)
    seen = set()
    for version in versions:
        obj(version, {"id", "samples", "scientific_valid"}, label="version")
        if text(version["id"]) in seen or version["scientific_valid"] not in (True, False, None) or isinstance(version["scientific_valid"], (int, float)) and not isinstance(version["scientific_valid"], bool):
            raise ValueError("Version IDs must be unique; scientific_valid must be true, false or null")
        seen.add(version["id"])
        for sample in rows(version["samples"], "samples", 10000, 1):
            obj(sample, {"id", "value"}, label="sample")
            text(sample["id"])
            number(sample["value"], "runtime", 0)
    baseline = next((v for v in versions if v["id"] == document["baseline"]), None)
    if baseline is None:
        raise ValueError("Baseline must identify one ordered version")
    adjusted = 1-(1-confidence)/(len(versions)-1)
    comparisons = []
    for version in versions:
        if version is baseline:
            continue
        analysis = compare({"schema": "raven.statistics/v1", "unit": document["unit"], "design": document["design"],
                            "control": baseline["samples"], "treatment": version["samples"], "confidence": adjusted})
        interval = analysis["interval"]
        if baseline["scientific_valid"] is not True or version["scientific_valid"] is not True:
            state = "scientifically-incomparable"
        elif interval is None:
            state = "inconclusive"
        elif interval[0] > threshold:
            state = "regression"
        elif interval[1] < -threshold:
            state = "improvement"
        elif interval[0] >= -threshold and interval[1] <= threshold:
            state = "within-declared-margin"
        else:
            state = "inconclusive"
        comparisons.append({"version": version["id"], "state": state, **analysis})
    first = next((item["version"] for item in comparisons if item["state"] == "regression"), None)
    regressed, nonmonotonic = False, False
    for item in comparisons:
        if item["state"] == "regression":
            regressed = True
        elif regressed and item["state"] in {"improvement", "within-declared-margin"}:
            nonmonotonic = True
    return {"baseline": baseline["id"], "comparisons": comparisons, "first_observed_regression": first,
            "nonmonotonic": nonmonotonic, "family_confidence": confidence,
            "method": "Student t comparisons with Bonferroni family adjustment; supplied version order, no binary-search assumption",
            "limit": "A regression association does not identify a causal source commit. Missing science checks, noisy or zero-variance samples remain inconclusive."}


def factorial(document):
    """Analyze an explicit randomized factorial design with observed controls."""
    from .experiment_design import analyze
    return analyze(document)


def factorial_plan(document):
    """Compile an ordinary reviewed campaign; planning executes no program."""
    from .experiment_design import prepare
    return prepare(document)
