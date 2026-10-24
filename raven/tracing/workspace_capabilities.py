"""Built-in adapters only: descriptions cannot authorize capture or load code."""


def describe(host):
    posix, linux = host["posix"], host["os"] == "Linux"
    rows = [
        (
            "activity-json",
            "Chrome/Perfetto activity import",
            posix,
            "Reported X/B/E host/device intervals, counters and explicit flow markers; tool-declared scope and clocks, not universal device stacks.",
            "raven trace import-activity REPORT.json --project PROJECT --job-id JOB --attempt ATTEMPT",
        ),
        (
            "torch-profiler",
            "Optional PyTorch activity capture",
            posix,
            "CPU operators and optional available CUDA/HIP activities from the application's PyTorch profiler. Device support is determined in that interpreter. Internal framework buffers are not hard-bounded by Raven's output quota.",
            "raven trace profile --python APP_PYTHON --project PROJECT --job-id JOB --attempt ATTEMPT -- SCRIPT.py",
        ),
        (
            "linux-perf-samples",
            "Owned Linux perf stack sampling",
            linux,
            "Statistical callchain observations of a newly owned program, with period weights and unknown stacks. No complete call durations, system-wide attachment or decoded instruction trace.",
            "raven trace sample --project PROJECT --job-id JOB --attempt ATTEMPT -- COMMAND",
        ),
        (
            "triggered-cooperative",
            "Bounded cooperative incident recording",
            posix,
            "Explicit application events retained in a bounded pre-trigger ring and post-trigger window; uninstrumented execution remains unknown.",
            "Use raven.tracing.activity_trigger.TriggeredCapture and an explicit application trigger.",
        ),
    ]
    return {
        "schema": "raven.trace-workspace-capabilities/v1",
        "adapters": [
            dict(
                id=key,
                name=name,
                implemented=True,
                status="requires-explicit-pilot" if available else "unavailable",
                prerequisites_present=(
                    host["tools"].get("perf", False)
                    if key == "linux-perf-samples"
                    else None
                ),
                usable=None,
                live_qualified=False,
                scope=scope,
                next_step=command,
                qualification="This metadata report performs no capture. Inspect the actual adapter receipt and source health for tool versions, coverage, loss and failure reasons.",
            )
            for key, name, available, scope, command in rows
        ],
        "vendors": [
            dict(
                vendor=vendor,
                activity_import="Supported standard TraceEvent JSON only; retain producer/runtime metadata.",
                dedicated_vendor_decoder=False,
                automatic_device_stack_capture=False,
                live_qualified=False,
                unblock=condition,
            )
            for vendor, condition in (
                (
                    "NVIDIA",
                    "Physical device, permitted CUDA profiler and a versioned activity report/pilot",
                ),
                (
                    "AMD",
                    "Physical device, supported HIP/ROCm producer and a versioned activity report/pilot",
                ),
                (
                    "Intel",
                    "Physical device, Level Zero-capable producer exporting the supported schema; dedicated binary decoding is not implemented",
                ),
                (
                    "Apple",
                    "Supported Apple host and a producer exporting the supported schema; Instruments binary decoding is not implemented",
                ),
            )
        ],
        "trust": "Fixed built-in registry; report content never selects plugins, executables, system-wide targets or scheduler actions.",
    }
