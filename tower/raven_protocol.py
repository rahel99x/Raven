"""Versioned, bounded Raven worker messages. No executable payloads or pickle."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import struct
import sys

MAJOR = 1
MINOR = 0
MAX_FRAME = 1 << 20
MAX_QUEUE = 8 << 20
MAX_STRING = 256 << 10
MAX_DEPTH = 24
MAX_RECORDS = 50000


class ProtocolError(ValueError):
    pass


def worker_argv():
    """Launch this installed/source package independently of the caller cwd."""
    return [sys.executable, "-c",
            "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('tower.raven_worker',run_name='__main__')",
            str(Path(__file__).resolve().parent.parent)]


def validate(value):
    count = 0
    def visit(item, depth):
        nonlocal count
        count += 1
        if count > MAX_RECORDS or depth > MAX_DEPTH:
            raise ProtocolError("Worker document exceeds record or nesting budget")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, str):
            if len(item.encode("utf-8")) > MAX_STRING:
                raise ProtocolError("Worker string exceeds its byte budget")
        elif isinstance(item, int):
            if not -(1 << 63) <= item < (1 << 63):
                raise ProtocolError("Worker integer exceeds signed 64-bit range")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ProtocolError("Worker numbers must be finite")
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 128:
                    raise ProtocolError("Worker object keys must be short strings")
                key.encode("utf-8")  # Reject surrogate keys before wire encoding.
                visit(child, depth + 1)
        else:
            raise ProtocolError("Worker messages contain only JSON data")
    try:
        visit(value, 0)
    except (UnicodeError, RecursionError) as exc:
        raise ProtocolError("Invalid worker text or nesting") from exc
    return value


def canonical(value):
    validate(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def encode(value):
    if not isinstance(value, dict):
        raise ProtocolError("Worker frame must contain an object")
    data = canonical(value)
    if len(data) > MAX_FRAME:
        raise ProtocolError("Worker frame exceeds 1 MiB")
    return struct.pack("!I", len(data)) + data


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ProtocolError("Duplicate worker object key")
        result[key] = value
    return result


class Decoder:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        if not isinstance(data, bytes) or len(self.buffer) + len(data) > MAX_QUEUE:
            raise ProtocolError("Worker receive queue exceeds 8 MiB")
        self.buffer.extend(data)
        if len(self.buffer) >= 4:
            size = struct.unpack("!I", self.buffer[:4])[0]
            if not 1 <= size <= MAX_FRAME:
                raise ProtocolError("Invalid worker frame size")

    def take(self, count=32):
        messages = []
        consumed = 0
        for _ in range(max(0, min(128, int(count)))):
            if len(self.buffer) - consumed < 4:
                break
            size = struct.unpack("!I", self.buffer[consumed:consumed + 4])[0]
            if not 1 <= size <= MAX_FRAME:
                raise ProtocolError("Invalid worker frame size")
            if len(self.buffer) - consumed < size + 4:
                break
            raw = bytes(self.buffer[consumed + 4:consumed + size + 4])
            try:
                value = json.loads(raw, object_pairs_hook=_pairs,
                                   parse_constant=lambda value: (_ for _ in ()).throw(ProtocolError("Nonfinite worker number")))
                validate(value)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise ProtocolError("Invalid worker JSON frame") from exc
            if not isinstance(value, dict):
                raise ProtocolError("Worker frame must contain an object")
            messages.append(value)
            consumed += size + 4
        if consumed:
            del self.buffer[:consumed]
        return messages


def build_digest():
    """Digest only the fixed worker implementation, never user paths or secrets."""
    root = Path(__file__).resolve().parent
    state = hashlib.sha256()
    for name in ("__init__.py", "raven_protocol.py", "raven_worker.py", "raven_resources.py", "raven_execution.py", "raven_config.py"):
        path = root / name
        state.update(name.encode() + b"\0")
        if path.is_file():
            with path.open("rb") as source:
                data = source.read(2 << 20)
                if source.read(1):
                    raise ProtocolError("Worker module exceeds the build manifest budget")
            state.update(data)
        else:
            state.update(b"unavailable")
    return state.hexdigest()
