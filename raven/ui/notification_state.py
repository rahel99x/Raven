"""Published notification selection identity and state, without alert actions."""

from __future__ import annotations


def initialize(app):
    state = getattr(app, "notification_ui_state", None)
    if not isinstance(state, dict):
        state = dict(
            severity="all",
            selected="",
            details=False,
            top=0,
            page=1,
            return_mode="main",
            revision=0,
            control_hits=[],
            painted=None,
            status_hits=[],
            status_painted=None,
            cache={},
            cache_width=None,
            document=None,
            document_key=None,
            follow=False,
            list_top=0,
            notices_key=None,
            notices=(),
        )
        app.notification_ui_state = state
    return state


def context(app):
    """Constant-size identity for stale pointer and keyboard-focus rejection."""
    state = initialize(app)
    center = getattr(app, "notifications", None)
    return (
        center.snapshot().revision if center is not None else 0,
        state["revision"],
        state["severity"],
        state["selected"],
        state["details"],
        state["top"],
    )
