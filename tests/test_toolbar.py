"""Precise menu hits, modal isolation, and live terminal update-slider input."""
from collections import deque
import curses
from types import SimpleNamespace

import pytest

from raven import layout as L, metric_sampling as M, refresh_rate as R, screen, toolbar as T
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views


def instance(**values):
    app = SimpleNamespace(cfg=Config(), mode="main", tab="jobs", quit=False,
                          selected_id="41", marks=set(), sampler=None, research=None,
                          logs=SimpleNamespace(browser=False), commands_seen=[], actions_seen=[],
                          messages=[], failures=[], project_state={}, sel_anchor=None,
                          log_selection_expected=False, **values)
    app.say = app.messages.append
    app.fail = app.failures.append
    app.run_command = app.commands_seen.append
    app.handle_action = app.actions_seen.append
    T.initialize(app)
    return app


def views(ascii_=False):
    return SimpleNamespace(g=L.Glyphs(ascii_))


def target(app, kind, index=0):
    hits = [hit for hit in T.initialize(app)["hits"] if hit[3] == kind]
    return hits[index]


def click(app, hit, button="left"):
    return T.handle_mouse(app, hit[0], hit[1], button=button)


@pytest.mark.parametrize("width", [0, 1, 2, 3, 4, 7, 8, 10, 16, 24, 30, 43, 44, 62, 80, 120, 200])
@pytest.mark.parametrize("rate", [1, 25, 50])
@pytest.mark.parametrize("ascii_", [False, True])
def test_toolbar_fits_every_width_with_nonoverlapping_exact_hits(width, rate, ascii_):
    app = instance()
    R.set_multiplier(app, rate)
    row = T.render_bar(views(ascii_), app, width)
    assert L.vlen(L.row_text(row)) == width
    assert all(0 <= x0 < x1 <= width and y == 0 for y, x0, x1, _, _ in app.toolbar_state["hits"])
    spans = [(x0, x1) for _, x0, x1, _, _ in app.toolbar_state["hits"]]
    assert all(left[1] <= right[0] for left, right in zip(spans, spans[1:]))
    actual = M.format_interval(R.cadence(app, "jobs"), ascii_=ascii_)
    if width >= L.vlen(actual) + 2:
        assert actual in L.row_text(row)
    if width:
        assert target(app, "quit")[1] == 0
    if width >= 8:
        assert any(hit[3] == "menu" for hit in app.toolbar_state["hits"])
    if ascii_:
        assert L.row_text(row).isascii()
    else:
        if any(hit[3] == "track" for hit in app.toolbar_state["hits"]):
            assert "▌" in L.row_text(row)


def test_slider_click_endpoints_and_drag_clamp_without_job_navigation():
    app = instance()
    T.render_bar(views(), app, 120)
    assert click(app, target(app, "track", -1), "press")
    assert R.multiplier(app) == 50 and app.toolbar_state["dragging"]
    assert T.handle_mouse(app, 9, -100, button="motion")
    assert R.multiplier(app) == 1
    assert T.handle_mouse(app, 4, 900, button="drag")
    assert R.multiplier(app) == 50
    assert T.handle_mouse(app, 4, 900, button="release")
    assert not app.toolbar_state["dragging"]
    assert not T.handle_mouse(app, 0, 0, button="motion")
    assert not app.commands_seen and not app.actions_seen


def test_completed_slider_click_does_not_capture_unrelated_later_pointer_motion():
    app = instance()
    T.render_bar(views(), app, 120)
    assert click(app, target(app, "track", -1))
    assert R.multiplier(app) == 50 and not app.toolbar_state["dragging"]
    assert not T.handle_mouse(app, 12, -100, button="motion")
    assert R.multiplier(app) == 50
    assert not T.handle_mouse(app, 12, -100, button="release")


@pytest.mark.parametrize("new_width", [0, 8, 40, 80, 180])
def test_slider_resize_cancels_capture_and_old_track_coordinates(new_width):
    app = instance()
    T.render_bar(views(), app, 120)
    assert click(app, target(app, "track", -1), "press")
    assert app.toolbar_state["dragging"] and R.multiplier(app) == 50
    T.render_bar(views(), app, new_width)
    assert not app.toolbar_state["dragging"]
    assert not T.handle_mouse(app, 10, -100, button="motion")
    assert R.multiplier(app) == 50
    # The release is consumed even though the track has moved or disappeared.
    assert T.handle_mouse(app, 10, -100, button="release")
    assert not app.toolbar_state["pressed"]


def test_slider_release_outside_the_bar_ends_capture_without_a_second_action():
    app = instance()
    T.render_bar(views(), app, 120)
    click(app, target(app, "track"), "press")
    T.handle_mouse(app, 25, 900, button="drag")
    assert R.multiplier(app) == 50
    assert T.handle_mouse(app, 25, 900, button="release")
    assert not T.handle_mouse(app, 25, -100, button="motion")
    assert R.multiplier(app) == 50 and not app.commands_seen


def test_slider_release_position_finishes_drag_when_terminal_omits_motion_reports():
    app = instance()
    T.render_bar(views(), app, 120)
    click(app, target(app, "track"), "press")
    assert R.multiplier(app) == 1
    assert click(app, target(app, "track", -1), "release")
    assert R.multiplier(app) == 50 and not app.toolbar_state["dragging"]
    assert not app.commands_seen and not app.actions_seen


def test_slider_arrows_home_end_wheel_and_return_to_underlying_modal():
    app = instance()
    app.mode = "help"
    T.render_bar(views(), app, 80)
    assert click(app, target(app, "rate"))
    assert T.handle_key(app, "end") and R.multiplier(app) == 50
    assert T.handle_key(app, "right") and R.multiplier(app) == 50
    assert T.handle_key(app, "left") and R.multiplier(app) == 49
    assert click(app, target(app, "rate"), "wheel-down") and R.multiplier(app) == 48
    assert click(app, target(app, "rate"), "wheel-up") and R.multiplier(app) == 49
    assert T.handle_key(app, "home") and R.multiplier(app) == 1
    assert T.handle_key(app, "down") and R.multiplier(app) == 1
    assert T.handle_key(app, "esc")
    assert app.mode == "help" and not app.toolbar_state["focus"]
    assert not T.handle_key(app, "down")


def test_mouse_minus_plus_and_partial_bar_value_remain_keyboard_operable():
    app = instance()
    T.render_bar(views(), app, 80)
    click(app, target(app, "plus"))
    assert R.multiplier(app) == 2
    click(app, target(app, "minus"))
    assert R.multiplier(app) == 1
    T.render_bar(views(True), app, 8)
    assert click(app, target(app, "rate"))
    assert T.handle_key(app, "end") and R.multiplier(app) == 50
    T.render_bar(views(True), app, 8)
    assert "500ms" in L.row_text(T.render_bar(views(True), app, 8))
    assert T.handle_key(app, "tab") and not app.toolbar_state["focus"]


@pytest.mark.parametrize("mode", ["main", "help", "details", "palette", "confirm", "terminal_probe"])
def test_keyboard_menu_browsing_retains_every_underlying_mode_and_does_nothing(mode):
    app = instance()
    app.mode = mode
    assert T.handle_key(app, "f10")
    assert app.toolbar_state["menu"] == 0
    for key in ("down", "down", "right", "down", "left", "home", "end"):
        assert T.handle_key(app, key)
    assert app.mode == mode and not app.commands_seen and not app.actions_seen and not app.quit
    assert T.handle_key(app, "esc")
    assert app.mode == mode and app.toolbar_state["menu"] is None
    assert not T.handle_key(app, "down")


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width,height", [(0, 0), (1, 1), (1, 2), (3, 3), (8, 4), (24, 5), (40, 8), (80, 16), (160, 40)])
@pytest.mark.parametrize("menu", range(4))
def test_dropdowns_reserve_toolbar_fit_screen_and_scroll_to_last_item(width, height, ascii_, menu):
    app = instance()
    T.render_bar(views(ascii_), app, width)
    T._open(app, menu)
    T.handle_key(app, "end")
    rows = T.overlay(views(ascii_), {}, app, width, height)
    assert all(1 <= y < height and 0 <= x < width and x + L.vlen(L.row_text(row)) <= width for y, x, row in rows)
    assert all(1 <= y < height and 0 <= left < right <= width for y, left, right, _ in app.toolbar_state["menu_hits"])
    last = T.menu_items(app, menu)[-1]
    if width >= 24 and height >= 4:
        assert any(hit[-1] == last.key for hit in app.toolbar_state["menu_hits"])
    if ascii_:
        assert all(L.row_text(row).isascii() for _, _, row in rows)


def test_mouse_menu_uses_actual_item_and_clears_old_targets_after_change():
    app = instance()
    T.render_bar(views(), app, 120)
    assert click(app, target(app, "menu", 0))
    T.overlay(views(), {}, app, 120, 30)
    first = app.toolbar_state["menu_hits"][0]
    assert first[-1] == "project"
    # Switch menu before the next paint. The old File menu has no live targets.
    assert T.handle_key(app, "right")
    assert not app.toolbar_state["menu_hits"]
    assert T.handle_mouse(app, first[0], first[1])
    assert app.toolbar_state["menu"] == 1
    assert not app.commands_seen and not app.actions_seen


def test_mouse_context_change_does_not_execute_stale_dropdown_target():
    app = instance()
    T.render_bar(views(), app, 120)
    T._open(app, 0)
    T.overlay(views(), {}, app, 120, 30)
    hit = app.toolbar_state["menu_hits"][7]
    app.tab = "history"
    assert T.handle_mouse(app, hit[0], hit[1])
    assert not app.commands_seen
    assert app.toolbar_state["menu"] == 0


def test_click_outside_menu_does_not_dismiss_or_select_hidden_job():
    app = instance()
    T.render_bar(views(), app, 120)
    T._open(app, 0, source="mouse")
    T.overlay(views(), {}, app, 120, 30)
    assert T.handle_mouse(app, 28, 119)
    assert app.toolbar_state["menu"] == 0
    assert not app.commands_seen and app.selected_id == "41"
    assert T.handle_mouse(app, 28, 119, button="release") is False
    assert app.toolbar_state["menu"] == 0
    assert T.handle_mouse(app, 28, 119, button="motion")
    assert app.toolbar_state["menu"] is None
    assert not T.handle_mouse(app, 28, 119)


def test_menu_pointer_motion_keeps_borders_padding_and_toolbar_corridor_open():
    app = instance()
    T.render_bar(views(), app, 120)
    click(app, target(app, "menu"))
    T.overlay(views(), {}, app, 120, 30)
    top, left, bottom, right = app.toolbar_state["menu_rect"]
    for y, x in ((top, left), (top, right - 1), (bottom - 1, left),
                 (bottom - 1, right - 1), (top + 2, right - 2)):
        assert T.handle_mouse(app, y, x, button="motion")
        assert app.toolbar_state["menu"] == 0
    assert not app.commands_seen and not app.actions_seen
    # All labels form one corridor, so moving to Edit switches menus without
    # requiring another click or an intermediate hidden-page interaction.
    edit = target(app, "menu", 1)
    assert T.handle_mouse(app, edit[0], edit[1], button="motion")
    assert app.toolbar_state["menu"] == 1 and not app.toolbar_state["menu_hits"]
    T.overlay(views(), {}, app, 120, 30)
    assert T.handle_mouse(app, 29, 119, button="motion")
    assert app.toolbar_state["menu"] is None and app.selected_id == "41"


def test_hovering_a_disabled_menu_option_highlights_without_invoking_it():
    app = instance()
    T.render_bar(views(), app, 120)
    click(app, target(app, "menu"))
    T.overlay(views(), {}, app, 120, 30)
    hit = next(hit for hit in app.toolbar_state["menu_hits"] if hit[-1] == "runs")
    assert T.handle_mouse(app, hit[0], hit[1], button="motion")
    assert T.menu_items(app, 0)[app.toolbar_state["cursor"]].key == "runs"
    descriptor = next(item for item in T.control_descriptors(app) if item["action"] == ("item", "runs"))
    assert descriptor["disabled"] and "project" in descriptor["reason"].lower()
    assert not app.commands_seen and not app.messages
    assert T.handle_mouse(app, hit[0], hit[1])
    assert app.toolbar_state["menu"] == 0 and not app.commands_seen
    assert "project" in app.messages[-1].lower()


def test_menu_controls_have_unique_stable_identity_and_one_slider_focus_stop():
    app = instance()
    T.render_bar(views(), app, 120)
    click(app, target(app, "menu"))
    T.overlay(views(), {}, app, 120, 30)
    controls = T.control_descriptors(app)
    ids = [item["id"] for item in controls]
    assert len(ids) == len(set(ids))
    assert sum(item["action"][0] == "track" for item in controls) == 1
    assert all(0 <= left < right <= 120 and 0 <= top < bottom <= 30
               for item in controls for top, left, bottom, right in [item["rect"]])
    assert len([item for item in controls if item["action"][0] == "item"]) == len(app.toolbar_state["menu_hits"])


def test_view_accessibility_labels_follow_restored_preferences_not_config_defaults():
    from raven import scrolling, startup
    app = instance()
    startup.restore(app, {"enabled": False})
    scrolling.restore(app, {"enabled": False})
    items = {item.key: item for item in T.menu_items(app, "View")}
    assert items["startup-toggle"].label == "Enable startup animation"
    assert items["smooth-scroll"].label == "Enable smooth scrolling"
    assert items["startup-toggle"].command == "startup toggle"
    assert items["startup-preview"].command == "startup preview"
    assert items["focus-buttons"].command == "focusbuttons"
    startup.restore(app, {"enabled": True})
    scrolling.restore(app, {"enabled": True})
    items = {item.key: item for item in T.menu_items(app, "View")}
    assert items["startup-toggle"].label == "Disable startup animation"
    assert items["smooth-scroll"].label == "Disable smooth scrolling"


def test_keyboard_opened_menu_does_not_close_for_an_unrelated_stationary_pointer():
    app = instance()
    T.render_bar(views(), app, 120)
    T.handle_key(app, "f10")
    T.overlay(views(), {}, app, 120, 30)
    assert T.handle_mouse(app, 29, 119, button="motion")
    assert app.toolbar_state["menu"] == 0
    inside = app.toolbar_state["menu_hits"][0]
    T.handle_mouse(app, inside[0], inside[1], button="motion")
    assert app.toolbar_state["menu_source"] == "mouse"
    T.handle_mouse(app, 29, 119, button="motion")
    assert app.toolbar_state["menu"] is None


def test_pending_scheduler_and_workflow_reviews_cannot_be_discarded_by_menu_commands():
    app = instance()
    for mode, state in [("confirm", {}), ("execution", {"pending_action": "execute"})]:
        app.mode, app.execution_state = mode, state
        T._open(app, 2)
        T._activate(app, T.menu_items(app, "View")[0])
        assert app.mode == mode and not app.commands_seen
        assert app.toolbar_state["menu"] == 2
        assert "pending job review" in app.messages[-1]
        T._activate(app, T.menu_items(app, "Help")[-1])
        assert app.mode == mode and app.toolbar_state["panel"] == "about"
        T.handle_key(app, "esc")
        assert app.mode == mode


def test_context_entries_remain_visible_but_block_unavailable_operations():
    app = instance()
    app.selected_id = None
    for menu, key, notice in [("File", "runs", "project"), ("File", "outputs", "run"),
                              ("Edit", "pin", "job"), ("Edit", "bookmark", "log")]:
        T._open(app, T.MENUS.index(menu))
        item = next(item for item in T.menu_items(app, menu) if item.key == key)
        T._activate(app, item)
        assert notice in app.messages[-1].lower()
        assert app.toolbar_state["menu"] is not None
    app.tab = "research"
    T._activate(app, next(item for item in T.menu_items(app, "Edit") if item.key == "columns"))
    assert "Jobs or History" in app.messages[-1]
    assert not app.commands_seen


def test_toolbar_quit_is_one_explicit_click_even_when_filter_or_confirmation_open():
    app = instance()
    app.mode, app.filter = "confirm", "filtered"
    T.render_bar(views(), app, 80)
    assert click(app, target(app, "quit"))
    assert app.quit and app.mode == "confirm" and app.filter == "filtered"


def test_all_menu_commands_resolve_to_real_capabilities_with_no_scheduler_mutations():
    app = App(Store(persist=False), None, None, Config(), "tester")
    commands = app.commands()
    for menu in T.MENUS:
        entries = T.menu_items(app, menu)
        assert len({item.key for item in entries}) == len(entries)
        for item in entries:
            if item.command:
                assert item.command.split()[0] in commands
                assert item.command.split()[0] not in ("submit", "cancel", "hold", "release", "requeue", "top", "resubmit", "orchestrate")


def test_parameterized_project_action_opens_editable_prompt_and_never_executes():
    app = App(Store(persist=False), None, None, Config(), "tester")
    app.mode = "help"
    T._activate(app, T.menu_items(app, "File")[0])
    assert app.mode == "palette" and app.palette_edit == "project "
    assert app.command_state["origin_mode"] == "help"
    assert not app.project_state["root"]
    assert not app.research


def test_tab_activation_dismisses_old_modal_and_research_uses_existing_workspace():
    app = App(Store(persist=False), None, None, Config(), "tester")
    app.mode = "help"
    item = next(item for item in T.menu_items(app, "View") if item.key == "tab-sources")
    T._activate(app, item)
    assert app.tab == "sources" and app.mode == "main"
    item = next(item for item in T.menu_items(app, "View") if item.key == "research-artifacts")
    T._activate(app, item)
    assert app.tab == "research" and app.research_view == "artifacts"


def test_real_visual_selection_and_rate_only_change_local_state(tmp_path):
    app = App(Store(persist=False), None, None, Config(), "tester")
    painter = Views(L.Glyphs(False), app.cfg)
    app.views_ref = painter
    app.store.apply_jobs([Job("41", "cpu-run", "cpu", "RUNNING")])
    painter.compose(app.store.snapshot(), app, 120, 30)
    app.mode = "help"
    T._activate(app, next(item for item in T.menu_items(app, "Edit") if item.key == "select"))
    assert app.mode == "main" and app.sel_anchor is not None
    T._activate(app, next(item for item in T.menu_items(app, "View") if item.key == "rate"))
    assert app.toolbar_state["focus"] == "rate"
    T.handle_key(app, "end")
    assert R.multiplier(app) == 50 and app.selected_id == "41"


def test_about_is_bounded_and_reserves_top_row_without_changing_underlying_dialog():
    app = instance()
    app.mode = "confirm"
    T.run_command(app, ["about"])
    for width, height in [(0, 0), (1, 1), (1, 2), (20, 5), (80, 20)]:
        rows = T.overlay(views(True), {}, app, width, height)
        assert all(1 <= y < height and x + L.vlen(L.row_text(row)) <= width for y, x, row in rows)
        assert all(L.row_text(row).isascii() for _, _, row in rows)
    assert app.mode == "confirm"
    assert T.handle_key(app, "end")
    rows = T.overlay(views(True), {}, app, 80, 8)
    assert "previous screen" in "\n".join(L.row_text(row) for _, _, row in rows)
    assert T.handle_key(app, "esc") and app.mode == "confirm"


@pytest.fixture
def terminal_dashboard():
    cfg = Config({"animations": False, "gpu_sampling": False, "log_lines": 0})
    store = Store(persist=False)
    store.apply_jobs([Job(str(index + 1), "cpu-run-" + str(index), "cpu", "RUNNING") for index in range(30)])
    app = App(store, None, None, cfg, "toolbar-tester")
    painter = Views(L.Glyphs(False), cfg)
    app.views_ref = painter
    painter.compose(store.snapshot(), app, 120, 30)
    return app, painter, store


def dispatch_mouse(app, x, y, buttons):
    screen._apply_input(app, ("mouse", (0, x, y, 0, buttons)), app.last_hits, curses)


@pytest.mark.parametrize("mode", ["analysis", "columns", "field_explanation", "log_tools_page"])
def test_global_menu_has_priority_over_underlying_palette_context(terminal_dashboard, mode):
    app, _, _ = terminal_dashboard
    app.mode = mode
    previous_command_input = app.palette_edit
    screen._apply_input(app, ("f10", None), app.last_hits, curses)
    assert app.toolbar_state["menu"] == 0 and app.mode == mode
    screen._apply_input(app, (":", None), app.last_hits, curses)
    assert app.mode == mode and app.palette_edit == previous_command_input
    assert app.toolbar_state["menu"] == 0
    screen._apply_input(app, ("esc", None), app.last_hits, curses)
    assert app.mode == mode and app.toolbar_state["menu"] is None
    # Only after the toolbar closes does ':' reach that dialog's command input.
    screen._apply_input(app, (":", None), app.last_hits, curses)
    assert app.mode == "palette"


def test_dispatcher_slider_wheel_changes_rate_without_scrolling_jobs(terminal_dashboard):
    app, painter, store = terminal_dashboard
    app.cursor["jobs"] = 10
    painter.compose(store.snapshot(), app, 120, 30)
    selected = app.selected_id
    rate = target(app, "rate")
    dispatch_mouse(app, rate[1], rate[0], curses.BUTTON4_PRESSED)
    assert R.multiplier(app) == 2
    assert app.cursor["jobs"] == 10 and app.selected_id == selected
    dispatch_mouse(app, rate[1], rate[0], curses.BUTTON5_PRESSED)
    assert R.multiplier(app) == 1
    assert app.cursor["jobs"] == 10 and app.selected_id == selected
    assert app.toolbar_state["focus"] == "rate"
    # Moving the wheel back into the table releases slider focus and restores
    # normal table navigation; the one event reaches exactly one controller.
    job_row = next(y for y, kind, _ in app.last_hits if kind == "job")
    dispatch_mouse(app, 5, job_row, curses.BUTTON5_PRESSED)
    assert app.cursor["jobs"] == 11 and R.multiplier(app) == 1
    assert app.toolbar_state["focus"] == ""


def test_dispatcher_drags_and_releases_slider_without_job_selection(terminal_dashboard):
    app, _, _ = terminal_dashboard
    first, last = target(app, "track"), target(app, "track", -1)
    selected, cursor = app.selected_id, app.cursor["jobs"]
    dispatch_mouse(app, last[1], 0, curses.BUTTON1_PRESSED)
    assert app.toolbar_state["dragging"] and R.multiplier(app) == 50
    dispatch_mouse(app, first[1], 0, curses.REPORT_MOUSE_POSITION)
    assert R.multiplier(app) == 1
    dispatch_mouse(app, first[1], 0, curses.BUTTON1_RELEASED)
    assert not app.toolbar_state["dragging"]
    dispatch_mouse(app, last[1], 0, curses.REPORT_MOUSE_POSITION)
    assert R.multiplier(app) == 1
    assert app.selected_id == selected and app.cursor["jobs"] == cursor
    assert app.mode == "main" and not app.logs.selection_active


def test_dispatcher_completed_click_never_latches_the_slider(terminal_dashboard):
    app, _, _ = terminal_dashboard
    first, last = target(app, "track"), target(app, "track", -1)
    selected = app.selected_id
    dispatch_mouse(app, last[1], 0, curses.BUTTON1_CLICKED)
    assert R.multiplier(app) == 50 and not app.toolbar_state["dragging"]
    dispatch_mouse(app, first[1], 0, curses.REPORT_MOUSE_POSITION)
    assert R.multiplier(app) == 50 and app.selected_id == selected


def test_dispatcher_pressed_motion_and_release_outside_slider_are_one_gesture(terminal_dashboard):
    app, _, _ = terminal_dashboard
    first, last = target(app, "track"), target(app, "track", -1)
    selected, cursor = app.selected_id, app.cursor["jobs"]
    dispatch_mouse(app, first[1], 0, curses.BUTTON1_PRESSED)
    dispatch_mouse(app, 900, 20, curses.REPORT_MOUSE_POSITION | curses.BUTTON1_PRESSED)
    assert app.toolbar_state["dragging"] and R.multiplier(app) == 50
    dispatch_mouse(app, 900, 20, curses.BUTTON1_RELEASED)
    assert not app.toolbar_state["dragging"]
    dispatch_mouse(app, first[1], 0, curses.REPORT_MOUSE_POSITION)
    assert R.multiplier(app) == 50 and app.selected_id == selected
    assert app.cursor["jobs"] == cursor and not app.logs.selection_active


def test_dispatcher_hover_selects_switches_and_dismisses_mouse_menu_without_actions(terminal_dashboard):
    app, painter, store = terminal_dashboard
    selected, cursor = app.selected_id, app.cursor["jobs"]
    file_button = target(app, "menu")
    dispatch_mouse(app, file_button[1], 0, curses.BUTTON1_PRESSED)
    dispatch_mouse(app, file_button[1], 0, curses.BUTTON1_RELEASED)
    painter.overlay(store.snapshot(), app, 120, 30)
    export = next(hit for hit in app.toolbar_state["menu_hits"] if hit[-1] == "export-text")
    dispatch_mouse(app, export[1], export[0], curses.REPORT_MOUSE_POSITION)
    assert T.menu_items(app, 0)[app.toolbar_state["cursor"]].key == "export-text"
    assert app.mode == "main" and app.selected_id == selected
    edit_button = target(app, "menu", 1)
    dispatch_mouse(app, edit_button[1], 0, curses.REPORT_MOUSE_POSITION)
    assert app.toolbar_state["menu"] == 1
    painter.overlay(store.snapshot(), app, 120, 30)
    dispatch_mouse(app, 119, 29, curses.REPORT_MOUSE_POSITION)
    assert app.toolbar_state["menu"] is None
    assert app.selected_id == selected and app.cursor["jobs"] == cursor
    assert not app.project_state["root"]


def test_dispatcher_menu_supports_repeated_toggles_without_closing(terminal_dashboard):
    app, painter, store = terminal_dashboard
    button = target(app, "menu", 2)
    dispatch_mouse(app, button[1], 0, curses.BUTTON1_CLICKED)
    for expected, label in ((True, "Resume inspection"), (False, "Pause inspection")):
        painter.overlay(store.snapshot(), app, 120, 100)
        hit = next(hit for hit in app.toolbar_state["menu_hits"] if hit[-1] == "freeze")
        dispatch_mouse(app, hit[1], hit[0], curses.BUTTON1_PRESSED)
        dispatch_mouse(app, hit[1], hit[0], curses.BUTTON1_RELEASED)
        assert app.toolbar_state["menu"] == 2
        assert bool(app.table_tools_state.get("freeze")) is expected
        assert label in next(item.label for item in T.menu_items(app, "View") if item.key == "freeze")
        assert not app.toolbar_state["menu_hits"]
    screen._apply_input(app, ("esc", None), app.last_hits, curses)
    assert app.toolbar_state["menu"] is None and app.mode == "main"


def test_prompt_keeps_dropdown_until_pointer_exit_and_preserves_editable_command(terminal_dashboard):
    app, painter, store = terminal_dashboard
    button = target(app, "menu")
    dispatch_mouse(app, button[1], 0, curses.BUTTON1_CLICKED)
    painter.overlay(store.snapshot(), app, 120, 30)
    hit = next(hit for hit in app.toolbar_state["menu_hits"] if hit[-1] == "project")
    dispatch_mouse(app, hit[1], hit[0], curses.BUTTON1_CLICKED)
    assert app.mode == "palette" and app.palette_edit == "project "
    assert app.toolbar_state["menu"] == 0 and not app.toolbar_state["menu_hits"]
    painter.overlay(store.snapshot(), app, 120, 30)
    dispatch_mouse(app, 119, 29, curses.REPORT_MOUSE_POSITION)
    assert app.toolbar_state["menu"] is None
    assert app.mode == "palette" and app.palette_edit == "project "
    assert app.command_state["origin_mode"] == "main" and not app.project_state["root"]


@pytest.mark.parametrize("mode", ["confirm", "terminal_probe"])
def test_dispatcher_quit_button_remains_available_over_reviews_and_input_probe(terminal_dashboard, mode):
    app, _, _ = terminal_dashboard
    app.mode = mode
    app.confirm = {"action": "cancel", "jobs": app.store.jobs[:1]}
    review = app.confirm.copy()
    probe_events = list(app.session_tools_state["probe_events"])
    quit_button = target(app, "quit")
    dispatch_mouse(app, quit_button[1], quit_button[0], curses.BUTTON1_PRESSED)
    assert app.quit and app.mode == mode and app.confirm == review
    assert app.session_tools_state["probe_events"] == probe_events


def test_dispatcher_rejects_stale_menu_target_after_underlying_tab_changes(terminal_dashboard):
    app, painter, store = terminal_dashboard
    screen._apply_input(app, ("f10", None), app.last_hits, curses)
    painter.overlay(store.snapshot(), app, 120, 30)
    project = next(hit for hit in app.toolbar_state["menu_hits"] if hit[-1] == "project")
    app.enter_tab("history")
    dispatch_mouse(app, project[1], project[0], curses.BUTTON1_PRESSED)
    assert app.toolbar_state["menu"] == 0 and app.tab == "history"
    assert app.mode == "main" and not app.palette_edit
    assert not app.project_state["root"] and not app.research


def test_queued_menu_navigation_repaints_before_next_move_and_activation(terminal_dashboard, monkeypatch):
    app, painter, store = terminal_dashboard
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    app.run_command("menu View")
    painter.overlay(store.snapshot(), app, 120, 12)

    class QueuedWindow:
        def __init__(self):
            self.values = deque([curses.KEY_DOWN, "\n"])

        def timeout(self, value):
            pass

        def get_wch(self):
            if not self.values:
                raise curses.error("empty event queue")
            return self.values.popleft()

    window = QueuedWindow()
    assert screen._consume_input_batch(app, window, curses, app.last_hits, ("down", None)) is None
    assert app.toolbar_state["cursor"] == 1 and app.tab == "jobs"
    assert list(window.values) == [curses.KEY_DOWN, "\n"]
    painter.overlay(store.snapshot(), app, 120, 12)
    # The next event is applied to the newly drawn menu. It must leave Enter
    # queued, because that Enter opens History, rather than stale Cluster.
    event = screen._read_input(window, curses)
    assert event == ("down", None)
    assert screen._consume_input_batch(app, window, curses, app.last_hits, event) is None
    assert app.toolbar_state["cursor"] == 2 and app.tab == "jobs"
    assert list(window.values) == ["\n"]
    painter.overlay(store.snapshot(), app, 120, 12)
    event = screen._read_input(window, curses)
    assert event == ("enter", None)
    screen._consume_input_batch(app, window, curses, app.last_hits, event)
    assert app.tab == "history" and app.toolbar_state["menu"] == 2
    assert not window.values and not app.marks


def test_every_width_retains_rate_value_across_responsive_layout_thresholds():
    app = instance()
    for rate in (1, 25, 50):
        R.set_multiplier(app, rate)
        for width in range(0, 201):
            row = T.render_bar(views(True), app, width)
            text = L.row_text(row)
            assert L.vlen(text) == width
            actual = M.format_interval(R.cadence(app, "jobs"), ascii_=True)
            if width >= L.vlen(actual) + 2:
                assert actual in text, (width, rate, text)


def test_rate_digit_changes_keep_slider_and_menu_geometry_fixed_at_every_width():
    app = instance()
    for width in range(8, 201):
        baseline = None
        for rate in (1, 9, 10, 25, 50):
            R.set_multiplier(app, rate)
            T.render_bar(views(), app, width)
            geometry = [(left, right, kind, key) for _, left, right, kind, key
                        in app.toolbar_state["hits"] if kind in ("menu", "minus", "track", "plus")]
            if baseline is None:
                baseline = geometry
            assert geometry == baseline, (width, rate, baseline, geometry)


def test_rate_crossing_ten_keeps_capture_and_drag_bounds_in_place():
    app = instance()
    R.set_multiplier(app, 9)
    T.render_bar(views(), app, 80)
    first, last = target(app, "track"), target(app, "track", -1)
    click(app, first, "press")
    T.handle_mouse(app, 0, last[1], button="drag")
    assert R.multiplier(app) == 50 and app.toolbar_state["dragging"]
    T.render_bar(views(), app, 80)
    assert target(app, "track")[1:3] == first[1:3]
    assert target(app, "track", -1)[1:3] == last[1:3]
    assert app.toolbar_state["dragging"]
    T.handle_mouse(app, 0, first[1], button="release")
    assert R.multiplier(app) == 1 and not app.toolbar_state["dragging"]


@pytest.mark.parametrize("base,rate,label", [(2.0, 1, "5s"), (2.0, 3, "4.55s"),
    (2.0, 25, "1.62s"), (2.0, 49, "524ms"), (2.0, 50, "500ms"),
    (.0005, 1, "5s"), (.0005, 50, "500ms"), (.005, 1, "5s"),
    (0.0, 50, "0s"), (30.0, 1, "30s"), (30.0, 50, "3s")])
@pytest.mark.parametrize("ascii_", [False, True])
def test_global_slider_displays_actual_safe_cadence_and_character_mode(base, rate, label, ascii_):
    app = instance()
    app.cfg.set("intervals", {"jobs": base})
    R.set_multiplier(app, rate)
    row = T.render_bar(views(ascii_), app, 100)
    text = L.row_text(row)
    label = label.replace("µ", "u") if ascii_ else label
    assert label in text
    assert f"{rate}x" not in text
    assert L.vlen(text) == 100
    assert any(label in item["label"] for item in T.control_descriptors(app))


@pytest.mark.parametrize("ascii_", [False, True])
def test_global_slider_every_keyboard_step_changes_poll_interval_within_domain(ascii_):
    app = instance()
    T.render_bar(views(ascii_), app, 120)
    assert click(app, target(app, "rate"))
    readings, labels, geometry = [], [], []
    for index in range(1, 51):
        assert R.multiplier(app) == index
        row = T.render_bar(views(ascii_), app, 120)
        readings.append(R.cadence(app, "jobs"))
        labels.append(T._polling_label(app, ascii_=ascii_))
        assert labels[-1] in L.row_text(row)
        geometry.append([(hit[1], hit[2], hit[3], hit[4])
                         for hit in app.toolbar_state["hits"]])
        assert T.handle_key(app, "right")
    assert readings[0] == 5.0 and readings[-1] == .5
    assert all(.5 <= value <= 5 for value in readings)
    assert all(left > right for left, right in zip(readings, readings[1:]))
    assert len(set(labels)) == 50
    assert all(painted == geometry[0] for painted in geometry)
    assert R.multiplier(app) == 50
    assert T.handle_key(app, "home") and R.cadence(app, "jobs") == 5
    assert T.handle_key(app, "end") and R.cadence(app, "jobs") == .5


def test_global_slider_every_visible_drag_position_changes_poll_interval():
    app = instance()
    T.render_bar(views(), app, 120)
    track = [hit for hit in app.toolbar_state["hits"] if hit[3] == "track"]
    assert len(track) > 2
    intervals = []
    assert click(app, track[0], "press")
    for hit in track:
        assert T.handle_mouse(app, hit[0], hit[1], button="drag")
        intervals.append(R.cadence(app, "jobs"))
    assert intervals[0] == 5.0 and intervals[-1] == .5
    assert all(left > right for left, right in zip(intervals, intervals[1:]))
    assert click(app, track[-1], "release")
    assert R.cadence(app, "jobs") == .5 and not app.toolbar_state["dragging"]


@pytest.mark.parametrize("launcher,slow,fast", [("raven", 60., 6.), ("ravenl", 60., 6.), ("ravenh", 5., .5)])
def test_managed_launcher_slider_updates_real_scheduler_and_label(launcher, slow, fast, tmp_path, monkeypatch):
    """Exercise the public launch path with allocation/transport IO replaced."""
    from raven import cli, raven_config, raven_launch, telemetry_settings
    from raven.sampler import Sampler

    monkeypatch.setattr(raven_launch, "resolve", lambda *args, **kwargs: raven_config.resolve(
        launcher=kwargs["launcher"], env={"HOME": str(tmp_path)}, system_policy_paths=[]))
    manager = SimpleNamespace(shutdown=lambda: None, snapshot=lambda: {"state": "heavy", "mode": "heavy"})
    monkeypatch.setattr(raven_launch, "_allocation", lambda _: manager)
    monkeypatch.setattr(raven_launch, "configure_placement", lambda *_: None)
    monkeypatch.setattr(raven_launch, "_wait_heavy", lambda *_: None)
    monkeypatch.setattr("raven.sampler.time.time", lambda: 1000.)
    seen = []

    def dashboard(argv, *, raven, raven_allocation):
        app = instance()
        app.cfg = Config(raven_config.to_raven_config(raven))
        app.cfg.data["raven"]["collection"]["jitter_fraction"] = 0.
        app.raven_allocation_manager = raven_allocation
        app.raven_ui_state = {"snapshot": {"state": "heavy" if raven_allocation else "light",
                                            "mode": "heavy" if raven_allocation else "light"}}
        app.research = SimpleNamespace(polling_multiplier=1, set_refresh_interval=lambda _, **kwargs: None)
        worker = Sampler(SimpleNamespace(), Store(persist=False), app.cfg["intervals"], [],
                         collection=app.cfg["raven"]["collection"])
        app.sampler = worker
        try:
            telemetry_settings.sync(app)
            R.set_multiplier(app, 1)
            worker.health("jobs").calls = 1
            worker.health("jobs").last_ok = 1000.
            worker.last_run["jobs"] = 1000.
            T.render_bar(views(), app, 140)
            assert click(app, target(app, "rate"))
            readings, labels, geometries = [], [], []
            for position in range(1, 51):
                assert R.multiplier(app) == position
                row = T.render_bar(views(), app, 140)
                actual = worker.effective_interval("jobs")
                assert R.candidate_cadence(app, "jobs", position) == pytest.approx(actual)
                labels.append(T._polling_label(app))
                assert labels[-1] in L.row_text(row)
                readings.append(actual)
                geometries.append([(hit[1], hit[2], hit[3], hit[4]) for hit in app.toolbar_state["hits"]])
                assert app.research.polling_multiplier == app.logs.polling_multiplier == 1
                assert not worker.due("jobs", 1000. + actual - .000001)
                assert worker.due("jobs", 1000. + actual + .000001)
                T.handle_key(app, "right")
            assert readings[0] == slow and readings[-1] == fast
            assert all(left > right for left, right in zip(readings, readings[1:]))
            assert len(set(labels)) == 50
            assert all(geometry == geometries[0] for geometry in geometries)
            assert T.handle_key(app, "home")
            assert worker.effective_interval("jobs") == slow
            assert T.handle_key(app, "end")
            assert worker.effective_interval("jobs") == fast
            seen.append(launcher)
            return 0
        finally:
            worker.shutdown()

    monkeypatch.setattr(cli, "main", dashboard)
    entry = raven_launch.heavy_main if launcher == "ravenh" else raven_launch.light_main
    assert entry([]) == 0
    assert seen == [launcher]


def test_toolbar_uses_live_sampler_queue_cadence_not_stale_configuration():
    app = instance()
    app.sampler = SimpleNamespace(intervals={"jobs": 10.0},
                                  effective_interval=lambda source: 0.75)
    text = L.row_text(T.render_bar(views(), app, 100))
    assert "750ms" in text
    reset = next(item for item in T.menu_items(app, "View") if item.key == "rate-reset")
    assert reset.label == "Reset queue polling to 10s"
    help_text = T._rate_help(app)
    assert "Home 10s" in help_text and "End 1s" in help_text
    assert "1x" not in help_text and "50x" not in help_text


@pytest.mark.parametrize("kind", ["track", "rate", "minus", "plus"])
def test_global_slider_right_click_resets_without_changing_jobs_or_modal(kind):
    app = instance()
    app.mode = "confirm"
    app.marks = {"41", "42"}
    R.set_multiplier(app, 50)
    T.render_bar(views(), app, 120)
    assert click(app, target(app, kind), "right")
    assert R.multiplier(app) == 1
    assert app.mode == "confirm" and app.marks == {"41", "42"}
    assert app.selected_id == "41" and not app.commands_seen
    assert not app.toolbar_state["dragging"] and not app.toolbar_state["pressed"]
    assert app.messages == ["Queue polling reset to 5s."]


def test_global_slider_right_click_ends_drag_without_late_release_mutation():
    app = instance()
    T.render_bar(views(), app, 100)
    assert click(app, target(app, "track", -1), "press")
    assert R.multiplier(app) == 50 and app.toolbar_state["dragging"]
    assert click(app, target(app, "track", -1), "right")
    assert R.multiplier(app) == 1 and not app.toolbar_state["dragging"]
    assert not T.handle_mouse(app, 10, 999, button="release")
    assert not T.handle_mouse(app, 10, 999, button="motion")
    assert R.multiplier(app) == 1


@pytest.mark.parametrize("base", [0, 2, .005, .0005, 15, 9999])
@pytest.mark.parametrize("ascii_", [False, True])
def test_interval_units_keep_captured_slider_geometry_fixed(base, ascii_):
    app = instance()
    app.cfg.set("intervals", {"jobs": base})
    for width in (8, 20, 40, 60, 100):
        geometry = None
        for rate in (1, 2, 3, 10, 50):
            R.set_multiplier(app, rate)
            row = T.render_bar(views(ascii_), app, width)
            assert L.vlen(L.row_text(row)) == width
            painted = [(left, right, kind, key) for _, left, right, kind, key
                       in app.toolbar_state["hits"] if kind in ("menu", "track", "plus", "minus")]
            if geometry is None:
                geometry = painted
            assert geometry == painted


def test_actual_curses_paint_keeps_global_bar_visible_above_tiny_confirmation(terminal_dashboard, monkeypatch):
    app, painter, store = terminal_dashboard
    app.cfg.data['raven']={'managed':True}
    app.mode = "confirm"
    app.confirm = {"action": "cancel", "jobs": store.jobs[:1]}
    for name in ("curs_set", "mousemask", "mouseinterval", "doupdate", "set_escdelay"):
        monkeypatch.setattr(curses, name, lambda *args: None)
    monkeypatch.setattr(curses, "has_colors", lambda: False)

    class PaintedWindow:
        def __init__(self):
            self.values = deque(["\x03"])
            self.rows = []
            self.paint_order = []

        def getmaxyx(self):
            return 2, 40

        def timeout(self, value):
            pass

        def keypad(self, enabled):
            pass

        def erase(self):
            self.rows = [[" "] * 40 for _ in range(2)]

        def addstr(self, y, x, text, attribute):
            self.paint_order.append((y, x, text))
            for character in text:
                cells = L.vlen(character)
                if cells and 0 <= y < 2 and 0 <= x < 40:
                    self.rows[y][x] = character
                x += cells

        def noutrefresh(self):
            pass

        def get_wch(self):
            if not self.values:
                raise curses.error("empty input")
            return self.values.popleft()

    window = PaintedWindow()
    monkeypatch.setattr(curses, "wrapper", lambda callback: callback(window))
    screen.run_curses(app, painter, None, store, None, app.cfg)
    assert app.quit and not window.values
    # The confirmation painter runs after the body, including on row zero in
    # a two-row terminal. The renderer must repaint the actual global controls
    # last; this verifies the final cells, rather than only renderer call order.
    top = "".join(window.rows[0])
    assert ("File" in top and "Edit" in top) or (" F " in top and " E " in top)
    assert "60s" in top and "Light" in top
    assert top.lstrip().startswith("x")
    assert not app.command_state["confirm_controls_visible"]
