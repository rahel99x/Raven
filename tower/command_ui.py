"""Editable fuzzy commands, contextual searchable help, and complete action reviews.

This module supplies overlays and key handling without invoking scheduler work
while rendering. Command execution remains with the controller; confirmations
retain its tested action dispatch and its explicit y/n shortcuts.
"""
from __future__ import annotations

import os
import re
import shlex
import textwrap
import time
from types import MappingProxyType

from . import command_registry as registry
from .layout import box, clip_row, cut, vlen
from .research import RESEARCH_VIEWS, clean

MAX_INPUT = 4096
MAX_HISTORY = 50
MAX_RESULTS = 100
MAX_UNDO = 100

# Compatibility views for integrations that imported the original metadata.
# All values originate in the same immutable command descriptors.
DESCRIPTIONS = MappingProxyType({item.name: item.description for item in registry.COMMANDS})
ID_COMMANDS = frozenset(item.name for item in registry.COMMANDS if item.job_ids)
PATH_COMMANDS = frozenset(item.name for item in registry.COMMANDS if item.paths)
PATH_FLAGS = registry.PATH_FLAGS
ARGUMENTS = MappingProxyType({item.name: item.arguments for item in registry.COMMANDS if item.arguments})
EXAMPLES = MappingProxyType({item.name: item.example for item in registry.COMMANDS if item.example})


def initialize(app):
    if not isinstance(getattr(app, "command_state", None), dict):
        app.command_state = {"history": [], "cursor": 0, "input_seen": None, "result_cursor": 0,
                             "result_top": 0, "page": 8, "history_index": None, "history_draft": "",
                             "help_query": "", "help_searching": False, "help_mode_seen": False,
                             "help_page": 8, "help_total": 0, "confirm_token": None,
                             "confirm_scroll": 0, "confirm_focus": "cancel", "confirm_page": 8, "confirm_total": None,
                             "path_cache": {}, "confirm_cache": None, "confirm_controls_visible": None}
    for key, value in (("undo", []), ("redo", []), ("paste_notice", ""), ("origin_mode", "main")):
        app.command_state.setdefault(key, value)
    return app.command_state


def restore(app, data):
    state = initialize(app)
    data = data if isinstance(data, dict) else {}
    history = data.get("history", [])
    if isinstance(history, list):
        state["history"] = [value[:MAX_INPUT] for value in history[-MAX_HISTORY:]
                            if isinstance(value, str) and value.strip() and all(ch.isprintable() or ch in "\n\t" for ch in value)]


def save(app):
    return {"history": initialize(app)["history"][-MAX_HISTORY:]}


def command_names():
    return ["commands", "help"]


def open_palette(app, text="", origin=None):
    state = initialize(app)
    if app.mode != "palette":
        state["origin_mode"] = app.mode if origin is None else origin
    state.update(paste_notice="", history_index=None)
    state["undo"].clear()
    state["redo"].clear()
    app.mode = "palette"
    _set_input(app, text, record=False)


def parse_command_line(line):
    """Parse quoted file arguments while preserving expressions and regex text.

    The controller already treats eval/find/filter/note as free-form tails;
    shell parsing would silently remove expression quotes and regex escapes.
    """
    first = str(line).strip().split(None, 1)
    if not first:
        return []
    if registry.command(first[0]).raw_tail:
        return [first[0]] + (first[1].split() if len(first) == 2 else [])
    return shlex.split(line)


def fuzzy_score(query, candidate):
    """Small deterministic subsequence matcher; exact/prefix matches lead."""
    query, candidate = query.casefold(), candidate.casefold()
    if not query:
        return 0
    if query == candidate:
        return 10000
    if candidate.startswith(query):
        return 5000 - len(candidate)
    where, positions = 0, []
    for char in query:
        found = candidate.find(char, where)
        if found < 0:
            return None
        positions.append(found)
        where = found + 1
    gaps = positions[-1] - positions[0] + 1 - len(positions)
    return 1000 - positions[0] * 5 - gaps * 10 - len(candidate)


def _sync_input(app):
    state = initialize(app)
    text = app.palette_edit[:MAX_INPUT]
    app.palette_edit = text
    if state["input_seen"] != text:
        state.update(cursor=len(text), input_seen=text, result_cursor=0, result_top=0)
    state["cursor"] = max(0, min(state["cursor"], len(text)))
    return state


def _set_input(app, text, cursor=None, *, reset_results=True, record=True):
    state = initialize(app)
    old = getattr(app, "palette_edit", "")
    if record and text[:MAX_INPUT] != old:
        state["undo"].append((old[:MAX_INPUT], state["cursor"]))
        del state["undo"][:-MAX_UNDO]
        state["redo"].clear()
    app.palette_edit = text[:MAX_INPUT]
    state["input_seen"] = app.palette_edit
    state["cursor"] = len(app.palette_edit) if cursor is None else max(0, min(cursor, len(app.palette_edit)))
    if reset_results:
        state["result_cursor"], state["result_top"] = 0, 0


def paste(app, text):
    """Insert one bracketed paste as editable text. It never runs a command."""
    if app.mode not in ("main", "palette"):
        app.fail("Close this dialog before pasting a command")
        return False
    if not isinstance(text, str):
        return False
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if any(not ch.isprintable() and ch not in "\n\t" for ch in text):
        app.fail("Paste contains terminal control characters; paste plain command text")
        return False
    if app.mode == "main":
        open_palette(app)
    state = _sync_input(app)
    cursor = state["cursor"]
    room = MAX_INPUT - len(app.palette_edit)
    inserted = text[:max(0, room)]
    _set_input(app, app.palette_edit[:cursor] + inserted + app.palette_edit[cursor:], cursor + len(inserted))
    state["paste_notice"] = ("Paste truncated to 4096 characters. " if len(inserted) != len(text) else "") + "Pasted text is editable; Enter runs one command only"
    return True


def _word_left(text, cursor):
    while cursor and text[cursor - 1].isspace():
        cursor -= 1
    while cursor and not text[cursor - 1].isspace():
        cursor -= 1
    return cursor


def _word_right(text, cursor):
    while cursor < len(text) and not text[cursor].isspace():
        cursor += 1
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    return cursor


def _undo(app, redo=False):
    state = _sync_input(app)
    source, destination = (state["redo"], state["undo"]) if redo else (state["undo"], state["redo"])
    if source:
        destination.append((app.palette_edit, state["cursor"]))
        del destination[:-MAX_UNDO]
        text, cursor = source.pop()
        _set_input(app, text, cursor, record=False)



def validation(app, text=None):
    """Report parse and known argument errors without I/O or side effects."""
    text = app.palette_edit if text is None else text
    if not text.strip():
        return "", "Type a command; Tab completes its name and arguments"
    if any(not ch.isprintable() and ch not in "\n\t" for ch in text):
        return "error", "Remove terminal control characters"
    try:
        args = parse_command_line(text)
    except ValueError as exc:
        return "error", str(exc) + "; close the quote before you run the command"
    command, words = args[0], args[1:]
    descriptor = registry.command(command, app)
    example = descriptor.example
    if command not in app.commands():
        return "warning", "Unknown command; select a completion or edit its name"
    if descriptor.owner == "plugin":
        return "", descriptor.usage or "Enter runs this plugin command"
    choices = {name: registry.argument_choices(name, app) for name in ("theme", "density", "tab")}
    if command == "theme" and words:
        from .palette import canonical_theme
        words = [canonical_theme(words[0])] + words[1:]
    if command in choices and (len(words) != 1 or words[0] not in choices[command]):
        return "warning", "Choose " + " | ".join(choices[command]) + "; example: " + example
    if command == "days" and words:
        try:
            valid = len(words) == 1 and float(words[0]) in app.days_options
        except ValueError:
            valid = False
        if not valid:
            return "warning", "Accounting windows: " + ", ".join(str(value) for value in app.days_options)
    if command in ID_COMMANDS and words and command not in ("note", "tag", "untag", "diff"):
        ids = {job.id for job in app.store.snapshot().get("jobs", [])} | {job.id for job in app.store.snapshot().get("finished", [])}
        unknown = [word for word in words if word not in ids and word not in ("all", "marked") and not word.startswith("-")]
        if unknown and not any(word.startswith("-") for word in words):
            return "warning", "Job ID is not in the cached inventory: " + ", ".join(unknown[:3]) + "; execution can query the scheduler"
    if "\n" in text:
        return "warning", "Multiline paste: Enter submits one command with whitespace-separated arguments"
    return "", ("Example: " + example if example else "Enter runs; scheduler changes still require their action review")


def _active_token(text, cursor):
    """Locate the lexical argument around the cursor, including unfinished quotes."""
    prefix = text[:cursor]
    quote, escaped, start = None, False, 0
    tokens = []
    for index, char in enumerate(prefix):
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char.isspace():
            if start < index:
                tokens.append(prefix[start:index])
            start = index + 1
    end, end_quote, end_escape = cursor, quote, escaped
    while end < len(text):
        char = text[end]
        if end_escape:
            end_escape = False
        elif char == "\\" and end_quote != "'":
            end_escape = True
        elif end_quote:
            if char == end_quote:
                end_quote = None
        elif char in ("'", '"'):
            end_quote = char
        elif char.isspace():
            break
        end += 1
    raw = prefix[start:]
    try:
        current = shlex.split(raw)[0] if raw else ""
    except ValueError:
        current = raw[1:] if raw.startswith(("'", '"')) else raw
    previous = []
    for token in tokens:
        try:
            previous.extend(shlex.split(token))
        except ValueError:
            previous.append(token.strip("'\""))
    return previous, current, start, end


def _path_candidates(app, prefix):
    # Remote completion uses already-known paths. It must never fall back to
    # the local machine or execute an SSH directory listing on a keypress.
    candidates = set()
    for entry in getattr(app.logs, "entries", [])[:512]:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            candidates.add(entry["path"])
    if app.research:
        for key in ("metrics_file", "contract", "workdir", "passport", "planning_file"):
            value = app.research.settings.get(key)
            if isinstance(value, str) and value:
                candidates.add(value)
    from .remote import LocalFiles, RemoteFiles
    files = getattr(app, "files", None) or app.logs.files
    state = initialize(app)
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    cache_key = (prefix, id(files), tuple(sorted(candidates)), cwd)
    cached = state["path_cache"].get(cache_key)
    if cached is not None and time.monotonic() - cached[0] < 1:
        return list(cached[1])
    if isinstance(files, LocalFiles) and not isinstance(files, RemoteFiles) and not getattr(files, "remote", False):
        expanded = os.path.expanduser(prefix)
        directory, fragment = os.path.split(expanded)
        display_dir = os.path.dirname(prefix)
        if len(directory) <= MAX_INPUT:
            try:
                with os.scandir(directory or ".") as entries:
                    for index, entry in enumerate(entries):
                        if index >= 512:
                            break
                        if entry.name.startswith(fragment) and (not entry.name.startswith(".") or fragment.startswith(".")):
                            value = os.path.join(display_dir, entry.name) if display_dir else entry.name
                            if entry.is_dir(follow_symlinks=False):
                                value += "/"
                            candidates.add(value)
            except (OSError, ValueError):
                pass
    result = sorted(value for value in candidates if value.startswith(prefix) and len(value) <= MAX_INPUT and value.isprintable())[:MAX_RESULTS]
    if len(state["path_cache"]) >= 8:
        state["path_cache"].pop(next(iter(state["path_cache"])))
    state["path_cache"][cache_key] = (time.monotonic(), result)
    return result


def _sort_argument_values(app, previous):
    """Complete sort syntax without confusing a column with a table name.

    History's ``nodes`` and Nodes' ``jobs`` are both current columns and table
    names. Until the following argument selects a direction or another column,
    offer both valid continuations, just as the command parser accepts them.
    """
    from .table_sort import TABLE_KEYS
    values = previous[1:]
    current = TABLE_KEYS.get(app.tab, ())
    directions = [(value, "column direction; off removes only this column")
                  for value in ("asc", "desc", "off")]

    def columns(table):
        return [(value, f"{table} column") for value in TABLE_KEYS[table]] + [("clear", f"remove all {table} column sorts")]

    if not values:
        choices = columns(app.tab) if app.tab in TABLE_KEYS else []
        existing = {value for value, _ in choices}
        return choices + [(table, "table; follow with a column or clear")
                          for table in TABLE_KEYS if table not in existing]
    first = values[0]
    if len(values) == 1:
        if first == "clear":
            return []
        if first in TABLE_KEYS:
            choices = columns(first)
            return directions + choices if first in current else choices
        return directions if first in current else []
    # With another argument present, the same rule as table_ui.run_command
    # decides whether the first word names a table or the current column.
    explicit_table = first in TABLE_KEYS and (first not in current or values[1] not in ("asc", "desc", "off"))
    if explicit_table and len(values) == 2 and values[1] in TABLE_KEYS[first]:
        return directions
    return []


def _argument_values(app, previous, prefix):
    command = previous[0] if previous else ""
    if registry.command(command, app).owner == "plugin":
        return []
    if command == "sortby":
        return _sort_argument_values(app, previous)
    if command == "ops":
        return registry.operation_arguments(previous)
    if registry.command(command, app).arguments and len(previous) == 1:
        choices = [(value, "command option") for value in registry.argument_choices(command, app)]
        if command == "execution":
            choices += [(value, "execution receipt") for value in _path_candidates(app, prefix)]
        return choices
    if command == "layout" and len(previous) == 2 and previous[1] in ("load", "delete"):
        return [(value, "saved workspace layout") for value in getattr(getattr(app, "layout_state", None), "named", {})]
    if command == "savedview" and len(previous) == 2 and previous[1] in ("load", "delete"):
        return [(value, "saved table view") for value in getattr(app, "table_state", {}).get("views", {})]
    if command == "location" and len(previous) == 2 and previous[1] in ("open", "delete"):
        return [(value, "saved exact location") for value in getattr(app, "navigation_tools_state", {}).get("locations", {})]
    if command in ("explain", "peek"):
        from .table_sort import TABLE_KEYS
        result = getattr(app, "analysis_result", {}) or {}
        metrics = result.get("series", {}) if isinstance(result, dict) else {}
        if len(previous) == 2 and previous[1] == "metric":
            return [(name, "published metric") for name in list(metrics)[:64]]
        table = previous[1] if len(previous) > 1 and previous[1] in TABLE_KEYS else app.tab
        values = [(key, "table field") for key in TABLE_KEYS.get(table, ())]
        if len(previous) == 1:
            values += [(key, "table; follow with a column") for key in TABLE_KEYS if key not in dict(values)]
            values += [("metric", "published metric; follow with its exact name")]
        return values
    if command == "metricdisplay":
        if len(previous) == 2:
            return [(value, "metric display option") for value in ("label", "unit", "precision", "reset")]
        if len(previous) == 3 and previous[2] == "precision":
            return [(str(value), "decimal places") for value in range(13)]
        if len(previous) == 3 and previous[2] == "unit":
            return [(value, "declared presentation unit; source values are unchanged") for value in ("s", "ms", "%", "B", "KiB", "MiB", "GiB")]
    if command == "run" and len(previous) == 2 and previous[1] == "select":
        return [(value["run_id"], clean(value.get("experiment_id", "project run"), limit=256))
                for value in getattr(app, "project_state", {}).get("runs", [])[:512]
                if isinstance(value, dict) and isinstance(value.get("run_id"), str)]
    if command == "loggroup":
        return [(value, "registered file group") for value in sorted({entry.get("group", "") for entry in app.logs.entries[:512]
                if isinstance(entry, dict) and isinstance(entry.get("group"), str) and entry.get("group")})]
    if command == "facet":
        choices = ["state=RUNNING", "state=PENDING", "state=FAILED", "state=COMPLETED", "state=TIMEOUT", "state=OUT_OF_MEMORY", "state=ACCOUNTING", "clear"]
        choices += ["partition=" + job.partition for job in app.store.snapshot().get("jobs", [])[:512]]
        return [(value, "field filter") for value in dict.fromkeys(choices)]
    if command == "chart" and len(previous) >= 2:
        choices = {"preset": ("5m", "30m", "2h", "all"), "axis": ("auto", "fixed", "log"), "range": ("clear",),
                   "events": ("on", "off"), "shared": ("on", "off")}
        if previous[1] in choices:
            return [(value, "chart " + previous[1] + " option") for value in choices[previous[1]]] if len(previous) == 2 else []
    if command in ("dashboard", "chart", "metricdisplay"):
        if command in ("chart", "metricdisplay") or len(previous) == 2 and previous[1] not in ("list", "reset", "search"):
            result = getattr(app, "analysis_result", {}) or {}
            series = result.get("series", {}) if isinstance(result, dict) else {}
            names = list(dict.fromkeys(list(series)[:128] + getattr(app, "analysis_state", {}).get("order", []) + ["CPU per core (%)", "Memory (GB)", "GPU utilization (%)"]))
            choices = [(value, "observed metric") for value in names]
            if command == "chart" and len(previous) == 1:
                choices += [(value, "chart option") for value in ("window", "zoom", "pan", "cursor", "preset", "axis", "range", "events", "event", "shared")]
            return choices
    if command == "columns" and previous[-1] in ("show", "hide"):
        from .table_ui import definitions
        return [(column.key, column.title) for column in definitions(app.tab if app.tab in ("jobs", "history") else "jobs")]
    if command == "tab":
        from .views import TABS
        return [(key, label) for key, label in TABS]
    if command in ("workspace", "workspaces") or command == "view" and app.tab == "research":
        return [(key, label) for key, label in RESEARCH_VIEWS]
    if command == "view":
        from .views import ANALYTICS_VIEWS, NODES_VIEWS
        return ANALYTICS_VIEWS if app.tab == "analytics" else NODES_VIEWS if app.tab == "nodes" else []
    if command == "theme":
        return [(value, "terminal theme") for value in registry.argument_choices(command, app)]
    if command == "sort":
        from .views import SORTS
        return [(value, "sort key") for value in SORTS.get(app.tab, [])]
    if command == "days":
        return [(str(value), "days of accounting history") for value in app.days_options]
    if command == "profile":
        return [(value, "cluster profile") for value in app.cfg.get("profiles", {})]
    previous_word = previous[-1] if previous else ""
    file_argument = previous_word in PATH_FLAGS or command == "diff" and len(previous) >= 2 and previous[1] == "passport" or command in PATH_COMMANDS and ("/" in prefix or prefix.startswith((".", "~")) or previous_word in ("capture", "show", "compare", "plan", "analyze", "workflow", "scaling", "passport") or len(previous) == 1)
    if file_argument:
        return [(value, "directory" if value.endswith("/") else "file path") for value in _path_candidates(app, prefix)]
    if command in ID_COMMANDS:
        snap = app.store.snapshot()
        seen, values = set(), []
        for job in list(snap.get("jobs", [])) + list(snap.get("finished", [])) + list(snap.get("departed_jobs", {}).values()):
            if job.id not in seen:
                values.append((job.id, clean(job.name, limit=256)))
                seen.add(job.id)
                if len(values) >= 512:
                    break
        if command in ("cancel", "hold", "release", "requeue", "top"):
            values = [("marked", "marked targets"), ("all", "all visible targets")] + values
        if command == "diff" and len(previous) == 1:
            values = [("passport", "compare passport files")] + values
        return values
    return []


def suggestions(app):
    state = _sync_input(app)
    previous, query, start, end = _active_token(app.palette_edit, state["cursor"])
    values = _argument_values(app, previous, query) if previous else [(name, registry.command(name, app).description) for name in dict.fromkeys(app.commands())]
    matches = []
    if not app.palette_edit:
        for index, value in enumerate(reversed(state["history"][-8:])):
            matches.append({"value": value, "description": "recent command", "kind": "history", "start": 0, "end": 0, "score": 20000 - index})
    for value, description in values:
        score = fuzzy_score(query, value)
        if score is not None:
            matches.append({"value": value, "description": description, "kind": "argument" if previous else "command",
                            "start": start, "end": end, "score": score})
    matches.sort(key=lambda row: (-row["score"], row["value"]))
    return matches[:MAX_RESULTS]


def complete(app):
    state = _sync_input(app)
    matches = suggestions(app)
    if not matches:
        return False
    row = matches[min(state["result_cursor"], len(matches) - 1)]
    if row["kind"] == "history":
        _set_input(app, row["value"])
        return True
    candidate = row["value"]
    files = getattr(app, "files", None) or app.logs.files
    if row["description"] in ("file path", "directory", "execution receipt") and candidate.startswith("~") and not getattr(files, "remote", False):
        candidate = os.path.expanduser(candidate)
    value = candidate if row["kind"] == "command" else shlex.quote(candidate)
    after = app.palette_edit[row["end"]:]
    # Operation fields need a value in the same token (manifest=PATH).
    suffix = "" if row["value"].endswith(("/", "=")) else " "
    if after.startswith(" "):
        suffix = ""
    text = app.palette_edit[:row["start"]] + value + suffix + after
    _set_input(app, text, row["start"] + len(value + suffix))
    return True


def _record_history(app, line):
    state = initialize(app)
    if line.strip():
        state["history"] = [item for item in state["history"] if item != line]
        state["history"].append(line[:MAX_INPUT])
        del state["history"][:-MAX_HISTORY]


def _history_move(app, older):
    state = _sync_input(app)
    history = state["history"]
    if not history:
        return
    index = state["history_index"]
    if index is None:
        state["history_draft"], index = app.palette_edit, len(history)
    index = max(0, min(len(history), index + (-1 if older else 1)))
    state["history_index"] = index
    _set_input(app, state["history_draft"] if index == len(history) else history[index])


def _palette_key(app, key):
    state = _sync_input(app)
    cursor, text = state["cursor"], app.palette_edit
    if key == "esc":
        app.mode = state.get("origin_mode", "main")
        _set_input(app, "")
        state["history_index"] = None
    elif key == "enter":
        line = text
        previous, _, _, _ = _active_token(text, cursor)
        matches = suggestions(app)
        if not previous and matches:
            row = matches[min(state["result_cursor"], len(matches) - 1)]
            if row["kind"] == "history":
                line = row["value"]
            elif row["kind"] == "command" and (text.strip().split(None, 1)[0] not in app.commands() or state["result_cursor"] > 0):
                line = text[:row["start"]] + row["value"] + text[row["end"]:]
        status, message = validation(app, line)
        if status == "error":
            app.fail(message)
            return
        app.mode = state.get("origin_mode", "main")
        _set_input(app, "")
        state["history_index"] = None
        _record_history(app, line)
        app.run_command(line)
    elif key == "tab":
        complete(app)
    elif key in ("up", "down"):
        matches = suggestions(app)
        state["result_cursor"] = max(0, min(max(0, len(matches) - 1), state["result_cursor"] + (-1 if key == "up" else 1)))
    elif key in ("pgup", "pgdn"):
        _history_move(app, key == "pgup")
    elif key in ("left", "right", "home", "end"):
        state["cursor"] = {"left": max(0, cursor - 1), "right": min(len(text), cursor + 1), "home": 0, "end": len(text)}[key]
    elif key in ("ctrl-left", "alt-b", "ctrl-right", "alt-f"):
        state["cursor"] = _word_left(text, cursor) if key in ("ctrl-left", "alt-b") else _word_right(text, cursor)
    elif key in ("ctrl-a", "ctrl-e"):
        state["cursor"] = 0 if key == "ctrl-a" else len(text)
    elif key in ("ctrl-w", "alt-backspace", "ctrl-backspace"):
        start = _word_left(text, cursor)
        _set_input(app, text[:start] + text[cursor:], start)
    elif key == "alt-d":
        end = _word_right(text, cursor)
        _set_input(app, text[:cursor] + text[end:], cursor)
    elif key in ("ctrl-u", "ctrl-k"):
        _set_input(app, text[cursor:] if key == "ctrl-u" else text[:cursor], 0 if key == "ctrl-u" else cursor)
    elif key in ("ctrl-z", "ctrl-y", "alt-u", "alt-r"):
        _undo(app, key in ("ctrl-y", "alt-r"))
    elif key == "backspace":
        if cursor:
            _set_input(app, text[:cursor - 1] + text[cursor:], cursor - 1)
    elif key == "delete":
        _set_input(app, text[:cursor] + text[cursor + 1:], cursor)
    elif key == "space" or len(key) == 1 and key.isprintable():
        char = " " if key == "space" else key
        _set_input(app, text[:cursor] + char + text[cursor:], cursor + 1)
        state["history_index"] = None


def _help_entries(app):
    k = app.keys_help
    page = app.tab
    common = [
        ("Navigation", "Tab / Shift-Tab / 1-9, 0", "Switch pages; 0 opens Research"),
        ("Navigation", ":back", "Restore the previous page, selection, filter, and scroll"),
        ("Navigation", ":forward / Alt-Right", "Return to a location after Back; a new destination clears Forward"),
        ("Navigation", ":jump / Ctrl-G", "Search cached jobs, runs, log files, saved views, workspaces, commands, and locations"),
        ("Navigation", ":location save NAME / open NAME", "Save and restore an exact page, source identity, filters, column sorts, and reading position"),
        ("Navigation", ":workspaces", "Search and open any Research workspace"),
        ("Navigation", "Ctrl-B", "Return to the previous page; :back also works in every terminal"),
        ("Navigation", k("up") + " / " + k("down"), "Move the current selection"),
        ("Navigation", "PgUp / PgDn / Home / End", "Move by a page, or to the first / last item"),
        ("Commands", k("palette"), "Editable fuzzy command palette; Tab completes names, IDs, and quoted paths"),
        ("Commands", "Left / Right / Home / End", "Move the command editing cursor"),
        ("Commands", "Ctrl-Left/Right or Alt-B/F; Ctrl-W or Alt-D", "Move by words; delete the previous or next word"),
        ("Commands", "Ctrl-A/E; Ctrl-U/K; Alt-U/R or Ctrl-Z/Y", "Move to the start/end; delete to the start/end; undo/redo edits in the command palette"),
        ("Commands", "Bracketed paste", "Insert text without execution; edit pasted multiline text, then press Enter to run one command"),
        ("Commands", "Up / Down", "Choose a command completion; PgUp / PgDn recalls command history"),
        ("Help", "/ then type", "Search this help; Backspace edits; Enter keeps results; Esc returns to the page"),
        ("Appearance", k("theme"), "Cycle themes; reader gives plain ASCII and static feedback"),
        ("Appearance", ":settings", "Preview appearance, sampling, mouse and clipboard preferences; Enter applies, Esc restores"),
        ("Appearance", ":keybindings", "Edit main-page keys, inspect conflicts, test a key, or restore default bindings"),
        ("Figures", ":explain COLUMN / :peek COLUMN", "Explain the selected field or inspect and copy its complete underlying value; metric NAME selects a published metric"),
        ("Workspace", ":density comfortable|compact|focused", "Choose spacing and information density"),
        ("Workspace", ":focus main|details / :maximize", "Focus and enlarge independently scrollable panels"),
        ("Workspace", ":layout save NAME / load NAME / split 60", "Save, restore, or resize a workspace arrangement"),
        ("Activity", ":activity / :task cancel", "Review retained notices and export paths; request cancellation of background work"),
        ("Projects", ":project PATH / :runs", "Discover a standard project and bind its exact run and execution attempt"),
        ("Projects", ":outputs / :artifact PATH", "Browse declared results and inspect bounded previews"),
        ("Data", k("refresh"), "Refresh scheduler sources and project files"),
        ("Exports", k("export_text") + " / " + k("export_csv") + " / " + k("export_json"), "Export the page, table, or selected job data"),
        ("Exports", ":export report", "Save a complete readable dashboard report"),
        ("Review", "Tab / Left / Right / Enter", "Choose Cancel or Confirm; the initial choice is Cancel"),
        ("Review", "Up / Down / PgUp / PgDn", "Review every action target without dismissing the dialog"),
        ("Review", "y / n / Esc", "Confirm explicitly, or keep all jobs unchanged"),
        ("Quit", k("quit"), "Quit; Esc closes overlays or clears a selection/filter"),
        ("Figures", "CPU% / EFF", "CPU% is the interval rate; EFF is CPU time divided by elapsed time times allocated cores"),
        ("Figures", "MEM% / GPU%", "MEM% is peak resident memory versus the request; GPU% is observed device utilization"),
        ("Figures", "accounting...", "A job left the live queue; terminal state is shown only after accounting confirms it"),
        ("Configuration", app.config_path or "tower --write-config", "Configuration is optional; --write-config creates documented defaults"),
    ]
    contextual = []
    if page in ("jobs", "history", "deps", "group"):
        contextual += [
            ("This page", k("details"), "Inspect the selected job and its scheduler steps"),
            ("This page", k("inspector") + " / :inspect", "Open the unified inspector with resources, evidence, steps, and files"),
            ("This page", k("log"), "Open logs for active, recent, or historical jobs"),
            ("This page", k("filter"), "Filter by job name, ID, partition, details, or #tag"),
            ("This page", k("sort") + " / " + k("reverse"), "Choose a sort key and reverse the order"),
            ("This page", k("mark") + " / " + k("mark_all") + " / " + k("unmark_all"), "Mark jobs; actions target marked jobs before the cursor job"),
            ("This page", k("pin") + " / :tag / :note", "Keep important jobs visible and attach annotations"),
            ("This page", k("cancel") + " / " + k("hold") + " / " + k("requeue"), "Review scheduler actions before sending them"),
            ("This page", k("resubmit"), "Clone the selected job; edit flags and review the tested submission"),
            ("This page", ":columns / :facet state=FAILED partition=gpu", "Choose columns and combine explicit field filters"),
            ("This page", ":savedview save NAME / load NAME", "Save or restore a useful filtered, sorted table"),
            ("This page", ":jobgroups", "Show or hide automatically detected launch groups"),
            ("This page", "g / u / U", "Group marked jobs; ungroup a collapsed group or selected members; clear marks"),
        ]
    elif page == "log":
        contextual += [
            ("Logs", "Up / Down / PgUp / PgDn", "Move the logical-line cursor; wrapped continuation rows select the same source line"),
            ("Logs", k("visual") + " then arrows, " + k("yank"), "Select and copy original log lines across pages; orange right-edge markers show the range"),
            ("Logs", k("copy_all") + " / :copy all", "Copy the complete file, independently of the retained tail or visible page"),
            ("Logs", k("visual_all"), "Select all retained log lines; use Y for the complete file"),
            ("Logs", k("log_files"), "Open the grouped file browser; / filters, Enter opens, Esc returns"),
            ("Logs", k("stderr") + " / " + k("log_file"), "Switch scheduler stdout/stderr or cycle registered files"),
            ("Logs", k("follow") + " / End", "Resume following the live tail; manual navigation pauses it"),
            ("Logs", k("filter") + " / " + k("find_next") + " / " + k("find_prev"), "Search with a regular expression; move to the next or previous match"),
            ("Logs", k("wrap"), "Wrap or clip long lines; copied content always retains original bytes"),
            ("Logs", k("bookmark") + " / " + k("bookmark_next"), "Bookmark the current line and cycle bookmarks"),
            ("Logs", k("less"), "Open the current file in less; q returns to Tower"),
            ("Logs", "Clipboard limits", "A complete private export remains available when a terminal clipboard is too small"),
            ("Logs", ":logview plain|split|json", "Choose the original, two-file split, or structured JSON presentation"),
            ("Logs", ":logpan N / :loggroup NAME / :logpreview", "Pan wide lines, fold file groups, or preview the selected file"),
        ]
    elif page == "research":
        contextual += [("Research", "Left / Right / :workspaces", "Choose a Research workspace"),
                       ("Research", "PgUp / PgDn / Home / End", "Scroll the workspace"),
                       ("Research", ":metrics / :passport / :artifacts", "Attach project streams and reporting files"),
                       ("Research", ":prepare / :submit", "Prepare offline, then review before submitting"),
                       ("Research", ":investigate JOBID", "Collect scheduler and application evidence for a job"),
                       ("Research", ":dashboard / :chart METRIC / :timeline", "Arrange metrics, inspect exact samples, and open event evidence"),
                       ("Research", ":diff JOBID JOBID / :diff passport LEFT RIGHT", "Compare runs and passports with readable field differences"),
                       ("Research", ":preflight SCRIPT --workdir DIR", "Edit effective batch resources and refresh local validation"),
                       ("Research", ":orchestrate workflow|scaling FILE --workdir DIR", "Prepare and review each execution node before submitting"),
                       ("Research", ":execution RECEIPT.json", "Inspect durable execution status, results, and reviewed recovery actions")]
        contextual += [("Research workspace", label, WORKSPACE_HELP.get(key, "Use the command palette to attach inputs and inspect evidence")) for key, label in RESEARCH_VIEWS]
    elif page == "analytics":
        contextual += [("Analytics", "Left / Right", "Switch job series, history, timeline, advice, and comparison"),
                       ("Analytics", k("days_more") + " / " + k("days_less"), "Choose a longer or shorter accounting window"),
                       ("Analytics", ":compare JOBID JOBID", "Compare resource use for explicit jobs")]
    elif page == "sources":
        contextual += [("Sources", k("source_toggle"), "Enable or disable the selected scheduler source"),
                       ("Sources", k("refresh"), "Retry and refresh all sources")]
    elif page == "nodes":
        contextual += [("Nodes", "Left / Right", "Switch allocated nodes and the cluster map")]
    from .table_sort import TABLE_KEYS
    if page in TABLE_KEYS:
        contextual += [
            ("Sorting", "Click a column header", "Cycle ascending, descending, then off; off removes only that column"),
            ("Sorting", "Header ^1 / v2", "The first chosen column has priority; later columns break ties, and numbers show the order"),
            ("Sorting", ":sortby [TABLE] COLUMN [asc|desc|off]", "Combine reversible column sorts from the keyboard; omitting the direction cycles that column"),
            ("Sorting", ":sortby [TABLE] clear", "Remove every column sort in that table and restore source order"),
        ]
        if page in ("jobs", "history", "group"):
            contextual.append(("Sorting", "JOBID", "Sort numeric IDs and array task numbers naturally, including 9 before 10 and _2 before _10"))
        if page == "jobs":
            contextual.append(("Sorting", ":sortby recent COLUMN asc|desc|off", "The Recents table keeps its own column priorities, independently of active jobs"))
    return contextual + registry.help_entries(app) + common


WORKSPACE_HELP = {"experiment": "Attach metrics with :metrics FILE; inspect progress and events",
                  "evidence": "Use :investigate JOBID to inspect scheduler and multi-file failure evidence",
                  "passport": "Use :passport show FILE or :passport compare LEFT RIGHT",
                  "artifacts": "Use :artifacts CONTRACT ROOT and :validate CONTRACT ROOT",
                  "submit": "Use :prepare SCRIPT --workdir DIR before :submit",
                  "workflow": "Inspect dependencies and review execution plans before submission",
                  "scaling": "Compare resource counts, elapsed time, and scaling efficiency"}


def _help_rows(app, width, ascii_):
    state = initialize(app)
    words = state["help_query"].casefold().split()
    entries = [entry for entry in _help_entries(app) if not words or all(word in " ".join(entry).casefold() for word in words)]
    content, section = [], None
    available = max(1, width - 12)
    for group, key, description in entries:
        if group != section:
            if content:
                content.append([("", "")])
            content.append([(" " + group, "bold")])
            section = group
        # Keep every instruction readable on small terminals instead of
        # dropping the tail of a fixed-width key/description table.
        content.append([("  " + clean(key, ascii_), "cyan")])
        for line in textwrap.wrap(clean(description, ascii_), width=available, break_long_words=True, break_on_hyphens=False) or [""]:
            content.append([("    " + line, "")])
    return content, len(entries)


def _help_key(app, key):
    state = initialize(app)
    if not state["help_mode_seen"]:
        state["help_mode_seen"] = True
    action = app.keymap.get(key)
    if key == "esc" or key in ("q", "?") and not state["help_searching"]:
        app.mode = "main"
        state["help_mode_seen"] = False
        return
    if key == "/":
        state["help_searching"] = True
    elif key == "enter":
        if state["help_searching"]:
            state["help_searching"] = False
        else:
            app.mode = "main"
            state["help_mode_seen"] = False
    elif key == "backspace":
        state["help_query"] = state["help_query"][:-1]
        app.scroll = 0
    elif key in ("up", "down", "pgup", "pgdn", "home", "end") or not state["help_searching"] and action in ("up", "down", "page_up", "page_down", "home", "end"):
        movement = {"up": "up", "down": "down", "pgup": "page_up", "pgdn": "page_down", "home": "home", "end": "end"}.get(key, action)
        top, page = app.scroll, state["help_page"]
        app.scroll = max(0, min(max(0, state["help_total"] - page),
            {"up": top - 1, "down": top + 1, "page_up": top - page, "page_down": top + page,
             "home": 0, "end": state["help_total"]}[movement]))
    elif key == "space" or len(key) == 1 and key.isprintable():
        state["help_query"] = (state["help_query"] + (" " if key == "space" else key))[:256]
        state["help_searching"] = True
        app.scroll = 0


def _confirm_state(app):
    state = initialize(app)
    token = id(app.confirm)
    if state["confirm_token"] != token:
        state.update(confirm_token=token, confirm_scroll=0, confirm_focus="cancel", confirm_total=None, confirm_cache=None,
                     confirm_hits=[], confirm_controls_visible=None)
    return state


def _wrap_exact(text, width):
    """Wrap reviewed argv without discarding spaces or wide characters."""
    width = max(1, width)
    if text.isascii():
        return textwrap.wrap(text, width=width, drop_whitespace=False, replace_whitespace=False,
                             break_on_hyphens=False) or [""]
    rows, chars, used = [], [], 0
    for chunk in re.findall(r"\s+|\S+", text):
        size = vlen(chunk)
        if chars and size <= width and used + size > width:
            rows.append("".join(chars))
            chars, used = [], 0
        for char in chunk:
            size = vlen(char)
            if chars and used + size > width:
                rows.append("".join(chars))
                chars, used = [], 0
            chars.append(char)
            used += size
    if chars:
        rows.append("".join(chars))
    return rows or [""]


def _confirm_rows(app, width, ascii_):
    state = _confirm_state(app)
    cache_key = (id(app.confirm), width, ascii_)
    if state["confirm_cache"] is not None and state["confirm_cache"][0] == cache_key:
        return state["confirm_cache"][1]
    data, lines = app.confirm, []
    action = str(data.get("action", "action"))
    if action == "submit" and isinstance(data.get("plan"), dict):
        plan = data["plan"]
        title = "Submit this reviewed batch script?"
        lines.append([(" workdir: " + clean(plan.get("workdir", ""), ascii_), "dim")])
        command = clean(plan.get("command", ""), ascii_, limit=1 << 20)
        for text in _wrap_exact(command, width - 12):
            lines.append([(" " + text, "cyan")])
        for issue in plan.get("issues", []):
            lines.append([(" " + clean(issue.get("message", ""), ascii_), "yellow")])
    elif action == "resubmit" and data.get("clone") is not None:
        clone = data["clone"]
        title = f"Resubmit {clean(clone.id, ascii_)} {clean(clone.name, ascii_)}?"
        lines.append([(" from the " + clean(clone.source, ascii_) + ", in " + clean(clone.workdir or "the current directory", ascii_) + ":", "dim")])
        for text in _wrap_exact(clean(clone.command(), ascii_, limit=1 << 20), width - 12):
            lines.append([(" " + text, "cyan")])
        flags = clone.flags()
        resources = "  ".join(f"{key} {flags[key]}" for key in ("partition", "cpus", "mem", "time", "gres", "nodes", "dependency") if key in flags)
        if resources:
            lines.append([(" " + clean(resources, ascii_), "")])
        lines.append([(" " + clean(clone.probe, ascii_), "yellow" if "would start" in clone.probe else "red")])
        for note in clone.notes:
            lines.append([(" " + clean(note, ascii_), "yellow")])
    else:
        jobs = data.get("jobs", [])
        title = f"{action.capitalize()} {len(jobs)} job{'s' if len(jobs) != 1 else ''}?"
        # Rows are cheap and bounded by the actual reviewed target inventory;
        # every target remains reachable, including positions beyond twelve.
        for index, job in enumerate(jobs):
            name = cut(clean(getattr(job, "name", ""), ascii_), max(8, width // 3), ascii_)
            job_state = "pending" if getattr(job, "pending", False) else clean(getattr(job, "state", "running"), ascii_).lower()
            lines.append([(f" {index + 1:>4}. {clean(job.id, ascii_):<12} {name}  {job_state} on {clean(getattr(job, 'partition', ''), ascii_)}", "")])
    for extra in data.get("review", [])[:512]:
        lines.append([(" " + clean(extra, ascii_), "")])
    result = title, lines
    state["confirm_cache"] = cache_key, result
    return result


def _confirm_key(app, key):
    state = _confirm_state(app)
    if key in ("y", "Y", "enter", "tab", "btab", "left", "right") and state["confirm_controls_visible"] is False:
        state["confirm_focus"] = "cancel"
        app.fail("Enlarge the terminal to review targets and Confirm / Cancel; n or Esc keeps the jobs")
        return
    if key in ("y", "Y"):
        app.finish_confirm(True)
    elif key in ("n", "N", "esc", "q"):
        app.finish_confirm(False)
    elif key in ("tab", "btab", "left", "right"):
        state["confirm_focus"] = "confirm" if state["confirm_focus"] == "cancel" else "cancel"
    elif key == "enter":
        app.finish_confirm(state["confirm_focus"] == "confirm")
    elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
        total = state["confirm_total"]
        if total is None:
            _, lines = _confirm_rows(app, getattr(app, "width", 120), getattr(app, "_ascii_cfg", False))
            total = len(lines)
        top, page = state["confirm_scroll"], state["confirm_page"]
        state["confirm_scroll"] = max(0, min(max(0, total - page),
            {"up": top - 1, "down": top + 1, "pgup": top - page, "pgdn": top + page,
             "home": 0, "end": total}[key]))


def handle_key(app, key):
    state = initialize(app)
    if app.mode != "help":
        state["help_mode_seen"] = False
    if app.mode == "palette":
        _palette_key(app, key)
        return True
    if app.mode == "help":
        _help_key(app, key)
        return True
    if app.mode == "confirm":
        _confirm_key(app, key)
        return True
    return False


def handle_mouse(app, y, x, button="left", shift=False):
    state = initialize(app)
    if app.mode not in ("help", "palette", "confirm"):
        return False
    if button != "left":
        return True
    if app.mode == "confirm":
        _confirm_state(app)
        choice = next((choice for row, start, end, choice in state.get("confirm_hits", [])
                       if row == y and start <= x < end), None)
        if choice is not None:
            app.finish_confirm(choice == "confirm")
    elif app.mode == "palette":
        index = next((index for row, index in state.get("result_hits", []) if row == y), None)
        if index is not None:
            state["result_cursor"] = index
    return True


def run_command(app, args):
    if not args or args[0] not in command_names():
        return False
    state = initialize(app)
    if args[0] == "help":
        state.update(help_query=" ".join(args[1:])[:256], help_searching=False)
        app.mode, app.scroll = "help", 0
    else:
        open_palette(app, " ".join(args[1:]))
    return True


def _palette_overlay(views, app, width, height):
    from . import modal_scrollbars as B
    state = _sync_input(app)
    matches = suggestions(app)
    page = max(1, height - 12)
    state["page"] = page
    index = max(0, min(state["result_cursor"], max(0, len(matches) - 1)))
    top = min(state["result_top"], max(0, len(matches) - page))
    if index < top:
        top = index
    elif index >= top + page:
        top = index - page + 1
    context = ("palette", app.palette_edit)
    logical, top = B.window(app, "modal:commands", top, len(matches), page,
                            context=context, focus=index)
    state.update(result_cursor=index, result_top=logical)
    # Show the insertion point directly; the terminal cursor can remain hidden
    # and screen-reader/ASCII modes retain the same editing behavior.
    cursor = state["cursor"]
    before, current, after = app.palette_edit[:cursor], app.palette_edit[cursor:cursor + 1] or " ", app.palette_edit[cursor + 1:]
    before, current, after = (value.replace("\n", "↵" if not views.g.ascii else "|").replace("\t", " ") for value in (before, current, after))
    visible = max(1, width - 14)
    start = max(0, cursor - visible + 1)
    before = before[start:]
    lines = [[(" :", "magenta"), (clean(before, views.g.ascii), ""), (clean(current, views.g.ascii), "rev+bold"),
              (clean(after[:visible], views.g.ascii), "")], [("", "")]]
    for row_index, row in enumerate(matches[top:top + page], top):
        selected = row_index == index
        style = "sel" if selected else "cyan"
        marker = "> " if views.g.ascii else "› "
        lines.append([((marker if selected else "  ") + clean(row["value"], views.g.ascii, limit=MAX_INPUT), style),
                      ("  " + cut(clean(row["description"], views.g.ascii), max(0, width - len(row["value"]) - 12), views.g.ascii), style if selected else "dim")])
    if not matches:
        lines.append([(" No completion matches. Enter runs the current command.", "dim")])
    status, hint = validation(app)
    lines += [[("", "")], [(" " + clean(hint, views.g.ascii), "yellow" if status else "dim")],
              [(" " + clean(state["paste_notice"], views.g.ascii), "cyan")] if state["paste_notice"] else [(" Alt-B/F moves words; Alt-U/R undoes/redoes", "dim")],
              [(" Up/Down choose  Tab completes  Enter runs  Esc closes", "dim")],
              [(" Left/Right edit  PgUp/PgDn recalls history", "dim")]]
    result = box(views.g, lines, width, height, "Commands")
    state["result_hits"] = [(result[index + 3][0], top + index) for index in range(min(page, len(matches) - top))
                            if index + 3 < len(result) - 1]
    return B.boxed(app, "modal:commands", result, start=2, count=len(matches), page=page,
                   target=logical, painted=top, setter=lambda value: state.update(result_top=value),
                   context=context)


def overlay(views, snap, app, width, height):
    from . import modal_scrollbars as B
    state = initialize(app)
    if app.mode == "palette":
        return _palette_overlay(views, app, width, height)
    if app.mode == "help":
        content, count = _help_rows(app, width, views.g.ascii)
        page = max(1, height - 10)
        state["help_page"], state["help_total"] = page, len(content)
        app.scroll = max(0, min(app.scroll, max(0, len(content) - page)))
        query = state["help_query"]
        context = ("help", app.tab, query, width)
        app.scroll, painted = B.window(app, "modal:help", app.scroll, len(content), page, context=context)
        lines = [[(" Search: ", "dim"), (clean(query, views.g.ascii) or "(/ to search)", "cyan")], [("", "")]]
        lines += content[painted:painted + page] or [[(" No matching instructions. Backspace edits the search.", "yellow")]]
        lines += [[("", "")], [(f" {count} instructions  rows {app.scroll + 1 if content else 0}-{min(len(content), app.scroll + page)}/{len(content)}", "dim")],
                  [(" Up/Down/PgUp/PgDn scroll  / searches  Esc closes", "dim")]]
        result = box(views.g, lines, width, height, "Help: " + app.tab.capitalize() + " keys")
        return B.boxed(app, "modal:help", result, start=2, count=len(content), page=page,
                       target=app.scroll, painted=painted, setter=lambda value: setattr(app, "scroll", value),
                       context=context)
    if app.mode == "confirm":
        state = _confirm_state(app)
        title, content = _confirm_rows(app, width, views.g.ascii)
        capacity = max(0, height - 4) if width >= 4 and height >= 3 else 0
        show_title, show_status, show_hint, spacing = capacity >= 2, capacity >= 6, capacity >= 4, capacity >= 8
        overhead = 1 + int(show_title) + int(show_status) + int(show_hint) + 2 * int(spacing)
        page = max(0, capacity - overhead)
        state["confirm_page"], state["confirm_total"] = max(1, page), len(content)
        top = max(0, min(state["confirm_scroll"], max(0, len(content) - page)))
        context = ("confirm", state["confirm_token"], width)
        logical, top = B.window(app, "modal:confirmation", top, len(content), page, context=context)
        state["confirm_scroll"] = logical
        lines = [[(" " + title, "bold")]] if show_title else []
        if spacing:
            lines.append([("", "")])
        data_start = len(lines)
        lines += content[top:top + page]
        focus = state["confirm_focus"]
        if spacing:
            lines.append([("", "")])
        lines.append([(" [ Cancel ] ", "sel" if focus == "cancel" else "dim"),
                      (" [ Confirm ] ", "sel" if focus == "confirm" else "yellow+bold")])
        if show_status:
            lines.append([(f" Review {top + 1 if content and page else 0}-{min(len(content), top + page)}/{len(content)}  Up/Down/PgUp/PgDn", "dim")])
        if show_hint:
            lines.append([(" Tab chooses  Enter activates  y confirms  n/Esc cancels", "dim")])
        result = box(views.g, lines, width, height, "Review action")
        state["confirm_hits"] = []
        for y, x, row in result:
            offset = x
            for text, _ in row:
                if "[ Cancel ]" in text or "[ Confirm ]" in text:
                    state["confirm_hits"].append((y, offset, offset + vlen(text), "confirm" if "[ Confirm ]" in text else "cancel"))
                offset += vlen(text)
        state["confirm_controls_visible"] = {hit[3] for hit in state["confirm_hits"]} == {"cancel", "confirm"} and (page > 0 or not content)
        if not state["confirm_controls_visible"]:
            state["confirm_focus"] = "cancel"
            state["confirm_hits"] = []
        return B.boxed(app, "modal:confirmation", result, start=data_start, count=len(content), page=page,
                       target=logical, painted=top, setter=lambda value: state.update(confirm_scroll=value),
                       context=context, header=0 if data_start else -1)
    return None
