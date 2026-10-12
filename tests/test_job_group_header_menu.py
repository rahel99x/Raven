"""Right-click group headers retain the exact painted pane and job attempts."""
import pytest

from raven import history_browser, job_group_drag, job_group_menu, job_groups, job_selection
from raven import manual_job_groups
from raven.model import Finished, Job
from test_job_group_mouse_integration import (
    dashboard, dispatch, frame, index, labels, mark, menu_point, point,
)


def header_point(hits, scope, gid):
    prefix = (scope + ":group:" + gid if scope.startswith("history:")
              else "jobgroup:" + scope + ":" + gid)
    return next((y, value["left"]) for y, kind, value in hits
                if kind == "control" and value["id"].startswith(prefix))


def prepare(dashboard, scope, kind, collapsed):
    app, store = dashboard
    if kind == "smart":
        # A launch marker creates one automatic group across active/history
        # sources; the menu must still restrict each pane to its own records.
        store.details.update({jid: {"RavenLaunchId": "header-batch"}
                              for jid in ("1", "2", "3", "21", "22", "23")})
        manual_job_groups.detach(app, store.snapshot(), group_ids=(index(dashboard).for_job("1").id,))
        app.table_state["manual_groups"] = {}
        app.manual_job_groups_revision += 1
    elif scope in ("recent", "history"):
        assert manual_job_groups.create(app, store.snapshot(), ("21", "22", "23")).changed
    if scope.startswith("history:"):
        app.tab = scope.split(":", 1)[1]
        history_browser._view(app).update(dock="left", ratio=40)
    elif scope == "analytics:advisor":
        app.tab, app.analytics_view = "analytics", "advisor"
    else:
        app.tab = "jobs" if scope == "recent" else scope
    if scope == "deps":
        store.jobs[1].dependency = "afterok:1"
        store.jobs[2].dependency = "afterok:2"
    gid = index(dashboard).for_job("21" if scope in ("recent", "history") else "1").id
    job_groups.fold(app, gid, collapsed)
    _, hits = frame(dashboard, height=90)
    return gid, hits


@pytest.mark.parametrize("scope", ["jobs", "recent", "history", "group", "deps", "analytics:advisor",
                                    *["history:" + tab for tab in history_browser.TABS]])
@pytest.mark.parametrize("kind", ["smart", "manual"])
@pytest.mark.parametrize("collapsed", [False, True])
@pytest.mark.parametrize("route", ["app", "native", "sgr"])
def test_header_opens_without_selection_for_exact_projected_members(dashboard, scope, kind, collapsed, route):
    app, _ = dashboard
    gid, hits = prepare(dashboard, scope, kind, collapsed)
    source_frame = job_group_drag.initialize(app)["frame"]
    expected = source_frame["headers"][(scope, gid)]["members"]
    # No focused row is needed and unrelated marks remain available on cancel.
    job_selection.clear(app)
    mark(app, "12", "26")
    dispatch(app, hits, header_point(hits, scope, gid), "right", route)
    assert app.mode == "job_group_menu"
    state = app.job_group_menu_state
    assert state["context"]["targets"] == expected
    assert state["context"]["source_group"].id == gid
    assert "Ungroup" in labels(app)
    assert "Rerun cancelled job" not in labels(app)
    assert app.marks == {"12", "26"}
    assert job_groups.registry(app).is_collapsed(index(dashboard).groups[gid]) == collapsed
    # A terminal's orphaned release after opening cannot choose a menu item.
    dispatch(app, hits, header_point(hits, scope, gid), "release", route)
    assert app.mode == "job_group_menu"
    app.handle("esc")
    assert app.marks == {"12", "26"}


@pytest.mark.parametrize("collapsed", [False, True])
@pytest.mark.parametrize("location", ["chevron", "body"])
def test_unselected_header_body_and_chevron_target_whole_projected_group(dashboard, collapsed, location):
    app, _ = dashboard
    gid, hits = prepare(dashboard, "jobs", "manual", collapsed)
    position = header_point(hits, "jobs", gid) if location == "chevron" else point(hits, "1")
    job_selection.clear(app)
    dispatch(app, hits, position, "right", "sgr")
    assert set(app.job_group_menu_state["context"]["targets"]) == {"1", "2", "3"}


def test_expanded_marked_top_row_retains_members_but_chevron_targets_group(dashboard):
    app, _ = dashboard
    gid, hits = prepare(dashboard, "jobs", "manual", False)
    mark(app, "1", "2")
    dispatch(app, hits, point(hits, "1"), "right")
    assert app.job_group_menu_state["context"]["targets"] == ("1", "2")
    assert "source_group" not in app.job_group_menu_state["context"]
    app.handle("esc")
    _, hits = frame(dashboard)
    dispatch(app, hits, header_point(hits, "jobs", gid), "right")
    assert app.job_group_menu_state["context"]["targets"] == ("1", "2", "3")


@pytest.mark.parametrize("scope", ["jobs", "recent", "history"])
@pytest.mark.parametrize("collapsed", [False, True])
def test_ungroup_header_never_removes_other_pane_members(dashboard, scope, collapsed):
    app, store = dashboard
    assert manual_job_groups.create(app, store.snapshot(), ("1", "2", "3", "21", "22", "23")).changed
    app.tab = "history" if scope == "history" else "jobs"
    gid = index(dashboard).for_job("1").id
    job_groups.fold(app, gid, collapsed)
    _, hits = frame(dashboard)
    dispatch(app, hits, header_point(hits, scope, gid), "right", "sgr")
    expected = ("1", "2", "3") if scope == "jobs" else ("21", "22", "23")
    assert set(app.job_group_menu_state["context"]["targets"]) == set(expected)
    _, hits = frame(dashboard)
    location = menu_point(app, "Ungroup")
    dispatch(app, hits, location, "press", "sgr")
    dispatch(app, hits, location, "release", "sgr")
    updated = index(dashboard)
    assert all(updated.for_job(jid) is None for jid in expected)
    other = "21" if scope == "jobs" else "1"
    assert updated.for_job(other) is not None
    assert set(updated.for_job(other).members) == ({"21", "22", "23"} if scope == "jobs" else {"1", "2", "3"})


@pytest.mark.parametrize("collapsed", [False, True])
def test_filtered_header_ungroups_only_matching_records_and_never_reruns_representative(dashboard, collapsed):
    app, store = dashboard
    assert manual_job_groups.create(app, store.snapshot(), ("21", "22", "23")).changed
    store.finished[0].state = "CANCELLED"
    app.tab, app.filter = "history", "21"
    gid = index(dashboard).for_job("21").id
    job_groups.fold(app, gid, collapsed)
    _, hits = frame(dashboard)
    dispatch(app, hits, header_point(hits, "history", gid), "right", "sgr")
    assert app.job_group_menu_state["context"]["targets"] == ("21",)
    assert "Rerun cancelled job" not in labels(app)
    _, hits = frame(dashboard)
    y, x = menu_point(app, "Ungroup")
    dispatch(app, hits, (y, x), "press")
    dispatch(app, hits, (y, x), "release")
    assert index(dashboard).for_job("21") is None
    assert index(dashboard).for_job("22").members == ("22", "23")


@pytest.mark.parametrize("change", ["attempt", "disappeared", "membership", "smart-membership"])
@pytest.mark.parametrize("repaint", [False, True])
def test_header_actions_reject_stale_hidden_jobs_and_changed_group(dashboard, change, repaint):
    app, store = dashboard
    gid, hits = prepare(dashboard, "jobs", "smart" if change == "smart-membership" else "manual", True)
    dispatch(app, hits, header_point(hits, "jobs", gid), "right", "sgr")
    _, hits = frame(dashboard)
    if change == "attempt":
        store.jobs[1].submit = "2026-10-11T10:00:00"
    elif change == "disappeared":
        store.jobs[:] = [record for record in store.jobs if record.id != "2"]
        store.group = list(store.jobs)
    elif change == "membership":
        manual_job_groups.add(app, store.snapshot(), gid, ("4",))
    else:
        store.details["4"] = {"RavenLaunchId": "header-batch"}
    if repaint:
        _, hits = frame(dashboard)
    before = app.table_state.get("manual_groups")
    position = menu_point(app, "Ungroup")
    dispatch(app, hits, position, "press", "sgr")
    dispatch(app, hits, position, "release", "sgr")
    assert app.table_state.get("manual_groups") == before
    assert app.mode == "main" and app.tab == "jobs"


@pytest.mark.parametrize("dock", ["left", "right", "top", "bottom"])
def test_shared_browser_header_hit_is_clipped_in_each_orientation(dashboard, dock):
    app, _ = dashboard
    app.tab = "research"
    history_browser._view(app).update(dock=dock, ratio=40)
    _, hits = frame(dashboard)
    gid = index(dashboard).for_job("1").id
    position = header_point(hits, "history:research", gid)
    dispatch(app, hits, position, "right", "sgr")
    assert set(app.job_group_menu_state["context"]["targets"]) == {"1", "2", "3"}
    assert app.mode == "job_group_menu" and app.tab == "research"


def test_scrolled_continuation_disclosure_keeps_offscreen_members_and_exact_focus(dashboard):
    app, store = dashboard
    store.jobs = [Job(str(i), "batch", "cpu", "RUNNING", submit="2026-10-09T10:00:00",
                      cluster="local") for i in range(100, 180)]
    store.group = list(store.jobs)
    assert manual_job_groups.create(app, store.snapshot(), tuple(record.id for record in store.jobs)).changed
    app.cursor["jobs"], app.top["jobs"] = 40, 40
    app.selected_id = "140"
    _, hits = frame(dashboard, height=24)
    gid = index(dashboard).for_job("100").id
    location = header_point(hits, "jobs", gid)
    source = job_group_drag.menu_source_at(app, *location)
    assert source["pointed"] != "100"
    dispatch(app, hits, location, "right", "sgr")
    assert len(app.job_group_menu_state["context"]["targets"]) == 80
    assert app.job_group_menu_state["context"]["targets"] == tuple(str(i) for i in range(100, 180))
    _, hits = frame(dashboard, height=24)
    location = menu_point(app, "Ungroup")
    dispatch(app, hits, location, "press", "sgr")
    dispatch(app, hits, location, "release", "sgr")
    assert not index(dashboard).groups
    assert app.cursor["jobs"] == int(source["pointed"]) - 100


def test_header_hover_never_copies_or_reads_members(dashboard, monkeypatch):
    app, store = dashboard
    gid, hits = prepare(dashboard, "jobs", "manual", True)
    position = header_point(hits, "jobs", gid)
    with monkeypatch.context() as guard:
        guard.setattr(store, "snapshot", lambda: pytest.fail("Hover copied jobs"))
        guard.setattr(job_groups.registry(app), "ensure", lambda *_: pytest.fail("Hover regrouped jobs"))
        for _ in range(100):
            dispatch(app, hits, position, "motion", "sgr")
    assert app.mode == "main"


def test_partly_archived_group_can_act_on_its_observed_members(dashboard):
    app, store = dashboard
    gid = index(dashboard).for_job("1").id
    store.jobs[:] = [record for record in store.jobs if record.id != "3"]
    store.group = list(store.jobs)
    job_groups.fold(app, gid, True)
    _, hits = frame(dashboard)
    dispatch(app, hits, header_point(hits, "jobs", gid), "right", "sgr")
    assert app.mode == "job_group_menu"
    assert app.job_group_menu_state["context"]["targets"] == ("1", "2")
    _, hits = frame(dashboard)
    location = menu_point(app, "Ungroup")
    dispatch(app, hits, location, "press", "sgr")
    dispatch(app, hits, location, "release", "sgr")
    assert index(dashboard).for_job("1") is None
    assert index(dashboard).for_job("2") is None


def test_filtered_header_does_not_require_ambiguous_excluded_member(dashboard):
    app, store = dashboard
    store.jobs = [Job(f"600_{i}", "wanted" if i < 3 else "excluded", "cpu", "RUNNING",
                      submit="2026-10-09T10:00:00", cluster="local") for i in range(1, 4)]
    store.group = list(store.jobs)
    store.finished = [Finished("600_3", "excluded", "COMPLETED", submit="2026-10-08T10:00:00", cluster="local")]
    app.filter = "wanted"
    _, hits = frame(dashboard)
    gid = index(dashboard).for_job("600_1").id
    assert gid not in job_group_drag.initialize(app)["frame"]["evidence"]
    dispatch(app, hits, header_point(hits, "jobs", gid), "right", "sgr")
    assert app.mode == "job_group_menu"
    assert app.job_group_menu_state["context"]["targets"] == ("600_1", "600_2")


@pytest.mark.parametrize("collapsed", [False, True])
def test_add_header_to_other_group_moves_all_scoped_members_in_natural_order(dashboard, collapsed):
    app, _ = dashboard
    gid, hits = prepare(dashboard, "jobs", "manual", collapsed)
    mark(app, "4", "5")
    dispatch(app, hits, header_point(hits, "jobs", gid), "right", "sgr")
    _, hits = frame(dashboard)
    location = menu_point(app, "Add to ")
    dispatch(app, hits, location, "press", "sgr")
    dispatch(app, hits, location, "release", "sgr")
    assert index(dashboard).for_job("1").members == ("1", "2", "3", "10", "11", "12")
    assert app.marks == {"4", "5"}


@pytest.mark.parametrize("route", ["native", "sgr"])
@pytest.mark.parametrize("collapsed", [False, True])
def test_cancelled_header_menu_cannot_toggle_group_from_delayed_release(dashboard, route, collapsed):
    app, _ = dashboard
    gid, hits = prepare(dashboard, "jobs", "manual", collapsed)
    position = header_point(hits, "jobs", gid)
    dispatch(app, hits, position, "right", route)
    frame(dashboard)
    app.handle("esc")
    _, hits = frame(dashboard)
    dispatch(app, hits, position, "release", route)
    assert job_groups.registry(app).is_collapsed(index(dashboard).groups[gid]) == collapsed
    assert app.mode == "main" and app.tab == "jobs"


@pytest.mark.parametrize("change", ["attempt", "membership", "resize", "sort"])
def test_old_header_hit_cannot_retarget_changed_source_before_repaint(dashboard, change):
    app, store = dashboard
    gid, hits = prepare(dashboard, "jobs", "manual", True)
    position = header_point(hits, "jobs", gid)
    if change == "attempt":
        store.jobs[1].submit = "2026-10-11T10:00:00"
    elif change == "membership":
        manual_job_groups.add(app, store.snapshot(), gid, ("4",))
    elif change == "resize":
        app.width -= 1
    else:
        app.run_command("sortby jobs id desc")
    dispatch(app, hits, position, "right", "sgr")
    assert app.mode == "main"


@pytest.mark.parametrize("width,height", [(60, 18), (90, 22)])
def test_header_menu_remains_keyboard_usable_in_small_terminal(dashboard, width, height):
    app, _ = dashboard
    gid = index(dashboard).for_job("1").id
    job_groups.fold(app, gid, True)
    frame(dashboard)
    app.selected_id, app.cursor["jobs"] = "1", app.visible_ids.index("1")
    _, hits = frame(dashboard, width=width, height=height)
    dispatch(app, hits, header_point(hits, "jobs", gid), "right", "sgr")
    assert app.mode == "job_group_menu"
    frame(dashboard, width=width, height=height)
    state = job_group_menu.initialize(app)
    state["cursor"] = next(i for i, action in enumerate(state["items"]) if action["action"] == "ungroup")
    app.handle("enter")
    assert index(dashboard).for_job("1") is None
