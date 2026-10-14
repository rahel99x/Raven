"""Default workspace, saved destinations, and contextual setup contracts."""
from types import SimpleNamespace

import pytest

from raven import layout as L, toolbar
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.views import Views


def app():
    return App(Store(persist=False), None, None,
               Config({"animations": False, "startup": {"enabled": False}}), "student")


def paint(owner, width=80, height=24):
    views = Views(L.Glyphs(owner.cfg.get("ascii", False)), owner.cfg)
    owner.views_ref = views
    rows, hits = views.compose(owner.store.snapshot(), owner, width, height)
    views.overlay(owner.store.snapshot(), owner, width, height)
    return views, rows, hits


def test_default_navigation_fits_80_columns_and_secondary_pages_stay_discoverable():
    owner = app()
    _, rows, _ = paint(owner)
    keys = [key for _, _, _, key in owner.tab_hits]
    assert keys == list(toolbar.PRIMARY_TABS)
    assert all(title in L.row_text(rows[3]) for title in ("Jobs", "History", "Analytics", "Research", "Log"))
    secondary = {item.command for item in toolbar.submenu_items(owner, "infrastructure")}
    assert {"tab cluster", "tab nodes", "tab group", "tab deps", "tab sources"} <= secondary
    owner.enter_tab("nodes")
    paint(owner)
    assert any(key == "nodes" for _, _, _, key in owner.tab_hits)


def test_favorites_roundtrip_only_destination_ids_and_reopen_exact_workspace():
    owner = app()
    owner.enter_tab("research")
    owner.research_view = "scaling"
    owner.run_command("favorite")
    saved = toolbar.save(owner)
    assert saved == {"favorites": ["research:scaling"]}
    assert "scaling" in dict(toolbar.visible_research_views(owner))
    other = app()
    toolbar.restore(other, saved)
    entry = next(item for item in toolbar.submenu_items(other, "favorites") if item.key == "favorite-research:scaling")
    toolbar._activate(other, entry)
    assert (other.tab, other.research_view) == ("research", "scaling")
    other.run_command("favorite")
    assert toolbar.save(other) == {"favorites": []}


def test_analytics_favorite_switches_page_before_selecting_view():
    owner = app()
    owner.enter_tab("analytics")
    owner.analytics_view = "timeline"
    owner.run_command("favorite")
    owner.enter_tab("jobs")
    entry = next(item for item in toolbar.submenu_items(owner, "favorites") if item.key == "favorite-analytics:timeline")
    toolbar._activate(owner, entry)
    assert (owner.tab, owner.analytics_view) == ("analytics", "timeline")


def test_invalid_saved_favorites_are_bounded_and_never_become_commands():
    owner = app()
    toolbar.restore(owner, {"favorites": [None, {}, "tab:nodes", "tab:nodes", "cancel 22", "tab:missing"]})
    assert toolbar.save(owner) == {"favorites": ["tab:nodes"]}
    assert "nodes" in dict(toolbar.visible_tabs(owner))
    toolbar.restore(owner, {"favorites": "tab:nodes"})
    assert toolbar.save(owner) == {"favorites": []}


def test_setup_submenu_back_retains_parent_position_and_stale_hits_do_nothing():
    owner = app()
    views, _, _ = paint(owner)
    owner.run_command("menu Setup")
    toolbar.handle_key(owner, "down")
    toolbar.handle_key(owner, "down")
    assert toolbar.menu_items(owner, "Setup")[owner.toolbar_state["cursor"]].key == "setup-recordings"
    toolbar.overlay(views, {}, owner, 80, 24)
    stale = next(hit for hit in owner.toolbar_state["menu_hits"] if hit[-1] == "setup-recordings")
    toolbar.handle_key(owner, "right")
    assert owner.toolbar_state["submenu"] == "recordings" and not owner.toolbar_state["menu_hits"]
    toolbar.handle_mouse(owner, stale[0], stale[1])
    assert owner.mode == "main" and owner.toolbar_state["submenu"] == "recordings"
    toolbar.handle_key(owner, "esc")
    assert owner.toolbar_state["submenu"] is None and owner.toolbar_state["cursor"] == 2
    toolbar.handle_key(owner, "esc")
    assert owner.toolbar_state["menu"] is None


@pytest.mark.parametrize("width,height", [(1, 2), (24, 8), (80, 24)])
@pytest.mark.parametrize("ascii_", [False, True])
def test_every_submenu_is_bounded_and_keyboard_reachable(width, height, ascii_):
    owner = app()
    views = SimpleNamespace(g=L.Glyphs(ascii_))
    for submenu in toolbar.SUBMENUS:
        if submenu == "compute":
            continue
        toolbar.render_bar(views, owner, width)
        toolbar._open(owner, 3)
        toolbar._open_submenu(owner, submenu)
        entries = toolbar.menu_items(owner, "Setup")
        assert len({item.key for item in entries}) == len(entries)
        toolbar.handle_key(owner, "end")
        rows = toolbar.overlay(views, {}, owner, width, height)
        assert all(1 <= y < height and 0 <= x < width and x + L.vlen(L.row_text(row)) <= width for y, x, row in rows)
        assert owner.toolbar_state["cursor"] == len(entries)-1
        if width >= 24:
            assert any(hit[-1] == entries[-1].key for hit in owner.toolbar_state["menu_hits"])


@pytest.mark.parametrize("tab,view,setup_command", [
    ("research", "experiment", "setup research experiment"),
    ("research", "scaling", "setup research scaling"),
    ("analytics", "job", "setup analytics job"),
    ("analytics", "advisor", "setup analytics advisor"),
])
def test_view_setup_is_next_to_current_view_without_global_catalog(tab, view, setup_command):
    owner = app()
    owner.enter_tab(tab)
    setattr(owner, tab + "_view", view)
    views = Views(L.Glyphs(False), owner.cfg)
    if tab == "research":
        from raven.research_views import render
        rows, hits = render(views, owner.store.snapshot(), owner, 80, 24)
    else:
        rows, hits = views.analytics_tab(owner.store.snapshot(), owner, 80, 24)
    commands = [entry["action"][1] for _, kind, entry in hits if kind == "control" and entry["action"][0] == "command"]
    assert setup_command in commands and "connectdata" in commands
    assert all(0 <= entry["left"] < entry["right"] <= 80 for _, kind, entry in hits if kind == "control")
    assert len(toolbar.menu_items(owner, "Setup")) == 5


def test_favorite_menu_rejects_hit_when_underlying_workspace_changes():
    owner = app()
    owner.enter_tab("research")
    views, _, _ = paint(owner)
    owner.run_command("favorite list")
    toolbar.overlay(views, {}, owner, 80, 24)
    old = next(hit for hit in owner.toolbar_state["menu_hits"] if hit[-1] == "favorite-toggle")
    owner.research_view = "scaling"
    toolbar.handle_mouse(owner, old[0], old[1])
    assert toolbar.save(owner) == {"favorites": []}
