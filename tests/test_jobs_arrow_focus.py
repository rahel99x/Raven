"""Physical mouse/key routing returns Jobs arrows to the clicked Main pane."""
import curses
from types import SimpleNamespace

import pytest

from raven import interaction as I, job_panels as J, job_selection as S, screen
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs
from raven.model import Finished, Job, Store
from raven.views import Views


@pytest.fixture
def dashboard():
    cfg = Config({"animations": False, "startup_animation": False, "log_lines": 0,
                  "workspace": {"density": "compact", "split": 50}})
    store = Store(persist=False)
    store.jobs = [Job(str(index), f"active-{index:02}", "cpu", "RUNNING", cpus=4) for index in range(1, 35)]
    store.finished = [Finished(str(index), f"past-{index:03}", "COMPLETED", cpus=4,
                               elapsed="00:10:00") for index in range(201, 251)]
    app = App(store, None, None, cfg, "test", interactive=False)
    views = Views(Glyphs(False), cfg)
    app.views_ref = views
    app.table_state["groups"] = False
    result = SimpleNamespace(app=app, views=views, store=store, hits=[])
    draw(result)
    yield result
    if app.research:
        app.research.close()


def draw(dashboard):
    app = dashboard.app
    _, dashboard.hits = dashboard.views.compose(dashboard.store.snapshot(), app, 160, 42)
    return I.initialize(app)["graph"]


def mouse(dashboard, y, x, button="click"):
    flag = {"press": curses.BUTTON1_PRESSED, "click": curses.BUTTON1_CLICKED,
            "release": curses.BUTTON1_RELEASED,
            "drag": curses.BUTTON1_PRESSED | curses.REPORT_MOUSE_POSITION}[button]
    screen._apply_input(dashboard.app, ("mouse", (0, x, y, 0, flag)), dashboard.hits, curses)


def row(dashboard, identifier):
    return next((y, 3) for y, kind, value in dashboard.hits if kind in ("job", "recent") and value == identifier)


def details_button(dashboard, mode="inspector"):
    y, _, value = next(hit for hit in dashboard.hits if hit[1] == "job_panel_tab" and hit[2][0] == mode)
    return y, value[1]


def drag(dashboard, first="2", last="4", *, release=True):
    mouse(dashboard, *row(dashboard, first), "press")
    draw(dashboard)
    mouse(dashboard, *row(dashboard, last), "drag")
    draw(dashboard)
    if release:
        mouse(dashboard, *row(dashboard, last), "release")
        draw(dashboard)


def forbidden(*args, **kwargs):
    raise AssertionError("Jobs focus or arrow movement copied a Store snapshot")


@pytest.mark.parametrize("return_gesture", ["click", "press"])
def test_real_drag_details_button_then_main_row_restores_arrows_and_retains_marks(dashboard, monkeypatch, return_gesture):
    app = dashboard.app
    drag(dashboard)
    marks = set(app.marks)
    assert marks == {"2", "3", "4"}
    mouse(dashboard, *details_button(dashboard))
    draw(dashboard)
    assert app.layout_state.focus == "details" and app.job_panel_state["focus"] == "tabs"
    assert app.interaction_state["active"]
    mouse(dashboard, *row(dashboard, "6"), return_gesture)
    if return_gesture == "press":
        mouse(dashboard, *row(dashboard, "6"), "release")
    assert app.layout_state.focus == "main" and not app.job_panel_state["focus"]
    assert not app.interaction_state["active"]
    assert app.selected_id == "6" and app.marks == marks
    monkeypatch.setattr(dashboard.store, "snapshot", forbidden)
    screen._apply_input(app, ("down", None), dashboard.hits, curses)
    assert app.selected_id == "7" and app.cursor["jobs"] == app.visible_ids.index("7")
    assert app.marks == marks
    screen._apply_input(app, ("up", None), dashboard.hits, curses)
    assert app.selected_id == "6"


def test_real_details_content_keeps_scroll_until_clicked_jobs_row(dashboard, monkeypatch):
    app = dashboard.app
    drag(dashboard)
    app.run_command("jobpanel analytics advisor")
    draw(dashboard)
    rect = app.job_panel_rect
    mouse(dashboard, rect.y + rect.height - 2, rect.x + 3)
    assert app.job_panel_state["focus"] == "content" and app.layout_state.focus == "details"
    cursor, selected, marks = app.cursor["jobs"], app.selected_id, set(app.marks)
    before = app.layout_state.scroll.get("jobs:details", 0)
    published_snapshot = dashboard.store.snapshot()
    monkeypatch.setattr(dashboard.store, "snapshot", forbidden)
    screen._apply_input(app, ("down", None), dashboard.hits, curses)
    assert app.layout_state.scroll["jobs:details"] == before + 1
    assert app.cursor["jobs"] == cursor and app.selected_id == selected
    # A viewport change publishes its current controls before the next click.
    # Rendering can reuse the existing snapshot; focus input still performs no
    # scheduler snapshot or source work.
    _, dashboard.hits = dashboard.views.compose(published_snapshot, app, 160, 42)
    mouse(dashboard, *row(dashboard, "6"), "press")
    mouse(dashboard, *row(dashboard, "6"), "release")
    screen._apply_input(app, ("down", None), dashboard.hits, curses)
    assert app.selected_id == "7" and app.marks == marks


def test_new_range_drag_after_details_content_returns_main_ownership(dashboard):
    app = dashboard.app
    drag(dashboard)
    app.run_command("jobpanel analytics advisor")
    draw(dashboard)
    rect = app.job_panel_rect
    mouse(dashboard, rect.y + rect.height - 2, rect.x + 3)
    drag(dashboard, "5", "7")
    assert app.marks == {"5", "6", "7"}
    assert app.layout_state.focus == "main" and not app.job_panel_state["focus"]
    app.handle("down")
    assert app.selected_id == "8"
    assert app.marks == {"5", "6", "7"}


@pytest.mark.parametrize("key", ["esc", "f6", "ctrl-w"])
def test_single_escape_or_pane_cycle_returns_details_buttons_to_main(dashboard, key):
    app = dashboard.app
    drag(dashboard)
    mouse(dashboard, *details_button(dashboard))
    draw(dashboard)
    marks = set(app.marks)
    assert app.layout_state.focus == "details"
    app.handle(key)
    assert app.layout_state.focus == "main" and not app.job_panel_state["focus"]
    assert not app.interaction_state["active"]
    app.handle("down")
    assert app.selected_id == "5" and app.marks == marks


def test_f6_main_details_main_preserves_content_scrolling_and_exact_job(dashboard):
    app = dashboard.app
    app.run_command("jobpanel analytics advisor")
    J.focus_main(app)
    draw(dashboard)
    selected = app.selected_id
    app.handle("f6")
    assert app.layout_state.focus == "details"
    top = app.layout_state.scroll.get("jobs:details", 0)
    app.handle("down")
    assert app.layout_state.scroll["jobs:details"] == top + 1
    assert app.selected_id == selected
    app.handle("f6")
    assert app.layout_state.focus == "main"
    app.handle("down")
    assert app.selected_id != selected


@pytest.mark.parametrize("stale", ["tabs", "views", "content"])
def test_stale_native_details_hint_never_steals_arrows_from_main(dashboard, stale):
    app = dashboard.app
    app.job_panel_state["focus"] = stale
    app.layout_state.focus = "main"
    selected = app.selected_id
    app.handle("down")
    assert app.selected_id != selected and not app.job_panel_state["focus"]


def test_deliberate_f8_row_details_and_back_keeps_graph_navigation(dashboard, monkeypatch):
    app = dashboard.app
    frame = draw(dashboard)
    selected = next(control for control in frame.controls if control.group == "job" and control.label == "4")
    I.handle_mouse(app, selected.rect.top, selected.rect.left, "motion")
    app.handle("f8")
    assert app.selected_id == "4"
    frame = draw(dashboard)
    monkeypatch.setattr(dashboard.store, "snapshot", forbidden)
    app.handle("right")
    focused = frame.get(app.interaction_state["focused"])
    assert focused and frame.region(focused) == "page:details"
    app.handle("left")
    assert app.interaction_state["focused"] == "job:4" and app.interaction_state["active"]
    app.handle("down")
    assert app.selected_id == "5" and app.interaction_state["active"]


def test_jobs_keyboard_boundary_movement_remains_bounded_after_focus_handoff(dashboard, monkeypatch):
    app = dashboard.app
    mouse(dashboard, *details_button(dashboard))
    draw(dashboard)
    mouse(dashboard, *row(dashboard, "3"), "press")
    mouse(dashboard, *row(dashboard, "3"), "release")
    marks = set(app.marks)
    monkeypatch.setattr(dashboard.store, "snapshot", forbidden)
    app.handle("home")
    assert app.cursor["jobs"] == 0
    for _ in range(10):
        app.handle("up")
    assert app.cursor["jobs"] == 0
    app.handle("end")
    expected = len(app.visible_ids) + len(app.recent_ids) - 1
    assert app.cursor["jobs"] == expected
    for _ in range(100):
        app.handle("down")
    # Recents deliberately admit older cached rows at the current end. The
    # true boundary remains bounded once all cached records are reachable.
    expected = len(app.visible_ids) + len(app.recent_ids) - 1
    assert expected == len(dashboard.store.jobs) + len(dashboard.store.finished) - 1
    assert app.cursor["jobs"] == expected and app.marks == marks


def test_focus_handoff_does_not_read_files_rebuild_jobs_or_save(dashboard, monkeypatch):
    app = dashboard.app
    app.job_panel_state["focus"], app.layout_state.focus = "content", "details"
    app.interaction_state.update(active=True, focused="job_panel_tab:inspector", pending_focus=True)
    marks, selected, cursor = {"2", "3"}, app.selected_id, app.cursor["jobs"]
    app.marks = set(marks)
    monkeypatch.setattr(dashboard.store, "snapshot", forbidden)
    monkeypatch.setattr(app, "save", forbidden)
    J.focus_main(app)
    assert app.layout_state.focus == "main" and not app.job_panel_state["focus"]
    assert not app.interaction_state["active"] and app.interaction_state["focused"] is None
    assert app.interaction_state["pending_focus"] is None
    assert app.marks == marks and app.selected_id == selected and app.cursor["jobs"] == cursor


@pytest.mark.parametrize("new_gesture", ["click", "press"])
def test_lost_range_release_is_committed_before_details_control_activation(dashboard, new_gesture):
    app = dashboard.app
    drag(dashboard, release=False)
    marks = set(app.marks)
    assert S.active(app) and marks == {"2", "3", "4"}
    mouse(dashboard, *details_button(dashboard), new_gesture)
    assert not S.active(app)
    draw(dashboard)
    assert app.layout_state.focus == "details"
    assert app.interaction_state["active"]
    app.handle("esc")
    assert app.layout_state.focus == "main" and app.marks == marks
    app.handle("down")
    assert app.selected_id == "5" and app.marks == marks


@pytest.mark.parametrize("toolbar_kind", ["refresh-settings", "menu"])
@pytest.mark.parametrize("new_gesture", ["press", "click"])
def test_fresh_toolbar_gesture_commits_unreleased_range_before_global_capture(dashboard, monkeypatch, toolbar_kind, new_gesture):
    app = dashboard.app
    drag(dashboard, release=False)
    marks, selected, cursor = set(app.marks), app.selected_id, app.cursor["jobs"]
    assert S.active(app)
    hit = next(hit for hit in app.toolbar_state["hits"] if hit[3] == toolbar_kind)
    y, left, right = hit[:3]
    monkeypatch.setattr(dashboard.store, "snapshot", forbidden)
    mouse(dashboard, y, (left + right - 1) // 2, new_gesture)
    if toolbar_kind == "refresh-settings":
        assert app.mode == "telemetry_settings"
    else:
        assert app.toolbar_state["menu"] is not None
    assert not S.active(app)
    assert app.marks == marks and app.selected_id == selected and app.cursor["jobs"] == cursor
    # A delayed release from the old range cannot adjust its endpoint or undo
    # its marks even when the global owner consumes the new gesture first.
    mouse(dashboard, *row(dashboard, "8"), "release")
    assert not S.active(app)
    assert app.marks == marks and app.selected_id == selected and app.cursor["jobs"] == cursor
