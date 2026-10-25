"""Job actions use one frozen selection and the shared native mouse review."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from raven import screen

from raven import command_ui, layout
from raven.actions import Actions
from raven.config import Config
from raven.controller import App
from raven.jobs import group_drag, group_menu, manual_groups
from raven.model import Finished, Job, Store
from raven.views import Views
from test_job_group_mouse_integration import dispatch


@pytest.fixture
def desktop(monkeypatch):
    store = Store(persist=False)
    store.jobs = [
        Job(
            "101",
            "running-one",
            "local",
            "RUNNING",
            user="test",
            submit="2026-10-25T01:00:00",
            start="2026-10-25T01:01:00",
            cluster="local",
        ),
        Job(
            "102",
            "pending-one",
            "local",
            "PENDING",
            user="test",
            submit="2026-10-25T01:00:01",
            cluster="local",
        ),
        Job(
            "103",
            "held-one",
            "local",
            "PENDING",
            reason="JobHeldUser",
            user="test",
            submit="2026-10-25T01:00:02",
            cluster="local",
        ),
    ]
    store.finished = [
        Finished(
            "104",
            "completed-one",
            "COMPLETED",
            submit="2026-10-25T00:00:00",
            cluster="local",
        )
    ]
    calls = []
    slurm = SimpleNamespace(b=SimpleNamespace(host="local"))
    for action in ("cancel", "hold", "release", "requeue", "top"):
        setattr(
            slurm,
            action,
            lambda ids, action=action: (calls.append((action, tuple(ids))) or True, ""),
        )
    cfg = Config(
        {
            "animations": False,
            "startup_animation": False,
            "smooth_scrolling": False,
            "log_lines": 0,
        }
    )
    app = App(store, None, Actions(slurm, store), cfg, "test", interactive=False)
    app.views_ref = Views(layout.Glyphs(False), cfg)
    monkeypatch.setattr(app, "save", lambda: None)
    yield SimpleNamespace(app=app, store=store, calls=calls)
    if app.research:
        app.research.close()


def paint(desktop, width=140, height=36):
    app, store = desktop.app, desktop.store
    snap = store.snapshot()
    rows, hits = app.views_ref.compose(snap, app, width, height, app.actions)
    app.views_ref.overlay(snap, app, width, height)
    return rows, hits


def context(desktop, ids, pointed=None):
    app, store = desktop.app, desktop.store
    pointed = pointed or ids[0]
    return dict(
        scope=app.tab,
        ids=tuple(ids),
        selected=pointed,
        pointed=pointed,
        tokens=manual_groups.selection_tokens(store.snapshot(), ids),
    )


def choose(desktop, action):
    app = desktop.app
    state = group_menu.initialize(app)
    state["cursor"] = next(
        index for index, item in enumerate(state["items"]) if item["action"] == action
    )
    group_menu.handle_key(app, "enter")


def button_point(app, choice="confirm"):
    y, left, _, _ = next(
        hit for hit in app.command_state["confirm_hits"] if hit[3] == choice
    )
    return y, left + 2


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
@pytest.mark.parametrize(
    "verb,jid", [("cancel", "101"), ("hold", "102"), ("release", "103")]
)
def test_native_right_click_to_review_to_scheduler_end_to_end(
    desktop, route, verb, jid
):
    app = desktop.app
    paint(desktop)
    app.cursor["jobs"] = app.visible_ids.index(jid)
    app.sync_selection()
    _, hits = paint(desktop)
    frame = group_drag.initialize(app)["frame"]
    row = next(
        (y, item)
        for y, items in frame["rows"].items()
        for item in items
        if item["pointed"] == jid
    )
    position = row[0], row[1]["rect"][1] + 5
    dispatch(app, hits, position, "right", route)
    assert app.mode == group_menu.MODE and not desktop.calls
    _, hits = paint(desktop)
    state = group_menu.initialize(app)
    index = next(
        index
        for index, item in enumerate(state["items"])
        if item["action"] == "scheduler:" + verb
    )
    y, left, _, _ = next(hit for hit in state["hits"] if hit[3] == index)
    dispatch(app, hits, (y, left + 2), "press", route)
    assert app.mode == group_menu.MODE and not desktop.calls
    dispatch(app, hits, (y, left + 2), "release", route)
    assert app.mode == "confirm" and [job.id for job in app.confirm["jobs"]] == [jid]
    assert app.confirm["action"] == verb and not desktop.calls
    _, hits = paint(desktop)
    point = button_point(app)
    dispatch(app, hits, point, "press", route)
    assert not desktop.calls
    # Real redraws between native press and release must preserve the review.
    _, hits = paint(desktop)
    dispatch(app, hits, point, "release", route)
    assert desktop.calls == [(verb, (jid,))] and app.mode == "main"
    dispatch(app, hits, point, "release", route)
    assert desktop.calls == [(verb, (jid,))]


def test_marked_scope_stays_frozen_and_single_row_does_not_absorb_marks(desktop):
    app = desktop.app
    app.marks = {"101", "102"}
    assert group_menu.open_menu(app, context(desktop, ["101", "102", "103"]))
    app.marks = {"103"}
    choose(desktop, "scheduler:cancel")
    assert [job.id for job in app.confirm["jobs"]] == ["101", "102"]
    app.finish_confirm(True)
    assert desktop.calls == [("cancel", ("101", "102"))]
    assert app.marks == {"103"}
    assert group_menu.open_menu(app, context(desktop, ["101", "102", "103"], "102"))
    choose(desktop, "scheduler:hold")
    assert [job.id for job in app.confirm["jobs"]] == ["102"]


def test_unselected_right_click_prepares_exact_job_without_losing_other_marks(desktop):
    app = desktop.app
    app.selected_id = "103"
    app.marks = {"101", "103"}
    source = context(desktop, ["101", "102", "103"], "102")
    source["selected"] = "103"
    assert group_menu.open_menu(app, source)
    choose(desktop, "scheduler:hold")
    assert [job.id for job in app.confirm["jobs"]] == ["102"]
    assert app.selected_id == "103" and app.marks == {"101", "103"}
    assert not desktop.calls


def test_mixed_group_actions_name_their_exact_applicable_subset(desktop):
    app = desktop.app
    app.marks = {"101", "102", "103", "104"}
    assert group_menu.open_menu(app, context(desktop, ["101", "102", "103", "104"]))
    choices = {
        item["action"]: item["label"] for item in group_menu.initialize(app)["items"]
    }
    assert choices["scheduler:hold"] == "Hold pending jobs (1 of 4)…"
    assert choices["scheduler:release"] == "Release held jobs (1 of 4)…"
    assert choices["scheduler:cancel"] == "Cancel active jobs (3 of 4)…"
    assert choices["cancel"] == "Close menu"
    choose(desktop, "scheduler:hold")
    assert [job.id for job in app.confirm["jobs"]] == ["102"]
    assert "3 other selected jobs are excluded" in app.confirm["review"][0]
    assert not desktop.calls


@pytest.mark.parametrize("change", ["attempt", "connection", "eligibility", "missing"])
def test_changed_menu_target_aborts_before_review(desktop, change):
    app, store = desktop.app, desktop.store
    assert group_menu.open_menu(app, context(desktop, ["102"]))
    if change == "attempt":
        store.jobs[1] = replace(store.jobs[1], submit="2026-10-25T02:00:00")
    elif change == "connection":
        app.actions = Actions(app.actions.slurm, store)
    elif change == "eligibility":
        store.jobs[1] = replace(store.jobs[1], state="RUNNING")
    else:
        store.jobs.pop(1)
    choose(desktop, "scheduler:hold")
    assert app.mode == "main" and not desktop.calls
    assert "changed" in app.message.lower()


def test_review_attempt_copy_rejects_in_place_record_reuse(desktop):
    app, store = desktop.app, desktop.store
    assert group_menu.open_menu(app, context(desktop, ["101"]))
    choose(desktop, "scheduler:cancel")
    assert app.confirm["jobs"][0] is not store.jobs[0]
    store.jobs[0].submit = "2026-10-25T03:00:00"
    app.finish_confirm(True)
    assert not desktop.calls and "identity changed" in app.message


@pytest.mark.parametrize("field", ["cluster", "user", "account"])
@pytest.mark.parametrize("replacement", ["different", ""])
def test_review_retains_known_identity_anchors_until_the_action(
    desktop, field, replacement
):
    app, store = desktop.app, desktop.store
    setattr(store.jobs[0], field, "verified")
    assert group_menu.open_menu(app, context(desktop, ["101"]))
    choose(desktop, "scheduler:cancel")
    setattr(store.jobs[0], field, replacement)
    app.finish_confirm(True)
    assert not desktop.calls and f"{field} identity changed" in app.message


def test_collapsed_group_review_names_hidden_active_members_and_excludes_finished(
    desktop,
):
    from raven.jobs import group_domain

    app, store = desktop.app, desktop.store
    result = manual_groups.create(app, store.snapshot(), ["101", "102", "103", "104"])
    assert result.changed
    group = group_domain.registry(app).ensure(store.snapshot()).groups[result.group_id]
    source = context(desktop, ["101", "102", "103", "104"])
    source["source_group"] = group
    assert group_menu.open_menu(app, source)
    choose(desktop, "scheduler:cancel")
    assert [job.id for job in app.confirm["jobs"]] == ["101", "102", "103"]
    assert "1 other selected job is excluded" in app.confirm["review"][0]
    app.finish_confirm(True)
    assert desktop.calls == [("cancel", ("101", "102", "103"))]


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
def test_confirm_orphan_mismatch_and_background_gestures_are_inert(desktop, route):
    app = desktop.app
    app.run_command("cancel 101")
    _, hits = paint(desktop)
    point = button_point(app)
    dispatch(app, hits, point, "release", route)
    dispatch(app, hits, point, "motion", route)
    dispatch(app, hits, (0, 0), "press", route)
    dispatch(app, hits, point, "release", route)
    dispatch(app, hits, point, "press", route)
    dispatch(app, hits, button_point(app, "cancel"), "release", route)
    assert not desktop.calls and app.mode == "confirm"
    dispatch(app, hits, button_point(app, "cancel"), "press", route)
    dispatch(app, hits, button_point(app, "cancel"), "release", route)
    assert app.mode == "main" and app.message == "kept" and not desktop.calls


@pytest.mark.parametrize("change", ["geometry", "review", "tab", "scroll"])
def test_review_geometry_and_identity_must_match_its_painted_buttons(desktop, change):
    app = desktop.app
    app.run_command("cancel 101")
    _, hits = paint(desktop)
    point = button_point(app)
    app.click(*point, hits, button="press")
    if change == "geometry":
        app.width -= 1
    elif change == "review":
        app.confirm = dict(app.confirm, jobs=[desktop.store.jobs[1]])
    elif change == "tab":
        app.tab = "history"
    else:
        app.command_state["confirm_scroll"] += 1
    app.click(*point, hits, button="release")
    assert not desktop.calls and app.mode == "confirm"


def test_opening_click_aggregate_cannot_confirm_the_new_review(desktop):
    app = desktop.app
    app.run_command("cancel 101")
    paint(desktop)
    y, x = button_point(app)
    # A native adapter can append an aggregate click for the menu action that
    # opened this review. Only a fresh physical press clears that exact guard.
    import time

    app.confirm = dict(
        app.confirm, pointer_guard=(time.monotonic() + 10, y, x - 1, x + 2)
    )
    paint(desktop)
    command_ui.handle_mouse(app, y, x, "left")
    assert not desktop.calls and app.mode == "confirm"
    command_ui.handle_mouse(app, y, x, "press")
    command_ui.handle_mouse(app, y, x, "release")
    assert desktop.calls == [("cancel", ("101",))]


def test_native_confirmation_hover_and_wheel_do_no_scheduler_or_store_work(
    desktop, monkeypatch
):
    app = desktop.app
    app.run_command("cancel 101")
    paint(desktop)
    point = button_point(app)
    monkeypatch.setattr(
        desktop.store, "snapshot", lambda: pytest.fail("pointer requested a snapshot")
    )
    for _ in range(100):
        command_ui.handle_mouse(app, *point, "motion")
        command_ui.handle_mouse(app, *point, "wheel-down")
    assert app.mode == "confirm" and not desktop.calls


def test_no_scheduler_actions_for_completed_only_context_or_replay(desktop):
    app = desktop.app
    assert group_menu.open_menu(app, context(desktop, ["104"]))
    assert all(
        not item["action"].startswith("scheduler:")
        for item in group_menu.initialize(app)["items"]
    )
    group_menu.close(app)
    app.replay = object()
    assert group_menu.open_menu(app, context(desktop, ["101"]))
    assert all(
        not item["action"].startswith("scheduler:")
        for item in group_menu.initialize(app)["items"]
    )


def test_palette_selection_supports_native_press_release(desktop):
    app = desktop.app
    command_ui.open_palette(app)
    _, hits = paint(desktop)
    y, index = app.command_state["result_hits"][1]
    app.click(y, app.width // 2, hits, button="release")
    assert app.command_state["result_cursor"] == 0
    app.click(y, app.width // 2, hits, button="press")
    app.click(y, app.width // 2, hits, button="release")
    assert app.command_state["result_cursor"] == index and app.mode == "palette"
    assert not desktop.calls


def test_palette_result_selection_is_bounded_to_its_painted_box(desktop):
    app = desktop.app
    command_ui.open_palette(app)
    _, hits = paint(desktop)
    y, left, right, index = app.command_state["result_spans"][1]
    assert index != app.command_state["result_cursor"]
    outside = left - 1 if left > 0 else right
    app.click(y, outside, hits, button="left")
    assert app.command_state["result_cursor"] == 0 and app.mode == "palette"
    app.click(y, left, hits, button="left")
    assert app.command_state["result_cursor"] == index


@pytest.mark.parametrize("width", [40, 80, 140])
@pytest.mark.parametrize("choice", ["cancel", "confirm"])
def test_confirmation_duplicate_aggregate_cannot_activate_sidebar(
    desktop, width, choice
):
    app = desktop.app
    app.run_command("cancel 101")
    _, hits = paint(desktop, width=width)
    point = button_point(app, choice)
    dispatch(app, hits, point, "press", "native")
    dispatch(app, hits, point, "release", "native")
    assert app.mode == "main"
    _, hits = paint(desktop, width=width)
    before = (app.tab, app.selected_id, set(app.marks))
    import curses

    y, x = point
    screen._apply_input(
        app, ("mouse", (0, x, y, 0, curses.BUTTON1_CLICKED)), hits, curses
    )
    assert (app.tab, app.selected_id, app.marks) == before
    assert desktop.calls == ([("cancel", ("101",))] if choice == "confirm" else [])


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
def test_palette_visible_controls_complete_run_and_close_with_mouse(desktop, route):
    app = desktop.app
    command_ui.open_palette(app, "cancel 101")
    _, hits = paint(desktop)

    def action_point(name):
        y, _, value = next(
            hit
            for hit in app.command_state["palette_control_hits"]
            if hit[2]["id"] == "palette-action:" + name
        )
        return y, value["left"] + 1

    point = action_point("run")
    dispatch(app, hits, point, "press", route)
    assert app.mode == "palette" and not desktop.calls
    dispatch(app, hits, point, "release", route)
    assert (
        app.mode == "confirm"
        and app.confirm["action"] == "cancel"
        and not desktop.calls
    )
    app.finish_confirm(False)
    command_ui.open_palette(app, "the")
    _, hits = paint(desktop)
    point = action_point("complete")
    dispatch(app, hits, point, "press", route)
    dispatch(app, hits, point, "release", route)
    assert app.mode == "palette" and app.palette_edit.strip() == "theme"
    _, hits = paint(desktop)
    point = action_point("close")
    dispatch(app, hits, point, "press", route)
    dispatch(app, hits, point, "release", route)
    assert app.mode == "main" and not desktop.calls
