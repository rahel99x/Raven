"""Adaptive accounting viewport with bounded preview and exact job selection.

This module reads published observations. Pointer and key navigation do not
fetch accounting, prepare logs, persist preferences, or start worker tasks.
The default preview inspects only enough source records to fill its viewport.
Older records are admitted in page-sized blocks when the user asks for them.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from functools import lru_cache
import math
import os
from operator import attrgetter
import time

from . import clock
from .model import Finished, Job, Live, stamp

INDEX_THRESHOLD = 256
LOAD_BLOCK = 32


@dataclass
class RecentState:
    enabled: bool = False
    ratio: int = 35
    manual_split: bool = False
    page: int = 5
    queue_count: int = 0
    target: int = 5
    exhausted: bool = False
    focus: bool = False
    rect: object = None
    loaded: list = field(default_factory=list)
    observation: dict = field(default_factory=dict, repr=False)
    options: object = None
    index_key: object = field(default=None, repr=False)
    index: list = field(default_factory=list, repr=False)
    index_generation: int = 0
    sorted_key: object = field(default=None, repr=False)
    sorted_rows: list = field(default_factory=list, repr=False)
    expiry: float | None = None
    projector: object = field(default=None, repr=False)


def initialize(app):
    state = getattr(app, "recent_history_state", None)
    if not isinstance(state, RecentState):
        state = RecentState()
        app.recent_history_state = state
    return state


def _base(app):
    from .table_tools import recent_limit
    value = recent_limit(app)
    return max(1, value) if type(value) is int else 5


def allocate(app, height, queue_count, *, reserved=0, recent_count=None):
    """Return Queue and Recents data rows within the actual Main pane budget.

    Each nonempty table has a section rule and a column heading. Small panes
    preserve one selectable row of each section before either receives more.
    The saved Recents count sets the preview size, rather than a history cap.
    """
    state = initialize(app)
    state.enabled = True
    state.queue_count = max(0, int(queue_count))
    state.focus = bool(state.loaded) and getattr(app, "cursor", {}).get("jobs", 0) >= state.queue_count
    height = max(0, int(height) - max(0, int(reserved)))
    if recent_count == 0:
        state.rect = None
        state.page = max(1, height - 2)
        return min(state.queue_count, max(0, height - 2)), 0
    if not state.queue_count:
        queue_rows, recent_rows = 0, max(0, height - 2)
    elif height < 6:
        # Two section rules and two table headings cannot share fewer than
        # six rows while keeping either section selectable. Show the focused
        # section with its normal header instead of hiding both tables.
        capacity = max(0, height - 2)
        queue_rows, recent_rows = (0, capacity) if state.focus else (min(state.queue_count, capacity), 0)
    else:
        slots = max(0, height - 4)
        recent_rows = max(1, min(slots - 1, round(slots * state.ratio / 100)))
        queue_rows = slots - recent_rows
        if not state.manual_split:
            queue_rows = min(state.queue_count, queue_rows)
            recent_rows += max(0, slots - recent_rows - queue_rows)
    state.page = max(1, recent_rows)
    state.target = max(state.target, _base(app), state.page)
    return queue_rows, recent_rows


def resize(app, percentage):
    """Set Recents' share of Queue/Recents space, without fetching or saving."""
    state = initialize(app)
    state.enabled = True
    state.manual_split = True
    state.ratio = max(10, min(90, int(percentage)))


def candidate_limit(app):
    state = initialize(app)
    if not state.enabled:
        return _base(app)
    top = max(0, getattr(app, "top", {}).get("recent", 0))
    return max(_base(app), state.target, top + state.page)


@lru_cache(maxsize=32)
def _record_getter(record_type):
    names = tuple(item.name for item in fields(record_type))
    return attrgetter(*names), len(names)


def _record_key(record):
    """Include content as well as identity: accounting can amend the same ID."""
    if is_dataclass(record):
        getter, count = _record_getter(type(record))
        values = getter(record)
        if count == 1:
            values = (values,)
        # Finished and Live observations contain scalar fields. A C-level
        # attrgetter avoids constructing twenty field/value pairs per record
        # each time a deliberately opened large history index is validated.
        if type(record) not in (Finished, Live):
            values = tuple(tuple(value) if isinstance(value, list) else value for value in values)
    else:
        values = tuple(sorted(vars(record).items()))
    return id(record), values


def _tag_key(tags):
    return tuple(sorted((str(jid), tuple(sorted((str(key), tuple(value) if isinstance(value, list) else value)
                                               for key, value in entry.items())))
                        for jid, entry in tags.items() if isinstance(entry, dict)))


def _matching(app, snap, source, *, active, pending_ids=(), limit=None):
    from .table_tools import filter_text, recent_matches
    from .table_ui import matches
    text = filter_text(app, "recent").casefold()
    result, seen = [], set(pending_ids)
    for record in source:
        if record.id in active or record.id in seen:
            continue
        if not matches(app, "recent", record, snap) or not recent_matches(app, record):
            continue
        if text.startswith("#"):
            if text[1:] not in [str(tag).casefold() for tag in snap.get("tags", {}).get(record.id, {}).get("tags", [])]:
                continue
        elif text:
            values = (record.id, record.name, record.partition,
                      "awaiting accounting" if isinstance(record, Job) else record.state)
            if not any(text in str(value).casefold() for value in values):
                continue
        result.append(record)
        seen.add(record.id)
        if limit is not None and len(result) >= limit:
            break
    return result


def records(app, snap):
    """Return the loaded Recents candidates in their complete displayed order.

    Sorting applies after candidate admission, as with the earlier Recents
    preview. Large, deliberately opened history prefixes cache filter work.
    That cache checks record contents, tags, active IDs and filter settings;
    history_revision alone does not prove amended accounting is unchanged.
    """
    from .table_ui import fingerprint, history_value, sort_rows
    from .table_tools import snapshot
    snap = snapshot(app, snap)
    state = initialize(app)
    options = fingerprint(app, "recent")
    selected = getattr(app, "selected_id", None)
    anchor = (selected if state.enabled and options == state.options and
              any(record.id == selected for record in state.loaded) else None)
    if state.options is not None and options != state.options:
        state.target, state.exhausted = max(_base(app), state.page), False
        getattr(app, "top", {}).update(recent=0)
        state.index_key, state.index = None, []
        state.sorted_key, state.sorted_rows = None, []
    state.options = options
    # Retain only observation fields required by filters and typed sorting.
    state.observation = {key: snap.get(key, {} if key in ("departed_jobs", "tags", "live", "details") else [])
                         for key in ("jobs", "finished", "departed_jobs", "tags", "live", "details", "group")}
    active = {job.id for job in snap.get("jobs", [])}
    limit = candidate_limit(app)
    pending = _matching(app, snap, reversed(list(snap.get("departed_jobs", {}).values())),
                        active=active, limit=limit)
    pending_ids = {job.id for job in pending}
    needed = max(0, limit - len(pending))
    indexed = False
    if not needed:
        finished = []
    elif state.enabled and limit > INDEX_THRESHOLD:
        indexed = True
        source = snap.get("finished", [])
        window = getattr(app, "table_tools_state", {}).get("recents", {}).get("window")
        # Numeric filters can use retained live samples even after accounting
        # appears. A timezone change also changes local Slurm date boundaries.
        live_key = tuple(sorted((jid, _record_key(value)) for jid, value in snap.get("live", {}).items()))
        zone = (os.environ.get("TZ"), time.tzname, time.timezone,
                getattr(time, "altzone", time.timezone), time.daylight) if window else None
        key = (tuple(_record_key(record) for record in source), tuple(sorted(active)),
               tuple(sorted(pending_ids)), _tag_key(snap.get("tags", {})), live_key, options, zone)
        now = clock.now()
        if key != state.index_key or state.expiry is not None and now >= state.expiry:
            state.index = _matching(app, snap, source, active=active, pending_ids=pending_ids)
            state.index_key = key
            state.index_generation += 1
            boundaries = []
            if window:
                for record in source:
                    ended = stamp(getattr(record, "end", ""))
                    if ended is not None:
                        # The lower time boundary is inclusive; expiration
                        # occurs immediately after it, rather than at it.
                        boundaries.extend(point for point in (ended, math.nextafter(ended + window, math.inf)) if point > now)
            state.expiry = min(boundaries) if boundaries else None
        finished = state.index[:needed]
    else:
        finished = _matching(app, snap, snap.get("finished", []), active=active,
                             pending_ids=pending_ids, limit=needed)
    if anchor and not any(record.id == anchor for record in pending + finished):
        # New completions can push a selected boundary row past a bounded
        # preview while Queue size remains constant. Find only this displaced
        # identity, and admit enough accounting prefix to keep it available.
        # Removed, filtered, expired or newly active jobs are not reintroduced.
        found = next(((index, record) for index, record in enumerate(snap.get("finished", []))
                      if record.id == anchor), None)
        if found and _matching(app, snap, [found[1]], active=active, pending_ids=pending_ids):
            state.target = max(limit + LOAD_BLOCK, found[0] + len(pending) + 1)
            return records(app, snap)
    sorted_key = (state.index_generation, limit, tuple(_record_key(record) for record in pending), options) if indexed else None
    if indexed and sorted_key == state.sorted_key:
        result = list(state.sorted_rows)
    else:
        result = sort_rows(app, "recent", pending + finished,
                           value=lambda record, column: history_value(record, column, snap))
        if indexed:
            state.sorted_key, state.sorted_rows = sorted_key, list(result)
    state.loaded = result
    state.exhausted = len(result) < limit
    return result


def publish(app, records_, page, queue_count, *, rect=None):
    """Publish actual data capacity and absolute pointer bounds after layout."""
    state = initialize(app)
    state.page = max(1, int(page))
    state.queue_count = max(0, int(queue_count))
    state.loaded = list(records_)
    state.rect = rect
    if state.enabled:
        state.target = max(state.target, state.page)


def register_scrollbar(app, width, first, page, painted, *, header):
    """Seek within retained observations; admit older candidates at next paint.

    A bottom request must reach the accounting history, rather than the end of
    the small preview. Pointer input changes only the admission budget and
    logical offset; filters, grouping and sorting remain render work.
    """
    from . import scrollbars
    state = initialize(app)
    if width < 2 or page <= 0:
        return
    retained = (len(state.observation.get("finished", ())) +
                len(state.observation.get("departed_jobs", {})))
    loaded_count = len(getattr(app, "recent_ids", state.loaded))
    count = loaded_count if state.exhausted else max(loaded_count, retained)
    def seek(value):
        value = max(0, int(value))
        if not state.exhausted:
            state.target = max(state.target, value + max(1, page))
        app.top["recent"] = value
    scrollbars.register(app, "recent", (first, 0, first + page, width),
        count, page, getattr(app, "top", {}).get("recent", painted), painted, seek,
        header=(header, 0, width) if width >= 6 else None)


def contains(app, y, x):
    rect = initialize(app).rect
    if rect is None:
        return False
    try:
        return rect.x <= x < rect.x + rect.width and rect.y <= y < rect.y + rect.height
    except AttributeError:
        left, top, width, height = rect
        return left <= x < left + width and top <= y < top + height


def _grow(app, desired, *, all_records=False):
    state = initialize(app)
    if state.exhausted or not state.observation:
        return
    block = max(LOAD_BLOCK, state.page)
    if all_records:
        state.target = len(state.observation.get("finished", [])) + len(state.observation.get("departed_jobs", {})) + 1
    else:
        state.target = max(state.target + block, desired + block)
    previous = getattr(app, "selected_id", None)
    while True:
        raw = records(app, state.observation)
        if callable(state.projector):
            state.loaded = list(state.projector(state.observation, raw))
        if len(state.loaded) > desired or state.exhausted or all_records:
            break
        # A folded launch can occupy many raw rows but one visible entry.
        # Skip its admitted members geometrically rather than queuing one
        # small load per hidden job while the user waits for the next group.
        state.target = max(state.target + block, state.target * 2)
    app.recent_ids = [record.id for record in state.loaded]
    if previous in app.recent_ids:
        app.cursor["jobs"] = state.queue_count + app.recent_ids.index(previous)


def navigate(app, action, *, wheel=False):
    """Move exact Recents IDs, loading more only at the admitted prefix edge."""
    state = initialize(app)
    if not state.enabled or getattr(app, "tab", "") != "jobs" or getattr(app, "mode", "main") != "main":
        return False
    if action not in ("up", "down", "page_up", "page_down", "home", "end"):
        return False
    cur = getattr(app, "cursor", {}).get("jobs", 0)
    if not wheel and cur < state.queue_count:
        return False
    state.focus = True
    cur = max(0, cur - state.queue_count)
    if wheel and getattr(app, "cursor", {}).get("jobs", 0) < state.queue_count:
        cur = max(0, getattr(app, "top", {}).get("recent", 0))
    delta = {"up": -1, "down": 1, "page_up": -state.page, "page_down": state.page}.get(action, 0)
    desired = 0 if action == "home" else cur + delta
    if action == "end":
        _grow(app, desired, all_records=True)
        desired = len(state.loaded) - 1
    elif desired >= len(state.loaded) and not state.exhausted:
        selected = getattr(app, "selected_id", None)
        _grow(app, desired)
        if selected in getattr(app, "recent_ids", []):
            cur = app.recent_ids.index(selected)
            desired = cur + delta
    if desired < 0 and not wheel and state.queue_count:
        app.cursor["jobs"] = state.queue_count - 1
        state.focus = False
        ids = getattr(app, "visible_ids", [])
        app.selected_id = ids[-1] if ids else None
        return True
    if not state.loaded:
        return True
    desired = max(0, min(len(state.loaded) - 1, desired))
    app.cursor["jobs"] = state.queue_count + desired
    app.selected_id = state.loaded[desired].id
    top = max(0, getattr(app, "top", {}).get("recent", 0))
    if desired < top:
        top = desired
    elif desired >= top + state.page:
        top = desired - state.page + 1
    app.top["recent"] = max(0, min(max(0, len(state.loaded) - state.page), top))
    return True


def handle_key(app, key):
    if getattr(getattr(app, "layout_state", None), "focus", "main") != "main":
        return False
    panel = getattr(app, "job_panel_state", {})
    if isinstance(panel, dict) and panel.get("focus"):
        return False
    action = getattr(app, "keymap", {}).get(key, key)
    action = {"pgup": "page_up", "pgdn": "page_down"}.get(action, action)
    return navigate(app, action)


def handle_mouse(app, y, x, button="left", shift=False):
    if button not in ("wheel-up", "wheel-down", "wheel_up", "wheel_down") or not contains(app, y, x):
        return False
    layout = getattr(app, "layout_state", None)
    if layout is not None:
        layout.focus = "main"
    panel = getattr(app, "job_panel_state", {})
    if isinstance(panel, dict):
        panel["focus"] = ""
    return navigate(app, "up" if button in ("wheel-up", "wheel_up") else "down", wheel=True)


def save(app):
    state = initialize(app)
    return {"version": 1, "ratio": state.ratio, "manual": state.manual_split}


def restore(app, data):
    if not isinstance(data, dict) or data.get("version", 1) != 1:
        return
    ratio, manual = data.get("ratio"), data.get("manual", False)
    if type(ratio) is int and 10 <= ratio <= 90 and type(manual) is bool:
        state = initialize(app)
        state.ratio, state.manual_split = ratio, manual


def command_names():
    return []


def run_command(app, args):
    return False


def overlay(views, snap, app, width, height):
    return None
