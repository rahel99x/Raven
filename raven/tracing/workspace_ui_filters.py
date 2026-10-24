"""Bounded terminal facet choices over the published indexed catalog."""

from __future__ import annotations

import shlex

FIELDS = {
    "node": "Node",
    "process": "Process",
    "thread": "Thread",
    "rank": "Rank",
    "task": "Logical task",
    "logical_cpu": "CPU",
    "device_id": "GPU",
    "kind": "Event type",
}
ALIASES = {
    "cpu": "logical_cpu",
    "gpu": "device_id",
    "device": "device_id",
    "type": "kind",
}
PAGE_SIZE = 12


def canonical(field):
    return ALIASES.get(field, field)


def sanitize(value):
    from .workspace_index_filters import validate_filters

    if not isinstance(value, dict) or set(value) - (set(FIELDS) - {"thread"}):
        return {}
    try:
        return validate_filters(**value)
    except (ValueError, TypeError):
        return {}


def choices(result, field):
    values = (result.get("dimensions") or {}).get(field, [])
    return (
        [
            value
            for value in values[:256]
            if type(value) is int
            or isinstance(value, str)
            and value.isprintable()
            and 0 < len(value) <= 512
        ]
        if isinstance(values, list)
        else []
    )


def prepared(result, view):
    """A changed indexed filter cannot paint an obsolete unfiltered query."""
    facets = {
        name: value
        for name, value in result.get("filters", {}).items()
        if name in FIELDS and name != "thread"
    }
    return sanitize(facets) == view.get("filters", {})


def render(views, view, result, job_id, width, append, controls):
    if not view.get("filters_open"):
        return [], []
    rows, hits = controls(
        views,
        [
            (
                name,
                label
                + (
                    ": "
                    + str(
                        view.get("thread")
                        if name == "thread"
                        else view["filters"].get(name)
                    )
                    if (
                        view.get("thread")
                        if name == "thread"
                        else view["filters"].get(name)
                    )
                    is not None
                    else ""
                ),
                "facet " + name,
            )
            for name, label in FIELDS.items()
        ]
        + [("clear", "Clear filters", "filter-clear")],
        job_id,
        width,
    )
    field = view.get("facet")
    if field not in FIELDS:
        append(
            rows,
            "Choose a filter. Values come from verified indexed metadata; capture limitations remain visible.",
            "dim",
            width,
        )
        return rows, hits
    values = choices(result, field)
    page = min(view.get("facet_page", 0), max(0, (len(values) - 1) // PAGE_SIZE))
    options = [("all", "All " + FIELDS[field].lower(), "filter-clear " + field)]
    for index, value in enumerate(values[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]):
        options.append(
            (str(index), str(value), "filter " + field + " " + shlex.quote(str(value)))
        )
    extra, extra_hits = controls(views, options, job_id, width)
    hits.extend((len(rows) + y, kind, value) for y, kind, value in extra_hits)
    rows.extend(extra)
    if len(values) > PAGE_SIZE:
        extra, extra_hits = controls(
            views,
            [
                ("previous", "Previous choices", "facet-previous"),
                ("next", "Next choices", "facet-next"),
            ],
            job_id,
            width,
        )
        hits.extend((len(rows) + y, kind, value) for y, kind, value in extra_hits)
        rows.extend(extra)
    append(
        rows,
        FIELDS[field]
        + ": "
        + str(len(values))
        + " indexed choices / page "
        + str(page + 1)
        + (
            " / catalog facet bound reached; exact filter values remain accepted"
            if field in result.get("dimensions_truncated", ())
            else ""
        ),
        "dim",
        width,
    )
    return rows, hits
