"""A full exported log must never become a silently shortened clipboard value."""
from __future__ import annotations

import base64
import io
import json
import os
import stat
import subprocess
import sys
import tracemalloc
from pathlib import Path

import pytest

from raven import clipboard


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def plain_terminal(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("STY", raising=False)


def expected_sequence(payload):
    return b"\x1b]52;c;" + base64.b64encode(payload) + b"\x07"


@pytest.mark.parametrize("text", [
    "", "no trailing newline", "one\n", "one\n\n", "a\tb\r\nnext\r\n",
    "π\t漢字\r\n🚀\n", "\x1b[31mraw terminal text\x1b[0m\n",
])
def test_osc52_preserves_exact_text_bytes(tmp_path, text):
    tty = tmp_path / "tty"
    assert clipboard.osc52(text, str(tty)) is True
    assert tty.read_bytes() == expected_sequence(text.encode("utf-8"))


@pytest.mark.parametrize("text", ["a" * 75_000, "€" * 25_000], ids=["ascii-at-limit", "utf8-at-limit"])
def test_osc52_allows_entire_utf8_payload_at_base64_limit(tmp_path, text):
    assert len(base64.b64encode(text.encode("utf-8"))) == clipboard.OSC52_LIMIT
    tty = tmp_path / "tty"
    assert clipboard.osc52(text, str(tty)) is True
    assert tty.read_bytes() == expected_sequence(text.encode("utf-8"))


@pytest.mark.parametrize("text", ["a" * 75_001, "€" * 25_001, "🚀" * 25_001],
                         ids=["ascii-over-limit", "utf8-over-limit", "four-byte-over-limit"])
def test_osc52_refuses_oversized_payload_without_any_output(tmp_path, monkeypatch, text):
    tty = tmp_path / "tty"
    tty.write_bytes(b"existing terminal content")
    fallback = Terminal()
    monkeypatch.setattr(sys, "__stdout__", fallback)
    assert clipboard.osc52(text, str(tty)) is False
    assert tty.read_bytes() == b"existing terminal content"
    assert fallback.getvalue() == ""


@pytest.mark.parametrize("environment,wrapper", [
    ({"TMUX": "session"}, "tmux"),
    ({"STY": "session"}, "screen"),
    ({"TMUX": "session", "STY": "session"}, "tmux"),
])
def test_osc52_wraps_one_complete_sequence_for_multiplexer(tmp_path, monkeypatch, environment, wrapper):
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    tty = tmp_path / "tty"
    payload = "tabs\tand\r\nUnicode π\n".encode("utf-8")
    assert clipboard.osc52(payload.decode("utf-8"), str(tty)) is True
    seq = expected_sequence(payload)
    expected = (b"\x1bPtmux;" + seq.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"
                if wrapper == "tmux" else b"\x1bP" + seq + b"\x1b\\")
    assert tty.read_bytes() == expected


def test_missing_controlling_terminal_uses_only_a_real_terminal_stdout(tmp_path, monkeypatch):
    terminal = Terminal()
    monkeypatch.setattr(sys, "__stdout__", terminal)
    assert clipboard.osc52("π\n", str(tmp_path / "absent" / "tty")) is True
    assert terminal.getvalue().encode("ascii") == expected_sequence("π\n".encode("utf-8"))


def test_missing_controlling_terminal_does_not_leak_escapes_into_redirected_stdout(tmp_path, monkeypatch):
    redirected = io.StringIO()
    monkeypatch.setattr(sys, "__stdout__", redirected)
    assert clipboard.osc52("private log\n", str(tmp_path / "absent" / "tty")) is False
    assert redirected.getvalue() == ""


def test_osc52_handles_unavailable_stdout_without_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "__stdout__", None)
    assert clipboard.osc52("log", str(tmp_path / "absent" / "tty")) is False


@pytest.mark.parametrize("payload", [
    b"", b"no trailing newline", b"line\n", b"line\n\n", b"a\tb\r\nnext\r\n",
    "π\t漢字\r\n🚀\n".encode("utf-8"),
])
def test_copy_file_sends_entire_exact_export_and_reports_byte_count(tmp_path, payload):
    source = tmp_path / "export.log"
    source.write_bytes(payload)
    tty = tmp_path / "tty"
    result = clipboard.copy_file(str(source), tty_path=str(tty), use_tools=False)
    assert result["bytes"] == len(payload)
    assert result["text"] is True
    assert result["methods"]
    assert tty.read_bytes() == expected_sequence(payload)
    assert source.read_bytes() == payload


def test_copy_file_oversized_export_is_not_partially_sent_to_terminal(tmp_path, monkeypatch):
    payload = ("Unicode π\tlong log\r\n" * 10_000).encode("utf-8")
    source = tmp_path / "export.log"
    source.write_bytes(payload)
    tty = tmp_path / "tty"
    tty.write_bytes(b"untouched")
    fallback = Terminal()
    monkeypatch.setattr(sys, "__stdout__", fallback)
    result = clipboard.copy_file(str(source), tty_path=str(tty), use_tools=False)
    assert result["bytes"] == len(payload)
    assert result["text"] is True
    assert result["methods"] == []
    assert result["warnings"]
    assert tty.read_bytes() == b"untouched"
    assert fallback.getvalue() == ""
    assert source.read_bytes() == payload


@pytest.mark.parametrize("prefix_bytes", [32_767, 65_535, 131_071])
def test_copy_file_utf8_validation_accepts_multibyte_characters_across_chunk_boundaries(tmp_path, prefix_bytes):
    source = tmp_path / "export.log"
    payload = b"a" * prefix_bytes + "🚀\r\nπ\t".encode("utf-8") + b"x" * 75_000
    source.write_bytes(payload)
    result = clipboard.copy_file(str(source), use_osc52=False, use_tools=False)
    assert result["bytes"] == len(payload)
    assert result["text"] is True
    assert source.read_bytes() == payload


def test_copy_file_validates_large_export_with_bounded_memory(tmp_path):
    source = tmp_path / "export.log"
    chunk = ("π\tline\r\n" * 8_192).encode("utf-8")
    with source.open("wb") as output:
        for _ in range(256):
            output.write(chunk)
    tracemalloc.start()
    try:
        result = clipboard.copy_file(str(source), use_osc52=False, use_tools=False)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["bytes"] == len(chunk) * 256
    assert result["text"] is True
    assert peak < 1_000_000, "validating a completed log must not load its full contents into memory"


@pytest.mark.parametrize("payload", [b"a\xffb\r\n", b"valid prefix\n\xf0\x9f", b"\x80"])
def test_copy_file_invalid_utf8_preserves_raw_export_and_avoids_text_clipboards(tmp_path, monkeypatch, payload):
    source = tmp_path / "export.log"
    source.write_bytes(payload)
    tty = tmp_path / "tty"
    tty.write_bytes(b"untouched")
    monkeypatch.setattr(clipboard.shutil, "which", lambda command: pytest.fail("invalid UTF-8 reached a text clipboard"))
    result = clipboard.copy_file(str(source), tty_path=str(tty))
    assert result["bytes"] == len(payload)
    assert result["text"] is False
    assert result["methods"] == []
    assert result["warnings"]
    assert tty.read_bytes() == b"untouched"
    assert source.read_bytes() == payload


def test_copy_file_streams_full_large_log_to_local_tool_without_status_output_leak(tmp_path, monkeypatch):
    source = tmp_path / "source.log"
    payload = ("π\t" + "x" * 999 + "\r\n").encode("utf-8") * 2_000
    source.write_bytes(payload)
    captured = tmp_path / "received.log"
    tool = tmp_path / "tool.py"
    tool.write_text(
        "import shutil, sys\n"
        "with open(sys.argv[1], 'wb') as dst:\n"
        "    shutil.copyfileobj(sys.stdin.buffer, dst, length=8192)\n"
        "print('TOOL_STDOUT_PRIVATE')\n"
        "print('TOOL_STDERR_PRIVATE', file=sys.stderr)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(clipboard, "TOOLS", [[sys.executable, str(tool), str(captured)]])
    original_run = subprocess.run
    calls = []

    def run_streamed(command, **kwargs):
        assert "input" not in kwargs, "full log was eagerly materialized for the tool"
        assert kwargs.get("stdin") is not None, "clipboard tool needs the exported file as stdin"
        assert hasattr(kwargs["stdin"], "fileno")
        calls.append(command)
        return original_run(command, **kwargs)

    monkeypatch.setattr(clipboard.subprocess, "run", run_streamed)
    result = clipboard.copy_file(str(source), use_osc52=False)
    assert len(calls) == 1
    assert result["bytes"] == len(payload)
    assert result["text"] is True
    assert result["methods"]
    assert captured.read_bytes() == payload
    assert source.read_bytes() == payload
    assert "TOOL_STDOUT_PRIVATE" not in repr(result)
    assert "TOOL_STDERR_PRIVATE" not in repr(result)


def test_copy_file_local_tool_failure_reports_no_success_and_keeps_export(tmp_path, monkeypatch):
    source = tmp_path / "source.log"
    payload = b"exact\ttabs\r\n"
    source.write_bytes(payload)
    monkeypatch.setattr(clipboard, "TOOLS", [[sys.executable, "-c", "import sys; print('private'); sys.exit(7)"]])
    result = clipboard.copy_file(str(source), use_osc52=False)
    assert result["methods"] == []
    assert result["warnings"]
    assert "private" not in repr(result)
    assert source.read_bytes() == payload


def test_copy_file_restarts_from_beginning_when_first_local_tool_fails(tmp_path, monkeypatch):
    source = tmp_path / "source.log"
    payload = ("π\tlog\r\n" * 20_000).encode("utf-8")
    source.write_bytes(payload)
    captured = tmp_path / "received.log"
    tool = tmp_path / "tool.py"
    tool.write_text(
        "import shutil, sys\n"
        "with open(sys.argv[1], 'wb') as dst:\n"
        "    shutil.copyfileobj(sys.stdin.buffer, dst, length=8192)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(clipboard, "TOOLS", [
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read(7); sys.exit(4)"],
        [sys.executable, str(tool), str(captured)],
    ])
    result = clipboard.copy_file(str(source), use_osc52=False)
    assert result["methods"]
    assert captured.read_bytes() == payload


def test_copy_file_local_tool_timeout_does_not_leak_process_output(tmp_path, monkeypatch):
    source = tmp_path / "source.log"
    source.write_bytes(b"log\r\n")
    monkeypatch.setattr(clipboard, "TOOLS", [[sys.executable]])

    def timed_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 5, output=b"PRIVATE_STDOUT", stderr=b"PRIVATE_STDERR")

    monkeypatch.setattr(clipboard.subprocess, "run", timed_out)
    result = clipboard.copy_file(str(source), use_osc52=False)
    assert result["methods"] == []
    assert result["warnings"]
    assert "PRIVATE_STDOUT" not in repr(result)
    assert "PRIVATE_STDERR" not in repr(result)


def test_copy_file_skips_display_clipboard_tools_without_a_display(tmp_path, monkeypatch):
    source = tmp_path / "source.log"
    source.write_bytes(b"log\n")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard, "TOOLS", [["xclip", "-selection", "clipboard"], ["wl-copy"]])
    monkeypatch.setattr(clipboard.shutil, "which", lambda command: "/usr/bin/" + command)
    monkeypatch.setattr(clipboard.subprocess, "run", lambda *args, **kwargs: pytest.fail("display tool ran without display"))
    result = clipboard.copy_file(str(source), use_osc52=False)
    assert result["methods"] == []
    assert result["warnings"]


def test_copy_file_needs_no_clipboard_when_disabled(tmp_path, monkeypatch):
    source = tmp_path / "source.log"
    payload = b"exact\ttabs\r\n"
    source.write_bytes(payload)
    monkeypatch.setattr(clipboard.shutil, "which", lambda command: pytest.fail("clipboard lookup was disabled"))
    result = clipboard.copy_file(str(source), use_osc52=False, use_tools=False)
    assert result["bytes"] == len(payload)
    assert result["text"] is True
    assert result["methods"] == []
    assert source.read_bytes() == payload


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink"])
def test_copy_file_rejects_nonregular_export_without_blocking_or_clipboard_output(tmp_path, monkeypatch, kind):
    source = tmp_path / "source.log"
    if kind == "directory":
        source.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"sensitive")
        source.symlink_to(target)
    monkeypatch.setattr(clipboard.shutil, "which", lambda command: pytest.fail("bad export reached clipboard tools"))
    result = clipboard.copy_file(str(source), use_osc52=False)
    assert result["methods"] == []
    assert result["warnings"]


def test_copy_file_rejects_fifo_without_blocking(tmp_path):
    source = tmp_path / "source.log"
    os.mkfifo(source)
    completed = subprocess.run(
        [sys.executable, "-c",
         "import json, sys; from raven.clipboard import copy_file; "
         "print(json.dumps(copy_file(sys.argv[1], use_osc52=False)))", str(source)],
        capture_output=True, timeout=2, check=True,
    )
    result = json.loads(completed.stdout)
    assert result["methods"] == []
    assert result["warnings"]


@pytest.mark.parametrize("text", ["a\tb\r\n", "π\r\n🚀", "last\n\n"])
def test_to_file_preserves_exact_utf8_and_is_private(tmp_path, text):
    path = clipboard.to_file(text, str(tmp_path))
    assert path is not None
    assert Path(path).read_bytes() == text.encode("utf-8")
    assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600


def test_to_file_refuses_symlink_destination_and_preserves_target(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"keep this")
    (tmp_path / "clipboard.txt").symlink_to(target)
    assert clipboard.to_file("new log", str(tmp_path)) is None
    assert target.read_bytes() == b"keep this"
    assert (tmp_path / "clipboard.txt").is_symlink()


@pytest.mark.parametrize("name", ["../escape.txt", "nested/escape.txt"])
def test_to_file_rejects_nonbasename_export_name(tmp_path, name):
    assert clipboard.to_file("private", str(tmp_path / "exports"), name=name) is None
    assert not (tmp_path / "escape.txt").exists()


def test_to_file_failed_encoding_preserves_existing_export(tmp_path):
    target = tmp_path / "clipboard.txt"
    target.write_bytes(b"previous complete export")
    assert clipboard.to_file("invalid surrogate \ud800", str(tmp_path)) is None
    assert target.read_bytes() == b"previous complete export"


def test_to_file_replacement_makes_an_existing_public_export_private(tmp_path):
    target = tmp_path / "clipboard.txt"
    target.write_bytes(b"old")
    target.chmod(0o644)
    assert clipboard.to_file("new\tπ\r\n", str(tmp_path)) == str(target)
    assert target.read_bytes() == "new\tπ\r\n".encode("utf-8")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_copy_oversized_selection_never_claims_partial_terminal_success(tmp_path):
    text = "π\tline\r\n" * 20_000
    tty = tmp_path / "tty"
    tty.write_bytes(b"untouched")
    status = clipboard.copy(text, str(tmp_path), tty_path=str(tty), use_tools=False)
    assert "terminal clipboard (OSC 52)" not in status
    assert str(tmp_path / "clipboard.txt") in status
    assert (tmp_path / "clipboard.txt").read_bytes() == text.encode("utf-8")
    assert tty.read_bytes() == b"untouched"
