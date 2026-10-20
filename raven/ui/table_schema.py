"""Canonical column descriptors shared by tables and renderers."""

from ..layout import Column
from .. import layout as L
from ..table_sort import TABLE_KEYS, chain
from dataclasses import replace

JOB_COLS = [
    Column("progress", "PROG", 6, 6),
    Column("id", "JOBID", 5, 16),
    Column("name", "NAME", 10, 30, flex=True),
    Column("part", "PART", 4, 9),
    Column("st", "ST", 2, 3),
    Column("where", "NODES", 6, 18, flex=True),
    Column("cpus", "CPU", 3, 4, ">"),
    Column("gpu", "GPU", 3, 8),
    Column("time", "ELAPSED/LIMIT", 8, 20),
    Column("left", "LEFT/WAIT", 9, 11, ">"),
    Column("cpu%", "CPU%", 4, 4, ">"),
    Column("eff", "EFF", 4, 5, ">"),
    Column("mem%", "MEM%", 4, 4, ">"),
    Column("gpu%", "GPU%", 4, 4, ">"),
    Column("flags", "FLAGS", 5, 12),
    Column("tags", "TAGS", 4, 14),
    Column("info", "INFO", 34, 60, flex=True),
]

JOB_DROP = (
    "tags",
    "left",
    "flags",
    "gpu%",
    "mem%",
    "eff",
    "cpu%",
    "part",
    "gpu",
    "where",
    "st",
)

FIN_COLS = [
    Column("id", "JOBID", 5, 16),
    Column("name", "NAME", 8, 30, flex=True),
    Column("state", "STATE", 5, 14),
    Column("part", "PART", 4, 9),
    Column("elapsed", "ELAPSED", 7, 12, ">"),
    Column("cpus", "CPU", 3, 4, ">"),
    Column("gpus", "GPU", 3, 3, ">"),
    Column("ce", "CPU EFF", 7, 7, ">"),
    Column("me", "MEM EFF", 7, 7, ">"),
    Column("rss", "PEAK MEM", 8, 10, ">"),
    Column("start", "STARTED", 5, 12),
    Column("end", "ENDED", 5, 12),
    Column("exit", "EXIT", 4, 6),
    Column("nodes", "NODES", 5, 16, flex=True),
    Column("tags", "TAGS", 4, 14),
    Column("info", "INFO", 10, 64, flex=True),
]

GROUP_COLS = [
    Column("user", "USER", 4, 12),
    Column("id", "JOBID", 5, 16),
    Column("name", "NAME", 8, 28, flex=True),
    Column("part", "PART", 4, 9),
    Column("st", "ST", 2, 3),
    Column("where", "NODES", 5, 18, flex=True),
    Column("cpus", "CPU", 3, 4, ">"),
    Column("gpu", "GPU", 3, 8),
    Column("time", "ELAPSED/LIMIT", 8, 20),
    Column("prio", "PRIO", 4, 7, ">"),
    Column("info", "INFO", 10, 40, flex=True),
]


FIELDS = {"state", "partition", "tag", "name", "id", "user"}

REQUIRED = {"id", "name", "st", "state"}


def definitions(tab):
    if tab == "jobs":
        return JOB_COLS
    if tab in ("history", "recent"):
        return FIN_COLS
    if tab == "group":
        return GROUP_COLS
    labels = {
        "nodes": {
            "name": "NODE",
            "cpus": "ALLOC/CPUS",
            "loadpct": "LOAD%",
            "mem": "MEM USED",
            "gused": "GRES USED",
            "gutil": "GPU%",
            "jobs": "MY JOBS",
        },
        "cluster": {
            "name": "PARTITION",
            "cidle": "CPUS IDLE",
            "calloc": "CPUS ALLOC",
            "mine": "MY RUN",
            "minep": "MY PEND",
            "gpus": "GPUS FREE/UP",
        },
        "sources": {"name": "SOURCE", "every": "INTERVAL", "last": "LAST OK"},
    }
    return [
        L.Column(key, labels.get(tab, {}).get(key, key.upper()), 4, 60)
        for key in TABLE_KEYS.get(tab, ())
    ]


def _order(value, tab):
    available = {column.key for column in definitions(tab)}
    if (
        not isinstance(value, list)
        or len(value) > len(available)
        or any(not isinstance(key, str) or key not in available for key in value)
        or len(value) != len(set(value))
    ):
        raise ValueError("invalid column order")
    return list(value)


def _widths(value, tab):
    available = {column.key for column in definitions(tab)}
    if (
        not isinstance(value, dict)
        or len(value) > len(available)
        or any(
            key not in available
            or isinstance(width, bool)
            or not isinstance(width, int)
            or not 2 <= width <= 120
            for key, width in value.items()
        )
    ):
        raise ValueError("column widths must be between 2 and 120 terminal cells")
    result = dict(value)
    if tab == "jobs" and "progress" in result:
        result["progress"] = 6
    return result


def _hidden(value, tab):
    available = {column.key for column in definitions(tab)}
    if (
        not isinstance(value, list)
        or len(value) > len(available)
        or any(
            not isinstance(key, str) or key not in available or key in REQUIRED
            for key in value
        )
    ):
        raise ValueError("invalid hidden columns")
    return sorted(set(value))


def _facets(value, tab):
    if (
        not isinstance(value, dict)
        or len(value) > len(FIELDS)
        or any(
            key not in FIELDS or not isinstance(text, str) or len(text) > 256
            for key, text in value.items()
        )
    ):
        raise ValueError("invalid field filters")
    return {key: text for key, text in value.items() if text}


def columns(app, tab, original):
    state = getattr(app, "table_state", {})
    hidden = state.get("hidden", {}).get(tab, [])
    hidden = hidden if isinstance(hidden, list) else []
    order = state.get("order", {}).get(tab, [])
    positions = (
        {key: index for index, key in enumerate(order)}
        if isinstance(order, list)
        else {}
    )
    original = sorted(
        original, key=lambda column: positions.get(column.key, len(positions))
    )
    widths = state.get("widths", {}).get(tab, {})
    original = [
        replace(column, lo=6, hi=6, flex=False)
        if tab == "jobs" and column.key == "progress"
        else replace(column, lo=widths[column.key], hi=widths[column.key], flex=False)
        if isinstance(widths, dict)
        and isinstance(widths.get(column.key), int)
        and not isinstance(widths[column.key], bool)
        and 2 <= widths[column.key] <= 120
        else column
        for column in original
    ]
    selected = chain(app, tab)
    if selected is not None:
        indicators = {
            key: f" {'^' if direction == 'asc' else 'v'}{index}"
            for index, (key, direction) in enumerate(selected, 1)
        }
        # Keep compact progress at its requested width, including the full
        # cascading priority. Other headings retain their normal sizing.
        titles = {
            column.key: (
                (
                    L.cut(
                        column.title, max(0, column.hi - L.vlen(indicators[column.key]))
                    )
                    if column.key == "progress"
                    else column.title
                )
                + indicators[column.key]
            )
            for column in original
            if column.key in indicators
        }
        return [
            replace(
                column,
                title=titles[column.key],
                lo=column.lo
                if column.key in widths
                else max(column.lo, L.vlen(titles[column.key])),
                hi=column.hi
                if column.key in widths
                else max(column.hi, L.vlen(titles[column.key])),
            )
            if column.key in indicators
            else column
            for column in original
            if column.key not in hidden or column.key in REQUIRED
        ]
    key = app.sort.get(tab, "")
    mapping = {
        "state": "st" if tab == "jobs" else "state",
        "cpu_eff": "ce",
        "mem_eff": "me",
    }
    sorted_key = mapping.get(key, key)
    return [
        replace(
            column,
            title=column.title + (" v" if app.reverse.get(tab) else " ^"),
            lo=column.lo
            if column.key in widths
            else max(column.lo, L.vlen(column.title) + 2),
            hi=column.hi
            if column.key in widths
            else max(column.hi, L.vlen(column.title) + 2),
        )
        if column.key == sorted_key
        else column
        for column in original
        if column.key not in hidden or column.key in REQUIRED
    ]


def ordered_definitions(app, tab):
    cols = definitions(tab)
    order = getattr(app, "table_state", {}).get("order", {}).get(tab, [])
    positions = {key: index for index, key in enumerate(order)}
    return sorted(cols, key=lambda column: positions.get(column.key, len(positions)))
