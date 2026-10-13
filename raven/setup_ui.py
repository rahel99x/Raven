"""Internal setup pages and an explicit allocation preferences editor.

Pages, hover, and editing use cached data. Account discovery and configuration
writes run in the shared background lane. Saving never requests an allocation.
"""
from __future__ import annotations

import copy
import re

from . import layout as L
from .control_rows import place_hits
from .log_text import display_text

FIELDS = (
    ("account", "Billing account", "Slurm default"),
    ("partition", "Partition", "Slurm default"),
    ("qos", "Quality of service (QOS)", "Slurm default"),
    ("cpus_per_task", "CPUs per task", "2"),
    ("memory_mib", "Memory (MiB)", "4096"),
    ("walltime_seconds", "Time limit (seconds)", "3600"),
    ("nodes", "Nodes", "1"),
    ("tasks", "Tasks", "1"),
    ("gpus", "GPUs", "0"),
)
NUMERIC_FIELDS = frozenset(key for key, _, _ in FIELDS[3:])
MAX_ACCOUNTS = 1024


def initialize(app):
    state = getattr(app, "setup_ui_state", None)
    if not isinstance(state, dict):
        state = dict(page="overview", generation=0, revision=0, cursor=0, top=0,
                     visible=1, return_mode="main", editing=None, picker=False,
                     form={}, form_scope=None, dirty=set(), discovery=None, service=None, service_scope=None,
                     busy="", message="", control_hits=[], painted=None)
        app.setup_ui_state = state
    return state


def command_names():
    return ["setup"]


def _text(value, limit=2048):
    return display_text(str(value))[:limit]


def _wrap(text, width):
    """Keep prose words intact while still fitting long paths and JSON."""
    rows, line, used = [], "", 0
    for chunk in re.findall(r"\s+|\S+", text):
        size = L.vlen(chunk)
        if line and not chunk.isspace() and size <= width and used + size > width:
            rows.append(line.rstrip())
            line, used = "", 0
        for char in chunk:
            size = L.vlen(char)
            if line and used + size > width:
                rows.append(line.rstrip())
                line, used = "", 0
            if not line and char.isspace():
                continue
            line += char
            used += size
    return rows + ([line] if line else []) or [""]


def _pending_review(app):
    return (getattr(app, "mode", "main") == "confirm" or
            bool(getattr(app, "execution_state", {}).get("pending_action")) or
            bool(getattr(app, "raven_ui_state", {}).get("review")))


def _scope(app):
    slurm = getattr(getattr(app, "actions", None), "slurm", None)
    return (id(getattr(app, "raven_resolved", None)), id(getattr(slurm, "b", None)),
            id(getattr(app, "raven_allocation_manager", None)),
            str(getattr(app, "profile_name", "")))


def _standalone(app):
    return getattr(app, "cfg", {}).get("raven", {}).get("allocation", {}).get("adapter") == "desktop"


def _frame(app):
    state = initialize(app)
    return (getattr(app, "mode", "main"), getattr(app, "tab", ""),
            getattr(app, "width", None), getattr(app, "height", None),
            _scope(app), state["page"], state["generation"], state["revision"],
            state["editing"], state["picker"], state["top"])


def _invalidate(state):
    state["revision"] += 1
    state.update(control_hits=[], painted=None)


def _say(app, message, *, error=False):
    state = initialize(app)
    state["message"] = _text(message)
    _invalidate(state)
    callback = getattr(app, "fail" if error else "say", None)
    if callable(callback):
        callback(state["message"])


def _service(app):
    state = initialize(app)
    scope = _scope(app)
    if state["service_scope"] != scope:
        state.update(service=None, service_scope=scope, discovery=None)
    if state["service"] is None:
        resolved = getattr(app, "raven_resolved", None)
        if resolved is None:
            raise ValueError("Allocation settings need a managed Raven site profile. Launch ravenl with your configured site.")
        from .setup_config import AllocationSetup
        slurm = getattr(getattr(app, "actions", None), "slurm", None)
        state["service"] = AllocationSetup(resolved, getattr(slurm, "b", None),
            manager=getattr(app, "raven_allocation_manager", None))
    return state["service"]


def _background(app, label, read, completed):
    state = initialize(app)
    if state["busy"]:
        _say(app, "Setup is already " + state["busy"].lower() + ".")
        return False
    hub = getattr(app, "research", None)
    if hub is None:
        _say(app, "The background reader is unavailable. Reopen Setup in an interactive Raven session.", error=True)
        return False
    state["busy"] = label
    _invalidate(state)

    def publish(value):
        state["busy"] = ""
        _invalidate(state)
        completed(value)

    if not hub.start_task(read, publish):
        state["busy"] = ""
        _say(app, "The shared background reader is busy. Try this action after it finishes.", error=True)
        return False
    return True


def _discover(app, *, refresh=False):
    state = initialize(app)
    if getattr(app, "replay", None) or getattr(getattr(app, "research", None), "demo", False):
        _say(app, "Account discovery is unavailable in demo or replay sessions.")
        return
    try:
        service = _service(app)
    except (ValueError, TypeError) as exc:
        _say(app, exc, error=True)
        return
    scope = _scope(app)

    def read():
        return service.discover(refresh=refresh)

    def completed(value):
        if _scope(app) != scope:
            _say(app, "Connection changed during discovery. Reopen allocation settings.", error=True)
        elif isinstance(value, Exception):
            _say(app, "Account discovery failed: " + str(value), error=True)
        else:
            state["discovery"] = value
            if state["page"] == "allocation":
                _say(app, "Account discovery finished. Select an association or keep Slurm's default account.")

    _background(app, "Discovering accounts", read, completed)


def _save(app):
    state = initialize(app)
    if getattr(app, "replay", None) or getattr(getattr(app, "research", None), "demo", False):
        _say(app, "Allocation settings cannot be saved in demo or replay sessions.", error=True)
        return
    if state["editing"] is not None:
        _say(app, "Finish editing the field with Enter before saving.", error=True)
        return
    if not state["dirty"]:
        _say(app, "No allocation settings have changed.")
        return
    try:
        service = _service(app)
        changes = {key: int(state["form"][key]) if key in NUMERIC_FIELDS else state["form"][key].strip()
                   for key in state["dirty"]}
    except (ValueError, TypeError) as exc:
        _say(app, "Cannot save allocation settings: " + str(exc), error=True)
        return
    scope = _scope(app)

    def read():
        return service.save(service.stage(changes))

    def completed(value):
        if isinstance(value, Exception):
            _say(app, "Allocation settings were not saved: " + str(value), error=True)
            return
        if _scope(app) != scope:
            _say(app, "Saved the original profile. The active connection changed; reopen its settings before requesting compute.")
            return
        allocation = copy.deepcopy(value["allocation"])
        if callable(getattr(app.cfg, "set", None)):
            app.cfg.set("raven.allocation", allocation)
        else:
            app.cfg["raven"]["allocation"] = allocation
        app.raven_resolved = value["resolved"]
        if hasattr(app.cfg, "__dict__"):
            app.cfg.raven_resolved = value["resolved"]
        state.update(form={key: str(allocation.get(key, "")) for key, _, _ in FIELDS},
                     form_scope=_scope(app), dirty=set(), service=None, service_scope=None)
        warnings = " ".join(_text(item) for item in value.get("warnings", ()))
        _say(app, "Saved allocation settings. No allocation was requested. "
             "Account and Budget monitoring retain their current account until Raven restarts. " + warnings)

    _background(app, "Saving settings", read, completed)


def _open(app, page):
    state = initialize(app)
    if _pending_review(app):
        _say(app, "Close the pending job review first.", error=True)
        return
    from .setup_content import topic
    try:
        if page != "allocation":
            topic(page)
    except ValueError:
        _say(app, "Unknown Setup page. Use setup research VIEW or setup analytics VIEW.", error=True)
        return
    if getattr(app, "mode", "main") != "setup":
        state["return_mode"] = "main" if getattr(app, "mode", "main") == "palette" else getattr(app, "mode", "main")
        from . import terminal_ui
        if terminal_ui.focused(app):
            terminal_ui.blur(app)
        from .pointer_routing import begin_gesture
        begin_gesture(app)
    if state["page"] != page:
        state["message"] = ""
    state.update(page=page, cursor=0, top=0, editing=None, picker=False)
    state["generation"] += 1
    _invalidate(state)
    app.mode = "setup"
    if page == "allocation":
        # Preserve an unfinished form while browsing other setup pages.
        if not state["form"] or state["form_scope"] != _scope(app):
            allocation = getattr(app, "cfg", {}).get("raven", {}).get("allocation", {})
            state.update(form={key: str(allocation.get(key, "")) for key, _, _ in FIELDS},
                         form_scope=_scope(app), dirty=set(), discovery=None)
        if state["discovery"] is None and not state["busy"]:
            _discover(app)


def _close(app):
    state = initialize(app)
    state["editing"] = None
    _invalidate(state)
    app.mode = state["return_mode"] if state["return_mode"] != "setup" else "main"


def _accounts(state):
    discovery = state["discovery"] or {}
    return [item for item in discovery.get("accounts", ())[:MAX_ACCOUNTS]
            if isinstance(item, dict) and isinstance(item.get("account"), str)]


def _choose_account(app, index):
    state = initialize(app)
    if state["busy"]:
        _say(app, "Wait for Setup's background action before changing an account.")
        return
    if index == "default":
        account = ""
    else:
        try:
            number = int(index)
            if number < 0:
                raise ValueError("negative index")
            association = _accounts(state)[number]
        except (ValueError, IndexError):
            _say(app, "The account list changed. Choose from the current list.", error=True)
            return
        cluster = (state["discovery"] or {}).get("cluster")
        if cluster and association.get("cluster") and association["cluster"] != cluster:
            _say(app, "This association belongs to " + association["cluster"] +
                 ". Switch to that configured site before selecting it.", error=True)
            return
        account = association["account"]
        # A partition-specific association is a linked choice, not a claim
        # that a same-named account works on every partition.
        if association.get("partition"):
            state["form"]["partition"] = association["partition"]
            state["dirty"].add("partition")
        qualities = association.get("qos", ())
        if state["form"].get("qos") and state["form"]["qos"] not in qualities:
            state["form"]["qos"] = association.get("default_qos", "")
            state["dirty"].add("qos")
    state["form"]["account"] = account
    state["dirty"].add("account")
    state.update(picker=False, cursor=0, top=0)
    _invalidate(state)


def run_command(app, args):
    if not args or args[0] != "setup":
        return False
    state = initialize(app)
    parts = list(args[1:]) or ["overview"]
    action = parts[0]
    if parts == ["updates"]:
        from .telemetry_settings_ui import open_settings
        open_settings(app)
    elif action in ("research", "analytics") and len(parts) == 2:
        _open(app, action + "." + parts[1])
    elif action in ("overview", "allocation", "project", "exports", "telemetry", "installation") and len(parts) == 1:
        _open(app, action)
    elif getattr(app, "mode", "") != "setup":
        _say(app, "Open Setup before using its controls.", error=True)
    elif action == "close" and len(parts) == 1:
        _close(app)
    elif action == "accounts" and len(parts) == 1:
        state.update(picker=True, editing=None, cursor=0, top=0)
        _invalidate(state)
    elif action == "form" and len(parts) == 1:
        state.update(picker=False, cursor=0, top=0)
        _invalidate(state)
    elif action == "account" and len(parts) == 2 and state["picker"]:
        _choose_account(app, parts[1])
    elif action == "refresh" and len(parts) == 1:
        _discover(app, refresh=True)
    elif action == "save" and len(parts) == 1:
        _save(app)
    elif action == "reset" and len(parts) == 1 and not state["busy"]:
        allocation = getattr(app, "cfg", {}).get("raven", {}).get("allocation", {})
        state.update(form={key: str(allocation.get(key, "")) for key, _, _ in FIELDS},
                     dirty=set(), editing=None)
        _say(app, "Restored the current saved allocation settings.")
    elif action == "edit" and len(parts) == 2 and parts[1] in dict((key, label) for key, label, _ in FIELDS) and not state["busy"] and not _standalone(app):
        state.update(editing=(parts[1], state["form"].get(parts[1], "")), picker=False)
        _invalidate(state)
    elif action == "action" and len(parts) == 2:
        choices = _actions(app)
        try:
            number = int(parts[1])
            if number < 0:
                raise ValueError("negative action index")
            command, prompt = choices[number][1:]
        except (ValueError, IndexError):
            _say(app, "The setup page changed. Choose a current action.", error=True)
            return True
        if command in ("raven request", "raven reuse") and (state["dirty"] or state["busy"]):
            _say(app, "Save or discard allocation edits and wait for Setup to finish before reviewing compute.", error=True)
            return True
        if prompt:
            from .command_ui import open_palette
            open_palette(app, command)
        else:
            app.run_command(command)
            # Core page switches can intentionally leave a caller's modal
            # untouched. A Setup navigation button must expose that page;
            # diagnostic and review handlers retain whichever modal they open.
            if (command.split()[0] in ("tab", "view", "workspace", "compare") and
                    getattr(app, "command_ok", True) and app.mode == "setup"):
                app.mode = "main"
                _invalidate(state)
    else:
        _say(app, "Use setup [allocation|project|telemetry|installation|research VIEW|analytics VIEW].", error=True)
    return True


def _actions(app):
    state = initialize(app)
    if state["page"] == "allocation":
        if _standalone(app):
            return [("Compute controls: inspect / release", "raven status", False),
                    ("Review local Heavy request", "raven request", False),
                    ("Terminal and sampler preferences", "settings", False)]
        if state["picker"]:
            return [("Back to allocation settings", "setup form", False),
                    ("Use Slurm's default account", "setup account default", False),
                    ("Enter an account manually", "setup edit account", False)]
        return [("Choose personal / lab association", "setup accounts", False),
                ("Refresh account discovery", "setup refresh", False),
                ("Save settings", "setup save", False),
                ("Discard edits", "setup reset", False),
                ("Compute controls: inspect / release", "raven status", False),
                ("Review Heavy allocation request", "raven request", False),
                ("Reuse an existing allocation", "raven reuse", False)]
    from .setup_content import topic
    return list(topic(state["page"]).get("actions", ()))


def _allocation_lines(app):
    state = initialize(app)
    discovery = state["discovery"] or {}
    raven = getattr(app, "cfg", {}).get("raven", {})
    standalone = _standalone(app)
    lines = ["Changes affect Raven Heavy's worker allocation, not scientific job submissions."]
    if raven.get("profile"):
        lines.append("Selected profile: " + _text(raven["profile"]))
    source = getattr(getattr(app, "raven_resolved", None), "source", "")
    if source:
        lines.append("Configuration file: " + _text(source))
    if standalone:
        lines += ["Standalone desktop: no Slurm billing account is needed; local CPU and memory limits still apply.",
                  "The local worker budget comes from the site configuration. See Installation for configuration and environment checks."]
    else:
        lines += ["Save writes the selected site profile. Request/reuse opens a separate allocation review.",
                  "Personal and lab accounts are Slurm billing associations, separate from your login name.",
                  "Keep the account empty for Slurm's configured default, including personal desktop Slurm.",
                  "A default account does not bypass scheduler limits. Ask the administrator about permitted resources."]
    if discovery:
        lines += ["Detected login: " + _text(discovery.get("username") or "unavailable"),
                  "Cluster: " + _text(discovery.get("cluster") or "unavailable")]
        if discovery.get("partitions"):
            lines.append("Partitions: " + ", ".join(_text(item, 96) for item in discovery["partitions"][:64]))
        lines += ["Discovery: " + _text(error) for error in discovery.get("errors", ())[:16]]
        if not discovery.get("accounts") and not standalone:
            lines.append("No associations were visible. Keep the default or enter an administrator-provided account.")
    if state["dirty"]:
        lines.append("Unsaved fields: " + ", ".join(sorted(state["dirty"])))
    origins = getattr(getattr(app, "raven_resolved", None), "origins", {})
    overrides = [label for key, label, _ in FIELDS if origins.get("allocation." + key) == "CLI"]
    if overrides:
        lines.append("Command-line overrides: " + ", ".join(overrides) + ". Relaunch without those flags to change them here.")
    lines.append("Release an active Heavy worker before saving its allocation settings.")
    return lines


def _document(app):
    state = initialize(app)
    result = []

    def action(label, command):
        result.append((label, command))

    action("Setup overview", "setup overview")
    action("Close Setup", "setup close")
    if state["busy"]:
        result.append((state["busy"] + " in the background...", None))
    if state["message"]:
        result.append((state["message"], None))
    for index, (label, _, _) in enumerate(_actions(app)):
        action(label, "setup action " + str(index))
    if state["page"] == "allocation":
        if state["picker"]:
            for index, association in enumerate(_accounts(state)):
                label = association["account"]
                details = []
                for key in ("cluster", "partition"):
                    if association.get(key):
                        details.append(key + " " + association[key])
                if association.get("qos"):
                    details.append("QOS " + ",".join(association["qos"]))
                if association.get("default"):
                    details.append("default association")
                cluster = (state["discovery"] or {}).get("cluster")
                if cluster and association.get("cluster") and association["cluster"] != cluster:
                    details.append("switch site to select")
                action(label + (" — " + "; ".join(details) if details else ""), "setup account " + str(index))
            result += [(line, None) for line in _allocation_lines(app)]
        elif not _standalone(app):
            for key, label, empty in FIELDS:
                value = state["form"].get(key, "")
                if state["editing"] is not None and state["editing"][0] == key:
                    value = state["editing"][1] + "_"
                action(label + ": " + (value or empty) + (" *" if key in state["dirty"] else ""), "setup edit " + key)
            result += [(line, None) for line in _allocation_lines(app)]
        else:
            result += [(line, None) for line in _allocation_lines(app)]
    else:
        from .setup_content import topic
        result += [(line, None) for line in topic(state["page"]).get("lines", ())]
    return result


def handle_key(app, key):
    if getattr(app, "mode", "") != "setup":
        return False
    state = initialize(app)
    if state["editing"] is not None:
        field, value = state["editing"]
        if key == "esc":
            state["editing"] = None
        elif key == "enter":
            state["form"][field] = value.strip()
            state["dirty"].add(field)
            state["editing"] = None
        elif key in ("backspace", "delete"):
            state["editing"] = (field, value[:-1])
        elif key in ("ctrl-u", "ctrl-a"):
            state["editing"] = (field, "")
        elif len(key) == 1 and key.isprintable() and len(value) < 256:
            state["editing"] = (field, value + key)
        _invalidate(state)
        return True
    choices = [(index, command) for index, (_, command) in enumerate(_document(app)) if command]
    state["cursor"] = max(0, min(state["cursor"], len(choices)-1))
    if key in ("esc", "q"):
        if state["picker"]:
            run_command(app, ["setup", "form"])
        else:
            _close(app)
    elif key in ("up", "down", "left", "right", "tab", "btab"):
        state["cursor"] = (state["cursor"] + (-1 if key in ("up", "left", "btab") else 1)) % len(choices)
        state["follow_cursor"] = True
        _invalidate(state)
    elif key in ("pgup", "pgdn", "home", "end"):
        state["top"] = max(0, {"pgup": state["top"] - state["visible"],
                              "pgdn": state["top"] + state["visible"], "home": 0, "end": 10**6}[key])
        state["follow_cursor"] = False
        _invalidate(state)
    elif key in ("enter", "space"):
        run_command(app, choices[state["cursor"]][1].split())
    elif key == ":":
        return False
    return True


def handle_mouse(app, y, x, button="left", shift=False):
    if getattr(app, "mode", "") != "setup":
        return False
    state = initialize(app)
    if button in ("wheel-up", "wheel-down"):
        state["top"] = max(0, state["top"] + (-3 if button == "wheel-up" else 3))
        state["follow_cursor"] = False
        _invalidate(state)
    elif button in ("left", "press") and state["painted"] == _frame(app) and state["editing"] is None:
        for row, _, item in state["control_hits"]:
            if row == y and item["left"] <= x < item["right"]:
                run_command(app, item["action"][1].split())
                break
    return True


def overlay(views, snap, app, width, height):
    if getattr(app, "mode", "") != "setup":
        return None
    state = initialize(app)
    state.update(control_hits=[], painted=None)
    from .setup_content import topic
    title = "Heavy allocation settings" if state["page"] == "allocation" else topic(state["page"])["title"]
    inner = max(1, width-6)
    document, hits, choice, selected_row = [], [], 0, 0
    for text, command in _document(app):
        text = _text(text)
        if views.g.ascii:
            text = text.encode("ascii", "replace").decode("ascii")
        if command:
            selected = choice == state["cursor"]
            if selected:
                selected_row = len(document)
            label = ("> " if selected else "  ") + text
            label = L.cut(label, inner, views.g.ascii)
            hits.append((len(document), "control", {"id": "setup:" + str(choice), "label": text,
                "left": 0, "right": L.vlen(label), "action": ("command", command), "group": "setup"}))
            document.append([(label, "sel+bold" if selected else "cyan+bg:surface")])
            choice += 1
        else:
            document.extend([(line, "text")] for line in _wrap(text, inner))
    capacity = max(0, height-5)
    state["visible"] = max(1, capacity)
    if state.pop("follow_cursor", False):
        if selected_row < state["top"]:
            state["top"] = selected_row
        elif selected_row >= state["top"] + capacity:
            state["top"] = selected_row-capacity+1
    state["top"] = max(0, min(state["top"], max(0, len(document)-capacity)))
    rows = document[state["top"]:state["top"]+capacity]
    footer = ("Enter keeps field; Esc cancels; Ctrl-U clears" if state["editing"] is not None else
              f"{state['top']+1}-{min(len(document), state['top']+capacity)}/{len(document)}  "
              "Tab: choose | Enter: open | PgUp/PgDn: scroll | Esc: back")
    if height >= 5:
        rows.append([(L.cut(footer, inner, views.g.ascii), "muted")])
    rendered = L.box(views.g, rows, width, height, "Setup: " + title, min_width=20)
    state["control_hits"] = place_hits(hits, rendered[1:-2] if height >= 5 else rendered[1:-1], offset=state["top"])
    state["painted"] = _frame(app)
    return rendered
