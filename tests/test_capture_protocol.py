"""One capture lifetime across native, direct, cancelled and fresh gestures."""
from types import SimpleNamespace

import pytest

from raven import pointer_routing as P, job_selection, toolbar
from test_pointer_integration import dashboard, MOUSE


def send(dashboard, route, point, button):
    y, x = point
    if route == "direct":
        dashboard.app.click(y, x, dashboard.app.last_hits, button=button)
    else:
        flags = {"press": MOUSE.BUTTON1_PRESSED,
                 "release": MOUSE.BUTTON1_RELEASED,
                 "drag": MOUSE.REPORT_MOUSE_POSITION | MOUSE.BUTTON1_PRESSED}
        dashboard.mouse(y, x, flags[button])


@pytest.mark.parametrize("route", ["native", "direct"])
def test_selection_owner_keeps_release_across_toolbar_and_new_slider_owns_next(dashboard, route):
    app = dashboard.app
    send(dashboard, route, dashboard.point("70"), "press")
    lease = app.pointer_routing_state["lease"]
    assert lease.owner.name == "job_selection"
    send(dashboard, route, dashboard.point("72"), "drag")
    selected = set(app.marks)
    toolbar.render_bar(dashboard.views, app, app.width)
    track = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "track")
    point = track[0], track[1]
    send(dashboard, route, point, "release")
    assert app.marks == selected and not app.toolbar_state["dragging"]
    assert app.pointer_routing_state["lease"] is None
    send(dashboard, route, point, "press")
    assert app.pointer_routing_state["lease"].owner.name == "toolbar"
    assert app.pointer_routing_state["lease"].generation > lease.generation
    send(dashboard, route, dashboard.point("74"), "release")
    assert app.marks == selected and not P.active(app)


@pytest.mark.parametrize("route", ["native", "direct"])
def test_lost_release_then_fresh_slider_commits_marks_and_revokes_old_owner(dashboard, route):
    app = dashboard.app
    send(dashboard, route, dashboard.point("70"), "press")
    send(dashboard, route, dashboard.point("72"), "drag")
    selected = set(app.marks)
    toolbar.render_bar(dashboard.views, app, app.width)
    track = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "track")
    send(dashboard, route, (track[0], track[1]), "press")
    assert app.pointer_routing_state["lease"].owner.name == "toolbar"
    assert not job_selection.active(app)
    assert not any(getattr(app, name, {}).get(marker) for name, marker in P.RELEASE_MARKERS)
    send(dashboard, route, (track[0], track[2] - 1), "release")
    assert app.marks == selected and app.pointer_routing_state["lease"] is None


@pytest.mark.parametrize("change", ["tab", "mode", "width", "height"])
def test_context_token_prevents_old_owner_from_dispatching_in_new_document(dashboard, change, monkeypatch):
    app = dashboard.app
    send(dashboard, "direct", dashboard.point("70"), "press")
    setattr(app, change, {"tab": "research", "mode": "confirm", "width": 120, "height": 30}[change])
    monkeypatch.setattr(job_selection, "handle_mouse", lambda *args, **kwargs: pytest.fail("stale owner dispatched"))
    assert P.route_capture(app, P.PointerEvent(0, 0, "release"))
    assert not P.active(app)
    assert app.pointer_routing_state["lease"] is None
    assert app.pointer_routing_state["last_cancel"] == "context-changed"


def test_cancelled_owner_tombstone_does_not_consume_fresh_owner_release(dashboard):
    app = dashboard.app
    send(dashboard, "direct", dashboard.point("70"), "press")
    P.cancel_capture(app, reason="unheld")
    assert P.route_capture(app, P.PointerEvent(1, 1, "drag"))
    toolbar.render_bar(dashboard.views, app, app.width)
    track = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "track")
    send(dashboard, "direct", (track[0], track[1]), "press")
    send(dashboard, "direct", (track[0], track[2] - 1), "release")
    assert not P.active(app) and app.pointer_routing_state["lease"] is None


def test_proxy_uses_root_owner_and_control_validators_without_document_reads(dashboard, monkeypatch):
    app = dashboard.app
    proxy = SimpleNamespace(_pointer_owner_app=app)
    send(dashboard, "direct", dashboard.point("70"), "press")
    lease = app.pointer_routing_state["lease"]
    monkeypatch.setattr(app.store, "snapshot", lambda: pytest.fail("capture read Store"))
    assert P.active(proxy)
    assert P.route_capture(proxy, P.PointerEvent(*dashboard.point("72"), "drag"))
    assert app.pointer_routing_state["lease"] is lease
    P.cancel_capture(proxy, reason="unheld")
    assert not P.active(proxy) and app.marks == {"70", "71", "72"}


def test_all_control_adapters_join_common_protocol():
    from importlib import import_module
    for owner in P.OWNERS:
        handler = import_module("raven." + owner.name).handle_mouse
        assert hasattr(handler, "__wrapped__"), owner.name


def test_capture_registry_never_initializes_unrelated_controls():
    app = SimpleNamespace()
    assert not P.active(app)
    assert not P.route_capture(app, P.PointerEvent(3, 4, "motion"))
    assert set(vars(app)) == {"pointer_routing_state"}


def test_failed_handler_releases_capture_without_hiding_error():
    app = SimpleNamespace(job_selection_state={"capture": None, "discard_release": False})
    @P.capture_handler("job_selection")
    def failing(app, y, x, button="left", shift=False):
        app.job_selection_state["capture"] = {"temporary": True}
        raise RuntimeError("failed preview")
    with pytest.raises(RuntimeError, match="failed preview"):
        failing(app, 1, 1, button="press")
    assert not P.active(app)
    assert app.pointer_routing_state["last_cancel"] == "handler-error"
    assert app.job_selection_state["discard_release"]


def test_captured_motion_updates_cosmetic_pointer_without_activating_hovered_control(dashboard):
    app = dashboard.app
    send(dashboard, "native", dashboard.point("70"), "press")
    toolbar.render_bar(dashboard.views, app, app.width)
    menu = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "menu")
    point = menu[0], menu[1]
    send(dashboard, "native", point, "drag")
    assert app.interaction_state["pointer"] == point
    assert app.toolbar_state["menu"] is None
    assert app.pointer_routing_state["lease"].owner.name == "job_selection"


@pytest.mark.parametrize("change", ["mode", "tab", "width", "height"])
def test_toolbar_context_cancel_consumes_original_release(dashboard, change):
    app = dashboard.app
    toolbar.render_bar(dashboard.views, app, app.width)
    track = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "track")
    send(dashboard, "direct", (track[0], track[1]), "press")
    assert app.pointer_routing_state["lease"].owner.name == "toolbar"
    setattr(app, change, {"tab": "research", "mode": "confirm", "width": 120, "height": 30}[change])
    assert P.route_capture(app, P.PointerEvent(10, 10, "release"))
    assert not P.active(app) and app.pointer_routing_state["lease"] is None
    assert not app.toolbar_state["discard_release"]


def test_unheld_toolbar_tombstone_is_consumed_and_cannot_steal_fresh_row_release(dashboard):
    app = dashboard.app
    toolbar.render_bar(dashboard.views, app, app.width)
    track = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "track")
    send(dashboard, "direct", (track[0], track[1]), "press")
    P.cancel_capture(app, reason="unheld")
    assert app.toolbar_state["discard_release"]
    assert toolbar.handle_mouse(app, 10, 10, button="release")
    assert app.pointer_routing_state["lease"] is None
    send(dashboard, "direct", (track[0], track[1]), "press")
    P.cancel_capture(app, reason="unheld")
    send(dashboard, "direct", dashboard.point("70"), "press")
    send(dashboard, "direct", dashboard.point("72"), "drag")
    send(dashboard, "direct", dashboard.point("72"), "release")
    assert app.marks == {"70", "71", "72"}
    assert not app.toolbar_state["discard_release"]
    assert not P.active(app)
