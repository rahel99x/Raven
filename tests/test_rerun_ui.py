"""Exact cancelled attempts survive menu, terminal and worker transitions.

Scheduler fidelity is covered by the rerun service tests. These tests use the
real controller and operations lifecycle while replacing the scheduler action,
so no gesture can submit or requeue an actual allocation.
"""
from dataclasses import asdict
from types import SimpleNamespace
import curses

import pytest

from tower import job_group_menu, job_groups, job_selection, manual_job_groups
from tower import operations, ops_ui, screen
from tower.model import Finished, Job
from test_job_group_mouse_integration import dispatch, menu_point, point
from test_operations_sequences import operation_dashboard


@pytest.fixture
def dashboard(operation_dashboard, monkeypatch):
    dashboard = operation_dashboard
    app, store = dashboard.app, dashboard.store
    store.finished = [
        Finished("90", "cancelled-alpha", "CANCELLED", submit="2026-10-09T11:00:00",
                 start="2026-10-09T11:01:00", end="2026-10-09T11:02:00", user="test"),
        Finished("91", "cancelled-beta", "CANCELLED by 1000", submit="2026-10-09T11:10:00",
                 start="2026-10-09T11:11:00", end="2026-10-09T11:12:00", user="test"),
        Finished("92", "completed-gamma", "COMPLETED", submit="2026-10-09T11:20:00",
                 start="2026-10-09T11:21:00", end="2026-10-09T11:22:00", user="test"),
    ]
    app.cfg.set("smooth_scrolling", False)
    monkeypatch.setattr(app, "save", lambda: None)
    app.selected_id = "90"
    return dashboard


def draw(dashboard, tab=None):
    if tab:
        dashboard.app.tab = tab
    rows, hits, overlay = dashboard.draw(220, 65)
    dashboard.app.last_hits = hits
    return rows, hits, overlay


def source(dashboard, identifier="90", ids=None):
    app, store = dashboard.app, dashboard.store
    identifiers = tuple(ids or (job.id for job in store.finished))
    return dict(scope=app.tab, ids=identifiers, pointed=identifier, selected=identifier,
                tokens=manual_job_groups.selection_tokens(store.snapshot(), identifiers))


def actions(app):
    return [item["action"] for item in app.job_group_menu_state["items"]]


def select_action(app, action):
    state = app.job_group_menu_state
    state["cursor"] = actions(app).index(action)
    app.handle("enter")


def reviewed_inspection(monkeypatch):
    calls = []

    def inspect(feature, params, ctx):
        calls.append((feature, params.copy(), ctx))
        return operations.report(feature, "Review the exact cancelled attempt", plan=operations.prepare_plan(
            ctx, feature, params, {"job_id": params["job_id"], "action": "requeue"}))

    monkeypatch.setattr(operations, "run", inspect)
    return calls


@pytest.mark.parametrize("tab,kind", [("jobs", "recent"), ("history", "fin")])
@pytest.mark.parametrize("route", ["app", "native", "sgr"])
def test_cancelled_row_context_rerun_inspects_only_that_job(dashboard, monkeypatch, tab, kind, route):
    app = dashboard.app
    inspected = reviewed_inspection(monkeypatch)
    applied = []
    monkeypatch.setattr(operations, "apply", lambda *args: applied.append(args))
    _, hits, _ = draw(dashboard, tab)
    location = point(hits, "90", kind)
    dispatch(app, hits, location, "press", route)
    dispatch(app, hits, location, "release", route)
    _, hits, _ = draw(dashboard)
    dispatch(app, hits, point(hits, "90", kind), "right", route)
    assert app.mode == "job_group_menu"
    assert "rerun" in actions(app)
    _, hits, _ = draw(dashboard)
    target = menu_point(app, "Rerun")
    dispatch(app, hits, target, "motion", route)
    assert inspected == []
    dispatch(app, hits, target, "press", route)
    dispatch(app, hits, target, "release", route)
    assert [(feature, params) for feature, params, _ in inspected] == [("rerun-job", {"job_id": "90"})]
    assert app.mode == "operations" and app.tab == tab
    assert app.ops_state["view"] == "report" and app.ops_state["result"]["plan"]
    assert applied == []


@pytest.mark.parametrize("state", ["CANCELLED", "CANCELLED by 1000", "CANCELLED+"])
def test_cancelled_accounting_state_variants_offer_rerun(dashboard, state):
    dashboard.store.finished[0].state = state
    assert job_group_menu.open_menu(dashboard.app, source(dashboard))
    assert "rerun" in actions(dashboard.app)


@pytest.mark.parametrize("state", ["", "CANCELLED+COMPLETING", "CANCELLED by other", "CANCELLED+RUNNING"])
def test_incomplete_or_ambiguous_accounting_state_does_not_offer_rerun(dashboard, state):
    dashboard.store.finished[0].state = state
    assert job_group_menu.open_menu(dashboard.app, source(dashboard))
    assert "rerun" not in actions(dashboard.app)


@pytest.mark.parametrize("case", ["multi", "completed", "failed", "live", "active-duplicate",
                                  "duplicate-finished", "array-aggregate", "heterogeneous", "replay", "collapsed"])
def test_ambiguous_or_ineligible_selection_never_offers_rerun(dashboard, case):
    app, store = dashboard.app, dashboard.store
    if case == "multi":
        app.marks = {"90", "91"}
    elif case in ("completed", "failed", "live"):
        store.finished[0].state = {"completed": "COMPLETED", "failed": "FAILED", "live": "RUNNING"}[case]
    elif case == "active-duplicate":
        store.jobs.append(Job("90", "new-attempt", "main", "PENDING"))
    elif case == "duplicate-finished":
        store.finished.append(Finished(**asdict(store.finished[0])))
    elif case in ("array-aggregate", "heterogeneous"):
        store.finished[0].id = "90_[1-4]" if case == "array-aggregate" else "90+1"
    elif case == "replay":
        app.replay = SimpleNamespace()
    else:
        result = manual_job_groups.create(app, store.snapshot(), ["90", "91"])
        assert result.changed
        app.table_state["groups"] = True
        job_groups.fold(app, result.group_id, True)
    job_group_menu.open_menu(app, source(dashboard, store.finished[0].id))
    assert "rerun" not in actions(app)


def test_exposed_array_member_can_rerun_without_absorbing_unrelated_marks(dashboard, monkeypatch):
    app, store = dashboard.app, dashboard.store
    store.finished[0].id, store.finished[1].id = "90_7", "90_8"
    result = manual_job_groups.create(app, store.snapshot(), ["90_7", "90_8"])
    assert result.changed
    app.table_state["groups"] = True
    job_groups.fold(app, result.group_id, False)
    app.marks = {"81", "82"}
    inspected = reviewed_inspection(monkeypatch)
    assert job_group_menu.open_menu(app, source(dashboard, "90_7"))
    assert "rerun" in actions(app)
    select_action(app, "rerun")
    assert inspected[0][1] == {"job_id": "90_7"}
    assert app.marks == {"81", "82"}


@pytest.mark.parametrize("field,value", [("submit", "2026-10-10T11:00:00"),
    ("start", "2026-10-10T11:01:00"), ("end", "2026-10-10T11:02:00"),
    ("state", "COMPLETED"), ("name", "different-source"), ("user", "different-user")])
def test_cancelled_row_changed_after_menu_open_cannot_inspect_replacement(dashboard, monkeypatch, field, value):
    app, store = dashboard.app, dashboard.store
    inspected = reviewed_inspection(monkeypatch)
    assert job_group_menu.open_menu(app, source(dashboard))
    setattr(store.finished[0], field, value)
    select_action(app, "rerun")
    assert inspected == []
    assert app.mode == "main" and "changed" in app.message.lower()


def test_more_expansion_invalidates_old_rerun_coordinates_and_token(dashboard, monkeypatch):
    app, store = dashboard.app, dashboard.store
    for first in (100, 110, 120, 130, 140):
        store.jobs.extend(Job(str(jid), f"job-{jid}", "main", "RUNNING",
                             submit=f"2026-10-09T12:{jid % 60:02}:00") for jid in (first, first + 1))
        assert manual_job_groups.create(app, store.snapshot(), [str(first), str(first + 1)]).changed
    inspected = reviewed_inspection(monkeypatch)
    assert job_group_menu.open_menu(app, source(dashboard))
    draw(dashboard)
    state = app.job_group_menu_state
    token, old_rerun = state["token"], actions(app).index("rerun")
    assert actions(app).count("add") == 3 and "more" in actions(app)
    select_action(app, "more")
    assert state["token"] != token and actions(app).count("add") == 5
    app.run_command(f"jobgroupmenu choose {token} {old_rerun}")
    assert inspected == [] and app.mode == "job_group_menu"
    _, hits, _ = draw(dashboard)
    target = menu_point(app, "Rerun")
    dispatch(app, hits, target, "release", "sgr")
    assert inspected == []
    dispatch(app, hits, target, "press", "sgr")
    dispatch(app, hits, target, "release", "sgr")
    assert len(inspected) == 1 and inspected[0][1]["job_id"] == "90"


def test_rerun_menu_hover_and_feedback_never_collect_scheduler_data(dashboard, monkeypatch):
    app = dashboard.app
    assert job_group_menu.open_menu(app, source(dashboard))
    _, hits, _ = draw(dashboard)
    locations = [(y, left) for y, left, _, _ in app.job_group_menu_state["hits"]]

    def forbidden(*_args, **_kwargs):
        pytest.fail("Rerun menu hover started evidence collection")

    monkeypatch.setattr(operations, "run", forbidden)
    monkeypatch.setattr(dashboard.slurm.b, "run", forbidden)
    monkeypatch.setattr(dashboard.store, "snapshot", forbidden)
    monkeypatch.setattr(ops_ui, "context", forbidden)
    for _ in range(20):
        for location in locations:
            dispatch(app, hits, location, "motion", "sgr")
    assert app.mode == "job_group_menu" and app.tab == "jobs"


def test_worker_delay_captures_original_record_and_review_rejects_new_attempt(dashboard, monkeypatch):
    app, store = dashboard.app, dashboard.store
    inspected = reviewed_inspection(monkeypatch)
    queued = []
    monkeypatch.setattr(app.research, "start_task", lambda work, done: queued.append((work, done)) or True)
    app.interactive = True
    original = asdict(store.finished[0])
    assert job_group_menu.open_menu(app, source(dashboard))
    select_action(app, "rerun")
    assert app.ops_state["running"] and inspected == []
    store.finished[0].submit = "2026-10-10T11:00:00"
    app.selected_id = "91"
    work, done = queued.pop()
    done(work())
    assert inspected[0][1] == {"job_id": "90"}
    assert inspected[0][2].finished[0] == original
    app.run_command("ops review")
    assert app.ops_state["view"] == "report"
    assert "changed" in app.message.lower()


@pytest.mark.parametrize("explicit", [True, False])
def test_rerun_alias_binds_exact_id_and_ignores_other_marks(dashboard, monkeypatch, explicit):
    app = dashboard.app
    app.selected_id = "91" if explicit else "90"
    app.marks = {"81", "82", "91"}
    job_selection._bind_marks(app, app.marks)
    inspected = reviewed_inspection(monkeypatch)
    app.run_command("rerun 90" if explicit else "rerun")
    assert len(inspected) == 1 and inspected[0][1] == {"job_id": "90"}
    assert app.marks == {"81", "82", "91"}
    assert app.selected_id == ("91" if explicit else "90")


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
@pytest.mark.parametrize("dispatched", [False, True])
def test_review_is_required_and_one_use_with_refresh_after_dispatch(dashboard, monkeypatch, route, dispatched):
    app = dashboard.app
    reviewed_inspection(monkeypatch)
    applied, refreshed = [], []

    def apply(feature, plan, ctx):
        applied.append((feature, plan, ctx))
        return operations.report(feature, "Rerun attempt recorded", data={"rerun_dispatched": dispatched})

    monkeypatch.setattr(operations, "apply", apply)
    monkeypatch.setattr(app.sampler, "refresh_all", lambda: refreshed.append(True))
    app.run_command("rerun 90")
    app.run_command("ops apply")
    assert applied == []
    app.run_command("ops review")
    app.run_command("ops apply")
    assert applied == []
    _, hits, _ = draw(dashboard)
    control = next(c for c in app.interaction_state["graph"].controls if c.id == "ops:apply")
    position = (control.rect.top, control.rect.left)
    dispatch(app, hits, position, "release", route)
    assert applied == []
    dispatch(app, hits, position, "press", route)
    dispatch(app, hits, position, "release", route)
    dispatch(app, hits, position, "press", route)
    dispatch(app, hits, position, "release", route)
    app.run_command("ops apply")
    assert len(applied) == 1 and applied[0][1]["params"]["job_id"] == "90"
    assert refreshed == ([True] if dispatched else [])


def test_inspection_completed_after_close_does_not_reopen_a_page(dashboard, monkeypatch):
    app = dashboard.app
    reviewed_inspection(monkeypatch)
    queued = []
    monkeypatch.setattr(app.research, "start_task", lambda work, done: queued.append((work, done)) or True)
    app.interactive = True
    app.run_command("rerun 90")
    work, done = queued.pop()
    # Finish collecting before cancellation, but delay publication until later.
    result = work()
    ops_ui.close(app)
    app.enter_tab("history")
    done(result)
    assert app.mode == "main" and app.tab == "history"
    assert app.ops_state["result"] is None
    assert not app.ops_state["running"]


def test_busy_worker_can_retry_inspection_without_claiming_an_action(dashboard, monkeypatch):
    app = dashboard.app
    inspected = reviewed_inspection(monkeypatch)
    monkeypatch.setattr(app.research, "start_task", lambda *_args: False)
    app.interactive = True
    app.run_command("rerun 90")
    assert not app.ops_state["running"] and inspected == []
    assert not app.ops_state["used"] and "busy" in app.message.lower()
    app.interactive = False
    app.run_command("ops inspect")
    assert len(inspected) == 1 and app.ops_state["result"]["plan"]


@pytest.mark.parametrize("stage", ["inspect", "apply"])
def test_connection_changed_before_completion_does_not_publish_action_on_new_cluster(dashboard, monkeypatch, stage):
    app = dashboard.app
    reviewed_inspection(monkeypatch)
    refreshed, queued = [], []
    monkeypatch.setattr(app.sampler, "refresh_all", lambda: refreshed.append(True))
    monkeypatch.setattr(operations, "apply", lambda feature, plan, ctx: operations.report(
        feature, "Accepted on previous connection", data={"rerun_dispatched": True, "receipt": "original"}))
    monkeypatch.setattr(app.research, "start_task", lambda work, done: queued.append((work, done)) or True)
    if stage == "apply":
        app.run_command("rerun 90")
        app.run_command("ops review")
        draw(dashboard)
    app.interactive = True
    app.run_command("rerun 90" if stage == "inspect" else "ops apply")
    work, done = queued.pop()
    result = work()
    app.cfg.set("host", "another-cluster.example")
    done(result)
    assert not app.ops_state["running"] and refreshed == []
    if stage == "inspect":
        assert app.ops_state["result"] is None
    else:
        assert app.ops_state["result"]["data"]["receipt"] == "original"
        assert "plan" not in app.ops_state["result"]
        assert "previous connection" in app.ops_state["error"]


class RetainedScheduler:
    """A strict argv transport for the complete service/UI review lifecycle."""
    def __init__(self, source, identifier):
        self.identifier = identifier
        self.calls = []
        self.record = dict(JobId="90", UserId="test(1000)", JobState="CANCELLED", BatchFlag="1",
                           Requeue="1", Restarts="0", SubmitTime=source.submit, StartTime=source.start,
                           EndTime=source.end, Dependency="afterok:71,afterany:72", WorkDir="/original/run",
                           Command="/original/batch.sh", StdOut="/original/out.log", StdErr="/original/err.log")
        if "_" in identifier:
            self.record.update(ArrayJobId=identifier.split("_")[0], ArrayTaskId=identifier.split("_")[1])

    def run(self, argv, timeout):
        self.calls.append(tuple(argv))
        if argv == ["scontrol", "show", "job", "-o", self.identifier]:
            return " ".join(f"{key}={value}" for key, value in self.record.items()), 0
        if argv == ["scontrol", "write", "batch_script", self.identifier, "-"]:
            return "#!/bin/bash\nprintf '%s' \"$ORIGINAL_PARAMETER\"\n", 0
        if argv == ["scontrol", "requeue", self.identifier]:
            self.record.update(JobState="PENDING", Restarts="1", SubmitTime="2026-10-10T11:00:00")
            return "", 0
        raise AssertionError(f"Unexpected scheduler command: {argv!r}")


@pytest.mark.parametrize("state", ["CANCELLED", "CANCELLED by 1000", "CANCELLED+"])
@pytest.mark.parametrize("identifier", ["90", "90_7"])
def test_real_operation_recovers_stored_script_and_requeues_exact_id_after_review(dashboard, monkeypatch, state, identifier):
    app, store = dashboard.app, dashboard.store
    store.finished[0].id, store.finished[0].state = identifier, state
    scheduler = RetainedScheduler(store.finished[0], identifier)
    dashboard.slurm.b = scheduler
    refreshed = []
    monkeypatch.setattr(app.sampler, "refresh_all", lambda: refreshed.append(True))
    assert job_group_menu.open_menu(app, source(dashboard, identifier))
    select_action(app, "rerun")
    result = app.ops_state["result"]
    assert result["status"] == "review", result
    assert result["data"]["dependency"] == "afterok:71,afterany:72"
    assert result["data"]["script_sha256"]
    assert all(call[1] != "requeue" for call in scheduler.calls)
    app.run_command("ops review")
    draw(dashboard)
    app.run_command("ops apply")
    result = app.ops_state["result"]
    assert result["status"] == "ok", result
    assert result["data"]["confirmed"]
    assert [call for call in scheduler.calls if call[1] == "requeue"] == [("scontrol", "requeue", identifier)]
    assert refreshed == [True]
    assert scheduler.record["Dependency"] == "afterok:71,afterany:72"
    app.run_command("ops apply")
    assert [call for call in scheduler.calls if call[1] == "requeue"] == [("scontrol", "requeue", identifier)]


def test_dependency_changed_after_review_blocks_dispatch_in_full_ui_lifecycle(dashboard, monkeypatch):
    app = dashboard.app
    scheduler = RetainedScheduler(dashboard.store.finished[0], "90")
    dashboard.slurm.b = scheduler
    refreshed = []
    monkeypatch.setattr(app.sampler, "refresh_all", lambda: refreshed.append(True))
    app.run_command("rerun 90")
    app.run_command("ops review")
    draw(dashboard)
    scheduler.record["Dependency"] = "afterok:73"
    app.run_command("ops apply")
    assert app.ops_state["result"]["status"] == "error"
    assert "changed" in app.ops_state["result"]["summary"].lower()
    assert not any(call[1] == "requeue" for call in scheduler.calls)
    assert refreshed == []


def compact_groups(dashboard):
    app, store = dashboard.app, dashboard.store
    for first in (100, 110, 120, 130, 140):
        store.jobs.extend(Job(str(jid), f"separate-{jid}", "main", "RUNNING",
                             submit=f"2026-10-09T12:{jid % 60:02}:00") for jid in (first, first + 1))
        assert manual_job_groups.create(app, store.snapshot(), [str(first), str(first + 1)]).changed
    assert job_group_menu.open_menu(app, source(dashboard))
    _, hits, _ = dashboard.draw(220, 15)
    app.last_hits = hits
    return hits, menu_point(app, "More (")


def aggregate(app, hits, location, kind):
    if kind == "direct-double":
        app.click(*location, hits, button="double")
    else:
        flag = curses.BUTTON1_CLICKED if kind == "native-click" else curses.BUTTON1_DOUBLE_CLICKED
        screen._apply_input(app, ("mouse", (0, location[1], location[0], 0, flag)), hits, curses)


def pointer_expand(dashboard, initial="pair"):
    app = dashboard.app
    hits, location = compact_groups(dashboard)
    if initial == "pair":
        dispatch(app, hits, location, "press", "native")
        dispatch(app, hits, location, "release", "native")
    else:
        aggregate(app, hits, location, "native-click")
    assert app.job_group_menu_state["expanded"]
    _, hits, _ = dashboard.draw(220, 15)
    app.last_hits = hits
    state = app.job_group_menu_state
    item = next(index for y, left, right, index in state["hits"]
                if y == location[0] and left <= location[1] < right)
    assert state["items"][item]["action"] == "add"
    return hits, location, state["items"][item]["group_id"]


@pytest.mark.parametrize("initial", ["pair", "aggregate"])
@pytest.mark.parametrize("duplicate", ["native-click", "native-double", "direct-double"])
def test_more_repaint_rejects_duplicate_completed_click_without_new_press(dashboard, monkeypatch, initial, duplicate):
    monkeypatch.setattr(job_group_menu.time, "monotonic", lambda: 100.0)
    app = dashboard.app
    hits, location, _ = pointer_expand(dashboard, initial)
    aggregate(app, hits, location, duplicate)
    assert app.mode == "job_group_menu"
    assert job_groups.registry(app).ensure(dashboard.store.snapshot()).for_job("90") is None
    assert app.job_group_menu_state["aggregate_guard"] is not None
    # The guard is limited to mouse aggregates; keyboard Cancel stays immediate.
    select_action(app, "cancel")
    assert app.mode == "main" and app.job_group_menu_state["aggregate_guard"] is None


@pytest.mark.parametrize("release_guard", ["new-press", "motion-outside", "expiry"])
def test_more_guard_allows_deliberate_next_action_without_delay(dashboard, monkeypatch, release_guard):
    now = [100.0]
    monkeypatch.setattr(job_group_menu.time, "monotonic", lambda: now[0])
    app = dashboard.app
    hits, location, destination = pointer_expand(dashboard)
    if release_guard == "new-press":
        dispatch(app, hits, location, "press", "native")
        assert app.job_group_menu_state["aggregate_guard"] is None
        dispatch(app, hits, location, "release", "native")
    else:
        if release_guard == "motion-outside":
            dispatch(app, hits, (0, 0), "motion", "native")
            assert app.job_group_menu_state["aggregate_guard"] is None
        else:
            now[0] += job_group_menu.AGGREGATE_GUARD_SECONDS
        aggregate(app, hits, location, "native-click")
    assert app.mode == "main"
    assert job_groups.registry(app).ensure(dashboard.store.snapshot()).for_job("90").id == destination


def test_more_guard_does_not_block_completed_click_elsewhere(dashboard, monkeypatch):
    monkeypatch.setattr(job_group_menu.time, "monotonic", lambda: 100.0)
    app = dashboard.app
    hits, old_location, _ = pointer_expand(dashboard)
    state = app.job_group_menu_state
    y, left, _, item = next(hit for hit in state["hits"]
                            if hit[0] != old_location[0] and state["items"][hit[3]]["action"] == "add")
    destination = state["items"][item]["group_id"]
    aggregate(app, hits, (y, left), "native-click")
    assert app.mode == "main"
    assert job_groups.registry(app).ensure(dashboard.store.snapshot()).for_job("90").id == destination


@pytest.mark.parametrize("route", ["keyboard", "command"])
def test_keyboard_and_focus_graph_more_do_not_arm_mouse_aggregate_guard(dashboard, route):
    app = dashboard.app
    compact_groups(dashboard)
    state = app.job_group_menu_state
    if route == "keyboard":
        select_action(app, "more")
    else:
        app.run_command(f"jobgroupmenu choose {state['token']} {actions(app).index('more')}")
    assert state["expanded"] and state["aggregate_guard"] is None


def test_reopening_menu_clears_old_more_aggregate_guard(dashboard, monkeypatch):
    monkeypatch.setattr(job_group_menu.time, "monotonic", lambda: 100.0)
    app = dashboard.app
    pointer_expand(dashboard)
    assert app.job_group_menu_state["aggregate_guard"] is not None
    app.handle("esc")
    assert app.job_group_menu_state["aggregate_guard"] is None
    assert job_group_menu.open_menu(app, source(dashboard))
    assert app.job_group_menu_state["aggregate_guard"] is None
