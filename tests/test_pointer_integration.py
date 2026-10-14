"""Pointer gestures through the real controller and painted hit registries."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from raven import execution_ui, interaction, job_selection, layout as L, screen, toolbar, refresh_rate
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store
from raven.remote import LocalFiles
from raven.views import Views


MOUSE = SimpleNamespace(BUTTON1_CLICKED=1, BUTTON1_PRESSED=2, BUTTON1_RELEASED=4,
    BUTTON1_DOUBLE_CLICKED=8, BUTTON3_CLICKED=16, BUTTON3_PRESSED=32,
    BUTTON4_PRESSED=64, BUTTON5_PRESSED=128, REPORT_MOUSE_POSITION=256, BUTTON_SHIFT=512)


def test_persistent_toolbar_keeps_underlying_prompt_visible_and_masks_its_controls(dashboard):
    app = dashboard.app
    app.run_command("columns jobs")
    app.handle("f10")
    _, _, positioned = dashboard.draw()
    rendered = "\n".join(L.row_text(row) for _, _, row in positioned)
    assert "Jobs columns" in rendered and "Enter done" in rendered
    assert app.mode == "columns" and app.toolbar_state["menu"] is not None
    rect = interaction.Rect(*app.toolbar_state["menu_rect"])
    graph = app.interaction_state["graph"]
    assert all(not control.rect.intersects(rect) for control in graph.controls if control.group == "columns")
    assert positioned[-1][0] >= 1  # The dropdown paints after the underlying dialog.


@pytest.fixture
def dashboard(tmp_path):
    cfg = Config()
    cfg.set("startup_animation", False)
    cfg.set("animations", False)
    cfg.set("log_lines", 0)
    store = Store(persist=False)
    store.jobs = [Job(str(identifier), "active-" + str(identifier), "cpu", "RUNNING", cpus=2)
                  for identifier in range(70, 76)]
    store.finished = [Finished(str(identifier), "recent-" + str(identifier), "FAILED", cpus=1, exit="1:0")
                      for identifier in range(101, 104)]
    for record in store.jobs + store.finished:
        root = tmp_path / record.id
        root.mkdir()
        log = root / "stdout.log"
        log.write_text("AB_exact_job_" + record.id + "\n")
        store.details[record.id] = {"StdOut": str(log), "StdErr": str(log), "WorkDir": str(root)}
    app = App(store, None, None, cfg, "test", interactive=False)
    views = Views(L.Glyphs(False), cfg, files=LocalFiles())
    app.views_ref, app.logs.files = views, views.files
    result = SimpleNamespace(app=app, views=views, store=store)

    def draw(width=160, height=48):
        app.width = width
        rows, hits = views.compose(store.snapshot(), app, width, height)
        overlay = views.overlay(store.snapshot(), app, width, height)
        return rows, hits, overlay

    def point(identifier):
        y, kind, value = next(hit for hit in app.last_hits
                              if hit[1] in ("job", "recent", "fin", "group", "dep") and hit[2] == identifier)
        return y, 4

    def mouse(y, x, bits, *, shift=False):
        if shift:
            bits |= MOUSE.BUTTON_SHIFT
        screen._apply_input(app, ("mouse", (0, x, y, 0, bits)), app.last_hits, MOUSE)

    result.draw, result.point, result.mouse = draw, point, mouse
    draw()
    yield result
    if app.research:
        app.research.close()


@pytest.mark.parametrize("tab", ["jobs", "history"])
@pytest.mark.parametrize("shift", [False, True])
@pytest.mark.parametrize("motion_bits", [MOUSE.REPORT_MOUSE_POSITION,
                                        MOUSE.REPORT_MOUSE_POSITION | MOUSE.BUTTON1_PRESSED])
def test_screen_drag_selection_marks_exact_ids_and_preserves_shift_base(dashboard, tab, shift, motion_bits):
    app = dashboard.app
    app.enter_tab(tab)
    dashboard.draw()
    ids = list(app.visible_ids) + list(app.recent_ids) if tab == "jobs" else [job.id for job in app.history_jobs()]
    anchor, target = ids[0], ids[2]
    app.marks = {"retained-hidden-job"}
    dashboard.mouse(*dashboard.point(anchor), MOUSE.BUTTON1_PRESSED, shift=shift)
    assert job_selection.active(app)
    assert app.selected_id == anchor
    # Shift-click now completes a range immediately, including a one-row range
    # when there is no prior anchor; ordinary presses still preserve marks.
    assert app.marks == {"retained-hidden-job"} | ({anchor} if shift else set())
    dashboard.draw()
    dashboard.mouse(*dashboard.point(target), motion_bits, shift=shift)
    expected = set(ids[:3]) | ({"retained-hidden-job"} if shift else set())
    assert app.marks == expected and app.selected_id == target
    dashboard.draw()
    dashboard.mouse(*dashboard.point(target), MOUSE.BUTTON1_RELEASED, shift=shift)
    assert app.marks == expected and not job_selection.active(app)
    assert app.sel_anchor is None  # Job marking does not become a text yank.


def test_screen_drag_can_cross_queue_into_recents_and_shrink_before_release(dashboard):
    app = dashboard.app
    ids = list(app.visible_ids) + list(app.recent_ids)
    anchor, recent = ids[2], app.recent_ids[1]
    dashboard.mouse(*dashboard.point(anchor), MOUSE.BUTTON1_PRESSED)
    dashboard.mouse(*dashboard.point(recent), MOUSE.REPORT_MOUSE_POSITION)
    assert app.marks == set(ids[2:ids.index(recent) + 1])
    dashboard.mouse(*dashboard.point(ids[3]), MOUSE.REPORT_MOUSE_POSITION)
    assert app.marks == set(ids[2:4])
    dashboard.mouse(*dashboard.point(ids[3]), MOUSE.BUTTON1_RELEASED)
    assert not job_selection.active(app) and app.marks == set(ids[2:4])


def test_screen_single_press_and_release_keeps_existing_marks(dashboard):
    app = dashboard.app
    app.marks = {"73"}
    dashboard.mouse(*dashboard.point("71"), MOUSE.BUTTON1_PRESSED)
    dashboard.mouse(*dashboard.point("71"), MOUSE.BUTTON1_RELEASED)
    assert app.selected_id == "71" and app.marks == {"73"}
    assert not job_selection.active(app)


def test_drag_release_in_details_does_not_select_a_same_height_job(dashboard):
    app = dashboard.app
    dashboard.mouse(*dashboard.point("70"), MOUSE.BUTTON1_PRESSED)
    dashboard.mouse(*dashboard.point("72"), MOUSE.REPORT_MOUSE_POSITION)
    marks = set(app.marks)
    y, _ = dashboard.point("75")
    dashboard.mouse(y, app.job_panel_rect.x + 3, MOUSE.BUTTON1_RELEASED)
    assert app.marks == marks and app.selected_id == "72"
    assert not job_selection.active(app)


def test_changed_queue_invalidates_drag_instead_of_marking_recycled_row_positions(dashboard):
    app = dashboard.app
    app.marks = {"73"}
    dashboard.mouse(*dashboard.point("70"), MOUSE.BUTTON1_PRESSED)
    dashboard.store.jobs = dashboard.store.jobs[1:]
    dashboard.draw()
    dashboard.mouse(*dashboard.point("72"), MOUSE.REPORT_MOUSE_POSITION)
    dashboard.mouse(*dashboard.point("72"), MOUSE.BUTTON1_RELEASED)
    assert app.marks == {"73"} and not job_selection.active(app)


def test_escape_cancels_drag_and_restores_marks_without_quitting(dashboard):
    app = dashboard.app
    app.marks = {"75"}
    dashboard.mouse(*dashboard.point("70"), MOUSE.BUTTON1_PRESSED)
    dashboard.mouse(*dashboard.point("73"), MOUSE.REPORT_MOUSE_POSITION)
    screen._apply_input(app, ("esc", None), app.last_hits, MOUSE)
    assert app.marks == {"75"} and not job_selection.active(app)
    assert not app.quit


@pytest.mark.parametrize("button", ["motion", "drag", "release"])
def test_app_click_passive_pointer_keeps_jobs_marks_and_log_selection(dashboard, button):
    app = dashboard.app
    before = app.selected_id
    app.marks = {"75"}
    app.logs.cursor = 11
    app.logs.selection_path = app.logs.path
    app.logs.selection_anchor = app.logs.selection_end = 11
    y, x = dashboard.point("73")
    app.click(y, x, app.last_hits, button=button)
    assert app.selected_id == before and app.marks == {"75"}
    assert app.logs.cursor == 11 and app.logs.selection_active
    assert not job_selection.active(app)


def test_screen_hover_highlights_semantic_button_without_selecting_other_job(dashboard):
    app = dashboard.app
    target = next(control for control in interaction.controls(app) if control.id == "job_panel_tab:research")
    before = app.selected_id, dict(app.cursor), set(app.marks), app.job_panel_state["mode"]
    dashboard.mouse(target.rect.top, target.rect.left, MOUSE.REPORT_MOUSE_POSITION)
    assert (app.selected_id, app.cursor, app.marks, app.job_panel_state["mode"]) == before
    assert interaction.initialize(app)["hovered"] == target.id
    rows, _, _ = dashboard.draw()
    assert any("under" in style for _, style in rows[target.rect.top])


def test_real_keyboard_graph_after_mouse_button_moves_focus_without_activation(dashboard):
    app = dashboard.app
    app.enter_tab("nodes")
    dashboard.draw()
    mine = next(control for control in interaction.controls(app) if control.id == "nodes-view:mine")
    dashboard.mouse(mine.rect.top, mine.rect.left, MOUSE.BUTTON1_CLICKED)
    dashboard.draw()
    assert interaction.initialize(app)["active"]
    screen._apply_input(app, ("right", None), app.last_hits, MOUSE)
    assert interaction.initialize(app)["focused"] == "nodes-view:map"
    assert app.nodes_view == "mine"
    screen._apply_input(app, ("enter", None), app.last_hits, MOUSE)
    assert app.nodes_view == "map"
    dashboard.draw()
    assert interaction.initialize(app)["active"]
    screen._apply_input(app, ("esc", None), app.last_hits, MOUSE)
    assert not interaction.initialize(app)["active"] and app.mode == "main"


def refresh_button(app, action):
    return next(control for control in interaction.controls(app)
                if control.action == ("command", "refreshsettings " + action))


def test_screen_refresh_summary_opens_panel_and_requires_fresh_press_to_change_rate(dashboard):
    app = dashboard.app
    summary = next(control for control in interaction.controls(app) if control.id == "toolbar:refresh-settings:None")
    app.marks = {"75"}
    before = app.selected_id, dict(app.cursor), set(app.marks)
    initial_rate = refresh_rate.multiplier(app)
    initial_seconds = refresh_rate.cadence(app, "jobs")
    dashboard.mouse(summary.rect.top, summary.rect.left, MOUSE.BUTTON1_PRESSED)
    assert app.mode == "telemetry_settings" and not toolbar.initialize(app)["dragging"]
    dashboard.draw()
    faster = refresh_button(app, "queue faster")
    # Moving the opening gesture onto an actual setting must not activate it.
    dashboard.mouse(faster.rect.top, faster.rect.left,
                    MOUSE.REPORT_MOUSE_POSITION | MOUSE.BUTTON1_PRESSED)
    dashboard.mouse(faster.rect.top, faster.rect.left, MOUSE.BUTTON1_RELEASED)
    assert not toolbar.initialize(app)["pressed"]
    assert refresh_rate.multiplier(app) == initial_rate
    assert (app.selected_id, app.cursor, app.marks) == before and not job_selection.active(app)
    dashboard.mouse(faster.rect.top, faster.rect.left, MOUSE.BUTTON1_PRESSED)
    dashboard.mouse(faster.rect.top, faster.rect.left, MOUSE.BUTTON1_RELEASED)
    assert refresh_rate.multiplier(app) > initial_rate
    assert refresh_rate.cadence(app, "jobs") < initial_seconds
    dashboard.draw()
    close = refresh_button(app, "close")
    dashboard.mouse(close.rect.top, close.rect.left, MOUSE.BUTTON1_PRESSED)
    dashboard.mouse(close.rect.top, close.rect.left, MOUSE.BUTTON1_RELEASED)
    assert app.mode == "main"
    assert (app.selected_id, app.cursor, app.marks) == before and not job_selection.active(app)


def test_real_content_wheel_after_button_focus_restores_normal_content_keys(dashboard):
    app = dashboard.app
    target = next(control for control in interaction.controls(app) if control.id == "job_panel_tab:inspector")
    dashboard.mouse(target.rect.top, target.rect.left, MOUSE.BUTTON1_CLICKED)
    dashboard.draw()
    assert interaction.initialize(app)["active"]
    y = app.job_panel_rect.y + app.job_panel_rect.height - 2
    dashboard.mouse(y, app.job_panel_rect.x + 4, MOUSE.BUTTON5_PRESSED)
    assert not interaction.initialize(app)["active"]
    assert interaction.initialize(app)["focused"] is None
    assert app.job_panel_state["focus"] == "content"


@pytest.mark.parametrize("width,height", [(2, 2), (20, 3), (40, 6), (80, 10)])
def test_actual_modal_graph_contains_only_visible_clipped_controls(dashboard, width, height):
    app = dashboard.app
    app.run_command("inspect 70")
    dashboard.draw()
    assert app.mode == "analysis"
    dashboard.draw(width, height)
    controls = interaction.controls(app)
    assert all(0 <= control.rect.top < control.rect.bottom <= height
               and 0 <= control.rect.left < control.rect.right <= width for control in controls)
    assert not any(control.id.startswith(("sort:", "job_panel", "tab:")) for control in controls)
    visible_modal_ids = {value["id"] for _, _, value in app.analysis_state.get("control_hits", [])}
    from raven.scrollbars import descriptors as scrollbar_descriptors
    visible_modal_ids.update(value['id'] for value in scrollbar_descriptors(app))
    assert all(control.id in visible_modal_ids or control.id.startswith("toolbar:") for control in controls)
    if width <= 2 or height <= 3:
        assert not visible_modal_ids


def test_changed_modal_job_and_terminal_dimensions_reject_old_graph_controls(dashboard):
    app = dashboard.app
    app.run_command("inspect 70")
    dashboard.draw()
    old = next(control for control in interaction.controls(app) if not control.id.startswith("toolbar:"))
    app.analysis_state["job"] = "71"
    assert interaction.controls(app) == ()
    assert not interaction.handle_mouse(app, old.rect.top, old.rect.left)
    dashboard.draw()
    app.width = 80
    assert interaction.controls(app) == ()
    assert not interaction.handle_key(app, "enter")


@pytest.mark.parametrize("field,replacement", [("chart_job", "71"), ("metric", "different-metric")])
def test_chart_identity_changes_invalidate_painted_graph_actions(dashboard, field, replacement):
    app = dashboard.app
    app.run_command("inspect 70")
    dashboard.draw()
    assert any(not control.id.startswith("toolbar:") for control in interaction.controls(app))
    app.analysis_state[field] = replacement
    assert interaction.controls(app) == ()
    assert not interaction.handle_key(app, "enter")


@pytest.mark.parametrize("choice", ["cancel", "confirm"])
def test_actual_execution_review_mouse_buttons_reuse_guarded_native_activation(dashboard, monkeypatch, choice):
    app = dashboard.app
    state = execution_ui.initialize(app)
    state.update(view="review", review={"kind": "batch", "nodes": []}, pending_action=("submit",),
                 running=False, detail=False, focus="nodes")
    app.mode = "execution"
    confirmed = []
    monkeypatch.setattr(execution_ui, "_confirm_operation", lambda value: confirmed.append(value.execution_state["review"]))
    dashboard.draw()
    control = next(control for control in interaction.controls(app) if control.id == "execution:" + choice)
    dashboard.mouse(control.rect.top, control.rect.left, MOUSE.BUTTON1_CLICKED)
    if choice == "confirm":
        assert confirmed == [state["review"]]
        assert state["focus"] == "confirm"
    else:
        assert not confirmed and app.mode == "main" and state["pending_action"] is None


def test_actual_execution_review_ignores_old_controls_after_pending_review_identity_changes(dashboard, monkeypatch):
    app = dashboard.app
    state = execution_ui.initialize(app)
    state.update(view="review", review={"kind": "batch", "nodes": []}, pending_action=tuple(["submit"]),
                 running=False, detail=False, focus="nodes")
    app.mode = "execution"
    confirmed = []
    monkeypatch.setattr(execution_ui, "_confirm_operation", lambda value: confirmed.append(True))
    dashboard.draw()
    control = next(control for control in interaction.controls(app) if control.id == "execution:confirm")
    state["pending_action"] = tuple(["submit"])
    dashboard.mouse(control.rect.top, control.rect.left, MOUSE.BUTTON1_CLICKED)
    assert not confirmed and state["focus"] == "nodes"


def test_each_wrapped_inline_link_fragment_is_mouse_hoverable_and_native_clickable(dashboard, monkeypatch):
    app = dashboard.app
    from raven import job_panels
    from raven.workspace_layout import _reflow
    citation = {"id": "long-path", "job": app.selected_id, "path": "/very/long/diagnostic/path/for/the/selected/job.log"}
    source = [[(" Diagnostic " + citation["path"], "cyan")]]
    source_hits = [(0, "job_panel_action", (("inline_log", citation), 0, 28))]
    fitted, mapped = _reflow(source, source_hits, 28)
    assert len(fitted) >= 2 and len(mapped) == len(fitted)
    rows = [[("", "")]] * 8 + fitted
    hits = [(row + 8, kind, value) for row, kind, value in mapped]
    app.last_hits = hits
    graph = interaction.publish(app, rows, hits, 160, 48)
    controls = [control for control in graph.controls if control.id.startswith("job_panel_action:inline_log:long-path")]
    assert len(controls) == len(fitted)
    clicks = []
    original = job_panels.handle_mouse
    def capture(value, y, x, button="left", shift=False):
        if button == "left" and any(control.rect.contains(y, x) for control in controls):
            clicks.append((y, x))
            return True
        return original(value, y, x, button=button, shift=shift)
    monkeypatch.setattr(job_panels, "handle_mouse", capture)
    for control in controls:
        dashboard.mouse(control.rect.top, control.rect.left, MOUSE.REPORT_MOUSE_POSITION)
        assert interaction.initialize(app)["hovered"] == control.id
        dashboard.mouse(control.rect.top, control.rect.left, MOUSE.BUTTON1_CLICKED)
    assert len(clicks) == len(controls)


def test_history_drag_release_in_details_keeps_last_deliberate_range(dashboard):
    app = dashboard.app
    app.enter_tab("history")
    dashboard.draw()
    ids = list(app.last_history_ids)
    dashboard.mouse(*dashboard.point(ids[0]), MOUSE.BUTTON1_PRESSED)
    dashboard.mouse(*dashboard.point(ids[1]), MOUSE.REPORT_MOUSE_POSITION)
    marks = set(app.marks)
    y, _ = dashboard.point(ids[2])
    # Do not redraw after selection: only the source identity has changed,
    # and the Details rectangle must still protect the hidden Main row.
    dashboard.mouse(y, app.job_panel_rect.x + 3, MOUSE.BUTTON1_RELEASED)
    assert app.marks == marks and app.selected_id == ids[1]
    assert not job_selection.active(app)
