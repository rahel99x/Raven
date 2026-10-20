"""Splitter state and focus transitions independent of pointer handlers."""


def initialize(app):
    state = getattr(app, "pane_drag_state", None)
    if not isinstance(state, dict):
        state = {
            "dividers": {},
            "capture": None,
            "focus": None,
            "focus_context": None,
            "size": None,
            "discard_release": False,
        }
        app.pane_drag_state = state
    state.setdefault("discard_release", False)
    return state


def blur(app):
    """A content gesture leaves keyboard resizing; an active drag keeps capture."""
    state = initialize(app)
    if state["capture"] is None:
        state["focus"], state["focus_context"] = None, None
