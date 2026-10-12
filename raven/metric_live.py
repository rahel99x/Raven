"""Exact-source Live windows and independent sampling-rate controls.

The time window changes only the display. The separate rate control publishes
bounded requests to background collectors without performing source I/O here.
State is transient, bounded, and scoped to an exact job/source/attempt.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
import time

from . import clock, layout as L
from .interaction import Rect

from .pointer_routing import capture_handler

MIN_DELTA = 1.0
MAX_DELTA = 30.0
MAX_METRICS = 128
MIN_WIDTH = 24
DOCUMENT_INTERVAL = 0.1
CAPTURE_TIMEOUT = 15.0
MIN_RATE = 1
MAX_RATE = 100
_UNSET_INTERVAL = object()
_UNSET_OBSERVATION = object()


@dataclass(frozen=True)
class Controls:
    key: tuple
    token: str
    rect: Rect
    visible: Rect
    toggle: Rect
    slider: Rect
    slider_full: Rect
    layer: int = 0
    rate_slider: Rect | None = None
    rate_slider_full: Rect | None = None


def initialize(app):
    state = getattr(app, "metric_live_state", None)
    if not isinstance(state, dict):
        state = {
            "entries": OrderedDict(),
            "current": OrderedDict(),
            "counter": 0,
            "records": (),
            "context": None,
            "command_records": (),
            "command_context": None,
            "command_viewport": None,
            "capture": None,
            "focus": None,
            "focus_kind": "window",
            "pending_focus": None,
            "revision": 0,
            "ascii": False,
        }
        app.metric_live_state = state
    return state


def canonical(identity):
    """A companion filled area shares its source curve's live display window."""
    from .chart_interaction import _key

    identity = _key(identity)
    if identity is None:
        return None
    if identity[0] == "resource-area":
        return ("resource-series", *identity[1:])
    return identity


def _family(identity):
    # Attempt, project/run and research generation remain in the exact key.
    # A newer published attempt invalidates older controls for the same source.
    return identity[:4]


def _finite(value):
    try:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    except (ValueError, TypeError, OverflowError):
        return False


def fraction(delta):
    """Left is thirty seconds; right is one second, with equal log steps."""
    if not _finite(delta):
        delta = MAX_DELTA
    delta = min(MAX_DELTA, max(MIN_DELTA, delta))
    return math.log(MAX_DELTA / delta) / math.log(MAX_DELTA / MIN_DELTA)


def delta_at(value):
    if not _finite(value):
        value = 0.0
    value = min(1.0, max(0.0, value))
    if value == 0:
        return MAX_DELTA
    if value == 1:
        return MIN_DELTA
    return MAX_DELTA * math.exp(-value * math.log(MAX_DELTA / MIN_DELTA))


def format_delta(value):
    return f"{value:.3g}s" if value >= 1 else f"{value*1000:.3g}ms"


def rate_fraction(value):
    """Sampling positions use an independent integer scale."""
    from .metric_sampling import validate_rate

    try:
        value = validate_rate(value)
    except (ValueError, TypeError, OverflowError):
        value = MIN_RATE
    return (value - MIN_RATE) / (MAX_RATE - MIN_RATE)


def rate_at(value):
    if not _finite(value):
        value = 0.0
    value = min(1.0, max(0.0, value))
    return round(MIN_RATE + value * (MAX_RATE - MIN_RATE))


def _sync_sampling(app):
    """Publish requests once; eligibility checks may invalidate other entries."""
    state = initialize(app)
    if state.get("sampling_syncing"):
        return
    from .metric_sampling import sync

    state["sampling_syncing"] = True
    try:
        sync(app)
    finally:
        state["sampling_syncing"] = False


def _generation(app, identity):
    """Read the exact queue-attempt generation from its published Store."""
    owner = getattr(app, "_chart_owner", app)
    store = getattr(owner, "store", None)
    reader = getattr(store, "job_attempt", None)
    if not callable(reader) or len(identity) < 2:
        return None
    generation = reader(str(identity[1]))
    if type(generation) is not int or generation < 0:
        return None
    return id(store), generation


def _invalidate(app, entry, *, reset_window=False, rotate_token=False):
    """Expired attempts cannot retain or restore faster collection requests."""
    state = initialize(app)
    changed_sampling = entry.get("rate", MIN_RATE) != MIN_RATE
    changed = changed_sampling or entry["enabled"]
    entry.update(running=False, enabled=False, rate=MIN_RATE)
    entry.pop("playback", None)
    entry.pop("buffered", None)
    if reset_window:
        changed |= entry["delta"] != MAX_DELTA
        entry["delta"] = MAX_DELTA
    if rotate_token:
        state["counter"] += 1
        entry["token"] = "m" + str(state["counter"])
        changed = True
    if changed:
        state["revision"] += 1
    if changed_sampling:
        _sync_sampling(app)


def _bind_generation(app, identity, entry):
    generation = _generation(app, identity)
    if generation is None:
        return True
    previous = entry.get("generation")
    entry["generation"] = generation
    if previous is not None and previous != generation:
        _invalidate(app, entry, reset_window=True, rotate_token=True)
        return False
    return True


def _entry(app, identity, *, running=None):
    state = initialize(app)
    identity = canonical(identity)
    if identity is None:
        return None
    entry = state["entries"].get(identity)
    changed_sampling = False
    if entry is None and running is False:
        return None
    if entry is None:
        state["counter"] += 1
        entry = {
            "token": "m" + str(state["counter"]),
            "delta": MAX_DELTA,
            "rate": MIN_RATE,
            "enabled": False,
            "running": False,
            "generation": None,
        }
        state["entries"][identity] = entry
        state["wanted_revision"] = state.get("wanted_revision", 0) + 1
    state["entries"].move_to_end(identity)
    while len(state["entries"]) > MAX_METRICS:
        stale, evicted = state["entries"].popitem(last=False)
        changed_sampling |= evicted.get("rate", MIN_RATE) != MIN_RATE
        state["wanted_revision"] = state.get("wanted_revision", 0) + 1
        if state["current"].get(_family(stale)) == stale:
            state["current"].pop(_family(stale), None)
    if running is not None:
        changed_sampling |= entry["running"] != (running is True) and entry.get(
            "rate", MIN_RATE
        ) != MIN_RATE
        if running is True:
            # A fresh source publication can establish a replacement attempt.
            # Retained old commands only run through _eligible and are rejected.
            if entry.get("generation") is not None:
                _bind_generation(app, identity, entry)
            entry["running"] = True
            previous = state["current"].get(_family(identity))
            if previous is not None and previous != identity:
                old = state["entries"].get(previous)
                if old:
                    changed_sampling |= old.get("rate", MIN_RATE) != MIN_RATE
                    _invalidate(app, old)
            state["current"][_family(identity)] = identity
            state["current"].move_to_end(_family(identity))
            while len(state["current"]) > MAX_METRICS:
                state["current"].popitem(last=False)
        else:
            _invalidate(app, entry)
    if changed_sampling:
        _sync_sampling(app)
    return entry


def set_running(app, identity, running):
    """The renderer supplies status from its already published exact job."""
    return _entry(app, identity, running=running is True)


def _slot_changed(state, jobs, identity):
    """Detect an in-place replacement without scanning unrelated job rows."""
    jid = str(identity[1]) if len(identity) > 1 else ""
    position = state.get("job_positions", {}).get(jid)
    return position is not None and (
        position >= len(jobs) or jobs[position] is not state.get("job_map", {}).get(jid)
    )


def _published_job(app, identity):
    """Index only wanted IDs, across the complete already published job list.

    A new list, list length, or wanted-ID set rebuilds this bounded index once.
    Additional metrics for the same job reuse it. State checks use the original
    Job objects, so in-place completion does not require rescanning other jobs.
    """
    state = initialize(app)
    store = getattr(app, "store", None)
    jobs = getattr(store, "jobs", None)
    if not isinstance(jobs, (list, tuple)):
        return None, False
    wanted_changed = False
    if state.get("indexed_revision") != state.get("wanted_revision", 0):
        wanted = frozenset(str(key[1]) for key in state["entries"] if len(key) > 1)
        wanted_changed = wanted != state.get("wanted_ids")
        state["wanted_ids"] = wanted
        state["indexed_revision"] = state.get("wanted_revision", 0)
    if (
        state.get("jobs_ref") is not jobs
        or state.get("store_ref") is not store
        or state.get("jobs_count") != len(jobs)
        or wanted_changed
        or _slot_changed(state, jobs, identity)
    ):
        wanted = state.get("wanted_ids", frozenset())
        found, positions = {}, {}
        for position, job in enumerate(jobs):
            jid = str(getattr(job, "id", ""))
            if jid in wanted:
                found[jid] = job
                positions[jid] = position
            if len(found) == len(wanted):
                break
        state["jobs_ref"], state["store_ref"] = jobs, store
        state["jobs_count"], state["job_map"] = len(jobs), found
        state["job_positions"] = positions
    return (
        state.get("job_map", {}).get(str(identity[1]) if len(identity) > 1 else ""),
        True,
    )


def _eligible(app, identity):
    state = initialize(app)
    entry = state["entries"].get(identity)
    valid = bool(
        entry
        and entry["running"]
        and state["current"].get(_family(identity)) == identity
    )
    if valid:
        job, available = _published_job(app, identity)
        if available:
            valid = job is not None and getattr(job, "state", None) == "RUNNING"
            if (
                valid
                and len(identity) > 4
                and isinstance(identity[4], str)
                and (
                    identity[0] == "resource-series"
                    or identity[4].startswith("scheduler:")
                )
            ):
                attempt = "|".join(
                    str(getattr(job, name, None) or "") for name in ("submit", "start")
                )
                prefix = "scheduler:" if identity[4].startswith("scheduler:") else ""
                valid = identity[4] == prefix + attempt
    if valid:
        valid = _bind_generation(app, identity, entry)
    if not valid and entry:
        _invalidate(app, entry)
    return valid


def _playback_key(identity):
    """Share native CPU/memory clocks with their modal and area presentations."""
    if len(identity) < 5:
        return identity
    scope, jid, metric, source, attempt = identity[:5]
    if scope == "reported-metric" and source == "Raven session resource samples":
        metric = {"CPU per core (%)": "cpu-rate", "Memory (GB)": "resident-memory"}.get(metric)
        if metric is None:
            return identity
        scope = "resource-series"
    if scope == "resource-series":
        if isinstance(attempt, str) and attempt.startswith("scheduler:"):
            attempt = attempt[len("scheduler:"):]
        return ("native-playback", jid, metric, attempt)
    return identity


def buffered(app, identity):
    """Whether this running source has entered automatic buffered playback."""
    identity = canonical(identity)
    if identity is None or not _eligible(app, identity):
        return False
    return bool(initialize(app)["entries"][identity].get("buffered"))


def display_end(app, identity, now=None, *, newest=None, previous=None,
                oldest=None, poll_interval=None):
    """Advance the buffered source clock from received timestamps.

    Full-history renderers may pace the published endpoint separately. No sampler demand,
    source read or graph render is performed here. Pointer input uses the
    published playback result instead of advancing it.
    """
    identity = canonical(identity)
    if identity is None or not _eligible(app, identity):
        return None
    entry = initialize(app)["entries"][identity]
    entry["buffered"] = True
    from . import metric_playback
    if poll_interval is None:
        from .metric_sampling import cadence
        poll_interval = cadence(app, identity)
    owner = getattr(app, "_chart_owner", app)
    replay = getattr(getattr(owner, "replay", None), "clock", None)
    generation = (entry.get("generation"), id(replay), getattr(replay, "generation", None))
    result = metric_playback.advance(
        app, _playback_key(identity), now=clock.now() if now is None else now,
        newest=newest, previous=previous, oldest=oldest, poll_interval=poll_interval,
        generation=generation)
    entry["playback"] = result
    return result.end if result is not None else None


def playback_status(app, identity):
    """Return the last buffered source clock without moving it."""
    identity = canonical(identity)
    if identity is None or not buffered(app, identity):
        return None
    return initialize(app)["entries"][identity].get("playback")


def playback_label(app, identity, *, ascii_=False, displayed_end=None):
    if not buffered(app, identity):
        return ""
    result = playback_status(app, identity)
    if result is None:
        return "buffering"
    from .metric_sampling import format_interval
    label = {"buffering": "buffering", "held": "held"}.get(result.status, "buffered")
    lag = result.lag
    if _finite(displayed_end):
        now = clock.now()
        if _finite(now) and now >= displayed_end:
            lag = now - displayed_end
    return label + " " + format_interval(lag, ascii_=ascii_) + " behind"


def display_resolution(app, identity, span, width, *, following=True):
    """Schedule work when the displayed edge can move a Braille subcolumn.

    A long full-history plot does not need ten full document preparations per
    second. Source publications and ordinary maintenance still refresh current
    values; this deadline only adds movement between those publications.
    """
    identity = canonical(identity)
    entry = initialize(app)["entries"].get(identity)
    if entry is None:
        return
    entry["following"] = bool(following)
    if _finite(span) and span > 0 and _finite(width) and width > 0:
        entry["display_interval"] = max(DOCUMENT_INTERVAL, span / (2 * width))
    else:
        entry["display_interval"] = DOCUMENT_INTERVAL


def window(app, identity, now=None, *, newest=_UNSET_OBSERVATION,
           previous=None, oldest=None, poll_interval=None):
    """Read the displayed Live window; observations opt into clock advancement."""
    identity = canonical(identity)
    if identity is None or not _eligible(app, identity):
        return None
    if newest is not _UNSET_OBSERVATION:
        display_end(app, identity, now, newest=newest, previous=previous,
                    oldest=oldest, poll_interval=poll_interval)
    entry = initialize(app)["entries"][identity]
    if not entry["enabled"]:
        return None
    capture = getattr(app, "chart_interaction_state", {}).get("capture")
    if capture and canonical(capture["plot"].key) == identity:
        # Keep the exact painted mapping stable while an XY area is selected.
        # Cancelling resumes Live; a committed box turns it off explicitly.
        return capture["plot"].x_bounds
    now = clock.now() if now is None else now
    if not _finite(now):
        return None
    playback = entry.get("playback")
    end = playback.end if playback is not None else now
    first = end - entry["delta"]
    return (first, end) if _finite(first) and first < end else None


def enabled(app, identity):
    identity = canonical(identity)
    entry = initialize(app)["entries"].get(identity)
    return bool(identity is not None and _eligible(app, identity) and entry["enabled"])


def _say(app, message):
    callback = getattr(app, "say", None)
    if callable(callback):
        callback(message)


def set_enabled(app, identity, value):
    identity = canonical(identity)
    if identity is None or not _eligible(app, identity) or type(value) is not bool:
        return False
    state = initialize(app)
    entry = state["entries"][identity]
    if value:
        from .chart_interaction import clear_source_zoom

        clear_source_zoom(app, identity, canonical)
    if entry["enabled"] != value:
        entry["enabled"] = value
        state["revision"] += 1
    return True


def stop_for_zoom(app, identity):
    identity = canonical(identity)
    state = initialize(app)
    entry = state["entries"].get(identity)
    if entry and entry["enabled"]:
        entry["enabled"] = False
        state["revision"] += 1


def set_delta(app, identity, value):
    identity = canonical(identity)
    if (
        identity is None
        or not _eligible(app, identity)
        or not _finite(value)
        or not MIN_DELTA <= value <= MAX_DELTA
    ):
        return False
    state = initialize(app)
    entry = state["entries"][identity]
    if entry["delta"] != value:
        entry["delta"] = float(value)
        # An inactive Live window is only a preference for the next toggle.
        # The feedback layer paints its slider without rerasterizing history.
        if entry["enabled"]:
            state["revision"] += 1
    return True


def set_rate(app, identity, value):
    identity = canonical(identity)
    from .metric_sampling import validate_rate

    try:
        value = validate_rate(value)
    except (ValueError, TypeError, OverflowError):
        return False
    if identity is None or not _eligible(app, identity):
        return False
    state = initialize(app)
    entry = state["entries"][identity]
    if entry.get("rate", MIN_RATE) != value:
        entry["rate"] = value
        state["revision"] += 1
        _sync_sampling(app)
    return True


def _row(g, entry, width, *, app=None, identity=None, poll_interval=_UNSET_INTERVAL):
    """A stable one-row layout for both independent sliders, even at 24 cells."""
    from .metric_sampling import cadence, format_interval

    if poll_interval is _UNSET_INTERVAL:
        poll_interval = cadence(app, identity) if app is not None else None
    current_interval = (
        format_interval(poll_interval, ascii_=g.ascii)
        if poll_interval is not None else "?"
    )
    # A faster global or companion-metric request can keep the effective poll
    # interval unchanged. Report this control's setting separately so its
    # value never appears frozen while the thumb moves. Neither value claims
    # the external producer publishes new observations at this cadence.
    from .refresh_rate import poll_interval as requested_interval
    requested = format_interval(
        requested_interval(entry.get("rate", MIN_RATE), MAX_RATE), ascii_=g.ascii
    )
    fast_interval = format_interval(0.5, ascii_=g.ascii)
    if width < 68:
        toggle = (
            ("+" if entry["enabled"] else "o")
            if g.ascii
            else ("●" if entry["enabled"] else "○")
        )
        delta_prefix, delta_suffix = "30s", "1s"
        rate_prefix = L.pad(current_interval, 6)
        rate_suffix = "S" + L.pad(requested, 5)
        separator = " "
    else:
        toggle = "[Live ON ]" if entry["enabled"] else "[Live off]"
        delta_prefix = "Delta " + L.pad(format_delta(entry["delta"]), 6) + " 30s "
        delta_suffix = " 1s"
        rate_prefix = (
            "Poll " + L.pad(current_interval, 6) + " Set "
            + L.pad(requested, 6) + " "
        )
        rate_suffix = " " + L.pad(fast_interval, 6)
        separator = " | " if g.ascii else " │ "
    fixed = sum(
        L.vlen(part)
        for part in (
            toggle, " ", delta_prefix, delta_suffix, separator,
            rate_prefix, rate_suffix,
        )
    )
    available = max(4, width - fixed)
    delta_count, rate_count = available // 2, available - available // 2
    delta_left = L.vlen(toggle + " " + delta_prefix)
    rate_left = delta_left + delta_count + L.vlen(delta_suffix + separator + rate_prefix)
    track, thumb = ("-", "o") if g.ascii else ("─", "◆")

    def slider(count, value):
        index = round(value * (count - 1))
        return [
            (track * index, "track"),
            (thumb, "cursor+bold"),
            (track * (count - index - 1), "track"),
        ]

    row = [
        (toggle, "cyan+bold" if entry["enabled"] else "muted"),
        (" " + delta_prefix, "text-secondary"),
    ]
    row.extend(slider(delta_count, fraction(entry["delta"])))
    row.extend([
        (delta_suffix, "dim"), (separator, "border"),
        (rate_prefix, "text-secondary"),
    ])
    row.extend(slider(rate_count, rate_fraction(entry.get("rate", MIN_RATE))))
    row.append((rate_suffix, "text-secondary" if width < 68 else "dim"))
    return L.clip_row(row, width), (
        0, L.vlen(toggle), delta_left, delta_left + delta_count,
        rate_left, rate_left + rate_count,
    )


def controls(g, app, identity, width, *, running=True, row=0, column=0, layer=0):
    """Render and stage one row before its curve. Filled companions omit it."""
    identity = canonical(identity)
    entry = _entry(app, identity, running=running is True)
    if (
        entry is None
        or not _eligible(app, identity)
        or running is not True
        or not isinstance(width, int)
        or isinstance(width, bool)
        or width < MIN_WIDTH
        or width > 4096
        or any(
            not isinstance(v, int) or isinstance(v, bool) for v in (row, column, layer)
        )
    ):
        return [], []
    rendered, spans = _row(g, entry, width, app=app, identity=identity)
    from .chart_interaction import Plot, put_records

    rect = Rect(row, column, row + 1, column + width)
    payload = (*spans, entry["token"])
    put_records(
        app,
        (
            Plot(
                identity,
                rect,
                rect,
                (0.0, 1.0),
                (0.0, 1.0),
                "linear",
                layer,
                "live-controls",
                payload,
            ),
        ),
    )
    hits = [
        (
            row,
            "control",
            {
                "id": "metric-live:" + entry["token"],
                "label": "Toggle Live metric window",
                "left": column + spans[0],
                "right": column + spans[1],
                "action": ("command", "metric-live " + entry["token"] + " toggle"),
                "group": "metric-live",
            },
        ),
        (
            row,
            "control",
            {
                "id": "metric-window:" + entry["token"],
                "label": "Adjust Live metric time window; right-click resets to 30 seconds",
                "left": column + spans[2],
                "right": column + spans[3],
                "action": ("command", "metric-window " + entry["token"] + " focus"),
                "group": "metric-live",
            },
        ),
        (
            row,
            "control",
            {
                "id": "metric-sampling:" + entry["token"],
                "label": "Adjust metric polling from 5s to 500ms; Set/S is requested, Poll is effective; right-click resets",
                "left": column + spans[4],
                "right": column + spans[5],
                "action": ("command", "metric-sampling " + entry["token"] + " focus"),
                "group": "metric-live",
            },
        ),
    ]
    return [rendered], hits


def _context(app):
    from .chart_interaction import _context as context

    return context(app)


def _blocked(app):
    from .chart_interaction import _blocked as blocked

    return blocked(app)


def _viewport(app):
    """Small, immutable presentation/source guard for palette commands.

    A palette hides the interaction registry while retaining the document it
    was opened over. Do not accept a saved token after that document moves,
    changes its split, or switches its reported source. This reads published
    UI state only; sample values and moving Live bounds are deliberately absent.
    """
    layout = getattr(app, "layout_state", None)
    scroll = getattr(layout, "scroll", {})
    scroll = scroll if isinstance(scroll, dict) else {}
    tab = getattr(app, "tab", "")
    browser = getattr(app, "history_browser_state", {})
    browser = browser if isinstance(browser, dict) else {}
    view = browser.get("views", {}).get(tab, {})
    view = view if isinstance(view, dict) else {}
    document = getattr(app, "analytics_document_state", {})
    document = document if isinstance(document, dict) else {}
    analysis = getattr(app, "analysis_state", {})
    analysis = analysis if isinstance(analysis, dict) else {}
    result = getattr(app, "analysis_result", {})
    result = result if isinstance(result, dict) else {}
    axes = analysis.get("axes", {})
    axis = axes.get(analysis.get("metric"), {}) if isinstance(axes, dict) else {}
    axis = axis if isinstance(axis, dict) else {}
    views = getattr(app, "views_ref", None) or getattr(app, "views", None)
    return (
        getattr(layout, "density", None),
        getattr(layout, "maximized", None),
        getattr(layout, "ratio", None),
        tuple(
            sorted(
                (key, value)
                for key, value in scroll.items()
                if isinstance(key, str) and key.startswith(tab + ":")
            )
        ),
        tuple(view.get(key) for key in ("dock", "enabled", "ratio", "top", "selected")),
        document.get("identity"),
        document.get("top"),
        getattr(app, "research_scroll", None),
        tuple(
            analysis.get(key) for key in ("scroll", "zoom", "pan", "window", "preset")
        ),
        tuple(
            tuple(analysis.get(key, ()))
            for key in ("pinned", "hidden", "order", "expanded")
        ),
        tuple(axis.get(key) for key in ("mode", "low", "high")),
        getattr(app, "analysis_result_job", None),
        getattr(app, "analysis_result_generation", None),
        result.get("path"),
        bool(getattr(getattr(views, "g", None), "ascii", False)),
    )


def _clip(rect, visible):
    candidate = Rect(
        max(rect.top, visible.top),
        max(rect.left, visible.left),
        min(rect.bottom, visible.bottom),
        min(rect.right, visible.right),
    )
    return (
        candidate
        if candidate.top < candidate.bottom and candidate.left < candidate.right
        else None
    )


def publish(app, records):
    """Publish transformed control rows from the same bounded plot pipeline."""
    state = initialize(app)
    published = []
    if not _blocked(app):
        for record in records:
            if not _eligible(app, record.key) or len(record.payload) != 7:
                continue
            a, b, c, d, e, f, token = record.payload
            entry = state["entries"].get(record.key)
            if not entry or token != entry["token"]:
                continue
            toggle = Rect(
                record.rect.top,
                record.rect.left + a,
                record.rect.bottom,
                record.rect.left + b,
            )
            slider = Rect(
                record.rect.top,
                record.rect.left + c,
                record.rect.bottom,
                record.rect.left + d,
            )
            rate_slider = Rect(
                record.rect.top,
                record.rect.left + e,
                record.rect.bottom,
                record.rect.left + f,
            )
            visible_toggle = _clip(toggle, record.visible)
            visible_slider = _clip(slider, record.visible)
            visible_rate = _clip(rate_slider, record.visible)
            if visible_toggle and visible_slider and visible_rate:
                published.append(
                    Controls(
                        record.key,
                        token,
                        record.rect,
                        record.visible,
                        visible_toggle,
                        visible_slider,
                        slider,
                        record.layer,
                        visible_rate,
                        rate_slider,
                    )
                )
            if len(published) >= MAX_METRICS:
                break
    state["records"] = tuple(published)
    state["context"] = _context(app)
    if not _blocked(app):
        # These records are only a command target after returning from the
        # palette. Never put them back into mouse or keyboard publication.
        state["command_records"] = state["records"]
        state["command_context"] = state["context"]
        state["command_viewport"] = _viewport(app)
    tick(app)
    return state["records"]


def descriptors(app):
    state = initialize(app)
    if _blocked(app) or state["context"] != _context(app):
        return ()
    output = []
    for control in state["records"]:
        for kind, rect, label, command in (
            (
                "metric-live",
                control.toggle,
                "Toggle Live metric window",
                "metric-live " + control.token + " toggle",
            ),
            (
                "metric-window",
                control.slider,
                "Adjust Live metric time window",
                "metric-window " + control.token + " focus",
            ),
            (
                "metric-sampling",
                control.rate_slider,
                "Adjust metric polling interval",
                "metric-sampling " + control.token + " focus",
            ),
        ):
            output.append(
                {
                    "id": kind + ":" + control.token,
                    "label": label,
                    "rect": rect,
                    "action": ("command", command),
                    "group": "metric-live",
                }
            )
    return tuple(output)


def _current(app, token):
    state = initialize(app)
    if _blocked(app) or state["context"] != _context(app):
        return None
    return next(
        (
            control
            for control in state["records"]
            if control.token == token and _eligible(app, control.key)
        ),
        None,
    )


def _command_current(app, token):
    control = _current(app, token)
    if control is not None:
        return control
    state = initialize(app)
    context = state["context"]
    if (
        _blocked(app)
        or not context
        or context[0] != "palette"
        or state.get("command_context") != _context(app)
        or state.get("command_viewport") != _viewport(app)
    ):
        return None
    return next(
        (
            control
            for control in state.get("command_records", ())
            if control.token == token and _eligible(app, control.key)
        ),
        None,
    )


def active(app):
    return initialize(app)["capture"] is not None


def cancel(app):
    state = initialize(app)
    capture = state["capture"]
    if capture:
        entry = state["entries"].get(capture["control"].key)
        field = "rate" if capture.get("kind") == "sampling" else "delta"
        if (
            entry
            and entry.get("token") is not None
            and entry.get("token") == getattr(capture["control"], "token", None)
            and entry.get("running")
            and _eligible(app, capture["control"].key)
            and entry[field] != capture["original"]
        ):
            entry[field] = capture["original"]
            if field == "rate" or entry["enabled"]:
                state["revision"] += 1
            if field == "rate":
                _sync_sampling(app)
    if capture:
        state["cancelled_release"] = True
    state["capture"] = None
    state["focus"] = None
    state["focus_kind"] = "window"
    state["pending_focus"] = None
    return capture is not None


def tick(app, now=None, *, identities=None):
    state = initialize(app)
    # Maintenance validates all retained collection requests. Cosmetic feedback
    # needs only its visible controls; visiting offscreen requests for every
    # mouse report repeatedly acquires Store locks with no visible benefit.
    entries = (state["entries"].items() if identities is None else
               ((identity, state["entries"].get(identity))
                for identity in dict.fromkeys(identities)))
    for identity, entry in entries:
        if not entry:
            continue
        if entry["enabled"] or entry.get("rate", MIN_RATE) != MIN_RATE:
            _eligible(app, identity)
    capture = state["capture"]
    if capture:
        current = _current(app, capture["control"].token)
        now = time.monotonic() if now is None else now
        if (
            current != capture["control"]
            or capture["context"] != _context(app)
            or now - capture["last"] > CAPTURE_TIMEOUT
        ):
            cancel(app)
    if state["focus"]:
        current = _current(app, state["focus"])
        pending = state.get("pending_focus")
        if pending:
            valid = (
                not _blocked(app)
                and pending["control"].token == state["focus"]
                and pending["context"] == _context(app)
                and pending["viewport"] == _viewport(app)
                and _eligible(app, pending["control"].key)
            )
            # The chart modal's outer compose publishes before its overlay.
            # Wait for that overlay, while keeping the same guarded source.
            awaiting_overlay = getattr(app, "mode", "main") == "analysis" and not any(
                plot is not None and plot.layer == 1
                for plot in getattr(app, "chart_interaction_state", {}).get(
                    "pending", ()
                )
            )
            awaiting_frame = (
                state["context"] and state["context"][0] == "palette"
            ) or awaiting_overlay
            if not valid or (current != pending["control"] and not awaiting_frame):
                state["focus"] = None
                state["pending_focus"] = None
            elif current is not None:
                state["pending_focus"] = None
        elif current is None:
            state["focus"] = None
    else:
        state["pending_focus"] = None


def _move(app, control, x, kind="window"):
    slider = control.rate_slider_full if kind == "sampling" else control.slider_full
    if slider is None:
        return
    span = slider.right - slider.left - 1
    position = (x - slider.left) / max(1, span)
    if kind == "sampling":
        set_rate(app, control.key, rate_at(position))
    else:
        set_delta(app, control.key, delta_at(position))


def _focus_graph(app, token, kind):
    """Transfer focus to this already published control without activating it."""
    from .interaction import _current as current_graph, initialize as interaction_state

    graph = current_graph(app)
    identity = kind + ":" + token
    target = graph.get(identity) if graph else None
    if target is not None and target.enabled:
        state = interaction_state(app)
        state["focused"], state["active"] = identity, True
        browser = getattr(app, "history_browser_state", None)
        if isinstance(browser, dict):
            browser["focused"] = False


@capture_handler("metric_live")
def handle_mouse(app, y, x, button="left", shift=False):
    state = initialize(app)
    previous = state["capture"]
    if (
        button == "release"
        and previous is None
        and state.pop("cancelled_release", False)
    ):
        return True
    if button in ("press", "left"):
        state["cancelled_release"] = False
    if any(not isinstance(v, int) or isinstance(v, bool) for v in (y, x)):
        return cancel(app)
    # Hover and an orphaned drag/release cannot change a control. In
    # particular, a chart drag passes through this handler on its way to the
    # owning graph; do not revalidate every unrelated live metric for it.
    if previous is None and button not in ("press", "left", "right"):
        return False
    tick(app)
    capture = state["capture"]
    if previous and not capture and button in ("motion", "drag", "release"):
        return True
    if capture:
        if button in ("motion", "drag", "release"):
            control = capture["control"]
            if (
                button == "release"
                and not control.visible.top <= y < control.visible.bottom
            ):
                cancel(app)
                return True
            _move(app, control, x, capture.get("kind", "window"))
            capture["last"] = time.monotonic()
            if button == "release":
                state["capture"] = None
            return True
        cancel(app)
    if (
        button not in ("press", "left", "right")
        or _blocked(app)
        or state["context"] != _context(app)
    ):
        return False
    for control in reversed(state["records"]):
        if button == "right":
            if control.slider.contains(y, x):
                set_delta(app, control.key, MAX_DELTA)
                return True
            if control.rate_slider and control.rate_slider.contains(y, x):
                set_rate(app, control.key, MIN_RATE)
                return True
            continue
        if control.toggle.contains(y, x):
            from .chart_interaction import cancel as cancel_chart

            cancel_chart(app)
            entry = state["entries"][control.key]
            set_enabled(app, control.key, not entry["enabled"])
            _focus_graph(app, control.token, "metric-live")
            state["focus"] = None
            return True
        kind = None
        if control.slider.contains(y, x):
            kind = "window"
        elif control.rate_slider and control.rate_slider.contains(y, x):
            kind = "sampling"
        if kind:
            from .chart_interaction import cancel as cancel_chart

            cancel_chart(app)
            state["focus"] = control.token
            state["focus_kind"] = kind
            _focus_graph(
                app, control.token,
                "metric-sampling" if kind == "sampling" else "metric-window",
            )
            if button == "press":
                state["capture"] = {
                    "control": control,
                    "context": _context(app),
                    "kind": kind,
                    "original": state["entries"][control.key][
                        "rate" if kind == "sampling" else "delta"
                    ],
                    "last": time.monotonic(),
                }
            _move(app, control, x, kind)
            return True
    state["focus"] = None
    return False


def handle_key(app, key):
    state = initialize(app)
    if active(app):
        cancel(app)
        return key == "esc"
    control = _current(app, state["focus"]) if state["focus"] else None
    if not control:
        return False
    if key in ("esc", "enter", "tab", "btab", "up", "down"):
        state["focus"] = None
        return key in ("esc", "enter")
    if key in ("left", "right", "home", "end", "pgup", "pgdn"):
        entry = state["entries"][control.key]
        if state.get("focus_kind") == "sampling":
            step = 10 if key in ("pgup", "pgdn") else 1
            value = MIN_RATE if key == "home" else MAX_RATE if key == "end" else (
                min(MAX_RATE, max(
                    MIN_RATE,
                    entry.get("rate", MIN_RATE)
                    + (-step if key in ("left", "pgup") else step),
                ))
            )
            set_rate(app, control.key, value)
            return True
        step = (5 if key in ("pgup", "pgdn") else 1) / max(
            1, control.slider_full.right - control.slider_full.left - 1
        )
        value = (
            0.0
            if key == "home"
            else (
                1.0
                if key == "end"
                else fraction(entry["delta"])
                + (-step if key in ("left", "pgup") else step)
            )
        )
        set_delta(app, control.key, delta_at(value))
        return True
    return False


def command_names():
    return ["metric-live", "metric-window", "metric-sampling"]


def run_command(app, args):
    if not args or args[0] not in command_names():
        return False
    control = _command_current(app, args[1]) if len(args) >= 2 else None
    if not control:
        _say(app, "Select a currently running metric's visible Live control")
        return True
    if (
        args[0] == "metric-live"
        and len(args) == 3
        and args[2] in ("on", "off", "toggle")
    ):
        entry = initialize(app)["entries"][control.key]
        set_enabled(
            app,
            control.key,
            not entry["enabled"] if args[2] == "toggle" else args[2] == "on",
        )
    elif args[0] in ("metric-window", "metric-sampling") and len(args) == 3:
        kind = "sampling" if args[0] == "metric-sampling" else "window"
        if args[2] == "focus":
            state = initialize(app)
            state["focus"] = control.token
            state["focus_kind"] = kind
            state["pending_focus"] = (
                {
                    "control": control,
                    "context": _context(app),
                    "viewport": _viewport(app),
                }
                if _current(app, control.token) is None
                else None
            )
        elif kind == "sampling":
            try:
                value = MIN_RATE if args[2] == "reset" else int(args[2])
            except (ValueError, OverflowError):
                value = None
            if set_rate(app, control.key, value):
                from .metric_sampling import cadence, format_interval

                glyphs = getattr(getattr(app, "views_ref", None), "g", None)
                ascii_ = bool(getattr(glyphs, "ascii", False))
                interval = cadence(app, control.key)
                description = (
                    format_interval(interval, ascii_=ascii_)
                    if interval is not None else "unavailable"
                )
                _say(app, "Metric polling interval: " + description)
            else:
                _say(app, "Metric sampling rate must be an integer from 1 to 100")
        else:
            try:
                value = MAX_DELTA if args[2] == "reset" else float(args[2])
            except (ValueError, OverflowError):
                value = float("nan")
            if not set_delta(app, control.key, value):
                _say(app, "Metric window must be 1 to 30 seconds")
    else:
        _say(
            app,
            "metric-live TOKEN on|off|toggle | "
            "metric-window TOKEN focus|SECONDS|reset | "
            "metric-sampling TOKEN focus|1..100|reset",
        )
    return True


def document_revision(app):
    return initialize(app)["revision"]


def document_interval(app):
    """At most ten refreshes/second while a following metric plot is visible."""
    if _blocked(app):
        return None
    chart = getattr(app, "chart_interaction_state", {})
    capture = chart.get("capture")
    captured = canonical(capture["plot"].key) if capture else None
    state = initialize(app)
    identities = {canonical(record.key) for record in state["records"]}
    # Narrow cards can show a plot without room for its slider row.
    identities.update(canonical(plot.key) for plot in chart.get("plots", ()))
    intervals = []
    for identity in identities:
        if identity is None or identity == captured:
            continue
        entry = state["entries"].get(identity)
        if not entry or not entry.get("following", True):
            continue
        if enabled(app, identity) or buffered(app, identity):
            intervals.append(entry.get("display_interval", DOCUMENT_INTERVAL))
    return min(intervals) if intervals else None


def feedback(app, g):
    """Update only the small cached control rows between document refreshes."""
    state = initialize(app)
    tick(app, identities=(control.key for control in state["records"]))
    if _blocked(app) or state["context"] != _context(app):
        return []
    from .metric_sampling import cadence

    previous = state.get("feedback_rows", {})
    current = {}
    output = []
    for control in state["records"]:
        entry = state["entries"].get(control.key)
        if not entry:
            continue
        poll_interval = cadence(app, control.key)
        cache_key = (
            control.key, control.token, control.rect, control.visible,
            bool(g.ascii), state["revision"], entry["enabled"],
            entry["delta"], entry.get("rate", MIN_RATE), poll_interval,
        )
        row = previous.get(cache_key)
        if row is None:
            row, _ = _row(
                g, entry, control.rect.right - control.rect.left,
                app=app, identity=control.key, poll_interval=poll_interval,
            )
            row = tuple(row)
        current[cache_key] = row
        left = control.visible.left - control.rect.left
        # Control text is one-cell terminal glyphs; cut in display coordinates.
        clipped = []
        position = 0
        for text, style in row:
            size = L.vlen(text)
            if position + size > left:
                skip = max(0, left - position)
                clipped.append((text[skip:], style))
            position += size
        from .interaction import decorate

        rendered = L.clip_row(clipped, control.visible.right - control.visible.left)
        rendered = decorate(
            app, [rendered], origin=(control.visible.top, control.visible.left)
        )[0]
        output.append((control.visible.top, control.visible.left, rendered))
    # Retain only the currently published controls. A viewport/source change
    # cannot grow this cache or reuse a prior attempt's row.
    state["feedback_rows"] = current
    return output


def overlay(views, snap, app, width, height):
    return None
