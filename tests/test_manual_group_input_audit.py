"""Independent user-gesture regressions for exact, pane-owned manual batches."""

import pytest

from raven import history_browser, job_groups, job_selection, layout as L
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store
from raven.views import Views


@pytest.fixture
def dashboard(monkeypatch):
    store = Store(persist=False)
    store.jobs = [Job("600_1", "train", "gpu", "RUNNING"),
                  Job("600_2", "train", "gpu", "RUNNING"),
                  Job("600_3", "train", "gpu", "RUNNING"),
                  Job("600_4", "train", "gpu", "RUNNING"),
                  Job("900", "independent", "cpu", "RUNNING")]
    store.finished = [Finished("500_1", "old run", "COMPLETED"),
                      Finished("500_2", "old run", "FAILED"),
                      Finished("500_3", "old run", "COMPLETED")]
    store.group = list(store.jobs)
    cfg = Config({"animations": False, "startup_animation": False,
                  "smooth_scrolling": False, "log_lines": 0,
                  "workspace": {"density": "compact"}})
    app = App(store, None, None, cfg, "tester", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    monkeypatch.setattr(app, "save", lambda: None)
    yield app, views, store
    if app.research:
        app.research.close()


def frame(dashboard, tab=None, *, width=280, height=60):
    app, views, store = dashboard
    if tab is not None:
        app.tab = "jobs" if tab == "recent" else tab
    return views.compose(store.snapshot(), app, width, height)


def table_point(hits, kind, jid):
    return next((y, 12) for y, actual, value in hits if actual == kind and value == jid)


def click(app, point, hits):
    app.click(*point, hits, button="press")
    app.click(*point, hits, button="release")


def drag(app, first, last, hits, *, release=True):
    app.click(*first, hits, button="press")
    app.click(*last, hits, button="drag")
    if release:
        app.click(*last, hits, button="release")


def index(dashboard):
    app, _, store = dashboard
    return job_groups.registry(app).ensure(store.snapshot())


NATIVE = [("jobs", "job", "600_1", "600_2"),
          ("recent", "recent", "500_1", "500_2"),
          ("history", "fin", "500_1", "500_2"),
          ("group", "group", "600_1", "600_2"),
          ("deps", "dep", "600_1", "600_2")]


@pytest.mark.parametrize("tab,kind,first,last", NATIVE)
def test_drag_group_and_ungroup_collapsed_summary_retains_real_allocations(dashboard, tab, kind, first, last):
    app, _, store = dashboard
    if tab == "deps":
        store.jobs[1].dependency = "afterok:600_1"
    _, hits = frame(dashboard, tab)
    drag(app, table_point(hits, kind, first), table_point(hits, kind, last), hits)
    selected = set(app.marks)
    assert selected == {first, last}
    original_records = tuple(store.jobs) + tuple(store.finished)
    app.handle("g")
    group = index(dashboard).for_job(first)
    assert group is not None and set(group.members) == selected
    assert job_groups.registry(app).is_collapsed(group)
    assert not app.marks and app.selected_id in selected
    assert app.tab == ("jobs" if tab == "recent" else tab)
    assert app.mode == "main" and not app.confirm
    frame(dashboard)
    app.handle("u")
    refreshed = index(dashboard)
    assert all(refreshed.for_job(jid) is None for jid in selected)
    assert tuple(store.jobs) + tuple(store.finished) == original_records
    assert app.mode == "main" and not app.confirm


@pytest.mark.parametrize("multiple", [False, True])
def test_ungroup_expanded_members_keeps_unselected_siblings_grouped(dashboard, multiple):
    app, _, _ = dashboard
    _, hits = frame(dashboard, "jobs")
    if multiple:
        drag(app, table_point(hits, "job", "600_2"), table_point(hits, "job", "600_3"), hits)
        removed = {"600_2", "600_3"}
    else:
        click(app, table_point(hits, "job", "600_2"), hits)
        removed = {"600_2"}
    app.handle("u")
    registry = index(dashboard)
    assert all(registry.for_job(jid) is None for jid in removed)
    surviving = registry.for_job("600_1")
    assert surviving is not None
    assert set(surviving.members) == {"600_1", "600_2", "600_3", "600_4"} - removed
    # Maintenance and frame changes cannot undo an explicit detach.
    for _ in range(3):
        frame(dashboard)
        assert all(index(dashboard).for_job(jid) is None for jid in removed)


def test_grouping_a_marked_collapsed_representative_never_adds_hidden_siblings(dashboard):
    app, _, _ = dashboard
    frame(dashboard, "jobs")
    assert job_groups.fold(app, "array:600", True)
    _, hits = frame(dashboard)
    drag(app, table_point(hits, "job", "600_1"), table_point(hits, "job", "900"), hits)
    assert app.marks == {"600_1", "900"}
    app.handle("g")
    groups = index(dashboard)
    assert set(groups.for_job("900").members) == {"600_1", "900"}
    assert set(groups.for_job("600_2").members) == {"600_2", "600_3", "600_4"}


def test_keyboard_group_finishes_capture_before_an_orphan_release_can_extend_it(dashboard):
    app, _, _ = dashboard
    _, hits = frame(dashboard, "jobs")
    drag(app, table_point(hits, "job", "600_1"), table_point(hits, "job", "600_2"), hits, release=False)
    assert job_selection.active(app)
    app.handle("g")
    group = index(dashboard).for_job("600_1")
    assert group is not None and set(group.members) == {"600_1", "600_2"}
    assert not job_selection.active(app)
    before = (app.tab, app.selected_id, set(app.marks))
    app.click(*table_point(hits, "job", "600_4"), hits, button="release")
    assert (app.tab, app.selected_id, app.marks) == before
    assert set(index(dashboard).for_job("600_1").members) == {"600_1", "600_2"}


@pytest.mark.parametrize("mode", ["filter", "palette", "help", "details"])
@pytest.mark.parametrize("key", ["g", "u"])
def test_modal_and_text_input_do_not_mutate_groups_or_steal_typed_characters(dashboard, mode, key):
    app, _, _ = dashboard
    frame(dashboard, "jobs")
    app.marks = {"600_1", "600_2"}
    before = index(dashboard)
    app.mode = mode
    app.filter_edit = app.palette_edit = ""
    app.handle(key)
    assert index(dashboard) == before and app.marks == {"600_1", "600_2"}
    if mode in ("filter", "palette"):
        assert getattr(app, mode + "_edit") == key


@pytest.mark.parametrize("key", ["g", "u"])
def test_universal_right_click_clear_makes_group_keys_inert(dashboard, key):
    app, _, _ = dashboard
    _, hits = frame(dashboard, "jobs")
    drag(app, table_point(hits, "job", "600_1"), table_point(hits, "job", "600_2"), hits)
    app.click(0, 100, hits, button="right")
    assert not app.marks and app.selected_id is None
    before = index(dashboard)
    app.handle(key)
    assert index(dashboard) == before
    assert not app.marks and app.mode == "main" and app.tab == "jobs"


@pytest.mark.parametrize("button", ["motion", "drag", "release"])
def test_mouse_coordinate_bytes_never_replay_group_or_tab_shortcuts(dashboard, button, monkeypatch):
    app, _, store = dashboard
    _, hits = frame(dashboard, "jobs")
    app.marks = {"600_1", "600_2"}
    app.handle("g")
    _, hits = frame(dashboard)
    before = (app.tab, app.selected_id, set(app.marks), index(dashboard))
    monkeypatch.setattr(store, "snapshot", lambda: pytest.fail("Passive hover fetched scheduler state"))
    monkeypatch.setattr(app, "run_command", lambda *_: pytest.fail("Passive hover executed a command"))
    for y, x in [(0, 38), (12, 70), (22, 84), (0, 84), (15, 140)] * 8:
        app.click(y, x, hits, button=button)
    assert (app.tab, app.selected_id, app.marks, job_groups.registry(app).index) == before


@pytest.mark.parametrize("stale", ["resize", "tab", "collapsed"])
def test_stale_job_drag_map_cannot_start_selection_or_mutate_manual_group(dashboard, stale):
    app, _, _ = dashboard
    _, old_hits = frame(dashboard, "jobs")
    app.marks = {"600_1", "600_2"}
    app.handle("g")
    frame(dashboard)
    if stale == "resize":
        frame(dashboard, width=200)
    elif stale == "tab":
        frame(dashboard, "history")
    before = (app.tab, app.selected_id, set(app.marks), index(dashboard))
    drag(app, table_point(old_hits, "job", "600_1"), table_point(old_hits, "job", "600_4"), old_hits)
    assert (app.tab, app.selected_id, app.marks, index(dashboard)) == before
    assert not job_selection.active(app)


def browser_point(hits, tab, jid):
    return next((y, min(value["right"] - 1, value["left"] + 7))
                for y, kind, value in hits if kind == "control"
                and value["id"] == "history:" + tab + ":job:" + jid)


@pytest.mark.parametrize("tab", history_browser.TABS)
@pytest.mark.parametrize("dock", ["left", "right", "top", "bottom"])
def test_every_browser_dock_marks_exact_rows_and_groups_without_switching_tabs(dashboard, tab, dock):
    app, _, _ = dashboard
    app.tab = tab
    history_browser._view(app)["dock"] = dock
    _, hits = frame(dashboard)
    first, last = browser_point(hits, tab, "600_1"), browser_point(hits, tab, "600_2")
    drag(app, first, last, hits)
    assert app.marks == {"600_1", "600_2"}
    app.handle("g")
    group = index(dashboard).for_job("600_1")
    assert group is not None and set(group.members) == {"600_1", "600_2"}
    assert job_groups.registry(app).is_collapsed(group)
    assert app.tab == tab and app.mode == "main"
    _, hits = frame(dashboard)
    # Use the actual representative painted in this sorted browser.
    representative = next(item.record.id for item in app.history_browser_state["items"]
                          if item.meta is not None and item.meta.group.id == group.id)
    click(app, browser_point(hits, tab, representative), hits)
    app.handle("u")
    assert all(index(dashboard).for_job(jid) is None for jid in group.members)
    assert app.tab == tab and app.mode == "main"


def test_group_command_does_not_consume_marks_owned_by_another_job_list(dashboard):
    app, _, store = dashboard
    store.group += [Job("700_1", "colleague run", "cpu", "RUNNING"),
                    Job("700_2", "colleague run", "cpu", "RUNNING"),
                    Job("700_3", "colleague run", "cpu", "RUNNING")]
    _, hits = frame(dashboard, "group")
    drag(app, table_point(hits, "group", "700_1"), table_point(hits, "group", "700_2"), hits)
    foreign = set(app.marks)
    _, hits = frame(dashboard, "jobs")
    drag(app, table_point(hits, "job", "600_1"), table_point(hits, "job", "600_2"), hits)
    app.marks.update(foreign)
    app.handle("g")
    groups = index(dashboard)
    assert set(groups.for_job("600_1").members) == {"600_1", "600_2"}
    assert set(groups.for_job("700_1").members) == {"700_1", "700_2", "700_3"}
    assert app.marks == foreign


@pytest.mark.parametrize("key", ["g", "u"])
def test_details_focus_cannot_act_on_marks_left_in_jobs(dashboard, key):
    from raven import job_panels
    app, _, _ = dashboard
    _, hits = frame(dashboard, "jobs")
    drag(app, table_point(hits, "job", "600_1"), table_point(hits, "job", "600_2"), hits)
    job_panels._activate(app, "analytics", view="job")
    app.job_panel_state["focus"] = "content"
    app.layout_state.focus = "details"
    frame(dashboard)
    before = index(dashboard)
    app.handle(key)
    assert index(dashboard) == before
    assert app.marks == {"600_1", "600_2"}
    assert app.tab == "jobs"


def test_uppercase_u_clears_marks_without_detaching_grouped_jobs(dashboard):
    app, _, _ = dashboard
    _, hits = frame(dashboard, "jobs")
    drag(app, table_point(hits, "job", "600_1"), table_point(hits, "job", "600_2"), hits)
    before = index(dashboard)
    app.handle("U")
    assert not app.marks and index(dashboard) == before


@pytest.mark.parametrize("key", ["g", "u"])
def test_explicit_rendered_line_selection_keeps_group_shortcuts_out(dashboard, key):
    from raven import text_selection
    app, _, _ = dashboard
    _, hits = frame(dashboard, "jobs")
    drag(app, table_point(hits, "job", "600_1"), table_point(hits, "job", "600_2"), hits)
    frame(dashboard)
    app.handle("v")
    assert text_selection.selected(app) or app.sel_anchor is not None
    before = index(dashboard)
    app.handle(key)
    assert index(dashboard) == before
    assert app.marks == {"600_1", "600_2"}


@pytest.mark.parametrize("key", ["g", "u"])
def test_open_toolbar_menu_keeps_group_shortcuts_out(dashboard, key):
    app, _, _ = dashboard
    _, hits = frame(dashboard, "jobs")
    drag(app, table_point(hits, "job", "600_1"), table_point(hits, "job", "600_2"), hits)
    before = index(dashboard)
    app.handle("f10")
    assert app.toolbar_state["menu"] is not None
    app.handle(key)
    assert index(dashboard) == before
    assert app.marks == {"600_1", "600_2"}


def test_advisor_drag_marks_only_the_exact_allocations_covered(dashboard):
    app, _, _ = dashboard
    app.analytics_view = "advisor"
    _, hits = frame(dashboard, "analytics")
    # Advisor documents have several lines per allocation. Empty explanation
    # lines and measured values must not become additional synthetic jobs.
    left = app.history_browser_content_rect.x + 12
    first = next((y, left) for y, kind, jid in hits if kind == "advisor_job" and jid == "600_1")
    last = next((y, left) for y, kind, jid in hits if kind == "advisor_job" and jid == "600_2")
    drag(app, first, last, hits)
    assert app.marks == {"600_1", "600_2"}
    app.handle("g")
    group = index(dashboard).for_job("600_1")
    assert group is not None and set(group.members) == {"600_1", "600_2"}
    assert app.tab == "analytics" and app.analytics_view == "advisor"


def test_queued_group_then_ungroup_before_repaint_uses_original_exact_members(dashboard):
    app, _, _ = dashboard
    _, hits = frame(dashboard, "jobs")
    drag(app, table_point(hits, "job", "600_2"), table_point(hits, "job", "600_3"), hits)
    app.handle("g")
    created = index(dashboard).for_job("600_2")
    assert created is not None and set(created.members) == {"600_2", "600_3"}
    app.handle("u")
    groups = index(dashboard)
    assert groups.for_job("600_2") is groups.for_job("600_3") is None
    assert set(groups.for_job("600_1").members) == {"600_1", "600_4"}
    app.handle("u")
    assert index(dashboard) == groups
    assert not job_selection.active(app) and app.mode == "main"


def test_disappeared_mark_before_group_key_does_not_create_a_partial_manual_group(dashboard):
    app, _, store = dashboard
    _, hits = frame(dashboard, "jobs")
    drag(app, table_point(hits, "job", "600_1"), table_point(hits, "job", "600_2"), hits)
    store.jobs = [job for job in store.jobs if job.id != "600_2"]
    store.group = list(store.jobs)
    app.handle("g")
    group = index(dashboard).for_job("600_1")
    assert group is not None and group.kind != "manual"
    assert set(group.members) == {"600_1", "600_3", "600_4"}
    assert not app.confirm and app.tab == "jobs"


def test_rejected_group_key_still_finishes_old_drag_before_late_release(dashboard):
    app, _, store = dashboard
    _, hits = frame(dashboard, "jobs")
    drag(app, table_point(hits, "job", "600_1"), table_point(hits, "job", "600_2"), hits, release=False)
    assert job_selection.active(app)
    store.jobs = [job for job in store.jobs if job.id != "600_2"]
    store.group = list(store.jobs)
    app.handle("g")
    assert not job_selection.active(app)
    before = set(app.marks)
    app.click(*table_point(hits, "job", "600_4"), hits, button="release")
    assert app.marks == before


def test_collapsed_summary_ungroup_removes_members_hidden_by_current_filter(dashboard):
    app, _, _ = dashboard
    _, hits = frame(dashboard, "jobs")
    drag(app, table_point(hits, "job", "600_1"), table_point(hits, "job", "600_2"), hits)
    app.handle("g")
    app.filter = "600_1"
    _, hits = frame(dashboard)
    assert app.visible_ids == ["600_1"]
    click(app, table_point(hits, "job", "600_1"), hits)
    app.handle("u")
    groups = index(dashboard)
    assert groups.for_job("600_1") is groups.for_job("600_2") is None
    app.filter = ""
    frame(dashboard)
    assert {"600_1", "600_2"}.issubset(app.visible_ids)


@pytest.mark.parametrize("owner", ["graph-hover", "graph-drag", "metric-slider"])
@pytest.mark.parametrize("key", ["g", "u"])
def test_graph_input_owners_cannot_group_or_detach_job_marks(dashboard, owner, key):
    from raven import chart_interaction, metric_live
    app, _, store = dashboard
    for sample in range(20):
        store.record("600_1", {"k": "live", "t": 180.0 + sample,
                               "cpu": sample / 25, "rss": 1024 ** 3})
    app.job_panel_state.update(mode="analytics", analytics_view="job")
    frame(dashboard, "jobs", height=90)
    app.selected_id, app.cursor["jobs"] = "600_1", app.visible_ids.index("600_1")
    _, hits = frame(dashboard, "jobs", height=90)
    app.marks = {"600_1", "600_2"}
    if owner == "metric-slider":
        control = metric_live.initialize(app)["records"][0]
        app.click(control.slider.top, control.slider.left + 1, hits, button="press")
        assert metric_live.active(app)
    else:
        plot = next(plot for plot in app.chart_interaction_state["plots"]
                    if plot.key[0] == "resource-series")
        point = plot.visible.top + 1, plot.visible.left + 3
        app.click(*point, hits, button="motion" if owner == "graph-hover" else "press")
        if owner == "graph-drag":
            assert chart_interaction.active(app)
    before = index(dashboard)
    app.handle(key)
    assert index(dashboard) == before
    assert app.marks == {"600_1", "600_2"}
    assert app.tab == "jobs" and app.mode == "main"
