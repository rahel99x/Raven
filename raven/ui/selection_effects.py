"""Selection state transitions independent of feature handlers or navigation."""

from __future__ import annotations
from .selection_state import initialize
from .text_selection_state import clear as clear_text


def resume(app, tab=None):
    # An explicit native-row gesture owns subsequent copies. Do not leave a
    # prior rendered range (or its invalidation guard) in front of it.
    clear_text(app)
    initialize(app)["deselected"][tab or getattr(app, "tab", "")] = False


def resume_lines(app):
    """A deliberate line gesture restores its cursor without selecting a job."""
    clear_text(app)
    initialize(app)["lines_deselected"] = False


def clear_lines(app):
    """Clear text and log cursors without changing job or dialog ownership."""
    clear_text(app)
    state = initialize(app)
    state["lines_deselected"] = True
    app.sel_anchor = app.click_row = None
    app.sel_end = 0
    app.log_selection_expected = False
    logs = getattr(app, "logs", None)
    if logs is not None:
        logs.clear_selection(reset_cursor=True)
        logs.match = None
    session = getattr(app, "job_panel_state", {}).get("session")
    if session is not None and session is not logs:
        session.clear_selection(reset_cursor=True)
        session.match = None
    log_workbench = getattr(app, "log_workbench_state", None)
    if isinstance(log_workbench, dict):
        log_workbench["citation"] = None
    log_tools = getattr(app, "log_tools_state", None)
    if isinstance(log_tools, dict):
        log_tools.update(selection=None, cursor_deselected=True)
