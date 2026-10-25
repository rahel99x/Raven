"""Cached-observation table controls in the shared terminal event loop."""

from __future__ import annotations
from .ui.table_schema import ordered_definitions
from .table_query import matches

from .table_query import (
    _state as _state,
    parse_rule as parse_rule,
    validate_numeric as validate_numeric,
    validate_dates as validate_dates,
    filter_text as filter_text,
    numeric_rules as numeric_rules,
    extra_fingerprint as extra_fingerprint,
    numeric_value as numeric_value,
    numeric_matches as numeric_matches,
    history_matches as history_matches,
    date_label as date_label,
    recent_limit as recent_limit,
    recent_matches as recent_matches,
    page_size as page_size,
    snapshot as snapshot,
    freeze_status as freeze_status,
    focused_column as focused_column,
    MAX_RULES as MAX_RULES,
    NUMERIC_FIELDS as NUMERIC_FIELDS,
    FILTER_FIELDS as FILTER_FIELDS,
    ALIASES as ALIASES,
    COMPARISON as COMPARISON,
    MEMORY as MEMORY,
)

import copy
from datetime import date, datetime, time as daytime, timedelta
import math

from . import clock, layout as L, table_sort
from .model import Finished, human
from .research_text import clean


def initialize(app):
    app.table_tools_state = {
        "numeric": {},
        "dates": None,
        "recents": {"count": 5, "window": None, "auto": False},
        "pages": {},
        "modal": "",
        "cursor": 0,
        "tab": "jobs",
        "edit": None,
        "header": None,
        "freeze": None,
        "freeze_time": None,
        "freeze_ids": None,
        "mark_scope": "all",
        "node": "",
        "hits": [],
        "expand_recent": False,
    }


def restore(app, data):
    if not isinstance(data, dict):
        return
    state = _state(app)
    numeric = data.get("numeric", {})
    for table, rules in numeric.items() if isinstance(numeric, dict) else ():
        if table in table_sort.TABLE_KEYS:
            try:
                state["numeric"][table] = validate_numeric(rules)
            except ValueError:
                pass
    try:
        state["dates"] = validate_dates(data.get("dates"))
        if state["dates"]:
            _ensure_history_window(app, state["dates"]["start"])
            _accounting_range(app, state["dates"])
    except ValueError:
        pass
    recent = data.get("recents", {})
    if isinstance(recent, dict):
        count, window, auto = (
            recent.get("count", 5),
            recent.get("window"),
            recent.get("auto", False),
        )
        if (
            not isinstance(count, bool)
            and count in (5, 10, 25)
            and isinstance(auto, bool)
            and (
                window is None
                or not isinstance(window, bool)
                and isinstance(window, (int, float))
                and math.isfinite(window)
                and 0 < window <= 3650 * 86400
            )
        ):
            state["recents"] = {"count": count, "window": window, "auto": auto}


def save(app):
    state = _state(app)
    return {
        "numeric": copy.deepcopy(state.get("numeric", {})),
        "dates": copy.deepcopy(state.get("dates")),
        "recents": dict(
            state.get("recents", {"count": 5, "window": None, "auto": False})
        ),
    }


from .table_query import set_filter_text as set_filter_text


def record_page(app, tab, visible):
    state = _state(app)
    if state:
        state["pages"][tab] = max(1, int(visible))


def select_resource(app, table, names):
    """Remember resource identities without borrowing a job selection."""
    state = _state(app)
    if not state:
        return
    ordered = state.setdefault("resource_ids", {})
    chosen = state.setdefault("selected_resources", {})
    previous = ordered.get(table, [])
    cursor = app.cursor.get(table, 0)
    old = previous[cursor] if 0 <= cursor < len(previous) else chosen.get(table)
    ordered[table] = list(names)
    if old in names:
        app.cursor[table] = names.index(old)
    cursor = app.clamp_cursor(table, len(names))
    chosen[table] = names[cursor] if names else None


def selected_record(app, table, snap):
    """Return the full current resource record for Explain/Peek."""
    state = _state(app)
    names = state.get("resource_ids", {}).get(table, [])
    if table == "sources":
        names = getattr(app, "source_ids", [])
    cursor = app.cursor.get(table, 0)
    name = (
        names[cursor]
        if 0 <= cursor < len(names)
        else state.get("selected_resources", {}).get(table)
    )
    if table == "nodes":
        return snap.get("nodes", {}).get(name) or snap.get("nodemap", {}).get(name)
    if table == "cluster":
        return next(
            (
                partition
                for partition in snap.get("partitions", [])
                if partition.name == name
            ),
            None,
        )
    if table == "sources":
        return snap.get("health", {}).get(name)
    return app.job_record(app.selected_id, snap)


def command_names():
    return [
        "sorteditor",
        "headers",
        "where",
        "filters",
        "viewpicker",
        "historyrange",
        "recents",
        "marked",
        "freeze",
        "jobactions",
        "node",
        "drill",
    ]


def _persist(app, table=None):
    callback = getattr(app, "table_sort_changed", None)
    if table and callable(callback):
        callback(table)
    elif callable(getattr(app, "save", None)):
        app.save()


def _open(app, modal, tab=None):
    _state(app).update(
        modal=modal,
        cursor=0,
        tab=tab or app.tab,
        edit=None,
        hits=[],
        item_ids=[],
        control_hits=[],
        menu_painted=None,
    )
    app.mode = "table_tools"


def _close(app):
    _state(app).update(
        modal="", edit=None, header=None, hits=[], control_hits=[], menu_painted=None
    )
    app.mode = "main"


def _table(app, values):
    values = list(values)
    table = values.pop(0) if values and values[0] in table_sort.TABLE_KEYS else app.tab
    if table not in table_sort.TABLE_KEYS:
        raise ValueError(
            "Choose jobs, recent, history, group, nodes, cluster or sources"
        )
    return table, values


def _date_range(app, values):
    today = datetime.fromtimestamp(clock.now()).date()
    if values == ["all"]:
        return None
    if values == ["today"]:
        start = end = today
    elif values == ["yesterday"]:
        start = end = today - timedelta(days=1)
    elif values in (["week"], ["this-week"]):
        start, end = today - timedelta(days=today.weekday()), today
    elif len(values) == 2:
        try:
            start, end = (date.fromisoformat(value) for value in values)
        except ValueError as exc:
            raise ValueError("Dates use YYYY-MM-DD") from exc
        if start > end:
            raise ValueError("Start date must be on or before end date")
    else:
        raise ValueError(
            "historyrange today|yesterday|week|all, or historyrange START END"
        )
    start_ts = datetime.combine(start, daytime.min).timestamp()
    end_ts = datetime.combine(end + timedelta(days=1), daytime.min).timestamp()
    days = max(1, math.ceil((clock.now() - start_ts) / 86400) + 1)
    if days > 3650:
        raise ValueError("History date ranges request at most 3650 days of accounting")
    _ensure_history_window(app, start_ts)
    return {
        "start": start_ts,
        "end": end_ts,
        "label": f"{start.isoformat()} through {end.isoformat()} (local dates)",
    }


def _ensure_history_window(app, start_ts):
    days = min(3650, max(1, math.ceil((clock.now() - start_ts) / 86400) + 1))
    if days > app.analytics_days_value():
        if days not in app.days_options:
            app.days_options.append(days)
            app.days_options.sort()
        app.set_days(app.days_options.index(days))


def _accounting_range(app, interval):
    setter = getattr(
        getattr(getattr(app, "sampler", None), "slurm", None), "set_history_range", None
    )
    if callable(setter):
        if interval is None:
            setter()
        else:
            setter(
                datetime.fromtimestamp(interval["start"]).isoformat(),
                datetime.fromtimestamp(interval["end"]).isoformat(),
            )


def run_command(app, args):
    if not args or args[0] not in command_names():
        return False
    command, values = args[0], list(args[1:])
    state = _state(app)
    try:
        if command in ("sorteditor", "headers", "filters", "where", "viewpicker"):
            table, values = (
                ("jobs", [])
                if command == "viewpicker"
                and not values
                and app.tab not in table_sort.TABLE_KEYS
                else _table(app, values)
            )
            if command == "where":
                if not values:
                    app.say(
                        f"{table}: "
                        + (
                            "; ".join(
                                f"{r[0]}{r[1]}{r[3]}" for r in numeric_rules(app, table)
                            )
                            or "no numeric filters"
                        )
                    )
                elif values == ["clear"]:
                    state["numeric"][table] = []
                    _persist(app, table)
                    app.say(f"{table} numeric filters cleared")
                else:
                    rules = [parse_rule(value) for value in values]
                    updated = list(numeric_rules(app, table))
                    for rule in rules:
                        updated = [
                            previous for previous in updated if previous[:2] != rule[:2]
                        ] + [rule]
                    state["numeric"][table] = validate_numeric(updated)
                    _persist(app, table)
                    app.say(
                        f"{table} numeric filters updated; unknown measurements do not match"
                    )
            elif values:
                raise ValueError(f"Usage: {command} [TABLE]")
            else:
                _open(
                    app,
                    {
                        "sorteditor": "sort",
                        "headers": "headers",
                        "filters": "filter",
                        "viewpicker": "views",
                    }[command],
                    table,
                )
        elif command == "historyrange":
            if not values:
                app.say(
                    date_label(app)
                    + "; historyrange today|yesterday|week|all or START END"
                )
            else:
                interval = _date_range(app, values)
                _accounting_range(app, interval)
                state["dates"] = interval
                app.cursor["history"], app.top["history"] = 0, 0
                _persist(app, "history")
                app.say(
                    "History: " + date_label(app) + "; accounting refreshes as needed"
                )
        elif command == "recents":
            from .recents_ui import run_command as recent_mode_command

            if values in (["grouped"], ["sequential"]):
                return recent_mode_command(app, args)
            settings = dict(state["recents"])
            if not values:
                app.say(
                    f"Recents: {recent_limit(app)} rows; recents grouped|sequential|5|10|25|auto|expand|collapse|window DURATION|window all"
                )
            elif values in (["expand"], ["collapse"]):
                state["expand_recent"] = values == ["expand"]
                app.say(f"Recents temporary limit: {recent_limit(app)}")
            elif values == ["auto"]:
                settings["auto"] = True
                state["recents"] = settings
                _persist(app)
                app.say("Recents expands to at most 25 rows when several jobs finish")
            elif len(values) == 1 and values[0] in ("5", "10", "25"):
                settings.update(count=int(values[0]), auto=False)
                state["recents"], state["expand_recent"] = settings, False
                _persist(app)
                app.say(f"Recents limit: {values[0]}")
            elif len(values) == 2 and values[0] == "window":
                seconds = (
                    None if values[1] == "all" else table_sort._duration(values[1])
                )
                if values[1] != "all" and (
                    seconds is None
                    or not math.isfinite(seconds)
                    or not 0 < seconds <= 3650 * 86400
                ):
                    raise ValueError("Recents window uses a duration such as 2h or 7d")
                settings["window"] = seconds
                state["recents"] = settings
                _persist(app)
                app.say(
                    "Recents window updated; awaiting-accounting jobs remain labelled"
                )
            else:
                raise ValueError(
                    "recents grouped|sequential|5|10|25|auto|expand|collapse|window DURATION|window all"
                )
        elif command == "marked":
            if (
                values
                and values[0] not in ("all", "hidden", "visible", "active", "finished")
                or len(values) > 1
            ):
                raise ValueError("marked [all|hidden|visible|active|finished]")
            state["mark_scope"] = values[0] if values else "all"
            _open(app, "marks")
        elif command == "freeze":
            if values not in ([], ["on"], ["off"]):
                raise ValueError("freeze [on|off]")
            on = not bool(state.get("freeze")) if not values else values == ["on"]
            if on and state.get("freeze") is None:
                observed = app.store.snapshot()
                state.update(
                    freeze=copy.deepcopy(observed),
                    freeze_time=clock.now(),
                    freeze_ids={job.id: job.state for job in observed.get("jobs", [])},
                )
                app.say(
                    "Inspection frozen; sampling continues; actions validate live jobs"
                )
            elif not on:
                frozen = state.get("freeze")
                live = app.store.snapshot()
                old = state.get("freeze_ids") or {}
                current = {job.id: job.state for job in live.get("jobs", [])}
                changed = len(set(old) ^ set(current)) + sum(
                    old[key] != current[key] for key in old.keys() & current.keys()
                )
                state.update(freeze=None, freeze_time=None, freeze_ids=None)
                if frozen:
                    _persist(app, app.tab if app.tab in table_sort.TABLE_KEYS else None)
                app.say(f"Inspection resumed; {changed} queue changes accumulated")
        elif command == "jobactions":
            if len(values) > 1:
                raise ValueError("jobactions [JOBID]")
            jid = values[0] if values else app.selected_id
            observed = snapshot(app, app.store.snapshot())
            if not jid or app.job_record(jid, observed) is None:
                raise ValueError("Choose an observed job ID")
            state["action_job"] = jid
            state["action_items"] = None
            _open(app, "actions")
            state["action_items"] = _items(app, observed)
        elif command == "node":
            if len(values) != 1:
                raise ValueError("node NODE_NAME")
            observed = snapshot(app, app.store.snapshot())
            if values[0] not in observed.get("nodes", {}) and values[
                0
            ] not in observed.get("nodemap", {}):
                raise ValueError("That node is not in the current observation")
            state["node"] = values[0]
            _open(app, "node")
        elif command == "drill":
            if len(values) != 2 or values[0] not in ("partition", "user"):
                raise ValueError("drill partition NAME, or drill user NAME")
            target = "group" if values[0] == "user" else "jobs"
            if target == app.tab:
                from .navigation_ui import record

                record(app, force=True)
            app.enter_tab(target)
            app.table_state["facets"].setdefault(target, {})[values[0]] = values[1]
            app.cursor[target], app.top[target] = 0, 0
            _persist(app, target)
            app.say(f"{target}: {values[0]} {values[1]}; Back restores the overview")
    except (ValueError, OverflowError, OSError) as exc:
        app.fail(str(exc))
    return True


def _records(app, snap):
    return {
        record.id: record
        for record in snap.get("jobs", [])
        + snap.get("finished", [])
        + list(snap.get("departed_jobs", {}).values())
        + snap.get("group", [])
    }


def marked_rows(app, snap=None):
    snap = app.store.snapshot() if snap is None else snap
    records = _records(app, snap)
    visible = set(getattr(app, "visible_ids", [])) | set(getattr(app, "recent_ids", []))
    if app.tab == "history":
        visible = {record.id for record in app.history_jobs(snap)}
    elif app.tab == "group":
        visible = set(getattr(app, "group_ids", []))
    active = {job.id for job in snap.get("jobs", [])}
    scope = _state(app).get("mark_scope", "all")
    result = []
    for jid in sorted(app.marks, key=table_sort._natural):
        record = records.get(jid)
        hidden = jid not in visible
        if (
            scope == "hidden"
            and not hidden
            or scope == "visible"
            and hidden
            or scope == "active"
            and jid not in active
            or scope == "finished"
            and not isinstance(record, Finished)
        ):
            continue
        result.append((jid, record, hidden))
    return result


def _items(app, snap):
    state, modal, tab = _state(app), _state(app)["modal"], _state(app)["tab"]
    if modal == "sort":
        return list(table_sort.chain(app, tab) or [])
    if modal == "headers":
        hidden = app.table_state["hidden"].get(tab, [])
        return [
            column
            for column in ordered_definitions(app, tab)
            if column.key not in hidden
        ]
    if modal == "filter":
        return list(
            {
                "nodes": ("name", "state", "cpus", "memory", "rss"),
                "cluster": ("name", "nodes"),
                "sources": ("name", "state"),
            }.get(tab, FILTER_FIELDS)
        )
    if modal == "views":
        return sorted(app.table_state["views"])
    if modal == "marks":
        return marked_rows(app, snap)
    if modal == "node":
        name = state["node"]
        by_id = {
            job.id: job
            for job in snap.get("jobs", []) + snap.get("group", [])
            if name in job.hosts or name == job.nodelist
        }
        return list(by_id.values())
    if modal == "actions":
        cached = state.get("action_items")
        if isinstance(cached, list):
            return list(cached)
        record = app.job_record(state.get("action_job"), snap)
        actions = [
            ("logs", "Open registered logs"),
            ("details", "Inspect scheduler evidence"),
            ("copy", "Copy exact job ID"),
            ("compare", "Add job to comparison"),
            ("research", "Open research evidence"),
        ]
        live = app.store.job(state.get("action_job"))
        if live and app.actions:
            actions += [
                (action, action.title() + " job (confirmation)")
                for action in ("cancel", "hold", "release", "requeue")
                if app.actions.applicable(action, live)[0]
            ]
        return actions if record else []
    return []


def _selection(app, snap=None):
    state = _state(app)
    snap = snapshot(app, app.store.snapshot()) if snap is None else snap
    items = _items(app, snap)
    modal = state["modal"]
    identities = _item_identities(modal, items)
    previous = state.get("item_ids", [])
    cursor = state["cursor"]
    chosen = previous[cursor] if 0 <= cursor < len(previous) else None
    if previous != identities and chosen in identities:
        state["cursor"] = identities.index(chosen)
    state["item_ids"] = identities
    state["cursor"] = max(0, min(state["cursor"], max(0, len(items) - 1)))
    return items, items[state["cursor"]] if items else None


def _item_identities(modal, items):
    return (
        [item[0] for item in items]
        if modal in ("marks", "sort", "actions")
        else [item.id for item in items]
        if modal == "node"
        else [item.key for item in items]
        if modal == "headers"
        else list(items)
    )


def _open_record(app, jid, action):
    _close(app)
    if action == "logs":
        app.open_log(jid)
    elif action == "details":
        app.detail_id, app.scroll, app.mode = jid, 0, "details"
        if app.sampler:
            app.sampler.select(jid)
            if app.store.job(jid) is None:
                app.sampler.select_fin(jid)
    elif action == "copy":
        from . import clipboard

        app.say(
            clipboard.copy_for_app(app, jid, app.state_dir, **clipboard.options(app))
        )
    elif action == "compare":
        if jid not in app.compare_ids:
            app.compare_ids.append(jid)
        app.analytics_job, app.analytics_view = jid, "compare"
        app.enter_tab("analytics")
    elif action == "research":
        app.research_job_id, app.research_view = jid, "evidence"
        app.enter_tab("research")
    else:
        app.run_command(action + " " + jid)


def _apply_filter_edit(app):
    state = _state(app)
    field, text, tab = (
        state["edit"]["field"],
        state["edit"]["text"].strip(),
        state["tab"],
    )
    try:
        if field in NUMERIC_FIELDS:
            updated = [rule for rule in numeric_rules(app, tab) if rule[0] != field]
            if text:
                updated.append(
                    parse_rule(field + (text if text[0] in "<>=!" else "=" + text))
                )
            state["numeric"][tab] = validate_numeric(updated)
        else:
            facets = dict(app.table_state["facets"].get(tab, {}))
            if text:
                facets[field] = text
            else:
                facets.pop(field, None)
            app.table_state["facets"][tab] = facets
        state["edit"] = None
        _persist(app, tab)
        app.say(f"{tab}: {field} filter updated")
    except ValueError as exc:
        app.fail(str(exc))


def handle_key(app, key):
    if app.mode != "table_tools":
        return False
    state, modal = _state(app), _state(app)["modal"]
    state.update(hits=[], control_hits=[], menu_painted=None)
    items, selected = _selection(app)
    if state["edit"] is not None:
        if key == "esc":
            state["edit"] = None
        elif key == "enter":
            _apply_filter_edit(app)
        elif key in ("backspace", "delete"):
            state["edit"]["text"] = state["edit"]["text"][:-1]
        elif key == "ctrl-u":
            state["edit"]["text"] = ""
        elif key == "space":
            state["edit"]["text"] = (state["edit"]["text"] + " ")[:256]
        elif len(key) == 1 and key.isprintable():
            state["edit"]["text"] = (state["edit"]["text"] + key)[:256]
        return True
    if key in ("esc", "q"):
        _close(app)
        return True
    if key in ("up", "down", "home", "end", "pgup", "pgdn"):
        step = {
            "up": -1,
            "down": 1,
            "home": -len(items),
            "end": len(items),
            "pgup": -max(1, state.get("visible", 10) - 1),
            "pgdn": max(1, state.get("visible", 10) - 1),
        }[key]
        state["cursor"] = max(0, min(max(0, len(items) - 1), state["cursor"] + step))
    elif modal == "sort" and key == "a":
        state["modal"], state["cursor"] = "headers", 0
        state["item_ids"] = []
    elif modal == "sort" and selected:
        chain = list(items)
        if key in ("left", "right"):
            index = state["cursor"]
            other = max(0, min(len(chain) - 1, index + (-1 if key == "left" else 1)))
            chain[index], chain[other] = chain[other], chain[index]
            state["cursor"] = other
        elif key in ("space", "enter"):
            chain[state["cursor"]] = (
                selected[0],
                "desc" if selected[1] == "asc" else "asc",
            )
        elif key in ("delete", "backspace", "d"):
            chain.pop(state["cursor"])
        else:
            return True
        app.table_state["sorts"][state["tab"]] = chain
        state["item_ids"] = [item[0] for item in chain]
        _persist(app, state["tab"])
    elif modal == "headers" and selected and key in ("enter", "space", "left", "right"):
        direction = "asc" if key == "left" else "desc" if key == "right" else None
        if direction:
            table_sort.set_sort(app, state["tab"], selected.key, direction)
        else:
            table_sort.cycle_sort(app, state["tab"], selected.key)
        _persist(app, state["tab"])
    elif modal == "filter" and selected and key == "enter":
        text = app.table_state["facets"].get(state["tab"], {}).get(selected, "")
        if selected in NUMERIC_FIELDS:
            found = next(
                (
                    rule
                    for rule in numeric_rules(app, state["tab"])
                    if rule[0] == selected
                ),
                None,
            )
            text = found[1] + found[3] if found else ""
        state["edit"] = {"field": selected, "text": text}
    elif modal == "filter" and key == "c":
        app.table_state["facets"][state["tab"]], state["numeric"][state["tab"]] = {}, []
        _persist(app, state["tab"])
    elif modal == "views" and key == "S":
        from .command_ui import open_palette

        open_palette(app, "savedview " + state["tab"] + " save ")
    elif modal == "views" and selected and key == "enter":
        _close(app)
        from .ui.owner_ports import lists

        table_command = lists(app).table_command
        table_command(app, ["savedview", "jobs", "load", selected])
        _persist(app)
    elif modal == "views" and selected and key in ("delete", "d"):
        app.table_state["views"].pop(selected, None)
        _persist(app)
    elif modal == "marks":
        if key == "s":
            scopes = ("all", "hidden", "visible", "active", "finished")
            state["mark_scope"] = scopes[
                (scopes.index(state["mark_scope"]) + 1) % len(scopes)
            ]
            state["cursor"] = 0
        elif key in ("delete", "d", "space") and selected:
            app.marks.discard(selected[0])
        elif key == "c":
            app.marks.difference_update(item[0] for item in items)
        elif selected and key in ("enter", "l"):
            _open_record(app, selected[0], "logs" if key == "l" else "details")
    elif modal == "actions" and selected and key == "enter":
        _open_record(app, state["action_job"], selected[0])
    elif modal == "node" and selected and key in ("enter", "l"):
        _open_record(app, selected.id, "logs" if key == "l" else "details")
    if modal == "headers":
        _, selected = _selection(app)
        state["header"] = (
            {"table": state["tab"], "column": selected.key} if selected else None
        )
    return True


def handle_click_hit(app, y, x, hits, button="left", shift=False):
    if app.mode != "main" or button != "left" or shift:
        return False
    for hy, kind, payload in hits:
        if hy == y:
            if kind == "node_cell":
                name, left, right = payload
                if not left <= x < right:
                    continue
                payload = name
            if kind in ("node_row", "node_cell"):
                _state(app).setdefault("selected_resources", {})["nodes"] = payload
                names = _state(app).get("resource_ids", {}).get("nodes", [])
                if payload in names:
                    app.cursor["nodes"] = names.index(payload)
                run_command(app, ["node", str(payload)])
                return True
            if kind == "partition_row":
                _state(app).setdefault("selected_resources", {})["cluster"] = payload
                names = _state(app).get("resource_ids", {}).get("cluster", [])
                if payload in names:
                    app.cursor["cluster"] = names.index(payload)
                run_command(app, ["drill", "partition", str(payload)])
                return True
            if kind == "user_drill":
                run_command(app, ["drill", "user", str(payload)])
                return True
    return False


def _menu_frame(app, items=None):
    state = _state(app)
    if items is None:
        items, _ = _selection(app)
    return (
        app.mode,
        getattr(app, "width", None),
        getattr(app, "height", None),
        state["modal"],
        state["tab"],
        state["cursor"],
        state["mark_scope"],
        state.get("node"),
        state.get("action_job"),
        tuple(_item_identities(state["modal"], items)),
        tuple(items) if state["modal"] == "sort" else None,
        tuple(sorted(state["edit"].items())) if state["edit"] else None,
        getattr(app.store, "_ui_revision", None),
    )


def _action_choices(state):
    if state["edit"] is not None:
        return [
            (key, label, ("key", key))
            for key, label in (
                ("esc", "Cancel edit"),
                ("enter", "Apply field"),
                ("ctrl-u", "Clear field"),
            )
        ]
    actions = {
        "sort": (
            ("a", "Add column"),
            ("space", "Change direction"),
            ("left", "Earlier"),
            ("right", "Later"),
            ("d", "Remove"),
        ),
        "headers": (
            ("enter", "Cycle order"),
            ("left", "Ascending"),
            ("right", "Descending"),
        ),
        "filter": (("enter", "Edit field"), ("c", "Clear filters")),
        "views": (("enter", "Load view"), ("d", "Delete view")),
        "marks": (
            ("enter", "Details"),
            ("l", "Logs"),
            ("s", "Change subset"),
            ("d", "Unmark"),
            ("c", "Clear subset"),
        ),
        "actions": (("enter", "Open selected action"),),
        "node": (("enter", "Details"), ("l", "Logs")),
    }
    choices = [
        (key, label, ("key", key))
        for key, label in (
            ("esc", "Done"),
            *actions[state["modal"]],
        )
    ]
    if state["modal"] == "views":
        choices.append(("save", "Save current view…", ("key", "S")))
    return choices


def handle_mouse(app, y, x, button="left", shift=False):
    if button != "left" or shift:
        return app.mode == "table_tools"
    if app.mode == "main" and y == getattr(app, "table_chip_y", None):
        tab, cells = getattr(app, "table_chip_cells", ("", []))
        for key, left, right in cells:
            if left <= x < right:
                if key.startswith("numeric:"):
                    rules = list(numeric_rules(app, tab))
                    target = getattr(app, "table_chip_rule_ids", {}).get(key)
                    _state(app)["numeric"][tab] = [
                        rule for rule in rules if tuple(rule) != target
                    ]
                else:
                    app.table_state["facets"].setdefault(tab, {}).pop(key, None)
                _persist(app, tab)
                app.say(f"{tab}: filter removed")
                return True
    if app.mode != "table_tools":
        return False
    state = _state(app)
    if state.get("menu_painted") != _menu_frame(app):
        return True
    for hy, _, value in state.get("control_hits", []):
        if y == hy and value["left"] <= x < value["right"]:
            handle_key(app, value["action"][1])
            return True
    for hy, left, right, index in _state(app).get("hits", []):
        if y == hy and left <= x < right:
            _state(app)["cursor"] = index
            handle_key(app, "enter")
            return True
    return True


def _filter_count(app, snap, table):
    if table == "nodes":
        candidates = list(snap.get("nodes", {}).values())
    elif table == "cluster":
        candidates = snap.get("partitions", [])
    elif table == "sources":
        candidates = [
            {
                "name": h.name,
                "error": h.error,
                "state": "off"
                if not h.enabled
                else "error"
                if h.error
                else "ok"
                if h.last_ok
                else "pending",
            }
            for h in snap.get("health", {}).values()
        ]
    else:
        candidates = (
            snap.get("finished", [])
            if table in ("history", "recent")
            else snap.get("group", [])
            if table == "group"
            else snap.get("jobs", [])
        )
    text = filter_text(app, table).casefold()

    def text_match(record):
        get = (
            record.get
            if isinstance(record, dict)
            else lambda key, default="": getattr(record, key, default)
        )
        tags = snap.get("tags", {}).get(get("id", ""), {}).get("tags", [])
        if text.startswith("#"):
            return text[1:] in [str(tag).casefold() for tag in tags]
        return not text or any(
            text in str(get(field, "")).casefold()
            for field in (
                "id",
                "name",
                "state",
                "partition",
                "partitions",
                "user",
                "avail",
                "error",
            )
        )

    return sum(
        matches(app, table, record, snap)
        and text_match(record)
        and (table != "history" or history_matches(app, record))
        for record in candidates
    )


def _filter_preview(app, snap, table):
    edit = _state(app).get("edit")
    if not edit:
        return _filter_count(app, snap, table), ""
    draft = copy.copy(app)
    draft.table_state = copy.deepcopy(app.table_state)
    draft.table_tools_state = dict(
        _state(app), numeric=copy.deepcopy(_state(app)["numeric"])
    )
    field, text = edit["field"], edit["text"].strip()
    try:
        if field in NUMERIC_FIELDS:
            rules = [rule for rule in numeric_rules(app, table) if rule[0] != field]
            if text:
                rules.append(
                    parse_rule(field + (text if text[0] in "<>=!" else "=" + text))
                )
            draft.table_tools_state["numeric"][table] = validate_numeric(rules)
        else:
            facets = draft.table_state["facets"].setdefault(table, {})
            if text:
                facets[field] = text
            else:
                facets.pop(field, None)
        return _filter_count(draft, snap, table), " (preview; Enter applies)"
    except ValueError:
        return _filter_count(
            app, snap, table
        ), " (incomplete value; current filters shown)"


def overlay(views, snap, app, width, height):
    if app.mode != "table_tools":
        return None
    state, modal, tab = _state(app), _state(app)["modal"], _state(app)["tab"]
    snap = snapshot(app, snap)
    items, selected = _selection(app, snap)
    titles = {
        "sort": "Sort priorities",
        "headers": "Column headers",
        "filter": "Filter builder",
        "views": "Saved view picker",
        "marks": "Marked jobs",
        "actions": "Job actions",
        "node": "Node details",
    }
    instructions = {
        "sort": "Up/Down select | Left/Right priority | Space direction | d remove | a add | Esc done",
        "headers": "Up/Down select | Enter cycles | Left ascending | Right descending | Esc done",
        "filter": "Arrows select field | Enter edit | empty removes | c clear | Esc done",
        "views": "Arrows select | Enter load | d delete | :savedview save NAME | Esc back",
        "marks": "s subset | d unmark | c clear subset | Enter details | l logs | Esc back",
        "actions": "Arrows select | Enter run | scheduler changes keep confirmation | Esc back",
        "node": "Arrows select job | Enter details | l logs | Esc back",
    }
    from .control_rows import buttons, place_hits

    action_rows, action_hits = buttons(
        views.g,
        max(1, width - 8),
        _action_choices(state),
        group="table-tools",
        prefix="table-tools-action:",
    )
    rows = action_rows + [[(f" {tab.title()} | {instructions[modal]}", "dim")]]
    if modal == "filter":
        count, suffix = _filter_preview(app, snap, tab)
        rows.append([(f" {count} cached records match filters{suffix}", "cyan")])
        rows.append(
            [(" Unknown measurements do not match; efficiencies use 30% or 0.3", "dim")]
        )
        if state["edit"]:
            edit = state["edit"]
            rows.append(
                [
                    (
                        f" {edit['field']}: {clean(edit['text'], views.g.ascii)}",
                        "accent+bold",
                    )
                ]
            )
            rows.append(
                [
                    (
                        " Type value/comparison; Enter applies; Ctrl-U clears; Esc cancels edit",
                        "dim",
                    )
                ]
            )
    elif modal == "marks":
        rows.append(
            [
                (
                    f" Subset {state['mark_scope']} | {len(items)} of {len(app.marks)} marked IDs | hidden marks remain active",
                    "yellow",
                )
            ]
        )
    elif modal == "actions":
        record = app.job_record(state["action_job"], snap)
        rows.append(
            [
                (
                    clean(
                        f" Exact job {state['action_job']} | {getattr(record, 'name', '')} | {getattr(record, 'state', 'unavailable')}",
                        views.g.ascii,
                    ),
                    "cyan+bold",
                )
            ]
        )
    elif modal == "node":
        name = state["node"]
        node = snap.get("nodes", {}).get(name)
        cell = snap.get("nodemap", {}).get(name)
        rows.append(
            [
                (
                    f" {name} | state {getattr(node, 'state', getattr(cell, 'state', 'unknown'))}",
                    "cyan+bold",
                )
            ]
        )
        if node:
            load = f"{node.load:g}" if node.load is not None else "unknown"
            free = (
                human(node.mem_free * 1024**2)
                if node.mem_free is not None
                else "unknown"
            )
            rows.append(
                [
                    (
                        f" CPUs {node.alloc}/{node.cpus} allocated | load {load} | free memory {free}",
                        "",
                    )
                ]
            )
            rows.append(
                [
                    (
                        clean(
                            f" GRES {node.gres or 'unknown'} | used {node.gres_used or 'unknown'} | partitions {node.partitions or 'unknown'}",
                            views.g.ascii,
                        ),
                        "",
                    )
                ]
            )
        elif cell:
            rows.append(
                [
                    (
                        f" CPUs {cell.cpus_alloc}/{cell.cpus} | GPUs {cell.gpus_used}/{cell.gpus} | allocated memory {human(cell.mem_alloc * 1024**2)}",
                        "",
                    )
                ]
            )
        health = snap.get("health", {}).get("nodes") or snap.get("health", {}).get(
            "nodemap"
        )
        age = (
            max(0, clock.now() - health.last_ok) if health and health.last_ok else None
        )
        rows.append(
            [
                (
                    f" Measurement age: {age:.0f}s"
                    if age is not None
                    else " Measurement age: unavailable",
                    "dim",
                )
            ]
        )
        if health and health.error:
            rows.append(
                [
                    (
                        clean(
                            " Reported source problem: " + health.error, views.g.ascii
                        ),
                        "red",
                    )
                ]
            )
        rows.append(
            [
                (
                    " Observed jobs on this node (ownership listing may be incomplete):",
                    "dim",
                )
            ]
        )
    available = max(1, height - len(rows) - 5)
    state["visible"] = available
    start = max(
        0, min(state["cursor"] - available // 2, max(0, len(items) - available))
    )
    from . import modal_scrollbars as B

    context = (
        modal,
        tab,
        state.get("node"),
        state.get("action_job"),
        state.get("mark_scope"),
    )
    logical, start = B.window(
        app,
        "modal:table-tools",
        start,
        len(items),
        available,
        context=context,
        focus=state["cursor"],
    )
    data_start = len(rows)
    row_items = []
    for index, item in enumerate(items[start : start + available], start):
        if modal == "sort":
            text = f" {index + 1}. {item[0]} {'ascending' if item[1] == 'asc' else 'descending'}"
        elif modal == "headers":
            direction = dict(table_sort.chain(app, tab) or []).get(item.key, "off")
            text = f" {item.title:16} ({item.key}) {direction}"
        elif modal == "filter":
            value = app.table_state["facets"].get(tab, {}).get(item, "")
            if item in NUMERIC_FIELDS:
                value = "; ".join(
                    rule[1] + rule[3]
                    for rule in numeric_rules(app, tab)
                    if rule[0] == item
                )
            text = f" {item:12} {value or '(all)'}"
        elif modal == "views":
            view = app.table_state["views"][item]
            facets = ", ".join(
                f"{key}={value}" for key, value in view.get("facets", {}).items()
            )
            sorts = ", ".join(
                f"{key}:{direction}" for key, direction in view.get("sorts", [])
            ) or view.get("sort", "default")
            text = f" {item} | {view.get('tab')} | {view.get('filter') or facets or 'all rows'} | {sorts} | {len(view.get('hidden', []))} hidden columns"
        elif modal == "marks":
            jid, record, hidden = item
            text = f" {jid:16} {getattr(record, 'name', 'record unavailable')} | {getattr(record, 'state', 'unknown')} | {'hidden by view' if hidden else 'visible'}"
        elif modal == "actions":
            text = " " + item[1]
        else:
            text = f" {item.id:16} {item.name} | {item.state} | {item.cpus} CPUs"
        row_items.append((len(rows), index))
        rows.append(
            [
                (
                    L.cut(clean(text, views.g.ascii), max(1, width - 8), views.g.ascii),
                    "sel" if index == state["cursor"] else "",
                )
            ]
        )
    if not items:
        rows.append(
            [
                (
                    " No sort criteria; use a to add a header"
                    if modal == "sort"
                    else " No matching cached entries",
                    "dim",
                )
            ]
        )
    result = L.box(views.g, rows, width, height, titles[modal])
    state["control_hits"] = place_hits(action_hits, result[1:-1])
    placements = result[1:-1]
    state["hits"] = [
        (
            placements[row_index][0],
            placements[row_index][1],
            min(
                width,
                placements[row_index][1] + L.vlen(L.row_text(placements[row_index][2])),
            ),
            index,
        )
        for row_index, index in row_items
        if row_index < len(placements)
    ]
    if modal == "headers" and selected:
        state["header"] = {"table": tab, "column": selected.key}
    state["menu_painted"] = _menu_frame(app, items)
    return B.boxed(
        app,
        "modal:table-tools",
        result,
        start=data_start,
        count=len(items),
        page=available,
        target=logical,
        painted=start,
        setter=lambda value: None,
        context=context,
    )
