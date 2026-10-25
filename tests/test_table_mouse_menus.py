"""Mouse parity for table menus, with exact visible controls and stale-frame guards."""
from __future__ import annotations

import pytest

from raven import layout as L, table_sort, table_tools as T, table_ui as C
from raven.config import Config
from raven.controller import App
from raven.model import Job, Node, Store
from raven.views import Views


@pytest.fixture
def dashboard(tmp_path):
    cfg = Config({"log_lines": 0, "animations": False})
    store = Store(state_dir=str(tmp_path / "state"))
    store.jobs = [Job("10", "alpha", "main", "RUNNING", cpus=2, hosts=["n1"]),
                  Job("11", "beta", "main", "PENDING", cpus=4)]
    store.nodes = {"n1": Node("n1", "MIXED", cpus=8, alloc=2)}
    app = App(store, None, None, cfg, "test")
    app.width, app.height = 100, 30
    views = app.views_ref = Views(L.Glyphs(False), cfg)
    views.compose(store.snapshot(), app, app.width, app.height)
    return app, views


def _paint(app, views, owner):
    return owner.overlay(views, app.store.snapshot(), app, app.width, app.height)


def _button(app, views, owner, key):
    _paint(app, views, owner)
    state = app.table_state if owner is C else app.table_tools_state
    row, _, payload = next(hit for hit in state["control_hits"] if hit[2]["action"] == ("key", key))
    return row, payload["left"]


def _click(app, views, owner, key):
    y, x = _button(app, views, owner, key)
    assert owner.handle_mouse(app, y, x)


def test_columns_all_presentation_actions_are_clickable_and_persist(dashboard):
    app, views = dashboard
    app.run_command("columns jobs")
    cols = C.ordered_definitions(app, "jobs")
    chosen = next(index for index, col in enumerate(cols) if col.key == "part")
    app.table_state["cursor"] = chosen
    _click(app, views, C, "space")
    assert "part" in app.table_state["hidden"]["jobs"]
    _click(app, views, C, "right")
    assert app.table_state["order"]["jobs"].index("part") == chosen + 1
    _click(app, views, C, "left")
    assert app.table_state["order"]["jobs"].index("part") == chosen
    _click(app, views, C, "+")
    width = app.table_state["widths"]["jobs"]["part"]
    _click(app, views, C, "-")
    assert app.table_state["widths"]["jobs"]["part"] == width - 1
    _click(app, views, C, "a")
    assert "part" not in app.table_state["widths"]["jobs"]
    _click(app, views, C, "r")
    assert app.table_state["order"]["jobs"] == app.table_state["hidden"]["jobs"] == []
    _click(app, views, C, "enter")
    assert app.mode == "main"
    assert app.store.load_ui()["workbench"]["table_ui"]["order"]["jobs"] == []


@pytest.mark.parametrize("dimensions", ((20, 8), (40, 10), (80, 24), (160, 50)))
def test_columns_done_is_clickable_in_short_narrow_terminals(dashboard, dimensions):
    app, views = dashboard
    app.width, app.height = dimensions
    app.run_command("columns jobs")
    frame = _paint(app, views, C)
    assert all(0 <= y < app.height and 0 <= x < app.width and x + L.vlen(L.row_text(row)) <= app.width
               for y, x, row in frame)
    _click(app, views, C, "enter")
    assert app.mode == "main"


@pytest.mark.parametrize("owner,command", ((C, "columns jobs"), (T, "headers jobs")))
def test_old_menu_coordinates_cannot_activate_after_resize_or_selection_change(dashboard, owner, command):
    app, views = dashboard
    app.run_command(command)
    key = "space" if owner is C else "right"
    y, x = _button(app, views, owner, key)
    app.width += 1
    before = C.save(app)
    assert owner.handle_mouse(app, y, x) is (owner is T)
    assert C.save(app) == before
    y, x = _button(app, views, owner, key)
    state = app.table_state if owner is C else app.table_tools_state
    state["cursor"] += 1
    before = C.save(app)
    assert owner.handle_mouse(app, y, x) is (owner is T)
    assert C.save(app) == before


def test_sort_priority_direction_and_remove_use_existing_keyboard_rules(dashboard):
    app, views = dashboard
    table_sort.set_sort(app, "jobs", "name", "asc")
    table_sort.set_sort(app, "jobs", "cpus", "desc")
    app.run_command("sorteditor jobs")
    _click(app, views, T, "right")
    assert table_sort.chain(app, "jobs") == [("cpus", "desc"), ("name", "asc")]
    _click(app, views, T, "space")
    assert table_sort.chain(app, "jobs")[1] == ("name", "desc")
    _click(app, views, T, "d")
    assert table_sort.chain(app, "jobs") == [("cpus", "desc")]
    _click(app, views, T, "a")
    assert app.table_tools_state["modal"] == "headers"
    _click(app, views, T, "esc")
    assert app.mode == "main"


def test_filter_edit_apply_cancel_clear_have_mouse_controls(dashboard):
    app, views = dashboard
    app.run_command("filters jobs")
    app.table_tools_state["cursor"] = T.FILTER_FIELDS.index("cpus")
    _click(app, views, T, "enter")
    for key in ">=4":
        T.handle_key(app, key)
    _click(app, views, T, "enter")
    assert T.numeric_rules(app, "jobs")[0][:3] == ("cpus", ">=", 4)
    _click(app, views, T, "enter")
    _click(app, views, T, "ctrl-u")
    assert app.table_tools_state["edit"]["text"] == ""
    _click(app, views, T, "esc")
    assert T.numeric_rules(app, "jobs")[0][:3] == ("cpus", ">=", 4)
    _click(app, views, T, "c")
    assert T.numeric_rules(app, "jobs") == []
    _click(app, views, T, "esc")
    assert app.mode == "main"


def test_saved_view_delete_and_named_save_prompt_are_clickable(dashboard):
    app, views = dashboard
    app.run_command("savedview jobs save baseline")
    app.run_command("viewpicker jobs")
    _click(app, views, T, "d")
    assert "baseline" not in app.table_state["views"]
    _click(app, views, T, "S")
    assert app.mode == "palette" and app.palette_edit == "savedview jobs save "


@pytest.mark.parametrize("command", ("sorteditor jobs", "headers jobs", "filters jobs", "viewpicker jobs", "marked", "jobactions 10", "node n1"))
def test_each_table_menu_has_a_mouse_exit(dashboard, command):
    app, views = dashboard
    app.run_command(command)
    assert app.mode == "table_tools", app.message
    _click(app, views, T, "esc")
    assert app.mode == "main"


def test_menu_action_is_consumed_once_until_repaint(dashboard):
    app, views = dashboard
    app.run_command("columns jobs")
    app.table_state["cursor"] = next(i for i, col in enumerate(C.ordered_definitions(app, "jobs")) if col.key == "part")
    y, x = _button(app, views, C, "space")
    C.handle_mouse(app, y, x)
    assert "part" in app.table_state["hidden"]["jobs"]
    C.handle_mouse(app, y, x)
    assert "part" in app.table_state["hidden"]["jobs"]


@pytest.mark.parametrize("command", ("sorteditor jobs", "headers jobs", "filters jobs", "viewpicker jobs", "marked", "jobactions 10", "node n1"))
def test_short_table_menu_keeps_mouse_exit_and_never_maps_border_as_a_row(dashboard, command):
    app, views = dashboard
    app.width, app.height = 20, 8
    app.run_command(command)
    frame = _paint(app, views, T)
    assert all(y != frame[-1][0] for y, left, right, index in app.table_tools_state["hits"])
    _click(app, views, T, "esc")
    assert app.mode == "main"


@pytest.mark.parametrize("dimensions", ((8, 7), (20, 8), (40, 10)))
def test_compact_columns_more_keeps_every_action_reachable_by_mouse(dashboard, dimensions):
    app, views = dashboard
    app.width, app.height = dimensions
    app.run_command("columns jobs")
    app.table_state["cursor"] = next(i for i, col in enumerate(C.ordered_definitions(app, "jobs")) if col.key == "part")
    _paint(app, views, C)
    assert any(hit[2].get("column", (None, None))[1] == "part" for hit in app.table_state["control_hits"])
    _click(app, views, C, "columns-actions")
    for key in ("space", "left", "right", "-", "+", "a", "r"):
        _click(app, views, C, key)
        _click(app, views, C, "columns-action-next")
    assert app.table_state["hidden"]["jobs"] == []
    assert app.table_state["widths"]["jobs"] == {}
    _click(app, views, C, "columns-actions")
    _click(app, views, C, "enter")
    assert app.mode == "main"
