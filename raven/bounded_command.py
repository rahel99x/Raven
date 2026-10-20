"""Cancellable bounded command transport port.

Adapters implement ``inspect_command``; domain operations never inspect concrete
backend types or import scheduler/UI adapters to choose a transport.
"""

import os
import selectors
import signal
import subprocess
import time

from .transport_errors import CommandError


def clean(value, limit=4096):
    return "".join(c if c.isprintable() else " " for c in str(value)[:limit])


def _process(argv, timeout, limit, cancel=None):
    """Bound process output while it is produced and reap the whole group."""
    process = None
    buffers = {"out": bytearray(), "err": bytearray()}
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, "out"), (process.stderr, "err")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                if cancel is not None and cancel.is_set():
                    raise CommandError(
                        "Operation cancelled; command outcome can require reconciliation."
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CommandError(
                        "Command timed out; inspect current state before retrying an action."
                    )
                for key, _ in selector.select(min(0.05, remaining)):
                    chunk = os.read(key.fileobj.fileno(), min(16384, limit + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        if sum(map(len, buffers.values())) + len(chunk) > limit:
                            raise CommandError(
                                "Command output exceeded the inspection limit."
                            )
                        buffers[key.data].extend(chunk)
        reason = None
        while process.poll() is None:
            if cancel is not None and cancel.is_set():
                reason = "cancelled"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "timeout"
                break
            try:
                process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                pass
        if reason == "cancelled":
            raise CommandError(
                "Operation cancelled; command outcome can require reconciliation."
            )
        if reason == "timeout":
            raise CommandError(
                "Command timed out; inspect current state before retrying an action."
            )
        out = buffers["out"].decode("utf-8", "replace")
        if process.returncode:
            error = buffers["err"].decode("utf-8", "replace") or out
            raise CommandError(
                f"{argv[0]} exit {process.returncode}: " + clean(error, 400)
            )
        return out
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandError(clean(exc)) from exc
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait(timeout=2)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def execute(backend, argv, timeout, limit, cancel=None, depth=0):
    if depth > 8:
        raise ValueError("The connection wrapper depth exceeds its bound.")
    if cancel is not None and cancel.is_set():
        raise CommandError("Operation cancelled before command execution.")
    inspect = getattr(backend, "inspect_command", None)
    if callable(inspect):
        return inspect(argv, timeout, limit, cancel, depth=depth)
    # Injected/simulated adapters retain their transport contract. The result is
    # still checked before publication; production ports bound while reading.
    out = backend.run(argv, timeout)[0]
    if not isinstance(out, str) or len(out.encode("utf-8")) > limit:
        raise CommandError("Command output exceeded the inspection limit.")
    return out
