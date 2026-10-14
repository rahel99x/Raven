"""A bounded, virtualized alert inbox over already collected evidence.

Wrapping retains compact offsets into each immutable message. Unchanged frames
reuse those offsets and construct only the visible rows; painting never queries
Slurm, reads files, or executes a suggested remedy.
"""
from __future__ import annotations

from array import array
from bisect import bisect_right
from dataclasses import dataclass
import time

from . import layout as L
from .control_rows import buttons, place_hits

SEVERITIES = ("critical", "moderate", "mild")
STYLES = {"critical": "red", "moderate": "orange", "mild": "yellow"}
BAR_KEY = "modal:notifications"


def initialize(app):
    state = getattr(app, "notification_ui_state", None)
    if not isinstance(state, dict):
        state = dict(severity="all", selected="", details=False, top=0, page=1,
                     return_mode="main", revision=0, control_hits=[], painted=None,
                     status_hits=[], status_painted=None, cache={}, cache_width=None,
                     document=None, document_key=None, follow=False, list_top=0,
                     notices_key=None, notices=())
        app.notification_ui_state = state
    return state


def _center(app):
    from .notifications import initialize as center
    return center(app)


def context(app):
    """Constant-size identity for stale pointer and keyboard-focus rejection."""
    state = initialize(app)
    center = getattr(app, "notifications", None)
    return (center.snapshot().revision if center is not None else 0,
            state["revision"], state["severity"], state["selected"],
            state["details"], state["top"])


def _frame(app):
    return (getattr(app, "mode", "main"), getattr(app, "tab", ""),
            getattr(app, "width", None), getattr(app, "height", None), context(app))


def _invalidate(app, *, resume=True):
    state = initialize(app)
    state["revision"] += 1
    state.update(control_hits=[], painted=None)
    if resume:
        from .scrollbars import resume as resume_scroll
        resume_scroll(app, BAR_KEY)


def open_notifications(app, severity="all"):
    """Open a severity inbox without starting background work or changing jobs."""
    if severity not in (*SEVERITIES, "all"):
        raise ValueError("Notification severity must be critical, moderate, mild, or all")
    state = initialize(app)
    if getattr(app, "mode", "main") != "notifications":
        state["return_mode"] = getattr(app, "mode", "main")
    state.update(severity=severity, details=False, top=0, follow=True)
    app.mode = "notifications"
    _invalidate(app)


def command_names():
    return ["notifications"]


def run_command(app, args):
    if not args or args[0] != "notifications":
        return False
    values = args[1:]
    # Preserve existing Activity filtering, persistence, and clear aliases.
    if values and values[0] in ("filter", "level", "job", "task", "persistent", "clear", "show"):
        return False
    if not values or len(values) == 1 and values[0] in (*SEVERITIES, "all"):
        open_notifications(app, values[0] if values else "all")
    elif values == ["back"]:
        _back(app)
    elif len(values) == 2 and values[0] == "related":
        _open_related(app, values[1])
    elif len(values) == 2 and values[0] in ("open", "dismiss"):
        if values[0] == "open":
            _open_detail(app, values[1])
        else:
            _dismiss(app, values[1])
    else:
        app.fail("Use notifications [critical|moderate|mild|all].")
    return True


def _open_related(app, identity):
    """Use typed retained context, never execute message contents as commands."""
    if getattr(app, "mode", "") != "notifications":
        return
    item = _center(app).get(identity)
    if item is None:
        return
    if item.run:
        from .run_home_ui import open_run
        root, run_id, attempt, job, cluster, token = item.run
        open_run(app, dict(project_root=root, run_id=run_id, attempt=attempt, job_id=job,
                          launch=dict(cluster=cluster, submission_token=token)))
    elif item.setting:
        command = {"connections": "profiles", "allocation": "setup allocation", "refresh": "refreshsettings",
                   "recordings": "setup telemetry", "diagnostics": "setup installation"}[item.setting]
        app.mode = "main"
        from .workbench import run_command
        run_command(app, command.split())
    elif item.job:
        # A bare scheduler job ID cannot establish a recorded attempt. Only
        # navigate to a currently cached record; no file association is guessed.
        record = getattr(app, "job_record", lambda job: None)(item.job)
        if record is not None:
            from .model import stamp
            submitted = stamp(getattr(record, "submit", ""))
            if submitted is not None and submitted > item.last_seen:
                record = None
        if record is None:
            app.fail("This job is not in the current connection's cache. Reconnect its original site or select its retained run.")
            return
        app.selected_id, app.tab, app.mode = item.job, "jobs", "main"


def _back(app):
    if getattr(app, "mode", "main") != "notifications":
        return
    state = initialize(app)
    if state["details"]:
        state.update(details=False, top=state["list_top"], follow=False)
    else:
        app.mode = state["return_mode"] if state["return_mode"] != "notifications" else "main"
    _invalidate(app)


def _open_detail(app, identity):
    state = initialize(app)
    if getattr(app, "mode", "main") != "notifications" or _center(app).get(identity) is None:
        return
    state.update(selected=identity, details=True, list_top=state["top"], top=0, follow=False)
    _invalidate(app)


def _notices(app):
    snapshot = _center(app).snapshot()
    state = initialize(app)
    severity = state["severity"]
    key = (snapshot.revision, severity)
    if state["notices_key"] != key:
        state["notices"] = (snapshot.notices if severity == "all" else
                            tuple(item for item in snapshot.notices if item.severity == severity and not item.resolved_at))
        state["notices_key"] = key
    return snapshot, state["notices"]


def _selection(state, notices):
    index = next((i for i, item in enumerate(notices) if item.id == state["selected"]), 0)
    state["selected"] = notices[index].id if notices else ""
    return index


def _dismiss(app, identity):
    state = initialize(app)
    _, notices = _notices(app)
    index = next((i for i, item in enumerate(notices) if item.id == identity), None)
    if index is None or not _center(app).dismiss(identity):
        return
    if state["selected"] == identity:
        remaining = notices[:index] + notices[index+1:]
        state["selected"] = remaining[min(index, len(remaining)-1)].id if remaining else ""
    if state["details"]:
        state.update(details=False, top=state["list_top"])
    state["follow"] = False
    _invalidate(app)


@dataclass(frozen=True)
class Wrapped:
    text: str
    offsets: array

    def __len__(self):
        return len(self.offsets) // 2

    def line(self, index):
        return self.text[self.offsets[2*index]:self.offsets[2*index+1]]


def _wrap(text, width, ascii_=False):
    """One linear scan, compact offsets, no copied offscreen line strings."""
    width = max(1, width)
    if ascii_ and not text.isascii():
        text = text.encode("ascii", "replace").decode("ascii")
    offsets = array("I")
    if text.isascii():
        # Scheduler diagnostics are mostly ASCII. Scan paragraphs in C and
        # retain word boundaries without walking every character in Python.
        start = 0
        while True:
            newline = text.find("\n", start)
            end = len(text) if newline < 0 else newline
            if start == end:
                offsets.extend((start, end))
            while start < end:
                stop = min(start+width, end)
                if stop < end:
                    space = text.rfind(" ", start+1, stop)
                    if space >= 0:
                        stop = space+1
                offsets.extend((start, stop))
                start = stop
            if newline < 0:
                break
            start = newline+1
        return Wrapped(text, offsets)
    start = used = 0
    for index, char in enumerate(text):
        if char == "\n":
            offsets.extend((start, index))
            start, used = index+1, 0
            continue
        size = 1 if char.isascii() else L.vlen(char)
        if used and used+size > width:
            offsets.extend((start, index))
            start, used = index, 0
        used += size
    if start < len(text) or not offsets or text.endswith("\n"):
        offsets.extend((start, len(text)))
    return Wrapped(text, offsets)


def _wrapped(state, item, width, ascii_):
    entry = state["cache"].get(item.id)
    if entry is None or entry[0] is not item.text:
        entry = (item.text, _wrap(item.text, width, ascii_))
        state["cache"][item.id] = entry
    return entry[1]


def text_rows(app, width, ascii_=False):
    """Keep full retained errors readable in noninteractive --once output."""
    center = getattr(app, "notifications", None)
    notices = center.snapshot().notices if center is not None else ()
    if not notices:
        return []
    rows = [[], [("Notifications", "accent+bold")]]
    for item in notices:
        heading = "Resolved" if item.resolved_at else item.severity.title()
        if item.source:
            heading += " | " + item.source
        if item.job:
            heading += " | job " + item.job
        if item.occurrences > 1:
            heading += f" | {item.occurrences} occurrences"
        for text, style in ((heading, STYLES[item.severity]+"+bold"), (item.text, "text")):
            wrapped = _wrap(text, width, ascii_)
            rows.extend([(wrapped.line(index), style)] for index in range(len(wrapped)))
        if item.truncated:
            rows.append([("Message exceeded the 64 KiB retention limit.", "yellow")])
        rows.append([])
    return rows


def _document(app, width, ascii_):
    state = initialize(app)
    snapshot, notices = _notices(app)
    if state["details"] and not any(item.id == state["selected"] for item in notices):
        # Expiry, dismissal, and connection changes must not silently replace
        # the evidence in an already open details page with a different alert.
        state.update(details=False, top=state["list_top"])
    _selection(state, notices)
    wrap_width = (width, ascii_)
    if state["cache_width"] != wrap_width:
        state.update(cache={}, cache_width=wrap_width, document_key=None)
    key = (snapshot.revision, state["severity"], state["details"],
           state["selected"] if state["details"] else "", width, ascii_)
    if state["document_key"] == key:
        return state["document"]
    live = {item.id for item in snapshot.notices}
    state["cache"] = {key: value for key, value in state["cache"].items() if key in live}
    if state["details"]:
        item = next((item for item in notices if item.id == state["selected"]), None)
        if item is None:
            state.update(details=False, top=state["list_top"])
            return _document(app, width, ascii_)
        lines = [("Full message", "accent+bold"), (item.text, "text"), ("", "text"),
                 ("Diagnosis", "accent+bold"), (item.diagnosis.issue, "text"),
                 ("Possible causes", "accent+bold")]
        lines.extend(("- " + value, "text") for value in item.diagnosis.causes)
        lines.append(("Proposed fixes", "accent+bold"))
        lines.extend((f"{index}. {value}", "text") for index, value in enumerate(item.diagnosis.fixes, 1))
        lines.extend([("", "text"), ("Suggested checks are guidance; Raven does not run them from this panel.", "dim"),
                      ("Source: " + (item.source or "Raven"), "dim")])
        if item.job:
            lines.append(("Job: " + item.job, "dim"))
        if item.task:
            lines.append(("Task: " + item.task, "dim"))
        lines.append((f"Occurrences: {item.occurrences}", "dim"))
        if item.run:
            lines.extend((("Run: " + str(item.run[1]) + " / attempt " + str(item.run[2]), "dim"),
                          ("Project: " + str(item.run[0]), "dim")))
        if item.resolved_at:
            lines.extend((("Resolved after verified recovery", "green+bold"), (item.resolution, "text")))
        if item.truncated:
            lines.append(("Retention limit: this message exceeded 64 KiB. Check the original source for the remainder.", "yellow"))
        blocks, starts, total = [], [], 0
        for text, style in lines:
            wrapped = _wrap(text, width, ascii_)
            starts.append(total)
            blocks.append((style, wrapped))
            total += len(wrapped)
        document = dict(notices=notices, blocks=blocks, starts=starts, total=total)
    else:
        blocks, starts, total = [], [], 0
        for item in notices:
            wrapped = _wrapped(state, item, max(1, width-2), ascii_)
            starts.append(total)
            blocks.append((item, wrapped))
            total += len(wrapped)+2  # Heading with dismiss control, then spacing.
        document = dict(notices=notices, blocks=blocks, starts=starts, total=total)
    state.update(document=document, document_key=key)
    return document


def _scroll(app, amount=None, *, target=None):
    state = initialize(app)
    state["top"] = max(0, target if target is not None else state["top"] + amount)
    state["follow"] = False
    _invalidate(app)


def handle_key(app, key):
    if getattr(app, "mode", "main") != "notifications":
        return False
    from .interaction import handle_key as focus_key
    if focus_key(app, key):
        return True
    state = initialize(app)
    _, notices = _notices(app)
    index = _selection(state, notices)
    if key in ("esc", "q", "left", "backspace"):
        _back(app)
    elif key in ("delete", "d"):
        _dismiss(app, state["selected"])
    elif key in ("enter", "space", "right") and notices and not state["details"]:
        _open_detail(app, state["selected"])
    elif key in ("up", "down", "tab", "btab") and not state["details"]:
        step = -1 if key in ("up", "btab") else 1
        if notices:
            state.update(selected=notices[max(0, min(len(notices)-1, index+step))].id, follow=True)
            _invalidate(app)
    elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
        if key in ("home", "end"):
            _scroll(app, target=0 if key == "home" else 10**9)
        else:
            _scroll(app, {"up": -1, "down": 1, "pgup": -state["page"], "pgdn": state["page"]}[key])
    elif key in ("1", "2", "3", "0"):
        open_notifications(app, {"1": "critical", "2": "moderate", "3": "mild", "0": "all"}[key])
    elif key == ":":
        from .command_ui import open_palette
        open_palette(app)
    return True


def status_row(views, app, width, y):
    """Compact severity buttons for the header; no notices means no extra row."""
    state = initialize(app)
    snapshot = _center(app).snapshot()
    state.update(status_hits=[], status_painted=None)
    if not any(snapshot.counts.get(severity, 0) for severity in SEVERITIES):
        return None
    row, x = [], 0
    symbol = "!" if views.g.ascii else "⚠"
    for severity in SEVERITIES:
        count = snapshot.counts.get(severity, 0)
        if not count:
            continue
        label = f" {symbol} {severity.title()} {count} "
        if x + L.vlen(label) > width:
            label = f" {symbol}{severity[0].upper()}:{count} "
        if x + L.vlen(label) > width:
            break
        row.append((label, STYLES[severity]+"+bold"))
        state["status_hits"].append((y, "control", dict(id="notifications:"+severity,
            label=f"{severity.title()} notifications: {count}", left=x, right=x+L.vlen(label),
            action=("command", "notifications "+severity), group="notifications")))
        x += L.vlen(label)
    state["status_painted"] = _frame(app)
    return row


def handle_mouse(app, y, x, button="left", shift=False):
    state = initialize(app)
    mode = getattr(app, "mode", "main")
    if mode != "notifications":
        if mode != "main" or button not in ("left", "press") or state["status_painted"] != _frame(app):
            return False
        for row, _, hit in state["status_hits"]:
            if row == y and hit["left"] <= x < hit["right"]:
                run_command(app, hit["action"][1].split())
                return True
        return False
    if button in ("wheel-up", "wheel-down"):
        _scroll(app, -3 if button == "wheel-up" else 3)
    elif button in ("left", "press") and state["painted"] == _frame(app):
        for row, _, hit in state["control_hits"]:
            if row == y and hit["left"] <= x < hit["right"]:
                run_command(app, hit["action"][1].split())
                break
    return True


def overlay(views, snap, app, width, height):
    if getattr(app, "mode", "main") != "notifications":
        return None
    from . import modal_scrollbars as B
    state = initialize(app)
    state.update(control_hits=[], painted=None)
    inner = max(1, width-5)  # Box borders, outer margins, and a dedicated rail.
    document = _document(app, inner, views.g.ascii)
    choices = [(severity, severity.title(), ("command", "notifications "+severity))
               for severity in ("all", *SEVERITIES)]
    choices.append(("back", "Back", ("command", "notifications back")))
    if state["details"]:
        choices.append(("dismiss", "Dismiss " + ("x" if views.g.ascii else "×"),
                        ("command", "notifications dismiss "+state["selected"])))
        item = _center(app).get(state["selected"])
        if item is not None and (item.run or item.setting or item.job):
            label = "Open affected run" if item.run else "Open related settings" if item.setting else "Open affected job"
            choices.append(("related", label, ("command", "notifications related "+item.id)))
    rows, hits = buttons(views.g, inner, choices, selected=state["severity"], group="notifications",
                         prefix="notification-filter:")
    header = len(rows)
    capacity = max(1, height-header-5)
    state["page"] = capacity
    if state.pop("follow", False) and not state["details"]:
        selected = next((index for index, (item, _) in enumerate(document["blocks"])
                         if item.id == state["selected"]), None)
        if selected is not None:
            row = document["starts"][selected]
            if row < state["top"] or row >= state["top"]+capacity:
                state["top"] = row
    scroll_context = ("notifications", state["severity"], state["details"],
                      state["selected"] if state["details"] else "", width)
    logical, painted = B.window(app, BAR_KEY, state["top"], document["total"], capacity,
                                context=scroll_context)
    state["top"] = logical
    symbol = "x" if views.g.ascii else "×"
    block = bisect_right(document["starts"], painted)-1
    for line in range(painted, min(document["total"], painted+capacity)):
        if block+1 < len(document["starts"]) and document["starts"][block+1] <= line:
            block += 1
        offset = line-document["starts"][block]
        item, wrapped = document["blocks"][block]
        row_index = len(rows)
        if state["details"]:
            rows.append([(wrapped.line(offset), item)])
            continue
        selected = item.id == state["selected"]
        if offset == 0:
            timestamp = time.strftime("%H:%M:%S", time.localtime(item.last_seen))
            label = f" {'RESOLVED' if item.resolved_at else item.severity.upper()}  {timestamp}"
            if item.occurrences > 1:
                label += f"  ({item.occurrences} occurrences)"
            label = L.truncate(label, max(0, inner-3))
            left = max(0, inner-3)
            rows.append([(label.ljust(left), "sel+bold" if selected else ("green" if item.resolved_at else STYLES[item.severity])+"+bold"),
                         (" "+symbol+" ", "red+bold")])
            hits.append((row_index, "control", dict(id="notification-dismiss:"+item.id, label="Dismiss notification",
                left=left, right=inner, action=("command", "notifications dismiss "+item.id), group="notifications")))
        elif offset <= len(wrapped):
            rows.append([("  "+wrapped.line(offset-1), "sel" if selected else "text")])
            left = inner
        else:
            rows.append([("", "")])
            continue
        hits.append((row_index, "control", dict(id=f"notification-open:{item.id}:{offset}", label="Inspect notification",
            left=0, right=left, action=("command", "notifications open "+item.id), group="notifications")))
    if not document["total"]:
        rows.append([(" No active notifications in this severity.", "dim")])
    footer = ("Arrows/PgUp/PgDn scroll | Del dismiss | Esc list" if state["details"] else
              "Arrows select | Enter details | Del dismiss | PgUp/PgDn scroll | Esc back")
    rows.append([(L.truncate(footer, inner), "dim")])
    title = "Notification details" if state["details"] else "Notifications: "+state["severity"].title()
    rendered = L.box(views.g, rows, width, height, title, min_width=max(1, width-2))
    state["control_hits"] = place_hits(hits, rendered[1:-1])
    rendered = B.boxed(app, BAR_KEY, rendered, start=header, count=document["total"], page=capacity,
                       target=logical, painted=painted, setter=lambda value: _set_top(app, value),
                       context=scroll_context, header=-1)
    state["painted"] = _frame(app)
    return rendered


def _set_top(app, value):
    state = initialize(app)
    state.update(top=value, follow=False)
    _invalidate(app, resume=False)
