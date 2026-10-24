"""Bounded pure layout for a published run-level trace query."""

from __future__ import annotations

import hashlib
import json

from .. import layout as L
from ..control_rows import buttons
from .timeline_ui import duration
from .workspace_ui_navigation import lane_key, matches
from .workspace_ui_state import (
    MAX_INTERVALS,
    _available_bounds,
    _bounds,
    _clock,
    _collapsed,
    _mode,
    _selected,
    _text,
    _window,
    initialize,
)

MAX_LANES = 16
COLORS = ("cyan", "green", "yellow", "magenta", "blue")


def _append(rows, text, style, width, *, maximum=8192):
    """Wrap safe details without losing a full symbol at ordinary widths."""
    text = _text(text, maximum)
    current, cells = "", 0
    for char in text:
        size = L.vlen(char)
        if size > width:
            char, size = "?", 1
        if cells + size > width:
            rows.append([(current, style)])
            current, cells = "", 0
        current += char
        cells += size
        if len(rows) >= 2048:
            return
    rows.append([(current, style)])


def _controls(views, choices, job_id, width, selected=None):
    rows, hits = buttons(
        views.g,
        width,
        [
            (key, label, ("command", "trace workspace " + action))
            for key, label, action in choices
        ],
        selected=selected,
        group="trace-workspace",
        prefix="trace-workspace:",
    )
    return rows, [
        (
            y,
            "job_panel_action",
            (
                ("trace_command", {"job": job_id, "command": value["action"][1]}),
                value["left"],
                value["right"],
            ),
        )
        for y, _, value in hits
    ]


def _group_id(clock, path):
    return hashlib.sha256(
        json.dumps([clock, path], ensure_ascii=True).encode()
    ).hexdigest()[:16]


def _prepared(result, view):
    projected = result.get("workspace_projection")
    if (
        isinstance(projected, dict)
        and result.get("workspace_projection_name", view["projection"])
        == view["projection"]
    ):
        return projected
    return result


def _lanes(result, view, clock, first, last):
    """Layout only the bounded, already published query rows."""
    from .workspace_ui_filters import prepared as filters_prepared

    if not filters_prepared(result, view):
        return []
    prepared = _prepared(result, view)
    known = {
        str(lane.get("id", lane.get("lane_id", ""))): lane
        for lane in prepared.get("lanes", ())
        if isinstance(lane, dict)
    }
    items = list(prepared.get("intervals", ()))
    expanded = {
        row.get("detail_id") for row in items if row.get("kind") == "cpu_sample"
    }
    items += [
        row
        for row in prepared.get("markers", ())
        if row.get("kind") == "cpu_sample"
        and row.get("id") not in expanded
        or view["projection"] == "cpu"
        and row.get("kind") == "cpu_observation"
    ]
    if view["projection"] == "cpu":
        items = [
            row
            for row in items
            if row.get("kind")
            in {"cpu_observation", "scheduling", "cpu_sample", "cpu_stack_slice"}
        ]
    elif view["projection"] == "gpu":
        items = [
            row
            for row in items
            if row.get("kind")
            in {"gpu_kernel", "gpu_copy", "gpu_sync", "gpu_synchronization"}
        ]
    source_map = {source.get("id"): source for source in result.get("sources", ())}
    grouped, seen = {}, set()
    for row in items[: MAX_INTERVALS * 2]:
        if row.get("id") in seen:
            continue
        seen.add(row.get("id"))
        if not matches(row, view):
            continue
        if clock is not None and row.get("clock_id") != clock:
            continue
        start, end = row.get("start_ns"), row.get("end_ns", row.get("start_ns"))
        if (
            type(start) is not int
            or type(end) is not int
            or end < first
            or start > last
        ):
            continue
        if view["query"] and view["query"] not in str(row.get("name", "")).casefold():
            continue
        key = lane_key(row)
        lane = known.get(key, {})
        source = source_map.get(row.get("source_id"), {})
        identity = source.get("identity") or {}
        node = _text(
            lane.get("node") or row.get("node") or identity.get("node", "unknown node"),
            128,
        )
        path = lane.get("path")
        if not isinstance(path, (list, tuple)) or not path:
            process = "PID " + str(identity.get("pid", "?"))
            if identity.get("rank") is not None:
                process += " / rank " + str(identity["rank"])
            if view["projection"] == "cpu":
                data = row.get("data") or {}
                path = [
                    "Node " + node,
                    "Socket " + str(row.get("socket_id", data.get("socket_id", "?"))),
                    "Core " + str(row.get("core_id", data.get("core_id", "?"))),
                    "Logical CPU "
                    + str(row.get("logical_cpu", data.get("logical_cpu", "?"))),
                    "Thread " + str(row.get("thread", "?")),
                ]
            elif view["projection"] == "gpu":
                data = row.get("data") or {}
                device = row.get("device", data.get("device", "unknown"))
                if isinstance(device, dict):
                    device = (
                        device.get("uuid")
                        or device.get("id")
                        or device.get("name")
                        or "local ordinal " + str(device.get("ordinal", "unknown"))
                    )
                path = [
                    "Node " + node,
                    "GPU " + str(device),
                    "Context "
                    + str(row.get("context", data.get("context", "unknown"))),
                    "Stream " + str(row.get("queue", data.get("queue", "unknown"))),
                    str(row.get("kind", "activity")),
                ]
            else:
                path = [
                    "Node " + node,
                    process,
                    "Thread " + str(row.get("thread", "?")),
                    str(row.get("kind", "call")) + " depth " + str(row.get("depth", 0)),
                ]

        path = [_text(value, 256) for value in path[:8]]
        path_ids = lane.get("path_ids")
        if (
            not isinstance(path_ids, list)
            or len(path_ids) != len(path)
            or any(not isinstance(value, str) for value in path_ids)
        ):
            path_ids = path
        current = grouped.setdefault(
            key,
            dict(
                id=key,
                path=path,
                path_ids=path_ids,
                rows=[],
                clock_id=row.get("clock_id"),
            ),
        )
        current["rows"].append(row)
    return sorted(grouped.values(), key=lambda row: (tuple(row["path"]), row["id"]))


def _draw_bar(views, row, view, first, last, width):
    cells = max(1, width)
    start, end = row["start_ns"], row.get("end_ns", row["start_ns"])
    x = max(0, min(cells - 1, (start - first) * cells // max(1, last - first)))
    right = max(
        x + 1,
        min(cells, ((end - first) * cells + last - first - 1) // max(1, last - first)),
    )
    point = row.get("point") is True or row.get("kind") in {
        "cpu_observation",
        "cpu_sample",
    }
    if point:
        x = max(
            0,
            min(
                cells - 1,
                (((start + end) // 2) - first) * cells // max(1, last - first),
            ),
        )
        right = x + 1
    style = (
        "sel+bold"
        if row.get("id") == view.get("selected")
        else COLORS[int(row.get("depth", 0)) % len(COLORS)]
    )
    label = (
        ("*" if views.g.ascii else "◆")
        if point
        else L.cut(_text(row.get("name", "unnamed")), right - x, views.g.ascii)
    )
    fill = "-" if views.g.ascii else "━"
    label += fill * max(0, right - x - L.vlen(label))
    return x, right, label, style


def _layout_entries(result, view, clocks):
    groups, flat, seen, count = [], [], set(), 0
    for clock in clocks:
        first, last = _window(view, result, clock)
        lanes = _lanes(result, view, clock, first, last)
        count += len(lanes)
        if view.get("all_clocks"):
            flat.append(dict(type="clock", clock_id=clock))
        pinned = [lane for lane in lanes if lane["id"] in view["pinned"]]
        if pinned:
            flat.append(dict(type="pin-heading", clock_id=clock))
            flat.extend(dict(type="lane", lane=lane, clock_id=clock) for lane in pinned)
        for lane in lanes:
            if lane["id"] in view["pinned"]:
                continue
            hidden = False
            for depth in range(max(1, len(lane["path"]) - 1)):
                path = lane["path"][: depth + 1]
                identifier = _group_id(clock, lane["path_ids"][: depth + 1])
                if identifier not in seen:
                    seen.add(identifier)
                    groups.append(identifier)
                    flat.append(
                        dict(
                            type="group",
                            id=identifier,
                            depth=depth,
                            label=path[-1],
                            clock_id=clock,
                        )
                    )
                if _collapsed(view, identifier):
                    hidden = True
                    break
            if not hidden:
                flat.append(dict(type="lane", lane=lane, clock_id=clock))
        if view.get("all_clocks") and not lanes:
            flat.append(dict(type="empty", clock_id=clock))
    return count, groups, flat


def render(views, state, job_id, width):
    """Draw a bounded published snapshot; no trace/index/file access occurs."""
    width = max(1, width)
    view = initialize(state)
    result = state.get("workspace_result") or {}
    clock = _clock(view, result)
    first, last = _window(view, result, clock)
    mode = _mode(view, result)
    primary = [
        (
            "follow",
            "Pause view" if mode == "live" else "Resume Live",
            "pause" if mode == "live" else "live",
        ),
        ("fit", "Fit", "fit"),
        ("view", "View: " + view["projection"], "view-next"),
        (
            "delta",
            ("Window " if views.g.ascii else "Δ ") + duration(view["delta_ns"]),
            "delta-next",
        ),
        ("find", "Find", "search"),
        (
            "controls",
            "Less" if view["advanced"] else "More..." if views.g.ascii else "More…",
            "controls",
        ),
    ]
    rows, hits = _controls(
        views, primary, job_id, width, selected="follow" if mode == "live" else None
    )
    if view["advanced"]:
        secondary = [
            ("zoom-in", "+", "zoom-in"),
            ("zoom-out", "-", "zoom-out"),
            ("pan-left", "Pan <", "pan-left"),
            ("pan-right", "Pan >", "pan-right"),
            ("previous", "Previous", "previous"),
            ("next", "Next", "next"),
            ("parent", "Parent call", "parent"),
            ("child", "Child call", "child"),
            ("pin", "Pin / unpin lane", "pin"),
            ("bookmark", "Bookmark", "bookmark"),
            ("bookmark-note", "Annotate range", "bookmark-note"),
            ("bookmarks", "Saved ranges", "bookmarks-next"),
            ("functions", "Functions", "functions"),
            ("analysis", "Investigate: " + view["analysis_mode"], "analysis-next"),
            ("symbol", "Resolve source / symbol", "symbol-prompt"),
            ("capacity", "Allocation / coverage context", "capacity-prompt"),
            ("filters", "Filters", "filters"),
            ("expand", "Expand", "expand"),
            ("collapse", "Collapse", "collapse"),
            ("source-next", "Source", "source-next"),
            ("thread-next", "Thread", "thread-next"),
            (
                "delta-custom",
                "Custom window" if views.g.ascii else "Custom Δ",
                "delta-custom",
            ),
        ]
        more, more_hits = _controls(views, secondary, job_id, width)
        hits.extend((len(rows) + y, kind, value) for y, kind, value in more_hits)
        rows.extend(more)
    from .workspace_ui_filters import render as render_filters

    filter_rows, filter_hits = render_filters(
        views, view, result, job_id, width, _append, _controls
    )
    hits.extend((len(rows) + y, kind, value) for y, kind, value in filter_hits)
    rows.extend(filter_rows)
    identity = result.get("identity") or {}
    _append(
        rows,
        "Run "
        + _text(identity.get("job_id", job_id), 128)
        + " / attempt "
        + _text(identity.get("attempt", "unknown"), 256),
        "heading",
        width,
    )
    _append(
        rows,
        ("Live following" if mode == "live" else "Explore")
        + " / "
        + (
            duration(last - first) + " window"
            if clock is not None
            else "independent local windows"
        )
        + " / "
        + (
            "indexed prefix caught up"
            if result.get("caught_up")
            else "indexing committed evidence"
        ),
        "dim",
        width,
    )
    bounds = _bounds(result)
    if clock is not None:
        _append(
            rows,
            "Clock "
            + _text(clock, 256)
            + " / "
            + str(first)
            + " - "
            + str(last)
            + " ns",
            "dim",
            width,
        )
    available_bounds = _available_bounds(result)
    if len(available_bounds) > 1:
        extra, extra_hits = _controls(
            views,
            [
                ("clock", "Next clock domain", "clock-next"),
                (
                    "all-clocks",
                    "All nodes / local axes"
                    if not view.get("all_clocks")
                    else "One clock domain",
                    "clocks-all",
                ),
            ],
            job_id,
            width,
        )
        hits.extend((len(rows) + y, kind, value) for y, kind, value in extra_hits)
        rows.extend(extra)
        _append(
            rows,
            str(len(bounds))
            + " clock domains; each has its own axis. No cross-node simultaneity is inferred.",
            "warning",
            width,
        )
    if clock in _bounds({"bounds": result.get("aligned_bounds", [])}):
        _append(
            rows,
            "Calibrated reference axis / producer-declared transform; inspect each interval's uncertainty and original clock. Not independently qualified.",
            "warning",
            width,
        )
    if result:
        _append(
            rows,
            str(len(result.get("sources", ())))
            + " sources / "
            + str(len(result.get("intervals", ())))
            + " queried intervals / "
            + str(len(result.get("markers", ())))
            + " queried markers",
            "dim",
            width,
        )
    from .workspace_ui_overview import render as render_overview

    overview_rows, overview_hits = render_overview(
        state.get("workspace_overview"), clock, job_id, width, views.g.ascii
    )
    hits.extend((len(rows) + y, kind, value) for y, kind, value in overview_hits)
    rows.extend(overview_rows)
    if view["query"]:
        _append(rows, "Filter: " + view["query"], "cyan", width)
    if view["source_ids"]:
        _append(rows, "Source filter: " + ", ".join(view["source_ids"]), "cyan", width)
    if view["thread"] is not None:
        _append(rows, "Thread filter: " + view["thread"], "cyan", width)
    for name, value in view["filters"].items():
        _append(rows, name.replace("_", " ") + " filter: " + str(value), "cyan", width)
    if state.get("error"):
        _append(rows, "Read error: " + _text(state["error"]), "warning", width)
    if state.get("workspace_preferences_notice"):
        _append(rows, state["workspace_preferences_notice"], "warning", width)
    if not result:
        _append(
            rows,
            "Preparing the exact-attempt trace workspace in the background.",
            "dim",
            width,
        )
        return rows, hits
    clocks = list(bounds) if view.get("all_clocks") else [clock]
    signature = (
        view["projection"],
        view["query"],
        tuple(view["collapsed"]),
        tuple(view["pinned"]),
        tuple(view["source_ids"]),
        view["thread"],
        tuple(view["filters"].items()),
        tuple((domain, *_window(view, result, domain)) for domain in clocks),
    )
    prepared = _prepared(result, view)
    cached = state.get("_workspace_layout_cache")
    if (
        cached is not None
        and cached["result"] is result
        and cached["prepared"] is prepared
        and cached["signature"] == signature
    ):
        lanes, groups, flat = cached["layout"]
    else:
        lanes, groups, flat = _layout_entries(result, view, clocks)
        state["_workspace_layout_cache"] = dict(
            result=result,
            prepared=prepared,
            signature=signature,
            layout=(lanes, groups, flat),
        )
    state["workspace_groups"] = groups
    page_size = MAX_LANES * 3
    pages = max(1, (len(flat) + page_size - 1) // page_size)
    page = min(max(0, view["lane_page"]), pages - 1)
    if pages > 1:
        extra, extra_hits = _controls(
            views,
            [
                ("lanes-previous", "Previous lanes", "lanes-previous"),
                ("lanes-next", "Next lanes", "lanes-next"),
            ],
            job_id,
            width,
        )
        hits.extend((len(rows) + y, kind, value) for y, kind, value in extra_hits)
        rows.extend(extra)
        _append(rows, f"Lane page {page + 1} / {pages}", "dim", width)
    drawn = 0
    previous_clock = object()
    for entry in flat[page * page_size : (page + 1) * page_size]:
        domain = entry["clock_id"]
        if view.get("all_clocks") and domain != previous_clock:
            first, last = _window(view, result, domain)
            _append(
                rows,
                "Clock "
                + _text(domain, 256)
                + " / local axis "
                + str(first)
                + " - "
                + str(last)
                + " ns",
                "heading",
                width,
            )
            mini, mini_hits = render_overview(
                state.get("workspace_overview"), domain, job_id, width, views.g.ascii
            )
            hits.extend((len(rows) + y, kind, value) for y, kind, value in mini_hits)
            rows.extend(mini)
            previous_clock = domain
        if entry["type"] == "clock":
            continue
        if entry["type"] == "pin-heading":
            _append(
                rows, "Pinned lanes / visible through group collapse", "heading", width
            )
            continue
        if entry["type"] == "empty":
            _append(
                rows,
                "No recorded "
                + view["projection"]
                + " intervals for this domain/range; missing capture remains unknown.",
                "dim",
                width,
            )
            continue
        if entry["type"] == "group":
            closed = _collapsed(view, entry["id"])
            arrow = (
                (">" if closed else "v") if views.g.ascii else ("▸" if closed else "▾")
            )
            label = L.cut(
                " " * min(6, entry["depth"]) + arrow + " " + entry["label"],
                width,
                views.g.ascii,
            )
            rows.append([(label, "heading")])
            if label:
                hits.append(
                    (
                        len(rows) - 1,
                        "job_panel_action",
                        (
                            (
                                "trace_command",
                                {
                                    "job": job_id,
                                    "command": "trace workspace group " + entry["id"],
                                },
                            ),
                            0,
                            L.vlen(label),
                        ),
                    )
                )
            continue
        lane = entry["lane"]
        first, last = _window(view, result, domain)
        _append(rows, " / ".join(lane["path"][-2:]), "muted", width, maximum=512)
        line, cursor = [], 0
        for item in sorted(
            lane["rows"], key=lambda row: (row["start_ns"], str(row.get("id", "")))
        ):
            x, right, label, style = _draw_bar(views, item, view, first, last, width)
            if right <= cursor:
                continue
            x = max(x, cursor)
            if x > cursor:
                line.append((" " * (x - cursor), ""))
            label = L.cut(label, right - x, views.g.ascii)
            line.append((label, style))
            if item.get("id"):
                hits.append(
                    (
                        len(rows),
                        "job_panel_action",
                        (
                            (
                                "trace_command",
                                {
                                    "job": job_id,
                                    "command": "trace workspace frame "
                                    + str(item["id"]),
                                },
                            ),
                            x,
                            right,
                        ),
                    )
                )
            cursor = right
            drawn += 1
            if cursor >= width:
                break
        rows.append(line)
    if not lanes:
        _append(
            rows,
            "No recorded "
            + view["projection"]
            + " intervals match this range. Missing capture remains unknown.",
            "warning",
            width,
        )
    elif not drawn:
        _append(
            rows,
            "Groups are collapsed; Expand restores their recorded lanes.",
            "dim",
            width,
        )
    if view["projection"] == "cpu":
        _append(
            rows,
            "CPU points are bracketed observations, not continuous residency or function CPU time.",
            "dim",
            width,
        )
    if result.get("truncated"):
        _append(
            rows,
            "Range query reached its event bound; zoom/filter to inspect exact calls. The recording is unchanged.",
            "warning",
            width,
        )
    selected = _selected(state, view)
    if selected:
        from .workspace_ui_inspector import render as render_inspector

        render_inspector(rows, selected, width, _append)
        from .workspace_ui_reports import symbol as render_symbol

        render_symbol(rows, selected, state.get("workspace_symbol"), width, _append)
    elif view["selected"]:
        _append(
            rows,
            "Selected event is outside this query. Its stable identity is retained; query its range or choose another event.",
            "dim",
            width,
        )
    else:
        _append(
            rows,
            "Select a bar/point or Next for full recorded duration and source. Pan or selection pauses following.",
            "dim",
            width,
        )
    for warning in result.get("warnings", ())[:16]:
        _append(rows, warning, "warning", width)
    if view["show_functions"]:
        _function_rows(rows, state, width)
    from .workspace_ui_reports import analysis as render_analysis

    analysis_rows, analysis_hits = render_analysis(
        views, state, job_id, width, _append, _controls
    )
    hits.extend((len(rows) + y, kind, value) for y, kind, value in analysis_hits)
    rows.extend(analysis_rows)
    return rows, hits


def _function_rows(rows, state, width):
    report = state.get("workspace_functions")
    _append(rows, "Function totals / recorded elapsed time", "heading", width)
    if not report:
        _append(
            rows,
            "Preparing indexed function statistics in the background.",
            "dim",
            width,
        )
        return
    groups = (
        report.get("functions", report.get("groups", report.get("rows", [])))
        if isinstance(report, dict)
        else report
    )
    for row in groups[:100]:
        if not isinstance(row, dict):
            continue
        value = row.get("inclusive_ns", row.get("total_ns", row.get("sum")))
        label = (
            _text(row.get("name", row.get("function", "unknown")), 512)
            + " / "
            + str(row.get("count", "?"))
            + " calls / "
            + (
                duration(value) + " inclusive"
                if type(value) is int
                else "elapsed unavailable"
            )
        )
        if type(row.get("self_ns")) is int and row.get("self_complete", True):
            label += " / " + duration(row["self_ns"]) + " self"
        if type(row.get("min_ns")) is int and type(row.get("max_ns")) is int:
            label += (
                " / "
                + duration(row["min_ns"])
                + " - "
                + duration(row["max_ns"])
                + " min/max"
            )
        _append(rows, label, "text", width)
    _metric_rows(rows, state, width)
    _append(
        rows,
        "Concurrent durations do not sum to makespan; elapsed time is not CPU utilization or GPU occupancy.",
        "dim",
        width,
    )


def _metric_rows(rows, state, width):
    report = state.get("workspace_metrics")
    if not isinstance(report, dict):
        return
    _append(
        rows, "Resource metrics / declared evidence and denominators", "heading", width
    )
    for metric in report.get("metrics", ())[:128]:
        if not isinstance(metric, dict):
            continue
        value = metric.get("value")
        available = value is not None
        display = (
            str(value) + " " + str(metric.get("unit", ""))
            if available
            else "unavailable: " + str(metric.get("reason", "measurement not provided"))
        )
        _append(
            rows,
            str(metric.get("name", "metric")) + ": " + display,
            "text" if available else "dim",
            width,
        )
        if metric.get("denominator") is not None:
            _append(rows, "Denominator: " + str(metric["denominator"]), "dim", width)
