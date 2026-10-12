"""Bounded source-preserving terminal presentations. These functions do no I/O."""
from __future__ import annotations

from datetime import datetime, timezone
from collections import Counter
import difflib
import json
import math
import re
from itertools import islice

MAX_NODES = 4096
MAX_DEPTH = 24
_STAMP = re.compile(r"(?P<stamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?)")


def timestamp(line):
    """Return a sortable instant plus timing basis; never infer a time zone."""
    match = _STAMP.search(line[:8192])
    if not match:
        return None, "missing"
    try:
        stamp = match["stamp"]
        value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        fraction = re.search(r"\.(\d+)", stamp)
        nanoseconds = int(fraction[1].ljust(9, "0")) if fraction else 0
        value = value.replace(microsecond=0)
        if value.tzinfo is None:
            delta = value - datetime(1970, 1, 1)
            return (delta.days * 86400 + delta.seconds) * 1000000000 + nanoseconds, "naive"
        delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return (delta.days * 86400 + delta.seconds) * 1000000000 + nanoseconds, "zoned"
    except (ValueError, OverflowError):
        return None, "invalid"


def aligned(left, right):
    """Merge two sorted streams, pairing only unambiguous equal instants.

    Missing, mixed-zone, and backwards clocks fall back to positional display.
    Equal repeated timestamps remain individual observations, never fabricated
    cross-stream correlations.
    """
    clocks = [[timestamp(line) for line in stream] for stream in (left, right)]
    bases = {basis for stream in clocks for _, basis in stream}
    if bases - {"naive", "zoned"} or len(bases) > 1:
        return [(i if i < len(left) else None, i if i < len(right) else None)
                for i in range(max(len(left), len(right)))], "Timing missing or mixed; positional rows"
    if any(any(stream[i][0] < stream[i - 1][0] for i in range(1, len(stream))) for stream in clocks):
        return [(i if i < len(left) else None, i if i < len(right) else None)
                for i in range(max(len(left), len(right)))], "Clock moved backwards; positional rows"
    counts = [Counter(value for value, _ in clock) for clock in clocks]
    i = j = 0
    rows = []
    while i < len(left) or j < len(right):
        a = clocks[0][i][0] if i < len(left) else float("inf")
        b = clocks[1][j][0] if j < len(right) else float("inf")
        if a == b and counts[0].get(a) == counts[1].get(b) == 1:
            rows.append((i, j)); i += 1; j += 1
        elif a <= b and i < len(left):
            rows.append((i, None)); i += 1
        else:
            rows.append((None, j)); j += 1
    duplicate = any(count > 1 for source in counts for count in source.values())
    basis = "Unzoned wall clocks; same time zone must be confirmed" if bases == {"naive"} else "UTC timestamps"
    return rows, basis + ("; repeated timestamps shown separately" if duplicate else "; exact instants aligned")


def diff_rows(left, right, ignore_time=False):
    """Compare original lines with explicit, opt-in timestamp normalization."""
    key = lambda text: _STAMP.sub("<time>", text, count=1) if ignore_time else text
    matcher = difflib.SequenceMatcher(None, [key(x) for x in left], [key(x) for x in right], autojunk=False)
    result = []
    for tag, a, b, c, d in matcher.get_opcodes():
        if tag == "equal":
            result.extend(dict(text="  " + left[i], style="dim", left=i, right=c + i - a) for i in range(a, b))
        else:
            result.extend(dict(text="- " + left[i], style="red", left=i, right=None) for i in range(a, b))
            result.extend(dict(text="+ " + right[j], style="green", left=None, right=j) for j in range(c, d))
    return result


def parse_json(line):
    if len(line) > 65536:
        return None
    try:
        value = json.loads(line, parse_constant=lambda text: (_ for _ in ()).throw(ValueError(text)))
        pending = [(value, 0)]
        count = 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if count > MAX_NODES or depth > MAX_DEPTH:
                return None
            if isinstance(item, float) and not math.isfinite(item):
                return None
            if isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        return value
    except (ValueError, RecursionError, OverflowError):
        return None


def field_value(value, path):
    """Dot-separated fields, with numeric array indexes; no evaluation."""
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isascii() and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def _json_nodes(value, collapsed=(), prefix="", base_depth=0):
    """Iterate tree nodes without building an unbounded sibling collection."""
    closed = set(collapsed)
    def children(item, path, depth):
        pairs = item.items() if isinstance(item, dict) else enumerate(item)
        for key, child in pairs:
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            yield child, path + "/" + escaped, str(key), depth + 1
    pending = [iter([(value, "", "", base_depth)])]
    while pending:
        try:
            item, path, label, depth = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        container = isinstance(item, (dict, list))
        bounded = depth >= MAX_DEPTH or len(path) > 4096
        pretty_label = json.dumps(label, ensure_ascii=False) + ": " if label else ""
        if container:
            folded = path in closed
            suffix = ("{...}" if isinstance(item, dict) else "[...]") if folded or bounded else ("{" if isinstance(item, dict) else "[")
            if bounded:
                suffix += " (tree depth/path limit)"
            text = "  " * min(depth, MAX_DEPTH) + ("> " if folded else "v ") + pretty_label + suffix
        else:
            text = "  " * min(depth, MAX_DEPTH) + "  " + pretty_label + json.dumps(item, ensure_ascii=False, allow_nan=False)
        yield dict(text=prefix + text, node=path, expandable=container and not bounded, depth=depth)
        if container and path not in closed and not bounded:
            pending.append(iter(children(item, path, depth)))


def json_rows(value, collapsed=(), prefix="", base_depth=0):
    """Tree rows include stable JSON-pointer identities for interaction."""
    return list(islice(_json_nodes(value, collapsed, prefix, base_depth), MAX_NODES))


def json_page(value, collapsed=(), start=0, limit=128):
    rows = list(islice(_json_nodes(value, collapsed), start, start + limit + 1))
    return rows[:limit], len(rows) > limit


def structured_rows(lines, collapsed=(), field="", query=""):
    rows = []
    omitted = 0
    for index, line in enumerate(lines):
        value = parse_json(line)
        if field and (value is None or query.casefold() not in str(field_value(value, field)).casefold()):
            omitted += 1
            continue
        if value is None:
            rows.append(dict(text=f"L{index + 1} {line}", node=None, expandable=False, original=index))
            continue
        prefix = str(index) + ":"
        closed = [path[len(prefix):] for path in collapsed if path.startswith(prefix)]
        for row in json_rows(value, closed, f"L{index + 1} "):
            row.update(node=prefix + row["node"], original=index)
            rows.append(row)
    return rows, omitted


def folded_rows(lines, expanded=()):
    """Fold consecutive repeated messages, retaining the complete raw range."""
    result = []
    opened = set(expanded)
    i = 0
    while i < len(lines):
        key = _STAMP.sub("<time>", lines[i], count=1)
        end = i + 1
        while end < len(lines) and _STAMP.sub("<time>", lines[end], count=1) == key:
            end += 1
        count = end - i
        identity = f"{i}:{end}"
        if count >= 3 and identity not in opened:
            result.append(dict(text=f"> L{i + 1}-{end} {count} repetitions; {count - 1} hidden | {lines[i]}",
                               node=identity, expandable=True, original=i, last=end - 1, hidden=count - 1))
        else:
            for number in range(i, end):
                result.append(dict(text=f"L{number + 1} {lines[number]}", node=identity if count >= 3 else None,
                                   expandable=count >= 3, original=number, last=number, hidden=0))
        i = end
    return result
