"""Opt-in fixed probes of the verified Heavy worker, never arbitrary job commands."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import stat
import tempfile
import time

SCHEMA = "raven.allocation-health/v1"
MAX_BYTES = 16 << 20


def identity(value):
    if (not isinstance(value, dict) or value.get("verified") is not True
            or value.get("site_kind") not in {"desktop", "slurm"}
            or type(value.get("uid")) is not int or value.get("uid") != os.getuid()
            or not isinstance(value.get("host"), str) or not value.get("host")
            or type(value.get("pid")) is not int or value["pid"] <= 0
            or not value.get("verification")):
        raise ValueError("Health probes require the authenticated Heavy worker identity")
    if value["site_kind"] == "slurm" and any(not value.get(key) for key in ("job_id", "step_id", "submit_time", "start_time")):
        raise ValueError("Health probes require a verified allocation and step")
    return json.loads(json.dumps(value, allow_nan=False))


def settings(params):
    selected = params.get("tests", "memory,scratch")
    selected = selected.split(",") if isinstance(selected, str) else selected
    if not isinstance(selected, list) or not selected or len(selected) > 3 or len(set(selected)) != len(selected):
        raise ValueError("Select memory, scratch, MPI, or their combination")
    if set(selected) - {"memory", "scratch", "mpi"}:
        raise ValueError("GPU probes require a separately qualified allocation adapter; no generic command runs")
    values = {}
    for key, default, low, high, integer in (("bytes", 1 << 20, 4096, MAX_BYTES, True),
            ("repeats", 3, 2, 8, True), ("warmup", 1, 0, 2, True),
            ("seconds", 10, .1, 30, False)):
        raw = params.get(key, default)
        if isinstance(raw, bool):
            raise ValueError("Invalid health probe " + key)
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid health probe " + key) from exc
        if not math.isfinite(value) or not low <= value <= high or integer and not value.is_integer():
            raise ValueError("Health probe " + key + " exceeds its bounded range")
        values[key] = int(value) if integer else value
    if "mpi" in selected and values["bytes"] > 1 << 20:
        raise ValueError("MPI messages are limited to 1 MiB per rank")
    values["tests"] = selected
    # Per-file peak and aggregate read+write bytes are explicit review data.
    values["peak_scratch_bytes"] = values["bytes"] if "scratch" in selected else 0
    values["maximum_io_bytes"] = 2 * values["bytes"] * (values["warmup"] + values["repeats"]) if "scratch" in selected else 0
    values["maximum_buffer_bytes"] = (4 if "mpi" in selected else 2) * values["bytes"]
    values["anonymous_mpi_compile_bytes"] = 3 * 65536 if "mpi" in selected else 0
    return values


def scratch_identity(path):
    path = Path(path).expanduser().absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Scratch directory cannot contain symlinks")
    status = path.stat()
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid() or status.st_mode & 0o022:
        raise ValueError("Choose an existing scratch directory owned by you and not writable by others")
    return {"path": str(path), "device": status.st_dev, "inode": status.st_ino}


def hardware():
    model = "unknown"
    try:
        with open("/proc/cpuinfo") as stream:
            cpuinfo = stream.read(65536)
        for line in cpuinfo.splitlines():
            if line.startswith("model name"):
                model = line.partition(":")[2].strip()
                break
    except OSError:
        pass
    try:
        affinity = sorted(os.sched_getaffinity(0))
    except AttributeError:
        affinity = []
    topology = []
    for cpu in affinity[:256]:
        placement = {"cpu": cpu}
        for key in ("physical_package_id", "core_id"):
            try:
                with open(f"/sys/devices/system/cpu/cpu{cpu}/topology/{key}") as stream:
                    placement[key] = int(stream.read(32).strip())
            except (OSError, ValueError):
                placement[key] = None
        topology.append(placement)
    return {"machine": platform.machine(), "cpu_model": model, "cpu_topology": topology,
            "logical_cpus": os.cpu_count(), "allowed_cpus": affinity[:256],
            "affinity_truncated": len(affinity) > 256,
            "kernel": platform.release(), "python": platform.python_version(),
            "libc": list(platform.libc_ver()), "implementation": "Python bytearray copy; cached scratch write/fsync/read"}


def cohort(receipt):
    keys = {"hardware": receipt["hardware"], "tests": receipt["settings"],
            "scratch_device": receipt.get("scratch", {}).get("device"),
            "site_kind": receipt["execution"]["site_kind"],
            # A local st_dev number cannot establish filesystem identity across
            # hosts. Memory-only cohorts can compare matching nodes; scratch
            # remains host-local until filesystem/device identity is qualified.
            "scratch_host": receipt["execution"]["host"] if "scratch" in receipt["settings"]["tests"] else None,
            "mpi_toolchain": receipt.get("mpi", {}).get("toolchain"),
            "mpi_library": receipt.get("mpi", {}).get("library_version")}
    return hashlib.sha256(json.dumps(keys, sort_keys=True).encode()).hexdigest()


def compare(receipt, baseline):
    if not baseline:
        return {"status": "no-baseline", "reason": "A single observation cannot identify unusual performance"}
    if not isinstance(baseline, dict) or baseline.get("schema") != SCHEMA or baseline.get("cohort") != receipt["cohort"]:
        return {"status": "incomparable", "reason": "Hardware, placement, settings, libraries or scratch cohort differs"}
    changes = []
    for name, summary in receipt["summary"].items():
        summaries = baseline.get("summary", {})
        old = summaries.get(name, {}) if isinstance(summaries, dict) else {}
        if not isinstance(old, dict):
            continue
        previous = old.get("median_seconds")
        if not isinstance(previous, (int, float)) or isinstance(previous, bool) or previous <= 0 or not math.isfinite(previous):
            continue
        spread = old.get("relative_range")
        stable = (isinstance(spread, (int, float)) and not isinstance(spread, bool)
                  and 0 <= spread <= .25 and summary["relative_range"] <= .25
                  and summary["count"] >= 2 and old.get("count", 0) == receipt["settings"]["repeats"])
        ratio = summary["median_seconds"] / previous
        changes.append({"test": name, "duration_ratio": ratio, "repeatable": stable,
                        "unusual": stable and ratio > 1.5})
    return {"status": "compared" if changes else "insufficient", "comparisons": changes,
            "reason": "Descriptive same-cohort comparison; interference and caching can vary; no causal node diagnosis"}


def probe(config, execution, scratch=None, *, cancel=None, baseline=None, clock=time.monotonic, mpi=None):
    execution = identity(execution)
    config = settings(config)
    started = clock()
    deadline = started + config["seconds"]
    receipt = {"schema": SCHEMA, "execution": execution, "hardware": hardware(),
               "settings": config, "scratch": scratch or {}, "samples": [], "summary": {},
               "started_unix_seconds": time.time(), "status": "completed", "interference":
               "This diagnostic consumes resources in Raven's Heavy allocation; it neither runs nor cancels the scientific workload",
               "uncertainty": ["Scratch reads may use the page cache; fsync does not measure distributed durability",
                               "Small Python probes are local fingerprints, not peak bandwidth or health ground truth",
                               "Kernel I/O stalls can delay process termination beyond the requested deadline"]}

    def checkpoint():
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Health probe cancelled")
        if clock() >= deadline:
            raise TimeoutError("Health probe time budget exhausted")

    try:
        checkpoint()
        if "mpi" in config["tests"]:
            from . import mpi_health
            if not mpi:
                raise ValueError("MPI requires a reviewed fixed toolchain")
            rows, receipt["mpi"] = mpi_health.probe(config, execution, mpi, cancel=cancel, deadline=deadline)
            receipt["samples"].extend(rows)
            checkpoint()
        if "scratch" in config["tests"]:
            if not scratch or scratch_identity(scratch["path"]) != scratch:
                raise ValueError("Reviewed scratch directory changed")
            directory = os.open(scratch["path"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            current = os.fstat(directory)
            if (current.st_dev, current.st_ino) != (scratch["device"], scratch["inode"]):
                os.close(directory)
                raise ValueError("Reviewed scratch identity changed while opening")
        else:
            directory = None
        try:
            source = bytearray(b"r" * config["bytes"])
            target = bytearray(config["bytes"])
            for name in config["tests"]:
                if name == "mpi":
                    continue
                for index in range(config["warmup"] + config["repeats"]):
                    checkpoint()
                    begin = clock()
                    if name == "memory":
                        target[:] = source
                        valid = target == source
                    else:
                        # Linux anonymous file: close/SIGKILL removes storage without
                        # relying on a finally block or a vulnerable named path.
                        with tempfile.TemporaryFile(dir=f"/proc/self/fd/{directory}") as stream:
                            stream.write(source)
                            stream.flush()
                            os.fsync(stream.fileno())
                            stream.seek(0)
                            valid = stream.readinto(target) == config["bytes"] and target == source
                    elapsed = clock() - begin
                    if not valid:
                        raise ValueError("Probe data verification failed")
                    receipt["samples"].append({"test": name, "repeat": index,
                        "warmup": index < config["warmup"], "seconds": elapsed,
                        "bytes": config["bytes"], "verified": True})
                    checkpoint()
        finally:
            if directory is not None:
                os.close(directory)
    except (InterruptedError, TimeoutError, OSError) as exc:
        receipt["status"] = "cancelled" if isinstance(exc, InterruptedError) else "budget-exhausted" if isinstance(exc, TimeoutError) else "error"
        receipt["error"] = str(exc)
    for name in config["tests"]:
        values = [row["seconds"] for row in receipt["samples"] if row["test"] == name and not row["warmup"]]
        if values:
            median = statistics.median(values)
            receipt["summary"][name] = {"count": len(values), "median_seconds": median,
                "minimum_seconds": min(values), "maximum_seconds": max(values),
                "relative_range": (max(values) - min(values)) / median if median > 0 else 0}
    receipt["elapsed_seconds"] = clock() - started
    receipt["cohort"] = cohort(receipt)
    receipt["comparison"] = compare(receipt, baseline)
    return receipt
