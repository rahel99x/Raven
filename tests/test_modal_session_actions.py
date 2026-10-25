"""Mouse-only task, alert, diagnostics, export, and exact log workflows."""

import curses
import pytest

from raven import interaction, log_tools, session_tools, screen
from raven.alerts import AlertEngine
from raven.model import Finished
from raven.research import ResearchHub
from test_modal_pointer import dashboard as dashboard, paint, button, native_click


def test_activity_mouse_selects_notice_expands_and_closes(dashboard):
    app, views = dashboard
    app.activity.post("First notice with details")
    app.activity.post("Second notice with details")
    app.run_command("activity")
    paint(app, views)
    targets = [control for control in interaction.controls(app) if control.id.startswith("activity-row:")]
    assert len(targets) == 2
    native_click(app, targets[1])
    assert app.activity.cursor == 1
    paint(app, views)
    native_click(app, button(app, "activity-action:expand"))
    assert app.activity.expanded
    paint(app, views)
    native_click(app, button(app, "activity-action:close"))
    assert app.mode == "main"


def test_activity_equal_timestamps_keep_both_native_rows_selectable(dashboard):
    app, views = dashboard
    app.activity.post("First same-time notice")
    app.activity.post("Second same-time notice")
    with app.activity.lock:
        for notice in app.activity.notices:
            notice["time"] = 123
    app.run_command("activity")
    paint(app, views)
    rows = sorted((control for control in interaction.controls(app)
                   if control.id.startswith("activity-row:")), key=lambda control: control.rect.top)
    assert len(rows) == 2 and rows[0].id != rows[1].id
    assert len(app.activity.mouse_rows) == 2
    native_click(app, rows[1])
    assert app.activity.cursor == 1
    paint(app, views)
    native_click(app, button(app, "activity-action:expand"))
    rendered = views.overlay(app.store.snapshot(), app, app.width, app.height)
    assert "First same-time notice" in "\n".join("".join(text for text, _ in row) for _, _, row in rendered)
    assert app.activity.cursor == 1 and app.activity.expanded


def test_activity_equal_visible_notices_keep_exact_path_and_task_when_selected_and_copied(dashboard, tmp_path, monkeypatch):
    from raven import activity_ui, clipboard
    app, views = dashboard
    first, second = tmp_path / "first-report.txt", tmp_path / "second-report.txt"
    app.activity.post("Same visible notice", job="15", path=str(first), task="first export")
    app.activity.post("Same visible notice", job="15", path=str(second), task="second export")
    with app.activity.lock:
        for notice in app.activity.notices:
            notice["time"] = 123
    copied = []
    monkeypatch.setattr(clipboard, "copy_for_app", lambda app, value, *args, **kwargs: copied.append(value) or "copied")
    app.run_command("activity")
    paint(app, views)
    targets = sorted((control for control in interaction.controls(app)
                      if control.id.startswith("activity-row:")), key=lambda control: control.rect.top)
    assert len(targets) == 2
    native_click(app, targets[1])
    notice = activity_ui._ordered(app)[app.activity.cursor]
    assert notice["path"] == str(first) and notice["task"] == "first export"
    paint(app, views)
    native_click(app, button(app, "activity-action:copy"))
    assert copied == [str(first)]


def test_task_cancel_mouse_uses_exact_task_and_new_task_cannot_receive_old_release(dashboard):
    app, views = dashboard
    first = app.activity.start("First export")
    app.run_command("activity")
    paint(app, views)
    target = button(app, "activity-action:cancel-task")
    app.click(target.rect.top, target.rect.left, app.last_hits, button="press")
    second = app.activity.start("Replacement export")
    paint(app, views)
    app.click(target.rect.top, target.rect.left, app.last_hits, button="release")
    assert not first["cancel"].is_set() and not second["cancel"].is_set()
    native_click(app, button(app, "activity-action:cancel-task"))
    assert second["cancel"].is_set() and not first["cancel"].is_set()


def test_alert_mouse_rule_selection_details_snooze_resume_and_close(dashboard):
    app, views = dashboard
    engine = AlertEngine([{"name": "CPU idle", "when": "running and cpu == 0"},
                          {"name": "Pending", "when": "pending"}], app.store)
    app.store.alerts = engine
    try:
        app.run_command("alerts")
        paint(app, views)
        target = next(item for item in interaction.controls(app) if item.id.startswith("session-row:1:"))
        native_click(app, target)
        assert app.session_tools_state["alert_cursor"] == 1
        paint(app, views)
        native_click(app, button(app, "session-action:snooze"))
        assert engine.notification_muted("Pending") and not engine.notification_muted("CPU idle")
        paint(app, views)
        native_click(app, button(app, "session-action:resume"))
        assert not engine.notification_muted("Pending")
        paint(app, views)
        native_click(app, button(app, "session-action:details"))
        assert app.session_tools_state["alert_details"]
        paint(app, views)
        native_click(app, button(app, "session-action:close"))
        assert app.mode == "session_alerts" and not app.session_tools_state["alert_details"]
        paint(app, views)
        native_click(app, button(app, "session-action:close"))
        assert app.mode == "main"
    finally:
        engine.close()
        app.store.alerts = None


def test_inbox_mouse_open_exact_completion_and_review(dashboard):
    app, views = dashboard
    completed = Finished("15", "Exact completed run", "COMPLETED", start="2026-10-24T01:00:00", end="2026-10-24T01:02:00")
    app.store.apply_finished([completed])
    app.run_command("inbox")
    paint(app, views)
    native_click(app, button(app, "session-action:open"))
    assert app.tab == "history" and app.mode == "main" and app.selected_id == "15"
    assert session_tools._identity(completed) in app.session_tools_state["reviewed"]


def test_diagnostics_native_buttons_and_probe_log_raw_motion_without_job_actions(dashboard):
    app, views = dashboard
    app.mode = "terminal_diagnostics"
    app.session_tools_state["diagnostics"] = [{"name": "Encoding", "status": "ok", "detail": "UTF-8 ready"}]
    paint(app, views)
    native_click(app, button(app, "session-action:input-test"))
    assert app.mode == "terminal_probe"
    paint(app, views)
    screen._apply_input(app, ("mouse", (0, 45, 10, 0, curses.REPORT_MOUSE_POSITION)), app.last_hits, curses)
    assert any("Mouse: motion x=45, y=10" in value for value in app.session_tools_state["probe_events"])
    native_click(app, button(app, "session-action:close"))
    assert app.mode == "main" and not app.confirm


@pytest.mark.parametrize("sgr", [False, True])
@pytest.mark.parametrize("direction", ["up", "down"])
def test_terminal_probe_preserves_native_and_sgr_wheel_coordinates(dashboard, sgr, direction):
    app, views = dashboard
    app.width, app.height, app.mode = 80, 24, "terminal_probe"
    paint(app, views)
    if sgr:
        code = "64" if direction == "up" else "65"
        match = screen._SGR_MOUSE.fullmatch("\x1b[<" + code + ";31;11M")
        event = ("mouse", screen._sgr_mouse(match, curses))
    else:
        mask = curses.BUTTON4_PRESSED if direction == "up" else curses.BUTTON5_PRESSED
        event = ("mouse", (0, 30, 10, 0, mask))
    screen._apply_input(app, event, app.last_hits, curses)
    assert app.session_tools_state["probe_events"][-1] == f"Mouse: wheel-{direction} x=30, y=10"
    assert app.mode == "terminal_probe" and app.tab == "jobs" and not app.confirm


def test_exports_preview_and_back_mouse_use_existing_source(dashboard, tmp_path):
    app, views = dashboard
    path = tmp_path / "exact report.txt"
    path.write_text("Full report contents\n")
    app.activity.post("Export ready", path=str(path))
    app.run_command("exports")
    paint(app, views)
    native_click(app, button(app, "activity-action:preview"))
    assert app.mode == "export_preview"
    pending = app.research.pending
    if pending is not None:
        pending[0].result(timeout=5)
    app.research.poll_task()
    assert app.activity.export_preview["path"] == str(path)
    assert app.activity.export_preview["lines"] == ["Full report contents"]
    paint(app, views)
    native_click(app, button(app, "activity-action:back"))
    assert app.mode == "exports" and path.read_text() == "Full report contents\n"


def test_log_source_page_mouse_select_pan_and_return_preserve_source(dashboard, tmp_path):
    app, views = dashboard
    path = tmp_path / "run.out"
    path.write_bytes(b"first\toriginal\r\nsecond\toriginal\r\nthird\r\n")
    app.store.apply_finished([Finished("15", "Completed log run", "COMPLETED")])
    app.store.details["15"] = {"StdOut": str(path)}
    app.open_log("15")
    app.logs.entry = {"id": "out", "label": "stdout", "path": str(path), "job_id": "15"}
    app.logs.path = str(path)
    buffer = app.logs.buffer(str(path))
    log_tools.observe_buffer(app, buffer)
    app.research = ResearchHub(app.cfg, app.logs.files)
    app.run_command("logolder")
    pending = app.research.pending
    if pending is not None:
        pending[0].result(timeout=5)
    app.research.poll_task()
    assert app.mode == "log_tools_page", (app.message, app.command_ok, app.research.pending)
    paint(app, views)
    native_click(app, button(app, "log-tool-action:pan-right"))
    assert app.log_tools_state["page_pan"] == 8
    paint(app, views)
    native_click(app, button(app, "log-tool-action:select"))
    assert app.log_tools_state["selection"] == (0, 0)
    paint(app, views)
    native_click(app, button(app, "log-tool-action:close"))
    assert app.mode == "main" and app.tab == "log"
    assert path.read_bytes() == b"first\toriginal\r\nsecond\toriginal\r\nthird\r\n"


@pytest.mark.parametrize("mode", ["activity", "exports", "export_preview", "session_inbox", "session_alerts", "terminal_diagnostics", "terminal_probe", "log_tools_page", "log_tools_results", "log_tools_marks"])
def test_all_added_menus_expose_mouse_recovery_at_80_columns(dashboard, mode):
    app, views = dashboard
    app.width, app.height, app.mode = 80, 24, mode
    paint(app, views)
    prefixes = ("activity-action:", "session-action:", "log-tool-action:")
    controls = [item for item in interaction.controls(app) if item.id.startswith(prefixes)]
    assert controls
    close = next(item for item in controls if item.id.endswith((":close", ":back")))
    assert 0 <= close.rect.top < close.rect.bottom <= 24
    assert 0 <= close.rect.left < close.rect.right <= 80
    native_click(app, close)
    assert app.mode != mode


@pytest.mark.parametrize("data", [None, {"error": "Readable error"}, {"sources": []}])
def test_log_alternate_wait_error_or_missing_sources_keep_original_button(dashboard, monkeypatch, data):
    from raven.job_logs import workbench as logs
    app, views = dashboard
    app.tab = "log"
    app.log_workbench_state["view"] = "split"
    app.logs.browser = False
    monkeypatch.setattr(logs, "_alternate", lambda app: data)
    rows = logs.overlay(views, {}, app, 120, 30)
    assert rows
    target = next(value for _, _, value in app.log_workbench_state["control_hits"] if value["id"] == "log-source:original")
    y = next(y for y, _, value in app.log_workbench_state["control_hits"] if value is target)
    assert logs.handle_mouse(app, y, target["left"])
    assert app.log_workbench_state["view"] == "plain"
