"""Connect existing observations, verify them, and preview without submitting work."""
from __future__ import annotations

import copy
import os

from . import connect_data as C, layout as L
from .control_rows import place_hits
from .log_text import display_text
from .setup_ui import _wrap

LABELS = (("cpu-memory", "CPU / memory"), ("gpu", "GPU"),
          ("scientific", "Scientific metrics"), ("tracing", "Tracing"))


def initialize(app):
    if not isinstance(getattr(app, "connect_data_state", None), dict):
        app.connect_data_state = dict(path="", metric="loss", page="connect", language="python",
            binding=None, declared=False, result=None, busy="", message="", editing=None,
            cursor=0, top=0, visible=1, revision=0, generation=0, return_mode="main",
            painted=None, control_hits=[])
    return app.connect_data_state


def command_names():
    return ["connectdata"]


def _invalidate(state):
    state["revision"] += 1
    state.update(painted=None, control_hits=[])


def _files(app):
    return getattr(app, "files", None) or getattr(getattr(app, "research", None), "files", None)


def _scope(app):
    binding = getattr(app, "project_state", {}).get("binding") or {}
    return (id(_files(app)), id(getattr(app, "raven_resolved", None)),
            tuple(binding.get(key) for key in ("project_root", "run_id", "job_id", "attempt", "remote_identity")),
            repr(binding.get("launch", {})), getattr(app, "selected_id", None))


def _frame(app):
    state = initialize(app)
    return (app.mode, getattr(app, "width", 0), getattr(app, "height", 0),
            _scope(app), state["revision"], state["top"])


def _queue(app, label, work, completed):
    state = initialize(app)
    if state["busy"]:
        state["message"] = "Wait for the current check to finish."
        _invalidate(state)
        return
    hub = getattr(app, "research", None)
    if hub is None:
        state["message"] = "Background file reading is unavailable in this session."
        return
    generation, scope = state["generation"], _scope(app)
    state["busy"] = label
    _invalidate(state)

    def perform():
        if generation != state["generation"] or scope != _scope(app):
            return None
        return work()

    def publish(value):
        if generation != state["generation"]:
            return
        state["busy"] = ""
        if scope != _scope(app):
            state.update(result=None, message="The selected run or connection changed. Reopen Connect data to check it.")
        elif isinstance(value, Exception):
            state.update(result=None, message=str(value))
        elif value is not None:
            completed(value)
        _invalidate(state)

    if not hub.start_task(perform, publish):
        state.update(busy="", message="The background reader is busy. Choose Validate sample again when it finishes.")
        _invalidate(state)


def open_connect(app, path=None):
    if (getattr(app, "mode", "main") == "confirm" or
            getattr(app, "execution_state", {}).get("pending_action") or
            getattr(app, "raven_ui_state", {}).get("review")):
        app.fail("Close the pending job or allocation review before connecting data.")
        return
    state = initialize(app)
    binding = copy.deepcopy(getattr(app, "project_state", {}).get("binding"))
    prior_mode = getattr(app, "mode", "main")
    if prior_mode != "connect_data":
        state["return_mode"] = "main" if prior_mode in ("palette", "toolbar_menu") else prior_mode
        from . import terminal_ui
        if terminal_ui.focused(app):
            terminal_ui.blur(app)
        from .pointer_routing import begin_gesture
        begin_gesture(app)
    chosen = path if path is not None else (binding or {}).get("metrics_file", "")
    if path is None and not chosen and not binding:
        chosen = getattr(getattr(app, "research", None), "settings", {}).get("metrics_file", "")
    state.update(path=chosen, binding=binding, declared=bool(binding and chosen == binding.get("metrics_file")),
                 result=None, page="connect", busy="", message="", editing=None, cursor=0, top=0,
                 generation=state["generation"]+1, source_scope=_scope(app))
    app.mode = "connect_data"
    _invalidate(state)
    if chosen or binding:
        _validate(app)
    else:
        state["result"] = C.inspect()


def _close(app):
    state = initialize(app)
    state.update(generation=state["generation"]+1, busy="", editing=None)
    app.mode = state["return_mode"] if state["return_mode"] != "connect_data" else "main"
    _invalidate(state)


def _validate(app, *, attach=False):
    state = initialize(app)
    if state.get("source_scope") != _scope(app):
        state.update(result=None, message="The selected run or connection changed. Close and reopen Connect data.")
        _invalidate(state)
        return
    files, binding, path, declared = _files(app), copy.deepcopy(state["binding"]), state["path"], state["declared"]
    if path and not getattr(files, "remote", False):
        path = os.path.abspath(os.path.expanduser(path))
    elif path and not os.path.isabs(path):
        state["message"] = "Use an absolute path on the connected SSH host. Local paths are never substituted."
        _invalidate(state)
        return
    if attach and not ((state["result"] or {}).get("metrics") or {}).get("usable"):
        state["message"] = "Validate at least one actual metric before opening its chart."
        _invalidate(state)
        return

    def done(value):
        state.update(result=value, path=path, message="")
        if attach:
            if not (value.get("metrics") or {}).get("usable"):
                state["message"] = "The source no longer has usable measurements. Review its current error below."
                return
            if not declared:
                from .ui.research_binding import detach_manual_source
                detach_manual_source(app)
                app.research.configure(metrics_file=path)
            _close(app)
            app.mode = "main"
            app.enter_tab("research")
            app.research_view = "experiment"
            if callable(getattr(app, "say", None)):
                app.say("Opened this run's verified metrics." if declared else "Attached manual metrics; this file is not assigned to a run.")

    _queue(app, "Rechecking before attachment" if attach else "Validating recordings",
           lambda: C.inspect(path, files=files, binding=binding, declared=declared), done)


def _activate(app, action):
    state = initialize(app)
    if action == "close":
        _close(app)
        return
    if state["busy"]:
        state["message"] = "Wait for the current check, or close this window."
        _invalidate(state)
        return
    if action == "validate":
        _validate(app)
    elif action == "attach":
        _validate(app, attach=True)
    elif action in ("connect", "snippet", "preview"):
        state.update(page=action, cursor=0, top=0, editing=None)
    elif action.startswith("edit:"):
        field = action.split(":", 1)[1]
        if field in ("path", "metric"):
            state["editing"] = field, state[field]
    elif action == "language":
        languages = ("python", "phase", "reduce", "bash")
        state["language"] = languages[(languages.index(state["language"])+1) % len(languages)]
    elif action == "copy":
        from . import clipboard
        text = C.snippet(state["language"], state["metric"])
        destination, options = getattr(app, "state_dir", None), clipboard.options(app)
        _queue(app, "Copying snippet", lambda: clipboard.copy(text, destination, **options),
               lambda result: state.update(message=str(result)))
    elif action == "run-source":
        state.update(path=(state["binding"] or {}).get("metrics_file", ""), declared=True, result=None)
        _validate(app)
    elif action == "runs":
        _close(app)
        from .project_ui import run_command
        run_command(app, ["runs"])
    _invalidate(state)


def run_command(app, args):
    if not args or args[0] != "connectdata":
        return False
    try:
        if len(args) == 1:
            open_connect(app)
        elif len(args) == 3 and args[1] == "action" and getattr(app, "mode", "") == "connect_data":
            _activate(app, args[2])
        elif len(args) == 2:
            open_connect(app, args[1])
        else:
            raise ValueError("Use connectdata [METRICS_JSONL_PATH].")
    except (ValueError, OSError) as exc:
        initialize(app)["message"] = str(exc)
        _invalidate(initialize(app))
    return True


def _document(app, glyphs=None, width=64):
    state = initialize(app)
    rows = [("Close", "close")]
    if state["busy"]:
        rows.append((state["busy"] + "…", None))
    if state["message"]:
        rows.append((state["message"], None))
    if state["page"] != "connect":
        rows.append(("Back to Connect data", "connect"))
    if state["page"] == "snippet":
        rows += [("Example: " + state["language"].title() + " (change)", "language"),
                 ("Metric name: " + state["metric"], "edit:metric"), ("Copy complete snippet", "copy"),
                 ("Decorate the function that returns your measured value, or call report_measurement(actual_value). Phase and Reduce examples show timing and explicit collective reporting. Bash keeps the manual helper. Raven launch supplies the client and exact attempt environment, even without tracing.", None),
                 ("The client writes this attempt's metrics.jsonl asynchronously and is inactive outside Raven unless configured. reduce= is collective: every rank must call in the same order. If your launch declares an external metrics file, keep its existing producer instead. Generation and Copy never execute code.", None)]
        try:
            rows += [("| " + line, None) for line in C.snippet(state["language"], state["metric"]).splitlines()]
        except ValueError as exc:
            rows.append((str(exc), None))
    elif state["page"] == "preview":
        rows.append(("Preview of actual observations (sample order; spacing is not elapsed time)", None))
        if glyphs:
            rows += [(line, None) for line in C.preview_lines((state["result"] or {}).get("metrics"), glyphs, max(1, width-2))]
        rows += [("Open chart in Research", "attach"), ("Validate sample again", "validate")]
    else:
        binding = state["binding"] or {}
        rows += [("Selected run: " + (binding.get("run_id") or "none") + (" / job " + str(binding["job_id"]) if binding.get("job_id") else ""), None),
                 ("Read files on: " + ("connected SSH host" if getattr(_files(app), "remote", False) else "this host"), None),
                 ("Choose an exact project run", "runs"), ("Metrics file: " + (state["path"] or "[choose a JSONL file]"), "edit:path"),
                 ("Validate sample", "validate"), ("Generate Bash / Python snippet", "snippet")]
        if binding.get("metrics_file"):
            rows.append(("Use selected run's declared metrics", "run-source"))
        if not state["declared"] and state["path"]:
            rows.append(("Manual file preview: its identity is not inferred from its name. Opening this chart detaches the current run context; it does not modify run.json.", None))
        result = state["result"]
        if result:
            for key, label in LABELS:
                entry = result["readiness"][key]
                rows += [(f"{label}: {entry['status']} | {entry['samples']} inspected samples", None),
                         (entry["reason"], None)]
                if entry.get("path"):
                    rows.append(("Source: " + entry["path"], None))
            metrics = result.get("metrics")
            if metrics:
                if metrics["truncated"]:
                    rows.append(("Bounded preview: at most 256 KiB read and 128 points retained per metric. This is not a full-file validation.", None))
                rows += [(line, None) for line in metrics["guidance"]]
                if metrics["usable"]:
                    rows += [("Preview chart", "preview"), ("Open chart in Research", "attach")]
        rows += [("CPU/memory and GPU are independent retained recordings. Choose them in Launch job for future jobs; past observations cannot be reconstructed. GPU recording still requires assigned devices and a supported driver.", None),
                 ("Automatic Python tracing: launch with raven launch SCRIPT --trace; no script hook is required. Python 3.12+ supports monitoring; Python 3.10/3.11 requires explicit legacy-profile selection. raven_exec is an optional override. Isolated interpreters and replaced PYTHONPATH can prevent attachment; see docs/guides/stack-tracing.md for coverage and gaps.", None)]
    if state["editing"]:
        rows.insert(1, ("Editing " + state["editing"][0] + ": " + state["editing"][1] + "_", None))
    return rows


def handle_key(app, key):
    if getattr(app, "mode", "") != "connect_data":
        return False
    state = initialize(app)
    if state["editing"]:
        field, value = state["editing"]
        if key == "esc":
            state["editing"] = None
        elif key == "enter":
            if field == "metric":
                try:
                    C.metric_name(value)
                except ValueError as exc:
                    state["message"] = str(exc)
                    _invalidate(state)
                    return True
            state[field], state["editing"] = value, None
            if field == "path":
                state.update(result=None, declared=bool(state["binding"] and value == state["binding"].get("metrics_file")))
        elif key in ("backspace", "delete"):
            state["editing"] = field, value[:-1]
        elif key in ("ctrl-u", "ctrl-a"):
            state["editing"] = field, ""
        elif key == "space" or len(key) == 1 and key.isprintable():
            state["editing"] = field, (value + (" " if key == "space" else key))[:4096]
        _invalidate(state)
        return True
    choices = [action for _, action in _document(app) if action]
    state["cursor"] = max(0, min(state["cursor"], len(choices)-1))
    if key in ("esc", "q"):
        _close(app)
    elif key in ("up", "down", "left", "right", "tab", "btab"):
        state["cursor"] = (state["cursor"] + (-1 if key in ("up", "left", "btab") else 1)) % len(choices)
        state["follow_cursor"] = True
    elif key in ("pgup", "pgdn", "home", "end"):
        state["top"] = max(0, {"pgup": state["top"]-state["visible"], "pgdn": state["top"]+state["visible"], "home": 0, "end": 10**6}[key])
        state["follow_cursor"] = False
    elif key in ("enter", "space"):
        _activate(app, choices[state["cursor"]])
    elif key == ":":
        return False
    _invalidate(state)
    return True


def paste(app, text):
    state = initialize(app)
    if getattr(app, "mode", "") != "connect_data" or not state["editing"]:
        return False
    field, value = state["editing"]
    if not isinstance(text, str) or any(not char.isprintable() for char in text) or len(value)+len(text) > 4096:
        state["message"] = "Paste one printable line of at most 4096 characters."
    else:
        state["editing"] = field, value + text
    _invalidate(state)
    return True


def handle_mouse(app, y, x, button="left", shift=False):
    if getattr(app, "mode", "") != "connect_data":
        return False
    state = initialize(app)
    if button in ("wheel-up", "wheel-down"):
        state["top"] = max(0, state["top"] + (-3 if button == "wheel-up" else 3))
        state["follow_cursor"] = False
        _invalidate(state)
    elif button in ("left", "press") and state["painted"] == _frame(app) and not state["editing"]:
        for row, _, control in state["control_hits"]:
            if row == y and control["left"] <= x < control["right"]:
                run_command(app, control["action"][1].split())
                break
    return True


def overlay(views, snap, app, width, height):
    if getattr(app, "mode", "") != "connect_data":
        return None
    state = initialize(app)
    state.update(control_hits=[], painted=None)
    inner, document, hits, choice, selected_row = max(1, width-6), [], [], 0, 0
    for text, action in _document(app, views.g, inner):
        text = display_text(str(text))
        if views.g.ascii:
            text = text.encode("ascii", "replace").decode()
        if action:
            selected = choice == state["cursor"]
            if selected:
                selected_row = len(document)
            label = L.cut(("> " if selected else "  ") + text, inner, views.g.ascii)
            hits.append((len(document), "control", {"id": "connect-data:" + str(choice), "label": text,
                         "left": 0, "right": L.vlen(label), "action": ("command", "connectdata action " + action), "group": "connect_data"}))
            document.append([(label, "sel+bold" if selected else "cyan+bg:surface")])
            choice += 1
        else:
            document.extend([(line, "text")] for line in _wrap(text, inner))
    capacity = max(0, height-5)
    state["visible"] = max(1, capacity)
    if state.pop("follow_cursor", False):
        if selected_row < state["top"]:
            state["top"] = selected_row
        elif selected_row >= state["top"] + capacity:
            state["top"] = selected_row-capacity+1
    state["top"] = max(0, min(state["top"], max(0, len(document)-capacity)))
    rows = document[state["top"]:state["top"]+capacity]
    if height >= 5:
        footer = "Enter: keep | Esc: cancel | Ctrl-U: clear" if state["editing"] else "Tab: choose | Enter: open | PgUp/PgDn: scroll | Esc: close"
        rows.append([(L.cut(footer, inner, views.g.ascii), "muted")])
    result = L.box(views.g, rows, width, height, "Connect data", min_width=20)
    state["control_hits"] = place_hits(hits, result[1:-2] if height >= 5 else result[1:-1], offset=state["top"])
    state["painted"] = _frame(app)
    return result
