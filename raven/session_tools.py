"""Completion review, notification controls and read-only terminal diagnostics."""
from __future__ import annotations

from bisect import bisect_right

import hashlib
import math
import socket
import time

from . import doctor, layout as L
from .model import terminal_state, TERMINAL_STATES
from .research import clean

LIMIT = 256


def initialize(app):
    if not isinstance(getattr(app, "session_tools_state", None), dict):
        app.session_tools_state = {"reviewed": [], "records": {}, "cursor": 0, "query": "", "filtering": False,
            "inbox_filter": "unread", "selected": "", "diagnostics": [], "diagnostic_cursor": 0,
            "probe_events": [], "alert_cursor": 0, "alert_draft": {}, "mouse_rows": {}, "mouse_mode": ""}
    return app.session_tools_state


def _scope(app):
    return {"user": app.user, "host": app.cfg.get("host", "") or socket.gethostname(),
            "profile": getattr(app, "profile_name", "")}


def restore(app, value):
    state = initialize(app)
    if not isinstance(value, dict) or value.get("scope") != _scope(app):
        return
    reviewed = value.get("reviewed", [])
    state["reviewed"] = list(dict.fromkeys(key for key in reviewed[-512:]
        if isinstance(key, str) and len(key) == 24 and all(c in "0123456789abcdef" for c in key))) if isinstance(reviewed, list) else []
    engine = app.store.alerts
    if engine:
        engine.restore_controls(value.get("alert_controls", {}))


def save(app):
    state = initialize(app)
    return {"scope": _scope(app), "reviewed": list(state["reviewed"])[-512:],
            "alert_controls": app.store.alerts.controls_snapshot() if app.store.alerts else {}}


def _identity(record):
    value = "\0".join(str(getattr(record, key, "")) for key in ("id", "start", "end")) + "\0" + terminal_state(record.state)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def observe(app, snap):
    """Consume published accounting records; queue disappearance is not completion."""
    state = initialize(app)
    records = state["records"]
    for record in snap.get("finished", [])[:LIMIT]:
        if terminal_state(record.state) not in TERMINAL_STATES:
            continue
        key = _identity(record)
        records[key] = record
    if len(records) > LIMIT:
        keep = sorted(records, key=lambda key: getattr(records[key], "end", ""), reverse=True)[:LIMIT]
        state["records"] = {key: records[key] for key in keep}


def inbox_items(app):
    state = initialize(app)
    seen = set(state["reviewed"])
    items = []
    for key, record in state["records"].items():
        unread = key not in seen
        failed = terminal_state(record.state) != "COMPLETED"
        if state["inbox_filter"] == "unread" and not unread or state["inbox_filter"] == "failed" and not failed:
            continue
        if state["query"].casefold() not in " ".join((record.id, record.name, record.state, record.partition)).casefold():
            continue
        items.append({"key": key, "record": record, "unread": unread, "failed": failed})
    # A stable end-time pass retains chronological order within each outcome.
    items.sort(key=lambda item: getattr(item["record"], "end", ""), reverse=True)
    items.sort(key=lambda item: (not item["unread"], not item["failed"]))
    return items


def unread_count(app):
    state = initialize(app)
    reviewed = set(state["reviewed"])
    return sum(key not in reviewed for key in state["records"])


def _ack(app, keys):
    state = initialize(app)
    state["reviewed"] = list(dict.fromkeys(state["reviewed"] + list(keys)))[-512:]
    state["selected"] = ""
    app.save()


def _pick(app):
    state = initialize(app)
    items = inbox_items(app)
    if not items:
        return None
    selected = state.get("selected")
    anchored = next((index for index, item in enumerate(items) if item["key"] == selected), None)
    cursor = anchored if anchored is not None else max(0, min(state["cursor"], len(items) - 1))
    state["cursor"], state["selected"] = cursor, items[cursor]["key"]
    return items[cursor]


def _open_completion(app, item, logs=False):
    state = initialize(app)
    record = item["record"]
    app.mode = "main"
    if logs:
        current = app.job_record(record.id) if hasattr(app, "job_record") else next(
            (entry for entry in list(app.store.snapshot().get("jobs", [])) + list(app.store.snapshot().get("finished", [])) if entry.id == record.id), None)
        if current is not None and _identity(current) != item["key"]:
            app.mode = "session_inbox"
            app.fail("Another or unverified execution uses this job ID. This cached completion remains unread; inspect its exact History record and declared paths.")
            return
        app.log_record = record
        app.open_log(record.id)
        if app.log_job != record.id:
            app.mode = "session_inbox"
            return
    else:
        app.enter_tab("history")
        app.filter = ""
        # An explicit ID facet exposes the chosen identity without selecting a
        # different row when other saved History filters would hide it.
        app.table_state["facets"]["history"] = {"id": record.id}
        history = app.history_jobs()
        exact = next((index for index, entry in enumerate(history) if _identity(entry) == item["key"]), None)
        if exact is None:
            app.mode = "session_inbox"
            app.say("This exact completion is not visible under the current History filters or date window. Adjust the filters or expand the window before opening it.")
            return
        app.cursor["history"] = exact
        app.sync_history_selection()
    _ack(app, [item["key"]])


def command_names():
    return ["inbox", "alerts", "terminaldoctor", "terminaltest"]


def run_command(app, args):
    if not args or args[0] not in command_names():
        return False
    state = initialize(app)
    command, values = args[0], args[1:]
    if command == "inbox":
        observe(app, app.store.snapshot())
        if not values or len(values) == 1 and values[0] in ("unread", "all", "failed"):
            state["inbox_filter"] = values[0] if values else "unread"
            state["cursor"], state["selected"] = 0, ""
            app.mode = "session_inbox"
        elif values[0] == "filter":
            state["query"], state["cursor"] = " ".join(values[1:])[:256], 0
            app.mode = "session_inbox"
        elif values[0] == "ack":
            items = inbox_items(app)
            if values[1:] == ["all"]:
                _ack(app, [item["key"] for item in items])
            elif len(values) == 2:
                selected = [item for item in items if item["record"].id == values[1]]
                if not selected:
                    app.fail("That job is not in the current completion list.")
                    return True
                _ack(app, [item["key"] for item in selected])
            else:
                app.fail("Use inbox ack JOBID|all.")
        else:
            app.fail("Use inbox [unread|all|failed|filter TEXT|ack JOBID|ack all].")
    elif command == "alerts":
        engine = app.store.alerts
        if engine is None:
            app.fail("The alert engine is unavailable in this session.")
            return True
        try:
            if not values or values == ["show"]:
                app.mode = "session_alerts"
            elif values[0] == "snooze" and len(values) in (3, 4):
                engine.snooze(values[1], float(values[2]), values[3] if len(values) == 4 else "*")
                app.say("Alert notifications snoozed. Conditions and events remain visible.")
                app.save()
            elif values[0] == "unsnooze" and len(values) in (1, 2, 3):
                engine.unsnooze(values[1] if len(values) >= 2 else None, values[2] if len(values) == 3 else "*")
                app.say("Alert snooze removed.")
                app.save()
            elif values == ["quiet", "off"]:
                engine.set_quiet()
                app.save()
                app.say("Quiet hours disabled.")
            elif values[0] == "quiet" and len(values) in (3, 4):
                engine.set_quiet(values[1], values[2], values[3] if len(values) == 4 else "America/Los_Angeles")
                app.save()
                app.say(f"Quiet hours saved in {engine.quiet_zone}.")
            else:
                raise ValueError("Use alerts [show|snooze RULE SECONDS [JOBID]|unsnooze [RULE [JOBID]]|quiet START END [ZONE]|quiet off].")
        except (ValueError, KeyError, OverflowError) as exc:
            app.fail(str(exc))
    elif command == "terminaldoctor":
        if values:
            app.fail("Use terminaldoctor without arguments.")
            return True
        from .research import ResearchHub
        if app.research is None:
            app.research = ResearchHub(app.cfg, app.logs.files)
        if app.research.pending:
            app.fail("A background operation is running. Try diagnostics when it finishes.")
            return True
        state["diagnostics"] = [{"name": "Inspection", "status": "ok", "detail": "Reading local terminal and path metadata..."}]
        state["diagnostic_cursor"], app.mode = 0, "terminal_diagnostics"
        def inspect():
            return doctor.terminal_evidence(app.cfg, state_dir=app.state_dir or "")
        def ready(value):
            state["diagnostics"] = [{"name": "Inspection", "status": "warning", "detail": str(value)}] if isinstance(value, Exception) else value
        app.research.start_task(inspect, ready)
    elif command == "terminaltest":
        if values == ["clipboard"]:
            from . import clipboard
            app.say(clipboard.copy("Raven clipboard test", app.state_dir,
                **clipboard.options(app)))
            state["probe_events"].append("Clipboard delivery requested. Paste into your terminal to verify the result.")
        elif values:
            app.fail("Use terminaltest [clipboard].")
            return True
        app.mode = "terminal_probe"
    return True


def handle_key(app, key):
    state = initialize(app)
    if app.mode == "session_inbox":
        if state["filtering"]:
            if key in ("enter", "esc"):
                state["filtering"] = False
            elif key == "backspace":
                state["query"] = state["query"][:-1]
            elif key == "space" or len(key) == 1 and key.isprintable():
                state["query"] = (state["query"] + (" " if key == "space" else key))[:256]
            state["cursor"], state["selected"] = 0, ""
            return True
        items = inbox_items(app)
        _pick(app)  # Reanchor before any queued navigation or action.
        if key in ("esc", "q"):
            app.mode = "main"
        elif key == "/":
            state["filtering"] = True
        elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
            n = len(items)
            step = {"up": -1, "down": 1, "pgup": -8, "pgdn": 8, "home": -n, "end": n}[key]
            state["cursor"] = max(0, min(max(0, n - 1), state["cursor"] + step))
            state["selected"] = items[state["cursor"]]["key"] if items else ""
        elif key == "u":
            state["inbox_filter"], state["cursor"], state["selected"] = "unread", 0, ""
        elif key == "f":
            state["inbox_filter"], state["cursor"], state["selected"] = "failed", 0, ""
        elif key == "a":
            state["inbox_filter"], state["cursor"], state["selected"] = "all", 0, ""
        elif key == "A":
            _ack(app, [item["key"] for item in items])
        elif key in ("enter", "l", "r"):
            item = _pick(app)
            if item:
                if key == "r":
                    _ack(app, [item["key"]])
                else:
                    _open_completion(app, item, logs=key == "l")
        return True
    if app.mode == "session_alerts":
        engine = app.store.alerts
        if key in ("esc", "q"):
            app.mode = "main"
        elif engine:
            if key in ("up", "down"):
                state["alert_cursor"] = max(0, min(max(0, len(engine.rules) - 1), state["alert_cursor"] + (1 if key == "down" else -1)))
            elif key in ("s", "u") and engine.rules:
                rule = engine.rules[state["alert_cursor"]]
                run_command(app, ["alerts", "snooze", rule.name, "1800"] if key == "s" else ["alerts", "unsnooze", rule.name])
            elif key == "Q":
                run_command(app, ["alerts", "quiet", "off"])
        return True
    if app.mode == "terminal_diagnostics":
        if key in ("esc", "q"):
            app.mode = "main"
        elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
            n = len(state["diagnostics"])
            step = {"up": -1, "down": 1, "pgup": -5, "pgdn": 5, "home": -n, "end": n}[key]
            state["diagnostic_cursor"] = max(0, min(max(0, n - 1), state["diagnostic_cursor"] + step))
        elif key == "t":
            run_command(app, ["terminaltest"])
        elif key == "r":
            run_command(app, ["terminaldoctor"])
        return True
    if app.mode == "terminal_probe":
        if key == "esc":
            app.mode = "main"
        else:
            state["probe_events"] = (state["probe_events"] + ["Key: " + clean(key, limit=80)])[-16:]
        return True
    return False


def handle_mouse(app, y, x, button="left", shift=False):
    if app.mode == "terminal_probe":
        state = initialize(app)
        event = f"Mouse: {button} x={x}, y={y}" + (" shift" if shift else "")
        state["probe_events"] = (state["probe_events"] + [event])[-16:]
        return True
    state = initialize(app)
    if app.mode == "session_inbox" and state.get("mouse_mode") == "session_inbox":
        hit = state["mouse_rows"].get(y)
        if hit and hit[1] <= x < hit[2] and button in ("left", "double"):
            items = inbox_items(app)
            index = next((i for i, item in enumerate(items) if item["key"] == hit[0]), None)
            if index is not None:
                state["cursor"], state["selected"] = index, hit[0]
                if button == "double": _open_completion(app, items[index])
        return True
    return False


def overlay(views, snap, app, width, height):
    from . import modal_scrollbars as B
    state = initialize(app)
    rows = []
    state["mouse_rows"], state["mouse_mode"] = {}, app.mode
    if app.mode == "session_inbox":
        logical_hits = {}
        observe(app, snap)
        items = inbox_items(app)
        selected = state["selected"]
        if selected:
            index = next((i for i, item in enumerate(items) if item["key"] == selected), None)
            if index is not None:
                state["cursor"] = index
        cursor = max(0, min(state["cursor"], max(0, len(items) - 1)))
        state["cursor"] = cursor
        state["selected"] = items[cursor]["key"] if items else ""
        rows = [[(f" {unread_count(app)} unreviewed | {state['inbox_filter']} | latest {LIMIT} retained completions", "accent+bold")],
                [(" Filter: " + (clean(state["query"], views.g.ascii) or "none"), "dim")]]
        page = max(1, height - 8)
        start = max(0, cursor - page // 2)
        context = ("session_inbox", state["inbox_filter"], state["query"])
        logical, start = B.window(app, "modal:inbox", start, len(items), page, context=context, focus=cursor)
        for index, item in enumerate(items[start:start + page], start):
            record = item["record"]
            mark = "NEW" if item["unread"] else "read"
            text = f" {mark:4} {record.id:16} {record.state:14} {record.name}  {record.end}"
            style = "sel" if index == cursor else "red" if item["failed"] else "green"
            logical_hits[len(rows)] = item["key"]
            rows.append([(L.cut(clean(text, views.g.ascii), max(1, width - 8), views.g.ascii), style)])
        if not items:
            rows.append([(" No completions match. a shows all known completions.", "dim")])
        rows.append([(" u unread | f failures | a all | / filter | Enter History | l logs | r reviewed | A review list | Esc back", "dim")])
        rendered = L.box(views.g, rows, width, height, "Completion inbox")
        for row_index, identity in logical_hits.items():
            if row_index + 1 < len(rendered) - 1:
                y, x, row = rendered[row_index + 1]
                state["mouse_rows"][y] = (identity, x + 1, x + L.vlen(L.row_text(row)) - 1)
        return B.boxed(app, "modal:inbox", rendered, start=2, count=len(items), page=page,
                       target=logical, painted=start, setter=lambda value: None, context=context)
    if app.mode == "session_alerts":
        engine = app.store.alerts
        if engine:
            controls = engine.controls_snapshot()
            quiet = controls["quiet"]
            text = "off" if quiet is None else " to ".join(f"{m // 60:02}:{m % 60:02}" for m in quiet) + " " + controls["zone"]
            rows.append([(" Quiet hours: " + text, "accent")])
            rules = engine.rules
            cursor = min(state["alert_cursor"], max(0, len(rules) - 1))
            page = max(1, (height - 7) // 2)
            start = 2 * max(0, cursor - page // 2)
            page *= 2
            context = ("session_alerts", id(engine), bool(views.g.ascii))
            logical, start = B.window(app, "modal:alerts", start, 2 * len(rules), page, context=context, focus=cursor)
            for index in range(start // 2, min(len(rules), (start + page + 1) // 2)):
                rule = rules[index]
                active = sorted(rule.active.copy())
                muted = engine.notification_muted(rule.name)
                card = [[(clean(f" {rule.name} | {len(active)} active | {'notifications muted' if muted else 'notifications enabled'}", views.g.ascii), "sel" if index == cursor else "")],
                        [("  " + clean(rule.when, views.g.ascii), "dim")]]
                rows.extend(card[max(0, start - index * 2):min(2, start + page - index * 2)])
            for entry in controls["snoozes"][:max(0, height - len(rows) - 6)]:
                rows.append([(clean(f" Snooze {entry['rule']} job={entry['job']} {max(0, entry['until'] - time.time()):.0f}s remaining", views.g.ascii), "yellow")])
            if not rules:
                rows.append([(" No alert rules are configured. Add rules in your Raven configuration.", "dim")])
        rows.append([(" Arrows choose rule | s snooze 30m | u resume rule | Q quiet off | :alerts for custom controls | Esc back", "dim")])
        rendered = L.box(views.g, rows, width, height, "Alert controls")
        if engine:
            return B.boxed(app, "modal:alerts", rendered, start=1, count=2 * len(rules), page=page,
                           target=logical, painted=start, setter=lambda value: None, context=context)
        return rendered
    if app.mode == "terminal_diagnostics":
        page = 3 * max(1, (height - 6) // 3)
        context = ("terminal_diagnostics", id(state["diagnostics"]), width, bool(views.g.ascii))
        offsets = _diagnostic_offsets(state, width, views.g.ascii)
        target = offsets[min(state["diagnostic_cursor"], max(0, len(offsets) - 2))]
        logical, start = B.window(app, "modal:diagnostics", target, offsets[-1], page,
                                  context=context, focus=state["diagnostic_cursor"])
        index = max(0, bisect_right(offsets, start) - 1)
        while index < len(state["diagnostics"]) and offsets[index] < start + page:
            item = state["diagnostics"][index]
            card = [[(f" {item['status'].upper()} {item['name']}", "cyan" if item["status"] == "ok" else "yellow")]]
            detail = clean(item["detail"], views.g.ascii)
            available = max(1, width - 10)
            card += [[("  " + detail[i:i + available], "")] for i in range(0, min(len(detail), available * 2), available)]
            rows.extend(card[max(0, start - offsets[index]):max(0, min(len(card), start + page - offsets[index]))])
            index += 1
        rows.append([(" Read-only evidence | arrows/pages browse | t interactive tests | r refresh | Esc back", "dim")])
        rendered = L.box(views.g, rows, width, height, "Terminal diagnostics")
        return B.boxed(app, "modal:diagnostics", rendered, start=0, count=offsets[-1], page=page,
                       target=logical, painted=start, setter=lambda value: None, context=context, header=-1)
    if app.mode == "terminal_probe":
        rows = [[(" Glyph alignment: each block should occupy one terminal cell.", "accent")],
                [(" |12345678901234567890|", "")],
                [(" |" + ("[][].[][].[][].[][]." if views.g.ascii else "████▁▂▃▄▅▆▇█⣿⣿┌┐└┘░▒▓") + "|", "cyan")],
                [(" cyan ", "cyan"), (" green ", "green"), (" yellow ", "yellow"), (" red ", "red"), (" magenta ", "magenta"), (" blue ", "blue")],
                [(" Press keys or click anywhere. These events do not activate commands.", "dim")]]
        rows += [[(" " + clean(event, views.g.ascii), "")] for event in state["probe_events"][-max(1, height - 10):]]
        rows.append([(" Esc closes | :terminaltest clipboard requests a copy test outside this probe", "dim")])
        return L.box(views.g, rows, width, height, "Terminal input test")
    return None


def _diagnostic_offsets(state, width, ascii_):
    """Index physical rows once per published source, width or glyph change."""
    source = state["diagnostics"]
    cached = state.get("diagnostic_row_index")
    identity = (width, bool(ascii_), len(source))
    if cached and cached["source"] is source and cached["identity"] == identity:
        return cached["offsets"]
    available = max(1, width - 10)
    offsets = [0]
    for item in source:
        length = min(len(clean(item["detail"], ascii_)), available * 2)
        offsets.append(offsets[-1] + 1 + (length + available - 1) // available)
    state["diagnostic_row_index"] = {"source": source, "identity": identity, "offsets": offsets}
    return offsets
