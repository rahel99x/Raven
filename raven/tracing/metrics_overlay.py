"""Best-effort scientific metric observations in an already owned Python trace.

The metrics client remains vendorable: it finds this *already loaded* hook in
``sys.modules`` and imports nothing for standalone reporting. The lifecycle
registry is populated only after successful Python capture startup. Epoch-only
historical JSONL is never assigned an invented monotonic clock.
"""

from __future__ import annotations

import hashlib
import math
import os
import sys
import threading
import time
import weakref

MAX_NAMES = 64
INTERVAL_NS = 50_000_000
MAX_CAPTURES = 8
_LOCK = threading.RLock()
_BINDINGS = weakref.WeakKeyDictionary()
_PROCESS_HOOK = "_raven_scientific_trace_overlay"


def _owner(*, create=False):
    """One registry across installed and isolated launch-runtime namespaces."""
    current = sys.modules[__name__]
    return (
        sys.modules.setdefault(_PROCESS_HOOK, current)
        if create
        else sys.modules.get(_PROCESS_HOOK, current)
    )


def _after_fork():
    global _LOCK, _BINDINGS
    _LOCK = threading.RLock()
    _BINDINGS = weakref.WeakKeyDictionary()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _active(capture):
    recorder = capture.recorder
    return (
        not capture._stopped
        and not recorder._fork_disabled
        and recorder._pid == os.getpid()
        and recorder.state == "OPEN"
    )


class _Binding:
    def __init__(self, capture):
        self.pid = os.getpid()
        self.names = {}
        self.name_count = 0
        self.lock = threading.RLock()
        self.coverage = {
            "schema": "raven.scientific-overlay/v1",
            "producer": "raven metrics micro-API/v1",
            "status": "available",
            "scope": "Admitted metric/progress calls observed in this owned Python process; same trace clock and thread lifetime",
            "policy": "At most 20 observations/s per admitted metric group, 64 metric names across configured streams per capture; coalesced and unavailable observations are counted",
            "clock": "Recorder monotonic clock at producer admission; JSONL epoch timestamps are descriptive only",
            "accepted": 0,
            "emitted": 0,
            "coalesced": 0,
            "dropped": 0,
            "ambiguous": 0,
            "last_error": "",
        }
        capture.recorder.coverage["scientific_metrics"] = self.coverage


def register(capture):
    """Called only by a successfully started PythonCapture; does no I/O."""
    owner = _owner(create=True)
    if owner is not sys.modules[__name__]:
        return owner.register(capture)
    with _LOCK:
        if capture in _BINDINGS:
            return True
        if len(_BINDINGS) >= MAX_CAPTURES or not _active(capture):
            return False
        _BINDINGS[capture] = _Binding(capture)
    return True


def unregister(capture):
    """A closed capture's bounded coverage stays in its authoritative manifest."""
    owner = _owner()
    if owner is not sys.modules[__name__]:
        return owner.unregister(capture)
    with _LOCK:
        binding = _BINDINGS.pop(capture, None)
    if binding is not None:
        with binding.lock:
            binding.coverage["status"] = "stopped"


def readiness():
    """Diagnostic metadata only; this does not authorize a recorder or job."""
    owner = _owner()
    if owner is not sys.modules[__name__]:
        return owner.readiness()
    with _LOCK:
        active = [capture for capture in _BINDINGS if _active(capture)]
    return {
        "schema": "raven.scientific-overlay-readiness/v1",
        "available": len(active) == 1,
        "active_captures": len(active),
        "reason": "One owned active Python capture supplies the existing stream and monotonic clock"
        if len(active) == 1
        else "Multiple active captures are ambiguous; no metric is assigned"
        if active
        else "Start the scientific program with Raven Python tracing to connect its metrics to a verified trace",
        "historical_jsonl": "Unavailable without an exact run-attempt association and recorded epoch/monotonic calibration bracket; existing epoch t alone is insufficient",
        "retained_cpu_memory": "Existing raven.cpu-memory-sample/v1 rows use collector epoch time and lack a recorded monotonic calibration/boot/attempt anchor; they remain available in Analytics rather than being silently overlaid",
    }


def _payload(client, record, kind, unit):
    values = record.get("metrics")
    if not isinstance(values, dict) or len(values) > MAX_NAMES:
        raise ValueError("Scientific overlay requires bounded validated metrics")
    values = dict(values)
    units = {name: unit or "unspecified" for name in values}
    namespace = "raven.microapi.metrics"
    progress = record.get("progress")
    if progress is not None:
        completed, total = progress["completed"], progress["total"]
        values = {
            "progress.completed": completed,
            "progress.total": total,
            "progress.fraction": completed / total,
        }
        units = {
            "progress.completed": progress["unit"] or "steps",
            "progress.total": progress["unit"] or "steps",
            "progress.fraction": "ratio",
        }
        namespace = "raven.microapi.progress"
    if not values or any(
        not isinstance(name, str)
        or not name.isprintable()
        or not 0 < len(name) <= 96
        or type(value) not in (int, float)
        or not math.isfinite(value)
        for name, value in values.items()
    ):
        raise ValueError("Scientific overlay requires finite numeric observations")
    stream_id = hashlib.sha256(os.fsencode(client.path)).hexdigest()
    data = {
        "schema": "raven.scientific-counter/v1",
        "values": values,
        "units": units,
        "metric_kinds": {name: kind for name in values},
        "producer_namespace": namespace,
        "producer_stream_id": stream_id,
        "producer": "raven metrics micro-API/v1",
        "source_pid": os.getpid(),
        "reported_epoch_seconds": record["t"],
        "timestamp_provenance": "Trace monotonic time at producer admission; reported epoch time is not used to align clocks",
    }
    for field in ("step", "phase"):
        if field in record:
            data[field] = record[field]
    return data


def publish(client, record, *, kind, unit):
    """Observe an admitted call; queue only, with no scientific-job side effects.

    Return a diagnostic code. The caller contains all exceptions, as with the
    existing optional reporting API. No new recorder, file, worker or allocation
    is created here; an ambiguous/no-longer-owned capture cannot be borrowed.
    """
    with _LOCK:
        active = [
            (capture, binding)
            for capture, binding in _BINDINGS.items()
            if binding.pid == os.getpid() and _active(capture)
        ]
    if not active:
        return "inactive"
    if len(active) != 1:
        for _, binding in active:
            binding.coverage["ambiguous"] += 1
            binding.coverage["last_error"] = (
                "More than one active Python capture; metric association withheld"
            )
        return "ambiguous"
    capture, binding = active[0]
    data = _payload(client, record, kind, unit)
    names = tuple(sorted(data["values"]))
    key = (data["producer_namespace"], data["producer_stream_id"], names)
    now = time.monotonic_ns()
    with binding.lock:
        coverage = binding.coverage
        if coverage["status"] != "available" or not _active(capture):
            return "inactive"
        coverage["accepted"] += 1
        previous = binding.names.get(key)
        if previous is None:
            if binding.name_count + len(names) > MAX_NAMES:
                coverage["dropped"] += 1
                coverage["last_error"] = (
                    "Scientific overlay reached 64 metric names; additional names are not recorded"
                )
                return "name-budget"
            previous = binding.names[key] = [None, 0]
            binding.name_count += len(names)
        if previous[0] is not None and now - previous[0] < INTERVAL_NS:
            previous[1] += 1
            coverage["coalesced"] += 1
            return "coalesced"
        data["overlay"] = {
            "max_rate_hz": 20,
            "coalesced_since_previous": previous[1],
            "semantics": "Observed admission-time point; unrecorded intermediate values are unknown",
        }
        # The ordinary monitoring callback holds this same recursion guard.
        # Without it, scope=all could trace Event.set() while enqueue holds its
        # non-reentrant queue lock and recursively enqueue another frame.
        previous_busy = getattr(capture.local, "busy", False)
        capture.local.busy = True
        try:
            thread = capture._thread_identity(queued=True)
            data["source_node"] = capture.recorder.identity["node"]
            # Existing recorder stamps the producer's monotonic clock; only
            # its background writer performs encoding and filesystem work.
            emitted = capture.recorder.enqueue("counter", data, thread=thread)
        finally:
            capture.local.busy = previous_busy
        if emitted:
            previous[:] = [now, 0]
            coverage["emitted"] += 1
            coverage["last_error"] = ""
            return "emitted"
        coverage["dropped"] += 1
        coverage["last_error"] = (
            "Owned trace recorder no longer accepts observations; existing metrics reporting continues"
        )
        return "recorder-unavailable"
