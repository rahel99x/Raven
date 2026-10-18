"""Shared length framing; each transport retains its own limits and JSON policy.

Transport bytes are not evidence canonicalization. Callers supply the serializer
and parser; this module never hashes, migrates, or interprets message actions.
"""

from __future__ import annotations

import struct
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any, NoReturn


@dataclass(frozen=True)
class FramePolicy:
    name: str
    max_frame: int
    max_queue: int
    error: type[ValueError] = ValueError
    eager_header: bool = False
    max_messages: int = 128
    bytes_only: bool = False

    def size(self, size: int) -> None:
        if not 1 <= size <= self.max_frame:
            raise self.error(f"{self.name} frame size is empty or exceeds its limit")


def frame(payload: bytes, policy: FramePolicy) -> bytes:
    """Frame already serialized immutable bytes, without serializing twice."""
    policy.size(len(payload))
    return struct.pack("!I", len(payload)) + payload


class Decoder:
    """Bounded incremental framing with one compaction per drain, not per frame."""

    def __init__(self, policy: FramePolicy, parse: Callable[[bytes], dict[str, Any]]) -> None:
        self.policy = policy
        self.parse = parse
        self.buffer = bytearray()

    def feed(self, data: bytes | bytearray | memoryview) -> None:
        policy = self.policy
        if (
            (policy.bytes_only and not isinstance(data, bytes))
            or not isinstance(data, (bytes, bytearray, memoryview))
            or len(self.buffer) + (data.nbytes if isinstance(data, memoryview) else len(data))
            > policy.max_queue
        ):
            raise policy.error(
                f"{policy.name} receive buffer exceeds its limit or has invalid bytes"
            )
        self.buffer.extend(data)
        if policy.eager_header and len(self.buffer) >= 4:
            policy.size(struct.unpack_from("!I", self.buffer)[0])

    def take(self, limit: int = 32) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        consumed = 0
        for _ in range(max(0, min(self.policy.max_messages, int(limit)))):
            if len(self.buffer) - consumed < 4:
                break
            size = struct.unpack_from("!I", self.buffer, consumed)[0]
            self.policy.size(size)
            if len(self.buffer) - consumed < size + 4:
                break
            raw = bytes(self.buffer[consumed + 4 : consumed + 4 + size])
            value = self.parse(raw)
            if not isinstance(value, dict):
                raise self.policy.error(f"{self.policy.name} message must be an object")
            out.append(value)
            consumed += size + 4
        if consumed:
            del self.buffer[:consumed]
        return out

    @property
    def has_complete_frame(self) -> bool:
        """Readiness hint only; invalid headers wake the owner to reject them."""
        if len(self.buffer) < 4:
            return False
        size = struct.unpack_from("!I", self.buffer)[0]
        return not 1 <= size <= self.policy.max_frame or len(self.buffer) >= size + 4


def unique_object(items: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """Duplicate fields cannot ambiguously select an action or ownership key."""
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate message object key")
        result[key] = value
    return result


def finite_constant(value: str) -> NoReturn:
    raise ValueError("Message numbers must be finite")


def write_pending(
    queue: deque[memoryview],
    write: Callable[[memoryview], int],
    budget: int = 65536,
    *,
    zero_is_error: bool = False,
) -> Iterator[int]:
    """Yield confirmed byte counts while preserving partial-write ordering.

    The caller owns failure recovery and byte accounting. Each yield follows
    queue advancement, so an exception on a later write cannot hide prior work.
    Backpressure and interruption return to the existing selector owner.
    """
    while queue and budget > 0:
        piece = queue[0][:budget]
        try:
            written = write(piece)
        except (BlockingIOError, InterruptedError):
            return
        if not written:
            if zero_is_error:
                raise BrokenPipeError("Message receiver stopped reading")
            return
        if not 0 < written <= len(piece):
            raise ValueError("Invalid partial-write byte count")
        queue[0] = queue[0][written:]
        if not queue[0]:
            queue.popleft()
        budget -= written
        yield written
