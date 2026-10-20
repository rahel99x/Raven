"""Bounded visual intervals from an already verified, single-stream snapshot.

Pairing reuses the interoperable export rules. A missing entry, exit or loss
boundary remains unknown: no span is extended to the edge of the viewport.
"""

from __future__ import annotations

import heapq

from ..tracing.provenance import _pairs
from ..job_logs.text import display_text

LOSS_KINDS = frozenset({"gap", "lost", "drop", "loss", "dropped"})
FRAME_KINDS = frozenset({"call", "resume", "return", "yield", "unwind"})


def _label(value, default, limit=8192):
    return display_text(value[:limit]) if isinstance(value, str) and value else default


def build(snapshot):
    """Project at most the reader's retained events; never read another file.

    Intervals use integer nanoseconds relative to this stream's first retained
    event. Python depths represent matched physical-thread nesting. Cooperative
    spans have separate nonoverlapping lanes, not inferred parent relationships.
    """
    if snapshot.get("status") == "INVALID":
        raise ValueError(
            "Cannot visualize an invalid trace; inspect its integrity warnings"
        )
    events = snapshot.get("events", ())
    if len(events) > 20_000:
        raise ValueError("Visual trace exceeds the 20,000-event retained window")
    origin = events[0]["mono_ns"] if events else 0
    frame_pairs, omitted = _pairs(events)
    span_pairs, spans, span_events = {}, {}, 0
    for index, event in enumerate(events):
        kind, data, thread = event["kind"], event["data"], event["thread"]
        if kind in LOSS_KINDS:
            spans.clear()
        elif kind in {"span_start", "span_end"}:
            span_events += 1
            span_id = data.get("span_id")
            if not isinstance(span_id, str) or not span_id:
                continue
            key = (thread, span_id)
            if kind == "span_start":
                # A reused active ID does not establish the earlier boundary.
                spans[key] = index
            elif key in spans:
                span_pairs[spans.pop(key)] = index

    intervals, markers, threads = (
        [],
        [],
        dict.fromkeys(event["thread"] for event in events),
    )
    stacks, cooperative_lanes = {}, {}
    closes = {closing: opening for opening, closing in frame_pairs.items()}
    consumed = (
        set(frame_pairs)
        | set(frame_pairs.values())
        | set(span_pairs)
        | set(span_pairs.values())
    )
    for index, event in enumerate(events):
        kind, data, thread = event["kind"], event["data"], event["thread"]
        stack = stacks.setdefault(thread, [])
        if index in closes and stack and stack[-1] == closes[index]:
            stack.pop()
        if index in frame_pairs or index in span_pairs:
            is_frame = index in frame_pairs
            closing = (frame_pairs if is_frame else span_pairs)[index]
            last = events[closing]
            if is_frame:
                depth = len(stack)
                stack.append(index)
            else:
                occupied, available, next_lane = cooperative_lanes.setdefault(
                    thread, ([], [], [0])
                )
                while occupied and occupied[0][0] <= event["mono_ns"]:
                    _, free = heapq.heappop(occupied)
                    heapq.heappush(available, free)
                if available:
                    depth = heapq.heappop(available)
                else:
                    depth = next_lane[0]
                    next_lane[0] += 1
                heapq.heappush(occupied, (last["mono_ns"], depth))
            category = "python" if is_frame else "cooperative"
            intervals.append(
                dict(
                    id=category + ":" + str(event["seq"]),
                    kind=category,
                    thread=thread,
                    depth=depth,
                    name=_label(
                        data.get("function" if is_frame else "name"), "unnamed"
                    ),
                    file=_label(data.get("file"), ""),
                    line=data.get("line") if type(data.get("line")) is int else None,
                    start_ns=event["mono_ns"] - origin,
                    end_ns=last["mono_ns"] - origin,
                    duration_ns=last["mono_ns"] - event["mono_ns"],
                    first_seq=event["seq"],
                    last_seq=last["seq"],
                    exit=last["kind"],
                )
            )
        if index not in consumed:
            markers.append(
                dict(
                    id="event:" + str(event["seq"]),
                    thread=thread,
                    name=_label(data.get("function") or data.get("name"), kind),
                    start_ns=event["mono_ns"] - origin,
                    seq=event["seq"],
                    kind=kind,
                )
            )

    warnings = list(snapshot.get("warnings", ()))
    unpaired_spans = span_events - 2 * len(span_pairs)
    if omitted or unpaired_spans:
        warnings.append(
            f"Unknown boundaries: {omitted} unmatched Python frame events and {unpaired_spans} unmatched span events; no durations invented."
        )
    if any(event["kind"] in LOSS_KINDS for event in events):
        warnings.append(
            "A recorded loss boundary interrupts intervals; its missing duration is unknown."
        )
    from .cpu_timeline import build as build_cpu

    cpu = build_cpu(snapshot, origin)
    end = max(
        [events[-1]["mono_ns"] - origin if events else 0]
        + [row["end_ns"] for row in cpu["observations"]]
    )
    return dict(
        schema="raven.trace-timeline/v1",
        identity=dict(snapshot.get("identity", {})),
        coverage=dict(snapshot.get("covered", {})),
        status=snapshot.get("status", "PARTIAL"),
        warnings=warnings,
        origin_ns=origin,
        start_ns=0,
        end_ns=end,
        cpu=cpu,
        intervals=intervals,
        threads=list(threads),
        markers=markers,
        unpaired_frame_events=omitted,
        unpaired_span_events=unpaired_spans,
        retained_events=len(events),
        read_seq=snapshot.get("read_seq", 0),
        committed_seq=snapshot.get("committed_seq", 0),
        evicted_events=snapshot.get("evicted", 0),
        sha256=snapshot.get("sha256", ""),
    )


def index(model):
    """Build one bounded worker-owned search index; paint does no event parsing."""
    by_thread = {thread: {} for thread in model["threads"]}
    by_id = {}
    for interval in model["intervals"]:
        lane = by_thread[interval["thread"]].setdefault(
            (interval["kind"], interval["depth"]), []
        )
        lane.append(interval)
        by_id[interval["id"]] = interval
    cpu_lanes = {lane["key"]: [] for lane in model["cpu"]["lanes"]}
    for observation in model["cpu"]["observations"]:
        cpu_lanes[observation["lane_key"]].append(observation)
        by_id[observation["id"]] = observation
    return {
        "threads": {
            thread: {
                key: (rows, [row["end_ns"] for row in rows])
                for key, rows in lanes.items()
            }
            for thread, lanes in by_thread.items()
        },
        "by_id": by_id,
        "losses": [
            marker for marker in model["markers"] if marker["kind"] in LOSS_KINDS
        ],
        "cpu_lanes": [
            (
                lane,
                cpu_lanes[lane["key"]],
                [row["at_ns"] for row in cpu_lanes[lane["key"]]],
            )
            for lane in model["cpu"]["lanes"]
        ],
    }
