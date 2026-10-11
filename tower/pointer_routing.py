"""Shared pointer ownership rules for native input and direct UI dispatch.

Handlers keep their painted-identity checks. This small common layer makes the
highest-priority captures and fresh-gesture release reset identical in both
routes, without replacing the established keyboard or byte decoder.
"""
from dataclasses import dataclass

BUTTONS = frozenset(("press", "left", "right", "motion", "drag", "release", "wheel-up", "wheel-down"))
RELEASE_MARKERS = (("scrollbar_state", "discard_release"),
                   ("column_resize_state", "discard_release"),
                   ("group_queue_drag_state", "discard_release"),
                   ("job_group_drag_state", "discard_release"),
                   ("job_selection_state", "discard_release"),
                   ("pane_drag_state", "discard_release"),
                   ("history_browser_state", "discard_release"),
                   ("text_selection_state", "discard_release"),
                   ("chart_interaction_state", "cancelled_release"),
                   ("metric_live_state", "cancelled_release"))


@dataclass(frozen=True)
class PointerEvent:
    y: int
    x: int
    button: str
    shift: bool = False
    alt: bool = False

    @property
    def valid(self):
        return type(self.y) is int and type(self.x) is int and type(self.button) is str and self.button in BUTTONS

    @property
    def fresh(self):
        return self.button in ("press", "left")


def begin_gesture(app):
    """An old cancelled release cannot consume a newer control's release.

This must run even when the toolbar consumes the press before App.click. It is
idempotent because a native event can also reach that direct dispatch route.
    """
    for name, marker in RELEASE_MARKERS:
        state = getattr(app, name, None)
        if isinstance(state, dict):
            state[marker] = False


def route_capture(app, event):
    """Offer an event to existing high-priority owners exactly in this order."""
    from . import column_resize, group_queue_drag, job_group_drag
    if not event.valid:
        return True
    for module, state_name in ((column_resize, "column_resize_state"),
                               (group_queue_drag, "group_queue_drag_state")):
        state = getattr(app, state_name, {}) or {}
        if module.active(app) or state.get("discard_release") and not event.fresh:
            options = {"alt": event.alt} if module is group_queue_drag else {}
            if module.handle_mouse(app, event.y, event.x, button=event.button, shift=event.shift, **options):
                return True
    if job_group_drag.active(app) or job_group_drag.pending(app) and not event.fresh:
        return job_group_drag.handle_mouse(app, event.y, event.x, button=event.button,
                                          shift=event.shift, alt=event.alt)
    return False
