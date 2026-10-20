"""Bounded table filtering and published preference queries, without UI handlers."""

from __future__ import annotations
import math
import re
from . import clock, table_sort
from .table_sort import sort_fingerprint
from .model import Job, Finished, secs, stamp
from .ui.table_schema import FIELDS

MAX_RULES = 16

NUMERIC_FIELDS = (
    "cpus",
    "gpus",
    "memory",
    "rss",
    "cpu_eff",
    "mem_eff",
    "elapsed",
    "priority",
    "nodes",
)

FILTER_FIELDS = ("state", "partition", "name", "id", "tag", "user") + NUMERIC_FIELDS

ALIASES = {
    "cpu": "cpus",
    "gpu": "gpus",
    "mem": "memory",
    "time": "elapsed",
    "eff": "cpu_eff",
    "ce": "cpu_eff",
    "me": "mem_eff",
    "prio": "priority",
}

COMPARISON = re.compile(r"^([a-z_]+)\s*(>=|<=|!=|==|=|>|<)\s*(.+)$", re.I)

MEMORY = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]?)(?:i?B)?$", re.I)


def _state(app):
    return getattr(app, "table_tools_state", {})


def parse_rule(text):
    match = COMPARISON.fullmatch(str(text).strip())
    if not match:
        raise ValueError("Use cpus>=8, memory>16GiB, cpu_eff<30%, or elapsed>=2h")
    field, operator, label = match.groups()
    field, label = ALIASES.get(field.lower(), field.lower()), label.strip()
    if field not in NUMERIC_FIELDS or len(label) > 64:
        raise ValueError("Numeric fields: " + ", ".join(NUMERIC_FIELDS))
    operator = "=" if operator == "==" else operator
    try:
        if field in ("memory", "rss"):
            unit = MEMORY.fullmatch(label)
            if not unit:
                raise ValueError("Memory uses bytes or units such as 16GiB")
            number = float(unit[1]) * 1024 ** (
                " KMGTPE".index(unit[2].upper()) if unit[2] else 0
            )
        elif field == "elapsed":
            number = table_sort._duration(label)
            if number is None:
                raise ValueError("Elapsed uses seconds, HH:MM:SS, or units such as 2h")
        elif field in ("cpu_eff", "mem_eff"):
            number = (
                float(label.removesuffix("%")) / 100
                if label.endswith("%")
                else float(label)
            )
        else:
            number = float(label)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid {field} threshold: {label}") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError("Use a finite, nonnegative threshold")
    return field, operator, number, label


def validate_numeric(value):
    if not isinstance(value, (list, tuple)) or len(value) > MAX_RULES:
        raise ValueError("At most 16 numeric filters are supported")
    result = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 4:
            raise ValueError("Invalid numeric filter")
        field, operator, number, label = item
        if (
            field not in NUMERIC_FIELDS
            or operator not in (">", ">=", "<", "<=", "=", "!=")
            or isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            or not isinstance(label, str)
            or not 1 <= len(label) <= 64
        ):
            raise ValueError("Invalid numeric filter")
        parsed = parse_rule(field + operator + label)
        if parsed[2] != number:
            raise ValueError("Numeric filter label and threshold disagree")
        result.append((field, operator, float(number), label))
    return result


def validate_dates(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"start", "end", "label"}:
        raise ValueError("Invalid History date range")
    start, end, label = value["start"], value["end"], value["label"]
    if (
        any(
            isinstance(v, bool)
            or not isinstance(v, (float, int))
            or not math.isfinite(v)
            for v in (start, end)
        )
        or start >= end
        or not isinstance(label, str)
        or len(label) > 128
    ):
        raise ValueError("Invalid History date range")
    return dict(value)


def filter_text(app, tab):
    state = getattr(app, "table_state", {})
    if isinstance(state, dict) and isinstance(state.get("filters"), dict):
        legacy = (
            getattr(app, "__dict__", {}).get("filter", "")
            if tab == getattr(app, "tab", "")
            else ""
        )
        text = state["filters"].get(tab, legacy)
        return text if isinstance(text, str) else ""
    return getattr(app, "_filter", getattr(app, "__dict__", {}).get("filter", ""))


def numeric_rules(app, tab):
    value = _state(app).get("numeric", {}).get(tab, [])
    return value if isinstance(value, (list, tuple)) else []


def extra_fingerprint(app, tab):
    state = _state(app)
    dates = state.get("dates") if tab == "history" else None
    recent = state.get("recents", {}) if tab == "recent" else {}
    return (
        filter_text(app, tab),
        tuple(tuple(rule) for rule in numeric_rules(app, tab)),
        tuple(sorted(dates.items())) if isinstance(dates, dict) else None,
        tuple(sorted(recent.items())) if isinstance(recent, dict) else (),
    )


def numeric_value(app, record, snap, field):
    if isinstance(record, dict):
        return record.get(field)
    if field == "memory":
        if hasattr(record, "mem_total"):
            return record.mem_total * 1024**2 if record.mem_total else None
        return (
            record.mem_bytes
            if isinstance(record, Job) and record.mem_req
            else (
                record.req_mem
                if isinstance(record, Finished) and record.req_mem
                else None
            )
        )
    if field == "elapsed":
        return secs(getattr(record, "elapsed", ""))
    live = snap.get("live", {}).get(getattr(record, "id", ""))
    if field == "rss":
        if hasattr(record, "mem_free"):
            return (
                (record.mem_total - record.mem_free) * 1024**2
                if record.mem_free is not None and record.mem_total
                else None
            )
        return (
            live.rss if live else record.rss if isinstance(record, Finished) else None
        )
    if field == "cpu_eff":
        return (
            live.avg
            if live
            else record.cpu_eff
            if isinstance(record, Finished)
            else None
        )
    if field == "mem_eff":
        if live and live.rss is not None and record.mem_bytes:
            return live.rss / record.mem_bytes
        return record.mem_eff if isinstance(record, Finished) else None
    return getattr(record, field, None)


def numeric_matches(app, tab, record, snap):
    for field, operator, target, _ in numeric_rules(app, tab):
        actual = numeric_value(app, record, snap, field)
        if (
            isinstance(actual, bool)
            or not isinstance(actual, (int, float))
            or not math.isfinite(actual)
        ):
            return False
        if not {
            ">": actual > target,
            ">=": actual >= target,
            "<": actual < target,
            "<=": actual <= target,
            "=": actual == target,
            "!=": actual != target,
        }[operator]:
            return False
    return True


def history_matches(app, record):
    interval = _state(app).get("dates")
    if not interval:
        return True
    ended = stamp(getattr(record, "end", ""))
    return ended is not None and interval["start"] <= ended < interval["end"]


def date_label(app):
    interval = _state(app).get("dates")
    return interval["label"] if interval else "available History window"


def recent_limit(app):
    state = _state(app)
    settings = state.get("recents", {})
    base = settings.get("count", 5)
    if state.get("expand_recent"):
        return 25
    if settings.get("auto"):
        return min(
            25, max(base, getattr(getattr(app, "completion", None), "new_history", 0))
        )
    return base


def recent_matches(app, record):
    window = _state(app).get("recents", {}).get("window")
    if not window or isinstance(record, Job):
        return True
    ended = stamp(getattr(record, "end", ""))
    return ended is not None and clock.now() - window <= ended <= clock.now()


def page_size(app, tab, fallback=10):
    value = _state(app).get("pages", {}).get(tab)
    if not isinstance(value, int) or value < 1:
        kinds = {
            "jobs": ("job", "recent"),
            "history": ("fin",),
            "group": ("group",),
            "sources": ("source",),
            "nodes": ("node_row",),
            "cluster": ("partition_row",),
        }.get(tab, ())
        count = len(
            {y for y, kind, _ in getattr(app, "last_hits", []) if kind in kinds}
        )
        value = count or fallback
    return max(1, value - 1)


def snapshot(app, snap):
    frozen = _state(app).get("freeze")
    return frozen if isinstance(frozen, dict) else snap


def freeze_status(app, live_snap=None):
    state = _state(app)
    if not state.get("freeze"):
        return ""
    age = max(0, clock.now() - state["freeze_time"])
    if live_snap is None:
        return f"FROZEN {age:.0f}s | :freeze off resumes"
    before = state["freeze_ids"]
    current = {job.id: job.state for job in live_snap.get("jobs", [])}
    changed = len(set(before) ^ set(current)) + sum(
        before[key] != current[key] for key in before.keys() & current.keys()
    )
    return f"FROZEN {age:.0f}s | {changed} queue changes | :freeze off resumes"


def focused_column(app):
    header = _state(app).get("header")
    return (header["table"], header["column"]) if isinstance(header, dict) else None


def facets_fingerprint(app, tab):
    facets = getattr(app, "table_state", {}).get("facets", {}).get(tab, {})
    return tuple(sorted(facets.items())) if isinstance(facets, dict) else ()


def fingerprint(app, tab):
    """Selection cache identity includes both filters and explicit sort state."""
    from .ui.list_preferences import fingerprint as job_list_fingerprint

    return (
        facets_fingerprint(app, tab),
        sort_fingerprint(app, tab),
        extra_fingerprint(app, tab),
        getattr(app, "table_state", {}).get("auto_groups", True) is True,
        job_list_fingerprint(app, tab)
        if tab in ("jobs", "recent", "history", "group")
        else None,
    )


def matches(app, tab, record, snap):
    facets = getattr(app, "table_state", {}).get("facets", {}).get(tab, {})
    rules = getattr(app, "table_tools_state", {}).get("numeric", {}).get(tab, ())
    active_numeric = isinstance(rules, (list, tuple)) and bool(rules)
    # Most table passes have no field filters. Read current settings directly
    # so in-place changes take effect, without preparing unused record fields
    # or sorting a fingerprint for every row in a large accounting window.
    if isinstance(facets, dict) and facets:
        get = (
            record.get
            if isinstance(record, dict)
            else lambda key, default="": getattr(record, key, default)
        )
        for field, expected in facets.items():
            if field == "tag":
                value = " ".join(
                    snap.get("tags", {}).get(get("id", ""), {}).get("tags", [])
                )
            elif field == "state":
                value = (
                    "ACCOUNTING"
                    if get("id", "") in snap.get("departed_jobs", {})
                    else get("state", "")
                )
            elif field == "user":
                value = get("user", "") or "?"
            else:
                value = get(field, "") if field in FIELDS else ""
            actual = str(value).casefold()
            choices = [part.casefold() for part in str(expected).split(",")]
            if field in ("state", "partition", "id", "user"):
                if actual not in choices:
                    return False
            elif field == "tag":
                if not any(choice in actual.split() for choice in choices):
                    return False
            elif not any(choice in actual for choice in choices):
                return False
    if active_numeric:
        return numeric_matches(app, tab, record, snap)
    return True


def set_filter_text(app, tab, text):
    state = getattr(app, "table_state", None)
    if isinstance(state, dict):
        state.setdefault("filters", {})[tab] = str(text)[:256]
        if "filter" in getattr(app, "__dict__", {}) and tab == getattr(app, "tab", ""):
            app.filter = str(text)[:256]
    else:
        app._filter = str(text)[:256]
