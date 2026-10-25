"""Full-source log pages, search results, named marks and reading positions.

The terminal thread handles only published data. File work shares ResearchHub's
single bounded worker. Older-page actions refer to that page's exact source;
copying never falls through to the live tail under a modal overlay.
"""

from __future__ import annotations
from ..ui.selection_state import lines_cleared
from ..ui.selection_effects import clear_lines

import math
import re
from collections import OrderedDict

from .. import layout as L
from . import match_index as log_match_index
from . import scan as log_scan
from .. import scrollbars as S
from ..research_text import clean

MODES = {"log_tools_page", "log_tools_results", "log_tools_marks"}


def initialize(app):
    if not isinstance(getattr(app, "log_tools_state", None), dict):
        app.log_tools_state = {
            "generation": 0,
            "busy": False,
            "page": None,
            "page_source": None,
            "page_files": None,
            "page_cursor": 0,
            "page_top": 0,
            "page_pan": 0,
            "selection": None,
            "page_history": [],
            "results": None,
            "result_cursor": 0,
            "mark_cursor": 0,
            "marks": [],
            "mouse_rows": {},
            "return_mode": "main",
            "page_return": "main",
            "positions": OrderedDict(),
            "active_key": None,
            "active_buffer": None,
            "restored": None,
            "search_mode": {"regex": True, "case": False, "word": False},
            "retained_explicit": False,
            "retained_cache": None,
            "retained_omitted": 0,
            "retained_matcher_cache": None,
            "page_pan_cache": OrderedDict(),
            "page_pan_page": None,
            "retained_index": None,
            "retained_target": None,
            "retained_target_buffer": None,
            "retained_token": None,
            "retained_pending": False,
            "retained_processed": 0,
            "retained_total": 0,
            "retained_known_count": None,
            "retained_navigation": None,
        }
    app.log_tools_state.setdefault("cursor_deselected", False)
    app.log_tools_state.setdefault("result_top", 0)
    app.log_tools_state.setdefault("mark_top", 0)
    app.log_tools_state.setdefault("control_hits", [])
    app.log_tools_state.setdefault("control_token", None)
    return app.log_tools_state


def _source(app):
    state = initialize(app)
    if app.mode == "log_tools_page" and state.get("page_source"):
        return dict(state["page_source"])
    path = app.resolve_log_path() if hasattr(app, "resolve_log_path") else app.logs.path
    if not path:
        raise ValueError("Open an exact log source before using this command")
    entry = dict(app.logs.entry or {})
    entry.update(path=path)
    entry.setdefault("label", path)
    entry.setdefault("job_id", getattr(app, "log_job", None))
    entry.setdefault("target", _target(app.logs.files))
    binding = (getattr(app, "project_state", {}) or {}).get("binding") or {}
    if binding:
        entry.setdefault("run_id", binding.get("run_id"))
        entry.setdefault("run_root", binding.get("run_root"))
    return entry


def _target(files):
    return getattr(getattr(files, "ssh", None), "target", "local")


def _key(app, path):
    binding = (getattr(app, "project_state", {}) or {}).get("binding") or {}
    files = app.logs.files
    target = _target(files)
    return (
        target,
        getattr(app, "log_job", None),
        binding.get("run_root"),
        binding.get("run_id"),
        path,
    )


def before_source_change(app):
    """Call before mutating a log source, including project/source selectors."""
    state = initialize(app)
    state["retained_navigation"] = None
    buf = state.get("active_buffer")
    key = state.get("active_key")
    if (
        key is not None
        and buf is not None
        and app.logs.path == buf.path
        and not buf.error
    ):
        state["positions"][key] = {
            "identity": (buf.ident, buf.reloads, buf.skipped_bytes),
            "top": app.logs.top,
            "cursor": app.logs.cursor,
            "follow": app.logs.following,
        }
        state["positions"].move_to_end(key)
        while len(state["positions"]) > 64:
            state["positions"].popitem(last=False)
    state["active_key"], state["active_buffer"], state["restored"] = None, None, None


def observe_buffer(app, buf):
    """Restore only positions for the same file identity and retained origin."""
    if buf is None:
        return
    state = initialize(app)
    key = _key(app, buf.path)
    if state.get("active_key") != key:
        # This also covers direct path assignments by third-party extensions.
        if state.get("active_key") is not None:
            before_source_change(app)
        state["active_key"], state["restored"] = key, None
    state["active_buffer"] = buf
    if buf.loading or buf.ident is None or buf.error:
        return
    if state["restored"] == key:
        identity = (buf.ident, buf.reloads, buf.skipped_bytes)
        saved = state["positions"].get(key)
        if saved is not None and saved["identity"] != identity:
            state["positions"].pop(key, None)
        state["positions"][key] = {
            "identity": identity,
            "top": app.logs.top,
            "cursor": app.logs.cursor,
            "follow": app.logs.following,
        }
        state["positions"].move_to_end(key)
        return
    saved = state["positions"].get(key)
    if saved and saved["identity"] == (buf.ident, buf.reloads, buf.skipped_bytes):
        app.logs.top = None if saved["follow"] else saved["top"]

        app.logs.cursor = (
            None if saved["follow"] or lines_cleared(app) else saved["cursor"]
        )
        app.logs.sync_buffer(buf)
    elif saved:
        state["positions"].pop(key, None)
    state["restored"] = key
    state["positions"][key] = {
        "identity": (buf.ident, buf.reloads, buf.skipped_bytes),
        "top": app.logs.top,
        "cursor": app.logs.cursor,
        "follow": app.logs.following,
    }
    state["positions"].move_to_end(key)
    while len(state["positions"]) > 64:
        state["positions"].popitem(last=False)


def restore(app, data):
    state = initialize(app)
    if not isinstance(data, dict):
        return
    marks = data.get("marks", [])
    if isinstance(marks, list):
        for mark in marks[:256]:
            if not isinstance(mark, dict) or not isinstance(mark.get("source"), dict):
                continue
            source, identity = mark["source"], mark.get("identity")
            if (
                isinstance(mark.get("name"), str)
                and 0 < len(mark["name"]) <= 80
                and mark["name"].isprintable()
                and isinstance(source.get("path"), str)
                and 0 < len(source["path"]) <= 4096
                and source["path"].isprintable()
                and type(mark.get("offset")) is int
                and 0 <= mark["offset"] <= 2**63 - 1
                and isinstance(identity, (list, tuple))
                and len(identity) == 2
                and all(type(v) is int and v >= 0 for v in identity)
            ):
                state["marks"].append(
                    {
                        "name": mark["name"],
                        "source": dict(source),
                        "offset": mark["offset"],
                        "identity": tuple(identity),
                        "line": mark.get("line")
                        if type(mark.get("line")) is int and mark["line"] > 0
                        else None,
                        "fragment": mark.get("fragment") is True,
                    }
                )
    options = data.get("search_mode")
    if isinstance(options, dict) and all(
        type(options.get(k)) is bool for k in ("regex", "case", "word")
    ):
        state["search_mode"] = {key: options[key] for key in ("regex", "case", "word")}
        state["retained_explicit"] = data.get("retained_explicit") is True


def save(app):
    state = initialize(app)
    return {
        "marks": state["marks"][:256],
        "search_mode": dict(state["search_mode"]),
        "retained_explicit": state["retained_explicit"],
    }


def command_names():
    return [
        "logolder",
        "logsearch",
        "logsearchmode",
        "logresults",
        "loggoto",
        "logmark",
        "logmarks",
    ]


def _hub(app):
    if getattr(app, "research", None) is None:
        from ..research import ResearchHub

        app.research = ResearchHub(app.cfg, app.logs.files)
    return app.research


def _task(app, label, fn, complete, *, source=""):
    state, hub = initialize(app), _hub(app)
    # Publish already-completed reads before admission. poll_task checks done()
    # and never waits for IO or broadens the single-worker queue.
    hub.poll_task()
    if state["busy"] or hub.pending or hub.closed:
        app.fail("The background reader is busy; retry the log command shortly")
        return False
    from ..activity_ui import Activity

    if not hasattr(app, "activity"):
        app.activity = Activity()
    activity = app.activity.start(label, source)
    token = state["generation"] + 1
    state["generation"], state["busy"] = token, True
    cancel = lambda: (
        hub.closed or activity["cancel"].is_set() or token != state["generation"]
    )
    progress = lambda done, total: app.activity.progress(activity, done, total)

    def finished(value):
        if token != state["generation"]:
            return
        state["busy"] = False
        if isinstance(value, Exception):
            app.activity.finish(
                activity,
                "cancelled" if isinstance(value, log_scan.ScanCancelled) else "error",
            )
            app.fail(clean(value))
            return
        app.activity.finish(activity, "ready")
        complete(value)

    if not hub.start_task(lambda: fn(cancel, progress), finished):
        state["busy"] = False
        app.activity.finish(activity, "not started")
        app.fail("The background reader is busy; retry the log command shortly")
        return False
    app.say(label + "; Ctrl-A shows progress; :task cancel stops the read")
    return True


def _show_page(
    app,
    source,
    offset=0,
    *,
    line=None,
    expected=None,
    return_mode=None,
    fragment=False,
    onloaded=None,
):
    state, files = initialize(app), app.logs.files
    if source.get("target", _target(files)) != _target(files):
        app.fail(
            "This source belongs to another connection; select its original profile"
        )
        return False
    back = return_mode or (
        app.mode if app.mode != "log_tools_page" else state["page_return"]
    )
    origin = (app.mode, app.tab, getattr(app, "log_job", None), app.logs.path)

    def complete(page):
        if app.mode != "log_tools_page":
            state["page_history"] = []
        state.update(
            page=page,
            page_source=dict(source),
            page_files=files,
            page_cursor=0,
            page_top=0,
            page_pan=0,
            cursor_deselected=False,
            selection=None,
            page_return=back,
        )
        if (app.mode, app.tab, getattr(app, "log_job", None), app.logs.path) == origin:
            app.mode = "log_tools_page"
        if onloaded is not None:
            onloaded(page)
        app.say("Source page ready; Esc returns; Y copies this entire source")

    return _task(
        app,
        "Reading source page",
        lambda cancel, progress: log_scan.read_page(
            files,
            source["path"],
            offset,
            line=line,
            expected=expected,
            cancel=cancel,
            align=not fragment,
        ),
        complete,
        source=source["path"],
    )


def _catalog(app, selected):
    # Cached declarations only. No catalog discovery or filesystem probe on UI.
    entries = list(getattr(app.logs, "entries", []))
    entries += list((getattr(app, "project_state", {}) or {}).get("logs", []))
    from ..ui.log_sources import entries as _entries

    entries += _entries(app)
    result, seen = [], set()
    for entry in [selected] + entries:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and entry["path"]
            and entry["path"] not in seen
        ):
            seen.add(entry["path"])
            source = dict(entry)
            for field in ("job_id", "run_id", "run_root", "target"):
                source.setdefault(field, selected.get(field))
            result.append(source)
    return result


def _search_options(state, values):
    options = {"regex": False, "case": False, "word": False}
    all_sources, query = False, []
    index, terminated = 0, False
    while index < len(values):
        value = values[index]
        if not terminated and value == "--":
            terminated = True
        elif not terminated and value.startswith("--"):
            if value == "--all":
                all_sources = True
            elif value in ("--literal", "--regex"):
                options["regex"] = value == "--regex"
            elif value in ("--case", "--nocase"):
                options["case"] = value == "--case"
            elif value == "--word":
                options["word"] = True
            else:
                raise ValueError("Unknown log search option: " + value)
        else:
            query.append(value)
        index += 1
    return all_sources, " ".join(query), options


def run_command(app, args):
    if args and args[0] == "copy" and app.mode == "log_tools_page":
        if args[1:] not in ([], ["all"], ["selection"]):
            app.fail("copy [selection|all] in a source page")
        else:
            _copy_page(app, all_file=args[1:] == ["all"])
        return True
    if not args or args[0] not in command_names():
        return False
    state, command, values = initialize(app), args[0], args[1:]
    try:
        if command == "logsearchmode":
            if not values or any(
                value not in ("literal", "regex", "case", "nocase", "word", "partial")
                for value in values
            ):
                raise ValueError(
                    "logsearchmode literal|regex [case|nocase] [word|partial]"
                )
            options = dict(state["search_mode"])
            for value in values:
                if value in ("literal", "regex"):
                    options["regex"] = value == "regex"
                elif value in ("case", "nocase"):
                    options["case"] = value == "case"
                else:
                    options["word"] = value == "word"
            if app.logs.search:
                log_scan.compile_search(app.logs.search, **options)
            (
                state["search_mode"],
                state["retained_explicit"],
                state["retained_cache"],
            ) = options, True, None
            app.say(
                "Retained-log search: "
                + ("regex" if options["regex"] else "literal")
                + "; "
                + ("case-sensitive" if options["case"] else "ignore case")
                + "; "
                + ("whole word" if options["word"] else "partial word")
            )
        elif command == "logsearch":
            all_sources, query, options = _search_options(state, values)
            log_scan.compile_search(query, **options)
            selected = _source(app)
            sources = _catalog(app, selected) if all_sources else [selected]
            files = app.logs.files
            origin = (app.mode, app.tab, getattr(app, "log_job", None), app.logs.path)

            def complete(value):
                state["results"], state["result_cursor"], state["return_mode"] = (
                    value,
                    0,
                    "main",
                )
                if (
                    app.mode,
                    app.tab,
                    getattr(app, "log_job", None),
                    app.logs.path,
                ) == origin:
                    app.mode = "log_tools_results"
                app.say(
                    f"{len(value['matches'])} log matches; "
                    + (
                        "complete snapshot"
                        if value["complete"]
                        else "partial coverage; inspect source reports"
                    )
                )

            _task(
                app,
                "Searching complete log sources",
                lambda cancel, progress: log_scan.search_sources(
                    files, sources, query, cancel=cancel, progress=progress, **options
                ),
                complete,
                source=selected["path"],
            )
        elif command == "logresults":
            if values:
                raise ValueError("logresults")
            if state["results"] is None:
                raise ValueError("Search a log with :logsearch first")
            state["return_mode"], app.mode = (
                app.mode if app.mode not in MODES else "main",
                "log_tools_results",
            )
        elif command == "logolder":
            if len(values) > 1:
                raise ValueError("logolder [BYTE_POSITION]")
            source = _source(app)
            buf = app.logs.buffers.get(source["path"])
            offset = (
                int(values[0])
                if values
                else max(
                    0,
                    (buf.skipped_bytes if buf is not None else 0) - log_scan.PAGE_BYTES,
                )
            )
            if offset < 0:
                raise ValueError("Byte position must be nonnegative")
            _show_page(app, source, offset)
        elif command == "loggoto":
            if len(values) != 2 or values[0] not in ("line", "byte", "percent", "time"):
                raise ValueError(
                    "loggoto line N|byte N|percent 0..100|time ISO_TIMESTAMP"
                )
            source, files = _source(app), app.logs.files
            kind, value = values
            if kind in ("line", "byte") and (
                not value.isascii() or int(value) < (1 if kind == "line" else 0)
            ):
                raise ValueError("Position is outside the valid range")
            if kind == "percent" and (
                not math.isfinite(float(value)) or not 0 <= float(value) <= 100
            ):
                raise ValueError("Percentage must be between 0 and 100")
            origin = (app.mode, app.tab, getattr(app, "log_job", None), app.logs.path)

            def complete(page):
                state.update(
                    page=page,
                    page_source=dict(source),
                    page_files=files,
                    page_cursor=0,
                    page_top=0,
                    page_pan=0,
                    cursor_deselected=False,
                    selection=None,
                    page_return="main",
                )
                if (
                    app.mode,
                    app.tab,
                    getattr(app, "log_job", None),
                    app.logs.path,
                ) == origin:
                    app.mode = "log_tools_page"

            _task(
                app,
                "Finding absolute log location",
                lambda cancel, progress: log_scan.locate(
                    files, source, kind, value, cancel=cancel, progress=progress
                ),
                complete,
                source=source["path"],
            )
        elif command == "logmark":
            name = " ".join(values)
            if not name or len(name) > 80 or not name.isprintable():
                raise ValueError("logmark NAME (1 to 80 printable characters)")
            if app.mode == "log_tools_page" and state["page"] and state["page"]["rows"]:
                page = state["page"]
                row = page["rows"][state["page_cursor"]]
                mark = {
                    "name": name,
                    "source": dict(state["page_source"]),
                    "offset": row["offset"],
                    "line": row["line"],
                    "identity": page["snapshot"]["ident"],
                    "fragment": row["partial_start"] or row["partial_end"],
                }
            else:
                source = _source(app)
                buf = app.logs.buffers.get(source["path"])
                index = app.logs.current_line(buf)
                if index is None or buf is None or buf.error or buf.ident is None:
                    raise ValueError(
                        "Wait for a readable log snapshot before setting a mark"
                    )
                offset = buf.skipped_bytes + sum(buf._line_bytes[:index])
                mark = {
                    "name": name,
                    "source": source,
                    "offset": offset,
                    "line": index + 1 if not buf.skipped_bytes else None,
                    "identity": buf.ident,
                }
            state["marks"] = [
                item
                for item in state["marks"]
                if not (
                    item["name"] == name
                    and item["source"]["path"] == mark["source"]["path"]
                )
            ]
            state["marks"].append(mark)
            state["marks"] = state["marks"][-256:]
            app.say("Log mark saved: " + name)
        elif command == "logmarks":
            if values:
                raise ValueError("logmarks")
            state["return_mode"], state["mark_cursor"] = (
                app.mode if app.mode not in MODES else "main",
                0,
            )
            app.mode = "log_tools_marks"
    except (ValueError, TypeError, OverflowError) as exc:
        app.fail(str(exc))
    return True


def retained_matcher(app, pattern=None):
    """Compile a bounded display/search expression; legacy invalid forms are literal."""
    state = initialize(app)
    pattern = app.logs.search if pattern is None else pattern
    key = (
        pattern,
        tuple(sorted(state["search_mode"].items())),
        state["retained_explicit"],
    )
    cached = state["retained_matcher_cache"]
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        matcher = log_scan.compile_search(pattern, **state["search_mode"])
    except ValueError:
        if state["retained_explicit"]:
            raise
        matcher = (
            re.compile(re.escape(str(pattern)[:1024]), re.IGNORECASE)
            if pattern
            else re.compile(r"(?!)")
        )
    state["retained_matcher_cache"] = (key, matcher)
    return matcher


def retained_matches(app, line, pattern=None, source_bytes=None):
    """Safe per-line renderer hook. Returns False for omitted oversized regex lines."""
    state = initialize(app)
    if state["search_mode"]["regex"]:
        if source_bytes is None:
            if len(line) > log_scan.REGEX_LINE_BYTES:
                return False
            source_bytes = len(line.encode("utf-8", "replace"))
        if source_bytes > log_scan.REGEX_LINE_BYTES:
            return False
    try:
        # Highlight only the painted slice. Full-source search/counting uses
        # the background index and must not be repeated for each UI frame.
        pan = max(0, int(getattr(app.logs, "pan", 0)))
        width = max(1, min(1024, int(getattr(app, "width", 120))))
        visible = L.truncate(line[pan:], width)
        return bool(retained_matcher(app, pattern).search(visible))
    except ValueError:
        return False


def _retained_context(app, buf):
    state = initialize(app)
    # Synchronous buffers can mutate in place; they retain the immediate API.
    nonce = None if getattr(app, "interactive", False) else buf.last_refresh
    return (
        buf._session_token,
        buf.ident,
        buf.reloads,
        buf.path,
        id(buf.files),
        buf.max_bytes,
        app.logs.search,
        tuple(sorted(state["search_mode"].items())),
        state["retained_explicit"],
        nonce,
    )


def _extend_retained(app, current, buf, context, matcher, state):
    interactive = getattr(app, "interactive", False)
    if interactive:
        # Reuse only a tiny literal append/trim on the event thread. Regex and
        # equal-size replacements require a worker regardless of buffer size.
        old = current.buffer
        dropped = buf.skipped_bytes - old.skipped_bytes
        regex_work = (
            state["search_mode"]["regex"]
            and re.escape(app.logs.search) != app.logs.search
        )
        if (
            regex_work
            or old is buf
            or dropped < 0
            or dropped > 8192
            or buf.size <= old.size
            and not dropped
            or abs(len(buf.lines) - len(old.lines)) > 32
            or len(buf._partial_raw) > 8192
        ):
            return None
        tail = max(0, len(buf.lines) - 32)
        if sum(buf._line_bytes[tail:]) > 8192:
            return None
    return log_match_index.extend(
        current,
        buf,
        context,
        matcher,
        regex=state["search_mode"]["regex"],
        query_length=len(app.logs.search),
        word=state["search_mode"]["word"],
    )


def _get_retained_index(app, buf):
    state = initialize(app)
    if buf is None or buf.error or not app.logs.search:
        state.update(
            retained_navigation=None,
            retained_pending=False,
            retained_processed=0,
            retained_total=0,
            retained_omitted=0,
            retained_known_count=0,
        )
        return None
    context = _retained_context(app, buf)
    navigation = state.get("retained_navigation")
    if navigation is not None and navigation[0] != context:
        state["retained_navigation"] = None
    state["retained_target"] = context
    state["retained_target_buffer"] = buf
    try:
        matcher = retained_matcher(app)
    except ValueError as exc:
        state["retained_pending"] = False
        app.fail(str(exc))
        return None
    current = state["retained_index"]
    if current is not None and current.context == context:
        if current.buffer is buf and current.stamp == log_match_index.buffer_stamp(buf):
            state.update(
                retained_pending=False,
                retained_processed=current.total,
                retained_total=current.total,
                retained_omitted=current.total_omitted,
                retained_known_count=current.total_count,
            )
            return current
        updated = _extend_retained(app, current, buf, context, matcher, state)
        if updated is not None:
            state["retained_index"] = updated
            state.update(
                retained_pending=False,
                retained_processed=updated.total,
                retained_total=updated.total,
                retained_omitted=updated.total_omitted,
                retained_known_count=updated.total_count,
            )
            return updated
    # Explicit noninteractive reads remain immediate. Every interactive full
    # index publishes through the existing worker; no queue is added.
    if not getattr(app, "interactive", False):
        current = log_match_index.build(
            buf, context, matcher, state["search_mode"]["regex"]
        )
        state["retained_index"] = current
        state.update(
            retained_pending=False,
            retained_processed=current.total,
            retained_total=current.total,
            retained_omitted=current.total_omitted,
            retained_known_count=current.total_count,
        )
        return current
    state["retained_pending"] = True
    state["retained_total"] = buf.total
    compatible = current is not None and current.context == context
    state["retained_processed"] = current.total if compatible else 0
    state["retained_known_count"] = current.total_count if compatible else None
    hub = _hub(app)
    hub.poll_task()
    # A completed indexing callback can make its immutable result available.
    ready = state["retained_index"]
    if ready is not None and ready.context == context:
        updated = (
            ready
            if ready.buffer is buf and ready.stamp == log_match_index.buffer_stamp(buf)
            else _extend_retained(app, ready, buf, context, matcher, state)
        )
        if updated is not None:
            state["retained_index"] = updated
            state.update(
                retained_pending=False,
                retained_processed=updated.total,
                retained_omitted=updated.total_omitted,
                retained_known_count=updated.total_count,
            )
            return updated
    if state["retained_token"] is None and not hub.closed and not hub.pending:
        token = object()
        state["retained_token"] = token
        regex = state["search_mode"]["regex"]

        def complete(value):
            if state["retained_token"] is token:
                state["retained_token"] = None
            if (
                state["retained_target"] != context
                or _retained_context(app, buf) != context
            ):
                navigation = state.get("retained_navigation")
                if navigation is not None and navigation[0] == context:
                    state["retained_navigation"] = None
                return
            if isinstance(value, Exception):
                state["retained_pending"] = False
                state["retained_navigation"] = None
                app.fail("Retained log search could not be indexed: " + clean(value))
                return
            newer = state["retained_index"]
            if (
                newer is not None
                and newer.context == context
                and newer.buffer is state["retained_target_buffer"]
                and newer.buffer is not value.buffer
            ):
                state["retained_pending"] = False
                return
            state["retained_index"] = value
            state.update(
                retained_pending=False,
                retained_processed=value.total,
                retained_omitted=value.total_omitted,
                retained_known_count=value.total_count,
            )
            _finish_retained_navigation(app, value)

        import time

        deadline = time.monotonic() + 5.0
        if not hub.start_task(
            lambda: log_match_index.build(
                buf,
                context,
                matcher,
                regex,
                cancel=lambda: hub.closed or state["retained_target"] != context,
                deadline=deadline,
            ),
            complete,
        ):
            state["retained_token"] = None
    return None


def _navigation_position(app):
    """A delayed match cannot override a later source, cursor or viewport choice."""
    logs = app.logs
    return (
        getattr(app, "mode", "main"),
        getattr(app, "tab", "log"),
        _key(app, logs.path),
        logs.path,
        logs._buffer_token,
        logs.cursor,
        logs.top,
        logs.match,
        logs.selection_generation,
    )


def _finish_retained_navigation(app, index):
    state = initialize(app)
    request = state.get("retained_navigation")
    if request is None:
        return
    state["retained_navigation"] = None
    context, position, start, backwards = request
    if context != index.context or position != _navigation_position(app):
        return
    found = log_match_index.find(index, start, backwards)
    if found is not None:
        app.logs.match = found
        app.logs.goto(found, index.buffer)
    if getattr(app, "message", "").startswith(
        ("Indexing retained log", "Search index updating")
    ):
        app.say(f"{index.total_count} lines match '{app.logs.search}'")


def find_retained(app, buf, backwards=False):
    """Find through compact cached flags; pending builds do not imply no match."""
    index = _get_retained_index(app, buf)
    if index is None:
        state = initialize(app)
        if buf is not None and state["retained_pending"]:
            start = app.logs.match if app.logs.match is not None else app.logs.cursor
            if start is None and app.logs.top is not None:
                start = app.logs.top + app.logs.page
            state["retained_navigation"] = (
                _retained_context(app, buf),
                _navigation_position(app),
                start,
                backwards,
            )
        return (True, None)
    initialize(app)["retained_navigation"] = None
    start = app.logs.match if app.logs.match is not None else app.logs.cursor
    if start is None and app.logs.top is not None:
        start = app.logs.top + app.logs.page
    found = log_match_index.find(index, start, backwards)
    if found is not None:
        app.logs.match = found
        app.logs.goto(found, buf)
    return (True, found)


def count_retained(app, buf):
    """Return the cached count. Inspect retained_pending for incomplete builds."""
    index = _get_retained_index(app, buf)
    if index is not None:
        return index.total_count
    return initialize(app)["retained_known_count"] or 0


def _copy_page(app, *, all_file=False):
    state = initialize(app)
    page, source = state["page"], state["page_source"]
    if not page or not source:
        app.fail("No published source page to copy")
        return
    if not all_file and state["cursor_deselected"] and state["selection"] is None:
        app.fail("Select a source line before copying; Y copies the entire source")
        return
    from ..clipboard import options
    from .copy import copy_full_log, copy_log_selection

    files, cb = state.get("page_files") or app.logs.files, options(app)
    if source.get("target", _target(files)) != _target(files):
        app.fail(
            "This source belongs to another connection; reopen its original profile"
        )
        return
    if all_file:

        def fn(cancel, progress):
            before = log_scan.snapshot(files, source["path"])
            if before["ident"] != page["snapshot"]["ident"]:
                raise log_scan.SourceChanged(
                    "Log source was replaced; reopen the page before copying"
                )
            return copy_full_log(
                source["path"],
                getattr(app, "state_dir", None),
                files=files,
                **cb,
                cancel=cancel,
                progress=progress,
            )
    else:
        selection = state["selection"] or (state["page_cursor"], state["page_cursor"])
        lo, hi = sorted(selection)
        chunks = tuple(row["raw"] for row in page["rows"][lo : hi + 1])

        def fn(cancel, progress):
            return copy_log_selection(
                chunks,
                getattr(app, "state_dir", None),
                source_path=source["path"],
                **cb,
                cancel=cancel,
                progress=progress,
            )

    original_selection = state["selection"]

    def complete(value):
        if value.get("status") not in ("ready", "partial"):
            app.fail(value.get("message", "Log copy failed"))
        else:
            if state["page"] is page and state["selection"] == original_selection:
                state["selection"] = None
            app.say(value.get("message", "Log bytes copied"))

    _task(app, "Copying exact source bytes", fn, complete, source=source["path"])


def handle_key(app, key):
    if app.mode not in MODES:
        return False
    state = initialize(app)
    if key in ("up", "down", "pgup", "pgdn", "home", "end"):
        S.resume(app, "log-tools:document")
    if key in ("esc", "q"):
        app.mode = (
            state["page_return"]
            if app.mode == "log_tools_page"
            else state["return_mode"]
        )
        state["selection"] = None
        return True
    if key == ":":
        from ..command_ui import open_palette

        open_palette(app)
        return True
    if key == "c" and state["busy"]:
        _, task = app.activity.snapshot()
        if task:
            task["cancel"].set()
        app.say("Log cancellation requested")
        return True
    mode = app.mode
    if mode == "log_tools_page":
        page = state["page"]
        if page is None:
            return True
        n = len(page["rows"])
        visible = max(1, getattr(app, "height", 24) - 9)
        if key in ("[", "]"):
            offset = page["previous"] if key == "[" else page["next"]
            line, fragment = None, False
            previous = None
            if key == "[" and state["page_history"]:
                previous = state["page_history"][-1]
                offset, line, fragment = previous
            elif key == "]" and page["rows"]:
                last = page["rows"][-1]
                fragment = last["partial_end"]
                line = (
                    last["line"] + (0 if fragment else 1)
                    if last["line"] is not None
                    else None
                )
            if key == "]" and offset >= page["size"]:
                app.say("End of the inspected source snapshot")
            elif _show_page(
                app,
                state["page_source"],
                offset,
                line=line,
                fragment=fragment,
                expected=page["snapshot"]["ident"],
                return_mode=state["page_return"],
            ):
                if key == "]":
                    first = page["rows"][0] if page["rows"] else {}
                    state["page_history"].append(
                        (
                            page["start"],
                            first.get("line"),
                            first.get("partial_start", False),
                        )
                    )
                    state["page_history"] = state["page_history"][-64:]
                elif previous is not None:
                    state["page_history"].pop()
        elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
            state["cursor_deselected"] = False
            step = {
                "up": -1,
                "down": 1,
                "pgup": -max(1, visible - 1),
                "pgdn": max(1, visible - 1),
                "home": -n,
                "end": n,
            }[key]
            state["page_cursor"] = max(
                0, min(max(0, n - 1), state["page_cursor"] + step)
            )
            if state["selection"] is not None:
                state["selection"] = (state["selection"][0], state["page_cursor"])
        elif key in ("left", "right"):
            state["page_pan"] = max(
                0,
                min(
                    log_scan.PAGE_BYTES,
                    state["page_pan"] + (-8 if key == "left" else 8),
                ),
            )
        elif key == "v":
            state["cursor_deselected"] = False
            state["selection"] = (
                None
                if state["selection"] is not None
                else (state["page_cursor"], state["page_cursor"])
            )
        elif key == "y":
            _copy_page(app)
        elif key == "Y":
            _copy_page(app, all_file=True)
        return True
    collection = (
        (state["results"] or {}).get("matches", [])
        if mode == "log_tools_results"
        else state["marks"]
    )
    field = "result_cursor" if mode == "log_tools_results" else "mark_cursor"
    if key in ("up", "down", "pgup", "pgdn", "home", "end"):
        state["cursor_deselected"] = False
        step = {
            "up": -1,
            "down": 1,
            "pgup": -max(1, getattr(app, "height", 24) - 10),
            "pgdn": max(1, getattr(app, "height", 24) - 10),
            "home": -len(collection),
            "end": len(collection),
        }[key]
        state[field] = max(0, min(max(0, len(collection) - 1), state[field] + step))
    elif key == "enter" and collection:
        if state["cursor_deselected"]:
            app.say(
                "Select a log result or mark with arrows or a click before opening it"
            )
            return True
        row = collection[state[field]]
        identity = (
            row["snapshot"]["ident"] if mode == "log_tools_results" else row["identity"]
        )
        _show_page(
            app,
            row["source"],
            row["offset"],
            line=row.get("line"),
            expected=identity,
            return_mode=mode,
            fragment=bool(row.get("fragment")),
        )
    elif key in ("delete", "d") and mode == "log_tools_marks" and collection:
        if state["cursor_deselected"]:
            app.say("Select a log mark with arrows or a click before deleting it")
            return True
        collection.pop(state[field])
        state[field] = min(state[field], max(0, len(collection) - 1))
        app.say("Log mark removed")
    return True


def handle_mouse(app, y, x, button="left", shift=False):
    if app.mode not in MODES:
        return False
    state = initialize(app)
    if button == "right":
        clear_lines(app)
        app.say(
            "Log line selection cleared; click a line or use arrows to select again"
        )
        return True
    if button in ("left", "double"):
        for row, _, value in state["control_hits"]:
            if row == y and value["left"] <= x < value["right"]:
                if state.get("control_token") == _mouse_context(app):
                    handle_key(app, value["action"][1])
                return True
    if state.get("source_token") != _mouse_context(app)[:6]:
        return True
    hit = state["mouse_rows"].get(y)
    if hit is None or not hit[1] <= x < hit[2] or button not in ("left", "double"):
        return True
    index = hit[0]
    S.resume(app, "log-tools:document")
    state["cursor_deselected"] = False
    if app.mode == "log_tools_page":
        if shift:
            anchor = (
                state["selection"][0] if state["selection"] else state["page_cursor"]
            )
            state["selection"] = (anchor, index)
        state["page_cursor"] = index
    else:
        state["result_cursor" if app.mode == "log_tools_results" else "mark_cursor"] = (
            index
        )
        if button == "double":
            handle_key(app, "enter")
    return True


def _pan(text, offset, max_chars=1024):
    if not offset:
        return text[:max_chars]
    if text.isascii():
        return text[offset : offset + max_chars]
    used, position = 0, 0
    for position, char in enumerate(text):
        used += L.vlen(char)
        if used >= offset:
            position += 1
            break
    else:
        return ""
    while position < len(text) and L.vlen(text[position]) == 0:
        position += 1
    return text[position : position + max_chars]


def overlay(views, snap, app, width, height):
    if app.mode not in MODES:
        return None
    state, g = initialize(app), views.g
    rows, indices = [], {}
    from ..control_rows import buttons, place_hits

    action_rows, action_hits = buttons(
        g,
        max(1, width - 8),
        _mouse_choices(app),
        group="log-tool-actions",
        prefix="log-tool-action:",
    )
    state["control_hits"] = []
    action_height = len(action_rows)
    top_field = (
        "page_top"
        if app.mode == "log_tools_page"
        else "result_top"
        if app.mode == "log_tools_results"
        else "mark_top"
    )
    scroll_context = (
        app.mode,
        id(state.get("page"))
        if app.mode == "log_tools_page"
        else id(state.get("results"))
        if app.mode == "log_tools_results"
        else id(state["marks"]),
        getattr(app, "log_job", None),
        width,
    )
    from ..scrolling import viewport

    if app.mode == "log_tools_page":
        page = state["page"]
        if page is None:
            result = L.box(
                g,
                action_rows + [[("No source page loaded", "dim")]],
                width,
                height,
                "Source page",
            )
            state["control_hits"] = place_hits(action_hits, result[1:-1])
            state["control_token"] = _mouse_context(app)
            state["source_token"] = state["control_token"][:6]
            state["mouse_rows"] = {}
            return result
        if state.get("page_pan_page") is not page:
            state["page_pan_cache"].clear()
            state["page_pan_page"] = page
        source = state["page_source"]
        rows += [
            [(clean(source.get("label", source["path"]), g.ascii), "accent+bold")],
            [(clean(source["path"], g.ascii), "dim")],
            [
                (
                    f"Bytes {page['start']:,}..{page['end']:,} / {page['size']:,}; "
                    + ("complete source" if page["complete"] else "partial source page")
                    + ("; line fragment" if page["partial"] else "")
                    + (f"; pan {state['page_pan']}" if state["page_pan"] else ""),
                    "cyan",
                )
            ],
        ]
        n = len(page["rows"])
        cursor = min(state["page_cursor"], max(0, n - 1))
        available = max(1, height - 9 - action_height)
        start = (
            state[top_field]
            if S.manual(app, "log-tools:document", context=scroll_context) is not None
            else max(0, cursor - available // 2)
        )
        start = max(0, min(start, max(0, n - available)))
        state[top_field] = start
        painted = viewport(
            app, "log-tools:document", start, n, available, context=scroll_context
        )
        for index, row in enumerate(
            page["rows"][painted : painted + available], painted
        ):
            label = (
                f"L{row['line']}" if row["line"] is not None else f"B{row['offset']}"
            )
            chosen = state["selection"] is not None and min(
                state["selection"]
            ) <= index <= max(state["selection"])
            marker = "*" if g.ascii else "◆"
            budget = max(256, width * 8)
            cache_key = (id(page), row["offset"], state["page_pan"], budget)
            cache = state["page_pan_cache"]
            if cache_key not in cache:
                cache[cache_key] = _pan(row["text"], state["page_pan"], budget)
                while len(cache) > 128:
                    cache.popitem(last=False)
            else:
                cache.move_to_end(cache_key)
            presented = cache[cache_key]
            text = L.cut(
                clean(f" {label} {presented}", g.ascii, limit=max(256, width * 8)),
                max(0, width - 7),
                g.ascii,
            )
            indices[len(rows)] = index
            pad = " " * max(0, width - 6 - L.vlen(text))
            rows.append(
                [
                    (
                        text + pad,
                        "sel"
                        if index == cursor and not state["cursor_deselected"]
                        else "",
                    ),
                    (" " + marker if chosen else "  ", "orange+bold"),
                ]
            )
        if not n:
            rows.append([("Empty source page", "dim")])
        rows += [
            [
                (
                    "Arrows/PgUp/PgDn move | Left/Right pan | [/] previous/next file page",
                    "dim",
                )
            ],
            [
                (
                    "v select | y copy selected raw lines | Y copy entire source | Esc back",
                    "dim",
                )
            ],
        ]
        title = "Source page"
    else:
        results = state["results"] or {}
        is_results = app.mode == "log_tools_results"
        collection = results.get("matches", []) if is_results else state["marks"]
        cursor = state["result_cursor" if is_results else "mark_cursor"]
        cursor = min(cursor, max(0, len(collection) - 1))
        rows.append(
            [
                (
                    (
                        f"{len(collection)} matches; "
                        + (
                            "complete snapshot"
                            if results.get("complete")
                            else "partial coverage"
                        )
                    )
                    if is_results
                    else f"{len(collection)} named log marks",
                    "accent+bold",
                )
            ]
        )
        if is_results:
            rows.append(
                [(clean("Search: " + results.get("query", ""), g.ascii), "cyan")]
            )
            reports = results.get("reports", [])
            done = sum(r.get("scanned", 0) for r in reports)
            total = sum(r.get("size", 0) for r in reports)
            errors = sum(bool(r.get("error")) for r in reports)
            omitted = sum(r.get("long_regex_lines", 0) for r in reports)
            rows.append(
                [
                    (
                        f"{len(reports)} sources; {done:,}/{total:,} bytes; {errors} source errors; {omitted} overlong regex lines omitted",
                        "dim",
                    )
                ]
            )
        available = max(1, height - 12 - action_height)
        n = len(collection)
        start = (
            state[top_field]
            if S.manual(app, "log-tools:document", context=scroll_context) is not None
            else max(0, cursor - available // 2)
        )
        start = max(0, min(start, max(0, n - available)))
        state[top_field] = start
        painted = viewport(
            app, "log-tools:document", start, n, available, context=scroll_context
        )
        for index, row in enumerate(collection[painted : painted + available], painted):
            label = row["source"].get("label", row["source"]["path"])
            location = f"L{row['line']}" if row.get("line") else f"B{row['offset']}"
            text = f" {label} {location}: " + (
                row["text"] if is_results else row["name"]
            )
            indices[len(rows)] = index
            rows.append(
                [
                    (
                        clean(text, g.ascii),
                        "sel"
                        if index == cursor and not state["cursor_deselected"]
                        else "",
                    )
                ]
            )
        if collection and is_results:
            current = collection[cursor]
            rows.append([(clean(current["source"]["path"], g.ascii), "cyan")])
            for context in current["before"][-1:] + current["after"][:1]:
                rows.append(
                    [(clean(f" L{context['line']} {context['text']}", g.ascii), "dim")]
                )
        elif not collection:
            rows.append(
                [
                    (
                        "No matches or marks. Each source report below states its coverage."
                        if is_results
                        else "Use :logmark NAME to save the current source line.",
                        "dim",
                    )
                ]
            )
        if is_results:
            for report in results.get("reports", [])[:2]:
                rows.append(
                    [
                        (
                            clean(
                                report["source"].get("label", report["source"]["path"])
                                + ": "
                                + (
                                    report.get("error")
                                    or (
                                        "complete"
                                        if report.get("complete")
                                        else "partial coverage"
                                    )
                                ),
                                g.ascii,
                            ),
                            "yellow" if not report.get("complete") else "dim",
                        )
                    ]
                )
                for gap in report.get("uncovered", ()):
                    rows.append(
                        [
                            (
                                f"  Unsearched bytes [{gap['start']}, {gap['end']}); newest bounded window scanned",
                                "dim",
                            )
                        ]
                    )
        rows.append(
            [
                (
                    "Arrows/PgUp/PgDn browse | Enter open exact source | Esc back"
                    + (" | d delete" if not is_results else ""),
                    "dim",
                )
            ]
        )
        title = "Log search" if is_results else "Log marks"
    result = L.box(g, action_rows + rows, width, height, "   " + title)
    state["control_hits"] = place_hits(action_hits, result[1:-1])
    state["control_token"] = _mouse_context(app)
    state["source_token"] = state["control_token"][:6]
    state["mouse_rows"] = {}
    for logical, index in indices.items():
        logical += action_height
        if logical + 1 < len(result) - 1:
            y, x, rendered = result[logical + 1]
            state["mouse_rows"][y] = (
                index,
                x + 1,
                x + L.vlen(L.row_text(rendered)) - 1,
            )
    if state["mouse_rows"] and len(result) >= 3:
        first_y = min(state["mouse_rows"])
        bottom = max(state["mouse_rows"]) + 1
        _, left, first = result[1]
        right = left + L.vlen(L.row_text(first))
        S.register(
            app,
            "log-tools:document",
            (first_y, left + 1, bottom, right),
            n,
            available,
            start,
            painted,
            lambda value: state.__setitem__(top_field, value),
            context=scroll_context,
            header=(result[0][0], left + 1, right - 1),
            layer=1,
            absolute=True,
        )
    return result


def restore_location(app, context):
    """Reload a portable source-page descriptor through the bounded worker."""
    if not isinstance(context, dict) or not isinstance(
        context.get("page_source"), dict
    ):
        app.fail("The saved log location has no exact source")
        return False
    source = dict(context["page_source"])
    offset, identity = context.get("offset", 0), context.get("identity")
    if (
        not isinstance(source.get("path"), str)
        or not source["path"]
        or type(offset) is not int
        or offset < 0
    ):
        app.fail("The saved log location is invalid")
        return False
    if identity is not None and (
        not isinstance(identity, (list, tuple))
        or len(identity) != 2
        or any(type(v) is not int or v < 0 for v in identity)
    ):
        app.fail("The saved log identity is invalid")
        return False

    def ready(page):
        state = initialize(app)
        cursor, pan = context.get("page_cursor", 0), context.get("page_pan", 0)
        state["page_cursor"] = (
            max(0, min(cursor, max(0, len(page["rows"]) - 1)))
            if type(cursor) is int
            else 0
        )
        state["page_pan"] = (
            max(0, min(pan, log_scan.PAGE_BYTES)) if type(pan) is int else 0
        )

    return _show_page(
        app,
        source,
        offset,
        expected=identity,
        line=context.get("line"),
        fragment=context.get("fragment") is True,
        return_mode="main",
        onloaded=ready,
    )


def _mouse_context(app):
    state = initialize(app)
    mode = getattr(app, "mode", "main")
    source = state.get("page_source") or {}
    selected = (
        state["page_cursor"]
        if mode == "log_tools_page"
        else state["result_cursor"]
        if mode == "log_tools_results"
        else state["mark_cursor"]
    )
    collection = state["marks"]
    mark = (
        collection[min(selected, len(collection) - 1)]
        if collection and mode == "log_tools_marks"
        else None
    )
    return (
        mode,
        state["generation"],
        state["busy"],
        id(state.get("page")),
        id(state.get("results")),
        source.get("path"),
        selected,
        state["cursor_deselected"],
        id(mark) if mark is not None else None,
        len(collection),
        state["selection"],
    )


def _mouse_choices(app):
    state = initialize(app)
    choices = [("close", "Back", "esc")]
    if app.mode == "log_tools_page" and state.get("page") is not None:
        choices += [
            ("previous", "Previous page", "["),
            ("next", "Next page", "]"),
            ("pan-left", "Pan left", "left"),
            ("pan-right", "Pan right", "right"),
            ("select", "Select lines", "v"),
            ("copy", "Copy selection", "y"),
            ("copy-source", "Copy source", "Y"),
        ]
    elif app.mode != "log_tools_page":
        choices.append(("open", "Open source", "enter"))
        if app.mode == "log_tools_marks":
            choices.append(("delete", "Delete mark", "d"))
    if state["busy"]:
        choices.append(("cancel", "Cancel operation", "c"))
    return [(identifier, label, ("key", key)) for identifier, label, key in choices]
