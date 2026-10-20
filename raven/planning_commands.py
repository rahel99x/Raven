"""Read-only wave-two commands and explicit preparation of reviewed candidate plans."""
from __future__ import annotations

import time

from .research_text import clean

COMMANDS = ("predict", "forecast", "blockers", "tradeoffs", "scaling", "workflow", "choose", "checkpoint-advice")


from .execution.planning import options as options


from .execution.planning import is_offline as is_offline


from .execution.planning import _finished as _finished


from .execution.planning import result as _planning_result
from .planning_io import load_json as load_json


def result(cmd, args, *, snap=None, job=None, observations=(), choices=None, execution=None):
    """Released reader injection point; canonical services live below this UI."""
    return _planning_result(cmd, args, snap=snap, job=job, observations=observations,
                            choices=choices, execution=execution, loader=load_json)


from .execution.planning import success as success


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
            raise ValueError("local planning preparation requires running Raven on the cluster")
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
