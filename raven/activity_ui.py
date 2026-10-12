"""Durable-in-session notices and bounded, honestly measured task progress."""
from __future__ import annotations

from collections import deque
import hashlib
import math
import os
import stat
import threading
import time

from . import layout as L
from .research import clean


class Activity:
    def __init__(self):
        self.notices = deque(maxlen=256)
        self.lock = threading.Lock()
        self.task = None
        self.cursor = 0
        self.expanded = False
        self.query = ""
        self.filtering = False
        self.exports = []
        self.export_cursor = 0
        self.export_selected = ""
        self.mouse_rows = {}
        self.mouse_mode = ""
        self.export_query = ""
        self.export_preview = None
        self.export_generation = 0
        self.persistent = False

    def post(self, text, level="info", path="", job="", task=""):
        with self.lock:
            now = time.time()
            self.notices.append({"text": clean(text, limit=8192), "level": level if level in ("info", "success", "warning", "error") else "info",
                                 "path": clean(path), "time": now, "job": clean(job, limit=256), "task": clean(task, limit=256)})
            if isinstance(path, str) and 0 < len(path) <= 4096 and path.isprintable() and os.path.isabs(path):
                identity = hashlib.sha256(os.fsencode(path)).hexdigest()[:16]
                previous = next((entry for entry in self.exports if entry["id"] == identity), {})
                entry = {"id": identity, "path": path, "label": previous.get("label") or os.path.basename(path),
                         "time": now, "job": clean(job, limit=256), "task": clean(task, limit=256)}
                self.exports = [entry] + [item for item in self.exports if item["id"] != identity][:127]

    def start(self, label, source=""):
        with self.lock:
            self.task = {"label": clean(label), "source": clean(source), "start": time.monotonic(),
                         "done": 0, "total": None, "cancel": threading.Event(), "status": "running"}
            return self.task

    def progress(self, task, done, total=None):
        with self.lock:
            if self.task is task:
                task["done"], task["total"] = max(0, int(done)), total

    def finish(self, task, status):
        with self.lock:
            if self.task is task:
                task["status"] = status

    def snapshot(self):
        with self.lock:
            return [dict(item) for item in self.notices], dict(self.task) if self.task else None


def initialize(app):
    app.activity = Activity()


def restore(app, data):
    if not isinstance(data, dict):
        return
    activity = app.activity
    activity.persistent = data.get("persistent") is True
    for item in data.get("exports", [])[:128] if isinstance(data.get("exports"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        path = item["path"]
        if not os.path.isabs(path) or len(path) > 4096 or not path.isprintable():
            continue
        entry = {"id": hashlib.sha256(os.fsencode(path)).hexdigest()[:16], "path": path}
        for key in ("label", "job", "task"):
            value = item.get(key, "")
            entry[key] = clean(value, limit=256) if isinstance(value, str) else ""
        value = item.get("time", 0)
        entry["time"] = value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 253402300799 else 0
        if not any(existing["id"] == entry["id"] for existing in activity.exports):
            activity.exports.append(entry)
    if activity.persistent:
        for item in data.get("notices", [])[-128:] if isinstance(data.get("notices"), list) else []:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                now = item.get("time", 0)
                activity.notices.append({"text": clean(item["text"], limit=8192),
                    "level": item.get("level") if item.get("level") in ("info", "success", "warning", "error") else "info",
                    "path": clean(item.get("path", "")), "job": clean(item.get("job", "")), "task": clean(item.get("task", "")),
                    "time": now if isinstance(now, (int, float)) and not isinstance(now, bool) and math.isfinite(now) and 0 <= now <= 253402300799 else 0})


def save(app):
    activity = app.activity
    with activity.lock:
        return {"exports": [dict(item) for item in activity.exports], "persistent": activity.persistent,
                "notices": [dict(item) for item in list(activity.notices)[-128:]] if activity.persistent else []}


def command_names():
    return ["activity", "notifications", "task", "exports"]


def _matches(item, query):
    fields = {"level", "job", "task"}
    for token in query.casefold().split():
        key, sep, value = token.partition("=")
        if sep and key in fields:
            if value not in str(item.get(key, "")).casefold():
                return False
        elif token not in " ".join(str(item.get(key, "")) for key in ("text", "path", "label", "job", "task")).casefold():
            return False
    return True


def _ordered(app):
    notices, _ = app.activity.snapshot()
    return [item for item in reversed(notices) if _matches(item, app.activity.query)]


def export_items(app):
    with app.activity.lock:
        return [dict(item) for item in app.activity.exports if _matches(item, app.activity.export_query)]


def _selected_export(app, entries):
    activity = app.activity
    anchor = next((index for index, item in enumerate(entries) if item["id"] == activity.export_selected), None)
    index = anchor if anchor is not None else max(0, min(activity.export_cursor, max(0, len(entries) - 1)))
    activity.export_cursor = index
    activity.export_selected = entries[index]["id"] if entries else ""
    return entries[index] if entries else None


def _preview_export(app, item):
    from .research import ResearchHub
    from .log_text import display_text
    activity = app.activity
    if app.research is None:
        app.research = ResearchHub(app.cfg, app.logs.files)
    # A completed tail read can still await publication between frames.
    # Publish it without waiting before admitting the explicit preview.
    app.research.poll_task()
    if app.research.pending:
        app.fail("A background operation is running. Try the preview again when it finishes.")
        return
    activity.export_generation += 1
    generation = activity.export_generation
    path = item["path"]
    preview = {"path": path, "label": item["label"], "lines": ["Loading preview..."], "scroll": 0}
    activity.export_preview = preview
    app.mode = "export_preview"
    def read():
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("The export is not a regular file.")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                raw = source.read(65537)
            after = os.stat(path, follow_symlinks=False)
            current = os.fstat(descriptor)
            before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if before != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or before != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns):
                raise ValueError("The export changed during the preview.")
            lines = [display_text(line) for line in raw[:65536].decode("utf-8", "replace").splitlines()[:256]]
            if len(raw) > 65536 or len(raw[:65536].splitlines()) > 256:
                lines.append("Preview limit reached. The original export contains more data.")
            return lines or ["Empty export."]
        finally:
            os.close(descriptor)
    def ready(value):
        if generation != activity.export_generation or activity.export_preview is not preview:
            return
        preview["lines"] = [str(value)] if isinstance(value, Exception) else value
    if not app.research.start_task(read, ready):
        activity.export_generation += 1
        activity.export_preview = None
        app.mode = "exports"
        app.fail("The export preview could not start.")


def run_command(app, args):
    if not args or args[0] not in command_names():
        return False
    activity = app.activity
    if args[0] == "exports":
        values = args[1:]
        if not values or values == ["show"]:
            app.mode, activity.export_cursor, activity.export_preview, activity.export_selected = "exports", 0, None, ""
        elif values[0] == "filter":
            activity.export_query, activity.export_cursor, activity.export_selected = " ".join(values[1:])[:256], 0, ""
            app.mode = "exports"
        elif values == ["clear"]:
            with activity.lock:
                activity.exports.clear()
            app.save()
            app.say("Export records cleared. Export files remain available.")
        elif len(values) >= 2 and values[0] in ("label", "forget", "preview", "copy"):
            entries = export_items(app)
            item = next((item for item in entries if item["id"] == values[1]), None)
            if item is None and values[1].isdigit() and 1 <= int(values[1]) <= len(entries):
                item = entries[int(values[1]) - 1]
            if item is None:
                app.fail("Choose an export ID or an index from the current export list.")
            elif values[0] in ("preview", "copy", "forget") and len(values) != 2:
                app.fail("Choose one export ID or index for this operation.")
            elif values[0] == "preview":
                _preview_export(app, item)
            elif values[0] == "copy":
                _copy_path(app, item["path"])
            elif values[0] == "label" and len(values) >= 3:
                with activity.lock:
                    for entry in activity.exports:
                        if entry["id"] == item["id"]:
                            entry["label"] = clean(" ".join(values[2:]), limit=256)
                app.save()
                app.say("Export label saved.")
            elif values[0] == "forget":
                with activity.lock:
                    activity.exports = [entry for entry in activity.exports if entry["id"] != item["id"]]
                app.save()
                app.say("Export record removed. The file remains available.")
            else:
                app.fail("Use exports label INDEX NAME.")
        else:
            app.fail("Use exports [show|filter TEXT|preview INDEX|copy INDEX|label INDEX NAME|forget INDEX|clear].")
    elif args[0] in ("activity", "notifications") and args[1:] and args[1] in ("filter", "level", "job", "task", "persistent"):
        option, values = args[1], args[2:]
        if option == "persistent" and values in (["on"], ["off"]):
            activity.persistent = values == ["on"]
            app.save()
        elif option == "filter":
            activity.query = " ".join(values)[:256]
        elif option in ("level", "job", "task") and len(values) == 1:
            activity.query = option + "=" + values[0]
        else:
            app.fail("Use activity filter TEXT or level|job|task VALUE or persistent on|off.")
            return True
        app.mode, activity.cursor = "activity", 0
    elif args[0] == "task" and args[1:] == ["cancel"]:
        _, task = app.activity.snapshot()
        if task and task["status"] == "running":
            task["cancel"].set()
            app.say("Cancellation requested; waiting for the next bounded chunk")
        else:
            app.fail("No cancellable export or copy operation is running")
    elif args[1:] == ["clear"]:
        with app.activity.lock:
            app.activity.notices.clear()
        app.save()
        app.say("Activity cleared")
    elif args[1:] and args[1:] != ["show"]:
        app.fail("Usage: activity [show|clear]; task [cancel]")
    else:
        app.mode = "activity"
        app.activity.cursor = 0
    return True


def _copy_path(app, value):
    from . import clipboard
    app.say(clipboard.copy(value, app.state_dir, **clipboard.options(app)))


def handle_key(app, key):
    activity = app.activity
    if app.mode == "export_preview":
        preview = activity.export_preview or {"lines": [], "scroll": 0}
        if key in ("esc", "q"):
            activity.export_generation += 1
            app.mode = "exports"
        elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
            n = len(preview["lines"])
            step = {"up": -1, "down": 1, "pgup": -10, "pgdn": 10, "home": -n, "end": n}[key]
            preview["scroll"] = max(0, min(max(0, n - 1), preview["scroll"] + step))
        elif key == "y":
            _copy_path(app, preview.get("path", ""))
        return True
    if app.mode == "exports":
        if activity.filtering:
            if key in ("enter", "esc"):
                activity.filtering = False
            elif key == "backspace":
                activity.export_query = activity.export_query[:-1]
            elif key == "space" or len(key) == 1 and key.isprintable():
                activity.export_query = (activity.export_query + (" " if key == "space" else key))[:256]
            activity.export_cursor = 0
            return True
        entries = export_items(app)
        _selected_export(app, entries)
        if key in ("esc", "q"):
            app.mode = "main"
        elif key == "/":
            activity.filtering = True
        elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
            n = len(entries)
            step = {"up": -1, "down": 1, "pgup": -8, "pgdn": 8, "home": -n, "end": n}[key]
            activity.export_cursor = max(0, min(max(0, n - 1), activity.export_cursor + step))
            activity.export_selected = entries[activity.export_cursor]["id"] if entries else ""
        elif entries:
            item = entries[min(activity.export_cursor, len(entries) - 1)]
            if key == "enter":
                _preview_export(app, item)
            elif key == "y":
                _copy_path(app, item["path"])
            elif key == "d":
                run_command(app, ["exports", "forget", item["id"]])
        return True
    if key == "ctrl-a" and app.mode == "main":
        app.mode = "activity"
        return True
    if app.mode != "activity":
        return False
    if activity.filtering:
        if key in ("enter", "esc"):
            activity.filtering = False
        elif key == "backspace":
            activity.query = activity.query[:-1]
        elif key == "space" or len(key) == 1 and key.isprintable():
            activity.query = (activity.query + (" " if key == "space" else key))[:256]
        activity.cursor = 0
        return True
    if key in ("esc", "q"):
        app.mode = "main"
    elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
        notices = _ordered(app)
        n = len(notices)
        step = {"up": -1, "down": 1, "pgup": -8, "pgdn": 8, "home": -n, "end": n}[key]
        app.activity.cursor = max(0, min(max(0, n - 1), app.activity.cursor + step))
    elif key == "enter":
        app.activity.expanded = not app.activity.expanded
    elif key == "/":
        activity.filtering = True
    elif key == "f":
        activity.query, activity.cursor = "", 0
    elif key == "e":
        app.mode, activity.export_cursor = "exports", 0
    elif key == "c":
        run_command(app, ["task", "cancel"])
    elif key == "y":
        notices = _ordered(app)
        if notices:
            from . import clipboard
            notice = notices[min(app.activity.cursor, len(notices) - 1)]
            app.say(clipboard.copy(notice["path"] or notice["text"], app.state_dir,
                                   **clipboard.options(app)))
    return True


def overlay(views, snap, app, width, height):
    from . import modal_scrollbars as B
    activity = app.activity
    activity.mouse_rows = {}
    activity.mouse_mode = app.mode
    if app.mode == "export_preview":
        preview = activity.export_preview or {"lines": [], "scroll": 0, "label": ""}
        page = max(1, height - 7)
        context = ("export_preview", activity.export_generation, preview.get("path"), width)
        logical, painted = B.window(app, "modal:export-preview", preview["scroll"], len(preview["lines"]), page, context=context)
        preview["scroll"] = logical
        rows = [[(" " + clean(preview["label"], views.g.ascii), "accent+bold")], [(" " + clean(preview.get("path", ""), views.g.ascii), "dim")]]
        rows += [[(L.cut(clean(line, views.g.ascii), max(1, width - 8), views.g.ascii), "")] for line in preview["lines"][painted:painted + page]]
        rows.append([(" Preview: at most 64 KiB / 256 lines | arrows/pages scroll | y copy path | Esc exports", "dim")])
        rendered = L.box(views.g, rows, width, height, "Export preview")
        return B.boxed(app, "modal:export-preview", rendered, start=2, count=len(preview["lines"]), page=page,
                       target=logical, painted=painted, setter=lambda value: preview.update(scroll=value), context=context)
    if app.mode == "exports":
        entries = export_items(app)
        _selected_export(app, entries)
        logical_hits = {}
        rows = [[(f" {len(entries)} export records | filter: {clean(activity.export_query, views.g.ascii) or 'none'}", "accent")]]
        cursor = min(activity.export_cursor, max(0, len(entries) - 1))
        page = max(1, height - 7)
        start = max(0, cursor - page // 2)
        context = ("exports", activity.export_query)
        logical, start = B.window(app, "modal:exports", start, len(entries), page, context=context, focus=cursor)
        for index, item in enumerate(entries[start:start + page], start):
            logical_hits[len(rows)] = item["id"]
            rows.append([(L.cut(clean(f" {index + 1:3} {item['label']}  {item['path']}", views.g.ascii), max(1, width - 8), views.g.ascii), "sel" if index == cursor else "")])
        if not entries:
            rows.append([(" No export records match. Copy or export a file to register its path.", "dim")])
        rows.append([(" / filter | arrows/pages browse | Enter preview | y copy path | d forget record | Esc back", "dim")])
        rendered = L.box(views.g, rows, width, height, "Exports")
        for row_index, identity in logical_hits.items():
            if row_index + 1 < len(rendered) - 1:
                y, x, row = rendered[row_index + 1]
                activity.mouse_rows[y] = (identity, x + 1, x + L.vlen(L.row_text(row)) - 1)
        return B.boxed(app, "modal:exports", rendered, start=1, count=len(entries), page=page,
                       target=logical, painted=start, setter=lambda value: None, context=context)
    if app.mode != "activity":
        return None
    notices, task = app.activity.snapshot()
    rows = [[(" Notifications and background work", "accent+bold")]]
    if task:
        elapsed = max(0, time.monotonic() - task["start"])
        rows.append([(clean(f" {task['status'].upper()}  {task['label']}  {elapsed:.1f}s", views.g.ascii), "cyan")])
        if task["total"] is not None:
            rows.append([(f" {task['done']:,}/{task['total']:,} bytes ", "dim")] +
                        L.gradient_bar(views.g, task["done"] / max(1, task["total"]), max(1, min(32, width - 10))))
        else:
            rows.append([(" Progress is not reported for this operation", "dim")])
    elif getattr(app, "research", None) and app.research.pending:
        rows.append([(" Background reader busy; progress unavailable", "cyan")])
    ordered = _ordered(app)
    rows.append([(f" Filter: {clean(activity.query, views.g.ascii) or 'none'} | {len(ordered)} matching notices", "dim")])
    cursor = min(app.activity.cursor, max(0, len(ordered) - 1))
    available = max(1, height - len(rows) - 6)
    start = max(0, cursor - available // 2)
    context = ("activity", activity.query, bool(activity.expanded), width)
    logical, start = B.window(app, "modal:activity", start, len(ordered), available, context=context, focus=cursor)
    data_start = len(rows)
    for index, notice in enumerate(ordered[start:start + available], start):
        timestamp = time.strftime("%H:%M:%S", time.localtime(notice["time"]))
        style = "sel" if index == cursor else {"error": "red", "warning": "yellow"}.get(notice["level"], "")
        rows.append([(L.cut(clean(f" {timestamp} {notice['level'].upper():7} {notice['text']}", views.g.ascii), max(0, width - 8), views.g.ascii), style)])
    if not ordered:
        rows.append([(" No notices yet. Copy and export results remain here during this session.", "dim")])
    if ordered and app.activity.expanded:
        value = clean(ordered[cursor]["text"], views.g.ascii)
        w = max(1, width - 10)
        text_rows = [[(value[i:i+w], "")] for i in range(0, len(value), w)] or [[("", "")]]
        rows = rows[:data_start]
        context = ("activity-expanded", ordered[cursor]["time"], ordered[cursor]["text"], width)
        logical, start = B.window(app, "modal:activity-expanded", 0, len(text_rows), available, context=context)
        rows += text_rows[start:start + available]
        key, total = "modal:activity-expanded", len(text_rows)
    else:
        key, total = "modal:activity", len(ordered)
    rows.append([(" / filter | f clear filter | e exports | arrows/pages browse | Enter expand | y copy | c cancel | Esc back", "dim")])
    rendered = L.box(views.g, rows, width, height, "Activity")
    return B.boxed(app, key, rendered, start=data_start, count=total, page=available,
                   target=logical, painted=start, setter=lambda value: None, context=context)


def handle_mouse(app, y, x, button="left", shift=False):
    activity = app.activity
    if app.mode != "exports" or activity.mouse_mode != "exports":
        return False
    hit = activity.mouse_rows.get(y)
    if hit and hit[1] <= x < hit[2] and button in ("left", "double"):
        entries = export_items(app)
        index = next((i for i, item in enumerate(entries) if item["id"] == hit[0]), None)
        if index is not None:
            activity.export_cursor, activity.export_selected = index, hit[0]
            if button == "double": _preview_export(app, entries[index])
    return True
