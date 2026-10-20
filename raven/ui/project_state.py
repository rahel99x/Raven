"""Cached project attachment and backend identity, independent of UI actions."""

from __future__ import annotations


def initialize(app):
    if not isinstance(getattr(app, "project_state", None), dict):
        app.project_state = {}
    state = app.project_state
    defaults = {
        "root": "",
        "runs": [],
        "warnings": [],
        "run_warnings": [],
        "binding": None,
        "logs": [],
        "status": "empty",
        "summary": "Choose a project with :project PATH",
        "generation": 0,
        "busy": False,
        "run_cursor": 0,
        "run_top": 0,
        "output_cursor": 0,
        "output_top": 0,
        "filter": "",
        "filtering": False,
        "collapsed": [],
        "tree": None,
        "preview": None,
        "preview_scroll": 0,
        "restore_run_id": "",
        "notices_open": False,
        "notices_scroll": 0,
        "preview_node": None,
        "preview_pages": [(0, 0)],
        "preview_page": 0,
        "preview_columns": None,
        "preview_sort": None,
        "preview_column": 0,
        "preview_collapsed": [],
        "preview_format": None,
    }
    defaults.update(
        registered_root="",
        binding_origin="",
        auto_generation=0,
        auto_pending=False,
        auto_last=0.0,
        auto_target=None,
        auto_roots=(),
        auto_suppressed=None,
        auto_status="idle",
        auto_summary="",
        auto_backend=None,
        auto_attempt=None,
    )
    for key, value in defaults.items():
        state.setdefault(key, value)
    return state


def selected_binding(app):
    value = initialize(app).get("binding")
    return value if isinstance(value, dict) else None


def _backend_token(hub):
    if hub is None:
        return None
    from ..project_remote import identity, supported

    return (id(hub), identity(hub.files) if supported(hub.files) else id(hub.files))
