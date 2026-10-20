"""Cell-based execution timeline using worker-prepared, verified intervals."""

from __future__ import annotations

from bisect import bisect_left
from itertools import islice

from .. import layout as L
from ..control_rows import buttons

COLORS = ("cyan", "green", "yellow", "magenta", "blue")


def initialize(state):
    return state.setdefault(
        "timeline_view", {"window": None, "thread": None, "selected": None, "query": ""}
    )


def reset(state):
    """Clear source-specific state while retaining the chosen visualization."""
    track = state.get("timeline_view", {}).get("track")
    state.pop("timeline_view", None)
    view = initialize(state)
    if track in {"calls", "cores"}:
        view["track"] = track


def duration(value):
    for unit, scale in (("s", 1_000_000_000), ("ms", 1_000_000), ("us", 1000)):
        if abs(value) >= scale:
            return f"{value / scale:.3g}{unit}"
    return str(value) + "ns"


def _window(model, view):
    origin, extent = model["origin_ns"], max(1, model["end_ns"])
    saved = view.get("window")
    if saved is None:
        return 0, extent
    length = min(extent, max(1, saved[1] - saved[0]))
    start = max(0, min(extent - length, saved[0] - origin))
    return start, start + length


def _thread(model, view):
    threads = model["threads"]
    if view.get("track", "calls") == "cores" and view.get("thread") is None:
        return None
    if view.get("thread") in threads:
        return view["thread"]
    return next(
        (row["thread"] for row in model["intervals"]), threads[0] if threads else None
    )


def _focus(model, view, interval):
    padding = max(
        1, interval.get("duration_ns", interval["end_ns"] - interval["start_ns"]) // 5
    )
    view["window"] = (
        model["origin_ns"] + interval["start_ns"] - padding,
        model["origin_ns"] + interval["end_ns"] + padding,
    )


def command(app, state, args):
    """Handle explicit timeline actions; the trace owner verifies its context."""
    if not args or args[0] not in {
        "zoom-in",
        "zoom-out",
        "pan-left",
        "pan-right",
        "fit",
        "focus",
        "thread-next",
        "thread-previous",
        "frame",
        "next",
        "previous",
        "find",
        "cores",
        "calls",
        "search",
    }:
        return False
    snapshot = state.get("result") or {}
    model, lookup = snapshot.get("timeline"), snapshot.get("timeline_index")
    if not model or not lookup:
        app.say(
            "Waiting for verified trace intervals; recording and unmatched boundaries remain visible in Events."
        )
        return True
    view = initialize(state)
    action = args[0]
    start, end = _window(model, view)
    length = end - start
    if action == "search":
        from ..command_ui import open_palette

        open_palette(app, "trace find " + view["query"])
        return True
    if action in {"cores", "calls"}:
        view.update(track=action, selected=None, thread=None)
        callback = getattr(app, "save", None)
        if callable(callback):
            callback()
    elif action in {"zoom-in", "zoom-out"}:
        target = (
            max(1, length // 2)
            if action == "zoom-in"
            else min(max(1, model["end_ns"]), length * 2)
        )
        center = (start + end) // 2
        view["window"] = (
            model["origin_ns"] + center - target // 2,
            model["origin_ns"] + center - target // 2 + target,
        )
    elif action in {"pan-left", "pan-right"}:
        shift = max(1, length // 3) * (-1 if action == "pan-left" else 1)
        view["window"] = (
            model["origin_ns"] + start + shift,
            model["origin_ns"] + end + shift,
        )
    elif action == "fit":
        view["window"] = None
    elif action == "focus":
        selected = lookup["by_id"].get(view["selected"])
        if selected:
            _focus(model, view, selected)
    elif action in {"thread-next", "thread-previous"}:
        threads = ([None] if view.get("track", "calls") == "cores" else []) + model[
            "threads"
        ]
        if threads:
            current = _thread(model, view)
            view["thread"] = threads[
                (threads.index(current) + (1 if action == "thread-next" else -1))
                % len(threads)
            ]
            view["selected"] = None
    elif action == "frame":
        if (
            len(args) != 3
            or args[1] != model["identity"].get("stream")
            or args[2] not in lookup["by_id"]
        ):
            app.say("That interval is no longer in this process's retained window.")
            return True
        selected = lookup["by_id"][args[2]]
        track = "cores" if selected["id"].startswith("cpu:") else "calls"
        changed_track = view.get("track", "calls") != track
        view.update(selected=args[2], track=track)
        if track != "cores" or view["thread"] is not None:
            view["thread"] = selected["thread"]
        if changed_track:
            callback = getattr(app, "save", None)
            if callable(callback):
                callback()
    else:
        if action == "find":
            view["query"] = " ".join(args[1:])[:256].casefold()
        thread = _thread(model, view)
        candidates = [
            row
            for row in (
                model["cpu"]["observations"]
                if view.get("track", "calls") == "cores"
                else model["intervals"]
            )
            if (thread is None or row["thread"] == thread)
            and (not view["query"] or view["query"] in row["name"].casefold())
        ]
        if candidates:
            current = next(
                (
                    i
                    for i, row in enumerate(candidates)
                    if row["id"] == view["selected"]
                ),
                -1,
            )
            position = (
                0
                if action == "find"
                else (current + (1 if action == "next" else -1)) % len(candidates)
            )
            selected = candidates[position]
            view["selected"] = selected["id"]
            _focus(model, view, selected)
        else:
            app.say(
                "No matching recorded intervals in this thread; choose another thread or clear :trace find."
            )
    state.update(view="timeline", summary=False, paint=None)
    return True


def render(views, state, job_id, width):
    """Render only the published model; per-lane binary search skips old bars."""
    snapshot = state.get("result") or {}
    model, lookup = snapshot.get("timeline"), snapshot.get("timeline_index")
    if model is None or lookup is None:
        return [
            [("Preparing the visual timeline from verified retained events.", "dim")]
        ], []
    view = initialize(state)
    cores = view.get("track", "calls") == "cores"
    thread = _thread(model, view)
    start, end = _window(model, view)
    controls = [
        ("calls", "Calls", "calls"),
        ("cores", "CPU cores", "cores"),
        ("in", "Zoom +", "zoom-in"),
        ("out", "Zoom -", "zoom-out"),
        ("left", "Pan <", "pan-left"),
        ("right", "Pan >", "pan-right"),
        ("fit", "Fit window", "fit"),
        ("focus", "Focus", "focus"),
        ("previous", "Previous", "previous"),
        ("next", "Next", "next"),
        ("search", "Find", "search"),
    ]
    if cores or len(model["threads"]) > 1:
        controls += [
            ("thread_previous", "Previous thread", "thread-previous"),
            ("thread_next", "Next thread", "thread-next"),
        ]
    rows, controls_hits = buttons(
        views.g,
        width,
        [
            (key, label, ("command", "trace " + command))
            for key, label, command in controls
        ],
        selected="cores" if cores else "calls",
        group="trace-timeline",
        prefix="trace-time:",
    )
    hits = [
        (
            y,
            "job_panel_action",
            (
                ("trace_command", {"job": job_id, "command": value["action"][1]}),
                value["left"],
                value["right"],
            ),
        )
        for y, _, value in controls_hits
    ]
    rows.append(
        [
            (
                f"Thread {thread or ('all' if cores else '-')} / {duration(start)} - {duration(end)} / "
                + ("automatic window" if view["window"] is None else "fixed window"),
                "heading",
            )
        ]
    )
    rows.append(
        [
            (
                "CPU observations at points in time; blank space is unknown residency."
                if cores
                else "Recorded wall time; Python depth / separate span lanes. Blank space is unobserved.",
                "dim",
            )
        ]
    )
    rows.append(
        [
            (
                "+/- zoom; Left/Right pan; f fit; n/N calls; t threads. Search: :trace find NAME",
                "dim",
            )
        ]
    )
    if view["query"]:
        rows.append(
            [
                (
                    "Function filter: " + view["query"] + " (clear with :trace find)",
                    "cyan",
                )
            ]
        )
    if cores:
        core_rows, core_hits = _cores(
            model, lookup, view, thread, start, end, job_id, width, views.g.ascii
        )
        hits.extend((len(rows) + y, kind, value) for y, kind, value in core_hits)
        rows.extend(core_rows)
        return rows, hits
    lanes = lookup["threads"].get(thread, {})
    bar_width = max(1, width - 5)
    stream = model["identity"].get("stream", "")
    drawn = bars = 0
    for (kind, depth), (intervals, ends) in sorted(
        lanes.items(), key=lambda item: (item[0][0] != "python", item[0][1])
    ):
        if width < 8:
            break
        if drawn >= 128:
            rows.append(
                [
                    (
                        "Display bounded to 128 depth lanes; inspect the browser timeline for more.",
                        "warning",
                    )
                ]
            )
            break
        if bars >= 2048:
            rows.append(
                [
                    (
                        "Display bounded to 2,048 selectable intervals; zoom or search to inspect smaller calls.",
                        "warning",
                    )
                ]
            )
            break
        first = bisect_left(ends, start)
        row = [(f"{'P' if kind == 'python' else 'S'}{depth:<3}"[:4] + " ", "muted")]
        cursor, regions = 0, []
        for interval in islice(intervals, first, None):
            if interval["start_ns"] > end:
                break
            left = max(
                0,
                min(
                    bar_width - 1,
                    (interval["start_ns"] - start) * bar_width // (end - start),
                ),
            )
            right = max(
                left + 1,
                min(
                    bar_width,
                    ((interval["end_ns"] - start) * bar_width + end - start - 1)
                    // (end - start),
                ),
            )
            # Several short calls may occupy one cell at this zoom. Preserve
            # one selectable representative; zoom reveals the other calls.
            if right <= cursor:
                continue
            left = max(left, cursor)
            if left > cursor:
                row.append((" " * (left - cursor), ""))
            selected = interval["id"] == view["selected"]
            matches = not view["query"] or view["query"] in interval["name"].casefold()
            color = (
                "sel+bold"
                if selected
                else (COLORS[depth % len(COLORS)] + "+bg:surface" if matches else "dim")
            )
            label = L.cut(interval["name"], right - left, views.g.ascii)
            label += ("-" if views.g.ascii else "━") * max(
                0, right - left - L.vlen(label)
            )
            row.append((label, color))
            regions.append((left + 5, right + 5, interval["id"]))
            bars += 1
            cursor = right
            if cursor >= bar_width or bars >= 2048:
                break
        if not regions:
            continue
        for left, right, identifier in regions:
            hits.append(
                (
                    len(rows),
                    "job_panel_action",
                    (
                        (
                            "trace_command",
                            {
                                "job": job_id,
                                "command": "trace frame " + stream + " " + identifier,
                            },
                        ),
                        left,
                        right,
                    ),
                )
            )
        rows.append(row)
        drawn += 1
    if not drawn:
        rows.append(
            [
                (
                    "No matched intervals in this window. Open calls and missing boundaries have no known duration.",
                    "warning",
                )
            ]
        )
    selected = lookup["by_id"].get(view["selected"])
    if selected:
        rows.append([("Selected: " + selected["name"], "heading")])
        rows.append(
            [
                (
                    f"{selected['kind']} / {duration(selected['duration_ns'])} wall time / "
                    f"sequences {selected['first_seq']} - {selected['last_seq']} / {selected['exit']}",
                    "text",
                )
            ]
        )
        if selected["file"]:
            rows.append(
                [
                    (
                        selected["file"]
                        + (
                            ":" + str(selected["line"])
                            if selected["line"] is not None
                            else ""
                        ),
                        "text",
                    )
                ]
            )
    elif view["selected"]:
        rows.append(
            [
                (
                    "Selected interval has left the retained window; choose another recorded call.",
                    "warning",
                )
            ]
        )
    else:
        rows.append(
            [
                (
                    "Click a colored interval or choose Next call to inspect its full name, duration and source.",
                    "dim",
                )
            ]
        )
    losses = lookup["losses"]
    if losses:
        rows.append(
            [
                (
                    "! Recorded loss markers: "
                    + ", ".join(duration(marker["start_ns"]) for marker in losses[:16])
                    + "; missing duration is unknown.",
                    "warning",
                )
            ]
        )
    rows.append(
        [
            (
                f"Retained: {model['retained_events']} events / {len(model['intervals'])} intervals; "
                f"evicted: {model['evicted_events']}; unmatched boundaries: "
                f"{model['unpaired_frame_events'] + model['unpaired_span_events']}.",
                "dim",
            )
        ]
    )
    rows.append(
        [
            (
                "One process clock. No native/device coverage or cross-rank timing is inferred.",
                "dim",
            )
        ]
    )
    return rows, hits


def _cores(model, lookup, view, thread, start, end, job_id, width, ascii_only):
    """Draw observed socket/core placement as points, never residency bars."""
    cpu = model["cpu"]
    rows, hits = [], []
    if not cpu["observations"]:
        rows.append(
            [
                (
                    "CPU placement: "
                    + cpu["status"]
                    + ". Existing call recordings have no inferred core assignment.",
                    "warning",
                )
            ]
        )
        rows.append(
            [
                (
                    "For a new run: raven launch SCRIPT --trace --trace-cpu-placement",
                    "cyan",
                )
            ]
        )
    shown, points = 0, 0
    for lane, observations, positions in lookup["cpu_lanes"]:
        if width < 8 or shown >= 128 or points >= 2048:
            break
        first = bisect_left(positions, start)
        visible = []
        for item in islice(observations, first, None):
            if item["at_ns"] > end:
                break
            if (thread is None or item["thread"] == thread) and (
                not view["query"] or view["query"] in item["name"].casefold()
            ):
                visible.append(item)
        if not visible:
            continue
        rows.append([(lane["label"], "heading")])
        line, cursor = [], 0
        for item in visible:
            column = max(
                0, min(width - 1, (item["at_ns"] - start) * width // (end - start))
            )
            if column < cursor:
                continue
            line.append((" " * (column - cursor), ""))
            line.append(
                (
                    "*" if ascii_only else "◆",
                    "sel+bold" if item["id"] == view["selected"] else "cyan",
                )
            )
            hits.append(
                (
                    len(rows),
                    "job_panel_action",
                    (
                        (
                            "trace_command",
                            {
                                "job": job_id,
                                "command": "trace frame "
                                + model["identity"].get("stream", "")
                                + " "
                                + item["id"],
                            },
                        ),
                        column,
                        column + 1,
                    ),
                )
            )
            cursor = column + 1
            points += 1
            if points >= 2048:
                break
        rows.append(line)
        shown += 1
    if cpu["observations"] and not shown:
        rows.append(
            [
                (
                    "No CPU observations match this time window and thread; Fit window or choose another thread.",
                    "dim",
                )
            ]
        )
    if shown >= 128 or points >= 2048:
        rows.append(
            [
                (
                    "Display limit reached; use the browser's virtualized core lanes for all recorded CPUs.",
                    "warning",
                )
            ]
        )
    selected = lookup["by_id"].get(view["selected"])
    if selected and selected["id"].startswith("cpu:"):
        rows.append([("Selected CPU observation: " + selected["name"], "heading")])
        rows.append(
            [
                (
                    f"Socket {selected['socket_id'] if selected['socket_id'] is not None else '?'} / "
                    f"core {selected['core_id'] if selected['core_id'] is not None else '?'} / logical CPU {selected['logical_cpu']} / "
                    f"thread {selected['thread']} / sequence {selected['seq']}",
                    "text",
                )
            ]
        )
        rows.append(
            [
                (
                    f"Query bracket: {duration(selected['start_ns'])} - {duration(selected['end_ns'])}; "
                    "the point is placed at its midpoint, not an exact scheduling transition.",
                    "dim",
                )
            ]
        )
    elif view["selected"]:
        rows.append(
            [
                (
                    "Selected observation has left the retained window; choose another CPU point.",
                    "warning",
                )
            ]
        )
    else:
        rows.append(
            [
                (
                    "Click a point or choose Next to inspect its actual socket, core, logical CPU and query bracket.",
                    "dim",
                )
            ]
        )
    rows.append(
        [
            (
                f"{len(cpu['observations'])} recorded observations / {len(cpu['lanes'])} logical CPU lanes. "
                "Hardware IDs are shown as recorded, including SMT siblings.",
                "dim",
            )
        ]
    )
    rows.append(
        [
            (
                "Observations do not establish migrations, CPU utilization or continuous core residency.",
                "warning",
            )
        ]
    )
    rows.extend(([(str(message), "warning")]) for message in cpu["warnings"][:16])
    return rows, hits
