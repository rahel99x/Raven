"""Optional event-time CPU observations, independent of the processor ISA.

Linux's sched_getcpu reports the calling thread's logical CPU at one instant.
It does not report scheduling history or continuous residency. Topology is a
bounded snapshot taken before capture; missing or hot-plugged CPUs stay unknown.
"""
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import sys
import time

SCHEMA = "raven.cpu-placement/v1"
MAX_CPUS = 8192
MAX_ID = (1 << 31) - 1


@lru_cache(maxsize=1)
def _cpu_function():
    """Resolve the C function once, without assuming an instruction set."""
    import ctypes
    library = ctypes.CDLL(None, use_errno=True)
    function = library.sched_getcpu
    function.argtypes = []
    function.restype = ctypes.c_int
    return function


def _topology_id(path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(32)
        value = int(raw)
        return value if 0 <= value <= MAX_ID else None
    except (OSError, ValueError):
        return None


def _topology(directory=Path("/sys/devices/system/cpu"), *, cpus=None):
    result = {}
    complete = True

    def record(number, root):
        socket_id = _topology_id(root / "physical_package_id")
        core_id = _topology_id(root / "core_id")
        result[number] = (socket_id, core_id)
        return socket_id is not None and core_id is not None

    try:
        if cpus is not None:
            # MPI ranks commonly inherit a small binding. Do not reread all
            # host cores for every rank; later affinity changes remain unknown.
            complete = len(cpus) <= MAX_CPUS
            for number in cpus:
                if len(result) >= MAX_CPUS:
                    break
                known = record(number, directory / ("cpu" + str(number)) / "topology")
                complete = complete and known
        else:
            with os.scandir(directory) as entries:
                for entry in entries:
                    suffix = entry.name[3:]
                    if not entry.name.startswith("cpu") or not suffix.isascii() or not suffix.isdecimal():
                        continue
                    if len(result) >= MAX_CPUS:
                        complete = False
                        break
                    if len(suffix) > 10 or int(suffix) > MAX_ID:
                        continue
                    known = record(int(suffix), Path(entry.path) / "topology")
                    complete = complete and known
    except OSError:
        complete = False
    return result, complete and bool(result)


class CPUPlacement:
    """Fail-open, bounded metadata collector for the current process only."""
    def __init__(self):
        self.pid = os.getpid()
        self.function = None
        self.topology = {}
        self.coverage = {
            "schema": "raven.cpu-placement-coverage/v1",
            "status": "unavailable", "source": "linux-sched_getcpu",
            "reason": "CPU placement observations require Linux sched_getcpu",
            "semantics": "Calling-thread CPU observed within each sample bracket; gaps, migrations, residency duration, and unobserved CPUs are unknown",
            "topology_source": "linux-sysfs", "topology_cpus": 0,
            "topology_limit": MAX_CPUS, "topology_status": "unavailable",
            "topology_scope": "bounded-host-metadata",
        }
        if sys.platform != "linux":
            return
        try:
            self.function = _cpu_function()
            cpu = self.function()
            if type(cpu) is not int or not 0 <= cpu <= MAX_ID:
                raise OSError("sched_getcpu returned no usable CPU")
            try:
                cpus = os.sched_getaffinity(0) or None
            except (AttributeError, OSError, NotImplementedError):
                cpus = None
            if cpus is not None:
                self.coverage["topology_scope"] = "initial-affinity"
            self.topology, complete = _topology(cpus=cpus)
            self.coverage.update(status="available", reason="", topology_cpus=len(self.topology),
                                 topology_status="complete" if complete else "partial" if self.topology else "unavailable")
        except Exception as exc:
            self._unavailable("CPU placement probe failed: " + type(exc).__name__)

    def _unavailable(self, reason):
        self.function = None
        self.coverage.update(status="unavailable", reason=reason)

    def observe(self):
        if self.function is None:
            return None
        if os.getpid() != self.pid:
            self._unavailable("Inherited CPU observer cannot attribute events after fork; start a child-owned recorder")
            return None
        try:
            start = time.monotonic_ns()
            cpu = self.function()
            end = time.monotonic_ns()
            if type(cpu) is not int or not 0 <= cpu <= MAX_ID or end < start:
                raise OSError("CPU observation unavailable")
            socket_id, core_id = self.topology.get(cpu, (None, None))
            return {"schema": SCHEMA, "logical_cpu": cpu, "socket_id": socket_id,
                    "core_id": core_id, "sample_start_ns": start, "sample_end_ns": end,
                    "source": "linux-sched_getcpu"}
        except Exception as exc:
            self._unavailable("CPU placement observation failed: " + type(exc).__name__)
            return None


def capabilities():
    """Read-only availability probe; this never starts recording or tracing."""
    return dict(CPUPlacement().coverage)
