"""Queue grips own one frozen gesture and never mutate scheduling on motion."""
from copy import deepcopy
import curses
from types import SimpleNamespace

import pytest

from raven import group_queue_drag as D, job_group_drag as M, job_groups as G
from raven import job_selection as S, layout as L, manual_job_groups as MG, queue_order_ui as Q
from raven import screen
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store
from raven.views import Views
from test_job_group_mouse_integration import dispatch, point

REAL_PREPARE_DROP = Q.prepare_drop


@pytest.fixture
def dashboard(monkeypatch):
    store = Store(persist=False)
    store.jobs = [Job(str(jid), "independent-" + str(jid), "cpu", "PENDING",
                      submit="2026-10-10T12:00:00", user="tester", cluster="local")
                  for jid in range(1, 10)]
    cfg = Config({"animations": False, "startup_animation": False, "smooth_scrolling": False,
                  "gpu_sampling": False, "log_lines": 0, "workspace": {"density": "compact"}})
    app = App(store, None, None, cfg, "tester", interactive=False)
    app.views_ref = Views(L.Glyphs(False), cfg)
    monkeypatch.setattr(app, "save", lambda: None)
    groups = [MG.create(app, store.snapshot(), ids).group_id
              for ids in (("1", "2"), ("4", "5"), ("7", "8"))]
    calls = []
    monkeypatch.setattr(Q, "prepare_drop", lambda *args, **kwargs: calls.append((args, kwargs)))
    ui = SimpleNamespace(app=app, store=store, groups=groups, calls=calls)
    draw(ui)
    yield ui
    if app.research:
        app.research.close()


def draw(ui, *, ascii_=False, width=240, height=60):
    ui.app.views_ref.g = L.Glyphs(ascii_)
    ui.rows, ui.hits = ui.app.views_ref.compose(ui.store.snapshot(), ui.app, width, height)
    return ui.rows, ui.hits


def grip(ui, group):
    value = D.initialize(ui.app)["frame"]["headers"][group]
    return value["y"], value["left"]


def row(ui, jid):
    return point(ui.app, ui.hits, jid, "job")


def send(ui, position, button, route="app", **kwargs):
    dispatch(ui.app, ui.hits, position, button, route, **kwargs)


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
@pytest.mark.parametrize("collapsed", [False, True])
@pytest.mark.parametrize("direction", ["up", "down"])
def test_real_group_grips_prepare_exactly_one_frozen_before_or_after_review(dashboard, route, collapsed, direction):
    ui, app = dashboard, dashboard.app
    if collapsed:
        for gid in ui.groups:
            G.fold(app, gid, True)
        draw(ui)
    low, high = sorted(ui.groups, key=lambda gid: grip(ui, gid)[0])[::2]
    source, target = (high, low) if direction == "up" else (low, high)
    source_evidence = D.initialize(app)["frame"]["headers"][source]["evidence"]
    target_evidence = D.initialize(app)["frame"]["headers"][target]["evidence"]
    assert grip(ui, source)[1] == app.workspace_main_rect.x + 2
    original_groups = deepcopy(app.table_state["manual_groups"])
    original_selection = app.selected_id
    send(ui, grip(ui, source), "press", route)
    assert D.active(app) and not M.active(app) and not S.active(app)
    send(ui, grip(ui, target), "drag", route)
    assert not ui.calls
    send(ui, grip(ui, target), "release", route)
    assert len(ui.calls) == 1
    args, kwargs = ui.calls[0]
    assert args == (app, source_evidence, target_evidence)
    assert kwargs == {"placement": "before" if direction == "up" else "after"}
    assert app.table_state["manual_groups"] == original_groups
    assert app.selected_id == original_selection
    assert not D.active(app) and app.tab == "jobs" and app.mode == "main"
    # Duplicate releases cannot re-run preparation or activate target controls.
    send(ui, grip(ui, target), "release", route)
    assert len(ui.calls) == 1
    assert G.registry(app).is_collapsed(G.registry(app).index.groups[target]) == collapsed


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
def test_plain_group_row_drag_selects_while_alt_job_drag_changes_membership(dashboard, route):
    ui, app = dashboard, dashboard.app
    original = deepcopy(app.table_state["manual_groups"])
    send(ui, row(ui, "1"), "press", route)
    send(ui, row(ui, "3"), "drag", route)
    send(ui, row(ui, "3"), "release", route)
    # Whole-batch sorting puts independent rows 3, 6, 9 before group 1, 2.
    assert app.marks == {"1", "3", "6", "9"} and not ui.calls
    assert app.table_state["manual_groups"] == original
    app.marks = {"3"}
    S._bind_marks(app, ("3",))
    draw(ui)
    send(ui, row(ui, "3"), "press", route, alt=True)
    assert M.active(app) and not D.active(app)
    send(ui, row(ui, "4"), "drag", route, alt=True)
    send(ui, row(ui, "4"), "release", route, alt=True)
    assert G.registry(app).ensure(ui.store.snapshot()).for_job("3").members == ("3", "4", "5")
    assert not ui.calls


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
@pytest.mark.parametrize("modifier", ["alt", "shift"])
def test_modified_grip_does_not_reorder_or_move_job_membership(dashboard, route, modifier):
    ui, app = dashboard, dashboard.app
    original = deepcopy(app.table_state["manual_groups"])
    app.marks = {"1", "2"}
    S._bind_marks(app, app.marks)
    send(ui, grip(ui, ui.groups[0]), "press", route, **{modifier: True})
    send(ui, grip(ui, ui.groups[-1]), "drag", route, **{modifier: True})
    send(ui, grip(ui, ui.groups[-1]), "release", route, **{modifier: True})
    assert not ui.calls and not D.active(app) and not M.active(app)
    assert app.table_state["manual_groups"] == original


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
@pytest.mark.parametrize("change", ["resize", "tab", "toolbar", "source-attempt", "target-attempt",
                                    "source-membership", "target-membership", "fold", "sort"])
def test_stale_painted_drag_cancels_and_eats_late_release(dashboard, route, change):
    ui, app = dashboard, dashboard.app
    source, target = ui.groups[0], ui.groups[-1]
    source_point, target_point = grip(ui, source), grip(ui, target)
    send(ui, source_point, "press", route)
    send(ui, target_point, "drag", route)
    assert D.active(app)
    if change == "resize":
        app.width -= 1
    elif change == "tab":
        app.tab = "research"
    elif change == "toolbar":
        app.toolbar_state["menu"] = "view"
    elif change.endswith("attempt"):
        ui.store.jobs[0 if change.startswith("source") else 6].submit = "2026-10-11T12:00:00"
        draw(ui)
    elif change.endswith("membership"):
        MG.detach(app, ui.store.snapshot(), ["2" if change.startswith("source") else "8"])
    elif change == "fold":
        G.fold(app, target, True)
    else:
        app.reverse["jobs"] = not app.reverse.get("jobs", False)
    send(ui, target_point, "release", route)
    send(ui, target_point, "release", route)
    assert not D.active(app) and not ui.calls
    assert app.tab == ("research" if change == "tab" else "jobs")


@pytest.mark.parametrize("which", ["source", "target"])
def test_reused_attempt_without_repaint_reaches_validator_but_not_operations(dashboard, monkeypatch, which):
    ui, app = dashboard, dashboard.app
    # Restore the real last-mile validator. No scheduler adapter should open.
    from raven import ops_ui
    monkeypatch.setattr(Q, "prepare_drop", REAL_PREPARE_DROP)
    monkeypatch.setattr(ops_ui, "_open", lambda *_a, **_kw: pytest.fail("Stale evidence opened an operation"))
    send(ui, grip(ui, ui.groups[0]), "press")
    target = grip(ui, ui.groups[-1])
    send(ui, target, "drag")
    ui.store.jobs[0 if which == "source" else 6].submit = "2026-10-11T12:00:00"
    send(ui, target, "release")
    assert app.mode == "main" and not D.active(app)


def test_repaint_cannot_retarget_a_drop_under_an_unmoved_pointer(dashboard):
    ui, app = dashboard, dashboard.app
    for gid in ui.groups:
        G.fold(app, gid, True)
    draw(ui)
    source, target = ui.groups[:2]
    target_point = grip(ui, target)
    send(ui, grip(ui, source), "press")
    send(ui, target_point, "drag")
    assert D.active(app)
    # One unrelated job disappears. Both frozen groups stay valid, but the
    # third group now sits beneath the pointer in the sorted batch layout.
    ui.store.jobs[:] = [job for job in ui.store.jobs if job.id != "3"]
    draw(ui)
    assert grip(ui, ui.groups[-1]) == target_point
    send(ui, target_point, "release")
    assert not ui.calls and not D.active(app)


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
@pytest.mark.parametrize("end", ["esc", "right", "own-group", "details", "tabs"])
def test_cancel_or_invalid_drop_never_activates_underlying_owner(dashboard, route, end):
    ui, app = dashboard, dashboard.app
    original_groups = deepcopy(app.table_state["manual_groups"])
    start = grip(ui, ui.groups[0])
    send(ui, start, "press", route)
    if end == "esc":
        send(ui, grip(ui, ui.groups[-1]), "drag", route)
        app.handle("esc")
        target = grip(ui, ui.groups[-1])
    elif end == "right":
        target = grip(ui, ui.groups[-1])
        send(ui, target, "right", route)
    elif end == "own-group":
        target = row(ui, "2")
    elif end == "details":
        rect = app.job_panel_rect
        target = rect.y + 1, rect.x + 4
    else:
        y, left, _, _ = next(hit for hit in app.tab_hits if hit[3] == "research")
        target = y, left
    send(ui, target, "drag", route)
    send(ui, target, "release", route)
    assert not ui.calls and not D.active(app)
    assert app.tab == "jobs" and app.mode == "main"
    assert app.table_state["manual_groups"] == original_groups


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("collapsed", [False, True])
def test_ghost_dims_only_source_main_rows_and_preserves_details_and_copy_text(dashboard, ascii_, collapsed):
    ui, app = dashboard, dashboard.app
    if collapsed:
        G.fold(app, ui.groups[0], True)
    draw(ui, ascii_=ascii_)
    rows = deepcopy(app.frame_rows)
    original = deepcopy(rows)
    frame = D.initialize(app)["frame"]
    main = app.workspace_main_rect
    send(ui, grip(ui, ui.groups[0]), "press")
    target = grip(ui, ui.groups[-1])
    send(ui, target, "drag")
    rendered = D.feedback(app, rows)
    assert rows == original and rendered is not rows
    assert "Release to review" in L.row_text(rendered[-1])
    assert (":" if ascii_ else "↕") in L.row_text(rendered[target[0]])[:main.width]
    assert "(2 jobs)" in L.row_text(rendered[target[0]])[:main.width]
    members = frame["members"][ui.groups[0]]
    assert len(members) == (1 if collapsed else 2)
    for y, rect in members:
        assert any("dim" in style for _, style in rendered[y])
    for before, after in zip(original[:-1], rendered[:-1]):
        assert L.row_text(before)[main.x + main.width:] == L.row_text(after)[main.x + main.width:]
    assert not ui.calls


def test_hover_and_drag_feedback_perform_no_store_reads_or_group_inference(dashboard, monkeypatch):
    ui, app = dashboard, dashboard.app
    source, target = grip(ui, ui.groups[0]), grip(ui, ui.groups[-1])
    send(ui, source, "press")
    with monkeypatch.context() as guard:
        guard.setattr(ui.store, "snapshot", lambda: pytest.fail("Queue pointer copied scheduler state"))
        guard.setattr(G.registry(app), "ensure", lambda *_: pytest.fail("Queue pointer inferred groups"))
        guard.setattr(app, "run_command", lambda *_: pytest.fail("Queue pointer activated a command"))
        for _ in range(30):
            for position in (source, target, (0, 30), (20, app.width - 2)):
                send(ui, position, "drag", "sgr")
                D.feedback(app, app.frame_rows)
    assert not ui.calls
    app.handle("esc")
    assert not D.active(app)


@pytest.mark.parametrize("flag", [curses.BUTTON1_CLICKED, curses.BUTTON1_DOUBLE_CLICKED])
def test_synthesized_grip_clicks_do_not_enter_job_or_start_scheduler_review(dashboard, monkeypatch, flag):
    ui, app = dashboard, dashboard.app
    y, x = grip(ui, ui.groups[0])
    prior = app.selected_id
    monkeypatch.setattr(app, "handle", lambda *_: pytest.fail("Grip click activated a row's Enter action"))
    screen._apply_input(app, ("mouse", (0, x, y, 0, flag)), ui.hits, curses)
    assert not ui.calls and not D.active(app) and not S.active(app)
    assert app.selected_id == prior and app.mode == "main" and app.tab == "jobs"


def test_unheld_sgr_motion_cancels_lost_release_without_preparing_a_drop(dashboard):
    ui, app = dashboard, dashboard.app
    source, target = grip(ui, ui.groups[0]), grip(ui, ui.groups[-1])
    send(ui, source, "press", "sgr")
    send(ui, target, "drag", "sgr")
    assert D.active(app)
    send(ui, target, "motion", "sgr")
    assert not D.active(app)
    send(ui, target, "release", "sgr")
    assert not ui.calls and app.tab == "jobs"


def test_native_motion_without_held_metadata_preserves_captured_grip(dashboard):
    ui, app = dashboard, dashboard.app
    source, target = grip(ui, ui.groups[0]), grip(ui, ui.groups[-1])
    send(ui, source, "press", "native")
    send(ui, target, "motion", "native")
    assert D.active(app) and not ui.calls
    send(ui, target, "release", "native")
    assert len(ui.calls) == 1 and not D.active(app)


def test_queue_ghost_does_not_cover_recents(dashboard):
    ui, app = dashboard, dashboard.app
    ui.store.finished = [Finished("21", "completed run", "COMPLETED")]
    draw(ui)
    recent_y = next(y for y, kind, _ in ui.hits if kind == "recent")
    before = deepcopy(app.frame_rows)
    send(ui, grip(ui, ui.groups[0]), "press")
    send(ui, (recent_y, 12), "drag")
    rendered = D.feedback(app, before)
    assert rendered[recent_y] == before[recent_y]
    send(ui, (recent_y, 12), "release")
    assert not ui.calls and not D.active(app)


def test_real_frame_cache_retains_grip_hover_and_column_targets_without_recompose(dashboard, monkeypatch):
    ui, app = dashboard, dashboard.app
    cache = screen._FrameCache()
    cache.rebuild(app, app.views_ref, ui.store, None, app.width, app.height)
    ui.hits = cache.hits
    key = "queue-grip:" + ui.groups[0]
    position = grip(ui, ui.groups[0])
    graph = app.interaction_state["graph"]
    assert graph.get(key) is not None
    column_ids = {control.id for control in graph.controls if control.id.startswith("column-resize:")}
    assert column_ids
    with monkeypatch.context() as guard:
        guard.setattr(app.views_ref, "compose", lambda *_a, **_kw: pytest.fail("Grip hover recomposed the page"))
        guard.setattr(ui.store, "snapshot", lambda: pytest.fail("Grip hover copied scheduler state"))
        for _ in range(20):
            send(ui, position, "motion", "sgr")
            cache.feedback(app, app.views_ref)
            assert app.interaction_state["hovered"] == key
        # A lightweight toolbar repaint must retain all published content
        # handles; it cannot replace the content's control graph with the bar.
        app.toolbar_state["focus"] = "copy"
        cache.feedback(app, app.views_ref)
        graph = app.interaction_state["graph"]
        assert graph.get(key) is not None
        assert all(graph.get(identifier) is not None for identifier in column_ids)
    assert not ui.calls and not D.active(app)
