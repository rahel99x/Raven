"""Rebuilt documents can catch up passive motion without reordering input."""
from collections import deque
from types import SimpleNamespace

import pytest

from raven import screen


MOUSE = SimpleNamespace(BUTTON1_CLICKED=1, BUTTON1_PRESSED=2, BUTTON1_RELEASED=4,
                       BUTTON1_DOUBLE_CLICKED=8, BUTTON3_CLICKED=16, BUTTON3_PRESSED=32,
                       BUTTON4_PRESSED=64, BUTTON5_PRESSED=128, REPORT_MOUSE_POSITION=256)


def motion(index, bits=256):
    return "mouse", (0, index, 10, 0, bits)


def fixture(monkeypatch, events):
    queued, applied, timeouts = deque(events), [], []
    app = SimpleNamespace(mode="main")
    window = SimpleNamespace(timeout=timeouts.append)
    monkeypatch.setattr(screen, "_read_input", lambda *_: queued.popleft() if queued else None)
    monkeypatch.setattr(screen, "_apply_input", lambda _, event, *_args: applied.append(event))
    return SimpleNamespace(app=app, window=window, queued=queued, applied=applied, timeouts=timeouts)


@pytest.mark.parametrize("event", [("q", None), ("resize", (52, 320)), ("paste", "research"),
                                    motion(5, 2), motion(5, 4), motion(5, 1), motion(5, 64)])
def test_first_deliberate_input_is_returned_after_only_the_latest_passive_motion(monkeypatch, event):
    latest, future = motion(2), motion(9)
    f = fixture(monkeypatch, [motion(1), latest, event, future])
    monkeypatch.setattr(screen, "POINTER_BATCH_SECONDS", 1.)
    assert screen._drain_prepaint_motion(f.app, f.window, MOUSE, []) is event
    assert f.applied == [latest]
    assert list(f.queued) == [future]


@pytest.mark.parametrize("owner,field", [("chart_interaction_state", "capture"), ("metric_live_state", "capture"),
    ("text_selection_state", "capture"), ("job_selection_state", "capture"), ("scrollbar_state", "capture"),
    ("pane_drag_state", "capture"), ("toolbar_state", "dragging"), ("history_browser_state", "drag")])
def test_capture_owner_prevents_prepaint_reads(monkeypatch, owner, field):
    event = motion(1)
    f = fixture(monkeypatch, [event])
    setattr(f.app, owner, {field: object()})
    assert screen._drain_prepaint_motion(f.app, f.window, MOUSE, []) is None
    assert list(f.queued) == [event] and not f.timeouts and not f.applied


def test_existing_pending_input_and_partial_decoder_keep_their_order_and_reports_are_bounded(monkeypatch):
    f = fixture(monkeypatch, [motion(index) for index in range(10)])
    key = ("down", None)
    assert screen._drain_prepaint_motion(f.app, f.window, MOUSE, [], key) is key
    assert len(f.queued) == 10 and not f.timeouts
    monkeypatch.setitem(screen._INPUT_READERS, id(f.window), SimpleNamespace(pending=True))
    assert screen._drain_prepaint_motion(f.app, f.window, MOUSE, []) is None
    assert len(f.queued) == 10 and not f.timeouts
    monkeypatch.delitem(screen._INPUT_READERS, id(f.window))
    monkeypatch.setattr(screen, "POINTER_BATCH_LIMIT", 3)
    monkeypatch.setattr(screen, "POINTER_BATCH_SECONDS", 1.)
    assert screen._drain_prepaint_motion(f.app, f.window, MOUSE, []) is None
    assert f.applied == [motion(2)] and len(f.queued) == 7


def test_held_motion_remains_a_deliberate_event_even_without_a_pressed_bit(monkeypatch):
    held = ("mouse", screen.MouseReport(motion(5)[1], held=True))
    f = fixture(monkeypatch, [motion(1), held, motion(9)])
    monkeypatch.setattr(screen, "POINTER_BATCH_SECONDS", 1.)
    assert screen._drain_prepaint_motion(f.app, f.window, MOUSE, []) is held
    assert f.applied == [motion(1)] and list(f.queued) == [motion(9)]


def test_non_main_mode_retains_its_input_without_reading(monkeypatch):
    event = ("paste", "research")
    f = fixture(monkeypatch, [event])
    f.app.mode = "command"
    assert screen._drain_prepaint_motion(f.app, f.window, MOUSE, []) is None
    assert list(f.queued) == [event] and not f.timeouts and not f.applied
