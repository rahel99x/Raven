"""Negotiated DEC 2026 frame boundaries; no terminal-name heuristics.

This object belongs to the UI thread. It neither reads stdin nor changes the
existing byte decoder, terminal transport, or image/layout semantics.
"""
from __future__ import annotations

from contextlib import contextmanager
import re
import time

QUERY = "\x1b[?2026$p"
BEGIN = "\x1b[?2026h"
END = "\x1b[?2026l"
_RESPONSE = re.compile(r"\x1b\[\?2026;([0-4])\$y\Z", re.ASCII)
PROBE_SECONDS = 1.0


class TerminalSync:
    def __init__(self, stream, mode="auto"):
        self.stream = stream
        self.mode = mode
        self.enabled = False
        self.pending = False
        self.probe_sent = False
        self.deadline = 0.0
        self.active = False
        self.closed = False
        self.frames = 0
        self.bytes = 0
        self.max_region_seconds = 0.0
        self.reason = "not started"

    def _write(self, value):
        try:
            self.stream.write(value)
            self.stream.flush()
            self.bytes += len(value)
            return True
        except (OSError, ValueError):
            self.enabled = self.pending = False
            self.reason = "terminal output unavailable; ordinary painting retained"
            return False

    def start(self):
        if self.closed or self.reason != "not started":
            return
        if self.mode not in ("auto", "on", "off"):
            self.reason = "invalid synchronized_output; choose auto, on, or off"
        elif not getattr(self.stream, "isatty", lambda: False)():
            self.reason = "not an interactive output terminal"
        elif self.mode == "off":
            self.reason = "disabled by configuration"
        elif self.mode == "on":
            self.enabled = True
            self.reason = "enabled explicitly; terminal support not verified"
        else:
            self.pending = True
            self.deadline = time.monotonic() + PROBE_SECONDS
            self.reason = "awaiting DEC 2026 capability response"
            self.probe_sent = self._write(QUERY)

    def receive(self, sequence):
        """Own our probe replies, including late/duplicate replies; enable once.

        Terminal control replies must never become embedded workload input when
        a slow transport exceeds the negotiation deadline.
        """
        self.expire()
        match = _RESPONSE.fullmatch(sequence)
        if not self.probe_sent or match is None:
            return False
        if not self.pending or self.closed:
            return True
        self.pending = False
        self.enabled = match[1] in ("1", "2")
        self.reason = ("DEC 2026 support reported by terminal" if self.enabled else
                       "terminal reports unsupported or non-switchable DEC 2026")
        return True

    def expire(self):
        if self.pending and time.monotonic() >= self.deadline:
            self.pending = False
            self.reason = "no capability response; ordinary painting retained"

    @contextmanager
    def frame(self, changed=True):
        """Enclose only a bounded frame flush, never input waits or collection."""
        self.expire()
        own = bool(changed and self.enabled and not self.active and not self.closed)
        start = time.monotonic() if own else 0.0
        if own:
            # Set ownership before writing: a partial failed begin still needs
            # the best-effort reset in finally. Nested frames share one owner.
            self.active = True
            self._write(BEGIN)
        try:
            yield
        finally:
            if own and self.active:
                self._write(END)
                self.active = False
                self.frames += 1
                self.max_region_seconds = max(self.max_region_seconds, time.monotonic() - start)

    def close(self):
        if self.active:
            self._write(END)
            self.active = False
        self.enabled = self.pending = False
        self.closed = True

    def snapshot(self):
        self.expire()
        return {"mode": self.mode if self.mode in ("auto", "on", "off") else "invalid",
                "enabled": self.enabled, "pending": self.pending, "reason": self.reason,
                "frames": self.frames, "control_bytes": self.bytes,
                "max_region_seconds": self.max_region_seconds}
