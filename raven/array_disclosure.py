"""Exact-cohort task disclosure using the already published Arrays document."""
from __future__ import annotations


def publish(app, groups):
    """Retain the chosen cohort when publication inserts or reorders arrays."""
    order = tuple((group.get("cluster", ""), group.get("id")) for group in groups)
    previous = getattr(app, "research_array_order", ())
    cursor = getattr(app, "cursor", {}).get("research", 0)
    if previous and 0 <= cursor < len(previous):
        key = previous[cursor]
        matches = [index for index, item in enumerate(order) if item == key]
        if len(matches) == 1:
            app.cursor["research"] = matches[0]
        else:
            app.cursor["research"] = max(0, min(cursor, len(order) - 1))
            app.research_array_open = False
            app.research_task_offset = 0
    app.research_array_order, app.research_groups = order, groups


def _index(app, array_id, cluster):
    matches = [index for index, group in enumerate(getattr(app, "research_groups", ()))
               if group.get("id") == array_id
               and (cluster is None or group.get("cluster", "") == cluster)]
    if len(matches) != 1:
        raise ValueError("Array cohort is absent or ambiguous in the current document")
    return matches[0]


def select(app, target):
    """Select an exact row; legacy plain IDs remain valid when unambiguous."""
    from .job_selection import resume
    array_id, cluster = target if isinstance(target, tuple) and len(target) == 2 else (target, None)
    index = _index(app, array_id, cluster)
    resume(app, "research")
    app.cursor["research"] = index
    app.research_task_offset = 0
    app.research_array_focus = True
    return index


def set_open(app, array_id, opened, *, cluster=None):
    """Open or close a known cohort without toggling another selected cohort.

    Arrays has a single selected task page, separate from shared batch folds.
    Repeated actions preserve the requested state; repeated opens also retain
    its current task page. No scheduler or Research request runs here.
    """
    if (getattr(app, "mode", "main") != "main" or getattr(app, "tab", "") != "research"
            or getattr(app, "research_view", "") != "arrays"):
        raise ValueError("Open Research / Arrays before changing its task page")
    if not isinstance(array_id, str) or type(opened) is not bool:
        raise ValueError("Choose a published array cohort")
    index = _index(app, array_id, cluster)
    current = getattr(app, "cursor", {}).get("research", 0)
    if opened:
        from .job_selection import resume
        resume(app, "research")
        if current != index or not getattr(app, "research_array_open", False):
            app.research_task_offset = 0
        app.cursor["research"] = index
        app.research_array_open = True
        app.research_array_focus = True
    elif current == index:
        app.research_array_open = False
        app.research_task_offset = 0
    return True


def command(app, args):
    if len(args) not in (2, 3) or args[0] not in ("open", "close"):
        raise ValueError("array open|close ARRAYID [CLUSTER]")
    return set_open(app, args[1], args[0] == "open",
                    cluster=args[2] if len(args) == 3 else None)
