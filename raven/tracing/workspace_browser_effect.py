"""Explicit live-browser effects, separated from terminal drawing and readers."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading


def _open(url):
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return "Viewer is on this SSH host. Forward its loopback port to your workstation; offline HTML export is also available."
    if sys.platform != "darwin" and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return "No graphical desktop detected. Open the URL through a local browser or forward the displayed loopback port."
    executable = shutil.which("open" if sys.platform == "darwin" else "xdg-open")
    if executable is None:
        return "Open the displayed URL in your browser."
    child = subprocess.Popen(
        [executable, url],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    return (
        "Browser open requested."
        if child.poll() in (None, 0)
        else "Desktop opener failed; open the displayed URL directly."
    )


def request(app, session, context):
    from ..background_tasks import research_hub
    from ..ui.trace_ports import browser_effects

    effects = browser_effects(app)
    effects.cancel_reader(app)
    state = app.trace_panel_state
    token = state["token"]
    cancelled = threading.Event()
    state["workspace_browser_cancel"] = cancelled
    hub = research_hub(app, getattr(app, "files", None))

    def work():
        if cancelled.is_set():
            raise InterruptedError("Live viewer request was cancelled")
        return session.open_browser(open_browser=False)

    def done(result):
        current = effects.current_context(app)
        if (
            cancelled.is_set()
            or state["token"] != token
            or state.get("workspace_session") is not session
            or session.closed
            or current is None
            or current["key"] != context["key"]
        ):
            if (
                not isinstance(result, Exception)
                and state.get("workspace_session") is not session
            ):
                session.retire()
            return
        if isinstance(result, Exception):
            app.fail("Live trace viewer: " + str(result)[:500])
            return
        try:
            message = _open(result.url)
        except OSError as exc:
            message = "Browser opener failed: " + str(exc)[:200]
        app.trace_workspace_browser = {
            "url": result.url,
            "message": message,
            "context": context["key"],
        }
        app.say("Live trace viewer: " + result.url + ". " + message)

    if not hub.start_task(work, done):
        app.say(
            "The trace reader is finishing its current batch; choose Live browser again shortly."
        )
        return False
    app.say("Preparing the private live trace viewer…")
    return True
