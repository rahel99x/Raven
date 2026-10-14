"""Cancelled, stale and stationary pointer reports cannot become page actions."""
from types import SimpleNamespace

import pytest

from raven import history_browser as H, layout as L, pane_drag as P
from raven import refresh_rate as R, scrollbars as S, text_selection as T, toolbar as B
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store


@pytest.fixture
def app():
    store = Store(persist=False)
    store.jobs = [Job("41", "selected", "cpu", "RUNNING")]
    store.finished = [Finished("40", "previous", "COMPLETED")]
    instance = App(store, None, None, Config({"animations": False}), "test", interactive=False)
    instance.width, instance.height, instance.body_origin = 120, 32, 4
    instance.selected_id = "41"
    yield instance
    if instance.research:
        instance.research.close()


def toolbar(app):
    B.render_bar(SimpleNamespace(g=L.Glyphs(False)), app, app.width)
    return B.initialize(app)["hits"]


def divider(app):
    app.ratio = 60
    P.begin_frame(app, app.width, app.height)
    return P.register(app, "split", "vertical", 71, 4, 1, 27, 0, 119, app.ratio,
                      lambda value: setattr(app, "ratio", value), minimum=20, maximum=80)


def document(app):
    rows = [[("", "")] for _ in range(app.height)]
    for index in range(5, 15):
        rows[index] = [(f"memory row {index}", "text")]
    S.begin_frame(app)
    S.register(app, "advisor", (5, 0, 15, 40), 100, 10, 0, 0,
               lambda value: setattr(app, "offset", value), context="job41")
    S.publish(app, app.width, app.height)
    T.publish(app, rows, app.width, app.height)


def browser(app):
    app.tab, app.analytics_job = "analytics", "41"
    P.begin_frame(app, app.width, app.height)
    H.wrap_render(SimpleNamespace(g=L.Glyphs(False)), app.store.snapshot(), app,
                  app.width, app.height - app.body_origin - 1,
                  lambda w, h: ([[("Metric details", "text")]], []))
    rect = app.history_browser_rect
    y, _, hit = next(hit for hit in H.initialize(app)["frame"]["hits"]
                     if hit[2]["id"].endswith(":drag"))
    return rect.y + y, rect.x + hit["left"]


@pytest.mark.parametrize("reason", ["tab", "mode", "resize", "right"])
def test_divider_cancelled_before_repaint_does_not_resume_on_hover(app, reason):
    divider(app)
    saves = []
    app.save = lambda: saves.append(True)
    assert P.handle_mouse(app, 10, 71, "press")
    assert P.handle_mouse(app, 10, 84, "drag")
    assert app.ratio != 60
    if reason == "tab":
        app.tab = "research"
    elif reason == "mode":
        app.mode = "help"
    elif reason == "resize":
        app.width = 80
    else:
        assert not P.handle_mouse(app, 8, 10, "right")
    P.tick(app)
    assert app.ratio == 60 and not P.active(app)
    assert not P.handle_mouse(app, 10, 100, "motion")
    assert P.handle_mouse(app, 10, 100, "release")
    assert not P.handle_mouse(app, 10, 100, "release")
    assert not saves


@pytest.mark.parametrize("reason", ["tab", "mode", "resize"])
def test_toolbar_press_cancels_context_change_before_next_paint(app, reason):
    y, left, _, _, _ = next(hit for hit in toolbar(app) if hit[3] == "menu")
    assert B.handle_mouse(app, y, left, "press")
    assert B.initialize(app)["pressed"]
    before = R.multiplier(app)
    if reason == "tab":
        app.tab = "research"
    elif reason == "mode":
        app.mode = "help"
    else:
        app.width = 80
    app.click(20, left, [], button="motion")
    assert R.multiplier(app) == before and not B.initialize(app)["pressed"]
    assert B.handle_mouse(app, 20, left, "release")
    assert R.multiplier(app) == before


def test_toolbar_stale_geometry_cannot_activate_old_quit_button(app):
    toolbar(app)
    app.width = 80
    assert not B.handle_mouse(app, 0, 0, "press")
    assert not app.quit


def test_toolbar_fixed_quit_still_available_when_modal_changes(app):
    toolbar(app)
    app.mode = "confirm"
    assert B.handle_mouse(app, 0, 0, "press")
    assert app.quit and app.mode == "confirm"


def test_stationary_refresh_control_reports_do_not_resynchronize_collectors(app, monkeypatch):
    from raven import telemetry_settings_ui as refresh
    views = SimpleNamespace(g=L.Glyphs(False))
    y, left, _, _, _ = next(hit for hit in toolbar(app) if hit[3] == "refresh-settings")
    assert B.handle_mouse(app, y, left, "press")
    assert app.mode == refresh.MODE
    refresh.overlay(views, {}, app, app.width, app.height)
    y, _, control = next(hit for hit in app.telemetry_settings_ui_state["control_hits"]
                         if hit[2]["action"] == ("command", "refreshsettings queue faster"))
    initial_rate = R.multiplier(app)
    assert refresh.handle_mouse(app, y, control["left"], "press")
    refresh.overlay(views, {}, app, app.width, app.height)
    rate = R.multiplier(app)
    assert rate > initial_rate
    monkeypatch.setattr(R, "_sync", lambda *args: pytest.fail("Stationary refresh motion resynchronized collectors"))
    for _ in range(200):
        assert refresh.handle_mouse(app, y, control["left"], "motion")
    assert refresh.handle_mouse(app, y, control["left"], "release")
    assert R.multiplier(app) == rate


@pytest.mark.parametrize("reason", ["tab", "mode", "resize", "menu", "escape"])
def test_dock_maintenance_cancellation_consumes_late_release(app, reason):
    point = browser(app)
    app.save = lambda: pytest.fail("Cancelled docking saved")
    assert H.handle_mouse(app, *point, "press")
    if reason == "tab":
        app.tab = "research"
    elif reason == "mode":
        app.mode = "help"
    elif reason == "resize":
        app.width = 80
    elif reason == "menu":
        app.toolbar_state["menu"] = 0
    else:
        assert H.handle_key(app, "esc")
    H.tick(app)
    assert not H.initialize(app)["drag"]
    assert H.handle_mouse(app, 5, 110, "release")
    assert not H.handle_mouse(app, 5, 110, "release")


@pytest.mark.parametrize("reason", ["tab", "mode", "resize", "menu"])
def test_text_capture_invalidates_before_repaint_without_losing_pinned_text(app, reason):
    app.tab = "analytics"
    document(app)
    assert T.handle_mouse(app, 5, 4, "press")
    original = dict(T.initialize(app)["selection"])
    if reason == "tab":
        app.tab = "research"
    elif reason == "mode":
        app.mode = "help"
    elif reason == "resize":
        app.width = 80
    else:
        app.toolbar_state["menu"] = 0
    assert not T.handle_mouse(app, 8, 4, "motion")
    assert not T.active(app)
    assert T.initialize(app)["selection"] == original
    assert T.handle_mouse(app, 8, 4, "release")
    assert not T.handle_mouse(app, 8, 4, "release")


def test_stationary_text_drag_does_not_rescan_selection_cache(app, monkeypatch):
    app.tab = "analytics"
    document(app)
    assert T.handle_mouse(app, 5, 4, "press")
    monkeypatch.setattr(T, "_remember", lambda *args: pytest.fail("Same-row motion rescanned cached text"))
    for x in range(40):
        assert T.handle_mouse(app, 5, x, "motion")
    assert T.handle_mouse(app, 5, 4, "release")


def test_stationary_scrollbar_capture_does_not_mark_document_dirty(app):
    app.tab = "analytics"
    document(app)
    assert S.handle_mouse(app, 5, 39, "press")
    assert S.handle_mouse(app, 10, 39, "drag")
    app.interaction_state["frame_required"] = False
    before = app.offset
    for x in range(200):
        assert S.handle_mouse(app, 10, x, "motion")
    assert app.offset == before and not app.interaction_state["frame_required"]
    assert S.handle_mouse(app, 10, 39, "release")


@pytest.mark.parametrize("owner", ["pane", "dock", "text", "scrollbar"])
def test_fresh_gesture_elsewhere_supersedes_cancelled_release(app, owner):
    if owner == "pane":
        divider(app)
        assert P.handle_mouse(app, 10, 71, "press")
        P.cancel(app)
        handler = P.handle_mouse
    elif owner == "dock":
        point = browser(app)
        assert H.handle_mouse(app, *point, "press")
        assert H.handle_key(app, "esc")
        handler = H.handle_mouse
    elif owner == "text":
        app.tab = "analytics"
        document(app)
        assert T.handle_mouse(app, 5, 4, "press")
        T.clear(app)
        handler = T.handle_mouse
    else:
        app.tab = "analytics"
        document(app)
        assert S.handle_mouse(app, 5, 39, "press")
        S.cancel(app)
        handler = S.handle_mouse
    # A new graph gesture lies outside this owner's geometry. Its release
    # belongs to that graph, rather than to the previous cancelled capture.
    assert not handler(app, -10, -10, "press")
    assert not handler(app, 8, 60, "release")


@pytest.mark.parametrize("preference", ["dock", "enabled", "ratio"])
def test_browser_changed_layout_cannot_activate_stale_job_rectangle(app, preference):
    browser(app)
    frame = H.initialize(app)["frame"]
    rect = app.history_browser_rect
    row, _, target = next(hit for hit in frame["hits"] if ":job:" in hit[2]["id"])
    view = H._view(app)
    view[preference] = {"dock": "bottom", "enabled": False, "ratio": 40}[preference]
    app.run_command = lambda *args: pytest.fail("Activated a stale history rectangle")
    assert not H.handle_mouse(app, rect.y + row, rect.x + target["left"], "press")


@pytest.mark.parametrize("button", ["motion", "drag", "release"])
def test_uncaptured_hover_cannot_activate_global_or_history_buttons(app, button):
    toolbar(app)
    app.run_command = lambda *args: pytest.fail("Hover executed a command")
    app.handle_action = lambda *args: pytest.fail("Hover executed an action")
    before = R.multiplier(app)
    for row, left, right, kind, key in tuple(B.initialize(app)["hits"]):
        assert not B.handle_mouse(app, row, left, button)
    assert not app.quit and R.multiplier(app) == before
    browser(app)
    rect = app.history_browser_rect
    selected = app.analytics_job
    for row, kind, target in tuple(H.initialize(app)["frame"]["hits"]):
        assert not H.handle_mouse(app, rect.y + row, rect.x + target["left"], button)
    assert app.tab == "analytics" and app.analytics_job == selected


@pytest.mark.parametrize("invalid", [None, True, 2.5, "7", float("nan")])
@pytest.mark.parametrize("handler", [B.handle_mouse, P.handle_mouse, H.handle_mouse, T.handle_mouse, S.handle_mouse])
def test_malformed_coordinates_cannot_mutate_pointer_state(app, handler, invalid):
    toolbar(app)
    divider(app)
    document(app)
    assert not handler(app, invalid, 4, "motion")
    assert not handler(app, 4, invalid, "press")
    assert app.tab == "jobs" and app.selected_id == "41" and not app.quit
