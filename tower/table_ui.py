"""Explicit columns, field filters and named table views with stable job IDs."""
from __future__ import annotations

from dataclasses import replace
import copy
import math
import re

from . import layout as L
from .table_sort import (TABLE_KEYS, chain, clear_sort, cycle_sort, describe_sort, header_hits,
                         history_value, reset_sort, set_sort, sort_fingerprint, sort_rows,
                         valid_header, validate_chain)

MAX_VIEWS = 32
COLUMN_LAYOUT_VERSION = 2
FIELDS = {"state", "partition", "tag", "name", "id", "user"}
REQUIRED = {"id", "name", "st", "state"}


def initialize(app):
    app.table_state = {"hidden": {}, "order": {}, "widths": {}, "filters": {}, "facets": {}, "views": {}, "sorts": {}, "cursor": 0, "tab": "jobs", "groups": True, "auto_groups": True, "collapsed": [], "control_hits": []}


def restore(app, data):
    if not isinstance(data, dict):
        return
    legacy_columns = data.get("column_layout_version") != COLUMN_LAYOUT_VERSION
    for tab in TABLE_KEYS:
        for key, check in (("hidden", _hidden), ("facets", _facets), ("order", _order), ("widths", _widths)):
            values = data.get(key, {})
            if isinstance(values, dict) and tab in values:
                try:
                    app.table_state[key][tab] = check(values[tab], tab)
                except ValueError:
                    pass
        if legacy_columns and tab == "jobs" and tab in app.table_state["order"]:
            order = app.table_state["order"][tab]
            app.table_state["order"][tab] = ["progress"] + [key for key in order if key != "progress"]
        filters = data.get("filters", {})
        if isinstance(filters, dict) and isinstance(filters.get(tab), str) and len(filters[tab]) <= 256:
            app.table_state["filters"][tab] = filters[tab]
    sorts = data.get("sorts", {})
    if isinstance(sorts, dict):
        for table in TABLE_KEYS:
            if table in sorts:
                try:
                    app.table_state["sorts"][table] = validate_chain(table, sorts[table])
                except ValueError:
                    pass
    views = data.get("views", {})
    if isinstance(views, dict):
        for name, value in list(views.items())[:MAX_VIEWS]:
            if not isinstance(name, str) or not re.fullmatch(r"[\w .-]{1,64}", name):
                continue
            try:
                view = _view(app, value)
                if legacy_columns and view["tab"] == "jobs":
                    view["order"] = ["progress"] + [key for key in view["order"] if key != "progress"]
                app.table_state["views"][name] = view
            except ValueError:
                continue
    if "groups" in data:
        app.table_state["groups"] = data.get("groups") is True
    if isinstance(data.get("auto_groups"), bool):
        app.table_state["auto_groups"] = data["auto_groups"]
    from .job_groups import valid_group_id
    app.table_state["collapsed"] = [value for value in data.get("collapsed", [])[:256] if valid_group_id(value)] if isinstance(data.get("collapsed"), list) else []
    from .manual_job_groups import validate_state
    app.table_state["manual_groups"] = validate_state(data.get("manual_groups"))


def save(app):
    result = {key: copy.deepcopy(app.table_state[key]) for key in ("hidden", "order", "widths", "filters", "facets", "views", "sorts", "groups", "collapsed")}
    result["auto_groups"] = app.table_state.get("auto_groups", True) is True
    from .column_resize import committed_widths
    committed_widths(app, result["widths"])
    result["column_layout_version"] = COLUMN_LAYOUT_VERSION
    from .manual_job_groups import validate_state
    result["manual_groups"] = validate_state(app.table_state.get("manual_groups"))
    return result


def command_names():
    return ["columns", "facet", "savedview", "jobgroups", "jobgroup", "sortby"]


def group_rows(app, rows, snap=None, tab="jobs", *, index=None):
    from .job_groups import project_rows
    return project_rows(app, rows, snap, tab, index=index)


def definitions(tab):
    from .views import JOB_COLS, Views
    if tab == "jobs":
        return JOB_COLS
    if tab in ("history", "recent"):
        return Views.FIN_COLS
    if tab == "group":
        return Views.GROUP_COLS
    labels = {"nodes": {"name": "NODE", "cpus": "ALLOC/CPUS", "loadpct": "LOAD%", "mem": "MEM USED", "gused": "GRES USED", "gutil": "GPU%", "jobs": "MY JOBS"},
              "cluster": {"name": "PARTITION", "cidle": "CPUS IDLE", "calloc": "CPUS ALLOC", "mine": "MY RUN", "minep": "MY PEND", "gpus": "GPUS FREE/UP"},
              "sources": {"name": "SOURCE", "every": "INTERVAL", "last": "LAST OK"}}
    return [L.Column(key, labels.get(tab, {}).get(key, key.upper()), 4, 60)
            for key in TABLE_KEYS.get(tab, ())]


def _order(value, tab):
    available = {column.key for column in definitions(tab)}
    if (not isinstance(value, list) or len(value) > len(available) or
            any(not isinstance(key, str) or key not in available for key in value) or len(value) != len(set(value))):
        raise ValueError("invalid column order")
    return list(value)


def _widths(value, tab):
    available = {column.key for column in definitions(tab)}
    if (not isinstance(value, dict) or len(value) > len(available) or
            any(key not in available or isinstance(width, bool) or not isinstance(width, int) or not 2 <= width <= 120
                for key, width in value.items())):
        raise ValueError("column widths must be between 2 and 120 terminal cells")
    result = dict(value)
    if tab == "jobs" and "progress" in result:
        result["progress"] = 6
    return result


def _hidden(value, tab):
    available = {column.key for column in definitions(tab)}
    if (not isinstance(value, list) or len(value) > len(available) or
            any(not isinstance(key, str) or key not in available or key in REQUIRED for key in value)):
        raise ValueError("invalid hidden columns")
    return sorted(set(value))


def _facets(value, tab):
    if (not isinstance(value, dict) or len(value) > len(FIELDS) or
            any(key not in FIELDS or not isinstance(text, str) or len(text) > 256
                for key, text in value.items())):
        raise ValueError("invalid field filters")
    return {key: text for key, text in value.items() if text}


def _view(app, value):
    """Validate the complete restored view before changing any navigation."""
    from .views import SORTS
    if not isinstance(value, dict) or not isinstance(value.get("tab"), str) or value["tab"] not in TABLE_KEYS:
        raise ValueError("invalid table")
    tab = value["tab"]
    text, sort = value.get("filter", ""), value.get("sort", app.sort.get(tab, "name"))
    reverse, days = value.get("reverse", False), value.get("days", app.analytics_days_value())
    if (not isinstance(text, str) or len(text) > 256 or sort not in SORTS.get(tab, ["name"]) or
            not isinstance(reverse, bool) or isinstance(days, bool) or
            not isinstance(days, (int, float)) or not math.isfinite(days) or not 0 < days <= 3650):
        raise ValueError("invalid table settings")
    result = {"tab": tab, "hidden": _hidden(value.get("hidden", []), tab),
              "order": _order(value.get("order", []), tab), "widths": _widths(value.get("widths", {}), tab),
              "facets": _facets(value.get("facets", {}), tab), "filter": text,
              "sort": sort, "reverse": reverse, "days": days}
    if "sorts" in value:
        result["sorts"] = validate_chain(tab, value["sorts"])
    if "numeric" in value or "dates" in value:
        from .table_tools import validate_numeric, validate_dates
        result["numeric"] = validate_numeric(value.get("numeric", []))
        result["dates"] = validate_dates(value.get("dates"))
    return result


def columns(app, tab, original):
    state = getattr(app, "table_state", {})
    hidden = state.get("hidden", {}).get(tab, [])
    hidden = hidden if isinstance(hidden, list) else []
    order = state.get("order", {}).get(tab, [])
    positions = {key: index for index, key in enumerate(order)} if isinstance(order, list) else {}
    original = sorted(original, key=lambda column: positions.get(column.key, len(positions)))
    widths = state.get("widths", {}).get(tab, {})
    original = [replace(column, lo=6, hi=6, flex=False) if tab == "jobs" and column.key == "progress" else
                replace(column, lo=widths[column.key], hi=widths[column.key], flex=False)
                if isinstance(widths, dict) and isinstance(widths.get(column.key), int) and not isinstance(widths[column.key], bool) and 2 <= widths[column.key] <= 120
                else column for column in original]
    selected = chain(app, tab)
    if selected is not None:
        indicators = {key: f" {'^' if direction == 'asc' else 'v'}{index}"
                      for index, (key, direction) in enumerate(selected, 1)}
        # Keep compact progress at its requested width, including the full
        # cascading priority. Other headings retain their normal sizing.
        titles = {column.key: ((L.cut(column.title, max(0, column.hi - L.vlen(indicators[column.key])))
                   if column.key == "progress" else column.title) + indicators[column.key])
                  for column in original if column.key in indicators}
        return [replace(column, title=titles[column.key],
                        lo=column.lo if column.key in widths else max(column.lo, L.vlen(titles[column.key])),
                        hi=column.hi if column.key in widths else max(column.hi, L.vlen(titles[column.key])))
                if column.key in indicators else column for column in original
                if column.key not in hidden or column.key in REQUIRED]
    key = app.sort.get(tab, "")
    mapping = {"state": "st" if tab == "jobs" else "state", "cpu_eff": "ce", "mem_eff": "me"}
    sorted_key = mapping.get(key, key)
    return [replace(column, title=column.title + (" v" if app.reverse.get(tab) else " ^"),
                    lo=column.lo if column.key in widths else max(column.lo, L.vlen(column.title) + 2),
                    hi=column.hi if column.key in widths else max(column.hi, L.vlen(column.title) + 2))
            if column.key == sorted_key else column for column in original
            if column.key not in hidden or column.key in REQUIRED]


def facets_fingerprint(app, tab):
    facets = getattr(app, "table_state", {}).get("facets", {}).get(tab, {})
    return tuple(sorted(facets.items())) if isinstance(facets, dict) else ()


def fingerprint(app, tab):
    """Selection cache identity includes both filters and explicit sort state."""
    from .table_tools import extra_fingerprint
    return (facets_fingerprint(app, tab), sort_fingerprint(app, tab), extra_fingerprint(app, tab),
            getattr(app, "table_state", {}).get("auto_groups", True) is True)


def matches(app, tab, record, snap):
    facets = getattr(app, "table_state", {}).get("facets", {}).get(tab, {})
    rules = getattr(app, "table_tools_state", {}).get("numeric", {}).get(tab, ())
    active_numeric = isinstance(rules, (list, tuple)) and bool(rules)
    # Most table passes have no field filters. Read current settings directly
    # so in-place changes take effect, without preparing unused record fields
    # or sorting a fingerprint for every row in a large accounting window.
    if isinstance(facets, dict) and facets:
        get = record.get if isinstance(record, dict) else lambda key, default="": getattr(record, key, default)
        for field, expected in facets.items():
            if field == "tag":
                value = " ".join(snap.get("tags", {}).get(get("id", ""), {}).get("tags", []))
            elif field == "state":
                value = "ACCOUNTING" if get("id", "") in snap.get("departed_jobs", {}) else get("state", "")
            elif field == "user":
                value = get("user", "") or "?"
            else:
                value = get(field, "") if field in FIELDS else ""
            actual = str(value).casefold()
            choices = [part.casefold() for part in str(expected).split(",")]
            if field in ("state", "partition", "id", "user"):
                if actual not in choices:
                    return False
            elif field == "tag":
                if not any(choice in actual.split() for choice in choices):
                    return False
            elif not any(choice in actual for choice in choices):
                return False
    if active_numeric:
        from .table_tools import numeric_matches
        return numeric_matches(app, tab, record, snap)
    return True


def chips(app, tab, width, ascii_=False):
    facets = facets_fingerprint(app, tab)
    from .research import clean
    from .table_tools import numeric_rules
    entries = [(key, f"[{key}={value} x]") for key, value in facets]
    rules = numeric_rules(app, tab)
    entries += [(f"numeric:{index}", f"[{rule[0]}{rule[1]}{rule[3]} x]") for index, rule in enumerate(rules)]
    app.table_chip_rule_ids = {f"numeric:{index}": tuple(rule) for index, rule in enumerate(rules)}
    x, cells, segments = 9, [], [(" Filters ", "accent")]
    for key, label in entries:
        text = clean(label + "  ", ascii_)
        if x >= width:
            break
        visible = L.cut(text, max(0, width - x), ascii_)
        segments.append((visible, "accent"))
        cells.append((key, x, min(width, x + L.vlen(label))))
        x += L.vlen(visible)
    app.table_chip_cells = (tab, cells)
    return [("".join(text for text, _ in segments), "accent")] if entries else []


def run_command(app, args):
    if not args or args[0] not in command_names():
        return False
    command, values = args[0], list(args[1:])
    if command == "jobgroup":
        if values == ["reset"]:
            from .manual_job_groups import reset
            result = reset(app)
            app.say(result.message)
            if result.changed:
                app.job_selection_state["capture"] = None
                app.interaction_state.update(active=False, focused=None, frame_required=True)
                app.save()
            return True
        if values in (["create"], ["ungroup"]):
            from .manual_group_ui import run_action
            run_action(app, values[0], explicit=True)
            return True
        if len(values) != 2 or values[0] not in ("toggle", "open", "close"):
            app.fail("Usage: jobgroup create|ungroup|reset, or jobgroup toggle|open|close GROUP_ID")
            return True
        from .job_groups import fold, registry
        from .table_tools import snapshot
        groups = registry(app)
        groups.ensure(snapshot(app, app.store.snapshot()))
        gid = values[1]
        if gid.isdigit() and "array:" + gid in groups.index.groups:
            gid = "array:" + gid
        collapsed = {"toggle": None, "open": False, "close": True}[values[0]]
        if not fold(app, gid, collapsed):
            app.fail("That launch group is no longer present")
        else:
            group = groups.index.groups[gid]
            app.say(group.label + (" closed" if groups.is_collapsed(group) else " open") + "; " + group.reason)
        return True
    if command == "sortby":
        # A column can share a table name (History's NODES; Nodes' MY JOBS).
        # Treat it as the current column when followed by a direction, or when
        # cycling it alone. An explicit table still precedes another column.
        current_keys = TABLE_KEYS.get(app.tab, ())
        explicit_table = bool(values and values[0] in TABLE_KEYS and
                              (values[0] not in current_keys or
                               len(values) > 1 and values[1] not in ("asc", "desc", "off")))
        table = values.pop(0) if explicit_table else app.tab
        if table not in TABLE_KEYS:
            app.fail("Choose a sortable table: " + ", ".join(TABLE_KEYS))
            return True
        if not values:
            app.say(f"{table}: " + describe_sort(app, table) + "; sortby COLUMN asc|desc|off, or sortby clear")
            return True
        try:
            if values == ["clear"]:
                clear_sort(app, table)
            elif len(values) == 1:
                cycle_sort(app, table, values[0])
            elif len(values) == 2:
                set_sort(app, table, values[0], values[1])
            else:
                raise ValueError("sortby [TABLE] COLUMN asc|desc|off, or sortby [TABLE] clear")
        except ValueError as exc:
            app.fail(str(exc) + "; columns: " + ", ".join(TABLE_KEYS[table]))
            return True
        callback = getattr(app, "table_sort_changed", None)
        if callable(callback):
            callback(table)
        if len(values) != 1 or values == ["clear"]:
            app.say(f"{table}: " + describe_sort(app, table))
        return True
    if command == "jobgroups":
        if values not in ([], ["on"], ["off"]):
            app.fail("Usage: jobgroups [on|off]")
        else:
            app.table_state["groups"] = not app.table_state["groups"] if not values else values == ["on"]
            if values == ["on"]:
                app.table_state["auto_groups"] = True
            app.say("Launch grouping " + ("on; Left/Right closes or opens the selected group" if app.table_state["groups"] else "off"))
        return True
    tab = values.pop(0) if values and values[0] in TABLE_KEYS else app.tab
    if tab not in TABLE_KEYS:
        app.fail("Choose a table, or specify jobs, recent, history, group, nodes, cluster or sources")
        return True
    state = app.table_state
    if command == "columns":
        if tab == "recent":
            # Embedded Recents deliberately shares Queue geometry. Do not
            # accept settings that would save successfully but never appear.
            from .column_resize import RECENT_ALIASES
            inverse = {value: key for key, value in RECENT_ALIASES.items()}
            available_jobs = {column.key for column in definitions("jobs")}
            requested = ([values[1]] if len(values) == 3 and values[0] == "width" else
                         [part for value in values[1:] for part in value.split(",")]
                         if values and values[0] in ("order", "show", "hide") else [])
            translated = [inverse.get(key, key) for key in requested]
            if any(key not in available_jobs for key in translated):
                app.fail("Recents shares Queue columns; configure History for completed-only columns")
                return True
            if requested:
                values = (["width", translated[0], values[2]] if values[0] == "width" else
                          [values[0], ",".join(translated)])
            tab = "jobs"
            if not values:
                app.say("Recents shares the Queue column layout")
        available = {column.key for column in definitions(tab)}
        if not values:
            state["tab"], state["cursor"], app.mode = tab, 0, "columns"
        elif values == ["reset"]:
            state["hidden"][tab] = []
            state["order"][tab] = []
            state["widths"][tab] = {}
            app.say(f"{tab} columns reset")
        elif len(values) >= 2 and values[0] == "order":
            try:
                state["order"][tab] = _order([part for value in values[1:] for part in value.split(",")], tab)
                app.say(f"{tab} column order updated")
            except ValueError as exc:
                app.fail(str(exc))
        elif len(values) == 3 and values[0] == "width":
            try:
                updated = dict(state["widths"].get(tab, {}))
                if values[2] == "auto":
                    if values[1] not in available:
                        raise ValueError("unknown column")
                    updated.pop(values[1], None)
                else:
                    if tab == "jobs" and values[1] == "progress" and int(values[2]) != 6:
                        raise ValueError("Progress uses a fixed width of six terminal cells")
                    updated[values[1]] = int(values[2])
                state["widths"][tab] = _widths(updated, tab)
                app.say(f"{tab} column width updated")
            except ValueError as exc:
                app.fail(str(exc))
        elif len(values) >= 2 and values[0] in ("show", "hide"):
            requested = {part for value in values[1:] for part in value.split(",")}
            if not requested <= available or (values[0] == "hide" and requested & REQUIRED):
                app.fail("Unknown columns or required identity/state column; use :columns to browse")
            else:
                hidden = set(state["hidden"].get(tab, []))
                hidden = hidden | requested if values[0] == "hide" else hidden - requested
                state["hidden"][tab] = sorted(hidden)
                app.say(f"{tab} columns updated")
        else:
            app.fail("Usage: columns [TABLE] [show|hide KEYS|order KEYS|width KEY 2..120|width KEY auto|reset]")
    elif command == "facet":
        if values == ["clear"]:
            state["facets"][tab] = {}
        else:
            parsed = dict(state["facets"].get(tab, {}))
            for value in values:
                field, separator, content = value.partition("=")
                if not separator or field not in FIELDS or len(content) > 256:
                    app.fail("Filters use state=FAILED,TIMEOUT partition=gpu tag=TAG name=TEXT id=ID")
                    return True
                if content:
                    parsed[field] = content
                else:
                    parsed.pop(field, None)
            state["facets"][tab] = parsed
        app.cursor[tab], app.top[tab] = 0, 0
        app.say(f"{tab} field filters updated")
    else:
        action = values[0] if values else "list"
        if action == "list":
            app.say("Saved views: " + (", ".join(sorted(state["views"])) or "none; savedview save NAME"))
        elif len(values) == 2 and action in ("save", "load", "delete"):
            name = values[1]
            if not re.fullmatch(r"[\w .-]{1,64}", name):
                app.fail("View names must be 1–64 letters, numbers, spaces, dots or hyphens")
            elif action == "save":
                if name not in state["views"] and len(state["views"]) >= MAX_VIEWS:
                    app.fail("At most 32 saved views; delete an unused view first")
                else:
                    from .table_tools import filter_text, numeric_rules
                    view = {"tab": tab, "hidden": list(state["hidden"].get(tab, [])),
                        "order": list(state["order"].get(tab, [])), "widths": dict(state["widths"].get(tab, {})),
                        "facets": dict(state["facets"].get(tab, {})), "filter": filter_text(app, tab),
                        "numeric": list(numeric_rules(app, tab)), "dates": copy.deepcopy(getattr(app, "table_tools_state", {}).get("dates")) if tab == "history" else None,
                        "sort": app.sort.get(tab, "name"), "reverse": bool(app.reverse.get(tab)),
                        "days": app.analytics_days_value()}
                    selected = chain(app, tab)
                    if selected is not None:
                        view["sorts"] = selected
                    state["views"][name] = view
                    app.say(f"Saved table view {name}")
            elif action == "delete":
                state["views"].pop(name, None)
                app.say(f"Deleted table view {name}")
            elif name not in state["views"]:
                app.fail("No saved view with that name")
            else:
                try:
                    view = _view(app, state["views"][name])
                except ValueError as exc:
                    app.fail(f"Saved view {name} is damaged: {exc}; delete it and save a new view")
                    return True
                target = view["tab"]
                app.enter_tab("jobs" if target == "recent" else target)
                state["hidden"][target] = view["hidden"]
                state["order"][target] = view["order"]
                state["widths"][target] = view["widths"]
                state["facets"][target] = view["facets"]
                from .table_tools import set_filter_text
                set_filter_text(app, target, view["filter"])
                app.sort[target] = view["sort"]
                if "numeric" in view and hasattr(app, "table_tools_state"):
                    app.table_tools_state["numeric"][target] = list(view["numeric"])
                    if target == "history":
                        app.table_tools_state["dates"] = view["dates"]
                app.reverse[target] = view["reverse"]
                if "sorts" in view:
                    state["sorts"][target] = list(view["sorts"])
                else:
                    reset_sort(app, target)
                if view["days"] not in app.days_options:
                    app.days_options.append(view["days"])
                    app.days_options.sort()
                app.set_days(app.days_options.index(view["days"]))
                app.cursor[target], app.top[target] = 0, 0
                app.say(f"Loaded table view {name}")
        else:
            app.fail("Usage: savedview list|save NAME|load NAME|delete NAME")
    return True


def handle_key(app, key):
    if app.mode == "main" and app.tab in ("jobs", "history", "group", "deps") and app.table_state["groups"] and key in ("left", "right"):
        from .job_groups import fold, registry
        from .table_tools import snapshot
        groups = registry(app)
        groups.ensure(snapshot(app, app.store.snapshot()))
        group = groups.index.for_job(app.selected_id)
        return fold(app, group.id, key == "left") if group else False
    if app.mode != "columns":
        return False
    state = app.table_state
    cols = ordered_definitions(app, state["tab"])
    if key in ("esc", "q", "enter"):
        app.mode = "main"
        state["control_hits"] = []
    elif key in ("up", "down", "home", "end"):
        step = {"up": -1, "down": 1, "home": -len(cols), "end": len(cols)}[key]
        state["cursor"] = max(0, min(len(cols) - 1, state["cursor"] + step))
    elif key == "space" and cols:
        column = cols[state["cursor"]]
        if column.key not in REQUIRED:
            hidden = set(state["hidden"].get(state["tab"], []))
            hidden.symmetric_difference_update({column.key})
            state["hidden"][state["tab"]] = sorted(hidden)
    elif key in ("left", "right") and cols:
        index = state["cursor"]
        other = max(0, min(len(cols) - 1, index + (-1 if key == "left" else 1)))
        keys = [column.key for column in cols]
        keys[index], keys[other] = keys[other], keys[index]
        state["order"][state["tab"]] = keys
        state["cursor"] = other
    elif key in ("+", "=", "-", "a") and cols:
        column = cols[state["cursor"]]
        if state["tab"] == "jobs" and column.key == "progress":
            app.say("Progress uses a fixed width of six terminal cells")
            return True
        widths = state["widths"].setdefault(state["tab"], {})
        if key == "a":
            widths.pop(column.key, None)
        else:
            widths[column.key] = max(2, min(120, widths.get(column.key, max(column.lo, L.vlen(column.title))) + (-1 if key == "-" else 1)))
    elif key == "r":
        for field, value in (("order", []), ("widths", {}), ("hidden", [])):
            state[field][state["tab"]] = value
        state["cursor"] = 0
    if key in ("esc", "q", "enter", "space", "left", "right", "+", "=", "-", "a", "r"):
        callback = getattr(app, "save", None)
        if callable(callback):
            callback()
    return True


def ordered_definitions(app, tab):
    cols = definitions(tab)
    order = getattr(app, "table_state", {}).get("order", {}).get(tab, [])
    positions = {key: index for index, key in enumerate(order)}
    return sorted(cols, key=lambda column: positions.get(column.key, len(positions)))


def handle_mouse(app, y, x, button="left", shift=False):
    """Toggle the exact checkbox painted here using the keyboard's rules."""
    if getattr(app, "mode", "main") != "columns" or button != "left":
        return False
    state = app.table_state
    for row, kind, value in state.get("control_hits", []):
        if row != y or not value["left"] <= x < value["right"]:
            continue
        table, column = value["column"]
        if table != state["tab"]:
            return False
        cols = ordered_definitions(app, table)
        index = next((index for index, item in enumerate(cols) if item.key == column), None)
        if index is None:
            return False
        state["cursor"] = index
        if column in REQUIRED:
            app.say("Required identity/state column; use width or reorder to adjust it")
        else:
            handle_key(app, "space")
        return True
    return False


def overlay(views, snap, app, width, height):
    state = app.table_state
    state["control_hits"] = []
    if app.mode != "columns":
        return None
    cols = ordered_definitions(app, state["tab"])
    hidden = state["hidden"].get(state["tab"], [])
    cursor = state["cursor"]
    # Two hints, two borders and the box's outer margins reserve six rows.
    # Fit selection to the actual visible checkbox capacity on tiny terminals.
    available = max(0, height - 6)
    start = max(0, min(max(0, len(cols) - available), cursor - available // 2))
    from . import modal_scrollbars as B
    context = ("columns", state["tab"])
    logical, start = B.window(app, "modal:columns", start, len(cols), available,
                              context=context, focus=cursor)
    rows = [[(" Space show/hide | Up/Down select | Left/Right reorder", "dim")],
            [(" +/- width | a automatic width | r reset | Enter done", "dim")]]
    for index, column in enumerate(cols[start:start + available], start):
        required = " (required)" if column.key in REQUIRED else ""
        width_text = ("6 (fixed)" if state["tab"] == "jobs" and column.key == "progress"
                      else str(state["widths"].get(state["tab"], {}).get(column.key, "auto")))
        text = f" [{' ' if column.key in hidden else 'x'}] {column.title:16} {column.key} width {width_text}{required}"
        rows.append([(text, "sel" if index == cursor else "")])
    rendered = L.box(views.g, rows, width, height, state["tab"].title() + " columns")
    # The box may clip rows or terminal cells. Derive controls only from its
    # actual interior placements, never guessed modal offsets or labels.
    for index, column in enumerate(cols[start:start + available]):
        line_index = index + 2
        if line_index >= max(0, len(rendered) - 2):
            break
        y, x, segments = rendered[line_index + 1]
        inner_width = max(0, L.vlen(L.row_text(segments)) - 2)
        visible_width = min(inner_width, L.vlen(L.row_text(rows[line_index])))
        if y < 1 or visible_width < 1:
            continue
        left, right = x + 1, x + 1 + visible_width
        required = column.key in REQUIRED
        state["control_hits"].append((y, "control", {
            "id": f"column:{state['tab']}:{column.key}", "label": column.title + " checkbox",
            "left": left, "right": right, "action": ("click", y, left), "group": "columns",
            "column": (state["tab"], column.key), "enabled": not required,
            "reason": "Required identity/state column" if required else ""}))
    return B.boxed(app, "modal:columns", rendered, start=2, count=len(cols), page=available,
                   target=logical, painted=start, setter=lambda value: None, context=context)
