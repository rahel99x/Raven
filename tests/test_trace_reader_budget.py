"""Aggregate reader admission, release and verified-prefix recovery."""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest

from raven.tracing.capture import Recorder
from raven.tracing.protocol import TraceReader
from raven.tracing.reader_budget import (
    ReaderMemoryBudget,
    ReaderMemoryLimit,
    retained_size,
)
from raven.tracing.workspace_index import TraceIndex


def recorder(root, *, dictionary=False, blocks=False):
    root.mkdir(mode=0o700, parents=True)
    rec = Recorder(
        root,
        job_id="42",
        attempt="a",
        cluster="local",
        background_flush=False,
        journal_format="blocks" if blocks else "events",
        max_bytes=16 << 20,
        initial_mono_ns=0,
    )
    if dictionary:
        # This is the actual format transition made by PythonCapture.start;
        # deterministic metadata avoids relying on application Python version.
        rec._async_frames = True
    return rec


def calls(rec, *, stamp=100):
    rec.emit("call", {"frame": "f", "function": "work"}, thread="one", mono_ns=stamp)
    rec.emit("return", {"frame": "f"}, thread="one", mono_ns=stamp + 5)


def codes(rec, count=100, *, stamp=200):
    for position in range(count):
        rec.emit(
            "code",
            {
                "id": position + 1,
                "function": "f" * 4096,
                "file": "app.py",
                "frame": str(position),
                "line": position,
            },
            mono_ns=stamp + position,
        )


def finish(index, paths):
    for _ in range(200):
        result = index.refresh(paths, max_events=512, max_seconds=0.1)
        if result["caught_up"]:
            return result
    pytest.fail("Reader-budget fixture did not catch up")


def test_shared_budget_refuses_before_growth_and_counts_nested_python_objects():
    budget = ReaderMemoryBudget(1 << 20)
    budget.reserve((1 << 20) - 10, "fixture")
    with pytest.raises(ReaderMemoryLimit, match="raw recordings are unchanged"):
        budget.reserve(11, "second reader")
    assert budget.used == (1 << 20) - 10
    budget.release(budget.used)
    assert budget.used == 0
    nested = [{} for _ in range(4096)]
    assert retained_size(nested) > len(str(nested)) * 4
    with pytest.raises(ValueError):
        budget.release(1)


def test_public_window_release_preserves_decoding_hash_and_stack_until_close(tmp_path):
    budget = ReaderMemoryBudget(1 << 20)
    with recorder(tmp_path / "p", dictionary=True) as rec:
        rec.emit(
            "code",
            {"id": 1, "function": "work", "file": "app.py", "frame": "f", "line": 1},
        )
        rec.emit("call", {"code": 1}, thread="one")
        rec.flush()
        reader = TraceReader(rec.path, memory_budget=budget)
        first = reader.refresh()
        sequence, checksum, retained = reader.sequence, reader.previous, budget.used
        assert first["stacks"][0]["frames"][0]["function"] == "work"
        reader.release_consumed_window()
        assert not reader.events and reader.retained_bytes == 0
        assert reader.sequence == sequence and reader.previous == checksum
        rec.emit("return", {"code": 1}, thread="one")
        rec.flush()
        second = reader.refresh()
        assert second["events"][0]["data"]["function"] == "work"
        assert not second["stacks"][0]["frames"]
        assert budget.used < retained
        reader.close()
        reader.close()
        assert budget.used == 0
        with pytest.raises(ValueError, match="closed"):
            reader.refresh()


def test_resource_limited_source_keeps_prefix_while_other_streams_progress_and_reopen(
    tmp_path,
):
    index_path = tmp_path / "index.sqlite"
    with (
        recorder(tmp_path / "large", dictionary=True) as large,
        recorder(tmp_path / "small") as small,
    ):
        calls(large)
        large.flush()
        with TraceIndex(index_path, reader_memory_bytes=1 << 20) as index:
            finish(index, [large.path])
            original = index.query()["intervals"][0]
            pressure = index.reader_memory.limit - index.reader_memory.used - 40_000
            index.reader_memory.reserve(pressure, "another admitted reader fixture")
            codes(large)
            large.flush()
            calls(small)
            small.flush()
            try:
                for _ in range(10):
                    result = index.refresh(
                        [large.path, small.path], max_events=512, max_seconds=0.1
                    )
                    if len(index.query()["intervals"]) == 2:
                        break
                limited = next(
                    row for row in result["sources"] if row["path"] == str(large.path)
                )
                assert (
                    limited["status"] == "RESOURCE_LIMIT"
                    and limited["read_status"] == "resource-limit"
                )
                assert "working-state budget exhausted" in limited["error"]
                assert limited["read_seq"] == 3 and not result["caught_up"]
                assert index.detail(original["id"]) == original
                assert limited["id"] not in index.readers
                assert index.reader_memory.used <= index.reader_memory.limit
                calls(small, stamp=500)
                small.flush()
                index.refresh([large.path, small.path], max_events=512, max_seconds=0.1)
                assert len(index.query()["intervals"]) == 3
                assert index.catalog()["index"]["resource_limited_sources"] == 1
            finally:
                index.reader_memory.release(pressure)
        assert index.reader_memory.used == 0
        # Reopening after the pressure is removed revalidates raw evidence,
        # restores the same interval IDs and admits the complete dictionary.
        with TraceIndex(index_path, reader_memory_bytes=2 << 20) as restored:
            assert finish(restored, [large.path, small.path])["caught_up"]
            assert restored.detail(original["id"]) == original
            assert len(restored.query()["intervals"]) == 3


def test_aggregate_partial_block_buffers_are_bounded_and_raw_files_untouched(tmp_path):
    paths, hashes = [], {}
    for position in range(8):
        with recorder(tmp_path / str(position), dictionary=True, blocks=True) as rec:
            codes(rec, count=260)
        paths.append(rec.path)
        hashes[rec.path] = hashlib.sha256(
            (rec.path.parent / "events.jsonl").read_bytes()
        ).hexdigest()
    with TraceIndex(tmp_path / "index.sqlite", reader_memory_bytes=2 << 20) as index:
        for path in paths:
            index.refresh([path], max_events=1, max_seconds=0.1)
        for _ in range(16):
            result = index.refresh(paths, max_events=1, max_seconds=0.1)
            if any(row["status"] == "RESOURCE_LIMIT" for row in result["sources"]):
                break
        assert any(row["status"] == "RESOURCE_LIMIT" for row in result["sources"])
        assert result["index"]["reader_memory"]["peak_charged_bytes"] <= 2 << 20
        assert (
            sum(
                len(reader._block[1])
                for reader in index.readers.values()
                if reader._block is not None
            )
            <= 2 << 20
        )
        assert all(not reader.events for reader in index.readers.values())
        for path in paths:
            assert (
                hashlib.sha256((path.parent / "events.jsonl").read_bytes()).hexdigest()
                == hashes[path]
            )
    assert index.reader_memory.used == 0


def test_checkpoint_admission_bounds_python_container_growth_without_corruption(
    tmp_path,
):
    with recorder(tmp_path / "p") as rec:
        rec.emit(
            "stack_checkpoint", {"frames": [{} for _ in range(2000)]}, thread="one"
        )
    budget = ReaderMemoryBudget(1 << 20)
    reader = TraceReader(rec.path, memory_budget=budget)
    with pytest.raises(ReaderMemoryLimit, match="stack frames"):
        reader.refresh()
    assert not reader.invalid and not reader.stack_map["one"]
    assert budget.used <= budget.limit
    reader.close()
    assert budget.used == 0
    restored_budget = ReaderMemoryBudget(4 << 20)
    restored = TraceReader(rec.path, memory_budget=restored_budget)
    assert restored.refresh()["status"] == "COMPLETE"
    assert len(restored.stack_map["one"]) == 2000
    restored.close()
    assert restored_budget.used == 0


def test_cancellation_and_quarantine_release_owned_state_without_reclassifying_limits(
    tmp_path,
):
    with recorder(tmp_path / "p") as rec:
        calls(rec)
    with TraceIndex(tmp_path / "index.sqlite", reader_memory_bytes=1 << 20) as index:
        stop = threading.Event()
        stop.set()
        index.refresh([rec.path], cancel=stop)
        assert index.reader_memory.used == 0
        finish(index, [rec.path])
        assert index.reader_memory.used > 0
        journal = rec.path.parent / "events.jsonl"
        replacement = journal.with_name("replacement")
        replacement.write_bytes(journal.read_bytes())
        replacement.replace(journal)
        result = index.refresh([rec.path], max_seconds=0.1)
        assert result["sources"][0]["status"] == "INVALID"
        assert result["sources"][0]["read_status"] == "invalid"
        assert index.reader_memory.used == 0


def test_mid_page_io_error_publishes_each_verified_event_once_before_retry(
    tmp_path, monkeypatch
):
    from raven.tracing import protocol

    with recorder(tmp_path / "p") as rec:
        calls(rec)
        calls(rec, stamp=200)
    real_open, real_fdopen = protocol.regular_open, protocol.os.fdopen
    journal_fd, failed = set(), False

    def tracked_open(path):
        descriptor = real_open(path)
        if Path(path).name == "events.jsonl":
            journal_fd.add(descriptor)
        return descriptor

    class FaultyStream:
        def __init__(self, stream):
            self.stream, self.lines = stream, 0

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def readline(self, *args):
            nonlocal failed
            self.lines += 1
            if not failed and self.lines == 4:
                failed = True
                raise OSError("temporary shared-storage read failure")
            return self.stream.readline(*args)

    def fdopen(descriptor, *args, **kwargs):
        stream = real_fdopen(descriptor, *args, **kwargs)
        if descriptor in journal_fd:
            journal_fd.discard(descriptor)
            return FaultyStream(stream)
        return stream

    with TraceIndex(tmp_path / "index.sqlite") as index:
        with monkeypatch.context() as patch:
            patch.setattr(protocol, "regular_open", tracked_open)
            patch.setattr(protocol.os, "fdopen", fdopen)
            first = index.refresh([rec.path], max_events=512, max_seconds=0.1)
            assert first["sources"][0]["read_status"] == "retry-later"
            assert (
                first["sources"][0]["error"] == "temporary shared-storage read failure"
            )
            assert first["sources"][0]["read_seq"] == 3
            assert len(index.query()["intervals"]) == 1
            assert not first["caught_up"]
            restored = finish(index, [rec.path])
        assert restored["caught_up"] and "error" not in restored["sources"][0]
        assert len(index.query()["intervals"]) == 2
        assert index.functions()["functions"][0]["count"] == 2


def test_reader_limit_never_closes_a_descriptor_reused_after_its_context(
    tmp_path, monkeypatch
):
    from raven.tracing import protocol

    with recorder(tmp_path / "p", dictionary=True, blocks=True) as rec:
        codes(rec, 260)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("owned sentinel")
    real_open, real_fdopen = protocol.regular_open, protocol.os.fdopen
    journals, replacements = set(), []

    def tracked_open(path):
        descriptor = real_open(path)
        if Path(path).name == "events.jsonl":
            journals.add(descriptor)
        return descriptor

    class ReusedDescriptor:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            descriptor = self.stream.fileno()
            result = self.stream.__exit__(*args)
            replacement = os.open(sentinel, os.O_RDONLY)
            replacements.append(replacement)
            assert replacement == descriptor
            return result

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def fdopen(descriptor, *args, **kwargs):
        stream = real_fdopen(descriptor, *args, **kwargs)
        if descriptor in journals:
            journals.discard(descriptor)
            return ReusedDescriptor(stream)
        return stream

    budget = ReaderMemoryBudget(1 << 20)
    reader = TraceReader(rec.path, memory_budget=budget)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(protocol, "regular_open", tracked_open)
            patch.setattr(protocol.os, "fdopen", fdopen)
            with pytest.raises(ReaderMemoryLimit):
                reader.refresh()
        assert len(replacements) == 1
        assert os.read(replacements[0], 64) == b"owned sentinel"
    finally:
        reader.close()
        for descriptor in replacements:
            try:
                os.close(descriptor)
            except OSError:
                pass
    assert budget.used == 0
