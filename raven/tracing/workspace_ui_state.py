"""Serializable trace workspace choices and commands over published evidence."""

from __future__ import annotations

import hashlib
import json

from ..job_logs.text import display_text

PROJECTIONS = ("execution", "cpu", "gpu", "node")
ANALYSES = (
    "none",
    "call-tree",
    "bottom-up",
    "causal",
    "counters",
    "hints",
    "metrics",
    "samples",
)
MAX_INTERVALS = 2000
MAX_BOOKMARKS = 32


def initialize(state):
    """Preferences are serializable; source-specific choices carry a run key."""
    if not isinstance(state.get("workspace_view"), dict):
        state["workspace_view"] = {}
    view = state["workspace_view"]
    defaults = dict(
        mode="auto",
        delta_ns=10_000_000_000,
        projection="execution",
        window=None,
        clock_id=None,
        selected=None,
        query="",
        lane_page=0,
        collapsed=[],
        bookmarks=[],
        show_functions=False,
        run_key=None,
        all_clocks=False,
        domain_windows={},
        advanced=False,
        selected_detail_id=None,
        source_ids=[],
        thread=None,
        pinned=[],
        analysis_mode="none",
        filters={},
        filters_open=False,
        facet=None,
        facet_page=0,
    )
    for key, value in defaults.items():
        view.setdefault(key, value)
    if view["projection"] not in PROJECTIONS:
        view["projection"] = "execution"
    if view["analysis_mode"] not in ANALYSES:
        view["analysis_mode"] = "none"
    from .workspace_ui_filters import FIELDS, sanitize

    view["filters"] = sanitize(view["filters"])
    if view["facet"] not in FIELDS:
        view["facet"] = None
    if type(view["facet_page"]) is not int or not 0 <= view["facet_page"] <= 256:
        view["facet_page"] = 0
    if not isinstance(view["mode"], str) or view["mode"] not in {
        "auto",
        "live",
        "explore",
    }:
        view["mode"] = "auto"
    if (
        type(view["delta_ns"]) is not int
        or not 1_000_000 <= view["delta_ns"] <= 86_400_000_000_000
    ):
        view["delta_ns"] = defaults["delta_ns"]
    for key, bound in (("collapsed", 256), ("bookmarks", MAX_BOOKMARKS)):
        if not isinstance(view[key], list):
            view[key] = []
        view[key] = view[key][-bound:]
    if not isinstance(view["query"], str):
        view["query"] = ""
    view["query"] = display_text(view["query"][:256]).casefold()
    if type(view["lane_page"]) is not int or not 0 <= view["lane_page"] <= 1_000_000:
        view["lane_page"] = 0
    for key, maximum in (
        ("clock_id", 256),
        ("selected", 512),
        ("selected_detail_id", 512),
        ("run_key", 64),
    ):
        value = view[key]
        if value is not None and (
            not isinstance(value, str)
            or not value.isprintable()
            or len(value) > maximum
        ):
            view[key] = None
    if type(view["all_clocks"]) is not bool:
        view["all_clocks"] = False
    view["domain_windows"] = _windows(view["domain_windows"])
    for key in ("advanced", "show_functions", "filters_open"):
        if type(view[key]) is not bool:
            view[key] = False
    view["window"] = _windows({"view": view["window"]}).get("view")
    for key, maximum, count in (("source_ids", 256, 512), ("pinned", 1024, 64)):
        values = view[key]
        view[key] = (
            list(
                dict.fromkeys(
                    value
                    for value in values[:count]
                    if isinstance(value, str)
                    and value.isprintable()
                    and 0 < len(value) <= maximum
                )
            )
            if isinstance(values, list)
            else []
        )
    if view["thread"] is not None and (
        not isinstance(view["thread"], str)
        or not view["thread"].isprintable()
        or not 0 < len(view["thread"]) <= 256
    ):
        view["thread"] = None
    view["collapsed"] = [
        value
        for value in view["collapsed"]
        if isinstance(value, str)
        and (
            value == "*"
            or len(value.removeprefix("!")) == 16
            and all(char in "0123456789abcdef" for char in value.removeprefix("!"))
        )
    ]
    valid_bookmarks = []
    for item in view["bookmarks"]:
        if not isinstance(item, dict):
            continue
        window = item.get("window")
        clock = item.get("clock_id")
        if (
            not isinstance(window, (list, tuple))
            or len(window) != 2
            or not all(
                type(value) is int and -(1 << 64) < value < (1 << 64)
                for value in window
            )
            or window[0] >= window[1]
        ):
            continue
        if clock is not None and (
            not isinstance(clock, str) or not clock.isprintable() or len(clock) > 256
        ):
            continue
        selected = item.get("selected")
        if selected is not None and (
            not isinstance(selected, str)
            or not selected.isprintable()
            or len(selected) > 512
        ):
            continue
        valid_bookmarks.append(
            dict(
                window=list(window),
                clock_id=clock,
                selected=selected,
                selected_detail_id=item.get("selected_detail_id")
                if isinstance(item.get("selected_detail_id"), str)
                and item["selected_detail_id"].isprintable()
                and len(item["selected_detail_id"]) <= 512
                else selected,
                label=_text(item.get("label", ""), 512),
                all_clocks=item.get("all_clocks") is True,
                domain_windows=_windows(item.get("domain_windows"), maximum=64),
            )
        )
    view["bookmarks"] = valid_bookmarks
    return view


def _windows(value, *, maximum=512):
    if not isinstance(value, dict):
        return {}
    output = {}
    for clock, window in value.items():
        if len(output) == maximum:
            break
        if not isinstance(clock, str) or not clock.isprintable() or len(clock) > 256:
            continue
        if (
            not isinstance(window, (list, tuple))
            or len(window) != 2
            or not all(
                type(item) is int and -(1 << 64) < item < (1 << 64) for item in window
            )
            or window[0] >= window[1]
        ):
            continue
        output[clock] = list(window)
    return output


def _collapsed(view, identifier):
    values = view["collapsed"]
    return identifier in values or "*" in values and "!" + identifier not in values


def _text(value, maximum=8192):
    return display_text(str(value)[:maximum])


def _identity_key(result):
    identity = result.get("identity") or {}
    selected = {
        key: identity.get(key) for key in ("job_id", "attempt", "cluster", "run_id")
    }
    return hashlib.sha256(
        json.dumps(selected, sort_keys=True, default=str).encode()
    ).hexdigest()


def _live(result):
    sources = result.get("sources") or []
    return any(str(source.get("status", "")).upper() == "LIVE" for source in sources)


def _mode(view, result):
    return (
        ("live" if _live(result) else "explore")
        if view["mode"] == "auto"
        else view["mode"]
    )


def _bounds(result):
    """Independent raw clock bounds. There is no implicit cross-host offset."""
    values = {}
    rows = result.get("bounds") or []
    if isinstance(rows, dict):
        rows = [
            dict(value, clock_id=key)
            for key, value in rows.items()
            if isinstance(value, dict)
        ]
    for row in rows:
        if not isinstance(row, dict):
            continue
        clock = row.get("clock_id")
        first, last = row.get("start_ns"), row.get("end_ns")
        if (
            isinstance(clock, str)
            and type(first) is int
            and type(last) is int
            and first <= last
        ):
            values[clock] = (first, last)
    if not values:
        for row in result.get("intervals", ()):
            clock = str(row.get("clock_id", "unknown"))
            first, last = row.get("start_ns"), row.get("end_ns")
            if type(first) is int and type(last) is int and first <= last:
                old = values.get(clock, (first, last))
                values[clock] = (min(old[0], first), max(old[1], last))
    return values


def _clock(view, result):
    bounds = _available_bounds(result)
    chosen = view.get("clock_id")
    if view.get("all_clocks"):
        return None
    return chosen if chosen in bounds else next(iter(bounds), None)


def _available_bounds(result):
    """Reference axes are opt-in; all-clocks enumeration still uses raw bounds."""
    values = _bounds(result)
    for clock, (first, last) in _bounds(
        {"bounds": result.get("aligned_bounds", [])}
    ).items():
        lower, upper = values.get(clock, (first, last))
        values[clock] = min(lower, first), max(upper, last)
    return values


def _window(view, result, clock):
    first, last = _available_bounds(result).get(clock, (0, 1))
    if _mode(view, result) == "live":
        return max(first, last - view["delta_ns"]), last
    saved = (
        view.get("domain_windows", {}).get(clock)
        if view.get("all_clocks")
        else view.get("window")
        if clock == _clock(view, result)
        else None
    )
    if (
        isinstance(saved, (list, tuple))
        and len(saved) == 2
        and all(type(value) is int for value in saved)
        and saved[0] < saved[1]
    ):
        maximum = (1 << 64) - 1
        length = min(maximum, saved[1] - saved[0])
        start = max(0, min(maximum - length, saved[0]))
        return start, start + length
    return first, last


def publish(state, result):
    """Publish without carrying a different attempt's selection into a new run.

    Completion freezes the last visible Live range. It does not fit, jump to a
    different event, clear group expansion, or stop an external recorder.
    """
    view = initialize(state)
    previous = state.get("workspace_result") or {}
    run_key = _identity_key(result)
    if view["run_key"] is not None and view["run_key"] != run_key:
        from .workspace_ui_preferences import remember

        remember(state, view, PREFERENCE_KEYS)
        restored = state.get("workspace_runs", {}).get(run_key)
        if isinstance(restored, dict):
            state["workspace_view"] = json.loads(json.dumps(restored))
            view = initialize(state)
        else:
            view.update(
                window=None,
                clock_id=None,
                selected=None,
                selected_detail_id=None,
                lane_page=0,
                collapsed=[],
                bookmarks=[],
                query="",
                mode="auto",
                all_clocks=False,
                domain_windows={},
                source_ids=[],
                thread=None,
                pinned=[],
                filters={},
                facet=None,
                facet_page=0,
            )
        state.pop("workspace_detail", None)
        state.pop("_workspace_selected_interval", None)
        state.pop("workspace_symbol", None)
        state.pop("workspace_analysis", None)
    elif previous and _mode(view, previous) == "live" and not _live(result):
        clock = _clock(view, previous)
        windows = {key: list(_window(view, previous, key)) for key in _bounds(previous)}
        view.update(
            mode="explore",
            clock_id=clock,
            window=list(_window(view, previous, clock)),
            domain_windows=windows if view.get("all_clocks") else {},
        )
    view["run_key"] = run_key
    state["workspace_result"] = result
    state["paint"] = None
    return result


def query_options(state):
    """Read-only request contract for the background query owner."""
    view = initialize(state)
    result = state.get("workspace_result") or {}
    clock = _clock(view, result)
    first, last = _window(view, result, clock)
    options = dict(search=view["query"], limit=MAX_INTERVALS, clock_id=clock)
    options.update(view["filters"])
    if view["source_ids"]:
        options["source_ids"] = list(view["source_ids"])
    if view["thread"] is not None:
        options["thread"] = view["thread"]
    if clock is not None and _available_bounds(result):
        options.update(start_ns=first, end_ns=last)
    return options


def query_requests(state):
    """A fair bounded request per independent clock in the current viewport."""
    view = initialize(state)
    result = state.get("workspace_result") or {}
    if not view.get("all_clocks"):
        return [query_options(state)]
    clocks = list(_bounds(result))[:512]
    limit = max(1, MAX_INTERVALS // max(1, len(clocks)))
    return [
        dict(
            query_options(state),
            start_ns=_window(view, result, clock)[0],
            end_ns=_window(view, result, clock)[1],
            clock_id=clock,
            search=view["query"],
            limit=limit,
        )
        for clock in clocks
    ] or [query_options(state)]


def _rows(result):
    return [
        row
        for row in (*result.get("intervals", ()), *result.get("markers", ()))
        if isinstance(row, dict)
    ]


def _selected(state, view):
    identifier = view.get("selected")
    result = state.get("workspace_result") or {}
    projected = result.get("workspace_projection")
    if isinstance(projected, dict):
        visible = next(
            (row for row in _rows(projected) if row.get("id") == identifier), None
        )
        if visible is not None:
            detail_id = visible.get("detail_id") or visible["id"]
            detail = state.get("workspace_detail")
            if not isinstance(detail, dict) or detail.get("id") != detail_id:
                detail = next(
                    (row for row in _rows(result) if row.get("id") == detail_id), None
                )
            if isinstance(detail, dict) and isinstance(detail.get("data"), dict):
                return dict(
                    visible, data={**detail["data"], **(visible.get("data") or {})}
                )
            return visible
    visible = next((row for row in _rows(result) if row.get("id") == identifier), None)
    if visible is not None:
        return visible
    pinned = state.get("_workspace_selected_interval")
    if isinstance(pinned, dict) and pinned.get("id") == identifier:
        return pinned
    detail = state.get("workspace_detail")
    if isinstance(detail, dict) and detail.get("id") in {
        identifier,
        view.get("selected_detail_id"),
    }:
        return dict(
            detail,
            selection_context="Verified source record; presentation interval is outside this query",
        )
    return None


def detail_request(state):
    view = initialize(state)
    return view.get("selected_detail_id") or view.get("selected")


def symbol_request(state):
    """Identify a recorded sample frame; the reader resolves actual source data.

    The returned index references the original marker's frames array, not a
    caller-supplied symbol/path. It carries no action or placement authority.
    """
    view = initialize(state)
    selected = _selected(state, view)
    request = {"event_id": detail_request(state)}
    if (
        not selected
        or selected.get("kind") != "cpu_sample"
        or not selected.get("detail_id")
    ):
        return request
    data = selected.get("data") or {}
    frames, depth = data.get("frames"), selected.get("depth")
    if isinstance(frames, list) and type(depth) is int and 0 <= depth < len(frames):
        request["frame_index"] = (
            len(frames) - 1 - depth
            if data.get("stack_order") == "leaf-to-root"
            else depth
        )
    return request


def _candidates(state, view, clock):
    from .workspace_ui_navigation import matches
    from .workspace_ui_filters import prepared as filters_prepared

    result = state.get("workspace_result") or {}
    if not filters_prepared(result, view):
        return []
    prepared = result.get("workspace_projection")
    if (
        isinstance(prepared, dict)
        and result.get("workspace_projection_name", view["projection"])
        == view["projection"]
    ):
        result = prepared
    rows, seen = [], set()
    items = list(result.get("intervals", ()))
    expanded = {
        row.get("detail_id") for row in items if row.get("kind") == "cpu_sample"
    }
    items += [
        row
        for row in result.get("markers", ())
        if row.get("kind") == "cpu_sample"
        and row.get("id") not in expanded
        or view["projection"] == "cpu"
        and row.get("kind") == "cpu_observation"
    ]
    for row in items:
        if (
            not row.get("id")
            or row["id"] in seen
            or clock is not None
            and row.get("clock_id") != clock
            or not matches(row, view)
        ):
            continue
        if view["projection"] == "cpu" and row.get("kind") not in {
            "cpu_observation",
            "scheduling",
            "cpu_sample",
            "cpu_stack_slice",
        }:
            continue
        if view["projection"] == "gpu" and row.get("kind") not in {
            "gpu_kernel",
            "gpu_copy",
            "gpu_sync",
            "gpu_synchronization",
        }:
            continue
        if view["query"] and view["query"] not in str(row.get("name", "")).casefold():
            continue
        seen.add(row["id"])
        rows.append(row)
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("clock_id", "")) if clock is None else "",
            row.get("start_ns", 0),
            str(row["id"]),
        ),
    )


PREFERENCE_KEYS = (
    "mode",
    "delta_ns",
    "projection",
    "window",
    "clock_id",
    "selected",
    "selected_detail_id",
    "query",
    "lane_page",
    "collapsed",
    "bookmarks",
    "show_functions",
    "run_key",
    "all_clocks",
    "domain_windows",
    "advanced",
    "source_ids",
    "thread",
    "pinned",
    "analysis_mode",
    "filters",
    "filters_open",
    "facet",
    "facet_page",
)


def save_preferences(state):
    from .workspace_ui_preferences import remember

    view = initialize(state)
    remember(state, view, PREFERENCE_KEYS)
    return json.loads(
        json.dumps(
            {
                **{key: view[key] for key in PREFERENCE_KEYS},
                "runs": state.get("workspace_runs", {}),
            },
            allow_nan=False,
        )
    )


def restore_preferences(state, value):
    from .workspace_ui_preferences import restore_history

    if isinstance(value, dict):
        state["workspace_view"] = {
            key: value[key] for key in PREFERENCE_KEYS if key in value
        }
        state["workspace_runs"] = restore_history(
            value.get("runs"), PREFERENCE_KEYS, initialize
        )
    return initialize(state)
