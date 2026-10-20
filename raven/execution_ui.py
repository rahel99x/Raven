"""Keyboard preflight forms and deliberately reviewed execution workspaces."""
from __future__ import annotations

from .user_identity import current_user

import os
from pathlib import Path
import queue
import socket
import threading
import time

from . import orchestrator, submission
from .layout import box, vlen
from .research_text import clean

_FIELDS = (("script", "Batch script"), ("workdir", "Working directory"), ("cpus", "CPUs / task"),
           ("mem", "Memory"), ("time", "Wall time"), ("partition", "Partition"), ("gres", "GPU / GRES"))
_FLAGS = {"cpus": "cpus-per-task", "mem": "mem", "time": "time", "partition": "partition", "gres": "gres"}


def command_names():
    return ["preflight", "orchestrate", "execution"]


def initialize(app):
    if not hasattr(app, "execution_state"):
        app.execution_state = {"view": "form", "values": {}, "field": 0, "editing": False,
                               "edit": "", "edit_cursor": 0, "plan": None, "base": None,
                               "review": None, "receipt": None, "receipt_path": "", "index": 0,
                               "top": 0, "detail": False, "detail_scroll": 0, "focus": "nodes",
                               "pending_action": None, "running": False, "cancel": threading.Event(),
                               "progress": queue.Queue(maxsize=64), "progress_text": "", "started": 0.0,
                               "confirm_visible": False, "confirm_screen": None}
    return app.execution_state


def restore(app, ui):
    state = initialize(app)
    saved = ui.get("execution", {}) if isinstance(ui, dict) else {}
    if isinstance(saved, dict) and isinstance(saved.get("receipt_path"), str) and len(saved["receipt_path"]) <= 4096:
        state["receipt_path"] = saved["receipt_path"]


def save(app):
    state = initialize(app)
    return {"execution": {"receipt_path": state["receipt_path"]}}


def close(app):
    initialize(app)["cancel"].set()


def connection_scope(app):
    """Capture a bounded connection identity using only already-known values."""
    slurm = getattr(getattr(app, "actions", None), "slurm", None)
    backend = getattr(slurm, "b", None)
    if backend is None:
        raise ValueError("execution connection scope is unknown; prepare a fresh review in the original connection")
    seen = set()
    for _ in range(8):
        if not hasattr(backend, "inner"):
            break
        if id(backend) in seen:
            raise ValueError("execution backend scope is unknown")
        seen.add(id(backend))
        backend = backend.inner
    else:
        raise ValueError("execution backend scope exceeds its wrapper limit")
    cfg = getattr(app, "cfg", None)
    getter = getattr(cfg, "get", lambda key, default=None: default)
    user = getattr(slurm, "user", None) or getattr(app, "user", "") or current_user()
    configured_host = getter("host", "")
    actual_host = getattr(backend, "host", None) or socket.gethostname()
    cluster = (getattr(slurm, "cluster_name", None) or getter("cluster_name", "")
               or getter("site.cluster_id", "") or os.environ.get("SLURM_CLUSTER_NAME", ""))
    try:
        uid = os.getuid()
    except (AttributeError, OSError) as exc:
        raise ValueError("execution owner scope is unknown") from exc
    return orchestrator.validate_scope({"schema": "raven.execution-scope/v1",
                                       "backend": type(backend).__module__ + "." + type(backend).__qualname__,
                                       "connection_host": actual_host, "configured_host": configured_host,
                                       "profile": getattr(app, "profile_name", getattr(cfg, "profile_name", "")),
                                       "user": user, "uid": uid, "cluster": cluster})


def _require_connection(app, review):
    current = connection_scope(app)
    orchestrator.require_scope(review, current)
    return current


def _enabled(app):
    if getattr(app, "replay", None) or getattr(getattr(app, "files", None), "remote", False):
        raise ValueError("execution forms require Raven running locally on the cluster; replay and SSH views cannot submit")
    if not getattr(app, "actions", None) or not getattr(app.actions, "slurm", None):
        raise ValueError("scheduler actions are unavailable")


def _background(app, function, completion, message):
    state = initialize(app)
    if state["running"]:
        app.fail("an execution operation is already running")
        return False
    state["cancel"].clear()

    def done(value):
        state["running"] = False
        if isinstance(value, Exception):
            app.fail("execution: " + clean(value))
        else:
            completion(value)

    if getattr(app, "interactive", True):
        hub = getattr(app, "research", None)
        if hub is None or not hub.start_task(function, done):
            app.fail("the research worker is busy; try again when it finishes")
            return False
        state["running"], state["started"] = True, time.monotonic()
        app.say(message)
    else:
        try:
            done(function())
        except Exception as exc:
            done(exc)
    return True


def _form_plan(state):
    values = state["values"]
    base = state["base"] or {}
    # Preserve declared inputs/outputs/parameters and flags outside this form.
    from .submission import _options
    issues = []
    parsed = _options(base.get("overrides", []), "overrides", issues)
    editable = {flag.replace("-", "_") for flag in _FLAGS.values()} | {"chdir"}
    overrides = []
    for item in parsed:
        if item["key"] not in editable:
            overrides.append(item["flag"] + ("=" + item["value"] if item["value"] is not None else ""))
    overrides += ["--" + flag + "=" + values[name] for name, flag in _FLAGS.items() if values.get(name)]
    plan = submission.prepare(values.get("script", ""), workdir=values.get("workdir") or None,
                              overrides=overrides, parameters=base.get("parameters"),
                              inputs=base.get("inputs", []), outputs=base.get("outputs", []))
    if isinstance(base.get("array_retry"), dict) and "manifest" in base["array_retry"]:
        import copy
        if plan["resources"].get("array") != base["array_retry"].get("indices"):
            raise ValueError("edited array indices no longer match the reviewed scientific input map")
        plan["array_retry"] = copy.deepcopy(base["array_retry"])
        plan["plan_id"] = submission._digest(plan)
    return plan


def _open_form(app, plan):
    state = initialize(app)
    resources = plan.get("resources", {})
    state.update(view="form", plan=plan, base=plan, field=0, editing=False, detail=False,
                 values={"script": plan["script"], "workdir": plan["workdir"],
                         **{name: str(resources.get(flag.replace("-", "_"), "")) for name, flag in _FLAGS.items()}})
    app.mode = "execution"
    app.say("Preflight: arrows choose a field; Enter edits; p validates; s reviews submission")


def _validated(app, plan):
    state = initialize(app)
    state["plan"] = plan
    if getattr(app, "research", None):
        app.research.plan = plan
    app.command_ok = bool(plan["valid"])
    app.say("Local preflight passed; s opens submission review" if plan["valid"] else "Local preflight found errors; inspect the messages")


def _receipt_done(app, receipt):
    state = initialize(app)
    state.update(receipt=receipt, receipt_path=receipt["receipt_path"], review=receipt["review"],
                 view="receipt", focus="nodes", pending_action=None, detail=False)
    app.mode = "execution"
    app.command_ok = receipt["status"] not in {"blocked"}
    app.say("Execution " + receipt["status"] + "; receipt " + receipt["receipt_path"])
    if getattr(app, "sampler", None):
        app.sampler.refresh_all()


def _load_receipt(app, path):
    receipt = orchestrator.load(path)
    return dict(receipt, receipt_path=str(Path(path).expanduser().absolute()))


def _progress(state, value):
    try:
        state["progress"].put_nowait(value)
    except queue.Full:
        pass


def _confirm_operation(app):
    state = initialize(app)
    action = state["pending_action"]
    if not action:
        return
    try:
        if not state["confirm_visible"]:
            raise ValueError("show the complete action and Confirm control before activation; resize the terminal if needed")
        _enabled(app)
        if not getattr(app, "interactive", True):
            raise ValueError("batch actions require interactive confirmation")
        slurm = app.actions.slurm
        path = state["receipt_path"]
        review = state["review"]
        scope = _require_connection(app, review)
        if action[0] == "start":
            # Reserve before background dispatch so interrupted writes remain findable.
            if not getattr(app, "state_dir", None):
                raise ValueError("execution requires persistent Raven state")
            receipt = orchestrator.create_receipt(review, app.state_dir)
            state["receipt"], state["receipt_path"] = receipt, receipt["receipt_path"]
            path = receipt["receipt_path"]
        if action[0] in {"start", "resume"}:
            fn = lambda: orchestrator.execute(review, slurm, app.state_dir, confirmed=True, receipt_path=path,
                                              cancel=state["cancel"].is_set, progress=lambda value: _progress(state, value), expected_scope=scope)
        elif action[0] == "retry":
            fn = lambda: orchestrator.retry(path, action[1], confirmed=True, expected_review_id=review["review_id"], expected_scope=scope)
        elif action[0] == "recover":
            fn = lambda: orchestrator.recover(path, action[1], action[2], slurm, confirmed=True, expected_review_id=review["review_id"], expected_scope=scope)
        else:
            raise ValueError("unsupported execution action")
        if _background(app, fn, lambda value: _receipt_done(app, value), "Executing the reviewed action once in the background"):
            state["pending_action"], state["view"], state["focus"] = None, "receipt", "nodes"
    except (ValueError, OSError, AttributeError) as exc:
        app.fail("execution: " + clean(exc))


def run_command(app, args):
    if not args or args[0] not in command_names():
        return False
    state = initialize(app)
    command, words = args[0], list(args[1:])
    try:
        if state["running"]:
            raise ValueError("an execution operation is already running; c requests cancellation in its workspace")
        if command == "preflight":
            if getattr(app, "replay", None) or getattr(getattr(app, "files", None), "remote", False):
                raise ValueError("preflight inspects local files; run Raven on the cluster")
            if words:
                from .research_commands import prepare_args
                _background(app, lambda: prepare_args(words)[0], lambda value: _open_form(app, value), "Inspecting the batch script locally")
            else:
                plan = getattr(getattr(app, "research", None), "plan", None)
                if not plan or plan.get("submittable") is False:
                    raise ValueError("preflight SCRIPT [--workdir DIR] [resource flags]")
                _open_form(app, plan)
        elif command == "orchestrate":
            from .research_commands import parser
            from .planning_io import load_json
            if getattr(app, "replay", None) or getattr(getattr(app, "files", None), "remote", False):
                raise ValueError("orchestration reviews require local files on the cluster")
            p = parser("orchestrate workflow|scaling FILE [--workdir DIR]")
            p.add_argument("kind", choices=("workflow", "scaling"))
            p.add_argument("file")
            p.add_argument("--workdir")
            opts = p.parse_args(words)
            scope = connection_scope(app)

            def prepared(value):
                state.update(view="review", review=value, receipt=None, receipt_path="", index=0, top=0,
                             detail=False, focus="nodes", pending_action=("start",), confirm_visible=False)
                app.mode = "execution"
                app.say("Review every node; Tab focuses Cancel / Confirm. Nothing has been submitted")

            _background(app, lambda: orchestrator.prepare_review(opts.kind, load_json(opts.file, max_bytes=8 << 20),
                                                                 workdir=opts.workdir, source=opts.file, scope=scope), prepared,
                        "Preparing a bounded execution review locally")
        else:
            action = words[0] if words else "show"
            if action == "policy":
                if len(words) != 4 or not state["review"]:
                    raise ValueError("Open an orchestration review, then use execution policy QUEUE_MODEL.json LIMITS.json OUTPUT.json")
                if getattr(app, "replay", None) or getattr(getattr(app, "files", None), "remote", False):
                    raise ValueError("Campaign policy export requires the original local review context")
                from .campaign_policy import export_manifest
                import copy
                _require_connection(app, state["review"])
                captured = copy.deepcopy(state["review"])
                captured_receipt = copy.deepcopy(state["receipt"])
                _background(app, lambda: export_manifest(captured, words[1], words[2], words[3], receipt=captured_receipt),
                            lambda path: app.say("Campaign policy exported: " + path + "; open Operations > Campaign submission policy"),
                            "Exporting the immutable campaign review; no jobs are submitted")
            elif action in {"resume", "retry", "recover", "collect"}:
                if not state["receipt_path"]:
                    raise ValueError("open an execution receipt first: execution RECEIPT.json")
                if action == "collect":
                    if getattr(app, "replay", None) or getattr(getattr(app, "files", None), "remote", False):
                        raise ValueError("collect requires the original local scheduler context; SSH and replay observations cannot update receipts")
                    if len(words) != 1:
                        raise ValueError("execution collect")
                    path = state["receipt_path"]
                    scope = connection_scope(app)
                    if state["receipt"]:
                        orchestrator.require_scope(state["receipt"]["review"], scope)
                    snap = app.store.snapshot()
                    _background(app, lambda: orchestrator.collect(path, snap, expected_scope=scope), lambda value: _receipt_done(app, value),
                                "Collecting observed scheduler records")
                else:
                    expected = {"resume": 1, "retry": 2, "recover": 3}[action]
                    if len(words) != expected:
                        raise ValueError("execution resume | retry NODE | recover NODE JOB_ID")
                    receipt = orchestrator.load(state["receipt_path"])
                    _require_connection(app, receipt["review"])
                    selected = next((index for index, node in enumerate(receipt["review"]["nodes"])
                                     if len(words) > 1 and node["id"] == words[1]), None)
                    if len(words) > 1 and selected is None:
                        raise ValueError("execution node is absent from the reviewed receipt")
                    state.update(receipt=receipt, review=receipt["review"], view="review", pending_action=tuple(words),
                                 focus="nodes", detail=False, index=selected or 0, top=0, confirm_visible=False)
                    app.mode = "execution"
                    app.say("Review " + action + "; Tab focuses Cancel / Confirm")
            else:
                path = words[0] if words else state["receipt_path"]
                if len(words) > 1 or not path:
                    raise ValueError("execution RECEIPT.json | resume | retry NODE | recover NODE JOB_ID | collect")
                _background(app, lambda: _load_receipt(app, path), lambda value: _receipt_done(app, value), "Opening the execution receipt")
        return True
    except (ValueError, OSError, AttributeError, TypeError) as exc:
        app.fail(command + ": " + clean(exc))
        return True


def _leave(app):
    state = initialize(app)
    state["editing"], state["pending_action"], state["confirm_visible"] = False, None, False
    app.mode = "main"


def handle_key(app, key):
    if getattr(app, "mode", None) != "execution":
        return False
    state = initialize(app)
    if key == "esc":
        if state["editing"]:
            state["editing"] = False
        elif state["detail"]:
            state["detail"] = False
        else:
            _leave(app)
        return True
    if state["running"]:
        if key in {"c", "C"}:
            state["cancel"].set()
            app.say("Cancellation requested; already accepted jobs keep running")
        return True
    if state["view"] == "form":
        if state["detail"]:
            if key in {"down", "j", "up", "k", "pgdn", "pgup", "home", "end"}:
                step = 10 if key in {"pgdn", "pgup"} else 1
                state["detail_scroll"] = (0 if key == "home" else 1 << 20 if key == "end" else
                                         max(0, state["detail_scroll"] + (step if key in {"down", "j", "pgdn"} else -step)))
                return True
            if key == "d":
                state["detail"] = False
                return True
            if key == "enter":
                return True
        if state["editing"]:
            if key == "space":
                key = " "
            value, cursor = state["edit"], state["edit_cursor"]
            if key == "enter":
                name = _FIELDS[state["field"]][0]
                state["values"][name], state["editing"], state["plan"] = value, False, None
                app.say("Field updated; p validates and refreshes the command preview")
            elif key == "left":
                cursor = max(0, cursor - 1)
            elif key == "right":
                cursor = min(len(value), cursor + 1)
            elif key == "home":
                cursor = 0
            elif key == "end":
                cursor = len(value)
            elif key in {"backspace", "\x7f", "\b"} and cursor:
                value, cursor = value[:cursor - 1] + value[cursor:], cursor - 1
            elif key in {"delete", "dc"}:
                value = value[:cursor] + value[cursor + 1:]
            elif key in {"ctrl-u", "\x15"}:
                value, cursor = "", 0
            elif len(key) == 1 and key.isprintable() and len(value) < 4096:
                value, cursor = value[:cursor] + key + value[cursor:], cursor + 1
            state["edit"], state["edit_cursor"] = value, cursor
        elif key in {"down", "tab", "j", "up", "btab", "k"}:
            state["field"] = (state["field"] + (1 if key in {"down", "tab", "j"} else -1)) % len(_FIELDS)
        elif key == "enter":
            value = state["values"].get(_FIELDS[state["field"]][0], "")
            state.update(editing=True, edit=value, edit_cursor=len(value))
        elif key in {"p", "r"}:
            _background(app, lambda: _form_plan(state), lambda value: _validated(app, value), "Validating the edited fields locally")
        elif key == "d":
            state["detail"], state["detail_scroll"] = True, 0
        elif key == "h":
            from .shell_checks_ui import open_for_plan
            open_for_plan(app, state["plan"])
        elif key == "s":
            plan = state["plan"]
            if not plan or not plan.get("valid"):
                app.fail("validate a valid plan with p before reviewing submission")
            elif plan.get("submittable") is False:
                app.fail("sealed workflow plans require :orchestrate")
            else:
                try:
                    _enabled(app)
                    app.confirm = {"action": "submit", "jobs": [], "plan": plan}
                    app.mode = "confirm"
                except ValueError as exc:
                    app.fail(clean(exc))
        return True
    nodes = (state["review"] or {}).get("nodes", [])
    if key in {"tab", "btab"}:
        state["detail"] = False  # confirmation controls must be visible before activation
        choices = ["nodes", "cancel", "confirm"] if state["pending_action"] else ["nodes"]
        current = choices.index(state["focus"]) if state["focus"] in choices else 0
        state["focus"] = choices[(current + (1 if key == "tab" else -1)) % len(choices)]
    elif key in {"down", "j", "up", "k", "pgdn", "pgup", "home", "end"}:
        if state["detail"]:
            step = 10 if key in {"pgdn", "pgup"} else 1
            state["detail_scroll"] = max(0, state["detail_scroll"] + (step if key in {"down", "j", "pgdn"} else -step))
        else:
            step = 10 if key in {"pgdn", "pgup"} else 1
            state["index"] = (0 if key == "home" else max(0, len(nodes) - 1) if key == "end" else
                              min(max(0, len(nodes) - 1), max(0, state["index"] + (step if key in {"down", "j", "pgdn"} else -step))))
            state["focus"] = "nodes"
    elif key in {"enter", "d"}:
        if key == "enter" and state["focus"] == "cancel":
            _leave(app)
        elif key == "enter" and state["focus"] == "confirm":
            _confirm_operation(app)
        else:
            state["detail"], state["detail_scroll"] = not state["detail"], 0
    elif key == "c":
        if state["receipt_path"] and not state["pending_action"]:
            run_command(app, ["execution", "collect"])
    elif key == ":":
        app.mode, app.palette_edit = "palette", "execution "
    return True


def _wrap(text, width, *, ascii_=False):
    """Bounded, display-safe command wrapping with no terminal escape output."""
    value = clean(text, ascii_=ascii_, limit=128 << 10)
    width = max(1, width)
    rows, current, used = [], [], 0
    for character in value:
        amount = vlen(character)
        if current and used + amount > width:
            rows.append("".join(current))
            current, used = [], 0
        current.append(character)
        used += amount
    if current:
        rows.append("".join(current))
    return rows or [""]


def overlay(views, snap, app, width, height):
    if getattr(app, "mode", None) != "execution":
        return None
    state = initialize(app)
    from . import modal_scrollbars as B
    scroll_spec = None
    state["confirm_visible"] = False
    state["control_hits"] = []
    state["confirm_screen"] = (width, height)
    while True:
        try:
            value = state["progress"].get_nowait()
            state["progress_text"] = (str(value.get("position", "")) + "/" + str(value.get("total", "")) + " "
                                      + clean(value.get("node", "")) + " / " + clean(value.get("status", "")))
        except queue.Empty:
            break
    usable = max(1, height - 6)
    inner = max(1, width - 8)
    lines = []
    if state["running"]:
        elapsed = max(0, time.monotonic() - state["started"])
        lines.append([(f" Working {elapsed:.1f}s  {state['progress_text']}  c requests cancellation", "yellow")])
    if state["view"] == "form":
        title = "Resource and submission workbench"
        for index, (name, label) in enumerate(_FIELDS):
            value = state["edit"] if state["editing"] and index == state["field"] else state["values"].get(name, "")
            prefix = views.g.cursor if index == state["field"] else " "
            lines.append([(f" {prefix} {label:<18} ", "cyan+bold" if index == state["field"] else "dim"),
                          (clean(value) or "(inherit)", "rev" if index == state["field"] else "")])
        plan = state["plan"]
        lines.append([(" [Shell checks] ", "accent"), ("h   Enter edit   p validate   d command/errors   s review submit   Esc back", "dim")])
        lines.append([(" LOCAL PREFLIGHT: " + ("VALID" if plan and plan["valid"] else "ERRORS" if plan else "NEEDS VALIDATION"),
                       "green+bold" if plan and plan["valid"] else "yellow+bold")])
        if plan:
            lines += [[(" " + part, "cyan")] for part in _wrap(plan["command"], inner, ascii_=views.g.ascii)[:max(1, usable - len(lines) - 1)]]
            for issue in plan["issues"][:max(0, usable - len(lines))]:
                lines.append([(" " + clean(issue["message"]), "red" if issue["level"] == "error" else "yellow")])
        if state["detail"]:
            detail_lines = [[(" Exact local command and validation messages", "cyan+bold")]]
            if plan:
                detail_lines += [[(" " + part, "cyan")] for part in _wrap(plan["command"], inner, ascii_=views.g.ascii)]
                for issue in plan["issues"]:
                    detail_lines += [[(" " + part, "red" if issue["level"] == "error" else "yellow")]
                                     for part in _wrap(issue["level"].upper() + ": " + issue["message"], inner, ascii_=views.g.ascii)]
            else:
                detail_lines.append([(" Changed fields need p validation before a command can be reviewed", "yellow")])
            state["detail_scroll"] = min(state["detail_scroll"], max(0, len(detail_lines) - max(1, usable - 1)))
            page = max(1, usable - 1)
            context = ("execution-form-details", id(plan), width)
            target, painted = B.window(app, "modal:execution-details", state["detail_scroll"], len(detail_lines), page, context=context)
            state["detail_scroll"] = target
            lines = detail_lines[painted:painted + page]
            scroll_spec = ("modal:execution-details", 0, len(detail_lines), page, target, painted,
                           lambda value: state.update(detail_scroll=value), context)
            lines.append([(" Arrows/PgDn scroll; Esc returns to fields; p validates; s reviews submission", "dim")])
        # Keep the selected form field visible in very short terminals.
        elif len(lines) > usable:
            top = max(0, min(state["field"] - usable + 1, len(_FIELDS) - 1))
            context = ("execution-form", id(plan), width)
            target, painted = B.window(app, "modal:execution-form", top, len(lines), usable,
                                       context=context, focus=state["field"])
            scroll_spec = ("modal:execution-form", 0, len(lines), usable, target, painted, lambda value: None, context)
            lines = lines[painted:painted + usable]
    else:
        review = state["review"] or {}
        nodes = review.get("nodes", [])
        receipt = state["receipt"] or {}
        action = state["pending_action"]
        title = ("Review " + action[0] if action else "Execution") + " / " + review.get("kind", "batch")
        # A box uses two border rows and keeps one screen row outside each edge.
        # Reserve critical action text and its controls before optional content.
        usable = max(1, height - 4)
        summary = []
        if action:
            if action[0] in {"retry", "recover"}:
                summary = ["Target node: " + action[1]]
                if action[0] == "recover":
                    summary.append("Scheduler job: " + action[2])
                else:
                    summary.append("Prepare a new attempt; resume submits it")
            elif action[0] == "resume":
                amount = sum(node.get("state") == "planned" for node in receipt.get("nodes", []))
                summary = [f"Resume {amount} unattempted jobs from the reviewed batch",
                           "Uses real parent IDs and unique --comment receipt markers"]
            else:
                summary = [f"Submit {len(nodes)} reviewed jobs once",
                           "Uses real parent IDs and unique --comment receipt markers"]
        summary_rows = [[(" " + part, "yellow+bold")] for text in summary
                        for part in _wrap(text, max(1, inner - 1), ascii_=views.g.ascii)]
        controls = [(" [Cancel] ", "rev" if state["focus"] == "cancel" else "dim"),
                    (" [Confirm] ", "yellow+rev" if state["focus"] == "confirm" else "yellow")]
        controls_width = sum(vlen(text) for text, _ in controls)
        can_confirm = bool(action and height >= 3 and width - 4 >= controls_width
                           and usable >= len(summary_rows) + 1 and not state["running"] and not state["detail"])
        footer = summary_rows + [controls] if action and can_confirm else (
            summary_rows[:max(0, usable - 1)] + [[(" [Cancel]  Resize to review and confirm", "yellow")]] if action else
            [[(" Receipt: " + state["receipt_path"], "dim")]] if state["receipt_path"] else [])
        if not action and receipt.get("collection") and usable >= len(footer) + 2:
            collection = receipt["collection"]
            analysis = collection.get("analysis", {})
            footer.append([(f" Collected: {len(collection.get('records', []))} scaling records; {len(collection.get('missing', []))} missing; analysis {analysis.get('status', 'not applicable')}", "cyan")])
        available = max(0, usable - len(footer))
        header = [(f" {len(nodes)} reviewed jobs  {receipt.get('status', 'not submitted')}  sequential bounded dispatch", "cyan+bold")]
        body = []
        if state["detail"] and nodes:
            selected = nodes[min(state["index"], len(nodes) - 1)]
            current = next((item for item in receipt.get("nodes", []) if item["id"] == selected["id"]), {})
            content = ["Node: " + selected["id"], "State: " + current.get("state", "planned") + "   Job ID: " + str(current.get("job_id") or "pending receipt"),
                       "Dependencies: " + (", ".join(selected["depends_on"]) or "none"), "Workdir: " + selected["plan"]["workdir"],
                       "Script SHA256: " + selected["plan"]["script_sha256"], "Exact reviewed base command:"]
            scope = review.get("scope")
            if scope:
                content[2:2] = [f"Connection: {scope['connection_host']}  profile={scope['profile'] or 'default'}  user={scope['user']}  owner UID={scope['uid']}",
                                f"Backend: {scope['backend']}  cluster={scope['cluster'] or 'not configured'}"]
            else:
                content[2:2] = ["Connection scope: unknown; inspect only; prepare a fresh scoped review"]
            content += _wrap(selected["plan"]["command"], inner, ascii_=views.g.ascii)
            if selected["depends_on"]:
                content += ["Execution adds --dependency=afterok:<verified upstream job IDs>"]
            content += ["Execution adds a unique --comment receipt identity for verified recovery."]
            for index, attempt in enumerate(current.get("attempts", []), 1):
                content.append(f"Attempt {index}: {attempt['state']}  job {attempt.get('job_id') or '?'}")
                content += _wrap(attempt.get("output", ""), inner, ascii_=views.g.ascii)
            observation = current.get("observation")
            if observation:
                content += [f"Observed: {observation['state']}  elapsed {observation['elapsed']}  exit {observation['exit']}"]
            content += ["Esc returns to the node list; arrows scroll"]
            state["detail_scroll"] = min(state["detail_scroll"], max(0, len(content) - max(1, usable - 1)))
            page = max(1, usable - 1)
            context = ("execution-node-details", id(review), selected["id"], width)
            target, painted = B.window(app, "modal:execution-details", state["detail_scroll"], len(content), page, context=context)
            state["detail_scroll"] = target
            lines = [[(" " + line, "")] for line in content[painted:painted + page]]
            scroll_spec = ("modal:execution-details", 0, len(content), page, target, painted,
                           lambda value: state.update(detail_scroll=value), context)
            lines.append([(" Esc returns; arrows scroll. Tab shows action controls", "dim")])
        else:
            # At compact sizes omit the generic header and help before any target
            # or control. Every visible node still has Enter details and arrows.
            if state["running"] and available:
                body.append([(f" Working {max(0, time.monotonic() - state['started']):.1f}s  {state['progress_text']}  c cancels later submissions", "yellow")])
            if available - len(body) >= 3:
                body.append(header)
            page = max(0, available - len(body) - (1 if available - len(body) >= 2 else 0))
            state["top"] = max(0, min(state["top"], max(0, len(nodes) - page)))
            if page and state["index"] < state["top"]:
                state["top"] = state["index"]
            if page and state["index"] >= state["top"] + page:
                state["top"] = state["index"] - page + 1
            context = ("execution-review", id(review), width)
            target, painted = B.window(app, "modal:execution-nodes", state["top"], len(nodes), page,
                                       context=context, focus=state["index"])
            state["top"] = target
            scroll_spec = ("modal:execution-nodes", len(body), len(nodes), page, target, painted,
                           lambda value: state.update(top=value), context)
            current_by_id = {item["id"]: item for item in receipt.get("nodes", [])}
            for index in range(painted, min(len(nodes), painted + page)):
                node = nodes[index]
                current = current_by_id.get(node["id"], {})
                prefix = views.g.cursor if index == state["index"] else " "
                body.append([(f" {prefix} {node['id']:<20} {current.get('state', 'planned'):<14} job {current.get('job_id') or '-'}", "rev" if index == state["index"] else ""),
                             ("  <- " + ",".join(node["depends_on"]) if node["depends_on"] else "", "dim")])
            if len(body) < available:
                body.append([(" Arrows select; Enter details; Tab buttons; : execution; c collect", "dim")])
            if action and available >= 5 and len(body) < available:
                body.append([(" Adds concrete upstream dependencies and unique --comment receipt identities.", "dim")])
            lines = body[:available] + footer
            state["confirm_visible"] = can_confirm
    presentation = [[(clean(text, ascii_=views.g.ascii, limit=128 << 10), style) for text, style in row] for row in lines[:usable]]
    rendered = box(views.g, presentation, width, height, clean(title, ascii_=views.g.ascii), min_width=30)
    if scroll_spec is not None:
        key, start, count, page, target, painted, setter, context = scroll_spec
        rendered = B.boxed(app, key, rendered, start=start, count=count, page=page, target=target,
                           painted=painted, setter=setter, context=context, header=-1)
    if state["view"] == "form" and not state["detail"] and not state["running"]:
        for y, x, row in rendered:
            text = "".join(segment for segment, _ in row)
            position = text.find("[Shell checks]")
            if position >= 0:
                left = x + vlen(text[:position])
                state["control_hits"].append((y, "control", {
                    "id": "execution:shell-checks", "label": "Shell checks", "left": left,
                    "right": left + len("[Shell checks]"), "group": "execution_form",
                    "action": ("click", y, left), "choice": "shell-checks"}))
    if state["pending_action"] and not state["detail"] and presentation:
        index = len(presentation) - 1
        if index < len(rendered) - 2:
            y, x, painted = rendered[index + 1]
            text = "".join(segment for segment, _ in painted)
            for choice, label in (("cancel", "[Cancel]"), ("confirm", "[Confirm]")):
                position = text.find(label)
                if position < 0 or choice == "confirm" and not state["confirm_visible"]:
                    continue
                left = x + vlen(text[:position])
                state["control_hits"].append((y, "control", {
                    "id": "execution:" + choice, "label": choice, "left": left,
                    "right": left + len(label), "group": "execution_confirmation",
                    "action": ("click", y, left), "choice": choice}))
    state["control_token"] = (id(state["review"]), id(state["pending_action"]), state["running"], state["detail"])
    return rendered


def handle_mouse(app, y, x, button="left", shift=False):
    if getattr(app, "mode", None) != "execution" or button != "left":
        return False
    state = initialize(app)
    if state.get("control_token") != (id(state["review"]), id(state["pending_action"]), state["running"], state["detail"]):
        return True
    for row, _, value in state.get("control_hits", []):
        if row == y and value["left"] <= x < value["right"]:
            if value["choice"] == "shell-checks":
                handle_key(app, "h")
                return True
            state["focus"] = value["choice"]
            # Reuse exact reviewed-plan, visibility, scope and mutation guards.
            handle_key(app, "enter")
            return True
    return False
