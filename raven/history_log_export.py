"""Explicit, asynchronous log bundles for selected History jobs.

The modal freezes exact job IDs when opened. Discovery, directory operations,
file copying and clipboard delivery share the existing ResearchHub worker;
painting and pointer navigation use only published data.
"""
from __future__ import annotations

import copy
import hashlib
import os
import threading
from itertools import islice

from . import layout as L
from .research import clean

MAX_JOBS = 1024
MAX_ENTRIES = 4096
MAX_NAME = 255
MODES = frozenset("history_log_" + stage for stage in
                  ("menu", "picker", "mkdir", "busy", "missing", "receipt", "error"))


class _Cancellation:
    """A worker stops when either its modal or the shared reader is closed."""
    def __init__(self, event, hub):
        self.event, self.hub = event, hub

    def is_set(self):
        return self.event.is_set() or self.hub.closed

    def set(self):
        self.event.set()


def initialize(app):
    state = getattr(app, "history_log_export_state", None)
    if not isinstance(state, dict):
        state = app.history_log_export_state = {}
    elif state.get("_initialized") is True:
        return state
    for key, value in dict(token=0, jobs=(), stage="", cursor=0, top=0, scroll=0,
                           root="", cwd="", parent=None, entries=[], limited=False,
                           hits=[], paint_token=None, hover=None, pressed=None,
                           name="", name_cursor=0, name_purpose="mkdir", pending=None, callback=None,
                           cancel=None, result=None, report=None, title="", status="",
                           progress=(0, None), progress_lock=threading.Lock(),
                           error="", listing_message="", return_stage="menu").items():
        state.setdefault(key, value)
    state["_initialized"] = True
    return state


def active(app):
    return getattr(app, "mode", "main") in MODES


def command_names():
    return ["historylogs"]


def _set_stage(app, stage):
    state = initialize(app)
    state.update(stage=stage, hits=[], paint_token=None, hover=None, pressed=None,
                 cursor=0, top=0, scroll=0)
    app.mode = "history_log_" + stage


def _feedback(app, message, failure=False):
    callback = getattr(app, "fail" if failure else "say", None)
    if callable(callback):
        callback(message)


def _history_ids(app):
    records = getattr(app, "history_all_records", None)
    if records is None:
        # This fallback reads published records only; never call history_jobs,
        # whose default path takes a complete Store snapshot during input.
        records = getattr(getattr(app, "store", None), "finished", ())
    return {record.id for record in records if isinstance(getattr(record, "id", None), str)}


def selected_jobs(app):
    """Return exact selected History IDs without broadening a marked selection."""
    available = _history_ids(app)
    marks = getattr(app, "marks", set())
    if marks:
        return tuple(sorted(identifier for identifier in marks if identifier in available))
    selected = getattr(app, "selected_id", None)
    return (selected,) if selected in available else ()


def open_menu(app, job_ids=None):
    if active(app):
        return True
    if getattr(app, "tab", "") != "history" or getattr(app, "mode", "main") != "main":
        return False
    jobs = selected_jobs(app) if job_ids is None else tuple(dict.fromkeys(job_ids))
    if (not jobs or len(jobs) > MAX_JOBS or any(not isinstance(jid, str) or not jid
            or len(jid) > 128 or not jid.isprintable() for jid in jobs)):
        _feedback(app, "Select 1 to 1024 History jobs to export their logs.", failure=True)
        return False
    if any(jid not in _history_ids(app) for jid in jobs):
        _feedback(app, "The selected jobs are no longer in the published History list.", failure=True)
        return False
    state = initialize(app)
    state["token"] += 1
    state.update(jobs=jobs, pending=None, callback=None, cancel=None, report=None,
                 result=None, name="", error="", status="", root="", cwd="", parent=None,
                 entries=[], limited=False, listing_message="")
    # Menus cannot obscure or retain another live gesture or dropdown.
    toolbar = getattr(app, "toolbar_state", {})
    if isinstance(toolbar, dict):
        toolbar.update(menu=None, panel=None, focus="", dragging=False,
                       menu_hits=[], menu_token=None)
    selection = getattr(app, "job_selection_state", {})
    if isinstance(selection, dict):
        selection["capture"] = None
    _set_stage(app, "menu")
    return True


def _inside_jobs(app, y, x):
    rectangle = getattr(app, "history_jobs_rect", None)
    if rectangle is not None and callable(getattr(rectangle, "contains", None)):
        return rectangle.contains(y, x)
    return (0 <= x < getattr(app, "width", 120) and
            any(row == y and kind == "fin" for row, kind, _ in getattr(app, "last_hits", ())))


def _projects_root(app):
    """Choose a local, lexical destination root; filesystem checks run later."""
    configured = getattr(app, "cfg", {}).get("exports", {}).get("projects_root", "")
    if isinstance(configured, str) and configured:
        if configured == "~" or configured.startswith("~/"):
            home = os.environ.get("HOME", "")
            configured = os.path.join(home, configured[2:]) if configured != "~" else home
        return os.path.abspath(configured)
    files = getattr(app, "files", None) or getattr(getattr(app, "logs", None), "files", None)
    if not getattr(files, "remote", False):
        project = getattr(app, "project_state", {})
        registered = project.get("registered_root") or project.get("root", "")
        if isinstance(registered, str) and registered.startswith("/"):
            return os.path.normpath(registered)
    return os.path.join(os.environ.get("HOME", os.getcwd()), "projects")


def _hub(app):
    if getattr(app, "research", None) is None:
        from .research import ResearchHub
        files = getattr(app, "files", None) or getattr(getattr(app, "logs", None), "files", None)
        app.research = ResearchHub(app.cfg, files=files)
    return app.research


def _queue(app, title, work, complete, *, return_stage="menu"):
    state = initialize(app)
    if state.get("pending") is not None or state.get("callback") is not None:
        return False
    state["token"] += 1
    event = threading.Event()
    state.update(cancel=event, progress=(0, None), title=title, status="queued",
                 return_stage=return_stage)
    _set_stage(app, "busy")
    state["pending"] = (state["token"], work, complete, event)
    _start(app)
    return True


def _start(app):
    state = initialize(app)
    task = state.get("pending")
    if task is None or not active(app):
        return
    token, work, complete, event = task
    hub = _hub(app)
    # Never accumulate requests behind a cancelled read that is still running.
    with hub.lock:
        if hub.closed:
            state.update(pending=None, status="error", error="The background reader is closed.")
            _set_stage(app, "error")
            return
        if hub.pending or hub.future is not None and not hub.future.done():
            return

        def progress(done, total=None):
            with state["progress_lock"]:
                if token == state["token"]:
                    state["progress"] = (max(0, int(done)), total)

        def finished(result):
            if token != state["token"]:
                return
            state.update(callback=None, cancel=None, status="done")
            if not active(app):
                return
            published = isinstance(result, dict) and bool(result.get("export_path") or result.get("incomplete_path"))
            if event.is_set() and published:
                message = ("Cancellation arrived after the bundle was saved; complete files remain at the shown path."
                           if result.get("export_path") else
                           "Cancellation cleanup could not remove the incomplete folder; inspect the shown path.")
                result = dict(result, warnings=list(result.get("warnings", ())) + [message])
                complete(result)
            elif event.is_set():
                # Backend has finished its cleanup before cancellation returns.
                _set_stage(app, state["return_stage"])
                if isinstance(result, dict) and result.get("created"):
                    created = result["created"]
                    entry = {"name": os.path.basename(created), "path": created}
                    if entry not in state["entries"]:
                        state["entries"] = sorted(state["entries"] + [entry], key=lambda value: value["name"].casefold())
                    _feedback(app, "Folder created before cancellation: " + clean(created))
                else:
                    _feedback(app, "Log operation cancelled.")
            elif isinstance(result, Exception):
                state["error"] = clean(result, limit=2048)
                _set_stage(app, "error")
            else:
                complete(result)

        if hub.start_task(lambda: work(_Cancellation(event, hub), progress), finished):
            state.update(pending=None, callback=finished, status="running")


def cancel(app, *, close=False):
    """Cancel bounded worker activity; no callback is detached while copying."""
    state = initialize(app)
    event = state.get("cancel")
    if event is not None:
        event.set()
    if state.get("pending") is not None:
        state.update(pending=None, cancel=None, status="cancelled")
        if not close:
            _set_stage(app, state["return_stage"])
    elif state.get("callback") is not None and not close:
        state["status"] = "cancelling"
        state.update(hits=[], paint_token=None, pressed=None)
        return
    if close:
        state["token"] += 1
        state.update(pending=None, callback=None, cancel=None, stage="", hits=[],
                     paint_token=None, hover=None, pressed=None)
        if active(app):
            app.mode = "main"


def tick(app):
    state = initialize(app)
    if (state.get("pending") is not None or state.get("callback") is not None) and not active(app):
        cancel(app, close=True)
        return
    if state.get("pending") is not None:
        _start(app)


def _publish_listing(app, listing):
    state = initialize(app)
    if not isinstance(listing, dict) or listing.get("status") not in ("ok", "ready"):
        state["error"] = clean(listing.get("message", "Directory listing failed.") if isinstance(listing, dict) else listing)
        _set_stage(app, "error")
        return
    state.update(root=listing["root"], cwd=listing["current"], parent=listing.get("parent"),
                 entries=list(listing.get("entries", ()))[:MAX_ENTRIES],
                 limited=bool(listing.get("limited")), listing_message=clean(listing.get("message", "")))
    _set_stage(app, "picker")


def _browse(app, path=None):
    from . import log_bundle
    state = initialize(app)
    root = state["root"] or _projects_root(app)
    current = path or root
    state["root"] = root
    return _queue(app, "Read project directories", lambda event, progress:
                  log_bundle.list_directories(root, current, cancel=event),
                  lambda result: _publish_listing(app, result), return_stage="menu" if not state["cwd"] else "picker")


def _create(app):
    from . import log_bundle
    state = initialize(app)
    name = state["name"]
    if (not name or name in (".", "..") or len(name.encode("utf-8")) > MAX_NAME or not name.isprintable()
            or any(c in name for c in "/\\")):
        state["error"] = "Enter one printable folder name, without / or \\."
        return
    root, current = state["root"], state["cwd"]
    if state["name_purpose"] == "open":
        _browse(app, os.path.join(current, name))
        return
    _queue(app, "Create project directory", lambda event, progress:
           log_bundle.mkdir_directory(root, current, name, cancel=event),
           lambda result: _publish_listing(app, result), return_stage="picker")


def _request_export(app, clipboard=False, discovered=None):
    from . import log_bundle
    state = initialize(app)
    jobs, destination, root = state["jobs"], None if clipboard else state["cwd"], state["root"]
    source = getattr(app, "project_state", {})
    project = copy.deepcopy({key: source.get(key) for key in ("registered_root", "root", "binding", "logs")})
    settings = dict(getattr(app, "cfg", {}).get("logs", {}))
    cb = dict(getattr(app, "cfg", {}).get("clipboard", {}))
    files = getattr(app, "files", None) or getattr(getattr(app, "logs", None), "files", None)
    slurm = getattr(getattr(app, "sampler", None), "slurm", None) or getattr(getattr(app, "actions", None), "slurm", None)
    store, state_dir = app.store, getattr(app, "state_dir", None)

    def work(event, progress):
        report = discovered
        if report is None:
            request = log_bundle.capture_jobs(jobs, store.snapshot(),
                                             registered_root=project.get("registered_root") or project.get("root") or "",
                                             binding=project.get("binding"), project_logs=project.get("logs") or (),
                                             log_settings=settings)
            report = log_bundle.discover_logs(request, slurm=slurm, files=files, cancel=event, progress=progress)
            if report.get("missing") or report.get("status") not in ("ok", "ready", "partial"):
                return {"discovery": report}
        return log_bundle.export_logs(report, destination, root=root or None, state_dir=state_dir,
                                      clipboard=clipboard, files=files, cancel=event, progress=progress,
                                      use_osc52=bool(cb.get("osc52", True)), use_tools=bool(cb.get("tools", True)),
                                      copy_destination=cb.get("destination", "copy"))

    def completed(result):
        if not isinstance(result, dict):
            state["error"] = "The log exporter returned an invalid receipt."
            _set_stage(app, "error")
            return
        report = result.get("discovery", result)
        state.update(result=result, report=report)
        if report.get("missing"):
            state["retry_clipboard"] = clipboard
            _set_stage(app, "missing")
            if result.get("export_path"):
                _feedback(app, clean(result.get("message", "Available logs saved; review missing outputs."), limit=8192))
        elif result.get("export_path") or result.get("status") in ("ready", "ok", "partial"):
            _set_stage(app, "receipt")
            _feedback(app, clean(result.get("message", "Log bundle saved."), limit=8192))
        else:
            state["error"] = clean(report.get("message", "; ".join(report.get("warnings", ())) or "Log export failed."), limit=2048)
            _set_stage(app, "error")

    _queue(app, "Copy selected jobs' complete logs" if clipboard else "Save selected jobs' complete logs",
           work, completed, return_stage="menu" if clipboard else "picker")


def run_command(app, args):
    if not args or args[0] != "historylogs":
        return False
    if args[1:] == ["cancel"]:
        cancel(app, close=not active(app) or initialize(app)["stage"] != "busy")
    elif args[1:] in ([], ["show"], ["clipboard"], ["directory"]):
        if open_menu(app) and initialize(app)["stage"] == "menu":
            if args[1:] == ["clipboard"]:
                _request_export(app, True)
            elif args[1:] == ["directory"]:
                _browse(app)
    else:
        _feedback(app, "Use historylogs [show|clipboard|directory|cancel].", failure=True)
    return True


def _items(app):
    state, stage = initialize(app), initialize(app)["stage"]
    if stage == "menu":
        return [("Copy all logs to clipboard", ("clipboard",)), ("Copy logs to directory", ("directory",)), ("Cancel", ("close",))]
    if stage == "picker":
        items = [("..  Parent folder", ("parent",))] if state["parent"] else []
        items += [("/ " + clean(item["name"]), ("directory-path", item["path"])) for item in state["entries"]]
        return items + [("Open named folder...", ("open-name",)), ("New folder...", ("mkdir",)),
                        ("Save here", ("save",)), ("Cancel", ("close",))]
    if stage == "mkdir":
        action = "Open folder" if state["name_purpose"] == "open" else "Create folder"
        return [("Folder name: " + state["name"], ("edit-name",)), (action, ("create",)), ("Cancel", ("picker",))]
    if stage == "busy":
        return [("Cancel operation", ("cancel-work",))]
    if stage == "missing":
        if (state.get("result") or {}).get("export_path"):
            return [("View export receipt", ("receipt",)), ("Done", ("close",))]
        available = [("Export available logs", ("available",))] if (state.get("report") or {}).get("entries") else []
        return [("Retry", ("retry",))] + available + [("Back to export menu", ("menu",)), ("Cancel", ("close",))]
    if stage == "error":
        return [("Back to export menu", ("menu",)), ("Cancel", ("close",))]
    if stage == "receipt":
        return [("Export again", ("menu",)), ("Done", ("close",))]
    return []


def _activate(app, action):
    state = initialize(app)
    state.update(hits=[], paint_token=None, pressed=None)
    name = action[0]
    if name == "close":
        cancel(app, close=True)
    elif name == "clipboard":
        _request_export(app, True)
    elif name == "directory":
        _browse(app)
    elif name == "directory-path":
        _browse(app, action[1])
    elif name == "parent" and state["parent"]:
        _browse(app, state["parent"])
    elif name in ("mkdir", "open-name"):
        state.update(name="", name_cursor=0, error="", name_purpose="open" if name == "open-name" else "mkdir")
        _set_stage(app, "mkdir")
    elif name == "create":
        _create(app)
    elif name == "edit-name":
        state["cursor"] = 0
    elif name == "save":
        _request_export(app)
    elif name in ("menu", "picker", "receipt"):
        _set_stage(app, name)
    elif name == "cancel-work":
        cancel(app)
    elif name == "retry":
        _request_export(app, bool(state.get("retry_clipboard")))
    elif name == "available":
        _request_export(app, bool(state.get("retry_clipboard")), discovered=state["report"])


def paste(app, text):
    """Accept printable folder-name text without interpreting or executing it."""
    if not active(app):
        return False
    state = initialize(app)
    if state["stage"] != "mkdir" or state["cursor"] != 0:
        return True
    if not isinstance(text, str) or not text.isprintable() or any(c in text for c in "/\\"):
        state["error"] = "Paste one printable folder name, without / or \\."
        return True
    position = state["name_cursor"]
    remaining = MAX_NAME - len(state["name"].encode("utf-8"))
    inserted = ""
    for char in text:
        size = len(char.encode("utf-8"))
        if size > remaining:
            break
        inserted += char
        remaining -= size
    state["name"] = state["name"][:position] + inserted + state["name"][position:]
    state["name_cursor"] += len(inserted)
    state["error"] = "Folder name limited to 255 UTF-8 bytes." if len(text) > len(inserted) else ""
    return True


def handle_key(app, key):
    if not active(app):
        return False
    state = initialize(app)
    stage = state["stage"]
    if key in ("esc", "ctrl-c", "q") and not (stage == "mkdir" and state["cursor"] == 0 and key == "q"):
        if stage == "busy":
            cancel(app)
        elif stage == "mkdir":
            _set_stage(app, "picker")
        else:
            cancel(app, close=True)
        return True
    if stage == "mkdir" and state["cursor"] == 0:
        name, position = state["name"], state["name_cursor"]
        if key == "left":
            state["name_cursor"] = max(0, position - 1)
        elif key == "right":
            state["name_cursor"] = min(len(name), position + 1)
        elif key in ("home", "end"):
            state["name_cursor"] = 0 if key == "home" else len(name)
        elif key == "backspace":
            state["name"] = name[:max(0, position - 1)] + name[position:]
            state["name_cursor"] = max(0, position - 1)
        elif key == "delete":
            state["name"] = name[:position] + name[position + 1:]
        elif key == "enter":
            _create(app)
        elif key == "space" or len(key) == 1 and key.isprintable():
            paste(app, " " if key == "space" else key)
        elif key not in ("tab", "btab", "up", "down"):
            return True
        else:
            state["cursor"] = 1 if key in ("tab", "down") else 2
        return True
    if stage == "picker" and key in ("left", "backspace"):
        if state["parent"]:
            _browse(app, state["parent"])
        return True
    if stage in ("missing", "receipt", "error") and key in ("up", "down", "pgup", "pgdn", "home", "end"):
        count = _detail_count(app)
        delta = {"up": -1, "down": 1, "pgup": -8, "pgdn": 8, "home": -count, "end": count}[key]
        state["scroll"] = max(0, min(max(0, count - 1), state["scroll"] + delta))
        return True
    items = _items(app)
    if key in ("up", "down", "left", "right", "tab", "btab", "pgup", "pgdn", "home", "end"):
        count = len(items)
        delta = {"up": -1, "down": 1, "left": -1, "right": 1, "tab": 1, "btab": -1,
                 "pgup": -8, "pgdn": 8, "home": -count, "end": count}[key]
        state["cursor"] = ((state["cursor"] + delta) % count if key in ("tab", "btab") and count
                           else max(0, min(max(0, count - 1), state["cursor"] + delta)))
    elif key == "enter" and items:
        _activate(app, items[min(state["cursor"], len(items) - 1)][1])
    return True


def _detail_parts(app):
    state = initialize(app)
    if state["stage"] == "error":
        result = state.get("result") or {}
        rows = [state["error"] or "Log export failed."]
        if result.get("incomplete_path"):
            rows.append("Incomplete folder: " + result["incomplete_path"])
        return rows, (), result.get("warnings", ())
    report = state.get("report") or {}
    result = state.get("result") or {}
    if state["stage"] == "missing":
        rows = (["Available logs were saved: " + result["export_path"]] if result.get("export_path") else
                ["No bundle was copied. Retry, or explicitly export the available logs."])
        methods = result.get("clipboard", {}).get("methods", ())
        if methods:
            rows.append("Clipboard: " + ", ".join(methods))
        rows.append("The following expected logs are unavailable:")
        missing = report.get("missing", ())
        if isinstance(missing, dict):
            missing = [{"job_id": jid, "path": value} for jid, values in missing.items()
                       for value in (values if isinstance(values, list) else [values])]
        return rows, missing, report.get("warnings", ())
    if state["stage"] == "receipt":
        rows = [result.get("message", "Log bundle saved.")]
        for field, label in (("export_path", "Bundle"), ("clipboard_path", "Clipboard text")):
            if result.get(field):
                rows.append(label + ": " + result[field])
        if result.get("clipboard", {}).get("methods"):
            rows.append("Clipboard: " + ", ".join(result["clipboard"]["methods"]))
        return rows, (), result.get("warnings", ())
    return [], (), ()


def _detail_count(app):
    prefix, missing, warnings = _detail_parts(app)
    return len(prefix) + len(missing) + len(warnings)


def _details(app, start=0, count=256):
    """Format only visible report lines, even for thousands of missing paths."""
    prefix, missing, warnings = _detail_parts(app)
    rows = []
    begin, end = max(0, start), max(0, start) + max(0, count)
    rows.extend(prefix[begin:min(end, len(prefix))])
    missing_begin, missing_end = max(0, begin - len(prefix)), max(0, end - len(prefix))
    for item in islice(missing, missing_begin, missing_end):
        if isinstance(item, dict):
            jid = item.get("job_id", item.get("job", "?"))
            path = item.get("path") or item.get("label") or "No expected log paths were found"
            reason = item.get("reason", item.get("message", "unavailable"))
            label = clean(item.get("label", "log"), limit=24)
            filename = L.cut(os.path.basename(path), 28)
            rows.append(f"Job {jid}: {label} | {filename} | {reason} | Path: {path}")
        else:
            rows.append(clean(item, limit=4096))
    warning_begin = max(0, begin - len(prefix) - len(missing))
    warning_end = max(0, end - len(prefix) - len(missing))
    rows.extend("Warning: " + clean(value) for value in islice(warnings, warning_begin, warning_end))
    return rows


def _paint_context(app, width=None, height=None):
    state = initialize(app)
    return (state["token"], getattr(app, "mode", "main"), getattr(app, "tab", ""),
            width if width is not None else getattr(app, "width", None),
            height if height is not None else getattr(app, "height", None), state["stage"], state["cwd"])


def overlay(views, snap, app, width, height):
    state = initialize(app)
    state.update(hits=[], paint_token=None)
    if not active(app):
        return None
    stage = state["stage"]
    from . import modal_scrollbars as B
    ascii_ = views.g.ascii
    room = max(1, width - 8)
    prefix = [[(L.cut(f" {len(state['jobs'])} selected jobs: " + ", ".join(state["jobs"]), room, ascii_), "accent+bold")]]
    title = {"menu": "Export History logs", "picker": "Choose log destination", "mkdir": "New project folder",
             "busy": state["title"], "missing": "Expected logs are missing", "receipt": "Log export complete",
             "error": "Log export could not finish"}.get(stage, "History logs")
    if stage == "mkdir" and state["name_purpose"] == "open":
        title = "Open named project folder"
    if stage in ("picker", "mkdir"):
        prefix.append([(L.cut(" " + state["cwd"], room, ascii_), "cyan")])
        if state["limited"]:
            prefix.append([(" Directory list limit reached; Open named folder accesses other children.", "yellow")])
    elif stage == "busy":
        with state["progress_lock"]:
            done, total = state["progress"]
        prefix.append([(f" {state['status'].title()} | {done:,}" + (f" / {total:,}" if isinstance(total, int) else " units"), "cyan")])
        if isinstance(total, int) and total > 0:
            prefix.append(L.gradient_bar(views.g, done / total, min(32, room)))
    if stage == "mkdir" and state["error"]:
        prefix.append([(L.cut(" " + state["error"], room, ascii_), "yellow")])
    items = _items(app)
    detail_count = _detail_count(app)
    available = max(1, height - 4)
    # Keep actions usable in short terminals. Details have a separate bounded
    # viewport; no invisible line generates a hit rectangle.
    prefix = prefix[:max(0, available - min(len(items), 3))]
    rows = list(prefix)
    logical_hits = []
    cursor = max(0, min(state["cursor"], max(0, len(items) - 1)))
    state["cursor"] = cursor
    if detail_count:
        content_room = max(0, available - len(rows) - min(len(items), 3))
        scroll = min(state["scroll"], max(0, detail_count - max(1, content_room)))
        detail_context = ("history-logs-details", state["token"], stage, id(state.get("report")), id(state.get("result")), width)
        detail_target, scroll = B.window(app, "modal:history-log-report", scroll, detail_count, content_room, context=detail_context)
        state["scroll"] = detail_target
        detail_start = len(rows)
        for value in _details(app, scroll, content_room):
            rows.append([(L.cut(" " + clean(value, ascii_, limit=8192), room, ascii_), "yellow" if stage == "missing" else "")])
    page = max(1, available - len(rows))
    top = min(max(0, state["top"]), max(0, len(items) - page))
    if cursor < top:
        top = cursor
    elif cursor >= top + page:
        top = cursor - page + 1
    context = ("history-logs-actions", state["token"], stage, state["cwd"])
    logical, top = B.window(app, "modal:history-log-actions", top, len(items), page,
                            context=context, focus=cursor)
    state["top"] = logical
    action_start = len(rows)
    for index, (label, action) in enumerate(items[top:top + page], top):
        logical_hits.append((len(rows), action))
        style = "sel" if index == cursor else "accent+under" if action == state["hover"] else "text"
        shown = " [ " + clean(label, ascii_, limit=4096) + " ]"
        rows.append([(L.cut(shown, room, ascii_), style)])
    if len(rows) < available:
        hint = ((" Type folder name | Enter open | Tab buttons | Esc back" if state["name_purpose"] == "open" else
                 " Type folder name | Enter create | Tab buttons | Esc back") if stage == "mkdir" else
                " Arrows scroll | Tab buttons | Enter choose | Esc close" if detail_count else
                " Arrows / Tab choose | Enter select | Esc cancel")
        rows.append([(L.cut(hint, room, ascii_), "dim")])
    rendered = L.box(views.g, rows, width, height, clean(title, ascii_))
    for row_index, action in logical_hits:
        index = row_index + 1
        if index < len(rendered) - 1:
            y, x, row = rendered[index]
            state["hits"].append((y, x + 1, x + L.vlen(L.row_text(row)) - 1, action))
    state["paint_size"] = (width, height)
    state["paint_token"] = _paint_context(app, width, height)
    rendered = B.boxed(app, "modal:history-log-actions", rendered, start=action_start, count=len(items), page=page,
                       target=logical, painted=top, setter=lambda value: state.update(top=value), context=context,
                       header=-1 if detail_count else 0)
    if detail_count:
        rendered = B.boxed(app, "modal:history-log-report", rendered, start=detail_start, count=detail_count, page=content_room,
                           target=detail_target, painted=scroll, setter=lambda value: state.update(scroll=value),
                           context=detail_context)
    return rendered


def _fresh(app):
    state = initialize(app)
    size = state.get("paint_size")
    if not size:
        return False
    width = getattr(app, "width", size[0])
    height = getattr(app, "height", size[1])
    return state["paint_token"] == _paint_context(app, width, height)


def controls(app):
    """Return current modal buttons for the shared directional focus graph."""
    if not active(app) or not _fresh(app):
        return ()
    from .interaction import Control, Rect
    state = initialize(app)
    labels = {action: label for label, action in _items(app)}
    return tuple(Control("history-log:" + str(state["token"]) + ":" +
                         hashlib.sha256(repr(action).encode("utf-8")).hexdigest()[:16],
                         clean(labels.get(action, action[0]), limit=512),
                         Rect(y, left, y + 1, right), ("click", y, left), "history-log-export", layer=2)
                 for index, (y, left, right, action) in enumerate(state["hits"]))


def handle_mouse(app, y, x, button="left", shift=False):
    if not active(app):
        if (button == "right" and getattr(app, "mode", "main") == "main"
                and getattr(app, "tab", "") == "history" and _inside_jobs(app, y, x)):
            open_menu(app)
            return True
        return False
    state = initialize(app)
    if not _fresh(app):
        state.update(hover=None, pressed=None)
        return True
    hit = next((action for row, left, right, action in state["hits"] if row == y and left <= x < right), None)
    if button in ("motion", "drag"):
        state["hover"] = hit
    elif button in ("wheel-up", "wheel-down", "wheel_up", "wheel_down"):
        handle_key(app, "up" if button in ("wheel-up", "wheel_up") else "down")
    elif button == "press":
        state["pressed"] = (state["paint_token"], hit) if hit else None
        if hit:
            state["cursor"] = next((i for i, (_, action) in enumerate(_items(app)) if action == hit), state["cursor"])
    elif button == "release":
        pressed = state["pressed"]
        state["pressed"] = None
        if pressed and pressed == (state["paint_token"], hit) and hit:
            _activate(app, hit)
    elif button in ("left", "double") and hit:
        state["cursor"] = next((i for i, (_, action) in enumerate(_items(app)) if action == hit), state["cursor"])
        _activate(app, hit)
    return True
