"""Global controls remain reachable through the composed native mouse path."""

import curses

import pytest

from raven import interaction, layout as L, palette, screen, toolbar
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views


def dashboard(width=80, ascii_=False):
    config = Config({"ascii": ascii_, "animations": False, "startup": {"enabled": False}})
    app = App(Store(persist=False), None, None, config, "student", ascii_=ascii_)
    views = Views(L.Glyphs(ascii_), config)
    app.views_ref = views
    app.width, app.height = width, 30
    paint(app, views)
    return app, views


def paint(app, views):
    rows, _ = views.compose(app.store.snapshot(), app, app.width, app.height)
    overlays = views.overlay(app.store.snapshot(), app, app.width, app.height)
    return rows, overlays


def native(app, y, x, flag):
    screen._apply_input(app, ("mouse", (0, x, y, 0, flag)), app.last_hits, curses)


@pytest.mark.parametrize("width", [40, 80, 140])
@pytest.mark.parametrize("ascii_", [False, True])
def test_all_themes_are_mouse_reachable_and_apply_live_with_sidebar(width, ascii_):
    app, views = dashboard(width, ascii_)
    if width >= 80:
        assert "Themes" in L.row_text(toolbar.render_bar(views, app, width))
    app.run_command("menu Themes")
    for theme in palette.THEME_NAMES:
        paint(app, views)
        y, left, _, _ = next(hit for hit in app.toolbar_state["menu_hits"]
                              if hit[-1] == "theme-" + theme)
        native(app, y, left, curses.BUTTON1_PRESSED)
        native(app, y, left, curses.BUTTON1_RELEASED)
        assert app.theme == theme
        assert app.mode == "main" and app.tab == "jobs"
        assert app.toolbar_state["menu"] == toolbar.MENUS.index("Themes")
        rows, overlays = paint(app, views)
        if ascii_:
            assert all(L.row_text(row).isascii() for _, _, row in overlays)
        assert any(control.group == "toolbar-menu" for control in interaction.controls(app))
        if theme != "system":
            assert palette.resolve(palette.cell_style("", theme), theme).background == palette.rgb(palette.theme_tokens(theme)["canvas"])


def test_sidebar_parent_does_not_get_duplicate_accessible_tab_stop():
    app, _ = dashboard()
    controls = interaction.controls(app)
    parents = [control for control in controls if control.id.startswith("sidebar:destination:")]
    assert parents
    assert not any(control.id.startswith("tab:") for control in controls)
    assert len({control.rect for control in parents}) == len(parents)


def test_unpainted_or_streamed_sidebar_does_not_claim_document_cells():
    from raven import sidebar

    app = App(Store(persist=False), None, None, Config(), "student")
    app.width, app.height = 80, 24
    assert not sidebar.handle_mouse(app, 10, 2, button="press")
    views = Views(L.Glyphs(False), app.cfg)
    views.compose(app.store.snapshot(), app, 80, 24)
    assert app.content_origin_x > 0
    views.compose(app.store.snapshot(), app, 80, None)
    assert app.content_origin_x == 0
    assert not sidebar.handle_mouse(app, 10, 2, button="press")


@pytest.mark.parametrize("hits", [None, "bad", [(1,)], [(1, "job", "701", "extra")]])
def test_native_sidebar_click_cannot_bypass_malformed_supplied_map(hits):
    app, _ = dashboard()
    target = next(control for control in interaction.controls(app)
                  if control.id == "sidebar:destination:tab:history")
    screen._apply_input(app, ("mouse", (0, target.rect.left, target.rect.top, 0,
                                         curses.BUTTON1_PRESSED)), hits, curses)
    assert app.tab == "jobs"


@pytest.mark.parametrize("width", [40, 80, 140])
def test_job_row_hitboxes_and_selection_do_not_include_navigation(width):
    app, views = dashboard(width)
    app.store.apply_jobs([Job("701", "train", "local", "RUNNING")])
    paint(app, views)
    target = next(control for control in interaction.controls(app) if control.id == "job:701")
    assert target.rect.left > app.content_origin_x
    assert all(control.id != "job:701" or control.rect.left >= app.content_origin_x
               for control in interaction.controls(app))

