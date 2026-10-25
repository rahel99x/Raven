"""A guided existing-script launch, separate from exports and Heavy allocation."""

from __future__ import annotations
from ..ui.form_state import edit_actions, edit_mouse, publish_edit_actions

import copy
import getpass
import os
import shlex
import threading

from .. import layout as L
from ..control_rows import place_hits
from ..job_logs.text import display_text

STEPS = ("project", "script", "resources", "recordings", "review")
RESOURCE_FIELDS = (
    ("account", "Workload billing account"),
    ("partition", "Partition"),
    ("qos", "Quality of service (QoS)"),
    ("cpus", "CPUs per task"),
    ("gpus", "GPUs"),
    ("memory", "Memory, e.g. 8 GiB"),
    ("time", "Time limit, e.g. 1h"),
)


def initialize(app):
    state = getattr(app, "launch_wizard_state", None)
    if not isinstance(state, dict):
        state = dict(
            stage="project",
            generation=0,
            revision=0,
            cursor=0,
            top=0,
            visible=1,
            follow=True,
            editing=None,
            busy=False,
            pending=None,
            cancel=None,
            draft={},
            review=None,
            imported=None,
            receipt=None,
            options={},
            error="",
            message="",
            control_hits=[],
            painted=None,
            attempted=False,
            return_mode="main",
        )
        app.launch_wizard_state = state
    return state


def command_names():
    return ["launchjob"]


def active(app):
    return getattr(app, "mode", "") == "launch_job"


def context(app):
    state = initialize(app)
    return (
        state["generation"],
        state["revision"],
        state["stage"],
        state["top"],
        state["editing"],
    )


def _invalidate(state):
    state["revision"] += 1
    state.update(control_hits=[], painted=None)


def _frame(app):
    return (
        getattr(app, "mode", ""),
        getattr(app, "width", None),
        getattr(app, "height", None),
        context(app),
    )


def _say(app, message, error=False):
    callback = getattr(app, "fail" if error else "say", None)
    if callable(callback):
        callback(str(message))


def _connection(app):
    resolved = getattr(app, "raven_resolved", None)
    data = getattr(resolved, "data", {}) or {}
    connection = copy.deepcopy(data.get("connection", {}))
    files = getattr(app, "files", None) or getattr(
        getattr(app, "research", None), "files", None
    )
    if getattr(files, "remote", False):
        backend = getattr(files, "b", None) or getattr(files, "backend", None)
        connection.update(
            transport="ssh",
            host=connection.get("host")
            or getattr(backend, "host", None)
            or "SUBMISSION_HOST",
        )
    return connection


def _scope(app):
    project = getattr(app, "project_state", {})
    return (
        _connection(app),
        id(getattr(app, "research", None)),
        project.get("registered_root", ""),
    )


def open_launch(app):
    from ..ui.form_state import _pending_review

    if _pending_review(app):
        _say(
            app,
            "Close the pending job or allocation review before opening Launch job.",
            True,
        )
        return False
    state = initialize(app)
    if state["busy"] or state["pending"]:
        _say(
            app,
            "The previous launch action is still completing. Its receipt will be reported; do not submit again.",
            True,
        )
        return False
    if getattr(app, "mode", "main") not in (
        "main",
        "setup",
        "onboarding",
        "launch_job",
        "palette",
        "run_home",
    ):
        _say(app, "Close the current dialog before launching a job.", True)
        return False
    project = getattr(app, "project_state", {})
    root = project.get("registered_root") or project.get("root") or os.getcwd()
    data = getattr(getattr(app, "raven_resolved", None), "data", {}) or {}
    standalone = (
        data.get("site", {}).get("kind") == "desktop"
        and data.get("connection", {}).get("transport") == "none"
    )
    origin = getattr(app, "mode", "main")
    if origin != "launch_job":
        from ..terminal import ui as terminal_ui

        if terminal_ui.focused(app):
            terminal_ui.blur(app)
        from ..pointer_routing import begin_gesture

        begin_gesture(app)
    state.update(
        generation=state["generation"] + 1,
        stage="project",
        cursor=0,
        top=0,
        follow=True,
        editing=None,
        review=None,
        imported=None,
        receipt=None,
        options={},
        accounts={},
        error="",
        message="",
        attempted=False,
        return_mode="main" if origin == "palette" else origin,
        scope=_scope(app),
        username=getpass.getuser(),
        draft=dict(
            project=root,
            script="",
            arguments="",
            execution="local" if standalone else "slurm",
            recipe="",
            recipe_name="lab-run",
            account="",
            partition="",
            qos="",
            cpus="",
            gpus="",
            memory="",
            time="",
            gpu=None,
            cpu_memory=None,
            trace=None,
            trace_profile="",
            basic_research=False,
        ),
    )
    app.mode = "launch_job"
    _invalidate(state)
    return True


def open_from_run(app, draft):
    """Copy retained choices into a new review; opening never submits a job."""
    retained = copy.deepcopy(draft)
    if not open_launch(app):
        return False
    state = initialize(app)
    state["draft"].update(retained)
    state.update(
        stage="resources",
        imported=copy.deepcopy(retained.get("captured_plan")),
        message="Retained resources and recording settings copied. Review before a new submission; the original source must still match.",
    )
    _invalidate(state)
    return True


def cancel(app, close=True):
    state = initialize(app)
    if state["cancel"] is not None:
        state["cancel"].set()
    if state["pending"]:
        state.update(pending=None, cancel=None)
    if close:
        app.mode = (
            state["return_mode"]
            if state["return_mode"] not in ("launch_job", "launch_setup")
            else "main"
        )
        if state["busy"] and state["attempted"]:
            _say(
                app,
                "Launch is completing; closing this panel does not cancel an accepted workload. Wait for its receipt before submitting again.",
            )
    _invalidate(state)


def _queue(app, label, work, complete, *, submitting=False):
    state = initialize(app)
    if state["busy"] or state["pending"]:
        return False
    state.update(
        pending=(work, complete, submitting),
        cancel=threading.Event(),
        error="",
        message=label,
    )
    _invalidate(state)
    tick(app)
    return True


def tick(app):
    state = initialize(app)
    if not state["pending"]:
        return
    if not active(app):
        cancel(app)
        return
    hub = getattr(app, "research", None)
    if hub is None:
        state.update(
            pending=None,
            cancel=None,
            error="The background reader is unavailable. Reopen Launch job in an interactive Raven session.",
        )
        _invalidate(state)
        return
    work, complete, submitting = state["pending"]
    token, event, scope = (
        state["generation"],
        state["cancel"],
        copy.deepcopy(state["scope"]),
    )

    def worker():
        if event.is_set() or getattr(hub, "closed", False):
            raise InterruptedError("Launch action closed before it started")
        if scope != _scope(app):
            raise ValueError(
                "The project or connection changed. Reopen Launch job on the intended host."
            )
        return work(event)

    def published(value):
        if token != state["generation"]:
            return
        state.update(busy=False, cancel=None, message="")
        if isinstance(value, Exception):
            state["error"] = str(value)
            if submitting and not isinstance(value, InterruptedError):
                state["error"] += (
                    " Check the scheduler and retained launch receipts before trying another submission. Raven will not retry automatically."
                )
            _say(app, "Launch job: " + state["error"], True)
        elif submitting:
            state.update(receipt=value, stage="done", error="")
            _say(
                app,
                "Submitted job " + value["job_id"]
                if value.get("job_id")
                else "Started local supervisor "
                + str(value.get("pid", ""))
                + "; run "
                + value["run_id"],
            )
        elif active(app) and scope == _scope(app):
            complete(value)
        _invalidate(state)

    if hub.start_task(worker, published):
        state.update(pending=None, busy=True)


def _remote_guard(app):
    connection = _connection(app)
    if connection.get("transport") not in (None, "none", "local"):
        host = connection.get("host") or "SUBMISSION_HOST"
        raise ValueError(
            "Remote workload launch is not supported from this dashboard. Run Raven on "
            + str(host)
            + " and choose File > Launch job there, or run raven launch SCRIPT --check on that host. No local fallback was started."
        )
    if getattr(app, "replay", None) or getattr(
        getattr(app, "research", None), "demo", False
    ):
        raise ValueError(
            "Demo and replay sessions cannot submit real workloads. Open your configured Raven session to launch a job."
        )


def _advance(app):
    from . import wizard as service

    state = initialize(app)
    draft, connection = copy.deepcopy(state["draft"]), _connection(app)
    _remote_guard(app)
    if state["stage"] == "project":
        if not draft["project"].strip():
            raise ValueError("Choose a project folder first")

        def loaded(value):
            state["draft"]["project"] = value["root"]
            state.update(options=value, stage="script", cursor=0, top=0, follow=True)

        _queue(
            app,
            "Reading project scripts and saved profiles...",
            lambda event: service.project_options(draft["project"]),
            loaded,
        )
    elif state["stage"] in ("script", "resources", "recordings"):
        destination = {
            "script": "resources",
            "resources": "recordings",
            "recordings": "review",
        }[state["stage"]]

        def reviewed(value):
            state.update(
                imported=value["plan"],
                review=value if destination == "review" else None,
                stage=destination,
                cursor=0,
                top=0,
                follow=True,
                attempted=False,
            )

        _queue(
            app,
            "Checking the existing script and effective settings...",
            lambda event: service.review(draft, connection=connection),
            reviewed,
        )


def _submit(app):
    from . import wizard as service

    state = initialize(app)
    if state["stage"] != "review" or state["review"] is None or state["attempted"]:
        return
    _remote_guard(app)
    if state["scope"] != _scope(app):
        raise ValueError(
            "The project or connection changed. Reopen Launch job before submission."
        )
    draft, reviewed, connection = (
        copy.deepcopy(state["draft"]),
        copy.deepcopy(state["review"]),
        _connection(app),
    )
    state["attempted"] = True
    _queue(
        app,
        "Submitting once; waiting for the actual receipt...",
        lambda event: service.submit(
            reviewed, draft, connection=connection, cancelled=event.is_set
        ),
        lambda value: None,
        submitting=True,
    )


def _save_recipe(app):
    from ..configuration.profiles import save_project_recipe, workload_defaults

    state = initialize(app)
    draft = copy.deepcopy(state["draft"])
    defaults = workload_defaults(
        account=draft["account"],
        partition=draft["partition"],
        qos=draft["qos"],
        cpus=draft["cpus"] or None,
        gpus=draft["gpus"] or None,
        memory=draft["memory"] or None,
        time_limit=draft["time"] or None,
    )
    expected = copy.deepcopy(state.get("options", {}).get("settings"))
    recipe = copy.deepcopy(
        (expected or {}).get("recipes", {}).get(draft["recipe_name"], {})
    )
    recipe["sbatch_defaults"] = defaults

    def saved(value):
        state["options"]["settings"] = value
        state["options"]["recipes"] = sorted(
            set(state["options"].get("recipes", [])) | {draft["recipe_name"]}
        )
        state["message"] = (
            "Saved workload resource profile "
            + draft["recipe_name"]
            + ". Script and environment directives keep their normal precedence."
        )

    _queue(
        app,
        "Saving project workload profile...",
        lambda event: save_project_recipe(
            draft["project"], draft["recipe_name"], recipe, expected=expected
        ),
        saved,
    )


def _discover_accounts(app):
    resolved = getattr(app, "raven_resolved", None)
    if resolved is None:
        raise ValueError(
            "Account discovery needs a configured Slurm site. You can still enter your assigned workload account manually."
        )
    backend = getattr(getattr(getattr(app, "actions", None), "slurm", None), "b", None)

    def work(event):
        from ..configuration.allocation import AllocationSetup

        return AllocationSetup(
            resolved, backend, manager=getattr(app, "raven_allocation_manager", None)
        ).discover()

    def completed(value):
        initialize(app)["accounts"] = value

    _queue(app, "Discovering visible Slurm account associations...", work, completed)


def _activate(app, action):
    state = initialize(app)
    if action == "close":
        cancel(app)
        return
    if state["busy"] or state["pending"] or state["editing"]:
        return
    try:
        if state["draft"].get("captured_plan") and (
            action in ("advanced", "save-recipe") or action.startswith("recipe:")
        ):
            raise ValueError(
                "This review keeps the prior run's recording settings. Open a fresh Launch job to choose a different project recipe."
            )
        if action == "next":
            _advance(app)
        elif action == "back":
            state.update(
                stage=STEPS[max(0, STEPS.index(state["stage"]) - 1)],
                review=None,
                cursor=0,
                top=0,
                follow=True,
            )
        elif action == "submit":
            _submit(app)
        elif action.startswith("field:"):
            field = action[6:]
            state["editing"] = (field, state["draft"].get(field, ""))
        elif action.startswith("script:"):
            state["draft"]["script"] = state["options"]["scripts"][int(action[7:])]
        elif action.startswith("execution:"):
            state["draft"]["execution"] = action.split(":", 1)[1]
        elif action.startswith("recipe:"):
            state["draft"]["recipe"] = action.split(":", 1)[1]
        elif action.startswith("toggle:"):
            field = action.split(":", 1)[1]
            state["draft"][field] = {None: True, True: False, False: None}[
                state["draft"].get(field)
            ]
            if field == "trace" and state["draft"][field] is not True:
                state["draft"]["trace_profile"] = ""
        elif action.startswith("trace-profile:"):
            from ..tracing.profiles import settings as trace_settings

            profile = action.split(":", 1)[1]
            trace_settings(profile)
            state["draft"].update(trace=True, trace_profile=profile)
        elif action == "research":
            state["draft"]["basic_research"] = not state["draft"]["basic_research"]
        elif action == "save-recipe":
            _save_recipe(app)
        elif action == "accounts":
            _discover_accounts(app)
        elif action.startswith("account:"):
            discovery = state.get("accounts") or {}
            association = discovery.get("accounts", [])[int(action.split(":", 1)[1])]
            if (
                discovery.get("cluster")
                and association.get("cluster")
                and discovery["cluster"] != association["cluster"]
            ):
                raise ValueError(
                    "That account belongs to another cluster. Switch to its site before selecting it."
                )
            state["draft"]["account"] = association["account"]
            if association.get("partition"):
                state["draft"]["partition"] = association["partition"]
            if state["draft"]["qos"] and state["draft"]["qos"] not in association.get(
                "qos", ()
            ):
                state["draft"]["qos"] = association.get("default_qos", "")
        elif action == "advanced":
            from ..ui.navigation_ports import forms

            forms(app).advanced_launch(app, state["draft"]["project"])
        elif action == "open-run" and state["receipt"]:
            if state["scope"] != _scope(app):
                raise ValueError(
                    "The connection or project changed. Open this receipt's project on its original host."
                )
            from ..ui.navigation_ports import forms

            forms(app).launch_receipt(app, copy.deepcopy(state["receipt"]))
    except (ValueError, OSError, KeyError) as exc:
        state["error"] = str(exc)
        _say(app, "Launch job: " + str(exc), True)
    _invalidate(state)


def run_command(app, args):
    if not args or args[0] != "launchjob":
        return False
    if args[1:] in ([], ["show"]):
        open_launch(app)
    elif len(args) == 3 and args[1] == "action" and active(app):
        _activate(app, args[2])
    elif args[1:] == ["close"] and active(app):
        cancel(app)
    else:
        _say(app, "Use launchjob or launchjob close.", True)
    return True


def _document(app):
    state = initialize(app)
    draft, stage = state["draft"], state["stage"]
    rows = [("Close launch", "close")]
    if stage in STEPS:
        rows.append(
            (f"Step {STEPS.index(stage) + 1} of 5: " + stage.capitalize(), None)
        )
    if state["error"]:
        rows.append((state["error"], None))
    if state["message"]:
        rows.append((state["message"], None))
    if state["busy"] or state["pending"]:
        return rows
    field = lambda key, label: (
        label + ": " + (draft.get(key) or "[inherit / unset]"),
        "field:" + key,
    )
    if stage == "project":
        rows += [
            (
                "Choose the folder containing this job's script and outputs. A Raven project does not need to exist yet.",
                None,
            ),
            field("project", "Project folder"),
            ("Continue", "next"),
        ]
    elif stage == "script":
        rows += [
            field("script", "Existing .sh or .bash script (relative to project)"),
            field(
                "arguments", "Application arguments (shell-style quoting; no expansion)"
            ),
            (
                ("[x] " if draft["execution"] == "slurm" else "[ ] ")
                + "Slurm (workstation or cluster)",
                "execution:slurm",
            ),
            (
                ("[x] " if draft["execution"] == "local" else "[ ] ")
                + "Standalone workstation (execute here without Slurm)",
                "execution:local",
            ),
        ]
        rows += [
            ("Use script: " + name, "script:" + str(index))
            for index, name in enumerate(state["options"].get("scripts", []))
        ]
        rows += [("Back", "back"), ("Import script and continue", "next")]
    elif stage == "resources":
        rows.append(
            (
                "Login: "
                + state.get("username", "current user")
                + ". These settings bill the scientific workload; Raven Heavy's allocation is configured separately.",
                None,
            )
        )
        if draft["execution"] == "slurm":
            rows.append(
                (
                    "Blank fields inherit the script, environment, and project defaults. Nonblank fields override them for this launch.",
                    None,
                )
            )
            rows += [field(key, label) for key, label in RESOURCE_FIELDS]
            rows.append(
                ("Discover my personal and lab account associations", "accounts")
            )
            discovery = state.get("accounts") or {}
            for index, association in enumerate(discovery.get("accounts", [])):
                description = (
                    association["account"]
                    + " / "
                    + (association.get("cluster") or "current cluster")
                )
                if association.get("partition"):
                    description += " / " + association["partition"]
                if association.get("qos"):
                    description += " / QoS: " + ", ".join(association["qos"])
                rows.append(("Use account: " + description, "account:" + str(index)))
            rows += [(error, None) for error in discovery.get("errors", [])]
            if draft.get("captured_plan"):
                rows.append(
                    (
                        "Recording settings and advanced scheduler options come from the retained run. These resource fields are explicit overrides; start a fresh Launch job to use a different recipe.",
                        None,
                    )
                )
            else:
                rows += [("Use project defaults (no named profile)", "recipe:")]
                rows += [
                    (
                        ("[x] " if draft["recipe"] == name else "")
                        + "Workload profile: "
                        + name,
                        "recipe:" + name,
                    )
                    for name in state["options"].get("recipes", [])
                ]
                rows += [
                    field("recipe_name", "Save resource profile as"),
                    ("Save these resource defaults for this project", "save-recipe"),
                ]
        else:
            rows.append(
                (
                    "Standalone runs execute on this workstation. Slurm directives do not reserve or limit local resources.",
                    None,
                )
            )
        plan = state.get("imported") or {}
        rows.append(
            (
                "Imported script and inherited values (final overrides appear in Review):",
                None,
            )
        )
        rows += [
            (
                key
                + " = "
                + str(value)
                + " ("
                + plan.get("option_sources", {}).get(key, "default")
                + ")",
                None,
            )
            for key, value in plan.get("effective_options", {}).items()
        ]
        rows += [("Back", "back"), ("Continue to recordings", "next")]
    elif stage == "recordings":
        for key, label in (
            ("cpu_memory", "CPU and memory recording"),
            ("gpu", "GPU recording"),
            ("trace", "Python call tracing"),
        ):
            value = {None: "Use saved/default setting", True: "On", False: "Off"}[
                draft.get(key)
            ]
            rows.append((label + ": " + value, "toggle:" + key))
        if draft.get("trace") is True:
            rows += [
                (
                    "Trace preset: "
                    + name
                    + (" (selected)" if draft.get("trace_profile") == name else ""),
                    "trace-profile:" + name,
                )
                for name in ("project", "topology", "pilot")
            ]
            rows.append(
                (
                    "Pilot: 4 MiB/process, 32 MiB/run, up to 16 processes. Topology adds CPU observations; neither captures device kernels. Python 3.10/3.11 requires an explicit legacy adapter in saved settings.",
                    None,
                )
            )
        rows += [
            (
                "Research: "
                + (
                    "Basic experiment and evidence"
                    if draft["basic_research"]
                    else "Use saved/default integrations"
                ),
                "research",
            ),
            (
                "GPU recording observes assigned devices; it does not request a GPU. Scientific metrics require an application producer.",
                None,
            ),
        ]
        if not draft.get("captured_plan"):
            rows.append(("Advanced project reporting settings", "advanced"))
        rows += [("Back", "back"), ("Review launch", "next")]
    elif stage == "review":
        plan = state["review"]["plan"]
        rows += [
            ("Project: " + plan["project_root"], None),
            ("Script: " + plan["script"], None),
            (
                "Execution: "
                + (
                    "Standalone workstation"
                    if plan["execution"] == "local"
                    else "Slurm"
                ),
                None,
            ),
            (
                "Workload billing: "
                + (
                    (plan["account"] or "Scheduler default")
                    + " ("
                    + plan["account_source"]
                    + ")"
                    if plan["execution"] == "slurm"
                    else "No Slurm billing; local execution"
                ),
                None,
            ),
            ("Application arguments: " + repr(plan["script_args"]), None),
            ("Working directory: " + plan["workdir"], None),
        ]
        rows += [
            (
                key
                + " = "
                + str(value)
                + " ("
                + plan["option_sources"].get(key, "default")
                + (
                    "; ignored for standalone execution"
                    if plan["execution"] == "local"
                    else ""
                )
                + ")",
                None,
            )
            for key, value in plan["effective_options"].items()
        ]
        rows += [
            (item["label"] + ": " + item["status"] + ". " + item["reason"], None)
            for item in plan["readiness"]
        ]
        rows += [(text, None) for text in plan["compatibility"]]
        budget = plan.get("trace_budget", {})
        if budget.get("enabled"):
            rows += [
                (
                    "Trace budget: "
                    + str(budget["process_bytes"] // (1 << 20))
                    + " MiB/process; "
                    + str(budget["run_bytes"] // (1 << 20))
                    + " MiB/run; "
                    + str(budget["max_processes"])
                    + " processes.",
                    None,
                ),
                (budget["overhead"], None),
                (budget["fidelity"], None),
            ]
        if plan.get("checkpoint_advice"):
            from ..checkpoint_advisor import review_lines

            rows += [(line, None) for line in review_lines(plan["checkpoint_advice"])]
        command_label = (
            "Base launch command; also apply the retained settings shown above"
            if draft.get("captured_plan")
            else "Equivalent command"
        )
        rows += [
            (
                command_label
                + " (run from "
                + plan["submit_dir"]
                + "): "
                + state["review"]["command"],
                None,
            ),
            (
                "The original script stays unchanged. Launching creates a source snapshot and recorded attempt. Closing Raven leaves accepted workloads running.",
                None,
            ),
        ]
        rows.append(("Back to recordings", "back"))
        if not state["attempted"]:
            rows.append(
                (
                    "Launch once on this workstation"
                    if plan["execution"] == "local"
                    else "Submit job once",
                    "submit",
                )
            )
    elif stage == "done":
        receipt = state["receipt"]
        rows += [
            (
                "Submitted job: " + receipt["job_id"]
                if receipt.get("job_id")
                else "Local supervisor started (application outcome is recorded separately).",
                None,
            ),
            ("Run: " + receipt["run_id"], None),
            ("Project: " + receipt["project_root"], None),
        ]
        if receipt.get("pid"):
            rows.append(
                (
                    "Supervisor PID: "
                    + str(receipt["pid"])
                    + ". To stop this still-running supervisor from a shell: kill -TERM "
                    + str(receipt["pid"])
                    + ". Verify its identity before using a retained PID.",
                    None,
                )
            )
        rows += [
            (stream + ": " + path, None)
            for stream, path in receipt.get("logs", {}).items()
        ]
        rows.append(
            ("Open run home: status, logs, recordings, and results", "open-run")
        )
    return rows


def paste(app, text):
    state = initialize(app)
    if not active(app) or state["editing"] is None:
        return False
    field, value = state["editing"]
    if len(value) + len(text) <= 4096 and all(char.isprintable() for char in text):
        state["editing"] = (field, value + text)
        _invalidate(state)
    else:
        _say(
            app,
            "Use at most 4096 printable characters. Paths are not shell-expanded.",
            True,
        )
    return True


def handle_key(app, key):
    if not active(app):
        return False
    state = initialize(app)
    if (
        key == "f8"
        or getattr(app, "interaction_state", {}).get("active")
        and key not in ("esc", "ctrl-c")
    ):
        from ..interaction import handle_key as focus_key

        if focus_key(app, key):
            return True
    if state["editing"] is not None:
        field, value = state["editing"]
        if key == "esc":
            state["editing"] = None
        elif key == "enter":
            state["draft"][field] = value
            state.update(editing=None, review=None, error="")
        elif key in ("backspace", "delete"):
            state["editing"] = (field, value[:-1])
        elif key in ("ctrl-u", "ctrl-a"):
            state["editing"] = (field, "")
        elif key == "space" or len(key) == 1 and key.isprintable():
            paste(app, " " if key == "space" else key)
        _invalidate(state)
        return True
    choices = [action for _, action in _document(app) if action]
    state["cursor"] = max(0, min(state["cursor"], len(choices) - 1))
    if key in ("esc", "q", "ctrl-c"):
        cancel(app)
    elif key in ("tab", "btab", "up", "down", "left", "right"):
        state["cursor"] = (
            state["cursor"] + (-1 if key in ("btab", "up", "left") else 1)
        ) % max(1, len(choices))
        state["follow"] = True
    elif key in ("enter", "space") and choices:
        _activate(app, choices[state["cursor"]])
    elif key in ("pgup", "pgdn", "home", "end"):
        state["top"] = max(
            0,
            {
                "pgup": state["top"] - state["visible"],
                "pgdn": state["top"] + state["visible"],
                "home": 0,
                "end": 10**9,
            }[key],
        )
        state["follow"] = False
    _invalidate(state)
    return True


def handle_mouse(app, y, x, button="left", shift=False):
    if not active(app):
        return False
    state = initialize(app)
    if edit_mouse(app, state, y, x, button, _frame(app), handle_key):
        return True
    if button in ("wheel-up", "wheel-down"):
        state["top"] = max(0, state["top"] + (-3 if button == "wheel-up" else 3))
        state["follow"] = False
        _invalidate(state)
    elif (
        button in ("left", "press")
        and state["painted"] == _frame(app)
        and state["editing"] is None
    ):
        for row, _, item in state["control_hits"]:
            if row == y and item["left"] <= x < item["right"]:
                _activate(app, shlex.split(item["action"][1])[2])
                break
    return True


def overlay(views, snap, app, width, height):
    if not active(app):
        return None
    state = initialize(app)
    from ..ui.prose import _wrap

    inner, document, hits, choice, selected_row = max(1, width - 6), [], [], 0, 0
    for text, action in _document(app):
        text = display_text(text)
        if state["editing"] and action == "field:" + state["editing"][0]:
            text += " -> " + state["editing"][1] + "_"
        wrapped = _wrap(text, max(1, inner - 2) if action else inner, views.g.ascii)
        selected = bool(action and choice == state["cursor"])
        if selected:
            selected_row = len(document)
        for index in range(len(wrapped)):
            line = wrapped.line(index)
            if action:
                line = L.cut(
                    ("> " if selected and index == 0 else "  ") + line,
                    inner,
                    views.g.ascii,
                )
                hits.append(
                    (
                        len(document),
                        "control",
                        {
                            "id": "launchjob:" + str(choice),
                            "label": text,
                            "left": 0,
                            "right": L.vlen(line),
                            "action": (
                                "command",
                                "launchjob action " + shlex.quote(action),
                            ),
                            "group": "launch-job",
                        },
                    )
                )
            document.append(
                [(line, "sel+bold" if selected else "cyan" if action else "text")]
            )
        choice += bool(action)
    editing = state["editing"] is not None
    edit_rows, edit_hits = edit_actions(views.g, inner) if editing else ([], [])
    capacity = max(0, height - 4 - len(edit_rows)) if editing else max(1, height - 5)
    state["visible"] = capacity
    if state["follow"]:
        if selected_row < state["top"]:
            state["top"] = selected_row
        elif selected_row >= state["top"] + capacity:
            state["top"] = max(0, selected_row - capacity + 1)
        state["follow"] = False
    state["top"] = max(0, min(state["top"], max(0, len(document) - capacity)))
    rows = document[state["top"] : state["top"] + capacity]
    footer_start = len(rows)
    if editing:
        rows.extend(edit_rows)
    elif height >= 5:
        rows.append(
            [
                (
                    L.cut(
                        "Tab/Arrows: choose | Enter: apply | PgUp/PgDn: scroll | Esc: close",
                        inner,
                        views.g.ascii,
                    ),
                    "muted",
                )
            ]
        )
    rendered = L.box(views.g, rows, width, height, "Launch job", min_width=10)
    state["control_hits"] = place_hits(
        hits, rendered[1:-2] if height >= 5 else rendered[1:-1], offset=state["top"]
    )
    state["painted"] = _frame(app)
    if editing:
        publish_edit_actions(
            app,
            state,
            edit_hits,
            rendered,
            footer_start,
            state["painted"],
            width,
            height,
        )
    return rendered
