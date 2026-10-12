"""Bounded job-array accounting and inspectable retry plans.

Task identities are arithmetic progressions, never expanded to make a summary.
Current records override history; individual task records override compressed
rows from the same source. A plain numeric job is not an array declaration.
When Slurm supplies only individual records, the unseen size stays unknown.
"""
from __future__ import annotations

import heapq
import math
import re
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass
from itertools import islice
from typing import Any, Iterable, Mapping

from .model import secs

MAX_INDEX = (1 << 63) - 1
MAX_PARTS = 1024
MAX_FRAGMENTS = 4096
MAX_RECORDS = 50000
MAX_SEGMENTS = 50000
FAILED_STATES = frozenset({"FAILED", "OUT_OF_MEMORY", "TIMEOUT", "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "PREEMPTED"})
ACTIVE_STATES = frozenset({"RUNNING", "COMPLETING", "CONFIGURING", "SUSPENDED", "RESIZING", "SIGNALING", "STAGE_OUT"})
_ID = re.compile(r"^([0-9]+)_([0-9]+|\[[^\]]+\])$")
_ARRAY_PREFIX = re.compile(r"^([0-9]+)_(.*)$")
_PART = re.compile(r"^([0-9]+)(?:-([0-9]+)(?::([0-9]+))?)?$")


@dataclass(frozen=True)
class _Range:
    start: int
    end: int
    step: int = 1

    @property
    def count(self) -> int:
        return (self.end - self.start) // self.step + 1

    def plain(self) -> dict:
        return {"start": self.start, "end": self.end, "step": self.step, "count": self.count}


def _number(value: str) -> int:
    if not value or len(value) > 19 or not value.isascii() or not value.isdigit():
        raise ValueError("array indices must be nonnegative decimal integers")
    number = int(value)
    if number > MAX_INDEX:
        raise ValueError("array index exceeds the supported 63-bit bound")
    return number


def _parse(spec: str) -> tuple[list[_Range], int | None]:
    if not isinstance(spec, str) or not spec or len(spec) > 65536:
        raise ValueError("array specification must be a nonempty bounded string")
    if spec.startswith("[") and spec.endswith("]"):
        spec = spec[1:-1]
    throttle = None
    if "%" in spec:
        if spec.count("%") != 1:
            raise ValueError("array specification has multiple concurrency limits")
        spec, value = spec.split("%")
        throttle = _number(value)
        if throttle == 0:
            raise ValueError("array concurrency must be positive")
    parts = spec.split(",")
    if len(parts) > MAX_PARTS:
        raise ValueError("too many array range fragments")
    result = []
    for part in parts:
        match = _PART.fullmatch(part)
        if not match:
            raise ValueError(f"invalid array range: {part[:80]}")
        start = _number(match[1])
        end = _number(match[2]) if match[2] else start
        step = _number(match[3]) if match[3] else 1
        if end < start or step == 0:
            raise ValueError("array ranges must ascend with a positive stride")
        end = start + ((end - start) // step) * step
        result.append(_Range(start, end, step))
    return result, throttle


def _intersection(a: _Range, b: _Range) -> _Range | None:
    lo, hi = max(a.start, b.start), min(a.end, b.end)
    divisor = math.gcd(a.step, b.step)
    difference = b.start - a.start
    if lo > hi or difference % divisor:
        return None
    modulus = b.step // divisor
    factor = 0 if modulus == 1 else (difference // divisor * pow(a.step // divisor, -1, modulus)) % modulus
    step = a.step * modulus
    first = a.start + a.step * factor
    first += ((lo - first + step - 1) // step) * step
    if first > hi:
        return None
    return _Range(first, first + ((hi - first) // step) * step, step)


def _subtract(a: _Range, b: _Range) -> list[_Range]:
    """a minus b, choosing the smaller exact progression decomposition."""
    hit = _intersection(a, b)
    if hit is None:
        return [a]
    if hit.step == a.step:
        result = []
        if a.start < hit.start:
            result.append(_Range(a.start, hit.start - a.step, a.step))
        if hit.end < a.end:
            result.append(_Range(hit.end + a.step, a.end, a.step))
        return result
    residues = hit.step // a.step - 1
    if min(hit.count + 1, residues + 2) > MAX_FRAGMENTS:
        raise ValueError("overlapping strides exceed bounded accounting complexity")
    result = []
    if hit.count + 1 <= residues + 2:
        cursor = a.start
        for point in range(hit.start, hit.end + 1, hit.step):
            if cursor < point:
                result.append(_Range(cursor, point - a.step, a.step))
            cursor = point + a.step
        if cursor <= a.end:
            result.append(_Range(cursor, a.end, a.step))
    else:
        if a.start < hit.start:
            result.append(_Range(a.start, hit.start - a.step, a.step))
        for residue in range(1, residues + 1):
            start = hit.start + residue * a.step
            if start <= hit.end:
                result.append(_Range(start, start + ((hit.end - start) // hit.step) * hit.step, hit.step))
        if hit.end < a.end:
            result.append(_Range(hit.end + a.step, a.end, a.step))
    return result


def _difference(ranges: list[_Range], occupied: list[_Range]) -> list[_Range]:
    return _difference_parts(ranges, {r.start for r in occupied if r.count == 1}, [r for r in occupied if r.count != 1])


def _difference_parts(ranges: list[_Range], points: set[int], runs: list[_Range]) -> list[_Range]:
    # Thousands of individual tasks must not produce quadratic scans. Points
    # use constant-time membership; deleting many points from a run uses one
    # sorted pass rather than repeatedly splitting an ever-growing list.
    if len(ranges) == 1 and ranges[0].count == 1:
        item = ranges[0]
        if item.start in points or any(_intersection(item, r) is not None for r in runs):
            return []
        return ranges
    for other in runs:
        ranges = [piece for item in ranges for piece in _subtract(item, other)]
        if len(ranges) > MAX_FRAGMENTS:
            raise ValueError("array accounting exceeds bounded fragment count")
        if not ranges:
            break
    if not points or not ranges:
        return ranges
    ordered = sorted(points)
    result = []
    for item in ranges:
        cursor = item.start
        left, right = bisect_left(ordered, item.start), bisect_right(ordered, item.end)
        for point in islice(ordered, left, right):
            if point < cursor or (point - item.start) % item.step:
                continue
            if cursor < point:
                result.append(_Range(cursor, point - item.step, item.step))
            cursor = point + item.step
            if len(result) > MAX_FRAGMENTS:
                raise ValueError("array accounting exceeds bounded fragment count")
        if cursor <= item.end:
            result.append(_Range(cursor, item.end, item.step))
    if len(result) > MAX_FRAGMENTS:
        raise ValueError("array accounting exceeds bounded fragment count")
    return result


def _union(ranges: Iterable[_Range]) -> list[_Range]:
    result: list[_Range] = []
    for item in ranges:
        result.extend(_difference([item], result))
        if len(result) > MAX_FRAGMENTS:
            raise ValueError("array accounting exceeds bounded fragment count")
    return result


def parse_range(spec: str) -> list[dict]:
    """Validate and normalize a Slurm array spec without materializing tasks.

    Optional brackets and a positive final ``%concurrency`` are accepted. The
    returned disjoint progressions contain start/end/step/count; the concurrency
    limit does not change membership. Excessively complex overlap is rejected.
    """
    return [item.plain() for item in _union(_parse(spec)[0])]


def compact(ranges: Iterable[Mapping[str, Any]]) -> str:
    """Format arithmetic progressions as an exact comma-separated array spec."""
    pieces = []
    for position, item in enumerate(islice(ranges, MAX_SEGMENTS + 1)):
        if position == MAX_SEGMENTS:
            raise ValueError("too many array range fragments")
        start, end, step = item["start"], item["end"], item.get("step", 1)
        if any(not isinstance(value, int) or isinstance(value, bool) for value in (start, end, step)):
            raise ValueError("array range fields must be integers")
        if not 0 <= start <= end <= MAX_INDEX or not 1 <= step <= MAX_INDEX:
            raise ValueError("invalid array range")
        end = start + ((end - start) // step) * step
        pieces.append(_Range(start, end, step))
    pieces.sort(key=lambda r: (r.start, r.end, r.step))
    merged: list[_Range] = []
    for item in pieces:
        if merged and item.step == merged[-1].step and item.start == merged[-1].end + item.step:
            previous = merged.pop()
            merged.append(_Range(previous.start, item.end, item.step))
        else:
            merged.append(item)
    return ",".join(str(r.start) if r.start == r.end else f"{r.start}-{r.end}" + (f":{r.step}" if r.step != 1 else "") for r in merged)


def _get(record: Any, key: str, default: Any = "") -> Any:
    return record.get(key, default) if isinstance(record, Mapping) else getattr(record, key, default)


def _records(records: Iterable[Any] | Mapping) -> Iterable[Any]:
    if isinstance(records, Mapping):
        return [records] if "id" in records or "jobid" in records else records.values()
    return records


def _state(value: Any) -> str:
    value = str(value or "UNKNOWN").upper().split(" ", 1)[0].rstrip("+")
    return {"R": "RUNNING", "PD": "PENDING", "CG": "COMPLETING", "CD": "COMPLETED", "F": "FAILED", "TO": "TIMEOUT", "OOM": "OUT_OF_MEMORY", "CA": "CANCELLED"}.get(value, value)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    point = (len(values) - 1) * percentile
    lo = int(point)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (point - lo)


def summarize(jobs: Iterable[Any], finished: Iterable[Any] = (), *, max_groups: int = 128, max_cells: int = 256) -> list[dict]:
    """Summarize observed array identities, with conservative unknown coverage.

    An optional parent record's ``array_spec``/``array_tasks``/``array`` declares
    full membership. Otherwise ``total`` means observed identities, and
    ``unobserved`` is null because the full submitted size cannot be inferred.
    Inputs and overlap arithmetic have fixed bounds; rejected complex records
    set ``exact=False`` and disable retries rather than silently overcounting.
    """
    if not isinstance(max_groups, int) or isinstance(max_groups, bool) or not 0 <= max_groups <= 5000:
        raise ValueError("max_groups must be between 0 and 5000")
    if not isinstance(max_cells, int) or isinstance(max_cells, bool) or not 0 <= max_cells <= 4096:
        raise ValueError("max_cells must be between 0 and 4096")
    groups: dict[tuple[str, str], dict] = {}
    sequence = 0
    input_truncated = False
    for source, records in (("history", finished), ("current", jobs)):
        for position, record in enumerate(islice(_records(records), MAX_RECORDS + 1)):
            if position == MAX_RECORDS:
                input_truncated = True
                break
            sequence += 1
            identity = str(_get(record, "id", _get(record, "jobid")))
            # Steps describe allocation/rank work, not additional array tasks.
            if "." in identity:
                continue
            match = _ID.fullmatch(identity)
            declaration = ""
            if not match and identity.isascii() and identity.isdigit():
                declaration = _get(record, "array_spec") or _get(record, "array_tasks") or _get(record, "array")
                if not declaration:
                    continue
                base, spec, individual = identity, declaration, False
            elif match:
                base, spec = match[1], match[2]
                individual = not spec.startswith("[")
            else:
                prefix = _ARRAY_PREFIX.fullmatch(identity)
                if not prefix:
                    continue
                base, spec, individual = prefix[1], prefix[2], False
            try:
                _number(base)
            except ValueError:
                continue
            cluster = str(_get(record, "cluster", _get(record, "cluster_name")))
            key = (cluster, base)
            if key not in groups:
                if len(groups) >= max_groups:
                    continue
                groups[key] = {"id": base, "cluster": cluster, "name": str(_get(record, "name")), "records": [], "declared": [], "exact": True, "warnings": [], "input_parts": 0, "submit_times": []}
            group = groups[key]
            submitted = str(_get(record, "submit"))
            if submitted and submitted.upper() not in ("UNKNOWN", "N/A", "NONE") and len(group["submit_times"]) < 2 and submitted not in group["submit_times"]:
                group["submit_times"].append(submitted)
            try:
                if not match and not declaration:
                    raise ValueError("malformed array task identity")
                parsed, throttle = _parse(spec)
            except ValueError:
                group["exact"] = False
                warning = "malformed array task identity or declaration in observed records"
                if warning not in group["warnings"]:
                    group["warnings"].append(warning)
                continue
            group["input_parts"] += len(parsed)
            if group["input_parts"] > MAX_RECORDS:
                group["exact"] = False
                warning = "input range fragments exceed bounded observation count"
                if warning not in group["warnings"]:
                    group["warnings"].append(warning)
                continue
            if declaration:
                group["declared"].extend(parsed)
                continue
            timestamp = str(_get(record, "end") or _get(record, "start") or _get(record, "submit"))
            raw_duration = _get(record, "elapsed_s", None)
            duration = raw_duration if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool) else secs(str(_get(record, "elapsed")))
            try:
                valid_duration = duration is None or (math.isfinite(duration) and duration >= 0)
            except (TypeError, OverflowError):
                valid_duration = False
            if not valid_duration:
                duration = None
            group["records"].append({"ranges": parsed, "state": _state(_get(record, "state")), "source": source, "individual": individual,
                                     "duration": duration if individual else None, "name": str(_get(record, "name")),
                                     "rank": (source == "current", individual, timestamp, sequence), "concurrency": throttle})
    result = []
    for group in groups.values():
        group.pop("input_parts")
        if input_truncated:
            group["exact"] = False
            group["warnings"].append("input records exceed bounded observation count")
        occupied: list[_Range] = []
        occupied_points: set[int] = set()
        occupied_runs: list[_Range] = []
        segments = []
        named = False
        for record in sorted(group.pop("records"), key=lambda r: r["rank"], reverse=True):
            try:
                remaining = _difference_parts(_union(record["ranges"]), occupied_points, occupied_runs)
                if len(remaining) + len(occupied) > MAX_SEGMENTS:
                    raise ValueError("array observation segments exceed bounded observation count")
            except ValueError as exc:
                group["exact"] = False
                if str(exc) not in group["warnings"]:
                    group["warnings"].append(str(exc))
                continue
            for item in remaining:
                segments.append({**item.plain(), "state": record["state"], "source": record["source"], "observed": True,
                                 "individual": record["individual"], "duration": record["duration"]})
            occupied.extend(remaining)
            occupied_points.update(r.start for r in remaining if r.count == 1)
            occupied_runs.extend(r for r in remaining if r.count != 1)
            if record["name"] and not named:
                group["name"] = record["name"]
                named = True
        declared = group.pop("declared")
        group["total_known"] = bool(declared)
        unobserved = None
        if declared:
            try:
                declared = _union(declared)
                outside = _difference(occupied, declared)
                if outside:
                    group["exact"] = False
                    group["total_known"] = False
                    group["warnings"].append("observed task identities contradict the declared array membership")
                missing = _difference_parts(declared, occupied_points, occupied_runs)
                unobserved = sum(r.count for r in missing)
                segments.extend({**r.plain(), "state": "UNKNOWN", "source": "declaration", "observed": False, "individual": False, "duration": None} for r in missing)
            except ValueError as exc:
                group["exact"] = False
                group["total_known"] = False
                group["warnings"].append(str(exc))
        states = Counter()
        for segment in segments:
            states[segment["state"]] += segment["count"]
        group.update(total=sum(states.values()), observed=sum(s["count"] for s in segments if s["observed"]), states=dict(states), unobserved=unobserved,
                     completed=states["COMPLETED"], running=sum(states[s] for s in ACTIVE_STATES), pending=states["PENDING"], unknown=states["UNKNOWN"],
                     failed=sum(states[s] for s in FAILED_STATES), _segments=segments)
        group["ranges"] = {state: compact(s for s in segments if s["state"] == state) for state in states}
        group["failures"] = compact(s for s in segments if s["state"] in FAILED_STATES and s["observed"])
        durations = sorted(s["duration"] for s in segments if s["individual"] and s["state"] == "COMPLETED" and s["duration"] is not None)
        group["duration"] = {"samples": len(durations), "p50": _percentile(durations, .5), "p90": _percentile(durations, .9), "p95": _percentile(durations, .95), "max": max(durations) if durations else None, "scope": "individually observed completed tasks"}
        baseline = group["duration"]["p50"]
        group["outliers"] = [{"index": s["start"], "state": s["state"], "duration": s["duration"]} for s in segments
                             if len(durations) >= 4 and s["individual"] and s["duration"] is not None and s["duration"] > max(60, baseline * 3)][:100]
        group["cells"] = tasks(group, limit=max_cells)
        group["truncated"] = group["total"] > len(group["cells"]) or not group["exact"]
        result.append(group)
    return sorted(result, key=lambda g: (g["cluster"], int(g["id"])))


def tasks(group: dict | Iterable[Any], *, offset: int = 0, limit: int = 100) -> list[dict]:
    """Sorted task drill-down with bounded allocation, including large offsets.

    With raw records, groups are concatenated in cluster/array order. An offset
    of one billion uses binary search over progressions, not a billion skips.
    """
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 4096:
        raise ValueError("limit must be between 0 and 4096")
    if not limit:
        return []
    if not isinstance(group, Mapping) or "_segments" not in group:
        output = []
        for summary in summarize(group, max_cells=0):
            if offset >= summary["total"]:
                offset -= summary["total"]
                continue
            output.extend(tasks(summary, offset=offset, limit=limit - len(output)))
            offset = 0
            if len(output) >= limit:
                break
        return output
    segments = group["_segments"]
    total = sum(s["count"] for s in segments)
    if not limit or offset >= total or not segments:
        return []
    low, high = min(s["start"] for s in segments), max(s["end"] for s in segments)
    while low < high:
        middle = (low + high) // 2
        count = sum(min(s["count"], (middle - s["start"]) // s["step"] + 1) for s in segments if s["start"] <= middle)
        if count > offset:
            high = middle
        else:
            low = middle + 1
    heap = []
    for number, segment in enumerate(segments):
        start = segment["start"] + max(0, (low - segment["start"] + segment["step"] - 1) // segment["step"]) * segment["step"]
        if start <= segment["end"]:
            heapq.heappush(heap, (start, number))
    result = []
    while heap and len(result) < limit:
        index, number = heapq.heappop(heap)
        segment = segments[number]
        result.append({"id": f"{group['id']}_{index}", "array": group["id"], "cluster": group.get("cluster", ""), "index": index,
                       "state": segment["state"], "source": segment["source"], "observed": segment["observed"], "duration": segment["duration"]})
        next_index = index + segment["step"]
        if next_index <= segment["end"]:
            heapq.heappush(heap, (next_index, number))
    return result


def select_failed(group: Mapping, *, indices: str | Iterable[int] | None = None) -> str:
    """Select only observed failures; reject stale/inexact or ineligible indices."""
    if not group.get("exact", False):
        raise ValueError("retry requires exact array accounting; refresh or narrow the records")
    failures = [_Range(s["start"], s["end"], s["step"]) for s in group["_segments"] if s["state"] in FAILED_STATES and s["observed"]]
    if indices is None:
        selected = failures
    else:
        if isinstance(indices, str):
            selected = _union(_parse(indices)[0])
        else:
            try:
                items = list(islice(indices, MAX_PARTS + 1))
            except TypeError as exc:
                raise ValueError("retry indices must be a range string or an iterable of integers") from exc
            if len(items) > MAX_PARTS or any(not isinstance(i, int) or isinstance(i, bool) or not 0 <= i <= MAX_INDEX for i in items):
                raise ValueError("retry indices must be at most 1024 nonnegative integers")
            selected = _union(_Range(i, i) for i in items)
        if _difference(selected, failures):
            raise ValueError("retry selection includes tasks without an observed failure")
    if not selected:
        raise ValueError("no observed failed tasks are eligible for retry")
    return compact(r.plain() for r in selected)


def retry_plan(group: Mapping, script: str, *, indices: str | Iterable[int] | None = None, limit: int | None = None, workdir: str | None = None) -> dict:
    """Prepare an exact ``sbatch --array`` plan without submitting anything."""
    specification = select_failed(group, indices=indices)
    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_INDEX:
            raise ValueError("retry concurrency must be a positive integer")
        specification += f"%{limit}"
    from .submission import prepare
    plan = prepare(script, workdir=workdir, overrides=["--array=" + specification])
    plan["array_retry"] = {"array": group["id"], "cluster": group.get("cluster", ""), "indices": specification,
                           "count": sum(r["count"] for r in parse_range(specification)), "requires_confirmation": True}
    return plan
