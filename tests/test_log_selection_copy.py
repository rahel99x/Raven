"""Pinned raw log selections export exactly, independently of source files."""
from __future__ import annotations

from pathlib import Path
import stat
import tracemalloc

import pytest

from raven import clipboard, log_copy
from raven.log_copy import copy_log_selection


def raw_export(chunks, state_dir, **kwargs):
    return copy_log_selection(chunks, state_dir, use_osc52=False, use_tools=False, **kwargs)


@pytest.mark.parametrize("chunks,expected,lines", [
    ([], b"", 0), ([b"one"], b"one", 1), ([b"one", b"\n"], b"one\n", 1),
    ([b"one", b"\n", b"two"], b"one\ntwo", 2),
    ([b"one", b"\n", b"two", b"\n"], b"one\ntwo\n", 2),
    ([b"one", b"\n", b""], b"one\n", 1),
    ([b"\tvalue\t\r", b"\n", b"end\r", b"\n"], b"\tvalue\t\r\nend\r\n", 2),
    (["é→\t最後".encode(), b"\n"], "é→\t最後\n".encode(), 1),
    ([b"\xff\x00raw\r", b"\n"], b"\xff\x00raw\r\n", 1),
])
def test_selection_preserves_exact_raw_bytes_and_newline_presence(tmp_path, chunks, expected, lines):
    result = raw_export(iter(chunks), tmp_path / "state", source_path="/remote/source-no-longer-exists.log")
    assert result["status"] == "ready", result["message"]
    target = Path(result["export_path"])
    assert target.read_bytes() == expected
    assert result["bytes"] == result["snapshot_bytes"] == len(expected)
    assert result["lines"] == lines
    assert result["source_path"] == "/remote/source-no-longer-exists.log"
    assert result["grew"] is False
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(target.parent.glob("*.tmp"))


def test_invalid_utf8_selection_keeps_full_raw_export_and_skips_text_transports(tmp_path, monkeypatch):
    monkeypatch.setattr(clipboard, "osc52", lambda *a: pytest.fail("invalid text must not be sent"))
    monkeypatch.setattr(clipboard, "_tool_from_file", lambda *a: pytest.fail("invalid text must not be sent"))
    result = copy_log_selection(iter([b"good\t", b"\n", b"bad\xff", b"\n"]), tmp_path / "state")
    assert result["status"] == "partial"
    assert Path(result["export_path"]).read_bytes() == b"good\t\nbad\xff\n"
    assert result["clipboard"]["methods"] == []
    assert result["clipboard"]["text"] is False
    assert "not valid UTF-8" in result["message"]


def test_large_selected_line_and_many_lines_never_join_a_second_whole_payload(tmp_path):
    line = b"x" * (8 * log_copy.CHUNK_BYTES + 31)
    expected_size = len(line) + 1 + 10000 * 2

    def chunks():
        yield line
        yield b"\n"
        for _ in range(10000):
            yield b"y"
            yield b"\n"

    tracemalloc.start()
    try:
        result = raw_export(chunks(), tmp_path / "state")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["status"] == "ready"
    assert result["bytes"] == expected_size
    assert result["lines"] == 10001
    assert peak < log_copy.CHUNK_BYTES
    with Path(result["export_path"]).open("rb") as exported:
        assert exported.read(len(line)) == line
        assert exported.read() == b"\n" + b"y\n" * 10000


def test_pinned_immutable_bytes_remain_after_original_buffer_replacement(tmp_path):
    original = [b"first\tline", b"second"]
    pinned = tuple(original)
    original.clear()
    original.extend([b"newer source data"])
    result = raw_export((piece for line in pinned for piece in (line, b"\n")), tmp_path / "state")
    assert Path(result["export_path"]).read_bytes() == b"first\tline\nsecond\n"


@pytest.mark.parametrize("bad_chunk", ["text", bytearray(b"mutable"), memoryview(bytearray(b"mutable")), None, 1])
def test_mutable_or_nonbyte_chunk_refused_atomically(tmp_path, monkeypatch, bad_chunk):
    monkeypatch.setattr(clipboard, "copy_file", lambda *a, **kw: pytest.fail("invalid selection must not reach clipboard"))
    result = copy_log_selection(iter([b"valid\n", bad_chunk]), tmp_path / "state")
    assert result["status"] == "error"
    assert "immutable bytes" in result["message"]
    assert result["export_path"] == ""
    assert not list((tmp_path / "state").rglob("*.*"))


def test_generator_failure_cleans_partial_export_and_never_copies_prefix(tmp_path, monkeypatch):
    def chunks():
        yield b"valid\n"
        raise OSError("selection producer failed\x1b")
    monkeypatch.setattr(clipboard, "copy_file", lambda *a, **kw: pytest.fail("partial selection must not reach clipboard"))
    result = copy_log_selection(chunks(), tmp_path / "state")
    assert result["status"] == "error"
    assert "producer failed" in result["message"]
    assert "\x1b" not in result["message"]
    assert result["export_path"] == ""
    assert not list((tmp_path / "state").rglob("*.*"))


def test_cancelled_large_chunk_stops_between_bounded_writes(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    def cancel():
        return any(path.stat().st_size > 0 for path in (state_dir / "exports").glob(".log-selection-*.tmp"))
    monkeypatch.setattr(clipboard, "copy_file", lambda *a, **kw: pytest.fail("cancelled selection must not reach clipboard"))
    result = copy_log_selection(iter([b"x" * (3 * log_copy.CHUNK_BYTES)]), state_dir, cancel=cancel)
    assert result["status"] == "error"
    assert "cancelled" in result["message"]
    assert result["bytes"] == log_copy.CHUNK_BYTES
    assert result["export_path"] == ""
    assert not list(state_dir.rglob("*.*"))


def test_cancelled_before_start_never_consumes_pinned_generator(tmp_path):
    def chunks():
        pytest.fail("cancelled operation must not consume selection")
        yield b"never"
    result = raw_export(chunks(), tmp_path / "state", cancel=lambda: True)
    assert result["status"] == "error"
    assert not (tmp_path / "state").exists()


def test_clipboard_exception_preserves_full_selected_export(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("display unavailable")
    monkeypatch.setattr(clipboard, "copy_file", fail)
    result = copy_log_selection(iter([b"first\t", b"\n", b"last"]), tmp_path / "state")
    assert result["status"] == "partial"
    assert Path(result["export_path"]).read_bytes() == b"first\t\nlast"
    assert "Clipboard delivery failed" in result["message"]


def test_large_selection_osc52_limit_never_sends_partial_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(clipboard, "osc52", lambda *a: pytest.fail("oversize selection must not emit OSC 52"))
    data = b"x" * 80000
    result = copy_log_selection(iter([data, b"\n"]), tmp_path / "state", use_tools=False)
    assert result["status"] == "partial"
    assert Path(result["export_path"]).read_bytes() == data + b"\n"
    assert "OSC 52 skipped" in result["message"]


def test_selection_export_directory_symlink_refused(tmp_path):
    state_dir, outside = tmp_path / "state", tmp_path / "outside"
    state_dir.mkdir()
    outside.mkdir()
    (state_dir / "exports").symlink_to(outside)
    result = raw_export(iter([b"private\n"]), state_dir)
    assert result["status"] == "error"
    assert not list(outside.iterdir())


def test_selection_name_collision_preserves_previous_export(tmp_path, monkeypatch):
    class UUID:
        hex = "fixed-selection"
    monkeypatch.setattr(log_copy.uuid, "uuid4", lambda: UUID())
    one = raw_export(iter([b"one\n"]), tmp_path / "state")
    two = raw_export(iter([b"two\n"]), tmp_path / "state")
    assert one["status"] == "ready"
    assert two["status"] == "error"
    assert Path(one["export_path"]).read_bytes() == b"one\n"
