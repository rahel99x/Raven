"""Pure bounded retained-line search indexes for immutable log snapshots."""
from __future__ import annotations

from dataclasses import dataclass

from .log_scan import REGEX_LINE_BYTES

MAX_INCREMENT_ROWS = 2048
MAX_INCREMENT_BYTES = 1 << 20


@dataclass(frozen=True)
class MatchIndex:
    context: tuple
    buffer: object
    stamp: tuple
    flags: bytes
    count: int
    omitted: int
    partial_flag: int
    partial_span: object

    @property
    def total_count(self):
        return self.count + (self.partial_flag == 1)

    @property
    def total_omitted(self):
        return self.omitted + (self.partial_flag == 2)

    @property
    def total(self):
        return len(self.flags) + bool(self.buffer._partial_raw)


def buffer_stamp(buffer):
    """Freeze mutable-buffer revision values instead of consulting an old reference."""
    return (buffer.size, buffer.last_refresh, buffer.reloads, buffer.skipped_bytes,
            len(buffer.lines), len(buffer._partial_raw))


def _flag(text, source_bytes, matcher, regex):
    if regex and source_bytes > REGEX_LINE_BYTES:
        return 2, None
    found = matcher.search(text)
    return (1, found.span()) if found else (0, None)


def build(buffer, context, matcher, regex):
    """Worker-safe: scan published strings only, without filesystem access."""
    flags = bytes(_flag(text, buffer._line_bytes[index], matcher, regex)[0]
                  for index, text in enumerate(buffer.lines))
    partial, span = _flag(buffer.partial, len(buffer._partial_raw), matcher, regex) if buffer._partial_raw else (0, None)
    return MatchIndex(context, buffer, buffer_stamp(buffer), flags, flags.count(1), flags.count(2), partial, span)


def extend(index, buffer, context, matcher, *, regex, query_length, word=False):
    """Reuse a validated retained prefix. None requests a fresh worker index."""
    old = index.buffer
    if old is buffer and index.stamp != buffer_stamp(buffer):
        return None
    if index.context != context or buffer.size < old.size or buffer.skipped_bytes < old.skipped_bytes:
        return None
    dropped_bytes = buffer.skipped_bytes - old.skipped_bytes
    consumed, drop = 0, 0
    while consumed < dropped_bytes and drop < len(index.flags) and drop < MAX_INCREMENT_ROWS:
        consumed += old._line_bytes[drop]
        drop += 1
    if consumed != dropped_bytes:
        return None
    retained = len(index.flags) - drop
    if len(buffer.lines) < retained:
        return None
    if retained and (old.lines[drop] is not buffer.lines[0] or old.lines[-1] is not buffer.lines[retained - 1]):
        return None
    # An equal-size replacement snapshot must not borrow match flags merely
    # because its first and last lines happen to be unchanged.
    if buffer is not old and buffer.size == old.size and not drop and old.lines != buffer.lines:
        return None
    added = len(buffer.lines) - retained
    if added > MAX_INCREMENT_ROWS or sum(buffer._line_bytes[retained:]) > MAX_INCREMENT_BYTES:
        return None
    extra = bytes(_flag(text, buffer._line_bytes[i], matcher, regex)[0]
                  for i, text in enumerate(buffer.lines[retained:], retained))
    prefix = index.flags[drop:]
    removed = index.flags[:drop]
    count = index.count - removed.count(1) + extra.count(1)
    omitted = index.omitted - removed.count(2) + extra.count(2)
    partial, span = 0, None
    if buffer._partial_raw:
        if regex and len(buffer._partial_raw) > REGEX_LINE_BYTES:
            partial = 2
        elif not regex and old._partial_raw and buffer.partial.startswith(old.partial):
            # A complete literal occurrence remains valid under append. A word
            # ending at the former EOF must be checked against the new suffix.
            if index.partial_flag == 1 and (not word or index.partial_span[1] < len(old.partial)):
                partial, span = 1, index.partial_span
            else:
                position = max(0, len(old.partial) - query_length - 1)
                found = matcher.search(buffer.partial, position)
                partial, span = (1, found.span()) if found else (0, None)
        elif len(buffer._partial_raw) > MAX_INCREMENT_BYTES and not regex:
            return None
        else:
            partial, span = _flag(buffer.partial, len(buffer._partial_raw), matcher, regex)
    return MatchIndex(context, buffer, buffer_stamp(buffer), prefix + extra, count, omitted, partial, span)


def find(index, start, backwards=False):
    """Use compact byte flags; missing matches never rescan source text."""
    count = index.total
    if not count or index.total_count == 0:
        return None
    flags = index.flags + (bytes((index.partial_flag,)) if index.buffer._partial_raw else b"")
    if backwards:
        stop = count if start is None else max(0, min(count, start))
        result = flags.rfind(b"\x01", 0, stop)
        return result if result >= 0 else flags.rfind(b"\x01", stop)
    stop = 0 if start is None else min(count, start + 1)
    result = flags.find(b"\x01", stop)
    if result < 0:
        result = flags.find(b"\x01", 0, stop)
    return result if result >= 0 else None
