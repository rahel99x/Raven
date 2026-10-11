"""Column checkboxes expose only their exact painted, semantic mouse targets."""
import pytest

from tower import layout as L, table_ui
from tower.config import Config
from tower.controller import App
from tower.model import Job, Store
from tower.views import Views


@pytest.fixture
def dashboard():
    cfg = Config({"log_lines": 0, "animations": False, "startup_animation": False})
    store = Store(persist=False)
    store.apply_jobs([Job("7", "test job", "cpu", "RUNNING")])
    app = App(store, None, None, cfg, "tester")
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    app.run_command("columns jobs")
    return app, views


def render(app, views, width=100, height=24):
    app.width = width
    rows, hits = views.compose(app.store.snapshot(), app, width, height)
    app.last_hits = hits
    overlay = views.overlay(app.store.snapshot(), app, width, height)
    return rows, overlay


def checkbox(app, key):
    return next(hit for hit in app.table_state["control_hits"] if hit[2]["column"][1] == key)


def test_actual_app_click_toggles_exact_column_and_preserves_sort_and_widths(dashboard, monkeypatch):
    app, views = dashboard
    app.run_command("sortby jobs id asc")
    app.run_command("sortby jobs cpus desc")
    app.run_command("columns jobs width cpus 12")
    app.run_command("columns jobs order mem%,id,cpus,name,st")
    saved = []
    monkeypatch.setattr(app, "save", lambda: saved.append(True))
    sort_before = list(app.table_state["sorts"]["jobs"])
    render(app, views)
    y, _, value = checkbox(app, "mem%")
    app.click(y, value["left"], app.last_hits)
    assert "mem%" in app.table_state["hidden"]["jobs"]
    assert app.table_state["sorts"]["jobs"] == sort_before
    assert app.table_state["widths"]["jobs"] == {"cpus": 12}
    assert saved
    render(app, views)
    y, _, value = checkbox(app, "mem%")
    app.click(y, value["right"] - 1, app.last_hits)
    assert "mem%" not in app.table_state["hidden"]["jobs"]
    assert app.table_state["cursor"] == 0  # The reordered semantic column.


@pytest.mark.parametrize("table", ["jobs", "history", "recent", "group", "nodes", "cluster", "sources"])
def test_all_table_column_checkboxes_toggle_using_stable_keys(dashboard, table):
    app, views = dashboard
    app.run_command("columns " + table)
    render(app, views)
    available = [hit for hit in app.table_state["control_hits"] if hit[2]["enabled"]]
    assert available
    y, _, value = available[-1]
    key = value["column"][1]
    app.click(y, value["left"], app.last_hits)
    shared = "jobs" if table == "recent" else table
    assert key in app.table_state["hidden"][shared]
    assert not set(app.table_state["hidden"][shared]) & table_ui.REQUIRED


@pytest.mark.parametrize("key", ["id", "name", "st"])
def test_required_checkbox_is_disabled_in_graph_and_cannot_hide_identity(dashboard, key):
    app, views = dashboard
    render(app, views)
    y, _, value = checkbox(app, key)
    assert not value["enabled"] and value["reason"]
    graph = app.interaction_state["graph"]
    control = graph.get(value["id"])
    assert control is not None and not control.enabled and control.reason
    app.click(y, value["left"], app.last_hits)
    assert key not in app.table_state["hidden"].get("jobs", [])
    assert "Required" in app.message


@pytest.mark.parametrize("width,height", [(6, 3), (8, 5), (8, 7), (14, 8), (25, 10), (60, 16), (100, 24)])
@pytest.mark.parametrize("ascii_", [False, True])
def test_targets_follow_actual_box_clipping_and_never_activate_borders(dashboard, width, height, ascii_):
    app, views = dashboard
    views.g = L.Glyphs(ascii_)
    app.table_state["cursor"] = min(8, len(table_ui.definitions("jobs")) - 1)
    rows, overlay = render(app, views, width, height)
    placements = {y: (x, segments) for y, x, segments in overlay or []}
    for y, kind, value in app.table_state["control_hits"]:
        assert kind == "control" and 1 <= y < height
        x, segments = placements[y]
        assert x < value["left"] < value["right"] <= x + L.vlen(L.row_text(segments)) - 1
        assert value["right"] <= width
        before = list(app.table_state["hidden"].get("jobs", []))
        assert not table_ui.handle_mouse(app, y, x)
        assert not table_ui.handle_mouse(app, y, x + L.vlen(L.row_text(segments)) - 1)
        assert app.table_state["hidden"].get("jobs", []) == before
    if height >= 7 and width >= 8:
        cols = table_ui.ordered_definitions(app, "jobs")
        selected = cols[app.table_state["cursor"]].key
        assert any(hit[2]["column"][1] == selected for hit in app.table_state["control_hits"])
    else:
        assert not app.table_state["control_hits"]


def test_scrolled_checkbox_maps_selected_end_column_not_old_absolute_index(dashboard):
    app, views = dashboard
    table_ui.handle_key(app, "end")
    render(app, views, width=55, height=9)
    selected = table_ui.ordered_definitions(app, "jobs")[app.table_state["cursor"]]
    y, _, value = checkbox(app, selected.key)
    assert len(app.table_state["control_hits"]) <= 3
    app.click(y, value["left"], app.last_hits)
    if selected.key not in table_ui.REQUIRED:
        assert app.table_state["hidden"]["jobs"] == [selected.key]


def test_stale_modal_registries_clear_after_closing_or_unusable_resize(dashboard):
    app, views = dashboard
    render(app, views)
    old = checkbox(app, "cpus")
    render(app, views, width=2, height=2)
    assert not app.table_state["control_hits"]
    assert not table_ui.handle_mouse(app, old[0], old[2]["left"])
    render(app, views)
    table_ui.handle_key(app, "enter")
    assert app.mode == "main" and not app.table_state["control_hits"]
    assert not table_ui.handle_mouse(app, old[0], old[2]["left"])


def test_transient_mouse_targets_are_not_persisted(dashboard):
    app, views = dashboard
    render(app, views)
    assert app.table_state["control_hits"]
    assert "control_hits" not in table_ui.save(app)
