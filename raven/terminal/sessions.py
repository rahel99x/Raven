"""Explicit terminal session actions below pane and command presentation."""

from __future__ import annotations
from itertools import islice
import os
import socket
from .session_state import (
    initialize,
    _bump,
    MAX_SESSIONS,
    MAX_SELECTION_BYTES,
    MAX_REPORT_LINES,
)


def _say(app, text, failure=False):
    fn = getattr(app, "fail" if failure else "say", None)
    if callable(fn):
        fn(str(text))


def _focus(app, value):
    state = initialize(app)
    value = bool(value)
    if state.keyboard_focus != value:
        state.keyboard_focus = value
        state.focus_generation += 1
        state.prefix = False
        if state.service is not None and state.active:
            try:
                state.service.send(state.active, {"type": "focus", "focused": value})
            except (OSError, ValueError, RuntimeError):
                pass
        _bump(state)
    if value:
        from ..startup import dismiss

        dismiss(app)
        from ..ui.workspace_state import initialize as layout_state

        layout_state(app).focus = "details"
        panel = getattr(app, "job_panel_state", None)
        if isinstance(panel, dict):
            panel["focus"] = ""


def blur(app):
    state = initialize(app)
    _focus(app, False)
    if state.capture:
        state.capture = None
        state.discard_release = True
    if state.preview:
        state.preview = None
        state.focus_generation += 1


def _service(app):
    state = initialize(app)
    if state.service is None:
        from .service import TerminalService

        state.service = TerminalService()
    return state.service


def _activate_session(app, sid):
    state = initialize(app)
    if state.active != sid:
        _focus(app, False)
        state.active = sid
        state.focus_generation += 1
        state.copy_mode, state.follow, state.top = False, True, 0
        state.selected = state.selection_anchor = state.selection_end = None
        state.selection_source = ()
        if state.capture:
            state.discard_release = True
        state.capture = None
        state.dialog = state.preview = state.report = state.clipboard_preview = None
        state.search_matches = []
    state.unread.pop(sid, None)
    state.modes[getattr(app, "tab", "jobs")] = "terminal"
    _focus(app, True)
    _bump(state)


def open_session(
    app, *, argv=None, cwd=None, label=None, kind="local", metadata=None, **spec
):
    """Explicit action only: create a shell/session with immutable launch context."""
    state = initialize(app)
    registered = []
    try:
        service = _service(app)
        settings = getattr(app, "cfg", {}).get("terminal", {})
        if len(service.sessions) >= max(
            1, min(MAX_SESSIONS, int(settings.get("max_sessions", MAX_SESSIONS)))
        ):
            raise ValueError(
                "Close a terminal session before opening another (limit 8)"
            )
        rows = max(1, state.body_rect.height if state.body_rect else 20)
        cols = max(1, state.body_rect.width if state.body_rect else 80)
        if argv is None:
            shell = str(settings.get("shell") or os.environ.get("SHELL") or "/bin/bash")
            argv = [shell] + (["-l"] if settings.get("login") else []) + ["-i"]
            metadata = {
                **(metadata or {}),
                "shell": shell,
                "host": socket.gethostname(),
            }
        options = dict(
            rows=rows,
            cols=cols,
            history_lines=max(1, min(10000, int(settings.get("history_lines", 2000)))),
            label=label or ("local:" + socket.gethostname()),
            kind=kind,
            metadata=metadata or {},
        )
        if argv is not None:
            options["argv"] = argv
        if cwd is not None:
            options["cwd"] = cwd
        options.update(spec)
        raven = getattr(app, "cfg", {}).get("raven", {})
        if raven.get("managed"):
            budget = (
                raven.get("resources", {})
                .get("light", {})
                .get("terminal_scrollback_bytes", 2 * 1024 * 1024)
            )
            # Enforce after launch options: reconnect/review specs cannot raise
            # the resolved Light policy's per-session retention ceiling.
            options["history_bytes"] = min(
                max(0, int(budget)), max(0, int(options.get("history_bytes", budget)))
            )
        try:
            from .evidence import register_session

            def env_factory(sid):
                registered.append(sid)
                return register_session(
                    app,
                    sid,
                    host=(metadata or {}).get("host", socket.gethostname()),
                    cwd=cwd or "",
                    metadata=metadata,
                )

            options["env_factory"] = env_factory
        except ImportError:
            pass
        sid = service.open(**options)
        if not isinstance(sid, str) or not sid:
            raise RuntimeError("Terminal service did not return a session identity")
        state.launches[sid] = dict(
            argv=argv, cwd=cwd, label=label, kind=kind, metadata=dict(metadata or {})
        )
        _activate_session(app, sid)
        state.error = ""
        return sid
    except (ImportError, OSError, ValueError, RuntimeError, TypeError) as exc:
        for registered_sid in registered:
            _unregister_bridge(app, registered_sid)
        state.error = str(exc)[:1000]
        _say(app, "Terminal: " + state.error, True)
        state.modes[getattr(app, "tab", "jobs")] = "terminal"
        _bump(state)
        return None


def prepare_input(app, text, label="Prepared command", *, helper_kind="generated"):
    state = initialize(app)
    if (
        not isinstance(text, str)
        or len(text.encode("utf-8")) > MAX_SELECTION_BYTES
        or "\x00" in text
    ):
        _say(app, "Prepared command is invalid or too large", True)
        return False
    if not state.active:
        _say(app, "Open a terminal session before preparing input", True)
        return False
    state.preview = {
        "text": text,
        "label": str(label)[:100],
        "session": state.active,
        "top": 0,
        "helper_kind": helper_kind,
    }
    state.dialog = state.report = None
    state.modes[getattr(app, "tab", "jobs")] = "terminal"
    state.focus_generation += 1
    _focus(app, True)
    _bump(state)
    return True


def show_report(app, lines, title="Terminal report"):
    state = initialize(app)
    bounded = []
    size = 0
    for line in islice(lines, MAX_REPORT_LINES + 1):
        text = str(line)[:4096]
        size += len(text.encode("utf-8"))
        if len(bounded) >= MAX_REPORT_LINES or size > MAX_SELECTION_BYTES:
            bounded.append(
                "[Report truncated at its line/byte budget; narrow the requested evidence.]"
            )
            break
        bounded.append(text)
    state.report = {"lines": tuple(bounded), "title": str(title)[:100], "top": 0}
    state.dialog = state.preview = state.clipboard_preview = None
    state.menu = False
    state.modes[getattr(app, "tab", "jobs")] = "terminal"
    state.focus_generation += 1
    _focus(app, True)
    _bump(state)


def cancel_prepared(app):
    state = initialize(app)
    state.preview = None
    state.focus_generation += 1
    _bump(state)


def _unregister_bridge(app, sid):
    try:
        from .evidence import unregister_session

        unregister_session(app, sid)
        return
    except ImportError:
        pass
    state = getattr(app, "terminal_fusion_state", {})
    bridge = state.get("bridge") if isinstance(state, dict) else None
    if bridge is not None:
        bridge.unregister(sid)
