"""Read-only wave-two commands and explicit preparation of reviewed candidate plans."""
from __future__ import annotations

import time
from itertools import islice

from .planning import analyze, record, select_job
from .planning_io import load_json
from .research_commands import parser
from .research import clean

COMMANDS = ("predict", "forecast", "blockers", "tradeoffs", "scaling", "workflow", "choose")


def options(cmd, args):
    if cmd not in COMMANDS:
        raise ValueError("unknown planning command")
    p = parser(cmd)
    if cmd in ("predict", "forecast", "blockers"):
        p.add_argument("job_id", nargs="?")
        p.add_argument("--file")
        p.add_argument("--coverage", type=float)
        if cmd == "predict":
            for name in ("name", "partition", "account", "qos"):
                p.add_argument("--" + name)
            for name in ("cpus", "nodes", "gpus"):
                p.add_argument("--" + name, type=int)
        return p.parse_args(args)
    if cmd == "tradeoffs":
        p.add_argument("file")
        p.add_argument("--choose", type=int)
        p.add_argument("--script")
        p.add_argument("--workdir")
        return p.parse_args(args)
    if cmd == "choose":
        p.add_argument("index", type=int)
        p.add_argument("script")
        p.add_argument("--file")
        p.add_argument("--workdir")
        return p.parse_args(args)
    p.add_argument("action", choices=("analyze", "plan"))
    p.add_argument("file")
    p.add_argument("--workdir")
    if cmd == "scaling":
        p.add_argument("--mode", choices=("strong", "weak"))
        p.add_argument("--baseline", type=int)
    return p.parse_args(args)


def is_offline(cmd, args):
    return cmd in ("tradeoffs", "scaling", "workflow", "choose") or cmd in ("predict", "forecast", "blockers") and any(a == "--file" or a.startswith("--file=") for a in args)


def _finished(output, opts, source):
    path = getattr(opts, "file", None)
    simulated = isinstance(source, dict) and source.get("simulated") is True
    opts.source_label = (("SIMULATED frozen observations: " if simulated else "") + str(path)) if path else "scheduler snapshot"
    output["source"] = opts.source_label
    if isinstance(source, dict) and "jobs" in source:
        output["data_jobs"] = [record(job) for job in islice(source["jobs"], 256)]
    return output, opts


def result(cmd, args, *, snap=None, job=None, observations=(), choices=None, execution=None):
    from .raven_execution import router_for, planning_inputs
    router = execution or router_for()
    if router.managed:
        opts = options(cmd, args)
        blocked = router.require(cmd, worker_operation="planning-command")
        if blocked is not None:
            return blocked, opts
        captured = planning_inputs(cmd, None, snap=snap, job=job, observations=observations)
        receipt = router.dispatch("planning-command", dict(command=cmd, args=list(args),
            snap=captured["snap"], job=captured["job"], observations=captured["observations"], choices=choices))
        if receipt.get("status") == "deferred":
            return receipt, opts
        from argparse import Namespace
        return receipt["result"]["output"], Namespace(**receipt["result"]["options"])
    opts = options(cmd, args)
    if cmd == "scaling":
        from .scaling import MAX_BYTES
        source = load_json(opts.file, max_bytes=MAX_BYTES)
    else:
        source = load_json(opts.file) if getattr(opts, "file", None) else None
    snap = snap or {}
    overrides = {}
    opts.analysis_overrides = overrides
    opts.selected_job_id = None
    if cmd in ("predict", "forecast", "blockers"):
        if isinstance(source, list) and cmd in ("forecast", "blockers"):
            source = {"jobs": source}
        mapping = source if isinstance(source, dict) else {}
        jobs = mapping.get("jobs", snap.get("jobs", []) + snap.get("finished", []))
        if opts.job_id:
            job = select_job(jobs, opts.job_id)
            overrides["job_id"] = opts.job_id
        elif "jobs" in mapping or mapping.get("job_id") is not None or job is None or cmd in ("forecast", "blockers") and record(job).get("state") not in ("PENDING", "PD"):
            job = select_job(jobs, mapping.get("job_id"), pending=cmd in ("forecast", "blockers"))
        selected = record(job).get("id") or record(job).get("job_id")
        opts.selected_job_id = str(selected) if selected is not None else None
        if cmd == "predict":
            query = {name: getattr(opts, name) for name in ("name", "partition", "account", "qos", "cpus", "nodes", "gpus") if getattr(opts, name) is not None}
            if query:
                overrides["query"] = query
        if opts.coverage is not None:
            overrides["coverage"] = opts.coverage
        output = analyze(cmd, source, snap=snap, job=job, observations=observations, overrides=overrides)
        return _finished(output, opts, source)
    if cmd in ("tradeoffs", "choose"):
        from .tradeoffs import prepare_choice
        comparison = analyze("tradeoffs", source) if source is not None else choices
        if comparison is None:
            raise ValueError("attach tradeoffs first, or use choose INDEX SCRIPT --file FILE")
        selected = opts.choose if cmd == "tradeoffs" else opts.index
        if selected is not None:
            if not opts.script:
                raise ValueError("--script is required when preparing a tradeoff choice")
            plan = prepare_choice(comparison, selected, opts.script, workdir=opts.workdir)
            comparison = dict(comparison, prepared_plan=plan)
        elif opts.script or opts.workdir:
            raise ValueError("--script and --workdir require --choose INDEX")
        return _finished(comparison, opts, source)
    if cmd == "scaling":
        if opts.action == "analyze" and opts.workdir:
            raise ValueError("--workdir applies to plan, not analysis")
        if opts.action == "plan" and (opts.mode is not None or opts.baseline is not None):
            raise ValueError("--mode and --baseline apply to analysis; edit the scaling recipe for planning")
        overrides["action"] = opts.action
        if opts.workdir is not None:
            overrides["workdir"] = opts.workdir
        if opts.mode is not None:
            overrides["mode"] = opts.mode
        if opts.baseline is not None:
            overrides["baseline"] = opts.baseline
        return _finished(analyze("scaling", source, overrides=overrides), opts, source)
    if cmd == "workflow":
        if opts.action == "analyze" and opts.workdir:
            raise ValueError("--workdir applies to plan, not analysis")
        overrides["action"] = opts.action
        if opts.workdir is not None:
            overrides["workdir"] = opts.workdir
        return _finished(analyze("workflow", source, overrides=overrides), opts, source)
    raise ValueError("unknown planning command")


def success(output):
    if output.get("prepared_plan"):
        return bool(output["prepared_plan"].get("valid"))
    return output.get("status") not in ("error", "invalid", "blocked", "deferred") and output.get("valid") is not False


def execute(app, cmd, args, *, ready=None):
    if cmd not in COMMANDS:
        return False
    hub = app.research
    if hub is None:
        app.fail("planning services are unavailable")
        return True
    try:
        if isinstance(ready, Exception):
            raise ready
        if getattr(app.files, "remote", False) and is_offline(cmd, args) or getattr(app, "replay", None) and cmd == "choose":
            raise ValueError("local planning preparation requires running Tower on the cluster")
        if ready is None:
            snap = app.store.snapshot()
            context = hub.context(snap, app)
            observations = hub.forecasts.observations() if hub.forecasts else ()
            choices = hub.planning_choices
            fn = lambda: result(cmd, args, snap=snap, job=context["job"], observations=observations, choices=choices, execution=hub.execution)
            if app.interactive:
                if not hub.start_task(fn, lambda value: execute(app, cmd, args, ready=value)):
                    raise ValueError("a research operation is still running; try after it completes")
                app.say(f"{cmd}: analyzing in the background")
                return True
            ready = fn()
        output, opts = ready
        app.research_result = output
        view = "tradeoffs" if cmd == "choose" else cmd
        path = getattr(opts, "file", None) or (hub.settings.get("planning_file", "") if cmd == "choose" else "")
        prior = hub.settings.get("planning_overrides", {}) if path == hub.settings.get("planning_file", "") else {}
        overrides = {key: value for key, value in prior.items() if key in COMMANDS and key != "choose"} if isinstance(prior, dict) else {}
        overrides[view] = dict(getattr(opts, "analysis_overrides", {}))
        hub.configure(planning_file=path, planning_overrides=overrides)
        app.research_view, app.tab, app.research_scroll = view, "research", 0
        if cmd in ("predict", "forecast", "blockers"):
            app.research_job_id = getattr(opts, "selected_job_id", None)
        if cmd in ("tradeoffs", "choose"):
            hub.planning_choices = output
        if output.get("prepared_plan"):
            hub.plan = output["prepared_plan"]
            app.research_view = "submit"
            cached = {"status": "ok", "plan": hub.plan}
        else:
            cached = dict(output, source=getattr(opts, "source_label", None) or path or "scheduler snapshot")
        with hub.lock:
            hub.cache[hub._key(hub.context(app.store.snapshot(), app))] = (time.monotonic(), cached)
        app.command_ok = success(output)
        app.say(f"{cmd}: {output.get('status', 'complete')}" + ("; prepared for review in Submit" if output.get("prepared_plan") else ""))
    except Exception as exc:
        app.fail(f"{cmd}: {clean(exc)}")
    return True
