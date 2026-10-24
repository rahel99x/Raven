"""Bounded statistics over recorded intervals, not inferred machine efficiency."""

from __future__ import annotations

from collections import defaultdict

from .workspace_model_clocks import integer, key, text
from .workspace_model_topology import (
    MAX_ITEMS,
    _data,
    _items,
    _label,
    _logical_task,
    _producer_location,
    _row_source,
    _sources,
    device_identity,
)

_TIMED = frozenset(
    {
        "python",
        "cooperative",
        "host",
        "gpu_kernel",
        "gpu_copy",
        "gpu_sync",
        "gpu_synchronization",
    }
)
_LOSS = frozenset({"gap", "lost", "drop", "loss", "dropped"})


def _bounds(row):
    start = integer(row.get("start_ns"), "interval start")
    end = integer(row.get("end_ns"), "interval end")
    if end < start:
        raise ValueError("Trace interval end precedes its start")
    if (
        "duration_ns" in row
        and row["duration_ns"] is not None
        and integer(row["duration_ns"], "interval duration") != end - start
    ):
        raise ValueError("Trace interval duration disagrees with its boundaries")
    return start, end


def union_duration(intervals, *, start_ns=None, end_ns=None):
    """Measure the union in one clock domain; touching/overlapping rows count once."""
    if not isinstance(intervals, (list, tuple)) or len(intervals) > MAX_ITEMS:
        raise ValueError("Trace duration union exceeds its bounded query window")
    if start_ns is not None:
        integer(start_ns, "range start")
    if end_ns is not None:
        integer(end_ns, "range end")
    if start_ns is not None and end_ns is not None and end_ns < start_ns:
        raise ValueError("Trace range end precedes its start")
    clocks, ranges = set(), []
    for row in intervals:
        if isinstance(row, dict):
            start, end = _bounds(row)
            if row.get("clock_id") is not None:
                clocks.add(text(row["clock_id"], "clock domain ID"))
        elif isinstance(row, (list, tuple)) and len(row) == 2:
            start, end = (
                integer(row[0], "interval start"),
                integer(row[1], "interval end"),
            )
            if end < start:
                raise ValueError("Trace interval end precedes its start")
        else:
            raise ValueError("Trace duration union needs interval pairs")
        start = max(start, start_ns) if start_ns is not None else start
        end = min(end, end_ns) if end_ns is not None else end
        if end > start:
            ranges.append((start, end))
    if len(clocks) > 1:
        raise ValueError("Cannot union intervals from different unaligned clocks")
    total, left, right = 0, None, None
    for start, end in sorted(ranges):
        if left is None:
            left, right = start, end
        elif start <= right:
            right = max(right, end)
        else:
            total += right - left
            left, right = start, end
    return total + (right - left if left is not None else 0)


def _self(row, elapsed):
    if row.get("kind") not in {"python", "host"} or row.get("self_ns") is None:
        return None
    value = integer(row["self_ns"], "self elapsed duration")
    if value > elapsed:
        raise ValueError("Trace self elapsed duration exceeds inclusive duration")
    return value


def _symbol(row):
    data = _data(row)
    text(row.get("source_id"), "source ID")
    text(row.get("clock_id"), "clock domain ID")
    name = row.get("name", "unnamed")
    name = name if isinstance(name, str) and name else "unnamed"
    file = row.get("file", "")
    file = file if isinstance(file, str) else ""
    line = (
        row.get("line") if type(row.get("line")) is int and row["line"] >= 0 else None
    )
    build = data.get("build_id", row.get("build_id"))
    # Missing symbol/build provenance does not authorize merging unrelated
    # processes' same-named functions into a supposedly identical binary.
    owner = (
        build
        if isinstance(build, str) and build
        else (
            row.get("source_id"),
            data.get("source_node"),
            data.get("source_pid"),
            data.get("source_process_start"),
            data.get("source_process_uuid"),
            data.get("process_generation"),
            row.get("device", data.get("device"))
            if row.get("kind", "").startswith("gpu_")
            else None,
            row.get("context", data.get("context"))
            if row.get("kind", "").startswith("gpu_")
            else None,
            data.get("context_generation")
            if row.get("kind", "").startswith("gpu_")
            else None,
        )
    )
    identifier = key("symbol", owner, row.get("kind"), name[:8192], file[:8192], line)
    return dict(
        id=identifier,
        name=_label(name, "unnamed", 8192),
        file=_label(file, "", 8192),
        line=line,
        kind=row.get("kind"),
    )


def function_statistics(query, *, limit=100):
    """Aggregate complete returned boundaries; totals are sums of call durations."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Invalid trace function-statistics limit")
    intervals, _ = _items(query)
    groups = {}
    for row in intervals:
        if row.get("kind") not in _TIMED:
            continue
        start, end = _bounds(row)
        symbol = _symbol(row)
        group = groups.setdefault(
            symbol["id"],
            dict(
                symbol,
                count=0,
                inclusive_ns=0,
                self_ns=0,
                self_unavailable_calls=0,
                min_ns=None,
                max_ns=None,
                source_ids=set(),
                clock_ids=set(),
            ),
        )
        elapsed = end - start
        group["count"] += 1
        group["inclusive_ns"] += elapsed
        group["min_ns"] = (
            elapsed if group["min_ns"] is None else min(elapsed, group["min_ns"])
        )
        group["max_ns"] = (
            elapsed if group["max_ns"] is None else max(elapsed, group["max_ns"])
        )
        own = _self(row, elapsed)
        if own is None:
            group["self_unavailable_calls"] += 1
        else:
            group["self_ns"] += own
        group["source_ids"].add(row.get("source_id"))
        group["clock_ids"].add(row.get("clock_id"))
    ordered = sorted(
        groups.values(), key=lambda row: (-row["inclusive_ns"], row["name"], row["id"])
    )
    result = []
    for group in ordered[:limit]:
        group["source_ids"] = sorted(
            value for value in group["source_ids"] if isinstance(value, str)
        )
        group["clock_ids"] = sorted(
            value for value in group["clock_ids"] if isinstance(value, str)
        )
        if group["self_unavailable_calls"]:
            group["self_ns"] = None
        result.append(group)
    return dict(
        schema="raven.trace-function-statistics/v1",
        units="nanoseconds",
        scope="Matched intervals returned by this query; not automatically the whole run",
        groups=result,
        omitted_groups=max(0, len(ordered) - limit),
        truncated_query=bool(query.get("truncated")),
        limitations=[
            "Inclusive elapsed totals may overlap across recursion, threads and processes; their sum is not job makespan or CPU time.",
            "Self elapsed requires validated same-activation nesting supplied by the index; missing children or GPU activity do not become zero.",
            "Sampling does not establish exact per-call duration. Unavailable self durations are null, not zero.",
        ],
    )


def _capacity(capacity, clock, query):
    if capacity is None:
        return None
    if not isinstance(capacity, dict):
        raise ValueError("Trace resource capacity must be an object")
    value = capacity if capacity.get("clock_id") == clock else capacity.get(clock)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Trace clock capacity must be an object")
    start = integer(value.get("start_ns"), "capacity range start")
    end = integer(value.get("end_ns"), "capacity range end")
    if end <= start:
        raise ValueError("Trace capacity needs a positive time range")
    if "logical_cpu_count" in value:
        integer(
            value["logical_cpu_count"],
            "allocated logical CPU count",
            minimum=1,
            maximum=8192,
        )
    if "allocated_logical_cpu_ns" in value:
        integer(
            value["allocated_logical_cpu_ns"],
            "allocated logical CPU time",
            minimum=1,
            maximum=((1 << 64) - 1) * 8192,
        )
    if "source_ids" in value:
        if (
            not isinstance(value["source_ids"], (list, tuple))
            or len(value["source_ids"]) > 512
        ):
            raise ValueError("Capacity source scope must be a bounded source-ID array")
        for source_id in value["source_ids"]:
            text(source_id, "capacity source ID")
    selected = query.get("range")
    if selected is not None and not isinstance(selected, dict):
        raise ValueError("Trace selected range must be an object")
    matches = not selected or all(
        selected.get(field) is None or selected[field] == expected
        for field, expected in (
            ("start_ns", start),
            ("end_ns", end),
            ("clock_id", clock),
        )
    )
    return dict(value, start_ns=start, end_ns=end, range_matches=matches)


def _metric(
    name,
    value,
    unit,
    *,
    clock_id=None,
    numerator=None,
    denominator=None,
    reason=None,
    **scope,
):
    return dict(
        name=name,
        value=value,
        unit=unit,
        clock_id=clock_id,
        numerator=numerator,
        denominator=denominator,
        availability="available" if value is not None else "unavailable",
        reason=reason,
        **scope,
    )


def _coverage_ready(query, sources, rows, capacity, markers):
    """A denominator declaration cannot fill missing or not-yet-read evidence."""
    if query.get("truncated") or query.get("caught_up") is False:
        return False
    relevant = {row.get("source_id") for row in rows}
    declared = capacity.get("source_ids", ()) if capacity else ()
    if isinstance(declared, (list, tuple)):
        relevant.update(declared)
    clock = rows[0].get("clock_id") if rows else None
    for marker in markers:
        if (
            marker.get("kind") in _LOSS
            and (marker.get("source_id") is None or marker.get("source_id") in relevant)
            and marker.get("clock_id", clock) == clock
        ):
            return False
    for source_id in relevant:
        source = sources.get(source_id)
        if source is None:
            continue  # A direct legacy caller's explicit capacity is still checked below.
        status = str(source.get("status", "")).upper()
        read_status = str(source.get("read_status", "")).upper()
        if status in {
            "INVALID",
            "PARTIAL",
            "ERROR",
            "LIMIT",
            "REVALIDATING",
            "QUOTA",
        } or read_status in {
            "INVALID",
            "PARTIAL",
            "ERROR",
            "LIMIT",
            "REVALIDATING",
            "QUOTA",
        }:
            return False
        if source.get("caught_up") is False or source.get("validated") is False:
            return False
        coverage = source.get("coverage", {})
        if (
            not isinstance(coverage, dict)
            or coverage.get("complete") is False
            or coverage.get("input_complete") is False
        ):
            return False
    return True


def resource_metrics(query, *, capacity=None):
    """Union device/CPU activity; percentages require explicit complete coverage.

    `capacity` is a per-clock descriptor with start/end, recorded allocation
    provenance, coverage_complete, and optional logical_cpu_count. GPU percent
    additionally requires gpu_coverage_complete. No descriptor is inferred from
    call duration, CPU brand, event spacing or a global machine CPU count.
    """
    intervals, markers = _items(query)
    sources = _sources(query)
    cpu, physical, devices = defaultdict(list), defaultdict(list), {}
    thread_states = {}
    for row in intervals:
        source_id, clock, source, identity = _row_source(row, sources)
        data = _data(row)
        location = _producer_location(row, source, identity)
        kind = row.get("kind")
        state = data.get("state")
        if kind == "scheduling" and state in {
            "running",
            "on-cpu",
            "blocked",
            "runnable",
            "off-cpu",
            "waiting",
        }:
            _bounds(row)
            task, task_generation = _logical_task(data)
            thread_scope = dict(
                source_id=source_id,
                node=location["node"],
                boot_id=location["boot_id"],
                node_lifetime=location["id"],
                source_pid=data.get("source_pid", identity.get("pid")),
                process_generation=data.get(
                    "process_generation",
                    data.get(
                        "source_process_start",
                        identity.get("process_start")
                        if "source_pid" not in data
                        else None,
                    ),
                ),
                thread=data.get("source_tid", row.get("thread")),
                thread_generation=data.get("thread_generation"),
                logical_task_id=task,
                task_generation=task_generation,
            )
            identifier = key("scheduler-thread", clock, thread_scope)
            group = thread_states.setdefault(
                (clock, identifier),
                {"scope": thread_scope, "states": defaultdict(list)},
            )
            group["states"]["on-cpu" if state == "running" else state].append(row)
        if kind == "scheduling" and data.get("state") in {"running", "on-cpu"}:
            cpu_id = row.get("logical_cpu", data.get("logical_cpu"))
            if type(cpu_id) is not int or not 0 <= cpu_id < 1 << 31:
                continue
            _bounds(row)
            node, boot, owner = location["node"], location["boot_id"], location["id"]
            cpu[(clock, node, boot, owner, cpu_id)].append(row)
            socket = row.get("socket_id", data.get("socket_id"))
            core = row.get("core_id", data.get("core_id"))
            die = row.get("die_id", data.get("die_id"))
            die = die if type(die) is int and 0 <= die < 1 << 31 else None
            if type(socket) is int and socket >= 0 and type(core) is int and core >= 0:
                physical[
                    (clock, node, boot, owner, socket, -1 if die is None else die, core)
                ].append(row)
        elif kind in {"gpu_kernel", "gpu_copy", "gpu_sync", "gpu_synchronization"}:
            _bounds(row)
            if kind not in {"gpu_kernel", "gpu_copy"}:
                continue
            device_scope = (
                key(
                    "device-source",
                    source_id,
                    data.get("source_node"),
                    data.get("source_pid"),
                    data.get("source_process_start"),
                    data.get("source_process_uuid"),
                    data.get("process_generation"),
                )
                if "source_pid" in data
                else source_id
            )
            device = device_identity(
                row.get("device", data.get("device")),
                source_id=device_scope,
                node=location["node"],
                boot_id=location["boot_id"],
                cluster=identity.get("cluster", "unknown"),
            )
            engine = data.get(
                "engine", "compute" if kind == "gpu_kernel" else "any-copy-engine"
            )
            engine = _label(engine, "unknown", 256)
            group = devices.setdefault(
                (clock, device["id"], engine), dict(device=device, rows=[])
            )
            group["rows"].append(row)
    metrics, cpu_totals, cpu_rows = [], defaultdict(int), defaultdict(list)
    for (clock, node, boot, owner, cpu_id), rows in sorted(cpu.items()):
        cap = _capacity(capacity, clock, query)
        value = union_duration(
            rows,
            start_ns=cap["start_ns"] if cap else None,
            end_ns=cap["end_ns"] if cap else None,
        )
        cpu_totals[clock] += value
        cpu_rows[clock].extend(rows)
        metrics.append(
            _metric(
                "logical_cpu_on_cpu_ns",
                value,
                "ns",
                clock_id=clock,
                node=node,
                boot_id=boot,
                node_lifetime=owner,
                logical_cpu=cpu_id,
                provenance="Union of producer-recorded running scheduler slices",
            )
        )
    for clock, total in sorted(cpu_totals.items()):
        cap = _capacity(capacity, clock, query)
        denominator = None
        if (
            cap
            and (cap.get("logical_cpu_count") or cap.get("allocated_logical_cpu_ns"))
            and cap.get("coverage_complete") is True
            and cap.get("cpu_coverage_complete", cap.get("coverage_complete")) is True
            and cap["range_matches"]
            and isinstance(cap.get("provenance"), str)
            and cap["provenance"]
            and _coverage_ready(query, sources, cpu_rows[clock], cap, markers)
        ):
            denominator = (
                cap.get("allocated_logical_cpu_ns")
                or (cap["end_ns"] - cap["start_ns"]) * cap["logical_cpu_count"]
            )
        # A numerator larger than the declared allocation means that the
        # allocation/capture scopes disagree. Never clamp a misleading result.
        valid = denominator is not None and total <= denominator
        metrics.append(
            _metric(
                "cpu_allocation_utilization",
                total / denominator if valid else None,
                "fraction",
                clock_id=clock,
                numerator=total,
                denominator=denominator,
                reason=None
                if valid
                else "Complete scheduler coverage and matching effective allocation/range provenance are unavailable or inconsistent",
            )
        )
    for (clock, node, boot, owner, socket, die, core), rows in sorted(physical.items()):
        cap = _capacity(capacity, clock, query)
        value = union_duration(
            rows,
            start_ns=cap["start_ns"] if cap else None,
            end_ns=cap["end_ns"] if cap else None,
        )
        metrics.append(
            _metric(
                "physical_core_busy_ns",
                value,
                "ns",
                clock_id=clock,
                node=node,
                boot_id=boot,
                node_lifetime=owner,
                socket_id=socket,
                die_id=None if die == -1 else die,
                core_id=core,
                provenance="Union across observed SMT siblings; not throughput efficiency",
            )
        )
    for (clock, _, engine), group in sorted(devices.items()):
        cap = _capacity(capacity, clock, query)
        value = union_duration(
            group["rows"],
            start_ns=cap["start_ns"] if cap else None,
            end_ns=cap["end_ns"] if cap else None,
        )
        metrics.append(
            _metric(
                "gpu_activity_busy_ns",
                value,
                "ns",
                clock_id=clock,
                device=group["device"],
                engine=engine,
                provenance="Union of reported device activities; host enqueue time excluded",
            )
        )
        complete = (
            cap
            and cap.get("coverage_complete") is True
            and cap["range_matches"]
            and cap.get("gpu_coverage_complete") is True
            and isinstance(cap.get("provenance"), str)
            and bool(cap["provenance"])
            and group["device"]["identity_scope"] != "unidentified device group"
            and _coverage_ready(query, sources, group["rows"], cap, markers)
        )
        denominator = cap["end_ns"] - cap["start_ns"] if complete else None
        metrics.append(
            _metric(
                "gpu_activity_busy_fraction",
                value / denominator if denominator else None,
                "fraction",
                clock_id=clock,
                numerator=value,
                denominator=denominator,
                device=group["device"],
                engine=engine,
                reason=None
                if denominator
                else "Complete device activity coverage and matching range provenance are unavailable",
            )
        )
    for (clock, _), group in sorted(thread_states.items()):
        cap = _capacity(capacity, clock, query)
        kwargs = dict(
            start_ns=cap["start_ns"] if cap else None,
            end_ns=cap["end_ns"] if cap else None,
        )
        off = [
            row
            for state, rows in group["states"].items()
            if state != "on-cpu"
            for row in rows
        ]
        if off:
            metrics.append(
                _metric(
                    "observed_off_cpu_ns",
                    union_duration(off, **kwargs),
                    "ns",
                    clock_id=clock,
                    **group["scope"],
                    provenance="Union of explicit thread/task scheduling state intervals; not physical-core idle time",
                )
            )
        for state, rows in sorted(group["states"].items()):
            metrics.append(
                _metric(
                    "observed_state_off_cpu_ns"
                    if state == "off-cpu"
                    else "observed_" + state.replace("-", "_") + "_ns",
                    union_duration(rows, **kwargs),
                    "ns",
                    clock_id=clock,
                    **group["scope"],
                    state=state,
                    provenance="Producer-recorded thread/task scheduling state duration; gaps remain unknown",
                )
            )
    if not cpu:
        metrics.append(
            _metric(
                "cpu_allocation_utilization",
                None,
                "fraction",
                reason="No usable scheduler running slices; function wall time and CPU placement points are insufficient",
            )
        )
    if not devices:
        metrics.append(
            _metric(
                "gpu_activity_busy_fraction",
                None,
                "fraction",
                reason="No device execution activities; host calls are not GPU execution measurements",
            )
        )
    metrics.extend(
        [
            _metric(
                "gpu_occupancy",
                None,
                "fraction",
                reason="A qualified adapter-specific occupancy counter is required; activity durations are insufficient",
            ),
            _metric(
                "parallel_efficiency",
                None,
                "fraction",
                reason="A comparable baseline workload and declared resource model are required",
            ),
        ]
    )
    return dict(
        schema="raven.trace-resource-metrics/v1",
        scope="Returned query evidence only",
        metrics=metrics,
        limitations=[
            "Unknown capture regions are not idle time.",
            "Logical CPU utilization and physical-core busy time do not measure hardware throughput efficiency.",
            "Independent clock domains remain separate; activity sums are not a global makespan.",
        ],
    )


def _recorded_nesting(row):
    data = _data(row)
    return row.get("kind") == "python" or (
        row.get("kind") in {"host", "cooperative"}
        and data.get("boundary_fidelity") == "matched-producer-boundaries"
        and type(data.get("depth")) is int
    )


def _recorded_depth(row):
    data = _data(row)
    depth = (
        data["depth"]
        if row.get("kind") != "python" and _recorded_nesting(row)
        else row.get("depth", 0)
    )
    if type(depth) is not int or not 0 <= depth <= 65536:
        raise ValueError("Invalid recorded call-tree depth")
    return depth


def call_tree(query, *, bottom_up=False, max_nodes=4096, max_depth=128):
    """Return a flat bounded tree of recorded call contexts, not inferred async ancestry."""
    if (
        type(bottom_up) is not bool
        or type(max_nodes) is not int
        or not 1 <= max_nodes <= 8192
        or type(max_depth) is not int
        or not 1 <= max_depth <= 256
    ):
        raise ValueError("Invalid trace call-tree budget")
    intervals, _ = _items(query)
    eligible = [row for row in intervals if row.get("kind") in _TIMED]
    for row in eligible:
        _bounds(row)
    ordered = sorted(
        eligible,
        key=lambda row: (
            str(row.get("source_id")),
            str(row.get("clock_id")),
            str(row.get("thread")),
            row.get("start_ns", 0),
            _recorded_depth(row),
            row.get("first_seq", 0),
            -row.get("end_ns", 0),
        ),
    )
    stacks, nodes, omitted, partial = {}, {}, 0, False
    for row in ordered:
        start, end = _bounds(row)
        task, task_generation = _logical_task(_data(row))
        context = (
            row.get("source_id"),
            row.get("clock_id"),
            (
                row.get("thread"),
                _data(row).get("source_node"),
                _data(row).get("source_pid"),
                _data(row).get("source_process_start"),
                _data(row).get("source_process_uuid"),
                _data(row).get("process_generation"),
                _data(row).get("thread_generation"),
                task,
                task_generation,
                "python-activation"
                if row.get("kind") == "python"
                else "producer-boundaries",
            ),
        )
        stack = stacks.setdefault(context, [])
        depth = _recorded_depth(row)
        symbol = _symbol(row)
        own = _self(row, end - start)
        if _recorded_nesting(row):
            while stack and not (
                stack[-1]["depth"] < depth
                and stack[-1]["start_ns"] <= start <= end <= stack[-1]["end_ns"]
            ):
                stack.pop()
            if stack and stack[-1]["depth"] + 1 != depth:
                stack.clear()
            partial = partial or (depth > 0 and not stack)
            ancestry = [item["symbol"] for item in stack] + [symbol]
        else:
            ancestry = [symbol]
        if len(ancestry) > max_depth:
            omitted += 1
            partial = True
            continue
        chain = list(reversed(ancestry)) if bottom_up else ancestry
        parent = None
        for level, item in enumerate(chain):
            identifier = key("tree", context[0], context[1], parent, item["id"])
            if identifier not in nodes and len(nodes) >= max_nodes:
                omitted += 1
                partial = True
                break
            node = nodes.setdefault(
                identifier,
                dict(
                    item,
                    id=identifier,
                    symbol_id=item["id"],
                    parent_id=parent,
                    depth=level,
                    count=0,
                    inclusive_ns=0,
                    self_ns=0,
                    self_unavailable_calls=0,
                    source_id=context[0],
                    clock_id=context[1],
                ),
            )
            # Top-down ancestors have their own observed invocation rows. Only
            # bottom-up caller paths receive the leaf invocation's weight.
            if bottom_up or level == len(chain) - 1:
                node["count"] += 1
                node["inclusive_ns"] += end - start
                if own is None:
                    node["self_unavailable_calls"] += 1
                else:
                    node["self_ns"] += own
            parent = identifier
        if _recorded_nesting(row):
            stack.append(dict(depth=depth, start_ns=start, end_ns=end, symbol=symbol))
    result = []
    for node in nodes.values():
        if node["self_unavailable_calls"]:
            node["self_ns"] = None
        result.append(node)
    return dict(
        schema="raven.trace-call-tree/v1",
        direction="bottom-up" if bottom_up else "top-down",
        units="nanoseconds of recorded elapsed time",
        nodes=result,
        omitted=omitted,
        partial=bool(partial or query.get("truncated")),
        scope="Matched intervals in this bounded query; missing callers and asynchronous links are not inferred",
        limitations=[
            "Ancestor relationships require valid Python or explicitly declared synchronous producer nesting; asynchronous/device spans remain separate roots.",
            "Inclusive durations can overlap; tree totals are not CPU time or job makespan.",
        ],
    )


def sample_statistics(query, *, limit=100):
    """Count observed stack hits/weights; no time is inferred between samples."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Invalid sampled-stack statistics limit")
    _, markers = _items(query)
    groups, observations, unavailable, scopes = {}, 0, 0, {}
    processed_frames = omitted_frames = omitted_symbols = 0
    for row in markers:
        if row.get("kind") != "cpu_sample":
            continue
        data = _data(row)
        frames = data.get("frames", [])
        if not isinstance(frames, list) or len(frames) > 2048:
            raise ValueError("Sampled-stack frame budget exceeded")
        weight = integer(
            data.get("weight", 1), "sample weight", minimum=1, maximum=(1 << 63) - 1
        )
        unit = data.get("weight_unit", "samples")
        if unit not in {"samples", "event-count", "ns"}:
            raise ValueError("Unsupported sampled-stack weight unit")
        event = str(data.get("event", "unknown"))[:256]
        order = data.get("stack_order")
        leaf = (
            len(frames) - 1
            if order == "root-to-leaf"
            else 0
            if order == "leaf-to-root"
            else None
        )
        observations += 1
        unavailable += not frames
        scope = (
            row.get("source_id"),
            row.get("clock_id"),
            data.get("source_node"),
            data.get("source_pid"),
            data.get("source_process_start"),
            data.get("source_process_uuid"),
            data.get("process_generation"),
            event,
            unit,
        )
        scoped = scopes.setdefault(
            scope, {"weight": 0, "observations": 0, "unknown_stack_weight": 0}
        )
        scoped["weight"] += weight
        scoped["observations"] += 1
        scoped["unknown_stack_weight"] += weight if not frames else 0
        seen = set()
        for index, raw in enumerate(frames):
            if processed_frames >= 100_000:
                omitted_frames += len(frames) - index
                break
            processed_frames += 1
            frame = {"name": raw} if isinstance(raw, str) else raw
            if not isinstance(frame, dict):
                raise ValueError("Sampled-stack frame must be text or an object")
            name = frame.get(
                "function", frame.get("name", frame.get("symbol", "unresolved frame"))
            )
            symbol = key(
                "sample-symbol",
                row.get("source_id"),
                row.get("clock_id"),
                data.get("source_node"),
                data.get("source_pid"),
                data.get("source_process_start"),
                data.get("source_process_uuid"),
                data.get("process_generation"),
                name,
                frame.get("dso"),
                event,
                unit,
            )
            if symbol not in groups and len(groups) >= 4096:
                omitted_symbols += 1
                continue
            group = groups.setdefault(
                symbol,
                dict(
                    id=symbol,
                    name=_label(name, "unresolved frame", 8192),
                    dso=_label(frame.get("dso"), "", 8192),
                    source_id=row.get("source_id"),
                    clock_id=row.get("clock_id"),
                    event=event,
                    weight_unit=unit,
                    source_node=data.get("source_node"),
                    source_pid=data.get("source_pid"),
                    scope=scope,
                    sample_hits=0,
                    leaf_hits=0,
                    weight=0,
                    leaf_weight=0,
                    frame_occurrences=0,
                    leaf_unknown=False,
                ),
            )
            group["frame_occurrences"] += 1
            if symbol not in seen:
                group["sample_hits"] += 1
                group["weight"] += weight
                seen.add(symbol)
            if leaf is None:
                group["leaf_unknown"] = True
            elif index == leaf:
                group["leaf_hits"] += 1
                group["leaf_weight"] += weight
    rows = sorted(
        groups.values(), key=lambda row: (-row["weight"], row["name"], row["id"])
    )
    for row in rows:
        scoped = scopes[row.pop("scope")]
        row.update(
            observed_weight_denominator=scoped["weight"],
            observed_sample_denominator=scoped["observations"],
            unknown_stack_weight=scoped["unknown_stack_weight"],
            inclusive_share=row["weight"] / scoped["weight"]
            if not omitted_frames and not omitted_symbols
            else None,
            leaf_share=row["leaf_weight"] / scoped["weight"]
            if not row["leaf_unknown"] and not omitted_frames and not omitted_symbols
            else None,
            share_unit="fraction of observed source/process/event/weight scope",
            counts_complete=not bool(omitted_frames or omitted_symbols),
        )
        if row.pop("leaf_unknown"):
            row["leaf_hits"] = row["leaf_weight"] = None
    return dict(
        schema="raven.trace-sample-statistics/v1",
        scope="Observed samples in this returned query, grouped by source/process/event/weight units",
        observations=observations,
        unavailable_stacks=unavailable,
        groups=rows[:limit],
        omitted_groups=max(0, len(rows) - limit),
        omitted_frame_occurrences=omitted_frames,
        omitted_symbol_occurrences=omitted_symbols,
        processed_frame_occurrences=processed_frames,
        bounded_work=dict(max_frame_occurrences=100_000, max_symbols=4096),
        truncated_query=bool(query.get("truncated")),
        limitations=[
            "Sample hits and weights do not establish exact per-call duration or continuous residency.",
            "A recursive function contributes one inclusive hit per sample; frame occurrences are reported separately.",
            "Missing stacks, lost observations and incomplete recording remain unknown.",
            "Shares use observed weights including unavailable stacks in the same source/process/event/unit scope; they are not exact CPU utilization or fractions of all application execution.",
            "The frame/symbol work budget leaves partial counts explicit and makes shares unavailable; aggregate returned limits do not change recorded data.",
        ],
    )
