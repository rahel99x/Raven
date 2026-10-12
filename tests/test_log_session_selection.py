"""Logical log cursor/range behavior and lossless retained-line copying."""
from __future__ import annotations

import os

import pytest

from raven.logs import LogBuffer, LogSession


def log_session(tmp_path, content=None, max_bytes=4096, page=5):
    path = tmp_path / "source.log"
    path.write_bytes(content if content is not None else b"".join(f"line {i:03d}\n".encode() for i in range(100)))
    session = LogSession(max_bytes=max_bytes)
    session.page = page
    return session, session.buffer(str(path)), path


def test_arrows_position_a_logical_line_cursor_before_starting_selection(tmp_path):
    session, buf, path = log_session(tmp_path)
    assert session.following and session.current_line(buf) == 99
    assert session.move_cursor("up", buf) == 98
    assert session.move_cursor("up", buf) == 97
    assert session.cursor == 97 and session.top == 95
    assert session.begin_selection(buf)
    assert (session.selection_anchor, session.selection_end) == (97, 97)
    assert session.move_cursor("down", buf) == 98
    assert session.selection_text(buf) == "line 097\nline 098\n"
    assert session.is_selected(97) and session.is_selected(98)
    assert not session.is_selected(96) and not session.is_selected(99)


def test_cursor_starts_at_the_top_of_a_paused_viewport(tmp_path):
    session, buf, path = log_session(tmp_path)
    session.top = 20
    assert session.current_line(buf) == 20
    session.move_cursor("down", buf)
    assert session.cursor == 21 and session.top == 20
    session.begin_selection(buf)
    assert session.selection_anchor == 21


def test_selection_extends_across_pages_and_home_end_in_both_directions(tmp_path):
    session, buf, path = log_session(tmp_path)
    session.begin_selection(buf, 40)
    assert session.move_cursor("page_down", buf) == 44
    assert session.top <= 44 < session.top + session.page
    assert session.move_cursor("page_down", buf) == 48
    assert session.is_selected(40) and session.is_selected(48)
    session.move_cursor("home", buf)
    assert session.cursor == 0 and session.top == 0
    assert session.is_selected(0) and session.is_selected(40)
    session.move_cursor("end", buf)
    assert session.cursor == 99 and not session.following
    assert session.is_selected(40) and session.is_selected(99)
    assert session.selection_text(buf).startswith("line 040\n")
    assert session.selection_text(buf).endswith("line 099\n")


@pytest.mark.parametrize("action", ["end", "follow"])
def test_end_or_follow_without_selection_resumes_tail_and_appends(tmp_path, action):
    session, buf, path = log_session(tmp_path)
    session.move_cursor("home", buf)
    assert not session.following and session.cursor == 0
    session.move_cursor(action, buf)
    assert session.following and session.cursor is None
    with path.open("ab") as stream:
        stream.write(b"last appended\n")
    buf = session.buffer(str(path))
    assert session.current_line(buf) == 100


def test_plain_arrows_and_pages_stay_paused_and_clamp_to_retained_lines(tmp_path):
    session, buf, path = log_session(tmp_path, b"zero\none\ntwo\n", page=20)
    session.move_cursor("home", buf)
    assert session.move_cursor("up", buf) == 0
    assert session.move_cursor("page_down", buf) == 2
    assert session.move_cursor("down", buf) == 2
    assert session.cursor == 2 and session.top == 0 and not session.following
    session.move_cursor("page_up", buf)
    assert session.cursor == 0


def test_selected_copy_preserves_tabs_crlf_long_width_and_unterminated_last_line(tmp_path, monkeypatch):
    content = b"first\tcolumn\r\n" + b"x" * 2000 + b"\nfinal\ttail\r"
    session, buf, path = log_session(tmp_path, content)
    assert buf.lines[0] == "first    column"
    monkeypatch.setattr(buf, "all_lines", lambda: pytest.fail("selection must not copy normalized full-buffer lines"))
    session.begin_selection(buf, 0)
    session.move_cursor("end", buf)
    assert session.selection_bytes(buf) == content
    assert session.selection_text(buf).encode() == content


def test_source_range_has_no_headers_markers_numbers_or_added_final_newline(tmp_path):
    session, buf, path = log_session(tmp_path, b"source first\n\nsource final")
    session.begin_selection(buf, 1)
    session.move_cursor("down", buf)
    assert session.selection_text(buf) == "\nsource final"
    assert buf.raw_range(2, 1) == b"\nsource final"
    assert buf.raw_range(0, 0) == b"source first\n"


def test_selection_bytes_keep_invalid_utf8_and_crlf_exact(tmp_path):
    raw = b"prefix\t\xff\r\nlast\x80"
    session, buf, path = log_session(tmp_path, raw)
    session.begin_selection(buf, 0)
    session.move_cursor("end", buf)
    assert session.selection_bytes(buf) == raw
    assert session.selection_text(buf) == raw.decode("utf-8", "replace")


def test_harmless_append_preserves_explicit_cursor_range_and_selected_source(tmp_path):
    session, buf, path = log_session(tmp_path)
    session.begin_selection(buf, 20)
    session.move_cursor("down", buf)
    before = session.selection_bytes(buf)
    with path.open("ab") as stream:
        stream.write(b"later line\n")
    buf = session.buffer(str(path))
    assert session.selection_active and session.cursor == 21
    assert session.selection_bytes(buf) == before
    assert len(buf.raw_lines) == len(buf.lines)


def test_partial_line_completion_preserves_the_logical_line_anchor_and_raw_bytes(tmp_path):
    session, buf, path = log_session(tmp_path, b"first\npartial\t")
    session.begin_selection(buf, 1)
    with path.open("ab") as stream:
        stream.write("🚀\r\nnext\n".encode())
    buf = session.buffer(str(path))
    assert session.selection_active and session.cursor == 1
    assert session.selection_bytes(buf) == "partial\t🚀\r\n".encode()
    assert buf.lines[1] == "partial    🚀"


def test_viewport_resize_keeps_the_logical_cursor_and_range_visible(tmp_path):
    session, buf, path = log_session(tmp_path)
    session.begin_selection(buf, 97)
    session.move_cursor("down", buf)
    before = session.selection_bytes(buf)
    session.page = 1
    buf = session.buffer(str(path))
    assert session.cursor == 98 and session.top == 98
    assert session.selection_active and session.selection_bytes(buf) == before
    session.page = 20
    session.sync_buffer(buf)
    assert session.top <= 98 < session.top + session.page
    assert session.selection_bytes(buf) == before


@pytest.mark.parametrize("change", ["truncate", "rotate", "evict", "jump"])
def test_file_generation_changes_clear_stale_cursor_and_selections(tmp_path, change):
    session, buf, path = log_session(tmp_path, b"first\nsecond\nthird\n", max_bytes=64)
    session.begin_selection(buf, 1)
    session.move_cursor("down", buf)
    session.match, session.last_bookmark = 1, 1
    if change == "truncate":
        path.write_bytes(b"new\n")
    elif change == "rotate":
        replacement = tmp_path / "replacement.log"
        replacement.write_bytes(b"different inode\n")
        os.replace(replacement, path)
    elif change == "evict":
        for i in range(12):
            with path.open("ab") as stream:
                stream.write(f"append {i:03d}\n".encode())
            buf = session.buffer(str(path))
    else:
        with path.open("ab") as stream:
            stream.write(b"burst\n" * 100)
    buf = session.buffer(str(path))
    assert not session.selection_active
    assert session.cursor is None and session.match is None and session.last_bookmark is None
    assert session.selection_bytes(buf) == b""
    assert len(buf.raw_lines) == len(buf.lines) == len(buf._line_bytes)


def test_file_switch_cancels_all_selection_without_copying_the_other_file(tmp_path):
    session, first, path = log_session(tmp_path, b"only original\n")
    session.select_all(first)
    assert session.selection_active and session.selection_all and session.is_selected(0)
    other = tmp_path / "other.log"
    other.write_bytes(b"other source\n")
    second = session.buffer(str(other))
    assert not session.selection_active and not session.selection_all
    assert session.selection_bytes(second) == b""
    session.buffer(str(path))
    assert not session.selection_active


def test_all_selection_marks_retained_lines_but_defers_copy_to_full_file_worker(tmp_path):
    session, buf, path = log_session(tmp_path)
    assert session.select_all(buf)
    assert session.selection_all and session.selection_active
    assert all(session.is_selected(i) for i in range(buf.total))
    assert session.selection_bytes(buf) == b"" and session.selection_text(buf) == ""
    session.clear_selection()
    assert not session.selection_active


@pytest.mark.parametrize("condition", ["empty", "missing", "error_after_read", "none"])
def test_empty_or_unreadable_buffers_cannot_select_or_yank_stale_lines(tmp_path, condition):
    session, buf, path = log_session(tmp_path, b"old\n")
    session.begin_selection(buf, 0)
    if condition == "empty":
        path.write_bytes(b"")
        buf = session.buffer(str(path))
    elif condition == "missing":
        buf = session.buffer(str(tmp_path / "absent.log"))
    elif condition == "error_after_read":
        path.unlink()
        buf = session.buffer(str(path))
    else:
        buf = None
    assert session.current_line(buf) is None
    assert session.move_cursor("down", buf) is None
    assert not session.begin_selection(buf) and not session.select_all(buf)
    assert session.selection_text(buf) == ""
    assert not session.selection_active


def test_search_goto_moves_the_logical_cursor_and_selection_uses_that_line(tmp_path):
    session, buf, path = log_session(tmp_path)
    session.move_cursor("home", buf)
    session.search = "line 060"
    assert session.find_next(buf) == 60
    assert session.cursor == 60 and session.current_line(buf) == 60
    assert session.top <= 60 < session.top + session.page
    session.begin_selection(buf)
    assert session.selection_text(buf) == "line 060\n"


def test_direct_buffer_refresh_still_invalidates_selection_before_copy(tmp_path):
    session, buf, path = log_session(tmp_path, b"one\ntwo\n")
    session.begin_selection(buf, 0)
    path.write_bytes(b"x\n")
    buf.refresh()
    assert session.selection_text(buf) == ""
    assert not session.selection_active and session.cursor is None
