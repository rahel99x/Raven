"""Trace workspace commands over published state; no capture/index I/O."""

from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation

from .workspace_ui_state import (
    ANALYSES,
    MAX_BOOKMARKS,
    PROJECTIONS,
    _available_bounds,
    _bounds,
    _candidates,
    _clock,
    _rows,
    _selected,
    _text,
    _window,
    initialize,
)


def _seconds(raw):
    if not isinstance(raw, str) or len(raw) > 32:
        raise ValueError("Choose a Live window between 1 ms and 24 hours.")
    raw = raw.strip().lower()
    factor = 1_000_000 if raw.endswith("ms") else 1_000_000_000
    value = raw[:-2] if raw.endswith("ms") else raw[:-1] if raw.endswith("s") else raw
    try:
        number = Decimal(value)
        if not number.is_finite():
            raise InvalidOperation
        nanos = int(number * factor)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError(
            "Choose a numeric Live window, for example 10s or 250ms."
        ) from exc
    if not 1_000_000 <= nanos <= 86_400_000_000_000:
        raise ValueError("Choose a Live window between 1 ms and 24 hours.")
    return nanos


def _request(state, app, *, save=True):
    revision = state.get("workspace_revision", 0)
    state["workspace_revision"] = (revision if type(revision) is int else 0) + 1
    state.update(
        view="workspace",
        summary=False,
        paint=None,
        workspace_dirty=True,
        paused=False,
        next_read=0.0,
    )
    if save:
        callback = getattr(app, "save", None)
        if callable(callback):
            callback()


def command(app, state, args):
    """Explicit commands over published evidence; no worker or file operations."""
    if args[:1] == ["workspace"]:
        args = args[1:]
    if not args:
        _request(state, app)
        return True
    accepted = {
        "live",
        "explore",
        "pause",
        "delta",
        "delta-next",
        "delta-custom",
        "fit",
        "zoom-in",
        "zoom-out",
        "pan-left",
        "pan-right",
        "projection",
        "view-next",
        "clock-next",
        "clock",
        "source",
        "source-location",
        "source-next",
        "thread",
        "thread-next",
        "clocks-all",
        "next",
        "previous",
        "frame",
        "focus",
        "parent",
        "child",
        "event",
        "pin",
        "find",
        "search",
        "lanes-next",
        "lanes-previous",
        "group",
        "collapse",
        "expand",
        "bookmark",
        "bookmark-note",
        "bookmarks-next",
        "functions",
        "source",
        "range",
        "controls",
        "analysis",
        "analysis-next",
        "symbol-prompt",
        "capacity-prompt",
        "filters",
        "facet",
        "facet-next",
        "facet-previous",
        "filter",
        "filter-clear",
    }
    if args[0] not in accepted:
        return False
    view = initialize(state)
    result = state.get("workspace_result") or {}
    action = args[0]
    clock = _clock(view, result)
    first, last = _window(view, result, clock)
    ranges = {key: list(_window(view, result, key)) for key in _bounds(result)}
    try:
        if action in {
            "search",
            "delta-custom",
            "bookmark-note",
            "symbol-prompt",
            "capacity-prompt",
        }:
            from ..command_ui import open_palette

            open_palette(
                app,
                "trace workspace "
                + (
                    "find " + view["query"]
                    if action == "search"
                    else "bookmark "
                    if action == "bookmark-note"
                    else "symbol - "
                    if action == "symbol-prompt"
                    else "capacity "
                    if action == "capacity-prompt"
                    else "delta "
                ),
            )
            return True
        if action == "live":
            if len(args) > 1:
                view["delta_ns"] = _seconds(args[1])
            view.update(mode="live", window=None, domain_windows={})
        elif action in {"pause", "explore"}:
            view.update(
                mode="explore",
                window=[first, last],
                domain_windows=ranges if view.get("all_clocks") else {},
            )
        elif action == "delta":
            if len(args) != 2:
                raise ValueError("Usage: trace workspace delta 10s")
            view["delta_ns"] = _seconds(args[1])
        elif action == "delta-next":
            values = (1_000_000_000, 10_000_000_000, 30_000_000_000, 60_000_000_000)
            current = view["delta_ns"]
            view["delta_ns"] = next(
                (value for value in values if value > current), values[0]
            )
        elif action == "fit":
            view.update(
                mode="explore",
                window=list(_available_bounds(result)[clock])
                if clock in _available_bounds(result)
                else None,
                domain_windows={
                    key: list(value) for key, value in _bounds(result).items()
                }
                if view.get("all_clocks")
                else {},
            )
        elif action in {"zoom-in", "zoom-out", "pan-left", "pan-right"}:
            length = max(1, last - first)
            if action.startswith("zoom"):
                target = max(1, length // 2) if action == "zoom-in" else length * 2
                center = (first + last) // 2
                first, last = center - target // 2, center - target // 2 + target
            else:
                shift = max(1, length // 3) * (-1 if action == "pan-left" else 1)
                first, last = first + shift, last + shift
            if view.get("all_clocks"):
                changed = {}
                for key, (left, right) in ranges.items():
                    span = max(1, right - left)
                    if action.startswith("zoom"):
                        size = max(1, span // 2) if action == "zoom-in" else span * 2
                        center = (left + right) // 2
                        changed[key] = [center - size // 2, center - size // 2 + size]
                    else:
                        shift = max(1, span // 3) * (-1 if action == "pan-left" else 1)
                        changed[key] = [left + shift, right + shift]
                view.update(mode="explore", domain_windows=changed)
            else:
                view.update(mode="explore", window=[first, last])
        elif action in {"projection", "view-next"}:
            selected = (
                args[1]
                if action == "projection" and len(args) == 2
                else PROJECTIONS[
                    (PROJECTIONS.index(view["projection"]) + 1) % len(PROJECTIONS)
                ]
                if action == "view-next"
                else None
            )
            if selected not in PROJECTIONS:
                raise ValueError("Choose execution, cpu, gpu or node.")
            view.update(projection=selected, lane_page=0)
            if selected == "node" and len(_bounds(result)) > 1:
                view.update(all_clocks=True, domain_windows=ranges)
        elif action == "clocks-all":
            view.update(
                all_clocks=not view.get("all_clocks", False),
                domain_windows=ranges,
                window=None,
                lane_page=0,
            )
        elif action in {"clock-next", "clock"}:
            clocks = list(_available_bounds(result))
            if action == "clock" and (len(args) != 2 or args[1] not in clocks):
                raise ValueError(
                    "Choose a raw or calibrated clock ID from the current catalog."
                )
            if clocks:
                position = clocks.index(clock) if clock in clocks else -1
                view.update(
                    clock_id=args[1]
                    if action == "clock"
                    else clocks[(position + 1) % len(clocks)],
                    window=None,
                    lane_page=0,
                    all_clocks=False,
                )
        elif action == "find":
            view.update(query=" ".join(args[1:])[:256].casefold(), lane_page=0)
        elif action in {
            "filters",
            "facet",
            "facet-next",
            "facet-previous",
            "filter",
            "filter-clear",
        }:
            from .workspace_ui_filters import FIELDS, canonical, sanitize

            field = canonical(args[1]) if len(args) > 1 else None
            if action == "filters":
                view["filters_open"] = not view["filters_open"]
            elif action.startswith("facet-"):
                view["facet_page"] = max(
                    0, view["facet_page"] + (1 if action == "facet-next" else -1)
                )
            elif action == "filter-clear" and len(args) == 1:
                view.update(
                    filters={}, thread=None, source_ids=[], facet=None, lane_page=0
                )
            elif field not in FIELDS:
                raise ValueError(
                    "Choose node, process, thread, rank, task, cpu, gpu or kind."
                )
            elif action == "facet":
                view.update(facet=field, facet_page=0, filters_open=True)
            elif action == "filter-clear":
                if field == "thread":
                    view["thread"] = None
                else:
                    view["filters"].pop(field, None)
                view["lane_page"] = 0
            elif len(args) != 3:
                raise ValueError("Usage: trace workspace filter FIELD VALUE")
            elif field == "thread":
                if not args[2].isprintable() or not 0 < len(args[2]) <= 256:
                    raise ValueError("Choose a bounded printable thread identifier.")
                view.update(thread=args[2], lane_page=0)
            else:
                value = int(args[2]) if field == "logical_cpu" else args[2]
                candidate = {**view["filters"], field: value}
                checked = sanitize(candidate)
                if checked != candidate:
                    raise ValueError("That indexed filter value is invalid.")
                view.update(filters=checked, lane_page=0)
                if field == "logical_cpu":
                    view["projection"] = "cpu"
                elif field == "device_id":
                    view["projection"] = "gpu"
        elif action in {"source", "source-next"}:
            if action == "source" and len(args) == 1:
                selected = _selected(state, view)
                app.say(
                    "Recorded source: " + _text(selected.get("file") or "unavailable")
                    if selected
                    else "Select a recorded call first."
                )
                return True
            sources = [
                str(row["id"])
                for row in result.get("sources", ())
                if isinstance(row, dict) and row.get("id")
            ]
            if action == "source-next":
                current = (
                    view["source_ids"][0] if len(view["source_ids"]) == 1 else None
                )
                choices = [None, *sources]
                selected = (
                    choices[(choices.index(current) + 1) % len(choices)]
                    if current in choices
                    else None
                )
                view["source_ids"] = [selected] if selected else []
            elif len(args) == 2 and args[1] == "all":
                view["source_ids"] = []
            elif not args[1:] or any(
                identifier not in sources for identifier in args[1:]
            ):
                raise ValueError("Choose current source IDs, or `source all`.")
            else:
                view["source_ids"] = list(dict.fromkeys(args[1:]))[:512]
            view["lane_page"] = 0
        elif action in {"thread", "thread-next"}:
            if action == "thread-next":
                threads = sorted(
                    {
                        str(row["thread"])
                        for row in _rows(result)
                        if row.get("thread") is not None
                    }
                )
                choices = [None, *threads]
                position = (
                    choices.index(view["thread"]) if view["thread"] in choices else 0
                )
                view["thread"] = choices[(position + 1) % len(choices)]
            elif len(args) != 2 or not args[1].isprintable() or len(args[1]) > 256:
                raise ValueError("Choose a recorded thread ID, or `thread all`.")
            else:
                view["thread"] = None if args[1] == "all" else args[1]
            view["lane_page"] = 0
        elif action == "event":
            if (
                len(args) != 2
                or not args[1].isprintable()
                or not 0 < len(args[1]) <= 512
            ):
                raise ValueError(
                    "Choose an exact event ID from this run's verified index."
                )
            view.update(selected=args[1], selected_detail_id=args[1], mode="explore")
            state.pop("_workspace_selected_interval", None)
        elif action == "pin":
            from .workspace_ui_navigation import lane_key

            selected = _selected(state, view)
            if not selected:
                raise ValueError("Select an event to pin its displayed lane.")
            key = lane_key(selected)
            if key in view["pinned"]:
                view["pinned"].remove(key)
            elif len(view["pinned"]) < 64:
                view["pinned"].append(key)
            else:
                raise ValueError(
                    "Unpin a lane before adding another; at most 64 lanes may be pinned."
                )
        elif action == "range":
            if (
                len(args) != 4
                or len(args[1]) != 16
                or len(args[2]) > 24
                or len(args[3]) > 24
            ):
                raise ValueError("Choose a current overview range.")
            matches = [
                key
                for key in _available_bounds(result)
                if hashlib.sha256(key.encode()).hexdigest()[:16] == args[1]
            ]
            if len(matches) != 1:
                raise ValueError("That overview clock is no longer available.")
            start, end = int(args[2]), int(args[3])
            lower, upper = _available_bounds(result)[matches[0]]
            if not lower <= start < end <= upper:
                raise ValueError("Choose a range within the indexed clock bounds.")
            view.update(
                mode="explore",
                clock_id=matches[0],
                window=[start, end],
                all_clocks=False,
            )
        elif action in {"next", "previous", "frame", "focus", "parent", "child"}:
            candidates = _candidates(state, view, clock)
            selected = _selected(state, view)
            if action == "frame":
                if len(args) != 2:
                    raise ValueError("Choose one recorded event identifier.")
                selected = next(
                    (row for row in candidates if row["id"] == args[1]), None
                )
                if selected is None:
                    raise ValueError(
                        "That interval is no longer in this query; refresh or choose a visible event."
                    )
            elif action in {"parent", "child"}:
                from .workspace_ui_navigation import related

                selected = related(candidates, selected, action)
                if selected is None:
                    raise ValueError(
                        "No unambiguous recorded "
                        + action
                        + " is available in this bounded query."
                    )
            elif action != "focus" and candidates:
                position = next(
                    (
                        index
                        for index, row in enumerate(candidates)
                        if row["id"] == view.get("selected")
                    ),
                    -1,
                )
                selected = candidates[
                    (position + (1 if action == "next" else -1)) % len(candidates)
                ]
            if selected:
                state["_workspace_selected_interval"] = dict(selected)
                view.update(
                    selected=selected["id"],
                    selected_detail_id=selected.get("detail_id") or selected["id"],
                    clock_id=selected.get("clock_id"),
                    mode="explore",
                    domain_windows=ranges if view.get("all_clocks") else {},
                )
                if action in {"focus", "next", "previous", "parent", "child"}:
                    padding = max(
                        1,
                        (
                            selected.get("end_ns", selected["start_ns"])
                            - selected["start_ns"]
                        )
                        // 5,
                    )
                    target = [
                        selected["start_ns"] - padding,
                        selected.get("end_ns", selected["start_ns"]) + padding + 1,
                    ]
                    if view.get("all_clocks"):
                        view["domain_windows"][selected.get("clock_id")] = target
                    else:
                        view["window"] = target
                elif view.get("window") is None:
                    view["window"] = [first, last]
        elif action in {"lanes-next", "lanes-previous"}:
            view["lane_page"] = max(
                0, view["lane_page"] + (1 if action == "lanes-next" else -1)
            )
        elif action == "group":
            if (
                len(args) != 2
                or len(args[1]) != 16
                or any(c not in "0123456789abcdef" for c in args[1])
            ):
                raise ValueError("Choose a current lane group.")
            collapsed = view["collapsed"]
            identifier = "!" + args[1] if "*" in collapsed else args[1]
            if identifier in collapsed:
                collapsed.remove(identifier)
            elif len(collapsed) < 256:
                collapsed.append(identifier)
            view["lane_page"] = 0
        elif action == "collapse":
            view["collapsed"] = ["*"]
            view["lane_page"] = 0
        elif action == "expand":
            view.update(collapsed=[], lane_page=0)
        elif action == "bookmark":
            if view.get("all_clocks") and len(ranges) > 64:
                raise ValueError(
                    "Choose one clock domain or at most 64 domains for a saved range."
                )
            bookmark = dict(
                clock_id=clock,
                window=[first, last],
                selected=view["selected"],
                selected_detail_id=view["selected_detail_id"],
                label=" ".join(args[1:])[:512],
                all_clocks=view.get("all_clocks", False),
                domain_windows=ranges if view.get("all_clocks") else {},
            )
            if bookmark not in view["bookmarks"]:
                view["bookmarks"] = (view["bookmarks"] + [bookmark])[-MAX_BOOKMARKS:]
            app.say(
                "Bookmarked the exact trace range; capture continues independently."
            )
        elif action == "bookmarks-next":
            bookmarks = view["bookmarks"]
            if bookmarks:
                position = next(
                    (
                        index
                        for index, item in enumerate(bookmarks)
                        if item.get("window") == view.get("window")
                        and item.get("clock_id") == clock
                    ),
                    -1,
                )
                view.update(bookmarks[(position + 1) % len(bookmarks)])
                view["mode"] = "explore"
                state.pop("_workspace_selected_interval", None)
                if view.get("label"):
                    app.say("Saved range: " + _text(view["label"], 512))
            else:
                app.say("No bookmarked ranges for this run yet.")
        elif action == "functions":
            view["show_functions"] = not view["show_functions"]
        elif action in {"analysis", "analysis-next"}:
            mode = (
                args[1]
                if action == "analysis" and len(args) == 2
                else ANALYSES[
                    (ANALYSES.index(view["analysis_mode"]) + 1) % len(ANALYSES)
                ]
                if action == "analysis-next"
                else None
            )
            if mode not in ANALYSES:
                raise ValueError(
                    "Choose none, call-tree, bottom-up, causal, counters, hints, metrics or samples."
                )
            view["analysis_mode"] = mode
        elif action == "controls":
            view["advanced"] = not view["advanced"]
        elif action == "source-location":
            selected = _selected(state, view)
            app.say(
                "Recorded source: " + _text(selected.get("file") or "unavailable")
                if selected
                else "Select a recorded call first."
            )
    except (ValueError, TypeError) as exc:
        app.say(str(exc))
        return True
    _request(state, app)
    return True
