"""Universal rendered-line selection over published terminal panes.

Pointer work reads only the last painted frame. A selected line is pinned as
text, so sampler updates cannot silently change the content being copied.
Raw log selection remains owned by LogSession and keeps original source bytes.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil

from . import clipboard, layout as L
from .interaction import Rect

from .pointer_routing import capture_handler

MAX_LINES = 20000
MAX_BYTES = 8 << 20
MAX_PANES = 128
from .interaction import ROW_KINDS
PROTECTED_KINDS = ROW_KINDS | frozenset(("log_line", "node_cell"))
EDIT_MODES = frozenset(("palette", "filter", "confirm", "bindings_editor", "terminal_probe",
                        "log_tools_page", "log_tools_results", "log_tools_marks"))


@dataclass(frozen=True)
class Pane:
    key: str
    rect: Rect
    context: object
    top: int = 0
    count: int = 0
    setter: object = None
    stride: int = 1
    layer: int = 0
    marker_column: object = None

    @property
    def page(self):
        return self.rect.bottom - self.rect.top


class _VisibleLines(Sequence):
    """Lazy, frame-owned view preserving the published visible-lines API."""
    def __init__(self, rows, pane):
        self.rows, self.pane, self.values = rows, pane, None

    def _values(self):
        if self.values is None:
            state, pane = {"rows": self.rows}, self.pane
            self.values = tuple((pane.top + y - pane.rect.top, _line(state, pane, y))
                                for y in range(pane.rect.top, pane.rect.bottom))
        return self.values

    def __len__(self):
        return self.pane.page

    def __getitem__(self, index):
        return self._values()[index]

    def __iter__(self):
        return iter(self._values())


def initialize(app):
    state = getattr(app, "text_selection_state", None)
    if not isinstance(state, dict):
        state = {"panes": (), "rows": {}, "visible": {}, "cache": OrderedDict(), "cache_bytes": 0, "selection": None,
                 "capture": None, "cursor": None, "explicit": False, "token": None,
                 "frame_required": False, "discard_release": False, "invalidated": False}
        app.text_selection_state = state
    return state


def _context(app):
    panels = getattr(app, "job_panel_state", {})
    project = getattr(app, "project_state", {})
    return (getattr(app, "mode", "main"), getattr(app, "tab", ""),
            getattr(app, "analytics_view", None), getattr(app, "research_view", None),
            getattr(app, "nodes_view", None), getattr(app, "analytics_job", None),
            getattr(app, "research_job_id", None), getattr(app, "selected_id", None),
            panels.get("mode"), panels.get("view"), panels.get("analytics_view"),
            panels.get("research_view"), project.get("root"))


def _token(app):
    toolbar = getattr(app, "toolbar_state", {})
    return (_context(app), getattr(app, "width", 0), getattr(app, "height", 0),
            toolbar.get("menu"), toolbar.get("panel"))


def clear(app):
    state = initialize(app)
    if state["capture"]:
        state["discard_release"] = True
    state.update(selection=None, capture=None, explicit=False, cursor=None, invalidated=False)


def active(app):
    return bool(initialize(app)["capture"])


def selected(app):
    return bool(initialize(app)["selection"])


def _slice(row, left, right):
    """Slice by terminal cells without tearing wide or combining characters."""
    result, column = [], 0
    for text, style in row:
        if not text:
            continue
        if text.isascii():
            if column >= right:
                return "".join(result).rstrip()
            start = max(0, left - column)
            stop = min(len(text), right - column)
            if start < stop:
                result.append(text[start:stop])
            column += len(text)
            if column > right:
                return "".join(result).rstrip()
            # At the exact right edge, the next segment may begin with
            # combining marks belonging to the last included character.
            continue
        # Most published segments already fit wholly inside one pane. Keep
        # their complete Unicode text instead of visiting every chart cell.
        # A leading combining mark still needs the boundary rules below.
        if len(text) <= 512:
            end = column + L.vlen(text)
            if end <= left:
                column = end
                continue
            if (column >= left and end <= right
                    and (L._character_width(text[0]) or result and left < column)):
                result.append(text)
                column = end
                continue
        for char in text:
            size = L._character_width(char)
            if size == 0:
                if result and left < column <= right:
                    result.append(char)
                continue
            if column >= right:
                return "".join(result).rstrip()
            if column >= left and column + size <= right:
                result.append(char)
            elif column < right and column + size > left:
                result.extend(" " for _ in range(min(right, column + size) - max(left, column)))
            column += size
    return "".join(result).rstrip()


def _region(value):
    if isinstance(value, Rect):
        return value
    if all(hasattr(value, name) for name in ("x", "y", "width", "height")):
        return Rect(value.y, value.x, value.y + value.height, value.x + value.width)
    return None


def _candidates(app, width, height, overlays):
    """Prefer published document geometry; keep a safe frame-only fallback."""
    panes, covered = [], []
    mode = getattr(app, "mode", "main")
    scrollbar_state = getattr(app, "scrollbar_state", {})
    for item in scrollbar_state.get("panes", ())[:MAX_PANES]:
        if mode != "main" and item.layer <= 0:
            continue
        rect = item.rect
        if rect.bottom <= rect.top or rect.right <= rect.left:
            continue
        stride = 1
        if item.key.startswith("history-browser:"):
            frame = getattr(app, "history_browser_state", {}).get("frame") or {}
            stride = max(1, int(frame.get("columns", 1)))
        # The final gutter belongs to the scrollbar, not to document text.
        rect = Rect(rect.top, rect.left, rect.bottom, max(rect.left, rect.right - (1 if item.count > item.page else 0)))
        if rect.right <= rect.left:
            continue
        pane = Pane(item.key, rect, (_context(app), item.context), item.painted // stride,
                    ceil(item.count / stride), item.setter, stride, item.layer,
                    item.rect.right if item.key == "inline-log" else None)
        panes.append(pane)
        covered.append(rect)
    context = _context(app)
    if overlays and mode != "main":
        top = max(1, min(y for y, x, row in overlays) + 1)
        bottom = min(height - 1, max(y for y, x, row in overlays))
        left = max(0, min(x for y, x, row in overlays) + 1)
        right = min(width, max(x + L.vlen(L.row_text(row)) for y, x, row in overlays) - 1)
        if top < bottom and left < right:
            panes.append(Pane("overlay:" + mode, Rect(top, left, bottom, right), context,
                              int(getattr(app, "scroll", 0)), max(bottom - top, len(overlays) - 2), layer=1))
    elif mode == "main":
        regions = [("details", _region(getattr(app, "job_panel_rect", None))),
                   ("main", _region(getattr(app, "workspace_main_rect", None)))]
        for key, rect in regions:
            if rect is not None and rect.bottom > rect.top and rect.right > rect.left:
                panes.append(Pane("frame:" + key, rect, context, count=rect.bottom - rect.top))
        start = max(1, int(getattr(app, "body_origin", 1)))
        if start < height - 1:
            panes.append(Pane("frame:body", Rect(start, 0, height - 1, width), context,
                              count=height - 1 - start))
    return tuple(sorted(panes[:MAX_PANES], key=lambda p: (p.layer, not p.key.startswith("frame:"),
                    -(p.rect.bottom - p.rect.top) * (p.rect.right - p.rect.left)), reverse=True))


def publish(app, rows, width, height, *, overlays=()):
    """Publish text geometry; extract only the pane a user selects or copies."""
    state = initialize(app)
    if height is None or width <= 0 or height <= 1:
        state.update(panes=(), rows={}, token=None)
        return
    app.width, app.height = width, height
    panes = _candidates(app, width, height, overlays)
    source_rows = {y: [(0, row)] for y, row in enumerate(rows[:height])}
    for y, x, row in overlays:
        if 0 <= y < height:
            source_rows.setdefault(y, []).append((x, row))
    state.update(panes=panes, rows=source_rows, token=_token(app))
    # Text extraction is unnecessary during ordinary graph/queue refreshes.
    # Keep this exact painted frame and materialize each pane only when used.
    state["visible"] = {pane.key: _VisibleLines(source_rows, pane) for pane in panes}
    selection = state["selection"]
    matching = next((pane for pane in panes if selection and pane.key == selection["key"]
                     and pane.context == selection["context"]), None)
    if selection and matching is None:
        clear(app)
        # Keep the copy intent after a repaint removes its source. Losing a
        # range must never turn y into a different row, pane or file copy.
        state["invalidated"] = True
        selection = None
    if state["capture"] and (matching is None or state["capture"]["geometry"] != matching.rect):
        state["capture"] = None
        state["discard_release"] = True
    # A single selected source needs a cross-page cache. Idle paints retain
    # only their visible text, keeping sampler updates and memory bounded.
    if selection:
        _remember(state, matching)
    else:
        state["cache"].clear()
        state["cache_bytes"] = 0
    state["frame_required"] = False


def _line(state, pane, y):
    spans = state["rows"].get(y, ())
    if pane.layer > 0:
        spans = spans[-1:]
    elif len(spans) > 1:
        # A modal owns the covered cells. Underlying text does not leak into
        # its copy selection or become an invisible activation target.
        if any(x < pane.rect.right and x + L.vlen(L.row_text(row)) > pane.rect.left for x, row in spans[1:]):
            return None
    if not spans:
        return ""
    x, row = spans[-1]
    return _slice(row, max(0, pane.rect.left - x), max(0, pane.rect.right - x))


def _remember(state, pane):
    if pane is None:
        return
    selection = state["selection"]
    lo, hi = sorted((selection["anchor"], selection["end"])) if selection else (-1, -1)
    cache = state["cache"]
    visible = state["visible"].get(pane.key)
    if visible is None:
        visible = tuple((pane.top + y - pane.rect.top, _line(state, pane, y))
                        for y in range(pane.rect.top, pane.rect.bottom))
        state["visible"][pane.key] = visible
    for index, text in visible:
        if lo <= index <= hi and index in cache:
            continue
        if text is not None:
            old = cache.get(index, "")
            if old == text and index in cache:
                continue
            delta = len(text.encode("utf-8")) - len(old.encode("utf-8"))
            while state["cache_bytes"] + delta > MAX_BYTES:
                candidate = next((i for i in cache if i != index and not lo <= i <= hi), None)
                if candidate is None:
                    break
                state["cache_bytes"] -= len(cache.pop(candidate).encode("utf-8"))
            if state["cache_bytes"] + delta > MAX_BYTES:
                continue
            state["cache_bytes"] += delta
            cache[index] = text
    while len(cache) > MAX_LINES or state["cache_bytes"] > MAX_BYTES:
        candidate = next((i for i in cache if not lo <= i <= hi), None)
        if candidate is None:
            break
        state["cache_bytes"] -= len(cache.pop(candidate).encode("utf-8"))


def _pane_at(state, y, x):
    return next((p for p in state["panes"] if p.rect.contains(y, x)), None)


def _current(app):
    state = initialize(app)
    if state["token"] == _token(app):
        return state
    # A source, mode or geometry can change between paint and input. End the
    # old gesture now rather than letting its capture survive until a paint.
    if state["capture"]:
        state["capture"] = None
        state["discard_release"] = True
    return None


def _protected(app, y, x, pane, control=None):
    if control is not None and control.action[:1] in (("row",), ("click",)):
        return True
    if getattr(app, "mode", "main") != "main":
        return False
    if getattr(app, "tab", "") == "log" and not getattr(getattr(app, "logs", None), "browser", True):
        return True
    details = _region(getattr(app, "job_panel_rect", None))
    if details is not None and details.contains(y, x):
        return False
    main = _region(getattr(app, "workspace_main_rect", None))
    if main is not None and not main.contains(y, x):
        return False
    return any(row == y and kind in PROTECTED_KINDS for row, kind, value in getattr(app, "last_hits", ()))


def _start(app, state, pane, y, explicit=False):
    index = pane.top + max(0, min(pane.page - 1, y - pane.rect.top))
    state["cache"].clear()
    state["cache_bytes"] = 0
    state.update(selection={"key": pane.key, "context": pane.context, "anchor": index, "end": index},
                 cursor=(pane.key, index), explicit=bool(explicit), invalidated=False)
    _remember(state, pane)
    # Existing screen-wide ranges must not overwrite a pane-local range.
    app.sel_anchor = None
    pointer = getattr(app, "interaction_state", None)
    if isinstance(pointer, dict):
        pointer.update(active=False, focused=None)


def extend_row_range(app, y, x):
    """Start a native row's Shift range inside its published document pane.

    Job and source-log ranges are routed to their owners before this fallback.
    Screen row numbers alone cannot isolate a native resource or file catalog
    from another column at the same height.
    """
    state = _current(app)
    if state is None:
        return False
    pane = _pane_at(state, y, x)
    if pane is None:
        return False
    cursor = getattr(app, "cursor_row", None)
    anchor = cursor() if callable(cursor) else getattr(app, "click_row", None)
    if not isinstance(anchor, int) or not pane.rect.top <= anchor < pane.rect.bottom:
        anchor = y
    _start(app, state, pane, anchor, explicit=True)
    state["selection"]["end"] = pane.top + y - pane.rect.top
    state["cursor"] = pane.key, state["selection"]["end"]
    _remember(state, pane)
    return True


@capture_handler("text_selection")
def handle_mouse(app, y, x, button="left", shift=False):
    if any(type(value) is not int for value in (y, x)):
        return False
    if button in ("press", "left"):
        previous = initialize(app)
        previous["capture"] = None
        previous["discard_release"] = False
    state = _current(app)
    if state is None or getattr(app, "mode", "main") in EDIT_MODES:
        stale = initialize(app)
        if button == "release" and stale["discard_release"]:
            stale["discard_release"] = False
            return True
        return False
    if button == "right":
        if selected(app) or active(app):
            clear(app)
            return True
        return False
    capture = state["capture"]
    if button in ("motion", "drag", "release"):
        if capture is None:
            if button == "release" and state["discard_release"]:
                state["discard_release"] = False
                return True
            return False
        pane = next((p for p in state["panes"] if p.key == capture["key"]
                     and p.context == capture["context"] and p.rect == capture["geometry"]), None)
        if pane is None:
            clear(app)
            state["invalidated"] = True
            return True
        index = pane.top + max(0, min(pane.page - 1, y - pane.rect.top))
        if state["selection"]["end"] != index:
            state["selection"]["end"] = index
            state["cursor"] = pane.key, index
            _remember(state, pane)
        if button == "release":
            state["capture"] = None
        return True
    if button not in ("left", "press"):
        return False
    pane = _pane_at(state, y, x)
    if pane is None:
        return False
    graph = getattr(app, "interaction_state", {}).get("graph")
    control = graph.at(y, x) if graph is not None else None
    if control is not None and control.button:
        return False
    if _protected(app, y, x, pane, control) and not state["explicit"]:
        return False
    text = _line(state, pane, y)
    if not text:
        return False
    selection = state["selection"]
    if shift and selection and selection["key"] == pane.key and selection["context"] == pane.context:
        selection["end"] = pane.top + y - pane.rect.top
        _remember(state, pane)
    else:
        _start(app, state, pane, y, explicit=state["explicit"])
    if button == "press":
        state["capture"] = {"key": pane.key, "context": pane.context, "geometry": pane.rect}
        state["discard_release"] = False
    return True


def _focused(app, state):
    selection = state["selection"]
    if selection:
        return next((p for p in state["panes"] if p.key == selection["key"] and p.context == selection["context"]), None)
    panes = state["panes"]
    if getattr(app, "mode", "main") != "main":
        return next((p for p in panes if p.layer > 0), None)
    tab = getattr(app, "tab", "")
    layout = getattr(app, "layout_state", None)
    focus = getattr(layout, "focus", "main")
    if focus == "details":
        target = next((p for p in panes if p.key == "workspace:" + tab + ":details"), None)
        if target is not None:
            return target
        target = next((p for p in panes if p.key == "frame:details"), None)
        if target is not None:
            return target
    if getattr(app, "history_browser_state", {}).get("focused"):
        target = next((p for p in panes if p.key == "history-browser:" + tab), None)
        if target is not None:
            return target
    if tab == "history":
        target = next((p for p in panes if p.key == "history"), None)
        if target is not None:
            return target
    if tab == "jobs":
        key = "recent" if getattr(app, "cursor", {}).get("jobs", 0) >= len(getattr(app, "visible_ids", ())) else "jobs"
        target = next((p for p in panes if p.key == key), None)
        if target is None:
            target = next((p for p in panes if p.key == "workspace:jobs:main"), None)
        if target is not None:
            return target
    if tab in ("analytics", "research"):
        target = next((p for p in panes if p.key.startswith(tab + ":") and "browser" not in p.key), None)
        if target is not None:
            return target
    pointer = getattr(app, "interaction_state", {}).get("pointer")
    if pointer:
        pane = _pane_at(state, *pointer)
        if pane is not None:
            return pane
    registered = [p for p in panes if not p.key.startswith("frame:")]
    return max(registered or panes, key=lambda p: p.page, default=None)


def handle_key(app, key):
    if getattr(app, "mode", "main") in EDIT_MODES:
        return False
    if getattr(app, "toolbar_state", {}).get("menu") is not None or getattr(app, "toolbar_state", {}).get("panel"):
        return False
    action = getattr(app, "keymap", {}).get(key, {"v": "visual", "V": "visual_all",
                                                "y": "yank", "Y": "copy_all"}.get(key, key))
    pending = initialize(app)
    if action == "yank":
        if copy_selection(app):
            return True
    if pending.get("invalidated") and (key == "esc" or action == "clear"):
        clear(app)
        app.say("Text selection cleared.")
        return True
    state = _current(app)
    if state is None:
        return False
    if action == "copy_all" and state["selection"]:
        if getattr(app, "mode", "main") in ("operations", "telemetry"):
            # These owners expose a complete report beyond the viewport.
            return False
        # Y is an explicit whole-pane operation, even during visual mode.
        return copy_visible_pane(app)
    if not state["selection"] and action not in ("visual", "visual_all"):
        return False
    pane = _focused(app, state)
    if pane is None:
        return False
    if action in ("visual", "visual_all"):
        # Exact original log bytes remain available through their established
        # visual mode; an unrelated rendered overlay can still be selected.
        if getattr(app, "tab", "") == "log" and getattr(app, "mode", "main") == "main" and not getattr(getattr(app, "logs", None), "browser", True):
            return False
        pointer = getattr(app, "interaction_state", {}).get("pointer")
        row = pointer[0] if pointer and pane.rect.contains(*pointer) else pane.rect.top
        click = getattr(app, "click_row", None)
        if isinstance(click, int) and pane.rect.top <= click < pane.rect.bottom:
            row = click
        elif not pointer and callable(getattr(app, "cursor_row", None)):
            candidate = app.cursor_row()
            if pane.rect.top <= candidate < pane.rect.bottom:
                row = candidate
        _start(app, state, pane, row, explicit=True)
        if action == "visual_all":
            state["selection"].update(anchor=pane.top, end=pane.top + pane.page - 1)
        app.say("Select rendered lines with arrows or drag; y copies; right-click or Esc clears.")
        return True
    if key == "esc" or action == "clear":
        clear(app)
        app.say("Text selection cleared.")
        return True
    aliases = {"pgup": "page_up", "pgdn": "page_down"}
    action = aliases.get(action, action)
    if action not in ("up", "down", "page_up", "page_down", "home", "end"):
        return False
    selection = state["selection"]
    end = selection["end"]
    limit = max(pane.count, pane.top + pane.page) - 1
    target = {"up": end - 1, "down": end + 1, "page_up": end - pane.page,
              "page_down": end + pane.page, "home": 0, "end": limit}[action]
    target = max(0, min(limit, target))
    if abs(target - selection["anchor"]) + 1 > MAX_LINES:
        app.say("Selection is limited to 20,000 rendered lines; narrow the range or export the source.")
        return True
    selection["end"] = target
    state["cursor"] = pane.key, target
    if callable(pane.setter) and not pane.top <= target < pane.top + pane.page:
        top = target if target < pane.top else target - pane.page + 1
        pane.setter(max(0, top) * pane.stride)
        from . import scrollbars
        registered = next((p for p in getattr(app, "scrollbar_state", {}).get("panes", ()) if p.key == pane.key), None)
        if registered is not None:
            scrollbars.set_manual(app, pane.key, max(0, top) * pane.stride, context=registered.context)
        state["frame_required"] = True
    _remember(state, pane)
    return True


def selection_text(app):
    state = initialize(app)
    selection = state["selection"]
    if not selection:
        return None
    if _current(app) is None:
        return None
    lo, hi = sorted((selection["anchor"], selection["end"]))
    if hi - lo + 1 > MAX_LINES:
        return None
    cache = state["cache"]
    if any(i not in cache for i in range(lo, hi + 1)):
        return None
    text = "\n".join(cache[i] for i in range(lo, hi + 1)) + "\n"
    return text if len(text.encode("utf-8")) <= MAX_BYTES else None


def copy_selection(app):
    """Return True when this module owns the copy, including guarded failures."""
    if initialize(app).get("invalidated"):
        app.fail("The selected text source changed. Select the lines again; no copy was sent.")
        return True
    if not selected(app):
        return False
    text = selection_text(app)
    if text is None:
        app.fail("Some selected rendered lines have not been displayed, or the source changed. Scroll through the range or export the source; no partial copy was sent.")
        return True
    if not text:
        app.fail("The selected rendered lines are empty.")
        return True
    message = clipboard.copy(text, getattr(app, "state_dir", None), **clipboard.options(app))
    app.say(message)
    store = getattr(app, "store", None)
    if store is not None:
        store.event("copy", message)
    clear(app)
    return True


def copy_visible_pane(app):
    """Copy all painted rows of the focused pane, independent of its range."""
    state = _current(app)
    if state is None or getattr(app, "mode", "main") in EDIT_MODES:
        return False
    pane = _focused(app, state)
    if pane is None:
        return False
    _start(app, state, pane, pane.rect.top, explicit=True)
    state["selection"].update(anchor=pane.top, end=pane.top + pane.page - 1)
    return copy_selection(app)


def feedback(app, *, ascii_=False):
    """Selection overlays require no source lookup or document rerender."""
    state = _current(app)
    if state is None or not state["selection"]:
        return []
    pane = _focused(app, state)
    if pane is None:
        return []
    lo, hi = sorted((state["selection"]["anchor"], state["selection"]["end"]))
    output = []
    for y in range(pane.rect.top, pane.rect.bottom):
        if not lo <= pane.top + y - pane.rect.top <= hi:
            continue
        text = state["cache"].get(pane.top + y - pane.rect.top)
        if text is None:
            continue
        width = pane.rect.right - pane.rect.left
        marker = ">" if ascii_ else "▏"
        if L.vlen(text) < width and pane.marker_column is None:
            row = [(L.pad(text, max(0, width - 1)), "text+bg:surface-raised+under"),
                   (marker, "warning+bold+bg:surface-raised")]
        else:
            row = [(L.truncate(text, width), "text+bg:surface-raised+under")]
        output.append((y, pane.rect.left, row))
        if isinstance(pane.marker_column, int) and pane.marker_column < getattr(app, "width", 0):
            output.append((y, pane.marker_column, [(marker, "warning+bold+bg:surface-raised")]))
    return output
