"""Preflight and orchestration expose guarded mouse paths to existing actions."""

from types import SimpleNamespace
import curses
import os
import shlex

import pytest

from raven import (
    execution_ui as E,
    interaction,
    layout,
    orchestrator,
    screen,
    submission,
)
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.views import Views
from tests.test_execution_ui import app as app, script as script, form, review


def paint(app, width=120, height=35):
    return E.overlay(SimpleNamespace(g=layout.Glyphs(True)), {}, app, width, height)


def click(app, identity):
    hit = next(
        hit
        for hit in app.execution_state["control_hits"]
        if hit[2]["id"] == "execution:" + identity
    )
    assert E.handle_mouse(app, hit[0], hit[2]["left"])
    return hit


def test_mouse_field_edit_apply_validate_and_review_complete_without_submission(
    app, script
):
    state = form(app, script)
    click(app, "field:cpus")
    assert state["editing"] and state["field"] == 2 and state["edit"] == "4"
    E.handle_key(app, "ctrl-u")
    E.handle_key(app, "8")
    paint(app)
    click(app, "apply")
    assert state["values"]["cpus"] == "8" and state["plan"] is None
    paint(app)
    click(app, "review")
    assert app.mode == "execution" and not app.actions.slurm.calls
    assert "validate" in app.messages[-1][1]
    paint(app)
    click(app, "validate")
    assert state["running"] and not app.actions.slurm.calls
    app.research.complete()
    paint(app)
    assert state["plan"]["valid"]
    assert "--cpus-per-task=8" in state["plan"]["argv"]
    click(app, "review")
    assert app.mode == "confirm" and app.confirm["action"] == "submit"
    assert not app.actions.slurm.calls


def test_mouse_discard_preserves_prior_value_and_back_restores_workbench(app, script):
    state = form(app, script)
    click(app, "field:mem")
    E.handle_key(app, "ctrl-u")
    E.handle_key(app, "9")
    paint(app)
    assert not any(
        value["id"].startswith("execution:field:")
        for _, _, value in state["control_hits"]
    )
    click(app, "discard")
    assert state["values"]["mem"] == "8G" and not state["editing"]
    paint(app)
    click(app, "back")
    assert app.mode == "main" and not app.actions.slurm.calls


def test_mouse_details_and_back_do_not_validate_or_submit(app, script):
    state = form(app, script)
    click(app, "details")
    paint(app)
    assert state["detail"]
    click(app, "back")
    assert not state["detail"] and app.mode == "execution"
    assert app.research.pending is None and not app.actions.slurm.calls


@pytest.mark.parametrize(
    "change", ["geometry", "plan", "field", "editing", "running", "detail"]
)
def test_stale_painted_actions_cannot_advance_a_different_form(app, script, change):
    state = form(app, script)
    hit = next(
        hit for hit in state["control_hits"] if hit[2]["id"] == "execution:review"
    )
    if change == "geometry":
        app.width = 121
    elif change == "plan":
        state["plan"] = dict(state["plan"])
    elif change == "field":
        state["field"] += 1
    else:
        state[change] = True
    E.handle_mouse(app, hit[0], hit[2]["left"])
    assert app.mode == "execution" and not app.actions.slurm.calls
    assert not hasattr(app, "confirm")


def test_review_nodes_have_mouse_details_and_back_without_consuming_review(
    app, script, tmp_path
):
    state = review(app, script, tmp_path)
    click(app, "node:second")
    assert state["detail"] and state["index"] == 1
    paint(app)
    assert not state["confirm_visible"]
    click(app, "back")
    assert not state["detail"] and state["pending_action"] == ("start",)
    assert not app.actions.slurm.calls
    paint(app)
    click(app, "cancel")
    assert app.mode == "main" and not app.actions.slurm.calls


def test_explicit_mouse_confirmation_submits_review_once_and_stale_click_cannot_repeat(
    app, script, tmp_path
):
    state = review(app, script, tmp_path)
    confirm = click(app, "confirm")
    assert state["running"] and not app.actions.slurm.calls
    app.research.complete()
    assert len(app.actions.slurm.calls) == 2
    assert "--dependency=afterok:101" in app.actions.slurm.calls[1][0]
    assert state["receipt"]["status"] == "submitted"
    E.handle_mouse(app, confirm[0], confirm[2]["left"])
    assert len(app.actions.slurm.calls) == 2 and app.research.pending is None


def test_receipt_collection_has_mouse_entry_and_preserves_scheduler_call_count(
    app, script, tmp_path
):
    state = review(app, script, tmp_path)
    receipt = orchestrator.create_receipt(state["review"], tmp_path / "state")
    state.update(
        receipt=receipt,
        receipt_path=receipt["receipt_path"],
        view="receipt",
        pending_action=None,
    )
    paint(app)
    click(app, "collect")
    assert state["running"]
    app.research.complete()
    assert state["receipt"]["collection"]["records"] == []
    assert not app.actions.slurm.calls
    paint(app)
    click(app, "back")
    assert app.mode == "main"


@pytest.mark.parametrize("view", ["form", "receipt"])
def test_running_cancel_button_sets_cooperative_event_without_cancelling_jobs(
    app, script, view
):
    state = form(app, script)
    state.update(view=view, running=True)
    paint(app)
    click(app, "stop")
    assert state["cancel"].is_set() and not app.actions.slurm.calls
    assert "already accepted jobs keep running" in app.messages[-1][1]


def test_background_validation_detail_retains_mouse_cancel_request(app, script):
    state = form(app, script)
    E.handle_key(app, "d")
    E.handle_key(app, "p")
    assert state["detail"] and state["running"]
    paint(app)
    click(app, "stop")
    assert state["cancel"].is_set() and not app.actions.slurm.calls


@pytest.mark.parametrize("width,height", [(20, 8), (40, 10), (80, 24), (120, 35)])
def test_mouse_controls_refer_only_to_visible_bounded_cells(app, script, width, height):
    state = form(app, script)
    rows = paint(app, width, height)
    by_y = {y: (x, layout.vlen(layout.row_text(row))) for y, x, row in rows}
    for y, _, control in state["control_hits"]:
        x, columns = by_y[y]
        assert 0 <= x < control["left"] < control["right"] < x + columns <= width
    assert all(layout.row_text(row).isascii() for _, _, row in rows)


def test_render_and_hover_do_not_read_inputs_or_start_actions(app, script, monkeypatch):
    form(app, script)
    monkeypatch.setattr(
        submission, "prepare", lambda *args, **kwargs: pytest.fail("render read input")
    )
    for _ in range(3):
        paint(app)
        for y, _, control in app.execution_state["control_hits"]:
            assert not E.handle_mouse(app, y, control["left"], button="motion")
    assert app.research.pending is None and not app.actions.slurm.calls


@pytest.fixture
def native_dashboard(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("SBATCH_"):
            monkeypatch.delenv(name)
    config = Config(
        {
            "animations": False,
            "startup_animation": False,
            "log_lines": 0,
            "gpu_sampling": False,
            "smooth_scrolling": False,
        }
    )
    app = App(Store(persist=False), None, None, config, "student", interactive=False)
    app.width, app.height = 100, 30
    app.actions = SimpleNamespace(slurm=SimpleNamespace(calls=[]))
    views = Views(layout.Glyphs(True), config)
    app.views_ref = views
    path = tmp_path / "job.sh"
    path.write_text("#!/bin/bash\n#SBATCH --cpus-per-task=4\ntrue\n")
    app.run_command("preflight " + shlex.quote(str(path)))

    def paint_frame():
        snap = app.store.snapshot()
        views.compose(snap, app, app.width, app.height)
        views.overlay(snap, app, app.width, app.height)

    def native_click(identity):
        control = next(
            item
            for item in interaction.controls(app)
            if item.id == "execution:" + identity
        )
        for button in (curses.BUTTON1_PRESSED, curses.BUTTON1_RELEASED):
            screen._apply_input(
                app,
                ("mouse", (0, control.rect.left, control.rect.top, 0, button)),
                app.last_hits,
                curses,
            )

    paint_frame()
    yield app, paint_frame, native_click
    if app.research:
        app.research.close()


def test_actual_curses_preflight_mouse_path_edits_validates_and_opens_review(
    native_dashboard,
):
    app, paint_frame, native_click = native_dashboard
    native_click("field:cpus")
    assert app.execution_state["editing"] and app.execution_state["field"] == 2
    app.handle("ctrl-u")
    app.handle("8")
    paint_frame()
    native_click("apply")
    assert app.execution_state["values"]["cpus"] == "8"
    paint_frame()
    native_click("validate")
    paint_frame()
    assert app.execution_state["plan"]["valid"]
    native_click("review")
    assert app.mode == "confirm" and app.confirm["action"] == "submit"
    assert not app.actions.slurm.calls


def test_f8_semantic_control_uses_identical_field_editing_path(native_dashboard):
    app, _, _ = native_dashboard
    app.handle("f8")
    app.interaction_state.update(active=True, focused="execution:field:mem")
    app.handle("enter")
    assert app.execution_state["editing"] and app.execution_state["field"] == 3


@pytest.mark.parametrize("change", ["plan", "editor"])
def test_stale_f8_controls_cannot_bypass_fresh_plan_or_editor_guard(
    native_dashboard, change
):
    app, paint_frame, native_click = native_dashboard
    if change == "editor":
        native_click("field:cpus")
        paint_frame()
        control_id = "execution:apply"
    else:
        control_id = "execution:review"
    app.handle("f8")
    app.interaction_state.update(active=True, focused=control_id)
    if change == "editor":
        app.execution_state["edit"] = "16"
    else:
        app.execution_state["plan"] = dict(app.execution_state["plan"])
    app.handle("enter")
    assert app.mode == "execution" and not app.actions.slurm.calls
    assert not app.confirm
    if change == "editor":
        assert app.execution_state["editing"]
        assert app.execution_state["values"]["cpus"] == "4"
