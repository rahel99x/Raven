"""Private process mechanics for an explicitly owned Linux perf collector.

No resource limit in this module is applied to the scientific application. A
separate exec wrapper limits only perf and its offline decoder. The application
gate preserves the child's PID across exec and fails closed on parent loss.
"""

from __future__ import annotations

import json
import math
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

_GATE = """import json,os,sys
fd=int(sys.argv[1]); argv=json.loads(sys.argv[2]); go=os.read(fd,1); os.close(fd)
if go != b'1': raise SystemExit(125)
try: os.execvpe(argv[0],argv,os.environ)
except OSError as error:
 print('Raven application exec failed: '+str(error),file=sys.stderr); raise SystemExit(127)
"""
_LIMITED = """import os,resource,sys
resource.setrlimit(resource.RLIMIT_FSIZE,(int(sys.argv[1]),int(sys.argv[1])))
resource.setrlimit(resource.RLIMIT_CPU,(int(sys.argv[2]),int(sys.argv[2])))
resource.setrlimit(resource.RLIMIT_CORE,(0,0)); os.umask(0o077)
os.execv(sys.argv[3],sys.argv[3:])
"""


def gated_application(argv):
    """Return a direct child waiting for one byte and its release descriptor."""
    read_fd, write_fd = os.pipe()
    try:
        child = subprocess.Popen(
            [sys.executable, "-I", "-c", _GATE, str(read_fd), json.dumps(argv)],
            pass_fds=(read_fd,),
        )
    except BaseException:
        os.close(write_fd)
        raise
    finally:
        os.close(read_fd)
    return child, write_fd


def release(descriptor):
    try:
        os.write(descriptor, b"1")
    finally:
        os.close(descriptor)


def limited_tool(argv, *, max_bytes, cpu_seconds, stdout, pass_fds=()):
    environment = dict(os.environ, LC_ALL="C", LANG="C")
    child = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            _LIMITED,
            str(max_bytes),
            str(max(1, math.ceil(cpu_seconds))),
            *argv,
        ],
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=subprocess.PIPE,
        close_fds=True,
        pass_fds=pass_fds,
        env=environment,
    )
    os.set_blocking(child.stderr.fileno(), False)
    return child


class Collector:
    """An owned collector, bounded stderr, and explicit enable acknowledgement."""

    def __init__(self, executable, target, output, *, frequency, max_bytes, seconds):
        self.child = None
        self.control = self.ack = None
        self.error = ""
        self.diagnostics = bytearray()
        self.stderr_bytes = 0
        self.output = Path(output)
        self.max_bytes = max_bytes
        self.stopped = False
        control_read, control_write = os.pipe()
        ack_read, ack_write = os.pipe()
        try:
            self.child = limited_tool(
                [
                    executable,
                    "record",
                    "--quiet",
                    "--clockid",
                    "mono",
                    "--event",
                    "cpu-clock:u",
                    "--freq",
                    str(frequency),
                    "--call-graph",
                    "fp",
                    "--mmap-pages",
                    "8",
                    "--control",
                    f"fd:{control_read},{ack_write}",
                    "--delay=-1",
                    "--pid",
                    str(target.pid),
                    "--output",
                    str(output),
                ],
                max_bytes=max_bytes,
                cpu_seconds=seconds + 5,
                stdout=subprocess.DEVNULL,
                pass_fds=(control_read, ack_write),
            )
            self.control, self.ack = control_write, ack_read
            os.set_blocking(ack_read, False)
            os.write(control_write, b"enable\n")
        except BaseException:
            if self.control is None:
                os.close(control_write)
                os.close(ack_read)
            if self.child is not None:
                self.stop()
            raise
        finally:
            os.close(control_read)
            os.close(ack_write)

    def drain(self):
        if self.child is None or self.child.stderr is None:
            return
        for _ in range(16):
            try:
                chunk = os.read(self.child.stderr.fileno(), 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            self.stderr_bytes += len(chunk)
            self.diagnostics.extend(chunk)
            del self.diagnostics[:-8192]
        if self.stderr_bytes > 65536:
            self.error = "perf diagnostic output exceeded 64 KiB"

    def ready(self, target, *, timeout=3):
        deadline, received = time.monotonic() + timeout, bytearray()
        while time.monotonic() < deadline:
            self.drain()
            # Do not reap the target while perf may still be opening its PID.
            # A dead unreaped direct child reserves that PID until the collector
            # is stopped; poll()/waitpid here would create a PID-reuse race.
            try:
                exited = os.waitid(
                    os.P_PID, target.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                )
            except ChildProcessError:
                exited = True
            if self.error or self.child.poll() is not None or exited is not None:
                break
            if select.select([self.ack], [], [], 0.02)[0]:
                chunk = os.read(self.ack, 64)
                if not chunk:
                    break
                received.extend(chunk)
                if received == b"ack\n":
                    return True
                if len(received) >= 64 or b"\n" in received:
                    break
        self.error = (
            self.error
            or "perf did not acknowledge owned-process capture; tool/options/permissions may be unavailable"
        )
        return False

    def quota_reached(self):
        try:
            return self.output.stat().st_size >= self.max_bytes
        except FileNotFoundError:
            return False

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        if self.child is not None:
            if self.child.poll() is None:
                self.child.send_signal(signal.SIGINT)
            deadline = time.monotonic() + 2
            while self.child.poll() is None and time.monotonic() < deadline:
                self.drain()
                time.sleep(0.01)
            if self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.child.kill()
            self.child.wait()
            self.drain()
            self.child.stderr.close()
        for name in ("control", "ack"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)

    def diagnostic_text(self):
        from ..job_logs.text import display_text

        return display_text(bytes(self.diagnostics).decode("utf-8", "replace"))


def decode(executable, raw, destination, *, max_bytes, timeout=15):
    """Retain a bounded text export. Partial/error output remains inspectable."""
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    child, stderr, diagnostic_bytes = None, bytearray(), 0
    try:
        child = limited_tool(
            [
                executable,
                "script",
                "--ns",
                "--input",
                str(raw),
                "--fields",
                "comm,pid,tid,cpu,time,period,event,ip,sym,dso",
            ],
            max_bytes=max_bytes,
            cpu_seconds=timeout,
            stdout=descriptor,
        )
        deadline = time.monotonic() + timeout
        while True:
            for _ in range(16):
                try:
                    chunk = os.read(child.stderr.fileno(), 4096)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                diagnostic_bytes += len(chunk)
                stderr.extend(chunk)
                del stderr[:-8192]
            if diagnostic_bytes > 65536 or time.monotonic() >= deadline:
                raise ValueError("perf decoder exceeded its time/diagnostic budget")
            if child.poll() is not None:
                break
            time.sleep(0.01)
        if child.returncode != 0:
            raise ValueError(
                "perf script failed or reached its output quota: "
                + stderr.decode("utf-8", "replace")[-2000:]
            )
        if os.fstat(descriptor).st_size >= max_bytes:
            raise ValueError("perf-script output reached its byte quota")
    finally:
        os.close(descriptor)
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stderr.close()


def probe(executable, directory, *, frequency, max_bytes):
    """Qualify options, attach permission and decoding without executing science."""
    target = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time\nwhile True: time.sleep(.01)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    collector = None
    try:
        collector = Collector(
            executable,
            target,
            directory / "probe.data",
            frequency=frequency,
            max_bytes=max_bytes,
            seconds=2,
        )
        if not collector.ready(target):
            raise ValueError(collector.error + ": " + collector.diagnostic_text())
        time.sleep(0.03)
        collector.stop()
        if collector.child.returncode != 0:
            raise ValueError(
                "perf owned-process probe failed: " + collector.diagnostic_text()
            )
        decode(
            executable,
            directory / "probe.data",
            directory / "probe.txt",
            max_bytes=max_bytes,
        )
    finally:
        if collector is not None:
            collector.stop()
        if target.poll() is None:
            target.terminate()
        try:
            target.wait(timeout=3)
        except subprocess.TimeoutExpired:
            target.kill()
            target.wait()
        for name in ("probe.data", "probe.txt"):
            (directory / name).unlink(missing_ok=True)
