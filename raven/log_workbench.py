"""Terminal log presentation and job-scoped evidence drilldown.

File metadata, previews and alternate views use ResearchHub's one worker.
Rendering consumes published snapshots only. Presentation never changes raw
LogSession bytes or the source selected by copy/yank commands.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
import json
import math
import time

from . import layout as L, scrollbars as S
from .research import clean
from .remote import LocalFiles
from . import log_presentation as presentation

META_TTL = 30.0
MAX_META_FILES = 32
PREVIEW_BYTES = 8192
VIEW_BYTES = 65536
MAX_VIEW_ROWS = 240


def initialize(app):
    app.log_workbench_state = dict(view="plain", pan=0, scroll=0, collapsed=[],
                                   current_group="", preview=False, cache=OrderedDict(),
                                   pending=None, generation=0, citation=None,
                                   pan_cache=OrderedDict(), pan_context=None, pan_source=None,
                                   align=True, diff_sources=None, ignore_time=False, json_filter=("", ""),
                                   json_collapsed=[], fold_expanded=[], rows=[], cursor=0,
                                   unread=0, unread_byte=None, observation=None, mouse_rows={}, split_ratio=50)


def _state(app):
    if not isinstance(getattr(app, "log_workbench_state", None), dict):
        initialize(app)
    return app.log_workbench_state


def restore(app, state):
    target = _state(app)
    state = state if isinstance(state, dict) else {}
    if state.get("view") in ("plain", "json", "split", "diff", "fold"):
        target["view"] = state["view"]
    # Horizontal position belongs to an open source, not to all future logs.
    # Ignore offsets saved by older versions so every launch starts at column 0.
    target["pan"], target["pan_source"], target["pan_context"] = 0, None, None
    target["pan_cache"].clear()
    collapsed = state.get("collapsed", [])
    if isinstance(collapsed, list):
        target["collapsed"] = [v for v in collapsed[:256] if isinstance(v, str) and len(v) <= 160 and v.isprintable()]
    target["preview"] = state.get("preview") is True
    ratio = state.get("split_ratio")
    if isinstance(ratio, int) and not isinstance(ratio, bool):
        target["split_ratio"] = max(20, min(80, ratio))


def save(app):
    state = _state(app)
    result = {key: state[key] for key in ("view", "collapsed", "preview")}
    if state.get("split_ratio", 50) != 50:
        result["split_ratio"] = state["split_ratio"]
    return result


def sync_source(app, path):
    """Start another job/run/file at its left edge; keep append redraws steady."""
    state = _state(app)
    binding = (getattr(app, "project_state", {}) or {}).get("binding") or {}
    source = (getattr(app, "log_job", None), binding.get("run_root"), binding.get("run_id"), path)
    if source != state["pan_source"]:
        state["pan"], state["pan_source"], state["pan_context"] = 0, source, None
        state["pan_cache"].clear()


def _sync_selected_source(app):
    resolve = getattr(app, "resolve_log_path", None)
    if resolve is not None:
        path = resolve()
        if path:
            # A source can change between frames (e.g. stderr then Right).
            sync_source(app, path)


def command_names():
    return ["logview", "logpan", "loggroup", "logpreview", "logalign", "logdiff", "logjson", "logfold", "logunread"]


def visible_entries(app, entries):
    """Filter collapsed file groups without changing their catalog identities."""
    collapsed = set(_state(app)["collapsed"])
    return [entry for entry in entries if entry.get("group", "Application") not in collapsed]


def _entries(app):
    entries = list(getattr(app.logs, "entries", []))
    store = getattr(app, "store", None)
    binding = (getattr(app, "project_state", {}) or {}).get("binding")
    if binding and not entries:
        # Bound declarations are already validated and published. Paired views
        # can use them before the user opens the native catalog, without a read.
        from .project_ui import log_entries
        entries = log_entries(app)
    if store is not None and hasattr(app, "log_target") and not binding:
        from .views import stdout_path
        snap = store.snapshot()
        job = app.log_target(snap)
        if job is not None:
            details = snap.get("details", {}).get(job.id, {})
            for key, field in (("stdout", "StdOut"), ("stderr", "StdErr")):
                declared = details.get(field)
                if not isinstance(declared, str) or not declared.strip() or declared.strip().casefold() in {"(null)", "n/a", "unknown", "none"}:
                    # Expanding an explicit scheduler path is pure. Guessing a
                    # local fallback probes the filesystem and belongs to the
                    # catalog worker, never to a redraw of cached entries.
                    continue
                path = stdout_path(job, details, _backend(app), field)
                if path and not any(entry["path"] == path for entry in entries):
                    entries.append(dict(id="scheduler." + key, path=path, label=key, group="Scheduler", source="scheduler"))
    if app.logs.entry and not any(e["path"] == app.logs.entry["path"] for e in entries):
        entries.insert(0, dict(app.logs.entry))
    return entries


def _selected_group(app):
    state = _state(app)
    entries = app.log_entries() if hasattr(app, "log_entries") else visible_entries(app, _entries(app))
    index = max(0, min(app.logs.browser_cursor, len(entries) - 1)) if entries else 0
    return state["current_group"] or (entries[index].get("group", "Application") if entries else "")


def _toggle_group(app, group):
    state = _state(app)
    if not group:
        app.fail("Select a log group or use :loggroup GROUP.")
        return
    collapsed = state["collapsed"]
    if group in collapsed:
        collapsed.remove(group)
        message = "expanded"
    else:
        collapsed.append(group)
        message = "collapsed"
    state["current_group"] = group
    app.logs.browser_cursor, app.logs.browser_top = 0, 0
    app.say(f"{message} {group}; :loggroup all expands every group")


def run_command(app, args):
    alternate = (app.tab == "log" and app.mode == "main" and not app.logs.browser and _state(app)["view"] != "plain")
    if args and alternate:
        if args[0] in ("logmark", "bookmark"):
            app.fail("Press o or Esc to open the original source before setting a log mark")
            return True
        if args[0] == "copy" and args[1:] != ["all"]:
            app.fail("Open the original source view before selecting or copying source lines; :copy all copies the exact file")
            return True
    if not args or args[0] not in command_names():
        return False
    state, command, values = _state(app), args[0], args[1:]
    try:
        if command == "logview":
            if len(values) != 1 or values[0] not in ("plain", "json", "split", "diff", "fold"):
                raise ValueError("logview plain|json|split|diff|fold")
            state["view"], state["scroll"], state["cursor"] = values[0], 0, 0
            app.tab = "log"
            app.say("Log view " + values[0] + "; Esc returns to original lines")
        elif command == "logalign":
            if values not in (["on"], ["off"]):
                raise ValueError("logalign on|off")
            state["align"] = values == ["on"]
            state["view"], state["scroll"], state["cursor"], app.tab = "split", 0, 0, "log"
            app.say("Timestamp alignment " + values[0] + "; missing or ambiguous timing is labelled")
        elif command == "logdiff":
            ignore = bool(values and values[-1] in ("ignore-time", "exact"))
            mode = values.pop() if ignore else "exact"
            if len(values) not in (0, 2):
                raise ValueError("logdiff [LEFT_ID RIGHT_ID] [ignore-time|exact]")
            entries = _entries(app)
            if values:
                selected = [next((entry for entry in entries if entry.get("id") == value), None) for value in values]
                if any(entry is None for entry in selected) or selected[0]["path"] == selected[1]["path"]:
                    raise ValueError("Choose two distinct registered source IDs from the log file catalog (O)")
                state["diff_sources"] = [dict(entry) for entry in selected]
            else:
                state["diff_sources"] = None
            state["ignore_time"], state["view"], state["scroll"], state["cursor"], app.tab = mode == "ignore-time", "diff", 0, 0, "log"
            app.say("Comparing registered log files; " + mode + "; [] choose the original copy source")
        elif command == "logjson":
            if values == ["clear"]:
                state["json_filter"] = ("", "")
            elif len(values) >= 2 and len(values[0]) <= 128 and len(" ".join(values[1:])) <= 512:
                state["json_filter"] = (values[0], " ".join(values[1:]))
            else:
                raise ValueError("logjson FIELD VALUE | logjson clear (dot-separated fields)")
            state["view"], state["scroll"], state["cursor"], app.tab = "json", 0, 0, "log"
            app.say("Structured log filter updated; Enter folds or expands the selected node")
        elif command == "logfold":
            if values not in (["on"], ["off"]):
                raise ValueError("logfold on|off")
            state["view"], state["scroll"], state["cursor"], app.tab = "fold" if values == ["on"] else "plain", 0, 0, "log"
            app.say("Repeated log messages " + ("folded; Enter expands the selected group" if values == ["on"] else "expanded"))
        elif command == "logunread":
            if values:
                raise ValueError("logunread (visit the first unread retained line)")
            buf = getattr(app.logs, "buffers", {}).get(app.logs.path)
            if buf is None:
                buf = getattr(app.logs, "cache", {}).get(app.logs.path)
            if not first_unread(app, buf):
                app.fail("No unread location is available in the current retained window")
        elif command == "logpan":
            if len(values) != 1 or not values[0].isascii():
                raise ValueError("logpan COLUMNS (0 resets; use +/- to move)")
            number = int(values[0])
            _sync_selected_source(app)
            state["pan"] = max(0, min(100000, state["pan"] + number if values[0].startswith(("+", "-")) else number))
            app.logs.wrap = False
            app.say(f"Horizontal offset {state['pan']} display columns")
        elif command == "loggroup":
            if values == ["all"]:
                state["collapsed"], state["current_group"] = [], ""
                app.say("Every log group expanded")
            else:
                _toggle_group(app, " ".join(values) if values else _selected_group(app))
        elif command == "logpreview":
            if values and values not in (["on"], ["off"]):
                raise ValueError("logpreview [on|off]")
            state["preview"] = values == ["on"] if values else not state["preview"]
            state["generation"] += 1
            app.say("Log previews " + ("on" if state["preview"] else "off"))
    except (ValueError, TypeError) as exc:
        app.fail(str(exc))
    return True


def handle_key(app, key):
    if app.tab != "log" or app.mode != "main":
        return False
    state = _state(app)
    action = getattr(app, "keymap", {}).get(key, key)
    movement = {"page_up": "pgup", "page_down": "pgdn"}.get(action, action)
    if getattr(app, "keymap", {}).get(key) == "refresh":
        state["cache"].clear()
        state["generation"] += 1
    if app.logs.browser:
        if key in ("up", "down", "pgup", "pgdn", "home", "end"):
            S.resume(app, "logs:catalog")
            from .job_selection import resume_lines
            resume_lines(app)
            state["current_group"] = ""
        if key == "space":
            _toggle_group(app, _selected_group(app))
            return True
        return False
    if state["view"] != "plain":
        if key == "esc" or action == "clear":
            state["view"], state["scroll"] = "plain", 0
            app.say("Original log lines")
            return True
        if movement in ("up", "down", "pgup", "pgdn", "home", "end"):
            S.resume(app, "logs:workbench")
            from .job_selection import resume_lines
            resume_lines(app)
            page = state.get("viewport_page", max(1, getattr(app, "height", 24) - 14))
            count = len(state["rows"])
            step = {"up": -1, "down": 1, "pgup": -max(1, page - 1), "pgdn": max(1, page - 1), "home": -count, "end": count}[movement]
            state["cursor"] = max(0, min(max(0, count - 1), state["cursor"] + step))
            state["scroll"] = min(state["scroll"], state["cursor"])
            if state["cursor"] >= state["scroll"] + page:
                state["scroll"] = state["cursor"] - page + 1
            return True
        if key in ("enter", "space") and state["view"] in ("json", "fold"):
            from .job_selection import lines_cleared
            if lines_cleared(app):
                app.say("Select a log row with arrows or a click before expanding it")
                return True
            rows = state["rows"]
            item = rows[min(state["cursor"], len(rows) - 1)] if rows else {}
            if item.get("expandable"):
                target = state["json_collapsed"] if state["view"] == "json" else state["fold_expanded"]
                if item["node"] in target:
                    target.remove(item["node"])
                else:
                    target.append(item["node"])
                state["cursor"] = max(0, state["cursor"])
            return True
        if key in ("v", "y", "V") or action in ("visual", "visual_all", "yank"):
            state["view"] = "plain"
            app.say("Original source view; use v to select its raw lines, then y to copy")
            return True
        if action in ("bookmark", "bookmark_next", "find_next", "find_prev"):
            state["view"] = "plain"
            app.say("Original source view for bookmarks and source search")
            return False
        if key == "o" and state["view"] in ("json", "fold", "diff"):
            from .job_selection import lines_cleared
            if lines_cleared(app):
                app.say("Select a log row with arrows or a click before opening its source")
                return True
            rows = state["rows"]
            item = rows[min(state["cursor"], len(rows) - 1)] if rows else {}
            source = item.get("original", item.get("left"))
            if source is not None:
                path = item.get("source_path", app.logs.path)
                if path != app.logs.path:
                    from .log_tools import before_source_change
                    before_source_change(app)
                    entry = next((entry for entry in _entries(app) if entry["path"] == path), None)
                    if entry is not None:
                        app.logs.clear_selection(reset_cursor=True)
                        app.log_selection_expected = False
                        app.logs.entry, app.logs.path, app.logs.top = dict(entry), "", None
                state["citation"] = dict(path=path, line=source + 1, line_basis="tail-relative",
                                          excerpt_line=item.get("source_text", ""), tail_distance=item.get("tail_distance"),
                                          file_identity=item.get("file_identity"))
                state["view"] = "plain"
                app.say("Original source view; select and copy its unchanged lines")
            return True
        if key in ("[", "]") and state["view"] in ("split", "diff"):
            entries = state["diff_sources"] or _split_entries(app) if state["view"] == "diff" else _split_entries(app)
            if len(entries) == 2:
                entry = entries[0 if key == "[" else 1]
                from .log_tools import before_source_change
                before_source_change(app)
                app.logs.clear_selection(reset_cursor=True)
                app.log_selection_expected = False
                app.logs.entry, app.logs.path, app.logs.top = dict(entry), "", None
                app.say("Copy source: " + entry.get("label", entry["path"]))
            return True
    if key in ("left", "right"):
        _sync_selected_source(app)
        state["pan"] = max(0, min(100000, state["pan"] + (-8 if key == "left" else 8)))
        app.logs.wrap = False
        return True
    return False


def handle_mouse(app, y, x, button="left", shift=False):
    state = _state(app)
    if app.tab != "log" or app.mode != "main" or app.logs.browser or state["view"] == "plain":
        return False
    if button == "left" and not shift:
        for row, _, value in state.get("control_hits", []):
            if row == y and value["left"] <= x < value["right"]:
                handle_key(app, value["action"][1])
                return True
    # Alternate panels cover the original hit map. Never select a hidden raw
    # line through the visible JSON, folded, split, or diff presentation.
    if button in ("left", "double") and not shift and y in state["mouse_rows"]:
        left, right, index = state["mouse_rows"][y]
        if left <= x < right:
            from .job_selection import resume_lines
            resume_lines(app)
            state["cursor"] = index
            if button == "double":
                handle_key(app, "enter")
    return True


def display_line(app, line):
    """Pan by terminal columns; never split wide characters or alter raw bytes."""
    state = _state(app)
    offset = state["pan"]
    if not offset:
        return line
    context = (app.logs.path, app.logs._buffer_token, offset)
    if state["pan_context"] != context:
        state["pan_context"] = context
        state["pan_cache"].clear()
    cached = state["pan_cache"].get(id(line))
    budget = max(1024, min(VIEW_BYTES, (getattr(app, "width", 120) + 16) * max(1, app.logs.page) * 2))
    if cached and cached[0] is line:
        return line[cached[1]:cached[1] + budget]
    used, index = 0, 0
    for index, char in enumerate(line):
        used += L.vlen(char)
        if used >= offset:
            break
    position = index + 1 if line else 0
    # An accent belongs to the preceding glyph, which panning just removed.
    while position < len(line) and L.vlen(line[position]) == 0:
        position += 1
    state["pan_cache"][id(line)] = (line, position)
    state["pan_cache"].move_to_end(id(line))
    while len(state["pan_cache"]) > 256:
        state["pan_cache"].popitem(last=False)
    return line[position:position + budget]


def status_label(app):
    state = _state(app)
    labels = [f"pan {state['pan']} (:logpan 0 resets)"] if state["pan"] else []
    if state["unread"]:
        labels.append(f"{state['unread']} unread lines (:logunread first; End follows)")
    return " | ".join(labels)


def observe_buffer(app, buf):
    """Observe published bytes once. Rendering and navigation perform no reads."""
    state = _state(app)
    if buf is None or buf.error or getattr(buf, "loading", False):
        return
    identity = (buf.path, buf.ident, buf.reloads)
    prior = state["observation"]
    current = dict(identity=identity, size=buf.size, complete=buf.size - len(buf._partial_raw), skipped=buf.skipped_bytes)
    if prior is None or prior["identity"] != identity or buf.size < prior["size"]:
        state["unread"], state["unread_byte"] = 0, None
    elif app.logs.top is None:
        state["unread"], state["unread_byte"] = 0, None
    elif buf.size > prior["size"]:
        if state["unread_byte"] is None:
            state["unread_byte"] = prior["complete"]
        # Recount the retained range so completing an already unread partial
        # line does not count the same source line twice.
        end = buf.size - len(buf._partial_raw)
        unread = 0
        for length in reversed(buf._line_bytes):
            if end <= state["unread_byte"]:
                break
            unread += 1
            end -= length
        state["unread"] = unread + int(bool(buf._partial_raw) and buf.size > state["unread_byte"])
    state["observation"] = current
    state["last_buffer"] = buf


def first_unread(app, buf=None):
    state = _state(app)
    buf = buf or state.get("last_buffer")
    target = state["unread_byte"]
    if buf is None or target is None or buf.path != app.logs.path or target < buf.skipped_bytes:
        return False
    position = buf.skipped_bytes
    for index, length in enumerate(buf._line_bytes):
        if position + length > target:
            app.logs.goto(index, buf)
            state["view"] = "plain"
            app.say(f"First unread retained line {index + 1}; End resumes following")
            return True
        position += length
    if buf._partial_raw and target <= position:
        app.logs.goto(len(buf.lines), buf)
        state["view"] = "plain"
        return True
    return False


def _backend(app):
    return app.files or app.logs.files or LocalFiles()


def _snapshot_tail(files, path, limit):
    """Read a stable exact backend; no local fallback for remote errors."""
    before = files.snapshot_stat(path) if hasattr(files, "snapshot_stat") else None
    if type(files) is LocalFiles:
        from .artifacts import read_local_tail
        raw, size, stable = read_local_tail(path, max_bytes=limit)
        if not stable:
            raise ValueError("File changed during inspection; refresh to retry")
    else:
        raw, size = files.tail(path, limit)
    if not isinstance(raw, bytes) or len(raw) > limit or type(size) is not int or size < len(raw):
        raise ValueError("Invalid or oversized file adapter reply")
    after = files.snapshot_stat(path) if before is not None else None
    if before is not None and before != after:
        raise ValueError("File changed during inspection; refresh to retry")
    truncated = size > len(raw)
    if truncated:
        newline = raw.find(b"\n")
        raw = raw[newline + 1:] if newline >= 0 else b""
    return raw, size, before, truncated


def _request(app, key, fn, *, ttl=META_TTL):
    """Coalesce presentation requests into the shared existing single worker."""
    state = _state(app)
    from .refresh_rate import file_interval, multiplier
    ttl = file_interval(ttl, multiplier(app), remote=bool(getattr(app.logs.files, "remote", False)))
    cached = state["cache"].get(key)
    if cached and time.monotonic() - cached[0] < ttl:
        state["cache"].move_to_end(key)
        return cached[1]
    hub = getattr(app, "research", None)
    if state["pending"] is not None or hub is None:
        return cached[1] if cached else None
    token = (state["generation"], key)
    state["pending"] = token
    def complete(result):
        current = _state(app)
        if current["pending"] == token:
            current["pending"] = None
        if token[0] != current["generation"]:
            return
        value = {"error": clean(result)} if isinstance(result, Exception) else result
        current["cache"][key] = (time.monotonic(), value)
        current["cache"].move_to_end(key)
        while len(current["cache"]) > 8:
            current["cache"].popitem(last=False)
    try:
        admitted = hub.start_task(fn, complete)
    except Exception:
        state["pending"] = None
        raise
    if not admitted:
        state["pending"] = None
    return cached[1] if cached else None


def _metadata(app, entries):
    state, files = _state(app), _backend(app)
    selected = app.log_entries() if hasattr(app, "log_entries") else entries
    cursor = max(0, min(app.logs.browser_cursor, len(selected) - 1)) if selected else 0
    selected_path = selected[cursor]["path"] if selected else ""
    ordered = sorted(entries, key=lambda entry: entry["path"] != selected_path)[:MAX_META_FILES]
    paths = tuple(sorted(entry["path"] for entry in ordered))
    preview = selected_path if state["preview"] else ""
    key = ("meta", id(files), paths, preview)
    def read():
        values = {}
        for path in paths:
            try:
                meta = files.snapshot_stat(path) if hasattr(files, "snapshot_stat") else {"size": files.stat(path)[0]}
                item = dict(meta)
                if path == preview:
                    raw, size, identity, truncated = _snapshot_tail(files, path, PREVIEW_BYTES)
                    item.update(preview=[clean(line, limit=500) for line in raw.decode("utf-8", "replace").splitlines()[-8:]],
                                truncated=truncated, size=size, file_identity=identity)
                values[path] = item
            except (OSError, ValueError, TypeError) as exc:
                values[path] = {"error": clean(exc)}
        return {"files": values, "omitted": max(0, len(entries) - len(paths))}
    return _request(app, key, read)


def _size(value):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def _updated(meta):
    value = meta.get("updated")
    if isinstance(value, (tuple, list)) and value:
        if isinstance(value[0], int):
            try:
                return datetime.fromtimestamp(value[0] / 1e9).strftime("%m-%d %H:%M")
            except (ValueError, OverflowError, OSError):
                return ""
        if isinstance(value[0], str):
            return value[0][:19]
    return ""


def render_browser(views, snap, app, width, height, legacy_rows, hits):
    """A grouped catalog with cached metadata, expandable previews and hit maps."""
    entries = _entries(app)
    if not entries:
        return legacy_rows, hits
    state, glyphs = _state(app), views.g
    data = _metadata(app, entries) or {}
    files = data.get("files", {})
    filtered = [entry for entry in entries if not app.logs.file_filter or any(app.logs.file_filter.casefold() in str(entry.get(k, "")).casefold() for k in ("label", "group", "path", "description"))]
    selected_entries = app.log_entries() if hasattr(app, "log_entries") else visible_entries(app, filtered)
    cursor = max(0, min(app.logs.browser_cursor, len(selected_entries) - 1)) if selected_entries else 0
    from .job_selection import lines_cleared
    selected_id = selected_entries[cursor]["id"] if selected_entries and not lines_cleared(app) else None
    grouped = OrderedDict()
    for entry in filtered:
        grouped.setdefault(entry.get("group", "Application"), []).append(entry)
    title = legacy_rows[0] if legacy_rows else L.rule(glyphs, width, "log files")
    rows = [title, [(f" {len(filtered)} files  Space folds group  :loggroup all expands  :logpreview toggles preview", "dim")]]
    warnings = [row for row in legacy_rows[1:5] if any("yellow" in style for _, style in row)]
    rows.extend(warnings[:2])
    if data.get("omitted"):
        rows.append([(f" Metadata inspects at most {MAX_META_FILES} files, selected first; {data['omitted']} omitted.", "yellow")])
    body, mapped, selected_row = [], [], 0
    for group, items in grouped.items():
        collapsed = group in state["collapsed"]
        indicator = ">" if collapsed else "v" if glyphs.ascii else "▾"
        mapped.append((len(body), "log_group", group))
        body.append([(" " + clean(f"{indicator} {group} ({len(items)})", glyphs.ascii), "cyan+bold")])
        if collapsed:
            continue
        for entry in items:
            selected = entry["id"] == selected_id
            if selected:
                selected_row = len(body)
            meta = files.get(entry["path"], {})
            suffix = " | " + clean(meta["error"], glyphs.ascii, 100) if meta.get("error") else " | " + _size(meta["size"]) + " " + _updated(meta) if type(meta.get("size")) is int else " | metadata pending"
            mapped.append((len(body), "log_file", entry["id"]))
            body.append([("   " + (glyphs.cursor if selected else " ") + " " + clean(entry.get("label", entry["path"]) + " [" + entry["id"] + "]", glyphs.ascii) + suffix, "rev+bold" if selected else "")])
            mapped.append((len(body), "log_file", entry["id"]))
            body.append([("     " + clean(entry["path"], glyphs.ascii), "dim")])
            if selected and state["preview"]:
                for line in meta.get("preview", []):
                    body.append([("       " + clean(line, glyphs.ascii), "dim")])
                if meta.get("truncated"):
                    body.append([("       Preview is a bounded tail; open the file for the original view.", "yellow")])
    available = len(body) if height is None else max(1, height - len(rows))
    scroll_context = (getattr(app, "log_job", None), tuple((entry.get("id"), entry.get("path")) for entry in filtered),
                      app.logs.file_filter, tuple(state["collapsed"]), state["preview"], width)
    top = max(0, min(app.logs.browser_top, max(0, len(body) - available)))
    if S.manual(app, "logs:catalog", context=scroll_context) is None:
        if selected_row < top:
            top = selected_row
        elif selected_row >= top + available:
            top = max(0, selected_row - available + 1)
    app.logs.browser_top, app.logs.browser_page = top, max(1, available // 2)
    from .scrolling import viewport
    painted = viewport(app, "logs:catalog", top, len(body), available,
                       context=scroll_context, immediate=height is None)
    prefix = len(rows)
    output = [L.clip_row(row, width) for row in rows + body[painted:painted + available]]
    if height is not None and width >= 6:
        if len(body) > available:
            output[0] = L.clip_row([("    ", "")] + output[0], width)
        S.register(app, "logs:catalog", (prefix, 0, prefix + available, width),
                   len(body), available, top, painted,
                   lambda value: setattr(app.logs, "browser_top", value),
                   context=scroll_context, header=(0, 0, width))
    return output, [(y - painted + prefix, kind, key) for y, kind, key in mapped if painted <= y < painted + available]


def _split_entries(app):
    entries = _entries(app)
    out = next((entry for entry in entries if entry.get("id") == "scheduler.stdout" or entry.get("role") == "stdout" or entry.get("label") == "stdout"), None)
    err = next((entry for entry in entries if entry.get("id") == "scheduler.stderr" or entry.get("role") == "stderr" or entry.get("label") == "stderr"), None)
    if out and err and out["path"] != err["path"]:
        return [out, err]
    selected = app.logs.entry or next((entry for entry in entries if entry["path"] == app.logs.path), None)
    result = [selected] if selected else []
    result += [entry for entry in entries if not selected or entry["path"] != selected["path"]]
    return result[:2]


def _alternate(app):
    state, files = _state(app), _backend(app)
    view = state["view"]
    entries = (state["diff_sources"] or _split_entries(app)) if view in ("split", "diff") else [dict(path=app.logs.path, label=(app.logs.entry or {}).get("label", "selected log"))] if app.logs.path else []
    key = ("alternate", id(files), view, tuple(entry["path"] for entry in entries))
    def read():
        sources = []
        for entry in entries:
            path = entry["path"]
            try:
                raw, size, identity, truncated = _snapshot_tail(files, path, VIEW_BYTES)
                # Logical source rows use LF, exactly as LogBuffer does. Bare
                # CR progress updates must not manufacture extra row indexes.
                all_raw = raw.split(b"\n")
                if all_raw and all_raw[-1] == b"":
                    all_raw.pop()
                all_lines = [line.decode("utf-8", "replace").rstrip("\r") for line in all_raw]
                line_offset = max(0, len(all_lines) - MAX_VIEW_ROWS)
                lines = all_lines[-MAX_VIEW_ROWS:]
                sources.append(dict(path=path, label=entry.get("label", path), lines=lines, size=size,
                                    truncated=truncated, omitted_lines=line_offset, file_identity=identity))
            except (OSError, ValueError, TypeError) as exc:
                sources.append(dict(path=path, label=entry.get("label", path), lines=[], error=clean(exc)))
        return {"sources": sources}
    return _request(app, key, read, ttl=2.0)


def _presentation_rows(state, sources):
    key = (tuple((item["path"], id(item)) for item in sources), state["view"], state["align"],
           state["ignore_time"], state["json_filter"], tuple(state["json_collapsed"]), tuple(state["fold_expanded"]))
    if state.get("presentation_key") == key:
        return state["rows"], state.get("presentation_note", "")
    view = state["view"]
    item = sources[0]
    note = ""
    if view == "split" and len(sources) == 2:
        if state["align"]:
            pairs, note = presentation.aligned(item["lines"], sources[1]["lines"])
        else:
            pairs = [(i if i < len(item["lines"]) else None, i if i < len(sources[1]["lines"]) else None)
                     for i in range(max(len(source["lines"]) for source in sources))]
            note = "Positional rows; timestamp alignment is off"
        result = [dict(pair=pair) for pair in pairs]
    elif view == "diff" and len(sources) == 2:
        result = presentation.diff_rows(item["lines"], sources[1]["lines"], state["ignore_time"])
        note = "Timestamp fields ignored by request" if state["ignore_time"] else "Exact source text comparison"
    elif view == "json":
        result, omitted = presentation.structured_rows(item["lines"], state["json_collapsed"], *state["json_filter"])
        note = f"Enter expands/folds node | o opens original | {omitted} source lines filtered"
    elif view == "fold":
        result = presentation.folded_rows(item["lines"], state["fold_expanded"])
        note = f"Enter expands/folds group | o opens original | {sum(row.get('hidden', 0) for row in result)} lines hidden"
    else:
        result = [dict(text=f"L{i + 1} {line}", original=i) for i, line in enumerate(item["lines"])]
    for row in result:
        source_item = item
        index = row.get("original")
        if view == "diff" and len(sources) == 2:
            side = 0 if row.get("left") is not None else 1
            source_item = sources[side]
            index = row.get("left" if side == 0 else "right")
            row["original"] = index
        if index is not None:
            from .log_text import display_text
            row["source_text"] = display_text(source_item["lines"][index])
            row["source_path"] = source_item["path"]
            row["tail_distance"] = len(source_item["lines"]) - index - 1
            row["file_identity"] = source_item.get("file_identity")
    state["presentation_key"], state["rows"], state["presentation_note"] = key, result, note
    return result, note


def _content_viewport(app, width, height):
    """Use a current dock's data rectangle without covering its job browser."""
    browser = getattr(app, "history_browser_state", {})
    frame = browser.get("frame") if isinstance(browser, dict) else None
    rect = getattr(app, "history_browser_content_rect", None)
    if (isinstance(frame, dict) and rect is not None and frame.get("tab") == app.tab == "log" and
            frame.get("mode") == getattr(app, "mode", "main") == "main" and
            frame.get("geometry") == (getattr(app, "width", None), getattr(app, "height", None)) and
            all(isinstance(getattr(rect, name, None), int) for name in ("x", "y", "width", "height"))):
        return max(0, rect.x), max(0, rect.y), max(0, rect.width), max(0, rect.height)
    return 0, 0, width, height


def _fit_header(rows, hits, budget, source_labels):
    """Keep source controls and evidence visible inside short docked panes."""
    if len(rows) <= budget:
        return rows, hits
    priorities = sorted({y for y, _, _ in hits})
    priorities.extend(index for index, row in enumerate(rows)
                      if any(style.startswith(("red", "yellow")) for _, style in row))
    priorities.extend(source_labels)
    priorities.extend(range(len(rows)))
    chosen = list(dict.fromkeys(priorities))[:max(0, budget)]
    chosen.sort()
    mapping = {old: new for new, old in enumerate(chosen)}
    return [rows[index] for index in chosen], [(mapping[y], kind, value) for y, kind, value in hits if y in mapping]


def overlay(views, snap, app, width, height):
    state = _state(app)
    state["control_hits"] = []
    if app.tab != "log" or app.mode != "main" or app.logs.browser or state["view"] == "plain":
        return None
    origin_x, origin_y, width, height = _content_viewport(app, width, height)
    data = _alternate(app)
    state["mouse_rows"] = {}
    body_start = None
    body, painted, page, scroll_context = [], 0, 0, ()
    split_left, split_extent = None, None
    button_hits = []
    source_label_rows = []
    rows = [[(" Esc original | arrows scroll/pan | Y copies the current complete source", "dim")],
            [(" Select/yank in the original view; panels preserve source bytes.", "dim")]]
    label = status_label(app)
    if label:
        rows.insert(0, [(" " + label, "yellow+bold")])
    if data is None:
        rows.append([(" Waiting for the shared background reader.", "dim")])
    elif data.get("error"):
        rows.append([(" " + clean(data["error"], views.g.ascii), "red")])
    else:
        sources = data.get("sources", [])
        if not sources:
            rows.append([(" Open a job log first; paired views need two registered files.", "yellow")])
        else:
            if state["view"] in ("split", "diff"):
                from .control_rows import buttons
                choices = [("original", "Original", ("key", "esc"))]
                if len(sources) == 2:
                    choices += [("left", "Copy source: left", ("key", "[")),
                                ("right", "Copy source: right", ("key", "]"))]
                controls, hits = buttons(views.g, max(0, width - 8), choices,
                                          group="log_sources", prefix="log-source:")
                button_hits = [(y + len(rows), kind, value) for y, kind, value in hits]
                rows.extend(controls)
            for item in sources:
                source_label_rows.append(len(rows))
                rows.append([(" " + clean(item["label"] + " | " + item["path"], views.g.ascii), "cyan+bold")])
                if item.get("error"):
                    rows.append([(" " + clean(item["error"], views.g.ascii), "red")])
                elif item.get("truncated") or item.get("omitted_lines"):
                    rows.append([(f" Bounded tail: at most {VIEW_BYTES} bytes / {MAX_VIEW_ROWS} original lines; L numbers are relative.", "yellow")])
                else:
                    rows.append([(f" Complete inspected source: {item.get('size', 0)} bytes / {len(item['lines'])} original lines.", "dim")])
            if state["view"] in ("split", "diff"):
                rows.append([(" [ left original copy source | ] right original copy source", "cyan")])
                current_path = (app.logs.entry or {}).get("path") or app.logs.path
                rows.append([(" Copy source: " + clean(current_path, views.g.ascii), "green+bold")])
                if len(sources) < 2:
                    rows.append([(" Only one source is available. Register another file for comparison.", "yellow")])
            body, note = _presentation_rows(state, sources)
            if note:
                rows.append([(" " + clean(note, views.g.ascii), "yellow" if state["view"] == "split" else "dim")])
            rows, button_hits = _fit_header(rows, button_hits, max(0, height - 5), source_label_rows)
            page = max(1, height - len(rows) - 5)
            state["viewport_page"] = page
            state["cursor"] = min(state["cursor"], max(0, len(body) - 1))
            state["scroll"] = min(state["scroll"], max(0, len(body) - page))
            scroll_context = (state["view"], getattr(app, "log_job", None), app.logs.path,
                              tuple((source.get("path"), source.get("file_identity")) for source in sources),
                              state["align"], state["ignore_time"], tuple(state["json_filter"]), width)
            from .scrolling import viewport
            painted = viewport(app, "logs:workbench", state["scroll"], len(body), page,
                               context=scroll_context)
            split_extent = max(2, width - 10)
            split_left = max(1, min(split_extent - 1, split_extent * state.get("split_ratio", 50) // 100))
            split_right = split_extent - split_left
            sep = " | " if views.g.ascii else " │ "
            body_start = len(rows)
            from .job_selection import lines_cleared
            cursor_visible = not lines_cleared(app)
            for index, item in enumerate(body[painted:painted + page], painted):
                if "pair" in item:
                    values = [source["lines"][line] if line is not None else "" for source, line in zip(sources, item["pair"])]
                    columns = [display_line(app, clean(value, views.g.ascii, VIEW_BYTES)) for value in values]
                    text = (L.pad(L.cut(columns[0], split_left, views.g.ascii), split_left) + sep +
                            L.pad(L.cut(columns[1], split_right, views.g.ascii), split_right))
                else:
                    text = display_line(app, clean(item["text"], views.g.ascii, VIEW_BYTES))
                marker = ">" if views.g.ascii else "›"
                rows.append([(" " + (marker if cursor_visible and index == state["cursor"] else " ") + " " + text,
                              "rev+bold" if cursor_visible and index == state["cursor"] else item.get("style", ""))])
    rendered = L.box(views.g, rows, width, height, "   Log workbench / " + state["view"], min_width=max(1, width - 4))
    rendered = [(y + origin_y, x + origin_x, row) for y, x, row in rendered]
    from .control_rows import place_hits
    state["control_hits"] = place_hits(button_hits, rendered[1:-1])
    if body_start is not None and len(rendered) >= 3:
        for relative, (y, x, row) in enumerate(rendered[1:-1]):
            if relative >= body_start:
                state["mouse_rows"][y] = (x + 1, x + L.vlen(L.row_text(row)) - 1,
                                           painted + relative - body_start)
        visible = rendered[1:-1][body_start:]
        if state["view"] == "split" and len(sources) == 2 and visible and split_left is not None:
            from . import pane_drag
            top, left, first_row = visible[0]
            column = left + 5 + split_left
            if column < left + L.vlen(L.row_text(first_row)) - 1:
                smallest = 8 if split_extent >= 16 else 1
                minimum = max(20, math.ceil(smallest * 100 / split_extent))
                maximum = min(80, (split_extent - smallest) * 100 // split_extent)
                if minimum <= maximum:
                    key = "log:sources"
                    pane_drag.register(app, key, "vertical", column, top, 1, len(visible),
                        left + 5, split_extent, state.get("split_ratio", 50),
                        lambda value: state.__setitem__("split_ratio", value),
                        minimum=minimum, maximum=maximum, label="Resize log sources")
                    canvas = [row for _, _, row in rendered]
                    pane_drag.paint(canvas, app, key, origin_x=rendered[0][1],
                                    origin_y=rendered[0][0], ascii_=views.g.ascii)
                    rendered = [(y, x, canvas[index]) for index, (y, x, _) in enumerate(rendered)]
                    hit = pane_drag.control_hit(app, key)
                    if hit and isinstance(getattr(app, "interaction_state", None), dict):
                        state["control_hits"].append(hit)
        if visible:
            top, left, first = visible[0]
            right = left + L.vlen(L.row_text(first))
            S.register(app, "logs:workbench", (top, left + 1, visible[-1][0] + 1, right),
                       len(body), page, state["scroll"], painted,
                       lambda value: state.__setitem__("scroll", value),
                       context=scroll_context,
                       header=(rendered[0][0], left + 1, right - 1), layer=1, absolute=True)
    return rendered


def open_citation(app, citation):
    """Open only an explicitly cited log source, then position cached raw lines."""
    if not isinstance(citation, dict) or not citation.get("path"):
        app.say("This citation describes a scheduler/event observation, not a log file.")
        return False
    path = citation["path"]
    if not isinstance(path, str) or not path or len(path) > 4096 or not path.isprintable():
        app.fail("Citation path is invalid; refresh Evidence.")
        return False
    jid = citation.get("job") or getattr(app, "research_job_id", None) or getattr(app, "selected_id", None)
    if jid and hasattr(app, "open_log"):
        app.open_log(jid)
        if app.tab != "log":
            return False
    from .log_tools import before_source_change
    before_source_change(app)
    app.logs.clear_selection(reset_cursor=True)
    app.log_selection_expected = False
    app.logs.entry = dict(id="citation." + citation.get("id", "source"), path=path,
                          label=citation.get("source", "Evidence log"), group="Evidence", source="citation")
    app.logs.browser, app.logs.browse_return, app.logs.path, app.logs.top = False, True, "", None
    state = _state(app)
    state["view"], state["citation"] = "plain", dict(citation)
    app.tab = "log"
    app.say("Opening cited source; Esc returns to its log catalog")
    return True


def apply_citation(app, buf):
    """Called after the existing log reader; performs no additional file I/O."""
    state = _state(app)
    citation = state.get("citation")
    if not citation or buf is None or buf.error:
        return
    state["citation"] = None
    metadata = citation.get("file_identity")
    if isinstance(metadata, dict) and metadata.get("ident") is not None and tuple(metadata["ident"]) != tuple(buf.ident or ()):
        app.fail("Cited log was replaced after inspection; refresh Evidence before following this location.")
        return
    index = None
    number = citation.get("line")
    if citation.get("line_basis") == "original" and not buf.skipped_bytes and type(number) is int:
        index = number - 1
    else:
        target = citation.get("excerpt_line", "")
        if target:
            # Search a bounded original prefix; disambiguate repeated messages
            # by taking the latest retained match from this inspected tail.
            target = target.replace("\t", "    ")
            if isinstance(metadata, dict) and type(metadata.get("size")) is int and metadata["size"] != buf.size:
                app.say("Cited log changed after inspection; its exact file is open. Refresh Evidence to locate the cited occurrence.")
                return
            distance = citation.get("tail_distance")
            if type(distance) is int and 0 <= distance < buf.total:
                candidate = buf.total - distance - 1
                line = buf.lines[candidate] if candidate < len(buf.lines) else buf.partial
                if line.startswith(target):
                    index = candidate
            if index is None:
                matches = []
                for i in range(max(0, buf.total - 1200), buf.total):
                    line = buf.lines[i] if i < len(buf.lines) else buf.partial
                    if line.startswith(target):
                        matches.append(i)
                if len(matches) == 1:
                    index = matches[0]
    if index is None or index < 0 or index >= buf.total:
        app.say("Citation is outside the retained log window or changed; the exact source file is open.")
        return
    from .job_selection import resume_lines
    resume_lines(app)
    app.logs.goto(index, buf)
    app.logs.match = index
    app.say(f"Cited {citation.get('id', 'source')} at retained line {index + 1}; original bytes remain selectable")
