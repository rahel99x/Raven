"""Notification publication through the real header and command interface."""
from types import SimpleNamespace

import pytest

from raven import layout as L, notification_ui, screen
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.views import Views


def dashboard(ascii_=False):
    cfg = Config({"animations": False, "gpu_sampling": False})
    app = App(Store(persist=False), None, None, cfg, "tester")
    views = Views(L.Glyphs(ascii_), cfg)
    app.views_ref = views
    app.width, app.height = 120, 30
    return app, views


@pytest.mark.parametrize("width", [80, 120, 200])
@pytest.mark.parametrize("ascii_", [False, True])
def test_header_replaces_clipped_errors_with_exact_severity_buttons(width, ascii_):
    app, views = dashboard(ascii_)
    app.width = width
    error = "Access denied: " + "a long resource path/" * 20 + "\nCheck its owner."
    app.fail(error)
    app.say("A partial result is available", level="warning")
    snap = app.store.snapshot()
    snap["health"] = {"jobs": SimpleNamespace(error="Raven collection deferred: rolling steps budget exhausted", enabled=True)}
    rows = views.header(snap, app, width)
    text = "\n".join(L.row_text(row) for row in rows)
    assert "Critical 1" in text and "Moderate 1" in text and "Mild 1" in text
    assert "sources:" not in text and "Access denied:" not in text
    assert error in [notice.text for notice in app.notifications.snapshot().notices]
    hits = app.notification_ui_state["status_hits"]
    assert len(hits) == 3
    for row, _, hit in hits:
        assert 0 <= hit["left"] < hit["right"] <= width
        assert L.vlen(L.row_text(rows[row])) <= width
        if ascii_:
            assert L.row_text(rows[row]).isascii()
    row, _, hit = hits[0]
    assert notification_ui.handle_mouse(app, row, hit["left"], button="left")
    assert app.mode == "notifications"
    assert app.notification_ui_state["severity"] == "critical"


def test_error_footer_points_to_retained_message_and_copy_receipts_remain_visible():
    app, views = dashboard()
    app.fail("First error line\nSecond error line with the relevant diagnosis")
    assert "notifications" in L.row_text(views.footer(app, 80))
    assert "First error" not in L.row_text(views.footer(app, 80))
    assert app.notifications.snapshot().notices[0].text.endswith("relevant diagnosis")
    app.notifications.dismiss(app.notifications.snapshot().notices[0].id)
    assert "New notification" not in L.row_text(views.footer(app, 80))
    app.say("copied 2 lines to clipboard.txt")
    assert "copied 2 lines" in L.row_text(views.footer(app, 80))


def test_dismissed_health_condition_stays_quiet_across_header_repaints():
    app, views = dashboard()
    snap = app.store.snapshot()
    source = SimpleNamespace(error="squeue: unable to contact controller", enabled=True)
    snap["health"] = {"jobs": source}
    views.header(snap, app, 120)
    snapshot = app.notifications.snapshot()
    assert len(snapshot.notices) == 1
    app.notifications.dismiss(snapshot.notices[0].id)
    for _ in range(3):
        views.header(snap, app, 120)
        assert not app.notification_ui_state["status_hits"]
    source.error = ""
    views.header(snap, app, 120)
    source.error = "squeue: unable to contact controller"
    views.header(snap, app, 120)
    assert len(app.notifications.snapshot().notices) == 1


def test_real_command_opens_notification_panel_and_keeps_activity_available():
    app, views = dashboard()
    app.fail("Configuration could not be loaded")
    app.run_command("notifications critical")
    assert app.mode == "notifications"
    assert app.notification_ui_state["severity"] == "critical"
    notification_ui.handle_key(app, "esc")
    app.run_command("activity")
    assert app.mode == "activity"


def test_background_notification_changes_quiet_frame_without_a_toast():
    cfg = Config({"animations": False, "startup_animation": False, "raven": {"managed": True}})
    app = App(Store(persist=False), None, None, cfg, "tester")
    app.job_panel_state["mode"] = "off"
    # A fixed already-published Store token isolates background alert changes.
    app.store.raven_limits = object()
    publication = ("unchanged-store-publication",)
    before = screen._quiet_jobs_token(app, publication=publication)
    assert before is not None
    app.activity.post("Background source failed", level="error")
    assert not app.message
    after = screen._quiet_jobs_token(app, publication=publication)
    assert after != before
    assert after == screen._quiet_jobs_token(app, publication=publication)
    app.notifications.dismiss(app.notifications.snapshot().notices[0].id)
    assert screen._quiet_jobs_token(app, publication=publication) != after


def test_once_output_includes_full_wrapped_notification_without_clicking():
    app, views = dashboard(True)
    app.fail("Long diagnostic: " + "z" * 400 + "\nFinal diagnostic line.")
    output = screen.once_text(app, views, app.store, None, 80, False)
    summary = output.split("\nNotifications\n", 1)[1]
    assert summary.count("z") == 400
    assert "Final diagnostic line." in summary
    assert all(L.vlen(line) <= 80 for line in output.splitlines())
