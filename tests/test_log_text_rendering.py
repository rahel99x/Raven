"""Terminal commands in source logs cannot overwrite prefixes or copied bytes."""
from __future__ import annotations

import pytest

from raven import logs as log_module
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text
from raven.log_text import display_text
from raven.logs import LogBuffer, LogSession
from raven.model import Finished, Store
from raven.remote import LocalFiles
from raven.views import Views


@pytest.mark.parametrize("prefix,suffix", [
    ("\x1b[31m", "\x1b[0m"),
    ("\x1b[?25l", "\x1b[?25h"),
    ("\x1b[1;38;2;255;30;20m", "\x1b[0m"),
    ("\x1b]0;worker title\x07", ""),
    ("\x1b]8;;https://example.test/log\x1b\\", "\x1b]8;;\x1b\\"),
    ("\x1bP1;2|private data\x1b\\", ""),
    ("\x1bXprivate data\x1b\\", ""),
    ("\x1b^private data\x1b\\", ""),
    ("\x1b_private data\x1b\\", ""),
    ("\x1b(B", ""),
    ("\x1b7", "\x1b8"),
    ("\x9b31m", "\x9b0m"),
    ("\x9d0;worker title\x9c", ""),
    ("\x90private data\x9c", ""),
    ("\x98private data\x9c", ""),
    ("\x9eprivate data\x9c", ""),
    ("\x9fprivate data\x9c", ""),
])
def test_complete_terminal_commands_preserve_visible_prefix_and_raw_copy(tmp_path, prefix, suffix):
    text = prefix + "ABprefix 界 🚀" + suffix + "\r\n"
    path = tmp_path / "source.log"
    raw = text.encode()
    path.write_bytes(raw)
    buf = LogBuffer(str(path))
    assert buf.refresh()
    assert buf.lines == ["ABprefix 界 🚀"]
    assert buf.total == 1
    assert buf.raw_range(0, 0) == raw


@pytest.mark.parametrize("source,expected", [
    ("ABprefix\rCDsuffix", "ABprefix^MCDsuffix"),
    ("ABprefix\b\bCDsuffix", "ABprefix^H^HCDsuffix"),
    ("ABprefix\x00CDsuffix", "ABprefix^@CDsuffix"),
    ("ABprefix\x07CDsuffix", "ABprefix^GCDsuffix"),
    ("ABprefix\x7fCDsuffix", "ABprefix^?CDsuffix"),
    ("ABprefix\x85CDsuffix", "ABprefix\\x85CDsuffix"),
    ("ABprefix\tCDsuffix", "ABprefix    CDsuffix"),
])
def test_embedded_controls_are_visible_and_cannot_move_terminal_cursor(tmp_path, source, expected):
    path = tmp_path / "source.log"
    raw = (source + "\r\n").encode()
    path.write_bytes(raw)
    session = LogSession()
    buf = session.buffer(str(path))
    assert buf.lines == [expected]
    assert not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in buf.lines[0])
    session.begin_selection(buf, 0)
    assert session.selection_bytes(buf) == raw


@pytest.mark.parametrize("source,expected", [
    ("ABprefix\x1b", "ABprefix^["),
    ("\x1b[31", "^[[31"),
    ("\x1b[31\x00ABprefix", "^[[31^@ABprefix"),
    ("\x1b]unfinished title ABprefix", "^[]unfinished title ABprefix"),
    ("\x1bPunfinished payload ABprefix", "^[Punfinished payload ABprefix"),
    ("\x1bXunfinished payload ABprefix", "^[Xunfinished payload ABprefix"),
    ("\x1b_unfinished payload ABprefix", "^[_unfinished payload ABprefix"),
    ("\x1bqABprefix", "^[qABprefix"),
    ("\x1b]unfinished\x1b[31mABprefix", "^[]unfinishedABprefix"),
    ("\x9dunfinished ABprefix", "\\x9dunfinished ABprefix"),
])
def test_malformed_and_unterminated_escapes_do_not_swallow_printable_remainder(source, expected):
    assert display_text(source) == expected


def test_very_long_unterminated_escape_keeps_payload_and_normal_text_reuses_object():
    text = "ABprefix " + "x" * 2_000_000
    assert display_text(text) is text
    assert display_text("\x1b]" + text) == "^[]" + text
    repeated = ("\x1b]unfinished " * 10_000) + "ABprefix"
    normalized = display_text(repeated)
    assert normalized.count("unfinished ") == 10_000
    assert normalized.endswith("ABprefix")


def test_partial_escape_and_multibyte_character_complete_across_writes(tmp_path):
    path = tmp_path / "growing.log"
    path.write_bytes(b"")
    buf = LogBuffer(str(path))
    rocket = "🚀".encode()
    chunks = [b"\x1b[", b"31mABprefix ", rocket[:2], rocket[2:] + b"\x1b]",
              b"8;;https://example.test", b"\x1b\\", b"\tend\r", b"\n"]
    raw = b""
    for index, chunk in enumerate(chunks):
        with path.open("ab") as stream:
            stream.write(chunk)
        raw += chunk
        assert buf.refresh()
        assert "\ufffd" not in buf.partial
        assert buf.raw_range(0, 0) == raw
        assert buf.total == 1
        if index == 0:
            assert buf.partial == "^[["
        if index == 5:
            assert buf.partial == "ABprefix 🚀"
        if index == 6:
            assert buf.partial == "ABprefix 🚀    end^M"
    assert buf.lines == ["ABprefix 🚀    end"]
    assert buf.partial == ""


def test_escape_only_partial_line_retains_source_index_selection_and_search(tmp_path):
    path = tmp_path / "escape-only.log"
    raw = b"first\n\x1b[31m\x1b[0m"
    path.write_bytes(raw)
    session = LogSession()
    buf = session.buffer(str(path))
    assert buf.partial == ""
    assert buf.total == 2
    assert buf.all_lines() == ["first", ""]
    assert buf.window(0, 10) == (["first", ""], 0)
    assert buf.count("^$") == 1
    assert buf.find("^$") == 1
    session.begin_selection(buf, 1)
    assert session.selection_bytes(buf) == b"\x1b[31m\x1b[0m"
    with path.open("ab") as stream:
        stream.write(b"\nABsecond\n")
    buf = session.buffer(str(path))
    assert buf.lines == ["first", "", "ABsecond"]
    assert session.cursor == 1
    assert session.selection_bytes(buf) == b"\x1b[31m\x1b[0m\n"


def test_invalid_utf8_and_rotation_keep_new_prefixes_and_exact_new_bytes(tmp_path):
    path = tmp_path / "rotating.log"
    initial = b"\x1b[31mABfirst\x1b[0m\xff\r\n"
    path.write_bytes(initial)
    buf = LogBuffer(str(path))
    assert buf.refresh()
    assert buf.lines == ["ABfirst\ufffd"]
    assert buf.raw_range(0, 0) == initial
    path.rename(tmp_path / "old.log")
    replacement = b"\x1b[32mCDreplacement\x1b[0m\x00\n"
    path.write_bytes(replacement)
    assert buf.refresh()
    assert buf.reloads == 1
    assert buf.lines == ["CDreplacement^@"]
    assert buf.raw_range(0, 0) == replacement


@pytest.mark.parametrize("ascii_", [True, False])
def test_actual_log_view_keeps_prefixes_and_normalizes_only_on_changed_file(tmp_path, monkeypatch, ascii_):
    path = tmp_path / "77.out"
    raw = b"\x1b[31mABprefix\x1b[0m\r\nCDprefix\x00suffix\nEFprefix\rGHsuffix\n"
    path.write_bytes(raw)
    cfg = Config({"log_lines": 0})
    store = Store(state_dir=None, persist=False)
    store.finished = [Finished("77", "failed experiment", "FAILED", workdir=str(tmp_path))]
    store.details["77"] = {"StdOut": str(path), "WorkDir": str(tmp_path)}
    app = App(store, None, None, cfg, "reader", ascii_=ascii_)
    app.files = app.logs.files = LocalFiles()
    views = Views(Glyphs(ascii_), cfg, files=app.files)
    app.views_ref = views
    seen = []
    normalize = log_module.display_text
    def record(text):
        seen.append(text)
        return normalize(text)
    monkeypatch.setattr(log_module, "display_text", record)
    try:
        app.open_log("77")
        rows, hits = views.compose(store.snapshot(), app, 100, 24)
        content = [row_text(rows[y]) for y, kind, key in hits if kind == "log_line"]
        assert any("ABprefix" in text for text in content)
        assert any("CDprefix^@suffix" in text for text in content)
        assert any("EFprefix^MGHsuffix" in text for text in content)
        assert not any(ord(char) < 32 or 127 <= ord(char) <= 159 for text in content for char in text)
        calls = len(seen)
        for _ in range(20):
            views.compose(store.snapshot(), app, 100, 24)
        assert len(seen) == calls
        app.handle("home")
        app.handle("v")
        app.handle("down")
        app.handle("down")
        assert app.logs.selection_bytes(app.read_log_buffer(str(path))) == raw
    finally:
        if app.logs.catalog:
            app.logs.catalog.close()
        if app.research:
            app.research.close()
