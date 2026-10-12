"""Dependency-free, bounded comparisons of explicitly identified experimental units."""
from __future__ import annotations

import math
import statistics


def number(value, label, *, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} is out of range") from exc
    if not math.isfinite(value) or abs(value) > 1e100 or (nonnegative and value < 0):
        raise ValueError(f"{label} must be finite, within ±1e100" + (", and nonnegative" if nonnegative else ""))
    return value


def _betacf(a, b, x):
    """Modified Lentz continued fraction; positive beta parameters only."""
    tiny = 1e-300
    c, d = 1.0, 1 - (a + b) * x / (a + 1)
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 301):
        for aa in (m * (b - m) * x / ((a + 2*m - 1) * (a + 2*m)),
                   -(a + m) * (a + b + m) * x / ((a + 2*m) * (a + 2*m + 1))):
            d = 1 + aa * d
            c = 1 + aa / c
            d = 1 / (d if abs(d) > tiny else tiny)
            c = c if abs(c) > tiny else tiny
            delta = d * c
            h *= delta
        if abs(delta - 1) < 2e-14:
            return h
    raise ValueError("Student t calculation did not converge")


def _beta(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    factor = math.exp(math.lgamma(a+b) - math.lgamma(a) - math.lgamma(b) + a*math.log(x) + b*math.log1p(-x))
    if x < (a+1)/(a+b+2):
        return factor * _betacf(a, b, x) / a
    return 1 - factor * _betacf(b, a, 1-x) / b


def t_survival(t, df):
    """Two-sided Student t tail probability."""
    return max(0.0, min(1.0, _beta(df/2, .5, df/(df+t*t))))


def t_critical(df, confidence=.95):
    lo, hi = 0.0, 1.0
    alpha = 1-confidence
    while t_survival(hi, df) > alpha:
        hi *= 2
        if hi > 1e6:
            raise ValueError("Student t confidence interval is unbounded")
    for _ in range(70):
        mid = (lo+hi)/2
        if t_survival(mid, df) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo+hi)/2


def _samples(rows, label):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100000:
        raise ValueError(f"{label} must contain 1–100000 experimental units")
    values = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "value"}:
            raise ValueError(f"{label} rows require exactly id and value")
        key = row["id"]
        if not isinstance(key, str) or not key.strip() or len(key) > 256 or key in values:
            raise ValueError(f"{label} unit IDs must be nonempty and unique")
        values[key] = number(row["value"], f"{label} {key}")
    return values


def compare(document):
    required = {"schema", "design", "unit", "control", "treatment"}
    if not isinstance(document, dict) or set(document) - (required | {"confidence"}) or not required <= set(document):
        raise ValueError("Statistics require schema, design, unit, control, treatment and optional confidence")
    if document["schema"] != "raven.statistics/v1" or document["design"] not in {"paired", "independent"}:
        raise ValueError("Use raven.statistics/v1 with independent or paired design")
    if not isinstance(document["unit"], str) or not document["unit"].strip() or len(document["unit"]) > 256:
        raise ValueError("Name the independent experimental unit")
    confidence = number(document.get("confidence", .95), "confidence")
    if not .8 <= confidence <= .999:
        raise ValueError("confidence must be within 0.8–0.999")
    a, b = _samples(document["control"], "control"), _samples(document["treatment"], "treatment")
    paired = document["design"] == "paired"
    if paired and a.keys() != b.keys():
        raise ValueError("Paired comparisons require exactly matching unit IDs; missing pairs cannot be dropped")
    if not paired and a.keys() & b.keys():
        raise ValueError("Independent groups cannot reuse a unit ID; use paired design for repeated units")
    mean_a, mean_b = statistics.fmean(a.values()), statistics.fmean(b.values())
    difference = mean_b - mean_a
    result = {"design": document["design"], "unit": document["unit"], "control_n": len(a), "treatment_n": len(b),
              "control_mean": mean_a, "treatment_mean": mean_b, "difference": difference,
              "confidence": confidence, "interval": None, "p_value": None, "df": None,
              "method": "paired Student t" if paired else "Welch Student t", "warnings": []}
    if min(len(a), len(b)) < 2:
        result["warnings"].append("At least two independent units per group are needed to estimate uncertainty")
        return result
    if paired:
        differences = [b[key]-value for key, value in a.items()]
        difference = statistics.fmean(differences)
        result["difference"] = difference
        variance = statistics.variance(differences)/len(differences)
        df = len(differences)-1
    else:
        va, vb = statistics.variance(a.values())/len(a), statistics.variance(b.values())/len(b)
        variance = va+vb
        denominator = (va/variance)**2/(len(a)-1) + (vb/variance)**2/(len(b)-1) if variance else 0
        df = 1/denominator if denominator else None
    if variance == 0:
        result["warnings"].append("Observed variance is zero; inferential confidence and p-values are not estimable")
        return result
    if df is None or not math.isfinite(df):
        raise ValueError("Variance could not be estimated reliably")
    error = math.sqrt(variance)
    radius = t_critical(df, confidence)*error
    result.update(interval=[difference-radius, difference+radius], p_value=t_survival(difference/error, df), df=df)
    if min(len(a), len(b)) < 10:
        result["warnings"].append("Small sample: the t interval assumes suitable experimental independence and distribution")
    result["warnings"].append("Exploratory single comparison; no multiple-comparison correction or causal claim")
    return result


def acceptance(specification, results):
    if not isinstance(specification, dict) or set(specification) != {"schema", "cases"} or specification["schema"] != "raven.acceptance/v1":
        raise ValueError("Expected raven.acceptance/v1 with cases")
    if not isinstance(results, dict) or set(results) != {"schema", "runs"} or results["schema"] != "raven.results/v1":
        raise ValueError("Expected raven.results/v1 with runs")
    cases, runs = specification["cases"], results["runs"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 10000 or not isinstance(runs, list) or len(runs) > 10000:
        raise ValueError("Acceptance cases and runs are bounded to 10000 records")
    observed = {}
    for row in runs:
        if not isinstance(row, dict) or set(row) != {"id", "metrics"} or not isinstance(row["id"], str) or not row["id"] or not isinstance(row["metrics"], dict):
            raise ValueError("Each result needs id and metrics")
        if row["id"] in observed:
            raise ValueError("Duplicate result run ID")
        observed[row["id"]] = row["metrics"]
    matrix, seen = [], set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"id", "expected"} or not isinstance(case["id"], str) or not case["id"] or not isinstance(case["expected"], dict) or not case["expected"]:
            raise ValueError("Each acceptance case needs id and a nonempty expected mapping")
        if case["id"] in seen:
            raise ValueError("Duplicate acceptance case ID")
        seen.add(case["id"])
        for metric, tolerance in case["expected"].items():
            if not isinstance(metric, str) or not metric or len(metric) > 256 or not isinstance(tolerance, dict) or set(tolerance) != {"value", "absolute", "relative"}:
                raise ValueError("Each metric needs value, absolute and relative tolerance")
            target = number(tolerance["value"], "expected value")
            absolute = number(tolerance["absolute"], "absolute tolerance", nonnegative=True)
            relative = number(tolerance["relative"], "relative tolerance", nonnegative=True)
            allowed = max(absolute, relative*abs(target))
            actual = observed.get(case["id"], {}).get(metric)
            error, reason = None, "missing"
            try:
                actual = number(actual, "observed value")
                error = abs(actual-target)
                reason = "pass" if error <= allowed else "outside tolerance"
            except (ValueError, OverflowError):
                reason = "missing" if actual is None else "invalid nonfinite/nonnumeric result"
                actual = None
            matrix.append({"case": case["id"], "metric": metric, "expected": target, "observed": actual,
                           "allowed_error": allowed, "absolute_error": error, "pass": reason == "pass", "reason": reason})
            if len(matrix) > 100000:
                raise ValueError("Acceptance matrix exceeds 100000 cells")
    return {"passed": sum(row["pass"] for row in matrix), "total": len(matrix), "matrix": matrix,
            "unrequested_runs": sorted(observed.keys()-seen)}
