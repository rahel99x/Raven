"""Pure bounded navigation over published trace rows, with explicit ancestry."""

from __future__ import annotations


def lane_key(row):
    return str(
        row.get("lane_id")
        or "/".join(
            str(row.get(name, "?")) for name in ("source_id", "thread", "kind", "depth")
        )
    )


def matches(row, view):
    sources = view.get("source_ids")
    data = row.get("data") or {}
    return (not sources or row.get("source_id") in sources) and (
        view.get("thread") is None
        or view["thread"] in {str(row.get("thread")), str(data.get("source_tid"))}
    )


def _stack_level(row):
    data = row.get("data") or {}
    if row.get("kind") == "python":
        depth = row.get("depth")
    elif (
        row.get("kind") in {"host", "cooperative"}
        and data.get("boundary_fidelity") == "matched-producer-boundaries"
    ):
        depth = data.get("depth")
    elif row.get("kind") == "cpu_sample" and row.get("detail_id"):
        depth = row.get("depth")
    else:
        return None
    return depth if type(depth) is int and 0 <= depth <= 65536 else None


def _context(row):
    data = row.get("data") or {}
    return (
        row.get("source_id"),
        row.get("clock_id"),
        row.get("thread"),
        data.get("source_node"),
        data.get("source_pid"),
        data.get("thread_generation"),
        row.get("detail_id") if row.get("kind") == "cpu_sample" else None,
    )


def related(rows, selected, direction):
    """Only recorded synchronous nesting (or one observed sample stack) qualifies.

    Missing callers outside a bounded viewport stay unknown. Device activities
    and overlapping independent spans are not turned into synchronous children.
    """
    if not selected or _stack_level(selected) is None:
        return None
    depth = _stack_level(selected)
    start, end = selected.get("start_ns"), selected.get("end_ns")
    if type(start) is not int or type(end) is not int:
        return None
    candidates = []
    for row in rows:
        if _context(row) != _context(selected) or row.get("id") == selected.get("id"):
            continue
        target_depth = depth - 1 if direction == "parent" else depth + 1
        if _stack_level(row) != target_depth:
            continue
        left, right = row.get("start_ns"), row.get("end_ns")
        if type(left) is not int or type(right) is not int:
            continue
        if (
            (left <= start <= end <= right)
            if direction == "parent"
            else (start <= left <= right <= end)
        ):
            candidates.append(row)
    if direction == "parent" and len(candidates) != 1:
        return None
    return (
        min(
            candidates,
            key=lambda row: (
                row["start_ns"],
                row.get("first_seq", 0),
                str(row.get("id")),
            ),
        )
        if candidates
        else None
    )
