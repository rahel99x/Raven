"""Nonblocking UI-side client for Raven's isolated terminal broker.

Only this module's owner (the UI thread) calls its methods.  Screen publications
are complete immutable states, so intermediate screens may be coalesced safely;
raw PTY bytes and accepted input are never coalesced or silently discarded.
"""
from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
import uuid

MAX_SESSIONS = 8
MAX_FRAME = 8 * 1024 * 1024
MAX_PENDING = 8 * 1024 * 1024
MAX_EVENT = 1024 * 1024


def frame(value):
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ValueError("Terminal message exceeds the IPC limit")
    return struct.pack("!I", len(data)) + data


class Decoder:
    """Bounded length framing, including partial headers and payloads."""
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        if len(self.buffer) + len(data) > MAX_FRAME + 65536 + 4:
            raise ValueError("Terminal IPC receive buffer exceeds its limit")
        self.buffer.extend(data)

    def take(self, limit=32):
        out = []
        consumed = 0
        while len(out) < limit and len(self.buffer) - consumed >= 4:
            size = struct.unpack_from("!I", self.buffer, consumed)[0]
            if size > MAX_FRAME:
                raise ValueError("Terminal IPC frame exceeds its limit")
            if len(self.buffer) - consumed < size + 4:
                break
            value = json.loads(self.buffer[consumed + 4:consumed + 4 + size])
            if not isinstance(value, dict):
                raise ValueError("Terminal IPC message must be an object")
            out.append(value)
            consumed += size + 4
        if consumed:
            del self.buffer[:consumed]
        return out


def dimensions(rows, cols):
    return max(1, min(160, int(rows))), max(2, min(400, int(cols)))


class TerminalService:
    """Session-preserving terminal service; opening a session never writes to UI."""
    def __init__(self):
        self.sessions = {}
        self.error = ""
        self._socket = None
        self._process = None
        self._decoder = Decoder()
        self._out = deque()
        self._pending = 0
        self._closed = False
        self._requests = 0
        self._identities = set()
        self._result_sizes = {}

    def _start(self):
        if self._closed:
            raise RuntimeError("Terminal service is closed")
        if self._process is not None:
            if self._process.poll() is not None:
                raise RuntimeError("Terminal broker exited; reopen Raven's terminal service")
            return
        parent, child = socket.socketpair()
        try:
            # No inherited shell, curses descriptors, or background worker locks.
            self._process = subprocess.Popen(
                [sys.executable, "-c", "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('raven.terminal_broker',run_name='__main__')",
                 str(Path(__file__).resolve().parent.parent), "--fd", str(child.fileno())],
                pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            parent.setblocking(False)
            self._socket = parent
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()

    def _queue(self, message):
        try:
            data = frame(message)
        except (TypeError, ValueError, OverflowError):
            self.error = "Terminal input is invalid or exceeds its size limit"
            return False
        if self._pending + len(data) > MAX_PENDING:
            self.error = "Terminal input queue is full; this input was not accepted"
            return False
        if self._socket is None or self._closed:
            self.error = "Terminal broker is unavailable"
            return False
        self._out.append(memoryview(data))
        self._pending += len(data)
        self._flush()
        return self._socket is not None

    def _flush(self):
        budget = 65536
        while self._socket is not None and self._out and budget > 0:
            try:
                sent = self._socket.send(self._out[0][:budget])
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                self._disconnect("Terminal broker connection closed")
                break
            if not sent:
                break
            self._pending -= sent
            budget -= sent
            self._out[0] = self._out[0][sent:]
            if not self._out[0]:
                self._out.popleft()

    def open(self, argv=None, cwd=None, label=None, env=None, kind="local", metadata=None, rows=24, cols=80,
             session_id=None, env_factory=None, history_lines=2000, history_bytes=4 * 1024 * 1024):
        if len(self.sessions) >= MAX_SESSIONS:
            raise ValueError("Close a terminal session before opening another (limit: 8)")
        rows, cols = dimensions(rows, cols)
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("Terminal metadata must be an object")
        if argv is None:
            shell = os.environ.get("SHELL") or "/bin/bash"
            argv = [shell, "-i"]
        if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(v, str) and v and "\0" not in v for v in argv):
            raise ValueError("Terminal command must be a nonempty argument list")
        sid = session_id or uuid.uuid4().hex
        if not isinstance(sid, str) or len(sid) != 32 or not sid.isalnum() or sid in self._identities:
            raise ValueError("Terminal session identity is invalid or has already been used")
        if env_factory is not None:
            env = {**(env or {}), **env_factory(sid)}
        if env is not None and (not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) and "\0" not in k + v and "=" not in k for k, v in env.items())):
            raise ValueError("Terminal environment contains invalid names or values")
        command = {"op": "open", "id": sid, "argv": list(argv), "cwd": os.fspath(cwd) if cwd is not None else None,
                   "label": str(label or os.path.basename(argv[0]))[:120], "env": env,
                   "kind": str(kind)[:40], "metadata": metadata or {}, "rows": rows, "cols": cols,
                   "history_lines": max(1, min(10000, int(history_lines))),
                   "history_bytes": max(0, min(4 * 1024 * 1024, int(history_bytes)))}
        # Validate before spawning, and avoid retaining argv/environment in snapshots.
        frame(command)
        self._start()
        self._identities.add(sid)
        self.sessions[sid] = {"id": sid, "seq": 0, "epoch": 0, "rows": rows, "cols": cols,
                              "label": command["label"], "kind": command["kind"], "metadata": command["metadata"],
                              "status": "starting", "lines": [], "history": [], "error": "", "exit_code": None}
        if not self._queue(command):
            self.sessions.pop(sid, None)
            self._result_sizes.pop(sid, None)
            raise RuntimeError(self.error)
        return sid

    def snapshot(self, sid):
        return self.sessions.get(sid, {})

    def send(self, sid, event):
        state = self.sessions.get(sid)
        if not state or state.get("status") not in ("starting", "running") or not isinstance(event, dict):
            return False
        try:
            payload = event.get("text", event.get("data", ""))
            if isinstance(payload, str) and len(payload.encode("utf-8")) > MAX_EVENT:
                self.error = "Terminal input exceeds 1 MiB; send smaller chunks"
                return False
            frame(event)
        except (TypeError, ValueError, OverflowError):
            return False
        return self._queue({"op": "input", "id": sid, "event": event})

    def resize(self, sid, rows, cols):
        state = self.sessions.get(sid)
        if not state or state.get("status") not in ("starting", "running"):
            return False
        rows, cols = dimensions(rows, cols)
        if (state.get("requested_rows", state.get("rows")), state.get("requested_cols", state.get("cols"))) == (rows, cols):
            return True
        if self._queue({"op": "resize", "id": sid, "rows": rows, "cols": cols}):
            self.sessions[sid] = {**state, "requested_rows": rows, "requested_cols": cols}
            return True
        return False

    def request(self, sid, action, **kwargs):
        if sid not in self.sessions or action not in {"rename", "snapshot", "history", "search", "clear_history", "cancel_input", "record_start", "record_stop", "export", "diagnostics", "visible"}:
            return False
        self._requests += 1
        return self._queue({"op": "request", "id": sid, "action": action, "request_id": self._requests, "args": kwargs})

    def close_session(self, sid):
        if sid not in self.sessions:
            return False
        accepted = self._queue({"op": "close", "id": sid})
        if accepted:
            # Late publications for this never-reused ID are ignored.
            self.sessions.pop(sid, None)
            self._result_sizes.pop(sid, None)
        return accepted

    def _disconnect(self, reason):
        self.error = reason
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._out.clear()
        self._pending = 0
        for sid, state in list(self.sessions.items()):
            if state.get("status") in ("starting", "running", "closing"):
                self.sessions[sid] = {**state, "status": "error", "error": reason}

    def _apply(self, value):
        sid = value.get("id")
        old = self.sessions.get(sid)
        if old is None:
            return False
        if value.get("op") == "snapshot":
            state = value.get("state")
            if not isinstance(state, dict) or state.get("seq", -1) <= old.get("seq", -1):
                return False
            if state.get("epoch", 0) < old.get("epoch", 0):
                return False
            # Publications contain full grids, so an intervening missing sequence
            # does not require applying unsafe dependent deltas.
            self.sessions[sid] = {**state, "id": sid, "result": old.get("result", {}), "results": old.get("results", ())}
            return True
        if value.get("op") == "result":
            results = [*old.get("results", ()), value]
            sizes = [*self._result_sizes.get(sid, ()), len(frame(value))]
            size = sum(sizes)
            while len(results) > 32 or (size > 4 * 1024 * 1024 and len(results) > 1):
                results.pop(0)
                size -= sizes.pop(0)
            self._result_sizes[sid] = tuple(sizes)
            self.sessions[sid] = {**old, "result": value, "results": tuple(results)}
            return True
        return False

    def poll(self):
        changed = False
        connected = self._socket is not None
        self._flush()
        if self._socket is None:
            if self._process is not None:
                self._process.poll()  # reap without waiting
            return connected
        try:
            budget = 262144
            for message in self._decoder.take(64):
                changed = self._apply(message) or changed
            while budget > 0:
                try:
                    data = self._socket.recv(min(65536, budget))
                except (BlockingIOError, InterruptedError):
                    break
                if not data:
                    self._disconnect("Terminal broker exited")
                    return True
                budget -= len(data)
                self._decoder.feed(data)
                for message in self._decoder.take(64):
                    changed = self._apply(message) or changed
        except (OSError, ValueError, TypeError, UnicodeError):
            self._disconnect("Terminal broker protocol error")
            return True
        if self._process is not None and self._process.poll() is not None:
            self._disconnect("Terminal broker exited")
            changed = True
        return changed

    def shutdown(self):
        if self._closed:
            return
        if self._socket is not None:
            self._queue({"op": "shutdown"})
            self._flush()
            # EOF is also a shutdown request, including when a queue was full.
            self._socket.close()
            self._socket = None
        self._closed = True
        self._out.clear()
        self._pending = 0
        if self._process is not None:
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                try:
                    self._process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    try:
                        self._process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        # A process stuck in a kernel wait can outlive SIGKILL.
                        # Keep a wait owner without preventing the rest of Raven's
                        # exit cleanup (including its private bridge) from running.
                        import threading
                        self.error = "Terminal broker shutdown is still pending after SIGKILL; cleanup reaper is waiting"
                        self._reaper = threading.Thread(target=self._process.wait, name="raven-terminal-reaper", daemon=True)
                        self._reaper.start()
        for sid, state in list(self.sessions.items()):
            self.sessions[sid] = {**state, "status": "closed"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
