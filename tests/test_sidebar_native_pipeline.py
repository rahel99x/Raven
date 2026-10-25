"""The real curses adapter, modal router and workbench persistence own the rail."""

import curses

import pytest

from raven import interaction, layout, screen, sidebar, workbench
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views


@pytest.fixture
def dashboard(monkeypatch):
    cfg = Config(
        {
            "animations": False,
            "startup_animation": False,
            "gpu_sampling": False,
            "log_lines": 0,
            "smooth_scrolling": False,
        }
    )
    store = Store(persist=False)
    store.apply_jobs([Job("10", "train", "local", "RUNNING")])
    app = App(store, None, None, cfg, "student", interactive=False)
    views = Views(layout.Glyphs(True), cfg)
    app.views_ref = views
    app.width, app.height = 80, 30
    saves = []
    monkeypatch.setattr(app, "save", lambda: saves.append(workbench.save(app)))

    def paint():
        rows, hits = views.compose(store.snapshot(), app, app.width, app.height)
        overlays = views.overlay(store.snapshot(), app, app.width, app.height)
        interaction.publish(app, rows, hits, app.width, app.height, overlays=overlays)
        return rows, overlays

    paint()
    yield app, paint, saves
    if app.research:
        app.research.close()


def dispatch(app, y, x, button):
    screen._apply_input(app, ("mouse", (0, x, y, 0, button)), app.last_hits, curses)


def target(app, identity, kind="destination"):
    return next(
        hit
        for hit in sidebar.initialize(app)["hits"]
        if hit[3] == kind and hit[4] == identity
    )


def test_native_right_click_popup_pins_and_restores_to_new_session(dashboard):
    app, paint, saves = dashboard
    hit = target(app, "tab:history")
    selected = app.selected_id
    dispatch(app, hit[0], hit[1], curses.BUTTON3_PRESSED)
    assert app.mode == "sidebar_menu" and app.selected_id == selected
    _, overlays = paint()
    assert overlays
    menu_hit = app.sidebar_state["menu_hits"][0]
    dispatch(app, menu_hit[0], menu_hit[1], curses.BUTTON1_PRESSED)
    assert not app.toolbar_state["favorites"]
    dispatch(app, menu_hit[0], menu_hit[1], curses.BUTTON1_RELEASED)
    assert app.mode == "main"
    assert saves[-1]["toolbar"]["favorites"] == ["tab:history"]
    assert saves[-1]["sidebar"]["favorite_order"] == "custom"
    app.toolbar_state["favorites"] = []
    workbench.restore(app, saves[-1])
    paint()
    assert target(app, "tab:history", "favorite")
    assert target(app, "tab:history", "destination")


def test_native_press_and_release_destination_navigates_once(dashboard):
    app, paint, saves = dashboard
    hit = target(app, "tab:history")
    dispatch(app, hit[0], hit[1], curses.BUTTON1_PRESSED)
    assert app.tab == "jobs"
    dispatch(app, hit[0], hit[1], curses.BUTTON1_RELEASED)
    assert app.tab == "history" and app.mode == "main"
    paint()
    # A native aggregated CLICKED notification following release is inert.
    dispatch(app, hit[0], hit[1], curses.BUTTON1_CLICKED)
    assert app.tab == "history"


def test_f8_graph_enters_popup_choice_without_changing_job_selection(dashboard):
    app, paint, saves = dashboard
    hit = target(app, "tab:analytics")
    dispatch(app, hit[0], hit[1], curses.BUTTON3_PRESSED)
    paint()
    selected = app.selected_id
    app.handle("f8")
    graph = app.interaction_state["graph"]
    first = next(item for item in graph.controls if item.id == "sidebar:menu:0")
    app.interaction_state.update(active=True, focused=first.id)
    app.handle("enter")
    assert app.toolbar_state["favorites"] == ["tab:analytics"]
    assert app.selected_id == selected and app.mode == "main"


def test_f10_toolbar_opens_while_sidebar_has_keyboard_focus(dashboard):
    app, paint, saves = dashboard
    app.run_command("sidebar focus")
    paint()
    assert app.mode == "sidebar"
    app.handle("f10")
    assert app.toolbar_state["menu"] == 0
    app.handle("esc")
    assert app.toolbar_state["menu"] is None
    app.handle("esc")
    assert app.mode == "main"


def test_native_drag_reorders_in_one_gesture_without_switching_page(dashboard):
    app, paint, saves = dashboard
    app.toolbar_state["favorites"] = ["tab:jobs", "tab:history", "tab:research"]
    paint()
    start, end = (
        target(app, "tab:jobs", "favorite"),
        target(app, "tab:research", "favorite"),
    )
    dispatch(app, start[0], start[1], curses.BUTTON1_PRESSED)
    dispatch(app, end[0], end[1], curses.REPORT_MOUSE_POSITION | curses.BUTTON1_PRESSED)
    paint()
    assert app.tab == "jobs" and not saves
    dispatch(app, end[0], end[1], curses.BUTTON1_RELEASED)
    assert app.tab == "jobs"
    assert app.toolbar_state["favorites"] == ["tab:history", "tab:research", "tab:jobs"]
    assert len(saves) == 1
