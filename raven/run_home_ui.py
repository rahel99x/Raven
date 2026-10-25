"""One retained run home; read-only refresh never changes the queue selection."""

from __future__ import annotations

import copy
import shlex
import time

from . import layout as L, run_home
from .control_rows import place_hits
from .job_logs.text import display_text


def initialize(app):
    state = getattr(app, "run_home_state", None)
    if not isinstance(state, dict):
        state = dict(
            binding=None,
            saved=None,
            result=None,
            error="",
            busy=False,
            generation=0,
            revision=0,
            cursor=0,
            top=0,
            visible=1,
            follow=True,
            last_read=0.0,
            pending=False,
            scope=None,
            return_mode="main",
            control_hits=[],
            painted=None,
        )
        app.run_home_state = state
    return state


def command_names():
    return ["runhome"]


def active(app):
    return getattr(app, "mode", "") == "run_home"


def context(app):
    state = initialize(app)
    return state["generation"], state["revision"], state["top"]


def _scope(app):
    from .ui.project_state import _backend_token

    connection = (getattr(getattr(app, "raven_resolved", None), "data", {}) or {}).get(
        "connection", {}
    )
    return (
        _backend_token(getattr(app, "research", None)),
        str(getattr(app, "profile_name", "")),
        tuple(sorted((str(key), repr(value)) for key, value in connection.items())),
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


def save(app):
    state = initialize(app)
    binding = state["binding"] or state["saved"] or {}
    if not binding:
        return {}
    return dict(
        binding={
            key: copy.deepcopy(binding[key])
            for key in (
                "project_root",
                "project_id",
                "experiment_id",
                "run_id",
                "attempt",
                "job_id",
                "launch",
                "automation_identity",
            )
            if key in binding
        },
        profile=str(getattr(app, "profile_name", "")),
    )


def restore(app, data):
    state = initialize(app)
    if not isinstance(data, dict) or not isinstance(data.get("binding"), dict):
        return
    binding = data["binding"]
    from .projects import _IDENT

    if (
        not isinstance(binding.get("project_root"), str)
        or not 0 < len(binding["project_root"]) <= 4096
        or not isinstance(binding.get("run_id"), str)
        or not _IDENT.fullmatch(binding["run_id"])
    ):
        return
    # Saved locations are navigation hints. They are never accepted as evidence.
    safe = {
        key: binding[key]
        for key in (
            "project_root",
            "project_id",
            "experiment_id",
            "run_id",
            "attempt",
            "job_id",
        )
        if key in binding
    }
    for key in ("attempt", "job_id", "project_id", "experiment_id"):
        if safe.get(key) is not None and not isinstance(safe[key], (str, int)):
            return
    for key in ("launch", "automation_identity"):
        value = binding.get(key, {})
        if (
            not isinstance(value, dict)
            or len(value) > 16
            or any(not isinstance(v, (str, int, type(None))) for v in value.values())
        ):
            return
        safe[key] = copy.deepcopy(value)
    state.update(saved=safe, saved_profile=str(data.get("profile", ""))[:256])


def open_run(app, binding=None):
    from .ui.form_state import _pending_review

    if _pending_review(app):
        app.fail("Close the pending job or allocation review before opening Run home.")
        return False
    from .ui.project_state import selected_binding

    state = initialize(app)
    chosen = binding or selected_binding(app)
    if getattr(app, "mode", "main") != "run_home":
        origin = getattr(app, "mode", "main")
        state["return_mode"] = "main" if origin == "palette" else origin
        from .terminal import ui as terminal_ui

        if terminal_ui.focused(app):
            terminal_ui.blur(app)
        from .pointer_routing import begin_gesture

        begin_gesture(app)
    if chosen:
        if not state["binding"] or run_home.identity(chosen) != run_home.identity(
            state["binding"]
        ):
            state.update(
                generation=state["generation"] + 1,
                binding=copy.deepcopy(chosen),
                result=None,
                error="",
                busy=False,
                pending=True,
                last_read=0.0,
                cursor=0,
                top=0,
                follow=True,
                scope=_scope(app),
            )
    app.mode = "run_home"
    _invalidate(state)
    tick(app)
    return True


def open_receipt(app, receipt):
    return open_run(app, run_home.receipt_binding(receipt))


def tick(app):
    state = initialize(app)
    if not active(app) or not state["binding"] or state["busy"]:
        return
    if state["scope"] != _scope(app):
        state.update(
            error="Connection changed. Use the original connection, then Reconnect this run to revalidate its files.",
            pending=False,
        )
        return
    now = time.monotonic()
    if not state["pending"] and now - state["last_read"] < 2.0:
        return
    hub = getattr(app, "research", None)
    if hub is None:
        state["error"] = (
            "The run reader is unavailable. Open a connected Raven session."
        )
        return
    binding, scope, generation = (
        copy.deepcopy(state["binding"]),
        state["scope"],
        state["generation"],
    )
    files = hub.files

    def work():
        if _scope(app) != scope:
            raise ValueError("Connection changed before the run read started")
        return run_home.load(binding, files=files)

    def done(value):
        if generation != state["generation"]:
            return
        state.update(busy=False, last_read=time.monotonic())
        if scope != _scope(app):
            state["error"] = (
                "Connection changed; reconnect this exact run to verify its current state."
            )
            _invalidate(state)
            return
        from .notifications import initialize as center_for

        center = center_for(app)
        key = ("run-home", run_home.identity(binding))
        if isinstance(value, Exception):
            state["error"] = str(value)
            center.condition(
                key,
                state["error"],
                source="Run home",
                job=binding.get("job_id") or "",
                run=run_home.identity(binding),
                observed_at=time.time(),
            )
        else:
            state.update(
                result=value,
                binding=copy.deepcopy(value["selected"]["binding"]),
                error="",
            )
            center.resolve_condition(
                key,
                "The exact run inventory was read and its attempt identity verified.",
                observed_at=time.time(),
            )
            for source in ("launch report", "recordings", "cpu-memory", "gpu"):
                issue = ("run-home-source", run_home.identity(binding), source)
                error = value["errors"].get(source, "")
                if error:
                    center.condition(
                        issue,
                        error,
                        source=source,
                        job=binding.get("job_id") or "",
                        run=run_home.identity(binding),
                        observed_at=time.time(),
                    )
                elif (source == "launch report" and value["report"]) or (
                    source in value["telemetry"]
                    and not value["telemetry"][source].get("error")
                    and value["telemetry"][source]["status"]
                    not in ("failed", "unavailable")
                ):
                    center.resolve_condition(
                        issue,
                        "The declared source was read successfully for this exact attempt.",
                        observed_at=time.time(),
                    )
            if value["telemetry"] and "recordings" not in value["errors"]:
                center.resolve_condition(
                    ("run-home-source", run_home.identity(binding), "recordings"),
                    "The declared telemetry index and stream boundaries were read successfully.",
                    observed_at=time.time(),
                )
        # Completion updates evidence in place; never change cursor/scroll/job.
        _invalidate(state)

    if hub.start_task(work, done):
        state.update(busy=True, pending=False)


def _bind(app):
    state = initialize(app)
    if state["scope"] != _scope(app) or not state["result"] or state["error"]:
        raise ValueError(
            "Reconnect this run and verify its inventory before opening a linked view."
        )
    from .ui.navigation_ports import projects as project_effects

    project_effects(app).bind_run(
        app, state["binding"]["project_root"], state["result"]["selected"]
    )


def _activate(app, action):
    state = initialize(app)
    try:
        if action == "close":
            app.mode = (
                state["return_mode"]
                if state["return_mode"] not in ("run_home", "launch_job", "palette")
                else "main"
            )
        elif action == "historical":
            from .ui.navigation_ports import forms

            forms(app).historical(app)
        elif action == "recover":
            if state["busy"]:
                return
            if not state["binding"]:
                if state.get("saved_profile") != str(getattr(app, "profile_name", "")):
                    raise ValueError(
                        "Select the saved run's original site profile before reconnecting it."
                    )
                state["binding"] = copy.deepcopy(state["saved"])
            if state["binding"]:
                state.update(scope=_scope(app), pending=True, error="")
                tick(app)
        elif action == "again":
            if (
                state["scope"] != _scope(app)
                or not state["result"]
                or not state["result"]["report"]
                or state["error"]
            ):
                raise ValueError(
                    "A verified retained launch report is required to copy its settings."
                )
            from .ui.navigation_ports import forms

            forms(app).relaunch(app, run_home.relaunch_draft(state["result"]["report"]))
        elif action in ("capsule", "lineage"):
            if state["scope"] != _scope(app) or not state["binding"] or state["error"]:
                raise ValueError(
                    "Reconnect and verify this retained run before opening its engineering tools."
                )
            from .ui import operations as tools

            feature = (
                "engineering-capsules"
                if action == "capsule"
                else "engineering-experiments"
            )
            binding = copy.deepcopy(state["binding"])
            tools.run_command(app, ["ops", "open", feature])
            values = tools.initialize(app)["values"]
            values["root"] = binding["project_root"]
            if action == "capsule":
                values["run_id"] = binding["run_id"]
            else:
                from .engineering._storage import name

                values["run_id"] = binding["run_id"]
                values["action"] = "connect"
                label = binding.get("experiment_id") or ""
                values["title"] = label
                try:
                    name(label)
                    values["experiment_id"] = label
                except ValueError:
                    # Scientific labels need not be valid persisted index IDs.
                    values["experiment_id"] = ""
        elif action in ("logs", "analytics", "research", "outputs", "export"):
            _bind(app)
            if action == "logs":
                app.log_job = state["binding"].get("job_id")
                if callable(getattr(app, "enter_tab", None)):
                    app.enter_tab("log")
                else:
                    app.tab = "log"
                app.mode = "main"
            elif action == "analytics":
                app.tab, app.analytics_view, app.mode = "analytics", "job", "main"
                app.analytics_job = state["binding"].get("job_id")
            elif action == "research":
                app.tab, app.research_view, app.mode = "research", "experiment", "main"
            elif action == "outputs":
                from .ui.navigation_ports import forms

                forms(app).outputs(app)
            else:
                from .ui.navigation_ports import forms

                # Use the existing exact-run export capture path, independent
                # of marked jobs or a reused scheduler job ID in the cache.
                app.mode = "project_runs"
                forms(app).export_run(app)
                if getattr(app, "data_export_state", None):
                    app.data_export_state["return_mode"] = "run_home"
        elif action == "connections":
            app.mode = "main"
            from .ui.navigation_ports import forms

            forms(app).connections(app)
    except (ValueError, OSError) as exc:
        state["error"] = str(exc)
        app.fail(str(exc))
    _invalidate(state)


def run_command(app, args):
    if not args or args[0] != "runhome":
        return False
    if len(args) == 1:
        open_run(app)
    elif len(args) == 3 and args[1] == "action" and active(app):
        _activate(app, args[2])
    else:
        app.fail(
            "Use runhome to open the selected run, or reconnect its saved location."
        )
    return True


def _document(app):
    state = initialize(app)
    rows = [("Back", "close")]
    binding = state["binding"] or state["saved"]
    if not binding:
        return rows + [
            (
                "No recorded run is selected. Choose a project and run, or launch a job to create its continuous run home.",
                None,
            )
        ]
    rows += [
        (
            "Run: "
            + str(binding.get("run_id"))
            + " | attempt "
            + str(binding.get("attempt") or "awaiting inventory"),
            None,
        ),
        ("Project: " + binding["project_root"], None),
        ("Job: " + str(binding.get("job_id") or "standalone / awaiting receipt"), None),
    ]
    if state["error"]:
        rows.append(("Read error: " + state["error"], None))
    if state["busy"]:
        rows.append(("Reading this exact run in the background...", None))
    rows += [
        ("Reconnect this run / refresh evidence", "recover"),
        ("Choose original connection", "connections"),
        ("Inspect historical observations (read-only)", "historical"),
        ("Experiment lineage and total resource use", "lineage"),
        ("Capture reproducible execution package", "capsule"),
    ]
    result = state["result"]
    if not result:
        rows.append(
            (
                "Reconnecting reads existing inventories and receipts. It never submits or repeats a workload.",
                None,
            )
        )
        return rows
    inventory = result["inventory"]
    rows.append(("Recorded state: " + str(inventory.get("state", "unknown")), None))
    # Cached scheduler status is supplementary: queue absence is not completion.
    record = (
        getattr(app, "job_record", lambda job: None)(binding.get("job_id"))
        if binding.get("job_id")
        else None
    )
    if record:
        cluster = binding.get("launch", {}).get("cluster")
        if not cluster or getattr(record, "cluster", "") == cluster:
            rows.append(
                (
                    "Scheduler cache for job ID "
                    + str(binding.get("job_id"))
                    + ": "
                    + str(getattr(record, "state", "unknown"))
                    + " (attempt not verified from this cache; retained state above is authoritative)",
                    None,
                )
            )
    report = result["report"]
    if report:
        plan = report["plan"]
        rows += [
            (
                "Scheduler outcome: "
                + str(report["receipt"].get("scheduler_outcome", "not observed")),
                None,
            ),
            (
                "Application exit: "
                + str(report["receipt"].get("application_exit_code", "not recorded")),
                None,
            ),
        ]
        rows += [
            ("Script: " + plan["script"], None),
            ("Source digest: " + plan["script_sha256"], None),
            ("Arguments: " + shlex.join(plan.get("script_args", [])), None),
            (
                "Workload billing: "
                + (
                    plan.get("account") or "scheduler default"
                    if plan["execution"] == "slurm"
                    else "standalone; no Slurm account"
                ),
                None,
            ),
        ]
        rows += [
            (
                key
                + ": "
                + str(value)
                + " ("
                + plan.get("option_sources", {}).get(key, "retained")
                + ")",
                None,
            )
            for key, value in plan.get("effective_options", {}).items()
        ]
        rows.append(("Run again with these settings (review required)", "again"))
    else:
        rows.append(
            (
                "No verified launch report is available. Existing logs and results remain accessible; Run again requires recorded source and settings.",
                None,
            )
        )
    rows += [
        (
            "Open logs (" + str(len(result["selected"]["logs"])) + " declared streams)",
            "logs",
        )
    ]
    rows += [
        (entry.get("label", "Log") + ": " + entry["path"], None)
        for entry in result["selected"]["logs"][:32]
    ]
    for family, label in (("cpu-memory", "CPU and memory"), ("gpu", "GPU")):
        data = result["telemetry"].get(family)
        text = (
            (
                str(data["samples"])
                + " retained samples (bounded tail); "
                + data["status"]
                + ("; " + data["reason"] if data["reason"] else "")
            )
            if data
            else "no retained source declared"
        )
        rows.append((label + ": " + text, None))
    rows += [
        (source + " read error: " + error, None)
        for source, error in result["errors"].items()
    ]
    rows += [
        ("Open Analytics and recordings", "analytics"),
        (
            "Scientific metrics: " + (binding.get("metrics_file") or "not connected"),
            None,
        ),
        ("Open Research", "research"),
        ("Browse declared outputs", "outputs"),
        ("Export this exact run", "export"),
    ]
    rows += [
        ("Binding notice: " + text, None)
        for text in result["selected"]["warnings"][:64]
    ]
    return rows


def handle_key(app, key):
    if not active(app):
        return False
    from .interaction import handle_key as focus_key

    if key == "f8" or getattr(app, "interaction_state", {}).get("active"):
        if focus_key(app, key):
            return True
    state = initialize(app)
    choices = [action for _, action in _document(app) if action]
    state["cursor"] = max(0, min(state["cursor"], len(choices) - 1))
    if key in ("esc", "q", "ctrl-c"):
        _activate(app, "close")
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
    if button in ("wheel-up", "wheel-down"):
        state.update(
            top=max(0, state["top"] + (-3 if button == "wheel-up" else 3)), follow=False
        )
        _invalidate(state)
    elif button in ("left", "press") and state["painted"] == _frame(app):
        for row, _, item in state["control_hits"]:
            if row == y and item["left"] <= x < item["right"]:
                _activate(app, shlex.split(item["action"][1])[2])
                break
    return True


def overlay(views, snap, app, width, height):
    if not active(app):
        return None
    from .ui.prose import _wrap

    state = initialize(app)
    inner, document, hits, choice, selected_row = max(1, width - 6), [], [], 0, 0
    for text, action in _document(app):
        text = display_text(text)
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
                        dict(
                            id="runhome:" + str(choice),
                            label=text,
                            left=0,
                            right=L.vlen(line),
                            action=("command", "runhome action " + action),
                            group="run-home",
                        ),
                    )
                )
            document.append(
                [(line, "sel+bold" if selected else "cyan" if action else "text")]
            )
        choice += bool(action)
    capacity = max(1, height - 5)
    state["visible"] = capacity
    if state["follow"]:
        if selected_row < state["top"]:
            state["top"] = selected_row
        elif selected_row >= state["top"] + capacity:
            state["top"] = max(0, selected_row - capacity + 1)
        state["follow"] = False
    state["top"] = max(0, min(state["top"], max(0, len(document) - capacity)))
    rows = document[state["top"] : state["top"] + capacity]
    if height >= 5:
        rows.append(
            [
                (
                    L.cut(
                        "Tab/Arrows: choose | Enter: open | PgUp/PgDn: scroll | Esc: back",
                        inner,
                        views.g.ascii,
                    ),
                    "muted",
                )
            ]
        )
    rendered = L.box(views.g, rows, width, height, "Run home", min_width=10)
    state["control_hits"] = place_hits(
        hits, rendered[1:-2] if height >= 5 else rendered[1:-1], offset=state["top"]
    )
    state["painted"] = _frame(app)
    return rendered
