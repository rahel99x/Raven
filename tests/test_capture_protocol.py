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
        flags = {
            "press": MOUSE.BUTTON1_PRESSED,
            "release": MOUSE.BUTTON1_RELEASED,
            "drag": MOUSE.REPORT_MOUSE_POSITION | MOUSE.BUTTON1_PRESSED,
        }
        dashboard.mouse(y, x, flags[button])


@pytest.mark.parametrize("route", ["native", "direct"])
def test_selection_owner_keeps_release_across_toolbar_and_new_menu_button_owns_next(
    dashboard, route
):
    app = dashboard.app
    send(dashboard, route, dashboard.point("70"), "press")
    lease = app.pointer_routing_state["lease"]
    assert lease.owner.name == "job_selection"
    send(dashboard, route, dashboard.point("72"), "drag")
    selected = set(app.marks)
    toolbar.render_bar(dashboard.views, app, app.width)
    menu = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "menu")
    point = menu[0], menu[1]
    send(dashboard, route, point, "release")
    assert app.marks == selected and not app.toolbar_state["dragging"]
    assert app.mode == "main" and app.toolbar_state["menu"] is None
    assert app.pointer_routing_state["lease"] is None
    send(dashboard, route, point, "press")
    assert app.pointer_routing_state["lease"].owner.name == "toolbar"
    assert app.mode == "main" and app.toolbar_state["menu"] is not None
    assert app.pointer_routing_state["lease"].generation > lease.generation
    send(dashboard, route, dashboard.point("74"), "release")
    assert app.marks == selected and not P.active(app)


@pytest.mark.parametrize("route", ["native", "direct"])
def test_lost_release_then_fresh_menu_button_commits_marks_and_revokes_old_owner(
    dashboard, route
):
    app = dashboard.app
    send(dashboard, route, dashboard.point("70"), "press")
    send(dashboard, route, dashboard.point("72"), "drag")
    selected = set(app.marks)
    toolbar.render_bar(dashboard.views, app, app.width)
    menu = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "menu")
    send(dashboard, route, (menu[0], menu[1]), "press")
    assert app.pointer_routing_state["lease"].owner.name == "toolbar"
    assert app.mode == "main" and app.toolbar_state["menu"] is not None
    assert not job_selection.active(app)
    assert not any(
        getattr(app, name, {}).get(marker) for name, marker in P.RELEASE_MARKERS
    )
    send(dashboard, route, (menu[0], menu[2] - 1), "release")
    assert app.marks == selected and app.pointer_routing_state["lease"] is None


@pytest.mark.parametrize("change", ["tab", "mode", "width", "height"])
def test_context_token_prevents_old_owner_from_dispatching_in_new_document(
    dashboard, change, monkeypatch
):
    app = dashboard.app
    send(dashboard, "direct", dashboard.point("70"), "press")
    setattr(
        app,
        change,
        {"tab": "research", "mode": "confirm", "width": 120, "height": 30}[change],
    )
    monkeypatch.setattr(
        job_selection,
        "handle_mouse",
        lambda *args, **kwargs: pytest.fail("stale owner dispatched"),
    )
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
    menu = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "menu")
    send(dashboard, "direct", (menu[0], menu[1]), "press")
    send(dashboard, "direct", (menu[0], menu[2] - 1), "release")
    assert not P.active(app) and app.pointer_routing_state["lease"] is None


def test_proxy_uses_root_owner_and_control_validators_without_document_reads(
    dashboard, monkeypatch
):
    app = dashboard.app
    proxy = SimpleNamespace(_pointer_owner_app=app)
    send(dashboard, "direct", dashboard.point("70"), "press")
    lease = app.pointer_routing_state["lease"]
    monkeypatch.setattr(
        app.store, "snapshot", lambda: pytest.fail("capture read a full Store snapshot")
    )
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
    app = SimpleNamespace(
        job_selection_state={"capture": None, "discard_release": False}
    )

    @P.capture_handler("job_selection")
    def failing(app, y, x, button="left", shift=False):
        app.job_selection_state["capture"] = {"temporary": True}
        raise RuntimeError("failed preview")

    with pytest.raises(RuntimeError, match="failed preview"):
        failing(app, 1, 1, button="press")
    assert not P.active(app)
    assert app.pointer_routing_state["last_cancel"] == "handler-error"
    assert app.job_selection_state["discard_release"]


def test_captured_motion_updates_cosmetic_pointer_without_activating_hovered_control(
    dashboard,
):
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
    menu = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "menu")
    send(dashboard, "direct", (menu[0], menu[1]), "press")
    assert app.pointer_routing_state["lease"].owner.name == "toolbar"
    setattr(
        app,
        change,
        {"tab": "research", "mode": "confirm", "width": 120, "height": 30}[change],
    )
    assert P.route_capture(app, P.PointerEvent(10, 10, "release"))
    assert not P.active(app) and app.pointer_routing_state["lease"] is None
    assert not app.toolbar_state["discard_release"]


def test_unheld_toolbar_tombstone_is_consumed_and_cannot_steal_fresh_row_release(
    dashboard,
):
    app = dashboard.app
    toolbar.render_bar(dashboard.views, app, app.width)
    menu = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "menu")
    send(dashboard, "direct", (menu[0], menu[1]), "press")
    P.cancel_capture(app, reason="unheld")
    assert app.toolbar_state["discard_release"]
    assert toolbar.handle_mouse(app, 10, 10, button="release")
    assert app.pointer_routing_state["lease"] is None
    app.handle("esc")
    assert app.mode == "main"
    dashboard.draw()
    send(dashboard, "direct", (menu[0], menu[1]), "press")
    P.cancel_capture(app, reason="unheld")
    app.handle("esc")
    assert app.mode == "main"
    dashboard.draw()
    # Closing the dropdown does not fabricate the missing release. The
    # next real row gesture must revoke that toolbar tombstone itself.
    assert app.toolbar_state["discard_release"]
    send(dashboard, "direct", dashboard.point("70"), "press")
    send(dashboard, "direct", dashboard.point("72"), "drag")
    send(dashboard, "direct", dashboard.point("72"), "release")
    assert app.marks == {"70", "71", "72"}
    assert not app.toolbar_state["discard_release"]
    assert not P.active(app)


def test_custom_capture_handler_is_scoped_to_its_app_and_cannot_poison_new_sessions(
    dashboard,
):
    canonical = P.binding_for(job_selection.handle_mouse)
    original_bindings = dashboard.app.pointer_bindings
    temporary = SimpleNamespace(
        job_selection_state={"capture": None, "discard_release": False}
    )

    @P.capture_handler("job_selection")
    def failing(app, y, x, button="left", shift=False):
        app.job_selection_state["capture"] = {"temporary": True}
        raise RuntimeError("isolated preview failure")

    with pytest.raises(RuntimeError, match="isolated preview failure"):
        failing(temporary, 1, 1, button="press")
    assert temporary.pointer_bindings["job_selection"].mouse is failing
    assert dashboard.app.pointer_bindings is original_bindings
    assert original_bindings["job_selection"] is canonical
    fresh = type(dashboard.app)(
        dashboard.store,
        None,
        None,
        dashboard.app.cfg,
        dashboard.app.user,
        interactive=False,
    )
    try:
        assert fresh.pointer_bindings is not original_bindings
        assert fresh.pointer_bindings["job_selection"] is canonical
        with pytest.raises(TypeError):
            fresh.pointer_bindings["job_selection"] = P.binding_for(failing)
    finally:
        if fresh.research:
            fresh.research.close()
    assert not P.active(temporary)
    assert temporary.job_selection_state["discard_release"]


def test_finite_pointer_composition_rejects_missing_duplicate_and_invalid_handlers(
    dashboard,
):
    bindings = tuple(dashboard.app.pointer_bindings.values())
    with pytest.raises(RuntimeError, match="Unbound pointer owners"):
        P.bind(SimpleNamespace(), bindings[:-1])
    with pytest.raises(ValueError, match="Duplicate pointer owner"):
        P.bind(SimpleNamespace(), bindings + bindings[:1])
    with pytest.raises(TypeError, match="capture capability"):
        P.binding_for(lambda *args: None)


def test_composed_gestures_reuse_the_same_binding_map_without_full_store_snapshots(
    dashboard, monkeypatch
):
    app = dashboard.app
    bindings = app.pointer_bindings
    monkeypatch.setattr(
        app.store, "snapshot", lambda: pytest.fail("capture read a full Store snapshot")
    )
    send(dashboard, "direct", dashboard.point("70"), "press")
    send(dashboard, "direct", dashboard.point("72"), "drag")
    send(dashboard, "direct", dashboard.point("72"), "release")
    assert app.pointer_bindings is bindings
    assert app.marks == {"70", "71", "72"}
    assert app.pointer_routing_state["lease"] is None
