"""Cached jump search, portable locations, settings, keys, and full field peeks.

The overlays use the existing event loop. They do not discover files, invoke
Slurm, or execute pasted commands while drawing or searching.
"""
from __future__ import annotations

import copy
import json
import math
import shlex
import time
from itertools import chain, islice

from . import clock, layout as L, navigation_ui
from .research import RESEARCH_VIEWS, clean
from .palette import THEME_NAMES

MODES = {"jump_picker", "settings_editor", "bindings_editor", "field_explanation", "value_peek", "locations_picker"}
MAX_LOCATIONS = 50
MAX_INDEX = 4096
MAX_VALUE = 65536
RESERVED_KEYS = {"ctrl-g": "jump search", "ctrl-b": "Back", "ctrl-p": "workspace picker", "ctrl-a": "Activity",
                 "ctrl-w": "workspace focus", "f6": "workspace focus", "z": "workspace maximize outside Logs",
                 "alt-left": "Back", "alt-right": "Forward"}


def initialize(app):
    if not isinstance(getattr(app, "navigation_tools_state", None), dict):
        app.navigation_tools_state = {}
    state = app.navigation_tools_state
    defaults = {"query": "", "cursor": 0, "top": 0, "page": 8, "hits": [], "index": [], "index_at": -1,
                "locations": {}, "settings": {}, "bindings": {}, "preview_backup": None,
                "polling_preview_backup": None,
                "edit": "", "editing": False, "test": False, "test_message": "", "field": "",
                "value": "", "scroll": 0, "return_mode": "main"}
    for key, value in defaults.items():
        state.setdefault(key, value)
    return state


def command_names():
    return ["jump", "location", "settings", "keybindings", "explain", "peek"]


def _plain(value, limit=4096):
    return isinstance(value, str) and len(value) <= limit and all(ch.isprintable() for ch in value)


def _json_value(value, depth=0, budget=None):
    """Produce bounded portable state; opaque result references never persist."""
    budget = [8192] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0 or depth > 12:
        raise ValueError("location is too large")
    if value is None or type(value) is bool or type(value) is int:
        if type(value) is int and abs(value) > 10**12:
            raise ValueError("location number exceeds bounds")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("location contains a non-finite value")
        return value
    if isinstance(value, str):
        if not _plain(value, MAX_VALUE):
            raise ValueError("location contains invalid text")
        return value
    if isinstance(value, (tuple, list)) and len(value) <= 512:
        return [_json_value(item, depth + 1, budget) for item in value]
    if isinstance(value, dict) and len(value) <= 512:
        return {key: _json_value(item, depth + 1, budget) for key, item in value.items()
                if _plain(key, 128)}
    raise ValueError("location contains unsupported data")


def _portable_location(app):
    saved = navigation_ui.location(app)
    saved.pop("passport_context", None)
    if isinstance(saved.get("analysis_context"), dict):
        saved["analysis_context"].pop("comparison", None)
        if saved["analysis_context"].get("modal") == "diff":
            saved.pop("analysis_context", None)
    saved["mode"] = "analysis" if saved.get("mode") == "analysis" and saved.get("analysis_context") else "main"
    saved["profile"] = getattr(app, "profile_name", "")
    from .log_tools import _target
    saved["source_target"] = _target(app.logs.files)
    saved["logs"].pop("_buffer_token", None)
    if "project_context" in saved:
        backup = saved["project_context"].get("binding_backup")
        saved["project_context"] = {key: value for key, value in saved["project_context"].items()
                                    if key not in ("runs", "warnings", "binding_backup")}
        if isinstance(backup, dict):
            # Clearing a reopened run must restore the original manually
            # attached sources. Loaded passport objects remain session-local;
            # the original declared passport path survives in settings.
            saved["project_context"]["binding_backup"] = {"settings": backup.get("settings", {}),
                "settings_extra": {key: value for key, value in backup.get("settings_extra", {}).items()
                                   if key in ("interval", "planning_file", "planning_files", "planning_overrides", "submit_file")},
                "log_manifest": backup.get("log_manifest", ""), "passport_record": None, "passport_diff": None}
    if "table_tools_context" in saved:
        saved["table_tools_context"].pop("freeze", None)
    tools = saved.pop("log_tools_context", None)
    if app.mode == "log_tools_page" and tools and tools.get("page") and tools.get("page_source"):
        page = tools["page"]
        first = page["rows"][0] if page.get("rows") else {}
        saved["log_page_descriptor"] = {"page_source": tools["page_source"], "offset": page["start"], "identity": page["snapshot"]["ident"],
                                        "line": first.get("line"), "fragment": first.get("partial_start", False),
                                        "page_cursor": tools["page_cursor"], "page_pan": tools["page_pan"]}
    return _json_value(saved)


def _valid_location(value):
    from .views import TABS
    if not isinstance(value, dict) or value.get("tab") not in dict(TABS):
        raise ValueError("invalid location page")
    result = _json_value(value)
    if len(json.dumps(result, ensure_ascii=False)) > 1024 * 1024:
        raise ValueError("location exceeds one MiB")
    for field in ("selected_id", "detail_id", "log_job", "research_job_id", "analytics_job"):
        if result.get(field) is not None and not _plain(result[field], 256):
            raise ValueError("invalid selected identity")
    for field in ("filter", "profile", "source_target", "log_manifest"):
        if field in result and not _plain(result[field], 4096 if field == "log_manifest" else 256):
            raise ValueError("invalid location text")
    for field in ("cursor", "top"):
        if field in result:
            result[field] = navigation_ui._bounded_map(result[field])
    for field in ("sort", "reverse", "research_settings", "table_context", "table_tools_context", "panel_context", "log_view_context", "project_context", "analysis_context", "log_page_descriptor"):
        if field in result and not isinstance(result[field], dict):
            raise ValueError("invalid " + field)
    project = result.get("project_context", {})
    backup = project.get("binding_backup")
    if backup is not None:
        if not isinstance(backup, dict) or not isinstance(backup.get("settings"), dict) or not _plain(backup.get("log_manifest"), 4096):
            raise ValueError("invalid original project sources")
        extra = backup.get("settings_extra", {})
        if not isinstance(extra, dict):
            raise ValueError("invalid original research settings")
        from .projects import PLANNING_PATHS
        for key, path in dict(extra, **backup["settings"]).items():
            if key in ("metrics_file", "contract", "workdir", "passport", "planning_file", "submit_file"):
                if not _plain(path, 4096):
                    raise ValueError("invalid original project source path")
            elif key == "planning_files":
                if (not isinstance(path, dict) or path.keys() - set(PLANNING_PATHS)
                        or any(not _plain(value, 4096) for value in path.values())):
                    raise ValueError("invalid original per-view planning paths")
            elif key == "planning_overrides":
                if (not isinstance(path, dict) or path.keys() - set(PLANNING_PATHS)
                        or any(not isinstance(options, dict) or len(options) > 16 for options in path.values())):
                    raise ValueError("invalid original planning overrides")
            elif key == "interval":
                if type(path) not in (int, float) or not 1 <= path <= 86400:
                    raise ValueError("invalid original research interval")
            else:
                raise ValueError("invalid original research setting")
    from .table_sort import TABLE_KEYS, validate_chain
    from .views import SORTS
    for table, key in result.get("sort", {}).items():
        if table not in SORTS or key not in SORTS[table]:
            raise ValueError("invalid table sort")
    if any(table not in SORTS or type(direction) is not bool for table, direction in result.get("reverse", {}).items()):
        raise ValueError("invalid reverse preference")
    table = result.get("table_context", {})
    for name in ("sorts", "facets", "filters", "hidden", "order", "widths"):
        if name in table and not isinstance(table[name], dict):
            raise ValueError("invalid table " + name)
    for scope, rules in table.get("sorts", {}).items():
        validate_chain(scope, rules)
    for scope, text in table.get("filters", {}).items():
        if scope not in TABLE_KEYS or not _plain(text, 256):
            raise ValueError("invalid table filter")
    from . import table_ui
    for field, check in (("facets", table_ui._facets), ("hidden", table_ui._hidden), ("order", table_ui._order), ("widths", table_ui._widths)):
        for scope, preference in table.get(field, {}).items():
            if scope not in TABLE_KEYS:
                raise ValueError("invalid table scope")
            check(preference, scope)
    tools = result.get("table_tools_context", {})
    if "numeric" in tools:
        from .table_tools import validate_dates, validate_numeric
        if not isinstance(tools["numeric"], dict):
            raise ValueError("invalid numeric filters")
        for scope, rules in tools["numeric"].items():
            if scope not in TABLE_KEYS:
                raise ValueError("invalid numeric filter table")
            validate_numeric(rules)
        validate_dates(tools.get("dates"))
    recent = tools.get("recents")
    if recent is not None:
        if not isinstance(recent, dict) or type(recent.get("count")) is not int or recent["count"] not in (5, 10, 25) or type(recent.get("auto")) is not bool:
            raise ValueError("invalid Recents settings")
        window = recent.get("window")
        if window is not None and (type(window) not in (int, float) or not math.isfinite(window) or not 0 < window <= 3650 * 86400):
            raise ValueError("invalid Recents time window")
    if "log_page_descriptor" in result:
        page = result["log_page_descriptor"]
        if not isinstance(page.get("page_source"), dict) or not _plain(page["page_source"].get("path")):
            raise ValueError("invalid saved log source")
        for field in ("offset", "page_cursor", "page_pan"):
            if type(page.get(field)) is not int or page[field] < 0:
                raise ValueError("invalid saved log page position")
        identity = page.get("identity")
        if not isinstance(identity, list) or len(identity) != 2 or any(type(number) is not int or number < 0 for number in identity):
            raise ValueError("invalid saved log identity")
    logs = result.get("logs", {})
    if not isinstance(logs, dict):
        raise ValueError("invalid log position")
    for field in ("path", "which", "search", "file_filter"):
        if field in logs and logs[field] is not None and not _plain(logs[field], 4096):
            raise ValueError("invalid log source text")
    if logs.get("entry") is not None and (not isinstance(logs["entry"], dict) or not _plain(logs["entry"].get("path"), 4096)):
        raise ValueError("invalid log source declaration")
    for field in ("top", "cursor", "file_index", "browser_cursor", "browser_top"):
        number = logs.get(field)
        if number is not None and (type(number) is not int or not 0 <= number <= 10**9):
            raise ValueError("invalid log position")
    return result


def restore(app, data):
    state = initialize(app)
    if not isinstance(data, dict):
        return
    locations = data.get("locations", {})
    if isinstance(locations, dict):
        for name, value in list(locations.items())[:MAX_LOCATIONS]:
            if not _plain(name, 64) or not name.strip():
                continue
            try:
                state["locations"][name] = _valid_location(value)
            except (ValueError, TypeError, RecursionError):
                continue
    settings = data.get("settings", {})
    if isinstance(settings, dict):
        for key, value in list(settings.items())[:64]:
            if _valid_setting(key, value):
                state["settings"][key] = value
        # Polling has its own saved preference. Legacy source settings must
        # not override the position already restored by refresh_rate.
        _apply_settings(app, state["settings"], polling=False)
    bindings = data.get("bindings", {})
    if isinstance(bindings, dict):
        for action, keys in list(bindings.items())[:128]:
            if action in app.cfg["keys"] and _valid_keys(keys) and not any(key in RESERVED_KEYS for key in keys):
                state["bindings"][action] = list(keys)
        if not _binding_conflicts(state["bindings"], app.cfg["keys"]):
            _apply_bindings(app, state["bindings"])
        else:
            state["bindings"] = {}


def save(app):
    state = initialize(app)
    preview = state.get("preview_backup")
    # A later theme/key command is the current preference. Never let an older
    # Settings Apply override it on restart. During an unaccepted live preview,
    # save its original values even if another operation saves the UI state.
    settings = dict(state["settings"])
    locked = _locked_settings(app)
    if isinstance(preview, dict):
        settings.update({key: value for key, value in preview.items() if key not in locked})
    else:
        settings = {key: value if key in locked else _read_setting(app, key) for key, value in settings.items()}
    return {"locations": copy.deepcopy(state["locations"]), "settings": settings,
            "bindings": copy.deepcopy(state["bindings"])}


def _open(app, mode):
    state = initialize(app)
    origin = state.get("return_mode", "main") if app.mode in MODES else app.mode
    state.update(cursor=0, top=0, scroll=0, hits=[], editing=False, test=False, return_mode=origin if origin != "palette" else "main")
    app.mode = mode


def _index(app):
    state = initialize(app)
    now = time.monotonic()
    if now - state["index_at"] < 1:
        return state["index"]
    result, seen = [], set()
    # Search needs identities only. Avoid copying retained metric histories
    # and node inventories on each index refresh.
    with app.store.lock:
        jobs = tuple(islice(chain(app.store.jobs, app.store.finished, app.store.departed_jobs.values()), MAX_INDEX))
    for job in jobs:
        if job.id in seen:
            continue
        seen.add(job.id)
        result.append({"kind": "job", "value": job.id, "description": "Job " + job.name + " · " + job.state})
        if len(result) >= 2048:
            break
    for run in getattr(app, "project_state", {}).get("runs", [])[:512]:
        if isinstance(run, dict) and _plain(run.get("run_id")):
            result.append({"kind": "run", "value": run["run_id"], "description": "Run " + str(run.get("experiment_id", ""))})
    entries = islice(chain(getattr(app.logs, "entries", []), getattr(app, "project_state", {}).get("logs", [])), 768)
    paths = set()
    for entry in entries:
        if isinstance(entry, dict) and _plain(entry.get("path")) and entry["path"] not in paths:
            paths.add(entry["path"])
            result.append({"kind": "log", "value": entry["path"], "description": "Log " + str(entry.get("label", entry["path"])), "entry": dict(entry)})
    result += [{"kind": "view", "value": name, "description": "Saved table view"} for name in list(getattr(app, "table_state", {}).get("views", {}))[:128]]
    result += [{"kind": "workspace", "value": key, "description": "Workspace " + label} for key, label in RESEARCH_VIEWS]
    result += [{"kind": "command", "value": name, "description": "Command · open for editing"} for name in app.commands()]
    result += [{"kind": "location", "value": name, "description": "Saved location · " + value["tab"]} for name, value in state["locations"].items()]
    state["index"], state["index_at"] = result[:MAX_INDEX], now
    return state["index"]


def jump_matches(app):
    from .command_ui import fuzzy_score
    state = initialize(app)
    query = state["query"].casefold().strip()
    result = []
    for index, entry in enumerate(_index(app)):
        value, desc = entry["value"], entry["description"]
        score = fuzzy_score(query, value) if query else 0
        if score is None and all(word in (value + " " + desc).casefold() for word in query.split()):
            score = 1
        if score is not None:
            result.append((score, -index, entry))
    result.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in result[:512]]


def _open_location(app, name):
    state = initialize(app)
    saved = state["locations"].get(name)
    if saved is None:
        app.fail("Unknown saved location: " + name)
        return
    if saved.get("profile", "") != getattr(app, "profile_name", ""):
        app.fail("This location belongs to a different cluster profile")
        return
    if saved["tab"] == "log" or saved.get("log_page_descriptor"):
        from .log_tools import _target
        if saved.get("source_target", "local") != _target(app.logs.files):
            app.fail("This log location belongs to a different source connection")
            return
    navigation_ui.record(app, saved["tab"], force=True)
    navigation_ui._restore_location(app, copy.deepcopy(saved))
    app.logs._buffer_token = None
    if saved.get("log_page_descriptor"):
        from .log_tools import restore_location
        if not restore_location(app, saved["log_page_descriptor"]):
            return
    app.say("Opened location " + name)


def _activate_jump(app, entry):
    kind, value = entry["kind"], entry["value"]
    app.mode = "main"
    if kind == "job":
        app.run_command("inspect " + shlex.quote(value))
    elif kind == "run":
        app.run_command("run select " + shlex.quote(value))
    elif kind == "view":
        app.run_command("savedview load " + shlex.quote(value))
    elif kind == "workspace":
        navigation_ui.run_command(app, ["workspace", value])
    elif kind == "location":
        _open_location(app, value)
    elif kind == "command":
        from .command_ui import open_palette
        open_palette(app, value + " ", origin=initialize(app).get("return_mode", "main"))
    elif kind == "log":
        navigation_ui.record(app, "log", force=True)
        log = dict(entry["entry"])
        app.log_job = log.get("job_id") or None
        app.log_record = app.job_record(app.log_job) if app.log_job else None
        app.selected_id = app.log_job
        app.logs.entry, app.logs.path = log, log["path"]
        app.logs.file_index, app.logs.top = 0, None
        app.logs.browser, app.logs.browse_return, app.logs._buffer_token = False, False, None
        app.logs.clear_selection(reset_cursor=True)
        app.enter_tab("log")
        app.say("Opened log " + value)


SETTING_CHOICES = {"theme": THEME_NAMES,
                   "workspace.density": ("comfortable", "compact", "focused")}
SETTING_LABELS = {"theme": "Theme", "workspace.density": "Density", "color": "Terminal colors", "animations": "Interface animations",
                  "bell": "Terminal bell", "mouse": "Mouse input", "gpu_sampling": "GPU sampling",
                  "clipboard.osc52": "Terminal clipboard (OSC 52)", "clipboard.tools": "Local clipboard tools",
                  "intervals.jobs": "Polling interval (seconds)"}
TELEMETRY_SETTING = "telemetry_settings"
SETTING_HELP = {"theme": "Preview colors and contrast. Reader uses ASCII and static feedback.",
                "color": "Allow terminal colors; disable for a plain display.",
                "workspace.density": "Preview panel spacing. Compact shows more rows; focused enlarges the active panel.",
                "animations": "Use short completion movement, History indicators, and graph selector easing.", "bell": "Ring the terminal bell for configured start notices.",
                "mouse": "Allow mouse clicks and wheel input. Keyboard controls remain available.",
                "gpu_sampling": "Enable existing GPU observations; this can use a small allocation step.",
                "clipboard.osc52": "Copy through the terminal escape sequence when supported.",
                "clipboard.tools": "Use an installed local clipboard utility when available.",
                "intervals.jobs": "Global native polling request: 0.5 to 5 seconds. Changes update Jobs, CPU, GPU and trace polling; source limits and per-metric requests still apply."}


def _managed_settings(app):
    return bool(app.cfg.get("raven", {}).get("managed"))


def _setting_keys(app):
    from .refresh_rate import NATIVE_SOURCES
    if _managed_settings(app):
        return [key for key in SETTING_LABELS if key not in ("gpu_sampling", "intervals.jobs")] + [TELEMETRY_SETTING]
    return list(SETTING_LABELS) + ["intervals." + name for name in app.cfg["intervals"]
                                 if name not in NATIVE_SOURCES][:32]


def _valid_setting(key, value):
    if key in SETTING_CHOICES:
        return value in SETTING_CHOICES[key]
    if key == "intervals.jobs":
        return type(value) in (int, float) and .5 <= value <= 5 and math.isfinite(value)
    if key in SETTING_LABELS:
        return type(value) is bool
    return isinstance(key, str) and key.startswith("intervals.") and len(key) < 64 and type(value) in (int, float) and math.isfinite(value) and 1 <= value <= 3600


def _read_setting(app, key):
    if key == TELEMETRY_SETTING:
        return "Open profiles and Custom settings"
    if key == "intervals.jobs":
        from .refresh_rate import cadence
        return cadence(app, "jobs")
    if key == "theme":
        return app.theme
    if key == "workspace.density":
        return getattr(app.layout_state, "density", app.cfg.get(key, "comfortable"))
    if key == "gpu_sampling":
        return bool(app.gpu)
    if key == "bell":
        return bool(app.bell)
    return app.cfg.get(key, True)


def _locked_settings(app):
    value = getattr(app.cfg, "ui_locked_settings", ())
    return set(value) if isinstance(value, (set, tuple, list)) else set()


def _apply_settings(app, values, *, polling=True):
    locked = _locked_settings(app)
    applied = {}
    for key, value in values.items():
        if _managed_settings(app) and (key == "gpu_sampling" or isinstance(key, str) and key.startswith("intervals.")):
            # Old generic settings snapshots must not replace the live
            # profile or bypass its shared collector and site policy.
            continue
        if key in locked or not _valid_setting(key, value):
            continue
        if key.startswith("intervals.") and key.split(".", 1)[1] not in app.cfg["intervals"]:
            continue
        if key == "intervals.jobs":
            if polling:
                from .refresh_rate import poll_position, set_multiplier
                set_multiplier(app, poll_position(value))
                applied[key] = _read_setting(app, key)
            continue
        app.cfg.set(key, value)
        applied[key] = value
        if key == "theme":
            app.set_theme(value)
        elif key == "workspace.density":
            app.layout_state.density = value
        elif key == "bell":
            app.bell = value
        elif key == "gpu_sampling":
            app.gpu = value
            if app.sampler:
                app.sampler.gpu_sampling = value
        elif key.startswith("intervals.") and app.sampler:
            app.sampler.intervals[key.split(".", 1)[1]] = float(value)
    callback = getattr(app, "settings_changed", None)
    if callback:
        callback(applied)


def _open_settings(app):
    state = initialize(app)
    from .refresh_rate import multiplier
    state["polling_preview_backup"] = None if _managed_settings(app) else multiplier(app)
    state["preview_backup"] = {key: _read_setting(app, key) for key in _setting_keys(app)}
    state["draft"] = dict(state["preview_backup"])
    _open(app, "settings_editor")


def _settings_default(app):
    from .config import Config, DEFAULTS
    defaults = Config(DEFAULTS)
    values = {key: defaults.get(key, True) for key in _setting_keys(app)}
    if _managed_settings(app):
        values[TELEMETRY_SETTING] = _read_setting(app, TELEMETRY_SETTING)
    else:
        values["intervals.jobs"] = 5.0
    values["mouse"] = True
    for key in _locked_settings(app):
        if key in values:
            values[key] = _read_setting(app, key)
    return values


def _settings_key(app, key):
    state = initialize(app)
    keys = _setting_keys(app)
    if keys[state["cursor"]] == TELEMETRY_SETTING and key in ("left", "right", "space", "enter"):
        app.run_command("telemetrysettings")
        return
    if key == "esc":
        _apply_settings(app, state["preview_backup"] or {})
        if state.get("polling_preview_backup") is not None and "intervals.jobs" not in _locked_settings(app):
            from .refresh_rate import set_multiplier
            set_multiplier(app, state["polling_preview_backup"])
        state["preview_backup"] = None
        state["polling_preview_backup"] = None
        app.mode = state["return_mode"]
        app.say("Settings preview cancelled")
    elif key == "enter":
        state["settings"].update({key: value for key, value in state["draft"].items()
                                  if key != TELEMETRY_SETTING and key not in _locked_settings(app)})
        _apply_settings(app, state["draft"])
        state["preview_backup"] = None
        state["polling_preview_backup"] = None
        app.mode = state["return_mode"]
        app.save()
        app.say("Settings applied and saved")
    elif key in ("left", "right", "space"):
        field = keys[state["cursor"]]
        if field in _locked_settings(app):
            app.fail("This setting is fixed by the launch options or environment; restart Raven with different launch options")
            return
        old = state["draft"][field]
        if field in SETTING_CHOICES:
            choices = SETTING_CHOICES[field]
            value = choices[(choices.index(old) + (-1 if key == "left" else 1)) % len(choices)]
        elif type(old) is bool:
            value = not old
        elif field == "intervals.jobs":
            value = max(.5, min(5.0, round(float(old) * (0.8 if key == "left" else 1.25), 2)))
        else:
            value = max(1.0, min(3600.0, round(float(old) * (0.8 if key == "left" else 1.25), 2)))
        state["draft"][field] = value
        _apply_settings(app, {field: value})
        if field == "intervals.jobs":
            state["draft"][field] = _read_setting(app, field)
    elif key == "D":
        state["draft"] = _settings_default(app)
        _apply_settings(app, state["draft"])
    else:
        _move(state, key, len(keys))


def _valid_keys(keys):
    if not isinstance(keys, list) or len(keys) > 4:
        return False
    return all(_valid_key(key) for key in keys) and len(keys) == len(set(keys))


def _valid_key(key):
    if not _plain(key, 32):
        return False
    named = ("up", "down", "left", "right", "enter", "esc", "space", "tab", "btab", "home", "end", "pgup", "pgdn", "delete", "backspace")
    from .screen import _ESCAPE_KEYS
    modifier = key in _ESCAPE_KEYS.values() or key.startswith("ctrl-") and len(key) == 6 and key[-1] in "abcdefgklnopqrstuvwxyz"
    function = key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24 and key == "f" + str(int(key[1:]))
    return len(key) == 1 and key != " " or key in named or modifier or function


def _binding_conflicts(overrides, defaults):
    combined = dict(defaults)
    combined.update(overrides)
    seen, conflicts = {}, []
    for action, keys in combined.items():
        for key in keys:
            if key in seen and seen[key] != action:
                conflicts.append((key, seen[key], action))
            seen[key] = action
    return conflicts


def _apply_bindings(app, bindings):
    for action, keys in bindings.items():
        app.cfg["keys"][action] = list(keys)
    app.keymap = app.cfg.keymap()


def _open_bindings(app):
    state = initialize(app)
    state["binding_draft"] = copy.deepcopy(app.cfg["keys"])
    _open(app, "bindings_editor")


def _bindings_key(app, key):
    state = initialize(app)
    draft = state["binding_draft"]
    actions = list(draft)
    action = actions[state["cursor"]]
    if state["test"]:
        state["test"] = False
        binding = next((name for name, keys in draft.items() if key in keys), "unbound")
        state["test_message"] = key + " -> " + binding + "; dialogs retain contextual editing keys"
        return
    if state["editing"]:
        if key == "esc":
            state["editing"] = False
        elif key == "enter":
            keys = [word for word in state["edit"].replace(",", " ").split() if word]
            if not _valid_keys(keys):
                app.fail("Use up to four distinct key names; empty disables this action")
                return
            candidate = dict(draft)
            candidate[action] = keys
            conflicts = _binding_conflicts(candidate, {})
            conflict = next((word for word in keys if word in RESERVED_KEYS), None)
            if conflicts or conflict:
                app.fail(("Key conflict: " + ", ".join(key + " belongs to " + other for key, other, _ in conflicts[:3])) if conflicts else "Key reserved for " + RESERVED_KEYS[conflict])
                return
            draft[action], state["editing"] = keys, False
        elif key == "backspace":
            state["edit"] = state["edit"][:-1]
        elif key == "space" or len(key) == 1 and key.isprintable():
            state["edit"] = (state["edit"] + (" " if key == "space" else key))[:128]
        return
    if key == "esc":
        app.mode = state["return_mode"]
    elif key in ("e", "enter"):
        state.update(editing=True, edit=" ".join(draft[action]))
    elif key == "t":
        state["test"] = True
    elif key == "D":
        from .config import DEFAULTS
        state["binding_draft"] = copy.deepcopy(DEFAULTS["keys"])
        state["cursor"] = min(state["cursor"], len(state["binding_draft"]) - 1)
    elif key == "a":
        if _binding_conflicts(draft, {}):
            app.fail("Resolve key conflicts before Apply")
            return
        _apply_bindings(app, draft)
        state["bindings"] = copy.deepcopy(draft)
        app.save()
        app.mode = state["return_mode"]
        app.say("Keybindings applied and saved; palette keys remain contextual")
    else:
        _move(state, key, len(actions))


FIELD_INFO = {
    "id": ("Job ID", "Scheduler identity; array task and step suffixes remain exact. No units.", "Unavailable if the source has no scheduler identity."),
    "name": ("Job name", "The complete scheduler job name. No units or sample window.", "Unavailable if the scheduler did not report a name."),
    "st": ("State", "The current scheduler state. ACCOUNTING means a job left the live queue and awaits confirmed accounting.", "Stale source status applies when the live queue has not refreshed."),
    "state": ("State", "The accounting terminal state, or ACCOUNTING while awaiting confirmation.", "Unavailable until accounting confirms the outcome."),
    "cpu": ("CPU rate", "Percent per allocated core over the last two valid live samples: change in CPU seconds / (wall seconds * allocated cores) * 100.", "Unknown with fewer than two samples, reset counters, or no allocation."),
    "ce": ("CPU efficiency", "Percent: total consumed CPU seconds / (elapsed seconds * allocated cores) * 100. Covers the elapsed run.", "Unknown without CPU time, elapsed time, or allocated cores."),
    "mem": ("Memory", "Peak resident memory in bytes, shown in a readable unit. This is an observed peak, not free node memory.", "Unknown when live/accounting RSS is unavailable."),
    "rss": ("Peak resident memory", "Reported peak resident set in bytes over the accounting interval/run.", "Unknown when the scheduler reports no usable peak."),
    "me": ("Memory efficiency", "Percent: observed peak resident memory / requested memory * 100.", "Unknown without a valid memory request or observation."),
    "gpu": ("GPU utilization", "Percent utilization observed by the existing GPU sampler over its device sample window.", "Unknown without a device sample; unsupported devices are not zero."),
    "cpus": ("Allocated CPUs", "Number of CPU cores allocated or requested for this job. Whole cores.", "Unknown when the scheduler did not provide an allocation."),
    "gpus": ("Allocated GPUs", "Number of GPUs allocated or requested across all job nodes.", "Unknown when the source does not provide GPU allocation."),
    "elapsed": ("Elapsed time", "Wall time in seconds from start to the latest sample or completion. Display may use days/hours/minutes.", "Pending jobs do not yet have measured runtime."),
    "left": ("Time remaining", "Seconds: configured time limit minus elapsed wall time; this is not an estimate of application progress.", "Unknown for missing or unlimited time limits."),
    "part": ("Partition", "The scheduler partition that accepted the job. No units.", "Unknown if the source did not report the partition."),
    "nodes": ("Nodes", "The full allocated node list. Some tables show a count instead; source identity remains available in the job inspector.", "Pending jobs may have no nodes yet."),
    "start": ("Start time", "Scheduler-reported timestamp. Pending forecasts are estimates and are labelled separately.", "Unknown before a confirmed start timestamp."),
    "end": ("End time", "Confirmed accounting completion timestamp for finished jobs; a live expected end is a scheduler limit estimate.", "Unknown before accounting reports completion."),
    "exit": ("Exit status", "Scheduler exit code and signal, shown as code:signal. 0:0 alone does not validate application outputs.", "Unknown until accounting reports an exit status."),
    "detail": ("Details", "Full node list for an allocated job or reported pending reason; no sample-derived formula.", "Unknown if the scheduler did not report a reason or allocation."),
    "tags": ("Tags", "User annotations from Raven state. No units or sampling.", "Empty means no tags were added."),
}
for _alias, _source in {"cpu%": "cpu", "eff": "ce", "mem%": "me", "gpu%": "gpu", "time": "elapsed", "where": "nodes", "info": "detail"}.items():
    FIELD_INFO[_alias] = FIELD_INFO[_source]
RESOURCE_INFO = {
    "sources": {"name": ("Source name", "The exact scheduler or plugin source identity. No units."),
                "state": ("Source state", "Off means disabled; error means the last request failed; pending means no successful sample; ok means the last request succeeded."),
                "every": ("Polling interval", "Effective seconds between this source's polling requests; failure backoff and in-flight work can delay updates further."),
                "last": ("Last successful sample", "Timestamp of the most recent successful source request; the table displays its age."),
                "latency": ("Request latency", "Milliseconds for the last completed source request."),
                "calls": ("Source calls", "Count of requests in this Raven session."),
                "errors": ("Source errors", "Count of failed requests in this Raven session."),
                "backoff": ("Failure backoff", "Additional seconds before retrying a failed source."),
                "error": ("Last error", "The complete diagnostic from the source; no truncation is applied to the copied value.")},
    "nodes": {"name": ("Node name", "The exact node identity reported by the scheduler."),
              "state": ("Node state", "The scheduler-reported node availability and allocation state; this is separate from a job outcome."),
              "cpus": ("Allocated CPUs", "Allocated cores / total configured node cores."),
              "load": ("Node CPU load", "The scheduler-reported CPU load observation. Its averaging window depends on the cluster's node reporting."),
              "loadpct": ("Node load percentage", "Percent: reported CPU load / configured node cores * 100."),
              "mem": ("Node memory used", "Bytes: (reported total MiB - free MiB) * 1048576. Unknown if free memory is missing or exceeds total."),
              "gres": ("Node resources", "Full configured generic resource declaration, including scheduler suffixes."),
              "gused": ("Allocated generic resources", "Full allocated generic resource declaration."),
              "gutil": ("Observed node GPU utilization", "Mean percentage of the current GPU device observations for your jobs on this node."),
              "jobs": ("Your jobs on this node", "Exact job IDs and full names for jobs allocated on the selected node.")},
    "cluster": {"name": ("Partition name", "The exact partition identity reported by the scheduler."),
                "avail": ("Partition availability", "The scheduler's reported up/down availability; availability alone does not guarantee admission."),
                "limit": ("Partition time limit", "The maximum scheduler wall-time limit; unlimited is not a measured duration."),
                "nodes": ("Partition nodes", "Count of configured nodes in this partition."),
                "nidle": ("Idle nodes", "Scheduler-reported idle node count."), "nalloc": ("Allocated nodes", "Scheduler-reported allocated node count."),
                "nother": ("Other nodes", "Scheduler-reported nodes in other states, including unavailable states."),
                "cidle": ("Idle cores", "Scheduler-reported unallocated core count."), "calloc": ("Allocated cores", "Scheduler-reported allocated core count."),
                "mine": ("Your running jobs", "Count of your live non-pending jobs in this partition."),
                "minep": ("Your pending jobs", "Count of your pending jobs in this partition."),
                "gpus": ("Partition GPUs", "Exact per-type counts of installed, allocated, free, and unavailable GPUs. Copied as JSON.")},
}


def _column(app, words):
    from .table_sort import TABLE_KEYS
    from . import table_ui
    table, key = app.tab, None
    from . import analysis_ui
    analysis = getattr(app, "analysis_state", {})
    if len(words) == 2 and words[0] == "metric":
        return "metric", words[1]
    if not words and app.mode == "analysis" and analysis.get("modal") == "chart" and analysis.get("metric"):
        return "metric", analysis["metric"]
    if len(words) == 2:
        table, key = words
    elif len(words) == 1:
        key = words[0]
    elif not words:
        try:
            from .table_tools import focused_column
            selected = focused_column(app)
            if selected and app.mode == "table_tools" and getattr(app, "table_tools_state", {}).get("modal") == "headers":
                table, key = selected
        except ImportError:
            pass
        key = key or "name"
    else:
        raise ValueError("explain/peek [TABLE] COLUMN")
    if table not in TABLE_KEYS:
        if key in FIELD_INFO:
            return table, key
        series, _, _ = analysis_ui.chart_data(app, app.store.snapshot())
        if key in series:
            return "metric", key
        raise ValueError("Choose a table column, for example: explain jobs ce")
    columns = table_ui.definitions(table)
    aliases = {"jobs": {"cpu": "cpu%", "ce": "eff", "me": "mem%", "elapsed": "time", "nodes": "where", "detail": "info"},
               "group": {"elapsed": "time", "nodes": "where", "detail": "info"}}
    key = aliases.get(table, {}).get(key, key)
    for column in columns:
        if key.casefold() in (column.key.casefold(), column.title.casefold()):
            key = column.key
            break
    if key not in TABLE_KEYS[table]:
        series, _, _ = analysis_ui.chart_data(app, app.store.snapshot())
        if key in series:
            return "metric", key
        raise ValueError("Unknown " + table + " column: " + key)
    return table, key


def raw_field(app, table, key):
    """Read the selected record by identity; never scrape clipped screen text."""
    from .model import Finished
    from .table_sort import history_value
    snap = app.store.snapshot()
    if table == "metric":
        from . import analysis_ui
        series, _, _ = analysis_ui.chart_data(app, snap)
        points = series.get(key)
        if points is None:
            raise ValueError("This metric has no published samples: " + key)
        state = app.analysis_state
        visible, _ = analysis_ui.viewport(points, state if state.get("metric") == key and state.get("modal") == "chart" else {})
        if not visible:
            return None
        index = max(0, min(int(state.get("cursor", 0)), len(visible) - 1)) if state.get("metric") == key and state.get("modal") == "chart" else len(visible) - 1
        return visible[index]["value"]
    if table in ("sources", "nodes", "cluster"):
        return _resource_field(app, table, key, snap)
    record = app.job_record(app.selected_id, snap) if app.selected_id else None
    if record is None:
        raise ValueError("Select a job first; peek keeps its exact identity")
    if key == "time":
        return str(record.elapsed) + "/" + str(getattr(record, "limit", ""))
    if key in ("flags", "info") and app.views_ref is not None:
        row = next((row for row in app.views_ref.job_rows(snap, app, app.actions) if row["id"] == record.id), None)
        if row is not None:
            return row.get(key)
    key = {"cpu%": "cpu", "eff": "ce", "mem%": "me", "gpu%": "gpu_util", "where": "nodes", "info": "detail"}.get(key, key)
    if key in ("id", "name", "start", "end", "exit"):
        return getattr(record, key, None)
    if key in ("user", "prio"):
        return getattr(record, "user" if key == "user" else "priority", None)
    if key in ("st", "state"):
        return record.state
    if key == "part":
        return record.partition
    if key == "nodes":
        return record.nodelist or (record.nodes if getattr(record, "pending", False) else "")
    if key == "detail":
        return getattr(record, "nodelist", "") or getattr(record, "reason", "")
    if key == "tags":
        return history_value(record, key, snap)
    if isinstance(record, Finished):
        value = history_value(record, key, snap)
        return value * 100 if key in ("ce", "me") and value is not None else value
    if key in ("cpus", "gpus") or key == "gpu":
        if key == "gpu":
            return record.gpu_text
        return getattr(record, key)
    if key == "elapsed":
        return record.elapsed_s
    if key == "left":
        return record.limit_s - record.elapsed_s if record.limit_s is not None and record.elapsed_s is not None else None
    live = snap.get("live", {}).get(record.id)
    if key in ("cpu", "ce", "mem", "rss", "me"):
        if live is None:
            return None
        if key == "cpu":
            value = live.rate if live.rate is not None else live.avg
            return value * 100 if value is not None else None
        if key == "ce":
            return live.avg * 100 if live.avg is not None else None
        if key in ("mem", "rss"):
            return live.rss
        return live.rss / record.mem_bytes * 100 if live.rss is not None and record.mem_bytes else None
    if key == "gpu_util":
        gpu = snap.get("gpu", {}).get(record.id)
        if gpu is None:
            return None
        samples = gpu if isinstance(gpu, list) else [gpu]
        values = [sample.util for sample in samples if isinstance(getattr(sample, "util", None), (int, float))
                  and not isinstance(sample.util, bool) and 0 <= sample.util <= 100]
        return sum(values) / len(values) if values else None
    raise ValueError("This field has no selected job value; select a job table or use the inspector")


def _resource_field(app, table, key, snap):
    try:
        from .table_tools import selected_record
        record = selected_record(app, table, snap)
    except ImportError:
        record = None
    if table == "sources" and record is None:
        names = app.ordered_source_ids()
        index = app.cursor.get("sources", 0)
        record = snap.get("health", {}).get(names[index]) if 0 <= index < len(names) else None
    if record is None:
        raise ValueError("Select a " + table + " row before you inspect its value")
    if table == "sources":
        from .refresh_rate import cadence
        values = {"name": record.name, "state": "off" if not record.enabled else "error" if record.error else "ok" if record.last_ok else "pending",
                  "every": cadence(app, record.name), "last": record.last_ok or None,
                  "latency": record.latency_ms if record.calls else None, "calls": record.calls, "errors": record.errors,
                  "backoff": record.backoff, "error": record.error}
    elif table == "nodes":
        jobs = [job for job in snap["jobs"] if record.name in job.hosts]
        gpu = [sample.util for job in jobs for sample in snap.get("gpu", {}).get(job.id) or []
               if sample.node == record.name and isinstance(sample.util, (int, float))
               and not isinstance(sample.util, bool) and 0 <= sample.util <= 100]
        alloc, load = getattr(record, "alloc", getattr(record, "cpus_alloc", 0)), getattr(record, "load", None)
        total, free = getattr(record, "mem_total", None), getattr(record, "mem_free", None)
        values = {"name": record.name, "state": record.state, "cpus": f"{alloc}/{record.cpus}", "load": load,
                  "loadpct": load / record.cpus * 100 if load is not None and record.cpus else None,
                  "mem": (total - free) * 1024**2 if free is not None and total and free <= total else None,
                  "gres": getattr(record, "gres", ""), "gused": getattr(record, "gres_used", ""), "gutil": sum(gpu) / len(gpu) if gpu else None,
                  "jobs": " ".join(job.id + "(" + job.name + ")" for job in jobs)}
    else:
        nodes, cpus = record.nodes_aiot.split("/"), record.cpus_aiot.split("/")
        values = {"name": record.name, "avail": record.avail, "limit": record.limit, "nodes": record.nodes,
                  "nidle": nodes[1] if len(nodes) > 1 else None, "nalloc": nodes[0] or None, "nother": nodes[2] if len(nodes) > 2 else None,
                  "cidle": cpus[1] if len(cpus) > 1 else None, "calloc": cpus[0] or None,
                  "mine": sum(job.partition == record.name and not job.pending for job in snap["jobs"]),
                  "minep": sum(job.partition == record.name and job.pending for job in snap["jobs"]), "gpus": json.dumps(record.gpus, ensure_ascii=False, sort_keys=True)}
    if key not in values:
        raise ValueError("No raw value exists for this field")
    return values[key]


def _field_open(app, table, key, peek=False):
    state = initialize(app)
    if peek:
        value = raw_field(app, table, key)
        text = "Unknown (no reported value)" if value is None else str(value)
        state.update(value=text[:MAX_VALUE], raw_value=text, field=table + "." + key, source_id=app.selected_id)
    elif table == "metric":
        from . import analysis_ui, chart_tools
        series, source, jid = analysis_ui.chart_data(app, app.store.snapshot())
        if key not in series:
            raise ValueError("This metric has no published samples: " + key)
        points = analysis_ui._points(series[key])
        preference = chart_tools.preference(app.analysis_state, key)
        formula = {"CPU per core (%)": FIELD_INFO["cpu"][1], "Memory (GB)": "Reported resident-set bytes / 1073741824, displayed in GiB.",
                   "GPU utilization (%)": "Mean percentage of the reported GPU device observations for this job."}.get(key,
                   "Reported application samples. Raven applies no inferred formula or unit conversion.")
        window = f"Published sample window: t={points[0]['t']!r} to t={points[-1]['t']!r}." if points else "No published sample window is available."
        missing = sum(point["value"] is None for point in points)
        state.update(field=preference.get("label", key), value="\n\n".join(("Metric ID: " + key + ". Declared unit: " + (preference.get("unit") or "not declared") + ".",
                     formula, "Scope: job " + str(jid or "not identified") + ". Source: " + str(source) + ".", window,
                     f"Samples: {len(points)}; unknown: {missing}. Missing and non-finite observations remain unknown; measured zero is valid.")))
    else:
        resource = RESOURCE_INFO.get(table, {}).get(key)
        info = (resource[0], resource[1], "Unknown when this source has no usable observation.") if resource else FIELD_INFO["gpus"] if key == "gpu" and table in ("jobs", "group") else FIELD_INFO.get(key, (key.upper(), "Reported source field; no derived formula is applied.", "Unknown when its source has no usable value."))
        from .refresh_rate import cadence, NATIVE_SOURCES
        from .metric_sampling import format_interval
        intervals = app.cfg["intervals"]
        source = "nodes" if table == "nodes" else "partitions" if table == "cluster" else "finished" if table in ("history", "recent") else "gpu" if key in ("gpu%", "gpu_util") else "live" if key in ("cpu", "cpu%", "ce", "eff", "mem", "mem%", "rss", "me") else "jobs"
        if table == "sources":
            names = app.ordered_source_ids()
            cursor = app.cursor.get("sources", 0)
            source = names[cursor] if 0 <= cursor < len(names) else "unknown"
        health = app.store.snapshot().get("health", {}).get(source)
        age = (clock.now() - health.last_ok) if health and health.last_ok else None
        freshness = f"Last successful source sample: {max(0, age):.1f} seconds ago." if age is not None else "No successful source sample has been recorded."
        interval = format_interval(cadence(app, source), ascii_=bool(app.cfg.get("ascii", False)))
        fetching = f"Source: {source}. Polling interval: {interval}; failure backoff is additional."
        if source not in NATIVE_SOURCES:
            fetching += f" Configured base: {intervals.get(source, 'unknown')} seconds."
        state.update(field=info[0], value="\n\n".join((info[1], info[2], fetching, freshness)))
    _open(app, "value_peek" if peek else "field_explanation")


def run_command(app, args):
    if not args or args[0] not in command_names():
        return False
    state, words = initialize(app), args[1:]
    try:
        if args[0] == "jump":
            state.update(query=" ".join(words)[:256], index_at=-1)
            _open(app, "jump_picker")
        elif args[0] == "location":
            if not words or words == ["list"]:
                _open(app, "locations_picker")
            elif len(words) == 2 and words[0] in ("save", "open", "delete") and _plain(words[1], 64) and words[1].strip():
                operation, name = words
                if operation == "save":
                    if name not in state["locations"] and len(state["locations"]) >= MAX_LOCATIONS:
                        raise ValueError("At most 50 locations can be saved; delete one first")
                    state["locations"][name] = _portable_location(app)
                    state["index_at"] = -1
                    app.save()
                    app.say("Saved location " + name)
                elif operation == "open":
                    _open_location(app, name)
                elif name in state["locations"]:
                    del state["locations"][name]
                    state["index_at"] = -1
                    app.save()
                    app.say("Deleted location " + name)
                else:
                    raise ValueError("Unknown saved location: " + name)
            else:
                raise ValueError("location [list|save NAME|open NAME|delete NAME]")
        elif args[0] == "settings":
            if words and words != ["reset"]:
                raise ValueError("settings [reset]")
            _open_settings(app)
            if words:
                _settings_key(app, "D")
        elif args[0] == "keybindings":
            if words and words != ["reset"]:
                raise ValueError("keybindings [reset]")
            _open_bindings(app)
            if words:
                _bindings_key(app, "D")
        else:
            table, key = _column(app, words)
            _field_open(app, table, key, args[0] == "peek")
    except (ValueError, TypeError) as exc:
        app.fail(str(exc))
    return True


def _move(state, key, total):
    cur, page = state["cursor"], state["page"]
    values = {"up": cur - 1, "down": cur + 1, "pgup": cur - page, "pgdn": cur + page, "home": 0, "end": total - 1}
    if key in values:
        state["cursor"] = max(0, min(max(0, total - 1), values[key]))


def handle_key(app, key):
    if app.mode == "main" and key == "ctrl-g":
        run_command(app, ["jump"])
        return True
    if app.mode not in MODES:
        return False
    state = initialize(app)
    if app.mode == "settings_editor":
        _settings_key(app, key)
    elif app.mode == "bindings_editor":
        _bindings_key(app, key)
    elif app.mode in ("value_peek", "field_explanation"):
        if key in ("esc", "q", "enter"):
            app.mode = state["return_mode"]
        elif key == "y" and app.mode == "value_peek":
            from . import clipboard
            result = clipboard.copy(state["raw_value"], app.state_dir, **clipboard.options(app))
            app.say(result)
        elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
            total, page = state.get("line_total", 1), state["page"]
            state["scroll"] = max(0, min(max(0, total - page), {"up": state["scroll"] - 1, "down": state["scroll"] + 1, "pgup": state["scroll"] - page,
                                  "pgdn": state["scroll"] + page, "home": 0, "end": total}[key]))
    else:
        entries = jump_matches(app) if app.mode == "jump_picker" else [{"value": name} for name in state["locations"]]
        if key == "esc":
            app.mode = state["return_mode"]
        elif key == "enter" and entries:
            entry = entries[min(state["cursor"], len(entries) - 1)]
            if app.mode == "jump_picker":
                _activate_jump(app, entry)
            else:
                _open_location(app, entry["value"])
        elif app.mode == "jump_picker" and key in ("backspace", "space") or app.mode == "jump_picker" and len(key) == 1 and key.isprintable():
            state["query"] = state["query"][:-1] if key == "backspace" else (state["query"] + (" " if key == "space" else key))[:256]
            state["cursor"], state["top"] = 0, 0
        else:
            _move(state, key, len(entries))
    return True


def handle_mouse(app, y, x, button="left", shift=False):
    if app.mode not in MODES:
        return False
    if button == "left":
        state = initialize(app)
        index = next((index for row, index in state["hits"] if row == y), None)
        if index is not None:
            state["cursor"] = index
    return True


def _list_overlay(views, app, width, height, title, rows, prefix, hints):
    from . import modal_scrollbars as B
    state = initialize(app)
    page = max(1, height - 11)
    cursor = max(0, min(state["cursor"], max(0, len(rows) - 1)))
    top = max(0, min(state["top"], max(0, len(rows) - page)))
    if cursor < top:
        top = cursor
    elif cursor >= top + page:
        top = cursor - page + 1
    context = (app.mode, state["query"], state["editing"], state["test"])
    logical, top = B.window(app, "modal:navigation-list", top, len(rows), page,
                            context=context, focus=cursor)
    state.update(cursor=cursor, top=logical, page=page)
    content = [[(" " + clean(prefix, views.g.ascii), "cyan")], [("", "")]]
    for index, (value, description) in enumerate(rows[top:top + page], top):
        style = "sel" if index == cursor else ""
        marker = "> " if views.g.ascii else "› "
        content.append([((marker if index == cursor else "  ") + clean(value, views.g.ascii, limit=4096), style),
                        ("  " + clean(description, views.g.ascii, limit=4096), style or "dim")])
    if not rows:
        content.append([(" No results. Edit the query or save a location first.", "yellow")])
    content += [[("", "")]] + [[(" " + clean(hint, views.g.ascii), "dim")] for hint in hints[:3]]
    result = L.box(views.g, content, width, height, title)
    state["hits"] = [(result[3 + offset][0], top + offset) for offset in range(min(page, len(rows) - top)) if 3 + offset < len(result) - 1]
    return B.boxed(app, "modal:navigation-list", result, start=2, count=len(rows), page=page,
                   target=logical, painted=top, setter=lambda value: state.update(top=value), context=context)


def overlay(views, snap, app, width, height):
    if app.mode not in MODES:
        return None
    state = initialize(app)
    if app.mode == "jump_picker":
        entries = jump_matches(app)
        rows = [(entry["value"], entry["description"]) for entry in entries]
        return _list_overlay(views, app, width, height, "Jump to a destination", rows, "Search: " + (state["query"] or "type a name, ID, or kind"),
                             [f"{len(rows)} cached matches; the index refreshes at most once per second", "Up/Down choose · Enter opens · Esc closes"])
    if app.mode == "locations_picker":
        rows = [(name, value["tab"] + " · " + str(value.get("selected_id") or value.get("log_job") or "")) for name, value in state["locations"].items()]
        return _list_overlay(views, app, width, height, "Saved locations", rows, "Exact destination bookmarks",
                             [":location save NAME · :location delete NAME", "Up/Down choose · Enter opens · Esc closes"])
    if app.mode == "settings_editor":
        keys = _setting_keys(app)
        locked = _locked_settings(app)
        rows = [("Telemetry updates" if key == TELEMETRY_SETTING else SETTING_LABELS.get(key, key.replace("intervals.", "Sample interval · ")),
                 (f"{state['draft'][key]:.3g}" if key == "intervals.jobs" else str(state["draft"][key]))
                 + (" [launch option]" if key in locked else "")) for key in keys]
        field = keys[min(state["cursor"], len(keys) - 1)]
        explanation = SETTING_HELP.get(field, "Seconds between existing source samples; from 1 to 3600 seconds.")
        if field == TELEMETRY_SETTING:
            explanation = "Enter opens live telemetry profiles and Custom settings. The toolbar slider changes queue polling."
        if field in locked:
            explanation = "This value is fixed by the launch options or environment. Restart Raven to change it."
        return _list_overlay(views, app, width, height, "Settings · live preview", rows, explanation,
                             ["Left/Right changes · Space toggles · D previews defaults", "Enter applies and saves · Esc restores all previous settings"])
    if app.mode == "bindings_editor":
        rows = [(action, " ".join(keys) or "disabled") for action, keys in state["binding_draft"].items()]
        prefix = "Keys: " + state["edit"] if state["editing"] else "Press a key to test it without executing its action" if state["test"] else state["test_message"] or "Main-page bindings; dialogs retain editing and review controls"
        return _list_overlay(views, app, width, height, "Keybindings", rows, prefix,
                             ["e/Enter edits · t tests · D restores draft defaults", "a applies and saves · Esc closes · conflicts must be resolved"])
    from .command_ui import _wrap_exact
    lines = []
    for paragraph in state["value"].splitlines() or [""]:
        lines.extend(_wrap_exact(clean(paragraph, views.g.ascii, limit=MAX_VALUE), max(1, width - 10)))
    page = max(1, height - 8)
    state["line_total"], state["page"] = len(lines), page
    state["scroll"] = max(0, min(state["scroll"], max(0, len(lines) - page)))
    from . import modal_scrollbars as B
    context = (app.mode, state["field"], state["value"], width)
    logical, painted = B.window(app, "modal:full-value", state["scroll"], len(lines), page, context=context)
    state["scroll"] = logical
    content = [[(" " + clean(state["field"], views.g.ascii), "cyan+bold")], [("", "")]]
    content += [[(" " + line, "")] for line in lines[painted:painted + page]]
    content += [[("", "")], [(" Up/Down/PgUp/PgDn scroll · y copies the complete raw value · Esc closes" if app.mode == "value_peek" else " Up/Down/PgUp/PgDn scroll · Esc closes", "dim")]]
    result = L.box(views.g, content, width, height, "Full value" if app.mode == "value_peek" else "Field explanation")
    return B.boxed(app, "modal:full-value", result, start=2, count=len(lines), page=page,
                   target=logical, painted=painted, setter=lambda value: state.update(scroll=value), context=context)
