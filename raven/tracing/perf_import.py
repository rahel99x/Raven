"""Bounded import of textual Linux perf-script stack samples.

A sample is one observed stack, not an instrumented function interval. Imported
process IDs are producer-declared and do not replace the importing process's
recording ownership identity.
"""

from __future__ import annotations

import hashlib
import os
import re
from decimal import Decimal, InvalidOperation

_HEADER = re.compile(
    r"^\s*(?P<comm>.+?)\s+(?P<pid>\d+)(?:/(?P<tid>\d+)|\s+(?P<tid_space>\d+))?\s+\[(?P<cpu>\d+)\]\s+(?P<time>\d+\.\d{1,12}):\s+(?:(?P<period>\d+)\s+)?(?P<event>\S+):\s*(?P<frame>.*)$"
)
_FRAME = re.compile(
    r"^\s*(?P<ip>(?:0x)?[0-9a-fA-F]+)\s+(?P<name>.*?)\s+\((?P<dso>[^\r\n]*)\)\s*$"
)
_MAX_LINE = 16384
_MAX_SAMPLE_DATA = 48 << 10
_MAX_FRAMES = 2048


def _timestamp(value):
    try:
        exact = Decimal(value) * 1_000_000_000
    except InvalidOperation as exc:
        raise ValueError("Invalid perf sample timestamp") from exc
    if (
        not exact.is_finite()
        or exact != exact.to_integral_value()
        or not 0 <= exact < 1 << 64
    ):
        raise ValueError(
            "Perf sample time must be exact nonnegative integer nanoseconds"
        )
    return int(exact)


def _frame(line):
    found = _FRAME.match(line)
    if found is None:
        return None
    values = found.groupdict()
    name, dso = values["name"], values["dso"]
    if len(name) > 8192 or len(dso) > 8192:
        return dict(
            name="unresolved oversized symbol",
            ip=values["ip"],
            symbol_unavailable="Symbol/DSO exceeds recording field limit",
            symbol_sha256=hashlib.sha256(line.encode()).hexdigest(),
        )
    return dict(name=name or "unresolved frame", ip=values["ip"], dso=dso)


def samples(lines, *, max_samples=100000):
    """Yield bounded samples/gaps from `perf script --ns -F ...` text lines."""
    from .protocol import encoded

    if type(max_samples) is not int or not 1 <= max_samples <= 1_000_000:
        raise ValueError("Invalid perf sample-count budget")
    current, count, stack_bytes, previous = None, 0, 0, -1

    def finish():
        nonlocal current, count
        if current is None:
            return None
        current.pop("_header_frame_pending", None)
        current["frames"] = list(reversed(current["frames"]))
        current["stack_order"] = "root-to-leaf"
        current["stack_status"] = (
            "partial"
            if current.get("stack_truncated")
            else "observed"
            if current["frames"]
            else "unavailable"
        )
        count += 1
        result, current = current, None
        return result

    for raw in lines:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_LINE:
            raise ValueError("Perf-script line exceeds its 16 KiB bound")
        line = raw.rstrip("\r\n")
        if not line.strip():
            result = finish()
            if result is not None:
                yield result
            continue
        if line.lstrip().startswith("#"):
            continue
        header = _HEADER.match(line)
        if header is not None:
            result = finish()
            if result is not None:
                yield result
            if count >= max_samples:
                raise ValueError("Perf-script sample count exceeds its explicit budget")
            fields = header.groupdict()
            stamp = _timestamp(fields["time"])
            if stamp < previous:
                raise ValueError(
                    "Perf-script samples are not in monotonic report order"
                )
            previous = stamp
            pid = int(fields["pid"])
            tid = int(fields["tid"] or fields["tid_space"] or fields["pid"])
            cpu = int(fields["cpu"])
            if not 0 < pid < 1 << 31 or not 0 < tid < 1 << 31 or not 0 <= cpu < 1 << 31:
                raise ValueError("Invalid perf process/thread/CPU identifier")
            period = int(fields["period"]) if fields["period"] else None
            if period is not None and not 0 < period < 1 << 63:
                raise ValueError("Invalid perf sample period")
            if len(fields["event"]) > 256:
                raise ValueError("Perf event name exceeds its field budget")
            current = dict(
                sample_time_ns=stamp,
                source_pid=pid,
                source_tid=tid,
                logical_cpu=cpu,
                comm=fields["comm"][:256],
                event=fields["event"],
                frames=[],
                weight=period or 1,
                weight_unit="ns"
                if period
                and fields["event"].split(":")[0] in {"cpu-clock", "task-clock"}
                else "event-count"
                if period
                else "samples",
                fidelity="sampled stacks; exact function durations unavailable",
            )
            stack_bytes = 0
            frame = _frame(fields["frame"])
            if frame is not None:
                current["frames"].append(frame)
                stack_bytes = len(encoded(frame))
                current["_header_frame_pending"] = True
            elif fields["frame"].strip():
                current["stack_truncated"] = True
            continue
        if "PERF_RECORD_LOST" in line or re.search(
            r"\blost\s+\d+\s+events\b", line, re.I
        ):
            result = finish()
            if result is not None:
                yield result
            yield dict(
                kind="gap",
                sample_time_ns=max(0, previous),
                reason="perf reported lost records",
                detail=line[:1024],
            )
            continue
        frame = _frame(line)
        if current is not None and frame is not None:
            if (
                current.pop("_header_frame_pending", False)
                and current["frames"]
                and frame == current["frames"][0]
            ):
                continue
            size = len(encoded(frame))
            if (
                len(current["frames"]) >= _MAX_FRAMES
                or stack_bytes + size > _MAX_SAMPLE_DATA
            ):
                current["stack_truncated"] = True
            else:
                current["frames"].append(frame)
                stack_bytes += size
            continue
        # Unknown text is not silently accepted as a complete stack format.
        if current is not None:
            current["stack_truncated"] = True
        yield dict(
            kind="gap",
            sample_time_ns=max(0, previous),
            reason="Unrecognized perf-script record",
            detail=line[:1024],
        )
    result = finish()
    if result is not None:
        yield result


def import_perf_script(
    input_path,
    *,
    project,
    job_id,
    attempt,
    cluster="local",
    node=None,
    max_bytes=32 << 20,
    max_samples=100000,
    output_bytes=64 << 20,
    complete=True,
    clock=None,
):
    """Import a private verified journal; raw report clocks stay report-local by default."""
    from .capture import Recorder, register_trace
    from .protocol import regular_open, text
    from .workspace_model_clocks import source_clock

    if (
        type(max_bytes) is not int
        or not 4096 <= max_bytes <= 256 << 20
        or type(output_bytes) is not int
        or not 4096 <= output_bytes <= 256 << 20
    ):
        raise ValueError("Invalid perf import byte budget")
    if type(complete) is not bool:
        raise ValueError("Perf input completeness must be explicit")
    if type(max_samples) is not int or not 1 <= max_samples <= 1_000_000:
        raise ValueError("Invalid perf sample-count budget")
    if isinstance(job_id, bool) or not isinstance(job_id, (str, int)):
        raise ValueError("An explicit perf import job ID is required")
    job_id = text(str(job_id), "job ID", 256)
    attempt = text(attempt, "attempt", 256)
    cluster = text(cluster, "cluster", 256)
    if node is not None:
        node = text(node, "source node", 256)
    if clock is not None:
        source_clock({"stream": "validate-clock", "cluster": cluster}, clock)
    fd = regular_open(input_path)
    count, gaps, partial_stacks = 0, 0, 0
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if before.st_size > max_bytes:
            raise ValueError("Perf-script report exceeds its byte budget")
        digest, size = hashlib.sha256(), 0
        while True:
            chunk = source.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("Perf-script report exceeds its byte budget")
            digest.update(chunk)
        report_sha = digest.hexdigest()
        source.seek(0)
        coverage = dict(
            adapter="linux-perf-script",
            fidelity="sampled stacks",
            scope="Producer-reported samples from this imported report; no exact per-call durations",
            format="perf-script-text",
            input_sha256=report_sha,
            input_complete=complete,
            clock=clock
            if clock is not None
            else dict(
                schema="raven.trace-clock/v1",
                kind="producer-report",
                id="perf-script",
                report_sha256=report_sha,
            ),
            qualification="Imported evidence; live capture qualification is separate",
        )
        recorder = Recorder(
            project,
            job_id=job_id,
            attempt=attempt,
            cluster=cluster,
            coverage=coverage,
            max_bytes=output_bytes,
            background_flush=False,
            journal_format="blocks",
            initial_mono_ns=0,
            stream_id="perf_"
            + hashlib.sha256(
                repr((report_sha, job_id, attempt, cluster, node)).encode()
            ).hexdigest()[:32],
            register=False,
            identity_extra={
                "launcher": "raven trace perf-import",
                "role": "imported-samples",
            },
        )
        observed, observed_size, last_stamp = hashlib.sha256(), 0, 0

        def lines():
            nonlocal observed_size
            while True:
                raw = source.readline(_MAX_LINE + 1)
                if not raw:
                    break
                observed_size += len(raw)
                if observed_size > max_bytes:
                    raise ValueError("Perf-script report exceeds its byte budget")
                observed.update(raw)
                try:
                    yield raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError("Perf-script text must be UTF-8") from exc

        try:
            for value in samples(lines(), max_samples=max_samples):
                stamp = value["sample_time_ns"]
                last_stamp = max(last_stamp, stamp)
                if value.get("kind") == "gap":
                    gaps += 1
                    emitted = recorder.emit(
                        "gap",
                        {
                            key: item
                            for key, item in value.items()
                            if key not in {"kind", "sample_time_ns"}
                        },
                        mono_ns=stamp,
                    )
                else:
                    count += 1
                    partial_stacks += value["stack_status"] != "observed"
                    value.update(
                        source_node=node or "unknown",
                        identity_fidelity="producer-declared report process/thread IDs",
                        provenance={
                            "format": "perf-script-text",
                            "input_sha256": report_sha,
                        },
                    )
                    emitted = recorder.emit(
                        "cpu_sample",
                        value,
                        thread=f"{value['source_pid']}/{value['source_tid']}",
                        mono_ns=stamp,
                    )
                if not emitted:
                    raise ValueError(
                        "Perf sample journal reached its recording/storage budget"
                    )
            after = os.fstat(source.fileno())
            if observed.hexdigest() != report_sha or before.st_size != after.st_size:
                raise ValueError("Perf-script report changed during import")
            if not count:
                raise ValueError("Perf-script report has no supported stack samples")
            if not recorder.emit(
                "stream_end", {"reason": "imported report end"}, mono_ns=last_stamp
            ):
                raise ValueError(
                    "Perf sample journal reached its recording/storage budget"
                )
            if gaps or partial_stacks or not complete:
                recorder._fail(
                    "PARTIAL",
                    "Samples retain explicit unknown/lost stack evidence or an incomplete perf capture",
                )
            else:
                recorder.state = "CLOSED"
        except BaseException:
            recorder._fail(
                "ERROR", "Perf-script import failed; no complete capture is claimed"
            )
            raise
        finally:
            recorder.close()
    registration_error = ""
    try:
        register_trace(project, recorder.path)
    except (OSError, ValueError) as exc:
        registration_error = type(exc).__name__ + ": " + str(exc)[:1500]
    return dict(
        schema="raven.perf-import/v1",
        path=str(recorder.path),
        manifest=str(recorder.path),
        input_sha256=report_sha,
        samples=count,
        gaps=gaps,
        partial_stacks=partial_stacks,
        status="PARTIAL" if gaps or partial_stacks or not complete else "COMPLETE",
        registered=not registration_error,
        registration_error=registration_error,
        registration_argv=[
            "raven",
            "trace",
            "register",
            "--project",
            str(project),
            str(recorder.path),
        ]
        if registration_error
        else [],
        fidelity="sampled stacks; exact function durations unavailable",
        limits=[
            "A sample is an observation, not continuous execution between samples.",
            "Imported PID/TID/CPU labels are producer-declared; recording ownership remains the importer.",
            "Native symbols and stack unwinding depend on the report and application build.",
        ],
    )
