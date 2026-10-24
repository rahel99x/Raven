"""Counter observations and cautious investigation prompts over one query.

No values are filled into missing regions. Counter names/units are producer
declarations, and hints always include the actual observations they describe.
"""

from __future__ import annotations

from collections import defaultdict

from .workspace_analysis_common import (
    LOSS_KINDS,
    _data,
    integer,
    key,
    label,
    numeric,
    partial,
    scope_identity,
    validate_rows,
)


def _units(data, name):
    units = data.get("units", data.get("unit", "unknown"))
    if isinstance(units, dict):
        units = units.get(name, "unknown")
    return label(units, "unknown", 128)


def counter_lens(query, *, max_series=128, max_points=4096):
    """Return exact bounded decimal points, explicit phases, and source gaps."""
    integer(max_series, "counter series limit", minimum=1, maximum=512)
    integer(max_points, "counter point limit", minimum=1, maximum=20_000)
    rows, _ = validate_rows(query)
    series, phases, gaps = {}, [], []
    omitted = dict(series=0, points=0, phases=0, gaps=0, invalid_values=0)
    points = 0
    for row in rows.values():
        data, kind = _data(row), row.get("kind")
        if kind in LOSS_KINDS:
            if len(gaps) < 128:
                gaps.append(
                    dict(
                        id=row["id"],
                        source_id=row["source_id"],
                        clock_id=row["clock_id"],
                        time_ns=row["start_ns"],
                        reason=label(
                            data.get("reason", row.get("name")),
                            "Reported recording loss",
                        ),
                    )
                )
            else:
                omitted["gaps"] += 1
        if kind in {"phase", "activity_marker"} or data.get("phase") is not None:
            if len(phases) < 256:
                phases.append(
                    dict(
                        id=row["id"],
                        source_id=row["source_id"],
                        clock_id=row["clock_id"],
                        time_ns=row["start_ns"],
                        name=label(data.get("phase", row.get("name"))),
                    )
                )
            else:
                omitted["phases"] += 1
        if kind not in {"counter", "metric", "metrics"}:
            continue
        values = data.get("values", data.get("metrics"))
        if values is None and "value" in data:
            values = {data.get("name", row.get("name", "counter")): data["value"]}
        if not isinstance(values, dict) or len(values) > 128:
            omitted["invalid_values"] += 1
            continue
        for name, raw in values.items():
            value = numeric(raw)
            if not isinstance(name, str) or not 0 < len(name) <= 512 or value is None:
                omitted["invalid_values"] += 1
                continue
            unit = _units(data, name)
            # The same report can contain different producer processes/devices.
            # Retain that scope instead of merging their same-named counters.
            owner = (
                row["source_id"],
                row["clock_id"],
                data.get("source_node"),
                data.get("source_pid"),
                row.get("thread"),
                data.get("device"),
                data.get("producer_namespace"),
                data.get("producer_stream_id"),
                unit,
            )
            identifier = key("counter", owner, name)
            item = series.get(identifier)
            if item is None:
                if len(series) >= max_series:
                    omitted["series"] += 1
                    continue
                item = series[identifier] = dict(
                    id=identifier,
                    name=label(name),
                    source_id=row["source_id"],
                    clock_id=row["clock_id"],
                    unit=unit,
                    source_node=label(data.get("source_node")),
                    source_pid=data.get("source_pid"),
                    producer_namespace=label(data.get("producer_namespace")),
                    producer_stream_id=label(data.get("producer_stream_id")),
                    thread=row.get("thread"),
                    points=[],
                    minimum=None,
                    maximum=None,
                    provenance="Explicit recorded counter observations; producer-defined meaning and units",
                )
            if points >= max_points:
                omitted["points"] += 1
                continue
            point = dict(id=row["id"], time_ns=row["start_ns"], value=str(value))
            if "raw_start_ns" in row:
                point.update(
                    raw_time_ns=row["raw_start_ns"],
                    raw_clock_id=row.get("raw_clock_id"),
                    alignment_uncertainty_ns=row.get("alignment_uncertainty_ns"),
                )
            item["points"].append(point)
            item["minimum"] = (
                str(value)
                if item["minimum"] is None
                else str(min(numeric(item["minimum"]), value))
            )
            item["maximum"] = (
                str(value)
                if item["maximum"] is None
                else str(max(numeric(item["maximum"]), value))
            )
            points += 1
    result = list(series.values())
    for item in result:
        item["points"].sort(key=lambda point: (point["time_ns"], point["id"]))
    return dict(
        schema="raven.trace-counter-lens/v1",
        identity=scope_identity(query),
        series=result,
        phases=phases,
        gaps=gaps,
        omitted=omitted,
        partial=partial(query) or any(omitted.values()),
        scope="Counter and phase observations in the returned query; missing sources or regions remain unknown",
        limitations=[
            "Counters are displayed as points, never interpolated or converted to rates without an explicit adapter definition.",
            "A shared name is not proof of a shared unit, device, process or clock.",
            "A scientific phase is an annotation; it does not independently calibrate a clock.",
        ],
    )


def bottleneck_hints(query, *, limit=20):
    """Evidence-linked prompts, not an automatic optimization diagnosis."""
    integer(limit, "investigation hint limit", minimum=1, maximum=128)
    rows, _ = validate_rows(query)
    hints = []

    def add(identifier, title, reason, evidence, checks, assumptions=()):
        hints.append(
            dict(
                id=key("hint", identifier, evidence),
                title=title,
                reason=reason,
                evidence_ids=list(evidence)[:16],
                proposed_checks=list(checks),
                assumptions=list(assumptions),
                confidence="observed evidence; performance cause unproved",
            )
        )

    losses = [row for row in rows.values() if row.get("kind") in LOSS_KINDS]
    if losses:
        add(
            "loss",
            "Recording coverage is incomplete",
            str(len(losses))
            + " explicit loss or gap observations occur in this query.",
            [row["id"] for row in losses],
            [
                "Inspect source health and recording quotas before comparing timing totals.",
                "Capture a smaller explicitly selected region if the application event rate exceeds the budget.",
            ],
        )
    by_kind = defaultdict(list)
    for row in rows.values():
        by_kind[row.get("kind")].append(row)
    timed = [
        row
        for row in rows.values()
        if row.get("kind")
        in {"python", "host", "cooperative", "gpu_kernel", "gpu_copy"}
        and row.get("end_ns", row["start_ns"]) > row["start_ns"]
    ]
    if timed:
        longest = max(timed, key=lambda row: row["end_ns"] - row["start_ns"])
        add(
            "elapsed",
            "Inspect the longest returned recorded interval",
            label(longest.get("name"))
            + " has "
            + str(longest["end_ns"] - longest["start_ns"])
            + " ns of inclusive recorded elapsed time in this query.",
            [longest["id"]],
            [
                "Inspect its child calls, explicit dependencies and counters.",
                "Compare a repeated representative workload before attributing an optimization benefit.",
            ],
            [
                "Elapsed time includes waits and instrumentation; it is not consumed CPU time.",
                "This is the longest returned interval, not necessarily the whole run's bottleneck.",
            ],
        )
    if by_kind["sample"] or by_kind["stack_sample"]:
        samples = by_kind["sample"] + by_kind["stack_sample"]
        add(
            "samples",
            "Use sample share with its stated sampling method",
            str(len(samples))
            + " returned statistical observations do not identify exact function entry/exit boundaries.",
            [row["id"] for row in samples],
            [
                "Review sampling period, sample weights, unknown stacks and collector coverage."
            ],
            [
                "Absence from samples does not establish that a thread blocked or a core was idle."
            ],
        )
    host = [
        row
        for row in by_kind["host"]
        if any(
            name in label(row.get("name")).lower()
            for name in ("cuda", "rocm", "hip", "metal", "launch")
        )
    ]
    if host and not (by_kind["gpu_kernel"] or by_kind["gpu_copy"]):
        add(
            "host-only",
            "Device activity is unavailable in this query",
            "GPU-related host calls are present, but no recorded kernel or transfer activity is returned for this range.",
            [row["id"] for row in host],
            [
                "Check the device adapter's readiness, capture scope and source filters.",
                "Import a compatible activity report or enable a qualified device collector.",
            ],
            [
                "A host API call's duration is not device execution time.",
                "Unreturned activity may be filtered, out of range or unrecorded; this does not prove the GPU was idle.",
            ],
        )
    return dict(
        schema="raven.trace-investigation-hints/v1",
        identity=scope_identity(query),
        hints=hints[:limit],
        omitted=max(0, len(hints) - limit),
        partial=partial(query),
        scope="Prompts supported by returned evidence; no causal performance diagnosis or unavailable ground truth is claimed",
    )
