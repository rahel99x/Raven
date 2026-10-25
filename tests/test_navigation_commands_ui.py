"""Behavioral checks for restoring context, searching help, and reviewing actions."""
from __future__ import annotations

from types import SimpleNamespace
import shlex

import pytest

from raven import command_ui as commands
from raven import navigation_ui as navigation
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text, vlen
from raven.model import Finished, Job, Store
from raven.remote import RemoteFiles
from raven.views import Views


@pytest.fixture
def dashboard():
    cfg = Config({"log_lines": 0})
    store = Store(persist=False)
    store.apply_jobs([Job("1", "first run", "main", "RUNNING"), Job("2", "second run", "gpu", "PENDING")])
    store.apply_finished([Finished("99", "historical run", "FAILED")])
    app = App(store, None, None, cfg, "tester", ascii_=True)
    views = Views(Glyphs(True), cfg)
    app.views_ref = views
    views.compose(store.snapshot(), app, 100, 24, None)
    navigation.initialize(app)
    commands.initialize(app)
    return app, views, store


def overlay_text(module, app, views, store, width=100, height=24):
    return "\n".join(row_text(row) for _, _, row in module.overlay(views, store.snapshot(), app, width, height))


def palette(app, text):
    app.mode, app.palette_edit = "palette", text
    commands._sync_input(app)


def test_back_restores_exact_job_after_inventory_reorders(dashboard):
    app, views, store = dashboard
    app.cursor["jobs"] = app.visible_ids.index("2")
    app.sync_selection()
    app.filter, app.top["jobs"] = "second", 4
    navigation.record(app, "history")
    app.tab, app.filter = "history", "historical"
    store.apply_jobs([Job("0", "second new run", "main", "RUNNING"), store.job("2"), store.job("1")])
    assert navigation.back(app)
    assert app.tab == "jobs" and app.filter == "second" and app.selected_id == "2"
    assert (app.visible_ids + app.recent_ids)[app.cursor["jobs"]] == "2"


def test_back_log_buffer_preserves_source_cursor_and_search(dashboard, tmp_path):
    app, views, store = dashboard
    path = tmp_path / "one.log"
    path.write_text("line one\nline two\nline three\n")
    app.tab, app.log_job, app.log_record = "log", "1", store.job("1")
    buf = app.logs.buffer(str(path))
    app.logs.top, app.logs.cursor, app.logs.search = 0, 1, "two"
    app.logs.entry = {"path": str(path), "label": "worker one"}
    app.logs.begin_selection(buf)
    navigation.record(app, "research")
    saved_token = app.navigation_state["stack"][-1]["logs"]["_buffer_token"]
    assert saved_token[0] is buf._session_token
    app.tab = "research"
    app.logs.top, app.logs.cursor, app.logs.search = None, None, ""
    assert navigation.back(app)
    assert app.logs._buffer_token is saved_token
    app.logs.buffer(str(path))
    assert app.logs.top == 0 and app.logs.cursor == 1 and app.logs.search == "two"
    assert not app.logs.selection_active and not app.log_selection_expected


def test_back_preserves_cached_path_identity_and_isolates_browser_metadata(dashboard, tmp_path):
    app, _, store = dashboard
    first, second = tmp_path / "one.log", tmp_path / "two.log"
    first.write_text("one\ntwo\nthree\n")
    second.write_text("other file\n")
    app.tab, app.log_job = "log", "1"
    original = app.logs.buffer(str(first))
    app.logs.top, app.logs.cursor, app.logs.search = 0, 1, "two"
    app.logs.entry = {"path": str(first), "label": "source one"}
    app.logs.entries = [dict(app.logs.entry)]
    navigation.record(app, "research")
    saved = app.navigation_state["stack"][-1]
    app.tab = "research"
    app.logs.entries[0]["label"] = "later metadata"
    app.logs.buffer(str(second))
    navigation.back(app)
    latest = app.logs.buffer(str(first))
    assert latest is original and app.logs._buffer_token[0] is original._session_token
    assert app.logs.cursor == 1 and app.logs.top == 0 and app.logs.search == "two"
    assert app.logs.entries[0]["label"] == "source one"
    app.logs.entries[0]["label"] = "changed after restoration"
    assert saved["logs"]["entries"][0]["label"] == "source one"


def test_back_to_evicted_log_buffer_discards_stale_logical_cursor(dashboard, tmp_path):
    app, _, _ = dashboard
    path = tmp_path / "original.log"
    path.write_text("one\ntwo\nthree\n")
    app.tab, app.log_job = "log", "1"
    original = app.logs.buffer(str(path))
    app.logs.top, app.logs.cursor = 0, 2
    navigation.record(app, "research")
    app.tab = "research"
    for index in range(9):
        other = tmp_path / f"other-{index}.log"
        other.write_text("other\n")
        app.logs.buffer(str(other))
    assert str(path) not in app.logs.buffers
    navigation.back(app)
    latest = app.logs.buffer(str(path))
    assert latest._session_token is not original._session_token
    assert app.logs.cursor is None and app.logs.top is None


def test_back_to_rotated_log_invalidates_old_logical_cursor(dashboard, tmp_path):
    app, _, store = dashboard
    path = tmp_path / "one.log"
    path.write_text("one\ntwo\nthree\n")
    app.tab, app.log_job = "log", "1"
    app.logs.buffer(str(path))
    app.logs.top, app.logs.cursor = 0, 2
    navigation.record(app, "jobs")
    app.tab = "jobs"
    path.rename(tmp_path / "old.log")
    path.write_text("replacement\n")
    navigation.back(app)
    app.logs.buffer(str(path))
    assert app.logs.cursor is None and app.logs.top is None


def test_open_log_then_enter_tab_record_once(dashboard):
    app, _, _ = dashboard
    assert navigation.record(app, "log")
    app.log_job = "2"
    assert not navigation.record(app, "log")
    assert len(app.navigation_state["stack"]) == 1


def test_navigation_bound_and_session_local_restore(dashboard):
    app, _, _ = dashboard
    for index in range(100):
        app.research_scroll = index
        navigation.record(app, "research", force=True)
    assert len(app.navigation_state["stack"]) == navigation.MAX_BACK
    saved = navigation.save(app)
    assert "stack" not in saved
    fresh = SimpleNamespace()
    navigation.restore(fresh, {"stack": ["bad"], "query": "x" * 1000})
    assert fresh.navigation_state["stack"] == []
    assert len(fresh.navigation_state["query"]) == 256


def test_workspace_search_and_keyboard_activation(dashboard):
    app, views, store = dashboard
    navigation.run_command(app, ["workspaces", "failure"])
    text = overlay_text(navigation, app, views, store)
    assert "Evidence" in text and "Scaling" not in text
    navigation.handle_key(app, "enter")
    assert app.tab == "research" and app.research_view == "evidence" and app.mode == "main"


def test_workspace_picker_scrolls_to_last_workspace(dashboard):
    app, views, store = dashboard
    navigation.run_command(app, ["workspaces"])
    overlay_text(navigation, app, views, store, height=12)
    navigation.handle_key(app, "end")
    text = overlay_text(navigation, app, views, store, height=12)
    assert "Operations" in text
    navigation.handle_key(app, "enter")
    assert app.research_view == "operations"


def test_research_view_back_restores_prior_view(dashboard):
    app, _, _ = dashboard
    app.tab, app.research_view, app.research_scroll = "research", "passport", 7
    navigation.run_command(app, ["workspace", "Evidence"])
    navigation.run_command(app, ["workspace", "Workflow"])
    navigation.back(app)
    assert app.research_view == "evidence"
    navigation.back(app)
    assert app.research_view == "passport" and app.research_scroll == 7


def test_breadcrumb_names_exact_log_and_origin(dashboard):
    app, _, _ = dashboard
    app.tab = "history"
    navigation.record(app, "log")
    app.tab, app.log_job = "log", "99"
    app.logs.entry = {"label": "worker-2\x1b[31m", "path": "/tmp/a.log"}
    text = row_text(navigation.breadcrumb(app, 100, True))
    assert text.startswith(" History > Logs > job 99 > worker-2")
    assert "\x1b" not in text and ":back" in text
    assert vlen(row_text(navigation.breadcrumb(app, 12, True))) <= 12


def test_fuzzy_ranking_exact_prefix_and_subsequence():
    assert commands.fuzzy_score("can", "cancel") > commands.fuzzy_score("can", "scan")
    assert commands.fuzzy_score("cancel", "cancel") > commands.fuzzy_score("can", "cancel")
    assert commands.fuzzy_score("wfl", "workflow") is not None
    assert commands.fuzzy_score("xyz", "workflow") is None


def test_palette_middle_editing_and_delete(dashboard):
    app, _, _ = dashboard
    palette(app, "filter bt")
    commands.handle_key(app, "left")
    commands.handle_key(app, "a")
    assert app.palette_edit == "filter bat"
    commands.handle_key(app, "home")
    commands.handle_key(app, "delete")
    assert app.palette_edit == "ilter bat"
    commands.handle_key(app, "end")
    commands.handle_key(app, "backspace")
    assert app.palette_edit == "ilter ba"


def test_palette_completes_prefix_and_fuzzy_command(dashboard):
    app, _, _ = dashboard
    palette(app, "canc")
    assert commands.complete(app) and app.palette_edit == "cancel "
    palette(app, "wfl")
    assert commands.complete(app) and app.palette_edit == "workflow "


def test_palette_selects_fuzzy_result_then_executes(dashboard):
    app, _, _ = dashboard
    palette(app, "cpr")
    captured = []
    app.run_command = captured.append
    matches = commands.suggestions(app)
    target = next(index for index, row in enumerate(matches) if row["value"] == "compare")
    for _ in range(target):
        commands.handle_key(app, "down")
    commands.handle_key(app, "enter")
    assert captured == ["compare"] and app.mode == "main"


def test_palette_argument_completion_for_jobs_and_views(dashboard):
    app, _, _ = dashboard
    palette(app, "log 9")
    assert commands.complete(app) and app.palette_edit == "log 99 "
    palette(app, "tab hi")
    assert commands.complete(app) and app.palette_edit == "tab history "


def test_new_workspace_argument_completions_are_discoverable(dashboard):
    app, _, _ = dashboard
    palette(app, "density comf")
    assert commands.complete(app) and app.palette_edit == "density comfortable "
    palette(app, "orchestrate wor")
    assert commands.complete(app) and app.palette_edit == "orchestrate workflow "
    app.analysis_result = {"series": {"validation loss": []}}
    palette(app, "dashboard pin vali")
    assert commands.complete(app)
    assert commands.parse_command_line(app.palette_edit) == ["dashboard", "pin", "validation loss"]


def test_integrated_dispatcher_help_and_editable_palette(dashboard):
    app, views, store = dashboard
    app.run_command("commands canc")
    assert app.mode == "palette" and app.palette_edit == "canc"
    app.handle("tab")
    assert app.palette_edit == "cancel "
    app.handle("esc")
    app.run_command("help accounting")
    text = "\n".join(row_text(row) for _, _, row in views.overlay(store.snapshot(), app, 80, 24))
    assert "terminal state" in text
    app.handle("esc")
    assert app.mode == "main"


def test_integrated_back_key_restores_prior_identity(dashboard):
    app, _, _ = dashboard
    previous = app.selected_id
    app.enter_tab("history")
    assert app.tab == "history"
    app.handle("ctrl-b")
    assert app.tab == "jobs" and app.selected_id == previous


def test_integrated_control_p_opens_searchable_workspace_picker(dashboard):
    app, _, _ = dashboard
    app.handle("ctrl-p")
    assert app.mode == "workspace_picker"
    for char in "passport":
        app.handle(char)
    app.handle("enter")
    assert app.tab == "research" and app.research_view == "passport"


def test_back_restores_analysis_inspector_and_detail_sampling_identity(dashboard):
    app, _, _ = dashboard
    app.mode, app.detail_id = "analysis", "2"
    app.analysis_state.update(modal="inspect", job="2", section=3, scroll=8, modal_back=[{"modal": "timeline", "cursor": 7}])
    navigation.record(app, "log", force=True)
    app.mode, app.tab, app.detail_id = "main", "log", "1"
    app.analysis_state.update(modal="", job="1", section=0, scroll=0, modal_back=[])
    navigation.back(app)
    assert app.mode == "analysis" and app.detail_id == "2"
    assert app.analysis_state["job"] == "2" and app.analysis_state["section"] == 3 and app.analysis_state["scroll"] == 8
    assert app.analysis_state["modal_back"][0]["modal"] == "timeline"


def test_confirmation_mouse_buttons_work_and_background_clicks_are_blocked(dashboard):
    app, views, store = dashboard
    called = []
    app.finish_confirm = called.append
    app.mode, app.confirm = "confirm", {"action": "cancel", "jobs": [Job("1", "one", "main", "RUNNING")]}
    views.overlay(store.snapshot(), app, 100, 24)
    old_tab, old_selected = app.tab, app.selected_id
    app.click(23, 0, app.last_hits)
    assert called == [] and app.tab == old_tab and app.selected_id == old_selected and not app.quit
    y, start, end, _ = next(hit for hit in app.command_state["confirm_hits"] if hit[3] == "confirm")
    app.click(y, start + 1, app.last_hits)
    assert called == [True]


def test_palette_mouse_selects_and_workspace_mouse_opens_its_exact_destination(dashboard):
    app, views, store = dashboard
    palette(app, "")
    views.overlay(store.snapshot(), app, 100, 24)
    y, index = app.command_state["result_hits"][1]
    app.click(y, 20, app.last_hits)
    assert app.command_state["result_cursor"] == index and app.mode == "palette"
    app.mode = "main"
    app.run_command("workspaces")
    views.overlay(store.snapshot(), app, 100, 24)
    y, index = app.navigation_state["picker_hits"][1]
    app.click(y, 20, app.last_hits)
    expected = navigation._workspaces(app)[index][0]
    assert app.navigation_state["picker_cursor"] == index
    assert app.mode == "main" and app.tab == "research" and app.research_view == expected


def test_palette_completes_quoted_path_with_spaces(dashboard, tmp_path, monkeypatch):
    app, _, _ = dashboard
    monkeypatch.chdir(tmp_path)
    (tmp_path / "train model.sbatch").write_text("#!/bin/bash\n")
    palette(app, 'prepare "train m')
    assert commands.complete(app)
    assert commands.parse_command_line(app.palette_edit) == ["prepare", "train model.sbatch"]


def test_palette_local_home_completion_expands_without_shell(dashboard, tmp_path, monkeypatch):
    app, _, _ = dashboard
    monkeypatch.setattr("raven.command_ui.os.path.expanduser", lambda value: str(tmp_path / value[2:]) if value.startswith("~/") else value)
    (tmp_path / "metric sample.jsonl").write_text("{}\n")
    palette(app, "metrics ~/metric")
    assert commands.complete(app)
    assert commands.parse_command_line(app.palette_edit) == ["metrics", str(tmp_path / "metric sample.jsonl")]


def test_review_wrap_retains_exact_spaces_and_wide_characters():
    text = "sbatch 'jobs/训练  model.sbatch' --job-name='two  spaces'"
    wrapped = commands._wrap_exact(text, 18)
    assert "".join(wrapped) == text
    assert all(vlen(line) <= 18 for line in wrapped)
    plain = "sbatch 'two  spaces' --flag " + "x" * 500
    assert "".join(commands._wrap_exact(plain, 18)) == plain


@pytest.mark.parametrize("ascii_presentation", [True, False])
def test_palette_unicode_keyboard_input_survives_presentation_and_execution(dashboard, ascii_presentation):
    app, views, store = dashboard
    views.set_ascii(ascii_presentation)
    app.mode, app.palette_edit = "palette", ""
    captured = []
    app.run_command = captured.append
    line = 'metrics "项目目录/训练 日志🧪.jsonl"'
    for char in line:
        app.handle("space" if char == " " else char)
    assert app.palette_edit == line
    original = app.palette_edit
    text = overlay_text(commands, app, views, store)
    assert app.palette_edit == original
    if ascii_presentation:
        assert text.isascii()
    app.handle("enter")
    assert captured == [line]
    assert commands.parse_command_line(captured[0]) == ["metrics", "项目目录/训练 日志🧪.jsonl"]
    assert commands.save(app)["history"][-1] == line
    assert "\\u" not in commands.save(app)["history"][-1]


def test_palette_unicode_cursor_editing_preserves_codepoints(dashboard):
    app, _, _ = dashboard
    palette(app, "filter 界🧪")
    app.handle("left")
    app.handle("é")
    app.handle("\u0301")
    assert app.palette_edit == "filter 界é\u0301🧪"
    app.handle("backspace")
    app.handle("delete")
    assert app.palette_edit == "filter 界é"
    app.handle("home")
    app.handle("end")
    app.handle("界")
    assert app.palette_edit == "filter 界é界"


def test_unicode_path_completion_preserves_spaces_and_actual_filename(dashboard, tmp_path, monkeypatch):
    app, views, store = dashboard
    monkeypatch.chdir(tmp_path)
    name = "训练  模型🧪.sbatch"
    (tmp_path / name).write_text("#!/bin/bash\n")
    app.mode, app.palette_edit = "palette", ""
    for char in 'prepare "训练':
        app.handle("space" if char == " " else char)
    app.handle("tab")
    assert commands.parse_command_line(app.palette_edit) == ["prepare", name]
    assert name in app.palette_edit and "\\u" not in app.palette_edit
    before = app.palette_edit
    overlay_text(commands, app, views, store)
    assert app.palette_edit == before


def test_unicode_metric_argument_completion_keeps_literal_name(dashboard):
    app, _, _ = dashboard
    app.analysis_result = {"series": {"验证 损失": []}}
    app.mode, app.palette_edit = "palette", ""
    for char in 'dashboard pin "验证':
        app.handle("space" if char == " " else char)
    app.handle("tab")
    assert commands.parse_command_line(app.palette_edit) == ["dashboard", "pin", "验证 损失"]
    assert "验证 损失" in app.palette_edit


@pytest.mark.parametrize("ascii_presentation", [True, False])
def test_help_unicode_search_uses_literal_config_path(dashboard, tmp_path, ascii_presentation):
    app, views, store = dashboard
    views.set_ascii(ascii_presentation)
    app.config_path = str(tmp_path / "配置.toml")
    app.mode = "help"
    app.handle("/")
    app.handle("配")
    app.handle("置")
    assert app.command_state["help_query"] == "配置"
    text = overlay_text(commands, app, views, store)
    assert "Configuration" in text and "No matching instructions" not in text
    assert app.command_state["help_query"] == "配置"
    if ascii_presentation:
        assert text.isascii()
    else:
        assert "配置" in text
    app.handle("backspace")
    assert app.command_state["help_query"] == "配"
    app.handle("esc")
    assert app.mode == "main"


@pytest.mark.parametrize("ascii_presentation", [True, False])
def test_workspace_picker_unicode_query_remains_editable_and_saved(dashboard, ascii_presentation):
    app, views, store = dashboard
    views.set_ascii(ascii_presentation)
    app.handle("ctrl-p")
    for char in "资源🧪":
        app.handle(char)
    assert app.navigation_state["query"] == "资源🧪"
    assert navigation.save(app)["query"] == "资源🧪"
    text = overlay_text(navigation, app, views, store)
    assert "No workspace matches" in text
    assert app.navigation_state["query"] == "资源🧪"
    if ascii_presentation:
        assert text.isascii()
    for _ in range(3):
        app.handle("backspace")
    for char in "evidence":
        app.handle(char)
    app.handle("enter")
    assert app.research_view == "evidence" and app.mode == "main"


def test_controller_attaches_unicode_quoted_metric_path_without_escape_conversion(dashboard, tmp_path):
    app, _, _ = dashboard
    from raven.research import ResearchHub
    app.research = ResearchHub(app.cfg)
    app.files = app.logs.files = app.research.files
    path = tmp_path / "项目 目录" / "训练 日志🧪.jsonl"
    path.parent.mkdir()
    path.write_text('{"schema":"raven.metric/v1","t":1,"values":{"loss":0.2}}\n')
    try:
        app.run_command("metrics " + shlex.quote(str(path)))
        assert app.command_ok
        assert app.research.settings["metrics_file"] == str(path)
        assert "项目 目录" in app.research.settings["metrics_file"]
        assert "\\u" not in app.research.settings["metrics_file"]
    finally:
        if app.research is not None:
            app.research.close()


def test_palette_completion_replaces_argument_at_middle_cursor(dashboard):
    app, _, _ = dashboard
    palette(app, "tab hi trailing")
    for _ in range(len(" trailing")):
        commands.handle_key(app, "left")
    assert commands.complete(app)
    assert app.palette_edit == "tab history trailing"


def test_palette_enter_with_cursor_inside_command_preserves_arguments(dashboard):
    app, _, _ = dashboard
    captured = []
    app.run_command = captured.append
    palette(app, "cancel 2")
    commands.handle_key(app, "home")
    commands.handle_key(app, "right")
    commands.handle_key(app, "right")
    commands.handle_key(app, "enter")
    assert captured == ["cancel 2"]


def test_palette_fuzzy_replacement_preserves_arguments_at_command_cursor(dashboard):
    app, _, _ = dashboard
    captured = []
    app.run_command = captured.append
    palette(app, "canc 2")
    commands.handle_key(app, "home")
    for _ in range(4):
        commands.handle_key(app, "right")
    commands.handle_key(app, "enter")
    assert captured == ["cancel 2"]


def test_remote_completion_uses_known_paths_and_never_local_listing(dashboard, monkeypatch):
    app, _, _ = dashboard
    app.files = object.__new__(RemoteFiles)
    app.logs.entries = [{"path": "/remote/my job.log", "label": "known worker"}]
    monkeypatch.setattr("raven.command_ui.os.scandir", lambda path: pytest.fail("remote completion attempted local listing"))
    palette(app, "metrics /remote/my")
    assert commands.complete(app)
    assert commands.parse_command_line(app.palette_edit) == ["metrics", "/remote/my job.log"]


def test_palette_history_deduplicates_and_retrieves_draft(dashboard):
    app, _, _ = dashboard
    for line in ("tab history", "tab jobs", "tab history"):
        commands._record_history(app, line)
    assert app.command_state["history"] == ["tab jobs", "tab history"]
    palette(app, "find unfinished")
    commands.handle_key(app, "pgup")
    assert app.palette_edit == "tab history"
    commands.handle_key(app, "pgup")
    assert app.palette_edit == "tab jobs"
    commands.handle_key(app, "pgdn")
    commands.handle_key(app, "pgdn")
    assert app.palette_edit == "find unfinished"


def test_palette_history_is_bounded_and_restore_rejects_controls(dashboard):
    app, _, _ = dashboard
    for index in range(100):
        commands._record_history(app, f"log {index}")
    assert len(commands.save(app)["history"]) == commands.MAX_HISTORY
    fresh = SimpleNamespace()
    commands.restore(fresh, {"history": ["tab jobs", None, "unsafe\x1b", "x" * 5000]})
    assert fresh.command_state["history"][0] == "tab jobs"
    assert len(fresh.command_state["history"]) == 2
    assert len(fresh.command_state["history"][1]) == commands.MAX_INPUT


def test_quoted_command_parser_preserves_paths_and_expression_semantics():
    assert commands.parse_command_line('prepare "jobs/train model.sbatch" --workdir "project folder"') == ["prepare", "jobs/train model.sbatch", "--workdir", "project folder"]
    assert commands.parse_command_line("eval 'two  spaces'.count(' ')") == ["eval", "'two", "spaces'.count('", "')"]
    assert commands.parse_command_line(r"find \bfailed\b") == ["find", r"\bfailed\b"]
    with pytest.raises(ValueError):
        commands.parse_command_line('prepare "unterminated')


def test_help_bottom_instructions_reachable_and_contextual(dashboard):
    app, views, store = dashboard
    app.mode, app.scroll = "help", 0
    initial = overlay_text(commands, app, views, store, height=14)
    assert "Inspect the selected job" in initial
    assert "Configuration" not in initial
    commands.handle_key(app, "end")
    bottom = overlay_text(commands, app, views, store, height=14)
    assert "Configuration" in bottom and app.scroll > 0
    commands.handle_key(app, "home")
    assert app.scroll == 0


def test_help_search_wrapped_results_and_keyboard_edit(dashboard):
    app, views, store = dashboard
    app.tab, app.mode = "log", "help"
    commands.handle_key(app, "/")
    for char in "complete":
        commands.handle_key(app, char)
    text = overlay_text(commands, app, views, store, width=48, height=30)
    assert "complete file" in text and "original log lines" not in text
    commands.handle_key(app, "backspace")
    assert app.command_state["help_query"] == "complet"
    commands.handle_key(app, "enter")
    assert app.mode == "help" and not app.command_state["help_searching"]
    commands.handle_key(app, "esc")
    assert app.mode == "main"


def test_help_query_command_and_no_results(dashboard):
    app, views, store = dashboard
    assert commands.run_command(app, ["help", "no-such-help"])
    text = overlay_text(commands, app, views, store)
    assert "No matching instructions" in text


def test_confirmation_all_targets_reachable_without_dismissal(dashboard):
    app, views, store = dashboard
    app.mode, app.confirm = "confirm", {"action": "cancel", "jobs": [Job(str(index), f"run-{index}", "gpu", "RUNNING") for index in range(40)]}
    overlay_text(commands, app, views, store, height=14)
    commands.handle_key(app, "end")
    text = overlay_text(commands, app, views, store, height=14)
    assert "run-39" in text and "Cancel 40 jobs?" in text and app.mode == "confirm"
    commands.handle_key(app, "z")
    assert app.mode == "confirm"


def test_confirmation_default_cancel_and_explicit_button_activation(dashboard):
    app, _, _ = dashboard
    called = []
    app.finish_confirm = called.append
    app.mode, app.confirm = "confirm", {"action": "cancel", "jobs": [Job("1", "one", "gpu", "RUNNING")]}
    commands.handle_key(app, "enter")
    assert called == [False]
    app.confirm = {"action": "cancel", "jobs": []}
    commands.handle_key(app, "tab")
    commands.handle_key(app, "enter")
    assert called == [False, True]


@pytest.mark.parametrize("size", [(80, 4), (80, 5), (80, 6), (20, 8), (1, 1)])
def test_confirmation_unrendered_controls_never_activate(dashboard, size):
    app, views, store = dashboard
    called = []
    app.finish_confirm = called.append
    app.mode, app.confirm = "confirm", {"action": "cancel", "jobs": [Job("1", "one", "main", "RUNNING")]}
    overlay_text(commands, app, views, store, *size)
    assert not app.command_state["confirm_controls_visible"]
    for key in ("tab", "enter", "y"):
        app.handle(key)
    assert called == [] and app.mode == "confirm" and "Enlarge the terminal" in app.message
    app.handle("esc")
    assert called == [False]


@pytest.mark.parametrize("height", [7, 8, 9, 12])
def test_confirmation_visible_controls_stay_sticky_and_resize_recovers(dashboard, height):
    app, views, store = dashboard
    called = []
    app.finish_confirm = called.append
    app.mode, app.confirm = "confirm", {"action": "cancel", "jobs": [Job(str(index), f"one-{index}", "main", "RUNNING") for index in range(20)]}
    overlay_text(commands, app, views, store, width=80, height=5)
    app.handle("tab")
    assert app.command_state["confirm_focus"] == "cancel"
    text = overlay_text(commands, app, views, store, width=80, height=height)
    assert "[ Cancel ]" in text and "[ Confirm ]" in text and app.command_state["confirm_controls_visible"]
    app.handle("end")
    text = overlay_text(commands, app, views, store, width=80, height=height)
    assert "one-19" in text and "[ Confirm ]" in text
    app.handle("tab")
    app.handle("enter")
    assert called == [True]


@pytest.mark.parametrize("key,expected", [("y", True), ("Y", True), ("n", False), ("esc", False)])
def test_confirmation_existing_explicit_shortcuts(dashboard, key, expected):
    app, _, _ = dashboard
    called = []
    app.finish_confirm = called.append
    app.mode, app.confirm = "confirm", {"action": "cancel", "jobs": []}
    assert commands.handle_key(app, key)
    assert called == [expected]


def test_submission_review_scrolls_full_command_and_issues(dashboard):
    app, views, store = dashboard
    app.mode, app.confirm = "confirm", {"action": "submit", "plan": {"workdir": "/project", "command": "sbatch " + "x" * 3000 + " final-script.sbatch", "issues": [{"message": "review warning"}]}}
    overlay_text(commands, app, views, store, width=70, height=16)
    commands.handle_key(app, "end")
    text = overlay_text(commands, app, views, store, width=70, height=16)
    assert "final-script.sbatch" in text and "review warning" in text


@pytest.mark.parametrize("mode,module", [("palette", commands), ("help", commands), ("confirm", commands), ("workspace_picker", navigation)])
@pytest.mark.parametrize("size", [(1, 1), (3, 2), (20, 8), (40, 12), (100, 24)])
def test_all_overlays_fit_tiny_ascii_terminals(dashboard, mode, module, size):
    app, views, store = dashboard
    width, height = size
    app.mode, app.confirm = mode, {"action": "cancel", "jobs": [Job("1", "one", "main", "RUNNING")]}
    for y, x, row in module.overlay(views, store.snapshot(), app, width, height):
        assert 0 <= y < height and 0 <= x < width
        assert vlen(row_text(row)) <= width - x
        assert row_text(row).isascii()
