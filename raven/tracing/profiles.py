"""Small explicit recording presets, expanded into existing launch settings.

These bound retained bytes/process attachment, not execution overhead or the
application's memory. A pilot must be measured on the actual workload/runtime.
"""

PROFILES = {
    "project": {
        "trace_scope": "project",
        "trace_helpers": False,
        "trace_cpu_placement": False,
        "trace_process_bytes": 64 << 20,
        "trace_run_bytes": 256 << 20,
        "trace_processes": 128,
    },
    "topology": {
        "trace_scope": "project",
        "trace_helpers": False,
        "trace_cpu_placement": True,
        "trace_process_bytes": 64 << 20,
        "trace_run_bytes": 256 << 20,
        "trace_processes": 128,
    },
    "pilot": {
        "trace_scope": "project",
        "trace_helpers": False,
        "trace_cpu_placement": False,
        "trace_process_bytes": 4 << 20,
        "trace_run_bytes": 32 << 20,
        "trace_processes": 16,
    },
}


def settings(name):
    if name not in PROFILES:
        raise ValueError("Choose the project, topology or pilot trace profile")
    return dict(PROFILES[name], trace=True)


def preview(value):
    enabled = bool(value.get("trace", False))
    process = value.get("trace_process_bytes", 64 << 20)
    run = value.get("trace_run_bytes", 256 << 20)
    count = value.get("trace_processes", 128)
    return {
        "schema": "raven.trace-budget-preview/v1",
        "enabled": enabled,
        "adapter": value.get("trace_adapter") or "python-monitoring",
        "scope": value.get("trace_scope", "project"),
        "helpers_visible": value.get("trace_helpers", False),
        "cpu_placement_points": value.get("trace_cpu_placement", False),
        "process_bytes": process,
        "run_bytes": run,
        "max_processes": count,
        "retained_byte_upper_bound": min(run, process * count) if enabled else 0,
        "overhead": "Unmeasured for this workload; compare a short representative pilot with an unrecorded baseline.",
        "fidelity": "Python call evidence only; helper recordings remain available; CPU placement is observations, not residency; no device activities are implied.",
        "resources": "No allocation or application memory/CPU limit is changed by this preset.",
        "when_full": "Capture stops or records an explicit gap according to the existing recorder budget; the scientific application continues.",
    }
