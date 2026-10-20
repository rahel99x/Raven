"""Rendered document selections are pane-local, bounded and source-aware."""
from types import SimpleNamespace
from random import Random
import time

import pytest

from raven import clipboard, interaction, layout as L, scrollbars as S, text_selection as T


def instance(**kw):
    data = dict(cfg={"clipboard": {}}, mode="main", tab="analytics", analytics_view="advisor",
                width=40, height=15, body_origin=2, selected_id=None, job_panel_state={},
                project_state={}, toolbar_state={}, keymap={}, sel_anchor=None, last_hits=[],
                messages=[], failures=[], state_dir=None, cursor={}, interaction_state={})
    data.update(kw)
    app = SimpleNamespace(**data)
    app.say = app.messages.append
    app.fail = app.failures.append
    from raven.workbench import bind_actions
    bind_actions(app)
    return app


def paint(app, lines, *, top=0, count=None, context="source", rect=(3, 0, 8, 30), key="advisor", setter=None):
    rows = [[("", "")] for _ in range(app.height)]
    for y, text in zip(range(rect[0], rect[2]), lines):
        rows[y] = [(text, "text")]
    S.begin_frame(app)
    S.register(app, key, rect, len(lines) if count is None else count, rect[2] - rect[0],
               top, top, setter or (lambda value: None), context=context)
    S.publish(app, app.width, app.height)
    T.publish(app, rows, app.width, app.height)
    return rows


def test_advisor_drag_copies_multiple_lines_without_job_or_source_action(monkeypatch):
    app = instance()
    paint(app, ["  memory peak 8 G", "  CPU efficiency 45%", "  recommended CPUs 4"], count=10)
    seen = []
    monkeypatch.setattr(clipboard, "copy", lambda text, *a, **kw: seen.append((text, kw)) or "copied")
    assert T.handle_mouse(app, 3, 5, button="press")
    assert T.active(app)
    assert T.handle_mouse(app, 5, 5, button="drag")
    assert T.handle_mouse(app, 5, 5, button="release")
    assert not T.active(app)
    assert T.copy_selection(app)
    assert seen == [("  memory peak 8 G\n  CPU efficiency 45%\n  recommended CPUs 4\n",
                     dict(use_osc52=True, use_tools=True, destination="copy"))]
    assert not T.selected(app)


def test_keyboard_v_arrow_selection_uses_cursor_row_without_mouse(monkeypatch):
    app = instance()
    app.cursor_row = lambda: 4
    paint(app, ["first", "second", "third", "fourth", "fifth"], count=10)
    assert T.handle_key(app, "v")
    assert T.handle_key(app, "down")
    assert T.handle_key(app, "down")
    assert T.selection_text(app) == "second\nthird\nfourth\n"


def test_plain_arrows_do_not_capture_job_navigation():
    app = instance(tab="jobs")
    paint(app, ["41 running", "42 waiting"], count=10)
    for key in ("up", "down", "pgup", "pgdn", "home", "end"):
        assert not T.handle_key(app, key)


@pytest.mark.parametrize("kind", sorted(T.PROTECTED_KINDS))
def test_job_and_original_log_row_drag_owners_have_priority_until_v(kind):
    app = instance(tab="jobs", last_hits=[(3, kind, "41")])
    paint(app, ["41 running", "42 waiting"], count=10)
    assert not T.handle_mouse(app, 3, 3, button="press")
    assert T.handle_key(app, "v")
    assert T.handle_mouse(app, 3, 3, button="press")
    assert T.active(app)


def test_original_logs_keep_their_exact_byte_visual_selection():
    app = instance(tab="log", logs=SimpleNamespace(browser=False))
    paint(app, ["rendered log"], count=10)
    assert not T.handle_key(app, "v")
    assert not T.handle_key(app, "V")
    assert not T.handle_mouse(app, 3, 1, button="press")


def test_explicit_pane_selection_does_not_cover_other_column():
    app = instance()
    paint(app, ["Main text  Details text", "Main two   Details two"], count=10, rect=(3, 11, 8, 30))
    app.interaction_state["pointer"] = (3, 12)
    assert T.handle_key(app, "v")
    assert T.handle_key(app, "down")
    assert T.selection_text(app) == "Details text\nDetails two\n"
    overlays = T.feedback(app)
    assert all(x == 11 and L.vlen(L.row_text(row)) == 18 for y, x, row in overlays)


def test_cross_page_keyboard_selection_scrolls_published_source_only():
    app = instance()
    offsets = []
    paint(app, [f"line {i}" for i in range(5)], count=20, setter=offsets.append)
    assert T.handle_key(app, "v")
    for _ in range(5):
        assert T.handle_key(app, "down")
    assert offsets == [1]
    assert T.initialize(app)["frame_required"]
    assert S.manual(app, "advisor", context="source") == 1
    # The unseen row is never fabricated or copied as a truncated range.
    assert T.selection_text(app) is None
    paint(app, [f"line {i}" for i in range(1, 6)], top=1, count=20, setter=offsets.append)
    assert T.selection_text(app) == "\n".join(f"line {i}" for i in range(6)) + "\n"


def test_source_updates_cannot_replace_already_selected_text():
    app = instance()
    paint(app, ["memory 8 G", "cpu 40%"], count=10)
    assert T.handle_mouse(app, 3, 2, button="press")
    assert T.handle_mouse(app, 4, 2, button="release")
    paint(app, ["memory 9 G", "cpu 50%"], count=10)
    assert T.selection_text(app) == "memory 8 G\ncpu 40%\n"


def test_idle_document_publication_does_not_extract_any_rendered_text(monkeypatch):
    app = instance()
    monkeypatch.setattr(T, '_slice', lambda *args, **kwargs:
                        pytest.fail('An idle publication sliced rendered text'))
    for index in range(20):
        paint(app, [f'fresh metric {index}', 'other pane'], count=100)
        assert not T.handle_mouse(app, 3, 2, button='motion')
        assert T.initialize(app)['panes']


def test_lazy_selection_extracts_only_selected_pane_once_and_pins_fresh_frames(monkeypatch):
    app = instance(tab='sources')
    calls, original = [], T._line

    def line(state, pane, y):
        calls.append((pane.key, y))
        return original(state, pane, y)

    monkeypatch.setattr(T, '_line', line)

    def publish(label):
        rows = [[('', '')] for _ in range(app.height)]
        for y in range(3, 8):
            rows[y] = [(f'left {y}'.ljust(20) + f'{label} {y}', 'text')]
        S.begin_frame(app)
        for key, rect in [('left', (3, 0, 8, 18)), ('right', (3, 20, 8, 40))]:
            S.register(app, key, rect, 10, 5, 0, 0, lambda value: None, context='same-source')
        S.publish(app, app.width, app.height)
        T.publish(app, rows, app.width, app.height)

    publish('old')
    assert not calls
    assert T.handle_mouse(app, 3, 22, button='press')
    assert len(calls) == 6 and {key for key, _ in calls} == {'right'}
    assert T.handle_mouse(app, 4, 22, button='release')
    assert len(calls) == 6, 'Extending a selection recopied the same painted pane'
    assert T.selection_text(app) == 'old 3\nold 4\n'
    calls.clear()
    publish('new')
    assert len(calls) == 5 and {key for key, _ in calls} == {'right'}
    assert T.selection_text(app) == 'old 3\nold 4\n'
    T.clear(app)
    calls.clear()
    publish('latest')
    assert not calls


def test_lazy_pane_copy_uses_latest_published_lines_without_selection(monkeypatch):
    app = instance()
    paint(app, ['outdated', 'outdated second'], count=10)
    paint(app, ['latest', 'latest second'], count=10)
    copied = []
    monkeypatch.setattr(clipboard, 'copy', lambda text, *args, **kwargs: copied.append(text) or 'copied')
    assert T.copy_visible_pane(app)
    assert copied == ['latest\nlatest second\n\n\n\n']


def test_published_visible_lines_can_be_requested_lazily_and_keep_their_frame():
    app = instance()
    paint(app, ['first frame', 'second line'], count=10)
    first = T.initialize(app)['visible']['advisor']
    assert len(first) == 5
    paint(app, ['new frame', 'new second'], count=10)
    assert dict(first) == {0: 'first frame', 1: 'second line', 2: '', 3: '', 4: ''}
    assert dict(T.initialize(app)['visible']['advisor'])[0] == 'new frame'


def test_source_switch_cancels_capture_and_consumes_late_release():
    app = instance()
    paint(app, ["first job"], context="job1", count=10)
    assert T.handle_mouse(app, 3, 2, button="press")
    paint(app, ["other job"], context="job2", count=10)
    assert not T.selected(app) and not T.active(app)
    assert T.handle_mouse(app, 3, 2, button="release")
    assert not T.handle_mouse(app, 3, 2, button="release")


@pytest.mark.parametrize("repaint", [False, True])
def test_cluster_copy_survives_background_project_discovery(monkeypatch, repaint):
    app = instance(tab="cluster", project_state={"root": ""})
    lines = ["GPU free 6", "GPU used 10"]
    paint(app, lines, key="workspace:cluster:details")
    assert T.handle_key(app, "v")
    assert T.handle_key(app, "down")
    # The automatic project reader can publish between selection and yank.
    # Cluster counters keep their source even when that unrelated task ends.
    app.project_state["root"] = "/lab/project"
    if repaint:
        paint(app, ["GPU free 5", "GPU used 11"], key="workspace:cluster:details")
    copied = []
    monkeypatch.setattr(clipboard, "copy", lambda text, *a, **kw: copied.append(text) or "copied")
    assert T.handle_key(app, "y")
    assert copied == ["GPU free 6\nGPU used 10\n"]
    assert not app.failures


@pytest.mark.parametrize("tab", ["jobs", "research", "analytics"])
def test_project_dependent_copy_still_rejects_changed_project(monkeypatch, tab):
    app = instance(tab=tab, project_state={"root": "/lab/first"})
    paint(app, ["First project metric"])
    assert T.handle_key(app, "v")
    app.project_state["root"] = "/lab/second"
    paint(app, ["Second project metric"])
    monkeypatch.setattr(clipboard, "copy", lambda *a, **kw: pytest.fail("Copied a changed source"))
    assert T.handle_key(app, "y")
    assert app.failures == ["The selected text source changed. Select the lines again; no copy was sent."]


def test_project_modal_on_cluster_still_rejects_changed_project(monkeypatch):
    app = instance(tab="cluster", mode="project_runs", project_state={"root": "/lab/first"})
    rows = [[("cluster counters", "text")] for _ in range(app.height)]
    overlays = [(3, 5, [("+------------+", "dim")]),
                (4, 5, [("| First run  |", "text")]),
                (5, 5, [("+------------+", "dim")])]
    T.publish(app, rows, app.width, app.height, overlays=overlays)
    assert T.handle_key(app, "v")
    app.project_state["root"] = "/lab/second"
    T.publish(app, rows, app.width, app.height, overlays=overlays)
    monkeypatch.setattr(clipboard, "copy", lambda *a, **kw: pytest.fail("Copied a changed project modal"))
    assert T.handle_key(app, "y")
    assert app.failures == ["The selected text source changed. Select the lines again; no copy was sent."]


def test_geometry_change_preserves_pinned_text_but_ends_pointer_capture():
    app = instance()
    paint(app, ["first"], count=10)
    assert T.handle_mouse(app, 3, 2, button="press")
    paint(app, ["first"], count=10, rect=(3, 0, 9, 30))
    assert T.selected(app) and not T.active(app)
    assert T.handle_mouse(app, 5, 2, button="release")


def test_right_click_clears_without_copy_or_click_action():
    app = instance()
    paint(app, ["first", "second"], count=10)
    assert T.handle_mouse(app, 3, 2, button="press")
    assert T.handle_mouse(app, 12, 35, button="right")
    assert not T.selected(app) and not T.active(app)
    assert not T.handle_key(app, "down")


def test_visible_control_is_not_intercepted_by_text_drag():
    app = instance()
    rows = paint(app, ["toggle metrics"], count=10)
    interaction.publish(app, rows, [], app.width, app.height,
                        extra_controls=[dict(id="toggle", label="Toggle", rect=(3, 0, 4, 20), action=("key", "x"))])
    assert not T.handle_mouse(app, 3, 3, button="press")


def test_overlay_selection_uses_modal_text_and_preserves_surrounding_source():
    app = instance(mode="help")
    rows = [[("underlying jobs", "text")] for _ in range(app.height)]
    overlays = [(3, 5, [("+------------+", "dim")]),
                (4, 5, [("| First help |", "text")]),
                (5, 5, [("| Next help  |", "text")]),
                (6, 5, [("+------------+", "dim")])]
    T.publish(app, rows, app.width, app.height, overlays=overlays)
    assert T.handle_mouse(app, 4, 8, button="press")
    assert T.handle_mouse(app, 5, 8, button="release")
    assert T.selection_text(app) == " First help\n Next help\n"


@pytest.mark.parametrize("mode", sorted(T.EDIT_MODES))
def test_edit_and_confirmation_modes_do_not_capture_text(mode):
    app = instance(mode=mode)
    paint(app, ["editor input"], count=10)
    assert not T.handle_mouse(app, 3, 2, button="press")
    assert not T.handle_key(app, "v")


def test_copy_refuses_missing_lines_and_does_not_send_partial_text(monkeypatch):
    app = instance()
    paint(app, ["first", "second"], count=100)
    assert T.handle_key(app, "v")
    assert T.handle_key(app, "end")
    monkeypatch.setattr(clipboard, "copy", lambda *a, **kw: pytest.fail("copied an unseen range"))
    assert T.copy_selection(app) and T.selected(app)
    assert "no partial copy" in app.failures[-1]


def test_wide_and_combining_characters_use_cell_boundaries():
    assert T._slice([("a界e\u0301z", "text")], 1, 5) == "界e\u0301z"
    assert T._slice([("a界e\u0301z", "text")], 2, 5) == " e\u0301z"
    assert T._slice([("a界e\u0301z", "text")], 0, 2) == "a"


@pytest.mark.parametrize("row,left,right,expected", [
    ([("ab", "text"), ("", "dim"), ("\u0301", "accent"), ("c", "text")], 0, 2, "ab\u0301"),
    ([("ab", "text"), ("\u0301", "accent"), ("c", "text")], 1, 2, "b\u0301"),
    ([("ab", "text"), ("\u0301", "accent"), ("c", "text")], 2, 3, "c"),
    ([("abc", "text"), ("\u0301", "accent")], 0, 2, "ab"),
    ([("ab", "text"), ("界", "accent"), ("cd", "text")], 3, 5, " c"),
    ([("ab", "text"), ("界", "accent"), ("cd", "text")], 0, 3, "ab"),
    ([("   ", "dim"), ("ab", "text"), ("  ", "dim")], 2, 8, " ab"),
    ([("", "text"), ("abc", "text")], 0, 0, ""),
])
def test_segment_slicing_preserves_combining_boundaries_and_partial_wide_cells(row, left, right, expected):
    assert T._slice(row, left, right) == expected


def test_optimized_slice_and_clip_match_scalar_reference_on_seeded_mixed_rows():
    # Keep the former scalar algorithms as an independent regression oracle
    # for segment fast paths, including empty segments and partial wide cells.
    def scalar_slice(row, left, right):
        result, column = [], 0
        for text, _ in row:
            text = L.display_text(text)
            for char in text:
                size = L.vlen(char)
                if size == 0:
                    if result and left < column <= right:
                        result.append(char)
                    continue
                if column >= right:
                    return "".join(result).rstrip()
                if column >= left and column + size <= right:
                    result.append(char)
                elif column < right and column + size > left:
                    result.extend(" " for _ in range(min(right, column + size) - max(left, column)))
                column += size
        return "".join(result).rstrip()

    def scalar_clip(row, width):
        result, used = [], 0
        for text, style in row:
            if used >= width:
                break
            # Clipping consumes display-safe segments: tabs expand before
            # fitting, and controls never enter a painted row. Keep the
            # scalar fitting oracle independent of the segment fast path.
            text = L.display_text(text)
            if L.vlen(text) > width - used:
                text = L.truncate(text, width - used)
            result.append((text, style))
            used += L.vlen(text)
        return result

    random = Random(483)
    alphabet = " a\t\x1b[31mZ界日é\u0301\u0302█⠿─╳⌛"
    for _ in range(512):
        row = [("".join(random.choices(alphabet, k=random.randrange(16))), "text")
               for _ in range(random.randrange(1, 8))]
        left = random.randrange(25)
        right = left + random.randrange(20)
        assert T._slice(row, left, right) == scalar_slice(row, left, right)
        for width in (-2, -1, 0, 1, 2, random.randrange(45)):
            assert L.clip_row(row, width) == scalar_clip(row, width)


def test_full_width_selected_text_remains_visible_without_marker_overwrite():
    app = instance()
    paint(app, ["X" * 29], count=10)
    assert T.handle_key(app, "v")
    assert L.row_text(T.feedback(app)[0][2]) == "X" * 29


def test_pointer_feedback_and_selection_updates_never_read_source(monkeypatch):
    app = instance()
    paint(app, ["first", "second"], count=10)
    monkeypatch.setattr("builtins.open", lambda *a, **kw: pytest.fail("pointer read a file"))
    assert T.handle_mouse(app, 3, 2, button="press")
    started = time.perf_counter()
    for i in range(1000):
        assert T.handle_mouse(app, 3 + i % 2, 2, button="drag")
        assert T.feedback(app)
    assert time.perf_counter() - started < 1.5


def test_nonoverflow_pane_is_selectable_without_rail_crop():
    app = instance()
    paint(app, ["one", "two"], count=2)
    assert any(p.key == "advisor" for p in T.initialize(app)["panes"])
    assert T.handle_mouse(app, 3, 1, button="press")
    assert T.handle_mouse(app, 4, 1, button="release")
    assert T.selection_text(app) == "one\ntwo\n"


def test_left_job_hit_does_not_suppress_details_advisor_selection():
    app = instance(tab="jobs", last_hits=[(3, "job", "41")],
                   job_panel_rect=interaction.Rect(3, 20, 8, 40),
                   workspace_main_rect=interaction.Rect(3, 0, 8, 19))
    paint(app, ["job left            Advisor memory 8 G"], count=10, rect=(3, 20, 8, 40),
          key="workspace:jobs:details")
    assert T.handle_mouse(app, 3, 24, button="press")
    assert "Advisor memory" in T.selection_text(app)


@pytest.mark.parametrize("action", [("row", 3, 2), ("click", 3, 2)])
@pytest.mark.parametrize("mode", ["main", "exports"])
def test_semantic_row_actions_keep_priority_even_without_old_hit_kind(action, mode):
    app = instance(mode=mode)
    rows = paint(app, ["open this source"], count=10)
    if mode != "main":
        T.publish(app, rows, app.width, app.height,
                  overlays=[(2, 0, [("+--------------------+", "dim")]),
                            (3, 0, [("|open this source    |", "text")]),
                            (4, 0, [("+--------------------+", "dim")])])
    interaction.publish(app, rows, [], app.width, app.height,
                        extra_controls=[dict(id="source-row", label="Source", rect=(3, 0, 4, 20),
                                             action=action, button=False)])
    assert not T.handle_mouse(app, 3, 2, button="press")
    assert T.handle_key(app, "v")
    assert T.handle_mouse(app, 3, 2, button="press")


def test_modal_does_not_select_main_document_outside_box():
    app = instance(mode="help")
    S.begin_frame(app)
    S.register(app, "jobs", (2, 0, 14, 40), 100, 12, 0, 0, lambda top: None, layer=0)
    S.publish(app, 40, 15)
    rows = [[("underlying job line", "text")] for _ in range(15)]
    overlays = [(4, 8, [("+----------------+", "dim")]),
                (5, 8, [("| Help text      |", "text")]),
                (6, 8, [("+----------------+", "dim")])]
    T.publish(app, rows, 40, 15, overlays=overlays)
    assert all(p.layer > 0 for p in T.initialize(app)["panes"])
    assert not T.handle_mouse(app, 10, 1, button="press")
    assert T.handle_mouse(app, 5, 10, button="press")
    assert "Help text" in T.selection_text(app)


def test_selected_feedback_renders_same_pinned_text_that_copy_delivers():
    app = instance()
    paint(app, ["memory 8 G", "cpu 40%"], count=10)
    assert T.handle_mouse(app, 3, 2, button="press")
    assert T.handle_mouse(app, 4, 2, button="release")
    paint(app, ["memory 9 G", "cpu 50%"], count=10)
    text = "\n".join(L.row_text(row) for y, x, row in T.feedback(app))
    assert "memory 8 G" in text and "cpu 40%" in text
    assert "memory 9 G" not in text and "cpu 50%" not in text


def test_default_keyboard_job_selection_prefers_main_not_one_row_recents():
    from raven.config import Config
    from raven.controller import App
    from raven.model import Job, Store
    from raven.screen import _FrameCache
    from raven.views import Views
    cfg = Config({"animations": False, "gpu_sampling": False})
    store = Store(persist=False)
    store.apply_jobs([Job("41", "one", "cpu", "RUNNING"), Job("42", "two", "cpu", "RUNNING")])
    app = App(store, None, None, cfg, "test")
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    cache = _FrameCache()
    cache.rebuild(app, views, store, None, 120, 30)
    app.handle("v")
    app.handle("down")
    text = T.selection_text(app)
    assert text is not None and text.count("\n") == 2
    assert "one" in text and "two" in text
    assert T.initialize(app)["selection"]["key"] in ("jobs", "workspace:jobs:main")


def test_copy_all_visible_pane_replaces_active_subset(monkeypatch):
    app = instance()
    paint(app, ["first", "second", "third", "fourth", "fifth"], count=10)
    assert T.handle_mouse(app, 4, 2, button="left")
    assert T.selection_text(app) == "second\n"
    captured = []
    monkeypatch.setattr(clipboard, "copy", lambda text, *a, **kw: captured.append(text) or "copied")
    assert T.copy_visible_pane(app)
    assert captured == ["first\nsecond\nthird\nfourth\nfifth\n"]


def test_inline_log_marker_is_in_original_last_column_and_leaves_rail_clear():
    app = instance()
    paint(app, ["log text"], count=10, rect=(3, 0, 8, 29), key="inline-log")
    assert T.handle_key(app, "v")
    output = T.feedback(app)
    assert any(x == 29 and L.row_text(row) == "▏" for y, x, row in output)
    assert all(x + L.vlen(L.row_text(row)) <= 28 or x == 29 for y, x, row in output)
