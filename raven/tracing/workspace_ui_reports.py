"""Accessible text reports over prepared symbols and bounded analysis results."""

from __future__ import annotations

from .timeline_ui import duration
from .workspace_ui_state import _text


def symbol(rows, selected, prepared, width, append):
    if not isinstance(prepared, dict) or prepared.get("event_id") != (
        selected.get("detail_id") or selected.get("id")
    ):
        return
    report = prepared.get("report")
    if not isinstance(report, dict):
        return
    append(rows, "Symbol and source / explicit provenance checks", "heading", width)
    append(
        rows,
        "Native resolution: " + _text(report.get("status", "unavailable")),
        "text",
        width,
    )
    if report.get("reason"):
        append(rows, report["reason"], "dim", width)
    if report.get("build_id"):
        append(rows, "Matched ELF build ID " + _text(report["build_id"]), "dim", width)
    for frame in report.get("frames", ())[:64]:
        if not isinstance(frame, dict):
            continue
        append(
            rows,
            _text(frame.get("name", "unresolved"))
            + " / "
            + _text(frame.get("file") or "unknown source")
            + (":" + str(frame["line"]) if frame.get("line") is not None else ""),
            "text",
            width,
        )
    source = report.get("source") or {}
    if source.get("status") != "verified":
        append(
            rows,
            "Source unavailable: "
            + _text(source.get("reason", "content proof not supplied")),
            "dim",
            width,
        )
        return
    append(
        rows,
        "Verified source: "
        + _text(source.get("path", ""))
        + " / "
        + _text((source.get("proof") or {}).get("method", "content proof")),
        "heading",
        width,
    )
    for line in source.get("lines", ())[:11]:
        if isinstance(line, dict):
            append(
                rows,
                str(line.get("line", "?")) + ": " + _text(line.get("text", "")),
                "sel" if line.get("line") == source.get("line") else "text",
                width,
            )


def _elapsed(value):
    return duration(value) if type(value) is int else "unavailable"


def _metric(rows, metric, width, append):
    value = metric.get("value")
    append(
        rows,
        _text(metric.get("name", "metric"))
        + ": "
        + (
            _text(value) + " " + _text(metric.get("unit", ""))
            if value is not None
            else "unavailable: "
            + _text(metric.get("reason", "measurement not provided"))
        ),
        "text" if value is not None else "dim",
        width,
    )
    for label in ("numerator", "denominator", "scope", "coverage"):
        if metric.get(label) is not None:
            append(rows, label.capitalize() + ": " + _text(metric[label]), "dim", width)


def _samples(rows, report, width, append):
    append(
        rows,
        "Observed sampled stacks / "
        + str(report.get("observations", 0))
        + " observations / "
        + str(report.get("unavailable_stacks", 0))
        + " unavailable stacks",
        "heading",
        width,
    )
    append(
        rows,
        report.get("scope", "Observed source/process/event/unit scope only"),
        "dim",
        width,
    )
    for group in report.get("groups", ())[:100]:
        if not isinstance(group, dict):
            continue
        inclusive, leaf = group.get("inclusive_share"), group.get("leaf_share")
        share = (
            str(round(inclusive * 100, 3)) + "% inclusive"
            if type(inclusive) in (int, float)
            else "inclusive share unavailable"
        )
        if type(leaf) in (int, float):
            share += " / " + str(round(leaf * 100, 3)) + "% leaf"
        append(
            rows,
            _text(group.get("name", "unresolved frame")) + " / " + share,
            "text",
            width,
        )
        append(
            rows,
            str(group.get("weight", "?"))
            + " / "
            + str(group.get("observed_weight_denominator", "?"))
            + " "
            + _text(group.get("weight_unit", "unknown units"))
            + " observed weight / unavailable stack weight "
            + str(group.get("unknown_stack_weight", 0)),
            "dim",
            width,
        )
        append(
            rows,
            "Source "
            + _text(group.get("source_id", "?"))
            + " / process "
            + _text(group.get("source_pid", "?"))
            + " / event "
            + _text(group.get("event", "?"))
            + " / clock "
            + _text(group.get("clock_id", "?")),
            "dim",
            width,
        )
    append(
        rows,
        "Observed sample shares are not exact per-call durations or CPU utilization. Unknown/lost execution remains unknown.",
        "dim",
        width,
    )
    if report.get("omitted_frame_occurrences") or report.get(
        "omitted_symbol_occurrences"
    ):
        append(
            rows,
            "Frame/symbol analysis budget reached; shares are unavailable rather than reported from partial counts.",
            "warning",
            width,
        )


def analysis(views, state, job_id, width, append, controls):
    rows, hits = [], []
    mode = state.get("workspace_view", {}).get("analysis_mode", "none")
    if mode == "none":
        return rows, hits
    append(rows, "Investigation / " + mode.replace("-", " "), "heading", width)
    prepared = state.get("workspace_analysis")
    if not isinstance(prepared, dict) or prepared.get("mode") != mode:
        append(
            rows,
            "Preparing this analysis in the background; the timeline remains interactive.",
            "dim",
            width,
        )
        return rows, hits
    report = prepared.get("report")
    if not isinstance(report, dict):
        append(
            rows, "This analysis is unavailable for the current query.", "dim", width
        )
        return rows, hits
    for key in ("scope", "reason"):
        if report.get(key):
            append(rows, report[key], "dim", width)
    if report.get("partial") or report.get("truncated"):
        append(
            rows,
            "Partial evidence / query or analysis budget reached; inspect limits before interpreting totals.",
            "warning",
            width,
        )
    if mode in {"call-tree", "bottom-up"}:
        for node in report.get("nodes", ())[:128]:
            if not isinstance(node, dict):
                continue
            depth = (
                min(16, max(0, node.get("depth", 0)))
                if type(node.get("depth")) is int
                else 0
            )
            append(
                rows,
                "  " * depth
                + _text(node.get("name", "unresolved"))
                + " / "
                + str(node.get("count", 0))
                + " calls / "
                + _elapsed(node.get("inclusive_ns"))
                + " inclusive / "
                + _elapsed(node.get("self_ns"))
                + " self",
                "text",
                width,
            )
        if len(report.get("nodes", ())) > 128:
            append(
                rows,
                "The terminal report shows 128 contexts; refine the query or inspect the full returned tree in the browser.",
                "dim",
                width,
            )
    elif mode == "causal":
        append(
            rows,
            "Observed links / "
            + str(len(report.get("ancestors", ())))
            + " ancestors / "
            + str(len(report.get("descendants", ())))
            + " descendants",
            "text",
            width,
        )
        chain = report.get("observed_chain") or {}
        append(
            rows,
            chain.get("selection_criterion", "No supported dependency chain"),
            "dim",
            width,
        )
        if chain.get("reason"):
            append(rows, chain["reason"], "dim", width)
        bounds = chain.get("elapsed_bounds_ns")
        if isinstance(bounds, dict):
            append(
                rows,
                "Observed chain elapsed bounds: "
                + _elapsed(bounds.get("lower"))
                + " - "
                + _elapsed(bounds.get("upper"))
                + "; this is not a measured global critical path.",
                "text",
                width,
            )
        for node in report.get("nodes", ())[:64]:
            if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                continue
            extra, extra_hits = controls(
                views,
                [
                    (
                        node["id"],
                        _text(node.get("name", node["id"]), 128),
                        "event " + node["id"],
                    )
                ],
                job_id,
                width,
            )
            hits.extend((len(rows) + y, kind, value) for y, kind, value in extra_hits)
            rows.extend(extra)
        for issue in report.get("unknown", ())[:16]:
            if isinstance(issue, dict):
                append(
                    rows,
                    issue.get(
                        "reason", issue.get("message", "Unknown dependency evidence")
                    ),
                    "warning",
                    width,
                )
    elif mode == "counters":
        for series in report.get("series", ())[:128]:
            if not isinstance(series, dict):
                continue
            append(
                rows,
                _text(series.get("name", "counter"))
                + " / "
                + _text(series.get("unit", "unit unavailable"))
                + " / "
                + str(len(series.get("points", ())))
                + " observed points",
                "text",
                width,
            )
            append(
                rows,
                "Source "
                + _text(series.get("source_id", "?"))
                + " / clock "
                + _text(series.get("clock_id", "?"))
                + " / "
                + _text(series.get("minimum", "?"))
                + " - "
                + _text(series.get("maximum", "?")),
                "dim",
                width,
            )
        append(
            rows,
            "Counter points are observations; no values between observations are invented.",
            "dim",
            width,
        )
    elif mode == "hints":
        for hint in report.get("hints", ())[:20]:
            if not isinstance(hint, dict):
                continue
            append(rows, hint.get("title", "Evidence hint"), "heading", width)
            for field in ("reason", "assumptions", "proposed_checks", "evidence_ids"):
                if hint.get(field):
                    append(
                        rows,
                        field.replace("_", " ").capitalize()
                        + ": "
                        + _text(hint[field]),
                        "text" if field == "reason" else "dim",
                        width,
                    )
    elif mode == "metrics":
        for metric in report.get("metrics", ())[:128]:
            if isinstance(metric, dict):
                _metric(rows, metric, width, append)
        if isinstance(report.get("sample_statistics"), dict):
            _samples(rows, report["sample_statistics"], width, append)
    elif mode == "samples":
        _samples(rows, report, width, append)
    for limitation in report.get("limitations", ())[:16]:
        append(rows, limitation, "dim", width)
    return rows, hits
