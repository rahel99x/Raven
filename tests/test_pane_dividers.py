"""Exact splitter geometry, capture, cancellation and bounded pointer work."""
from types import SimpleNamespace

import pytest

from raven import interaction, job_panels, layout as L, pane_drag as P, workspace_layout as W


def app(**overrides):
    instance = SimpleNamespace(tab="jobs", mode="main", width=160, height=48,
                               body_origin=7, toolbar_state={}, cfg={}, saves=0,
                               messages=[], value=60)
    for key, value in overrides.items():
        setattr(instance, key, value)
    instance.say = instance.messages.append
    instance.save = lambda: setattr(instance, "saves", instance.saves + 1)
    from raven.workbench import bind_actions
    bind_actions(instance)
    P.begin_frame(instance, instance.width, instance.height)
    return instance


def divider(instance, *, axis="vertical", key="workspace:jobs", x=95, y=7,
            width=1, height=40, origin=0, extent=159, minimum=20, maximum=80,
            full_vertical=True):
    return P.register(instance, key, axis, x, y, width, height, origin, extent,
                      instance.value, lambda value: setattr(instance, "value", value),
                      minimum=minimum, maximum=maximum, full_vertical=full_vertical)


@pytest.mark.parametrize("buffer", [-1, 0, 1])
def test_buffer_press_has_no_jump_and_drag_saves_once_on_release(buffer):
    instance = app()
    divider(instance)
    assert P.handle_mouse(instance, 12, 95 + buffer, "press")
    assert P.active(instance) and instance.value == 60 and instance.saves == 0
    assert P.handle_mouse(instance, 12, 111 + buffer, "motion")
    assert instance.value == 70 and instance.saves == 0
    # Rendering a changed split replaces descriptors, retaining capture.
    P.begin_frame(instance, instance.width, instance.height)
    divider(instance, x=111)
    assert P.handle_mouse(instance, 12, 111 + buffer, "release")
    assert not P.active(instance) and instance.value == 70 and instance.saves == 1


@pytest.mark.parametrize("coordinates", [(12, 93), (12, 97), (6, 95), (47, 95)])
def test_capture_does_not_extend_beyond_buffer_or_divider_height(coordinates):
    instance = app()
    divider(instance)
    assert not P.handle_mouse(instance, *coordinates, "press")
    assert not P.active(instance)


@pytest.mark.parametrize("kind,value", [
    ("sort_header", ("recent", "id", 1, 20)),
    ("control", {"left": 1, "right": 20}),
    ("job_panel_tab", ("logs", 1, 20)),
    ("job_panel_view", ("advisor", 1, 20)),
    ("job_panel_file", ("stdout", 1, 20)),
    ("job_panel_action", (("select", "77"), 1, 20)),
    ("node_cell", ("node-1", 1, 20)),
])
def test_horizontal_buffer_preserves_neighboring_sort_headers_and_buttons(kind, value):
    instance = app()
    divider(instance, axis="horizontal", x=0, y=20, width=160, height=1,
            origin=7, extent=40)
    instance.last_hits = [(21, kind, value)]
    assert not P.handle_mouse(instance, 21, 10, "press")
    assert not P.active(instance)
    # Buffer space outside the button keeps the generous resize target.
    assert P.handle_mouse(instance, 21, 30, "press")
    assert P.active(instance)


def test_exact_divider_line_and_existing_capture_win_over_neighboring_buttons():
    instance = app()
    divider(instance, axis="horizontal", x=0, y=20, width=160, height=1,
            origin=7, extent=40)
    instance.last_hits = [(20, "sort_header", ("recent", "id", 1, 20)),
                          (21, "sort_header", ("recent", "id", 1, 20))]
    assert P.handle_mouse(instance, 20, 10, "press")
    assert P.handle_mouse(instance, 21, 10, "motion")
    assert P.handle_mouse(instance, 21, 10, "release")
    assert not P.active(instance)


def test_vertical_buffer_keeps_adjacent_details_buttons_clickable():
    instance = app()
    divider(instance)
    instance.last_hits = [(12, "job_panel_tab", ("logs", 96, 104))]
    assert not P.handle_mouse(instance, 12, 96, "press")
    assert P.handle_mouse(instance, 12, 95, "press")


@pytest.mark.parametrize("kind", ["job", "recent", "fin", "dep"])
def test_divider_buffer_keeps_real_data_rows_selectable_but_free_margin_draggable(kind):
    instance = app()
    divider(instance, axis="horizontal", x=0, y=20, width=160, height=1,
            origin=7, extent=40)
    rows = [[("", "")]] * 48
    rows[21] = L.fill_row([(" 77 running", "")], 160, "")
    interaction.publish(instance, rows, [(21, kind, "77")], 160, 48)
    assert not P.handle_mouse(instance, 21, 5, "press")
    assert not P.active(instance)
    assert P.handle_mouse(instance, 21, 0, "press")
    assert P.active(instance)


def test_drag_back_to_original_position_does_not_persist():
    instance = app()
    divider(instance)
    P.handle_mouse(instance, 12, 95, "press")
    P.handle_mouse(instance, 12, 111, "motion")
    assert instance.value == 70
    P.handle_mouse(instance, 12, 95, "motion")
    assert instance.value == 60
    P.handle_mouse(instance, 12, 95, "release")
    assert instance.saves == 0


def test_horizontal_capture_uses_y_and_clamps_both_panes():
    instance = app(height=32, value=50)
    divider(instance, axis="horizontal", x=0, y=19, width=160, height=1,
            origin=7, extent=24, minimum=20, maximum=80)
    assert P.handle_mouse(instance, 20, 50, "press")
    P.handle_mouse(instance, -1000, 50, "motion")
    assert instance.value == 20
    P.handle_mouse(instance, 1000, 50, "motion")
    assert instance.value == 80
    P.handle_mouse(instance, 1000, 50, "release")
    assert instance.saves == 1


@pytest.mark.parametrize("cancel", ["escape", "tab", "resize", "modal", "menu", "hidden", "origin"])
def test_stale_drag_cancels_restores_original_and_never_saves(cancel):
    instance = app()
    divider(instance)
    P.handle_mouse(instance, 12, 95, "press")
    P.handle_mouse(instance, 12, 111, "motion")
    assert instance.value == 70
    if cancel == "escape":
        assert P.handle_key(instance, "esc")
    else:
        if cancel == "tab":
            instance.tab = "analytics"
        elif cancel == "resize":
            instance.width = 120
        elif cancel == "modal":
            instance.mode = "confirm"
        elif cancel == "menu":
            instance.toolbar_state["menu"] = "view"
        elif cancel == "hidden":
            P.begin_frame(instance, instance.width, instance.height)
        elif cancel == "origin":
            divider(instance, origin=1)
        P.tick(instance)
    assert instance.value == 60 and instance.saves == 0 and not P.active(instance)


def test_stale_press_after_tab_switch_does_not_resize_the_previous_page():
    instance = app()
    divider(instance)
    instance.tab = "history"
    assert not P.handle_mouse(instance, 12, 95, "press")
    assert not P.active(instance)


@pytest.mark.parametrize("blocked", ["confirm", "menu", "panel"])
def test_modal_or_toolbar_has_priority_over_registered_dividers(blocked):
    instance = app()
    divider(instance)
    if blocked == "confirm":
        instance.mode = "confirm"
    else:
        instance.toolbar_state[blocked] = "view"
    assert not P.handle_mouse(instance, 12, 95, "press")


def test_keyboard_focus_uses_axis_arrows_and_releases_button_navigation():
    instance = app(interaction_state={"active": True})
    divider(instance)
    assert P.run_command(instance, ["pane-focus", "workspace:jobs"])
    assert P.handle_key(instance, "right") and instance.value == 62 and instance.saves == 1
    assert not P.handle_key(instance, "down")
    divider(instance, x=98)
    assert P.handle_key(instance, "pgup") and instance.value == 52 and instance.saves == 2
    assert P.handle_key(instance, "esc")
    assert instance.pane_drag_state["focus"] is None and not instance.interaction_state["active"]
    assert not P.handle_key(instance, "right")


def test_explicit_focus_from_command_palette_survives_return_to_main_frame():
    instance = app()
    instance.mode = "palette"
    P.begin_frame(instance, 160, 48)
    divider(instance)
    # App.run_command restores main before dispatching a confirmed command.
    instance.mode = "main"
    assert P.run_command(instance, ["pane-focus", "workspace:jobs"])
    P.begin_frame(instance, 160, 48)
    divider(instance)
    assert instance.pane_drag_state["focus"] == "workspace:jobs"
    assert P.handle_key(instance, "right") and instance.value == 62
    instance.tab = "history"
    assert not P.handle_key(instance, "right") and instance.value == 62
    P.begin_frame(instance, 160, 48)
    assert instance.pane_drag_state["focus"] is None


def test_other_content_click_releases_divider_keyboard_focus():
    instance = app()
    divider(instance)
    assert P.handle_mouse(instance, 12, 95, "left")
    assert not P.handle_mouse(instance, 12, 20, "press")
    assert instance.pane_drag_state["focus"] is None


def test_wheel_restores_content_navigation_after_a_completed_resize():
    instance = app()
    divider(instance, axis="horizontal", x=0, y=20, width=160, height=1,
            origin=7, extent=40)
    assert P.handle_mouse(instance, 20, 10, "press")
    assert P.handle_mouse(instance, 23, 10, "motion")
    assert P.handle_mouse(instance, 23, 10, "release")
    assert instance.pane_drag_state["focus"] == "workspace:jobs"
    assert not P.handle_mouse(instance, 26, 10, "wheel-down")
    assert instance.pane_drag_state["focus"] is None
    assert not P.handle_key(instance, "up")


def test_content_blur_keeps_an_active_drag_owner_and_shutdown_cancel_restores():
    instance = app()
    divider(instance)
    P.handle_mouse(instance, 12, 95, "press")
    P.handle_mouse(instance, 12, 111, "motion")
    P.blur(instance)
    assert P.active(instance) and instance.pane_drag_state["focus"] == "workspace:jobs"
    P.cancel(instance)
    assert instance.value == 60 and not P.active(instance)
    assert instance.pane_drag_state["focus"] is None and instance.saves == 0


def test_unrelated_save_records_only_committed_size_and_keeps_live_preview():
    instance = app()
    divider(instance)
    P.handle_mouse(instance, 12, 95, "press")
    P.handle_mouse(instance, 12, 111, "motion")
    assert instance.value == 70
    with P.committed(instance):
        assert instance.value == 60
    assert instance.value == 70 and P.active(instance)
    with pytest.raises(RuntimeError):
        with P.committed(instance):
            raise RuntimeError("Preference writer failed")
    assert instance.value == 70 and instance.saves == 0


def test_drag_uses_original_setter_for_preview_and_restores_auxiliary_settings():
    instance = app(manual=False)
    original_value, original_manual = instance.value, instance.manual
    def setter(value):
        instance.value = value
        instance.manual = original_manual if value == original_value else True
    P.register(instance, "recent:jobs", "vertical", 95, 7, 1, 40, 0, 159,
               instance.value, setter, minimum=20, maximum=80)
    P.handle_mouse(instance, 12, 95, "press")
    P.handle_mouse(instance, 12, 111, "motion")
    assert instance.manual and instance.value == 70
    P.begin_frame(instance, 160, 48)
    # The newest frame's descriptor must not replace rollback semantics.
    P.register(instance, "recent:jobs", "vertical", 111, 7, 1, 40, 0, 159,
               instance.value, lambda value: setattr(instance, "value", value), minimum=20, maximum=80)
    with P.committed(instance):
        assert not instance.manual and instance.value == 60
    assert instance.manual and instance.value == 70
    P.handle_mouse(instance, 12, 95, "release")
    assert not instance.manual and instance.value == 60 and instance.saves == 0


@pytest.mark.parametrize("ascii_", [False, True])
def test_vertical_line_has_one_centered_diamond_and_preserves_side_cells(ascii_):
    instance = app(width=16, height=15, body_origin=3)
    divider(instance, x=7, y=3, height=11, extent=15)
    canvas = [[("abcdefg", "green"), (" ", ""), ("ijklmnop", "yellow")]
              for _ in range(11)]
    P.paint(canvas, instance, "workspace:jobs", origin_y=3, ascii_=ascii_)
    marker, line = ("*", "|") if ascii_ else ("◆", "│")
    assert [L.row_text(row)[7] for row in canvas] == [line] * 5 + [marker] + [line] * 5
    assert all(L.row_text(row)[:7] == "abcdefg" and L.row_text(row)[8:] == "ijklmnop" for row in canvas)
    assert all(L.vlen(L.row_text(row)) == 16 for row in canvas)
    assert canvas[5][1] == (marker, "accent+bold+bg:canvas")
    if ascii_:
        assert L.to_text(canvas, 16).isascii()


def test_partial_vertical_and_horizontal_dividers_do_not_have_diamonds():
    instance = app(width=16, height=15, body_origin=3)
    divider(instance, x=7, y=5, height=7, extent=15)
    canvas = [[(" " * 16, "")] for _ in range(11)]
    P.paint(canvas, instance, "workspace:jobs", origin_y=3)
    assert "◆" not in L.to_text(canvas, 16)
    divider(instance, key="recent", axis="horizontal", x=1, y=8, width=14, height=1,
            origin=3, extent=10)
    P.paint(canvas, instance, "recent", origin_y=3)
    assert L.row_text(canvas[5]) == " " + "─" * 14 + " "


def test_exact_semantic_center_hit_has_no_live_callback_in_action():
    instance = app()
    divider(instance)
    row, kind, descriptor = P.control_hit(instance, "workspace:jobs", origin_y=7)
    assert row == 19 and kind == "control"
    assert descriptor["left"] == 95 and descriptor["right"] == 96
    assert descriptor["action"] == ("command", "pane-focus workspace:jobs")
    assert descriptor["id"] == "pane:workspace:jobs"


def test_invalid_or_overflowing_registry_geometry_cannot_expand_canvas():
    instance = app(width=16, height=15, body_origin=3)
    assert divider(instance, x=100) is None
    assert divider(instance, x=7, y=100) is None
    assert divider(instance, width=True) is None
    registered = divider(instance, axis="horizontal", x=7, y=8, width=100, height=100)
    assert registered.width == 9 and registered.height == 7
    canvas = [[(" " * 10, "")] for _ in range(10)]
    P.paint(canvas, instance, registered.key, origin_y=3)
    assert all(L.vlen(L.row_text(row)) == 10 for row in canvas)


def test_divider_replacement_preserves_combining_and_wide_text_on_both_sides():
    instance = app(width=12, height=8, body_origin=3)
    divider(instance, x=5, y=3, height=4, extent=11)
    canvas = [[("a\u0301界cd 界efgh", "cyan")]]
    P.paint(canvas, instance, "workspace:jobs", origin_y=3)
    text = L.row_text(canvas[0])
    assert text == "a\u0301界cd│界efgh"
    assert L.vlen(text) == 12


def test_resized_metadata_wrap_counts_combining_marks_as_zero_cells():
    rows = W._wrap_row([("a\u0301b\u0301c\u0301d\u0301界 xyz", "cyan")], 6)
    assert L.row_text(rows[0]) == "a\u0301b\u0301c\u0301d\u0301界"
    assert all(L.vlen(L.row_text(row)) <= 6 for row in rows)


def test_workspace_divider_keeps_jobs_hits_exact_and_adapts_to_stacked_layout():
    instance = app(cfg={"workspace": {"density": "comfortable"}}, selected_id="12",
                   interaction_state={})
    job_panels.initialize(instance)
    groups = {"main": {"rows": [[(" 12 running", "rev")]], "hits": [(0, "job", "12")]},
              "details": {"rows": [[(" Evidence", "")]], "hits": []}}
    rows, hits = W.transform_body(instance, [], [], 160, 40, groups=groups)
    record = instance.pane_drag_state["dividers"]["workspace:jobs"]
    assert record.axis == "vertical" and record.full_vertical
    assert sum("◆" in L.row_text(row) for row in rows) == 1
    job_y = next(y for y, kind, identifier in hits if kind == "job" and identifier == "12")
    assert "12 running" in L.row_text(rows[job_y])
    assert instance.workspace_main_usable_height == 37
    instance.width, instance.height, instance.body_origin = 80, 20, 3
    P.begin_frame(instance, 80, 20)
    rows, hits = W.transform_body(instance, [], [], 80, 16, groups=groups)
    record = instance.pane_drag_state["dividers"]["workspace:jobs"]
    assert record.axis == "horizontal" and not record.full_vertical
    assert "◆" not in L.to_text(rows, 80)
    assert all(L.vlen(L.row_text(row)) <= 80 for row in rows)


def test_main_budget_is_available_before_native_jobs_renderer_runs():
    instance = app(cfg={"workspace": {}}, job_panel_state={"mode": "off"})
    observed = []
    def renderer(width, height):
        observed.append((instance.workspace_main_rect, instance.workspace_main_usable_height))
        return [[(" jobs", "")]], []
    views = SimpleNamespace(g=L.Glyphs(True))
    # Maximizing Main intentionally avoids invoking the actual Details renderer.
    W.initialize(instance).maximized = True
    W.render_body(views, {}, instance, 160, 40, None, renderer)
    assert len(observed) == 1 and observed[0][0].height == 40 and observed[0][1] == 39


@pytest.mark.parametrize("trigger", ["command", "next", "f6", "ctrl-w", "ctrl_w"])
def test_explicit_main_focus_releases_nested_controls_without_changing_details_mode(trigger):
    instance = app(job_panel_state={"mode": "off", "focus": "tabs"},
                   interaction_state={"active": True, "focused": "job_panel_tab:off", "pending_focus": True},
                   history_browser_state={"focused": True})
    state = W.initialize(instance)
    state.focus = "details"
    state.available = ("main", "details")
    divider(instance)
    P.run_command(instance, ["pane-focus", "workspace:jobs"])
    if trigger == "command":
        assert W.run_command(instance, ["focus", "main"])
    elif trigger == "next":
        assert W.run_command(instance, ["focus", "next"])
    else:
        assert W.handle_key(instance, trigger)
    assert state.focus == "main"
    assert instance.job_panel_state == {"mode": "off", "focus": ""}
    assert instance.interaction_state["active"] is False and instance.interaction_state["focused"] is None
    assert instance.interaction_state["pending_focus"] is None
    assert instance.history_browser_state["focused"] is False
    assert instance.pane_drag_state["focus"] is None
    # Main content navigation is now delegated to the existing job controller.
    assert not job_panels.handle_key(instance, "home")
    assert not P.handle_key(instance, "up")


def test_native_focus_main_restores_job_home_after_jobpanel_off():
    from raven.config import Config
    from raven.controller import App
    from raven.model import Job, Store
    from raven.views import Views
    cfg = Config()
    store = Store(persist=False)
    store.jobs = [Job("900", "first experiment", "cpu", "RUNNING"),
                  Job("901", "second experiment", "cpu", "RUNNING")]
    instance = App(store, None, None, cfg, "test", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    instance.views_ref = views
    views.compose(store.snapshot(), instance, 160, 40)
    instance.cursor["jobs"] = 1
    instance.sync_selection()
    instance.run_command("jobpanel off")
    views.compose(store.snapshot(), instance, 160, 40)
    assert instance.selected_id == "901" and instance.job_panel_state["focus"] == "tabs"
    instance.run_command("focus main")
    instance.handle("home")
    assert instance.selected_id == "900" and instance.cursor["jobs"] == 0
    assert instance.job_panel_state["mode"] == "off" and instance.job_panel_state["focus"] == ""


def test_divider_registry_and_pointer_work_are_bounded_and_have_no_io(monkeypatch):
    instance = app()
    for index in range(P.MAX_DIVIDERS + 100):
        divider(instance, key=f"split:{index}")
    assert len(instance.pane_drag_state["dividers"]) == P.MAX_DIVIDERS
    def forbidden(*args, **kwargs):
        raise AssertionError("Pointer capture must not read files or save session state")
    monkeypatch.setattr("builtins.open", forbidden)
    instance.save = forbidden
    assert P.handle_mouse(instance, 12, 95, "press")
    for x in range(10, 150):
        assert P.handle_mouse(instance, 12, x, "motion")
    assert P.handle_key(instance, "esc")
    assert instance.value == 60


@pytest.mark.parametrize("width,height", [(110, 8), (160, 40), (80, 8), (48, 12)])
def test_split_minimums_hold_at_all_drag_percentages(width, height):
    instance = app(job_panel_state={}, cfg={"workspace": {"density": "comfortable"}})
    for ratio in range(0, 101):
        W.initialize(instance).ratio = ratio
        rects = W.geometry(instance, width, height)
        if len(rects) == 2:
            if width >= 110:
                assert all(rect.width >= 24 for rect in rects.values())
            else:
                assert all(rect.height >= 3 for rect in rects.values())


def test_fitted_divider_keeps_suffix_styles_and_discards_replaced_edge_marks():
    row = [("x", "text"), ("\u0301", "text"), ("yz", "accent"),
           ("", "empty"), ("\u0301", "text")]
    assert P._replace(row, 0, "│", "border", 3, fitted=True) == [
        ("│", "border"), ("yz", "accent")]


def test_fitted_divider_preserves_combining_marks_inside_a_suffix_segment():
    row = [("b▁", "text"), ("é▁b\u0301 ", "accent"), ("", "empty")]
    assert P._replace(row, 0, "│", "border", 6, fitted=True) == [
        ("│", "border"), ("▁", "text"), ("é▁b\u0301 ", "accent")]
