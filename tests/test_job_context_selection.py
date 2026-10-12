"""Selection clearing and History gestures through actual input routing."""
from types import SimpleNamespace

import pytest

from raven import job_selection, layout as L, screen
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store
from raven.remote import LocalFiles
from raven.views import Views


MOUSE = SimpleNamespace(BUTTON1_CLICKED=1, BUTTON1_PRESSED=2, BUTTON1_RELEASED=4,
    BUTTON1_DOUBLE_CLICKED=8, BUTTON3_CLICKED=16, BUTTON3_PRESSED=32,
    BUTTON4_PRESSED=64, BUTTON5_PRESSED=128, REPORT_MOUSE_POSITION=256, BUTTON_SHIFT=512)


@pytest.fixture
def dashboard(tmp_path):
    cfg = Config({"animations": False, "startup_animation": False,
                  "workspace": {"density": "compact"}, "exports": {"projects_root": str(tmp_path)}})
    store = Store(persist=False)
    store.jobs = [Job(str(i), "active " + str(i), "cpu", "RUNNING") for i in range(1, 7)]
    store.finished = [Finished(str(i), "done " + str(i), "FAILED") for i in range(100, 109)]
    app = App(store, None, None, cfg, "tester", interactive=False)
    app.files = LocalFiles()
    views = Views(L.Glyphs(False), cfg, files=app.files)
    app.views_ref = views

    def draw(width=160, height=40):
        return views.compose(store.snapshot(), app, width, height)

    def mouse(y, x, bits):
        screen._apply_input(app, ("mouse", (0, x, y, 0, bits)), app.last_hits, MOUSE)

    def point(identifier):
        return next((y, 6) for y, kind, value in app.last_hits
                    if kind in ("job", "recent", "fin") and value == identifier)

    draw()
    yield SimpleNamespace(app=app, store=store, views=views, draw=draw, mouse=mouse, point=point)
    if app.research:
        app.research.close()


@pytest.mark.parametrize("position", ["row", "details", "toolbar", "footer", "tab"])
@pytest.mark.parametrize("bits", [MOUSE.BUTTON3_PRESSED, MOUSE.BUTTON3_CLICKED])
def test_jobs_right_click_clears_without_activating_any_surface(dashboard, position, bits):
    d, app = dashboard, dashboard.app
    app.marks = {"1", "2"}
    if position == "row":
        y, x = d.point("3")
    elif position == "details":
        rect = app.job_panel_rect
        y, x = rect.y + 1, rect.x + 2
    elif position == "toolbar":
        y, x = 0, 1
    elif position == "footer":
        y, x = app.height - 1, 10
    else:
        y, left, right, name = next(hit for hit in app.tab_hits if hit[-1] == "history")
        x = left
    before = app.job_panel_state["mode"], app.tab
    d.mouse(y, x, bits)
    assert app.selected_id is None and not app.marks
    assert app.sel_anchor is None and not app.quit
    assert (app.job_panel_state["mode"], app.tab) == before
    app.sync_selection()
    rows, hits = d.draw()
    assert app.selected_id is None
    for y, kind, value in hits:
        if kind in ("job", "recent"):
            assert not any("rev" in style.split("+") or "sel" in style.split("+")
                           for _, style in app.frame_rows[y])


@pytest.mark.parametrize("method", ["press", "click", "arrow", "home"])
def test_explicit_row_input_reselects_after_clear(dashboard, method):
    d, app = dashboard, dashboard.app
    d.mouse(0, 1, MOUSE.BUTTON3_PRESSED)
    d.draw()
    if method in ("press", "click"):
        d.mouse(*d.point("3"), MOUSE.BUTTON1_PRESSED if method == "press" else MOUSE.BUTTON1_CLICKED)
        expected = "3"
    else:
        app.handle("down" if method == "arrow" else "home")
        expected = "2" if method == "arrow" else "1"
    d.draw()
    assert app.selected_id == expected
    assert job_selection.selected(app, "jobs", True)


def test_history_right_click_outside_list_clears_without_export_or_row_target(dashboard):
    d, app = dashboard, dashboard.app
    app.enter_tab("history")
    d.draw()
    app.marks = {"100", "101"}
    d.mouse(0, 1, MOUSE.BUTTON3_PRESSED)
    d.draw()
    app.sync_selection()
    assert app.selected_id is None and not app.marks
    assert app.target_ids() == [] and app.mode == "main"
    assert not app.history_jobs_rect.contains(0, 1)


def test_history_right_click_in_list_freezes_marked_ids_without_actions(dashboard):
    d, app = dashboard, dashboard.app
    app.enter_tab("history")
    d.draw()
    app.marks = {"100", "102"}
    d.mouse(*d.point("104"), MOUSE.BUTTON3_PRESSED)
    assert app.mode == "history_log_menu" and not app.quit
    assert app.marks == {"100", "102"}
    assert not app.confirm


@pytest.mark.parametrize("forward", [True, False])
def test_history_drag_marks_exact_visible_range_and_displays_all_markers(dashboard, forward):
    d, app = dashboard, dashboard.app
    app.enter_tab("history")
    d.draw()
    ids = list(app.last_history_ids)
    anchor, end = (ids[1], ids[5]) if forward else (ids[5], ids[1])
    d.mouse(*d.point(anchor), MOUSE.BUTTON1_PRESSED)
    d.mouse(*d.point(end), MOUSE.REPORT_MOUSE_POSITION | MOUSE.BUTTON1_PRESSED)
    d.mouse(*d.point(end), MOUSE.BUTTON1_RELEASED)
    assert app.marks == set(ids[1:6]) and app.selected_id == end
    assert not job_selection.active(app)
    rows, hits = d.draw()
    for y, kind, value in hits:
        if kind == "fin" and value in app.marks:
            assert "●" in L.row_text(app.frame_rows[y])


def test_history_capture_uses_published_order_without_snapshot_per_motion(dashboard, monkeypatch):
    d, app = dashboard, dashboard.app
    app.enter_tab("history")
    d.draw()
    ids = list(app.last_history_ids)
    d.mouse(*d.point(ids[0]), MOUSE.BUTTON1_PRESSED)
    monkeypatch.setattr(app, "history_jobs", lambda *args: (_ for _ in ()).throw(AssertionError("input snapshot")))
    monkeypatch.setattr(d.store, "snapshot", lambda: (_ for _ in ()).throw(AssertionError("input snapshot")))
    d.mouse(*d.point(ids[2]), MOUSE.REPORT_MOUSE_POSITION)
    d.mouse(*d.point(ids[2]), MOUSE.BUTTON1_RELEASED)
    assert app.marks == set(ids[:3])


def test_history_capture_invalidates_on_new_published_order(dashboard):
    d, app = dashboard, dashboard.app
    app.enter_tab("history")
    d.draw()
    ids = list(app.last_history_ids)
    d.mouse(*d.point(ids[0]), MOUSE.BUTTON1_PRESSED)
    app.last_history_ids.reverse()
    d.mouse(*d.point(ids[2]), MOUSE.REPORT_MOUSE_POSITION)
    assert not job_selection.active(app) and not app.marks


def test_right_click_during_drag_clears_marks_and_ends_capture(dashboard):
    d, app = dashboard, dashboard.app
    d.mouse(*d.point("1"), MOUSE.BUTTON1_PRESSED)
    d.mouse(*d.point("3"), MOUSE.REPORT_MOUSE_POSITION)
    assert app.marks == {"1", "2", "3"}
    d.mouse(0, 1, MOUSE.BUTTON3_PRESSED)
    assert not app.marks and app.selected_id is None and not job_selection.active(app)


@pytest.mark.parametrize("density", ["compact", "comfortable", "focused"])
@pytest.mark.parametrize("width,height", [(40, 20), (80, 24), (160, 50)])
def test_history_list_rect_tracks_clipping_and_panel_geometry(dashboard, density, width, height):
    d, app = dashboard, dashboard.app
    app.enter_tab("history")
    app.layout_state.density = density
    rows, hits = d.draw(width, height)
    rect = app.history_jobs_rect
    assert rect is not None
    assert 0 <= rect.top < rect.bottom <= height - 1
    assert 0 <= rect.left < rect.right <= width
    assert not rect.contains(0, 1) and not rect.contains(height - 1, 1)
    for y, kind, value in hits:
        if kind == "fin":
            assert rect.top <= y < rect.bottom
