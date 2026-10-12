"""Opt-in, bounded UI timings with no file writes or source data in a frame."""
from __future__ import annotations

from collections import Counter, deque
from contextlib import contextmanager
import heapq
import json
import math
import os
import platform
import re
import time

from . import __version__

MAX_SAMPLES = 2048
MAX_SLOW = 64
MAX_TRANSITIONS = 128
MAX_PHASES = 32
_LABEL = re.compile(r"[A-Za-z0-9_-]{1,64}\Z", re.ASCII)


def _label(value):
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else "other"


def _context(app):
    def dimension(name):
        value = getattr(app, name, 0)
        return max(0, min(value, 100000)) if isinstance(value, int) else 0
    return {"tab": _label(getattr(app, "tab", "")),
            "mode": _label(getattr(app, "mode", "")),
            "width": dimension("width"), "height": dimension("height")}


class TraceError(Exception):
    pass


class UITrace:
    def __init__(self):
        self.started = time.perf_counter_ns()
        self.phases = {}
        self.inputs = Counter()
        self.transitions = deque(maxlen=MAX_TRANSITIONS)
        self.slow = []
        self.serial = 0
        self.input_kind = "none"
        self.input_action = "none"
        self.input_at = None
        self.maximum_geometry = (0, 0)

    def input(self, app, event):
        """Record the event category and a main-page binding, never its text."""
        name, _ = event
        kind = name if name in ("mouse", "paste", "resize") else "keyboard" if name else "ignored"
        action = "none"
        if kind == "keyboard" and getattr(app, "mode", "") == "main":
            action = _label(getattr(app, "keymap", {}).get(name, "unbound"))
        self.input_kind, self.input_action = kind, action
        self.input_at = time.perf_counter_ns()
        self.inputs[kind] += 1

    def record(self, app, phase, start, cpu_start, before):
        elapsed = max(0., (time.perf_counter_ns() - start) / 1e6)
        cpu = max(0., (time.thread_time_ns() - cpu_start) / 1e6)
        phase = _label(phase)
        if phase not in self.phases and len(self.phases) >= MAX_PHASES:
            return
        state = self.phases.setdefault(phase, {
            "calls": 0, "total_ms": 0., "cpu_ms": 0., "max_ms": 0.,
            "samples": deque(maxlen=MAX_SAMPLES)})
        state["calls"] += 1
        state["total_ms"] += elapsed
        state["cpu_ms"] += cpu
        state["max_ms"] = max(state["max_ms"], elapsed)
        state["samples"].append(elapsed)
        after = _context(app)
        self.maximum_geometry = tuple(max(old, new) for old, new in
                                      zip(self.maximum_geometry, (after["width"], after["height"])))
        event = {"at_ms": round((start - self.started) / 1e6, 3),
                 "phase": phase, "wall_ms": round(elapsed, 3), "cpu_ms": round(cpu, 3),
                 "context": before, "input_kind": self.input_kind,
                 "input_action": self.input_action,
                 "input_age_ms": None if self.input_at is None else round(max(0, start - self.input_at) / 1e6, 3)}
        # Waiting for a user is expected. Include its statistics, but do not
        # present an ordinary curses timeout as a slow processing frame.
        if phase != "input_wait" and elapsed >= 8.:
            self.serial += 1
            item = (elapsed, self.serial, event)
            if len(self.slow) < MAX_SLOW:
                heapq.heappush(self.slow, item)
            elif elapsed > self.slow[0][0]:
                heapq.heapreplace(self.slow, item)
        if (before["tab"], before["mode"]) != (after["tab"], after["mode"]):
            self.transitions.append(dict(event, after=after))

    def measure(self, app, phase, operation, *args, **kwargs):
        before = _context(app)
        start, cpu_start = time.perf_counter_ns(), time.thread_time_ns()
        try:
            return operation(*args, **kwargs)
        finally:
            self.record(app, phase, start, cpu_start, before)

    def report(self):
        phases = {}
        for phase, state in sorted(self.phases.items()):
            values = sorted(state["samples"])
            count = state["calls"]
            result = {name: round(state[name], 3) for name in ("total_ms", "cpu_ms", "max_ms")}
            result.update(calls=count, mean_ms=round(state["total_ms"] / count, 3),
                          retained_samples=len(values))
            for label, fraction in (("p50_ms", .5), ("p95_ms", .95), ("p99_ms", .99)):
                result[label] = round(values[max(0, math.ceil(len(values) * fraction) - 1)], 3)
            phases[phase] = result
        return {"schema": "raven.ui-trace/v1", "version": __version__,
                "python": platform.python_version(), "system": platform.system(),
                "terminal": _label(os.environ.get("TERM", "unknown")),
                "elapsed_seconds": round((time.perf_counter_ns() - self.started) / 1e9, 3),
                "maximum_geometry": {"width": self.maximum_geometry[0], "height": self.maximum_geometry[1]},
                "inputs": dict(self.inputs), "phases": phases,
                "slowest": [item[2] for item in sorted(self.slow, reverse=True)],
                "transitions": list(self.transitions),
                "limits": {"samples_per_phase": MAX_SAMPLES, "slowest": MAX_SLOW,
                           "transitions": MAX_TRANSITIONS, "phases": MAX_PHASES},
                "notes": ["Phase times overlap; do not add parent and child phases.",
                          "CPU time measures only the UI thread; wall time also includes waiting and scheduling.",
                          "input_wait includes the configured idle timeout and is excluded from slowest.",
                          "Percentiles use the latest bounded sample window; totals and maxima cover this run.",
                          "Input counts describe dispatched events after motion coalescing.",
                          "Input context is the most recently dispatched event; its age does not establish causation.",
                          "No typed text, raw terminal bytes, job IDs, log contents, or source paths are recorded."]}


def timed(app, phase, operation, *args, **kwargs):
    trace = getattr(app, "ui_trace", None)
    return operation(*args, **kwargs) if trace is None else trace.measure(app, phase, operation, *args, **kwargs)


@contextmanager
def capture(path, app=None):
    """Reserve a private new file; save bounded evidence after curses exits."""
    try:
        fd = os.open(os.path.expanduser(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise TraceError(f"Cannot create UI trace: {exc}") from exc
    trace, previous = UITrace(), getattr(app, "ui_trace", None)
    if app is not None:
        app.ui_trace = trace
    try:
        yield trace
    finally:
        if app is not None:
            app.ui_trace = previous
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(trace.report(), output, ensure_ascii=True, allow_nan=False, indent=2)
                output.write("\n")
        except OSError as exc:
            raise TraceError(f"Cannot save UI trace: {exc}") from exc
