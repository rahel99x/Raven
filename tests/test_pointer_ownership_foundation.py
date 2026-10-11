"""Cancelled drag owners cannot steal subsequent toolbar or graph releases."""
from collections import deque
import curses

import pytest

from tower import screen, toolbar, refresh_rate
from tower.pointer_routing import RELEASE_MARKERS, PointerEvent
from test_pointer_integration import dashboard, MOUSE


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
def test_new_toolbar_slider_press_owns_its_release_after_cancelled_capture(dashboard, monkeypatch, owner, marker, route):
    app = dashboard.app
    toolbar.render_bar(dashboard.views, app, app.width)
    state = getattr(app, owner, None)
    if state is None:
        # All native owners normally initialize with the App; the test also
        # covers an optional owner's unpopulated state without starting work.
        state = {}
        setattr(app, owner, state)
    state[marker] = True
    tracks = [hit for hit in app.toolbar_state["hits"] if hit[3] == "track"]
    y, left = tracks[0][:2]
    right = tracks[-1][2] - 1
    before = (app.tab, app.mode, app.selected_id, set(app.marks))
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
    assert app.toolbar_state["dragging"]
    send(right, y + 3, "drag")
    send(right, y + 3, "release")
    assert not app.toolbar_state["dragging"] and not app.toolbar_state["pressed"]
    assert refresh_rate.multiplier(app) == 50
    assert (app.tab, app.mode, app.selected_id, app.marks) == before
    assert not state[marker]


@pytest.mark.parametrize("button", [None, [], {}, 7, True, "unknown"])
def test_unknown_pointer_input_is_inert(dashboard, button):
    app = dashboard.app
    before = (app.tab, app.mode, app.selected_id, set(app.marks))
    assert not PointerEvent(0, 0, button).valid
    app.click(0, 0, app.last_hits, button=button)
    assert (app.tab, app.mode, app.selected_id, app.marks) == before
