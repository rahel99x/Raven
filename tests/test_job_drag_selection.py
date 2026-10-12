"""Mouse-selected ranges retain exact IDs and existing bulk-action review."""
from types import SimpleNamespace

import pytest

from raven import job_selection as drag
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs
from raven.model import Job, Store
from raven.views import Views


@pytest.fixture
def dashboard():
    store = Store(persist=False)
    store.jobs = [Job(str(i), "task " + str(i), "main", "RUNNING") for i in range(1, 11)]
    app = App(store, None, None, Config({"animations": False}), "tester", ascii_=True)
    app.width = 180
    views = Views(Glyphs(True), app.cfg)
    app.views_ref = views
    views.compose(store.snapshot(), app, app.width, 38)
    yield app, store, views
    if app.research:
        app.research.close()


def point(app, identifier):
    row = next(y for y, kind, value in app.last_hits if kind == "job" and value == identifier)
    return row, 3


def send(app, identifier, button, shift=False):
    y, x = point(app, identifier)
    return drag.handle_mouse(app, y, x, button=button, shift=shift)


def test_plain_press_and_release_retains_marks_and_normal_single_selection(dashboard):
    app, _, _ = dashboard
    app.marks = {"9"}
    assert send(app, "2", "press")
    assert app.selected_id == "2" and app.marks == {"9"}
    assert send(app, "2", "release")
    assert app.marks == {"9"} and not drag.active(app)


@pytest.mark.parametrize("first,last", [("2", "6"), ("6", "2")])
def test_drag_marks_sorted_range_in_both_directions_without_scheduler_work(dashboard, first, last):
    app, _, _ = dashboard
    app.marks = {"10"}
    send(app, first, "press")
    assert send(app, last, "motion")
    assert app.marks == {"2", "3", "4", "5", "6"}
    assert send(app, last, "release")
    assert app.selected_id == last and app.mode == "main" and app.confirm == {}


def test_release_on_another_row_also_completes_range_without_motion_event(dashboard):
    app, _, _ = dashboard
    send(app, "2", "press")
    send(app, "4", "release")
    assert app.marks == {"2", "3", "4"}


@pytest.mark.parametrize("intervening_event", [None, "motion", "drag"])
def test_live_header_reflow_cannot_change_endpoint_under_stationary_pointer(dashboard, intervening_event):
    app, _, _ = dashboard
    send(app, "2", "press")
    send(app, "4", "drag")
    y, x = point(app, "4")
    # Selecting an unlinked job can remove the auto-linked header. The same
    # coordinate now paints job 5, while displayed order and IDs stay intact.
    app.last_hits = [(row - 1, kind, value) if kind == "job" else (row, kind, value)
                     for row, kind, value in app.last_hits]
    assert next(value for row, kind, value in app.last_hits if row == y and kind == "job") == "5"
    if intervening_event:
        assert drag.handle_mouse(app, y, x + 1, button=intervening_event)
    assert drag.handle_mouse(app, y, x, button="release")
    assert app.marks == {"2", "3", "4"} and app.selected_id == "4"
    assert not drag.active(app)


def test_resize_during_capture_keeps_exact_marked_ids_and_stops_drag(dashboard):
    app, _, _ = dashboard
    send(app, "2", "press")
    send(app, "4", "drag")
    app.width -= 1
    assert send(app, "6", "release")
    assert app.marks == {"2", "3", "4"} and not drag.active(app)


def test_shift_drag_extends_marks_and_drag_back_shrinks_only_new_range(dashboard):
    app, _, _ = dashboard
    app.marks = {"10"}
    send(app, "2", "press", shift=True)
    send(app, "6", "drag")
    send(app, "3", "motion")
    send(app, "3", "release")
    assert app.marks == {"2", "3", "10"}


@pytest.mark.parametrize("first,last", [("2", "6"), ("6", "2")])
def test_synthesized_clicks_set_and_extend_the_same_anchor(dashboard, first, last):
    app, _, _ = dashboard
    assert send(app, first, "left")
    assert send(app, last, "left", shift=True)
    assert app.marks == {"2", "3", "4", "5", "6"}
    assert not drag.active(app)


def test_repeated_shift_click_shrinks_range_and_preserves_original_marks(dashboard):
    app, _, _ = dashboard
    app.marks = {"10"}
    send(app, "2", "left")
    send(app, "6", "left", shift=True)
    assert app.marks == {"2", "3", "4", "5", "6", "10"}
    send(app, "3", "left", shift=True)
    assert app.marks == {"2", "3", "10"}


def test_reordered_range_requires_a_new_shift_anchor(dashboard):
    app, _, _ = dashboard
    send(app, "2", "left")
    app.visible_ids.reverse()
    send(app, "6", "left", shift=True)
    assert app.marks == {"6"}


def test_shift_click_never_resurrects_marks_replaced_by_plain_drag(dashboard):
    app, _, _ = dashboard
    app.marks = {"10"}
    send(app, "2", "press")
    send(app, "6", "release")
    send(app, "3", "left", shift=True)
    assert app.marks == {"2", "3"}


def test_shift_click_respects_intervening_explicit_mark_changes(dashboard):
    app, _, _ = dashboard
    app.marks = {"10"}
    send(app, "2", "left")
    send(app, "6", "left", shift=True)
    app.marks.discard("10")
    app.marks.add("9")
    send(app, "3", "left", shift=True)
    assert app.marks == {"2", "3", "9"}


def test_repeated_motion_on_one_endpoint_does_not_rebind_or_report_marks(dashboard, monkeypatch):
    app, _, _ = dashboard
    send(app, "2", "press")
    send(app, "6", "drag")
    monkeypatch.setattr(drag, "_bind_marks", lambda *_: pytest.fail("Unchanged range was rebound"))
    monkeypatch.setattr(app, "say", lambda *_: pytest.fail("Unchanged range was reported again"))
    for _ in range(50):
        send(app, "6", "motion")
    assert app.marks == {"2", "3", "4", "5", "6"}


def test_escape_during_drag_restores_original_marks(dashboard):
    app, _, _ = dashboard
    app.marks = {"10"}
    send(app, "2", "press")
    send(app, "6", "motion")
    assert drag.handle_key(app, "esc")
    assert app.marks == {"10"} and not drag.active(app)


def test_capture_stops_on_reordering_and_never_marks_a_replacement_row(dashboard):
    app, _, _ = dashboard
    send(app, "2", "press")
    app.visible_ids.reverse()
    assert send(app, "6", "motion")
    assert app.marks == set() and not drag.active(app)


def test_tab_or_modal_change_cancels_pending_gesture(dashboard):
    app, _, _ = dashboard
    send(app, "2", "press")
    app.mode = "confirm"
    drag.tick(app)
    assert not drag.active(app) and not send(app, "6", "motion")
    assert app.marks == set()


def test_release_outside_window_ends_capture_without_extending_range(dashboard):
    app, _, _ = dashboard
    send(app, "2", "press")
    send(app, "4", "motion")
    assert drag.handle_mouse(app, -200, 500, button="release")
    assert app.marks == {"2", "3", "4"} and not drag.active(app)


def test_details_buttons_never_start_a_job_range(dashboard):
    app, _, _ = dashboard
    rect = app.job_panel_rect
    assert not drag.handle_mouse(app, rect.y + 1, rect.x + 1, button="press")
    assert not drag.active(app) and not app.marks


def test_bulk_cancel_review_uses_exact_range_and_drag_executes_nothing(dashboard):
    app, _, _ = dashboard
    calls = []
    app.actions = SimpleNamespace(applicable=lambda action, job: (True, ""),
                                  perform=lambda *args, **kwargs: calls.append((args, kwargs)))
    send(app, "2", "press")
    send(app, "4", "motion")
    send(app, "4", "release")
    assert calls == []
    app.start_confirm("cancel")
    assert app.mode == "confirm" and app.confirm["action"] == "cancel"
    assert [job.id for job in app.confirm["jobs"]] == ["2", "3", "4"]
    assert calls == []
