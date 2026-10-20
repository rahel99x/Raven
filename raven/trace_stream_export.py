"""Complete-prefix trace artifacts with streaming serialization and hard bounds."""

import heapq
import json
import os
import tempfile
from pathlib import Path

from .trace_stream import MAX_OUTPUT_BYTES, traverse, write_json_items


class _Writer:
    def __init__(self, stream):
        self.stream = stream
        self.bytes = 0

    def write(self, value):
        self.bytes += len(value.encode())
        if self.bytes > MAX_OUTPUT_BYTES:
            raise ValueError(
                "Trace output exceeds its 32 MiB artifact budget; no partial output was published"
            )
        self.stream.write(value)


def _json(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _threads(spool):
    result = {}
    for i, (thread,) in enumerate(
        spool.db.execute("SELECT thread FROM events GROUP BY thread ORDER BY min(seq)")
    ):
        if i >= 4096:
            raise ValueError(
                "Trace export exceeds its 4096-thread budget; raw evidence remains available"
            )
        result[thread] = i + 1
    return result


def _perfetto_items(spool):
    origin = spool.first["mono_ns"]
    pid = spool.snapshot["identity"]["pid"]
    threads = _threads(spool)

    def intervals():
        for first, last, category in spool.pairs():
            data = first["data"]
            args = (
                {
                    "first_seq": first["seq"],
                    "last_seq": last["seq"],
                    "exit": last["kind"],
                    "file": data.get("file"),
                    "line": data.get("line"),
                }
                if category == "python"
                else {**data, "first_seq": first["seq"], "last_seq": last["seq"]}
            )
            yield {
                "ph": "X",
                "cat": category,
                "name": data.get("function" if category == "python" else "name", "unknown"),
                "ts": (first["mono_ns"] - origin) / 1000,
                "dur": (last["mono_ns"] - first["mono_ns"]) / 1000,
                "pid": pid,
                "tid": threads[first["thread"]],
                "args": args,
            }

    def markers():
        query = """SELECT e.seq,e.mono,e.thread,e.kind,e.data FROM events e
                   LEFT JOIN pairs a ON a.opening=e.seq LEFT JOIN pairs b ON b.closing=e.seq
                   WHERE e.kind NOT IN ('call','resume','return','yield','unwind') OR (a.opening IS NULL AND b.closing IS NULL)
                   ORDER BY e.seq"""
        for seq, stamp, thread, kind, data in spool.db.execute(query):
            yield {
                "ph": "i",
                "s": "t",
                "cat": "recorded",
                "name": kind,
                "ts": (stamp - origin) / 1000,
                "pid": pid,
                "tid": threads[thread],
                "args": {**json.loads(data), "seq": seq},
            }

    yield from heapq.merge(
        intervals(), markers(), key=lambda event: (event["ts"], -event.get("dur", 0))
    )


def _speedscope(stream, spool):
    if spool.snapshot["covered"].get("adapter") not in {"python-profile", "python-sys-monitoring"}:
        raise ValueError(
            "Speedscope requires recorded Python frame events; use Perfetto for cooperative/native timelines"
        )
    frames, ids = [], {}

    def key(data):
        return data.get("function", "unknown"), data.get("file"), data.get("line")

    estimated = 0
    for first, last, category in spool.pairs():
        if category != "python":
            continue
        identity = key(first["data"])
        if identity not in ids:
            frame = {"name": identity[0]}
            if isinstance(identity[1], str):
                frame["file"] = identity[1]
            if type(identity[2]) is int and identity[2] > 0:
                frame["line"] = identity[2]
            estimated += len(_json(frame))
            if estimated > MAX_OUTPUT_BYTES:
                raise ValueError("Trace frame dictionary exceeds its artifact byte budget")
            ids[identity] = len(frames)
            frames.append(frame)
    if not frames:
        raise ValueError(
            "No matched Python frame intervals are available for an evented speedscope view"
        )
    origin = spool.first["mono_ns"]
    stream.write(
        '{"$schema":"https://www.speedscope.app/file-format-schema.json","name":"Raven recorded Python intervals","exporter":"Raven","activeProfileIndex":0,"shared":{"frames":'
    )
    stream.write(_json(frames))
    stream.write('},"profiles":[')
    first_profile = True
    for (thread,) in spool.db.execute(
        "SELECT a.thread FROM pairs p JOIN events a ON a.seq=p.opening WHERE p.category='python' GROUP BY a.thread ORDER BY min(a.seq)"
    ):
        query = """SELECT e.seq,e.mono,a.data,'O' FROM pairs p JOIN events e ON e.seq=p.opening JOIN events a ON a.seq=p.opening WHERE p.category='python' AND e.thread=?
                   UNION ALL SELECT e.seq,e.mono,a.data,'C' FROM pairs p JOIN events e ON e.seq=p.closing JOIN events a ON a.seq=p.opening WHERE p.category='python' AND e.thread=? ORDER BY 1"""
        bounds = spool.db.execute(
            "SELECT min(a.mono),max(b.mono) FROM pairs p JOIN events a ON a.seq=p.opening JOIN events b ON b.seq=p.closing WHERE p.category='python' AND a.thread=?",
            (thread,),
        ).fetchone()
        if not first_profile:
            stream.write(",")
        first_profile = False
        stream.write(
            _json(
                {
                    "type": "evented",
                    "name": "Thread " + thread,
                    "unit": "nanoseconds",
                    "startValue": bounds[0] - origin,
                    "endValue": bounds[1] - origin,
                }
            )[:-1]
            + ',"events":['
        )
        items = (
            {"type": kind, "at": stamp - origin, "frame": ids[key(json.loads(data))]}
            for seq, stamp, data, kind in spool.db.execute(query, (thread, thread))
        )
        write_json_items(stream, items)
        stream.write("]}")
    stream.write('],"raven":' + _json(spool.provenance()) + "}")


def _html_model(spool):
    from .log_text import display_text
    from .trace_cpu_timeline import build as cpu_build

    origin = spool.first["mono_ns"]
    intervals, markers, stacks, lanes = [], [], {}, {}
    estimate = 0

    def admit(row, target):
        nonlocal estimate
        estimate += len(_json(row))
        if estimate > MAX_OUTPUT_BYTES:
            raise ValueError(
                "HTML timeline exceeds its artifact byte budget; use a narrower recording"
            )
        target.append(row)

    for first, last, category in spool.pairs():
        thread, data = first["thread"], first["data"]
        if category == "python":
            stack = stacks.setdefault(thread, [])
            while stack and stack[-1] < first["seq"]:
                stack.pop()
            depth = len(stack)
            stack.append(last["seq"])
        else:
            occupied, available, next_lane = lanes.setdefault(thread, ([], [], [0]))
            while occupied and occupied[0][0] <= first["mono_ns"]:
                _, free = heapq.heappop(occupied)
                heapq.heappush(available, free)
            if available:
                depth = heapq.heappop(available)
            else:
                depth = next_lane[0]
                next_lane[0] += 1
            heapq.heappush(occupied, (last["mono_ns"], depth))
        admit(
            dict(
                id=category + ":" + str(first["seq"]),
                kind=category,
                thread=thread,
                depth=depth,
                name=display_text(
                    data.get("function" if category == "python" else "name", "unnamed")
                )
                if isinstance(
                    data.get("function" if category == "python" else "name", "unnamed"), str
                )
                else "unnamed",
                file=display_text(data.get("file", ""))
                if isinstance(data.get("file", ""), str)
                else "",
                line=data.get("line") if type(data.get("line")) is int else None,
                start_ns=first["mono_ns"] - origin,
                end_ns=last["mono_ns"] - origin,
                duration_ns=last["mono_ns"] - first["mono_ns"],
                first_seq=first["seq"],
                last_seq=last["seq"],
                exit=last["kind"],
            ),
            intervals,
        )
    query = """SELECT e.seq,e.mono,e.thread,e.kind,e.data FROM events e LEFT JOIN pairs a ON a.opening=e.seq LEFT JOIN pairs b ON b.closing=e.seq
               WHERE a.opening IS NULL AND b.closing IS NULL ORDER BY e.seq"""
    for seq, stamp, thread, kind, raw in spool.db.execute(query):
        data = json.loads(raw)
        name = data.get("function") or data.get("name") or kind
        admit(
            dict(
                id="event:" + str(seq),
                thread=thread,
                name=display_text(name) if isinstance(name, str) else kind,
                start_ns=stamp - origin,
                seq=seq,
                kind=kind,
            ),
            markers,
        )
    cpu, observations, cpu_lanes = None, [], {}
    partial_cpu = False
    page = []
    for event in spool.events():
        page.append(event)
        if len(page) == 512:
            cpu = cpu_build(dict(events=page, covered=spool.snapshot["covered"]), origin)
            partial_cpu = partial_cpu or cpu["status"] == "partial"
            for item in cpu["observations"]:
                admit(item, observations)
            cpu_lanes.update((lane["key"], lane) for lane in cpu["lanes"])
            page.clear()
    if page:
        cpu = cpu_build(dict(events=page, covered=spool.snapshot["covered"]), origin)
        partial_cpu = partial_cpu or cpu["status"] == "partial"
        for item in cpu["observations"]:
            admit(item, observations)
        cpu_lanes.update((lane["key"], lane) for lane in cpu["lanes"])
    cpu = cpu or cpu_build(dict(events=[], covered=spool.snapshot["covered"]), origin)
    observations.sort(key=lambda item: (item["at_ns"], item["seq"]))
    cpu = dict(
        cpu,
        status="observed" if observations else cpu["status"],
        observations=observations,
        lanes=sorted(
            cpu_lanes.values(),
            key=lambda lane: tuple(
                v if v is not None else 1 << 31
                for v in (lane["socket_id"], lane["core_id"], lane["logical_cpu"])
            ),
        ),
    )
    if observations:
        cpu["warnings"] = [
            "Each point locates one emitting thread during its query; execution between observations is unknown."
        ]
        if partial_cpu:
            cpu["status"] = "partial"
            cpu["warnings"].append(
                "Some malformed CPU observations were omitted; their placement remains unknown."
            )
    return dict(
        schema="raven.trace-timeline/v1",
        identity=spool.snapshot["identity"],
        coverage=spool.snapshot["covered"],
        status=spool.snapshot["status"],
        warnings=spool.snapshot["warnings"]
        + ["All verified committed events were traversed; this is a static artifact."],
        origin_ns=origin,
        start_ns=0,
        end_ns=max([spool.last["mono_ns"] - origin] + [item["end_ns"] for item in observations]),
        cpu=cpu,
        intervals=intervals,
        threads=list(_threads(spool)),
        markers=markers,
        unpaired_frame_events=spool.omitted,
        unpaired_span_events=spool.omitted_spans,
        retained_events=spool.last["seq"],
        read_seq=spool.snapshot["read_seq"],
        committed_seq=spool.snapshot["committed_seq"],
        evicted_events=0,
        sha256=spool.snapshot["sha256"],
    )


def export(path, output, *, format="perfetto", managed=False):
    if format not in {"perfetto", "speedscope", "html"}:
        raise ValueError("Choose perfetto, speedscope or html")
    with traverse(path, managed=managed) as spool:
        if format == "html":
            from .trace_browser import export as browser_export

            snapshot = dict(spool.snapshot, timeline=_html_model(spool))
            return browser_export(snapshot, output, max_bytes=MAX_OUTPUT_BYTES)
        output = Path(output).expanduser()
        fd, temporary = tempfile.mkstemp(prefix=".raven-trace-", dir=output.parent)
        count = 0
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                writer = _Writer(handle)
                if format == "perfetto":
                    writer.write('{"traceEvents":[')
                    write_json_items(writer, _perfetto_items(spool))
                    writer.write(
                        '],"displayTimeUnit":"ms","raven":' + _json(spool.provenance()) + "}"
                    )
                else:
                    _speedscope(writer, spool)
                writer.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                count = writer.bytes
            os.link(temporary, output)
        finally:
            os.unlink(temporary)
        return {
            "path": str(output),
            "format": format,
            "read_seq": spool.snapshot["read_seq"],
            "source_status": spool.snapshot["status"],
            "bytes": count,
            "warnings": spool.snapshot["warnings"],
        }


def inspect(path, *, json_output=False, output=None, managed=False):
    import sys

    output = output or sys.stdout
    with traverse(path, managed=managed) as spool:
        if not json_output:
            output.write("\n".join(spool.snapshot["rows"] + spool.snapshot["warnings"]) + "\n")
            return
        # Validate and finish the private artifact before emitting any stdout.
        # A late integrity error or byte limit cannot masquerade as success.
        temporary = Path(spool.folder.name) / "inspect.json"
        with temporary.open("w", encoding="utf-8") as handle:
            writer = _Writer(handle)
            metadata = {key: value for key, value in spool.snapshot.items() if key != "events"}
            metadata.update(
                evicted=0, window_start=spool.first["seq"], window_end=spool.last["seq"]
            )
            writer.write(_json(metadata)[:-1] + ',"events":[')
            write_json_items(writer, spool.events())
            writer.write("]}\n")
        with temporary.open("r", encoding="utf-8") as handle:
            while chunk := handle.read(65536):
                output.write(chunk)
