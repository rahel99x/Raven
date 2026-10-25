"""Readable, bounded in-memory evidence summaries for the shared workbench."""

from __future__ import annotations

from ..research_text import clean

MAX_ROWS = 1800
MAX_ITEMS = 40


def _text(value):
    if value is None:
        return "unknown"
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return clean(str(value))


def rows(value):
    """Expose actual values/errors; the full structured result remains copyable.

    Presentation uses no source reads or scheduler calls. Large raw arrays have
    an explicit display boundary rather than an implicit completeness claim.
    """
    if not isinstance(value, dict):
        return [
            "Records: " + str(len(value)) if isinstance(value, list) else _text(value)
        ]
    output = []
    records = value.get("records")
    if isinstance(records, list):
        output.append(
            "Jobs: "
            + str(len(records))
            + " | full native identities/resources are retained in Copy report"
        )
        for record in records[:1000]:
            output.append(
                " | ".join(
                    _text(record.get(key)) for key in ("id", "state", "name", "user")
                )
            )
        if len(records) > 1000:
            output.append(
                "First 1,000 jobs shown; narrow the native query or copy the full bounded report."
            )
    capabilities = value.get("capabilities", value)
    stats = capabilities.get("read_stats") if isinstance(capabilities, dict) else None
    if isinstance(stats, dict):
        output.append(
            "Scheduler reads: "
            + str(stats.get("requests", 0))
            + " | cached: "
            + str(stats.get("cache_hits", 0))
            + " | last successful read: "
            + str(stats.get("last_read") or "none")
        )
    # Show failures and qualification before detailed data, regardless of how
    # producers order their dictionaries. Do not hide a missing GPU source
    # behind a long list of otherwise valid CPU samples.
    priority = (
        "status",
        "summary",
        "error",
        "recovery",
        "qualification",
        "association",
        "limitations",
        "limits",
        "warnings",
        "unknown",
        "uncertainty",
        "read_errors",
        "checks",
        "coverage",
        "identity",
        "experiment_id",
        "attempts",
        "states",
        "totals",
        "execution",
        "waiting",
        "device_activity",
    )
    omitted = False

    def visit(item, path, depth=0):
        nonlocal omitted
        if len(output) >= MAX_ROWS:
            omitted = True
            return
        if depth > 12:
            output.append(path + ": nested details retained in Copy report")
            return
        if isinstance(item, dict):
            if not item:
                output.append(path + ": no declared values")
            for key, child in item.items():
                visit(child, path + " / " + str(key).replace("_", " "), depth + 1)
                if len(output) >= MAX_ROWS:
                    omitted = True
                    break
        elif isinstance(item, (list, tuple)):
            output.append(path + ": " + str(len(item)) + " entries")
            for index, child in enumerate(item[:MAX_ITEMS]):
                visit(child, path + " [" + str(index + 1) + "]", depth + 1)
            if len(item) > MAX_ITEMS:
                output.append(
                    path
                    + ": first "
                    + str(MAX_ITEMS)
                    + " entries shown; full values retained in Copy report"
                )
        else:
            output.append(path + ": " + _text(item))

    keys = list(dict.fromkeys((*priority, *value)))
    for key in keys:
        if key in value and key not in {"records", "schema"}:
            visit(value[key], key.replace("_", " ").capitalize())
        if len(output) >= MAX_ROWS:
            omitted = True
            break
    if omitted:
        output = output[: MAX_ROWS - 1] + [
            "Display bound reached. Copy report retains the full structured evidence; the recording is unchanged."
        ]
    return output
