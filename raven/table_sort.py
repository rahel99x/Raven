"""Stable, reversible column sorting shared by terminal tables.

The absence of a table override retains the historical default sort. An empty
override is deliberately different: the table returns to its source order.
Columns are ordered by priority, and unknown values remain last in either
direction. No sort reads files or queries the scheduler.
"""
from __future__ import annotations

import math
import re
from typing import Any

from .model import secs, stamp


TABLE_KEYS = {
    "jobs": ("id", "name", "progress", "part", "st", "where", "cpus", "gpu", "time", "left",
             "cpu%", "eff", "mem%", "gpu%", "flags", "tags", "info"),
    "history": ("id", "name", "state", "part", "elapsed", "cpus", "gpus", "ce", "me",
                "rss", "start", "end", "exit", "nodes", "tags", "info"),
    "group": ("user", "id", "name", "part", "st", "where", "cpus", "gpu", "time", "prio", "info"),
    "nodes": ("name", "state", "cpus", "load", "loadpct", "mem", "gres", "gused", "gutil", "jobs"),
    "sources": ("name", "state", "every", "last", "latency", "calls", "errors", "backoff", "error"),
    "cluster": ("name", "avail", "limit", "nodes", "nidle", "nalloc", "nother", "cidle", "calloc", "mine", "minep", "gpus"),
}
TABLE_KEYS["recent"] = TABLE_KEYS["history"]
DIRECTIONS = ("asc", "desc")
MISSING_TEXT = frozenset(("", "-", "?", "n/a", "na", "none", "null", "(null)",
                          "unknown", "not_set"))
NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
MEMORY = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([kmgtpe]?)(?:i?b)?$", re.I)
DURATION = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([dhms])", re.I)


def validate_chain(table: str, value: Any) -> list[tuple[str, str]]:
    """Validate a complete persisted/requested chain before applying it."""
    if not isinstance(table, str) or table not in TABLE_KEYS or not isinstance(value, (list, tuple)) or len(value) > len(TABLE_KEYS[table]):
        raise ValueError("invalid column sort list")
    result, seen = [], set()
    for entry in value:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise ValueError("column sorts use [column, asc|desc]")
        key, direction = entry
        if (not isinstance(key, str) or key not in TABLE_KEYS[table] or
                not isinstance(direction, str) or direction not in DIRECTIONS or key in seen):
            raise ValueError("invalid or repeated sort column")
        result.append((key, direction))
        seen.add(key)
    return result


def chain(app, table: str) -> list[tuple[str, str]] | None:
    state = getattr(app, "table_state", {})
    sorts = state.get("sorts", {}) if isinstance(state, dict) else {}
    if not isinstance(sorts, dict) or table not in sorts:
        return None
    try:
        return validate_chain(table, sorts[table])
    except ValueError:
        return None


def sort_fingerprint(app, table: str):
    value = chain(app, table)
    return None if value is None else tuple(value)


def describe_sort(app, table: str) -> str:
    value = chain(app, table)
    if value is None:
        key = getattr(app, "sort", {}).get(table, "name")
        reverse = getattr(app, "reverse", {}).get(table, False)
        return f"sorted by {key}" + (" (reversed)" if reverse else "")
    if not value:
        return "source order"
    return "sorted by " + ", then ".join(f"{key} {direction}" for key, direction in value)


# Renderer-facing alias: the complete label also covers inherited/default sorts.
describe = describe_sort


def set_sort(app, table: str, column: str, direction: str) -> list[tuple[str, str]]:
    """Set/remove one column without changing the precedence of other columns."""
    if (table not in TABLE_KEYS or not isinstance(column, str) or column not in TABLE_KEYS[table] or
            direction not in (*DIRECTIONS, "off")):
        raise ValueError("choose a table column and asc, desc or off")
    existing = chain(app, table) or []
    result = [(key, direction if key == column else previous)
              for key, previous in existing if key != column or direction != "off"]
    if direction != "off" and not any(key == column for key, _ in result):
        result.append((column, direction))
    app.table_state.setdefault("sorts", {})[table] = result
    return list(result)


def cycle_sort(app, table: str, column: str) -> list[tuple[str, str]]:
    existing = dict(chain(app, table) or [])
    current = existing.get(column)
    direction = "asc" if current is None else "desc" if current == "asc" else "off"
    result = set_sort(app, table, column, direction)
    app.say(describe_sort(app, table))
    return result


def clear_sort(app, table: str) -> None:
    if table not in TABLE_KEYS:
        raise ValueError("invalid table")
    app.table_state.setdefault("sorts", {})[table] = []


def reset_sort(app, table: str) -> None:
    """Resume the legacy keyboard-selected default for a table."""
    sorts = getattr(app, "table_state", {}).get("sorts", {})
    if isinstance(sorts, dict):
        sorts.pop(table, None)


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _duration(value):
    if isinstance(value, (int, float)):
        return _number(value)
    text = str(value).strip()
    if text.casefold() in ("unlimited", "infinite"):
        # A finite number stays comparable without promoting unknowns to zero.
        return float("inf")
    if text.casefold().startswith("waited "):
        text = text[7:]
    if text.casefold().endswith(" ago"):
        text = text[:-4]
    parsed = secs(text)
    if parsed is not None:
        return _number(parsed)
    matches = list(DURATION.finditer(text))
    if not matches or DURATION.sub("", text).strip():
        return None
    return _number(sum(float(match[1]) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[match[2].lower()]
                       for match in matches))


def _memory(value):
    if isinstance(value, (int, float)):
        return _number(value)
    match = MEMORY.fullmatch(str(value).strip())
    if not match:
        return None
    number = _number(match[1])
    return None if number is None else number * 1024 ** (" kmgtpe".index(match[2].lower()) if match[2] else 0)


def _natural(text: str):
    # Tagged chunks avoid int/string comparisons for unlike IDs or node names.
    def token(chunk):
        if chunk.isascii() and chunk.isdecimal():
            digits = chunk.lstrip("0") or "0"
            return 1, (len(digits), digits)
        return 0, chunk
    return tuple(token(chunk) for chunk in re.split(r"([0-9]+)", text.casefold()) if chunk)


def _kind(table: str, key: str):
    if key in ("id", "exit"):
        return "text"
    if key in ("start", "end") and table in ("history", "recent"):
        return "date"
    if key in ("elapsed", "time", "left", "limit", "every", "last", "backoff"):
        return "duration"
    if key in ("rss", "mem"):
        return "memory"
    if key in ("cpus", "gpus", "gpu", "cpu%", "eff", "mem%", "gpu%", "ce", "me", "load", "loadpct", "gutil",
               "prio", "nidle", "nalloc", "nother", "cidle", "calloc", "mine", "minep", "latency", "calls", "errors"):
        return "number"
    if key == "nodes" and table == "cluster":
        return "number"
    return "text"


def normalize(value, kind="text"):
    """Return a comparable tagged key, or None for an unknown cell.

    Native numeric and tuple values take precedence over presentation parsers.
    Tuple elements have their own type tags so heterogeneous IDs never raise.
    """
    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        parts = []
        for item in value:
            parsed = normalize(item, kind)
            parts.append((1, ()) if parsed is None else (0, parsed))
        return (2, tuple(parts)) if any(part[0] == 0 for part in parts) else None
    if isinstance(value, (int, float)):
        number = _number(value)
        return None if number is None else (0, number)
    if not isinstance(value, str):
        value = str(value)
    text = value.strip()
    if text.casefold() in MISSING_TEXT:
        return None
    if kind == "date":
        number = stamp(text)
        return None if number is None else (0, number)
    if kind == "duration":
        # ELAPSED/LIMIT is ordered by elapsed, then by the limit for ties.
        if "/" in text:
            return normalize(tuple(text.split("/", 1)), "duration")
        number = _duration(text)
        return None if number is None else (0, number)
    if kind == "memory":
        if "/" in text:
            left, right = text.split("/", 1)
            suffix = re.search(r"\s*([kmgtpe](?:i?b)?)\s*$", right, re.I)
            left = left + suffix[1] if suffix and not re.search(r"[a-z]", left, re.I) else left
            return normalize((_memory(left), _memory(right)), "number")
        number = _memory(text)
        return None if number is None else (0, number)
    if kind == "number":
        if "/" in text:
            return normalize(tuple(_number(part.strip()) for part in text.split("/", 1)), "number")
        percent = text.removesuffix("%").strip()
        if key_match := re.fullmatch(r"(.+?)\s*(?:ms|s)", percent, re.I):
            percent = key_match[1]
        if gpu_match := re.fullmatch(r"([0-9]+)x(.+)", percent):
            return normalize((int(gpu_match[1]), gpu_match[2]), "text")
        number = _number(percent) if NUMBER.fullmatch(percent) else None
        return None if number is None else (0, number)
    return (1, _natural(text))


def sort_rows(app, table: str, rows, value=None):
    """Sort all rows before viewport slicing; ties preserve the source sequence."""
    result = list(rows)
    sorts = chain(app, table)
    if not sorts:
        return result
    if value is None:
        def value(row, key):
            raw = row.get("_sort", {}) if isinstance(row, dict) else {}
            if isinstance(raw, dict) and key in raw:
                return raw[key]
            return row.get(key) if isinstance(row, dict) else getattr(row, key, None)
    for key, direction in reversed(sorts):
        kind = _kind(table, key)
        known, unknown = [], []
        for row in result:
            parsed = normalize(value(row, key), kind)
            if parsed is None:
                unknown.append(row)
            else:
                known.append((parsed, row))
        known.sort(key=lambda item: item[0], reverse=direction == "desc")
        result = [row for _, row in known] + unknown
    return result


def history_value(record, key: str, snap=None):
    """Typed accounting/recent values, independent of abbreviated cell text."""
    from .model import Job
    if key == "id":
        return record.id
    if key == "name":
        return record.name
    if key == "state":
        return "ACCOUNTING" if isinstance(record, Job) else record.state
    if key == "info":
        # Sorting precedes presentation grouping, as it does in the live table.
        # Use the allocation's real status; a group header remains its first
        # visible record and never masquerades as an aggregate allocation.
        return (record.state, getattr(record, "reason", ""))
    if key == "part":
        return record.partition
    if key == "elapsed":
        return secs(record.elapsed)
    if key in ("cpus", "gpus"):
        return getattr(record, key, None)
    if key in ("ce", "me"):
        return getattr(record, "cpu_eff" if key == "ce" else "mem_eff", None)
    if key == "rss":
        value = getattr(record, "rss", None)
        return value if value else None
    if key in ("start", "end"):
        return stamp(getattr(record, key, None))
    if key == "exit":
        return getattr(record, "exit", None)
    if key == "nodes":
        return getattr(record, "nodelist", None)
    if key == "tags":
        tags = (snap or {}).get("tags", {})
        entry = tags.get(record.id, {}) if isinstance(tags, dict) else {}
        values = entry.get("tags", []) if isinstance(entry, dict) else []
        return " ".join(tag for tag in values if isinstance(tag, str)) if isinstance(values, list) else ""
    return None


def valid_header(payload) -> bool:
    if not isinstance(payload, (tuple, list)) or len(payload) != 4:
        return False
    table, key, x0, x1 = payload
    return (isinstance(table, str) and table in TABLE_KEYS and isinstance(key, str) and key in TABLE_KEYS[table]
            and type(x0) is int and type(x1) is int and 0 <= x0 < x1 <= 100000)


def header_hits(table: str, cells, y: int):
    if type(y) is not int or y < 0 or table not in TABLE_KEYS:
        return []
    result = []
    for cell in cells:
        if not isinstance(cell, (tuple, list)) or len(cell) != 3:
            continue
        payload = (table, *cell)
        if valid_header(payload):
            result.append((y, "sort_header", payload))
    return result
