"""Explicit cooperative incident capture with bounded pre/post retention.

This is a metadata flight recorder, not an automatic sampler or process
attachment API. Before a trigger it keeps a rolling in-memory window. The
trigger publishes that retained window into the ordinary integrity journal;
subsequent events are available live until the declared post budget ends.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

from . import protocol


class TriggeredCapture:
    """Capture an explicitly triggered slice without stopping scientific work.

    ``emit`` validates and snapshots bounded metadata. ``trigger(reason)`` is
    idempotent and returns whether this call triggered capture. Storage/quota
    failures stop capture, remain in ``receipt`` and do not kill the program.
    Programmer input errors still raise ``ValueError``. Use as a context manager
    or call ``close`` to save a never-triggered receipt and release the timer.
    """

    def __init__(
        self,
        project,
        *,
        job_id,
        attempt,
        cluster="local",
        pre_seconds=5.0,
        pre_events=4096,
        pre_bytes=1 << 20,
        post_seconds=5.0,
        post_events=4096,
        max_bytes=16 << 20,
    ):
        for value, label in ((pre_seconds, "pre"), (post_seconds, "post")):
            if (
                isinstance(value, bool)
                or not isinstance(value, (float, int))
                or not math.isfinite(value)
                or not 0.001 <= value <= 3600
            ):
                raise ValueError(
                    f"Triggered {label} duration must be 1 ms through one hour"
                )
        for value, label, maximum, minimum in (
            (pre_events, "pre events", 65536, 1),
            (post_events, "post events", 1_000_000, 1),
            (pre_bytes, "pre bytes", 64 << 20, 1024),
            (max_bytes, "journal bytes", 1 << 30, 4096),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"Triggered {label} exceeds its bounded range")
        if isinstance(job_id, bool) or not isinstance(job_id, (int, str)):
            raise ValueError("Triggered capture requires an explicit job ID")
        self.project = Path(project).absolute()
        self.identity = {
            "job_id": protocol.text(str(job_id), "job ID", 256),
            "attempt": protocol.text(attempt, "attempt", 256),
            "cluster": protocol.text(cluster, "cluster", 256),
        }
        self.pre_ns, self.post_seconds = int(pre_seconds * 1e9), post_seconds
        self.pre_events, self.pre_bytes = pre_events, pre_bytes
        self.post_events, self.max_bytes = post_events, max_bytes
        self._ring, self._bytes = deque(), 0
        self._pid, self._lock = os.getpid(), threading.RLock()
        self._threads = threading.local()
        self._started = self._closed = False
        self._last = 0
        self._recorder = self._timer = None
        self._deadline = None
        self.receipt_path = None
        self.receipt = {
            "schema": "raven.triggered-capture/v1",
            "status": "not-started",
            "identity": dict(self.identity),
            "trigger": None,
            "manifest": None,
            "pre_budget": {
                "seconds": pre_seconds,
                "events": pre_events,
                "bytes": pre_bytes,
            },
            "post_budget": {"seconds": post_seconds, "events": post_events},
            "journal_budget_bytes": max_bytes,
            "evicted_pre_events": 0,
            "retained_pre_events": 0,
            "retained_post_events": 0,
            "errors": [],
            "scope": "Explicit cooperative metadata only; uninstrumented execution is unknown",
            "retention": "Rolling pre-window evicts older metadata intentionally; post capture stops at its first budget",
            "capture_qualified": False,
        }

    def _error(self, error, *, receipt=None, status="error"):
        from ..job_logs.text import display_text

        receipt = self.receipt if receipt is None else receipt
        receipt["status"] = status
        if len(receipt["errors"]) < 16:
            receipt["errors"].append(
                display_text(type(error).__name__ + ": " + str(error))[:2000]
            )

    def _publish(self, receipt=None):
        from .capture import _safe_directory, _trace_root
        from ..reporting import _atomic_json

        receipt = self.receipt if receipt is None else receipt
        try:
            if self.receipt_path is None:
                parent = _trace_root(self.project).parent / "triggered-captures"
                parent.mkdir(mode=0o700, exist_ok=True)
                _safe_directory(parent)
                folder = parent / uuid.uuid4().hex
                folder.mkdir(mode=0o700)
                self.receipt_path = folder / "receipt.json"
                receipt["receipt"] = str(self.receipt_path)
            _atomic_json(self.receipt_path, receipt, replace=True)
        except (OSError, ValueError) as error:
            self._error(error, receipt=receipt)

    def __enter__(self):
        if os.getpid() != self._pid:
            raise RuntimeError("A triggered capture cannot be inherited across fork")
        with self._lock:
            if self._started or self._closed:
                raise RuntimeError("A triggered capture can be entered only once")
            self._started = True
            self.receipt["status"] = "armed"
            self._publish()
        return self

    def _thread(self):
        value = getattr(self._threads, "identity", None)
        if value is None:
            value = f"thread:{threading.get_native_id()}:{uuid.uuid4().hex}"
            self._threads.identity = value
        return value

    def _evict(self, stamp, *, incoming=0):
        while self._ring and (
            len(self._ring) + bool(incoming) > self.pre_events
            or self._bytes + incoming > self.pre_bytes
            or self._ring[0][0] < stamp - self.pre_ns
        ):
            _, raw = self._ring.popleft()
            self._bytes -= len(raw)
            self.receipt["evicted_pre_events"] += 1

    def emit(self, kind, data=None, *, thread=None, mono_ns=None):
        """Record one event, preserving an explicit original monotonic stamp."""
        if os.getpid() != self._pid:
            return False
        with self._lock:
            if not self._started:
                raise RuntimeError("Triggered events require an entered capture")
            if self._closed or self.receipt["status"] not in {"armed", "recording"}:
                return False
            stamp = time.monotonic_ns() if mono_ns is None else mono_ns
            event = protocol.validate_event(
                {
                    "kind": kind,
                    "data": {} if data is None else data,
                    "thread": str(thread) if thread is not None else self._thread(),
                    "mono_ns": stamp,
                }
            )
            if stamp < self._last:
                raise ValueError("Triggered event timestamps must not move backwards")
            self._last = stamp
            # Snapshot mutable caller-owned values and account actual encoded
            # metadata bytes, rather than unbounded application object graphs.
            raw = protocol.encoded(event)
            if self.receipt["status"] == "armed":
                self._evict(stamp, incoming=len(raw))
                if len(raw) > self.pre_bytes:
                    self.receipt["evicted_pre_events"] += 1
                    return False
                self._ring.append((stamp, raw))
                self._bytes += len(raw)
                return True
            if time.monotonic() >= self._deadline:
                self._finish("post-time-budget")
                return False
            if not self._write(json.loads(raw)):
                return False
            self.receipt["retained_post_events"] += 1
            if self.receipt["retained_post_events"] >= self.post_events:
                self._finish("post-event-budget")
            return True

    def _write(self, event):
        try:
            if self._recorder.emit(
                event["kind"],
                event["data"],
                thread=event["thread"],
                mono_ns=event["mono_ns"],
            ):
                return True
            self.receipt["capture_state"] = self._recorder.state
            self._finish("journal-budget-or-storage-failure", status="partial")
        except (OSError, ValueError) as error:
            self._error(error)
            self._finish("write-error", status="error")
        return False

    def trigger(self, reason):
        """Publish the retained prefix once; later trigger calls return False."""
        reason = protocol.text(reason, "trigger reason", 1024)
        if os.getpid() != self._pid:
            return False
        with self._lock:
            if not self._started:
                raise RuntimeError("Trigger requires an entered capture")
            if self._closed or self.receipt["status"] != "armed":
                return False
            from .capture import Recorder, register_trace

            stamp = max(time.monotonic_ns(), self._last)
            self._evict(stamp)
            self.receipt["retained_pre_events"] = len(self._ring)
            self.receipt["trigger"] = {"reason": reason, "mono_ns": stamp}
            self.receipt["status"] = "recording"
            owner = self

            class SliceRecorder(Recorder):
                def emit(self, kind, data=None, *, mono_ns=None, **kwargs):
                    return super().emit(
                        kind,
                        data,
                        mono_ns=max(time.monotonic_ns(), owner._last)
                        if mono_ns is None
                        else mono_ns,
                        **kwargs,
                    )

            try:
                self._recorder = SliceRecorder(
                    self.project,
                    **self.identity,
                    initial_mono_ns=self._ring[0][0] if self._ring else stamp,
                    max_bytes=self.max_bytes,
                    background_flush=False,
                    journal_format="blocks",
                    register=False,
                    coverage={
                        "adapter": "cooperative-triggered",
                        "scope": self.receipt["scope"],
                        "retention": self.receipt["retention"],
                        "evicted_pre_events": self.receipt["evicted_pre_events"],
                        "trigger": self.receipt["trigger"],
                    },
                )
                self.receipt["manifest"] = str(self._recorder.path)
                while self._ring:
                    _, raw = self._ring.popleft()
                    self._bytes -= len(raw)
                    if not self._write(json.loads(raw)):
                        break
                self._ring.clear()
                self._bytes = 0
                if self.receipt["status"] != "recording":
                    return False
                self._last = stamp
                if not self._write(
                    {
                        "kind": "capture_trigger",
                        "data": {"reason": reason},
                        "thread": self._thread(),
                        "mono_ns": stamp,
                    }
                ):
                    return False
                self._recorder.flush()
                try:
                    register_trace(self.project, self._recorder.path)
                    self.receipt["registered"] = True
                except (OSError, ValueError) as error:
                    self.receipt["registered"] = False
                    self.receipt["registration_error"] = str(error)[:2000]
                    self.receipt["registration_argv"] = [
                        "raven",
                        "trace",
                        "register",
                        "--project",
                        str(self.project),
                        str(self._recorder.path),
                    ]
                self._deadline = time.monotonic() + self.post_seconds
                self._timer = threading.Timer(self.post_seconds, self._expire)
                self._timer.daemon = True
                self._timer.name = "RavenTriggeredCaptureDeadline"
                self._timer.start()
                self._publish()
                return True
            except (OSError, ValueError) as error:
                self._error(error)
                self._finish("trigger-error", status="error")
                return False

    def _expire(self):
        if os.getpid() != self._pid:
            return
        with self._lock:
            if self.receipt["status"] == "recording":
                self._finish("post-time-budget")

    def _finish(self, reason, *, status="complete"):
        # The public receipt is also read without this owner's lock. Keep its
        # terminal fields private until the journal and receipt are durable;
        # returning from close alone does not prove a complete recorder footer.
        final = dict(self.receipt, errors=list(self.receipt["errors"]))
        final.update(stop_reason=reason, status=status)
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._recorder is not None:
            try:
                self._recorder.close()
            except (OSError, ValueError) as error:
                self._error(error, receipt=final)
            state = self._recorder.state
            final.update(journal_bytes=self._recorder.bytes, capture_state=state)
            if state == "ERROR" or final["status"] == "complete" and state != "CLOSED":
                self._error(
                    RuntimeError(
                        "Recorder shutdown ended in "
                        + str(state)
                        + "; only its verified committed prefix is available"
                    ),
                    receipt=final,
                    status="error" if state == "ERROR" else "partial",
                )
        self._publish(final)
        # Preserve aliases held by existing SDK callers, while publishing all
        # terminal fields together after both durability barriers above.
        self.receipt.update(final)

    def close(self):
        if os.getpid() != self._pid:
            return
        with self._lock:
            if self._closed:
                return
            if not self._started:
                raise RuntimeError("Triggered close requires an entered capture")
            if self.receipt["status"] == "recording":
                self._finish("closed-by-user")
            elif self.receipt["status"] == "armed":
                self.receipt["status"] = "never-triggered"
                self.receipt["discarded_pre_events"] = len(self._ring)
                self._publish()
            self._ring.clear()
            self._bytes = 0
            self._closed = True

    def __exit__(self, *_):
        self.close()
        return False

    @contextmanager
    def span(self, name):
        """Emit original matched span boundaries; evicted halves remain unknown."""
        name = protocol.text(name, "span name", 8192)
        identifier, thread = uuid.uuid4().hex, self._thread()
        self.emit("span_start", {"name": name, "span_id": identifier}, thread=thread)
        try:
            yield identifier
        finally:
            self.emit("span_end", {"span_id": identifier}, thread=thread)
