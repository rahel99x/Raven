"""Captured, terminal-cell column resizing over published table geometry.

Only a header's separator cells start a resize; the heading itself keeps its
sort action. Preview widths live in memory, and a release persists once. The
capture owns motion and release even when the pointer crosses another pane.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import islice

from .table_sort import valid_header

from .pointer_routing import capture_handler

MAX_HEADERS = 160
RECENT_ALIASES = {"id": "id", "name": "name", "part": "part", "st": "state",
                  "where": "nodes", "cpus": "cpus", "gpu": "gpus", "time": "elapsed",
                  "eff": "ce", "mem%": "me", "flags": "exit", "tags": "tags", "info": "info"}


@dataclass(frozen=True)
class Grip:
    table: str
    column: str
    y: int
    left: int
    right: int
    width: int
    maximum: int
    start: int

    @property
    def key(self):
        return self.table, self.column, self.y


def initialize(app):
    state = getattr(app, "column_resize_state", None)
    if not isinstance(state, dict):
        state = {"grips": (), "capture": None, "discard_release": False, "context": None}
        app.column_resize_state = state
    return state


def _context(app):
    rect = getattr(app, "workspace_main_rect", None)
    return (getattr(app, "tab", None), getattr(app, "mode", "main"),
            getattr(app, "width", None), getattr(app, "height", None),
            None if rect is None else (rect.x, rect.y, rect.width, rect.height))


def _blocked(app):
    toolbar = getattr(app, "toolbar_state", {}) or {}
    return (getattr(app, "mode", "main") != "main" or toolbar.get("menu") is not None or
            bool(toolbar.get("panel")) or bool(getattr(app, "job_group_menu_state", {}).get("open")))


def _canonical(app, table, column):
    if table == "recent" and getattr(app, "tab", None) == "jobs":
        inverse = {value: key for key, value in RECENT_ALIASES.items()}
        if column in inverse:
            return "jobs", inverse[column]
    return table, column


def _widths(app, table):
    return app.table_state.setdefault("widths", {}).setdefault(table, {})


def _changed(app):
    state = getattr(app, "interaction_state", None)
    if isinstance(state, dict):
        state["frame_required"] = True


def _restore(app, capture):
    widths = _widths(app, capture["table"])
    if capture["original"] is None:
        widths.pop(capture["column"], None)
    else:
        widths[capture["column"]] = capture["original"]


def cancel(app):
    state = initialize(app)
    capture = state["capture"]
    if capture:
        _restore(app, capture)
        state.update(capture=None, discard_release=True)
        _changed(app)


def active(app):
    return bool(initialize(app)["capture"])


def committed_widths(app, copied_widths):
    """An unrelated settings save must not persist an unfinished preview."""
    capture = initialize(app)["capture"]
    if capture:
        widths = copied_widths.setdefault(capture["table"], {})
        if capture["original"] is None:
            widths.pop(capture["column"], None)
        else:
            widths[capture["column"]] = capture["original"]
    return copied_widths


def publish(app, rows, hits, width, height, *, ascii_=False):
    """Publish final coordinates after all pane transforms, without data I/O."""
    state = initialize(app)
    context = _context(app)
    if state["capture"] and (state["capture"]["context"] != context or _blocked(app)):
        cancel(app)
    state["context"] = context
    grouped, count = {}, 0
    if height is not None and not _blocked(app):
        for hit in islice(hits or (), MAX_HEADERS * 32):
            if not isinstance(hit, (tuple, list)) or len(hit) != 3:
                continue
            y, kind, value = hit
            if (kind == "sort_header" and type(y) is int and 0 <= y < min(len(rows), height - 1)
                    and valid_header(value) and value[3] <= width):
                grouped.setdefault((value[0], y), []).append(value)
                count += 1
                if count >= MAX_HEADERS:
                    break
    grips = []
    rect = getattr(app, "workspace_main_rect", None)
    for (table, y), headers in grouped.items():
        headers.sort(key=lambda value: value[2])
        edge = width - 1
        if (rect is not None and rect.y <= y < rect.y + rect.height and
                rect.x <= headers[0][2] < rect.x + rect.width):
            edge = min(edge, rect.x + rect.width - 1)
        for index, (_, column, left, right) in enumerate(headers):
            canonical_table, canonical_column = _canonical(app, table, column)
            if canonical_table == "jobs" and canonical_column == "progress":
                continue
            # Keep the complete heading clickable. The two-cell gap is the
            # grip plus its buffer; never consume a neighbouring heading.
            stop = headers[index + 1][2] if index + 1 < len(headers) else edge
            stop = min(stop, edge, right + 2)
            grip_start = right
            if right >= stop:
                # A table fitted exactly to its pane has no final gap. Keep
                # a one-cell end cap before the scrollbar; the heading's
                # other cells retain sorting, including keyboard activation.
                if index + 1 != len(headers) or right != edge or right - left < 3:
                    continue
                grip_start = right - 1
            remainder = sum(4 for item in headers[index + 1:]
                            if item[1] in ("id", "name", "st", "state"))
            maximum = max(2, min(120, edge - left - remainder))
            grips.append(Grip(canonical_table, canonical_column, y, grip_start, stop,
                              right - left, maximum, left))
    state["grips"] = tuple(grips)
    capture = state["capture"]
    if capture and not any(grip.key == capture["key"] and grip.start == capture["start"] for grip in grips):
        cancel(app)
    from .pane_drag import _replace
    for grip in grips:
        style = "accent+bold" if capture and grip.key == capture["key"] else "border"
        rows[grip.y] = _replace(rows[grip.y], grip.left, "|" if ascii_ else "│", style, width)


def descriptors(app):
    """Share bounded hover/focus targets with the existing navigation graph."""
    return [{"id": "column-resize:" + grip.table + ":" + grip.column + ":" + str(grip.y),
             "label": "Resize " + grip.column + "; drag separator, right-click resets width",
             "rect": (grip.y, grip.left, grip.y + 1, grip.right),
             "action": ("click", grip.y, grip.left), "group": "column-resize"}
            for grip in initialize(app)["grips"]]


def handle_key(app, key):
    if not active(app):
        return False
    cancel(app)
    return key == "esc"


def _save(app):
    callback = getattr(app, "save", None)
    if callable(callback):
        callback()


@capture_handler("column_resize")
def handle_mouse(app, y, x, button="left", shift=False):
    if type(y) is not int or type(x) is not int:
        return False
    state = initialize(app)
    if button in ("press", "left"):
        state["discard_release"] = False
    capture = state["capture"]
    if capture:
        if _blocked(app) or capture["context"] != _context(app):
            cancel(app)
            return button in ("motion", "drag", "release")
        if button == "right":
            cancel(app)
            return True
        if button in ("motion", "drag", "release"):
            delta = x - capture["pointer"]
            value = (capture["width"] if not delta else
                     max(2, min(capture["maximum"], capture["width"] + delta)))
            if value != capture["current"]:
                _widths(app, capture["table"])[capture["column"]] = value
                capture["current"] = value
                _changed(app)
            if button == "release":
                state["capture"] = None
                if capture["current"] != capture["width"]:
                    _save(app)
                else:
                    _restore(app, capture)
            return True
        if button not in ("press", "left"):
            # Wheels cannot change a table behind an unfinished resize.
            return True
        # A fresh press supersedes a lost terminal release. Restore the old
        # preview before giving the new, independently targeted gesture back.
        cancel(app)
        state["discard_release"] = False
    if button == "release" and state["discard_release"]:
        state["discard_release"] = False
        return True
    if (_blocked(app) or state["context"] != _context(app) or shift or
            button not in ("press", "left", "right")):
        return False
    grip = next((item for item in state["grips"] if item.y == y and item.left <= x < item.right), None)
    if grip is None:
        return False
    widths = _widths(app, grip.table)
    if button == "right":
        if grip.column in widths:
            widths.pop(grip.column)
            _changed(app)
            _save(app)
        return True
    if button == "left":
        callback = getattr(app, "say", None)
        if callable(callback):
            callback("Drag the header separator to resize; right-click resets its width")
        return True
    state["capture"] = {"key": grip.key, "context": _context(app), "table": grip.table,
                        "column": grip.column, "pointer": x, "width": grip.width,
                        "current": grip.width, "original": widths.get(grip.column),
                        "maximum": grip.maximum, "start": grip.start}
    return True
