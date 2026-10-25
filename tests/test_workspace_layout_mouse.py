"""The existing layout picker supports native mouse gestures and F8 actions."""

from unittest.mock import Mock

import pytest

from raven import layout as L, workspace_layout as W
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.views import Views


@pytest.fixture
def dashboard():
    store = Store(persist=False)
    app = App(store, None, None, Config({"animations": False}), "reader", interactive=False)
    app.save = Mock()
    views = Views(L.Glyphs(True), app.cfg)
    app.views_ref = views

    def paint(width=80, height=24):
        rows, hits = views.compose(store.snapshot(), app, width, height)
        views.overlay(store.snapshot(), app, width, height)
        return rows, hits

    yield app, paint
    if app.research:
        app.research.close()


def hit(app, key):
    return next((y, value["left"]) for y, _, value in app.workspace_layout_ui_state["control_hits"]
                if value["id"] == "layout:" + key)


def test_native_layout_release_applies_only_the_reviewed_same_control(dashboard):
    app, paint = dashboard
    app.run_command("layout list")
    _, hits = paint()
    y, x = hit(app, "compact")
    original = W.initialize(app).density
    app.click(y, x, hits, button="press")
    assert W.initialize(app).density == original and not app.save.called
    app.click(y, x, hits, button="release")
    assert W.initialize(app).density == "compact" and app.save.call_count == 1
    app.click(y, x, hits, button="left")  # Native adapters may append this.
    assert app.save.call_count == 1


def test_layout_release_on_another_control_or_after_change_is_inert(dashboard):
    app, paint = dashboard
    app.run_command("layout list")
    _, hits = paint()
    y, x = hit(app, "compact")
    app.click(y, x, hits, button="press")
    other_y, other_x = hit(app, "focused")
    app.click(other_y, other_x, hits, button="release")
    assert not app.save.called
    app.click(y, x, hits, button="press")
    queued_action = next(value["action"][1] for _, _, value in app.workspace_layout_ui_state["control_hits"]
                         if value["id"] == "layout:compact")
    W.initialize(app).ratio = 65
    app.click(y, x, hits, button="release")
    assert not app.save.called
    app.run_command(queued_action)  # A stale keyboard/focus action is also inert.
    assert not app.save.called


def test_layout_save_load_delete_close_and_focus_are_visible_actions(dashboard):
    app, paint = dashboard
    app.run_command("layout save lab")
    app.run_command("layout list")
    _, hits = paint()
    y, x = hit(app, "save")
    app.click(y, x, hits)
    assert app.mode == "palette" and app.palette_edit == "layout save "
    app.handle("esc")
    assert app.mode == "layout"
    _, hits = paint()
    y, x = hit(app, "load:lab")
    app.click(y, x, hits)
    assert app.mode == "layout" and "lab" in W.initialize(app).named
    _, hits = paint()
    y, x = hit(app, "delete:lab")
    app.click(y, x, hits)
    assert "lab" not in W.initialize(app).named
    _, hits = paint()
    y, x = hit(app, "close")
    app.click(y, x, hits)
    assert app.mode == "main"


@pytest.mark.parametrize("width,height", [(24, 8), (40, 12), (80, 24)])
def test_layout_controls_are_bounded_and_close_stays_reachable(dashboard, width, height):
    app, paint = dashboard
    for index in range(16):
        app.run_command("layout save lab" + str(index))
    app.run_command("layout list")
    paint(width, height)
    close = hit(app, "close")
    W.handle_mouse(app, 0, 0, "wheel-down")
    paint(width, height)
    assert hit(app, "close") == close
    assert all(0 <= y < height and 0 <= value["left"] < value["right"] <= width
               for y, _, value in app.workspace_layout_ui_state["control_hits"])
    app.handle("f8")
    assert app.interaction_state["active"]
