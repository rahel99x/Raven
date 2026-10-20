"""Terminal research commands; offline work never starts a scheduler sampler."""
from __future__ import annotations

import json
import os
import shlex

from .research_text import clean

COMMANDS = ("metric", "metrics", "passport", "validate", "artifacts", "prepare", "submit", "investigate", "array")
OFFLINE = {"metric", "passport", "validate", "prepare"}
BACKGROUND_VIEWS = {"prepare": "submit", "submit": "submit", "array": "submit",
                    "passport": "passport", "validate": "artifacts"}


def _background_context(app, hub):
    """Identity of the workflow that accepted an explicit background command."""
    project = getattr(app, "project_state", {})
    project = project if isinstance(project, dict) else {}
    binding = project.get("binding")
    binding = binding if isinstance(binding, dict) else {}
    return (getattr(app, "tab", None), getattr(app, "mode", None),
            getattr(app, "research_view", None), getattr(app, "research_job_id", None),
            getattr(app, "selected_id", None), getattr(app, "detail_id", None),
            project.get("generation"), project.get("root"),
            tuple(binding.get(key) for key in ("job_id", "run_id", "attempt", "project_root", "run_root")),
            hub.generation)


def _publish_detached(app, hub, cmd, ready, resources):
    """Retain completed work without replacing another workflow or its source."""
    if isinstance(ready, Exception):
        app.fail(f"{cmd}: {clean(ready)}")
        return
    code = 0
    if cmd in OFFLINE:
        result, code = ready
    elif cmd == "submit":
        result, options = ready
    else:
        result = ready
    app.research_background_result = {"command": cmd, "result": result}
    if app.research_result is None:
        app.research_result = result
    changed = False
    if cmd in ("prepare", "submit", "array") and hub.plan is resources[0]:
        hub.plan = result
        changed = True
        if cmd == "submit":
            hub.prepared_submission = (result, options.passport_dir)
    elif cmd == "passport":
        if "passport" in result and hub.passport is resources[1]:
            hub.passport = result["passport"]
            changed = True
        elif "differences" in result and hub.passport_diff is resources[2]:
            hub.passport_diff = result["differences"]
            changed = True
        elif "passport" not in result and "differences" not in result and hub.passport is resources[1]:
            hub.passport = result
            changed = True
    # Cached cards derived from the retained plan/passport must display the
    # completed result when the user deliberately returns to its workspace.
    if changed:
        with hub.lock:
            for key in list(hub.cache):
                if len(key) > 1 and key[1] == BACKGROUND_VIEWS.get(cmd):
                    del hub.cache[key]
    if cmd in ("submit", "array") and (not isinstance(result, dict) or not result.get("valid")):
        app.fail(f"{cmd}: submission preflight failed; the completed result remains available")
        return
    if code:
        app.fail(f"{cmd}: validation failed; the completed result remains available")
    else:
        app.command_ok = True
        app.say(f"{cmd}: complete; current workspace retained" +
                ("; open Research / Submit to review the prepared plan" if cmd in ("prepare", "submit", "array") else ""))


from .entry.command_options import Parser as Parser


from .entry.command_options import parser as parser


from .entry.command_options import sbatch_value_option as sbatch_value_option


from .entry.command_options import command_value_option as command_value_option


from .entry.submission import script_args as script_args


from .entry.submission import prepare_args as prepare_args


def offline_result(cmd, args, *, execution=None):
    from .execution.admission import research_command_route
    routed = research_command_route(cmd, args, execution)
    if routed is not None:
        return routed
    if cmd == "metric":
        from .metrics import write_metric
        p = parser("metric FILE --value NAME=NUMBER")
        p.add_argument("path")
        p.add_argument("--value", action="append", required=True)
        p.add_argument("--step", type=int)
        p.add_argument("--phase", default="")
        p.add_argument("--completed", type=float)
        p.add_argument("--total", type=float)
        p.add_argument("--unit", default="")
        a = p.parse_args(args)
        values = {}
        for value in a.value:
            key, sep, number = value.partition("=")
            if not sep or not key or key in values:
                raise ValueError("metrics must be unique NAME=NUMBER pairs")
            values[key] = float(number)
        return write_metric(os.path.expanduser(a.path), values, step=a.step, phase=a.phase,
                            completed=a.completed, total=a.total, unit=a.unit), 0
    if cmd == "validate":
        from .artifacts import load_contract, validate_contract
        p = parser("validate CONTRACT ROOT")
        p.add_argument("contract")
        p.add_argument("root")
        a = p.parse_args(args)
        result = validate_contract(load_contract(os.path.expanduser(a.contract)), os.path.expanduser(a.root))
        return result, 0 if result.get("valid") else 1
    if cmd == "prepare":
        plan, _ = prepare_args(args)
        return plan, 0 if plan.get("valid") else 1
    if cmd == "passport":
        from . import provenance
        if not args:
            raise ValueError("passport capture SCRIPT | show FILE | compare LEFT RIGHT")
        action, rest = args[0], args[1:]
        if action == "show" and len(rest) == 1:
            return provenance.load(os.path.expanduser(rest[0])), 0
        if action == "compare" and len(rest) == 2:
            return {"differences": provenance.diff(*(os.path.expanduser(x) for x in rest))}, 0
        if action == "capture":
            p = parser("passport capture SCRIPT --workdir DIR --output-dir DIR")
            p.add_argument("script")
            p.add_argument("--workdir", default=os.getcwd())
            p.add_argument("--output-dir", default=".raven/passports")
            p.add_argument("--input", action="append", default=[])
            p.add_argument("--env", action="append", default=[])
            a = p.parse_args(rest)
            passport = provenance.capture(os.path.expanduser(a.workdir), script=os.path.expanduser(a.script),
                                          inputs=a.input, environment_names=a.env)
            saved = provenance.save(passport, os.path.expanduser(a.output_dir))
            return {"path": str(saved), "passport": passport}, 0
        raise ValueError("passport capture SCRIPT | show FILE | compare LEFT RIGHT")
    raise ValueError("unknown research command")


def offline(line, *, host="", replay=False):
    """Return an exit code for an offline command, or None for a dashboard command."""
    try:
        words = shlex.split(line)
        if not words:
            return None
        from .command_registry import CORE_NAMES
        matches = [c for c in CORE_NAMES if c.startswith(words[0])]
        cmd = words[0] if words[0] in CORE_NAMES else matches[0] if len(matches) == 1 else words[0]
        from .planning_commands import COMMANDS as planning_commands, is_offline, result, success
        if cmd in planning_commands and is_offline(cmd, words[1:]):
            if host or replay:
                raise ValueError("offline planning requires local files on the cluster")
            output, _ = result(cmd, words[1:])
            print(json.dumps(output, indent=2, ensure_ascii=True, allow_nan=False))
            return 0 if success(output) else 1
        if cmd not in OFFLINE:
            return None
        if host or replay:
            raise ValueError("offline research commands use local files; run Raven on the cluster for these commands")
        result, code = offline_result(cmd, words[1:])
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
        return code
    except (ValueError, OSError, TypeError) as exc:
        print("research: " + clean(exc))
        return 1


def execute(app, cmd, args, *, ready=None):
    """Return True when handled. Job actions always enter the existing confirmation flow."""
    if cmd == "array" and args and args[0] in ("open", "close"):
        # Published task-page disclosure is immediate UI state. It must not
        # enter the array-retry background operation or submission workflow.
        from .array_disclosure import command
        try:
            command(app, args)
        except ValueError as exc:
            app.fail(str(exc))
        return True
    from .planning_commands import execute as planning_execute
    if planning_execute(app, cmd, args, ready=ready):
        return True
    if cmd not in COMMANDS:
        return False
    args = tuple(args)
    hub = getattr(app, "research", None)
    if hub is None:
        app.fail("research services are unavailable")
        return True
    app.research_result = None
    try:
        if isinstance(ready, Exception):
            raise ready
        if cmd == "submit" and ready is None and getattr(hub, "pending", None):
            raise ValueError("a research operation is still running; review the completed plan before submission")
        if app.interactive and ready is None and (cmd in OFFLINE or cmd == "array" or (cmd == "submit" and args)):
            if getattr(app.files, "remote", False) or getattr(app, "replay", None):
                raise ValueError("this research command requires local files on the cluster")
            from .project_ui import cancel_automatic
            cancel_automatic(app)
            if cmd in OFFLINE:
                execution = getattr(hub, "execution", None)
                fn = (lambda: offline_result(cmd, args, execution=execution)) if getattr(execution, "managed", False) else (lambda: offline_result(cmd, args))
            elif cmd == "submit":
                fn = lambda: prepare_args(args)
            else:
                jobs, finished = list(app.store.jobs), list(app.store.finished)
                from .array_manifest_ui import capture
                manifest = capture(app)
                fn = lambda: array_plan(args, jobs, finished, manifest=manifest, files=hub.files)
            token, origin = object(), None
            resources = (hub.plan, hub.passport, hub.passport_diff)

            def completed(value):
                if getattr(app, "_research_background_request", None) is not token:
                    return
                app._research_background_request = None
                try:
                    if getattr(app, "research", None) is hub and _background_context(app, hub) == origin:
                        execute(app, cmd, args, ready=value)
                    else:
                        _publish_detached(app, hub, cmd, value, resources)
                except Exception as exc:
                    app.fail(f"{cmd}: {clean(exc)}")

            if not hub.start_task(fn, completed):
                raise ValueError("a research operation is still running; try again after it completes")
            app._research_background_request = token
            if cmd != "metric":
                view = BACKGROUND_VIEWS[cmd]
                if app.tab == "research" and app.research_view != view:
                    from .navigation_ui import record
                    record(app, "research", force=True)
                app.enter_tab("research")
                app.mode, app.research_view, app.research_scroll = "main", view, 0
            origin = _background_context(app, hub)
            app.say(f"{cmd}: preparing in the background")
            return True
        if cmd in OFFLINE:
            if getattr(app.files, "remote", False) or getattr(app, "replay", None):
                raise ValueError("this research command requires local files on the cluster")
            execution = getattr(hub, "execution", None)
            result, code = ready if ready is not None else (offline_result(cmd, args, execution=execution)
                if getattr(execution, "managed", False) else offline_result(cmd, args))
            app.research_result = result
            if cmd == "prepare":
                from .ui.research_binding import detach_manual_source
                detach_manual_source(app)
                hub.plan = result
                hub.configure()
                app.research_view = "submit"
            elif cmd == "passport":
                from .ui.research_binding import detach_manual_source
                detach_manual_source(app)
                if "passport" in result:
                    hub.passport = result["passport"]
                elif "differences" in result:
                    hub.passport_diff = result["differences"]
                else:
                    hub.passport = result
                hub.configure(passport="")
                app.research_view = "passport"
            elif cmd == "validate":
                from .ui.research_binding import detach_manual_source
                detach_manual_source(app)
                hub.configure(contract=args[0], workdir=args[1])
                app.research_view = "artifacts"
            if code:
                app.fail(f"{cmd}: validation failed; see Research for evidence")
            else:
                app.command_ok = True
                app.say(f"{cmd}: complete" + (f" | {result['path']}" if isinstance(result, dict) and "path" in result else ""))
        elif cmd in ("metrics", "artifacts"):
            if len(args) != (1 if cmd == "metrics" else 2):
                raise ValueError("metrics FILE" if cmd == "metrics" else "artifacts CONTRACT ROOT")
            from .ui.research_binding import detach_manual_source
            detach_manual_source(app)
            hub.configure(**({"metrics_file": args[0]} if cmd == "metrics" else {"contract": args[0], "workdir": args[1]}))
            app.research_view = "experiment" if cmd == "metrics" else "artifacts"
            app.say(f"{cmd}: attached")
        elif cmd == "investigate":
            if len(args) != 1:
                raise ValueError("investigate JOBID")
            if args[0] not in {j.id for j in app.store.jobs + app.store.finished}:
                raise ValueError("job is not present in the current Jobs or History snapshot")
            from .ui.research_binding import select_job
            select_job(app, args[0], view="evidence")
            app.say(f"investigating {args[0]}")
        elif cmd == "array":
            from .ui.research_binding import detach_manual_source
            detach_manual_source(app)
            from .array_manifest_ui import capture
            hub.plan = ready if ready is not None else array_plan(args, app.store.jobs, app.store.finished,
                                                                  manifest=capture(app), files=hub.files)
            if not hub.plan.get("valid"):
                raise ValueError("retry script failed preflight; prepare a valid script before retrying")
            hub.configure()
            app.research_view = "submit"
            app.say("failed-task retry prepared; review Research / Submit, then :submit")
            if not app.interactive:
                app.confirm, app.mode = {"action": "submit", "jobs": [], "plan": hub.plan}, "confirm"
        elif cmd == "submit":
            if getattr(app, "replay", None):
                raise ValueError("submissions are unavailable during replay")
            if args:
                from .ui.research_binding import detach_manual_source
                detach_manual_source(app)
                hub.plan, opts = ready if ready is not None else prepare_args(args)
                passport_dir = opts.passport_dir
            else:
                from .project_ui import selected_binding
                if selected_binding(app) is not None:
                    raise ValueError("run submission reports are read-only; use :prepare SCRIPT --workdir DIR to review a new submission")
                retained = getattr(hub, "prepared_submission", None)
                passport_dir = retained[1] if retained and retained[0] is hub.plan else None
            if not hub.plan or not hub.plan.get("valid"):
                raise ValueError("prepare a valid batch script first; review issues in Research / Submit")
            hub.configure()
            app.research_view = "submit"
            app.confirm = {"action": "submit", "jobs": [], "plan": hub.plan, "passport_directory": passport_dir}
            app.mode = "confirm"
        if cmd != "metric":
            app.tab = "research"  # preserve an explicitly selected evidence job
        if not app.interactive and cmd in ("metrics", "artifacts", "investigate"):
            app.research_result = hub.request(hub.context(app.store.snapshot(), app), wait=True, force=True)
            if app.research_result.get("status") in ("error", "invalid", "incomplete", "missing"):
                app.command_ok = False
    except Exception as exc:
        app.fail(f"{cmd}: {clean(exc)}")
    return True


def array_plan(args, jobs, finished, *, manifest=None, files=None):
    from .arrays import retry_plan, summarize
    if len(args) < 3 or args[0] != "retry":
        raise ValueError("array retry ARRAYID SCRIPT [--workdir DIR] [--indices RANGE] [--limit N]")
    array_id, script = args[1:3]
    p = parser("array retry")
    p.add_argument("--workdir")
    p.add_argument("--indices")
    p.add_argument("--limit", type=int)
    opts = p.parse_args(args[3:])
    groups = summarize(jobs, finished)
    matching = [g for g in groups if g["id"] == array_id]
    if len(matching) != 1:
        raise ValueError("array is absent or ambiguous across clusters in the current snapshot")
    plan = retry_plan(matching[0], script, indices=opts.indices, limit=opts.limit, workdir=opts.workdir)
    if manifest and manifest[0].matches(matching[0]["id"], matching[0].get("cluster", "")):
        from .array_manifest import verify, retry_metadata
        from .submission import _digest
        frozen, path = manifest[:2]
        submitted = set(manifest[2]) if len(manifest) > 2 else set()
        observed = set(matching[0].get("submit_times", ()))
        if len(observed) > 1 or submitted and observed and submitted != observed:
            raise ValueError("array submission identity changed or is ambiguous; reload its input map before preparing a retry")
        verify(path, files, frozen)
        plan["array_retry"]["manifest"] = retry_metadata(frozen, path, plan["array_retry"]["indices"])
        plan["plan_id"] = _digest(plan)
    return plan


def submission_done(app, result):
    if isinstance(result, Exception):
        # A scheduler may have accepted before the adapter raised. Never retry automatically.
        result = {"ok": False, "submitted": None, "state": "unknown", "job_id": None,
                  "error": "submission outcome unknown: " + clean(result)}
    app.research_result = result
    app.command_ok = bool(result["ok"])
    app.say("submitted " + str(result["job_id"]) if result["ok"] else "submission: " + clean(result.get("error") or result.get("output")))
    if result.get("passport") and app.research:
        app.research.passport = result["passport"]
    app.store.event("action", clean(app.message))
    if app.sampler and result.get("submitted") is not False:
        app.sampler.refresh_all()
