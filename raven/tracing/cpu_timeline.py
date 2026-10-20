"""Typed CPU-placement observations for a verified single-process trace.

An observation locates the emitting thread during one bracketed OS query. It
does not turn the surrounding function interval into CPU residency evidence.
"""

from __future__ import annotations

from ..job_logs.text import display_text


SCHEMA = "raven.cpu-placement/v1"
_FIELDS = {
    "schema",
    "logical_cpu",
    "socket_id",
    "core_id",
    "sample_start_ns",
    "sample_end_ns",
    "source",
}


def _cpu_id(value, *, optional=False):
    return optional and value is None or type(value) is int and 0 <= value < 1 << 31


def build(snapshot, origin_ns):
    events = snapshot.get("events", ())
    if len(events) > 20000:
        raise ValueError("CPU placement exceeds the 20,000-event retained window")
    observations, lanes, rejected = [], {}, 0
    for event in events:
        data = event["data"]
        if "cpu_observation" not in data:
            continue
        value = data["cpu_observation"]
        if (
            not isinstance(value, dict)
            or not _FIELDS <= set(value)
            or value.get("schema") != SCHEMA
            or value.get("source") != "linux-sched_getcpu"
            or not _cpu_id(value.get("logical_cpu"))
            or not _cpu_id(value.get("socket_id"), optional=True)
            or not _cpu_id(value.get("core_id"), optional=True)
            or any(
                type(value.get(key)) is not int or not 0 <= value[key] < 1 << 64
                for key in ("sample_start_ns", "sample_end_ns")
            )
            or not event["mono_ns"]
            <= value["sample_start_ns"]
            <= value["sample_end_ns"]
        ):
            rejected += 1
            continue
        socket, core, cpu = value["socket_id"], value["core_id"], value["logical_cpu"]
        lane_key = f"{socket}/{core}/{cpu}"
        if lane_key not in lanes:
            label = f"Socket {socket if socket is not None else '?'} / core {core if core is not None else '?'} / logical CPU {cpu}"
            lanes[lane_key] = dict(
                key=lane_key,
                label=label,
                socket_id=socket,
                core_id=core,
                logical_cpu=cpu,
            )
        start, end = (
            value["sample_start_ns"] - origin_ns,
            value["sample_end_ns"] - origin_ns,
        )
        name = data.get("function") or data.get("name") or event["kind"]
        observations.append(
            dict(
                id="cpu:" + str(event["seq"]),
                thread=event["thread"],
                start_ns=start,
                end_ns=end,
                at_ns=(start + end) // 2,
                name=display_text(
                    name[:8192] if isinstance(name, str) else event["kind"]
                ),
                seq=event["seq"],
                logical_cpu=cpu,
                socket_id=socket,
                core_id=core,
                source=value["source"],
                lane_key=lane_key,
            )
        )
    coverage = snapshot.get("covered", {}).get("cpu_placement", {})
    status = "observed" if observations else "not-recorded"
    warnings = [
        "Only observed CPUs are shown. Each point locates one emitting thread during its query; execution between observations is unknown."
    ]
    if isinstance(coverage, dict) and not observations:
        if coverage.get("status") in {"unavailable", "unsupported"}:
            status = "unavailable"
        reason = coverage.get("reason")
        if isinstance(reason, str) and reason:
            warnings.append(display_text(reason[:1024]))
    if rejected:
        status = "partial" if observations else "unavailable"
        warnings.append(
            f"Rejected {rejected} malformed CPU observations; no placement inferred for those events."
        )
    if any(row["socket_id"] is None or row["core_id"] is None for row in observations):
        warnings.append(
            "Physical topology is unavailable for some logical CPUs; '?' means unknown, not core zero."
        )
    if not observations:
        warnings.append(
            "This retained window has no usable CPU-placement observations. Enable --trace-cpu-placement for a future supported Python launch."
        )

    def order(lane):
        return tuple(
            value if value is not None else 1 << 31
            for value in (lane["socket_id"], lane["core_id"], lane["logical_cpu"])
        )

    # Explicit query brackets can overlap. Index by their observed midpoint,
    # retaining the original event sequence rather than guessing event order.
    observations.sort(key=lambda row: (row["at_ns"], row["seq"]))
    return dict(
        status=status,
        observations=observations,
        lanes=sorted(lanes.values(), key=order),
        warnings=warnings,
    )
