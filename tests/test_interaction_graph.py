"""Painted-cell interactions remain cosmetic until explicit activation."""
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from raven import interaction as ui, layout as L


class App:
    def __init__(self, mode="main", tab="jobs"):
        self.mode, self.tab = mode, tab
        self.width, self.height = 60, 12
        self.selected_id = "7"
        self.last_hits, self.tab_hits = [], []
        self.calls = []
        from raven.workbench import bind_actions
        bind_actions(self)

    def click(self, y, x, hits, **kwargs):
        assert not ui.handle_mouse(self, y, x)
        self.calls.append(("click", y, x))

    def handle(self, key):
        assert not ui.handle_key(self, key)
        self.calls.append(("key", key))

    def run_command(self, line):
        self.calls.append(("command", line))

    def say(self, text):
        self.calls.append(("say", text))

    def fail(self, text):
        self.calls.append(("fail", text))


def button(identity, y, left, right, *, action=None, group="test", disabled=False):
    return (y, "control", {"id": identity, "left": left, "right": right, "group": group,
                           "action": action or ("command", "test " + identity), "disabled": disabled})


def paint(app, hits=(), overlays=None, extra=()):
    rows = [[("x" + " " * 59, "dim")]] * 12
    app.last_hits = list(hits)
    return ui.publish(app, rows, app.last_hits, 60, 12, overlays, extra)


def test_graph_is_immutable_and_copies_action_and_descriptor_data():
    app = App()
    hit = button("one", 2, 3, 10, action=["command", "view job"])
    graph = paint(app, [hit])
    hit[2]["action"][1] = "cancel 7"
    hit[2]["left"] = 40
    assert graph.controls[0].action == ("command", "view job")
    assert graph.controls[0].rect == ui.Rect(2, 3, 3, 10)
    with pytest.raises(FrozenInstanceError):
        graph.controls[0].label = "changed"
    assert isinstance(graph.controls, tuple)


def test_explicit_control_instances_are_normalized_and_do_not_retain_mutable_actions():
    app = App()
    action = ["command", "view job"]
    control = ui.Control("direct", "Direct", ui.Rect(2, 3, 3, 10), action)
    graph = paint(app, extra=[control])
    action[1] = "cancel 7"
    assert graph.get("direct").action == ("command", "view job")
    invalid = ui.Control("invalid", "Invalid", ui.Rect("two", 3, 3, 10), ("command", "view job"))
    assert not paint(app, extra=[invalid]).controls


def test_partial_state_from_an_embedder_is_completed_without_losing_its_pointer():
    app = App()
    app.interaction_state = {"pointer": (2, 5)}
    graph = paint(app, [button("one", 2, 3, 10)])
    assert graph.get("one") and ui.initialize(app)["hovered"] == "one"


@pytest.mark.parametrize("button_type", ["motion", "drag", "press", "release"])
def test_passive_pointer_never_activates_or_changes_log_job_selection(button_type):
    app = App()
    app.logs = SimpleNamespace(cursor=15, selection_active=True, path="saved.log", browser=False)
    paint(app, [button("inspect", 2, 3, 10)])
    assert not ui.handle_mouse(app, 2, 6, button_type)
    assert ui.initialize(app)["hovered"] == "inspect"
    assert app.selected_id == "7" and app.logs.cursor == 15
    assert app.logs.selection_active and not app.calls


def test_hover_motion_outside_releases_feedback_without_entering_focus():
    app = App()
    paint(app, [button("one", 2, 3, 10)])
    ui.handle_mouse(app, 2, 3, "motion")
    assert not ui.initialize(app)["active"]
    ui.handle_mouse(app, 2, 10, "motion")
    assert ui.initialize(app)["hovered"] is None


def test_explicit_mouse_click_activates_once_and_retains_arrow_focus():
    app = App()
    paint(app, [button("left", 2, 3, 10), button("right", 2, 15, 22)])
    assert ui.handle_mouse(app, 2, 8)
    assert app.calls == [("command", "test left")]
    assert ui.handle_key(app, "right")
    assert ui.initialize(app)["focused"] == "right"
    assert app.calls == [("command", "test left")]
    assert ui.handle_key(app, "enter")
    assert app.calls[-1] == ("command", "test right")


@pytest.mark.parametrize("key", ["up", "down", "left", "right", "enter", "space", "tab", "home", "end"])
def test_content_keys_unchanged_before_explicit_focus(key):
    app = App()
    paint(app, [button("one", 2, 3, 10)])
    assert not ui.handle_key(app, key)
    assert not app.calls


@pytest.mark.parametrize("exit_button", ["wheel-up", "wheel-down", "wheel_up", "wheel_down", "left"])
def test_scrolling_or_content_click_restores_content_keys(exit_button):
    app = App()
    paint(app, [button("one", 2, 3, 10)])
    ui.handle_key(app, "f8")
    assert not ui.handle_mouse(app, 8, 40, exit_button)
    assert not ui.initialize(app)["active"]
    assert not ui.handle_key(app, "up")


@pytest.mark.parametrize("key", ["esc", "f8"])
def test_focus_can_exit_without_underlying_escape_action(key):
    app = App()
    paint(app, [button("one", 2, 3, 10)])
    ui.handle_key(app, "f8")
    assert ui.handle_key(app, key)
    assert not ui.initialize(app)["active"] and not app.calls


def test_typing_a_content_shortcut_exits_focus_and_falls_through():
    app = App()
    paint(app, [button("one", 2, 3, 10)])
    ui.handle_key(app, "f8")
    assert not ui.handle_key(app, "l")
    assert not ui.initialize(app)["active"]


def test_directional_edges_prefer_aligned_controls_over_diagonal_shortcuts():
    app = App()
    graph = paint(app, [button("start", 2, 3, 10), button("diagonal", 3, 11, 13),
                        button("aligned", 2, 18, 24)])
    assert ui.nearest(graph, graph.get("start"), "right").id == "aligned"
    assert ui.nearest(graph, graph.get("aligned"), "left").id == "start"
    assert ui.nearest(graph, graph.get("start"), "up") is None


def test_directional_edges_prefer_same_group_among_aligned_choices():
    app = App()
    graph = paint(app, [button("start", 2, 3, 10), button("other", 4, 3, 10, group="other"),
                        button("same", 5, 3, 10)])
    assert ui.nearest(graph, graph.get("start"), "down").id == "same"


@pytest.mark.parametrize("disabled", [True, False])
def test_disabled_controls_are_hoverable_but_skipped_by_arrows(disabled):
    app = App()
    graph = paint(app, [button("start", 2, 3, 10), button("middle", 2, 11, 15, disabled=disabled),
                        button("last", 2, 20, 24)])
    assert ui.nearest(graph, graph.get("start"), "right").id == ("last" if disabled else "middle")
    ui.handle_mouse(app, 2, 12, "motion")
    assert ui.initialize(app)["hovered"] == "middle"


def test_ordered_navigation_home_end_and_tab_wrap_are_screen_ordered():
    app = App()
    paint(app, [button("last", 8, 30, 40), button("first", 2, 3, 10), button("middle", 4, 10, 20)])
    ui.handle_key(app, "f8")
    ui.handle_key(app, "home")
    assert ui.initialize(app)["focused"] == "first"
    ui.handle_key(app, "tab")
    assert ui.initialize(app)["focused"] == "middle"
    ui.handle_key(app, "end")
    assert ui.initialize(app)["focused"] == "last"
    ui.handle_key(app, "tab")
    assert ui.initialize(app)["focused"] == "first"
    ui.handle_key(app, "btab")
    assert ui.initialize(app)["focused"] == "last"


@pytest.mark.parametrize("mutate", [lambda app: setattr(app, "tab", "history"),
                                    lambda app: setattr(app, "mode", "confirm"),
                                    lambda app: setattr(app, "selected_id", "8"),
                                    lambda app: setattr(app, "width", 70),
                                    lambda app: setattr(app, "height", 15)])
def test_context_or_resize_change_rejects_stale_click_and_keyboard_activation(mutate):
    app = App()
    paint(app, [button("one", 2, 3, 10)])
    ui.handle_key(app, "f8")
    mutate(app)
    assert not ui.handle_mouse(app, 2, 3)
    assert not ui.handle_key(app, "enter")
    assert not app.calls and ui.controls(app) == ()


def test_changed_confirmation_scope_rejects_old_confirm_geometry():
    app = App(mode="confirm")
    app.confirm = {"jobs": ["7"]}
    app.command_state = {"confirm_hits": [(3, 10, 20, "confirm")]}
    paint(app)
    app.confirm = {"jobs": ["8"]}
    assert not ui.handle_mouse(app, 3, 15)
    assert not app.calls


def test_publish_preserves_semantic_focus_after_reflow_and_clears_removed_control():
    app = App()
    paint(app, [button("one", 2, 3, 10)])
    ui.handle_key(app, "f8")
    paint(app, [button("one", 8, 30, 40)])
    assert ui.initialize(app)["focused"] == "one" and ui.initialize(app)["active"]
    ui.handle_key(app, "enter")
    assert app.calls == [("command", "test one")]
    paint(app, [])
    assert ui.initialize(app)["focused"] is None and not ui.initialize(app)["active"]


def test_explicit_focus_command_is_deferred_until_next_published_frame():
    app = App()
    paint(app, [button("old", 2, 3, 10)])
    app.mode = "palette"
    assert ui.run_command(app, ["focusbuttons", "on"])
    assert ui.initialize(app)["pending_focus"] is True
    app.mode = "main"
    paint(app, [button("new", 2, 3, 10)])
    assert ui.initialize(app)["active"] and ui.initialize(app)["focused"] == "new"
    assert not any(call[0] == "command" for call in app.calls)


def test_off_command_cancels_deferred_focus():
    app = App()
    ui.run_command(app, ["focusbuttons", "on"])
    ui.run_command(app, ["focusbuttons", "off"])
    paint(app, [button("one", 2, 3, 10)])
    assert not ui.initialize(app)["active"]


@pytest.mark.parametrize("args", [["focusbuttons", "maybe"], ["focusbuttons", "on", "extra"]])
def test_invalid_focus_command_is_bounded_and_explains_usage(args):
    app = App()
    assert ui.run_command(app, args)
    assert app.calls == [("fail", "focusbuttons [on|off]")]
    assert not ui.run_command(app, ["unrelated"])


def test_data_rows_keep_native_single_click_behavior_and_keyboard_enter_activates():
    app = App()
    graph = paint(app, [(4, "job", "7")])
    assert not graph.controls[0].button
    assert not ui.handle_mouse(app, 4, 5)
    ui.handle_key(app, "f8")
    assert ui.handle_key(app, "enter")
    assert app.calls == [("click", 4, 0), ("key", "enter")]


@pytest.mark.parametrize("kind", ["sort_header", "node_cell", "job_panel_tab", "job_panel_file",
                                  "job_panel_view", "job_panel_action"])
def test_existing_bounded_hit_adapters_have_exact_half_open_cells(kind):
    app = App()
    payload = ("jobs", "id", 3, 10) if kind == "sort_header" else ("target", 3, 10)
    graph = paint(app, [(4, kind, payload)])
    assert graph.at(4, 3) and graph.at(4, 9)
    assert graph.at(4, 2) is None and graph.at(4, 10) is None
    if kind != "node_cell":
        assert ui.handle_mouse(app, 4, 5)
        assert app.calls == [("click", 4, 3)]


def test_normal_job_row_feedback_does_not_cross_into_details_pane():
    app = App()
    app.job_panel_rect = SimpleNamespace(x=30, y=2, width=30, height=9)
    graph = paint(app, [(4, "job", "7")])
    assert graph.at(4, 28) and graph.at(4, 30) is None


def test_tabs_and_filter_chips_are_adapted_with_semantic_identity():
    app = App()
    app.tab_hits = [(3, 2, 10, "jobs"), (3, 11, 21, "history")]
    app.table_chip_y, app.table_chip_cells = 4, ("jobs", [("state", 3, 15)])
    graph = paint(app)
    assert [control.id for control in graph.controls] == ["tab:jobs", "tab:history", "filter-chip:jobs:state"]


def test_toolbar_track_is_one_focus_stop_and_menu_disabled_reason_is_preserved():
    app = App()
    app.toolbar_state = {"hits": [(0, 2, 8, "menu", 0), (0, 35, 36, "track", None),
                                   (0, 36, 37, "track", None), (0, 37, 38, "track", None)],
                         "menu": 0, "menu_hits": [(2, 3, 20, "blocked")],
                         "menu_disabled": {"blocked": "Select a job first."}}
    graph = paint(app)
    assert len([control for control in graph.controls if control.id == "toolbar:track"]) == 1
    assert graph.get("toolbar:track").rect == ui.Rect(0, 35, 1, 38)
    assert ui.handle_mouse(app, 2, 10)
    assert app.calls == [("say", "Select a job first.")]


@pytest.mark.parametrize("mode,state_name", [("analysis", "analysis_state"), ("project_runs", "project_state"),
                                            ("project_outputs", "project_state"), ("project_preview", "project_state"),
                                            ("execution", "execution_state")])
def test_modal_explicit_controls_are_visible_and_hidden_page_hits_excluded(mode, state_name):
    app = App(mode)
    setattr(app, state_name, {"control_hits": [button("modal", 4, 10, 20)]})
    graph = paint(app, [button("hidden", 6, 3, 10)])
    assert graph.get("modal") and graph.get("hidden") is None


def test_confirm_and_table_tools_precise_modal_registries_dispatch_existing_controller():
    app = App(mode="confirm")
    app.command_state = {"confirm_hits": [(4, 12, 24, "cancel")]}
    graph = paint(app)
    assert graph.get("confirm:cancel")
    assert ui.handle_mouse(app, 4, 16)
    assert app.calls == [("click", 4, 12)]
    app.mode = "table_tools"
    app.table_tools_state = {"modal": "headers", "hits": [(5, 10, 30, 1)]}
    graph = paint(app)
    assert graph.get("table-tools:headers:1")


@pytest.mark.parametrize("mode,state_name,registry", [
    ("palette", "command_state", "result_hits"),
    ("jump_picker", "navigation_tools_state", "hits"),
    ("locations_picker", "navigation_tools_state", "hits"),
    ("settings_editor", "navigation_tools_state", "hits"),
    ("bindings_editor", "navigation_tools_state", "hits"),
])
def test_modal_row_registry_adapters_use_painted_overlay_bounds(mode, state_name, registry):
    app = App(mode)
    setattr(app, state_name, {registry: [(4, 2)]})
    overlay = [(4, 10, [("| Item text         |", "")])]
    graph = paint(app, overlays=overlay)
    assert len(graph.controls) == 1
    assert graph.controls[0].rect == ui.Rect(4, 11, 5, 30)


@pytest.mark.parametrize("mode,state_name", [("session_inbox", "session_tools_state"),
                                            ("log_tools_results", "log_tools_state"),
                                            ("log_tools_marks", "log_tools_state")])
def test_exact_mouse_row_registry_adapters(mode, state_name):
    app = App(mode)
    setattr(app, state_name, {"mouse_rows": {4: ("item", 10, 30)}})
    graph = paint(app)
    assert len(graph.controls) == 1 and graph.controls[0].rect == ui.Rect(4, 10, 5, 30)


def test_dropdown_obscures_underlying_controls_but_retains_toolbar():
    app = App()
    app.toolbar_state = {"hits": [(0, 2, 8, "menu", 0)], "menu": 0, "menu_hits": []}
    overlay = [(2, 1, [(" [ dropdown ] ", "")])]
    graph = paint(app, [button("hidden", 2, 3, 10), button("visible", 5, 3, 10)], overlay)
    assert graph.get("hidden") is None and graph.get("visible") and graph.get("toolbar:menu:0")


def test_toolbar_dropdown_masks_modal_controls_without_masking_the_modal_itself():
    app = App(mode="analysis")
    app.analysis_state = {"control_hits": [button("hidden-modal", 4, 10, 20), button("visible-modal", 10, 10, 20)]}
    app.toolbar_state = {"hits": [(0, 2, 8, "menu", 0)], "menu": 0,
                         "menu_rect": (1, 0, 8, 30), "menu_hits": [(4, 3, 20, "entry")]}
    graph = paint(app, overlays=[(4, 0, [("Dropdown" + " " * 22, "")])])
    assert graph.get("hidden-modal") is None
    assert graph.get("visible-modal")
    assert graph.get("toolbar:menu:0") and graph.get("toolbar:menu:0:entry")
    app.toolbar_state.update(menu=None, menu_rect=None, menu_hits=[])
    graph = paint(app, overlays=[(4, 8, [("| Own modal body |", "")])])
    assert graph.get("hidden-modal")  # Its own overlay is not an occluding layer.


@pytest.mark.parametrize("text", ["  alpha beta", "  αλφα βήτα", "  工作者e\u0301", "  e\u0301界😀"])
def test_feedback_preserves_exact_unicode_text_cell_width_and_combining_styles(text):
    app = App()
    rows = [[(text, "cyan+dim")]]
    hits = [button("one", 0, 2, 8)]
    ui.publish(app, rows, hits, 60, 12)
    ui.handle_mouse(app, 0, 3, "motion")
    painted = ui.decorate(app, rows)
    assert L.row_text(painted[0]) == text
    assert L.vlen(L.row_text(painted[0])) == L.vlen(text)
    assert rows == [[(text, "cyan+dim")]]
    assert any("under" in style for _, style in painted[0])
    for segment, style in painted[0]:
        assert not segment.startswith("\u0301")


def test_overlay_feedback_uses_absolute_origin_and_does_not_change_painter_content():
    app = App(mode="analysis")
    app.analysis_state = {"control_hits": [button("section", 4, 11, 17)]}
    overlay = [(4, 10, [("| button |", "dim")])]
    paint(app, overlays=overlay)
    ui.handle_mouse(app, 4, 12, "motion")
    output = ui.decorate_overlays(app, overlay)
    assert output[0][:2] == (4, 10)
    assert L.row_text(output[0][2]) == "| button |"
    assert any("under" in style for _, style in output[0][2])
    assert ui.decorate_overlays(app, None) is None


def test_control_clipping_limits_mouse_and_activation_coordinates():
    app = App()
    graph = paint(app, [(2, "job_panel_tab", ("inspect", -5, 8)), button("outside", 20, 0, 10)])
    assert len(graph.controls) == 1 and graph.controls[0].rect == ui.Rect(2, 0, 3, 8)
    assert ui.handle_mouse(app, 2, 0)
    assert app.calls == [("click", 2, 0)]


@pytest.mark.parametrize("action", [("callable", lambda: None), ("command", {"text": "cancel"}), (), None])
def test_untrusted_or_non_scalar_action_descriptors_are_ignored(action):
    app = App()
    hit = button("invalid", 2, 3, 10)
    hit[2]["action"] = action
    assert not paint(app, [hit]).controls


def test_duplicate_controls_and_unbounded_input_are_capped():
    app = App()
    def many():
        for index in range(ui.MAX_CONTROLS * 4):
            yield button(str(index // 2), 2, 3, 10)
    graph = paint(app, many())
    assert len(graph.controls) == ui.MAX_CONTROLS // 2
    assert len({control.id for control in graph.controls}) == len(graph.controls)


def test_key_action_bypasses_graph_routing_instead_of_reactivating_itself():
    app = App()
    paint(app, [button("back", 2, 3, 10, action=("key", "esc"))])
    assert ui.handle_mouse(app, 2, 6)
    assert app.calls == [("key", "esc")]


def test_unsupported_setting_action_cannot_change_arbitrary_controller_attributes():
    app = App()
    paint(app, [button("bad", 2, 3, 10, action=("set", "selected_id", "8"))])
    assert not ui.handle_mouse(app, 2, 6)
    assert app.selected_id == "7" and not app.calls


def test_graph_publish_and_traversal_never_schedule_or_read_research_services():
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError("Graph accessed service " + name)
    app = App()
    app.research = app.sampler = app.store = app.files = Forbidden()
    paint(app, [button("one", 2, 3, 10), button("two", 2, 15, 20)])
    ui.handle_mouse(app, 2, 6, "motion")
    ui.handle_key(app, "f8")
    ui.handle_key(app, "right")
    ui.decorate(app, [[(" Buttons ", "")]])
    assert not app.calls


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width", [40, 80, 160])
@pytest.mark.parametrize("tab,prefix,attribute", [("nodes", "nodes-view:", "nodes_view"),
                                               ("analytics", "analytics-view:", "analytics_view"),
                                               ("research", "research-view:", "research_view")])
def test_actual_controller_all_secondary_view_buttons_mouse_activate_and_keep_focus(ascii_, width, tab, prefix, attribute):
    from raven.config import Config
    from raven.controller import App as RavenApp
    from raven.model import Job, Store
    from raven.remote import LocalFiles
    from raven.views import Views
    from raven.controller import ANALYTICS_VIEWS, NODES_VIEWS
    from raven.research import RESEARCH_VIEWS

    cfg = Config()
    store = Store(persist=False)
    store.jobs = [Job("7", "sample", "cpu", "RUNNING")]
    app = RavenApp(store, None, None, cfg, "test", interactive=False)
    views = Views(L.Glyphs(ascii_), cfg, files=LocalFiles())
    app.views_ref = views
    app.enter_tab(tab)
    choices = dict({"nodes": NODES_VIEWS, "analytics": ANALYTICS_VIEWS, "research": RESEARCH_VIEWS}[tab])
    if tab == "research":
        # Experienced users can pin every workspace and retain direct buttons.
        from raven import toolbar
        toolbar.restore(app, {"favorites": ["research:" + key for key in choices]})
    try:
        for key in choices:
            views.compose(store.snapshot(), app, width, 60)
            control = next(control for control in ui.controls(app) if control.id == prefix + key)
            app.click(control.rect.top, control.rect.left, app.last_hits)
            assert getattr(app, attribute) == key
            assert app.tab == tab
            views.compose(store.snapshot(), app, width, 60)
            assert ui.initialize(app)["active"]
            assert ui.initialize(app)["focused"] == prefix + key
    finally:
        if app.research:
            app.research.close()


def test_actual_command_palette_focus_request_enters_next_frame_without_second_gesture():
    from raven.config import Config
    from raven.controller import App as RavenApp
    from raven.model import Store
    from raven.remote import LocalFiles
    from raven.views import Views

    cfg = Config()
    store = Store(persist=False)
    app = RavenApp(store, None, None, cfg, "test", interactive=False)
    views = Views(L.Glyphs(False), cfg, files=LocalFiles())
    app.views_ref = views
    views.compose(store.snapshot(), app, 120, 32)
    app.handle(":")
    views.overlay(store.snapshot(), app, 120, 32)
    for key in "focusbuttons on":
        app.handle(key if key != " " else "space")
    app.handle("enter")
    assert app.mode == "main"
    views.compose(store.snapshot(), app, 120, 32)
    assert ui.initialize(app)["active"]


def test_inline_control_identity_is_stable_when_its_nested_hit_geometry_changes():
    app = App()
    payload = ("control", {"id": "research-view:workflow", "left": 2, "right": 8})
    paint(app, [(4, "job_panel_action", (payload, 25, 35))])
    ui.handle_mouse(app, 4, 30)
    payload = ("control", {"id": "research-view:workflow", "left": 4, "right": 10})
    graph = paint(app, [(5, "job_panel_action", (payload, 30, 40))])
    assert graph.controls[0].id == "job_panel_action:control:research-view:workflow"
    assert ui.initialize(app)["active"]


def test_combining_mark_on_first_highlighted_grapheme_keeps_its_base_style():
    app = App()
    rows = [[("e\u0301 abc", "dim")]]
    ui.publish(app, rows, [button("one", 0, 0, 2)], 60, 12)
    ui.handle_mouse(app, 0, 0, "motion")
    output = ui.decorate(app, rows)
    assert output[0][0][0].startswith("e\u0301")
    assert "under" in output[0][0][1]


@pytest.mark.parametrize("text", [" selected job ", " 選択e\u0301界 "])
@pytest.mark.parametrize("active", [False, True])
def test_native_selected_row_retains_its_style_under_hover_and_keyboard_focus(text, active):
    app = App()
    source_style = "cyan+bg:surface+sel"
    rows = [[(text, source_style)], [(" ordinary job ", "cyan")]]
    ui.publish(app, rows, [button("selected", 0, 1, 8), button("ordinary", 1, 1, 8)], 60, 12)
    ui.handle_mouse(app, 0, 3, "motion")
    ui.initialize(app).update(active=active, focused="selected")
    painted = ui.decorate(app, rows)
    assert L.row_text(painted[0]) == text
    accented = [style for _, style in painted[0] if "under" in style.split("+")]
    assert accented
    for style in accented:
        assert set(source_style.split("+")) <= set(style.split("+"))
        assert "bold" in style.split("+")
        assert "bg:surface-raised" not in style.split("+")
        assert "bg:panel" not in style.split("+")
    assert rows[0] == [(text, source_style)]
    ui.handle_mouse(app, 1, 3, "motion")
    ui.initialize(app).update(active=False)
    painted = ui.decorate(app, rows)
    assert painted[0] == rows[0]
    assert any("under" in style for _, style in painted[1])


@pytest.mark.parametrize("text", [" selected content ", " 選択e\u0301界 "])
@pytest.mark.parametrize("active", [False, True])
def test_explicit_yank_rows_keep_selection_and_orange_marker_under_hover_and_focus(text, active):
    app = App()
    selected = [(text, "sel"), ("◆", "fg:#fb923c+bold")]
    ordinary = [(" ordinary button ", "cyan+dim")]
    rows = [[("header", "")], selected, ordinary]
    ui.publish(app, rows, [button("selected", 1, 1, 8), button("ordinary", 2, 1, 8)], 60, 12)
    ui.handle_mouse(app, 1, 3, "motion")
    state = ui.initialize(app)
    state.update(active=active, focused="selected")
    app.sel_anchor = app.sel_end = 1
    painted = ui.decorate(app, rows)
    assert painted[1] is selected
    assert painted[1][-1] == ("◆", "fg:#fb923c+bold")
    assert L.row_text(painted[1]) == L.row_text(selected)
    # The cursor still provides feedback on controls beyond the yank range.
    ui.handle_mouse(app, 2, 3, "motion")
    painted = ui.decorate(app, rows)
    assert painted[1] is selected
    assert any("under" in style for _, style in painted[2])


def test_selected_screen_line_does_not_suppress_overlay_menu_feedback():
    app = App()
    app.sel_anchor = app.sel_end = 4
    overlay = [(4, 10, [("| button |", "dim")])]
    paint(app, overlays=overlay, extra=[{"id": "menu", "rect": (4, 11, 5, 17),
                                       "action": ("command", "test menu")}])
    ui.handle_mouse(app, 4, 12, "motion")
    painted = ui.decorate_overlays(app, overlay)
    assert any("under" in style for _, style in painted[0][2])
    assert L.row_text(painted[0][2]) == "| button |"


@pytest.mark.parametrize("tab", ["jobs", "history", "analytics", "research", "deps", "log", "nodes", "sources"])
@pytest.mark.parametrize("event", ["motion", "drag", "release", "press", "right", "wheel-up", "wheel-down"])
def test_passive_events_over_research_control_never_switch_page_or_job(tab, event):
    app = App(tab=tab)
    app.tab_hits = [(1, 25, 40, "research")]
    app.job_panel_state = {"mode": "analytics", "analytics_view": "job"}
    paint(app, [button("plot-control", 7, 25, 40, action=("set", "research_view", "workflow")),
                (7, "job", "7")])
    for _ in range(15):
        for y, x in ((7, 3), (7, 30), (1, 30), (8, 40)):
            assert not ui.handle_mouse(app, y, x, button=event)
    assert app.tab == tab and app.selected_id == "7"
    assert app.job_panel_state["mode"] == "analytics"
    assert not app.calls
    assert not ui.initialize(app)["active"]


@pytest.mark.parametrize("state_name,key,before,after", [
    ("analysis_state", "scroll", 0, 4),
    ("analysis_state", "section", 0, 1),
    ("analysis_state", "zoom", 1.0, 2.0),
    ("analysis_state", "pan", 0.0, .5),
    ("analysis_state", "window", None, 15.0),
    ("project_state", "run_top", 0, 5),
    ("project_state", "output_top", 0, 5),
    ("project_state", "preview_page", 0, 1),
    ("job_panel_state", "file_id", "out", "err"),
])
def test_document_changes_reject_old_control_geometry_before_repaint(state_name, key, before, after):
    app = App()
    setattr(app, state_name, {key: before})
    paint(app, [button("read-old-row", 4, 10, 20)])
    ui.handle_key(app, "f8")
    getattr(app, state_name)[key] = after
    assert ui.controls(app) == ()
    assert not ui.handle_mouse(app, 4, 15)
    assert not ui.handle_key(app, "enter")
    assert not app.calls


def test_jobs_details_log_scroll_rejects_previous_visible_action():
    app = App()
    app.job_panel_state = {"session": SimpleNamespace(path="job-7.log", top=10)}
    paint(app, [button("old-log-action", 4, 30, 45)])
    app.job_panel_state["session"].top = 20
    assert not ui.handle_mouse(app, 4, 35)
    assert not app.calls


@pytest.mark.parametrize("field,after", [("run_id", "run-b"), ("job_id", "8"),
                                         ("attempt", "retry"), ("project_root", "/other")])
def test_run_binding_changes_in_place_reject_previous_project_actions(field, after):
    app = App(tab="research")
    binding = {"project_root": "/project", "run_id": "run-a", "job_id": "7", "attempt": "first"}
    app.project_state = {"root": "/project", "binding": binding}
    paint(app, [button("old-report", 4, 10, 20)])
    binding[field] = after
    assert not ui.handle_mouse(app, 4, 15)
    assert not app.calls


def test_published_native_hit_token_copies_nested_actions_and_matches_mapping_order():
    app = App()
    action = ["command", "view research"]
    descriptor = {"id": "research", "left": 3, "right": 10, "action": action}
    hits = [(4, "control", descriptor)]
    paint(app, hits)
    state = ui.initialize(app)
    recorded = state["published_hit_token"]
    reordered = [(4, "control", dict(reversed(tuple(descriptor.items()))))]
    assert ui.hit_token(reordered) == recorded
    action[1] = "cancel 7"
    assert ui.hit_token(hits) != recorded
    assert state["published_hit_token"] == recorded
    assert ui.hit_token([(True, "control", descriptor)]) != ui.hit_token(hits)


def test_invalid_oversized_or_recursive_hit_payloads_fail_closed():
    assert ui.hit_token([(1, "bad", object())]) is None
    assert ui.hit_token([(1, "bad", list(range(129)))]) is None
    assert ui.hit_token([(1, "job", "7")] * (ui.MAX_CONTROLS + 1)) is None
    recursive = []
    recursive.append(recursive)
    assert ui.hit_token([(1, "bad", recursive)]) is None


def test_nested_registry_signature_is_not_recomputed_by_pointer_or_feedback(monkeypatch):
    app = App()
    paint(app, [button("control", 4, 10, 20)])

    def forbidden(*args, **kwargs):
        raise AssertionError("Passive pointer copied the native hit registry")

    monkeypatch.setattr(ui, "hit_token", forbidden)
    for y, x in ((4, 11), (5, 11), (4, 15), (8, 30)) * 10:
        assert not ui.handle_mouse(app, y, x, "motion")
        ui.feedback_rows(app)
        ui.decorate(app, [[("line", "")]] * 12)
