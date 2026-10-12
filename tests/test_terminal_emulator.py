"""Real VT input/output fixtures for the optional embedded-terminal engine."""
from __future__ import annotations

import json
import random

import pytest

pytest.importorskip("pyte")
from raven.terminal_emulator import MAX_INPUT, MAX_REPLY, TerminalEngine


def text(engine):
    return ["".join(segment[0] for segment in row) for row in engine.snapshot()["lines"]]


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 1000])
def test_fragmented_unicode_rendition_cursor_and_queries(chunk):
    engine = TerminalEngine(4, 20)
    output = "héllo 界e\u0301\r\n\x1b[38;2;12;34;56mcolored\x1b[0m\x1b[6n".encode()
    for offset in range(0, len(output), chunk):
        engine.feed(output[offset:offset + chunk])
    assert text(engine)[0].startswith("héllo 界é")
    assert text(engine)[1].startswith("colored")
    assert "fg:#0c2238" in engine.snapshot()["lines"][1][0][1]
    assert engine.take_replies() == b"\x1b[2;8R"
    assert engine.take_replies() == b""
    json.dumps(engine.snapshot())


def test_full_screen_editor_restores_shell_and_not_alternate_history():
    engine = TerminalEngine(4, 16)
    engine.feed(b"shell$ command")
    before = text(engine)
    engine.feed(b"\x1b[?1049h\x1b[?1h\x1b=\x1b[2J\x1b[Hfile.txt\r\nline2\x1b[4;1H-- INSERT --")
    assert engine.alternate
    assert text(engine)[3].startswith("-- INSERT --")
    assert engine.encode({"key": "up"}) == b"\x1bOA"
    engine.feed(b"\r\n" * 20)
    assert not engine.history_text()
    engine.feed(b"\x1b[?1l\x1b>\x1b[?1049l")
    assert not engine.alternate
    assert text(engine) == before
    assert engine.snapshot()["cursor"]["x"] == 14
    assert engine.encode({"key": "up"}) == b"\x1b[A"
    assert engine.encode({"key": "kp_1"}) == b"1"


@pytest.mark.parametrize("mode", [47, 1047, 1049])
def test_alternate_repeated_enable_does_not_erase_current_application(mode):
    engine = TerminalEngine(2, 10)
    engine.feed(f"\x1b[?{mode}hhello\x1b[?{mode}h".encode())
    assert text(engine)[0].startswith("hello")
    engine.feed(f"\x1b[?{mode}l".encode())
    assert not engine.alternate


def test_partial_scroll_region_does_not_pollute_shell_history():
    engine = TerminalEngine(4, 8)
    engine.feed(b"head\x1b[2;3r\x1b[3;1Hbody\n\n\n")
    assert not engine.history_text()
    assert text(engine)[0].startswith("head")


def test_history_is_bounded_with_stable_eviction_boundary_and_clear():
    engine = TerminalEngine(2, 10, history_limit=3)
    engine.feed(b"".join(f"line{i}\r\n".encode() for i in range(10)))
    snapshot = engine.snapshot()
    assert snapshot["history"] == ["line6", "line7", "line8"]
    assert snapshot["history_start"] == 6
    assert snapshot["history_count"] == 3
    assert engine.snapshot(include_history=False)["history"] == []
    assert engine.snapshot(include_history=False)["history_count"] == 3
    engine.feed(b"\x1b[3J")
    assert engine.history_text() == []
    assert engine.history_start == 9


def test_explicit_history_byte_limit_counts_utf8_and_preserves_newest_rows():
    engine = TerminalEngine(2, 10, history_limit=100, history_bytes=12)
    engine.feed(("界界\r\n" * 10).encode())
    snapshot = engine.snapshot()
    assert snapshot["history"] == ["界界", "界界"]
    assert snapshot["history_bytes"] == snapshot["history_bytes_limit"] == 12
    assert snapshot["history_start"] == 7
    engine.clear_history()
    assert engine.snapshot()["history_bytes"] == 0


def test_history_byte_limit_cannot_exceed_legacy_ceiling_or_go_negative():
    from raven.terminal_emulator import MAX_HISTORY_BYTES
    assert TerminalEngine(history_bytes=10**9).history_bytes_limit == MAX_HISTORY_BYTES
    engine = TerminalEngine(2, 8, history_bytes=-1)
    engine.feed(b"line\r\n" * 10)
    assert engine.history_text() == []
    assert engine.history_bytes_limit == 0


@pytest.mark.parametrize("sequence", [b"\x1b]52;c;secret\x07", b"\x1bPqgraphics\x1b\\", b"\x1b_private\x1b\\", b"\x1b^private\x1b\\"])
def test_untrusted_strings_never_reach_screen_or_outer_control(sequence):
    engine = TerminalEngine(2, 20)
    for byte in sequence:
        engine.feed(bytes([byte]))
    engine.feed(b"safe")
    assert text(engine)[0].startswith("safe")
    assert engine.unsupported + engine.clipboard_denied == 1
    assert engine.take_replies() == b""
    assert "secret" not in json.dumps(engine.snapshot())


def test_title_is_framed_sanitized_bounded_and_never_a_control_escape():
    engine = TerminalEngine(2, 10)
    engine.feed(b"\x1b]0;title\n\tbad\x1b[31m\x1b\\X")
    assert engine.title == "titlebad[31m"
    assert text(engine)[0].startswith("X")
    engine.feed(b"\x1b]2;" + b"a" * 1000 + b"\x07")
    assert len(engine.title) == 512


@pytest.mark.parametrize("prefix,ending", [(b"\x1b]2;", b"\x07"), (b"\x1bP", b"\x1b\\"), (b"\x1b[", b"m")])
def test_hostile_unterminated_frame_has_bounded_memory_and_recovers(prefix, ending):
    engine = TerminalEngine(2, 10)
    engine.feed(prefix)
    for _ in range(100):
        engine.feed(b"1" * 10000)
    assert len(engine._control) <= 4096
    assert engine._overflow
    engine.feed(ending + b"OK")
    assert text(engine)[0].startswith("OK")
    assert engine.unsupported == 1


def test_control_cancels_malformed_frame_and_next_queries_still_work():
    engine = TerminalEngine(2, 10)
    engine.feed(b"\x1b[123\x18OK\x1b[5n")
    assert text(engine)[0].startswith("OK")
    assert engine.take_replies() == b"\x1b[0n"
    engine.feed("\x1b[²m".encode())
    engine.feed(b"\rGOOD\x1b[5n")
    assert text(engine)[0].startswith("GOOD")
    assert engine.take_replies() == b"\x1b[0n"


def test_reply_flood_is_bounded():
    engine = TerminalEngine(2, 10)
    engine.feed(b"\x1b[5n" * 20000)
    assert len(engine.take_replies()) <= MAX_REPLY
    assert engine.unsupported > 0


def test_combining_and_saved_cursor_flood_are_bounded():
    engine = TerminalEngine(2, 10)
    engine.feed(("x" + "\u0301" * 20000).encode())
    assert len(text(engine)[0]) <= 42
    assert engine.unsupported >= 1
    engine.feed(b"\x1b7" * 10000)
    assert len(engine.screen.savepoints) <= 16


@pytest.mark.parametrize("event,expected", [
    ({"key": "y"}, b"y"), ({"key": "q"}, b"q"), ({"key": ":"}, b":"),
    ({"key": "escape"}, b"\x1b"), ({"key": "Ctrl-C"}, b"\x03"),
    ({"key": "c", "ctrl": True}, b"\x03"), ({"key": "Ctrl-D"}, b"\x04"),
    ({"key": "Ctrl-Z"}, b"\x1a"), ({"key": "]", "ctrl": True}, b"\x1d"),
    ({"key": " ", "ctrl": True}, b"\x00"), ({"key": "?", "ctrl": True}, b"\x7f"),
    ({"key": "x", "alt": True}, b"\x1bx"), ({"key": "tab", "shift": True}, b"\x1b[Z"),
    ({"key": "up", "ctrl": True}, b"\x1b[1;5A"), ({"key": "delete"}, b"\x1b[3~"),
    ({"key": "F1"}, b"\x1bOP"), ({"key": "F12", "shift": True}, b"\x1b[24;2~"),
    ({"key": "backspace"}, b"\x7f"), ({"key": "enter"}, b"\r"),
    ({"type": "text", "text": "界"}, "界".encode()),
    ({"type": "raw", "data": "\x1b[97;5u"}, b"\x1b[97;5u"),
])
def test_semantic_keyboard_belongs_to_child(event, expected):
    assert TerminalEngine().encode(event) == expected


def test_raw_rejects_unframed_or_multiple_frames():
    engine = TerminalEngine()
    for value in ("rm -rf /", "\x1b[Aquit", "\x1b]52;bad\x07", "\x1b[A\x1b[B", "\x1b[" + "1" * 1000 + "m"):
        with pytest.raises(ValueError):
            engine.encode({"type": "raw", "data": value})


def test_current_modes_control_input_encoding_and_reset():
    engine = TerminalEngine()
    assert engine.encode({"type": "focus", "focused": True}) == b""
    engine.feed(b"\x1b[?1;1004;2004h\x1b=")
    assert engine.encode({"key": "up"}) == b"\x1bOA"
    assert engine.encode({"key": "kp_enter"}) == b"\x1bOM"
    assert engine.encode({"type": "focus", "focused": True}) == b"\x1b[I"
    assert engine.encode({"type": "focus", "focused": False}) == b"\x1b[O"
    assert engine.encode({"type": "paste", "text": "one\ntwo"}) == b"\x1b[200~one\ntwo\x1b[201~"
    engine.feed(b"\x1bc")
    assert engine.encode({"key": "up"}) == b"\x1b[A"
    assert engine.encode({"type": "paste", "text": "one\ntwo"}) == b"one\ntwo"


def test_large_paste_is_whole_or_rejected_and_cannot_end_its_own_frame():
    engine = TerminalEngine()
    engine.feed(b"\x1b[?2004h")
    value = "a" * MAX_INPUT
    assert engine.encode({"type": "paste", "text": value}) == b"\x1b[200~" + value.encode() + b"\x1b[201~"
    with pytest.raises(ValueError):
        engine.encode({"type": "paste", "text": value + "x"})
    with pytest.raises(ValueError):
        engine.encode({"type": "paste", "text": "bad\x1b[201~command\n"})


def test_mouse_modes_drag_modifiers_outside_release_and_disable():
    engine = TerminalEngine(8, 40)
    event = {"type": "mouse", "x": 4, "y": 2, "button": 0, "action": "press"}
    assert engine.encode(event) == b""
    engine.feed(b"\x1b[?1002;1006h")
    assert engine.encode(event) == b"\x1b[<0;5;3M"
    assert engine.encode(dict(event, action="move", shift=True)) == b"\x1b[<36;5;3M"
    assert engine.encode(dict(event, action="move", button=-1)) == b""
    assert engine.encode(dict(event, action="release", x=99, y=-2)) == b"\x1b[<0;40;1m"
    assert engine.encode(dict(event, action="press", x=99)) == b""
    assert engine.encode(dict(event, action="wheel", delta=-1)) == b"\x1b[<65;5;3M"
    engine.feed(b"\x1b[?1002l")
    assert engine.encode(event) == b""


def test_legacy_mouse_does_not_overflow_byte_coordinates():
    engine = TerminalEngine(160, 400)
    engine.feed(b"\x1b[?1000h")
    event = {"type": "mouse", "x": 4, "y": 2, "button": 1, "action": "press"}
    assert engine.encode(event) == b"\x1b[M!%#"
    assert engine.encode(dict(event, x=300)) == b""
    assert engine.encode(dict(event, action="move")) == b""


def test_resize_preserves_rows_bounds_and_invalidates_damage():
    engine = TerminalEngine(5, 20)
    engine.feed("long 界e\u0301 line\x1b[5;19Htail".encode())
    engine.consume_damage()
    engine.resize(2, 8)
    snapshot = engine.snapshot()
    assert snapshot["rows"] == 2 and snapshot["cols"] == 8
    assert len(snapshot["lines"]) == 2
    assert 0 <= snapshot["cursor"]["x"] < 8
    assert 0 <= snapshot["cursor"]["y"] < 2
    assert engine.consume_damage() == [0, 1]
    engine.resize(2, 8)
    assert engine.consume_damage() == []
    engine.resize(6, 30)
    assert len(text(engine)) == 6


def test_snapshot_immutability_damage_order_and_cache():
    engine = TerminalEngine(2, 10)
    before = engine.snapshot()
    engine.consume_damage()
    engine.feed(b"hello")
    assert engine.consume_damage() == [0]
    after = engine.snapshot()
    assert before["lines"][0][0][0] == " " * 10
    assert after["lines"][0][0][0].startswith("hello")
    after["lines"][0][0][0] = "mutated"
    assert text(engine)[0].startswith("hello")


def test_colors_cursor_shape_visibility_and_column_mode_containment():
    engine = TerminalEngine(3, 20)
    engine.feed(b"\x1b[31;44;1;4mA\x1b[38:2::1:2:3mB\x1b[0m\x1b[?25l\x1b[5 q")
    snapshot = engine.snapshot()
    assert "fg:#cd0000" in snapshot["lines"][0][0][1]
    assert "bg:#0000ee" in snapshot["lines"][0][0][1]
    assert "bold" in snapshot["lines"][0][0][1]
    assert "under" in snapshot["lines"][0][0][1]
    assert "fg:#010203" in snapshot["lines"][0][1][1]
    assert snapshot["cursor"]["shape"] == 5
    assert not snapshot["cursor"]["visible"]
    engine.feed(b"\x1b[?3h")
    assert engine.screen.columns == engine.cols == 20
    engine.feed(b"\x1b[?25h")
    assert engine.snapshot()["cursor"]["visible"]


@pytest.mark.parametrize("rows,cols", [(0, 2), (2, 0), (161, 2), (2, 401), (True, 10)])
def test_geometry_limits(rows, cols):
    with pytest.raises(ValueError):
        TerminalEngine(rows, cols)


def test_random_fragmented_hostile_output_does_not_escape_or_break_next_frame():
    rng = random.Random(713)
    engine = TerminalEngine(7, 24, 8)
    fragments = [b"\x1b[", b"\x1b]", b"\x1bP", b"\x18", b"\x1b\\", b"\x07", b"\xff", b"\x1b[?1049h", b"\x1b[?1049l", b"42", b";", b"m", b"\n", "界".encode()]
    for _ in range(1000):
        engine.feed(rng.choice(fragments))
        snapshot = engine.snapshot(include_history=False)
        assert len(snapshot["lines"]) == 7
        assert snapshot["history_count"] <= 8
        assert "\x1b" not in "".join("".join(segment[0] for segment in row) for row in snapshot["lines"])
    engine.feed(b"\x18\x1bc\x1b[HRECOVERED\x1b[5n")
    assert text(engine)[0].startswith("RECOVERED")
    assert engine.take_replies().endswith(b"\x1b[0n")


@pytest.mark.parametrize("key,expected", [
    ("ctrl-space", b"\x00"), ("ctrl-\\", b"\x1c"), ("ctrl-]", b"\x1d"),
    ("ctrl-^", b"\x1e"), ("ctrl-_", b"\x1f"), ("btab", b"\x1b[Z"),
    ("space", b" "), ("ctrl-backspace", b"\x08"), ("alt-backspace", b"\x1b\x7f"),
    ("pgup", b"\x1b[5~"), ("alt-pgdn", b"\x1b[6;3~"),
    ("ctrl-alt-left", b"\x1b[1;7D"), ("f13", b"\x1b[1;2P"), ("f24", b"\x1b[24;2~"),
])
def test_outer_decoder_key_vocabulary(key, expected):
    assert TerminalEngine().encode({"key": key}) == expected


def test_dec_graphics_remain_compatible_with_utf8_output():
    engine = TerminalEngine(2, 20)
    engine.feed("\x1b(0lqqk\x1b(B café".encode())
    assert text(engine)[0].startswith("┌──┐ café")


def test_wide_glyph_at_right_edge_wraps_without_render_overflow():
    from wcwidth import wcswidth
    engine = TerminalEngine(3, 5)
    engine.feed("abcd界X".encode())
    lines = text(engine)
    assert lines[0] == "abcd "
    assert lines[1].startswith("界X")
    assert all(wcswidth(line) == 5 for line in lines)
    engine.feed(b"\x1b[2;2HZ")
    assert text(engine)[1].startswith(" ZX")
    assert all(wcswidth(line) == 5 for line in text(engine))


def test_combining_mark_after_wide_glyph_and_cursor_abuse_are_bounded():
    engine = TerminalEngine(2, 10)
    engine.feed("界\u0301".encode())
    assert text(engine)[0].startswith("界\u0301")
    for _ in range(100):
        engine.feed("x\x1b[2G\u0301".encode())
    assert all(len(cell.data) <= 9 for row in engine.screen.buffer.values() for cell in row.values())


def test_unsupported_joined_emoji_does_not_discard_following_text():
    engine = TerminalEngine(2, 30)
    engine.feed("👩\u200d💻 after".encode())
    assert "after" in text(engine)[0]
    assert engine.unsupported >= 1


def test_max_geometry_snapshot_fits_broker_budget():
    engine = TerminalEngine(160, 400)
    cell = engine.screen.default_char
    for row in range(160):
        for col in range(400):
            engine.screen.buffer[row][col] = cell._replace(
                data="x" + "\u0301" * 8, fg=f"{row * 400 + col:06x}", bg="010203",
                bold=True, underscore=True, reverse=True)
    encoded = json.dumps(engine.snapshot(include_history=False), ensure_ascii=False).encode("utf-8")
    assert len(encoded) < 8 * 1024 * 1024


def test_child_clipboard_writes_require_explicit_mediation_and_reads_are_denied():
    import base64
    engine = TerminalEngine()
    engine.feed(b"\x1b]52;c;" + base64.b64encode("private 界".encode()) + b"\x07")
    assert engine.take_replies() == b""
    snapshot = engine.snapshot()
    assert snapshot["clipboard_requests"] == [{"id": 1, "selection": "c", "text": "private 界"}]
    assert "private" not in "\n".join(text(engine))
    engine.feed(b"\x1b]52;c;?\x07")
    assert engine.take_replies() == b""
    assert engine.snapshot()["clipboard_denied"] == 1
    engine.dismiss_clipboard(1)
    assert engine.snapshot()["clipboard_requests"] == []


def test_child_clipboard_queue_and_payload_limits():
    import base64
    engine = TerminalEngine()
    for i in range(20):
        engine.feed(b"\x1b]52;c;" + base64.b64encode(str(i).encode()) + b"\x07")
    requests = engine.snapshot()["clipboard_requests"]
    assert len(requests) == 8
    assert requests[0]["text"] == "12" and requests[-1]["text"] == "19"
    engine.feed(b"\x1b]52;c;" + base64.b64encode(b"x" * 16384) + b"\x07")
    assert len(engine.snapshot()["clipboard_requests"][-1]["text"]) == 16384
    engine.feed(b"\x1b]52;c;" + base64.b64encode(b"x" * 16385) + b"\x07")
    assert engine.snapshot()["clipboard_denied"] == 1


def test_hyperlink_metadata_tracks_cells_through_sgr_scroll_and_erase():
    engine = TerminalEngine(2, 12)
    engine.feed(b"\x1b]8;;https://example.org/result\x1b\\click\x1b[31m here\x1b]8;;\x1b\\\r\nnext")
    link = engine.snapshot()["links"][0]
    assert link == {"id": 1, "row": 0, "start": 0, "end": 10, "url": "https://example.org/result"}
    engine.feed(b"\r\nmore")
    assert engine.snapshot()["links"] == []
    assert "click here" in engine.history_text()
    assert engine.take_replies() == b""


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://host/\x1b[1m", "https://host/\n", "http://[bad", "https://host/" + "x" * 3000])
def test_unsafe_link_destinations_are_not_exposed(url):
    engine = TerminalEngine()
    engine.feed(("\x1b]8;;" + url + "\x1b\\label\x1b]8;;\x1b\\").encode())
    assert engine.snapshot()["links"] == []
    assert engine.unsupported >= 1


def test_hyperlink_registry_eviction_never_retargets_cached_cells():
    engine = TerminalEngine(4, 100)
    engine.feed(b"\x1b]8;;https://original\x1b\\old\x1b]8;;\x1b\\")
    assert engine.snapshot()["links"][0]["url"] == "https://original"
    for i in range(129):
        engine.feed(f"\x1b]8;;https://new{i}\x1b\\\x1b]8;;\x1b\\".encode())
    assert engine.snapshot()["links"] == []
    assert len(engine._links) == 128


def test_real_vim_pty_edit_exit_restores_shell(tmp_path):
    """Exercise an installed editor, rather than only replaying escape fixtures."""
    import shutil
    import subprocess
    import time
    from raven.terminal_service import TerminalService

    editor = shutil.which("vim") or shutil.which("vi")
    if not editor or not shutil.which("bash"):
        pytest.skip("Vim and Bash are not installed")
    version = subprocess.run([editor, "--version"], capture_output=True, timeout=5)
    if b"VIM" not in version.stdout:
        pytest.skip("Installed vi is not Vim")
    path = tmp_path / "editor.txt"
    path.write_text("alpha\nbeta\n")

    def plain(state):
        return "\n".join("".join(segment[0] for segment in row) for row in state.get("lines", []))

    with TerminalService() as service:
        sid = service.open(argv=[shutil.which("bash"), "--noprofile", "--norc", "-i"],
                           env={"PS1": "RAVEN_VT_TEST> "}, rows=12, cols=60)

        def wait(predicate):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                service.poll()
                state = service.snapshot(sid)
                if predicate(state):
                    return state
                time.sleep(.01)
            pytest.fail("Editor PTY did not reach expected state: " + repr(service.snapshot(sid)))

        wait(lambda state: "RAVEN_VT_TEST>" in plain(state))
        # --clean depends on a defaults.vim missing in some minimal Vim packages.
        # Explicit -u/-U/-i disables dotfiles without requiring those runtime files.
        import shlex
        command = shlex.join([editor, "-u", "NONE", "-U", "NONE", "-i", "NONE", "-N", str(path)])
        assert service.send(sid, {"type": "text", "text": command + "\r"})
        wait(lambda state: state.get("modes", {}).get("alternate") and "alpha" in plain(state))
        assert service.send(sid, {"type": "text", "text": "iEdited "})
        assert service.send(sid, {"type": "key", "key": "esc"})
        assert service.send(sid, {"type": "text", "text": ":wq\r"})
        wait(lambda state: not state.get("modes", {}).get("alternate") and "RAVEN_VT_TEST>" in plain(state))
        assert path.read_text() == "Edited alpha\nbeta\n"


def test_soft_wrap_metadata_distinguishes_real_newline_and_survives_history():
    engine = TerminalEngine(2, 5, 10)
    engine.feed(b"1234567\r\nnext\r\n")
    snapshot = engine.snapshot()
    assert snapshot["history"] == ["12345", "67"]
    assert snapshot["history_wrapped"] == [True, False]
    assert snapshot["line_wrapped"] == [False, False]
    engine.feed(b"\x1b[H123456")
    assert engine.snapshot()["line_wrapped"][0]
    engine.feed(b"\x1b[2J")
    assert not any(engine.snapshot()["line_wrapped"])


def test_full_width_explicit_newline_is_not_a_soft_wrap():
    engine = TerminalEngine(2, 5)
    engine.feed(b"12345\r\n67890\r\n")
    assert engine.snapshot()["history_wrapped"] == [False]
    assert engine.snapshot()["line_wrapped"] == [False, False]
