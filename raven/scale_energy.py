"""Energy accounting with explicit attribution and failed-attempt costs."""
from __future__ import annotations

from .scale_common import document, identity, items, number, text, timestamp, display

UNITS = {"J": 1.0, "kJ": 1000.0, "Wh": 3600.0, "kWh": 3600000.0}


def integrate(series):
    if not isinstance(series, dict):
        raise ValueError("energy must be an object")
    scope = series.get("scope")
    if scope not in ("job", "node", "gpu"):
        raise ValueError("energy scope must be job, node, or gpu")
    attribution = series.get("attribution")
    if attribution not in ("exclusive", "shared", "allocated_fraction"):
        raise ValueError("energy attribution must be exclusive, shared, or allocated_fraction")
    fraction = number(series.get("fraction"), "fraction") if attribution == "allocated_fraction" else 1.0
    if not 0 < fraction <= 1:
        raise ValueError("fraction must be in (0, 1]")
    source = text(series.get("source"), "energy source")
    kind = series.get("kind", "total")
    warnings, gaps, resets = [], 0, 0
    full_window = False
    if kind == "total":
        unit = series.get("unit")
        if unit not in UNITS:
            raise ValueError("energy unit must be J, kJ, Wh, or kWh")
        raw = number(series.get("value"), "energy value", optional=True)
        total = raw * UNITS[unit] if raw is not None else None
        coverage = None
        full_window = series.get("coverage_complete") is True
    elif kind in ("counter", "power"):
        unit = series.get("unit")
        if (kind == "power" and unit != "W") or (kind == "counter" and unit not in UNITS):
            raise ValueError("power requires W; counter requires an energy unit")
        samples = items(series.get("samples"), "samples", limit=100000, empty=False)
        max_gap = number(series.get("max_gap_s", 300), "max_gap_s")
        if max_gap <= 0:
            raise ValueError("max_gap_s must be positive")
        window_start = timestamp(series["window_start"]) if series.get("window_start") is not None else None
        window_end = timestamp(series["window_end"]) if series.get("window_end") is not None else None
        if (window_start is None) != (window_end is None) or (window_start is not None and window_end <= window_start):
            raise ValueError("Energy window requires both ordered window_start and window_end")
        previous, total, coverage, intervals, first = None, 0.0, 0.0, 0, None
        for sample in samples:
            now = timestamp(sample.get("t"))
            if first is None:
                first = now
            if window_start is not None and not window_start <= now <= window_end:
                raise ValueError("Energy samples must lie inside the declared window")
            value = number(sample.get("value"), "sample value", optional=True)
            if previous is not None:
                prior_t, prior_v = previous
                delta = now - prior_t
                if delta <= 0:
                    raise ValueError("Energy timestamps must strictly increase")
                if value is None or prior_v is None or delta > max_gap:
                    gaps += 1
                elif kind == "counter" and value < prior_v:
                    resets += 1
                else:
                    total += ((value - prior_v) * UNITS[unit] if kind == "counter" else (value + prior_v) * delta / 2)
                    coverage += delta
                    intervals += 1
            previous = (now, value)
        if not intervals:
            total = None
        full_window = window_start is not None and first == window_start and previous[0] == window_end
        if gaps:
            warnings.append(f"{gaps} missing or overlong intervals omitted")
        if resets:
            warnings.append(f"{resets} counter resets omitted; reset intervals cannot be reconstructed")
    else:
        raise ValueError("energy kind must be total, counter, or power")
    if total is not None:
        total = number(total * fraction, "integrated energy")
    if not full_window:
        warnings.append("Complete attempt coverage is not established; declare complete total coverage or a fully sampled attempt window")
    if attribution == "shared":
        warnings.append("Shared node/device energy cannot be attributed to this job")
    if attribution == "allocated_fraction":
        warnings.append("Allocation-fraction energy is an estimate, not measured job energy")
    return {"joules": total, "scope": scope, "attribution": attribution, "source": source,
            "coverage_s": coverage, "gaps": gaps, "resets": resets, "warnings": warnings,
            "complete": total is not None and full_window and not gaps and not resets and attribution != "shared"}


def analyze(value):
    document(value, "raven.energy/v1")
    unit = text(value.get("work_unit"), "work_unit", limit=80)
    attempts = items(value.get("attempts"), "attempts", limit=10000, empty=False)
    seen, results, rows, warnings = set(), [], [], []
    total, useful, failed_energy, incomplete = 0.0, 0.0, 0.0, False
    scopes, estimated = set(), False
    for attempt in attempts:
        key = identity(attempt)
        if key in seen:
            raise ValueError("Duplicate cluster/job/attempt energy record")
        seen.add(key)
        state = text(attempt.get("state"), "state")
        accepted = number(attempt.get("accepted_work"), "accepted_work", optional=True)
        energy = integrate(attempt["energy"]) if attempt.get("energy") is not None else None
        if accepted is None:
            incomplete = True
        else:
            useful += accepted
        attributed = energy is not None and energy["attribution"] != "shared"
        joules = energy["joules"] if attributed else None
        if joules is None or not energy["complete"]:
            incomplete = True
        if joules is not None:
            total += joules
            scopes.add(energy["scope"])
            estimated |= energy["attribution"] == "allocated_fraction"
            if state not in ("COMPLETED", "SUCCEEDED"):
                failed_energy += joules
        if energy:
            warnings.extend(f"{key[0]}/{key[1]}/{key[2]}: {warning}" for warning in energy["warnings"])
        result = dict(cluster=key[0], job_id=key[1], attempt=key[2], state=state,
                      accepted_work=accepted, energy=energy, attributed_joules=joules)
        results.append(result)
        rows.append(f"{key[0]} / {key[1]} / {key[2]}: {state}; {display(joules, ' J')}; accepted {display(accepted)} {unit}")
    number(total, "campaign energy")
    number(useful, "campaign useful work")
    if len(scopes) > 1:
        incomplete = True
        warnings.append("Mixed job/node/GPU boundaries: campaign efficiency is unavailable until the accounting boundary is consistent")
    ratio = total / useful if useful > 0 and not incomplete else None
    if incomplete:
        warnings.append("Incomplete evidence: observed attributed energy is a partial total; campaign efficiency is unknown")
    if useful == 0:
        warnings.append("No accepted useful work; energy per result is undefined")
    rows[:0] = [f"Attributed energy: {display(total, ' J')} ({'partial' if incomplete else 'complete'}); non-success attempt energy: {display(failed_energy, ' J')}",
                f"Accepted work: {display(useful)} {unit}; energy per accepted unit: {display(ratio, ' J/' + unit)}"]
    return {"attempts": results, "observed_joules": total, "failed_attempt_joules": failed_energy,
            "accepted_work": useful, "work_unit": unit, "joules_per_work": ratio,
            "complete": not incomplete, "estimated": estimated, "rows": rows, "warnings": warnings}
