"""Selection-bound job actions over immutable, painted row identities.

Opening and activating this modal are explicit operations. Hover, wheel input,
painting and keyboard navigation use its frozen, bounded action list only.
"""

from __future__ import annotations
from raven.table_sort import _natural
from raven.ui.control_graph import Control, Rect

from dataclasses import asdict, replace
import re
import time

from raven import layout as L
from raven.research_text import clean

MODE = "job_group_menu"
PANE = "modal:job-groups"
RECENT_GROUPS = 3
AGGREGATE_GUARD_SECONDS = 0.5


def initialize(app):
    state = getattr(app, "job_group_menu_state", None)
    if not isinstance(state, dict):
        state = app.job_group_menu_state = dict(
            token=0,
            context=None,
            items=(),
            prefix=(),
            destinations=(),
            suffix=(),
            expanded=False,
            cursor=0,
            top=0,
            page=1,
            hits=(),
            hover=None,
            pressed=None,
            paint_token=None,
            aggregate_guard=None,
        )
    return state


def active(app):
    return getattr(app, "mode", "main") == MODE


def command_names():
    return ["jobgroupmenu"]


def _feedback(app, message):
    callback = getattr(app, "say", None)
    if callable(callback):
        callback(message)


def close(app):
    state = initialize(app)
    # Release evidence for old snapshots as soon as the modal closes. A menu
    # containing many historical groups must not retain their record objects.
    state.update(
        context=None,
        items=(),
        prefix=(),
        destinations=(),
        suffix=(),
        expanded=False,
        hits=(),
        paint_token=None,
        hover=None,
        pressed=None,
        aggregate_guard=None,
    )
    if active(app):
        app.mode = "main"
    pointer = getattr(app, "interaction_state", {})
    pointer.update(active=False, focused=None, pressed=None, frame_required=True)


def _recent_key(group, evidence):
    """Rank batches by observed submission, never by arrival order or label.

    Frozen evidence already contains checked attempt times. A natural job ID
    breaks ties and provides a deterministic fallback for session-only records.
    Do this only when the user opens the menu, never on paint or pointer motion.
    """

    latest = max((token[1] for _, token in evidence.tokens if token[1]), default="")
    return latest, max((_natural(jid) for jid in group.members), default=()), group.id


def _menu_items(prefix, destinations, suffix, *, expanded=False):
    if expanded or len(destinations) <= RECENT_GROUPS:
        return (*prefix, *destinations, *suffix)
    more = dict(
        label=f"More ({len(destinations) - RECENT_GROUPS} more groups)", action="more"
    )
    return (*prefix, *destinations[:RECENT_GROUPS], more, *suffix)


def _scheduler_items(app, snap, targets):
    """Freeze the applicable subset at menu-open time, without scheduler I/O.

    A group may contain both finished and active members. Counts make that
    distinction explicit; choosing an item never silently acquires new jobs.
    """
    actions = getattr(app, "actions", None)
    if actions is None or getattr(app, "replay", None):
        return ()
    from raven.historical_ui import active as historical_active

    if historical_active(app):
        return ()
    records = {job.id: job for job in snap.get("jobs", ())}
    result = []
    for verb, title in (
        ("hold", "Hold pending"),
        ("release", "Release held"),
        ("cancel", "Cancel active"),
    ):
        ids = tuple(
            jid
            for jid in targets
            if jid in records and actions.applicable(verb, records[jid])[0]
        )
        if not ids:
            continue
        count = (
            str(len(ids))
            if len(ids) == len(targets)
            else f"{len(ids)} of {len(targets)}"
        )
        result.append(
            dict(
                label=f"{title} jobs ({count})…",
                action="scheduler:" + verb,
                targets=ids,
            )
        )
    return tuple(result)


def _review_scheduler(app, frozen, item, pointer_guard=None):
    """Prepare the existing one-shot review for the exact frozen active jobs."""
    from raven.jobs import group_actions as A

    snap = A.validate(app, frozen)
    if snap is None:
        return
    if frozen.get("action_scope") != app._action_scope():
        _feedback(app, "Connection changed; select the current jobs and review again")
        return
    verb = item["action"].split(":", 1)[1]
    jobs = {job.id: job for job in snap.get("jobs", ())}
    ids = item["targets"]
    if any(
        jid not in jobs or not app.actions.applicable(verb, jobs[jid])[0] for jid in ids
    ):
        _feedback(
            app,
            "Selected jobs changed; review the current jobs before applying " + verb,
        )
        return
    # Store publication objects may be replaced or updated while the review is
    # open. Preserve the reviewed attempt fields for finish_confirm's recheck.
    review = dict(
        action=verb,
        jobs=[replace(jobs[jid]) for jid in ids],
        action_scope=frozen["action_scope"],
    )
    if len(ids) != len(frozen["targets"]):
        excluded = len(frozen["targets"]) - len(ids)
        review["review"] = [
            f"Only the {len(ids)} applicable active job{'s' if len(ids) != 1 else ''} "
            f"listed above {'are' if len(ids) != 1 else 'is'} targeted; "
            f"{excluded} other selected job{'s' if excluded != 1 else ''} "
            f"{'are' if excluded != 1 else 'is'} excluded."
        ]
    if pointer_guard is not None:
        review["pointer_guard"] = pointer_guard
    app.confirm, app.mode = review, "confirm"


def _expand(app):
    state = initialize(app)
    state.update(
        token=state["token"] + 1,
        expanded=True,
        items=_menu_items(
            state["prefix"], state["destinations"], state["suffix"], expanded=True
        ),
        hits=(),
        paint_token=None,
        hover=None,
        pressed=None,
        aggregate_guard=None,
    )
    # Existing coordinates and F8 actions must not acquire the new meaning of
    # the former More row. Keep the cursor there to reveal the first older batch.
    getattr(app, "interaction_state", {}).update(
        active=False, focused=None, pressed=None, frame_required=True
    )


def open_menu(app, context=None):
    """Open for a projected group header, marked row, or pointed single row.

    An unmarked pointed row is an exact single-job context even when another
    list retains marks. Unrelated marks must never broaden its actions.
    """
    if getattr(app, "mode", "main") != "main" or not isinstance(context, dict):
        return False
    pointed = context.get("pointed")
    marks = getattr(app, "marks", ())
    source_group = context.get("source_group")
    if source_group is not None:
        targets = context.get("ids", ())
    elif pointed in marks:
        targets = tuple(jid for jid in context.get("ids", ()) if jid in marks)
    elif pointed and pointed in context.get("ids", ()):
        targets = (pointed,)
    else:
        return False
    if not targets:
        return False
    from raven.jobs import group_actions as A
    from raven.jobs import group_domain as G
    from raven.jobs import manual_groups as M

    if len(targets) > M.MAX_IDENTITIES:
        _feedback(app, f"Select at most {M.MAX_IDENTITIES} jobs for a group action")
        return True
    frozen = A.freeze(app, context, targets)
    snap = A.validate(app, frozen)
    if snap is None:
        return True
    registry = G.registry(app)
    index = registry.ensure(snap)
    selected = frozenset(targets)
    destinations = [
        group for group in index.groups.values() if not selected.issubset(group.members)
    ]
    collapsed = {}
    grouped = False
    for jid in targets:
        group = index.for_job(jid)
        if group is not None:
            grouped = True
            from raven.jobs.list_controls import painted_group

            visible_fold = painted_group(app, context["scope"], jid, group)
            if source_group is None and visible_fold:
                collapsed[group.id] = group
    required = tuple(dict.fromkeys((*collapsed, *(group.id for group in destinations))))
    evidence = M.target_evidences(app, snap, required, index=index, all_groups=True)
    frozen["ungroup_groups"] = {gid: evidence.get(gid) for gid in collapsed}
    prefix, destination_items, suffix = [], [], []
    scheduler_items = _scheduler_items(app, snap, targets)
    if scheduler_items:
        frozen["action_scope"] = app._action_scope()
        prefix.extend(scheduler_items)
    if len(targets) >= 2:
        prefix.append(dict(label="Create Group", action="create"))
    if grouped:
        prefix.append(dict(label="Ungroup", action="ungroup"))
    names = {}
    for group in destinations:
        names[group.label] = names.get(group.label, 0) + 1
    eligible = [group for group in destinations if evidence.get(group.id) is not None]
    for group in sorted(
        eligible, key=lambda value: _recent_key(value, evidence[value.id]), reverse=True
    ):
        expected = evidence.get(group.id)
        if expected is None:
            continue
        label = clean(group.label, limit=512)
        if names[group.label] > 1:
            members = group.members
            label += (
                " ("
                + members[0]
                + (".." + members[-1] if len(members) > 1 else "")
                + ")"
            )
        destination_items.append(
            dict(
                label="Add to " + label,
                action="add",
                group_id=group.id,
                expected=expected,
            )
        )
    if getattr(app, "tab", "") == "history":
        suffix.append(dict(label="Export logs", action="export"))
    # A folded header stands for a batch, not the individual ID used to paint
    # it. Exact reruns require an exposed single member and a cancelled record.
    if (
        len(targets) == 1
        and source_group is None
        and not collapsed
        and not getattr(app, "replay", None)
    ):
        job_id = targets[0]
        records = [record for record in snap.get("finished", ()) if record.id == job_id]
        if (
            len(records) == 1
            and re.fullmatch(r"[0-9]{1,20}(?:_[0-9]{1,10})?", job_id)
            and re.fullmatch(r"CANCELLED(?:\+| by [0-9]{1,20})?", records[0].state)
            and not any(record.id == job_id for record in snap.get("jobs", ()))
        ):
            frozen["rerun_source"] = asdict(records[0])
            suffix.append(dict(label="Rerun cancelled job", action="rerun"))
    suffix.append(dict(label="Close menu", action="cancel"))
    prefix, destination_items, suffix = (
        tuple(prefix),
        tuple(destination_items),
        tuple(suffix),
    )
    state = initialize(app)
    state.update(
        token=state["token"] + 1,
        context=frozen,
        prefix=prefix,
        destinations=destination_items,
        suffix=suffix,
        expanded=False,
        items=_menu_items(prefix, destination_items, suffix),
        tab=getattr(app, "tab", ""),
        cursor=0,
        top=0,
        page=1,
        hits=(),
        paint_token=None,
        hover=None,
        pressed=None,
        aggregate_guard=None,
    )
    # End the old gesture before changing ownership; a late release is inert.
    getattr(app, "job_selection_state", {})["capture"] = None
    from raven import chart_interaction, metric_live, pane_drag

    chart_interaction.cancel(app)
    metric_live.cancel(app)
    pane_drag.cancel(app)
    toolbar = getattr(app, "toolbar_state", {})
    toolbar.update(
        menu=None, panel=None, focus="", dragging=False, menu_hits=[], menu_token=None
    )
    pointer = getattr(app, "interaction_state", {})
    pointer.update(active=False, focused=None, pressed=None, frame_required=True)
    app.mode = MODE
    return True


def _activate(app, index, *, pointer=False):
    state = initialize(app)
    if (
        not active(app)
        or type(index) is not int
        or not 0 <= index < len(state["items"])
    ):
        return
    item, frozen = state["items"][index], state["context"]
    if item["action"] == "more":
        # Some native adapters report a completed click after its press/release
        # pair. Expansion changes the action at these coordinates even after a
        # valid repaint. Protect only this short-lived aggregate, not a new
        # physical press or a deliberate click elsewhere in the menu.
        previous_hit = (
            next(
                (
                    (row, left, right)
                    for row, left, right, actual in state["hits"]
                    if actual == index
                ),
                None,
            )
            if pointer
            else None
        )
        _expand(app)
        if previous_hit is not None:
            state["aggregate_guard"] = (
                time.monotonic() + AGGREGATE_GUARD_SECONDS,
                *previous_hit,
            )
        return
    pointer_guard = None
    if pointer:
        hit = next(
            (
                (row, left, right)
                for row, left, right, actual in state["hits"]
                if actual == index
            ),
            None,
        )
        if hit is not None:
            pointer_guard = (time.monotonic() + AGGREGATE_GUARD_SECONDS, *hit)
    close(app)
    action = item["action"]
    if action == "cancel":
        return
    if action.startswith("scheduler:"):
        _review_scheduler(app, frozen, item, pointer_guard)
        return
    from raven.jobs import group_actions as A

    if action == "rerun":
        if A.validate(app, frozen) is not None and frozen.get("rerun_source"):
            from ..ui.operations import open_rerun

            try:
                open_rerun(app, frozen["targets"][0], source=frozen["rerun_source"])
            except (ValueError, KeyError, OSError, TypeError) as exc:
                _feedback(app, "Rerun: " + clean(exc))
        return
    if action == "export":
        if A.validate(app, frozen) is not None:
            from ..job_logs.history_export import open_menu as export_menu

            export_menu(app, frozen["targets"])
        return
    A.execute(
        app,
        frozen,
        action,
        group_id=item.get("group_id"),
        expected=item.get("expected"),
    )


def run_command(app, args):
    if not args or args[0] != "jobgroupmenu":
        return False
    state = initialize(app)
    if args[1:] == ["cancel"]:
        close(app)
    elif (
        active(app)
        and len(args) == 4
        and args[1] == "choose"
        and args[2] == str(state["token"])
        and args[3].isdigit()
        and _fresh(app)
    ):
        _activate(app, int(args[3]))
    return True


def handle_key(app, key):
    if not active(app):
        return False
    state = initialize(app)
    if (
        key == "f8"
        or getattr(app, "interaction_state", {}).get("active")
        and key not in ("esc", "ctrl-c", "q")
    ):
        from raven.interaction import handle_key as focus_key

        if focus_key(app, key):
            return True
    if key in ("esc", "ctrl-c", "q"):
        close(app)
    elif key in (
        "up",
        "down",
        "left",
        "right",
        "tab",
        "btab",
        "pgup",
        "pgdn",
        "home",
        "end",
    ):
        count, page = len(state["items"]), max(1, state["page"])
        delta = {
            "up": -1,
            "down": 1,
            "left": -1,
            "right": 1,
            "tab": 1,
            "btab": -1,
            "pgup": -page,
            "pgdn": page,
            "home": -count,
            "end": count,
        }[key]
        cursor = state["cursor"] + delta
        state["cursor"] = (
            cursor % count
            if key in ("tab", "btab") and count
            else max(0, min(count - 1, cursor))
        )
        state.update(pressed=None, hover=None)
    elif key in ("enter", "space"):
        _activate(app, state["cursor"])
    return True


def tick(app):
    state = initialize(app)
    if active(app) and state.get("tab") != getattr(app, "tab", ""):
        close(app)
        _feedback(app, "Job menu closed because its source page changed")


def _paint_context(app, width=None, height=None):
    state = initialize(app)
    return (
        state["token"],
        getattr(app, "mode", "main"),
        getattr(app, "tab", ""),
        width if width is not None else getattr(app, "width", None),
        height if height is not None else getattr(app, "height", None),
        state["top"],
    )


def _fresh(app):
    state = initialize(app)
    return bool(active(app) and state.get("paint_token") == _paint_context(app))


def overlay(views, snap, app, width, height):
    state = initialize(app)
    state.update(hits=(), paint_token=None)
    if not active(app):
        return None
    from raven import modal_scrollbars as B
    from raven import scrollbars as S

    staged = S.initialize(app)["staged"]
    staged[:] = [pane for pane in staged if pane.key != PANE]
    ascii_ = views.g.ascii
    room, available = max(1, min(88, width - 8)), max(1, height - 4)
    targets = state["context"]["targets"]
    source_group = state["context"].get("source_group")
    heading = (
        (" in " + clean(source_group.label, ascii_, limit=256)) if source_group else ""
    )
    prefix = [
        [
            (
                L.cut(
                    f" {len(targets)} selected job"
                    + ("s" if len(targets) != 1 else "")
                    + heading
                    + ": "
                    + ", ".join(targets[:8])
                    + (", ..." if len(targets) > 8 else ""),
                    room,
                    ascii_,
                ),
                "accent+bold",
            )
        ]
    ]
    if available < 2:
        prefix = []
    items = state["items"]
    count = len(items)
    cursor = max(0, min(state["cursor"], max(0, count - 1)))
    state["cursor"] = cursor
    page = max(1, available - len(prefix) - (1 if available >= 5 else 0))
    top = min(max(0, state["top"]), max(0, count - page))
    if cursor < top:
        top = cursor
    elif cursor >= top + page:
        top = cursor - page + 1
    context = (MODE, state["token"])
    logical, painted = B.window(
        app, PANE, top, count, page, context=context, focus=cursor
    )
    state.update(top=logical, page=page)
    rows, logical_hits = list(prefix), []
    for index in range(painted, min(count, painted + page)):
        item = items[index]
        style = (
            "sel"
            if index == cursor
            else "accent+under"
            if index == state["hover"]
            else "text"
        )
        logical_hits.append((len(rows), index))
        rows.append(
            [
                (
                    L.cut(
                        " [ " + clean(item["label"], ascii_, limit=1024) + " ]",
                        room,
                        ascii_,
                    ),
                    style,
                )
            ]
        )
    if len(rows) < available:
        rows.append(
            [
                (
                    L.cut(
                        " Arrows / wheel choose | Enter select | Esc cancel",
                        room,
                        ascii_,
                    ),
                    "dim",
                )
            ]
        )
    rendered = L.box(views.g, rows, width, height, "Job actions")
    hits = []
    for logical_row, index in logical_hits:
        row_index = logical_row + 1
        if row_index < len(rendered) - 1:
            y, x, segments = rendered[row_index]
            right = x + L.vlen(L.row_text(segments)) - 2  # reserve the scrollbar cell
            if x + 1 < right:
                hits.append((y, x + 1, right, index))
    state.update(hits=tuple(hits), paint_token=_paint_context(app, width, height))
    return B.boxed(
        app,
        PANE,
        rendered,
        start=len(prefix),
        count=count,
        page=page,
        target=logical,
        painted=painted,
        setter=lambda value: _scroll(app, value),
        context=context,
        header=0 if prefix else -1,
    )


def _scroll(app, value):
    state = initialize(app)
    state.update(top=max(0, int(value)), pressed=None, hover=None, paint_token=None)


def controls(app):
    if not _fresh(app):
        return ()

    state = initialize(app)
    token = str(state["token"])
    return tuple(
        Control(
            "job-group-menu:" + token + ":" + str(index),
            state["items"][index]["label"],
            Rect(y, left, y + 1, right),
            ("command", "jobgroupmenu choose " + token + " " + str(index)),
            "job-group-menu",
            layer=2,
        )
        for y, left, right, index in state["hits"]
    )


def handle_mouse(app, y, x, button="left", shift=False):
    if not active(app):
        if button != "right" or getattr(app, "mode", "main") != "main":
            return False
        from raven.jobs.group_drag import menu_source_at

        return open_menu(app, menu_source_at(app, y, x))
    state = initialize(app)
    guard = state.get("aggregate_guard")
    if guard is not None:
        deadline, row, left, right = guard
        inside = y == row and left <= x < right
        if (
            time.monotonic() >= deadline
            or button == "press"
            or button in ("motion", "drag")
            and not inside
        ):
            state["aggregate_guard"] = None
        elif button in ("left", "double") and inside:
            return True
    if not _fresh(app):
        state.update(hover=None, pressed=None)
        return True
    hit = next(
        (
            index
            for row, left, right, index in state["hits"]
            if row == y and left <= x < right
        ),
        None,
    )
    if button in ("motion", "drag"):
        state["hover"] = hit
    elif button in ("wheel-up", "wheel-down", "wheel_up", "wheel_down"):
        handle_key(app, "up" if button in ("wheel-up", "wheel_up") else "down")
    elif button == "press":
        state["pressed"] = (state["paint_token"], hit) if hit is not None else None
        if hit is not None:
            state["cursor"] = hit
    elif button == "release":
        pressed, state["pressed"] = state["pressed"], None
        if hit is not None and pressed == (state["paint_token"], hit):
            _activate(app, hit, pointer=True)
    elif button in ("left", "double") and hit is not None:
        state["cursor"] = hit
        _activate(app, hit, pointer=True)
    return True
