"""Strict, bounded local JSON input for inspectable research plans."""
from __future__ import annotations

import json
import math
import os
import stat


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key[:120]}")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"non-finite JSON value: {value}")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number is outside the finite range")
    return number


def _integer(value):
    if len(value.lstrip("-")) > 78:
        raise ValueError("planning JSON integer is too large")
    number = int(value)
    if number.bit_length() > 256:
        raise ValueError("planning JSON integer is too large")
    return number


def _bounded(value, max_depth):
    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > 100000 or depth > max_depth:
            raise ValueError("planning JSON exceeds the item or depth limit")
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
        elif isinstance(item, int) and not isinstance(item, bool) and item.bit_length() > 256:
            raise ValueError("planning JSON integer is too large")


def load_json(path, *, max_bytes=1048576, max_depth=32):
    """Read one stable regular UTF-8 file, without following its final symlink."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 16 * 1024 * 1024:
        raise ValueError("JSON read limit must be between 1 byte and 16 MiB")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 1 <= max_depth <= 64:
        raise ValueError("JSON depth limit must be between 1 and 64")
    from .source_budget import source_limit, source_read
    max_bytes = source_limit(max_bytes)
    path = os.path.expanduser(os.fspath(path))
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("planning input must be a regular file")
        if before.st_size > max_bytes:
            raise ValueError(f"planning JSON exceeds the {max_bytes}-byte read limit")
        source_read(before.st_size)
        data = bytearray()
        while len(data) <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if signature(before) != signature(after) or signature(after) != signature(named) or len(data) != before.st_size:
            raise ValueError("planning JSON changed during inspection")
        if len(data) > max_bytes:
            raise ValueError("planning JSON exceeds the read limit")
        try:
            value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_int=_integer, parse_float=_float, parse_constant=_constant)
        except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid planning JSON: {str(exc)[:200]}") from exc
        if not isinstance(value, (dict, list)):
            raise ValueError("planning JSON must contain an object or array")
        _bounded(value, max_depth)
        return value
    finally:
        os.close(fd)
