"""Scheduling-backed stack slices and point-only sampled stacks."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict

from .workspace_model_clocks import integer, key


def _data(row):
    value = row.get("data", {})
    return value if isinstance(value, dict) else {}


def _owner(row, sources):
    from .workspace_model_identity import _logical_task, _producer_location, _row_source

    data = _data(row)
    # Imported process/thread IDs are meaningful within their one report.
    # Without a cross-source lifetime/native-thread mapping, source-local
    # matching is safer than confusing Python get_ident with a native TID.
    source_id, clock, source, identity = _row_source(row, sources)
    location = _producer_location(row, source, identity)
    task, task_generation = _logical_task(data)
    return (
        source_id,
        clock,
        location["id"],
        str(data.get("source_pid", identity.get("pid"))),
        (
            data.get("source_process_start"),
            data.get("source_process_uuid"),
            data.get("process_generation"),
        ),
        str(data.get("source_tid", row.get("thread"))),
        data.get("thread_generation"),
        task,
        task_generation,
    )


def execution_slices(intervals, sources, *, limit=5000):
    """Intersect matched call boundaries with explicit running scheduler slices.

    No CPU placement point, sample, scheduling guess or PID-only cross-source
    association authorizes this projection. Output is bounded even if malformed
    overlapping scheduler intervals make an interval index less selective.
    """
    if type(limit) is not int or not 1 <= limit <= 20000:
        raise ValueError("Invalid trace CPU stack-slice budget")
    schedule = defaultdict(list)
    for row in intervals:
        data = _data(row)
        cpu = row.get("logical_cpu", data.get("logical_cpu"))
        if (
            row.get("kind") == "scheduling"
            and data.get("state") in {"running", "on-cpu"}
            and type(cpu) is int
            and 0 <= cpu < 1 << 31
        ):
            start, end = (
                integer(row.get("start_ns"), "scheduler start"),
                integer(row.get("end_ns"), "scheduler end"),
            )
            if end < start:
                raise ValueError("Scheduler slice end precedes start")
            schedule[_owner(row, sources)].append((start, end, row))
    indexed = {}
    for owner, rows in schedule.items():
        rows.sort(key=lambda item: (item[0], item[1], str(item[2].get("id"))))
        maximum, ends = 0, []
        for _, end, _ in rows:
            maximum = max(maximum, end)
            ends.append(maximum)
        indexed[owner] = (rows, ends)
    result, checks, exhausted = [], 0, False
    max_checks = max(1000, min(200000, limit * 20))
    for call in intervals:
        if call.get("kind") not in {"python", "host"}:
            continue
        values = indexed.get(_owner(call, sources))
        if values is None:
            continue
        start, end = (
            integer(call.get("start_ns"), "call start"),
            integer(call.get("end_ns"), "call end"),
        )
        if end < start:
            raise ValueError("Call interval end precedes start")
        rows, ends = values
        position = bisect_right(ends, start)
        for index in range(position, len(rows)):
            first, last, slice_ = rows[index]
            checks += 1
            if checks > max_checks or len(result) >= limit:
                exhausted = True
                break
            if first >= end:
                break
            left, right = max(first, start), min(last, end)
            if right <= left:
                continue
            data = _data(slice_)
            combined = dict(
                _data(call),
                state="running",
                logical_cpu=slice_.get("logical_cpu", data.get("logical_cpu")),
                socket_id=slice_.get("socket_id", data.get("socket_id")),
                core_id=slice_.get("core_id", data.get("core_id")),
                die_id=slice_.get("die_id", data.get("die_id")),
                numa_node=slice_.get("numa_node", data.get("numa_node")),
                core_type=slice_.get("core_type", data.get("core_type")),
                scheduling_event_id=slice_.get("id"),
                original_kind=call["kind"],
            )
            result.append(
                dict(
                    call,
                    id=key("cpu-stack", call.get("id"), slice_.get("id"), left, right),
                    detail_id=call.get("id"),
                    parent_interval_id=call.get("id"),
                    scheduling_event_id=slice_.get("id"),
                    kind="cpu_stack_slice",
                    start_ns=left,
                    end_ns=right,
                    duration_ns=right - left,
                    elapsed_ns=end - start,
                    self_ns=None,
                    logical_cpu=combined["logical_cpu"],
                    socket_id=combined["socket_id"],
                    core_id=combined["core_id"],
                    die_id=combined["die_id"],
                    numa_node=combined["numa_node"],
                    core_type=combined["core_type"],
                    duration_kind="Recorded on-CPU intersection; full call elapsed is separate",
                    data=combined,
                )
            )
        if exhausted:
            break
    return dict(
        intervals=result,
        truncated=exhausted,
        checks=checks,
        warnings=[
            "CPU stack projection reached its query/work budget; narrow the selected range."
        ]
        if exhausted
        else [],
    )


def sample_items(markers, *, limit=10000):
    """Expand a sampled stack vertically at one observed time, never over time."""
    result, exhausted = [], False
    for row in markers:
        if row.get("kind") != "cpu_sample":
            continue
        data = _data(row)
        frames = data.get("frames", [])
        if not isinstance(frames, list) or len(frames) > 2048:
            raise ValueError("CPU sample stack exceeds its frame bound")
        frames = frames or [{"name": "unresolved sampled stack"}]
        if data.get("stack_order") == "leaf-to-root":
            frames = list(reversed(frames))
        for depth, frame in enumerate(frames):
            if len(result) >= limit:
                exhausted = True
                break
            value = {"name": frame} if isinstance(frame, str) else frame
            if not isinstance(value, dict):
                raise ValueError("CPU sample frame must be text or an object")
            stamp = integer(
                row.get("start_ns", data.get("sample_time_ns")), "sample timestamp"
            )
            result.append(
                dict(
                    row,
                    id=key("sample-frame", row.get("id"), depth),
                    detail_id=row.get("id"),
                    parent_interval_id=row.get("id"),
                    depth=depth,
                    name=value.get("name", value.get("symbol", "unresolved frame")),
                    file=value.get("file", value.get("dso", "")),
                    line=value.get("line"),
                    start_ns=stamp,
                    end_ns=stamp,
                    duration_ns=0,
                    self_ns=None,
                    point=True,
                    render="point",
                    logical_cpu=data.get("logical_cpu"),
                    socket_id=data.get("socket_id"),
                    core_id=data.get("core_id"),
                    die_id=data.get("die_id"),
                    numa_node=data.get("numa_node"),
                    core_type=data.get("core_type"),
                    duration_kind="Sample observation; per-call duration unknown",
                    sample_weight=data.get("weight", 1),
                    sample_weight_unit=data.get("weight_unit", "samples"),
                    data=dict(
                        {
                            field: value
                            for field, value in data.items()
                            if field != "frames"
                        },
                        sampled_frame=value,
                    ),
                )
            )
        if exhausted:
            break
    return dict(
        intervals=result,
        truncated=exhausted,
        warnings=[
            "Sampled-stack display reached its frame budget; narrow the selected range."
        ]
        if exhausted
        else [],
    )
