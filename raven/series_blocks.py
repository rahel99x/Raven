"""Opt-in, lossless committed blocks for attempt-scoped native recordings.

All frame integers are little-endian. Payload values use finite JSON's exact
integer / binary64 round-trip representation, including signed zero; timestamps
retain their original epoch-second numeric representation. JSON object samples
preserve key order and omitted versus null fields.
Compression is zlib, never executable deserialization. Readers return a verified
prefix and explicit coverage on truncation/corruption; they never skip damage.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import struct
import zlib

MAGIC = b"RAVNBLK1"
COMMIT = b"COMMIT1!"
HEADER = struct.Struct("<8sIII32s32s")
FOOTER = struct.Struct("<8sQ32s")
MAX_RAW = 4 << 20
MAX_PACKED = 4 << 20
MAX_ROWS = 1024
MAX_KEYS = 256
MAX_FILE = 64 << 20
MAX_BLOCKS = 65536


def _identity(path):
    # Renaming a block file into another attempt cannot relabel its evidence.
    name = Path(path).name
    if name.endswith(".rvb"):
        name = name[:-4]
    elif name.endswith(".jsonl"):
        name = name[:-6]
    return hashlib.sha256(name.encode()).digest()


def _finite(value):
    raise ValueError("Non-finite block value: " + value)


def _float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Compact numeric exponent exceeds finite binary64")
    return value


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("Duplicate block field")
        out[key] = value
    return out


def _shape(raw):
    # Bound container depth and scalar slots before json materializes objects.
    quoted = escaped = False
    depth = slots = 0
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1; slots += 1
            if depth > 24:
                raise ValueError("Compact JSON nesting budget exceeded")
        elif byte in (93, 125):
            depth -= 1
        elif byte in (44, 58):
            slots += 1
        if slots > 100000:
            raise ValueError("Compact JSON structure budget exceeded")


def _json(raw):
    _shape(raw)
    return json.loads(raw, parse_constant=_finite, parse_float=_float, object_pairs_hook=_pairs)


def encode(samples, identity):
    """One bounded frame. JSON arrays preserve row order and missing-key fidelity."""
    if not samples or len(samples) > MAX_ROWS or len(identity) != 32:
        raise ValueError("Invalid compact block row or identity bound")
    for sample in samples:
        if (not isinstance(sample, dict) or len(sample) > MAX_KEYS
                or not all(isinstance(key, str) for key in sample)):
            raise ValueError("Compact samples require bounded string-keyed objects")
    raw = json.dumps(samples, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    if len(raw) > MAX_RAW:
        raise ValueError("Compact block exceeds its decoded byte budget")
    _shape(raw)  # Writer and reader admit the same bounded representation.
    payload = zlib.compress(raw, 6)
    if len(payload) > MAX_PACKED:
        raise ValueError("Compact block exceeds its encoded byte budget")
    header = HEADER.pack(MAGIC, len(raw), len(payload), len(samples), identity, hashlib.sha256(raw).digest())
    body = header + payload
    return body + FOOTER.pack(COMMIT, len(body) + FOOTER.size, hashlib.sha256(body).digest())


def decode(frame, identity):
    if len(frame) < HEADER.size + FOOTER.size:
        raise ValueError("Truncated compact block")
    magic, raw_length, size, count, scope, digest = HEADER.unpack_from(frame)
    if magic != MAGIC or not 0 < raw_length <= MAX_RAW or not 0 < size <= MAX_PACKED or not 0 < count <= MAX_ROWS:
        raise ValueError("Invalid compact block version or bounds")
    if scope != identity:
        raise ValueError("Compact block belongs to another attempt filename")
    if len(frame) != HEADER.size + size + FOOTER.size:
        raise ValueError("Truncated compact block")
    marker, length, committed = FOOTER.unpack_from(frame, len(frame) - FOOTER.size)
    body = frame[:-FOOTER.size]
    if marker != COMMIT or length != len(frame) or hashlib.sha256(body).digest() != committed:
        raise ValueError("Compact append commit checksum does not match")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(frame[HEADER.size:-FOOTER.size], MAX_RAW + 1)
    if len(raw) != raw_length or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("Compact decoded length or compression stream is invalid")
    if hashlib.sha256(raw).digest() != digest:
        raise ValueError("Compact data checksum does not match")
    value = _json(raw)
    if not isinstance(value, list) or len(value) != count:
        raise ValueError("Invalid compact row count")
    if any(not isinstance(row, dict) or len(row) > MAX_KEYS for row in value):
        raise ValueError("Invalid compact object or field bound")
    return value


def frames(samples, path):
    """Bound work and retain exact values; split oversized batches recursively."""
    identity = _identity(path)
    for offset in range(0, len(samples), MAX_ROWS):
        stack = [samples[offset:offset + MAX_ROWS]]
        while stack:
            batch = stack.pop()
            try:
                yield encode(batch, identity)
            except (ValueError, RecursionError) as exc:
                if len(batch) < 2:
                    raise ValueError("Sample cannot fit a compact block: " + str(exc)) from exc
                middle = len(batch) // 2
                stack.extend((batch[middle:], batch[:middle]))


def _open(path, flags, mode=0o600):
    fd = os.open(path, flags | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), mode)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("Compact recording must be a regular file")
    return fd


def verify_append(path):
    """Check the final committed block without re-reading all retained data."""
    try:
        fd = _open(path, os.O_RDONLY)
    except FileNotFoundError:
        return
    with os.fdopen(fd, "rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        if not size:
            return
        if size < FOOTER.size:
            raise ValueError("Incomplete compact append; use copy/verify recovery before writing")
        stream.seek(-FOOTER.size, os.SEEK_END)
        marker, length, _ = FOOTER.unpack(stream.read(FOOTER.size))
        if marker != COMMIT or not HEADER.size + FOOTER.size <= length <= MAX_PACKED + HEADER.size + FOOTER.size or length > size:
            raise ValueError("Incomplete compact append; use copy/verify recovery before writing")
        stream.seek(-length, os.SEEK_END)
        decode(stream.read(length), _identity(path))


def append(path, samples, *, writer=None):
    """The caller owns the state writer lock and serializes this append."""
    verify_append(path)
    if writer is not None:
        for payload in frames(samples, path):
            writer(path, payload)
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = _open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        for payload in frames(samples, path):
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("Short compact append")
                view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def read(path, keep=64000, *, max_bytes=MAX_FILE, identity=None, max_decoded_bytes=MAX_FILE, cancel=None):
    from collections import deque
    rows = deque(maxlen=max(0, keep))
    consumed = decoded = blocks = total = 0
    status = {"status": "ready", "format": "blocks-v1", "message": "Verified compact recordings restored.", "ignored": 0}
    try:
        if cancel is not None and cancel.is_set():
            status.update(status="cancelled", message="Compact read cancelled; no further records were acquired.")
            return [], dict(status, bytes=0, decoded_bytes=0, blocks=0, verified_samples=0, retained_samples=0, fidelity="raw")
        fd = _open(path, os.O_RDONLY)
        with os.fdopen(fd, "rb") as stream:
            while True:
                if cancel is not None and cancel.is_set():
                    status.update(status="cancelled", message="Compact read cancelled; verified prefix retained.")
                    break
                header = stream.read(HEADER.size)
                if not header:
                    break
                if len(header) != HEADER.size:
                    raise ValueError("Truncated compact header")
                magic, raw, size, count, scope, digest = HEADER.unpack(header)
                if magic != MAGIC or not 0 < size <= MAX_PACKED or not 0 < raw <= MAX_RAW or not 0 < count <= MAX_ROWS:
                    raise ValueError("Invalid compact version or block bounds")
                length = HEADER.size + size + FOOTER.size
                if consumed + length > max_bytes or decoded + raw > min(MAX_FILE, max_decoded_bytes) or blocks >= MAX_BLOCKS:
                    status.update(status="limited", message="Compact read budget reached; verified prefix retained.")
                    break
                frame = header + stream.read(size + FOOTER.size)
                samples = decode(frame, _identity(path) if identity is None else identity)
                rows.extend(samples); total += len(samples); blocks += 1; consumed += length; decoded += raw
    except FileNotFoundError:
        status.update(status="missing", message="No compact recording exists.")
    except (OSError, ValueError, RecursionError, zlib.error, struct.error) as exc:
        status.update(status="error", message="Compact coverage stopped at verified prefix: " + str(exc), ignored=1)
    status.update(bytes=consumed, decoded_bytes=decoded, blocks=blocks, verified_samples=total, retained_samples=len(rows), fidelity="raw")
    return list(rows), status
