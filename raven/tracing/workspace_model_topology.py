"""Bounded alternative lane projections of the same recorded trace evidence."""

from __future__ import annotations

from .workspace_model_identity import (
    _component,
    _data,
    _label,
    _logical_task,
    _producer_location,
    _row_source,
)

MAX_ITEMS = 20_000
MAX_SOURCES = 4096
MAX_LANES = 8192


def _items(query):
    if not isinstance(query, dict):
        raise ValueError("Trace workspace query must be an object")
    intervals, markers = query.get("intervals", []), query.get("markers", [])
    if not isinstance(intervals, (list, tuple)) or not isinstance(
        markers, (list, tuple)
    ):
        raise ValueError("Trace workspace items must be bounded arrays")
    if len(intervals) + len(markers) > MAX_ITEMS:
        raise ValueError("Trace workspace projection exceeds its bounded query window")
    if any(not isinstance(row, dict) for row in (*intervals, *markers)):
        raise ValueError("Trace workspace items must be objects")
    return intervals, markers


def _sources(query):
    sources = query.get("sources", [])
    if not isinstance(sources, (list, tuple)) or len(sources) > MAX_SOURCES:
        raise ValueError("Trace workspace source catalog exceeds its bound")
    result = {}
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("id"), str):
            raise ValueError("Trace workspace source needs a stable ID")
        if source["id"] in result:
            raise ValueError("Duplicate trace workspace source identity")
        result[source["id"]] = source
    return result


def _numeric_id(value):
    return value if type(value) is int and 0 <= value < 1 << 31 else None


def device_identity(
    device, *, source_id, node="unknown", boot_id="unknown", cluster="unknown"
):
    """Identify an observed device without treating a local ordinal as global."""
    from .workspace_model_clocks import key, known, text

    text(source_id, "source ID")
    if device is None:
        device = {}
    if not isinstance(device, dict):
        raise ValueError("Trace device metadata must be an object")
    for field in ("id", "partition"):
        value = device.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (str, int))
        ):
            raise ValueError("Invalid trace device " + field)
    vendor = _component(device.get("vendor"))
    uuid = device.get("uuid")
    uuid = text(uuid, "device UUID") if uuid is not None else None
    partition = _component(device.get("partition"), "whole-or-unknown")
    ordinal = device.get("ordinal")
    if ordinal is not None and (type(ordinal) is not int or not 0 <= ordinal < 1 << 31):
        raise ValueError("Invalid trace device ordinal")
    reported = _component(
        device.get("id"), str(ordinal) if ordinal is not None else "unknown"
    )
    if uuid is not None and known(node) and known(boot_id):
        parts = (cluster, node, boot_id, vendor, uuid, partition)
        scope = "recorded-physical-identity"
    else:
        parts = (source_id, node, boot_id, vendor, uuid, reported, partition, ordinal)
        scope = (
            "source-local; physical identity or device lifetime unavailable"
            if device.get("id") is not None or ordinal is not None
            else "unidentified device group"
        )
    label = f"GPU {reported} ({vendor})"
    if uuid is not None:
        label += " / " + _label(uuid)
    if partition != "whole-or-unknown":
        label += " / partition " + partition
    return dict(
        id=key("device", *parts),
        label=label,
        vendor=vendor,
        uuid=uuid,
        partition=partition,
        ordinal=ordinal,
        node=node,
        boot_id=boot_id,
        identity_scope=scope,
    )


def _lane(row, sources, perspective):
    from .workspace_model_clocks import key

    source_id, clock_id, source, identity = _row_source(row, sources)
    data = _data(row)
    location = _producer_location(row, source, identity)
    node = location["node"]
    kind = row.get("kind", "unknown")
    if not isinstance(kind, str) or len(kind) > 128:
        raise ValueError("Invalid trace interval kind")
    thread = _component(data.get("source_tid", row.get("thread")))
    generation = _component(data.get("thread_generation"))
    depth = row.get("display_depth", row.get("depth", 0))
    if type(depth) is not int or not 0 <= depth <= 65536:
        raise ValueError("Invalid recorded trace stack depth")
    rank = identity.get("rank")
    process = _component(data.get("source_pid", identity.get("pid")))
    process_generation = (
        data.get("source_process_start"),
        data.get("source_process_uuid"),
        data.get("process_generation"),
    )
    process_label = (
        f"Rank {_component(rank)} / " if rank is not None else ""
    ) + f"PID {process}"
    lifetime = next(
        (str(value) for value in process_generation if value is not None), None
    )
    if (
        lifetime is None
        and "source_pid" not in data
        and process == str(identity.get("pid"))
    ):
        lifetime = _component(identity.get("process_start"), "") or None
    source_clock = source.get("clock", {})
    source_clock = source_clock if isinstance(source_clock, dict) else {}
    clock_producer = source_clock.get("producer", {})
    if (
        lifetime is None
        and isinstance(clock_producer, dict)
        and process == str(clock_producer.get("pid"))
    ):
        lifetime = _component(clock_producer.get("process_start"), "") or None
    process_label += " · " + (
        "lifetime " + _label(lifetime, limit=32)
        if lifetime
        else "stream " + source_id[-8:]
    )
    thread_label = (
        "Thread "
        + thread
        + (" · generation " + generation if generation != "unknown" else "")
    )
    raw_task, raw_task_generation = _logical_task(data)
    task = _component(raw_task)
    task_generation = _component(raw_task_generation)
    node_label = location["label"]
    path, parts, fidelity = [], [], "recorded interval"
    extra = {}
    path_ids = []
    is_gpu = kind in {"gpu_kernel", "gpu_copy", "gpu_sync", "gpu_synchronization"}
    if perspective == "cpu":
        if kind not in {
            "cpu_observation",
            "scheduling",
            "cpu_stack_slice",
            "cpu_sample",
        }:
            return None
        cpu = _numeric_id(row.get("logical_cpu", data.get("logical_cpu")))
        socket = _numeric_id(row.get("socket_id", data.get("socket_id")))
        core = _numeric_id(row.get("core_id", data.get("core_id")))
        die = _numeric_id(row.get("die_id", data.get("die_id")))
        numa = _numeric_id(row.get("numa_node", data.get("numa_node")))
        core_type_value = row.get("core_type", data.get("core_type"))
        core_type = _label(core_type_value, default="", limit=128) or None
        if kind in {"scheduling", "cpu_stack_slice"} and (
            cpu is None or data.get("state") not in {"running", "on-cpu"}
        ):
            return None
        path = [node_label, "Socket " + _component(socket)]
        resource_scope = (
            location["id"]
            if socket is not None and core is not None
            else key("unknown-topology", location["id"], source_id)
        )
        path_ids = [location["id"], key("socket", resource_scope, socket)]
        if die is not None:
            path.append("Die " + str(die))
            path_ids.append(key("die", resource_scope, socket, die))
        core_label = "Core " + _component(core)
        if core_type:
            core_label += " · " + core_type
        if numa is not None:
            core_label += " · NUMA " + str(numa)
        path.extend([core_label, "Logical CPU " + _component(cpu)])
        path_ids.extend(
            [
                key("core", resource_scope, socket, die, core),
                key(
                    "logical-cpu",
                    location["id"],
                    cpu,
                    source_id if cpu is None else None,
                ),
            ]
        )
        if kind in {"cpu_stack_slice", "cpu_sample"}:
            path.append(thread_label + " / stack depth " + str(depth))
            if task != "unknown":
                path[-1] += (
                    " / logical task "
                    + task
                    + (
                        " · generation " + task_generation
                        if task_generation != "unknown"
                        else ""
                    )
                )
            path_ids.append(
                key(
                    "cpu-thread-stack",
                    source_id,
                    process,
                    process_generation,
                    thread,
                    generation,
                    raw_task,
                    raw_task_generation,
                    kind,
                    depth,
                )
            )
        # Without physical topology, the logical ID stays scoped to its source.
        parts = (location["id"], socket, die, core, cpu, clock_id)
        if kind in {"cpu_stack_slice", "cpu_sample"}:
            parts += (
                source_id,
                process_generation,
                thread,
                generation,
                raw_task,
                raw_task_generation,
                kind,
                depth,
            )
        fidelity = (
            "placement query bracket; residency unknown"
            if kind == "cpu_observation"
            else "producer-recorded scheduler execution slice"
        )
        if kind == "cpu_sample":
            fidelity = (
                "sampled stack at one observed time; continuous execution unknown"
            )
        elif kind == "cpu_stack_slice":
            fidelity = (
                "intersection of recorded call and scheduler execution boundaries"
            )
        extra.update(
            logical_cpu=cpu,
            socket_id=socket,
            die_id=die,
            numa_node=numa,
            core_id=core,
            core_type=core_type,
        )
    elif perspective == "gpu" or is_gpu:
        if not is_gpu:
            return None
        device_scope = (
            key(
                "device-source",
                source_id,
                data.get("source_node"),
                data.get("source_pid"),
                process_generation,
            )
            if "source_pid" in data
            else source_id
        )
        device = device_identity(
            row.get("device", data.get("device")),
            source_id=device_scope,
            node=node,
            boot_id=location["boot_id"],
            cluster=_component(identity.get("cluster")),
        )
        context = _component(row.get("context", data.get("context")))
        queue = _component(row.get("queue", data.get("queue")))
        context_generation = _component(data.get("context_generation"))
        queue_generation = _component(data.get("queue_generation"))
        path = [
            node_label,
            device["label"],
            process_label + " / context " + context,
            "Stream/queue " + queue,
            kind.removeprefix("gpu_"),
        ]
        context_key = key(
            "context",
            device["id"],
            source_id,
            process,
            process_generation,
            context,
            context_generation,
        )
        queue_key = key("queue", context_key, queue, queue_generation)
        path_ids = [
            location["id"],
            device["id"],
            context_key,
            queue_key,
            key("activity-kind", queue_key, kind),
        ]
        parts = (
            row.get("activity_lane", 0),
            device["id"],
            source_id,
            process,
            process_generation,
            context,
            context_generation,
            queue,
            queue_generation,
            kind,
            clock_id,
        )
        if row.get("overlapping_activity"):
            path.append("Independent activity lane " + str(row.get("activity_lane", 0)))
            path_ids.append(
                key("activity-slot", path_ids[-1], row.get("activity_lane", 0))
            )
        fidelity = "producer-recorded device activity; host duration is separate"
        extra.update(device=device, context=context, queue=queue)
    else:
        if kind in {"cpu_observation", "flow"}:
            return None
        path = [
            node_label,
            process_label,
            thread_label,
            f"{kind} / "
            + (
                "depth "
                if row.get("recorded_nesting", kind == "python")
                else "independent activity lane "
            )
            + str(depth),
        ]
        parts = (
            source_id,
            process,
            process_generation,
            thread,
            generation,
            kind,
            depth,
            clock_id,
        )
        process_key = key(
            "process", location["id"], source_id, process, process_generation
        )
        thread_key = key("thread", process_key, thread, generation)
        path_ids = [location["id"], process_key, thread_key]
        if task != "unknown":
            path.insert(
                3,
                "Logical task "
                + task
                + (
                    " · generation " + task_generation
                    if task_generation != "unknown"
                    else ""
                ),
            )
            path_ids.append(
                key("logical-task", thread_key, raw_task, raw_task_generation)
            )
        path_ids.append(key("stack-lane", path_ids[-1], kind, depth))
        parts += (raw_task, raw_task_generation)
    lane_id = key("lane", perspective, *parts)
    return dict(
        id=lane_id,
        lane_id=lane_id,
        path=path,
        path_ids=path_ids,
        label=" / ".join(path),
        source_id=source_id,
        clock_id=clock_id,
        thread=thread if perspective != "cpu" else None,
        thread_generation=generation,
        logical_task_id=task if task != "unknown" else None,
        task_generation=task_generation if task != "unknown" else None,
        node=node,
        boot_id=location["boot_id"],
        node_lifetime=location["id"],
        node_lifetime_complete=location["complete"],
        kind=kind,
        depth=depth,
        fidelity=fidelity,
        identity_fidelity="producer-declared report identities"
        if any(field in data for field in ("source_node", "source_pid", "source_tid"))
        else "recorded source identity",
        **extra,
    )


def _activity_slots(rows):
    """Color overlap without asserting that containment implies parenthood."""
    import heapq
    from collections import defaultdict

    from .workspace_model_clocks import integer, key

    groups, result = defaultdict(list), []
    kinds = {
        "host",
        "cooperative",
        "gpu_kernel",
        "gpu_copy",
        "gpu_sync",
        "gpu_synchronization",
    }
    for row in rows:
        if row.get("kind") not in kinds:
            result.append(row)
            continue
        data = _data(row)
        depth = data.get("depth")
        if (
            row["kind"] in {"host", "cooperative"}
            and data.get("boundary_fidelity") == "matched-producer-boundaries"
            and type(depth) is int
            and 0 <= depth <= 65536
        ):
            result.append(dict(row, display_depth=depth, recorded_nesting=True))
            continue
        group = key(
            "activity-group",
            row.get("source_id"),
            row.get("clock_id"),
            row.get("thread"),
            data.get("source_node"),
            data.get("source_pid"),
            row.get("kind"),
            row.get("device", data.get("device")),
            row.get("context", data.get("context")),
            row.get("queue", data.get("queue")),
        )
        groups[group].append(row)
    for items in groups.values():
        active, available, prepared, count = [], [], [], 0
        for row in sorted(
            items,
            key=lambda row: (
                row.get("start_ns", 0),
                row.get("end_ns", 0),
                str(row.get("id")),
            ),
        ):
            start, end = (
                integer(row.get("start_ns"), "activity start"),
                integer(row.get("end_ns"), "activity end"),
            )
            if end < start:
                raise ValueError("Activity end precedes its start")
            while active and active[0][0] <= start:
                _, lane = heapq.heappop(active)
                heapq.heappush(available, lane)
            if available:
                lane = heapq.heappop(available)
            else:
                lane, count = count, count + 1
            heapq.heappush(active, (end, lane))
            prepared.append(
                dict(
                    row, activity_lane=lane, display_depth=lane, recorded_nesting=False
                )
            )
        result.extend(dict(row, overlapping_activity=count > 1) for row in prepared)
    return result


def project(query, perspective="execution", *, projection=None, max_lanes=4096):
    """Project one bounded query; no I/O, global alignment or inferred residency.

    CPU observations occur in the query's marker list. They become explicitly
    point-rendered view items with their original uncertainty bracket intact.
    Metrics must continue to distinguish them from execution intervals.
    """
    if projection is not None:
        perspective = projection
    if perspective not in {"execution", "node", "cpu", "gpu"}:
        raise ValueError("Unknown trace workspace perspective")
    if type(max_lanes) is not int or not 1 <= max_lanes <= MAX_LANES:
        raise ValueError("Invalid trace workspace lane budget")
    intervals, markers = _items(query)
    sources = _sources(query)
    candidates = list(intervals)
    from .workspace_model_cpu import execution_slices, sample_items

    extra_warnings, extra_truncated = [], False
    samples = sample_items(
        markers, limit=min(10000, max(1, MAX_ITEMS - len(candidates)))
    )
    if perspective in {"execution", "node", "cpu"}:
        candidates.extend(samples["intervals"])
        extra_warnings.extend(samples["warnings"])
        extra_truncated = samples["truncated"]
    if perspective in {"execution", "node"}:
        from .workspace_model_clocks import integer

        for row in markers:
            if row.get("kind") != "collective":
                continue
            stamp = integer(row.get("start_ns"), "collective observation time")
            if row.get("end_ns", stamp) != stamp:
                raise ValueError(
                    "A collective observation is a point, not an inferred call duration"
                )
            data = _data(row)
            candidates.append(
                dict(
                    row,
                    name=_label(data.get("operation"), "MPI collective")
                    + " · "
                    + _label(data.get("phase"), "observation"),
                    start_ns=stamp,
                    end_ns=stamp,
                    duration_ns=0,
                    self_ns=None,
                    point=True,
                    render="point",
                    duration_kind="Recorded MPI collective observation; no per-call duration is inferred",
                )
            )
    if perspective == "cpu":
        slices = execution_slices(intervals, sources, limit=5000)
        candidates.extend(slices["intervals"])
        extra_warnings.extend(slices["warnings"])
        extra_truncated = extra_truncated or slices["truncated"]
        candidates.extend(
            row for row in markers if row.get("kind") == "cpu_observation"
        )
    lanes, rows, hidden = {}, [], set()
    for row in _activity_slots(candidates):
        lane = _lane(row, sources, perspective)
        if lane is None:
            continue
        lane_id = lane["id"]
        if lane_id not in lanes and len(lanes) >= max_lanes:
            hidden.add(lane_id)
            continue
        previous = lanes.setdefault(lane_id, lane)
        if previous["kind"] != lane["kind"]:
            previous.update(
                kind="cpu",
                fidelity="Mixed placement points and scheduler slices; each item retains its own semantics",
            )
        item = dict(row, lane_id=lane_id)
        if row.get("kind") == "cpu_observation":
            item.update(
                point=True,
                render="point",
                duration_kind="query uncertainty; not CPU residency",
            )
        rows.append(item)
    warnings = list(query.get("warnings", [])) + extra_warnings
    if perspective == "cpu":
        warnings.append(
            "CPU placement points do not establish residency between observations; only recorded scheduler slices represent on-CPU intervals."
        )
    if perspective == "gpu" and not rows:
        warnings.append(
            "No device execution activities are available in this query; host calls do not establish GPU durations."
        )
    if hidden:
        warnings.append(
            f"The view lane budget hides {len(hidden)} lanes; narrow the query or expand the configured view budget."
        )
    return dict(
        query,
        projection=perspective,
        intervals=rows,
        lanes=sorted(
            lanes.values(),
            key=lambda row: (row["node"], row["clock_id"], row["label"], row["id"]),
        ),
        warnings=warnings,
        hidden_lanes=len(hidden),
        truncated=bool(query.get("truncated") or hidden or extra_truncated),
    )
