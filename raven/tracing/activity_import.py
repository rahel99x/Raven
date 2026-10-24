"""Bounded, data-only import of producer-reported CPU/GPU trace activities.

Chrome Trace Event JSON is also an interchange format accepted by Perfetto.
This importer does not decode Perfetto protobuf, attach to a device, or verify
that an exporting profiler observed every activity. Importer process identity
and producer-declared process/device identity are intentionally kept separate.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from copy import deepcopy

from . import protocol


SCHEMA = "raven.activity-import/v1"
MAX_EVENTS = 200_000
MAX_VALUE_CHARS = 1 << 20
MAX_METADATA_VALUES = 64
MAX_OPEN_BOUNDARIES = 65_536
_MAX_TIME = (1 << 63) - 1
_GPU_KERNEL = frozenset(
    {"kernel", "gpu_kernel", "cuda_kernel", "hip_kernel", "xpu_kernel", "metal_kernel"}
)
_GPU_COPY = frozenset(
    {
        "gpu_memcpy",
        "gpu_memset",
        "cuda_memcpy",
        "hip_memcpy",
        "gpu_copy",
        "gpu_transfer",
    }
)
_HOST = frozenset(
    {
        "cpu_op",
        "python_function",
        "cuda_runtime",
        "cuda_driver",
        "hip_runtime",
        "xpu_runtime",
        "user_annotation",
        "host",
        "cpu",
    }
)


def _number(token):
    if len(token) > 128:
        raise ValueError("Activity number exceeds 128 characters")
    value = Decimal(token)
    if not value.is_finite():
        raise ValueError("Activity numbers must be finite")
    if value and not -1000 <= value.adjusted() <= 1000:
        raise ValueError("Activity numeric exponent exceeds its bounded range")
    return value


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate activity JSON key")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("Activity numbers must be finite")


class _Document:
    """Decode one bounded event at a time, not a whole report object graph."""

    def __init__(self, text):
        self.text, self.position = text, 0
        self.decoder = json.JSONDecoder(
            parse_float=_number,
            parse_int=_number,
            parse_constant=_constant,
            object_pairs_hook=_pairs,
        )
        self.metadata = {}

    def _space(self):
        while self.position < len(self.text) and self.text[self.position] in " \t\r\n":
            self.position += 1

    def _take(self, character):
        self._space()
        if self.position >= len(self.text) or self.text[self.position] != character:
            raise ValueError("Malformed activity JSON document")
        self.position += 1

    def _value(self):
        self._space()
        start, cursor, quoted, escaped, stack = (
            self.position,
            self.position,
            False,
            False,
            [],
        )
        if start == len(self.text):
            raise ValueError("Incomplete activity JSON value")
        while cursor < len(self.text):
            character = self.text[cursor]
            if cursor - start > MAX_VALUE_CHARS:
                raise ValueError("One activity JSON value exceeds 1 MiB")
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
            elif character in "[{":
                stack.append(character)
                if len(stack) > 32:
                    raise ValueError("Activity JSON nesting exceeds 32")
            elif character in "]}":
                if not stack:
                    break
                opening = stack.pop()
                if (opening, character) not in (("[", "]"), ("{", "}")):
                    raise ValueError("Mismatched activity JSON nesting")
            elif not stack and character in ",:\r\n\t ":
                break
            cursor += 1
            if not quoted and not stack and self.text[start] in '[{"':
                break
        if quoted or stack or cursor == start:
            raise ValueError("Incomplete activity JSON value")
        fragment = self.text[start:cursor]
        try:
            value = self.decoder.decode(fragment)
        except (json.JSONDecodeError, InvalidOperation, RecursionError) as exc:
            raise ValueError("Invalid activity JSON value") from exc
        self.position = cursor
        return value, fragment

    def _events(self):
        self._take("[")
        self._space()
        if self.position < len(self.text) and self.text[self.position] == "]":
            self.position += 1
            return
        count = 0
        while True:
            value, raw = self._value()
            count += 1
            if count > MAX_EVENTS:
                raise ValueError("Activity report exceeds 200,000 events")
            if not isinstance(value, dict):
                raise ValueError("Activity traceEvents entries must be objects")
            yield value, raw
            self._space()
            if self.position < len(self.text) and self.text[self.position] == "]":
                self.position += 1
                return
            self._take(",")

    def events(self):
        self._space()
        if self.position < len(self.text) and self.text[self.position] == "[":
            yield from self._events()
        else:
            self._take("{")
            seen, found = set(), False
            while True:
                self._space()
                if self.position < len(self.text) and self.text[self.position] == "}":
                    self.position += 1
                    break
                key, _ = self._value()
                if (
                    not isinstance(key, str)
                    or key in seen
                    or len(seen) >= MAX_METADATA_VALUES
                ):
                    raise ValueError(
                        "Invalid, duplicate or excessive activity document keys"
                    )
                seen.add(key)
                self._take(":")
                if key == "traceEvents":
                    found = True
                    yield from self._events()
                else:
                    self.metadata[key], _ = self._value()
                self._space()
                if self.position < len(self.text) and self.text[self.position] == "}":
                    self.position += 1
                    break
                self._take(",")
            if not found:
                raise ValueError("Expected a Chrome traceEvents array")
        self._space()
        if self.position != len(self.text):
            raise ValueError("Trailing activity JSON content")


def _label(value, name, *, maximum=1024, default="unknown"):
    if value is None:
        return default
    if isinstance(value, Decimal):
        if abs(value) > _MAX_TIME:
            raise ValueError(name + " exceeds the supported identifier range")
        value = str(int(value)) if value == value.to_integral_value() else str(value)
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return protocol.text(value, name, maximum)


def _integer(value, name, *, maximum=_MAX_TIME):
    if isinstance(value, bool):
        raise ValueError(name + " must be an integer")
    if isinstance(value, str):
        value = _number(value)
    if isinstance(value, Decimal):
        if not 0 <= value <= maximum or value != value.to_integral_value():
            raise ValueError(name + " must be an integer")
        value = int(value)
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(name + " is outside its nonnegative integer range")
    return value


def _time(value, name):
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise ValueError(name + " must contain microseconds")
    try:
        original = value if isinstance(value, Decimal) else _number(str(value))
        if not original.is_finite() or not 0 <= original <= Decimal(_MAX_TIME) / 1000:
            raise ValueError(name + " exceeds nonnegative signed-64-bit nanoseconds")
        with localcontext() as context:
            context.prec = 160
            nanos = original * 1000
            rounded = int(nanos.to_integral_value(rounding=ROUND_HALF_EVEN))
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError("Invalid activity timestamp") from exc
    if not original.is_finite() or original < 0 or not 0 <= rounded <= _MAX_TIME:
        raise ValueError(name + " exceeds nonnegative signed-64-bit nanoseconds")
    return original, rounded, nanos != rounded


def _first(values, *names):
    return next((values[key] for key in names if key in values), None)


def _json(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    )


def _decimal_order(left, right):
    left, right = Decimal(left), Decimal(right)
    return (left > right) - (left < right)


def _origin(event, source_nodes, node):
    args = event.get("args", {})
    if not isinstance(args, dict):
        raise ValueError("Activity args must be an object")
    pid_value, tid_value = event.get("pid"), event.get("tid")
    # Chrome global instants legitimately omit process/thread identity or use
    # an empty string. Preserve unknown rather than borrowing the importer PID.
    pid = _label(
        None if pid_value == "" else pid_value, "activity process", maximum=128
    )
    tid = _label(None if tid_value == "" else tid_value, "activity thread", maximum=128)
    actual_node = _label(
        _first(args, "node", "hostname") or source_nodes.get(pid) or node,
        "activity node",
        maximum=256,
    )
    return args, pid, tid, actual_node


def _device(args, properties, node, pid, digest):
    ordinal = _first(args, "device", "device id", "Device Id", "device_id", "gpu")
    ordinal = (
        _integer(ordinal, "device ordinal", maximum=1_000_000)
        if ordinal is not None
        else None
    )
    record = properties.get(ordinal, {})
    uuid = _first(args, "device_uuid", "Device UUID", "uuid") or record.get("uuid")
    partition = _first(args, "partition", "mig_uuid", "device_partition") or record.get(
        "partition"
    )
    vendor = _first(args, "vendor", "device_vendor") or record.get("vendor")
    name = _first(args, "device_name", "Device Name") or record.get("name")
    physical = _label(uuid, "device UUID", maximum=256) if uuid is not None else None
    partition = (
        _label(partition, "device partition", maximum=256)
        if partition is not None
        else None
    )
    vendor = (
        _label(vendor, "device vendor", maximum=128)
        if vendor is not None
        else "unknown"
    )
    # Ordinals are process-local. Even a UUID cannot establish a known node
    # when the producer omitted it, so retain that ambiguity in the identity.
    identity = (
        [node, physical, partition]
        if physical and node != "unknown"
        else [digest, node, pid, ordinal, physical, partition]
    )
    return {
        "id": "device:" + hashlib.sha256(_json(identity).encode()).hexdigest()[:32],
        "node": node,
        "uuid": physical,
        "partition": partition,
        "ordinal": ordinal,
        "vendor": vendor,
        "name": _label(name, "device name", maximum=512),
        "identity_scope": "node-uuid-partition"
        if physical and node != "unknown"
        else "producer-report-process",
    }


def _properties(metadata):
    records = metadata.get("deviceProperties", [])
    if not isinstance(records, list) or len(records) > 4096:
        raise ValueError("Invalid bounded deviceProperties metadata")
    result = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Device metadata must be an object")
        ordinal = _integer(
            _first(record, "id", "device", "ordinal"),
            "device property ordinal",
            maximum=1_000_000,
        )
        if ordinal in result:
            raise ValueError("Duplicate device property ordinal")
        result[ordinal] = {
            key: record[key]
            for key in ("uuid", "vendor", "partition", "name")
            if key in record
        }
    return result


def _kind(category, args):
    categories = {part.strip().lower() for part in category.split(",")}
    if categories & _GPU_KERNEL:
        return "gpu_kernel"
    if categories & _GPU_COPY:
        return "gpu_copy"
    if "scheduling" in categories:
        _integer(args.get("cpu"), "scheduling CPU", maximum=1_000_000)
        if args.get("state") not in {"running", "runnable", "blocked"}:
            raise ValueError("Scheduling activities require a declared state")
        return "scheduling"
    return "host" if categories & _HOST else "cooperative"


def _flow_identity(event, pid, digest):
    scope = _label(event.get("scope"), "flow scope", maximum=128, default="default")
    identity = event.get("id2")
    if identity is not None:
        if (
            not isinstance(identity, dict)
            or len(identity) != 1
            or not set(identity) <= {"local", "global"}
        ):
            raise ValueError("Unsupported Chrome flow id2")
        kind, value = next(iter(identity.items()))
    else:
        # Legacy flow IDs (unlike legacy async event IDs) are global within
        # their category/scope. Explicit id2.local is process-scoped.
        kind, value = "global", event.get("id")
    if value is None:
        raise ValueError("Flow event requires an explicit id or id2")
    value = _label(value, "flow ID", maximum=256)
    category = _label(event.get("cat"), "flow category", maximum=1024, default="")
    key = [digest, category, scope, kind, pid if kind == "local" else "", value]
    return "flow:" + hashlib.sha256(_json(key).encode()).hexdigest()[:32]


class _ReportRecorder:
    """Use Recorder's normal integrity writer with a report-clock close stamp."""

    def __new__(cls, *args, end_ns, **kwargs):
        from .capture import Recorder

        class HistoricalRecorder(Recorder):
            def emit(
                self, kind, data=None, *, thread=None, mono_ns=None, _observation=None
            ):
                return super().emit(
                    kind,
                    data,
                    thread=thread,
                    mono_ns=end_ns if mono_ns is None else mono_ns,
                    _observation=_observation,
                )

        return HistoricalRecorder(*args, **kwargs)


def import_activity(
    input_path,
    *,
    project,
    job_id,
    attempt,
    cluster="local",
    node=None,
    max_bytes=32 << 20,
    max_output_bytes=None,
    device_metadata=None,
    clock=None,
):
    """Import a complete bounded JSON report as registered verified journals.

    Returns a receipt containing ``manifest``/``path``, provenance and counts.
    Unsupported phases and unmatched boundaries are counted, not silently
    described as complete process/device capture. Original report IDs are
    producer declarations; they never authorize attachment to live processes.
    """
    if type(max_bytes) is not int or not 4096 <= max_bytes <= 128 << 20:
        raise ValueError("Activity input budget must be 4 KiB through 128 MiB")
    if max_output_bytes is None:
        max_output_bytes = min(1 << 30, max_bytes * 8)
    if type(max_output_bytes) is not int or not 4096 <= max_output_bytes <= 1 << 30:
        raise ValueError("Activity journal budget must be 4 KiB through 1 GiB")
    overrides = (
        _properties({"deviceProperties": device_metadata})
        if device_metadata is not None
        else {}
    )
    if isinstance(job_id, bool) or not isinstance(job_id, (str, int)):
        raise ValueError("An explicit activity job ID is required")
    job_id = protocol.text(str(job_id), "job ID", 256)
    attempt = protocol.text(attempt, "attempt", 256)
    cluster = protocol.text(cluster, "cluster", 256)
    if node is not None:
        node = protocol.text(node, "source node", 256)
    descriptor = protocol.regular_open(input_path)
    with os.fdopen(descriptor, "rb") as incoming:
        raw = incoming.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("Activity report exceeds its input byte budget")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        document = _Document(raw.decode("utf-8"))
    except UnicodeError as exc:
        raise ValueError("Activity report must be UTF-8 JSON") from exc
    del raw
    if clock is not None:
        # Only an explicitly owning runtime adapter can declare a common
        # lifetime across separate reports. Bare imports remain SHA-isolated.
        if not isinstance(clock, dict) or clock.get("kind") != "producer-session":
            raise ValueError(
                "An activity clock override requires a declared producer session"
            )
        from .workspace_model import source_clock

        source_clock(
            {
                "stream": "activity-import",
                "node": "unknown",
                "boot_id": "unknown",
                "cluster": cluster,
            },
            clock,
        )
        clock = deepcopy(clock)
    clock_id = clock["id"] if clock else "chrome:" + digest[:32]
    counts, ignored, warnings = Counter(), Counter(), []
    with tempfile.TemporaryDirectory(prefix="raven-activity-") as folder:
        spool = Path(folder) / "activities.sqlite"
        database = sqlite3.connect(spool)
        spool.chmod(0o600)
        try:
            database.execute("PRAGMA journal_mode=OFF")
            database.execute("PRAGMA temp_store=FILE")
            database.execute("PRAGMA cache_size=-2048")
            database.create_collation("activity_decimal", _decimal_order)
            database.execute(
                f"PRAGMA max_page_count={(max_bytes * 4 + (4 << 20)) // 4096}"
            )
            database.execute(
                "CREATE TABLE raw_events(ordinal INTEGER PRIMARY KEY, stamp INTEGER, precise TEXT, payload TEXT)"
            )
            database.execute(
                "CREATE TABLE activities(ordinal INTEGER PRIMARY KEY, stamp INTEGER, payload TEXT, kind TEXT, thread TEXT)"
            )
            nodes, process_names, thread_names = {}, {}, {}
            for ordinal, (event, fragment) in enumerate(document.events()):
                phase = event.get("ph")
                if (
                    not isinstance(phase, str)
                    or not phase.isprintable()
                    or not 1 <= len(phase) <= 4
                ):
                    raise ValueError("Activity event requires a bounded phase")
                if phase == "M":
                    args = event.get("args", {})
                    if not isinstance(args, dict):
                        raise ValueError("Activity metadata args must be an object")
                    if event.get("name") in {"raven_node", "node_name"}:
                        pid = _label(event.get("pid"), "metadata process", maximum=128)
                        nodes[pid] = _label(
                            _first(args, "node", "name"), "metadata node", maximum=256
                        )
                    elif event.get("name") == "process_name":
                        process_names[
                            _label(event.get("pid"), "metadata process", maximum=128)
                        ] = _label(args.get("name"), "process name", maximum=512)
                    elif event.get("name") == "thread_name":
                        thread_names[
                            (
                                _label(
                                    event.get("pid"), "metadata process", maximum=128
                                ),
                                _label(
                                    event.get("tid"), "metadata thread", maximum=128
                                ),
                            )
                        ] = _label(args.get("name"), "thread name", maximum=512)
                    counts["metadata"] += 1
                    continue
                if phase not in {"X", "B", "E", "C", "s", "t", "f", "i", "I"}:
                    ignored[
                        phase
                        if phase in ignored or len(ignored) < 64
                        else "other_unsupported_phase"
                    ] += 1
                    continue
                _, stamp, _ = _time(event.get("ts"), "activity timestamp")
                database.execute(
                    "INSERT INTO raw_events VALUES(?,?,?,?)",
                    (ordinal, stamp, str(event["ts"]), fragment),
                )
            properties = _properties(document.metadata)
            producer_epoch = document.metadata.get("baseTimeNanoseconds")
            if producer_epoch is not None:
                producer_epoch = _integer(producer_epoch, "producer clock epoch")
            if clock is not None:
                if (
                    "producer_epoch_ns" in clock
                    and clock["producer_epoch_ns"] != producer_epoch
                ):
                    raise ValueError(
                        "Producer report clock epoch changed within one capture session"
                    )
                # This is an identity check, not an absolute-time calibration.
                clock["producer_epoch_ns"] = producer_epoch
            for ordinal, details in overrides.items():
                current = properties.setdefault(ordinal, {})
                for key, value in details.items():
                    if key == "name" and current.get(key) is not None:
                        continue
                    if (
                        key == "vendor"
                        and str(current.get(key, "unknown")).casefold()
                        == str(value).casefold()
                    ):
                        continue
                    if current.get(key) is not None and current[key] != value:
                        raise ValueError(
                            "Collector device metadata conflicts with report metadata"
                        )
                    current[key] = value
            stacks, opened, last_end = defaultdict(list), 0, 0
            for ordinal, stamp, fragment in database.execute(
                "SELECT ordinal,stamp,payload FROM raw_events ORDER BY stamp,precise COLLATE activity_decimal,ordinal"
            ):
                event = document.decoder.decode(fragment)
                args, pid, tid, source_node = _origin(event, nodes, node)
                phase = event["ph"]
                name = _label(event.get("name"), "activity name", maximum=8192)
                category = _label(
                    event.get("cat"), "activity category", maximum=1024, default=""
                )
                thread = (
                    "producer:"
                    + hashlib.sha256(
                        _json([source_node, pid, tid]).encode()
                    ).hexdigest()[:32]
                )
                base = {
                    "name": name,
                    "event_id": "chrome:" + str(ordinal),
                    "clock_id": clock_id,
                    "source_pid": pid,
                    "source_tid": tid,
                    "source_node": source_node,
                    "source_process_name": process_names.get(pid, "unknown"),
                    "source_thread_name": thread_names.get((pid, tid), "unknown"),
                    "category": category,
                    "producer_scope": _label(
                        event.get("s"), "event scope", maximum=128
                    ),
                    "source_ts_us": str(event["ts"]),
                    "provenance": {
                        "format": "chrome-trace-event-json",
                        "input_sha256": digest,
                        "identity": "producer-declared; not verified live process ownership",
                    },
                }
                if phase == "B":
                    if opened >= MAX_OPEN_BOUNDARIES or len(stacks[thread]) >= 1024:
                        raise ValueError("Activity open-boundary budget exceeded")
                    stacks[thread].append((ordinal, stamp, event, base))
                    opened += 1
                    continue
                if phase == "E":
                    if not stacks[thread]:
                        ignored["unmatched_end"] += 1
                        continue
                    ending = event
                    first_ordinal, start, opening, base = stacks[thread].pop()
                    opened -= 1
                    ordinal, event = first_ordinal, opening
                    args = event.get("args") or {}
                    name, category = base["name"], base["category"]
                    end, stamp = stamp, start
                    base["boundary_fidelity"] = "matched-producer-boundaries"
                    base["depth"] = len(stacks[thread])
                    base["source_end_ts_us"] = str(ending["ts"])
                    _, _, first_rounded = _time(opening["ts"], "activity begin")
                    _, _, last_rounded = _time(ending["ts"], "activity end")
                    base["timestamp_rounding_bound_ns"] = (
                        1 if first_rounded or last_rounded else 0
                    )
                    base["measurement_uncertainty_ns"] = None
                elif phase == "X":
                    original, stamp, quantized_start = _time(
                        event["ts"], "activity timestamp"
                    )
                    duration, _, quantized_duration = _time(
                        event.get("dur"), "activity duration"
                    )
                    with localcontext() as context:
                        context.prec = 160
                        _, end, quantized_end = _time(
                            original + duration, "activity end"
                        )
                    base.update(
                        source_duration_us=str(event["dur"]),
                        timestamp_rounding_bound_ns=1
                        if quantized_start or quantized_duration or quantized_end
                        else 0,
                        measurement_uncertainty_ns=None,
                        boundary_fidelity="recorded-complete-activity",
                    )
                elif phase in {"s", "t", "f"}:
                    base.update(
                        flow_id=_flow_identity(event, pid, digest),
                        flow_phase={"s": "start", "t": "step", "f": "end"}[phase],
                        correlation_scope=digest,
                        source_time_ns=stamp,
                    )
                    _store(database, ordinal, stamp, "flow", thread, base)
                    counts["flows"] += 1
                    last_end = max(last_end, stamp)
                    continue
                elif phase == "C":
                    values = {}
                    for key, value in args.items():
                        if not isinstance(value, (Decimal, int)) or isinstance(
                            value, bool
                        ):
                            continue
                        key = _label(key, "counter name", maximum=128)
                        if len(values) >= 64:
                            raise ValueError("Activity counter exceeds 64 fields")
                        # Decimal text preserves producer precision and avoids
                        # silently promoting unitless counters to CPU/GPU rates.
                        values[key] = str(value)
                    base.update(values=values, units="producer-defined")
                    _store(database, ordinal, stamp, "counter", thread, base)
                    counts["counters"] += 1
                    last_end = max(last_end, stamp)
                    continue
                else:
                    _store(database, ordinal, stamp, "activity_marker", thread, base)
                    counts["markers"] += 1
                    last_end = max(last_end, stamp)
                    continue
                activity_kind = _kind(category, args)
                base.update(
                    activity_kind=activity_kind,
                    start_ns=stamp,
                    end_ns=end,
                    duration_ns=end - stamp,
                )
                if activity_kind.startswith("gpu_"):
                    base["device"] = _device(args, properties, source_node, pid, digest)
                    size = _first(args, "bytes", "Bytes", "transfer_bytes")
                    if size is not None:
                        base["transfer_bytes"] = _integer(size, "activity byte count")
                    launch = {}
                    for dimension in ("grid", "block"):
                        if dimension in args:
                            values = args[dimension]
                            if (
                                not isinstance(values, list)
                                or not 1 <= len(values) <= 3
                            ):
                                raise ValueError(
                                    "Kernel launch dimensions must contain 1 through 3 integers"
                                )
                            launch[dimension] = [
                                _integer(
                                    value, "kernel launch dimension", maximum=1 << 31
                                )
                                for value in values
                            ]
                    if launch:
                        base["launch_config"] = launch
                if activity_kind == "scheduling":
                    base.update(
                        cpu=_integer(args["cpu"], "scheduling CPU", maximum=1_000_000),
                        state=args["state"],
                    )
                    for key in ("socket", "core", "numa"):
                        if key in args:
                            base[key] = _integer(
                                args[key], "scheduling " + key, maximum=1_000_000
                            )
                for field, names in {
                    "context": ("context", "context_id"),
                    "queue": ("stream", "stream_id", "queue"),
                    "correlation_id": (
                        "correlation",
                        "correlation_id",
                        "correlation id",
                    ),
                    "external_id": ("External id", "external_id"),
                }.items():
                    value = _first(args, *names)
                    if value is not None:
                        base[field] = _label(value, field, maximum=256)
                _store(database, ordinal, stamp, "activity", thread, base)
                counts[activity_kind] += 1
                last_end = max(last_end, end)
            if opened:
                ignored["unmatched_begin"] += opened
            if ignored:
                warnings.append(
                    "Unsupported phases or unmatched boundaries remain unobserved: "
                    + _json(dict(ignored))
                )
            first = database.execute("SELECT MIN(stamp) FROM activities").fetchone()[0]
            if first is None:
                raise ValueError(
                    "Activity report contains no supported activities, counters or markers"
                )
            coverage = {
                "adapter": "chrome-trace-event-json-import",
                "scope": "Producer-reported activities; not automatic hardware capture or complete device stacks",
                "input_sha256": digest,
                "clock": clock
                or {
                    "schema": "raven.trace-clock/v1",
                    "kind": "producer-report",
                    "id": clock_id,
                    "report_sha256": digest,
                },
                "identity_scope": "Journal owner is importer; source PID/thread/node/device fields are producer declarations",
                "device_metadata": "producer report plus explicit collector metadata"
                if overrides
                else "producer report only",
                "limitations": warnings
                + [
                    "GPU host calls are not kernel timings; imported reports do not qualify live capture or hardware counters."
                ],
            }
            stream_id = (
                "activity_"
                + hashlib.sha256(
                    _json([digest, job_id, attempt, cluster, node]).encode()
                ).hexdigest()[:32]
            )
            recorder = _ReportRecorder(
                project,
                job_id=job_id,
                attempt=attempt,
                cluster=cluster,
                coverage=coverage,
                max_bytes=max_output_bytes,
                initial_mono_ns=first,
                end_ns=last_end,
                background_flush=False,
                journal_format="blocks",
                stream_id=stream_id,
                register=False,
            )
            try:
                for stamp, payload, kind, thread in database.execute(
                    "SELECT stamp,payload,kind,thread FROM activities ORDER BY stamp,ordinal"
                ):
                    if not recorder.emit(
                        kind, json.loads(payload), thread=thread, mono_ns=stamp
                    ):
                        raise ValueError(
                            "Activity import exceeded its journal storage budget"
                        )
            except BaseException:
                recorder._fail(
                    "ERROR",
                    "Activity import did not finish; the partial journal is not registered",
                )
                raise
            finally:
                recorder.close()
            from .capture import register_trace

            registration_error = ""
            try:
                register_trace(project, recorder.path)
            except (OSError, ValueError) as error:
                # Retain the complete private journal for explicit attachment;
                # registry contention must not demand re-import or destroy it.
                registration_error = type(error).__name__ + ": " + str(error)[:1500]
                warnings.append(
                    "Imported trace registration failed: " + registration_error
                )
            return {
                "schema": SCHEMA,
                "manifest": str(recorder.path),
                "path": str(recorder.path),
                "input_sha256": digest,
                "format": "chrome-trace-event-json",
                "counts": dict(counts),
                "journal_bytes": recorder.bytes,
                "registered": not registration_error,
                "registration_error": registration_error,
                "registration_argv": [
                    "raven",
                    "trace",
                    "register",
                    "--project",
                    str(project),
                    str(recorder.path),
                ]
                if registration_error
                else [],
                "ignored": dict(ignored),
                "warnings": warnings,
                "clock": coverage["clock"],
                "producer_epoch_ns": producer_epoch,
                "capture_qualified": False,
                "scope": "Imported producer-reported evidence; live process membership and hardware capture are not established",
            }
        finally:
            database.close()


def _store(database, ordinal, stamp, kind, thread, data):
    protocol.validate_event(
        {"kind": kind, "mono_ns": stamp, "thread": thread, "data": data}
    )
    database.execute(
        "INSERT INTO activities VALUES(?,?,?,?,?)",
        (ordinal, stamp, _json(data), kind, thread),
    )
