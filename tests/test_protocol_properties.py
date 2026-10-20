"""Generated wire partitions supplement the existing every-boundary fixtures."""
import hashlib
import json
import struct
from collections import deque

from hypothesis import given, settings, strategies as st
import pytest

from raven import raven_protocol, raven_allocation, terminal_service
from raven.framing import write_pending


CHANNELS = (raven_protocol, raven_allocation, terminal_service)
text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=32)
scalar = st.none() | st.booleans() | st.integers(-(2**63), 2**63 - 1) | st.floats(allow_nan=False, allow_infinity=False) | text
value = st.recursive(scalar, lambda child: st.lists(child, max_size=5) | st.dictionaries(text, child, max_size=5), max_leaves=20)
documents = st.lists(st.dictionaries(text, value, max_size=5), min_size=1, max_size=8)


@pytest.mark.parametrize("channel", CHANNELS)
@settings(max_examples=80, deadline=None)
@given(documents, st.lists(st.integers(1, 200), min_size=1, max_size=30), st.integers(1, 8))
def test_arbitrary_fragmentation_coalescing_preserves_order(channel, documents, chunks, drain):
    encode = getattr(channel, "encode", None) or channel.frame
    wire = b"".join(encode(document) for document in documents)
    decoder, result, offset, index = channel.Decoder(), [], 0, 0
    while offset < len(wire):
        size = chunks[index % len(chunks)]
        decoder.feed(wire[offset:offset + size])
        result.extend(decoder.take(drain))
        offset += size
        index += 1
    while True:
        batch = decoder.take(drain)
        if not batch:
            break
        result.extend(batch)
    assert result == documents
    assert not decoder.buffer


@pytest.mark.parametrize("channel", CHANNELS)
@pytest.mark.parametrize("raw", [b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'{"op":"safe","op":"unsafe"}',
                                  b'{"x":"\\ud800"}', b'{"\\ud800":1}', b'\xff', b'{', b''])
def test_malformed_frames_never_publish(channel, raw):
    decoder = channel.Decoder()
    with pytest.raises(ValueError):
        decoder.feed(struct.pack("!I", len(raw)) + raw)
        decoder.take()


@pytest.mark.parametrize("channel", CHANNELS)
def test_queue_policy_is_not_expanded_to_largest_channel(channel):
    decoder = channel.Decoder()
    with pytest.raises(ValueError):
        decoder.feed(b"x" * (channel.POLICY.max_queue + 1))
    assert not decoder.buffer


@given(st.dictionaries(text, value, max_size=8))
@settings(max_examples=80, deadline=None)
def test_canonical_digest_roundtrip_is_stable(document):
    canonical = raven_protocol.canonical(document)
    assert raven_protocol.digest(json.loads(canonical)) == hashlib.sha256(canonical).hexdigest()
    assert raven_protocol.canonical(dict(reversed(list(document.items())))) == canonical


@pytest.mark.parametrize("channel,depth", [(raven_protocol, 40), (raven_allocation, 40), (terminal_service, 32)])
def test_channel_nesting_bound_is_explicit(channel, depth):
    # Worker payloads admit depth32; their worker/allocation envelopes retain
    # eight levels of bounded headroom. Terminal metadata retains its own cap.
    document = 0
    for _ in range(depth):
        document = {"child": document}
    encode = getattr(channel, "encode", None) or channel.frame
    decoder = channel.Decoder()
    decoder.feed(encode(document))
    assert decoder.take() == [document]
    document = {"child": document}
    # Terminal framing serializes trusted engine snapshots without revisiting
    # every scalar; its receiver and action-specific validation enforce bounds.
    if channel is not terminal_service:
        with pytest.raises(ValueError):
            encode(document)
    wire = json.dumps(document).encode()
    decoder = channel.Decoder()
    decoder.feed(struct.pack("!I", len(wire)) + wire)
    with pytest.raises(ValueError):
        decoder.take()


@pytest.mark.parametrize("channel", CHANNELS)
def test_partial_payload_retained_without_publication(channel):
    encode = getattr(channel, "encode", None) or channel.frame
    raw = encode({"identity": "attempt-1"})
    decoder = channel.Decoder()
    decoder.feed(raw[:-1])
    assert decoder.take() == []
    decoder.feed(raw[-1:])
    assert decoder.take() == [{"identity": "attempt-1"}]


@given(st.lists(st.binary(min_size=1, max_size=100), min_size=1, max_size=12), st.integers(1, 50))
def test_partial_write_budget_preserves_bytes_and_accounting(messages, chunk):
    queue = deque(memoryview(value) for value in messages)
    written = bytearray()
    accounted = 0
    def send(value):
        data = value[:chunk]
        written.extend(data)
        return len(data)
    while queue:
        accounted += sum(write_pending(queue, send, budget=31))
    assert bytes(written) == b"".join(messages)
    assert accounted == len(written)


def test_backpressure_after_partial_write_preserves_unsent_tail():
    queue = deque([memoryview(b"abcdef")])
    calls = 0
    def send(value):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise BlockingIOError
        return 2
    assert list(write_pending(queue, send)) == [2]
    assert bytes(queue[0]) == b"cdef"


def test_terminal_metadata_depth_is_checked_in_envelope_before_process_start(monkeypatch):
    service = terminal_service.TerminalService()
    monkeypatch.setattr(service, "_start", lambda: pytest.fail("invalid metadata must not spawn a process"))
    metadata = 0
    for _ in range(32):
        metadata = {"child": metadata}
    with pytest.raises(ValueError, match="structure limits"):
        service.open(["/bin/sh"], metadata=metadata)
    assert not service.sessions


def test_terminal_deep_input_is_rejected_before_disrupting_other_sessions(monkeypatch):
    service = terminal_service.TerminalService()
    service.sessions["known"] = {"status": "running"}
    monkeypatch.setattr(service, "_queue", lambda value: pytest.fail("invalid input must not reach broker"))
    event = 0
    for _ in range(32):
        event = {"child": event}
    assert not service.send("known", event)
    assert "invalid" in service.error


def test_multibyte_memoryview_cannot_bypass_receive_budget():
    from array import array
    from raven.framing import Decoder, FramePolicy
    decoder = Decoder(FramePolicy("test", 20, 20), json.loads)
    raw = memoryview(array("Q", [1, 2, 3]))
    assert len(raw) == 3 and raw.nbytes == 24
    with pytest.raises(ValueError):
        decoder.feed(raw)
    assert not decoder.buffer


@pytest.mark.parametrize("channel", CHANNELS)
def test_baseline_wire_fixture_remains_compatible(channel):
    # Captured from 33316a0 using each old encoder. No new schema/digest is
    # substituted into the fixture; unlike helper builds, wire framing is v1.
    value = {"op": "snapshot", "session_id": "é", "generation": 2, "rows": [[1, "sample", 0.1]]}
    if channel is raven_protocol:
        payload = b'{"generation":2,"op":"snapshot","rows":[[1,"sample",0.1]],"session_id":"\xc3\xa9"}'
    else:
        payload = b'{"op":"snapshot","session_id":"\xc3\xa9","generation":2,"rows":[[1,"sample",0.1]]}'
    wire = struct.pack("!I", len(payload)) + payload
    encode = getattr(channel, "encode", None) or channel.frame
    assert encode(value) == wire
    decoder = channel.Decoder()
    decoder.feed(wire)
    assert decoder.take() == [value]
