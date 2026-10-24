"""A bounded clock-local density minimap over published overview bins."""

from __future__ import annotations

import hashlib

from .. import layout as L


def render(report, clock, job_id, width, ascii_only):
    if not isinstance(report, dict):
        return [], []
    domain = next(
        (row for row in report.get("domains", ()) if row.get("clock_id") == clock), None
    )
    if domain is None:
        return [], []
    bins = [
        row
        for row in domain.get("bins", ())[:1024]
        if type(row.get("count")) is int
        and row["count"] >= 0
        and type(row.get("start_ns")) is int
        and type(row.get("end_ns")) is int
    ]
    if not bins:
        return [], []
    width = max(1, width)
    columns = min(width, len(bins))
    scale = max(1, max(row["count"] for row in bins))
    glyphs = ".:-=+*#@" if ascii_only else "▁▂▃▄▅▆▇█"
    values, hits = [], []
    clock_key = hashlib.sha256(str(clock).encode()).hexdigest()[:16]
    for column in range(columns):
        first = column * len(bins) // columns
        last = max(first + 1, (column + 1) * len(bins) // columns)
        group = bins[first:last]
        value = max(row["count"] for row in group)
        char = (
            " "
            if not value
            else glyphs[
                min(len(glyphs) - 1, (value * (len(glyphs) - 1) + scale - 1) // scale)
            ]
        )
        values.append(char)
        start, end = group[0]["start_ns"], group[-1]["end_ns"]
        if start < end:
            hits.append(
                (
                    1,
                    "job_panel_action",
                    (
                        (
                            "trace_command",
                            {
                                "job": job_id,
                                "command": f"trace workspace range {clock_key} {start} {end}",
                            },
                        ),
                        column,
                        column + 1,
                    ),
                )
            )
    rows = [
        [
            (
                L.cut(
                    "Overview density of interval starts (click to explore)",
                    width,
                    ascii_only,
                ),
                "muted",
            )
        ],
        [("".join(values), "cyan")],
    ]
    return rows, hits
