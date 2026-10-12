"""Right-click clears persistent selections without losing visible data sources."""
import curses
from types import SimpleNamespace

import pytest

from raven import history_browser as H, interaction as I, job_selection as S
from raven import log_workbench as W, log_tools as T, log_scan, screen
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text
from raven.model import Finished, Job, Store
from raven.remote import LocalFiles
from raven.views import Views


@pytest.fixture
def dashboard(tmp_path):
    path = tmp_path / "stdout-7.log"
    path.write_text("".join(f"line {i:03d}: payload\n" for i in range(80)))
    cfg = Config({"animations": False, "startup_animation": False, "log_lines": 0,
                  "workspace": {"density": "compact", "split": 50}})
    store = Store(persist=False, state_dir=str(tmp_path / "state"))
    store.jobs = [Job(str(i), f"trial-{i}", "cpu", "RUNNING", cpus=4,
                      user="tester", dependency=f"afterok:{i - 1}" if i > 7 else "") for i in range(7, 11)]
    store.group = list(store.jobs)
    store.finished = [Finished(str(i), f"past-{i}", "FAILED", workdir=str(tmp_path))
                      for i in range(201, 207)]
    store.details["7"] = {"StdOut": str(path), "StdErr": str(path), "WorkDir": str(tmp_path)}
    app = App(store, None, None, cfg, "test", interactive=False)
    files = LocalFiles()
    app.files = app.logs.files = files
    views = Views(Glyphs(False), cfg, files=files)
    app.views_ref = views
    app.table_state["groups"] = False
    d = SimpleNamespace(app=app, store=store, views=views, path=path, hits=[], rows=[])
    draw(d)
    yield d
    if app.logs.catalog:
        app.logs.catalog.close()
    if app.research:
        app.research.close()


def draw(d, tab=None):
    if tab:
        d.app.tab = tab
    d.rows, d.hits = d.views.compose(d.store.snapshot(), d.app, 160, 42)
    d.app.last_rows, d.app.last_hits = d.rows, d.hits
    return d.rows


def right(d, y=0, x=1, flag=None):
    screen._apply_input(d.app, ("mouse", (0, x, y, 0, flag or curses.BUTTON3_CLICKED)), d.hits, curses)


def open_log(d):
    d.app.open_log("7")
    d.app.logs.browser = False
    draw(d, "log")
    assert d.app.logs.path == str(d.path)


def forbidden(*args, **kwargs):
    pytest.fail("Selection feedback performed source IO or copied a scheduler snapshot")


@pytest.mark.parametrize("tab", S.JOB_SCOPES)
@pytest.mark.parametrize("flag", [curses.BUTTON3_CLICKED, curses.BUTTON3_PRESSED])
def test_right_click_globally_clears_jobs_and_both_line_ranges_without_io(dashboard, monkeypatch, tab, flag):
    d, app = dashboard, dashboard.app
    open_log(d)
    app.logs.begin_selection(app.logs.buffer(app.logs.path), 3)
    app.logs.selection_end = 5
    app.analytics_job, app.research_job_id = "8", "9"
    app.selected_id, app.marks = "7", {"7", "8"}
    app.sel_anchor, app.sel_end, app.click_row = 4, 6, 5
    app.log_selection_expected = True
    app.job_selection_state["capture"] = {"obsolete": True}
    app.interaction_state.update(active=True, focused="job:7")
    app.tab = tab
    source = app.log_job, app.analytics_job, app.research_job_id, app.logs.path
    monkeypatch.setattr(d.store, "snapshot", forbidden)
    monkeypatch.setattr(app.logs, "buffer", forbidden)
    monkeypatch.setattr(app.files, "read", forbidden)
    right(d, flag=flag)
    assert app.mode == "main" and not app.quit
    assert not app.marks and app.selected_id is None
    assert not app.logs.selection_active and app.logs.cursor is None
    assert app.sel_anchor is None and app.click_row is None and not app.log_selection_expected
    assert app.job_selection_state["capture"] is None and not app.interaction_state["active"]
    assert (app.log_job, app.analytics_job, app.research_job_id, app.logs.path) == source
    assert all(S.cleared(app, scope) for scope in S.JOB_SCOPES)
    assert S.lines_cleared(app) and app.target_ids() == []


def test_refresh_and_tab_switch_do_not_reselect_sources_or_sidebar_jobs(dashboard):
    d, app = dashboard, dashboard.app
    open_log(d)
    app.analytics_job, app.research_job_id = "8", "9"
    right(d)
    for tab in ("jobs", "history", "group", "deps", "analytics", "research", "log"):
        app.enter_tab(tab)
        app.sync_selection()
        draw(d)
        app.sync_selection()
        assert app.selected_id is None and app.target_ids() == []
        assert S.cleared(app)
        if tab in H.TABS:
            assert H._selected(app, H._view(app)) is None
    assert (app.analytics_job, app.research_job_id, app.log_job) == ("8", "9", "7")
    assert app.logs.path == str(d.path)


@pytest.mark.parametrize("visual", ["v", "V"])
def test_log_visual_selection_right_clears_and_arrow_only_restores_line_cursor(dashboard, visual):
    d, app = dashboard, dashboard.app
    open_log(d)
    app.handle("home")
    app.handle("down")
    app.handle(visual)
    assert app.logs.selection_active
    right(d)
    draw(d)
    assert not app.logs.selection_active and app.logs.cursor is None
    assert not any(row_text(d.rows[y]).endswith("›") for y, kind, _ in d.hits if kind == "log_line")
    app.handle("home")
    app.handle("down")
    assert app.logs.cursor == 1 and not S.lines_cleared(app)
    assert S.cleared(app, "log") and app.selected_id is None and app.target_ids() == []
    assert H._selected(app, H._view(app)) is None


def test_log_mouse_selection_and_shift_extension_restore_lines_but_not_job(dashboard):
    d, app = dashboard, dashboard.app
    open_log(d)
    app.handle("home")
    draw(d)
    right(d)
    draw(d)  # Publish the deselected document before the next row gesture.
    first = next(y for y, kind, value in d.hits if kind == "log_line" and value == "2")
    last = next(y for y, kind, value in d.hits if kind == "log_line" and value == "5")
    app.click(first, app.history_browser_content_rect.x + 10, d.hits)
    app.click(last, app.history_browser_content_rect.x + 10, d.hits, shift=True)
    assert (app.logs.selection_anchor, app.logs.selection_end) == (2, 5)
    assert not S.lines_cleared(app) and S.cleared(app, "log")
    assert app.selected_id is None and app.target_ids() == []


@pytest.mark.parametrize("tab", H.TABS)
def test_explicit_sidebar_job_activation_restores_only_its_scope(dashboard, tab):
    d, app = dashboard, dashboard.app
    open_log(d)
    draw(d, tab)
    right(d)
    assert H.activate(app, "201")
    assert not S.cleared(app, tab)
    assert all(S.cleared(app, scope) for scope in S.JOB_SCOPES if scope != tab)
    assert app.selected_id == "201" and app.target_ids() == ["201"]
    assert H._selected(app, H._view(app)) == "201"


@pytest.mark.parametrize("tab", ["jobs", "history", "group", "deps"])
def test_job_cursor_arrow_explicitly_restores_only_the_current_scope(dashboard, tab):
    d, app = dashboard, dashboard.app
    draw(d, tab)
    right(d)
    app.handle("down")
    assert not S.cleared(app, tab)
    assert all(S.cleared(app, scope) for scope in S.JOB_SCOPES if scope != tab)
    assert S.lines_cleared(app)
    assert app.selected_id and app.target_ids() == [app.selected_id]


def test_details_content_arrow_does_not_reselect_cleared_main_job(dashboard):
    d, app = dashboard, dashboard.app
    app.run_command("jobpanel analytics advisor")
    draw(d)
    right(d)
    # Scrolling a data pane is not a new selection of its old job row.
    app.layout_state.focus, app.job_panel_state["focus"] = "details", "content"
    app.handle("down")
    assert S.cleared(app, "jobs") and app.selected_id is None


def test_analytics_and_research_scroll_only_reselect_when_changing_job(dashboard):
    d, app = dashboard, dashboard.app
    app.analytics_job, app.research_job_id = "7", "7"
    draw(d, "analytics")
    right(d)
    app.analytics_view = "history"
    app.handle("down")
    assert S.cleared(app, "analytics")
    app.analytics_view = "job"
    app.handle("down")
    assert app.analytics_job == "8" and app.selected_id == "8"
    assert not S.cleared(app, "analytics")
    app.tab, app.research_view = "research", "site"
    app.handle("down")
    assert S.cleared(app, "research") and app.selected_id is None
    app.research_view = "experiment"
    app.handle("down")
    assert app.research_job_id == "8" and not S.cleared(app, "research")


def test_graph_right_reset_retains_selection_and_has_priority_on_any_page(dashboard, monkeypatch):
    d, app = dashboard, dashboard.app
    app.marks, app.selected_id = {"7", "8"}, "7"
    calls = []
    monkeypatch.setattr("raven.chart_interaction.handle_mouse", lambda *args, **kwargs: calls.append(kwargs) or True)
    app.tab = "research"
    right(d)
    assert app.marks == {"7", "8"} and app.selected_id == "7"
    assert not S.cleared(app) and calls == [{"button": "right"}]


def test_history_list_right_opens_export_context_and_outside_clears(dashboard):
    d, app = dashboard, dashboard.app
    draw(d, "history")
    app.marks = {"201", "202"}
    rect = app.history_jobs_rect
    right(d, rect.top + 1, rect.left + 2)
    assert app.mode == "history_log_menu" and app.marks == {"201", "202"}
    assert app.history_log_export_state["jobs"] == ("201", "202")
    app.handle("esc")
    right(d)
    assert app.mode == "main" and not app.marks and S.cleared(app, "history")


@pytest.mark.parametrize("mode", ["help", "details", "confirm", "analysis", "history_log_menu"])
def test_dialog_right_retains_its_input_owner_without_global_clear(dashboard, mode):
    d, app = dashboard, dashboard.app
    app.mode, app.marks, app.selected_id = mode, {"7", "8"}, "7"
    right(d)
    assert app.mode == mode and app.marks == {"7", "8"} and app.selected_id == "7"
    assert not S.cleared(app)


def test_file_catalog_arrows_restore_file_cursor_without_sidebar_job(dashboard):
    d, app = dashboard, dashboard.app
    open_log(d)
    app.logs.entries = [{"id": "stdout", "path": str(d.path), "label": "stdout", "group": "Scheduler"}]
    app.logs.browser = True
    draw(d)
    right(d)
    rendered = W.render_browser(d.views, d.store.snapshot(), app, 100, 20, [], [])[0]
    assert not any(style == "sel" for row in rendered for _, style in row)
    app.handle("down")
    assert not S.lines_cleared(app) and S.cleared(app, "log")
    assert app.selected_id is None and app.target_ids() == []


def test_universal_clear_discards_delayed_citation_and_capture(dashboard):
    d, app = dashboard, dashboard.app
    open_log(d)
    app.log_workbench_state["citation"] = {"path": str(d.path), "line": 5}
    app.history_browser_state.update(drag={"obsolete": True}, focused=True)
    H._view(app).update(selected="201", explicit=True)
    app.toolbar_state.update(dragging=True, pressed=True)
    right(d)
    assert app.log_workbench_state["citation"] is None
    assert app.history_browser_state["drag"] is None and not app.history_browser_state["focused"]
    assert not H._view(app)["explicit"] and H._view(app)["selected"] is None
    assert not app.toolbar_state["dragging"] and not app.toolbar_state["pressed"]
    screen._apply_input(app, ("mouse", (0, 3, 10, 0, curses.BUTTON1_RELEASED)), d.hits, curses)
    assert not app.marks and app.selected_id is None and app.logs.cursor is None


@pytest.mark.parametrize("view", ["fold", "json", "split", "diff"])
def test_alternate_log_panels_clear_cursor_and_restore_only_lines(dashboard, monkeypatch, view):
    d, app = dashboard, dashboard.app
    open_log(d)
    data = {"sources": [{"label": "stdout", "path": str(d.path),
                          "lines": ['{"key":1}', '{"key":2}', '{"key":3}'], "size": 30}]}
    if view in ("split", "diff"):
        data["sources"].append({"label": "stderr", "path": str(d.path) + ".err",
                                "lines": ['{"key":4}', '{"key":5}', '{"key":6}'], "size": 30})
    monkeypatch.setattr(W, "_alternate", lambda _: data)
    state = app.log_workbench_state
    state.update(view=view, cursor=1, scroll=0)
    d.views.overlay(d.store.snapshot(), app, 160, 42)
    right(d)
    draw(d)
    rendered = d.views.overlay(d.store.snapshot(), app, 160, 42)
    assert not any("rev" in style for _, _, row in rendered for _, style in row)
    y, (left, _, index) = next(iter(state["mouse_rows"].items()))
    app.click(y, left, d.hits)
    assert state["cursor"] == index and not S.lines_cleared(app)
    assert S.cleared(app, "log") and app.selected_id is None
    rendered = W.overlay(d.views, d.store.snapshot(), app, 160, 42)
    assert any("rev" in style for _, _, row in rendered for _, style in row)


def test_saved_reading_position_never_restores_a_cleared_line_cursor(dashboard):
    d, app = dashboard, dashboard.app
    open_log(d)
    buf = app.logs.buffer(app.logs.path)
    state = T.initialize(app)
    key = T._key(app, buf.path)
    state["positions"][key] = {"identity": (buf.ident, buf.reloads, buf.skipped_bytes),
                               "top": 3, "cursor": 5, "follow": False}
    right(d)
    state["active_key"], state["restored"] = None, None
    T.observe_buffer(app, buf)
    assert app.logs.top == 3 and app.logs.cursor is None
    assert S.lines_cleared(app)


def source_page(d):
    state = T.initialize(d.app)
    page = log_scan.read_page(d.app.files, str(d.path), 0)
    state.update(page=page, page_source={"path": str(d.path), "label": "stdout"},
                 page_files=d.app.files, page_cursor=1, selection=(1, 3), page_return="main")
    d.app.mode = "log_tools_page"
    d.views.overlay(d.store.snapshot(), d.app, 160, 42)
    return state


@pytest.mark.parametrize("position", ["line", "outside"])
def test_full_source_modal_right_clear_keeps_dialog_sources_and_jobs(dashboard, monkeypatch, position):
    d, app = dashboard, dashboard.app
    open_log(d)
    state = source_page(d)
    app.logs.begin_selection(app.logs.buffer(app.logs.path), 2)
    app.marks, app.selected_id = {"7", "8"}, "7"
    point = next((y, hit[1]) for y, hit in state["mouse_rows"].items()) if position == "line" else (0, 1)
    monkeypatch.setattr(d.store, "snapshot", forbidden)
    monkeypatch.setattr(app.files, "read", forbidden)
    right(d, *point)
    assert app.mode == "log_tools_page" and state["selection"] is None
    assert state["cursor_deselected"] and app.marks == {"7", "8"} and app.selected_id == "7"
    assert app.logs.cursor is None and not app.logs.selection_active
    assert not S.cleared(app, "log")
    rendered = T.overlay(d.views, {}, app, 160, 42)
    assert not any(style == "sel" for _, _, row in rendered for _, style in row)
    app.handle("y")
    assert "Select a source line" in app.message
    app.handle("down")
    assert not state["cursor_deselected"] and state["page_cursor"] == 2
    app.handle("v")
    assert state["selection"] == (2, 2)


def test_full_source_modal_shift_click_can_extend_after_own_clear(dashboard):
    d, app = dashboard, dashboard.app
    open_log(d)
    state = source_page(d)
    right(d)
    d.views.overlay(d.store.snapshot(), app, 160, 42)
    entries = list(state["mouse_rows"].items())
    y, first = entries[2]
    app.click(y, first[1], d.hits)
    y, last = entries[4]
    app.click(y, last[1], d.hits, shift=True)
    assert state["selection"] == (first[0], last[0])
    assert not state["cursor_deselected"] and app.mode == "log_tools_page"


@pytest.mark.parametrize("mode", ["log_tools_results", "log_tools_marks"])
def test_cleared_result_or_mark_cannot_open_or_delete_an_invisible_row(dashboard, monkeypatch, mode):
    d, app = dashboard, dashboard.app
    open_log(d)
    state = T.initialize(app)
    item = {"source": {"path": str(d.path)}, "offset": 0, "line": 1,
            "snapshot": {"ident": (1, 2)}, "identity": (1, 2)}
    state.update(results={"matches": [item]}, marks=[item], result_cursor=0, mark_cursor=0)
    app.mode = mode
    calls = []
    monkeypatch.setattr(T, "_show_page", lambda *args, **kwargs: calls.append(kwargs) or True)
    right(d)
    app.handle("enter")
    assert not calls and app.mode == mode
    if mode == "log_tools_marks":
        app.handle("delete")
        assert state["marks"] == [item]
    app.handle("down")
    assert not state["cursor_deselected"]
    app.handle("enter")
    assert len(calls) == 1
    if mode == "log_tools_marks":
        app.handle("delete")
        assert not state["marks"]


def test_cleared_source_page_retains_explicit_whole_file_copy_action(dashboard, monkeypatch):
    d, app = dashboard, dashboard.app
    open_log(d)
    source_page(d)
    calls = []
    monkeypatch.setattr(T, "_task", lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    right(d)
    app.handle("Y")
    assert len(calls) == 1 and calls[0][0][1] == "Copying exact source bytes"
    assert app.mode == "log_tools_page" and app.logs.path == str(d.path)


@pytest.mark.parametrize("mode", ["help", "details", "analysis"])
def test_dialog_clears_carried_ranges_locally_without_job_or_toolbar_action(dashboard, monkeypatch, mode):
    d, app = dashboard, dashboard.app
    open_log(d)
    app.logs.begin_selection(app.logs.buffer(app.logs.path), 2)
    app.logs.selection_end = 4
    app.sel_anchor, app.sel_end, app.click_row = 5, 7, 5
    app.selected_id, app.marks = "7", {"7", "8"}
    app.mode = mode
    source = app.logs.path, app.log_job
    if mode == "analysis":
        app.analysis_state["modal"] = "inspect"
    monkeypatch.setattr(d.store, "snapshot", forbidden)
    monkeypatch.setattr(app.logs, "buffer", forbidden)
    right(d)
    assert app.mode == mode and app.selected_id == "7" and app.marks == {"7", "8"}
    assert app.sel_anchor is None and app.click_row is None and app.logs.cursor is None
    assert not app.logs.selection_active and S.lines_cleared(app)
    assert not any(S.cleared(app, scope) for scope in S.JOB_SCOPES)
    assert (app.logs.path, app.log_job) == source
    assert app.toolbar_state["menu"] is None and not app.quit


def test_analysis_modal_graph_reset_preserves_carried_ranges(dashboard, monkeypatch):
    d, app = dashboard, dashboard.app
    open_log(d)
    app.logs.begin_selection(app.logs.buffer(app.logs.path), 2)
    app.sel_anchor, app.sel_end, app.click_row = 4, 6, 4
    app.mode = "analysis"
    app.analysis_state["modal"] = "chart"
    monkeypatch.setattr("raven.chart_interaction.handle_mouse", lambda *args, **kwargs: True)
    right(d)
    assert app.logs.selection_active and app.sel_anchor == 4 and not S.lines_cleared(app)
    assert app.mode == "analysis"


@pytest.mark.parametrize("point", [(-1, 0), (0, -1), (42, 0), (0, 160),
                                   (None, 1), (1, "2"), (False, 2), (1, float("nan"))])
def test_invalid_or_offscreen_rightclick_never_clears_or_routes(dashboard, point):
    app = dashboard.app
    app.marks, app.selected_id = {"7", "8"}, "7"
    assert not S.context_click(app, *point, button="right")
    assert app.marks == {"7", "8"} and app.selected_id == "7"
    assert not S.cleared(app)


@pytest.mark.parametrize("reselect", ["arrow", "click"])
def test_cleared_array_cohort_enter_is_inert_and_pages_scroll_content_until_reselected(dashboard, reselect):
    d, app = dashboard, dashboard.app
    app.tab, app.research_view = "research", "arrays"
    app.research_groups = [{"id": "7"}, {"id": "8"}]
    app.research_array_open, app.research_task_offset = True, 24
    app.research_scroll, app.research_rows = 0, 200
    app.last_hits = [(10, "research_array", "7"), (11, "research_array", "8")]
    # Exercise the native cohort API with a manually supplied headless map.
    # These synthetic rows have no composed document or pointer graph.
    I.initialize(app)["graph"] = None
    right(d)
    app.handle("enter")
    assert app.research_array_open and app.research_task_offset == 24
    assert "Select an array cohort" in app.message and S.cleared(app, "research")
    app.handle("pgdn")
    assert app.research_scroll == 12 and app.research_task_offset == 24
    app.handle("pgup")
    assert app.research_scroll == 0 and app.research_task_offset == 24
    assert S.cleared(app, "research")
    if reselect == "arrow":
        app.handle("down")
    else:
        app.click(11, 10, app.last_hits)
    assert not S.cleared(app, "research")
    app.handle("enter")
    assert not app.research_array_open and app.research_task_offset == 0
    app.handle("enter")
    app.handle("pgdn")
    assert app.research_array_open and app.research_task_offset == 24
