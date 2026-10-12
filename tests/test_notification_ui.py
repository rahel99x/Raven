"""Alerts retain complete evidence while keeping rendering and input bounded."""
from types import SimpleNamespace

import pytest

from raven import interaction as I, layout as L, notification_ui as N, scrollbars as B, workbench
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.notifications import initialize as center


@pytest.fixture
def app():
    result = App(Store(persist=False), None, None, Config({"animations": False}), "student")
    result.width, result.height = 80, 24
    return result


def render(app, ascii_=False):
    B.begin_frame(app, app.width, app.height)
    return N.overlay(SimpleNamespace(g=L.Glyphs(ascii_)), {}, app, app.width, app.height)


def text(rows):
    return "\n".join(L.row_text(row) for _, _, row in rows)


def post(app, message, level="error", **kwargs):
    identity = center(app).post(message, level, **kwargs)
    return center(app).get(identity)


def hit(app, prefix):
    return next((y, value) for y, _, value in app.notification_ui_state["control_hits"]
                if value["id"].startswith(prefix))


def test_command_opens_new_panel_and_preserves_activity_filter_aliases(app):
    assert workbench.run_command(app, ["notifications"])
    assert app.mode == "notifications"
    assert workbench.run_command(app, ["notifications", "moderate"])
    assert app.notification_ui_state["severity"] == "moderate"
    assert workbench.run_command(app, ["notifications", "level", "error"])
    assert app.mode == "activity" and app.activity.query == "level=error"
    assert workbench.run_command(app, ["activity"])
    assert app.mode == "activity"
    assert workbench.INSPECTION_MODES["notifications"] == "notification_ui"


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width,height", [(80, 24), (120, 40), (160, 50)])
def test_full_message_wraps_without_ellipsis_and_every_cell_fits(app, ascii_, width, height):
    app.width, app.height = width, height
    message = "Permission denied: " + "a-long-path/" * 45 + "LAST-CHARACTER"
    item = post(app, message)
    N.open_notifications(app, "critical")
    rows = render(app, ascii_)
    assert "…" not in text(rows)
    assert all(0 <= y < height and x >= 0 and x+L.vlen(L.row_text(row)) <= width for y, x, row in rows)
    body = N._document(app, width-5, ascii_)["blocks"][0][1]
    assert "".join(body.line(index) for index in range(len(body))) == item.text
    y, target = hit(app, "notification-dismiss:")
    assert ("x" if ascii_ else "×") in text(rows)
    assert target["right"]-target["left"] == 3


def test_notification_click_shows_evidence_causes_and_fixes_without_running_commands(app, monkeypatch):
    item = post(app, "Permission denied on /lab/private/run.json", source="project", job="442")
    N.open_notifications(app)
    render(app)
    monkeypatch.setattr(app, "run_command", lambda value: pytest.fail("No suggested command may run"))
    y, target = hit(app, "notification-open:")
    assert N.handle_mouse(app, y, target["left"])
    assert app.notification_ui_state["details"]
    document = N._document(app, 75, False)
    full = "\n".join(wrapped.text for _, wrapped in document["blocks"])
    assert item.text in full
    assert "Possible causes" in full and "Proposed fixes" in full
    assert "Check the full message" in full and "Job: 442" in full
    assert "does not run" in full


def test_exact_dismiss_cells_remove_only_clicked_notice(app):
    first = post(app, "First warning", "warning")
    second = post(app, "Second warning", "warning")
    N.open_notifications(app)
    render(app)
    y, target = hit(app, "notification-dismiss:"+second.id)
    N.handle_mouse(app, y, target["right"])
    assert center(app).get(second.id) is not None
    N.handle_mouse(app, y, target["left"])
    assert center(app).get(second.id) is None
    assert center(app).get(first.id) is first
    assert not app.notification_ui_state["details"]


def test_keyboard_delete_dismisses_selected_and_escape_returns_origin(app):
    item = post(app, "A warning", "warning")
    app.mode = "help"
    N.open_notifications(app)
    render(app)
    N.handle_key(app, "enter")
    assert app.notification_ui_state["details"]
    N.handle_key(app, "esc")
    assert app.mode == "notifications" and not app.notification_ui_state["details"]
    N.handle_key(app, "delete")
    assert center(app).get(item.id) is None
    N.handle_key(app, "esc")
    assert app.mode == "help"


def test_selected_notice_survives_new_arrival_and_closed_detail_never_retargets(app):
    first = post(app, "Original issue")
    N.open_notifications(app)
    render(app)
    post(app, "New unrelated issue")
    render(app)
    assert app.notification_ui_state["selected"] == first.id
    N.handle_key(app, "enter")
    render(app)
    center(app).dismiss(first.id)
    render(app)
    assert not app.notification_ui_state["details"]
    assert app.notification_ui_state["selected"] != first.id


def test_stale_notice_click_cannot_retarget_after_new_revision(app):
    original = post(app, "Original issue")
    N.open_notifications(app)
    render(app)
    y, target = hit(app, "notification-dismiss:"+original.id)
    post(app, "New issue")
    N.handle_mouse(app, y, target["left"])
    assert center(app).get(original.id) is original


def test_wrong_filter_does_not_dismiss_unseen_record(app):
    critical = post(app, "Critical issue")
    mild = post(app, "Collection deferred: rolling steps budget exhausted")
    N.open_notifications(app, "mild")
    render(app)
    N.run_command(app, ["notifications", "dismiss", critical.id])
    assert center(app).get(critical.id) is critical
    N.handle_key(app, "delete")
    assert center(app).get(mild.id) is None


def test_long_message_offscreen_text_stays_accessible_with_compact_cached_offsets(app, monkeypatch):
    message = ("A"*79 + "\n")*500 + "THE-FINAL-LINE"
    item = post(app, message)
    N.open_notifications(app)
    render(app)
    cached = app.notification_ui_state["document"]
    wrapped = cached["blocks"][0][1]
    assert wrapped.text is item.text
    assert wrapped.offsets.itemsize == 4
    assert len(app.notification_ui_state["control_hits"]) <= app.height+5
    monkeypatch.setattr(N, "_wrap", lambda *args, **kwargs: pytest.fail("Unchanged repaint must reuse wrapping"))
    render(app)
    assert app.notification_ui_state["document"] is cached
    N.handle_key(app, "end")
    rows = render(app)
    assert "THE-FINAL-LINE" in text(rows)
    assert len(rows) <= app.height


@pytest.mark.parametrize("ascii_", [False, True])
def test_unicode_wrap_preserves_original_and_width(app, ascii_):
    message = "训练运行  e\u0301  " * 25 + "\nsecond paragraph"
    item = post(app, message)
    N.open_notifications(app)
    render(app, ascii_)
    body = app.notification_ui_state["document"]["blocks"][0][1]
    assert all(L.vlen(body.line(index)) <= 73 for index in range(len(body)))
    expected = item.text.encode("ascii", "replace").decode("ascii") if ascii_ else item.text
    assert "".join(body.line(index) for index in range(len(body))) == expected.replace("\n", "")


def test_detail_reports_explicit_retention_limit(app):
    item = post(app, "X"*70000)
    assert item.truncated
    N.open_notifications(app)
    render(app)
    N.handle_key(app, "enter")
    document = N._document(app, 75, False)
    assert any("64 KiB" in wrapped.text for _, wrapped in document["blocks"])


def test_panel_scrollbar_has_reserved_rail_and_modal_clicks_do_not_select_jobs(app):
    post(app, "long message\n"*200)
    N.open_notifications(app)
    render(app)
    pane = next(pane for pane in B.initialize(app)["staged"] if pane.key == N.BAR_KEY)
    assert pane.absolute and pane.layer == 1 and pane.count > pane.page
    assert all(hit["right"] <= pane.rect.right-1 for y, _, hit in app.notification_ui_state["control_hits"]
               if pane.rect.top <= y < pane.rect.bottom)
    selected = app.selected_id
    N.handle_mouse(app, app.height-1, 0)
    assert app.selected_id == selected and app.mode == "notifications"


def test_interaction_graph_exposes_visible_notice_controls_and_rejects_stale_revision(app):
    item = post(app, "Permission denied")
    N.open_notifications(app)
    rows = render(app)
    I.publish(app, [[("", "")]]*app.height, [], app.width, app.height, overlays=rows)
    graph = I._current(app)
    assert any(control.id == "notification-dismiss:"+item.id for control in graph.controls)
    post(app, "New failure")
    assert I._current(app) is None


def test_real_keyboard_can_focus_filter_dismiss_and_details_controls(app):
    post(app, "Permission denied")
    N.open_notifications(app)
    rows = render(app)
    I.publish(app, [[("", "")]]*app.height, [], app.width, app.height, overlays=rows)
    app.handle("f8")
    assert app.interaction_state["active"]
    previous = app.interaction_state["focused"]
    app.handle("tab")
    assert app.interaction_state["focused"] != previous
    app.handle("esc")
    assert not app.interaction_state["active"] and app.mode == "notifications"
    app.handle("esc")
    assert app.mode == "main"


def test_status_row_counts_full_labels_and_hit_graph(app):
    post(app, "critical", "critical")
    post(app, "moderate", "moderate")
    post(app, "mild", "mild")
    views = SimpleNamespace(g=L.Glyphs(True))
    row = N.status_row(views, app, 80, 3)
    assert "Critical 1" in L.row_text(row) and "Moderate 1" in L.row_text(row) and "Mild 1" in L.row_text(row)
    assert {style for _, style in row} == {"red+bold", "orange+bold", "yellow+bold"}
    I.publish(app, [[("", "")]]*3+[row], [], app.width, app.height)
    assert {control.id for control in I._current(app).controls if control.id.startswith("notifications:")} == {
        "notifications:critical", "notifications:moderate", "notifications:mild"}
    target = app.notification_ui_state["status_hits"][1][2]
    N.handle_mouse(app, 3, target["left"])
    assert app.mode == "notifications" and app.notification_ui_state["severity"] == "moderate"


def test_status_has_no_phantom_row_and_stale_status_clicks_fail_closed(app):
    views = SimpleNamespace(g=L.Glyphs(False))
    assert N.status_row(views, app, 80, 3) is None
    item = post(app, "Failure")
    N.status_row(views, app, 80, 3)
    target = app.notification_ui_state["status_hits"][0][2]
    center(app).dismiss(item.id)
    assert not N.handle_mouse(app, 3, target["left"])
    assert app.mode == "main"


@pytest.mark.parametrize("button", ["left", "press"])
def test_real_controller_routes_header_notice_details_and_dismissal(app, button):
    from raven.views import Views
    views = Views(L.Glyphs(False), app.cfg)
    app.views_ref = views
    item = post(app, "Permission denied on /lab/experiment")

    def paint():
        snap = app.store.snapshot()
        views.compose(snap, app, app.width, app.height)
        return views.overlay(snap, app, app.width, app.height)

    paint()
    y, _, target = app.notification_ui_state["status_hits"][0]
    app.click(y, target["left"], app.last_hits, button=button)
    assert app.mode == "notifications"
    paint()
    y, target = hit(app, "notification-open:"+item.id)
    app.click(y, target["left"], app.last_hits, button=button)
    assert app.notification_ui_state["details"]
    paint()
    y, target = hit(app, "notification-filter:dismiss")
    app.click(y, target["left"], app.last_hits, button=button)
    assert center(app).get(item.id) is None
    assert app.mode == "notifications" and not app.notification_ui_state["details"]
    app.handle("esc")
    assert app.mode == "main"


def test_modal_scrollbar_bottom_and_top_show_complete_messages_without_changing_selection(app):
    item = post(app, "\n".join(f"error-line-{index:03}" for index in range(200)))
    N.open_notifications(app)

    def paint():
        rows = render(app)
        panes = B.publish(app, app.width, app.height, overlays=rows)
        return rows, next(pane for pane in panes if pane.key == N.BAR_KEY)

    _, pane = paint()
    assert B.handle_mouse(app, pane.header[0], pane.header[1]+2, button="left")
    rows, pane = paint()
    assert pane.target == pane.painted == pane.limit
    assert "error-line-199" in text(rows)
    assert app.notification_ui_state["selected"] == item.id
    assert B.handle_mouse(app, pane.header[0], pane.header[1], button="left")
    rows, pane = paint()
    assert pane.target == pane.painted == 0
    assert "error-line-000" in text(rows)
