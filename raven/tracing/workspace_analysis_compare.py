"""Declared workload comparisons with source/build-safe function matching."""

from __future__ import annotations

from fractions import Fraction

from .workspace_analysis_common import (
    _data,
    integer,
    metric,
    partial,
    scope_identity,
    text,
    validate_rows,
)

CONTEXT_SCHEMA = "raven.trace-comparison-context/v1"
_UNITS = frozenset({"logical-cpu", "physical-core", "gpu", "rank", "process", "worker"})
_MAX_GROUPS = 512


def _groups(query):
    from .workspace_model_metrics import _symbol, function_statistics

    rows, _ = validate_rows(query)
    statistics = function_statistics(query, limit=1000)
    metadata = {}
    for row in rows.values():
        if row.get("kind") not in {
            "python",
            "cooperative",
            "host",
            "gpu_kernel",
            "gpu_copy",
            "gpu_sync",
            "gpu_synchronization",
        }:
            continue
        symbol = _symbol(row)
        info = metadata.setdefault(symbol["id"], {"build_ids": set()})
        build = _data(row).get("build_id", row.get("build_id"))
        if isinstance(build, str) and build:
            info["build_ids"].add(build)
    for group in statistics["groups"]:
        group["recorded_build_ids"] = sorted(
            metadata.get(group["id"], {}).get("build_ids", ())
        )
    return statistics


def _context(value, current, baseline):
    reasons = []
    if value is None:
        return {}, ["No workload/input/build/resource comparison context was supplied"]
    if not isinstance(value, dict) or value.get("schema") != CONTEXT_SCHEMA:
        raise ValueError("Unsupported trace comparison context schema")
    try:
        provenance = text(value.get("provenance"), "comparison provenance", 2048)
    except ValueError as exc:
        provenance = "unavailable"
        reasons.append(str(exc))
    for flag in ("workload_comparable", "inputs_comparable", "builds_comparable"):
        if value.get(flag) is not True:
            reasons.append(flag + " requires an explicit true declaration")
    descriptors = {}
    for side, query in (("current", current), ("baseline", baseline)):
        supplied = value.get(side)
        if not isinstance(supplied, dict):
            reasons.append(side + " comparison descriptor is missing")
            continue
        descriptor = dict(supplied)
        for name in ("workload", "input", "build", "scope", "elapsed_provenance"):
            try:
                descriptor[name] = text(supplied.get(name), side + " " + name, 1024)
            except ValueError as exc:
                reasons.append(str(exc))
        try:
            descriptor["elapsed_ns"] = integer(
                supplied.get("elapsed_ns"), side + " reference elapsed", minimum=1
            )
        except ValueError as exc:
            reasons.append(str(exc))
        resources = supplied.get("resources")
        if not isinstance(resources, dict):
            reasons.append(side + " resource descriptor is missing")
        else:
            resources = dict(resources)
            try:
                resources["count"] = integer(
                    resources.get("count"),
                    side + " resource count",
                    minimum=1,
                    maximum=1_000_000,
                )
                resources["model"] = text(
                    resources.get("model"), side + " resource model", 512
                )
                if resources.get("unit") not in _UNITS:
                    raise ValueError("Unsupported " + side + " resource unit")
            except ValueError as exc:
                reasons.append(str(exc))
            descriptor["resources"] = resources
        rows, _ = validate_rows(query)
        observed = {
            _data(row).get("build_id", row.get("build_id")) for row in rows.values()
        }
        observed = {item for item in observed if isinstance(item, str) and item}
        permitted = descriptor.get("recorded_builds", [descriptor.get("build")])
        if (
            not isinstance(permitted, list)
            or len(permitted) > 256
            or any(
                not isinstance(item, str) or not item or len(item) > 1024
                for item in permitted
            )
        ):
            reasons.append(side + " recorded build list is invalid")
        elif not observed <= set(permitted):
            reasons.append(
                side + " build declaration disagrees with recorded symbol build IDs"
            )
        descriptors[side] = descriptor
    if len(descriptors) == 2:
        a, b = descriptors["current"], descriptors["baseline"]
        for field in ("workload", "input", "scope"):
            if a.get(field) != b.get(field):
                reasons.append("Current and baseline " + field + " identifiers differ")
    return dict(value, **descriptors, provenance=provenance), list(
        dict.fromkeys(reasons)
    )


def _source_pairs(context, current, baseline):
    pairs = context.get("source_pairs", [])
    if not isinstance(pairs, list) or len(pairs) > 512:
        raise ValueError("Comparison source mappings exceed their bounded list")
    current_sources = {source for group in current for source in group["source_ids"]}
    baseline_sources = {source for group in baseline for source in group["source_ids"]}
    mapping = ({}, {})
    for position, pair in enumerate(pairs):
        if not isinstance(pair, dict) or set(pair) - {
            "current",
            "baseline",
            "provenance",
        }:
            raise ValueError("Invalid comparison source mapping")
        a, b = pair.get("current"), pair.get("baseline")
        if a not in current_sources or b not in baseline_sources:
            raise ValueError(
                "Comparison source mapping references an unavailable source"
            )
        if a in mapping[0] or b in mapping[1]:
            raise ValueError("Comparison source mappings must be one-to-one")
        mapping[0][a], mapping[1][b] = position, position
    return mapping


def _symbol_key(group, mappings):
    sources = group["source_ids"]
    explicit = [mappings[source] for source in sources if source in mappings]
    tail = group["kind"], group["name"], group["file"], group["line"]
    if len(sources) == len(explicit) == 1:
        return ("declared-source-pair", explicit[0], *tail)
    if len(group["recorded_build_ids"]) == 1:
        return ("recorded-build", group["recorded_build_ids"][0], *tail)
    return ("source-lifetime", tuple(sources), *tail)


def _values(group):
    return {
        name: group.get(name)
        for name in (
            "id",
            "count",
            "inclusive_ns",
            "self_ns",
            "min_ns",
            "max_ns",
            "source_ids",
            "clock_ids",
            "recorded_build_ids",
        )
    }


def _metrics(context, reasons):
    if reasons:
        reason = "; ".join(reasons)
        return [
            metric("reference_speedup", None, unit="ratio", reason=reason),
            metric("parallel_efficiency", None, reason=reason),
        ]
    current, baseline = context["current"], context["baseline"]
    a, b = current["elapsed_ns"], baseline["elapsed_ns"]
    speedup = Fraction(b, a)
    speedup_metric = metric(
        "reference_speedup",
        float(speedup),
        unit="ratio",
        numerator=speedup.numerator,
        denominator=speedup.denominator,
        scope=current["scope"],
        provenance=context["provenance"],
        meaning="Ratio of declared comparable reference elapsed times, not proof of an optimization's cause",
    )
    resources, reference = current["resources"], baseline["resources"]
    mismatch = None
    if resources.get("unit") != reference.get("unit") or resources.get(
        "model"
    ) != reference.get("model"):
        mismatch = (
            "Resource units/models differ; counts do not establish equivalent capacity"
        )
    elif reference.get("count") != 1:
        mismatch = "Parallel efficiency needs a declared single-resource baseline"
    if mismatch:
        return [speedup_metric, metric("parallel_efficiency", None, reason=mismatch)]
    efficiency = Fraction(b, a * resources["count"])
    return [
        speedup_metric,
        metric(
            "parallel_efficiency",
            float(efficiency),
            numerator=efficiency.numerator,
            denominator=efficiency.denominator,
            resource_count=resources["count"],
            resource_unit=resources["unit"],
            resource_model=resources["model"],
            scope=current["scope"],
            provenance=context["provenance"],
            meaning="T1 / (P × TP) using user-declared comparable timings/resources; values above one are not clamped or attributed to a cause",
        ),
    ]


def compare(query, baseline, *, context=None):
    current_statistics, baseline_statistics = _groups(query), _groups(baseline)
    current, reference = current_statistics["groups"], baseline_statistics["groups"]
    checked, reasons = _context(context, query, baseline)
    mapping = _source_pairs(checked, current, reference)
    first, last = defaultdict_list(), defaultdict_list()
    for group in current:
        first.setdefault(_symbol_key(group, mapping[0]), []).append(group)
    for group in reference:
        last.setdefault(_symbol_key(group, mapping[1]), []).append(group)
    functions, matched_a, matched_b = [], set(), set()
    if not reasons:
        for identifier in sorted(set(first) & set(last), key=repr):
            # Multiple groups sharing a declared semantic key are ambiguous;
            # never collapse a pair of unknown binaries into one function.
            if len(first[identifier]) != 1 or len(last[identifier]) != 1:
                continue
            a, b = first[identifier][0], last[identifier][0]
            ratio = (
                Fraction(a["inclusive_ns"], b["inclusive_ns"])
                if b["inclusive_ns"]
                else None
            )
            functions.append(
                dict(
                    name=a["name"],
                    kind=a["kind"],
                    file=a["file"],
                    line=a["line"],
                    current=_values(a),
                    baseline=_values(b),
                    delta_inclusive_ns=a["inclusive_ns"] - b["inclusive_ns"],
                    ratio=float(ratio) if ratio is not None else None,
                    ratio_exact={
                        "numerator": ratio.numerator,
                        "denominator": ratio.denominator,
                    }
                    if ratio is not None
                    else None,
                    reason=None
                    if ratio is not None
                    else "A zero baseline duration has no finite relative ratio",
                    scope="Matched "
                    + str(identifier[0])
                    + "; complete recorded calls intersecting the supplied query, not inferred whole-job speedup",
                )
            )
            matched_a.add(a["id"])
            matched_b.add(b["id"])
    omitted = max(0, len(functions) - _MAX_GROUPS)
    functions = functions[:_MAX_GROUPS]
    current_unmatched = [
        _values(group) | {"name": group["name"], "kind": group["kind"]}
        for group in current
        if group["id"] not in matched_a
    ]
    baseline_unmatched = [
        _values(group) | {"name": group["name"], "kind": group["kind"]}
        for group in reference
        if group["id"] not in matched_b
    ]
    incomplete = (
        partial(query)
        or partial(baseline)
        or bool(
            current_statistics["omitted_groups"]
            or baseline_statistics["omitted_groups"]
            or omitted
        )
    )
    return dict(
        schema="raven.trace-comparison/v1",
        status="unavailable" if reasons else "partial" if incomplete else "comparable",
        identity=scope_identity(query),
        baseline_identity=scope_identity(baseline),
        comparability=dict(
            available=not reasons,
            reasons=reasons,
            provenance=checked.get("provenance", "unavailable"),
            scope="User-declared workload/input/build comparability; not an independently controlled benchmark",
        ),
        functions=functions,
        unmatched_current=current_unmatched[:_MAX_GROUPS],
        unmatched_baseline=baseline_unmatched[:_MAX_GROUPS],
        omitted=dict(
            matches=omitted,
            current=max(0, len(current_unmatched) - _MAX_GROUPS),
            baseline=max(0, len(baseline_unmatched) - _MAX_GROUPS),
        ),
        metrics=_metrics(checked, reasons),
        partial=incomplete,
        limitations=[
            "Function totals describe returned recorded intervals; missing calls, query limits and capture gaps remain unknown.",
            "Unknown symbols match only within the same source lifetime or an explicit one-to-one source mapping; names alone do not establish binary equivalence.",
            "Reference elapsed/resource metrics come from the declared context, not a sum of overlapping thread or GPU durations.",
            "A duration ratio is an observation, not proof of statistical significance, attainable speedup, or a bottleneck cause.",
        ],
    )


def defaultdict_list():
    # Ordinary dictionaries keep caller-supplied values out of default factories.
    return {}
