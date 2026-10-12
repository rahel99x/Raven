"""Isolated PTY owner.  This process never accesses curses or the outer TTY."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import argparse
import errno
import fcntl
import getpass
import json
import os
from pathlib import Path
import pty
import queue
import selectors
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid

from .terminal_service import Decoder, MAX_EVENT, MAX_SESSIONS, dimensions, frame

INPUT_LIMIT = 2 * 1024 * 1024
READ_SLICE = 4096
PUBLISH_INTERVAL = 1 / 30


class RecordingWriter:
    """Bounded optional disk writer; slow recording storage cannot stop PTYs.

    A full queue ends recording explicitly.  PTY protocol handling continues;
    accepted recording events are drained and the reported stop reason makes
    incomplete coverage visible.  No input or environment is recorded.
    """
    def __init__(self, path, sid, cols, rows):
        self.path = path
        self.closed = False
        self.stopping = False
        self.reason = ""
        self.error = ""
        self.started = time.monotonic()
        self.ready = False
        self.queue = queue.Queue(maxsize=32)
        self.thread = threading.Thread(target=self._run, args=(sid, cols, rows), name="raven-terminal-recording", daemon=True)
        self.thread.start()

    def _run(self, sid, cols, rows):
        recorder = None
        try:
            from .terminal_recording import Recorder
            clock = [self.started]
            recorder = Recorder(self.path, sid, cols=cols, rows=rows, clock=lambda: clock[0])
            self.ready = True
            while True:
                try:
                    event, args, captured_at = self.queue.get(timeout=.05)
                except queue.Empty:
                    if self.stopping:
                        break
                    continue
                # Preserve PTY receipt timing, rather than slow disk-write timing.
                clock[0] = captured_at
                if event == "output":
                    recorder.output(*args)
                else:
                    recorder.resize(*args)
                if recorder.closed:
                    self.reason = recorder.reason
                    break
        except (ImportError, OSError, ValueError, TypeError, RuntimeError) as exc:
            self.error = self.reason = str(exc)
        finally:
            if recorder:
                recorder.close(self.reason or "stopped by user")
            self.closed = True

    def _put(self, event, args):
        if self.closed or self.stopping:
            return False
        try:
            self.queue.put_nowait((event, args, time.monotonic()))
        except queue.Full:
            self.stopping = True
            self.reason = "Recording storage fell behind; recording stopped with incomplete coverage"
            return False
        return True

    def output(self, data):
        return self._put("output", (data,))

    def resize(self, cols, rows):
        return self._put("resize", (cols, rows))

    def close(self):
        self.stopping = True


@dataclass
class Session:
    id: str
    rows: int
    cols: int
    label: str
    kind: str
    metadata: dict
    engine: object = None
    process: object = None
    fd: int = -1
    status: str = "starting"
    exit_code: object = None
    error: str = ""
    seq: int = 0
    epoch: int = 0
    dirty: bool = True
    last_publish: float = 0
    output_bytes: int = 0
    input_bytes: int = 0
    input_rejected: int = 0
    pending: bytearray = field(default_factory=bytearray)
    close_at: float = 0
    exit_at: float = 0
    recorder: object = None
    record_path: str = ""
    parse_seconds: float = 0
    visible: bool = True
    recording_stopped: object = None
    history_snapshot: object = None


class Broker:
    def __init__(self, channel, *, adopt_children=False):
        self.channel = channel
        channel.setblocking(False)
        self.selector = selectors.DefaultSelector()
        self.selector.register(channel, selectors.EVENT_READ, None)
        self.decoder = Decoder()
        self.sessions = {}
        self.results = deque()
        self.result_bytes = 0
        self.out = memoryview(b"")
        self.running = True
        self.shutting_down = False
        self.shutdown_at = 0
        self.recorders = []
        self.read_turn = 0
        self.subreaper = False
        if adopt_children and sys.platform.startswith("linux"):
            try:
                import ctypes
                # Adopt shell grandchildren when a shell exits before collecting
                # them.  The broker is isolated; Raven's other workers/processes
                # are not descendants and cannot be adopted or reaped here.
                self.subreaper = ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
            except (ImportError, AttributeError, OSError):
                pass

    def interest(self, session):
        if session.fd < 0:
            return
        events = selectors.EVENT_READ | (selectors.EVENT_WRITE if session.pending else 0)
        try:
            self.selector.modify(session.fd, events, session)
        except (KeyError, ValueError, OSError):
            pass

    def queue_result(self, session, message, result=None, error=""):
        value = {"op": "result", "id": session.id, "action": message.get("action", ""),
                 "request_id": message.get("request_id", 0), "result": result or {}, "error": str(error)[:1000]}
        data = frame(value)
        if len(self.results) >= 32 or self.result_bytes + len(data) > 4 * 1024 * 1024:
            # Stop accepting commands until the client consumes its replies.
            raise ValueError("Terminal IPC response queue is full")
        self.results.append(data)
        self.result_bytes += len(data)

    def open(self, message):
        sid = message.get("id")
        if not isinstance(sid, str) or len(sid) != 32 or not sid.isalnum() or sid in self.sessions:
            return
        rows, cols = dimensions(message.get("rows", 24), message.get("cols", 80))
        session = Session(sid, rows, cols, str(message.get("label", "shell"))[:120],
                          str(message.get("kind", "local"))[:40], message.get("metadata", {}))
        self.sessions[sid] = session
        if sum(s.status != "closing" for s in self.sessions.values()) > MAX_SESSIONS:
            session.status, session.error = "error", "Terminal session limit reached (8)"
            return
        master = slave = -1
        try:
            from .terminal_emulator import TerminalEngine
            session.engine = TerminalEngine(rows, cols,
                history_limit=max(1, min(10000, int(message.get("history_lines", 2000)))),
                history_bytes=max(0, min(4 * 1024 * 1024, int(message.get("history_bytes", 4 * 1024 * 1024)))))
            argv = message.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v and "\0" not in v for v in argv):
                raise ValueError("Terminal command must be a nonempty argument list")
            env = os.environ.copy()
            updates = message.get("env") or {}
            if not isinstance(updates, dict) or not all(isinstance(k, str) and isinstance(v, str) and "\0" not in k + v and "=" not in k for k, v in updates.items()):
                raise ValueError("Invalid terminal environment")
            env.update(updates)
            env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "RAVEN_TERMINAL": "1"})
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.set_blocking(master, False)
            child = str(Path(__file__).with_name("terminal_child.py"))
            deadline = session.metadata.get("launch_deadline")
            if deadline is not None and time.monotonic() > float(deadline):
                raise ValueError("Allocation review expired before launch; review the current job again")
            process = subprocess.Popen([sys.executable, child, *argv], stdin=slave, stdout=slave, stderr=slave,
                                       cwd=message.get("cwd") or None, env=env, close_fds=True)
            session.process, session.fd, session.status = process, master, "running"
            master = -1
            self.selector.register(session.fd, selectors.EVENT_READ, session)
        except (ImportError, OSError, ValueError, TypeError, RuntimeError) as exc:
            session.status = "error"
            session.error = ("Embedded terminal needs its optional engine. From the Raven checkout, run: pipx install --force '.[terminal]'. " + str(exc)) if isinstance(exc, ImportError) else str(exc)
        finally:
            for fd in (master, slave):
                if fd >= 0:
                    os.close(fd)

    def handle(self, message):
        op = message.get("op")
        if op == "shutdown":
            self.begin_shutdown()
            return
        if self.shutting_down:
            return
        if op == "open":
            self.open(message)
            return
        session = self.sessions.get(message.get("id"))
        if session is None:
            return
        if op == "close":
            self.close(session)
        elif op == "resize" and session.status == "running":
            rows, cols = dimensions(message.get("rows"), message.get("cols"))
            if (rows, cols) != (session.rows, session.cols):
                session.rows, session.cols = rows, cols
                session.epoch += 1
                session.engine.resize(rows, cols)
                if session.fd >= 0:
                    fcntl.ioctl(session.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                if session.recorder:
                    self.record(session, "resize", cols, rows)
                session.dirty = True
        elif op == "input" and session.status == "running":
            event = message.get("event")
            if not isinstance(event, dict):
                return
            payload = event.get("text", event.get("data", ""))
            if isinstance(payload, str) and len(payload.encode("utf-8")) > MAX_EVENT:
                session.error = "Terminal input exceeds 1 MiB; input rejected"
                session.input_rejected += 1
            else:
                data = session.engine.encode(event)
                if not data:
                    return
                if len(session.pending) + len(data) > INPUT_LIMIT:
                    session.error = "Terminal input queue is full; this input was not accepted"
                    session.input_rejected += 1
                else:
                    session.pending.extend(data)
                    self.interest(session)
            session.dirty = True
        elif op == "request":
            self.request(session, message)

    def request(self, session, message):
        action, args = message.get("action"), message.get("args") or {}
        if not isinstance(args, dict):
            return
        try:
            if action == "rename":
                session.label = str(args.get("label", "shell"))[:120]
            elif action == "visible":
                session.visible = bool(args.get("visible", True))
            elif action == "snapshot":
                pass
            elif action == "cancel_input":
                cancelled = len(session.pending)
                session.pending.clear()
                self.interest(session)
                self.queue_result(session, message, {"cancelled_bytes": cancelled, "delivered_bytes": session.input_bytes,
                                                      "notice": "Delivered bytes cannot be retracted or executed commands undone."})
            elif action in ("history", "search"):
                token = args.get("snapshot_id")
                if token:
                    frozen = session.history_snapshot
                    if not frozen or frozen["id"] != token or time.monotonic() - frozen["created"] > 15:
                        raise ValueError("Terminal history snapshot expired; reload history")
                else:
                    history = session.engine.history_text() if session.engine else []
                    engine_state = session.engine.snapshot(include_history=False) if session.engine else {}
                    frozen = {"id": uuid.uuid4().hex, "created": time.monotonic(), "lines": history,
                              "wrapped": session.engine.history_wrapped() if session.engine else [],
                              "start": engine_state.get("history_start", 0)}
                    session.history_snapshot = frozen
                history = frozen["lines"]
                start = max(0, int(args.get("offset", 0)))
                limit = max(1, min(1000, int(args.get("limit", 200))))
                if action == "search":
                    query = str(args.get("query", ""))[:4096]
                    matches = [{"line": i, "text": line, "wrapped": bool(frozen["wrapped"][i])} for i, line in enumerate(history) if query and query.casefold() in line.casefold()]
                    self.queue_result(session, message, {"matches": matches[start:start + limit], "total": len(matches),
                        "history_start": frozen["start"], "snapshot_id": frozen["id"]})
                else:
                    self.queue_result(session, message, {"lines": history[start:start + limit], "offset": start, "total": len(history),
                        "wrapped": frozen["wrapped"][start:start + limit],
                        "history_start": frozen["start"], "snapshot_id": frozen["id"]})
            elif action == "clear_history":
                clear = getattr(session.engine, "clear_history", None)
                if not callable(clear):
                    raise ValueError("This terminal engine does not expose history clearing")
                clear()
            elif action == "record_start":
                if session.recorder:
                    raise ValueError("This session is already recording")
                if sum(not writer.closed for writer in self.recorders) >= MAX_SESSIONS:
                    raise ValueError("Recording writers are still flushing; wait before starting another")
                session.recorder = RecordingWriter(str(args["path"]), session.id, session.cols, session.rows)
                self.recorders.append(session.recorder)
                session.record_path = str(args["path"])
                self.queue_result(session, message, {"recording": True, "path": session.record_path})
            elif action == "record_stop":
                self.stop_recording(session)
                self.queue_result(session, message, {"recording": False, "path": session.record_path})
            elif action == "diagnostics":
                self.queue_result(session, message, {"pid": session.process.pid if session.process else None,
                    "output_bytes": session.output_bytes, "delivered_bytes": session.input_bytes, "input_pending": len(session.pending),
                    "input_rejected": session.input_rejected, "parse_seconds": session.parse_seconds, "broker_pid": os.getpid(),
                    "engine": type(session.engine).__name__, "epoch": session.epoch, "status": session.status})
            else:
                raise ValueError("Unsupported terminal action")
            session.dirty = True
        except (ImportError, OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            self.queue_result(session, message, error=str(exc))

    def record(self, session, kind, *args):
        try:
            if kind == "output":
                accepted = session.recorder.output(*args)
            else:
                accepted = session.recorder.resize(*args)
            if not accepted or session.recorder.closed:
                session.error = "Recording stopped: " + session.recorder.reason
                self.stop_recording(session)
        except (OSError, ValueError, RuntimeError) as exc:
            session.error = "Recording stopped: " + str(exc)
            self.stop_recording(session)

    def stop_recording(self, session):
        recorder, session.recorder = session.recorder, None
        if recorder:
            session.recording_stopped = recorder
            try:
                recorder.close()
            except (OSError, ValueError, RuntimeError) as exc:
                session.error = "Recording close failed: " + str(exc)
        session.dirty = True

    def read(self, session):
        if session.fd < 0:
            return
        try:
            data = os.read(session.fd, READ_SLICE)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            if exc.errno != errno.EIO:
                session.error = str(exc)
            data = b""
        if not data:
            self.close_fd(session)
            return
        session.output_bytes += len(data)
        if session.recorder:
            self.record(session, "output", data)
        started = time.monotonic()
        session.engine.feed(data)
        session.parse_seconds += time.monotonic() - started
        replies = session.engine.take_replies()
        if replies:
            if len(session.pending) + len(replies) > INPUT_LIMIT:
                session.error = "Terminal reply queue saturated"
                self.close(session)
            else:
                session.pending.extend(replies)
                self.interest(session)
        session.dirty = True

    def write(self, session):
        if session.fd < 0 or not session.pending:
            return
        try:
            count = os.write(session.fd, session.pending[:READ_SLICE])
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            session.error = str(exc)
            session.pending.clear()
            self.interest(session)
            return
        del session.pending[:count]
        session.input_bytes += count
        session.dirty = True
        self.interest(session)

    def close_fd(self, session):
        if session.fd >= 0:
            try:
                self.selector.unregister(session.fd)
            except (KeyError, ValueError, OSError):
                pass
            try:
                os.close(session.fd)
            except OSError:
                pass
            session.fd = -1
        if session.pending:
            session.error = f"Terminal closed with {len(session.pending)} input bytes undelivered"
        session.pending.clear()
        session.dirty = True

    def signal_owned(self, session, signum):
        process = session.process
        if process is None:
            return
        # Each launcher creates its own session.  Linux background jobs may use
        # different process groups; signal only groups proven to have that SID.
        groups = {process.pid} if process.poll() is None else set()
        if sys.platform.startswith("linux"):
            try:
                entries = os.scandir("/proc")
                with entries:
                    for entry in entries:
                        if not entry.name.isdigit():
                            continue
                        try:
                            fields = Path(entry.path, "stat").read_text().rsplit(")", 1)[1].split()
                            if int(fields[3]) == process.pid:
                                groups.add(int(fields[2]))
                        except (OSError, ValueError, IndexError):
                            continue
            except OSError:
                pass
        for group in groups:
            if group <= 1 or group == os.getpgrp():
                continue
            try:
                os.killpg(group, signum)
            except ProcessLookupError:
                pass
            except PermissionError:
                session.error = "Could not signal an owned terminal process group"
        if process.poll() is None:
            # Also handles a launcher that has not reached setsid yet.
            try:
                process.send_signal(signum)
            except ProcessLookupError:
                pass

    def close(self, session):
        if session.close_at:
            return
        session.status = "closing"
        session.close_at = time.monotonic()
        self.signal_owned(session, signal.SIGHUP)
        session.dirty = True
        if session.process is None:
            self.stop_recording(session)
            self.sessions.pop(session.id, None)

    def begin_shutdown(self):
        if not self.shutting_down:
            self.shutting_down = True
            self.shutdown_at = time.monotonic()
            for session in list(self.sessions.values()):
                self.close(session)

    def maintenance(self):
        now = time.monotonic()
        self.reap_adopted()
        self.recorders = [writer for writer in self.recorders if not writer.closed]
        for session in list(self.sessions.values()):
            if session.history_snapshot and now - session.history_snapshot["created"] > 15:
                session.history_snapshot = None
            if session.recorder and session.recorder.closed:
                if session.recorder.error or session.recorder.reason:
                    session.error = "Recording stopped: " + (session.recorder.error or session.recorder.reason)
                self.stop_recording(session)
            if session.recording_stopped and session.recording_stopped.closed:
                if session.recording_stopped.error:
                    session.error = "Recording stopped: " + session.recording_stopped.error
                session.recording_stopped = None
                session.dirty = True
            if session.process is None:
                continue
            code = session.process.poll()
            if code is not None and not session.exit_at:
                session.exit_at = now
                session.exit_code = code
            if session.close_at and now - session.close_at >= .25:
                self.signal_owned(session, signal.SIGKILL)
                self.close_fd(session)
                self.stop_recording(session)
                # Retain the Popen until reaped; never abandon a wait handle.
                if session.process.poll() is not None:
                    self.sessions.pop(session.id, None)
            elif session.exit_at and (session.fd < 0 or now - session.exit_at > .25):
                self.signal_owned(session, signal.SIGKILL)
                self.close_fd(session)
                self.stop_recording(session)
                session.status = "exited"
                # Process is already reaped; retain display with exit status.
                session.exit_at = 0
                session.process = None
                session.dirty = True
        if self.shutting_down and not self.sessions:
            self.running = False
        if self.shutting_down and now - self.shutdown_at > 1.25:
            self.running = False

    def reap_adopted(self):
        if not self.subreaper or not hasattr(os, "WNOWAIT"):
            return
        direct = {s.process.pid: s.process for s in self.sessions.values() if s.process is not None}
        for _ in range(64):
            try:
                child = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if child is None:
                    return
                if child.si_pid in direct:
                    # Popen must collect its own child to preserve exact status.
                    direct[child.si_pid].poll()
                else:
                    os.waitpid(child.si_pid, os.WNOHANG)
            except (ChildProcessError, ProcessLookupError, InterruptedError):
                return

    def publication(self, session):
        state = session.engine.snapshot(include_history=False) if session.engine else {"lines": [], "history": []}
        if session.engine:
            session.engine.consume_damage()
        session.seq += 1
        state.update({"id": session.id, "seq": session.seq, "epoch": session.epoch,
                      "rows": session.rows, "cols": session.cols, "label": session.label, "kind": session.kind,
                      "metadata": session.metadata, "host": socket.gethostname(), "user": getpass.getuser(),
                      "status": session.status, "error": session.error, "exit_code": session.exit_code,
                      "pid": session.process.pid if session.process else None, "output_bytes": session.output_bytes,
                      "input_pending": len(session.pending), "input_bytes": session.input_bytes,
                      "input_rejected": session.input_rejected, "recording": bool(session.recorder),
                      "recording_flushing": bool(session.recording_stopped and not session.recording_stopped.closed),
                      "recording_state": ("recording" if session.recorder.ready else "starting") if session.recorder else ("flushing" if session.recording_stopped and not session.recording_stopped.closed else "stopped"),
                      "record_path": session.record_path, "parse_seconds": session.parse_seconds})
        return {"op": "snapshot", "id": session.id, "state": state}

    def flush(self):
        if not self.out:
            value = None
            if self.results:
                data = self.results.popleft()
                self.result_bytes -= len(data)
                self.out = memoryview(data)
            else:
                now = time.monotonic()
                # Oldest eligible publication first prevents a noisy first
                # session from monopolizing a slow client's socket.
                eligible = [s for s in self.sessions.values() if s.dirty and now - s.last_publish >= (PUBLISH_INTERVAL if s.visible or s.status != "running" else 1.0)]
                if eligible:
                    session = min(eligible, key=lambda s: s.last_publish)
                    value = self.publication(session)
                    session.dirty, session.last_publish = False, now
            if value is not None:
                self.out = memoryview(frame(value))
        if self.out:
            try:
                sent = self.channel.send(self.out[:65536])
                self.out = self.out[sent:]
            except (BlockingIOError, InterruptedError):
                pass
            except OSError:
                self.begin_shutdown()
                self.out = memoryview(b"")
        try:
            self.selector.modify(self.channel, selectors.EVENT_READ | (selectors.EVENT_WRITE if self.out else 0), None)
        except (OSError, ValueError, KeyError):
            pass

    def receive(self):
        try:
            data = self.channel.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if data:
            self.decoder.feed(data)
        else:
            self.begin_shutdown()
            try:
                self.selector.unregister(self.channel)
            except (KeyError, ValueError, OSError):
                pass

    def run(self):
        try:
            while self.running:
                for message in self.decoder.take(32):
                    try:
                        self.handle(message)
                    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
                        session = self.sessions.get(message.get("id"))
                        if session:
                            session.error, session.dirty = str(exc)[:1000], True
                ready = self.selector.select(.01)
                # Control channel first, bounded PTY slice for every ready session.
                ready.sort(key=lambda pair: pair[0].data is not None)
                readable = []
                for key, events in ready:
                    session = key.data
                    if session is None:
                        if events & selectors.EVENT_READ:
                            self.receive()
                    else:
                        if events & selectors.EVENT_WRITE:
                            self.write(session)
                        if events & selectors.EVENT_READ:
                            readable.append(session)
                if readable:
                    start = self.read_turn % len(readable)
                    readable = readable[start:] + readable[:start]
                    parse_deadline = time.monotonic() + .012
                    consumed = 0
                    for session in readable:
                        self.read(session)
                        consumed += 1
                        if time.monotonic() >= parse_deadline:
                            break
                    self.read_turn = (start + consumed) % len(readable)
                self.maintenance()
                self.flush()
        except (OSError, ValueError, TypeError, KeyboardInterrupt):
            pass
        finally:
            for session in list(self.sessions.values()):
                self.signal_owned(session, signal.SIGKILL)
                self.close_fd(session)
                self.stop_recording(session)
                if session.process is not None:
                    try:
                        session.process.wait(timeout=.25)
                    except subprocess.TimeoutExpired:
                        pass
            deadline = time.monotonic() + .5
            for writer in self.recorders:
                writer.close()
                writer.thread.join(max(0, deadline - time.monotonic()))
            if self.subreaper:
                # SIGKILL completion and orphan adoption are asynchronous.  Give
                # owned grandchildren a short bounded drain before broker exit.
                deadline = time.monotonic() + .25
                while time.monotonic() < deadline:
                    self.reap_adopted()
                    time.sleep(.005)
            self.selector.close()
            self.channel.close()


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--fd", type=int, required=True)
    args = parser.parse_args(argv)
    broker = Broker(socket.socket(fileno=args.fd), adopt_children=True)
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, lambda *_args: broker.begin_shutdown())
    broker.run()


if __name__ == "__main__":
    main()
