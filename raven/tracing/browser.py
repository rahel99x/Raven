"""Private, offline browser views of an already verified retained trace snapshot.

Exporting never uploads data or starts a recorder. Only an explicit UI action may
ask the local desktop to open the completed file; remote sessions retain a path.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import uuid


from ..tracing.document import MAX_OUTPUT_BYTES as MAX_OUTPUT_BYTES

from ..tracing.document import _STYLE as _STYLE

from ..tracing.document import _SCRIPT as _SCRIPT


from ..tracing.document import _browser_model as _browser_model


from ..tracing.document import render as render


from ..tracing.document import export as export


def open_browser(path, *, environ=None):
    """Request a local graphical opener; a request is not proof of a visible tab."""
    environ = os.environ if environ is None else environ
    if environ.get("SSH_CONNECTION") or environ.get("SSH_TTY"):
        return "Saved on this SSH host; copy the HTML file to your workstation and open it there."
    if sys.platform != "darwin" and not (
        environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")
    ):
        return "No graphical desktop detected; copy the HTML file to your workstation and open it there."
    command = shutil.which("open" if sys.platform == "darwin" else "xdg-open")
    if command is None:
        return "No desktop browser opener is installed; open the saved HTML file in your browser."
    try:
        process = subprocess.Popen(
            [command, Path(path).absolute().as_uri()],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        code = process.poll()
        if code not in (None, 0):
            return "The desktop browser opener failed; open the saved HTML file in your browser."
    except OSError as exc:
        return (
            "Browser request failed: "
            + str(exc)
            + "; open the saved HTML file in your browser."
        )
    return (
        "Browser open requested; if no tab appears, open the saved HTML file directly."
    )


def cancel(app):
    state = getattr(app, "trace_browser_state", None)
    if isinstance(state, dict) and state.get("cancel") is not None:
        state["cancel"].set()


def request(app, snapshot, context):
    """Explicit UI action; bounded disk/browser work uses the existing worker."""
    from ..background_tasks import research_hub as _worker
    from ..ui.trace_ports import browser_effects

    effects = browser_effects(app)
    state = getattr(app, "trace_browser_state", None)
    if isinstance(state, dict) and state.get("pending"):
        app.say("A trace browser export is already in progress.")
        return False
    hub = _worker(app, getattr(app, "files", None))
    # Prioritize this explicit action over a background trace read, while keeping
    # the already published snapshot pinned to its selected attempt.
    effects.cancel_reader(app)
    event = threading.Event()
    state = {
        "cancel": event,
        "pending": True,
        "path": "",
        "message": "",
        "context": context["key"],
    }
    app.trace_browser_state = state

    def work():
        if event.is_set():
            raise InterruptedError("Trace browser export cancelled")
        from ..export import export_dir

        root = Path(export_dir(None))
        output = root / ("trace-" + uuid.uuid4().hex + ".html")
        # The UI exports an already retained bounded window, without a new
        # source read. HTML representation may exceed the CLI's smaller read
        # prefix, so it uses the explicit visualization output cap.
        result = export(snapshot, output, max_bytes=MAX_OUTPUT_BYTES, cancel=event)
        return result

    def done(result):
        state["pending"] = False
        current = effects.current_context(app)
        if event.is_set() or current is None or current["key"] != context["key"]:
            if not isinstance(result, Exception):
                state.update(
                    path=result["path"],
                    message="Saved for the previous selection; browser was not opened.",
                )
                app.say(
                    "Trace HTML for job "
                    + str(context.get("job_id", "?"))
                    + ": "
                    + result["path"]
                    + ". "
                    + state["message"]
                )
            return
        if isinstance(result, Exception):
            from ..research_text import clean

            state["message"] = "Trace browser export failed: " + clean(result, 500)
            app.fail(state["message"])
        else:
            # The completion callback owns the UI selection. Desktop opening
            # only starts a detached opener (no wait), after this exact-context
            # check; the worker cannot race a selection change into opening.
            state.update(path=result["path"], message=open_browser(result["path"]))
            app.say("Trace HTML: " + result["path"] + ". " + state["message"])

    if not hub.start_task(work, done):
        state["pending"] = False
        app.say("The background reader is busy; choose View in browser again shortly.")
        return False
    app.say("Preparing a private offline trace timeline…")
    return True
