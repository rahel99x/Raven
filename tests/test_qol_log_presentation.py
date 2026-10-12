from __future__ import annotations

from types import SimpleNamespace

import pytest

from raven import layout as L, log_presentation as P, log_workbench as W
from raven.logs import LogSession
from raven.remote import LocalFiles
from raven.research import ResearchHub
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Store
from raven.views import Views


def app(path):
    files = LocalFiles()
    result = SimpleNamespace(logs=LogSession(files=files), files=files, research=ResearchHub({}, files),
                             tab="log", mode="main", height=24, message="", keymap={}, log_selection_expected=False)
    result.say = lambda text: setattr(result, "message", text)
    result.fail = lambda text: setattr(result, "message", text)
    W.initialize(result)
    result.logs.path = str(path)
    result.logs.entries = [dict(id="left", label="stdout", path=str(path), role="stdout")]
    return result


def finish(result):
    result.research.future.result(5)
    result.research.poll_task()


def text(result, ascii_=False, width=100, height=24):
    return "\n".join(L.row_text(row) for _, _, row in W.overlay(SimpleNamespace(g=L.Glyphs(ascii_)), {}, result, width, height))


def test_timestamp_aligns_distinct_offsets_without_fabricating_pairs():
    left = ["2026-10-05T12:00:00Z one", "2026-10-05T12:00:02Z three"]
    right = ["2026-10-05T05:00:00-07:00 same", "2026-10-05T12:00:01Z two"]
    pairs, note = P.aligned(left, right)
    assert pairs == [(0, 0), (None, 1), (1, None)]
    assert "UTC" in note


def test_alignment_retains_nanosecond_precision():
    left = ["2026-10-05T12:00:00.123456700Z left"]
    right = ["2026-10-05T12:00:00.123456799Z right"]
    assert P.aligned(left, right)[0] == [(0, None), (None, 0)]


@pytest.mark.parametrize("fraction", ["123456789"[:length] for length in range(1, 10)])
@pytest.mark.parametrize("second,zone,basis", [
    ("1970-01-01T00:00:00", "", "naive"),
    ("1970-01-01T00:00:00", "Z", "zoned"),
    ("1970-01-01T07:30:00", "+07:30", "zoned"),
    ("1970-01-01T07:30:00", "+0730", "zoned"),
    ("1969-12-31T20:30:00", "-0330", "zoned"),
])
def test_timestamp_preserves_each_fractional_digit_and_offset(second, zone, basis, fraction):
    assert P.timestamp(f"{second}.{fraction}{zone} log") == (int(fraction.ljust(9, "0")), basis)


@pytest.mark.parametrize("left,right,warning", [
    (["message"], ["2026-10-05T12:00:00Z x"], "missing"),
    (["2026-10-05T12:00:00 x"], ["2026-10-05T12:00:00Z x"], "mixed"),
    (["2026-10-05T12:00:02Z x", "2026-10-05T12:00:01Z x"], [], "backwards"),
])
def test_ambiguous_clock_has_explicit_positional_fallback(left, right, warning):
    pairs, note = P.aligned(left, right)
    assert warning.casefold() in note.casefold()
    assert pairs[0] == (0, 0 if right else None)


def test_repeated_timestamps_not_paired_arbitrarily():
    line = "2026-10-05T12:00:00Z hello"
    pairs, note = P.aligned([line, line], [line])
    assert pairs == [(0, None), (1, None), (None, 0)]
    assert "repeated" in note


def test_diff_reports_actual_additions_and_explicit_time_normalization():
    left = ["2026-10-05T12:00:00Z start", "old"]
    right = ["2026-10-05T12:00:01Z start", "new"]
    exact = P.diff_rows(left, right)
    normalized = P.diff_rows(left, right, True)
    assert len(exact) == 4 and len(normalized) == 3
    assert normalized[0]["left"] == normalized[0]["right"] == 0
    assert normalized[-2]["text"] == "- old" and normalized[-1]["text"] == "+ new"


def test_structured_filter_expansion_preserves_original_indexes():
    lines = ['{"severity":"error","worker":{"rank":2}}', '{"severity":"info"}', "plain text"]
    rows, omitted = P.structured_rows(lines, field="severity", query="ERROR")
    assert omitted == 2 and all(row["original"] == 0 for row in rows)
    assert any(row["node"] == "0:/worker/rank" for row in rows)
    collapsed, _ = P.structured_rows(lines, ["0:/worker"], field="severity", query="error")
    assert any(row["node"] == "0:/worker" and "{...}" in row["text"] for row in collapsed)
    assert not any(row["node"] == "0:/worker/rank" for row in collapsed)


@pytest.mark.parametrize("line", ['{"value":1e999}', '{"value":NaN}', '{"value":Infinity}', "[" * 40 + "0" + "]" * 40])
def test_invalid_or_unbounded_json_remains_original_text(line):
    rows, omitted = P.structured_rows([line])
    assert omitted == 0 and rows[0]["text"] == "L1 " + line
    assert not rows[0]["expandable"]


def test_noise_folds_and_expands_actual_contiguous_ranges():
    lines = [f"2026-10-05T12:00:0{i}Z heartbeat" for i in range(4)] + ["error"]
    folded = P.folded_rows(lines)
    assert len(folded) == 2 and folded[0]["hidden"] == 3
    assert folded[0]["original"] == 0 and folded[0]["last"] == 3
    expanded = P.folded_rows(lines, [folded[0]["node"]])
    assert len(expanded) == 5 and [item["original"] for item in expanded] == list(range(5))


def test_unread_tracks_append_partial_completion_once_and_original_jump(tmp_path):
    path = tmp_path / "log"
    path.write_bytes(b"first\n")
    result = app(path)
    try:
        buf = result.logs.buffer(str(path))
        W.observe_buffer(result, buf)
        result.logs.top = 0
        path.write_bytes(b"first\nnew")
        buf.refresh(); W.observe_buffer(result, buf)
        assert result.log_workbench_state["unread"] == 1
        path.write_bytes(b"first\nnew line\nsecond\n")
        buf.refresh(); W.observe_buffer(result, buf)
        assert result.log_workbench_state["unread"] == 2
        assert W.first_unread(result, buf)
        assert result.logs.cursor == 1
        assert "2 unread" in W.status_label(result)
        result.logs.top = None
        W.observe_buffer(result, buf)
        assert not W.status_label(result)
    finally:
        result.research.close()


def test_unread_rotation_resets_source_identity(tmp_path):
    path = tmp_path / "log"
    path.write_bytes(b"old\n")
    result = app(path)
    try:
        buf = result.logs.buffer(str(path)); W.observe_buffer(result, buf)
        result.logs.top = 0
        with path.open("ab") as stream: stream.write(b"new\n")
        buf.refresh(); W.observe_buffer(result, buf)
        assert result.log_workbench_state["unread"] == 1
        path.unlink(); path.write_bytes(b"other\n")
        buf.refresh(); W.observe_buffer(result, buf)
        assert result.log_workbench_state["unread"] == 0
    finally:
        result.research.close()


@pytest.mark.parametrize("view", ["diff", "fold"])
def test_new_presentation_preference_round_trips_without_source_paths(tmp_path, view):
    path = tmp_path / "log"; path.write_text("one\n")
    result = app(path)
    try:
        W.run_command(result, ["logview", view])
        saved = W.save(result)
        W.initialize(result); W.restore(result, saved)
        assert result.log_workbench_state["view"] == view
        assert result.log_workbench_state["diff_sources"] is None
        assert str(path) not in str(saved)
    finally:
        result.research.close()


@pytest.mark.parametrize("ascii_", [False, True])
def test_interactive_fold_and_json_keep_raw_copy_bytes(tmp_path, ascii_):
    path = tmp_path / "log"
    raw = b'{"nested":{"value":9}}\r\n' + b"heartbeat\n" * 4
    path.write_bytes(raw)
    result = app(path)
    try:
        buf = result.logs.buffer(str(path))
        result.logs.begin_selection(buf, 0); result.logs.move_cursor("end", buf)
        W.run_command(result, ["logfold", "on"])
        text(result, ascii_); finish(result)
        assert "3 lines hidden" in text(result, ascii_)
        W.handle_key(result, "down"); W.handle_key(result, "enter")
        assert "0 lines hidden" in text(result, ascii_)
        assert result.logs.selection_bytes(buf) == raw
        W.run_command(result, ["logjson", "nested.value", "9"])
        text(result, ascii_); finish(result)
        assert '"value": 9' in text(result, ascii_)
        W.handle_key(result, "enter")
        assert "{...}" in text(result, ascii_)
        assert result.logs.selection_bytes(buf) == raw
    finally:
        result.research.close()


def test_diff_uses_registered_files_and_rejects_unknown_paths(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    left.write_text("same\nleft only\n"); right.write_text("same\nright only\n")
    result = app(left)
    result.logs.entries.append(dict(id="right", label="stderr", role="stderr", path=str(right)))
    try:
        W.run_command(result, ["logdiff", "left", "right", "exact"])
        text(result); finish(result)
        content = text(result)
        assert "- left only" in content and "+ right only" in content
        assert str(left) in content and str(right) in content
        W.handle_key(result, "]")
        assert result.logs.entry["path"] == str(right)
        result.logs.path = str(right)
        result.log_workbench_state["cursor"] = 1
        W.handle_key(result, "o")
        assert result.logs.entry["path"] == str(left)
        assert result.log_workbench_state["citation"]["path"] == str(left)
        assert result.log_workbench_state["citation"]["excerpt_line"] == "left only"
        W.run_command(result, ["logdiff", str(left), "/private/secret"])
        assert "registered" in result.message
    finally:
        result.research.close()


def test_overlay_mouse_focus_never_clicks_through_to_hidden_raw_rows(tmp_path):
    path = tmp_path / "log"; path.write_text('{"nested":{"rank":2}}\n')
    result = app(path)
    try:
        buf = result.logs.buffer(str(path))
        W.run_command(result, ["logview", "json"])
        text(result); finish(result); text(result)
        assert W.handle_mouse(result, 0, 0)
        assert result.logs.cursor is None
        mapped = result.log_workbench_state["mouse_rows"]
        y = next(y for y, (_, _, index) in mapped.items() if index == 1)
        left, _, _ = mapped[y]
        W.handle_mouse(result, y, left, button="double")
        assert result.log_workbench_state["cursor"] == 1
        assert "0:/nested" in result.log_workbench_state["json_collapsed"]
        assert not result.logs.selection_active
        W.run_command(result, ["logmark", "visible"])
        assert "original source" in result.message
        W.run_command(result, ["copy", "selection"])
        assert "before selecting" in result.message
    finally:
        result.research.close()


def test_remapped_navigation_and_selection_leave_hidden_buffer_untouched(tmp_path):
    path = tmp_path / "log"; path.write_text("one\ntwo\nthree\n")
    result = app(path)
    try:
        result.keymap = {"j": "down", "g": "home", "z": "visual"}
        buf = result.logs.buffer(str(path))
        W.run_command(result, ["logfold", "on"])
        text(result); finish(result); text(result)
        W.handle_key(result, "j")
        assert result.log_workbench_state["cursor"] == 1 and buf.total == 3
        W.handle_key(result, "g")
        assert result.log_workbench_state["cursor"] == 0
        W.handle_key(result, "z")
        assert result.log_workbench_state["view"] == "plain"
        assert not result.logs.selection_active
    finally:
        result.research.close()


def test_cr_progress_updates_retain_exact_logical_source_indexes(tmp_path):
    path = tmp_path / "log"; path.write_bytes(b"step 1\rstep 2\nheartbeat\nheartbeat\nheartbeat\n")
    result = app(path)
    try:
        buf = result.logs.buffer(str(path))
        W.run_command(result, ["logfold", "on"])
        text(result); finish(result); text(result)
        assert result.log_workbench_state["rows"][0]["source_text"] == buf.lines[0]
        assert result.log_workbench_state["rows"][1]["original"] == 1
        W.handle_key(result, "o")
        W.apply_citation(result, buf)
        assert result.logs.cursor == 0
    finally:
        result.research.close()


def test_real_app_mouse_fold_then_original_selection_and_source_position(tmp_path):
    out, err = tmp_path / "out", tmp_path / "err"
    out.write_bytes(b"heartbeat\n" * 8 + b"last\n")
    err.write_bytes(b"failure\n")
    cfg = Config({"log_lines": 0, "clipboard": {"osc52": False, "tools": False}})
    store = Store(persist=False)
    store.finished = [Finished("77", "run", "FAILED", workdir=str(tmp_path))]
    store.details["77"] = dict(StdOut=str(out), StdErr=str(err), WorkDir=str(tmp_path))
    result = App(store, None, None, cfg, "test", interactive=False)
    result.files = result.logs.files = LocalFiles()
    result.research = ResearchHub(cfg, result.files)
    views = Views(L.Glyphs(False), cfg, files=result.files)
    result.views_ref = views
    try:
        result.open_log("77")
        _, result.last_hits = views.compose(store.snapshot(), result, 100, 24)
        result.logs.page = 3
        result.logs.top = 1
        result.logs.cursor = 2
        result.run_command("logfold on")
        views.overlay(store.snapshot(), result, 100, 24); finish(result)
        views.overlay(store.snapshot(), result, 100, 24)
        y, (left, _, index) = next(iter(result.log_workbench_state["mouse_rows"].items()))
        result.click(y, left, result.last_hits)
        assert result.log_workbench_state["cursor"] == index
        assert not result.logs.selection_active
        result.handle("o")
        views.compose(store.snapshot(), result, 100, 24)
        assert result.logs.cursor == 0
        result.handle("v"); result.handle("down")
        buf = result.logs.buffers[str(out)]
        assert result.logs.selection_bytes(buf) == b"heartbeat\nheartbeat\n"
        result.logs.clear_selection(); result.logs.top = 1; result.logs.cursor = 2
        result.run_command("logview split")
        views.overlay(store.snapshot(), result, 100, 24); finish(result)
        views.overlay(store.snapshot(), result, 100, 24)
        result.handle("]")
        views.compose(store.snapshot(), result, 100, 24)
        assert result.logs.path == str(err)
        result.handle("[")
        views.compose(store.snapshot(), result, 100, 24)
        assert result.logs.path == str(out) and result.logs.cursor == 2
        assert result.log_workbench_state["pan"] == 0
    finally:
        result.research.close()


@pytest.mark.parametrize("view", ["json", "fold", "diff", "split"])
@pytest.mark.parametrize("dimensions", [(1, 1), (8, 5), (80, 24), (160, 50)])
def test_all_presentations_bound_terminal_geometry(tmp_path, view, dimensions):
    path = tmp_path / "log"; path.write_text('{"value":"界"}\n')
    result = app(path)
    try:
        W.run_command(result, ["logview", view])
        width, height = dimensions
        text(result, True, width, height); finish(result)
        rows = W.overlay(SimpleNamespace(g=L.Glyphs(True)), {}, result, width, height)
        assert all(0 <= x < width and 0 <= y < height and x + L.vlen(L.row_text(row)) <= width for y, x, row in rows)
        assert all(L.row_text(row).isascii() for _, _, row in rows)
    finally:
        result.research.close()
