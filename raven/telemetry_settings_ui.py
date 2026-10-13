"""Live Heavy sampling controls with a bounded, keyboard-accessible editor.

Rendering reads cached settings only. Presets apply on the UI thread; explicit
configuration writes use the existing background lane. A draft never changes
sampling until the user applies it, even while its intensity colour updates.
"""
from __future__ import annotations

from . import layout as L
from .control_rows import buttons, place_hits
from .log_text import display_text
from .palette import gradient_style
from .setup_ui import _wrap

MODE = "telemetry_settings"
COMMAND = "telemetrysettings"


def initialize(app):
    state = getattr(app, "telemetry_settings_ui_state", None)
    if not isinstance(state, dict):
        state = dict(generation=0, revision=0, return_mode="main", page="presets",
                     cursor=0, top=0, visible=1, follow_cursor=True, editing=None,
                     draft={}, dirty=False, busy=False, message="", error="",
                     control_hits=[], painted=None)
        app.telemetry_settings_ui_state = state
    return state


def command_names():
    return [COMMAND]


def _model():
    from . import telemetry_settings
    return telemetry_settings


def _invalidate(state):
    state["revision"] += 1
    state.update(control_hits=[], painted=None)


def _frame(app):
    state = initialize(app)
    return (getattr(app, "mode", ""), getattr(app, "width", None), getattr(app, "height", None),
            state["generation"], state["revision"], state["page"], state["editing"], state["top"])


def _say(app, message, error=False):
    state = initialize(app)
    state["error" if error else "message"] = display_text(str(message))
    state["message" if error else "error"] = ""
    _invalidate(state)
    callback = getattr(app, "fail" if error else "say", None)
    if callable(callback):
        callback(str(message))


def _format(value):
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def _draft(snapshot):
    return {field["key"]: _format(snapshot["custom"].get(field["key"], field["default"]))
            for field in _model().FIELDS}


def open_settings(app):
    from .setup_ui import _pending_review
    if _pending_review(app):
        _say(app, "Close the pending job review before changing sampling settings.", True)
        return False
    state = initialize(app)
    if state["busy"]:
        _say(app, "Telemetry settings are still being saved.")
        return False
    try:
        snapshot = _model().settings_snapshot(app)
    except (ValueError, TypeError) as exc:
        _say(app, exc, True)
        return False
    origin = getattr(app, "mode", "main")
    if origin != MODE:
        state["return_mode"] = "main" if origin == "palette" else origin
        from . import terminal_ui
        if terminal_ui.focused(app):
            terminal_ui.blur(app)
        from .pointer_routing import begin_gesture
        begin_gesture(app)
    state.update(generation=state["generation"] + 1, page="presets", cursor=0,
                 top=0, editing=None, draft=_draft(snapshot), dirty=False,
                 message="", error="", follow_cursor=True)
    _invalidate(state)
    app.mode = MODE
    return True


def _close(app):
    state = initialize(app)
    state.update(editing=None, dirty=False)
    _invalidate(state)
    app.mode = state["return_mode"] if state["return_mode"] != MODE else "main"


def _parsed(state):
    values = {}
    for field in _model().FIELDS:
        raw = state["draft"][field["key"]]
        try:
            number = float(raw)
            if field["type"] == "int":
                if not number.is_integer():
                    raise ValueError("expected an integer")
                number = int(number)
        except (ValueError, OverflowError):
            raise ValueError(field["label"] + " needs a " + ("whole number" if field["type"] == "int" else "number")) from None
        values[field["key"]] = number
    validated = _model().validate_custom(values)
    return values if validated is None else validated


def _apply(app, preset, *, draft=False):
    state = initialize(app)
    if state["busy"]:
        _say(app, "Wait for the configuration save before changing the active profile.")
        return False
    try:
        custom = _parsed(state) if draft else None
        result = _model().apply_settings(app, preset, custom=custom, persist=False)
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        _say(app, exc, True)
        return False
    if preset != "custom":
        state.update(page="presets", top=0, cursor=0, follow_cursor=True)
    if draft:
        state.update(dirty=False, editing=None, draft=_draft(_model().settings_snapshot(app)))
    _say(app, (result or {}).get("message") or preset.title() + " sampling applied live. No restart needed.")
    return True


def _save(app):
    state = initialize(app)
    if state["busy"]:
        return
    if state["editing"] is not None:
        _say(app, "Press Enter to keep the current field, or Escape to cancel it.", True)
        return
    if (state["dirty"] and state["page"] == "custom" and
            _model().settings_snapshot(app)["preset"] == "custom" and
            not _apply(app, "custom", draft=True)):
        return
    hub = getattr(app, "research", None)
    if hub is None or not callable(getattr(hub, "start_task", None)):
        _say(app, "Configuration saving needs the background reader. Live changes remain active for this session.", True)
        return
    try:
        request = _model().prepare_save(app)
    except (ValueError, TypeError, OSError) as exc:
        _say(app, exc, True)
        return
    state["busy"] = True
    _invalidate(state)

    def work():
        return _model().save_settings(request)

    def completed(value):
        state["busy"] = False
        if isinstance(value, Exception):
            _say(app, "Live settings remain active, but the configuration could not be saved: " + str(value), True)
        else:
            _model().saved(app, request, value)
            warnings = " ".join(str(warning) for warning in (value or {}).get("warnings", ()))
            _say(app, "Saved telemetry settings for later launches. Current sampling continues without a restart." +
                 (" " + warnings if warnings else ""))

    if not hub.start_task(work, completed):
        state["busy"] = False
        _say(app, "The background reader is busy. Live settings are active; try Save settings again when it finishes.", True)


def run_command(app, args):
    if not args or args[0] != COMMAND:
        return False
    parts = list(args[1:])
    if not parts:
        open_settings(app)
        return True
    if getattr(app, "mode", "") != MODE:
        _say(app, "Open Setup > Heavy telemetry update settings before using its controls.", True)
        return True
    state = initialize(app)
    action = parts[0]
    if len(parts) == 2 and action == "preset" and parts[1] in _model().PRESETS:
        if state["editing"] is not None:
            _say(app, "Press Enter or Escape to finish the current field first.")
        else:
            _apply(app, parts[1])
    elif parts == ["custom"]:
        if _model().settings_snapshot(app)["preset"] != "custom":
            _say(app, "Select Custom before opening its settings.")
        else:
            state.update(page="custom", top=0, follow_cursor=True)
            state["cursor"] = _choices(state, _model().settings_snapshot(app)).index(COMMAND + " edit " + _model().FIELDS[0]["key"])
            _invalidate(state)
    elif len(parts) == 2 and action == "edit" and state["page"] == "custom" and not state["busy"]:
        if parts[1] in state["draft"]:
            state["editing"] = (parts[1], state["draft"][parts[1]])
            state["cursor"] = _choices(state, _model().settings_snapshot(app)).index(COMMAND + " edit " + parts[1])
            state["follow_cursor"] = True
            _invalidate(state)
    elif parts == ["apply"]:
        if state["editing"] is None:
            _apply(app, "custom", draft=True)
        else:
            _say(app, "Press Enter to keep the current field before applying.", True)
    elif parts == ["save"]:
        _save(app)
    elif parts == ["defaults"] and not state["busy"]:
        state.update(draft={field["key"]: _format(field["default"]) for field in _model().FIELDS},
                     editing=None, dirty=True)
        _say(app, "Default values loaded into the draft. Apply custom to use them.")
    elif parts == ["cancel"]:
        state.update(page="presets", editing=None, draft=_draft(_model().settings_snapshot(app)),
                     dirty=False, cursor=0, top=0, follow_cursor=True)
        _say(app, "Unapplied custom edits discarded. The active sampling profile is unchanged.")
    elif parts == ["close"]:
        _close(app)
    else:
        _say(app, "Use the current telemetry settings controls.", True)
    return True


def _actions(state, snapshot, ascii_=False):
    choices = [("preset:" + name, ("(*) " if snapshot["preset"] == name else "( ) ") + name.title(),
                ("command", COMMAND + " preset " + name)) for name in _model().PRESETS]
    if snapshot["preset"] == "custom":
        choices.append(("custom", "[gear] Edit" if ascii_ else "\u2699 Edit", ("command", COMMAND + " custom")))
    return choices


def _choices(state, snapshot):
    values = [COMMAND + " preset " + name for name in _model().PRESETS]
    if snapshot["preset"] == "custom":
        values.append(COMMAND + " custom")
    if state["page"] == "custom":
        values.extend(COMMAND + " " + name for name in ("apply", "save", "defaults", "cancel", "close"))
        values.extend(COMMAND + " edit " + field["key"] for field in _model().FIELDS)
    else:
        values.extend(COMMAND + " " + name for name in ("save", "close"))
    return values


def value_style(field, raw):
    """Values interpolate continuously; invalid edits use an explicit warning."""
    try:
        number = float(raw)
        if not field["min"] <= number <= field["max"] or (field["type"] == "int" and not number.is_integer()):
            return "yellow+bold", "invalid"
        fraction = _model().value_fraction(field["key"], number)
    except (ValueError, TypeError, OverflowError):
        return "yellow+bold", "invalid"
    # Semantic white preserves readable contrast on the light theme. The pink
    # endpoint is deliberately distinct from the application's purple accent.
    return gradient_style("white", "#ff69b4", fraction) + "+bold", str(round(fraction * 100)) + "% intensity"


def handle_key(app, key):
    if getattr(app, "mode", "") != MODE:
        return False
    state = initialize(app)
    if state["editing"] is not None:
        field, value = state["editing"]
        if key == "esc":
            state["editing"] = None
        elif key == "enter":
            state["draft"][field] = value.strip()
            state.update(editing=None, dirty=True, error="")
        elif key in ("backspace", "delete"):
            state["editing"] = (field, value[:-1])
        elif key in ("ctrl-u", "ctrl-a"):
            state["editing"] = (field, "")
        elif len(key) == 1 and key.isprintable() and len(value) < 32:
            state["editing"] = (field, value + key)
        _invalidate(state)
        return True
    snapshot = _model().settings_snapshot(app)
    choices = _choices(state, snapshot)
    state["cursor"] = max(0, min(state["cursor"], len(choices) - 1))
    if key in ("esc", "q"):
        run_command(app, [COMMAND, "cancel" if state["page"] == "custom" else "close"])
    elif key in ("up", "down", "left", "right", "tab", "btab"):
        state["cursor"] = (state["cursor"] + (-1 if key in ("up", "left", "btab") else 1)) % len(choices)
        state["follow_cursor"] = True
        _invalidate(state)
    elif key in ("pgup", "pgdn", "home", "end"):
        state["top"] = max(0, {"pgup": state["top"] - state["visible"], "pgdn": state["top"] + state["visible"],
                              "home": 0, "end": 10**6}[key])
        state["follow_cursor"] = False
        _invalidate(state)
    elif key in ("enter", "space"):
        run_command(app, choices[state["cursor"]].split())
    elif key == ":":
        return False
    return True


def handle_mouse(app, y, x, button="left", shift=False):
    if getattr(app, "mode", "") != MODE:
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
    if getattr(app, "mode", "") != MODE:
        return None
    state = initialize(app)
    state.update(control_hits=[], painted=None)
    snapshot = _model().settings_snapshot(app)
    inner = max(1, width - 6)
    document, hits = [], []
    choices = _choices(state, snapshot)
    state["cursor"] = max(0, min(state["cursor"], len(choices) - 1))
    selected = choices[state["cursor"]]

    def prose(value, style="text"):
        value = display_text(str(value))
        if views.g.ascii:
            value = value.encode("ascii", "replace").decode("ascii")
        document.extend([(line, style)] for line in _wrap(value, inner))

    def controls(items):
        focused = next((identifier for identifier, _, action in items if action == ("command", selected)), None)
        rows, controls_ = buttons(views.g, inner, items, selected=focused, group=MODE, prefix=MODE + ":")
        offset = len(document)
        document.extend(rows)
        hits.extend((row + offset, kind, payload) for row, kind, payload in controls_)

    compact = width < 60 or height < 20
    prose(("" if compact else "Active profile: ") + snapshot["preset"].title() +
          (" | Heavy active" if snapshot["heavy"] else " | Light mode: Heavy rates are staged"), "bold")
    controls(_actions(state, snapshot, views.g.ascii))
    if state["page"] == "custom":
        labels = (("apply", "Apply"), ("save", "Save"), ("defaults", "Reset"),
                  ("cancel", "Back"), ("close", "x" if views.g.ascii else "×")) if compact else (
                  ("apply", "Apply custom"), ("save", "Save settings"), ("defaults", "Reset draft to Default"),
                  ("cancel", "Cancel edits / Back"), ("close", "Close"))
        controls([(name, label, ("command", COMMAND + " " + name)) for name, label in labels])
        prose("Draft; Apply uses these values." if compact else
              "Custom values are a draft until Apply or Save. White = Default; pink = Ultra; the percentage also shows intensity without colour.", "muted")
        if state["dirty"]:
            prose("Unapplied custom edits", "yellow")
    else:
        controls([(name, label, ("command", COMMAND + " " + name)) for name, label in (
            ("save", "Save settings"), ("close", "Close"))])
        prose("Presets apply live. Save remembers." if compact else
              "Presets apply immediately. Custom starts at Default and keeps your edits between presets. Save settings writes your configuration for later launches.", "muted")
    if snapshot.get("config_path") and not compact:
        prose("Configuration: " + str(snapshot["config_path"]), "muted")
    if str(snapshot.get("config_path", "")).lower().endswith(".toml"):
        prose("Saving TOML preserves configuration values but rewrites comments and formatting.", "yellow")
    if state["busy"]:
        prose("Saving configuration in the background...", "cyan")
    if state["error"]:
        prose("Issue: " + state["error"], "yellow+bold")
    if state["message"] and not compact:
        prose(state["message"], "green")
    if state["page"] != "custom" and not compact:
        prose("Current effective intervals and collection budgets. Source limits and backoff can delay observations.", "muted")
    category = None
    for field in _model().FIELDS:
        if field.get("category") and field["category"] != category:
            category = field["category"]
            prose(category + (" (advanced)" if category == "Budgets" else ""), "cyan+bold")
        key, label = field["key"], field["label"]
        effective = snapshot["effective"].get(key, field["default"])
        if state["page"] != "custom":
            prose(label + ": " + _format(effective) + " " + field["unit"])
            continue
        editing = state["editing"] is not None and state["editing"][0] == key
        raw = state["editing"][1] if editing else state["draft"][key]
        style, intensity = value_style(field, raw)
        command = COMMAND + " edit " + key
        prefix = "> " if command == selected else "  "
        label_text = L.cut(prefix + label, inner, views.g.ascii)
        document.append([(label_text, "bold" if command == selected else "text")])
        value_text = L.cut("  " + (raw or "[empty]") + ("_" if editing else "") + " " + field["unit"], inner, views.g.ascii)
        row = len(document)
        document.append([(value_text, style + ("+under" if editing else ""))])
        for hit_row, text in ((row - 1, label_text), (row, value_text)):
            hits.append((hit_row, "control", {"id": MODE + ":field:" + key + ":" + str(hit_row),
                         "label": label + " " + raw, "left": 0, "right": L.vlen(text),
                         "action": ("command", command), "group": MODE}))
        prose("  " + intensity + " | effective " + _format(effective) + " | Default " + _format(field["default"]) +
              " / Ultra " + _format(field["ultra"]) + " | allowed " + _format(field["min"]) + ".." + _format(field["max"]), "muted")
        prose("  " + field["description"], "muted")
    if compact:
        if state["message"]:
            prose(state["message"], "green")
        if snapshot.get("config_path"):
            prose("Configuration: " + str(snapshot["config_path"]), "muted")
        prose("Custom starts at Default and keeps your edits between presets. White = Default; pink = Ultra; intensity is also shown as a percentage.", "muted")
    for limit in snapshot.get("limits", ()):
        prose(str(limit), "muted")
    prose("Slurm accounting and job-owned trace writers control when new measurements exist. Faster polling cannot create missing samples.", "muted")
    capacity = max(0, height - 5)
    state["visible"] = max(1, capacity)
    selected_row = next((row for row, _, item in hits if item["action"] == ("command", selected)), 0)
    if state.pop("follow_cursor", False):
        if selected_row < state["top"]:
            state["top"] = selected_row
        elif selected_row + (2 if " edit " in selected else 0) >= state["top"] + capacity:
            state["top"] = selected_row - capacity + (3 if " edit " in selected else 1)
    state["top"] = max(0, min(state["top"], max(0, len(document) - capacity)))
    rows = document[state["top"]:state["top"] + capacity]
    footer = ("Enter: keep | Esc: cancel field | Ctrl-U: clear" if state["editing"] is not None else
              "Tab/arrows: choose | Enter: open | PgUp/PgDn: scroll | Esc: back")
    if compact:
        footer = "Enter: keep | Esc: undo" if state["editing"] is not None else "Home: actions | Esc: back"
    footer = f"{min(state['top'] + 1, len(document))}-{min(len(document), state['top'] + capacity)}/{len(document)} " + footer
    if height >= 5:
        rows.append([(L.cut(footer, inner, views.g.ascii), "muted")])
    title = "Heavy telemetry / Custom" if state["page"] == "custom" else "Heavy telemetry updates"
    rendered = L.box(views.g, rows, width, height, title, min_width=20)
    state["control_hits"] = place_hits(hits, rendered[1:-2] if height >= 5 else rendered[1:-1], offset=state["top"])
    state["painted"] = _frame(app)
    return rendered
