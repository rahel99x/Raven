"""Attempt-safe job moves over the last painted list, without pointer I/O.

Plain row gestures always select, including rows that are already marked.
Alt-press explicitly starts a membership move; only a deliberate release over
a published group commits it. Group-header grips have their own queue owner.
"""

from __future__ import annotations
from raven.table_query import fingerprint
from raven.ui.selection_state import cleared
from raven.ui.control_graph import _decorate_row

from collections.abc import Mapping
from itertools import islice

from raven.pointer_routing import capture_handler

MAX_ROWS = 4096


def initialize(app):
    state = getattr(app, "job_group_drag_state", None)
    if not isinstance(state, dict):
        state = {
            "frame": None,
            "capture": None,
            "pending": None,
            "armed": None,
            "discard_release": False,
        }
        app.job_group_drag_state = state
    return state


def command_names():
    return []


def run_command(app, args):
    return False


def overlay(views, snap, app, width, height):
    return None


class _Tokens(Mapping):
    """Do not copy a 50,000-row table to hit-test one pointer report."""

    def __init__(self, app, ids):
        self.app, self.ids = app, ids

    def __getitem__(self, key):
        state = getattr(self.app, "job_selection_state", {})
        published = state.get("published_tokens", {})
        if key in getattr(self.app, "marks", ()):
            return state.get("mark_tokens", {}).get(key, published.get(key))
        return published.get(key)

    def __iter__(self):
        return iter(self.ids)

    def __len__(self):
        return len(self.ids)


def _signature(app):
    """Bounded preferences invalidate geometry before a replacement frame."""

    tab = getattr(app, "tab", "")
    tables = (tab, "recent") if tab == "jobs" else (tab,)
    table = getattr(app, "table_state", {})
    top, sort, reverse = (getattr(app, name, {}) for name in ("top", "sort", "reverse"))
    layout = getattr(app, "layout_state", None)
    browser = getattr(app, "history_browser_state", {}).get("views", {}).get(tab, {})
    recent = getattr(app, "recent_history_state", None)
    return (
        getattr(app, "mode", "main"),
        tab,
        getattr(app, "width", None),
        getattr(app, "height", None),
        tuple(
            (
                name,
                top.get(name),
                sort.get(name),
                reverse.get(name),
                fingerprint(app, name),
            )
            for name in tables
        ),
        table.get("groups"),
        tuple(islice(table.get("collapsed", ()), 256)),
        getattr(app, "manual_job_groups_revision", 0),
        tuple(
            getattr(layout, name, None) for name in ("density", "ratio", "maximized")
        ),
        tuple(browser.get(name) for name in ("dock", "enabled", "ratio", "top")),
        getattr(recent, "ratio", None),
        getattr(recent, "manual_split", None),
        getattr(app, "analytics_view", None),
        getattr(app, "research_view", None),
        getattr(app, "deps_scope_top", None),
        getattr(app, "analytics_scroll_offsets", {}).get("advisor", {}).get("top"),
    )


def _blocked(app):
    toolbar = getattr(app, "toolbar_state", {})
    return (
        getattr(app, "mode", "main") != "main"
        or toolbar.get("menu") is not None
        or toolbar.get("panel")
        or getattr(app, "text_selection_state", {}).get("explicit")
        or getattr(app, "sel_anchor", None) is not None
        or getattr(app, "text_selection_state", {}).get("capture")
        or getattr(app, "chart_interaction_state", {}).get("capture")
        or getattr(app, "metric_live_state", {}).get("capture")
    )


def _rect(value):
    if value is None:
        return None
    if hasattr(value, "top"):
        return value.top, value.left, value.bottom, value.right
    return value.y, value.x, value.y + value.height, value.x + value.width


def _intersection(left, right):
    if right is None:
        return left
    return (
        max(left[0], right[0]),
        max(left[1], right[1]),
        min(left[2], right[2]),
        min(left[3], right[3]),
    )


def publish(app, snap, rows, hits, width, height):
    """Publish only visible row spans and their immutable destination evidence."""
    from raven.jobs import selection as job_selection
    from raven.jobs import group_domain as job_groups
    from raven.jobs import manual_groups as manual_job_groups

    state = initialize(app)
    if (
        height is None
        or getattr(app, "tab", "") not in job_selection.JOB_SCOPES
        or _blocked(app)
    ):
        state["frame"] = None
        cancel(app)
        return
    tab = app.tab
    scopes, by_y, exclusions, disclosures, headers, buffers = {}, {}, {}, {}, {}, {}
    native_ids = job_selection._order(app)
    if native_ids:
        scope = "analytics:advisor" if tab == "analytics" else tab
        scopes[scope] = native_ids
        if tab == "jobs":
            scopes["recent"] = native_ids
    browser = getattr(app, "history_browser_state", {})
    browser_frame = browser.get("frame") or {}
    if browser_frame.get("tab") == tab and browser_frame.get("dock") != "off":
        scopes["history:" + tab] = tuple(browser.get("selection_ids", ()))
    visible_groups, row_count = set(), 0
    main = _rect(getattr(app, "workspace_main_rect", None))
    content = _rect(getattr(app, "history_browser_content_rect", None))
    native_kinds = job_selection.ROW_KINDS.get(tab, ())
    for y, kind, value in islice(hits, MAX_ROWS * 16):
        if not isinstance(y, int) or not 0 <= y < min(height - 1, len(rows)):
            continue
        scope, identifier, rectangle = None, None, None
        if kind == "control" and isinstance(value, dict):
            left, right = value.get("left"), value.get("right")
            if type(left) is not int or type(right) is not int:
                continue
            key = str(value.get("id", ""))
            if key.startswith("history:" + tab + ":job:"):
                scope, identifier = "history:" + tab, key.split(":job:", 1)[1]
            elif tab == "analytics" and key.startswith("advisor-job:"):
                scope, identifier = "analytics:advisor", key[len("advisor-job:") :]
            else:
                target = (
                    disclosures
                    if (
                        value.get("group") == "job-groups"
                        or key.startswith("jobgroup:")
                        or key.startswith("history:" + tab + ":group:")
                    )
                    else exclusions
                )
                target.setdefault(y, []).append((max(0, left), min(width, right)))
                continue
            rectangle = (y, max(0, left), y + 1, min(width, right))
        elif kind in native_kinds and kind != "advisor_job" and isinstance(value, str):
            scope, identifier = ("recent" if kind == "recent" else tab), value
            rectangle = (y, 0, y + 1, width)
            if tab in ("jobs", "history", "group", "deps"):
                rectangle = _intersection(rectangle, main)
            rectangle = _intersection(rectangle, content)
        if scope not in scopes or rectangle is None:
            continue
        if rectangle[0] >= rectangle[2] or rectangle[1] >= rectangle[3]:
            continue
        meta = job_groups.metadata_for_record(app, scope, identifier)
        group_id = meta.group.id if meta is not None else None
        if group_id:
            visible_groups.add(group_id)
            # Share one bounded list per projected group. This includes its
            # offscreen/collapsed members, but never jobs removed by the pane's
            # filter or jobs that belong only to another pane (e.g. Recents).
            key = scope, group_id
            if key not in headers:
                headers[key] = {
                    "group": meta.group,
                    "members": tuple(
                        job_groups._value(record, "id")
                        for record in islice(
                            meta.records, manual_job_groups.MAX_IDENTITIES + 1
                        )
                    ),
                }
        by_y.setdefault(y, []).append(
            {
                "scope": scope,
                "pointed": identifier,
                "rect": rectangle,
                "group_id": group_id,
                "header": bool(meta and meta.header),
                "collapsed": bool(meta and meta.collapsed),
            }
        )
        row_count += 1
        if row_count >= MAX_ROWS:
            break
    # Scroll rails and dividers own their complete hit area, including buffers.
    for pane in getattr(app, "scrollbar_state", {}).get("staged", ()):
        rectangle = _rect(pane.rect)
        for y in range(max(0, rectangle[0]), min(height - 1, rectangle[2])):
            if y in by_y:
                exclusions.setdefault(y, []).append((rectangle[3] - 1, rectangle[3]))
    for divider in getattr(app, "pane_drag_state", {}).get("dividers", {}).values():
        from raven.pane_drag import BUFFER

        top, bottom = divider.y, divider.y + divider.height
        left, right = divider.x, divider.x + divider.width
        for y in range(max(0, top), min(height - 1, bottom)):
            if y in by_y:
                exclusions.setdefault(y, []).append((left, right))
        if divider.axis == "vertical":
            left, right = left - BUFFER, right + BUFFER
        else:
            top, bottom = top - BUFFER, bottom + BUFFER
        for y in range(max(0, top), min(height - 1, bottom)):
            if y in by_y:
                buffers.setdefault(y, []).append((left, right))
    index = getattr(getattr(app, "job_groups", None), "index", None)
    evidence = (
        manual_job_groups.target_evidences(
            app, snap, visible_groups, index=index, all_groups=True
        )
        if visible_groups and index is not None
        else {}
    )
    # Existing destination evidence already freezes hidden members. A group
    # with an excluded ambiguous record may lack destination evidence yet
    # still have a usable source subset. Cache missing tokens once per frame;
    # neither right-click hit tests nor hover query scheduler state.
    missing = {
        jid
        for (_, gid), header in headers.items()
        if gid not in evidence
        for jid in header["members"]
    }
    if missing:
        cache = state.get("header_token_cache")
        if cache is None:
            cache = state["header_token_cache"] = (
                manual_job_groups.SelectionTokenCache()
            )
        header_tokens = cache.update(
            snap, missing, registry=getattr(app, "job_groups", None)
        )
    else:
        header_tokens = {}
    frame = {
        "rows": by_y,
        "scopes": scopes,
        "excluded": exclusions,
        "buffers": buffers,
        "disclosures": disclosures,
        "headers": headers,
        "header_tokens": header_tokens,
        "evidence": evidence,
        "signature": _signature(app),
    }
    state["frame"] = frame
    capture = state["capture"]
    if capture:
        context = capture["source"]
        tokens = getattr(app, "job_selection_state", {}).get("published_tokens", {})
        target = capture.get("target")
        if (
            capture["signature"] != frame["signature"]
            or context["ids"] != scopes.get(context["scope"])
            or any(
                tokens.get(jid) != context["tokens"].get(jid)
                for jid in context["targets"]
            )
            or target is not None
            and evidence.get(target["group_id"]) != target["expected"]
        ):
            cancel(app)
        elif target is not None:
            current = source_at(app, *capture["point"], include_disclosure=True)
            if current is None or any(
                current[key] != target[key]
                for key in ("scope", "pointed", "rect", "group_id")
            ):
                cancel(app)


def _current(app):
    frame = initialize(app)["frame"]
    return (
        frame
        if frame and not _blocked(app) and frame["signature"] == _signature(app)
        else None
    )


def source_at(app, y, x, *, include_disclosure=False, include_buffer=False):
    """Return the exact pointed source regardless of old keyboard pane focus."""
    if any(type(value) is not int for value in (y, x)):
        return None
    frame = _current(app)
    if frame is None or any(
        left <= x < right for left, right in frame["excluded"].get(y, ())
    ):
        return None
    if not include_buffer and any(
        left <= x < right for left, right in frame["buffers"].get(y, ())
    ):
        return None
    if not include_disclosure and any(
        left <= x < right for left, right in frame["disclosures"].get(y, ())
    ):
        return None
    row = next(
        (
            item
            for item in frame["rows"].get(y, ())
            if item["rect"][1] <= x < item["rect"][3]
        ),
        None,
    )
    if row is None:
        return None

    ids, scope = frame["scopes"][row["scope"]], row["scope"]
    chosen = None
    if not cleared(app):
        if scope.startswith("history:"):
            browser = getattr(app, "history_browser_state", {})
            index = min(max(0, browser.get("index", 0)), len(ids) - 1)
            chosen = ids[index] if ids else None
        elif scope == "analytics:advisor":
            chosen = getattr(app, "analytics_job", None)
        elif ids:
            index = min(
                max(0, getattr(app, "cursor", {}).get(app.tab, 0)), len(ids) - 1
            )
            chosen = ids[index]
    return dict(row, ids=ids, selected=chosen, tokens=_Tokens(app, ids))


def menu_source_at(app, y, x):
    """Resolve a group header explicitly, without changing marks or folds.

    Expanded tables share their first member's data row with the disclosure.
    Marked data cells retain member selection semantics; the disclosure always
    targets its projected group. Collapsed rows always represent the group.
    """
    # A right-click does not resize. Visible data/header cells beside a
    # divider remain usable; the divider itself and other controls stay inert.
    source = source_at(app, y, x, include_disclosure=True, include_buffer=True)
    if source is None or not source.get("group_id"):
        return source
    frame = _current(app)
    disclosure = any(
        left <= x < right for left, right in frame["disclosures"].get(y, ())
    )
    if not (source["header"] or disclosure):
        return source
    if (
        not source["collapsed"]
        and not disclosure
        and source["pointed"] in getattr(app, "marks", ())
    ):
        return source
    header = frame["headers"].get((source["scope"], source["group_id"]))
    if header is None:
        return source
    evidence = frame["evidence"].get(source["group_id"])
    tokens = dict(evidence.tokens) if evidence is not None else frame["header_tokens"]
    return dict(
        source,
        ids=header["members"],
        pane_ids=source["ids"],
        selected=source["pointed"],
        tokens=tokens,
        source_group=header["group"],
    )


def active(app):
    return initialize(app)["capture"] is not None


def pending(app):
    state = initialize(app)
    return state["pending"] is not None or state["discard_release"]


def cancel(app):
    state = initialize(app)
    if state["capture"]:
        state["discard_release"] = True
    state.update(capture=None, pending=None, armed=None)


def tick(app):
    state = initialize(app)
    if state["capture"] and _current(app) is None:
        cancel(app)
    elif (state["pending"] or state["armed"]) and _blocked(app):
        cancel(app)


def handle_key(app, key):
    owned = active(app)
    cancel(app)
    if owned and key == "esc":
        app.say("Job move cancelled; marks kept")
        return True
    return False


def _target(app, source):
    if source is None or not source.get("group_id"):
        return None
    evidence = initialize(app)["frame"]["evidence"].get(source["group_id"])
    if evidence is None:
        return None
    return dict(source, expected=evidence)


@capture_handler("job_group_drag", cancel=cancel)
def handle_mouse(app, y, x, button="left", shift=False, alt=False):
    state = initialize(app)
    capture = state["capture"]
    if any(type(value) is not int for value in (y, x)):
        cancel(app)
        return bool(capture)
    if button in ("press", "left"):
        state["discard_release"] = False
    if capture:
        if button == "right" or shift or _current(app) is None:
            cancel(app)
            return True
        if button in ("motion", "drag", "release"):
            if (y, x) != capture["point"]:
                capture["moved"] = True
                capture["point"] = (y, x)
                capture["target"] = _target(
                    app, source_at(app, y, x, include_disclosure=True)
                )
            if button == "release":
                state.update(
                    capture=None, pending=None, armed=None, discard_release=False
                )
                target = capture.get("target")
                if capture["moved"] and target is not None:
                    from raven.jobs.group_actions import execute

                    execute(
                        app,
                        capture["source"],
                        "add",
                        group_id=target["group_id"],
                        expected=target["expected"],
                    )
                elif capture["moved"]:
                    app.say(
                        "Job move cancelled; release over an existing group to add jobs"
                    )
                else:
                    # A marked row still behaves like an ordinary click when
                    # no move occurred. Source activation is deliberately
                    # deferred until release; moving across Logs never opens
                    # files or looks up scheduler records.
                    from raven.jobs.selection import handle_mouse as select_mouse

                    source = source_at(app, y, x)
                    if source is not None:
                        select_mouse(app, y, x, "press")
                        select_mouse(app, y, x, "release")
            return True
        cancel(app)
        # A wheel or new press during a move ends it without activating the
        # controls under a pointer that still belongs to the old gesture.
        return True
    if button == "release" and state["discard_release"]:
        state["discard_release"] = False
        return True
    if button == "right":
        state.update(pending=None, armed=None)
        return False
    if button != "press" or not alt or shift:
        state.update(pending=None, armed=None)
        return False
    source = source_at(app, y, x)
    if source is None:
        state.update(pending=None, armed=None)
        return False
    jid, _scope = source["pointed"], source["scope"]
    token = source["tokens"].get(jid)
    marked = jid in getattr(app, "marks", ())
    if token is not None:
        from raven.jobs.group_actions import freeze

        targets = (
            tuple(identifier for identifier in source["ids"] if identifier in app.marks)
            if marked
            else (jid,)
        )
        frozen = freeze(app, source, job_ids=targets)
        if frozen is None:
            return False
        state.update(
            capture={
                "source": frozen,
                "signature": state["frame"]["signature"],
                "point": (y, x),
                "moved": False,
                "target": None,
            },
            pending=None,
            armed=None,
        )
        getattr(app, "job_selection_state", {})["capture"] = None
        return True
    return False


def feedback(app, rows):
    """Paint only the destination span and footer; retain pristine copy text."""
    capture = initialize(app)["capture"]
    if not capture or not capture["moved"] or not rows:
        return rows
    from raven import layout as L
    from ..job_logs.text import display_text

    result = list(rows)
    target = capture.get("target")
    count = len(capture["source"]["targets"])
    if target:
        y, left, _, right = target["rect"]
        if 0 <= y < len(result) - 1:
            result[y] = _decorate_row(
                result[y], [(left, right, "accent+bold+bg:track")]
            )
        label = display_text(getattr(target["expected"], "label", "group"))
        message = f" {count} job{'s' if count != 1 else ''} -> {label} | Release to add; Esc cancels"
    else:
        message = f" Move {count} job{'s' if count != 1 else ''} | Drop on an existing group; Esc cancels"
    width = max(0, getattr(app, "width", 120))
    result[-1] = [(L.pad(L.cut(message, width), width), "accent+bold+bg:surface")]
    return result
