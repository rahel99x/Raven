"""Causal links from explicit flow records, never from nearby timestamps."""

from __future__ import annotations

from collections import defaultdict

from .workspace_model_clocks import integer, key, text
from .workspace_model_topology import _data, _items


def _point(row):
    data = _data(row)
    phase = data.get("flow_phase")
    if phase not in {"start", "step", "end"}:
        raise ValueError("Invalid explicit trace flow phase")
    result = dict(
        event_id=text(row.get("id"), "flow event ID", 512),
        source_id=text(row.get("source_id"), "flow source ID"),
        clock_id=text(row.get("clock_id"), "flow clock ID"),
        flow_id=text(data.get("flow_id"), "flow ID"),
        scope=text(data.get("correlation_scope"), "flow correlation scope", 1024),
        phase=phase,
        time_ns=integer(
            row.get("start_ns", data.get("source_time_ns")), "flow timestamp"
        ),
        thread=row.get("thread"),
        source_pid=data.get("source_pid"),
        source_tid=data.get("source_tid"),
        source_node=data.get("source_node"),
        alignment_uncertainty_ns=integer(
            row.get("alignment_uncertainty_ns", 0), "flow clock uncertainty"
        ),
        raw_clock_id=row.get("raw_clock_id"),
    )
    sequence = row.get("seq", row.get("first_seq"))
    result["seq"] = sequence if type(sequence) is int and sequence >= 0 else None
    return result


def _edge(first, last):
    same_clock = first["clock_id"] == last["clock_id"]
    delta = last["time_ns"] - first["time_ns"]
    uncertainty = first["alignment_uncertainty_ns"] + last["alignment_uncertainty_ns"]
    calibrated = first["raw_clock_id"] is not None or last["raw_clock_id"] is not None
    if same_clock and delta < -uncertainty:
        raise ValueError("Explicit flow reverses time in one clock domain")
    return dict(
        id=key(
            "flow",
            first["scope"],
            first["flow_id"],
            first["event_id"],
            last["event_id"],
        ),
        flow_id=first["flow_id"],
        correlation_scope=first["scope"],
        source_event_id=first["event_id"],
        target_event_id=last["event_id"],
        source_id=first["source_id"],
        target_source_id=last["source_id"],
        source_clock_id=first["clock_id"],
        target_clock_id=last["clock_id"],
        start_ns=first["time_ns"],
        end_ns=last["time_ns"],
        duration_ns=delta if same_clock and delta >= 0 else None,
        duration_uncertainty_ns=uncertainty if same_clock else None,
        duration_lower_bound_ns=max(0, delta - uncertainty) if same_clock else None,
        duration_upper_bound_ns=delta + uncertainty if same_clock else None,
        timing=(
            "calibrated clock bounds overlap; causal link only"
            if same_clock and delta < 0
            else "calibrated clock estimate; declared uncertainty retained"
            if same_clock and calibrated
            else "same recorded clock"
            if same_clock
            else "unaligned clocks; causal link only"
        ),
        provenance="Explicit flow start/step/end observations; no enclosing call inferred",
        source=first,
        target=last,
    )


def explicit_flows(query, *, limit=2000):
    """Pair bounded scoped flow chains and expose ambiguous/missing endpoints.

    Report-local/global ID semantics must already be preserved by the importer.
    Reused IDs can form multiple lifetimes only when their order is established.
    Bare activity correlation IDs are not treated as explicit causal edges.
    """
    if type(limit) is not int or not 1 <= limit <= 20_000:
        raise ValueError("Invalid explicit trace flow limit")
    intervals, markers = _items(query)
    groups, rejected, duplicates = defaultdict(list), 0, set()
    for row in (*markers, *intervals):
        if row.get("kind") != "flow":
            continue
        try:
            point = _point(row)
        except ValueError:
            rejected += 1
            continue
        if point["event_id"] in duplicates:
            rejected += 1
            continue
        duplicates.add(point["event_id"])
        groups[(point["scope"], point["flow_id"])].append(point)
    links, unresolved, omitted, unplaced = [], 0, 0, 0

    def append(first, last):
        nonlocal omitted, rejected
        try:
            edge = _edge(first, last)
        except ValueError:
            rejected += 1
            return
        if len(links) < limit:
            links.append(edge)
        else:
            omitted += 1

    for points in groups.values():
        domains = {point["clock_id"] for point in points}
        starts = [point for point in points if point["phase"] == "start"]
        ends = [point for point in points if point["phase"] == "end"]
        if len(domains) > 1:
            if len(starts) == len(ends) == 1:
                append(starts[0], ends[0])
                unplaced += sum(point["phase"] == "step" for point in points)
            else:
                unresolved += len(points)
            continue
        sources = {point["source_id"] for point in points}
        sequenced = (
            len(sources) == 1
            and all(point["seq"] is not None for point in points)
            and len({point["seq"] for point in points}) == len(points)
        )
        if sequenced:
            ordered = sorted(points, key=lambda point: point["seq"])
        else:
            # Equal timestamps on different sources do not identify the order
            # of reused lifetimes. A unique start/end pair is phase-defined.
            if len(starts) == len(ends) == 1 and len(points) == 2:
                append(starts[0], ends[0])
                continue
            if len({point["time_ns"] for point in points}) != len(points):
                unresolved += len(points)
                continue
            ordered = sorted(points, key=lambda point: point["time_ns"])
            uncertain = any(
                right["time_ns"] - left["time_ns"]
                <= left["alignment_uncertainty_ns"] + right["alignment_uncertainty_ns"]
                for left, right in zip(ordered, ordered[1:])
            )
            if uncertain:
                if len(starts) == len(ends) == 1:
                    append(starts[0], ends[0])
                    unplaced += sum(point["phase"] == "step" for point in points)
                else:
                    unresolved += len(points)
                continue
        active, chain, ambiguous = None, [], False
        for point in ordered:
            if point["phase"] == "start":
                if active is not None:
                    ambiguous = True
                    unresolved += len(chain)
                active, chain = point, [point]
            elif active is None:
                unresolved += 1
            elif point["phase"] == "step":
                chain.append(point)
            else:
                chain.append(point)
                if ambiguous:
                    unresolved += len(chain)
                else:
                    for left, right in zip(chain, chain[1:]):
                        append(left, right)
                active, chain, ambiguous = None, [], False
        unresolved += len(chain)
    warnings = []
    if rejected:
        warnings.append(
            f"Rejected {rejected} malformed or duplicate explicit flow records."
        )
    if unresolved:
        warnings.append(
            f"{unresolved} flow endpoints have missing or ambiguous lifetimes; no links inferred for them."
        )
    if unplaced:
        warnings.append(
            f"{unplaced} intermediate flow points have unaligned or uncertain clock ordering; only unique start/end causality is shown."
        )
    if omitted:
        warnings.append(
            f"The query flow budget omitted {omitted} links; narrow the selected range."
        )
    return dict(
        schema="raven.trace-flows/v1",
        links=links,
        rejected=rejected,
        unresolved=unresolved,
        omitted=omitted,
        warnings=warnings,
        scope="Explicit flow observations in the returned query; not a complete critical path",
    )
