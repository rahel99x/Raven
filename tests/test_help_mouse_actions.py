"""Help has explicit mouse actions without losing search, scroll or keyboard UX."""

import pytest

from raven import command_ui, layout
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views
from test_job_group_mouse_integration import dispatch


@pytest.fixture
def app(monkeypatch):
    cfg = Config(
        {
            "animations": False,
            "startup_animation": False,
            "smooth_scrolling": False,
            "log_lines": 0,
        }
    )
    store = Store(persist=False)
    store.jobs = [Job("42", "scientific-job", "local", "RUNNING")]
    value = App(store, None, None, cfg, "student", interactive=False)
    value.views_ref = Views(layout.Glyphs(True), cfg)
    monkeypatch.setattr(value, "save", lambda: pytest.fail("Help persisted settings"))
    value.run_command("help")
    yield value
    if value.research:
        value.research.close()


def paint(app, width=80, height=24):
    snap = app.store.snapshot()
    _, hits = app.views_ref.compose(snap, app, width, height)
    rows = app.views_ref.overlay(snap, app, width, height)
    return hits, rows


def point(app, name):
    y, _, control = next(
        hit
        for hit in app.command_state["help_control_hits"]
        if hit[2]["id"] == "help-action:" + name
    )
    return y, control["left"] + 1


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
def test_mouse_search_done_and_close_preserve_query_without_actions(app, route):
    hits, _ = paint(app)
    position = point(app, "search")
    dispatch(app, hits, position, "press", route)
    assert not app.command_state["help_searching"]
    hits, _ = paint(app)
    dispatch(app, hits, position, "release", route)
    assert app.mode == "help" and app.command_state["help_searching"]
    for char in "accounting":
        app.handle(char)
    hits, rows = paint(app)
    assert "accounting" in "\n".join(layout.row_text(row) for _, _, row in rows).lower()
    control = next(
        hit[2]
        for hit in app.command_state["help_control_hits"]
        if hit[2]["id"] == "help-action:search"
    )
    assert control["label"] == "Done filtering"
    position = point(app, "search")
    dispatch(app, hits, position, "press", route)
    dispatch(app, hits, position, "release", route)
    assert app.mode == "help" and not app.command_state["help_searching"]
    assert app.command_state["help_query"] == "accounting"
    hits, _ = paint(app)
    position = point(app, "close")
    dispatch(app, hits, position, "press", route)
    dispatch(app, hits, position, "release", route)
    assert app.mode == "main" and not app.confirm
    hits, _ = paint(app)
    before = app.tab, app.selected_id, set(app.marks)
    app.click(*position, hits, button="left")
    assert (app.tab, app.selected_id, app.marks) == before


@pytest.mark.parametrize("name", ["search", "close"])
def test_help_buttons_are_keyboard_accessible(app, name):
    paint(app)
    assert app.interaction_state["graph"].get("help-action:" + name)
    app.handle("f8")
    app.interaction_state["focused"] = "help-action:" + name
    app.handle("enter")
    assert app.mode == ("main" if name == "close" else "help")
    assert app.command_state["help_searching"] == (name == "search")


@pytest.mark.parametrize("change", ["resize", "query", "tab", "scroll", "search-state"])
def test_displaced_help_action_cannot_apply_without_repaint(app, change):
    hits, _ = paint(app)
    position = point(app, "close")
    app.click(*position, hits, button="press")
    if change == "resize":
        app.width -= 1
    elif change == "query":
        app.command_state["help_query"] = "changed"
    elif change == "tab":
        app.tab = "history"
    elif change == "scroll":
        app.scroll += 1
    else:
        app.command_state["help_searching"] = True
    app.click(*position, hits, button="release")
    assert app.mode == "help"


def test_help_orphan_mismatch_and_outside_box_clicks_are_inert(app):
    hits, _ = paint(app)
    search, close = point(app, "search"), point(app, "close")
    app.click(*close, hits, button="release")
    app.click(*close, hits, button="motion")
    app.click(*close, hits, button="press")
    app.click(*search, hits, button="release")
    app.click(search[0], 0, hits, button="left")
    assert app.mode == "help" and not app.command_state["help_searching"]


@pytest.mark.parametrize("width,height", [(80, 5), (30, 10), (80, 24), (1, 1), (3, 2)])
def test_help_buttons_have_only_actual_painted_bounds(app, width, height):
    hits, rows = paint(app, width, height)
    for y, _, control in app.command_state["help_control_hits"]:
        assert 0 <= y < height and 0 <= control["left"] < control["right"] <= width
        assert any(row_y == y and row_x <= control["left"] for row_y, row_x, _ in rows)
    if not app.command_state["help_control_hits"]:
        app.click(0, 0, hits, button="left")
        assert app.mode == "help"
    else:
        assert any(
            hit[2]["id"] == "help-action:close"
            for hit in app.command_state["help_control_hits"]
        )


def test_help_pointer_and_repaint_do_not_read_sources(app, monkeypatch):
    paint(app)
    position = point(app, "search")
    monkeypatch.setattr(
        app.store, "snapshot", lambda: pytest.fail("Help read scheduler state")
    )
    for _ in range(20):
        command_ui.handle_mouse(app, *position, button="motion")
        command_ui.overlay(app.views_ref, {}, app, app.width, app.height)
    assert app.mode == "help" and not app.confirm
