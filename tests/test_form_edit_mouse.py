"""Field edits expose local mouse actions and retain form validation/ownership."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from raven import (
    connect_data_ui,
    data_export_ui,
    layout,
    onboarding_ui,
    setup_ui,
    telemetry_settings_ui,
)
from raven.config import Config
from raven.controller import App
from raven.launching import wizard_ui
from raven.model import Store
from raven.views import Views
from test_job_group_mouse_integration import dispatch


FORMS = (
    (setup_ui, "setup", "account"),
    (onboarding_ui, "onboarding", "name"),
    (connect_data_ui, "connect_data", "metric"),
    (telemetry_settings_ui, "telemetry_settings", "queue_seconds"),
    (wizard_ui, "launch_job", "project"),
    (data_export_ui, "data_export", "path"),
)


@pytest.fixture(params=FORMS, ids=[item[1] for item in FORMS])
def form(request, monkeypatch):
    module, mode, field = request.param
    cfg = Config(
        {
            "animations": False,
            "startup_animation": False,
            "smooth_scrolling": False,
            "log_lines": 0,
        }
    )
    app = App(Store(persist=False), None, None, cfg, "researcher", interactive=False)
    app.views_ref = Views(layout.Glyphs(False), cfg)
    app.width, app.height, app.mode = 80, 24, mode
    state = module.initialize(app)
    if module in (setup_ui, onboarding_ui):
        saved = state["form"]
    elif module in (telemetry_settings_ui, wizard_ui):
        saved = state["draft"]
    elif module is data_export_ui:
        state["stage"] = "form"
        saved = state["destination"]
    else:
        saved = state
    before, after = ("5", "1") if field == "queue_seconds" else ("loss", "accuracy")
    saved[field] = before
    state["editing"] = field, after
    writes = []
    monkeypatch.setattr(app, "save", lambda: writes.append("save"))
    yield SimpleNamespace(
        app=app,
        module=module,
        state=state,
        saved=saved,
        field=field,
        before=before,
        after=after,
        writes=writes,
    )
    if app.research:
        app.research.close()


def paint(form, width=80, height=24):
    app = form.app
    snap = app.store.snapshot()
    _, hits = app.views_ref.compose(snap, app, width, height)
    app.views_ref.overlay(snap, app, width, height)
    return hits


def control_point(form, action):
    row, _, control = next(
        hit
        for hit in form.state["control_hits"]
        if hit[2]["id"] == "field-edit:" + action
    )
    return row, control["left"] + 1


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
@pytest.mark.parametrize("action", ["keep", "cancel"])
def test_mouse_field_actions_keep_or_cancel_only_that_field(form, route, action):
    app, state = form.app, form.state
    mode = app.mode
    hits = paint(form)
    assert {hit[2]["label"] for hit in state["control_hits"]} == {
        "Keep field",
        "Cancel edit",
    }
    point = control_point(form, action)
    dispatch(app, hits, point, "press", route)
    assert state["editing"] is not None and form.saved[form.field] == form.before
    hits = paint(form)
    dispatch(app, hits, point, "release", route)
    assert state["editing"] is None
    assert form.saved[form.field] == (form.after if action == "keep" else form.before)
    assert app.mode == mode and not app.confirm and not form.writes
    assert (
        not state.get("busy") and not state.get("pending") and not state.get("running")
    )


@pytest.mark.parametrize("mutation", ["resize", "editing", "revision", "scope"])
def test_stale_field_buttons_cannot_commit_a_different_edit(form, mutation):
    app, state = form.app, form.state
    hits = paint(form)
    point = control_point(form, "keep")
    app.click(*point, hits, button="press")
    if mutation == "resize":
        app.width -= 1
    elif mutation == "editing":
        state["editing"] = form.field, "changed-again"
    elif mutation == "revision":
        state["revision"] += 1
    else:
        app.mode = "help"
    app.click(*point, hits, button="release")
    assert state["editing"] is not None and form.saved[form.field] == form.before
    assert not form.writes and not app.confirm


def test_orphan_mismatch_and_background_field_clicks_do_not_commit(form):
    app, state = form.app, form.state
    hits = paint(form)
    keep, cancel = control_point(form, "keep"), control_point(form, "cancel")
    app.click(*keep, hits, button="release")
    app.click(*keep, hits, button="motion")
    app.click(*keep, hits, button="press")
    app.click(*cancel, hits, button="release")
    app.click(0, 0, hits, button="left")
    assert state["editing"] == (form.field, form.after)
    assert form.saved[form.field] == form.before and not form.writes


@pytest.mark.parametrize("width,height", [(30, 10), (80, 5), (4, 3), (1, 1)])
def test_field_edit_controls_are_only_actionable_when_actually_painted(
    form, width, height
):
    hits = paint(form, width, height)
    for row, _, control in form.state["control_hits"]:
        assert 0 <= row < height and 0 <= control["left"] < control["right"] <= width
    if not form.state["control_hits"]:
        form.app.click(0, 0, hits, button="press")
        form.app.click(0, 0, hits, button="release")
        assert form.state["editing"] is not None


@pytest.mark.parametrize("form", [FORMS[2]], indirect=True, ids=["connect_data"])
def test_metric_validation_error_remains_editable_then_mouse_cancel_restores_value(
    form,
):
    form.state["editing"] = "metric", "invalid=metric"
    hits = paint(form)
    point = control_point(form, "keep")
    dispatch(form.app, hits, point, "press", "native")
    dispatch(form.app, hits, point, "release", "native")
    assert form.state["editing"] is not None and form.saved["metric"] == form.before
    assert form.state["message"] and not form.writes
    hits = paint(form)
    point = control_point(form, "cancel")
    dispatch(form.app, hits, point, "press", "native")
    dispatch(form.app, hits, point, "release", "native")
    assert form.state["editing"] is None and form.saved["metric"] == form.before


def test_edit_footer_paint_and_hover_do_not_read_store_or_persist(form, monkeypatch):
    app, state = form.app, form.state
    paint(form)
    point = control_point(form, "keep")
    original = deepcopy(state["editing"])
    monkeypatch.setattr(
        app.store, "snapshot", lambda: pytest.fail("field pointer read scheduler state")
    )
    for _ in range(30):
        form.module.handle_mouse(app, *point, button="motion")
        form.module.overlay(app.views_ref, {}, app, app.width, app.height)
    assert state["editing"] == original and not form.writes


def test_edit_actions_remain_keyboard_accessible_through_published_controls(form):
    app = form.app
    paint(form)
    graph = app.interaction_state["graph"]
    assert graph.get("field-edit:keep") and graph.get("field-edit:cancel")
    app.handle("f8")
    app.interaction_state["focused"] = "field-edit:cancel"
    app.handle("enter")
    assert form.state["editing"] is None and form.saved[form.field] == form.before
    assert not form.writes and not app.confirm


@pytest.mark.parametrize("action", ["keep", "cancel"])
def test_edit_release_aggregate_cannot_click_the_revealed_form(form, action):
    app = form.app
    hits = paint(form)
    point = control_point(form, action)
    dispatch(app, hits, point, "press", "native")
    dispatch(app, hits, point, "release", "native")
    assert form.state["editing"] is None
    hits = paint(form)
    before = app.mode, form.state["revision"], form.saved[form.field]
    app.click(*point, hits, button="left")
    assert (app.mode, form.state["revision"], form.saved[form.field]) == before
    assert not form.writes and not app.confirm
