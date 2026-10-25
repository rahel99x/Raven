"""User gestures through the real controller and terminal decoder stay scoped."""
from collections import deque
import curses

import pytest

from raven import history_browser, job_groups, job_selection, layout, manual_job_groups, screen
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store
from raven.views import Views


class InputWindow:
    def __init__(self, text):
        self.values = deque(text)

    def get_wch(self):
        if self.values:
            return self.values.popleft()
        raise curses.error("empty")

    def timeout(self, _value):
        pass


@pytest.fixture
def dashboard(monkeypatch):
    store = Store(persist=False)
    store.jobs = [Job(str(identifier), "standalone-" + chr(64 + identifier), "cpu", "RUNNING",
                      submit="2026-10-09T10:00:00", cluster="local")
                  for identifier in range(1, 13)]
    store.finished = [Finished(str(identifier), "completed-" + chr(64 + identifier), "COMPLETED",
                               submit="2026-10-08T10:00:00", cluster="local")
                      for identifier in range(21, 27)]
    store.group = list(store.jobs)
    cfg = Config({"animations": False, "startup_animation": False,
                  "smooth_scrolling": False, "gpu_sampling": False, "log_lines": 0,
                  "workspace": {"density": "compact"}})
    app = App(store, None, None, cfg, "tester", interactive=False)
    app.views_ref = Views(layout.Glyphs(False), cfg)
    monkeypatch.setattr(app, "save", lambda: None)
    assert manual_job_groups.create(app, store.snapshot(), ["1", "2", "3"]).changed
    assert manual_job_groups.create(app, store.snapshot(), ["10", "11", "12"]).changed
    yield app, store
    if app.research:
        app.research.close()


def frame(dashboard, tab=None, *, width=260, height=65):
    app, store = dashboard
    if tab:
        app.tab = tab
    snapshot = store.snapshot()
    rows, hits = app.views_ref.compose(snapshot, app, width, height)
    app.last_hits = hits
    app.views_ref.overlay(snapshot, app, width, height)
    return rows, hits


def point(app, hits, identifier, kind="job"):
    """Choose a data cell from this frame's real global job rectangle."""
    from raven.jobs import group_drag
    y = next(y for y, actual, value in hits if actual == kind and value == identifier)
    record = next(item for item in group_drag.initialize(app)["frame"]["rows"][y]
                  if item["pointed"] == identifier)
    return y, min(record["rect"][1] + 12, record["rect"][3] - 1)


def disclosure_point(app, hits, identifier):
    from raven.jobs import group_drag
    y = point(app, hits, identifier)[0]
    left, _right = group_drag.initialize(app)["frame"]["disclosures"][y][0]
    return y, left


def mark(app, *identifiers):
    app.marks = set(identifiers)
    job_selection._bind_marks(app, identifiers)


def index(dashboard):
    app, store = dashboard
    return job_groups.registry(app).ensure(store.snapshot())


def dispatch(app, hits, position, button, route="app", *, shift=False, alt=False):
    y, x = position
    if route == "app":
        app.click(y, x, hits, button=button, shift=shift, alt=alt)
        return
    if route == "native":
        flag = {"press": curses.BUTTON1_PRESSED,
                "release": curses.BUTTON1_RELEASED,
                "drag": curses.REPORT_MOUSE_POSITION | curses.BUTTON1_PRESSED,
                "motion": curses.REPORT_MOUSE_POSITION,
                "right": curses.BUTTON3_PRESSED}[button]
        flag |= curses.BUTTON_SHIFT if shift else 0
        flag |= curses.BUTTON_ALT if alt else 0
        event = ("mouse", (0, x, y, 0, flag))
    else:
        code = {"press": 0, "release": 0, "drag": 32, "motion": 35, "right": 2}[button]
        code += 4 if shift else 0
        code += 8 if alt else 0
        ending = "m" if button == "release" else "M"
        reader = screen._InputReader(InputWindow(f"\x1b[<{code};{x + 1};{y + 1}{ending}"))
        # The decoder can yield mid-packet when the process is preempted.
        # Resume its bounded read just as the UI loop does; a loaded test
        # machine must not turn that intentional yield into a false failure.
        event = None
        for _ in range(32):
            event = reader.read(curses)
            if event is not None and event[0] is not None:
                break
        assert event is not None and event[0] == "mouse"
        assert not reader.queue
    screen._apply_input(app, event, hits, curses)


def gesture(app, hits, source, target, route="app", *, alt=True):
    dispatch(app, hits, source, "press", route, alt=alt)
    dispatch(app, hits, target, "drag", route, alt=alt)
    dispatch(app, hits, target, "release", route, alt=alt)


def labels(app):
    return [item["label"] for item in app.job_group_menu_state["items"]]


def menu_point(app, prefix):
    state = app.job_group_menu_state
    item = next(i for i, value in enumerate(state["items"]) if value["label"].startswith(prefix))
    return next((y, left) for y, left, _right, i in state["hits"] if i == item)


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
def test_marked_rows_drop_once_into_group_through_controller_and_terminal(dashboard, route):
    app, _ = dashboard
    _, hits = frame(dashboard)
    mark(app, "7", "8", "9")
    destination = index(dashboard).for_job("1").id
    gesture(app, hits, point(app, hits, "7"), point(app, hits, "1"), route)
    group = index(dashboard).for_job("7")
    assert group is not None and group.id == destination
    assert group.members == ("1", "2", "3", "7", "8", "9")
    assert app.mode == "main" and app.tab == "jobs"
    assert not app.marks
    # One terminal can deliver both a release and a later synthesized release.
    before = app.table_state["manual_groups"]
    dispatch(app, hits, point(app, hits, "10"), "release", route)
    assert app.table_state["manual_groups"] == before
    assert index(dashboard).for_job("7").id == destination


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
def test_selected_row_context_menu_reaches_create_action_without_deselecting(dashboard, route):
    app, _ = dashboard
    _, hits = frame(dashboard)
    mark(app, "4", "5")
    dispatch(app, hits, point(app, hits, "4"), "right", route)
    assert app.mode == "job_group_menu"
    assert app.marks == {"4", "5"}
    assert "Create Group" in labels(app)
    assert "Ungroup" not in labels(app)
    assert any(label.startswith("Add to ") for label in labels(app))
    _, hits = frame(dashboard)
    location = menu_point(app, "Create Group")
    dispatch(app, hits, location, "press", route)
    dispatch(app, hits, location, "release", route)
    group = index(dashboard).for_job("4")
    assert group is not None and group.members == ("4", "5")
    assert app.mode == "main" and app.tab == "jobs"


def test_groups_keep_numeric_order_after_late_middle_jobs_are_dropped(dashboard):
    app, _ = dashboard
    for batch in (("7", "8", "9"), ("4", "5", "6")):
        _, hits = frame(dashboard)
        mark(app, *batch)
        gesture(app, hits, point(app, hits, batch[0]), point(app, hits, "1"))
    group = index(dashboard).for_job("1")
    assert group.members == tuple(str(identifier) for identifier in range(1, 10))
    _, hits = frame(dashboard)
    rendered = [identifier for _y, kind, identifier in hits if kind == "job" and identifier in group.members]
    assert rendered == list(group.members)


@pytest.mark.parametrize("route", ["app", "sgr"])
def test_right_click_unselected_row_opens_exact_context_and_preserves_marks(dashboard, route):
    app, _ = dashboard
    _, hits = frame(dashboard)
    mark(app, "4", "5")
    dispatch(app, hits, point(app, hits, "6"), "right", route)
    assert app.mode == "job_group_menu" and app.marks == {"4", "5"}
    assert app.job_group_menu_state["context"]["targets"] == ("6",)
    assert index(dashboard).for_job("4") is None


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
def test_single_row_can_be_clicked_then_alt_dragged_without_marking_siblings(dashboard, route):
    app, _ = dashboard
    _, hits = frame(dashboard)
    source = point(app, hits, "4")
    dispatch(app, hits, source, "press", route)
    dispatch(app, hits, source, "release", route)
    _, hits = frame(dashboard)
    gesture(app, hits, point(app, hits, "4"), point(app, hits, "1"), route)
    assert index(dashboard).for_job("4").members == ("1", "2", "3", "4")
    assert index(dashboard).for_job("5") is None
    assert app.tab == "jobs" and app.mode == "main"


@pytest.mark.parametrize("shift", [False, True])
def test_range_selection_is_preserved_when_row_was_not_armed_or_shift_is_held(dashboard, shift):
    app, _ = dashboard
    _, hits = frame(dashboard)
    if shift:
        mark(app, "4")
    source, target = point(app, hits, "4"), point(app, hits, "6")
    dispatch(app, hits, source, "press", shift=shift)
    dispatch(app, hits, target, "drag", shift=shift)
    dispatch(app, hits, target, "release", shift=shift)
    assert app.marks == {"4", "5", "6"}
    assert all(index(dashboard).for_job(identifier) is None for identifier in ("4", "5", "6"))


@pytest.mark.parametrize("reason", ["resize", "tab", "modal", "source-attempt", "target-attempt", "membership", "fold"])
def test_drag_rejects_changes_to_source_destination_or_view(dashboard, reason):
    from raven import job_group_drag

    app, store = dashboard
    _, hits = frame(dashboard)
    mark(app, "4", "5")
    dispatch(app, hits, point(app, hits, "4"), "press", alt=True)
    dispatch(app, hits, point(app, hits, "1"), "drag")
    assert job_group_drag.active(app)
    if reason == "resize":
        app.width -= 1
    elif reason == "tab":
        app.tab = "history"
    elif reason == "modal":
        app.mode = "help"
    elif reason == "source-attempt":
        store.jobs[3].submit = "2026-10-10T10:00:00"
    elif reason == "target-attempt":
        store.jobs[1].submit = "2026-10-10T10:00:00"
    elif reason == "membership":
        assert manual_job_groups.detach(app, store.snapshot(), ["2"]).changed
    else:
        job_groups.fold(app, index(dashboard).for_job("1").id, True)
    dispatch(app, hits, point(app, hits, "1"), "release")
    assert not job_group_drag.active(app)
    assert index(dashboard).for_job("4") is None
    assert index(dashboard).for_job("5") is None
    assert app.tab == ("history" if reason == "tab" else "jobs")
    assert app.mode == ("help" if reason == "modal" else "main")


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
def test_drag_pointer_hot_path_has_no_snapshot_inference_or_command_dispatch(dashboard, route, monkeypatch):
    from raven import job_group_drag

    app, store = dashboard
    _, hits = frame(dashboard)
    mark(app, "4", "5")
    dispatch(app, hits, point(app, hits, "4"), "press", route, alt=True)
    assert job_group_drag.active(app)
    with monkeypatch.context() as guard:
        guard.setattr(store, "snapshot", lambda: pytest.fail("Drag motion copied scheduler state"))
        guard.setattr(job_groups.registry(app), "ensure", lambda *_: pytest.fail("Drag motion rebuilt grouping"))
        guard.setattr(app, "run_command", lambda *_: pytest.fail("Drag motion activated a command"))
        for _ in range(50):
            for location in (point(app, hits, "1"), (0, 38), (20, 180), point(app, hits, "4")):
                dispatch(app, hits, location, "drag", route)
        assert job_group_drag.active(app)
    dispatch(app, hits, point(app, hits, "1"), "release", route)
    assert index(dashboard).for_job("4").members == ("1", "2", "3", "4", "5")
    assert app.tab == "jobs" and app.mode == "main"


def test_proven_unheld_sgr_report_cancels_drop_after_missing_release(dashboard):
    from raven import job_group_drag

    app, _ = dashboard
    _, hits = frame(dashboard)
    mark(app, "4", "5")
    dispatch(app, hits, point(app, hits, "4"), "press", "sgr", alt=True)
    dispatch(app, hits, point(app, hits, "1"), "drag", "sgr")
    assert job_group_drag.active(app)
    dispatch(app, hits, point(app, hits, "1"), "motion", "sgr")
    assert not job_group_drag.active(app)
    dispatch(app, hits, point(app, hits, "1"), "release", "sgr")
    assert index(dashboard).for_job("4") is None
    assert app.marks == {"4", "5"}
    assert app.tab == "jobs" and app.mode == "main"


def test_native_curses_motion_without_held_metadata_keeps_drag_compatibility(dashboard):
    from raven import job_group_drag

    app, _ = dashboard
    _, hits = frame(dashboard)
    mark(app, "4", "5")
    dispatch(app, hits, point(app, hits, "4"), "press", "native", alt=True)
    dispatch(app, hits, point(app, hits, "1"), "motion", "native")
    assert job_group_drag.active(app)
    dispatch(app, hits, point(app, hits, "1"), "release", "native")
    assert index(dashboard).for_job("4").members == ("1", "2", "3", "4", "5")


@pytest.mark.parametrize("route", ["app", "sgr"])
def test_release_over_toolbar_or_tab_never_clicks_through(dashboard, route):
    from raven import job_group_drag

    app, _ = dashboard
    _, hits = frame(dashboard)
    mark(app, "4", "5")
    tab_row, tab_left, _tab_right, _tab_name = next(hit for hit in app.tab_hits if hit[3] == "research")
    gesture(app, hits, point(app, hits, "4"), (tab_row, tab_left), route)
    assert not job_group_drag.active(app)
    assert app.tab == "jobs" and app.mode == "main" and app.marks == {"4", "5"}
    assert index(dashboard).for_job("4") is None


@pytest.mark.parametrize("change", ["source-attempt", "target-attempt", "source-disappeared"])
def test_menu_action_validates_frozen_jobs_before_mutating_groups(dashboard, change):
    app, store = dashboard
    _, hits = frame(dashboard)
    mark(app, "4", "5")
    dispatch(app, hits, point(app, hits, "4"), "right", "sgr")
    assert app.mode == "job_group_menu"
    _, hits = frame(dashboard)
    location = menu_point(app, "Add to ")
    if change == "source-attempt":
        store.jobs[3].submit = "2026-10-10T10:00:00"
    elif change == "target-attempt":
        # Both destinations contain a modified attempt, whichever one is listed first.
        store.jobs[0].submit = store.jobs[9].submit = "2026-10-10T10:00:00"
    else:
        store.jobs[:] = [job for job in store.jobs if job.id != "5"]
        store.group = list(store.jobs)
    dispatch(app, hits, location, "press", "sgr")
    dispatch(app, hits, location, "release", "sgr")
    assert index(dashboard).for_job("4") is None
    assert index(dashboard).for_job("5") is None
    assert app.tab == "jobs"


def test_expanded_member_context_menu_ungroups_only_selected_members(dashboard):
    app, _ = dashboard
    _, hits = frame(dashboard)
    mark(app, "1", "2")
    dispatch(app, hits, point(app, hits, "1"), "right")
    assert app.mode == "job_group_menu" and "Ungroup" in labels(app)
    _, hits = frame(dashboard)
    location = menu_point(app, "Ungroup")
    dispatch(app, hits, location, "press")
    dispatch(app, hits, location, "release")
    assert index(dashboard).for_job("1") is None
    assert index(dashboard).for_job("2") is None
    assert index(dashboard).for_job("10").members == ("10", "11", "12")


def test_menu_hover_never_dispatches_actions_or_reads_scheduler_state(dashboard, monkeypatch):
    app, store = dashboard
    _, hits = frame(dashboard)
    mark(app, "4", "5")
    dispatch(app, hits, point(app, hits, "4"), "right", "sgr")
    assert app.mode == "job_group_menu"
    _, hits = frame(dashboard)
    locations = [(y, left) for y, left, _right, _i in app.job_group_menu_state["hits"]]
    with monkeypatch.context() as guard:
        guard.setattr(store, "snapshot", lambda: pytest.fail("Menu hover fetched jobs"))
        guard.setattr(app, "run_command", lambda *_: pytest.fail("Menu hover activated a command"))
        for _ in range(30):
            for location in locations:
                dispatch(app, hits, location, "motion", "sgr")
    assert app.mode == "job_group_menu" and app.marks == {"4", "5"}
    assert index(dashboard).for_job("4") is None


def browser_point(app, hits, tab, identifier):
    return next((y, min(value["left"] + 6, value["right"] - 1))
                for y, kind, value in hits if kind == "control"
                and value["id"] == f"history:{tab}:job:{identifier}")


@pytest.mark.parametrize("tab", history_browser.TABS)
@pytest.mark.parametrize("dock", ["left", "right", "top", "bottom"])
def test_group_drop_works_in_each_shared_history_browser_orientation(dashboard, tab, dock):
    app, _ = dashboard
    app.tab = tab
    history_browser._view(app)["dock"] = dock
    _, hits = frame(dashboard)
    mark(app, "4", "5")
    gesture(app, hits, browser_point(app, hits, tab, "4"), browser_point(app, hits, tab, "1"), "sgr")
    assert index(dashboard).for_job("4").members == ("1", "2", "3", "4", "5")
    assert app.tab == tab and app.mode == "main"


@pytest.mark.parametrize("tab,kind,source,target", [
    ("history", "fin", "24", "21"), ("jobs", "recent", "24", "21"),
    ("group", "group", "4", "1"), ("deps", "dep", "4", "1"),
])
def test_native_other_job_lists_allow_exact_drop_and_context_menu(dashboard, tab, kind, source, target):
    app, store = dashboard
    if kind in ("fin", "recent"):
        assert manual_job_groups.create(app, store.snapshot(), ["21", "22", "23"]).changed
    if tab == "deps":
        store.jobs[3].dependency = "afterok:1"
        store.jobs[4].dependency = "afterok:2"
    _, hits = frame(dashboard, tab)
    mark(app, source)
    gesture(app, hits, point(app, hits, source, kind), point(app, hits, target, kind), "sgr")
    group = index(dashboard).for_job(source)
    assert group is not None and target in group.members
    _, hits = frame(dashboard)
    mark(app, source)
    dispatch(app, hits, point(app, hits, source, kind), "right", "sgr")
    assert app.mode == "job_group_menu" and "Ungroup" in labels(app)
    if tab == "history":
        assert "Export logs" in labels(app)
    assert app.tab == tab


def test_advisor_job_rows_own_only_their_bounded_list_rectangles(dashboard):
    app, _ = dashboard
    app.tab, app.analytics_view = "analytics", "advisor"
    _, hits = frame(dashboard)

    def advisor_point(identifier):
        return next((y, min(value["right"] - 1, value["left"] + 6))
                    for y, kind, value in hits if kind == "control"
                    and value["id"] == "advisor-job:" + identifier)

    mark(app, "4", "5")
    gesture(app, hits, advisor_point("4"), advisor_point("1"), "sgr")
    assert index(dashboard).for_job("4").members == ("1", "2", "3", "4", "5")
    assert app.tab == "analytics" and app.analytics_view == "advisor"


@pytest.mark.parametrize("route", ["app", "native", "sgr"])
def test_dragging_marked_jobs_across_live_graph_cannot_capture_or_zoom_chart(dashboard, route):
    from raven import chart_interaction, job_group_drag

    app, store = dashboard
    for step in range(30):
        store.record("1", {"k": "live", "t": 180 + step, "cpu": .2 + step / 60,
                           "rss": 1024 ** 3})
    app.job_panel_state.update(mode="analytics", analytics_view="job")
    frame(dashboard, height=90)
    app.selected_id, app.cursor["jobs"] = "1", app.visible_ids.index("1")
    _, hits = frame(dashboard, height=90)
    plot = next(plot for plot in app.chart_interaction_state["plots"] if plot.key[0] == "resource-series")
    graph_point = (plot.visible.top + 1, plot.visible.left + 3)
    mark(app, "4", "5")
    dispatch(app, hits, point(app, hits, "4"), "press", route, alt=True)
    assert job_group_drag.active(app)
    for _ in range(25):
        dispatch(app, hits, graph_point, "drag", route)
        dispatch(app, hits, point(app, hits, "1"), "drag", route)
    assert not chart_interaction.active(app)
    dispatch(app, hits, graph_point, "release", route)
    assert not job_group_drag.active(app) and not chart_interaction.active(app)
    assert index(dashboard).for_job("4") is None
    assert app.marks == {"4", "5"} and app.tab == "jobs"


@pytest.mark.parametrize("route", ["app", "sgr"])
def test_real_chart_gesture_and_right_reset_do_not_open_job_menu(dashboard, route):
    from raven import chart_interaction, job_group_drag

    app, store = dashboard
    for step in range(30):
        store.record("1", {"k": "live", "t": 180 + step, "cpu": .2 + step / 60,
                           "rss": 1024 ** 3})
    app.job_panel_state.update(mode="analytics", analytics_view="job")
    frame(dashboard, height=90)
    app.selected_id, app.cursor["jobs"] = "1", app.visible_ids.index("1")
    _, hits = frame(dashboard, height=90)
    plot = next(plot for plot in app.chart_interaction_state["plots"] if plot.key[0] == "resource-series")
    first = (plot.visible.top + 1, plot.visible.left + 3)
    last = (plot.visible.bottom - 2, plot.visible.right - 3)
    mark(app, "4", "5")
    dispatch(app, hits, first, "press", route)
    assert chart_interaction.active(app) and not job_group_drag.active(app)
    dispatch(app, hits, last, "drag", route)
    dispatch(app, hits, last, "release", route)
    _, hits = frame(dashboard, height=90)
    dispatch(app, hits, first, "right", route)
    assert app.mode == "main" and app.tab == "jobs" and app.marks == {"4", "5"}
    assert not job_group_drag.active(app)


@pytest.mark.parametrize("route", ["app", "sgr"])
def test_group_chevron_accepts_context_menu_and_drop_without_folding(dashboard, route):
    app, _ = dashboard
    _, hits = frame(dashboard)
    destination = index(dashboard).for_job("1")
    location = disclosure_point(app, hits, "1")
    mark(app, "1")
    dispatch(app, hits, location, "right", route)
    assert app.mode == "job_group_menu" and "Ungroup" in labels(app)
    app.handle("esc")
    _, hits = frame(dashboard)
    mark(app, "4", "5")
    gesture(app, hits, point(app, hits, "4"), disclosure_point(app, hits, "1"), route)
    assert index(dashboard).for_job("4").id == destination.id
    assert not job_groups.registry(app).is_collapsed(destination)
    assert app.mode == "main" and app.tab == "jobs"


@pytest.mark.parametrize("route", ["app", "sgr"])
def test_drop_on_collapsed_destination_retains_hidden_siblings_and_summary(dashboard, route):
    app, _ = dashboard
    destination = index(dashboard).for_job("1")
    job_groups.fold(app, destination.id, True)
    _, hits = frame(dashboard)
    assert not any(kind == "job" and identifier == "2" for _, kind, identifier in hits)
    mark(app, "4", "5")
    gesture(app, hits, point(app, hits, "4"), point(app, hits, "1"), route)
    group = index(dashboard).for_job("4")
    assert group.members == ("1", "2", "3", "4", "5")
    assert job_groups.registry(app).is_collapsed(group)


def many_group_menu(dashboard, *, expanded=True):
    app, store = dashboard
    for index_ in range(12):
        ids = [str(100 + index_ * 2 + offset) for offset in range(2)]
        store.jobs.extend(Job(identifier, "aux" + identifier, "cpu", "RUNNING",
                              submit="2026-10-09T10:00:00", cluster="local")
                          for identifier in ids)
        assert manual_job_groups.create(app, store.snapshot(), ids).changed
    store.group = list(store.jobs)
    _, hits = frame(dashboard)
    mark(app, "4", "5")
    dispatch(app, hits, point(app, hits, "4"), "right")
    assert app.mode == "job_group_menu"
    if expanded:
        from raven import job_group_menu
        state = job_group_menu.initialize(app)
        state["cursor"] = next(index for index, item in enumerate(state["items"]) if item["action"] == "more")
        job_group_menu.handle_key(app, "enter")
    return frame(dashboard, height=15)


@pytest.mark.parametrize("route", ["app", "sgr"])
def test_group_menu_more_expands_through_real_pointer_without_action_or_tab_jump(dashboard, route):
    app, _ = dashboard
    _, hits = many_group_menu(dashboard, expanded=False)
    assert sum(label.startswith("Add to ") for label in labels(app)) == 3
    location = menu_point(app, "More (")
    dispatch(app, hits, location, "press", route)
    dispatch(app, hits, location, "release", route)
    _, hits = frame(dashboard, height=15)
    dispatch(app, hits, location, "release", route)
    assert app.mode == "job_group_menu" and app.tab == "jobs"
    assert app.marks == {"4", "5"}
    assert sum(label.startswith("Add to ") for label in labels(app)) == 14
    assert not any(label.startswith("More (") for label in labels(app))
    assert index(dashboard).for_job("4") is None


@pytest.mark.parametrize("route", ["app", "sgr"])
def test_group_menu_modal_scrollbar_header_controls_reach_top_and_bottom(dashboard, route):
    app, _ = dashboard
    _, hits = many_group_menu(dashboard)
    pane = next(pane for pane in app.scrollbar_state["panes"] if pane.key == "modal:job-groups")
    assert pane.limit > 0
    y, left, _right = pane.header
    dispatch(app, hits, (y, left + 2), "press", route)
    dispatch(app, hits, (y, left + 2), "release", route)
    assert app.job_group_menu_state["top"] == pane.limit
    _, hits = frame(dashboard, height=15)
    assert app.job_group_menu_state["top"] == pane.limit
    assert any(app.job_group_menu_state["items"][item]["label"] == "Close menu"
               for _row, _left, _right, item in app.job_group_menu_state["hits"])
    pane = next(pane for pane in app.scrollbar_state["panes"] if pane.key == "modal:job-groups")
    y, left, _right = pane.header
    dispatch(app, hits, (y, left), "press", route)
    dispatch(app, hits, (y, left), "release", route)
    assert app.job_group_menu_state["top"] == 0
    assert app.mode == "job_group_menu" and app.marks == {"4", "5"}


def test_group_menu_cached_hover_does_not_accumulate_scrollbars_or_recompose(dashboard, monkeypatch):
    app, store = dashboard
    many_group_menu(dashboard)
    cache = screen._FrameCache()
    cache.rebuild(app, app.views_ref, store, None, 260, 15)
    staged = len(app.scrollbar_state["staged"])
    pointer_locations = [(y, left) for y, left, _right, _index in app.job_group_menu_state["hits"]]
    with monkeypatch.context() as guard:
        guard.setattr(app.views_ref, "compose", lambda *_a, **_k: pytest.fail("Menu hover recomposed page"))
        guard.setattr(store, "snapshot", lambda: pytest.fail("Menu hover copied scheduler state"))
        for _ in range(20):
            for location in pointer_locations:
                dispatch(app, cache.hits, location, "motion", "sgr")
                cache.feedback(app, app.views_ref)
    assert len(app.scrollbar_state["staged"]) <= staged + 1
    assert len([pane for pane in app.scrollbar_state["panes"] if pane.key == "modal:job-groups"]) == 1
    assert app.mode == "job_group_menu" and app.marks == {"4", "5"}


def selection_list(dashboard, scope):
    """Use the same published spans as pointer ownership, in painted order."""
    from raven import job_group_drag
    app, store = dashboard
    if scope.startswith("history:"):
        app.tab = scope.split(":", 1)[1]
        history_browser._view(app).update(dock="left", ratio=40)
    elif scope == "analytics:advisor":
        app.tab, app.analytics_view = "analytics", "advisor"
    else:
        app.tab = "jobs" if scope == "recent" else scope
    if scope == "deps":
        for index_, job in enumerate(store.jobs[3:7], 1):
            job.dependency = "afterok:" + str(index_)
    _, hits = frame(dashboard, height=90)
    source_frame = job_group_drag.initialize(app)["frame"]
    positions = {}
    for row, records in source_frame["rows"].items():
        for record in records:
            if record["scope"] != scope:
                continue
            for column in range(record["rect"][1], record["rect"][3]):
                source = job_group_drag.source_at(app, row, column)
                if source is not None and source["scope"] == scope:
                    positions.setdefault(record["pointed"], (row, column))
                    break
    all_ids = source_frame["scopes"][scope]
    if scope == "jobs":
        all_ids = tuple(app.visible_ids)
    elif scope == "recent":
        all_ids = tuple(app.recent_ids)
    ids = [jid for jid in all_ids if jid in positions]
    assert len(ids) >= 3
    return hits, positions, ids, all_ids


@pytest.mark.parametrize("scope", ["jobs", "recent", "history", "group", "deps", "analytics:advisor",
                                    *["history:" + tab for tab in history_browser.TABS]])
@pytest.mark.parametrize("route", ["app", "native", "sgr"])
@pytest.mark.parametrize("reverse", [False, True])
def test_click_then_shift_click_selects_inclusive_range_in_each_list(dashboard, scope, route, reverse):
    app, _ = dashboard
    hits, positions, ids, order = selection_list(dashboard, scope)
    first, last = (ids[2], ids[0]) if reverse else (ids[0], ids[2])
    dispatch(app, hits, positions[first], "press", route)
    dispatch(app, hits, positions[first], "release", route)
    # Repaint may update Details, but a stable source range retains its anchor.
    hits, positions, _, order = selection_list(dashboard, scope)
    dispatch(app, hits, positions[last], "press", route, shift=True)
    dispatch(app, hits, positions[last], "release", route, shift=True)
    low, high = sorted((order.index(first), order.index(last)))
    assert app.marks == set(order[low:high + 1])
    assert app.mode == "main"


@pytest.mark.parametrize("scope", ["jobs", "recent", "history", "group", "deps", "analytics:advisor",
                                    *["history:" + tab for tab in history_browser.TABS]])
@pytest.mark.parametrize("route", ["app", "native", "sgr"])
@pytest.mark.parametrize("marked", [False, True])
def test_plain_drag_from_selected_or_marked_row_always_selects_range(dashboard, scope, route, marked):
    app, _ = dashboard
    hits, positions, ids, order = selection_list(dashboard, scope)
    first, last = ids[0], ids[2]
    if marked:
        mark(app, first, ids[-1])
    dispatch(app, hits, positions[first], "press", route)
    dispatch(app, hits, positions[first], "release", route)
    hits, positions, _, order = selection_list(dashboard, scope)
    before = app.table_state["manual_groups"]
    gesture(app, hits, positions[first], positions[last], route, alt=False)
    low, high = sorted((order.index(first), order.index(last)))
    assert app.marks == set(order[low:high + 1])
    assert app.table_state["manual_groups"] == before
    assert app.mode == "main"


def test_shift_anchor_never_spans_jobs_and_recent_panes(dashboard):
    app, _ = dashboard
    hits, positions, ids, _ = selection_list(dashboard, "jobs")
    dispatch(app, hits, positions[ids[0]], "press")
    dispatch(app, hits, positions[ids[0]], "release")
    hits, positions, ids, _ = selection_list(dashboard, "recent")
    dispatch(app, hits, positions[ids[2]], "press", shift=True)
    dispatch(app, hits, positions[ids[2]], "release", shift=True)
    assert app.marks == {ids[2]}


def test_shift_anchor_rejects_reused_job_attempt(dashboard):
    app, store = dashboard
    hits, positions, _, _ = selection_list(dashboard, "jobs")
    dispatch(app, hits, positions["4"], "press")
    dispatch(app, hits, positions["4"], "release")
    store.jobs[3].submit = "2026-10-10T18:00:00"
    hits, positions, _, _ = selection_list(dashboard, "jobs")
    dispatch(app, hits, positions["6"], "press", shift=True)
    dispatch(app, hits, positions["6"], "release", shift=True)
    assert app.marks == {"6"}


def test_clearing_selection_forgets_shift_anchor(dashboard):
    app, _ = dashboard
    hits, positions, _, _ = selection_list(dashboard, "jobs")
    dispatch(app, hits, positions["4"], "press")
    dispatch(app, hits, positions["4"], "release")
    job_selection.clear(app)
    hits, positions, _, _ = selection_list(dashboard, "jobs")
    dispatch(app, hits, positions["6"], "press", shift=True)
    dispatch(app, hits, positions["6"], "release", shift=True)
    assert app.marks == {"6"}


def test_alt_shift_drag_keeps_range_selection_priority(dashboard):
    app, _ = dashboard
    hits, positions, _, _ = selection_list(dashboard, "jobs")
    mark(app, "4")
    dispatch(app, hits, positions["4"], "press", "sgr", shift=True, alt=True)
    dispatch(app, hits, positions["6"], "drag", "sgr", shift=True, alt=True)
    dispatch(app, hits, positions["6"], "release", "sgr", shift=True, alt=True)
    assert app.marks == {"4", "5", "6"}
    assert index(dashboard).for_job("4") is None
