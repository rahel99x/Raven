"""Compile diagnostic intent against explicit site capabilities, without execution."""
from __future__ import annotations

import math
from fractions import Fraction

from .operation_support import digest
from .runtime_contracts import identifier, number, object_, sequence, text, unique_names


def compile_recipe(intent, site, *, cancel=None):
    if cancel is not None and cancel.is_set():
        raise ValueError("Diagnostic compilation cancelled")
    object_(intent, {"schema", "question", "required", "budget"}, {"approximations", "runtime"})
    object_(site, {"schema", "identity", "runtime", "adapters"})
    if intent["schema"] != "raven.diagnostic-intent/v1" or site["schema"] != "raven.site-capabilities/v1":
        raise ValueError("Expected diagnostic-intent/v1 and site-capabilities/v1")
    text(intent["question"], "question", 4096)
    text(site["identity"], "site identity", 512)
    required = unique_names(intent["required"], "required observations", 32)
    if not required:
        raise ValueError("At least one required observation must be declared")
    for runtime in (site["runtime"], intent.get("runtime", {})):
        if not isinstance(runtime, dict) or len(runtime) > 32:
            raise ValueError("Runtime fingerprint must be a bounded mapping")
        for key, value in runtime.items():
            identifier(key, "runtime key")
            text(value, "runtime fingerprint", 256)
    for key, value in intent.get("runtime", {}).items():
        if site["runtime"].get(key) != value:
            raise ValueError(f"Runtime requirement is unavailable: {key}={value}")
    budget = _cost(intent["budget"])
    approximations = intent.get("approximations", {})
    if not isinstance(approximations, dict) or set(approximations) - set(required):
        raise ValueError("Approximations must name a required observation")
    alternatives = {key: set(unique_names(approximations.get(key, []), "explicitly permitted approximations", 16)) for key in required}
    adapters = sequence(site["adapters"], "adapters", 16)
    accepted, rejected, names = [], [], set()
    for adapter in adapters:
        object_(adapter, {"id", "provides", "requires", "conflicts", "cost", "permitted", "argv"})
        name = identifier(adapter["id"], "adapter ID")
        if name in names:
            raise ValueError("Duplicate adapter ID")
        names.add(name)
        _provides = unique_names(adapter["provides"], "provided observations", 64)
        unique_names(adapter["conflicts"], "adapter conflicts", 16)
        _cost(adapter["cost"])
        if not isinstance(adapter["permitted"], bool):
            raise ValueError("Adapter permitted must be a boolean")
        requirements = adapter["requires"]
        if not isinstance(requirements, dict) or len(requirements) > 32:
            raise ValueError("Adapter requirements must be runtime fingerprints")
        for key, value in requirements.items():
            identifier(key)
            text(value, "required runtime fingerprint", 256)
        argv = sequence(adapter["argv"], "adapter argv", 64, 1)
        for argument in argv:
            text(argument, "adapter argument", 4096)
        # Plans show exact commands but never execute capability-file contents.
        reasons = []
        if not adapter["permitted"]:
            reasons.append("site policy does not permit this adapter")
        if any(site["runtime"].get(key) != value for key, value in requirements.items()):
            reasons.append("runtime fingerprint differs")
        if reasons:
            rejected.append({"id": name, "reasons": reasons})
        else:
            accepted.append(adapter)
    if any(set(adapter["conflicts"]) - names for adapter in adapters):
        raise ValueError("Adapter conflicts reference an unknown adapter")
    # At most 2^16 alternatives. Optimize approximation count first, then resource
    # fractions and number of adapters. This is an exact bounded set-cover search.
    best = None
    for mask in range(1, 1 << len(accepted)):
        if mask % 128 == 0 and cancel is not None and cancel.is_set():
            raise ValueError("Diagnostic compilation cancelled")
        chosen = [adapter for index, adapter in enumerate(accepted) if mask & (1 << index)]
        chosen_names = {adapter["id"] for adapter in chosen}
        if any(set(adapter["conflicts"]) & chosen_names for adapter in chosen):
            continue
        cost = {key: sum(adapter["cost"][key] for adapter in chosen) for key in budget}
        if any(cost[key] > budget[key] for key in budget):
            continue
        available = {item for adapter in chosen for item in adapter["provides"]}
        coverage = {}
        for needed in required:
            if needed in available:
                coverage[needed] = {"observed": needed, "exact": True}
            elif alternatives[needed] & available:
                coverage[needed] = {"observed": sorted(alternatives[needed] & available)[0], "exact": False}
        if len(coverage) != len(required):
            continue
        score = (sum(not value["exact"] for value in coverage.values()),
                 sum(cost[key] / budget[key] if budget[key] else 0 for key in budget), len(chosen), sorted(chosen_names))
        if best is None or score < best[0]:
            best = score, chosen, cost, coverage
    if best is None:
        available = {item for adapter in accepted for item in adapter["provides"]}
        missing = [needed for needed in required if needed not in available and not alternatives[needed] & available]
        raise ValueError("No permitted diagnostic plan satisfies every mandatory observation, conflicts and budget" + (": missing " + ", ".join(missing) if missing else ""))
    _, chosen, cost, coverage = best
    return {"schema": "raven.diagnostic-plan/v1", "intent_digest": digest(intent), "site_digest": digest(site),
            "question": intent["question"], "site": site["identity"], "runtime": site["runtime"],
            "adapters": chosen, "estimated_cost": cost, "coverage": coverage, "rejected": rejected,
            "executable": False, "limitations": ["Capabilities and costs are declared evidence, not live certification.",
                                                      "Commands are reviewable recipes only; Raven does not execute arbitrary manifest commands.",
                                                      "No approximation is permitted unless explicitly listed in the intent."]}


def _cost(value):
    object_(value, {"cpu_fraction", "memory_bytes", "disk_bytes", "seconds"}, name="resource budget")
    for key, limit in (("cpu_fraction", 1024), ("memory_bytes", 2**60), ("disk_bytes", 2**60), ("seconds", 604800)):
        number(value[key], key, high=limit)
    return value


def summarize_query(document, query):
    """Exact count/sum/extrema over declared numeric columns, retaining provenance.

    This deliberately supports only its advertised query family: arbitrary
    quantiles, joins, correlations, and reconstruction require raw data.
    """
    object_(document, {"schema", "identity", "rows", "coverage"})
    object_(query, {"schema", "fields", "operations"})
    if document["schema"] != "raven.query-source/v1" or query["schema"] != "raven.summary-query/v1":
        raise ValueError("Expected query-source/v1 and summary-query/v1")
    text(document["identity"], "source identity", 512)
    object_(document["coverage"], {"complete", "reason"})
    if not isinstance(document["coverage"]["complete"], bool):
        raise ValueError("Coverage complete must be boolean")
    text(document["coverage"]["reason"], "coverage explanation", 1024)
    fields = unique_names(query["fields"], "query fields", 32)
    operations = unique_names(query["operations"], "query operations", 4)
    if not fields or not operations or set(operations) - {"count", "sum", "min", "max"}:
        raise ValueError("Choose fields and count, sum, min or max only")
    rows = sequence(document["rows"], "source rows", 50000)
    values = {key: [] for key in fields}
    refs, gaps = [], {key: 0 for key in fields}
    for row in rows:
        object_(row, {"ref", "values"})
        ref = text(row["ref"], "raw reference", 1024)
        if not isinstance(row["values"], dict) or len(row["values"]) > 64:
            raise ValueError("Row values must be a bounded mapping")
        refs.append(ref)
        for key in fields:
            value = row["values"].get(key)
            if value is None:
                gaps[key] += 1
            else:
                number(value, key, low=-1e100)
                values[key].append(value)
    if len(set(refs)) != len(refs):
        raise ValueError("Raw references must uniquely identify rows")
    summaries, exact_sums = {}, {}
    for key, items in values.items():
        total = math.fsum(items)
        if not math.isfinite(total):
            raise ValueError("Summary exceeds finite numerical range")
        supported = {"count": len(items), "sum": total, "min": min(items) if items else None, "max": max(items) if items else None}
        summaries[key] = {operation: supported[operation] for operation in operations}
        if "sum" in operations:
            exact = sum((Fraction(value) for value in items), Fraction())
            exact_sums[key] = {"numerator": str(exact.numerator), "denominator": str(exact.denominator)}
    return {"schema": "raven.query-summary/v1", "source_digest": digest(document), "query": query,
            "summaries": summaries, "null_or_missing": gaps, "raw_references": refs,
            "coverage": document["coverage"], "rows": len(rows), "exact_sums": exact_sums,
            "precision": "Counts and rational sums are exact over supplied stored numbers; display sums use floating-point accumulation. JSON decimals already parsed as floats retain their binary value, not arbitrary decimal precision."}
