"""Read-only local trace prerequisites, separate from capture qualification.

The report never starts a profiler, claims remote capabilities, or equates a
visible performance-monitoring device with permission to record it.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import sys


SCHEMA = "raven.trace-capabilities/v1"
_PMUS = ("intel_pt", "cs_etm", "ibs_op", "ibs_fetch")
_TOOLS = ("perf", "mpicc", "xcrun")


def _probe():
    from .trace_cpu import capabilities as cpu_capabilities
    system = platform.system()
    return {
        "os": system,
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "posix": os.name == "posix",
        "monitoring_api": getattr(sys, "monitoring", None) is not None,
        "profile_api": callable(getattr(sys, "setprofile", None)),
        "native_transport": all(hasattr(socket, name) for name in ("AF_UNIX", "SOCK_SEQPACKET", "MSG_TRUNC")),
        "proc_identity": system == "Linux" and os.path.isfile("/proc/self/stat"),
        "cpu_placement": cpu_capabilities(),
        "tools": {tool: shutil.which(tool) is not None for tool in _TOOLS},
        "visible_pmus": {name: system == "Linux" and os.path.isdir("/sys/bus/event_source/devices/" + name)
                         for name in _PMUS},
    }


def describe():
    """Inspect this interface process's host; do not inspect a selected job."""
    host = _probe()
    posix = host["posix"]
    linux = host["os"] == "Linux"
    native = linux and host["native_transport"] and host["proc_identity"]
    cpu = host["cpu_placement"]
    adapters = [
        {"id": "python-monitoring", "name": "Automatic Python frames",
         "implemented": True,
         "status": "runtime-present" if posix and host["monitoring_api"] else "unavailable",
         "scope": "Python call, return, yield, resume and unwind events in included Python code; native internals and GPU kernels are unknown.",
         "next_step": "Use raven launch --trace with CPython 3.12+ on a supported Linux installation. Launch verifies free monitoring slots, project access and capture limits."},
        {"id": "python-legacy-profile", "name": "Legacy Python frames",
         "implemented": True,
         "status": "explicit-opt-in" if posix and host["profile_api"] else "unavailable",
         "scope": "Python call/return observations; yield, return and exception unwind cannot be distinguished reliably.",
         "next_step": "On Python 3.10/3.11, explicitly choose --trace-adapter python-legacy-profile for raven launch --trace. An existing profiler is never replaced."},
        {"id": "cooperative-python", "name": "Explicit Python spans",
         "implemented": True, "status": "runtime-present" if posix else "unavailable",
         "scope": "Only spans and metadata explicitly emitted by the application; uninstrumented calls are unknown.",
         "next_step": "Use raven.trace_recorder.Recorder and its span context manager in a POSIX Python process."},
        {"id": "cooperative-native-socket", "name": "Explicit C/C++ events",
         "implemented": True, "status": "requires-build" if native else "unavailable",
         "scope": "Linux SDK events in participating processes/threads; no automatic native stack or instruction capture.",
         "next_step": "Generate raven trace native --sdk-header, compile the application against it, and run through raven trace native. The current SDK requires Linux /proc and syscall interfaces."},
        {"id": "pmpi-world", "name": "Selected MPI collectives",
         "implemented": True, "status": "requires-build" if native else "unavailable",
         "scope": "C MPI_COMM_WORLD Barrier, supported Bcast types, and scalar Allreduce SUM; at most 256 ranks. No MPI_THREAD_MULTIPLE or arbitrary native stacks.",
         "next_step": "Generate raven trace mpi --source and compile with the application's matching MPI compiler/runtime and Raven native SDK; set a shared RAVEN_MPI_COMM_GENERATION."},
        {"id": "linux-process-counters", "name": "Owned process counters",
         "implemented": True, "status": "requires-owned-target" if linux and host["proc_identity"] else "unavailable",
         "scope": "Bounded /proc process/thread counters and observed states; these are samples, not call stacks or exact scheduling history.",
         "next_step": "Use the admitted process profiler on an explicitly verified, owned process on the Heavy worker host."},
        {"id": "software-cpu-placement", "name": "CPU socket/core observations",
         "implemented": True,
         "status": "explicit-opt-in" if linux and posix and cpu["status"] == "available" else "unavailable",
         "scope": "Per-process/thread event-time logical CPU observations, mapped to kernel-exposed socket and physical-core IDs when available; sample brackets do not establish continuous residency.",
         "next_step": "Use raven launch --trace --trace-cpu-placement, or raven trace run --cpu-placement. Missing topology and unsupported platforms remain unknown; no hardware tracing adapter is required."},
    ]
    hardware = [
        {"id": "intel-pt", "name": "Intel Processor Trace", "implemented": False,
         "status": "not-implemented", "visible_pmus": ["intel_pt"] if host["visible_pmus"]["intel_pt"] else [],
         "scope": "Hardware control-flow tracing on supported Intel CPUs; decoding needs the matching binaries and trace context. Visibility does not prove permissions or complete capture."},
        {"id": "arm-coresight", "name": "Arm CoreSight ETM", "implemented": False,
         "status": "not-implemented", "visible_pmus": ["cs_etm"] if host["visible_pmus"]["cs_etm"] else [],
         "scope": "Hardware instruction/control-flow tracing only where the Arm SoC, kernel and platform expose a usable trace path. Arm, Marvell or Apple branding alone proves no availability."},
        {"id": "amd-ibs", "name": "AMD Instruction-Based Sampling", "implemented": False,
         "status": "not-implemented", "visible_pmus": [name for name in ("ibs_op", "ibs_fetch") if host["visible_pmus"][name]],
         "scope": "Instruction-based samples on supported AMD CPUs; sampling cannot reconstruct every call or provide an Intel PT-equivalent continuous execution history."},
        {"id": "apple-instruments", "name": "Apple Instruments", "implemented": False,
         "status": "not-implemented", "visible_pmus": [],
         "scope": "Platform-specific profiling outside Raven's current adapters; xcrun presence does not verify Instruments, recording permissions or exact instruction tracing."},
    ]
    return {"schema": SCHEMA, "host": host, "adapters": adapters, "hardware": hardware,
            "viewer": {"architecture_independent": True,
                       "scope": "The same verified Raven event timeline is used in the terminal and offline browser, regardless of the recording CPU vendor. Capture coverage is carried by each recording."},
            "qualification": "Local prerequisites only; this command does not record, benchmark or qualify this host, remote workers, GPU execution, native instruction tracing or another operating system.",
            "platform_support": "Linux/WSL2 supports persistent Raven state. macOS dashboard use requires --no-state; macOS capture remains unqualified even when Python APIs are present.",
            "identity": "Linux /proc provides boot/process-start evidence when accessible. Else those identities remain unknown; no hardware or ownership guarantee is inferred.",
            "documentation": "docs/guides/trace-capabilities.md"}


def render(report):
    host = report["host"]
    lines = ["Raven trace capabilities", f"Host: {host['os']} {host['architecture']} / {host['implementation']} {host['python']}",
             "Scope: local interface host; selected jobs and remote workers are not probed.", "",
             report["viewer"]["scope"], "", "Capture adapters:"]
    for adapter in report["adapters"]:
        lines.extend([f"  {adapter['name']}: {adapter['status']}",
                      "    " + adapter["scope"], "    " + adapter["next_step"]])
        if adapter["id"] == "software-cpu-placement":
            cpu = host["cpu_placement"]
            lines.append(f"    Topology: {cpu['topology_status']}; {cpu['topology_cpus']} logical CPUs in the bounded host snapshot.")
            if cpu.get("reason"):
                lines.append("    " + cpu["reason"])
    lines.extend(["", "Hardware capture adapters:"])
    for adapter in report["hardware"]:
        visible = ", ".join(adapter["visible_pmus"]) or "none"
        lines.extend([f"  {adapter['name']}: {adapter['status']} (visible Linux PMUs: {visible})",
                      "    " + adapter["scope"]])
    tools = ", ".join(name for name, present in host["tools"].items() if present) or "none"
    lines.extend(["", "Optional tools found on PATH: " + tools,
                  "Tool/PMU presence is not recording permission or qualification.",
                  report["platform_support"], report["identity"], report["qualification"], "Guide: " + report["documentation"]])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="raven trace capabilities",
                                     description="Read local capture prerequisites without starting a profiler or changing a job")
    parser.add_argument("--json", action="store_true", help="print the structured capability report")
    args = parser.parse_args(argv)
    report = describe()
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
