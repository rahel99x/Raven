"""Opt-in statistical stacks for a newly launched, user-owned Linux child.

This adapter does not attach to arbitrary PIDs, replace Slurm, require root, or
infer call durations from samples. An unavailable optional collector executes
the requested command exactly once with normal environment and resource limits.
"""

from __future__ import annotations

import math
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import perf_capture_process as processes


def _find_perf():
    return shutil.which("perf") if sys.platform.startswith("linux") else None


def capabilities():
    present = _find_perf()
    return {
        "id": "linux-perf-owned-samples",
        "implemented": True,
        "status": "prerequisite-present" if present else "unavailable",
        "qualified": False,
        "scope": "Statistical frame-pointer user stacks of a newly launched child and inherited descendants; not exact call durations or CPU residency",
        "prerequisites": "Linux perf with control-fd acknowledgement, cpu-clock:u event and local owned-process permissions",
        "executable": present,
        "live": "A bounded window is published when the collector stops, including while the application continues; samples are not streamed individually",
    }


class _Receipt:
    def __init__(self, project, identity, *, frequency, max_bytes, seconds, required):
        from .capture import _safe_directory, _trace_root

        self.directory = self.path = None
        self.value = {
            "schema": "raven.perf-capture/v1",
            "adapter": "linux-perf-owned-samples",
            "status": "preparing",
            "identity": identity,
            "required": required,
            "application_status": "not-started",
            "application_exit": None,
            "application_launches": 0,
            "frequency_hz": frequency,
            "capture_limit_seconds": seconds,
            "raw_limit_bytes": max_bytes,
            "text_limit_bytes": max_bytes,
            "journal_limit_bytes": max_bytes,
            "collector_ring_pages": 8,
            "capture_qualified": False,
            "errors": [],
            "receipt": None,
            "fidelity": "Statistical frame-pointer user stacks; no exact per-call timing, CPU residency or off-CPU inference",
            "scope": "The newly owned child lifetime through its completion or the explicit capture limit; inherited descendants may contribute samples only within that window",
            "bootstrap_scope": "Samples can include the short trusted launch gate before exec; recorded image/symbol evidence determines which code was observed",
            "delivery": "The bounded sample window is imported when the collector stops; later application execution is outside that window",
        }
        try:
            parent = _trace_root(project).parent / "perf-captures"
            parent.mkdir(mode=0o700, exist_ok=True)
            _safe_directory(parent)
            self.directory = parent / uuid.uuid4().hex
            self.directory.mkdir(mode=0o700)
            self.path = self.directory / "receipt.json"
            self.value["receipt"] = str(self.path)
            self.publish(strict=True)
        except OSError as error:
            self.value.update(status="unavailable", receipt=None)
            self.error(
                "Capture storage unavailable; receipt could not be persisted: "
                + str(error)
            )
            self.path = None

    def error(self, error):
        from ..job_logs.text import display_text

        if len(self.value["errors"]) < 16:
            self.value["errors"].append(display_text(str(error))[:4000])

    def publish(self, *, strict=False):
        from ..reporting import _atomic_json

        if self.path is None:
            return
        try:
            _atomic_json(self.path, self.value, replace=True)
        except (OSError, ValueError) as error:
            if strict:
                raise
            self.value["receipt_write_error"] = str(error)[:1000]

    def finish(self, application):
        code = application.returncode
        self.value.update(
            application_status="completed",
            application_returncode=code,
            application_exit=code if code >= 0 else 128 - code,
        )


def _validate(
    argv, *, job_id, attempt, cluster, frequency, max_bytes, seconds, required
):
    from .protocol import text

    if not isinstance(argv, (tuple, list)) or not 1 <= len(argv) <= 1024:
        raise ValueError("Sampling requires a bounded command argument list")
    if any(
        not isinstance(value, str) or "\x00" in value or len(value) > 16384
        for value in argv
    ):
        raise ValueError("Invalid sampling command arguments")
    if not argv[0] or sum(len(value.encode("utf-8")) + 1 for value in argv) > 32768:
        raise ValueError("Sampling command exceeds its 32 KiB argument budget")
    if isinstance(job_id, bool) or not isinstance(job_id, (str, int)):
        raise ValueError("An explicit sampling job ID is required")  # noqa: TRY004 - configuration errors share the CLI's ValueError path
    text(str(job_id), "job ID", 256)
    text(attempt, "attempt", 256)
    text(cluster, "cluster", 256)
    if type(frequency) is not int or not 1 <= frequency <= 10000:
        raise ValueError("Sampling frequency must be 1 through 10,000 Hz")
    if type(max_bytes) is not int or not 4096 <= max_bytes <= 128 << 20:
        raise ValueError("Sampling artifact quota must be 4 KiB through 128 MiB")
    if (
        type(seconds) not in (float, int)
        or not math.isfinite(seconds)
        or not 0.1 <= seconds <= 3600
    ):
        raise ValueError("Sampling window must be 0.1 through 3600 seconds")
    if type(required) is not bool:
        raise ValueError("Required sampling policy must be boolean")


def _run_unrecorded(argv, receipt):
    """Only called before any application has been released or executed."""
    receipt.value.update(application_status="starting", application_launches=1)
    receipt.publish()
    try:
        application = subprocess.Popen(argv)
    except OSError as error:
        receipt.error("Application launch failed: " + str(error))
        receipt.value.update(application_status="launch-failed", application_exit=127)
        receipt.publish()
        return
    receipt.value["application_pid"] = application.pid
    try:
        application.wait()
    finally:
        if application.poll() is not None:
            receipt.finish(application)
        else:
            receipt.value["application_status"] = "running-after-viewer-interruption"
        receipt.publish()


def _import(executable, receipt, *, project, max_bytes, complete):
    from .perf_import import import_perf_script

    raw = receipt.directory / "samples.data"
    report = receipt.directory / "samples.txt"
    receipt.value.update(raw_report=str(raw), text_report=str(report))
    processes.decode(executable, raw, report, max_bytes=max_bytes)
    receipt.value["recording"] = import_perf_script(
        report,
        project=project,
        max_bytes=max_bytes,
        output_bytes=max_bytes,
        complete=complete,
        clock={
            "schema": "raven.trace-clock/v1",
            "kind": "monotonic",
            "id": "monotonic",
        },
        **receipt.value["identity"],
    )


def _record(
    argv, executable, receipt, *, project, frequency, max_bytes, seconds, required
):
    application, gate = processes.gated_application(argv)
    collector, released = None, False
    receipt.value["application_pid"] = application.pid
    try:
        try:
            collector = processes.Collector(
                executable,
                application,
                receipt.directory / "samples.data",
                frequency=frequency,
                max_bytes=max_bytes,
                seconds=seconds,
            )
            ready = collector.ready(application)
            if not ready:
                receipt.error(collector.error + ": " + collector.diagnostic_text())
        except (OSError, ValueError) as error:
            receipt.error(error)
            ready = False
        if not ready:
            receipt.value["status"] = "unavailable"
            if collector is not None:
                collector.stop()
            if required:
                receipt.value["application_exit"] = 2
                return
        # From this point there is no retry or second execution of argv.
        receipt.value.update(application_status="running", application_launches=1)
        receipt.value["status"] = "recording" if ready else "unavailable"
        receipt.publish()
        descriptor, gate = gate, None
        processes.release(descriptor)
        released = True
        started, reason = time.monotonic(), "application-completed"
        published = False
        while application.poll() is None:
            if collector is not None and not collector.stopped:
                collector.drain()
                if collector.quota_reached():
                    reason = "raw-byte-quota"
                elif collector.error:
                    reason = "collector-error"
                elif collector.child.poll() is not None:
                    reason = "collector-exited"
                elif time.monotonic() - started >= seconds:
                    reason = "capture-duration"
                else:
                    time.sleep(0.02)
                    continue
                collector.stop()
                _publish_capture(
                    executable,
                    collector,
                    receipt,
                    project=project,
                    max_bytes=max_bytes,
                    reason=reason,
                )
                published = True
                receipt.publish()
            time.sleep(0.02)
        if collector is not None:
            collector.stop()
        receipt.finish(application)
        if receipt.value["status"] == "unavailable":
            return
        if not published:
            _publish_capture(
                executable,
                collector,
                receipt,
                project=project,
                max_bytes=max_bytes,
                reason=reason,
            )
    finally:
        if collector is not None:
            collector.stop()
        if gate is not None:
            os.close(gate)  # EOF exits the gate without invoking scientific argv.
        if not released:
            application.wait(timeout=3)
        elif application.poll() is None:
            receipt.value["application_status"] = "running-after-viewer-interruption"
            receipt.value["status"] = "partial"
        receipt.publish()


def _publish_capture(executable, collector, receipt, *, project, max_bytes, reason):
    receipt.value.update(
        stop_reason=reason,
        collector_returncode=collector.child.returncode,
        collector_diagnostics=collector.diagnostic_text(),
    )
    complete = reason == "application-completed" and collector.child.returncode == 0
    receipt.value["status"] = "complete" if complete else "partial"
    try:
        _import(
            executable, receipt, project=project, max_bytes=max_bytes, complete=complete
        )
    except (OSError, ValueError) as error:
        receipt.value["status"] = "partial"
        receipt.error(
            "Sample export/import unavailable; raw artifacts retained: " + str(error)
        )


def sample_command(
    argv,
    *,
    project,
    job_id,
    attempt,
    cluster="local",
    frequency=99,
    max_bytes=64 << 20,
    capture_seconds=30,
    required=False,
):
    """Run once, optionally sample, and return a durable private capture receipt.

    ``required`` prevents launch when the preflight/attachment is unavailable;
    capture errors after launch never rerun or terminate the scientific job.
    """
    _validate(
        argv,
        job_id=job_id,
        attempt=attempt,
        cluster=cluster,
        frequency=frequency,
        max_bytes=max_bytes,
        seconds=capture_seconds,
        required=required,
    )
    receipt = _Receipt(
        Path(project).absolute(),
        {
            "job_id": str(job_id),
            "attempt": attempt,
            "cluster": cluster,
            "node": socket.gethostname(),
        },
        frequency=frequency,
        max_bytes=max_bytes,
        seconds=capture_seconds,
        required=required,
    )
    executable = _find_perf()
    try:
        if receipt.path is None:
            raise ValueError("Sampling requires writable private artifact storage")
        if executable is None:
            raise ValueError(
                "Linux perf is not installed in this execution environment"
            )
        executable = str(Path(executable).resolve(strict=True))
        processes.probe(
            executable,
            receipt.directory,
            frequency=frequency,
            max_bytes=min(max_bytes, 2 << 20),
        )
    except (OSError, ValueError) as error:
        receipt.error(error)
        receipt.value["status"] = "unavailable"
        if required:
            receipt.value["application_exit"] = 2
            receipt.publish()
        else:
            _run_unrecorded(argv, receipt)
        return receipt.value
    receipt.value["capture_prerequisites_verified"] = True
    _record(
        argv,
        executable,
        receipt,
        project=project,
        frequency=frequency,
        max_bytes=max_bytes,
        seconds=capture_seconds,
        required=required,
    )
    return receipt.value
