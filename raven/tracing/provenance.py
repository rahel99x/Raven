"""Trace interval reconstruction and provenance shared by readers/exporters."""

from __future__ import annotations


def _pairs(events):
    """Pair observed frame entries/exits without inventing unknown stack ends."""
    stacks, pairs = {}, {}
    frame_events = 0
    for index, event in enumerate(events):
        kind, data = event["kind"], event["data"]
        if kind in {"gap", "lost", "drop", "loss", "dropped"}:
            # Loss can omit exits, entries or thread metadata. Do not infer a
            # duration across any unresolved boundary in this stream.
            stacks.clear()
        elif kind == "stack_checkpoint":
            stacks.pop(event["thread"], None)
        if kind not in {"call", "resume", "return", "yield", "unwind"}:
            continue
        frame_events += 1
        stack = stacks.setdefault(event["thread"], [])
        if not isinstance(data.get("frame"), str) or not data["frame"]:
            stack.clear()
            continue
        if kind in {"call", "resume"}:
            if not isinstance(data.get("function"), str):
                stack.clear()
                continue
            stack.append(index)
        elif stack:
            opening = stack[-1]
            if data.get("frame") != events[opening]["data"].get("frame"):
                stack.clear()
            else:
                stack.pop()
                pairs[opening] = index
    return pairs, frame_events - 2 * len(pairs)


def _provenance(snapshot, omitted):
    return {
        "schema": "raven.trace-export/v1",
        "identity": snapshot["identity"],
        "coverage": snapshot["covered"],
        "source_status": snapshot["status"],
        "committed_seq": snapshot["committed_seq"],
        "read_seq": snapshot["read_seq"],
        "verified_prefix_sha256": snapshot["sha256"],
        "evicted_events": snapshot["evicted"],
        "unpaired_frame_events": omitted,
        "warnings": snapshot["warnings"],
        "limitations": [
            "Only the verified retained prefix is exported.",
            "Recorded wall-time intervals are not sampled CPU time or complete logical async stacks.",
            "Unpaired frames have unknown boundaries; no synthetic closing events are added.",
        ],
    }
