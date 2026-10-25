"""Shared form layout and field-edit gestures, without backend operations."""

from __future__ import annotations
import re
import time
from .. import layout as L
from ..control_rows import buttons, place_hits


def _wrap(text, width):
    """Keep prose words intact while still fitting long paths and JSON."""
    rows, line, used = [], "", 0
    for chunk in re.findall(r"\s+|\S+", text):
        size = L.vlen(chunk)
        if line and not chunk.isspace() and size <= width and used + size > width:
            rows.append(line.rstrip())
            line, used = "", 0
        for char in chunk:
            size = L.vlen(char)
            if line and used + size > width:
                rows.append(line.rstrip())
                line, used = "", 0
            if not line and char.isspace():
                continue
            line += char
            used += size
    return rows + ([line] if line else []) or [""]


def _pending_review(app):
    return (
        getattr(app, "mode", "main") == "confirm"
        or bool(getattr(app, "execution_state", {}).get("pending_action"))
        or bool(getattr(app, "raven_ui_state", {}).get("review"))
    )


def edit_actions(glyphs, width):
    """Visible local field actions; keeping a field never saves or launches."""
    return buttons(
        glyphs,
        max(1, width),
        (
            ("keep", "Keep field", ("key", "enter")),
            ("cancel", "Cancel edit", ("key", "esc")),
        ),
        group="field-edit",
        prefix="field-edit:",
    )


def publish_edit_actions(app, state, hits, rendered, start, frame, width, height):
    """Publish only the editing footer's actually painted, bounded controls."""
    state["control_hits"] = place_hits(
        [(row + start, kind, value) for row, kind, value in hits], rendered[1:-1]
    )
    state["edit_painted"] = (frame, width, height, state.get("editing"))


def edit_mouse(app, state, y, x, button, frame, key_handler):
    """A complete native gesture delegates to the form's existing field logic."""
    if state.get("editing") is None:
        state["edit_pressed"] = None
        return False
    if button == "release":
        # A non-editing form can enter its editor on the opening press. Its
        # release must not become an aggregate click on a newly painted footer.
        from .modal_pointer import initialize

        initialize(app)["aggregate"] = (time.monotonic() + 0.5, y, x)
    if button in ("wheel-up", "wheel-down"):
        state["edit_pressed"] = None
        return False
    expected = (
        frame,
        getattr(app, "width", None),
        getattr(app, "height", None),
        state.get("editing"),
    )
    if state.get("edit_painted") != expected:
        state["edit_pressed"] = None
        return True
    target = next(
        (
            value["action"]
            for row, _kind, value in state.get("control_hits", ())
            if value.get("group") == "field-edit"
            and row == y
            and value["left"] <= x < value["right"]
        ),
        None,
    )
    activate = False
    if button == "press":
        state["edit_pressed"] = (expected, target) if target else None
    elif button == "release":
        pressed, state["edit_pressed"] = state.get("edit_pressed"), None
        activate = target is not None and pressed == (expected, target)
    elif button in ("left", "double"):
        state["edit_pressed"] = None
        activate = target is not None
    if activate:
        key_handler(app, target[1])
    return True
