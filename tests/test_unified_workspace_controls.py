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


@pytest.mark.parametrize("width", [40, 80, 140])
def test_legacy_details_has_mouse_scroll_close_and_no_duplicate_launch(width):
    app, views = dashboard(width)
    app.store.apply_jobs([Job("701", "train", "local", "RUNNING")])
    app.store.details["701"] = {"Key" + str(index): str(index) for index in range(60)}
    paint(app, views)
    app.open_details()
    paint(app, views)

    def target(kind):
        return next(hit for hit in app.toolbar_state["hits"] if hit[3] == kind)

    down = target("dialog-down")
    native(app, down[0], down[1], curses.BUTTON1_PRESSED)
    native(app, down[0], down[1], curses.BUTTON1_RELEASED)
    assert app.mode == "details" and app.scroll == 1 and app.selected_id == "701"
    paint(app, views)
    up = target("dialog-up")
    native(app, up[0], up[1], curses.BUTTON1_PRESSED)
    native(app, up[0], up[1], curses.BUTTON1_RELEASED)
    assert app.scroll == 0
    app.run_command("menu File")
    paint(app, views)
    close = target("dialog-close")
    native(app, close[0], close[1], curses.BUTTON1_PRESSED)
    native(app, close[0], close[1], curses.BUTTON1_RELEASED)
    assert app.mode == "main" and not app.quit and app.selected_id == "701"
    assert app.toolbar_state["menu"] is None
    paint(app, views)
    native(app, close[0], close[1], curses.BUTTON1_CLICKED)
    assert app.mode == "main" and not app.quit


@pytest.mark.parametrize("action", ["filter-apply", "filter-clear"])
@pytest.mark.parametrize("menu_open", [False, True])
def test_footer_filter_mouse_actions_use_the_existing_edit_semantics(action, menu_open):
    app, views = dashboard()
    app.store.apply_jobs([Job("701", "train", "local", "RUNNING")])
    paint(app, views)
    app.handle("/")
    assert app.mode == "filter"
    app.filter_edit = "train"
    if menu_open:
        app.run_command("menu File")
    paint(app, views)
    target = next(hit for hit in app.toolbar_state["hits"] if hit[3] == action)
    native(app, target[0], target[1], curses.BUTTON1_PRESSED)
    native(app, target[0], target[1], curses.BUTTON1_RELEASED)
    assert app.mode == "main"
    assert app.filter == ("train" if action == "filter-apply" else "")
    paint(app, views)
    native(app, target[0], target[1], curses.BUTTON1_CLICKED)
    assert app.mode == "main"


def test_legacy_details_close_rejects_stale_job_identity_and_supports_f8():
    app, views = dashboard()
    app.store.apply_jobs([Job("701", "train", "local", "RUNNING")])
    paint(app, views)
    app.open_details()
    paint(app, views)
    old = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "dialog-close")
    app.detail_id = "702"
    native(app, old[0], old[1], curses.BUTTON1_PRESSED)
    native(app, old[0], old[1], curses.BUTTON1_RELEASED)
    assert app.mode == "details"
    paint(app, views)
    native(app, old[0], old[1], curses.BUTTON1_CLICKED)
    assert app.mode == "details"
    app.handle("f8")
    app.interaction_state.update(active=True, focused="toolbar:dialog-close:None")
    app.handle("enter")
    assert app.mode == "main" and app.selected_id == "701"


@pytest.mark.parametrize("mode,kind", [("details", "dialog-close"), ("filter", "filter-apply"),
                                       ("filter", "filter-clear")])
def test_legacy_mouse_action_owns_activation_when_f8_focuses_quit(mode, kind):
    app, views = dashboard()
    app.store.apply_jobs([Job("701", "train", "local", "RUNNING")])
    paint(app, views)
    app.open_details() if mode == "details" else app.handle("/")
    if mode == "filter":
        app.filter_edit = "train"
    paint(app, views)
    app.handle("f8")
    app.interaction_state.update(active=True, focused="toolbar:quit:None")
    target = next(hit for hit in app.toolbar_state["hits"] if hit[3] == kind)
    native(app, target[0], target[1], curses.BUTTON1_PRESSED)
    native(app, target[0], target[1], curses.BUTTON1_RELEASED)
    assert app.mode == "main" and not app.quit
    if mode == "filter":
        assert app.filter == ("train" if kind == "filter-apply" else "")

