"""Real pane traversal remains current as scheduler tables admit new rows."""
import pytest

from raven import interaction as ui, layout as L, table_ui
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store
from raven.remote import LocalFiles
from raven.views import Views


def control(identity, y, left, right, group="buttons", *, button=True, enabled=True):
    return ui.Control(identity, identity, ui.Rect(y, left, y + 1, right),
                      ("command", "noop " + identity), group, enabled, button=button)


def graph(controls, *, stacked=False):
    regions = (("page:main", ui.Rect(2, 0, 20 if stacked else 35, 100 if stacked else 50)),
               ("page:details", ui.Rect(21 if stacked else 2, 0 if stacked else 51, 35, 100)))
    return ui.Graph(tuple(controls), (), 100, 36, 1, regions=regions)


@pytest.mark.parametrize("stacked", [False, True])
def test_recents_right_reaches_details_even_when_cpu_header_center_is_closer(stacked):
    row = control("recent:101", 14, 1, 49 if not stacked else 99, "recent", button=False)
    cpu = control("sort:recent:cpus", 12, 38, 45, "sort:recent")
    details = control("job_panel_tab:inspector", 23 if stacked else 4, 1 if stacked else 53,
                      12 if stacked else 66, "job_panel_tab")
    frame = graph([row, cpu, details], stacked=stacked)
    assert ui.nearest(frame, row, "right") is details


def test_down_prefers_next_row_in_its_pane_over_a_nearer_diagonal_details_action():
    first = control("recent:101", 12, 1, 49, "recent", button=False)
    second = control("recent:102", 14, 1, 49, "recent", button=False)
    details = control("job_panel_action:open", 13, 53, 65, "job_panel_action")
    frame = graph([first, second, details])
    assert ui.nearest(frame, first, "down") is second


def test_details_left_edge_returns_to_selected_recents_instead_of_cpu_header():
    recent = control("recent:101", 14, 1, 49, "recent", button=False)
    cpu = control("sort:recent:cpus", 12, 38, 45, "sort:recent")
    inspector = control("job_panel_tab:inspector", 4, 53, 66, "job_panel_tab")
    frame = graph([recent, cpu, inspector])
    assert ui.nearest(frame, inspector, "left") is recent


def test_wide_rows_do_not_traverse_diagonally_into_their_own_column_headers():
    row = control("recent:101", 14, 1, 49, "recent", button=False)
    header = control("sort:recent:cpus", 12, 38, 45, "sort:recent")
    frame = graph([row, header])
    assert ui.nearest(frame, row, "right") is None


@pytest.mark.parametrize("side,direction", [("left", "right"), ("right", "left")])
def test_job_browser_row_crosses_into_native_content_instead_of_its_dock_button(side, direction):
    browser = ui.Rect(3, 0 if side == "left" else 71, 29, 29 if side == "left" else 100)
    content = ui.Rect(3, 30 if side == "left" else 0, 29, 100 if side == "left" else 70)
    row = control("history:analytics:job:101", 18, browser.left, browser.right, "job-history")
    dock = control("history:analytics:dock", 3, browser.right - 8, browser.right - 3, "job-history")
    metric = control("analytics-view:job", 5, content.left, content.left + 12, "analytics-views")
    frame = ui.Graph((row, dock, metric), (), 100, 30, 1,
                     regions=(("page:history", browser), ("page:main", content)))
    assert ui.nearest(frame, row, direction) is metric


@pytest.fixture
def native():
    store = Store(persist=False)
    store.jobs = [Job(str(10 + index), "active-" + str(index), "cpu", "RUNNING") for index in range(40)]
    store.finished = [Finished(str(1000 + index), "past-" + str(index), "COMPLETED") for index in range(300)]
    cfg = Config()
    app = App(store, None, None, cfg, "reader", interactive=False)
    views = Views(L.Glyphs(False), cfg, files=LocalFiles())
    app.views_ref = views
    # Keep this test independent of automatic launch evidence inference.
    app.table_state["groups"] = False
    yield app, views, store
    if app.research:
        app.research.close()


def draw(native, width=120, height=40):
    app, views, store = native
    views.compose(store.snapshot(), app, width, height)
    return ui.initialize(app)["graph"]


def focus(app, control_):
    ui.handle_mouse(app, control_.rect.top, control_.rect.right - 1, "motion")
    app.handle("f8")


@pytest.mark.parametrize("width", [80, 120, 160])
@pytest.mark.parametrize("ascii_", [False, True])
def test_native_recent_loading_republishes_arrow_focus_and_right_details(native, width, ascii_):
    app, views, store = native
    views.set_ascii(ascii_)
    frame = draw(native, width, 40)
    last = [item for item in frame.controls if item.group == "recent"][-1]
    admitted = len(app.recent_ids)
    focus(app, last)
    assert app.selected_id == last.label
    assert ui.needs_frame(app)
    frame = draw(native, width, 40)
    for _ in range(admitted + 5):
        app.handle("down")
        assert ui.needs_frame(app)
        frame = draw(native, width, 40)
        selected = frame.get(ui.initialize(app)["focused"])
        assert selected and selected.group == "recent"
        assert selected.label == app.selected_id
        assert app.recent_ids[app.cursor["jobs"] - len(app.visible_ids)] == selected.label
        assert not ui.needs_frame(app)
    assert len(app.recent_ids) > admitted
    selected_id = app.selected_id
    app.handle("right")
    target = frame.get(ui.initialize(app)["focused"])
    assert target and frame.region(target) == "page:details"
    assert not target.id.startswith("sort:")
    assert app.selected_id == selected_id
    # Traversing does not activate lazy Quick Advisor or change Details mode.
    assert app.job_panel_state["mode"] == "inspector"
    assert app.job_panel_state.get("quick", {}).get("status", "idle") == "idle"
    assert target.id == "job_panel_tab:inspector"
    app.handle("left")
    assert ui.initialize(app)["focused"] == "recent:" + selected_id


def test_native_queue_keyboard_edge_reveals_next_real_job_without_leaving_graph(native):
    app, _, _ = native
    frame = draw(native)
    shown = [item for item in frame.controls if item.group == "job"]
    assert len(shown) >= 2
    last = shown[-1]
    focus(app, last)
    draw(native)
    expected = app.visible_ids[app.visible_ids.index(last.label) + 1]
    app.handle("down")
    assert ui.needs_frame(app) and app.selected_id == expected
    frame = draw(native)
    assert ui.initialize(app)["active"]
    assert frame.get(ui.initialize(app)["focused"]).label == expected
    assert app.top["jobs"] > 0


def test_leading_job_cells_keep_row_selection_priority_above_recent_divider(native):
    app, _, store = native
    # The last queue row is one cell above the draggable Recents separator.
    store.jobs = [Job(str(index), "pending-" + str(index), "cpu", "PENDING") for index in range(5)]
    frame = draw(native)
    last = [item for item in frame.controls if item.group == "job"][-1]
    divider = app.pane_drag_state["dividers"]["recent:jobs"]
    assert last.rect.top == divider.y - 1
    assert frame.at(last.rect.top, app.workspace_main_rect.x + 5).id == last.id
    app.click(last.rect.top, app.workspace_main_rect.x + 5, app.last_hits)
    assert app.cursor["jobs"] == app.visible_ids.index(last.label)
    assert not app.pane_drag_state["capture"]
    assert app.pane_drag_state["focus"] != "recent:jobs"
    # The structural outer margin remains the generous resize target.
    assert frame.at(last.rect.top, app.workspace_main_rect.x) is None


def test_native_new_completion_preserves_scrolled_focus_and_exact_details_job(native):
    app, _, store = native
    frame = draw(native)
    recent = [item for item in frame.controls if item.group == "recent"][-1]
    focus(app, recent)
    draw(native)
    for _ in range(12):
        app.handle("down")
        draw(native)
    selected_id, focused = app.selected_id, ui.initialize(app)["focused"]
    store.finished.insert(0, Finished("9000", "new-completion", "FAILED"))
    frame = draw(native)
    assert ui.initialize(app)["active"] and ui.initialize(app)["focused"] == focused
    assert app.selected_id == selected_id
    app.handle("right")
    assert frame.region(frame.get(ui.initialize(app)["focused"])) == "page:details"
    assert app.selected_id == selected_id


def test_removed_focused_job_recovers_selected_real_row_in_the_same_table(native):
    app, _, store = native
    frame = draw(native)
    row = next(item for item in frame.controls if item.group == "job")
    focus(app, row)
    draw(native)
    store.jobs[:] = [job for job in store.jobs if job.id != row.label]
    frame = draw(native)
    selected = frame.get(ui.initialize(app)["focused"])
    assert selected and selected.group == "job" and selected.label == app.selected_id
    assert selected.label != row.label and ui.initialize(app)["active"]


def test_completed_focused_job_follows_its_exact_identity_into_recents(native):
    app, _, store = native
    frame = draw(native)
    row = next(item for item in frame.controls if item.group == "job")
    focus(app, row)
    draw(native)
    store.jobs[:] = [job for job in store.jobs if job.id != row.label]
    store.finished.insert(0, Finished(row.label, "just-completed", "FAILED"))
    frame = draw(native)
    selected = frame.get(ui.initialize(app)["focused"])
    assert selected and selected.group == "recent" and selected.label == row.label
    assert app.selected_id == row.label and ui.initialize(app)["active"]


def test_filter_and_sort_keep_graph_focus_on_the_exact_visible_job(native):
    app, _, _ = native
    frame = draw(native)
    row = [item for item in frame.controls if item.group == "job"][1]
    focus(app, row)
    draw(native)
    table_ui.set_sort(app, "jobs", "id", "desc")
    app.table_sort_changed("jobs", persist=False)
    frame = draw(native)
    assert frame.get(ui.initialize(app)["focused"]).label == row.label
    app.filter = "active-39"
    frame = draw(native)
    target = frame.get(ui.initialize(app)["focused"])
    assert target and target.group == "job" and target.label == app.selected_id
    assert target.label != row.label
    if ui.needs_frame(app):
        frame = draw(native)
    assert ui.controls(app) and not ui.needs_frame(app)
    assert frame.get(ui.initialize(app)["focused"]).label == app.selected_id


@pytest.mark.parametrize("mutate", [lambda app: app.top.update(recent=2),
                                     lambda app: app.cursor.update(jobs=2),
                                     lambda app: setattr(app, "filter", "missing"),
                                     lambda app: app.table_state["sorts"].update(jobs=[("id", "desc")]),
                                     lambda app: setattr(app.layout_state, "ratio", 70)])
def test_unpainted_viewport_changes_reject_old_mouse_and_activation(native, mutate):
    app, _, _ = native
    frame = draw(native)
    button_ = next(item for item in frame.controls if item.group == "job_panel_tab")
    state = ui.initialize(app)
    state.update(active=True, focused=button_.id)
    mutate(app)
    assert not ui.controls(app)
    assert not ui.handle_mouse(app, button_.rect.top, button_.rect.left)
    assert not ui.handle_key(app, "enter")
    assert app.job_panel_state["mode"] == "inspector"


def test_f8_transfers_arrow_ownership_from_native_history_browser(native):
    app, _, _ = native
    app.enter_tab("analytics")
    draw(native)
    app.history_browser_state["focused"] = True
    ui.run_command(app, ["focusbuttons", "on"])
    assert ui.initialize(app)["active"]
    assert not app.history_browser_state["focused"]


def test_research_does_not_reuse_jobs_details_geometry(native):
    app, _, _ = native
    draw(native)
    assert app.job_panel_rect is not None
    app.enter_tab("research")
    app.run_command("history-dock off")
    frame = draw(native)
    assert not any(name == "page:details" for name, _ in frame.regions)
    native_buttons = [item for item in frame.controls if item.id.startswith("research-view:")]
    assert native_buttons and all(frame.region(item) == "page:main" for item in native_buttons)


@pytest.mark.parametrize("key", ["enter", "space", "right"])
def test_pending_row_refresh_cannot_activate_previous_job_details(native, key):
    app, _, _ = native
    frame = draw(native)
    recent = next(item for item in frame.controls if item.group == "recent")
    focus(app, recent)
    assert ui.needs_frame(app)
    mode, panel_mode = app.mode, app.job_panel_state["mode"]
    assert ui.handle_key(app, key)
    assert app.mode == mode and app.job_panel_state["mode"] == panel_mode
    assert app.selected_id == recent.label
    assert ui.needs_frame(app)


def test_escape_exits_graph_while_row_refresh_is_pending(native):
    app, _, _ = native
    frame = draw(native)
    recent = next(item for item in frame.controls if item.group == "recent")
    focus(app, recent)
    assert ui.handle_key(app, "esc")
    assert not ui.initialize(app)["active"]


def test_native_hover_neither_selects_jobs_nor_requests_graph_refresh(native):
    app, _, _ = native
    frame = draw(native)
    before = app.selected_id
    for item in frame.controls:
        ui.handle_mouse(app, item.rect.top, item.rect.left, "motion")
        assert not ui.needs_frame(app)
        assert app.selected_id == before
    assert ui.initialize(app)["graph"] is frame


@pytest.mark.parametrize("width", [80, 160])
def test_history_graph_edge_admits_old_jobs_then_crosses_to_details(native, width):
    app, _, _ = native
    app.enter_tab("history")
    frame = draw(native, width, 40)
    shown = [item for item in frame.controls if item.group == "fin"]
    assert len(shown) >= 2
    last = shown[-1]
    focus(app, last)
    assert app.selected_id == last.label
    draw(native, width, 40)
    for _ in range(len(shown) + 3):
        previous = app.selected_id
        expected = app.last_history_ids[app.last_history_ids.index(previous) + 1]
        app.handle("down")
        assert ui.needs_frame(app) and app.selected_id == expected
        frame = draw(native, width, 40)
        current = frame.get(ui.initialize(app)["focused"])
        assert current.label == expected and current.group == "fin"
    app.handle("right")
    target = frame.get(ui.initialize(app)["focused"])
    assert frame.region(target) == "page:details"
    assert target.id == "job_panel_tab:inspector"
    app.handle("left")
    assert ui.initialize(app)["focused"] == "fin:" + app.selected_id
    assert app.top["history"] > 0
