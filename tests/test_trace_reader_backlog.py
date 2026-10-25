"""Bounded replay defers work without changing retained recording evidence."""

from __future__ import annotations

import hashlib

import pytest

from raven.tracing.capture import Recorder
from raven.tracing.protocol import TraceReader
from raven.tracing.workspace_index import TraceIndex


BACKLOG = "Reader budget reached or evidence missing; more committed records remain"
UNKNOWN_PREFIX = "Return without a recorded entry; stack prefix is unknown"
PAIR_COUNT = 9382
EVENT_COUNT = PAIR_COUNT * 2 + 3


@pytest.fixture(scope="module")
def closed_recording(tmp_path_factory):
    """Use the reported source size, with known complete frame boundaries."""
    root = tmp_path_factory.mktemp("closed-trace-backlog")
    with Recorder(
        root,
        job_id="3510",
        attempt="controlled-attempt",
        cluster="controlled",
        journal_format="blocks",
        flush_events=256,
        background_flush=False,
        initial_mono_ns=100,
    ) as recorder:
        for position in range(PAIR_COUNT):
            start = 100 + position * 10
            assert recorder.emit(
                "call",
                {"function": "work", "frame": "work", "file": "app.py", "line": 1},
                thread="worker",
                mono_ns=start,
            )
            assert recorder.emit(
                "return", {"frame": "work"}, thread="worker", mono_ns=start + 5
            )
        assert recorder.emit("note", {"name": "finished"})
    journal = recorder.directory / "events.jsonl"
    return recorder.path, journal, hashlib.sha256(journal.read_bytes()).hexdigest()


def test_closed_18767_event_reader_backlog_clears_after_bounded_replay(
    closed_recording,
):
    path, journal, original_digest = closed_recording
    reader = TraceReader(path)
    result = reader.refresh(max_events=73, max_seconds=0.001)
    assert not result["caught_up"]
    assert BACKLOG in result["warnings"]
    assert UNKNOWN_PREFIX not in result["warnings"]
    previous = result["read_seq"]
    assert 0 < previous <= 73
    for _ in range(EVENT_COUNT):
        result = reader.refresh(max_events=73, max_seconds=0.001)
        assert 0 <= result["read_seq"] - previous <= 73
        previous = result["read_seq"]
        if result["caught_up"]:
            break
        assert BACKLOG in result["warnings"]
    assert result["caught_up"]
    assert result["read_seq"] == result["committed_seq"] == EVENT_COUNT
    assert result["status"] == "COMPLETE"
    assert result["complete"]
    assert not result["warnings"]
    assert reader.refresh()["warnings"] == []
    assert hashlib.sha256(journal.read_bytes()).hexdigest() == original_digest


def test_workspace_clears_ingest_backlog_but_keeps_query_bound_explicit(
    tmp_path, closed_recording
):
    path, journal, original_digest = closed_recording
    with TraceIndex(tmp_path / "index.sqlite") as index:
        result = index.refresh([path], max_events=73, max_seconds=0.01)
        assert not result["caught_up"]
        assert BACKLOG in result["warnings"]
        for _ in range(EVENT_COUNT):
            result = index.refresh([path], max_events=173, max_seconds=0.01)
            if result["caught_up"]:
                break
        assert result["caught_up"]
        assert result["sources"][0]["read_seq"] == EVENT_COUNT
        assert result["sources"][0]["status"] == "COMPLETE"
        assert not result["warnings"]
        fit = index.query(limit=20)
        assert fit["caught_up"] and fit["truncated"]
        assert len(fit["intervals"]) == 20
        assert BACKLOG not in fit["warnings"]
        zoomed = index.query(start_ns=100, end_ns=106, limit=20)
        assert not zoomed["truncated"]
        assert len(zoomed["intervals"]) == 1
        assert zoomed["intervals"][0]["duration_ns"] == 5
        functions = index.functions()["functions"]
        assert functions[0]["count"] == PAIR_COUNT
        assert functions[0]["inclusive_ns"] == PAIR_COUNT * 5
    assert hashlib.sha256(journal.read_bytes()).hexdigest() == original_digest


def test_catching_up_does_not_erase_a_real_unknown_stack_prefix(tmp_path):
    with Recorder(
        tmp_path, background_flush=False, journal_format="blocks"
    ) as recorder:
        # A recording attached after this invocation began has no observed entry.
        assert recorder.emit("return", {"frame": "already-running"}, thread="worker")
        assert recorder.emit(
            "call", {"frame": "work", "function": "work"}, thread="worker"
        )
        assert recorder.emit("return", {"frame": "work"}, thread="worker")
    reader = TraceReader(recorder.path)
    result = reader.refresh(max_events=2)
    assert UNKNOWN_PREFIX in result["warnings"]
    assert BACKLOG in result["warnings"]
    result = reader.refresh()
    assert result["caught_up"]
    assert BACKLOG not in result["warnings"]
    assert UNKNOWN_PREFIX in result["warnings"]
    assert result["read_seq"] == 5
