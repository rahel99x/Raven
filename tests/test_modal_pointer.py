"""Native gestures activate only the current painted legacy modal control."""

import curses

import pytest

from raven import interaction, layout as L, navigation_tools, navigation_ui, screen, workbench
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.ui import modal_pointer as M
from raven.ui.control_graph import Control, Rect
from raven.ui.focus import Frame, Owner
from raven.views import Views


@pytest.fixture
def dashboard():
    config = Config({"animations": False, "startup": {"enabled": False}})
    app = App(Store(persist=False), None, None, config, "student", interactive=False)
    app.width, app.height = 120, 30
    views = Views(L.Glyphs(True), config)
    app.views_ref = views
    yield app, views
    if app.research:
        app.research.close()


def publish(app, control=None):
    control = control or Control("exact", "Exact action", Rect(8, 20, 9, 40),
                                 ("command", "reviewed action"), "modal", layer=2)
    rows = [[(" " * app.width, "")] for _ in range(app.height)]
    interaction.publish(app, rows, (), app.width, app.height, overlays=((8, 0, [(" " * app.width, "")]),), extra_controls=(control,))
    return control


def paint(app, views):
    snapshot = app.store.snapshot()
    views.compose(snapshot, app, app.width, app.height)
    views.overlay(snapshot, app, app.width, app.height)


def frame(app, owner="analysis_ui", mode="analysis", callback=None):
    app.mode = mode
    calls = []
    receiver = callback or (lambda app, y, x, **kwargs: calls.append((y, x, kwargs)))
    return Frame(Owner(owner, lambda app, key: calls.append(("key", key)), receiver),
                 (mode, app.tab)), calls


@pytest.mark.parametrize("owner,mode", [(owner, mode) for owner, modes in M.LEGACY_MODES.items() for mode in sorted(modes)])
def test_each_explicit_legacy_owner_gets_exactly_one_aggregate_click(dashboard, owner, mode):
    app, _ = dashboard
    modal, calls = frame(app, owner, mode)
    publish(app)
    assert M.route(app, modal, 8, 22, button="press")
    assert not calls
    assert M.route(app, modal, 8, 22, button="release")
    assert calls == [(8, 22, {"button": "left", "shift": False})]
    assert M.continuation(app, "left")
    assert M.route(app, None, 8, 22, button="left")
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["mode", "geometry", "action", "position", "disabled", "native", "supplied-map"])
def test_release_rejects_changed_owner_semantics_or_painted_control(dashboard, change):
    app, _ = dashboard
    modal, calls = frame(app, "navigation_tools", "jump_picker")
    publish(app)
    M.route(app, modal, 8, 22, button="press")
    current, x = True, 22
    if change == "mode":
        app.mode = "main"
    elif change == "geometry":
        app.width += 1
    elif change == "action":
        publish(app, Control("exact", "Changed", Rect(8, 20, 9, 40), ("command", "new"), "modal", layer=2))
    elif change == "position":
        x = 80
    elif change == "disabled":
        publish(app, Control("exact", "Disabled", Rect(8, 20, 9, 40), ("command", "reviewed action"), "modal", enabled=False, layer=2))
    elif change == "native":
        app.navigation_tools_state["query"] = "different results"
    else:
        current = False
    assert M.route(app, modal, 8, x, button="release", current=current)
    assert not calls


def test_equivalent_repaint_keeps_semantic_control_without_requiring_same_graph_object(dashboard):
    app, _ = dashboard
    modal, calls = frame(app)
    publish(app)
    old = app.interaction_state["graph"]
    M.route(app, modal, 8, 22, button="press")
    another = Control("unrelated", "Elsewhere", Rect(10, 20, 11, 40), ("command", "unrelated"), "modal", layer=2)
    rows = [[(" " * app.width, "")] for _ in range(app.height)]
    target = old.get("exact")
    interaction.publish(app, rows, (), app.width, app.height, overlays=((8, 0, rows[8]),), extra_controls=(target, another))
    assert app.interaction_state["graph"] is not old
    M.route(app, modal, 8, 22, button="release")
    assert len(calls) == 1


def test_blank_press_drag_and_orphan_release_do_not_activate_button(dashboard):
    app, _ = dashboard
    modal, calls = frame(app)
    publish(app)
    M.route(app, modal, 3, 2, button="press")
    M.route(app, modal, 8, 22, button="drag")
    M.route(app, modal, 8, 22, button="release")
    M.route(app, modal, 8, 22, button="release")
    assert not calls


def test_aggregate_only_terminals_still_activate_and_unknown_owners_keep_native_handler(dashboard):
    app, _ = dashboard
    modal, calls = frame(app)
    publish(app)
    assert M.route(app, modal, 8, 22)
    assert len(calls) == 1
    modern = Frame(Owner("new_form"), ("analysis", "jobs"))
    assert not M.route(app, modern, 8, 22)


def test_existing_right_click_and_scroll_keys_preserved_and_cancel_old_capture(dashboard):
    app, _ = dashboard
    modal, calls = frame(app)
    publish(app)
    M.route(app, modal, 8, 22, button="press")
    M.route(app, modal, 8, 22, button="right")
    M.route(app, modal, 8, 22, button="release")
    M.route(app, modal, 8, 22, button="wheel-down")
    assert calls == [(8, 22, {"button": "right", "shift": False}), ("key", "down")]


def button(app, identity):
    return next(control for control in interaction.controls(app) if control.id == identity)


def native_click(app, target):
    y, x = target.rect.top, target.rect.left
    app.click(y, x, app.last_hits, button="press")
    app.click(y, x, app.last_hits, button="release")


def test_real_workspace_picker_native_click_opens_exact_workspace_and_absorbs_duplicate(dashboard):
    app, views = dashboard
    app.run_command("workspaces")
    paint(app, views)
    target = next(control for control in interaction.controls(app) if control.id.startswith("workspace-picker:") and control.id.endswith(":1"))
    expected = navigation_ui._workspaces(app)[1][0]
    native_click(app, target)
    assert (app.mode, app.tab, app.research_view) == ("main", "research", expected)
    assert workbench.route_modal_mouse(app, target.rect.top, target.rect.left, button="left")
    assert app.research_view == expected


@pytest.mark.parametrize("width", [40, 80])
@pytest.mark.parametrize("native_adapter", [False, True])
def test_modal_aggregate_after_repaint_cannot_activate_sidebar_under_old_menu(dashboard, width, native_adapter):
    app, views = dashboard
    app.width, app.height = width, 24
    app.run_command("workspaces")
    paint(app, views)
    target = next(item for item in interaction.controls(app) if item.id == "workspace-picker:1")
    expected = navigation_ui._workspaces(app)[1][0]
    if native_adapter:
        for mask in (curses.BUTTON1_PRESSED, curses.BUTTON1_RELEASED):
            screen._apply_input(app, ("mouse", (0, target.rect.left, target.rect.top, 0, mask)), app.last_hits, curses)
    else:
        native_click(app, target)
    assert app.research_view == expected and app.tab == "research"
    paint(app, views)
    if native_adapter:
        screen._apply_input(app, ("mouse", (0, target.rect.left, target.rect.top, 0, curses.BUTTON1_CLICKED)), app.last_hits, curses)
    else:
        app.click(target.rect.top, target.rect.left, app.last_hits, button="left")
    assert app.research_view == expected and app.tab == "research"


def test_settings_mouse_preview_cancel_and_apply_complete_without_keyboard_shortcuts(dashboard):
    app, views = dashboard
    original = app.theme
    app.run_command("settings")
    paint(app, views)
    native_click(app, button(app, "navigation-action:next"))
    assert app.theme != original and app.mode == "settings_editor"
    paint(app, views)
    native_click(app, button(app, "navigation-action:cancel"))
    assert app.mode == "main" and app.theme == original
    app.run_command("settings")
    paint(app, views)
    native_click(app, button(app, "navigation-action:next"))
    selected = app.theme
    paint(app, views)
    native_click(app, button(app, "navigation-action:apply"))
    assert app.mode == "main" and app.theme == selected
    assert app.navigation_tools_state["settings"]["theme"] == selected


def test_workspace_close_button_is_mouse_actionable(dashboard):
    app, views = dashboard
    app.run_command("workspaces")
    paint(app, views)
    native_click(app, button(app, "workspace-action:close"))
    assert app.mode == "main" and app.tab == "jobs"


def test_columns_native_mouse_toggle_uses_same_checked_column_and_persists(dashboard):
    app, views = dashboard
    app.run_command("columns")
    paint(app, views)
    target = next(control for control in interaction.controls(app) if control.id.startswith("column:") and control.label == "ELAPSED/LIMIT checkbox")
    native_click(app, target)
    assert "time" in app.table_state["hidden"].get("jobs", ())
    paint(app, views)
    native_click(app, button(app, target.id))
    assert "time" not in app.table_state["hidden"].get("jobs", ())


def test_actual_curses_press_release_dispatch_reaches_legacy_modal(dashboard):
    app, views = dashboard
    app.run_command("settings")
    paint(app, views)
    target = button(app, "navigation-action:next")
    original = app.theme
    for mask in (curses.BUTTON1_PRESSED, curses.BUTTON1_RELEASED, curses.BUTTON1_CLICKED):
        event = ("mouse", (0, target.rect.left, target.rect.top, 0, mask))
        screen._apply_input(app, event, app.last_hits, curses)
    choices = navigation_tools.SETTING_CHOICES["theme"]
    assert app.theme == choices[(choices.index(original) + 1) % len(choices)]
    assert app.mode == "settings_editor"


def test_new_press_supersedes_duplicate_guard_and_preserves_repeated_choices(dashboard):
    app, _ = dashboard
    modal, calls = frame(app)
    publish(app)
    for _ in range(2):
        M.route(app, modal, 8, 22, button="press")
        M.route(app, modal, 8, 22, button="release")
    assert len(calls) == 2


def test_value_view_close_works_without_key_shortcut(dashboard):
    app, views = dashboard
    app.mode = "value_peek"
    app.navigation_tools_state.update(field="Exact value", value="Complete value", raw_value="Complete value", return_mode="main")
    paint(app, views)
    native_click(app, button(app, "navigation-action:close"))
    assert app.mode == "main"


@pytest.mark.parametrize("width,height", [(80, 24), (120, 30), (40, 20)])
@pytest.mark.parametrize("command", ["settings", "keybindings", "workspaces", "location"])
def test_navigation_actions_have_visible_exact_bounds(dashboard, width, height, command):
    app, views = dashboard
    app.width, app.height = width, height
    app.run_command(command)
    paint(app, views)
    controls = [control for control in interaction.controls(app)
                if control.id.startswith(("navigation-action:", "workspace-action:"))]
    assert controls
    assert all(0 <= item.rect.left < item.rect.right <= width and 0 <= item.rect.top < item.rect.bottom <= height for item in controls)
    assert any(item.id.endswith((":close", ":cancel")) for item in controls)


def test_changed_search_query_invalidates_aggregate_click_before_next_paint(dashboard):
    app, views = dashboard
    app.run_command("workspaces")
    paint(app, views)
    target = next(item for item in interaction.controls(app) if item.id.startswith("workspace-picker:"))
    app.navigation_state["query"] = "no matches now"
    app.click(target.rect.top, target.rect.left, app.last_hits)
    assert app.mode == "workspace_picker"
    assert app.research_view == "experiment"


@pytest.mark.parametrize("native", (False, True))
def test_disabled_control_explains_reason_without_invoking_owner(dashboard, native):
    app, _ = dashboard
    modal, calls = frame(app)
    publish(app, Control("blocked", "Blocked action", Rect(8, 20, 9, 40),
                         ("command", "must never run"), "modal", enabled=False,
                         reason="Required identity/state column", layer=2))
    if native:
        M.route(app, modal, 8, 22, button="press")
        assert app.message == ""
        M.route(app, modal, 8, 22, button="release")
    else:
        M.route(app, modal, 8, 22, button="left")
    assert not calls
    assert app.message == "Required identity/state column"


@pytest.mark.parametrize("change", ("enabled", "reason", "action", "geometry"))
def test_disabled_feedback_rejects_changed_semantics_between_press_and_release(dashboard, change):
    app, _ = dashboard
    modal, calls = frame(app)
    original = dict(id="blocked", label="Blocked", rect=Rect(8, 20, 9, 40),
                    action=("command", "must never run"), group="modal", enabled=False,
                    reason="Original reason", layer=2)
    publish(app, Control(**original))
    M.route(app, modal, 8, 22, button="press")
    if change == "geometry":
        app.width += 1
    else:
        original.update({"enabled": {"enabled": True}, "reason": {"reason": "New reason"},
                         "action": {"action": ("command", "different")}}[change])
        publish(app, Control(**original))
    M.route(app, modal, 8, 22, button="release")
    assert not calls and app.message == ""
