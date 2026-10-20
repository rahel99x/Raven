"""Bounded Slurm array membership arithmetic without submission adapters."""

from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from itertools import islice
from typing import Iterable

MAX_INDEX = (1 << 63) - 1


MAX_PARTS = 1024


MAX_FRAGMENTS = 4096


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
        return {
            "start": self.start,
            "end": self.end,
            "step": self.step,
            "count": self.count,
        }


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
    factor = (
        0
        if modulus == 1
        else (difference // divisor * pow(a.step // divisor, -1, modulus)) % modulus
    )
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
                result.append(
                    _Range(
                        start,
                        start + ((hit.end - start) // hit.step) * hit.step,
                        hit.step,
                    )
                )
        if hit.end < a.end:
            result.append(_Range(hit.end + a.step, a.end, a.step))
    return result


def _difference(ranges: list[_Range], occupied: list[_Range]) -> list[_Range]:
    return _difference_parts(
        ranges,
        {r.start for r in occupied if r.count == 1},
        [r for r in occupied if r.count != 1],
    )


def _difference_parts(
    ranges: list[_Range], points: set[int], runs: list[_Range]
) -> list[_Range]:
    # Thousands of individual tasks must not produce quadratic scans. Points
    # use constant-time membership; deleting many points from a run uses one
    # sorted pass rather than repeatedly splitting an ever-growing list.
    if len(ranges) == 1 and ranges[0].count == 1:
        item = ranges[0]
        if item.start in points or any(
            _intersection(item, r) is not None for r in runs
        ):
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
