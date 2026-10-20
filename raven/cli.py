"""raven: an interactive terminal dashboard of your Slurm jobs.

    raven                        # interactive (curses)
    raven --watch                # the animated non-interactive screen (Ctrl-C exits)
    raven --once [--tab history] # one frame of text
    raven --json                 # the whole snapshot as JSON
    raven --fake                 # a simulated cluster, to try it anywhere
    raven --write-config         # ~/.config/raven/config.toml with the commented defaults
    raven --profile mycluster    # a named profile from the config (a cluster: host, account, ...)
    raven --host login.example.edu   # remote mode: every command over ssh
    raven --record session.jsonl.gz / --replay session.jsonl.gz --speed 10
    raven run cancel 123 --yes   # one palette command, no screen
    raven report --help         # job-bound Research reporting from Bash
    raven terminal --help       # embedded terminal hooks, replay and diagnostics
    raven --eval 'n_running'     # an expression over the snapshot
    raven --wait-for 'n_pending == 0' --timeout 3600   # block until it holds (exit 0) or time out (exit 2)

Tabs: Jobs (ordered batches, a cursor, marks, actions), Cluster (partitions, GPUs, fair share, the account's
load), History (sacct with efficiency and totals), Analytics (charts), Nodes (the nodes running your jobs), Log
(the selected job's stdout, following), Sources (health of every Slurm command).  ? lists the keys inside."""
from __future__ import annotations

from .user_identity import current_user

import json
import math
import os
import re
import shlex
import shutil
import sys
import time

from . import clock, plugins, screen
from .actions import Actions, Notifier
from .configuration.dashboard import Config, default_path, state_dir
from .controller import App
from .layout import Glyphs
from .model import Store
from .remote import LocalFiles, RemoteFiles, SshBackend
from .sampler import Sampler
from .slurm import Backend, CommandError, FakeBackend, Slurm
from .views import Views


class _Reconnect(Exception):
    """A reviewed profile change, handled after the old session is closed."""

    def __init__(self, path, profile):
        self.path, self.profile = path, profile
        super().__init__(profile)


class _OpenDemo(Exception):
    """Open simulated observations after closing the real connection."""


def _reconnect_config(current, request):
    from .configuration.policy import resolve
    # Keep an explicit extra policy across reconnects. A profile's own policy
    # is resolved from the newly selected profile, as at ordinary startup.
    return resolve(request.path or current.source or None, site=request.profile,
                   launcher="light", site_policy_path=current.extra_policy_path or None)


class Session:
    """Everything one run of the dashboard holds; ``close`` stops the sampler and the recorder."""

    def __init__(self, user, store, sampler, actions, views, app, backend, files, api):
        self.user, self.store, self.sampler, self.actions, self.views, self.app = user, store, sampler, actions, views, app
        self.backend, self.files, self.api = backend, files, api

    def close(self):
        try:
            self._close_services()
        finally:
            if self.store is not None:
                self.store.close()

    def _close_services(self):
        from .historical_ui import shutdown as shutdown_history
        shutdown_history(self.app)
        from raven.collection.diagnostics import close as close_diagnostics
        close_diagnostics(self.app)
        from .terminal.ui import shutdown as shutdown_terminal
        shutdown_terminal(self.app)
        from .tracing.ui import cancel as cancel_trace
        cancel_trace(self.app, clear=True)
        from .job_logs.history_export import cancel as cancel_log_export
        cancel_log_export(self.app, close=True)
        from .data_export_ui import cancel as cancel_data_export
        cancel_data_export(self.app, close=True)
        from .launching.wizard_ui import cancel as cancel_launch
        cancel_launch(self.app, close=True)
        from .execution_ui import close
        close(self.app)
        from .shell_checks_ui import close as close_shell_checks
        close_shell_checks(self.app)
        from .ui.operations import shutdown as shutdown_operations
        shutdown_operations(self.app)
        self.app.save()
        from .ui_persistence import close as close_preferences
        close_preferences(self.app)
        if getattr(self.app, "research", None):
            self.app.research.close()
        if self.app.logs.catalog:
            self.app.logs.catalog.close()
        self.sampler.shutdown()
        from .ui.allocation import shutdown as shutdown_allocation
        shutdown_allocation(self.app)
        for consumer in (getattr(self.sampler, "on_event", None), getattr(self.store, "alerts", None)):
            close = getattr(consumer, "close", None)
            if callable(close):
                close()
        governor = getattr(self.app, "worker_scheduler", None)
        if governor is not None:
            governor.shutdown(wait=False, cancel_futures=True)
        rec = getattr(self.backend, "close", None)
        if rec:
            rec()
        clock.reset()

    def __iter__(self):
        return iter((self.user, self.store, self.sampler, self.actions, self.views, self.app))


def make_backend(args, cfg: Config, user: str):
    """(backend, files reader, replay or None, the user the data belongs to)."""
    replay = None
    if args.replay:
        from .record import ReplayBackend
        replay = ReplayBackend(args.replay, speed=args.speed, paused=args.paused)
        clock.set_source(replay.clock.now)
        backend, files = replay, LocalFiles()
        user = replay.user or user
    elif args.fake:
        backend, files = FakeBackend(user or "alex"), LocalFiles()
    else:
        host = args.host or cfg["host"]
        if host:
            ssh = SshBackend(host, user=args.ssh_user or cfg["ssh_user"], opts=cfg["ssh_opts"])
            backend, files = ssh, RemoteFiles(ssh, timeout=cfg["timeouts"]["command"])
        else:
            backend, files = Backend(), LocalFiles()
    record = args.record or cfg["record"]
    if record and not args.replay:
        from .record import RecordingBackend
        backend = RecordingBackend(backend, os.path.expanduser(record), meta=dict(user=user, host=args.host or cfg["host"], profile=cfg.profile_name), owns_inner=True)
    return backend, files, replay, user


def ascii_mode(args, cfg: Config) -> bool:
    """Explicit glyph preferences override config, within the output encoding's limits."""
    if cfg["theme"] == "reader":
        return True
    encoding = sys.stdout.encoding or "ascii"
    try:
        "█▁⣿┌".encode(encoding)
    except (UnicodeError, LookupError):
        return True
    if args.ascii is not None:
        return args.ascii
    return bool(cfg["ascii"] or os.environ.get("TERM", "dumb") == "dumb")


from .connection_scope import forecast_scope as forecast_scope


def state_namespace(cfg: Config) -> str:
    """Validate an optional path component before any backend or state writes."""
    value = cfg.get("state_namespace", "")
    if not isinstance(value, str) or value and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("state_namespace must be empty or 1-64 ASCII letters, digits, underscores, or hyphens")
    return value


def scoped_state_dir(backend, cfg: Config, user: str) -> str:
    """Keep opt-in connection state separate without querying the scheduler."""
    namespace = state_namespace(cfg)
    base = cfg.get("raven", {}).get("storage", {}).get("state_dir") or state_dir()
    if not namespace:
        return base
    context = forecast_scope(backend, cfg, user)
    if context is None:
        raise ValueError("state_namespace requires a known local owner and connection host")
    import hashlib
    identity = json.dumps(context, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return os.path.join(base, "profiles", namespace, digest)


def build(args, cfg: Config, *, raven_allocation=None) -> Session:
    state_namespace(cfg)
    raven = cfg.get("raven", {})
    managed = raven.get("managed") is True
    worker_mode = getattr(args, "workers", None) or cfg.get("worker_mode", "multi")
    if worker_mode not in ("single", "multi"):
        raise ValueError("worker_mode must be single or multi")
    cfg.set("worker_mode", worker_mode)
    if cfg.get("gpu_provider", "auto") not in ("auto", "nvidia", "amd", "intel"):
        raise ValueError("gpu_provider must be auto, nvidia, amd, or intel")
    if getattr(args, "workers", None):
        cfg.ui_locked_settings = set(getattr(cfg, "ui_locked_settings", ())) | {"worker_mode"}
    from .cadence import poll_position, validate_multiplier
    requested_rate = args.rate if args.rate is not None else cfg.get("polling_multiplier", 1)
    if args.interval and args.rate is None:
        requested_rate = poll_position(args.interval)
    cfg.set("polling_multiplier", validate_multiplier(requested_rate))
    user = args.user or cfg["user"] or current_user()
    if args.fake and not user:
        user = "alex"
    shared_backend = None if args.fake or args.replay else getattr(raven_allocation, "collection_backend", None)
    if shared_backend is not None and not (args.fake or args.replay):
        backend, replay = shared_backend, None
        transport = backend
        while getattr(transport, "inner", None) is not None:
            transport = transport.inner
        files = RemoteFiles(transport, timeout=cfg["timeouts"]["command"]) if isinstance(transport, SshBackend) else LocalFiles()
        record = args.record or cfg["record"]
        if record:
            from .record import RecordingBackend
            # Record at the transport, below the existing owner. This retains
            # exact admission and does not create a second connection/cache.
            shared_backend.inner = RecordingBackend(shared_backend.inner, os.path.expanduser(record),
                                                    meta=dict(user=user, host=cfg["host"], profile=cfg.profile_name), owns_inner=True)
    else:
        backend, files, replay, user = make_backend(args, cfg, user)
    sdir = None if (args.no_state or args.fake or args.replay) else scoped_state_dir(backend, cfg, user)
    if (managed or not (args.fake or args.replay)) and shared_backend is None:
        from .platform_session import wrap_backend
        backend = wrap_backend(backend, cfg, forecast_scope(backend, cfg, user), sdir)
    if managed:
        from raven.collection.ledger import wrap_files, make_placement_verifier
        files = wrap_files(files, backend)
        if raven_allocation is None:
            from .raven_allocation import AllocationManager
            policy = {key: value for key, value in raven.items() if key != "managed"}
            raven_allocation = AllocationManager(policy)
        if getattr(backend, "ledger", None) is not None:
            raven_allocation.collection_backend = backend
            raven_allocation.set_placement_verifier(make_placement_verifier(backend))
    slurm = Slurm(backend, user, timeout=cfg["timeouts"]["command"], gpu_timeout=cfg["timeouts"]["gpu"], action_timeout=cfg["timeouts"]["action"],
                  gpu_provider=cfg.get("gpu_provider", "auto"))
    state_read_only = bool(args.once or args.json or args.csv or args.report or args.eval)
    state_options = dict(read_only=False, strict_owner=not state_read_only, source_scope=forecast_scope(backend, cfg, user),
                         series_storage=cfg.get("raven", {}).get("storage", {}).get("series_format", cfg.get("series_storage", "jsonl")))
    if managed:
        from raven.collection.retention import LightLimits
        light_limits = LightLimits(raven["resources"]["light"], sdir)
        store = Store(state_dir=sdir, persist=sdir is not None, series_keep=int(cfg["series_keep"]), raven_limits=light_limits, **state_options)
    else:
        store = Store(state_dir=sdir, persist=sdir is not None, series_keep=int(cfg["series_keep"]), **state_options)
    if args.interval:
        cfg.set("intervals.jobs", args.interval)
        cfg.ui_locked_settings = set(getattr(cfg, "ui_locked_settings", ())) | {"intervals.jobs"}
    intervals = dict(cfg["intervals"])
    bell_fn = (lambda: sys.stdout.write("\a")) if args.watch else None
    from .worker_scheduler import WorkerScheduler
    limits = raven.get("resources", {}).get("light", {}) if managed else {}
    governor = WorkerScheduler(mode=worker_mode, workers=limits.get("max_workers", 4),
                               max_queue=limits.get("max_worker_queue", 512),
                               aggregate_limit=limits.get("max_workers") if managed else None)
    notifier = Notifier(cfg["notify"]["command"], cfg["notify"]["events"], bell=bell_fn, bell_kinds=("started",) if (args.bell or cfg["bell"]) else (), worker_scheduler=governor)
    sampler = Sampler(slurm, store, intervals, cfg["gpu_types"], history_days=args.days or cfg["history_days"], account=args.account or cfg["account"],
                      gpu_sampling=not args.no_gpu and cfg["gpu_sampling"], on_event=notifier, weather=bool(cfg["weather"]), probes=cfg["weather_probes"],
                      budget=bool(cfg["budget"]), files=files, worker_scheduler=governor,
                      collection=({**raven["collection"], "jitter_fraction": 0.} if args.fake or args.replay else raven["collection"]) if managed else None)
    api = None
    if not args.no_plugins and not managed:
        api = plugins.load(plugins.discover(cfg.path, cfg["plugins"]))
        for name, interval, fn in api.sources:
            sampler.add_source(name, interval, fn)
        sampler.hooks.extend(api.event_hooks)
        for name, err in api.errors.items():
            store.event("plugin", f"plugin {name} failed to load: {err.splitlines()[0]}", name=name)
    actions = Actions(slurm, store, emit=sampler.emit)
    from .alerts import AlertEngine
    rules = list(cfg["alerts"] or [])
    for i, expr in enumerate(args.alert or []):
        rules.append(dict(name=f"alert {i + 1}", when=expr, actions=["bell", "event"], every=600))
    store.alerts = AlertEngine(rules, store, user=user, notify=notifier, bell=bell_fn, worker_scheduler=governor)
    if managed and sdir is not None and not any(os.path.lexists(os.path.join(sdir, name)) for name in ("ui.json", "ui.json.bak")):
        from .installation.config_migration import import_ui
        store.raven_ui_import = import_ui(raven["storage"]["state_dir"], forecast_scope(backend, cfg, user))
    ascii_ = ascii_mode(args, cfg)
    views = Views(Glyphs(ascii_), cfg, files=files, plugins=api)
    app = App(store, sampler, actions, cfg, user, ascii_=ascii_, interactive=not (args.once or args.json or args.csv or args.watch or args.run or args.eval or args.wait_for or args.report))
    # Keep the source and precedence information outside serializable settings.
    # Setup uses it to save the selected profile without flattening other ones.
    app.raven_resolved = getattr(cfg, "raven_resolved", None)
    from .worker_ui import bind
    bind(app, governor)
    from .ui_persistence import bind as bind_preferences
    bind_preferences(app, governor)
    from .gpu_provider_ui import bind as bind_gpu_provider
    bind_gpu_provider(app)
    app.plugins, app.files, app.views_ref = api, files, views
    app.demo = bool(args.fake)
    app.logs.files = files
    app.host_label = (args.host or cfg["host"]) if not (args.fake or args.replay) else ""
    app.replay = replay
    from .research import ResearchHub
    app.research = ResearchHub(cfg, files, demo=args.fake, slurm=slurm, worker_scheduler=governor,
                              settings={k: getattr(args, k) for k in ("metrics_file", "contract", "workdir", "passport", "planning_file")})
    if managed:
        from .ui.allocation import bind as bind_allocation
        from .execution.policy import bind as bind_execution
        bind_allocation(app, raven_allocation)
        bind_execution(app, raven_allocation)
        from raven.collection.retention import bind as bind_limits
        app.raven_limits = light_limits
        bind_limits(app, store=store, views=views, logs=app.logs)
        from raven.collection.diagnostics import bind as bind_diagnostics
        bind_diagnostics(app)
        from .telemetry_settings import sync as sync_telemetry
        sync_telemetry(app)
    if args.rate is not None or args.interval:
        from .refresh_rate import set_multiplier
        set_multiplier(app, requested_rate)
    from .forecast import ForecastTracker
    app.research.forecasts = ForecastTracker()
    app.research.forecast_restore_warning = ""
    app.forecast_scope = forecast_scope(backend, cfg, user) if store.persist else None
    if store.persist:
        state = store.load_ui()
        saved = state.get("forecast_state") if isinstance(state, dict) else None
        scope = saved.get("scope") if isinstance(saved, dict) else None
        scope_matches = (app.forecast_scope is not None and isinstance(scope, dict)
                         and scope == app.forecast_scope
                         and all(type(scope[key]) is type(value) for key, value in app.forecast_scope.items()))
        if isinstance(state, dict) and "forecast_observations" in state:
            app.research.forecast_restore_warning = "Legacy unscoped forecast evidence was ignored; new calibration is collected for this connection."
        if saved is not None and not (isinstance(saved, dict) and type(saved.get("version")) is int and saved["version"] == 1):
            app.research.forecast_restore_warning = "Malformed saved forecast state was ignored; live sampling continues."
        elif saved is not None and not scope_matches:
            app.research.forecast_restore_warning = "Saved forecast evidence belongs to another connection or profile and was ignored."
        elif scope_matches and isinstance(saved, dict):
            try:
                rows = saved.get("observations", [])
                if not isinstance(rows, list):
                    raise ValueError("saved observations must be a JSON array")
                app.research.forecasts.restore(rows)
                if len(rows) != len(app.research.forecasts.observations()):
                    app.research.forecast_restore_warning = "Malformed, duplicate, future, or excess saved forecast records were omitted."
            except (ValueError, TypeError, OverflowError, RecursionError):
                # Corrupt local state cannot stop live polling or become evidence.
                app.research.forecasts.restore([])
                app.research.forecast_restore_warning = "Malformed saved forecast observations were ignored; live sampling continues."
    sampler.job_observers.append(app.research.forecasts.observe)
    if args.research_view:
        app.research_view = args.research_view
    if args.tab:
        app.enter_tab(args.tab)
    if app.theme == "reader":
        views.set_ascii(True)
    if args.no_gpu:
        app.gpu = False
    if args.bell:
        app.bell = True
    if (getattr(args, "getting_started", False) and app.interactive
            and not args.fake and not args.replay):
        from .onboarding_ui import open_setup
        open_setup(app)
    return Session(user, store, sampler, actions, views, app, backend, files, api)


def settle(s: Session, tab: str = None, *, full: bool = False, sources=(), selected_job=None):
    """One sampling round plus the details of the selected job: what a non-interactive run needs before it renders."""
    demand = getattr(s.sampler, "set_collection_demand", None)
    managed = getattr(s.sampler, "collection", None) is not None
    sources = tuple(dict.fromkeys((*sources, *(("nodes", "partitions", "share", "account", "live") if full else ()))))
    deadline = time.monotonic() + 6.0
    if demand is not None:
        demand(tab or s.app.tab, selected_job or s.app.selected_id, sources=sources)
    if managed:
        s.sampler.prime_collection_snapshot()
    # Resolve the first selected job before collecting native metrics. The
    # following round already requests its details; no extra scheduler call
    # is needed, and unverified samples need not be discarded immediately.
    initial_metrics = {}
    if not s.sampler.want_detail and not s.store.jobs:
        for name in ("live", "gpu"):
            if name in s.store.health:
                initial_metrics[name] = s.store.health[name].enabled
                s.store.health[name].enabled = False
    try:
        s.sampler.round(wait=True)
    finally:
        for name, enabled in initial_metrics.items():
            s.store.health[name].enabled = enabled
    s.app.tab = "jobs"
    s.views.compose(s.store.render_snapshot(), s.app, 160, None, s.actions)     # settles the selected job
    if tab == "analytics":
        # The queue's first batch can be pending. Resolve the chart's actual
        # selection before subscribing to its live measurements.
        s.app.tab = tab
        s.views.compose(s.store.render_snapshot(), s.app, 160, None, s.actions)
    target = selected_job or (s.app.analytics_job if tab == "analytics" else s.app.selected_id)
    if target:
        s.sampler.want_detail = target
    if managed:
        demand(tab or "jobs", target, sources=sources)
        s.sampler.prime_collection_snapshot()
    else:
        s.sampler.last_run["details"] = s.sampler.last_run["account"] = 0.0
    detail_source = s.sampler.sources["details"]
    inspection = getattr(s.sampler.slurm.b, "snapshot_inspection", None) if managed else None
    if callable(inspection):
        def selected_inspection():
            with inspection():
                return detail_source()
        s.sampler.sources["details"] = selected_inspection
    try:
        s.sampler.round(wait=True)
    finally:
        s.sampler.sources["details"] = detail_source
    if managed:
        # Honor short admission retry hints within the snapshot's wait budget.
        # Never reset source clocks, endpoint quotas or circuit state.
        while (pending := tuple(s.sampler._collection_deferred)):
            due = min(s.sampler._collection_not_before.get(name, 0.) for name in pending)
            delay = max(0., due - time.time()) + .02
            if time.monotonic() + delay <= deadline:
                time.sleep(delay)
                s.sampler.round(wait=True)
            else:
                break
    from .project_ui import settle as settle_project
    settle_project(s.app, s.store.render_snapshot())
    if tab:
        s.app.tab = tab
    if tab == "research":
        s.app.research.request(s.app.research.context(s.store.render_snapshot(), s.app), wait=True)


def scripted(args, s: Session) -> int:
    """--run, --eval and --wait-for: no screen, an exit code."""
    app = s.app
    if args.run:
        advised, target = False, None
        try:
            words = shlex.split(args.run)
            matches = [command for command in app.commands() if words and command.startswith(words[0])]
            if words and (words[0] == "resubmit" or matches == ["resubmit"]):
                from .resubmit import parse_override_args
                _, identifiers, switches = parse_override_args(words[1:])
                advised = "advised" in switches
                target = identifiers[0] if identifiers else None
        except ValueError:
            pass  # The normal command handler presents its parsing error.
        settle(s, args.tab, sources=("live",) if advised else (), selected_job=target if advised else None)
        if advised:
            target = target or app.selected_id
            job = s.store.job(target) if target else None
            if job is not None and not job.pending:
                live = s.store.render_snapshot()["live"].get(target)
                maximum_age = max(5., s.sampler.effective_interval("live") * 2)
                measured = live is not None and any(
                    value is not None and math.isfinite(value) and value >= 0
                    for value in (live.cpu_time, live.rss, live.avg, live.rate))
                if not measured or not 0 <= clock.now() - live.t <= maximum_age:
                    print(f"resubmit: current live advice is unavailable for {target}; no submission was sent.")
                    return 1
        app.tick()
        jobs = []
        app.run_command(args.run)
        if app.mode == "confirm":
            jobs = app.confirm["jobs"]
            if not args.yes:
                if app.confirm.get("action") == "submit":
                    plan = app.confirm["plan"]
                    print(f"would run in {plan['workdir']}: {plan['command']}\nadd --yes to submit")
                elif app.confirm.get("action") == "resubmit":
                    c = app.confirm["clone"]
                    print(f"would run in {c.workdir or '.'}: {c.command()}\n  {c.probe}" + "".join(f"\n  note: {n}" for n in c.notes) + "\nadd --yes to submit")
                else:
                    print(f"{app.confirm['action']} would apply to {' '.join(j.id for j in jobs)}: add --yes to confirm")
                return 3
            app.finish_confirm(True)
        if app.research_result is not None:
            print(json.dumps(app.research_result, indent=2, ensure_ascii=True, allow_nan=False))
            return 0 if app.command_ok else 1
        if args.json:
            print(json.dumps({"status": "ok" if app.command_ok else "error", "message": app.message or "ok"}, ensure_ascii=True))
            return 0 if app.command_ok else 1
        print(app.message or "ok")
        for e in list(s.store.events)[-len(jobs) if app.message and "sent" in app.message else -1:]:
            if e.get("kind") in ("action", "export", "copy") and not e.get("old"):
                print("  " + e["text"])
        return 0 if app.command_ok else 1
    if args.eval:
        settle(s, args.tab)
        out = app.evaluate(args.eval)
        print(json.dumps({"status": "error", "message": out}) if args.json and out.startswith("eval:") else json.dumps(out) if args.json else out)
        return 1 if out.startswith("eval:") else 0
    from .expr import Expr, ExprError, cluster_ns
    try:
        expr = Expr(args.wait_for)
    except ExprError as e:
        print(f"wait-for: {e}", file=sys.stderr)
        return 1
    t0 = time.time()
    poll = max(0.5, args.poll or s.sampler.effective_interval("jobs"))
    while True:
        s.sampler.round(wait=True)
        try:
            v = expr(cluster_ns(s.store.render_snapshot(), s.user))
        except ExprError as e:
            print(f"wait-for: {e}", file=sys.stderr)
            return 1
        if v:
            print(app.evaluate(args.wait_for))
            return 0
        if args.timeout and time.time() - t0 >= args.timeout:
            print(f"wait-for: '{args.wait_for}' still false after {args.timeout:g}s", file=sys.stderr)
            return 2
        time.sleep(poll)


from .entry.grammar import parse as parse


def gpu_check(args, cfg):
    """Run a bounded evidence check without creating a dashboard session."""
    from copy import copy
    from .gpu_diagnostics import diagnose, render, save
    # Diagnostic evidence uses the explicitly requested private report folder.
    # Do not append to the user's configured session recorder.
    diagnostic_cfg = Config(cfg.data)
    diagnostic_cfg.path, diagnostic_cfg.profile_name = cfg.path, cfg.profile_name
    diagnostic_cfg.set("record", "")
    diagnostic_args = copy(args)
    diagnostic_args.record = ""
    backend = None
    try:
        user = args.user or cfg["user"] or current_user()
        backend, files, replay, user = make_backend(diagnostic_args, diagnostic_cfg, user)
        location = "" if args.no_state or args.fake or args.replay else scoped_state_dir(backend, cfg, user)
        if cfg.get("raven", {}).get("managed"):
            from .platform_session import wrap_backend
            from raven.collection.ledger import wrap_files
            backend = wrap_backend(backend, cfg, forecast_scope(backend, cfg, user), location)
            files = wrap_files(files, backend)
        result = diagnose(cfg, backend, files, user=user, job_id=args.gpu_check,
                          no_gpu=args.no_gpu, state_path=location, no_state=args.no_state)
        if args.fake:
            result["mode"] = "demo"
        elif replay:
            result["mode"] = "replay"
        if args.fake or replay:
            result["scope"] += " This is simulated or recorded evidence, not a live hardware check."
        if args.gpu_check_output:
            result["report_directory"] = save(result, args.gpu_check_output)
        print(json.dumps(result, indent=2, allow_nan=False) if args.json else render(result))
        if args.gpu_check_output and not args.json:
            print(f"\nSaved GPU evidence: {result['report_directory']}")
        return 0 if result["ok"] else 1
    except (OSError, ValueError, TypeError, CommandError) as exc:
        print(f"raven: GPU check: {exc}", file=sys.stderr)
        return 1
    finally:
        close = getattr(backend, "close", None)
        if close:
            close()
        clock.reset()


def _entry_main(argv=None, *, raven=None, raven_allocation=None):
    from .entry.workload import dispatch as dispatch_workload
    try:
        workload = dispatch_workload(list(sys.argv[1:] if argv is None else argv), resolved=raven)
        if workload is not None:
            return workload
    except (OSError, ValueError, RuntimeError) as exc:
        print("Raven: " + str(exc), file=sys.stderr)
        return 2
    if raven is None:
        return _dispatch_main(argv)
    while True:
        try:
            return _managed_main(argv, raven=raven, raven_allocation=raven_allocation)
        except _OpenDemo:
            try:
                raven = _reconnect_config(raven, _Reconnect(raven.source, raven.data["profile"]))
            except (OSError, ValueError, RuntimeError) as exc:
                print("Raven: could not open the demo: " + str(exc), file=sys.stderr)
                return 2
            raven_allocation = None
            argv = ["--fake", "--no-state", "--no-plugins"]
        except _Reconnect as request:
            try:
                raven = _reconnect_config(raven, request)
            except (OSError, ValueError, RuntimeError) as exc:
                print("Raven: could not open the saved profile: " + str(exc), file=sys.stderr)
                return 2
            raven_allocation = None
            # Connection overrides and first-run flags belong to the old
            # session. Keep explicit privacy/presentation choices only.
            argv = [arg for arg in (sys.argv[1:] if argv is None else argv)
                    if arg in {"--no-state", "--no-plugins", "--no-color", "--ascii"}]


def _managed_main(argv, *, raven, raven_allocation=None):
    from .execution.policy import configure_process, process_router, reset_process
    from .execution.admission import cli_admission, needs_job_local
    previous = process_router()
    from .configuration.policy import to_raven_config
    owned_manager = raven_allocation is None
    if owned_manager:
        from .raven_allocation import AllocationManager
        raven_allocation = AllocationManager(raven)
    router = configure_process(to_raven_config(raven)["raven"], raven_allocation)
    driver_backend = None
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        if needs_job_local(arguments) or arguments[:2] == ["trace", "cleanup-runtime"]:
            if getattr(raven_allocation, "collection_backend", None) is None:
                from .entry.placement import configure_placement
                configure_placement(raven, raven_allocation)
                driver_backend = raven_allocation.collection_backend
            router.source_backend = raven_allocation.collection_backend
        unavailable = cli_admission(arguments)
        if unavailable is not None:
            print(json.dumps(unavailable, indent=2, allow_nan=False))
            return 3
        return _dispatch_main(arguments, raven=raven, raven_allocation=raven_allocation)
    finally:
        try:
            if owned_manager:
                raven_allocation.shutdown()
        finally:
            try:
                if driver_backend is not None:
                    close = getattr(driver_backend, "close", None)
                    if close is not None:
                        close()
            finally:
                reset_process(previous)


def _dispatch_main(argv=None, *, raven=None, raven_allocation=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["trust"]:
        from .path_trust_cli import main as trust_main
        return trust_main(argv[1:])
    if argv[:1] in (["update"], ["revert"]):
        from .update import cli as maintenance
        return maintenance(argv)
    from .entry.workload import dispatch as dispatch_workload
    workload = dispatch_workload(argv, resolved=raven)
    if workload is not None:
        return workload
    if argv and argv[0] == "terminal":
        if argv[1:2] == ["selftest"]:
            from .terminal.diagnostics import cli as terminal_selftest
            return terminal_selftest(argv[2:])
        from .terminal.fusion import cli as terminal_main
        return terminal_main(argv[1:])
    if argv and argv[0] == "trace":
        from .tracing.cli import main as trace_main
        return trace_main(argv[1:])
    if argv and argv[0] == "runtime":
        from .runtime_control import main as runtime_main
        return runtime_main(argv[1:])
    if argv and argv[0] == "series":
        from raven.recordings.cli import main as series_main
        return series_main(argv[1:])
    if argv and argv[0] == "query":
        from .query_cli import main as query_main
        return query_main(argv[1:])
    if argv and argv[0] == "mcp":
        from .mcp_server import main as mcp_main
        return mcp_main(argv[1:])
    if argv and argv[0] == "report":
        from .research_reporting import main as reporting_main
        return reporting_main(argv[1:])
    args = parse(argv)
    if args.ui_trace and not sys.stdout.isatty():
        print("raven: --ui-trace requires an interactive terminal", file=sys.stderr)
        return 1
    if args.ui_trace:
        from .ui_trace import capture, TraceError
        try:
            with capture(args.ui_trace) as trace:
                return _main(args, trace) if raven is None else _main(args, trace, raven=raven, raven_allocation=raven_allocation)
        except TraceError as exc:
            print(f"raven: {exc}", file=sys.stderr)
            return 1
    return _main(args) if raven is None else _main(args, raven=raven, raven_allocation=raven_allocation)


def _main(args, ui_trace=None, *, raven=None, raven_allocation=None):
    if args.write_config:
        path = default_path() if sys.version_info >= (3, 11) else default_path()[:-5] + ".json"
        try:
            print(Config.write_default(path))
        except OSError as exc:
            print(f"raven: could not create config: {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        if raven is None:
            cfg0 = Config.load(args.config)
        else:
            from .configuration.policy import to_raven_config, merge as merge_raven_preferences
            legacy = {}
            if raven.legacy_source:
                original = Config.load(raven.legacy_source)
                if raven.legacy_profile:
                    original = original.profile(raven.legacy_profile)
                state_namespace(original)
                legacy = original.data
            cfg0 = Config(legacy)
            for key, value in to_raven_config(raven).items():
                # These are partial preference overlays, not replacements for
                # shell argv, scrollback, or layout settings from the source.
                # Governed caps win; unrelated native preferences survive.
                if key in ("terminal", "workspace") and isinstance(cfg0.get(key), dict):
                    if key == "terminal":
                        requested = cfg0.get(key).get("max_sessions")
                        if type(requested) is int and requested >= 1:
                            value = {**value, "max_sessions": min(requested, value["max_sessions"])}
                    value = merge_raven_preferences(cfg0.get(key), value)
                cfg0.set(key, value)
            cfg0.set("raven.managed", True)
            cfg0.path = raven.source or ""
            cfg0.profile_name = raven.data["profile"]
            cfg0.raven_resolved = raven
            if args.host and args.host != cfg0["host"]:
                raise ValueError("Select the Raven connection with --site/--config before startup; --host cannot retarget an admitted session.")
            if args.ssh_user and args.ssh_user != cfg0["ssh_user"]:
                raise ValueError("The SSH user must match the selected Raven connection.")
            if args.profile:
                raise ValueError("Use --site for Raven profiles; legacy --profile cannot override governed settings.")
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        print(f"raven: configuration: {exc}", file=sys.stderr)
        return 1
    profile = args.profile
    while True:
        try:
            cfg = cfg0.profile(profile) if profile else cfg0
        except KeyError as exc:
            print(f"raven: {exc.args[0]}", file=sys.stderr)
            return 1
        if args.no_color or os.environ.get("NO_COLOR"):
            cfg.set("color", False)
            cfg.ui_locked_settings = set(getattr(cfg, "ui_locked_settings", ())) | {"color"}
        if args.doctor:
            if raven is not None and raven.data["site"]["kind"] == "desktop" and not args.fake:
                from .configuration.policy import doctor as raven_doctor
                result = raven_doctor(raven)
                if args.json:
                    print(json.dumps(result, indent=2, allow_nan=False))
                else:
                    print("Raven / standalone desktop environment\nPrerequisites ready. Slurm is not required.\n" + result["qualification"])
                return 0
            from .doctor import diagnose, render
            from .configuration.dashboard import state_dir as default_state_dir
            path = '' if args.no_state else raven.data['storage']['state_dir'] if raven is not None else os.environ.get('RAVEN_STATE_DIR') or default_state_dir()
            result = diagnose(cfg, fake=args.fake, host=args.host, ssh_user=args.ssh_user, state_dir=path)
            print(render(result, as_json=args.json))
            return 0 if result["ready"] else 1
        if args.gpu_check is not None:
            return gpu_check(args, cfg)
        if args.run:
            from .research_commands import offline
            code = offline(args.run, host=args.host or cfg["host"], replay=bool(args.replay))
            if code is not None:
                return code
        try:
            s = build(args, cfg) if raven is None else build(args, cfg, raven_allocation=raven_allocation)
        except (ValueError, OSError, TypeError) as exc:
            print(f"raven: {exc}", file=sys.stderr)
            return 1
        app, views, store, sampler, actions = s.app, s.views, s.store, s.sampler, s.actions
        if ui_trace is not None:
            app.ui_trace = ui_trace
            sampler.ui_trace = ui_trace
            if getattr(sampler, "worker_scheduler", None) is not None:
                sampler.worker_scheduler.ui_trace = ui_trace
        width = args.width or max(1, shutil.get_terminal_size((130, 40)).columns)
        color = cfg["color"] and sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb"
        try:
            if args.run or args.eval or args.wait_for:
                return scripted(args, s)
            if args.once or args.json or args.csv or args.report:
                settle(s, args.tab, full=bool(args.json))
                if args.report:
                    from . import report
                    app.tick()
                    page = report.build(store.snapshot(), app, views, actions, width=width)
                    if args.report == "-":
                        print(page)
                    else:
                        with open(os.path.expanduser(args.report), "w", encoding="utf-8") as f:
                            f.write(page)
                        print(args.report)
                elif args.json:
                    payload = json.loads(screen.once_json(store))
                    if app.tab == "research":
                        payload["research"] = app.research.current(app.research.context(store.snapshot(), app))
                    print(json.dumps(payload, ensure_ascii=True, allow_nan=False))
                elif args.csv:
                    from .export import tab_csv
                    app.tick()
                    content = tab_csv(views, store.snapshot(), app, actions)
                    print(content if content is not None else f"no table on the {app.tab} tab", end="")
                else:
                    print(screen.once_text(app, views, store, actions, width, color, tab=args.tab))
                return 0
            # A visible first snapshot should not wait for background phase
            # jitter. Producer admission still enforces shared source floors
            # and endpoint budgets before any scheduler command runs.
            if getattr(sampler, "collection", None) is not None:
                sampler.set_collection_demand(app.tab, app.selected_id)
                sampler.prime_collection_snapshot()
            sampler.start()
            try:
                if args.watch or not sys.stdout.isatty():
                    # Painting a clock/status snapshot does not fetch Slurm.
                    # Keep watch readable even under a slow collection floor.
                    screen.run_watch(app, views, sampler, store, actions, cfg, interval=max(0.5, min(1., sampler.effective_interval("jobs"))), color=color)
                else:
                    screen.run_curses(app, views, sampler, store, actions, cfg)
            except KeyboardInterrupt:
                pass
        finally:
            s.close()
        if getattr(app, "start_demo", False) and raven is not None:
            raise _OpenDemo()
        if app.switch_profile and raven is not None:
            raise _Reconnect(getattr(app, "switch_profile_config", "") or raven.source,
                             app.switch_profile)
        if app.switch_profile:                              # :profile <name>: the same screen on another cluster
            profile = app.switch_profile
            continue
        return 0


def main(argv=None, *, raven=None, raven_allocation=None):
    """JSON commands have one document; executed local workloads own their streams."""
    import contextlib
    import io
    words = list(sys.argv[1:] if argv is None else argv)
    if "--json" not in words or "--help" in words or "-h" in words:
        return _entry_main(words, raven=raven, raven_allocation=raven_allocation)
    output = io.StringIO()
    error = None
    with contextlib.redirect_stdout(output):
        try:
            code = _entry_main(words, raven=raven, raven_allocation=raven_allocation)
        except (OSError, ValueError, RuntimeError) as exc:
            error, code = str(exc), 2
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
            error = str(exc.code) if not isinstance(exc.code, int) else "Invalid command arguments" if code else None
    from .cli_output import ApplicationExit
    if isinstance(code, ApplicationExit):
        sys.stdout.write(output.getvalue())
        return code
    raw = output.getvalue().strip()
    try:
        document = json.loads(raw) if error is None else None
    except (ValueError, RecursionError):
        document = None
    if document is None:
        document = {"status": "error" if code else "ok", "message": (error or raw or "Command failed" if code else raw or "ok")[:65536]}
    print(json.dumps(document, ensure_ascii=True, allow_nan=False))
    return code
