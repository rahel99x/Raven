"""Dedicated group-header drags prepare reviewed scheduler order requests.

Pointer feedback uses only the last published frame. A drop passes immutable
group evidence to the background operation; it never edits priorities itself.
"""
from __future__ import annotations

from . import layout as L

from .pointer_routing import capture_handler


def initialize(app):
    state = getattr(app, "group_queue_drag_state", None)
    if not isinstance(state, dict):
        state = {"frame": None, "capture": None, "discard_release": False}
        app.group_queue_drag_state = state
    return state


def command_names():
    return ["queue-drag"]


def run_command(app, args):
    if not args or args[0] != "queue-drag":
        return False
    app.say("Drag a group's order handle before or after another group; release opens a scheduler review")
    return True


def overlay(views, snap, app, width, height):
    return None


def active(app):
    return bool(initialize(app)["capture"])


def cancel(app):
    state = initialize(app)
    if state["capture"]:
        state["discard_release"] = True
    state["capture"] = None


def _current(app):
    from .job_group_drag import _current as source_frame
    frame = initialize(app)["frame"]
    source = source_frame(app)
    return frame if (frame and source and app.tab == "jobs"
                     and frame["signature"] == source["signature"]) else None


def publish(app, rows, hits, width, height, *, ascii_=False):
    """Add a handle in the spacer beside each painted Main group header."""
    from .job_group_drag import _current as source_frame
    from .pane_drag import _replace
    state = initialize(app)
    source = source_frame(app)
    headers, members = {}, {}
    if height is not None and app.tab == "jobs" and source:
        for y, spans in source["rows"].items():
            for span in spans:
                if span["scope"] == "jobs" and span["group_id"]:
                    members.setdefault(span["group_id"], []).append((y, span["rect"]))
        # Fold controls already identify first-visible expanded headers, even
        # when the group's first member has scrolled out of the viewport.
        for y, kind, value in tuple(hits):
            if kind != "control" or not isinstance(value, dict):
                continue
            identifier = value.get("id", "")
            if not identifier.startswith("jobgroup:jobs:"):
                continue
            gid = identifier[len("jobgroup:jobs:"):]
            evidence = source["evidence"].get(gid)
            span = next((item for item in source["rows"].get(y, ())
                         if item["scope"] == "jobs" and item["group_id"] == gid), None)
            left = value.get("left", -10) + 2
            if evidence is None or span is None or not 0 <= left < width or y >= len(rows):
                continue
            if left >= span["rect"][3] - 1:
                continue
            headers[gid] = {"y": y, "left": left, "rect": span["rect"], "evidence": evidence}
            rows[y] = _replace(rows[y], left, ":" if ascii_ else "↕", "accent", width)
            hits.append((y, "control", {"id": "queue-grip:" + gid,
                "label": "Reorder pending jobs in " + evidence.label + "; drag to another group for review",
                "left": left, "right": left + 1, "action": ("command", "queue-drag"),
                "group": "queue-order"}))
    state["frame"] = ({"headers": headers, "members": members, "source": source,
                       "signature": source["signature"]} if headers else None)
    capture = state["capture"]
    if capture:
        frame = _current(app)
        current = frame["headers"].get(capture["group"]) if frame else None
        if (current is None or current != capture["origin"]
                or capture["signature"] != frame["signature"]
                or capture["order"] != source["scopes"].get("jobs")
                or capture["geometry"] != _geometry(frame)):
            cancel(app)
        elif capture.get("target"):
            gid, expected, _ = capture["target"]
            if source["evidence"].get(gid) != expected:
                cancel(app)


def tick(app):
    if active(app) and _current(app) is None:
        cancel(app)


def handle_key(app, key):
    owned = active(app)
    cancel(app)
    if owned and key == "esc":
        app.say("Queue-order drag cancelled; scheduler order unchanged")
        return True
    return False


def _destination(frame, capture, y, x):
    if any(left <= x < right for left, right in frame["source"]["excluded"].get(y, ())):
        return None
    row = next((row for row in frame["source"]["rows"].get(y, ())
                if row["scope"] == "jobs" and row["rect"][1] <= x < row["rect"][3]), None)
    gid = row["group_id"] if row else None
    if not gid or gid == capture["group"] or gid not in frame["headers"]:
        return None
    header = frame["headers"][gid]
    placement = "after" if y > capture["origin"]["y"] else "before"
    return gid, header["evidence"], placement


def _geometry(frame):
    """Freeze visible row targets so a refresh cannot redirect a still pointer."""
    return tuple((y, row["group_id"], row["rect"])
                 for y, spans in frame["source"]["rows"].items()
                 for row in spans if row["scope"] == "jobs")


@capture_handler("group_queue_drag")
def handle_mouse(app, y, x, button="left", shift=False, alt=False):
    state = initialize(app)
    capture = state["capture"]
    if type(y) is not int or type(x) is not int:
        cancel(app)
        return bool(capture)
    if button in ("press", "left"):
        state["discard_release"] = False
    frame = _current(app)
    if capture:
        if frame is None or button == "right" or shift:
            cancel(app)
            return True
        if button in ("motion", "drag", "release"):
            capture["moved"] |= (y, x) != capture["start"]
            capture["point"] = y, x
            capture["target"] = _destination(frame, capture, y, x)
            if button == "release":
                state["capture"] = None
                target = capture["target"]
                if capture["moved"] and target:
                    from .queue_order_ui import prepare_drop
                    prepare_drop(app, capture["origin"]["evidence"], target[1], placement=target[2])
                else:
                    app.say("Drop the order handle on another Main group to review its pending-job order")
            return True
        cancel(app)
        return True
    if button == "release" and state["discard_release"]:
        state["discard_release"] = False
        return True
    if frame is None or shift or button not in ("press", "left"):
        return False
    header = next(((gid, value) for gid, value in frame["headers"].items()
                   if value["y"] == y and value["left"] == x), None)
    if header is None:
        return False
    if alt:
        app.say("Use the order handle without Alt; Alt-drag a job row to change its group membership")
        return True
    if button == "left":
        return run_command(app, ["queue-drag"])
    gid, origin = header
    state["capture"] = {"group": gid, "origin": origin, "signature": frame["signature"],
                        "order": frame["source"]["scopes"].get("jobs"),
                        "geometry": _geometry(frame),
                        "start": (y, x), "point": (y, x), "moved": False, "target": None}
    getattr(app, "job_selection_state", {})["capture"] = None
    return True


def feedback(app, rows):
    """Dim expanded members and move only the group label; never alter hits."""
    state = initialize(app)
    capture, frame = state["capture"], _current(app)
    if not capture or not capture["moved"] or not frame or not rows:
        return rows
    from .interaction import _decorate_row
    from .pane_drag import _replace
    from .log_text import display_text
    result = list(rows)
    for y, rect in frame["members"].get(capture["group"], ()):
        if 0 <= y < len(result) - 1:
            result[y] = _decorate_row(result[y], [(rect[1], rect[3], "dim")])
    origin = capture["origin"]
    label = display_text(origin["evidence"].label)
    target = capture["target"]
    y, _ = capture["point"]
    queue_row = any(row["scope"] == "jobs" for row in frame["source"]["rows"].get(y, ()))
    if queue_row and 0 <= y < len(result) - 1:
        left, right = origin["rect"][1], origin["rect"][3]
        ascii_ = getattr(getattr(app, "views_ref", None), "g", L.Glyphs(False)).ascii
        glyph = ":" if ascii_ else "↕"
        text = L.cut(f" {glyph} {label} ({len(origin['evidence'].members)} jobs)", right - left, ascii_)
        result[y] = _replace(result[y], left, text, "accent+bold+bg:surface-raised", app.width)
    if target:
        destination = display_text(target[1].label)
        message = f" Move {label} {target[2]} {destination} | Release to review; Esc cancels"
    else:
        message = " Reorder group | Drop on another Main group; Esc cancels | Scheduler unchanged until confirmed"
    result[-1] = [(L.pad(L.cut(message, app.width), app.width), "accent+bold+bg:surface")]
    return result
