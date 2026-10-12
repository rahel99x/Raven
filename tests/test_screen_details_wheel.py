"""Published Details wheels route once without querying displaced hover graphs."""
from types import SimpleNamespace
import curses

import pytest

from raven import (analytics_document as D, chart_interaction as C, history_browser as H,
                   interaction as I, job_panels as J, metric_live as M,
                   recent_history as R, screen, toolbar as T)
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs
from raven.model import Finished, Job, Store
from raven.views import Views


@pytest.fixture
def dashboard():
    cfg = Config({"workspace": {"density": "compact", "split": 45},
                  "startup_animation": False, "animations": False, "log_lines": 0})
    store = Store(persist=False)
    store.jobs = [Job("900", "active-job", "cpu", "RUNNING", cpus=4)]
    store.finished = [Finished(str(700 + i), f"finished-{i:02}", "COMPLETED",
                               cpus=4, elapsed="00:10:00") for i in range(48)]
    app = App(store, None, None, cfg, "test", interactive=False)
    views = Views(Glyphs(False), cfg)
    app.views_ref = views
    views.compose(store.snapshot(), app, 160, 36)
    app.run_command("jobpanel analytics advisor")
    _, hits = views.compose(store.snapshot(), app, 160, 36)
    yield app, views, store, hits
    if app.research:
        app.research.close()


def wheel(app, hits, y, x, direction=1):
    flag = curses.BUTTON5_PRESSED if direction > 0 else curses.BUTTON4_PRESSED
    screen._apply_input(app, ("mouse", (0, x, y, 0, flag)), hits, curses)


def content_cell(app):
    rect = app.job_panel_rect
    return rect.y + rect.height - 2, rect.x + 2


def forbidden(*args, **kwargs):
    raise AssertionError("wheel repeated click/hover routing or read source data")


def test_details_burst_routes_once_keeps_all_moves_and_never_reads_or_queries_hover(dashboard, monkeypatch):
    app, _, store, hits = dashboard
    y, x = content_cell(app)
    selected, cursor = app.selected_id, dict(app.cursor)
    calls = []
    original = J.handle_mouse
    def route(*args, **kwargs):
        calls.append(kwargs["button"])
        return original(*args, **kwargs)
    monkeypatch.setattr(J, "handle_mouse", route)
    monkeypatch.setattr(app, "click", forbidden)
    monkeypatch.setattr(store, "snapshot", forbidden)
    monkeypatch.setattr(I, "_context", forbidden)
    monkeypatch.setattr(I, "handle_mouse", forbidden)
    monkeypatch.setattr(C, "hover", forbidden)
    for _ in range(100):
        wheel(app, hits, y, x)
    for _ in range(27):
        wheel(app, hits, y, x, -1)
    assert calls == ["wheel-down"] * 100 + ["wheel-up"] * 27
    assert app.layout_state.scroll["jobs:details"] == 73
    assert app.selected_id == selected and app.cursor == cursor
    assert app.interaction_state["pointer"] == app.chart_interaction_state["pointer"] == (y, x)
    assert not I.needs_frame(app)


def test_sticky_header_wheel_uses_current_hits_without_changing_view_or_scroll(dashboard, monkeypatch):
    app, _, _, hits = dashboard
    y, _, value = next(hit for hit in hits if hit[1] == "job_panel_tab")
    _, left, right = value
    before = (J.initialize(app)["mode"], J.initialize(app)["analytics_view"])
    app.last_hits = []  # The input owns the exact freshly published hit map.
    monkeypatch.setattr(app, "click", forbidden)
    wheel(app, hits, y, (left + right - 1) // 2)
    assert (J.initialize(app)["mode"], J.initialize(app)["analytics_view"]) == before
    assert app.layout_state.scroll["jobs:details"] == 0
    assert app.last_hits is hits


def test_wheel_cancels_chart_and_live_previews_before_scrolling_and_preserves_pointer(dashboard):
    app, _, _, hits = dashboard
    y, x = content_cell(app)
    app.chart_interaction_state["capture"] = {"preview": "uncommitted"}
    identity = ("resource-series", "900", "cpu-rate", "%", "|")
    live = M.initialize(app)
    entry = M.set_running(app, identity, True)
    assert M.set_delta(app, identity, 1.0)
    live["capture"] = {"control": SimpleNamespace(key=identity, token=entry["token"]), "original": 30.0}
    app.interaction_state.update(active=True, focused="job:900")
    wheel(app, hits, y, x)
    assert app.chart_interaction_state["capture"] is None
    assert live["capture"] is None and live["entries"][identity]["delta"] == 30.0
    assert not app.interaction_state["active"] and app.interaction_state["focused"] is None
    assert app.layout_state.scroll["jobs:details"] == 1
    assert app.interaction_state["pointer"] == app.chart_interaction_state["pointer"] == (y, x)


@pytest.mark.parametrize("owner", ["toolbar", "history", "recent", "series"])
def test_global_and_native_wheel_owners_keep_precedence_over_details(dashboard, monkeypatch, owner):
    app, _, _, hits = dashboard
    calls = []
    routes = (("toolbar", T), ("history", H), ("recent", R), ("series", D))
    for name, module in routes:
        def route(*args, _name=name, **kwargs):
            calls.append(_name)
            return _name == owner
        monkeypatch.setattr(module, "handle_mouse", route)
    monkeypatch.setattr(J, "handle_mouse", forbidden)
    monkeypatch.setattr(app, "click", forbidden)
    y, x = content_cell(app)
    wheel(app, hits, y, x)
    order = [name for name, _ in routes]
    assert calls == order[:order.index(owner) + 1]
    assert app.layout_state.scroll["jobs:details"] == 0
    assert app.interaction_state["pointer"] == app.chart_interaction_state["pointer"] == (y, x)


def test_modal_over_details_keeps_controller_click_route(dashboard, monkeypatch):
    app, _, _, hits = dashboard
    app.mode = "confirm"
    calls = []
    monkeypatch.setattr(app, "click", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(J, "handle_mouse", forbidden)
    y, x = content_cell(app)
    wheel(app, hits, y, x)
    assert calls == [((y, x, hits), {"button": "wheel-down", "shift": False})]
    assert app.layout_state.scroll["jobs:details"] == 0


def test_passive_motion_still_resolves_current_pointer_feedback(dashboard, monkeypatch):
    app, _, _, hits = dashboard
    y, x = content_cell(app)
    calls = []
    for name, module, attribute in (("graph", I, "handle_mouse"), ("chart", C, "hover")):
        original = getattr(module, attribute)
        def route(*args, _name=name, _original=original, **kwargs):
            calls.append((_name, args[1:3]))
            return _original(*args, **kwargs)
        monkeypatch.setattr(module, attribute, route)
    screen._apply_input(app, ("mouse", (0, x, y, 0, curses.REPORT_MOUSE_POSITION)), hits, curses)
    assert calls == [("graph", (y, x)), ("chart", (y, x))]
    assert app.layout_state.scroll["jobs:details"] == 0


def test_history_modal_wheel_preserves_controller_and_background_pane_geometry(dashboard, monkeypatch):
    app, views, store, _ = dashboard
    app.enter_tab("history")
    _, hits = views.compose(store.snapshot(), app, 160, 36)
    app.mode = "confirm"
    calls = []
    monkeypatch.setattr(app, "click", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(J, "handle_mouse", forbidden)
    y, x = content_cell(app)
    wheel(app, hits, y, x)
    assert calls == [((y, x, hits), {"button": "wheel-down", "shift": False})]
    assert app.layout_state.scroll["history:details"] == 0


def test_history_details_wheel_burst_uses_history_scroll_without_reading_sources(dashboard, monkeypatch):
    app, views, store, _ = dashboard
    app.enter_tab("history")
    views.compose(store.snapshot(), app, 160, 36)
    app.run_command("jobpanel analytics advisor")
    _, hits = views.compose(store.snapshot(), app, 160, 36)
    y, x = content_cell(app)
    previous = (app.selected_id, dict(app.cursor), app.layout_state.scroll["jobs:details"])
    monkeypatch.setattr(app, "click", forbidden)
    monkeypatch.setattr(store, "snapshot", forbidden)
    monkeypatch.setattr(I, "_context", forbidden)
    monkeypatch.setattr(C, "hover", forbidden)
    for _ in range(100):
        wheel(app, hits, y, x)
    for _ in range(27):
        wheel(app, hits, y, x, -1)
    assert app.layout_state.scroll["history:details"] == 73
    assert (app.selected_id, app.cursor, app.layout_state.scroll["jobs:details"]) == previous
