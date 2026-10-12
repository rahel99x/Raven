"""Full-file copies preserve exact bytes and never publish incomplete exports."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import stat

import pytest

from raven import clipboard, log_copy
from raven.log_copy import copy_full_log
from raven.logs import LogBuffer
from raven.remote import LocalFiles, RemoteFiles


def exported(result):
    assert result["status"] in ("ready", "partial"), result["message"]
    return Path(result["export_path"])


def no_clipboard(path, state_dir, **kwargs):
    return copy_full_log(str(path), state_dir, use_osc52=False, use_tools=False, **kwargs)


@pytest.mark.parametrize("data,lines", [
    (b"", 0), (b"single", 1), (b"single\n", 1), (b"first\nsecond", 2),
    (b"\tindent\tvalue\r\nsecond\r\n", 2), ("aé→\tb\n最後\n".encode(), 2),
    (b"\xff\x00\x1braw\r\n", 1),
])
def test_exact_complete_file_with_original_tabs_newlines_and_binary_bytes(tmp_path, data, lines):
    source = tmp_path / "selected log.out"
    source.write_bytes(data)
    result = no_clipboard(source, tmp_path / "state")
    target = exported(result)
    assert target.read_bytes() == data
    assert result["bytes"] == result["snapshot_bytes"] == len(data)
    assert result["lines"] == lines
    assert result["source_path"] == str(source)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(target.parent.glob("*.tmp"))


def test_entire_log_beyond_display_tail_limit_and_long_unwrapped_lines(tmp_path):
    source = tmp_path / "huge.out"
    data = b"FIRST\toriginal\r\n" + b"x" * (3 * log_copy.CHUNK_BYTES) + b"\nLAST\tend"
    source.write_bytes(data)
    tail = LogBuffer(str(source), max_bytes=32)
    tail.refresh()
    assert tail.truncated
    result = no_clipboard(source, tmp_path / "state")
    assert exported(result).read_bytes() == data
    assert result["bytes"] > tail.max_bytes
    assert result["lines"] == 3


def test_unique_exports_preserve_previous_full_copies(tmp_path):
    source = tmp_path / "source.out"
    source.write_bytes(b"first\n")
    one = no_clipboard(source, tmp_path / "state")
    source.write_bytes(b"second\n")
    two = no_clipboard(source, tmp_path / "state")
    assert exported(one) != exported(two)
    assert exported(one).read_bytes() == b"first\n"
    assert exported(two).read_bytes() == b"second\n"


def test_empty_file_can_clear_a_text_clipboard_but_exports_zero_bytes(tmp_path, monkeypatch):
    source = tmp_path / "empty.out"
    source.touch()
    sent = []
    monkeypatch.setattr(clipboard, "osc52", lambda text, path: sent.append(text) or True)
    result = copy_full_log(str(source), tmp_path / "state", use_tools=False)
    assert exported(result).read_bytes() == b""
    assert sent == [""]
    assert "OSC 52 request sent" in result["message"]


def test_oversized_clipboard_does_not_hide_full_export_or_claim_prefix_success(tmp_path, monkeypatch):
    source = tmp_path / "large.out"
    data = b"start\n" + b"x" * 80000 + b"\nend\n"
    source.write_bytes(data)
    monkeypatch.setattr(clipboard, "osc52", lambda *args: pytest.fail("oversized clipboard must not be emitted"))
    result = copy_full_log(str(source), tmp_path / "state", use_tools=False)
    assert exported(result).read_bytes() == data
    assert result["status"] == "partial"
    assert result["clipboard"]["methods"] == []
    assert "OSC 52 skipped" in result["message"]
    assert "Saved full log" in result["message"]


def test_binary_source_still_has_complete_raw_export_and_no_text_clipboard(tmp_path, monkeypatch):
    source = tmp_path / "raw.out"
    source.write_bytes(b"good\n\xff\n")
    monkeypatch.setattr(clipboard, "osc52", lambda *args: pytest.fail("invalid text must not be copied"))
    monkeypatch.setattr(clipboard, "_tool_from_file", lambda *args: pytest.fail("invalid text must not be copied"))
    result = copy_full_log(str(source), tmp_path / "state")
    assert exported(result).read_bytes() == b"good\n\xff\n"
    assert result["status"] == "partial"
    assert "not valid UTF-8" in result["message"]


def test_clipboard_failure_preserves_successful_full_export(tmp_path, monkeypatch):
    source = tmp_path / "source.out"
    source.write_bytes(b"complete\n")
    def fail(*args, **kwargs):
        raise OSError("display lost\x1b[0m")
    monkeypatch.setattr(clipboard, "copy_file", fail)
    result = copy_full_log(str(source), tmp_path / "state")
    assert exported(result).read_bytes() == b"complete\n"
    assert result["status"] == "partial"
    assert "Clipboard delivery failed" in result["message"]
    assert "\x1b" not in result["message"]


@pytest.mark.parametrize("kind", ["missing", "fifo", "directory"])
def test_local_nonregular_or_missing_source_never_publishes_export(tmp_path, kind):
    source = tmp_path / "bad-source"
    if kind == "fifo":
        os.mkfifo(source)
    elif kind == "directory":
        source.mkdir()
    result = no_clipboard(source, tmp_path / "state")
    assert result["status"] == "error"
    assert result["export_path"] == ""
    assert not list((tmp_path / "state").rglob("*.log"))


def test_explicit_local_log_symlink_reads_the_regular_target(tmp_path):
    target, source = tmp_path / "target.out", tmp_path / "current.out"
    target.write_bytes(b"actual source\n")
    source.symlink_to(target)
    result = no_clipboard(source, tmp_path / "state")
    assert exported(result).read_bytes() == b"actual source\n"


@pytest.mark.parametrize("change", ["rotate", "rewrite", "append"])
def test_actual_local_log_mutation_during_stream_is_detected(tmp_path, monkeypatch, change):
    source = tmp_path / "source.out"
    initial = b"first\tline\n"
    source.write_bytes(initial)
    real_read = os.read
    changed = [False]

    def read(fd, count):
        data = real_read(fd, count)
        if not changed[0]:
            changed[0] = True
            if change == "rotate":
                source.rename(tmp_path / "rotated.out")
                source.write_bytes(b"replacement\n")
            elif change == "rewrite":
                source.write_bytes(b"x" * len(initial))
            else:
                with source.open("ab") as output:
                    output.write(b"new append\n")
        return data

    monkeypatch.setattr(log_copy.os, "read", read)
    result = no_clipboard(source, tmp_path / "state")
    if change == "append":
        assert exported(result).read_bytes() == initial
        assert result["grew"]
        assert "initial" in result["message"]
    else:
        assert result["status"] == "error"
        assert result["export_path"] == ""
        assert not list((tmp_path / "state").rglob("*.log"))


def test_cancellation_before_clipboard_removes_completed_unadvertised_export(tmp_path, monkeypatch):
    source, state_dir = tmp_path / "source.out", tmp_path / "state"
    source.write_bytes(b"complete log\n")
    monkeypatch.setattr(clipboard, "copy_file", lambda *a, **kw: pytest.fail("cancelled app must not send clipboard"))
    result = copy_full_log(str(source), state_dir,
                           cancel=lambda: bool(list((state_dir / "exports").glob("*.log"))))
    assert result["status"] == "error"
    assert "cancelled" in result["message"]
    assert result["export_path"] == ""
    assert not list(state_dir.rglob("*.log"))


class FileFixture:
    remote = True

    def __init__(self, data, *, mode="stable"):
        self.data = data
        self.mode = mode
        self.stats = 0
        self.reads = []

    def stat(self, path):
        assert path == "/remote/selected.out"
        self.stats += 1
        if self.mode == "rotation" and self.reads:
            return len(self.data), (1, 3)
        if self.mode == "truncate" and self.reads:
            return len(self.data) - 1, (1, 2)
        if self.mode == "grow" and self.reads:
            return len(self.data) + 100, (1, 2)
        return len(self.data), (1, 2)

    def read(self, path, offset, length):
        assert path == "/remote/selected.out"
        assert length <= log_copy.CHUNK_BYTES
        self.reads.append((offset, length))
        if self.mode == "failed":
            raise OSError("SSH disconnected")
        if self.mode == "short":
            return self.data[offset:offset + max(0, length - 1)]
        return self.data[offset:offset + length]


def test_remote_entire_file_streamed_by_offset_without_local_fallback(tmp_path):
    data = b"FIRST\t\n" + b"x" * (2 * log_copy.CHUNK_BYTES + 20) + b"\nLAST\n"
    files = FileFixture(data)
    result = copy_full_log("/remote/selected.out", tmp_path / "state", files=files, use_osc52=False, use_tools=False)
    assert exported(result).read_bytes() == data
    assert len(files.reads) == 3
    assert files.reads[0] == (0, log_copy.CHUNK_BYTES)
    assert files.reads[1] == (log_copy.CHUNK_BYTES, log_copy.CHUNK_BYTES)
    assert result["source_path"] == "/remote/selected.out"


@pytest.mark.parametrize("mode", ["rotation", "truncate", "short", "failed"])
def test_remote_copy_errors_remove_partial_exports_and_never_send_clipboard(tmp_path, monkeypatch, mode):
    files = FileFixture(b"real\nlog\n", mode=mode)
    monkeypatch.setattr(clipboard, "copy_file", lambda *a, **kw: pytest.fail("failed source must not be copied"))
    result = copy_full_log("/remote/selected.out", tmp_path / "state", files=files)
    assert result["status"] == "error"
    assert result["export_path"] == ""
    assert not list((tmp_path / "state").rglob("*.*"))


def test_growing_log_exports_truthful_initial_snapshot(tmp_path):
    files = FileFixture(b"initial\n", mode="grow")
    result = copy_full_log("/remote/selected.out", tmp_path / "state", files=files, use_osc52=False, use_tools=False)
    assert exported(result).read_bytes() == b"initial\n"
    assert result["status"] == "partial"
    assert result["grew"] is True
    assert result["bytes"] == result["snapshot_bytes"] == 8
    assert "initial 8-byte snapshot" in result["message"]


def test_remote_same_size_in_place_mutation_is_rejected(tmp_path):
    class Files(FileFixture):
        def snapshot_stat(self, path):
            self.stats += 1
            return {"size": len(self.data), "ident": (1, 2), "updated": self.stats > 2}

    result = copy_full_log("/remote/selected.out", tmp_path / "state", files=Files(b"initial\n"), use_osc52=False, use_tools=False)
    assert result["status"] == "error"
    assert "changed in place" in result["message"]
    assert result["export_path"] == ""


def test_cancellation_checks_each_chunk_and_cleans_partial_export(tmp_path, monkeypatch):
    files = FileFixture(b"x" * (3 * log_copy.CHUNK_BYTES))
    monkeypatch.setattr(clipboard, "copy_file", lambda *a, **kw: pytest.fail("cancelled source must not reach clipboard"))
    result = copy_full_log("/remote/selected.out", tmp_path / "state", files=files,
                           cancel=lambda: len(files.reads) >= 1)
    assert result["status"] == "error"
    assert "cancelled" in result["message"]
    assert len(files.reads) == 1
    assert result["export_path"] == ""
    assert not list((tmp_path / "state").rglob("*.*"))


def test_cancelled_before_start_does_not_touch_source_or_destination(tmp_path):
    result = no_clipboard(tmp_path / "does-not-exist", tmp_path / "state", cancel=lambda: True)
    assert result["status"] == "error"
    assert "cancelled" in result["message"]
    assert not (tmp_path / "state").exists()


def test_export_directory_symlink_cannot_redirect_full_copy(tmp_path):
    source, state_dir, outside = tmp_path / "source.out", tmp_path / "state", tmp_path / "outside"
    source.write_bytes(b"data\n")
    state_dir.mkdir()
    outside.mkdir()
    (state_dir / "exports").symlink_to(outside)
    result = no_clipboard(source, state_dir)
    assert result["status"] == "error"
    assert not list(outside.iterdir())


def test_copy_cannot_overwrite_existing_export_on_name_collision(tmp_path, monkeypatch):
    class UUID:
        hex = "fixed-token"
    monkeypatch.setattr(log_copy.uuid, "uuid4", lambda: UUID())
    source = tmp_path / "source.out"
    source.write_bytes(b"first\n")
    first = no_clipboard(source, tmp_path / "state")
    source.write_bytes(b"second\n")
    second = no_clipboard(source, tmp_path / "state")
    assert second["status"] == "error"
    assert exported(first).read_bytes() == b"first\n"
    assert not list(exported(first).parent.glob("*.tmp"))


def test_real_ssh_adapter_snapshots_regular_target_and_exact_byte_reads(tmp_path):
    data = b"first\tline\r\nlast\n"
    calls = []

    class SSH:
        def run(self, command, timeout):
            calls.append(command)
            if command[0] == "stat":
                assert command[:4] == ["stat", "-L", "-c", "%f|%d|%i|%s|%y|%z"]
                return f"81a4|1|2|{len(data)}|2026-10-05 10:00:00.123 +0000|2026-10-05 10:00:00.123 +0000", 0
            assert command[:2] == ["sh", "-c"]
            assert "dd if=" in command[2]
            return base64.b64encode(data).decode(), 0

    result = copy_full_log("/remote/selected.out", tmp_path / "state", files=RemoteFiles(SSH()),
                           use_osc52=False, use_tools=False)
    assert exported(result).read_bytes() == data
    assert len([call for call in calls if call[0] == "sh"]) == 1


@pytest.mark.parametrize("metadata", [
    "41ed|1|2|0|date|date", "81a4|1|2|-1|date|date", "81a4|1|2|2|bad\n|date", "invalid", "x" * 1025,
])
def test_remote_nonregular_or_bad_snapshot_metadata_refused(metadata):
    class SSH:
        def run(self, command, timeout):
            return metadata, 0
    with pytest.raises(OSError):
        RemoteFiles(SSH()).snapshot_stat("/remote/selected.out")
