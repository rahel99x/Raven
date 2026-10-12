"""All pointer owners protect a fragmented Escape prefix during a gesture."""
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("state,key", [("chart_interaction_state", "capture"),
    ("metric_live_state", "capture"), ("job_selection_state", "capture"),
    ("text_selection_state", "capture"), ("scrollbar_state", "capture"),
    ("pane_drag_state", "capture"), ("history_browser_state", "drag"),
    ("toolbar_state", "dragging")])
def test_active_pointer_escape_grace_tracks_every_capture_owner(state, key):
    from raven.screen import _protect_pointer_escape
    dashboard = SimpleNamespace()
    assert not _protect_pointer_escape(dashboard)
    setattr(dashboard, state, {key: {"active": True}})
    assert _protect_pointer_escape(dashboard)
    getattr(dashboard, state)[key] = None
    assert not _protect_pointer_escape(dashboard)
