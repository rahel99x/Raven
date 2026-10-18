"""Bounded analysis of *declared* cooperative trace evidence.

These pure functions perform no collection, scheduler calls or clock alignment.
``COMPLETE`` means that the supplied causal evidence was internally consistent,
not that arbitrary application execution was instrumented. Explicit dependency
links use qualified ``<stream>:<span_id>`` endpoints and point from prerequisite
to dependent. Only finish-before-start links contribute to path duration.
"""
from __future__ import annotations

from collections import defaultdict, deque
from itertools import islice

MAX_EVENTS = 20_000
MAX_SPANS = 4_096
MAX_LINKS = 8_192
MAX_COLLECTIVES = 4_096
MAX_MEMBERS = 4_096
MAX_WARNINGS = 64
MAX_TEXT = 256
MAX_INTEGER = (1 << 63) - 1
_PRECEDES = frozenset(("precedes", "happens_before", "depends_on"))
_FIELDS = ("span_id", "name", "parent_id", "source", "target", "relation",
           "communicator", "generation", "sequence", "operation", "members",
           "rank", "dtype", "count", "root", "phase", "reduction")


def _text(value):
    """Render only bounded scalars; never execute external terminal controls."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    if isinstance(value, int) and value.bit_length() > 64:
        return ""
    return "".join(ch if ch.isprintable() else " " for ch in str(value)[:MAX_TEXT])


def _id(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    if isinstance(value, int) and not 0 <= value <= MAX_INTEGER:
        return None
    value = str(value)
    if not value or len(value) > MAX_TEXT or not all(ch.isprintable() for ch in value):
        return None
    return value


def _endpoint(value):
    if not isinstance(value, str) or not 0 < len(value) <= 2 * MAX_TEXT + 1:
        return None
    return value if ":" in value and all(value.split(":", 1)) and all(ch.isprintable() for ch in value) else None


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_INTEGER else None


def _warn(warnings, message):
    if message not in warnings and len(warnings) < MAX_WARNINGS:
        warnings.append(message)


def _fingerprint(value):
    # Transport records are flat. Do not recursively inspect untrusted data or
    # compare arbitrarily deep containers when detecting replayed records.
    if isinstance(value, (str, int, float, bool, type(None))):
        if isinstance(value, int) and value.bit_length() > 64:
            return ("oversized integer",)
        return (type(value).__name__, str(value)[:2 * MAX_TEXT + 2])
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, len(value), tuple(
            (type(item).__name__, _text(item)) for item in islice(value, MAX_MEMBERS + 1)))
    return (type(value).__name__,)


def _prepare(events):
    warnings, records, seen, identities = [], [], {}, {}
    invalid = False
    try:
        iterator = iter(events)
    except TypeError:
        return [], ["Trace input is not an event iterable."], True
    for index, event in enumerate(islice(iterator, MAX_EVENTS + 1)):
        if index == MAX_EVENTS:
            _warn(warnings, f"Analysis limited to {MAX_EVENTS} events; evidence is incomplete.")
            break
        if not isinstance(event, dict):
            invalid = True
            _warn(warnings, "Malformed event envelope was excluded.")
            continue
        stream, seq, timestamp = _id(event.get("stream")), _integer(event.get("seq")), _integer(event.get("mono_ns"))
        data, kind = event.get("data"), _id(event.get("kind"))
        if stream is None or ":" in stream or seq is None or timestamp is None or not isinstance(data, dict) or kind is None:
            invalid = True
            _warn(warnings, "Event requires stream, nonnegative sequence/time, kind and data object.")
            continue
        identity = tuple(_id(event.get(key)) for key in ("job_id", "attempt", "node", "pid", "cluster", "boot_id", "process_start"))
        if stream in identities and identities[stream] != identity:
            invalid = True
            _warn(warnings, f"Stream {stream} has conflicting process/job identities.")
        identities[stream] = identity
        signature = (timestamp, kind, identity, _fingerprint(event.get("thread")))
        key = (stream, seq)
        if key in seen:
            prior_signature, prior_data = seen[key]
            if (prior_signature != signature or any(_fingerprint(prior_data.get(field)) != _fingerprint(data.get(field))
                                                    for field in _FIELDS)):
                invalid = True
                _warn(warnings, f"Conflicting replay at {stream} sequence {seq}.")
            continue
        # Retain bounded event references, not a second copy of every rank list.
        # Fingerprinting members eagerly makes large-rank traces quadratic in
        # memory even when the normal stream contains no replayed events.
        seen[key] = (signature, data)
        records.append(dict(stream=stream, seq=seq, mono_ns=timestamp, kind=kind,
                            data=data, identity=identity, thread=_text(event.get("thread"))))
    # Arrival order across nodes/transport retries has no timing significance.
    records.sort(key=lambda event: (event["stream"], event["seq"]))
    previous = {}
    for event in records:
        stream = event["stream"]
        prior = previous.get(stream)
        if prior is not None:
            if event["seq"] != prior[0] + 1:
                _warn(warnings, f"Sequence gap in stream {stream}; evidence is incomplete.")
            if event["mono_ns"] < prior[1]:
                invalid = True
                _warn(warnings, f"Monotonic time moved backward in stream {stream}.")
        previous[stream] = (event["seq"], event["mono_ns"])
        if event["kind"] in ("gap", "lost", "drop", "loss", "dropped"):
            _warn(warnings, f"Stream {stream} reports lost trace evidence.")
    return records, warnings, invalid


def _result(status, title, rows, warnings, data):
    return dict(status=status, title=title, rows=rows, warnings=warnings, data=data)


def analyze_causality(events):
    """Return measured spans and a longest explicitly ordered leaf-span chain.

    Path duration is the sum of measured local elapsed durations. It is neither
    global wall-clock makespan nor a prediction of attainable speedup. Nested
    parents remain visible, but are excluded from path weights so their children
    are not counted twice. No implicit edges are inferred from timestamps.
    """
    records, warnings, invalid = _prepare(events)
    spans, links, ends = {}, [], {}
    for event in records:
        kind, data, stream = event["kind"], event["data"], event["stream"]
        if kind in ("span_start", "span_end"):
            local_id = _id(data.get("span_id"))
            if local_id is None:
                invalid = True
                _warn(warnings, "Span record has no valid span_id.")
                continue
            key = f"{stream}:{local_id}"
            if kind == "span_end":
                if key in ends:
                    invalid = True
                    _warn(warnings, f"Span {key} has multiple end records.")
                elif len(ends) < MAX_SPANS:
                    ends[key] = event
                else:
                    _warn(warnings, f"Span limit {MAX_SPANS} reached; evidence is incomplete.")
                continue
            if key in spans:
                invalid = True
                _warn(warnings, f"Span identifier {key} was reused.")
                continue
            if len(spans) >= MAX_SPANS:
                _warn(warnings, f"Span limit {MAX_SPANS} reached; evidence is incomplete.")
                continue
            parent_id = _id(data.get("parent_id"))
            if data.get("parent_id") is not None and parent_id is None:
                invalid = True
                _warn(warnings, f"Span {key} has an invalid parent_id.")
            spans[key] = dict(id=key, stream=stream, name=_text(data.get("name")) or local_id,
                              parent=f"{stream}:{parent_id}" if parent_id is not None else None,
                              start_ns=event["mono_ns"], end_ns=None, duration_ns=None,
                              thread=event["thread"], node=event["identity"][2],
                              identity=event["identity"], start_seq=event["seq"], nested=False)
        elif kind == "link":
            if len(links) >= MAX_LINKS:
                _warn(warnings, f"Link limit {MAX_LINKS} reached; evidence is incomplete.")
                continue
            source, target = _endpoint(data.get("source")), _endpoint(data.get("target"))
            if source is None or target is None:
                invalid = True
                _warn(warnings, "Dependency endpoints must be qualified stream:span_id identifiers.")
                continue
            links.append(dict(source=source, target=target, relation=_text(data.get("relation")) or "precedes"))
    for key, event in ends.items():
        span = spans.get(key)
        if span is None:
            _warn(warnings, f"End without observed start for {key}.")
            continue
        if event["seq"] <= span["start_seq"] or event["mono_ns"] < span["start_ns"]:
            invalid = True
            _warn(warnings, f"Invalid span duration/order for {key}.")
            continue
        span["end_ns"] = event["mono_ns"]
        span["duration_ns"] = event["mono_ns"] - span["start_ns"]
    parent_graph = {}
    for key, span in spans.items():
        if span["end_ns"] is None:
            _warn(warnings, f"Span {key} is open or its end is missing.")
        parent = spans.get(span["parent"])
        if span["parent"] and parent is None:
            _warn(warnings, f"Parent of {key} was not observed.")
        elif parent is not None:
            parent["nested"] = True
            parent_graph[key] = parent["id"]
            if (parent["start_ns"] > span["start_ns"] or
                    parent["end_ns"] is not None and span["end_ns"] is not None and parent["end_ns"] < span["end_ns"]):
                invalid = True
                _warn(warnings, f"Span {key} falls outside its declared parent.")
    # Iterative parent-cycle detection; malicious nesting cannot exhaust Python's
    # recursion stack and each vertex is visited at most twice.
    checked = set()
    for key in parent_graph:
        active, cursor = set(), key
        while cursor in parent_graph and cursor not in checked:
            if cursor in active:
                invalid = True
                _warn(warnings, "Declared span parent relationships contain a cycle.")
                break
            active.add(cursor)
            cursor = parent_graph[cursor]
        checked.update(active)
    leaves = {key: span for key, span in spans.items() if not span["nested"] and span["duration_ns"] is not None}
    edges, adjacency, indegree = [], defaultdict(list), dict.fromkeys(leaves, 0)
    all_adjacency, all_indegree = defaultdict(list), dict.fromkeys(spans, 0)
    seen_edges = set()
    for edge in links:
        source, target, relation = edge["source"], edge["target"], edge["relation"]
        signature = (source, target, relation)
        if signature in seen_edges:
            continue
        seen_edges.add(signature)
        edges.append(edge)
        if source not in spans or target not in spans:
            _warn(warnings, f"Dependency {source} -> {target} has an unobserved endpoint.")
            continue
        if (spans[source]["identity"][:2], spans[source]["identity"][4]) != (spans[target]["identity"][:2], spans[target]["identity"][4]):
            invalid = True
            _warn(warnings, "Dependency crosses job identities or attempts.")
            continue
        if relation not in _PRECEDES:
            _warn(warnings, f"Relation {relation} has no certified finish-before-start semantics.")
            continue
        all_adjacency[source].append(target)
        all_indegree[target] += 1
        if source not in leaves or target not in leaves:
            _warn(warnings, "Open or nested span dependency excluded from duration path.")
            continue
        a, b = leaves[source], leaves[target]
        if a["stream"] == b["stream"] and a["end_ns"] > b["start_ns"]:
            invalid = True
            _warn(warnings, f"Finish-before-start dependency overlaps on stream {a['stream']}.")
            continue
        adjacency[source].append(target)
        indegree[target] += 1
    pending_all = deque(key for key, count in all_indegree.items() if count == 0)
    visited_all = 0
    while pending_all:
        source = pending_all.popleft()
        visited_all += 1
        for target in all_adjacency[source]:
            all_indegree[target] -= 1
            if all_indegree[target] == 0:
                pending_all.append(target)
    if visited_all != len(spans):
        invalid = True
        _warn(warnings, "Explicit dependency graph contains a cycle.")
    pending = deque(key for key, count in indegree.items() if count == 0)
    distance = {key: span["duration_ns"] for key, span in leaves.items()}
    local_distance = dict(distance)
    local_predecessor = {}
    predecessor, visited = {}, 0
    while pending:
        source = pending.popleft()
        visited += 1
        for target in adjacency[source]:
            candidate = distance[source] + leaves[target]["duration_ns"]
            if candidate > distance[target]:
                distance[target], predecessor[target] = candidate, source
            # A process path never borrows another process's elapsed time.
            # Cross-process links remain available only in the explicit global
            # dependency result above; coincident timestamps imply no edges.
            if leaves[source]["stream"] == leaves[target]["stream"]:
                local_candidate = local_distance[source] + leaves[target]["duration_ns"]
                if local_candidate > local_distance[target]:
                    local_distance[target], local_predecessor[target] = local_candidate, source
            indegree[target] -= 1
            if indegree[target] == 0:
                pending.append(target)
    if visited != len(leaves):
        invalid = True
        _warn(warnings, "Explicit dependency graph contains a cycle.")
    path, total = [], None
    if distance and not invalid:
        cursor = max(distance, key=distance.get)
        total = distance[cursor]
        while cursor is not None:
            path.append(cursor)
            cursor = predecessor.get(cursor)
        path.reverse()
    status = "INVALID" if invalid else "PARTIAL" if warnings else "COMPLETE" if spans else "EMPTY"
    process_paths = {}
    for key, span in leaves.items():
        stream = span["stream"]
        current = process_paths.get(stream)
        if current is None or local_distance[key] > local_distance[current]:
            process_paths[stream] = key
    processes = []
    for stream, endpoint in process_paths.items():
        local_path, cursor = [], endpoint if not invalid else None
        while cursor is not None:
            local_path.append(cursor)
            cursor = local_predecessor.get(cursor)
        local_path.reverse()
        identity = leaves[endpoint]["identity"]
        processes.append(dict(stream=stream, node=identity[2], pid=identity[3], path=local_path,
                              duration_ns=local_distance[endpoint] if not invalid else None,
                              status=status, clock_model="process-local monotonic; explicit links only"))
    rows = ["Observed finish-before-start chain; local elapsed durations, not global wall time."]
    if path:
        rows.append(f"Measured chain: {total / 1_000_000:.3f} ms across {len(path)} spans")
        rows.extend(f"{leaves[key]['name']} [{key}]  {leaves[key]['duration_ns'] / 1_000_000:.3f} ms" for key in path)
    else:
        rows.append("No defensible duration path is available.")
    rows.append("Only declared instrumentation and dependencies are covered; absence of gaps is not proof of full coverage.")
    if len(processes) > 1:
        rows.append(f"{len(processes)} independent process paths; cross-process alignment requires explicit links.")
    return _result(status, "Causal dependency evidence", rows, warnings,
                   dict(spans=list(spans.values()), edges=edges, path=path, duration_ns=total, processes=processes,
                        clock_model="per-stream monotonic; no inferred cross-node clock alignment"))


def _operation(value):
    name = _text(value).lower().replace("_", "").replace("-", "").replace(" ", "")
    for prefix in ("mpi", "nccl"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return {"allreduce": "ALLREDUCE", "broadcast": "BCAST", "bcast": "BCAST", "barrier": "BARRIER"}.get(name, name.upper())


def _dtype(value):
    name = _text(value).lower().replace("_", "").replace(" ", "")
    # Only fixed-width scalar signatures can be compared from this envelope.
    # MPI derived types require their full type signature, not a handle/name.
    aliases = {"mpifloat": "float32", "mpidouble": "float64", "float": "float32", "double": "float64",
               "ncclfloat": "float32", "ncclfloat32": "float32", "ncclfloat64": "float64",
               "mpibyte": "byte", "uint8": "byte", "mpiint32t": "int32", "mpiint64t": "int64",
               "mpiuint32t": "uint32", "mpiuint64t": "uint64"}
    name = aliases.get(name, name)
    return name if name in {"float16", "bfloat16", "float32", "float64", "int8", "int16", "int32", "int64",
                           "byte", "uint16", "uint32", "uint64", "complex64", "complex128"} else None


def audit_collectives(events):
    """Audit declared intracommunicator AllReduce, Bcast and Barrier evidence.

    Communicator/generation identities and the complete ordered member rank set
    must be supplied consistently by the adapter. Missing entry/completion is
    incomplete evidence, never proof of deadlock. Unknown operations, derived
    datatypes and variable-count collectives are explicitly unsupported.
    """
    records, warnings, invalid = _prepare(events)
    groups = {}
    for event in records:
        if event["kind"] != "collective":
            continue
        data = event["data"]
        communicator, generation = _id(data.get("communicator")), _id(data.get("generation"))
        sequence, rank = _integer(data.get("sequence")), _integer(data.get("rank"))
        members = data.get("members")
        phase, operation = data.get("phase"), _operation(data.get("operation"))
        if (communicator is None or generation is None or sequence is None or rank is None or
                not isinstance(members, (list, tuple)) or not 0 < len(members) <= MAX_MEMBERS or
                any(_integer(member) is None for member in members) or len(set(members)) != len(members) or
                rank not in members or phase not in ("enter", "complete") or not operation):
            invalid = True
            _warn(warnings, "Malformed collective record excluded; communicator, generation, members and phase are required.")
            continue
        scope = event["identity"][:2]
        cluster = event["identity"][4]
        key = (*scope, cluster, communicator, generation, sequence)
        if key not in groups:
            if len(groups) >= MAX_COLLECTIVES:
                _warn(warnings, f"Collective limit {MAX_COLLECTIVES} reached; evidence is incomplete.")
                continue
            groups[key] = dict(job_id=scope[0], attempt=scope[1], cluster=cluster, communicator=communicator,
                               generation=generation, sequence=sequence, members=tuple(members),
                               ranks={}, problems=[], uncertain=[], ambiguous=False)
        group = groups[key]
        if tuple(members) != group["members"]:
            _warn(group["uncertain"], "Communicator member ordering/identity differs between records.")
            group["ambiguous"] = True
        owner = (event["stream"], *event["identity"][2:])
        reduction = _id(data.get("reduction"))
        reduction = reduction.lower().removeprefix("mpi_") if reduction is not None else None
        signature = (operation, _dtype(data.get("dtype")), _integer(data.get("count")), _integer(data.get("root")), reduction)
        if operation == "BARRIER":
            signature = (operation, None, None, None, None)
        entry = group["ranks"].get(rank)
        if entry is None:
            entry = group["ranks"][rank] = dict(owner=owner, signature=signature, phases=set())
        else:
            if entry["owner"] != owner:
                _warn(group["uncertain"], f"Rank {rank} is claimed by multiple process streams.")
                group["ambiguous"] = True
            if entry["signature"] != signature:
                _warn(group["problems"], f"Rank {rank} reports conflicting operation parameters for this sequence.")
        entry["phases"].add(phase)
    findings = []
    for group in groups.values():
        ranks, members = group["ranks"], group["members"]
        missing = [rank for rank in members if rank not in ranks or "enter" not in ranks[rank]["phases"]]
        unfinished = [rank for rank in members if rank not in ranks or "complete" not in ranks[rank]["phases"]]
        signatures = [entry["signature"] for entry in ranks.values()]
        operations = {item[0] for item in signatures}
        if len(operations) > 1:
            _warn(group["problems"], "Collective operations disagree between ranks.")
        operation = next(iter(operations)) if len(operations) == 1 else "/".join(sorted(operations))
        if len(operations) == 1 and operation not in ("ALLREDUCE", "BCAST", "BARRIER"):
            _warn(group["uncertain"], "Operation semantics are not certified by this auditor.")
        elif len(operations) == 1 and operation != "BARRIER":
            dtypes, counts = {item[1] for item in signatures}, {item[2] for item in signatures}
            if None in dtypes or None in counts:
                _warn(group["uncertain"], "A fixed-width scalar datatype/count signature is unavailable.")
            else:
                if len(dtypes) > 1 and not (operation == "BCAST" and counts == {0}):
                    _warn(group["problems"], "Scalar datatypes disagree between ranks.")
                if len(counts) > 1:
                    _warn(group["problems"], "Element counts disagree between ranks.")
            if operation == "BCAST":
                roots = {item[3] for item in signatures}
                if None in roots:
                    _warn(group["uncertain"], "Broadcast root is unavailable.")
                elif len(roots) > 1 or not roots.issubset(set(members)):
                    _warn(group["problems"], "Broadcast roots disagree or are outside the communicator.")
            if operation == "ALLREDUCE":
                reductions = {item[4] for item in signatures}
                if len(reductions) > 1:
                    if None in reductions:
                        _warn(group["uncertain"], "Reduction operator is supplied by only some ranks.")
                    else:
                        _warn(group["problems"], "Reduction operators disagree between ranks.")
        agreement = ("UNKNOWN" if group["ambiguous"] else "MISMATCH" if group["problems"] else
                     "UNKNOWN" if group["uncertain"] else "MATCH")
        # An ambiguous communicator/rank identity cannot substantiate a blame
        # assignment even when the merged records appear to disagree.
        status = agreement if agreement != "MATCH" else "INCOMPLETE" if missing or unfinished else "MATCH"
        finding = {key: group[key] for key in ("job_id", "attempt", "cluster", "communicator", "generation", "sequence")}
        finding.update(operation=operation, members=list(members), observed_ranks=sorted(ranks),
                       missing_ranks=missing, unfinished_ranks=unfinished, agreement=agreement,
                       status=status, reasons=group["problems"] + group["uncertain"])
        finding["rank_evidence"] = [dict(rank=rank, stream=entry["owner"][0], node=entry["owner"][1],
                                        pid=entry["owner"][2], operation=entry["signature"][0],
                                        dtype=entry["signature"][1], count=entry["signature"][2],
                                        root=entry["signature"][3], reduction=entry["signature"][4], phases=sorted(entry["phases"]))
                                    for rank, entry in sorted(ranks.items())]
        if invalid:
            finding.update(status="UNKNOWN", agreement="UNKNOWN")
            finding["reasons"].append("Invalid trace envelopes prevent certified protocol comparison.")
        findings.append(finding)
    statuses = {item["status"] for item in findings}
    if invalid:
        status = "UNKNOWN"
    elif "MISMATCH" in statuses:
        status = "MISMATCH"
    elif "UNKNOWN" in statuses:
        status = "UNKNOWN"
    elif warnings or "INCOMPLETE" in statuses:
        status = "INCOMPLETE"
    else:
        status = "MATCH" if findings else "EMPTY"
    rows = ["Declared collective signatures only; absent progress is not proof of deadlock.",
            "Reduction operators are checked when supplied; absent operator evidence is outside the audit scope."]
    rows.extend(f"{item['communicator']} / generation {item['generation']} / sequence {item['sequence']}: "
                f"{item['operation']} {item['status']} ({len(item['observed_ranks'])}/{len(item['members'])} ranks)"
                for item in findings)
    if not findings:
        rows.append("No valid collective evidence is available.")
    return _result(status, "Collective protocol evidence", rows, warnings,
                   dict(collectives=findings, scope="declared intracommunicator scalar operations"))
