"""Normalize native pointer gestures for explicitly registered legacy dialogs.

Modern forms own their own gestures. These older owners accept an aggregate
``left`` only; the adapter waits for a matching release over the same published
semantic control before forwarding that click. It retains at most one capture
and one short aggregate-click guard, with no renderer or backend dependencies.
"""

from __future__ import annotations

import time

from .interaction_state import _current, hit_token

# This is a migration boundary, not handler probing: new modal owners must
# implement native gestures themselves rather than silently enter this list.
LEGACY_MODES = {
    "execution_ui": frozenset(("execution",)),
    "navigation_ui": frozenset(("workspace_picker",)),
    "navigation_tools": frozenset(
        (
            "jump_picker",
            "locations_picker",
            "settings_editor",
            "bindings_editor",
            "value_peek",
            "field_explanation",
        )
    ),
    "analysis_ui": frozenset(("analysis",)),
    "table_ui": frozenset(("columns",)),
    "table_tools": frozenset(("table_tools",)),
    "project_ui": frozenset(("project_runs", "project_outputs", "project_preview")),
    "activity_ui": frozenset(("activity", "exports", "export_preview")),
    "session_tools": frozenset(
        ("session_inbox", "session_alerts", "terminal_diagnostics", "terminal_probe")
    ),
    "log_tools": frozenset(("log_tools_page", "log_tools_results", "log_tools_marks")),
    "workspace_layout": frozenset(("layout",)),
}

_STATE_FIELDS = {
    "navigation_ui": ("navigation_state", ("query", "picker_top")),
    "navigation_tools": (
        "navigation_tools_state",
        ("query", "top", "editing", "test", "field", "value", "index_at"),
    ),
    "execution_ui": ("execution_state", ("view", "running", "detail", "control_token")),
    "analysis_ui": ("analysis_state", ("modal", "job", "chart_job", "scroll")),
    "table_ui": ("table_state", ("tab", "menu_painted")),
    "table_tools": (
        "table_tools_state",
        ("modal", "tab", "edit", "top", "menu_painted"),
    ),
    "project_ui": (
        "project_state",
        (
            "root",
            "generation",
            "run_top",
            "output_top",
            "preview_scroll",
            "filter",
            "control_token",
        ),
    ),
    "activity_ui": ("activity_ui_state", ("control_token",)),
    "session_tools": (
        "session_tools_state",
        ("query", "inbox_filter", "mouse_mode", "control_token"),
    ),
    "workspace_layout": ("workspace_layout_ui_state", ("token",)),
    "log_tools": (
        "log_tools_state",
        (
            "page_source",
            "generation",
            "page_top",
            "result_top",
            "mark_top",
            "control_token",
        ),
    ),
}


def initialize(app):
    state = getattr(app, "modal_pointer_state", None)
    if not isinstance(state, dict):
        state = app.modal_pointer_state = {"capture": None, "aggregate": None}
    return state


def continuation(app, button):
    """An old gesture retains ownership even after its modal closes/changes."""
    state = initialize(app)
    return bool(
        state["capture"] is not None
        and button in ("motion", "drag", "release", "left")
        or state["aggregate"] is not None
        and button == "left"
    )


def consume_aggregate(app, y, x, button):
    """Absorb a release's duplicate click before global/sidebar dispatch.

    This guard-only hook never changes pending capture ownership. A new press
    starts an intentional gesture and clears any preceding duplicate guard.
    """
    state = initialize(app)
    guard = state["aggregate"]
    if button == "press":
        state["aggregate"] = None
        return False
    if guard is None:
        return False
    deadline, row, column = guard
    if time.monotonic() > deadline:
        state["aggregate"] = None
        return False
    if button == "left":
        state["aggregate"] = None
        return (y, x) == (row, column)
    return False


def _context(app, frame):
    if frame is None:
        return None
    name, mode = frame.owner.name, getattr(app, "mode", "main")
    if mode not in LEGACY_MODES.get(name, ()):
        return None
    attribute, fields = _STATE_FIELDS.get(name, ("", ()))
    state = getattr(app, attribute, {})
    state = state if isinstance(state, dict) else {}
    semantic = hit_token(tuple(state.get(field) for field in fields))
    if semantic is None:
        return None
    return (
        name,
        mode,
        frame.identity,
        getattr(app, "width", None),
        getattr(app, "height", None),
        semantic,
    )


def _control(app, y, x):
    graph = _current(app)
    if graph is None:
        return None
    target = graph.at(y, x)
    if (
        target is None
        or target.layer < 2
        or target.group.startswith(("toolbar", "sidebar"))
    ):
        return None
    action = hit_token((target.action,))
    return (
        (target.id, target.rect, action, target.button, target.enabled, target.reason)
        if action is not None
        else None
    )


def _dispatch(app, frame, y, x, shift, target):
    if not target[4]:
        # Disabled controls still explain themselves after a complete, current
        # gesture. Never delegate one to an owner's action handler.
        if target[5] and callable(getattr(app, "say", None)):
            app.say(target[5])
        return
    callback = frame.owner.mouse
    if callback is None:
        return
    mode = getattr(app, "mode", "main")
    callback(app, y, x, button="left", shift=shift)
    # Navigational choices should open with a mouse click. Editable settings
    # rows only focus a value; their visible action buttons apply/cancel it.
    if (
        getattr(app, "mode", "main") == mode
        and mode in ("workspace_picker", "jump_picker", "locations_picker")
        and not target[3]
        and frame.owner.key is not None
    ):
        frame.owner.key(app, "enter")


def route(app, frame, y, x, *, button="left", shift=False, current=True):
    """Consume a legacy modal event; return False for an unregistered owner.

    Call for owned continuations before the usual modal early return, then
    after foreground scrollbar/chart handlers for fresh events. ``frame`` may
    be None, allowing a just-closed modal to absorb its duplicate aggregate.
    ``current`` is the controller's supplied-hit-map validation.
    """
    state = initialize(app)
    if consume_aggregate(app, y, x, button):
        return True
    context = _context(app, frame)
    captured = state["capture"]
    if button == "press":
        state.update(capture=None, aggregate=None)

    if captured is not None and button in ("motion", "drag", "release", "left"):
        if button in ("motion", "drag"):
            if context != captured[0]:
                state["capture"] = (None, None, captured[2])
            elif getattr(app, "mode", "") == "terminal_probe" and captured[1] is None:
                frame.owner.mouse(app, y, x, button=button, shift=shift)
            return True
        state["capture"] = None
        expected, target, pressed_shift = captured
        actual = _control(app, y, x) if current and context == expected else None
        if button == "release":
            state["aggregate"] = (time.monotonic() + 0.5, y, x)
        if target is not None and target == actual and shift == pressed_shift:
            _dispatch(app, frame, y, x, shift, target)
        elif (
            target is None
            and context == expected
            and getattr(app, "mode", "") == "terminal_probe"
        ):
            frame.owner.mouse(app, y, x, button=button, shift=shift)
        return True

    if context is None:
        return False
    if button in ("press", "left"):
        target = _control(app, y, x) if current else None
        if button == "press":
            # A blank press still owns its release: dragging into a button
            # must never activate an action the user did not press.
            state["capture"] = (context, target, shift)
            if target is None and getattr(app, "mode", "") == "terminal_probe":
                frame.owner.mouse(app, y, x, button=button, shift=shift)
        elif target is not None:
            _dispatch(app, frame, y, x, shift, target)
        return True
    if button in ("wheel-up", "wheel-down"):
        state["capture"] = None
        if (
            getattr(app, "mode", "") == "terminal_probe"
            and frame.owner.mouse is not None
        ):
            frame.owner.mouse(app, y, x, button=button, shift=shift)
        elif frame.owner.key is not None:
            frame.owner.key(app, "up" if button == "wheel-up" else "down")
        return True
    if button == "right":
        state["capture"] = None
        if frame.owner.mouse is not None:
            frame.owner.mouse(app, y, x, button=button, shift=shift)
    elif getattr(app, "mode", "") == "terminal_probe" and frame.owner.mouse is not None:
        frame.owner.mouse(app, y, x, button=button, shift=shift)
    # Orphan releases and passive reports never activate an underlying row.
    return True
