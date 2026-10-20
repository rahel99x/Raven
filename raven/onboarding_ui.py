"""First-run and saved-site wizard; rendering never discovers or saves settings."""
from __future__ import annotations

import shlex
import sys

from . import layout as L
from .entry import onboarding_service as O
from .control_rows import place_hits
from .job_logs.text import display_text
from .setup_ui import _wrap


def initialize(app):
    state = getattr(app, "onboarding_ui_state", None)
    if not isinstance(state, dict):
        state = dict(page="welcome", form={}, make_default=False, draft=None,
                     cursor=0, top=0, visible=1, editing=None, busy="", message="",
                     service=None, scope=None, report=None, discovery=None, profiles=[],
                     saved=None, selected_profile=None, return_mode="main", generation=0, revision=0, painted=None, control_hits=[])
        app.onboarding_ui_state = state
    return state


def command_names():
    return ["gettingstarted", "profiles"]


def _invalidate(state):
    state["revision"] += 1
    state.update(painted=None, control_hits=[])


def _frame(app):
    state = initialize(app)
    return (app.mode, getattr(app, "width", 0), getattr(app, "height", 0),
            id(getattr(app, "raven_resolved", None)), state["revision"], state["top"])


def _say(app, message):
    state = initialize(app)
    state["message"] = display_text(str(message))
    _invalidate(state)


def _page(app, page):
    state = initialize(app)
    state.update(page=page, cursor=0, top=0, editing=None)
    _invalidate(state)


def _service(app):
    state = initialize(app)
    resolved = getattr(app, "raven_resolved", None)
    if resolved is None:
        raise ValueError("Getting started needs a managed Raven session. Open ravenl or run raven setup.")
    if state["service"] is None or state["scope"] is not resolved:
        from .configuration.profiles import ProfileSetup
        backend = getattr(getattr(getattr(app, "actions", None), "slurm", None), "b", None)
        state.update(service=ProfileSetup(resolved, backend), scope=resolved)
    return state["service"]


def _background(app, label, action, completed):
    state = initialize(app)
    if state["busy"]:
        _say(app, "Wait for " + state["busy"].lower() + " to finish.")
        return
    hub = getattr(app, "research", None)
    if hub is None:
        _say(app, "Background setup is unavailable; use raven setup from your terminal.")
        return
    scope = getattr(app, "raven_resolved", None)
    state["busy"] = label
    _invalidate(state)

    def publish(value):
        state["busy"] = ""
        _invalidate(state)
        if scope is not getattr(app, "raven_resolved", None):
            state["draft"] = None
            _say(app, "The active connection changed. Reopen Getting started before saving.")
        elif isinstance(value, Exception):
            _say(app, value)
        else:
            completed(value)

    if not hub.start_task(action, publish):
        state["busy"] = ""
        _say(app, "The background reader is busy. Try again after it finishes.")


def open_setup(app):
    if (getattr(app, "mode", "main") == "confirm" or
            getattr(app, "execution_state", {}).get("pending_action") or
            getattr(app, "raven_ui_state", {}).get("review")):
        if callable(getattr(app, "fail", None)):
            app.fail("Close the pending job or allocation review before opening setup.")
        return
    state = initialize(app)
    resolved = getattr(app, "raven_resolved", None)
    if resolved is None:
        if callable(getattr(app, "fail", None)):
            app.fail("Open ravenl, or run raven setup in your terminal, to configure an installation.")
        return
    if not state["form"] or state["scope"] is not resolved:
        state.update(form=O.default_form(resolved), make_default=not bool(resolved.source),
                     service=None, scope=resolved, draft=None, message="", page="welcome")
    if app.mode != "onboarding":
        state["return_mode"] = "main" if app.mode == "palette" else app.mode
        from .terminal import ui as terminal_ui
        if terminal_ui.focused(app):
            terminal_ui.blur(app)
        from .pointer_routing import begin_gesture
        begin_gesture(app)
    app.mode = "onboarding"
    state["generation"] += 1
    _invalidate(state)


def _close(app):
    state = initialize(app)
    app.mode = "main" if state["return_mode"] == "onboarding" else state["return_mode"]
    state["editing"] = None
    _invalidate(state)


def _review(app, *, discover=False):
    state = initialize(app)
    service = _service(app)
    form, make_default = dict(state["form"]), state["make_default"]

    def read():
        draft = O.stage_form(service, form, make_default=make_default)
        return (draft, service.discover(draft)) if discover else draft

    def done(value):
        if discover:
            state["draft"], state["discovery"] = value
            _page(app, "accounts")
        else:
            state["draft"] = value
            _page(app, "review")
        state["message"] = ""

    _background(app, "Discovering associations" if discover else "Checking profile", read, done)


def _save(app):
    state = initialize(app)
    if state["page"] != "review" or state["draft"] is None:
        _say(app, "Review the current profile before saving.")
        return
    if getattr(app, "replay", None) or getattr(getattr(app, "research", None), "demo", False):
        _say(app, "The demo keeps configuration unchanged. Run raven setup to save a real profile.")
        return
    service, draft = _service(app), state["draft"]

    def done(value):
        state["saved"] = value
        state["draft"] = None
        _page(app, "saved")
        state["message"] = "Profile saved. The current dashboard connection is unchanged."

    _background(app, "Saving reviewed profile", lambda: service.save(draft), done)


def _profiles(app):
    def done(value):
        initialize(app)["profiles"] = value
        _page(app, "profiles")
    _background(app, "Reading saved profiles", _service(app).list_profiles, done)


def _load_profile(app, name):
    state = initialize(app)
    resolved = app.raven_resolved

    def read():
        from .configuration.policy import resolve
        return resolve(resolved.source or None, site=name)

    def done(value):
        state.update(form=O.default_form(value), draft=None, make_default=False, message="", selected_profile=name)
        _page(app, "profile")

    _background(app, "Reading selected profile", read, done)


def _check_restart(app, destination):
    """A new dashboard must not interrupt allocation or submission ownership."""
    manager = getattr(app, "raven_allocation_manager", None)
    if manager is not None and manager.snapshot().get("state") != "light":
        raise ValueError("Release or cancel Raven's Heavy worker before " + destination + ".")
    if (getattr(app, "execution_state", {}).get("pending_action") or
            getattr(app, "raven_ui_state", {}).get("review")):
        raise ValueError("Close the pending job or allocation review before " + destination + ".")
    launch = getattr(app, "launch_wizard_state", {})
    if launch.get("busy") or launch.get("pending"):
        raise ValueError("Wait for the current launch operation to finish before " + destination + ".")


def run_command(app, args):
    if not args or args[0] not in command_names():
        return False
    if len(args) == 1:
        open_setup(app)
        if args[0] == "profiles" and app.mode == "onboarding":
            try:
                _profiles(app)
            except (OSError, ValueError, RuntimeError) as exc:
                _say(app, exc)
        return True
    if getattr(app, "mode", "") != "onboarding":
        return True
    state = initialize(app)
    action = args[1]
    if state["busy"] and action != "close":
        _say(app, "Wait for " + state["busy"].lower() + " to finish.")
        return True
    try:
        if action == "close":
            _close(app)
        elif action in ("welcome", "profile", "demo"):
            _page(app, action)
        elif action == "kind" and len(args) == 3 and args[2] in dict(O.KINDS):
            kind = args[2]
            if state["form"]["kind"] != kind:
                state["form"].update(kind=kind, host="", user="", cluster="", account="", partition="",
                                     worker_python="" if kind == "ssh-slurm" else sys.executable)
            state["draft"] = None
            state["selected_profile"] = None
            _page(app, "profile")
        elif action == "edit" and len(args) == 3 and args[2] in O.FORM_FIELDS:
            state["editing"] = (args[2], state["form"][args[2]])
            _invalidate(state)
        elif action == "default":
            state["make_default"] = not state["make_default"]
            state["draft"] = None
            _invalidate(state)
        elif action in ("review", "accounts"):
            _review(app, discover=action == "accounts")
        elif action == "account" and len(args) == 3 and state["page"] == "accounts":
            accounts = (state["discovery"] or {}).get("accounts", ())
            index = int(args[2])
            if index < 0 or index >= len(accounts):
                raise ValueError("Choose an association from the current list.")
            item = accounts[index]
            cluster = (state["discovery"] or {}).get("cluster")
            if cluster and item.get("cluster") and cluster != item["cluster"]:
                raise ValueError("This account belongs to another cluster. Configure that site before choosing its association.")
            state["form"].update(account=item["account"], partition=item.get("partition") or state["form"]["partition"])
            state["draft"] = None
            _page(app, "profile")
        elif action == "save":
            _save(app)
        elif action == "connect":
            _check_restart(app, "switching sites")
            saved = state["saved"] if state["page"] == "saved" else None
            profile = saved["profile"] if saved else state["selected_profile"]
            if not profile:
                raise ValueError("Save or choose an existing profile before connecting.")
            app.switch_profile = profile
            app.switch_profile_config = saved["path"] if saved else app.raven_resolved.source
            app.quit = True
        elif action == "open-demo":
            _check_restart(app, "opening the offline demo")
            app.start_demo = True
            app.quit = True
        elif action == "checks":
            def done(value):
                state["report"] = value
                _page(app, "checks")
            _background(app, "Checking installation", lambda: O.installation_report(app.raven_resolved), done)
        elif action == "profiles":
            _profiles(app)
        elif action == "load" and len(args) == 3:
            _load_profile(app, args[2])
        elif action == "resume":
            def done(value):
                if value is None:
                    _say(app, "There is no saved setup draft. Choose an environment to begin.")
                else:
                    state.update(form=value["form"], make_default=value["make_default"], draft=None)
                    _page(app, "profile")
                    _say(app, "Resumed setup progress. Review before saving configuration.")
            _background(app, "Reading saved draft", lambda: O.load_progress(app.raven_resolved), done)
        elif action == "draft":
            if getattr(app, "replay", None) or getattr(getattr(app, "research", None), "demo", False):
                raise ValueError("The demo keeps saved state unchanged.")
            form, default = dict(state["form"]), state["make_default"]
            _background(app, "Saving setup progress", lambda: O.save_progress(app.raven_resolved, form, default),
                        lambda path: _say(app, "Draft saved. Resume it here or with raven setup --resume. Configuration is unchanged."))
    except (OSError, ValueError, RuntimeError) as exc:
        _say(app, exc)
    return True


def _document(app):
    state = initialize(app)
    page = state["page"]
    rows = [("Back to getting started", "gettingstarted welcome")] if page != "welcome" else []
    rows += [("Close / continue monitoring", "gettingstarted close")]
    if state["busy"]:
        rows.append((state["busy"] + "...", None))
    if state["message"]:
        rows.append((state["message"], None))
    if page == "welcome":
        rows += [("Choose where Raven connects. Complete setup before requesting Heavy; saving never submits jobs or allocates compute.", None)]
        rows += [(label, "gettingstarted kind " + kind) for kind, label in O.KINDS]
        rows += [("Review installation checks", "gettingstarted checks"),
                 ("Saved sites and lab profiles", "gettingstarted profiles"),
                 ("Resume a saved setup draft", "gettingstarted resume"),
                 ("Try Raven with simulated jobs", "gettingstarted demo")]
    elif page == "profile":
        rows.append((dict(O.KINDS)[state["form"]["kind"]], None))
        fields = [("name", "Profile name", "required")]
        if state["form"]["kind"] == "ssh-slurm":
            fields += [("host", "SSH host alias", "required"), ("user", "SSH login", "SSH configuration default")]
        fields += [("worker_python", "Worker Python", "configure later for Heavy")]
        if state["form"]["kind"] != "standalone":
            fields += [("account", "Raven Heavy billing account", "Slurm default"),
                       ("partition", "Heavy partition", "Slurm default")]
        for key, label, empty in fields:
            value = state["form"][key]
            if state["editing"] and state["editing"][0] == key:
                value = state["editing"][1] + "_"
            rows.append((label + ": " + (value or empty), "gettingstarted edit " + key))
        rows += [("Default for future launches: " + ("Yes" if state["make_default"] else "No"), "gettingstarted default"),
                 ("Continue to review", "gettingstarted review")]
        if state["form"]["kind"] != "standalone":
            rows += [("Discover account associations", "gettingstarted accounts"),
                     ("Your login identifies you. The billing account identifies the personal or lab allocation charged for Raven Heavy.", None),
                     ("Scientific job accounts and resources are chosen separately in Launch job or your submission script.", None)]
        rows += [("Save draft for later", "gettingstarted draft"),
                 ("Worker Python must have this Raven release installed on the compute host. Setup cannot verify compute-node access.", None)]
        if state["selected_profile"]:
            rows += [("Use saved profile (without saving edits)", "gettingstarted connect")]
    elif page == "review":
        rows += [(line, None) for line in O.preview_lines(state["draft"])]
        rows += [("Save reviewed profile", "gettingstarted save"), ("Back to edit", "gettingstarted profile")]
    elif page == "saved":
        rows += [("Use this profile", "gettingstarted connect"),
                 ("Or open the saved profile in a new session:", None), (state["saved"]["command"], None),
                 ("Then choose File > Launch job to submit a scientific workload, or Heavy to review a Raven worker allocation.", None)]
        rows += [(str(item), None) for item in state["saved"].get("warnings", ())]
    elif page == "checks":
        rows += [(line, None) for line in O.report_lines(state["report"])]
    elif page == "demo":
        rows += [("Open offline demo", "gettingstarted open-demo"),
                 ("Or open a separate terminal and run:", None), ("raven setup --demo", None),
                 ("The demo uses simulated jobs, disables persistent state and plugins, and does not contact Slurm.", None),
                 ("Explore Jobs, Analytics, and Research. Press q to leave the demo, then run raven setup to configure your own site.", None)]
    elif page == "accounts":
        discovery = state["discovery"] or {}
        rows += [("Connected login: " + (discovery.get("username") or "not yet verified"), None)]
        rows += [(str(item), None) for item in discovery.get("errors", ())]
        for index, association in enumerate(discovery.get("accounts", ())):
            label = association["account"] + " / " + (association.get("cluster") or "cluster unspecified")
            if association.get("partition"):
                label += " / " + association["partition"]
            rows.append((label, "gettingstarted account " + str(index)))
        rows += [("Use default or enter account manually", "gettingstarted profile"),
                 ("Visible associations do not guarantee allocation admission. No account is selected automatically.", None)]
    elif page == "profiles":
        for profile in state["profiles"]:
            name = profile.get("profile", profile.get("name", ""))
            rows.append((profile.get("label") or name, "gettingstarted load " + shlex.quote(name)))
        if not state["profiles"]:
            rows.append(("No named profiles have been saved yet. Choose an environment to create one.", None))
    return rows


def handle_key(app, key):
    if getattr(app, "mode", "") != "onboarding":
        return False
    state = initialize(app)
    if state["editing"]:
        field, value = state["editing"]
        if key == "esc":
            state["editing"] = None
        elif key == "enter":
            state["form"][field] = value.strip()
            state.update(editing=None, draft=None)
        elif key in ("backspace", "delete"):
            state["editing"] = field, value[:-1]
        elif key in ("ctrl-u", "ctrl-a"):
            state["editing"] = field, ""
        elif key == "space" and len(value) < 4096:
            state["editing"] = field, value + " "
        elif len(key) == 1 and key.isprintable() and len(value) < 4096:
            state["editing"] = field, value + key
        _invalidate(state)
        return True
    choices = [command for _, command in _document(app) if command]
    state["cursor"] = max(0, min(state["cursor"], len(choices)-1))
    if key in ("esc", "q"):
        _close(app)
    elif key in ("up", "down", "left", "right", "tab", "btab"):
        state["cursor"] = (state["cursor"] + (-1 if key in ("up", "left", "btab") else 1)) % len(choices)
        state["follow_cursor"] = True
        _invalidate(state)
    elif key in ("pgup", "pgdn", "home", "end"):
        state["top"] = max(0, {"pgup": state["top"]-state["visible"], "pgdn": state["top"]+state["visible"],
                              "home": 0, "end": 10**6}[key])
        state["follow_cursor"] = False
        _invalidate(state)
    elif key in ("enter", "space"):
        run_command(app, shlex.split(choices[state["cursor"]]))
    elif key == ":":
        return False
    return True


def paste(app, text):
    """Accept one bounded printable field; never interpret pasted commands."""
    state = initialize(app)
    if getattr(app, "mode", "") != "onboarding" or not state["editing"]:
        return False
    if not isinstance(text, str) or any(not char.isprintable() for char in text):
        _say(app, "Paste a single line without control characters.")
        return True
    field, value = state["editing"]
    if len(value) + len(text) > 4096:
        _say(app, "The field is limited to 4096 characters.")
        return True
    state["editing"] = field, value + text
    _invalidate(state)
    return True


def handle_mouse(app, y, x, button="left", shift=False):
    if getattr(app, "mode", "") != "onboarding":
        return False
    state = initialize(app)
    if button in ("wheel-up", "wheel-down"):
        state["top"] = max(0, state["top"] + (-3 if button == "wheel-up" else 3))
        state["follow_cursor"] = False
        _invalidate(state)
    elif button in ("left", "press") and state["painted"] == _frame(app) and not state["editing"]:
        for row, _, control in state["control_hits"]:
            if row == y and control["left"] <= x < control["right"]:
                run_command(app, shlex.split(control["action"][1]))
                break
    return True


def overlay(views, snap, app, width, height):
    if getattr(app, "mode", "") != "onboarding":
        return None
    state = initialize(app)
    state.update(control_hits=[], painted=None)
    inner, document, hits, choice, selected_row = max(1, width-6), [], [], 0, 0
    for text, command in _document(app):
        text = display_text(str(text))
        if views.g.ascii:
            text = text.encode("ascii", "replace").decode()
        if command:
            selected = choice == state["cursor"]
            if selected:
                selected_row = len(document)
            label = L.cut(("> " if selected else "  ") + text, inner, views.g.ascii)
            hits.append((len(document), "control", {"id": "onboarding:" + str(choice), "label": text,
                         "left": 0, "right": L.vlen(label), "action": ("command", command), "group": "onboarding"}))
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
    if height >= 5:
        footer = "Enter: keep field | Esc: cancel | Ctrl-U: clear" if state["editing"] else "Tab: choose | Enter: open | PgUp/PgDn: scroll | Esc: close"
        rows.append([(L.cut(footer, inner, views.g.ascii), "muted")])
    result = L.box(views.g, rows, width, height, "Getting started with Raven", min_width=20)
    state["control_hits"] = place_hits(hits, result[1:-2] if height >= 5 else result[1:-1], offset=state["top"])
    state["painted"] = _frame(app)
    return result
