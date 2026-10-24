"""Bounded selected-event dependency slices from explicit recorded evidence."""

from __future__ import annotations

from collections import defaultdict, deque
import heapq

from .workspace_analysis_common import (
    MAX_GRAPH_EDGES,
    MAX_GRAPH_NODES,
    _data,
    aligned_points,
    issue,
    key,
    label,
    partial,
    scope_identity,
    text,
    validate_rows,
)

_PRECEDES = frozenset({"precedes", "happens_before", "depends_on"})
_DEVICE = frozenset({"gpu_kernel", "gpu_copy", "gpu_sync", "gpu_synchronization"})


def _references(rows, sources):
    aliases = defaultdict(set)
    for identifier, row in rows.items():
        aliases[identifier].add(identifier)
        data = _data(row)
        identity = sources.get(row["source_id"], {}).get("identity", {})
        if row.get("kind") == "cooperative" and isinstance(data.get("span_id"), str):
            stream = identity.get("stream")
            if isinstance(stream, str):
                aliases[stream + ":" + data["span_id"]].add(identifier)
        event_id = data.get("event_id")
        if isinstance(event_id, str):
            # A report-local event ID is only addressable through its source.
            aliases[row["source_id"] + ":" + event_id].add(identifier)
    return aliases


def _resolve(value, aliases):
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 1024
        or not value.isprintable()
    ):
        return None
    values = aliases.get(value, ())
    return next(iter(values)) if len(values) == 1 else None


def _delay(first, last, relation, sources):
    left = (
        first["start_ns"]
        if relation in {"flow", "launches"}
        else first.get("end_ns", first["start_ns"])
    )
    right = last["start_ns"]
    aligned, reason = aligned_points([(first, left), (last, right)], sources)
    if aligned is None:
        return dict(
            availability="unavailable",
            reason=reason,
            bounds_ns=None,
            clock_id=None,
            ordering_proven=False,
        )
    domain, (a, b) = aligned
    delta = b["time_ns"] - a["time_ns"]
    error = a["uncertainty_ns"] + b["uncertainty_ns"]
    if delta + error < 0:
        return dict(
            availability="inconsistent",
            reason="Declared dependency reverses recorded time beyond its stated uncertainty",
            bounds_ns=None,
            clock_id=domain,
            ordering_proven=False,
        )
    return dict(
        availability="available",
        reason=None,
        bounds_ns={"lower": max(0, delta - error), "upper": delta + error},
        clock_id=domain,
        ordering_proven=delta - error >= 0,
        provenance=[a["provenance"], b["provenance"]],
        meaning="Recorded source-entry to target-entry delay; not inferred GPU queue time"
        if relation == "launches"
        else "Recorded dependency endpoint delay",
    )


def _typed_link(value, rows, aliases):
    if (
        not isinstance(value, dict)
        or value.get("schema") != "raven.trace-activity-correlation/v1"
    ):
        raise ValueError("Unsupported activity-correlation declaration")
    if value.get("relation", "launches") != "launches":
        raise ValueError("Unsupported activity-correlation relation")
    first = _resolve(value.get("source_id"), aliases)
    last = _resolve(value.get("target_id"), aliases)
    if first is None or last is None:
        raise ValueError("Activity-correlation endpoint is missing or ambiguous")
    a, b = rows[first], rows[last]
    if a.get("kind") != "host" or b.get("kind") not in _DEVICE:
        raise ValueError(
            "Host/device correlation requires a host activity and device activity"
        )
    scope = text(value.get("correlation_scope"), "activity correlation scope", 1024)
    correlation = text(value.get("correlation_id"), "activity correlation ID", 256)
    provenance = text(value.get("provenance"), "activity correlation provenance", 1024)
    for row in (a, b):
        data = _data(row)
        declared_scope = data.get("correlation_scope")
        if declared_scope is None and isinstance(data.get("provenance"), dict):
            declared_scope = data["provenance"].get("input_sha256")
        if data.get("correlation_id") != correlation or declared_scope != scope:
            raise ValueError(
                "Activity-correlation declaration disagrees with its endpoint metadata"
            )
    return first, last, "launches", provenance


def _edges(query, rows, sources):
    from .workspace_model_flows import explicit_flows

    aliases = _references(rows, sources)
    edges, unknown, seen = [], [], set()
    omitted = 0

    def add(first, last, relation, provenance, evidence):
        nonlocal omitted
        if first is None or last is None:
            issue(
                unknown,
                "Dependency has an unobserved or ambiguous endpoint",
                evidence_ids=evidence,
                kind="missing-endpoint",
            )
            return
        signature = first, last, relation
        if signature in seen:
            return
        seen.add(signature)
        if len(edges) >= MAX_GRAPH_EDGES:
            omitted += 1
            return
        timing = _delay(rows[first], rows[last], relation, sources)
        edges.append(
            dict(
                id=key("dependency", *signature, provenance),
                source_id=first,
                target_id=last,
                relation=relation,
                provenance=provenance,
                evidence_ids=list(evidence)[:16],
                timing=timing,
                consistency="inconsistent"
                if timing["availability"] == "inconsistent"
                else "observed-declaration",
            )
        )

    flows = explicit_flows(query, limit=MAX_GRAPH_EDGES)
    for edge in flows["links"]:
        add(
            edge["source_event_id"],
            edge["target_event_id"],
            "flow",
            edge["provenance"],
            [edge["source_event_id"], edge["target_event_id"]],
        )
    if flows["unresolved"] or flows["rejected"] or flows["omitted"]:
        issue(
            unknown,
            "Some explicit flow endpoints are missing, invalid or outside the bounded query",
            kind="partial-flow",
            unresolved=flows["unresolved"],
            rejected=flows["rejected"],
            omitted=flows["omitted"],
        )
    for identifier, row in rows.items():
        data = _data(row)
        if row.get("kind") == "link":
            relation = data.get("relation", "precedes")
            if relation not in _PRECEDES:
                issue(
                    unknown,
                    "This declared relation has no supported dependency semantics",
                    evidence_ids=[identifier],
                    kind="unsupported-relation",
                )
            else:
                add(
                    _resolve(data.get("source"), aliases),
                    _resolve(data.get("target"), aliases),
                    relation,
                    "Explicit recorded qualified dependency link",
                    [identifier],
                )
        depends = data.get("depends_on")
        if depends is not None:
            if not isinstance(depends, list) or len(depends) > 64:
                issue(
                    unknown,
                    "depends_on must contain at most 64 explicit event references",
                    evidence_ids=[identifier],
                    kind="malformed-dependency",
                )
            else:
                for reference in depends:
                    add(
                        _resolve(reference, aliases),
                        identifier,
                        "depends_on",
                        "Explicit endpoint dependency declaration",
                        [identifier],
                    )
    correlations = query.get("activity_links", [])
    qualified_activity_ids = set()
    if not isinstance(correlations, list) or len(correlations) > MAX_GRAPH_EDGES:
        raise ValueError("Activity-correlation declarations exceed their bounded list")
    for declaration in correlations:
        try:
            first, last, relation, provenance = _typed_link(declaration, rows, aliases)
        except ValueError as exc:
            issue(unknown, str(exc), kind="invalid-activity-correlation")
        else:
            qualified_activity_ids.update((first, last))
            add(first, last, relation, provenance, [first, last])
    from .workspace_correlations import correlations as recorded_correlations

    bridge = recorded_correlations(
        query, limit=MAX_GRAPH_EDGES, qualified_activity_ids=qualified_activity_ids
    )
    for declaration in bridge["links"]:
        first, last = declaration["source_id"], declaration["target_id"]
        if first not in rows or last not in rows:
            issue(
                unknown,
                "Recorded protocol dependency endpoint is outside this query",
                kind="missing-protocol-endpoint",
            )
            continue
        add(
            first,
            last,
            declaration["relation"],
            declaration["provenance"],
            declaration["evidence_ids"],
        )
    for item in bridge["unknown"]:
        issue(
            unknown,
            item["reason"],
            kind=item.get("kind", "unknown-protocol-evidence"),
            evidence_ids=item.get("evidence_ids", ()),
        )
    if bridge["omitted"]:
        issue(
            unknown,
            "Recorded protocol dependency budget reached; narrow the query",
            kind="protocol-link-budget",
            omitted=bridge["omitted"],
        )
    if omitted:
        issue(
            unknown,
            "Dependency edge budget reached; narrow the inspected range",
            kind="edge-budget",
            omitted=omitted,
        )
    return edges, unknown


def _component(selected, adjacency, reverse, maximum):
    chosen, pending = {selected}, deque([(selected, 0), (selected, 1)])
    visited = [{selected}, {selected}]
    directions = (reverse, adjacency)
    clipped = False
    while pending:
        node, direction = pending.popleft()
        for value in sorted(set(directions[direction][node])):
            if value in visited[direction]:
                continue
            if value not in chosen and len(chosen) >= maximum:
                clipped = True
                continue
            chosen.add(value)
            visited[direction].add(value)
            pending.append((value, direction))
    return chosen, clipped


def _reachable(start, adjacency, allowed):
    found, pending = set(), [start]
    while pending:
        for value in adjacency[pending.pop()]:
            if value in allowed and value not in found:
                found.add(value)
                pending.append(value)
    found.discard(start)
    return found


def _cycles(nodes, adjacency, reverse):
    order, visited = [], set()
    for start in sorted(nodes):
        if start in visited:
            continue
        pending = [(start, False)]
        while pending:
            node, done = pending.pop()
            if done:
                order.append(node)
            elif node not in visited:
                visited.add(node)
                pending.append((node, True))
                pending.extend(
                    (child, False)
                    for child in adjacency[node]
                    if child in nodes and child not in visited
                )
    visited.clear()
    result = []
    for start in reversed(order):
        if start in visited:
            continue
        component, pending = [], [start]
        visited.add(start)
        while pending:
            node = pending.pop()
            component.append(node)
            for child in reverse[node]:
                if child in nodes and child not in visited:
                    visited.add(child)
                    pending.append(child)
        if len(component) > 1 or start in adjacency[start]:
            result.append(sorted(component))
    return result


def _chain(selected, nodes, edges, sources, rows, cycles):
    unavailable = dict(
        node_ids=[],
        edge_ids=[],
        selection_criterion="Most explicit dependency links through the selected event",
        elapsed_bounds_ns=None,
        clock_id=None,
        reason=None,
    )
    if cycles:
        return dict(
            unavailable,
            reason="Cyclic dependency declarations do not provide a defensible longest path",
        )
    if any(edge["consistency"] == "inconsistent" for edge in edges):
        return dict(
            unavailable, reason="Dependency declarations contradict recorded time"
        )
    adjacency, reverse, indegree = (
        defaultdict(list),
        defaultdict(list),
        dict.fromkeys(nodes, 0),
    )
    edge_lookup = {}
    for edge in edges:
        first, last = edge["source_id"], edge["target_id"]
        if (first, last) not in edge_lookup:
            adjacency[first].append(last)
            reverse[last].append(first)
            indegree[last] += 1
            edge_lookup[first, last] = edge["id"]
    ready, order = [node for node in nodes if indegree[node] == 0], []
    heapq.heapify(ready)
    while ready:
        node = heapq.heappop(ready)
        order.append(node)
        for child in adjacency[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, child)
    before, after, predecessor, successor = (
        dict.fromkeys(nodes, 0),
        dict.fromkeys(nodes, 0),
        {},
        {},
    )
    for node in order:
        for child in sorted(adjacency[node]):
            if before[node] + 1 > before[child]:
                before[child], predecessor[child] = before[node] + 1, node
    for node in reversed(order):
        for child in sorted(adjacency[node]):
            if after[child] + 1 > after[node]:
                after[node], successor[node] = after[child] + 1, child
    left, cursor = [], selected
    while cursor in predecessor:
        cursor = predecessor[cursor]
        left.append(cursor)
    chain = list(reversed(left)) + [selected]
    cursor = selected
    while cursor in successor:
        cursor = successor[cursor]
        chain.append(cursor)
    result = dict(
        unavailable,
        node_ids=chain,
        edge_ids=[edge_lookup[a, b] for a, b in zip(chain, chain[1:])],
    )
    aligned, reason = aligned_points(
        [
            (rows[node], stamp)
            for node in chain
            for stamp in (
                rows[node]["start_ns"],
                rows[node].get("end_ns", rows[node]["start_ns"]),
            )
        ],
        sources,
    )
    if aligned is None:
        return dict(result, reason=reason)
    clock, values = aligned
    starts, ends = values[::2], values[1::2]
    lower = max(
        0,
        max(point["time_ns"] - point["uncertainty_ns"] for point in ends)
        - min(point["time_ns"] + point["uncertainty_ns"] for point in starts),
    )
    upper = max(point["time_ns"] + point["uncertainty_ns"] for point in ends) - min(
        point["time_ns"] - point["uncertainty_ns"] for point in starts
    )
    return dict(
        result,
        elapsed_bounds_ns={"lower": lower, "upper": upper},
        clock_id=clock,
        reason=None,
        timing_scope="Elapsed span covering observed chain boundaries; not a sum of overlapping work or the true global critical path",
    )


def causal_slice(query, selected_id, *, max_nodes=4096):
    if type(max_nodes) is not int or not 1 <= max_nodes <= MAX_GRAPH_NODES:
        raise ValueError("Causal slice node limit must be 1–8192")
    selected_id = text(selected_id, "selected analysis event ID", 1024)
    rows, sources = validate_rows(query)
    result = dict(
        schema="raven.trace-causal-slice/v1",
        selected_id=selected_id,
        identity=scope_identity(query),
        nodes=[],
        edges=[],
        ancestors=[],
        descendants=[],
        cycles=[],
        unknown=[],
        observed_chain=None,
        partial=partial(query),
        status="unavailable",
        scope="Explicit dependencies in the returned query; never the unobserved global critical path",
        limitations=[
            "No dependency is inferred from timestamp proximity, thread adjacency or a bare activity correlation ID.",
            "Producer declarations and calibration bounds are not independently qualified causality or timing.",
            "The selected chain maximizes observed link count, not machine performance or unobserved execution time.",
        ],
    )
    if selected_id not in rows:
        result["reason"] = (
            "The selected event is outside the returned query; focus or widen the range before investigating it"
        )
        return result
    edges, unknown = _edges(query, rows, sources)
    adjacency, reverse = defaultdict(list), defaultdict(list)
    for edge in edges:
        adjacency[edge["source_id"]].append(edge["target_id"])
        reverse[edge["target_id"]].append(edge["source_id"])
    envelope, clipped = _component(selected_id, adjacency, reverse, max_nodes)
    ancestors = _reachable(selected_id, reverse, envelope)
    descendants = _reachable(selected_id, adjacency, envelope)
    nodes = {selected_id} | ancestors | descendants
    selected_edges = [
        edge
        for edge in edges
        if edge["source_id"] in nodes and edge["target_id"] in nodes
    ]
    cycles = _cycles(nodes, adjacency, reverse)
    if clipped:
        issue(
            unknown,
            "Selected dependency component exceeds the node budget; narrow the range",
            kind="node-budget",
        )
    result.update(
        nodes=[
            dict(
                id=node,
                kind=rows[node].get("kind"),
                name=label(rows[node].get("name")),
                source_id=rows[node]["source_id"],
                clock_id=rows[node]["clock_id"],
                start_ns=rows[node]["start_ns"],
                end_ns=rows[node].get("end_ns", rows[node]["start_ns"]),
            )
            for node in sorted(nodes)
        ],
        edges=selected_edges,
        ancestors=sorted(ancestors),
        descendants=sorted(descendants),
        cycles=cycles,
        unknown=unknown,
        partial=result["partial"] or clipped or bool(unknown),
        status="inconsistent"
        if cycles
        or any(edge["consistency"] == "inconsistent" for edge in selected_edges)
        else "partial"
        if result["partial"] or clipped or unknown
        else "observed",
    )
    result["observed_chain"] = _chain(
        selected_id, nodes, selected_edges, sources, rows, cycles
    )
    if not selected_edges:
        issue(
            result["unknown"],
            "No explicit dependency involving this event was observed in the returned query",
            evidence_ids=[selected_id],
            kind="missing-dependency",
        )
        result.update(status="partial", partial=True)
    return result
