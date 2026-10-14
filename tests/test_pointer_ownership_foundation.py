"""Cancelled drag owners cannot steal subsequent toolbar or graph releases."""
from collections import deque
from importlib import import_module
import curses

import pytest

from raven import screen, toolbar, refresh_rate
from raven.pointer_routing import OWNERS, RELEASE_MARKERS, PointerEvent, active as capture_active
from test_pointer_integration import dashboard, MOUSE, refresh_button


class RawWindow:
    def __init__(self):
        self.values = deque()

    def getch(self):
        return self.values.popleft() if self.values else -1

    def keypad(self, enabled):
        pass

    def timeout(self, value):
        pass


def raw_event(dashboard, x, y, button):
    suffix = "m" if button == "release" else "M"
    code = 32 if button == "drag" else 0
    raw = f"\x1b[<{code};{x + 1};{y + 1}{suffix}".encode()
    window = RawWindow()
    reader = screen._InputReader(window)
    events = []
    # Every byte arrives in its own read, including the ESC/CSI prefix.
    for byte in raw:
        window.values.append(byte)
        event = reader.read(curses)
        if event is not None:
            events.append(event)
    assert len(events) == 1 and events[0][0] == "mouse"
    screen._apply_input(dashboard.app, events[0], dashboard.app.last_hits, curses)


@pytest.mark.parametrize("owner,marker", RELEASE_MARKERS)
@pytest.mark.parametrize("route", ["native", "fragmented", "direct"])
def test_new_refresh_summary_gesture_owns_its_release_after_cancelled_capture(dashboard, monkeypatch, owner, marker, route):
    app = dashboard.app
    toolbar.render_bar(dashboard.views, app, app.width)
    state = getattr(app, owner, None)
    if state is None:
        # Optional surfaces initialize lazily. A cancelled gesture has its
        # owner's real state shape even when the surface is no longer visible.
        module = next(item.name for item in OWNERS if item.state == owner)
        state = import_module("raven." + module).initialize(app)
    state[marker] = True
    y, left, _, _, _ = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "refresh-settings")
    app.marks = {"75"}
    before = (app.tab, app.selected_id, dict(app.cursor), set(app.marks))
    initial_rate = refresh_rate.multiplier(app)
    initial_seconds = refresh_rate.cadence(app, "jobs")
    snapshot = dashboard.store.snapshot()
    def forbid(*args, **kwargs):
        pytest.fail("Pointer dispatch must not copy Store or perform collection")
    monkeypatch.setattr(dashboard.store, "snapshot", forbid)
    monkeypatch.setattr(dashboard.store, "render_snapshot", forbid)
    def send(x, y, button):
        if route == "fragmented":
            raw_event(dashboard, x, y, button)
        elif route == "native":
            flags = {"press": MOUSE.BUTTON1_PRESSED,
                     "release": MOUSE.BUTTON1_RELEASED,
                     "drag": MOUSE.REPORT_MOUSE_POSITION | MOUSE.BUTTON1_PRESSED}
            dashboard.mouse(y, x, flags[button])
        else:
            app.click(y, x, app.last_hits, button=button)
    send(left, y, "press")
    assert app.mode == "telemetry_settings" and not state[marker]
    assert not app.toolbar_state["dragging"]
    # Painting uses the admitted snapshot; opening and interacting with this
    # local preference panel must not collect another scheduler snapshot.
    dashboard.views.overlay(snapshot, app, app.width, app.height)
    faster = refresh_button(app, "queue faster")
    send(faster.rect.left, faster.rect.top, "drag")
    send(faster.rect.left, faster.rect.top, "release")
    assert not app.toolbar_state["dragging"] and not app.toolbar_state["pressed"]
    assert not capture_active(app)
    assert refresh_rate.multiplier(app) == initial_rate
    assert (app.tab, app.selected_id, app.cursor, app.marks) == before
    assert not state.get(marker, False)
    # A fresh gesture reaches the new panel, including after a cancelled owner
    # had retained its old release marker. Only this press changes cadence.
    send(faster.rect.left, faster.rect.top, "press")
    send(faster.rect.left, faster.rect.top, "release")
    assert refresh_rate.multiplier(app) > initial_rate
    assert refresh_rate.cadence(app, "jobs") < initial_seconds
    queue_rate = refresh_rate.multiplier(app)
    dashboard.views.overlay(snapshot, app, app.width, app.height)
    recording = refresh_button(app, "recordings 1")
    send(recording.rect.left, recording.rect.top, "press")
    send(recording.rect.left, recording.rect.top, "release")
    assert refresh_rate.recordings_interval(app) == 1.
    assert refresh_rate.multiplier(app) == queue_rate
    dashboard.views.overlay(snapshot, app, app.width, app.height)
    close = refresh_button(app, "close")
    send(close.rect.left, close.rect.top, "press")
    send(close.rect.left, close.rect.top, "release")
    assert app.mode == "main" and not capture_active(app)
    assert (app.tab, app.selected_id, app.cursor, app.marks) == before
    assert not state.get(marker, False)


@pytest.mark.parametrize("button", [None, [], {}, 7, True, "unknown"])
def test_unknown_pointer_input_is_inert(dashboard, button):
    app = dashboard.app
    before = (app.tab, app.mode, app.selected_id, set(app.marks))
    assert not PointerEvent(0, 0, button).valid
    app.click(0, 0, app.last_hits, button=button)
    assert (app.tab, app.mode, app.selected_id, app.marks) == before
