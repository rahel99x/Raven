"""Read-only, bounded checks for an explicit metrics stream and exact run.

Uses the same contracts and confined local/SSH readers as Research and Analytics.
Declarations, retained observations and verified journal events remain distinct.
"""
from __future__ import annotations

import math
import os
import shlex

from .metrics import MetricReader

SAMPLE_BYTES = 256 << 10
SAMPLE_POINTS = 128


def metric_name(value):
    if not isinstance(value, str) or not value or len(value) > 96 or not value.isprintable() or "=" in value:
        raise ValueError("Metric name must contain 1–96 printable characters, without '='.")
    return value


def snippet(language, name="loss"):
    """Copyable adapters accept real values; generating code never records one."""
    name = metric_name(name)
    if language == "bash":
        return ('# Launch this script through Raven to receive its exact run environment.\n'
                '# Call report_measurement "$measured_value" after computing a real value.\n'
                '# Use one reporting coordinator per run (not every MPI rank).\n'
                'report_measurement() {\n'
                '  : "${RAVEN_RUN_DIR:?Launch this script with Raven}"\n'
                '  : "${RAVEN_REPORT_PYTHON:?Missing Raven reporting Python}"\n'
                '  : "${RAVEN_REPORT_HELPER:?Missing Raven reporting helper}"\n'
                '  "$RAVEN_REPORT_PYTHON" -I "$RAVEN_REPORT_HELPER" metric \\\n'
                '    ' + shlex.quote('--value=' + name + '=') + '"${1:?Supply an actual measurement}"\n'
                '}\n')
    if language == "python":
        return ('# Launch through Raven; call report_measurement(actual_value, step=step).\n'
                '# One coordinator writes this run. No Raven import is needed here.\n'
                'import math\nimport os\nimport subprocess\n\n'
                'def report_measurement(value, *, step=None):\n'
                '    if isinstance(value, bool) or not math.isfinite(float(value)):\n'
                '        raise ValueError("Supply a finite measured number")\n'
                '    command = [os.environ["RAVEN_REPORT_PYTHON"], "-I",\n'
                '               os.environ["RAVEN_REPORT_HELPER"], "metric", "--run",\n'
                '               os.environ["RAVEN_RUN_DIR"],\n'
                '               ' + repr('--value=' + name + '=') + ' + str(float(value))]\n'
                '    if step is not None:\n'
                '        if type(step) is not int or step < 0:\n'
                '            raise ValueError("step must be a nonnegative integer")\n'
                '        command += ["--step", str(step)]\n'
                '    subprocess.run(command, check=True, timeout=10)\n')
    raise ValueError("Choose Bash or Python.")


def validate_metrics(path, *, files=None, binding=None):
    """Inspect a bounded tail, keeping valid data alongside exact parser errors."""
    if not isinstance(path, str) or not path or len(path) > 4096 or not path.isprintable():
        raise ValueError("Choose a printable metrics-file path of at most 4096 characters.")
    reader = MetricReader(files, max_bytes=SAMPLE_BYTES, max_points=SAMPLE_POINTS, max_streams=1)
    value = reader.read_confined(path, binding) if binding else reader.read(path)
    issues = list(value["errors"])
    if not value["records"] and not issues:
        issues.append("No complete JSONL observations yet. Append a JSON object followed by a newline.")
    if value["records"] and not value["series"]:
        issues.append("No chart values: add metrics with at least one named finite number, for example your measured loss.")
    value["issues"] = issues
    value["usable"] = bool(value["series"])
    value["guidance"] = (
        "Each line needs t (nonnegative epoch seconds). To draw a chart, metrics maps names to finite numbers.",
        "Optional step is a nonnegative integer. Optional progress needs completed and total > 0, with completed <= total.",
        "Omit unavailable measurements. null, Boolean, strings, NaN and Infinity are not measurements.",
        "Use one writer per exact attempt, at most 64 metric names and 65,536 bytes per row; end every row with a newline.",
    )
    return value


def _number(value):
    return type(value) in (float, int) and math.isfinite(value)


def _family(family, result):
    source = next((row for row in result["index"]["sources"] if row["family"] == family), None)
    if source is None:
        return {"status": "not-recorded", "samples": 0, "reason": "This attempt has no declared " + family + " recording."}
    rows = result.get(family, [])
    keys = ("cpu_seconds", "rss_bytes") if family == "cpu-memory" else ("utilization_percent", "memory_used_mib")
    samples = sum(any(_number(row.get(key)) for key in keys) for row in rows)
    reason = result.get("errors", {}).get(family) or source.get("reason", "")
    incomplete = source["status"] in ("partial", "unavailable", "failed") or any(
        row.get("status") == "partial" or not all(_number(row.get(key)) for key in keys) for row in rows)
    if samples and incomplete and not reason:
        reason = "Some counters or recording coverage are unavailable; available observations remain usable."
    return {"status": "partial" if samples and (reason or incomplete) else "ready" if samples else "unavailable" if reason else "waiting",
            "samples": samples, "reason": reason or ("Retained observations available." if samples else "No measured counters in the bounded sample yet."),
            "path": source["path"], "coverage": source.get("coverage", {}),
            "source_status": source["status"]}


def _trace(binding, files):
    """Read one registered stream only; SSH validates metadata, never local files."""
    from .projects import read_bound_json, relative_path
    from .trace_protocol import validate_identity, TraceReader
    root = binding["run_root"]
    index = read_bound_json(binding, os.path.join(root, "trace/index.json"), files=files, max_bytes=65536)
    if (not isinstance(index, dict) or index.get("schema") != "raven.trace-index/v1"
            or not isinstance(index.get("sources"), list) or len(index["sources"]) > 256):
        raise ValueError("Expected a bounded raven.trace-index/v1 sources list.")
    sources = [row for row in index["sources"] if isinstance(row, dict) and row.get("schema") == "raven.trace/v1"]
    if not sources:
        return {"status": "not-recorded", "samples": 0, "reason": "No registered trace manifest for this attempt."}
    path = os.path.join(root, relative_path(sources[0].get("path")))
    manifest = read_bound_json(binding, path, files=files, max_bytes=65536)
    if not isinstance(manifest, dict) or manifest.get("schema") != "raven.trace/v1":
        raise ValueError("Expected a raven.trace/v1 manifest.")
    identity = validate_identity(manifest.get("identity"))
    if ((identity.get("run_id") or identity.get("attempt")) != binding["run_id"]
            or identity.get("job_id") != (binding.get("job_id") or "local")
            or (binding.get("launch", {}).get("cluster") and identity.get("cluster") != binding["launch"]["cluster"])):
        raise ValueError("Trace identity does not match the selected run, job or cluster.")
    if getattr(files, "remote", False):
        return {"status": "declared", "samples": 0, "path": path,
                "reason": "Exact trace identity verified over SSH. Open Raven on the trace host to verify journal events."}
    snapshot = TraceReader(path, retain=128).refresh(max_events=128, max_bytes=SAMPLE_BYTES, max_seconds=.015)
    if snapshot["identity"] != identity:
        raise ValueError("Trace identity changed during validation; retry.")
    # Revalidate run identity after the journal read, including replaced parents.
    read_bound_json(binding, os.path.join(root, "trace/index.json"), files=files, max_bytes=65536)
    count = len(snapshot["events"])
    issues = snapshot.get("warnings", [])
    return {"status": "partial" if issues else "ready" if count else "waiting", "samples": count,
            "path": path, "reason": "; ".join(issues) or ("Verified bounded journal prefix; more streams/events may exist." if count else "No committed trace events yet.")}


def inspect(path="", *, files=None, binding=None, declared=False):
    """Return independent evidence for all four recording families, read-only."""
    result = {"metrics": None, "readiness": {}, "binding": binding, "declared": declared}
    if path:
        try:
            result["metrics"] = validate_metrics(path, files=files, binding=binding if declared else None)
            value = result["metrics"]
            result["readiness"]["scientific"] = {"status": "partial" if value["usable"] and value["issues"] else "ready" if value["usable"] else "needs-input",
                "samples": value["records"], "reason": "; ".join(value["issues"]) or "Actual measurements available for preview.", "path": path}
        except (OSError, ValueError) as exc:
            result["readiness"]["scientific"] = {"status": "unavailable", "samples": 0, "reason": str(exc), "path": path}
    else:
        result["readiness"]["scientific"] = {"status": "needs-input", "samples": 0, "reason": "Choose an existing JSONL metrics file or generate a producer snippet."}
    if not binding:
        for family in ("cpu-memory", "gpu", "tracing"):
            result["readiness"][family] = {"status": "not-selected", "samples": 0, "reason": "Select an exact project run to inspect its recordings."}
        return result
    try:
        from .projects import read_bound_telemetry
        telemetry = read_bound_telemetry(binding, files=files, max_bytes=SAMPLE_BYTES)
        for family in ("cpu-memory", "gpu"):
            result["readiness"][family] = _family(family, telemetry)
    except (OSError, ValueError) as exc:
        for family in ("cpu-memory", "gpu"):
            result["readiness"][family] = {"status": "not-recorded" if isinstance(exc, FileNotFoundError) else "unavailable", "samples": 0, "reason": str(exc)}
    try:
        result["readiness"]["tracing"] = _trace(binding, files)
    except (OSError, ValueError) as exc:
        result["readiness"]["tracing"] = {"status": "not-recorded" if isinstance(exc, FileNotFoundError) else "unavailable", "samples": 0, "reason": str(exc)}
    return result


def preview_lines(result, glyphs, width=64):
    """Small honest chart with axis range; order is sample order, not uniform time."""
    from .layout import spark
    rows = []
    for name, points in list((result or {}).get("series", {}).items())[:8]:
        selected = points[-max(1, min(width, SAMPLE_POINTS)):]
        values = [point["value"] for point in selected]
        if not values:
            continue
        low, high = min(values), max(values)
        # Scaling finite extreme values avoids overflow in their difference.
        scale = max(abs(low), abs(high), 1)
        normalized = [((value / scale - low / scale) / (high / scale - low / scale)) if high != low else .5 for value in values]
        rows += [f"{name}: last {values[-1]:.6g} | range {low:.6g}–{high:.6g}",
                 spark(glyphs, normalized, len(values)),
                 f"{len(values)} observations | epoch {selected[0]['t']:.6g} to {selected[-1]['t']:.6g}"]
    return rows
