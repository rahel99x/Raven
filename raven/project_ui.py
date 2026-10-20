"""Cached project/run picker and declared artifact explorer for the terminal UI."""
from __future__ import annotations

import os
import copy
import time
import shlex
from concurrent.futures import TimeoutError as FutureTimeoutError

from . import layout as L, projects, artifact_pages
from .log_presentation import json_page
from .research_text import clean

MODES = {"project_runs", "project_outputs", "project_preview"}


def initialize(app):
    if not isinstance(getattr(app, "project_state", None), dict):
        app.project_state = {}
    state = app.project_state
    defaults = {"root": "", "runs": [], "warnings": [], "run_warnings": [], "binding": None, "logs": [], "status": "empty", "summary": "Choose a project with :project PATH",
                "generation": 0, "busy": False, "run_cursor": 0, "run_top": 0, "output_cursor": 0, "output_top": 0, "filter": "", "filtering": False,
                "collapsed": [], "tree": None, "preview": None, "preview_scroll": 0, "restore_run_id": "", "notices_open": False, "notices_scroll": 0,
                "preview_node": None, "preview_pages": [(0, 0)], "preview_page": 0, "preview_columns": None,
                "preview_sort": None, "preview_column": 0, "preview_collapsed": [], "preview_format": None}
    defaults.update(registered_root="", binding_origin="", auto_generation=0, auto_pending=False,
                    auto_last=0.0, auto_target=None, auto_roots=(), auto_suppressed=None,
                    auto_status="idle", auto_summary="", auto_backend=None, auto_attempt=None)
    for key, value in defaults.items():
        state.setdefault(key, value)
    return state


def restore(app, data):
    state = initialize(app)
    if not isinstance(data, dict):
        return
    root, run_id = data.get("project_root", ""), data.get("run_id", "")
    if isinstance(root, str) and 0 < len(root) <= 4096 and all(ch.isprintable() for ch in root):
        state["root"] = root
        state["registered_root"] = root
        state["summary"] = "Saved project; :runs reloads and revalidates its inventories"
    if isinstance(run_id, str) and projects._IDENT.fullmatch(run_id):
        state["restore_run_id"] = run_id


def save(app):
    state = initialize(app)
    binding = state.get("binding") or {}
    return {"project_root": state.get("root", ""), "run_id": binding.get("run_id", state.get("restore_run_id", ""))}


def selected_binding(app):
    value = initialize(app).get("binding")
    return value if isinstance(value, dict) else None


def status_row(app, width, ascii_=False):
    """Pure cached attachment feedback for Research headers and Jobs panels."""
    state = initialize(app)
    binding = state.get("binding")
    if binding:
        sources = sum(bool(binding.get(key)) for key in ("metrics_file", "contract", "passport", "planning_file", "submit_file"))
        sources += len(binding.get("planning_files", {}))
        text = (f" {'Auto-linked' if state.get('binding_origin') == 'automatic' else 'Run'} {binding['run_id']}"
                f" | job {binding.get('job_id') or 'not submitted'} | {sources} declared reports | {len(state.get('logs', []))} logs")
        if _array_receipt(binding):
            text += " | array launch receipt; task states in Arrays"
        style = "cyan"
    elif state.get("auto_status") in ("ambiguous", "incomplete", "changed", "error", "unavailable"):
        text, style = " Reports: " + state.get("auto_summary", ""), "yellow"
    else:
        return []
    return [(L.cut(clean(text, ascii_), max(0, width), ascii_), style)]


def log_entries(app):
    return list(initialize(app).get("logs", []))


def _array_receipt(value):
    launch = value.get("launch", value.get("inventory", {}).get("metadata", {}).get("raven_launch", {}))
    return isinstance(launch, dict) and launch.get("array_submission_receipt") is True


def resolve_log_entry(app):
    """Resolve the chosen run's current exact stream before drawing or copying."""
    binding = selected_binding(app)
    if not binding:
        return None

    def selected(entry):
        role = entry.get("role")
        if role not in ("stdout", "stderr"):
            role = next((name for name in ("stderr", "stdout") if binding.get(name) and binding[name] == entry.get("path")), role)
        if role in ("stdout", "stderr"):
            app.logs.which = "err" if role == "stderr" else "out"
        return dict(entry)

    explicit = app.logs.entry
    if isinstance(explicit, dict) and explicit.get("path") and explicit.get("run_id", binding["run_id"]) == binding["run_id"]:
        return selected(explicit)
    entries = log_entries(app)
    if app.logs.file_index > 0 and entries:
        return selected(entries[(app.logs.file_index - 1) % len(entries)])
    role = "stderr" if app.logs.which == "err" else "stdout"
    path = binding.get(role, "")
    if path:
        existing = next((entry for entry in entries if entry.get("path") == path), None)
        return selected(existing) if existing else {"id": "project." + role, "path": path, "label": role, "group": "Run streams", "role": role,
                                              "source": "project", "run_id": binding["run_id"], "job_id": binding.get("job_id")}
    # Absence of a stderr declaration cannot turn into an unrelated stdout copy.
    return selected(entries[0]) if role == "stdout" and entries else None


def cycle_log_entry(app):
    """Move through the cached declarations without discovery or source reads."""
    if not selected_binding(app):
        return None
    entries = log_entries(app)
    if not entries:
        return None
    current = resolve_log_entry(app)
    current_index = next((index for index, entry in enumerate(entries) if current and entry.get("path") == current.get("path")), -1)
    chosen = dict(entries[(current_index + 1) % len(entries)])
    from .log_tools import before_source_change
    before_source_change(app)
    app.logs.entry, app.logs.file_index = chosen, 0
    if chosen.get("role") in ("stdout", "stderr"):
        app.logs.which = "err" if chosen["role"] == "stderr" else "out"
    app.logs.browser, app.logs.browse_return = False, False
    app.logs.path, app.logs.top, app.logs.match, app.logs.last_bookmark = "", None, None, None
    app.logs._buffer_token = None
    app.logs.clear_selection(reset_cursor=True)
    app.log_selection_expected, app.log_render_token = False, None
    return chosen


def clear_binding(app, *, suppress_auto=False):
    """Leave the chosen run without losing the project inventory or manual paths."""
    state = initialize(app)
    sampler = getattr(app, "sampler", None)
    if hasattr(sampler, "bind_telemetry"):
        sampler.bind_telemetry(None)
    if suppress_auto:
        state["auto_suppressed"] = _target_job(app)
    state["auto_generation"] += 1
    state["auto_pending"] = False
    from .array_manifest_ui import detach_automatic
    detach_automatic(app)
    backup = state.pop("binding_backup", None)
    if backup is not None and app.research is not None:
        original = dict(backup.get("settings_extra", {}), **backup["settings"])
        app.research.settings = copy.deepcopy(original)
        app.research.configure()
        app.research.passport, app.research.passport_diff = backup["passport_record"], backup["passport_diff"]
        app.cfg["logs"]["manifest_file"] = backup["log_manifest"]
    if state.get("binding") or backup is not None:
        from .log_tools import before_source_change
        before_source_change(app)
        state.update(binding=None, binding_origin="", logs=[], run_warnings=[], tree=None, preview=None, restore_run_id="")
        app.research_job_id, app.log_job, app.log_record = None, None, None
        if app.research is not None and backup is None:
            app.research.passport, app.research.passport_diff = None, None
        app.logs.entry, app.logs.entries = None, []
        app.logs.path, app.logs.top, app.logs.match, app.logs.last_bookmark = "", None, None, None
        app.logs._buffer_token = None
        app.logs.candidates.clear()
        app.logs.clear_selection(reset_cursor=True)
        app.log_selection_expected = False
    if state["busy"]:
        state["generation"] += 1
        state["busy"] = False


def command_names():
    return ["project", "runs", "run", "outputs", "artifact"]


def _hub(app):
    if app.research is None:
        from .research import ResearchHub
        files = getattr(app, "files", None) or getattr(getattr(app, "logs", None), "files", None)
        app.research = ResearchHub(app.cfg, files=files)
    return app.research


def _task(app, title, worker, complete, failed=None):
    state = initialize(app)
    hub = _hub(app)
    files = hub.files
    backend = _backend_token(hub)
    cancel_automatic(app)
    if state["busy"]:
        app.fail("A project read is already running; wait for its result")
        return False
    token = state["generation"] + 1
    state["generation"], state["busy"] = token, True

    def finished(value):
        if token != state["generation"]:
            return
        state["busy"] = False
        if getattr(app, "research", None) is not hub or hub.files is not files or _backend_token(hub) != backend:
            state["summary"] = title + ": connection changed; select the project again"
            return
        if isinstance(value, Exception):
            if failed is not None:
                failed()
            state["summary"] = f"{title}: {clean(value)}"
            app.fail(state["summary"])
            return
        complete(value)

    if not hub.start_task(worker, finished):
        state["busy"] = False
        app.fail("The background reader is busy; retry this project command shortly")
        return False
    app.say(title + " in the background")
    return True


def cancel_automatic(app):
    """Give an explicit project/research command priority without blocking."""
    from .job_progress import cancel_automatic as cancel_progress
    cancel_progress(app)
    state = initialize(app)
    hub = getattr(app, "research", None)
    if state.get("auto_pending"):
        pending = getattr(hub, "pending", None)
        if pending and pending[1] is state.get("auto_callback"):
            if hasattr(hub, "cancel_task"):
                hub.cancel_task(pending[1])
            else:
                hub.pending = None
        state["auto_generation"] += 1
        state["auto_pending"] = False
        state["auto_last"] = time.monotonic()


def _refresh_runs(app, root):
    state = initialize(app)
    hub = _hub(app)
    files = hub.files
    state["registered_root"] = os.path.abspath(os.path.expanduser(root))
    state["auto_generation"] += 1
    state["auto_last"] = 0.0

    def complete(value):
        previous = state.get("binding") or {}
        same_root = value["root"] == state.get("root")
        state.update(root=value["root"], runs=value["runs"], warnings=value["warnings"], status=value["status"], summary=value["summary"], limited=value["limited"])
        state["auto_last"] = time.monotonic()
        if not same_root:
            if previous:
                clear_binding(app)
            state.update(binding=None, logs=[], run_warnings=[], tree=None, preview=None, run_cursor=0, run_top=0, filter="", collapsed=[])
        wanted = previous.get("run_id") or state.get("restore_run_id") if same_root else None
        state["run_cursor"] = next((index for index, run in enumerate(state["runs"]) if run["run_id"] == wanted), 0)
        app.say(value["summary"])

    return _task(app, "Read project inventories", lambda: projects.discover_project(root, files=files), complete)


def _apply_run(app, value, *, automatic=False, refresh=False):
    from .log_tools import before_source_change
    state = initialize(app)
    binding = value["binding"]
    sampler = getattr(app, "sampler", None)
    if hasattr(sampler, "bind_telemetry"):
        sampler.bind_telemetry(binding if binding.get("telemetry_index") else None)
    previous = state.get("binding") or {}
    current_entry = resolve_log_entry(app) if previous else None
    source_changed = (any(previous.get(key) != binding.get(key) for key in
                          ("project_root", "run_id", "job_id", "attempt", "metrics_file", "contract", "run_root", "passport",
                           "planning_file", "planning_files", "submit_file", "log_manifest", "stdout", "stderr"))
                      or state.get("logs", []) != value["logs"])
    identity_changed = any(previous.get(key) != binding.get(key) for key in ("project_root", "run_id", "job_id", "attempt", "launch", "remote_identity"))
    new_log_paths = {entry["path"] for entry in value["logs"]}
    previous_log_paths = {entry["path"] for entry in state.get("logs", [])}
    current_path = current_entry.get("path") if current_entry else None
    # The native catalog also contains scheduler and discovered files outside
    # the run's declarations. A report refresh does not revoke those sources.
    native_source = bool(current_entry and (current_entry.get("source") in ("scheduler", "discovered", "directory")
                          or str(current_entry.get("id", "")).startswith("scheduler.")))
    project_source = bool(current_entry and (current_entry.get("source") == "project"
                           or current_path in previous_log_paths and not native_source))
    reset_logs = (not refresh or identity_changed
                  or project_source and current_path not in new_log_paths)
    if reset_logs:
        before_source_change(app)
    if "binding_backup" not in state:
        hub = _hub(app)
        keep = {key: hub.settings.get(key, "") for key in ("metrics_file", "contract", "workdir", "passport")}
        keep.update({key: hub.settings[key] for key in ("planning_file", "planning_files", "planning_overrides", "submit_file")
                     if hub.settings.get(key)})
        state["binding_backup"] = {"settings": copy.deepcopy(keep),
                                   "settings_extra": copy.deepcopy({key: value for key, value in hub.settings.items() if key not in keep}),
                                   "log_manifest": app.cfg["logs"].get("manifest_file", ""), "passport_record": hub.passport, "passport_diff": hub.passport_diff}
    shown_state = "array launch receipt; task states in Arrays" if _array_receipt(binding) else binding["state"]
    state.update(binding=binding, logs=value["logs"], run_warnings=value["warnings"], restore_run_id=binding["run_id"],
                 summary=f"{'Linked' if automatic else 'Selected'} {binding['run_id']} / attempt {binding.get('attempt', '?')} / {shown_state}")
    if source_changed or not refresh:
        state.update(tree=None, preview=None)
    if not refresh:
        state["binding_origin"] = "automatic" if automatic else "manual"
        state["auto_last"] = time.monotonic()
    hub = _hub(app)
    settings = {"metrics_file": binding["metrics_file"], "contract": binding["contract"], "workdir": binding["run_root"], "passport": binding["passport"],
                "planning_file": binding.get("planning_file", ""), "planning_files": copy.deepcopy(binding.get("planning_files", {})),
                "submit_file": binding.get("submit_file", ""), "planning_overrides": {}}
    passport_changed = hub.passport != value.get("passport_record")
    if identity_changed or any(hub.settings.get(key) != value for key, value in settings.items()) or passport_changed:
        if refresh and not identity_changed and hasattr(hub, "rebind_sources"):
            hub.rebind_sources(passport_changed=passport_changed, **settings)
        else:
            hub.configure(**settings)
    from .array_manifest_ui import apply_automatic
    apply_automatic(app, value)
    # A prior run's in-memory passport must never appear under the new identity.
    app.research.passport = value.get("passport_record")
    app.research.passport_diff = None
    app.cfg["logs"]["manifest_file"] = binding["log_manifest"]
    if not automatic and not refresh:
        app.research_job_id = binding["job_id"]
        app.selected_id = binding["job_id"]
        app.log_job = binding["job_id"]
        app.log_record = None
    if refresh and not reset_logs:
        return
    app.logs.entry = None
    app.logs.entries = []
    app.logs.browser = False
    app.logs.candidates.clear()
    app.logs.clear_selection(reset_cursor=True)
    app.logs.path, app.logs.top, app.logs.match, app.logs.last_bookmark = "", None, None, None
    app.logs._buffer_token = None
    app.logs.which, app.logs.file_index = "out", 0
    app.log_selection_expected = False
    if not automatic and not refresh:
        app.tab, app.research_view, app.mode = "research", "experiment" if binding["metrics_file"] else "artifacts", "main"
        app.research_scroll = 0
        app.say(state["summary"] + (f"; {len(value['warnings'])} binding notices (:runs)" if value["warnings"] else ""))


def _select(app, run_id, *, home=False):
    state = initialize(app)
    if not state["root"]:
        raise ValueError("Choose a project first with :project PATH")
    root = state["root"]
    hub = _hub(app)
    files = hub.files
    origin = (getattr(app, "mode", "main"), getattr(app, "selected_id", None))
    intent = object()
    previous_intent = state.get("home_intent")
    if home:
        state["home_intent"] = intent
    def selected(value):
        if home and (state.get("home_intent") is not intent or getattr(app, "quit", False)
                     or origin != (getattr(app, "mode", "main"), getattr(app, "selected_id", None))):
            return
        _apply_run(app, value)
        if home:
            from .run_home_ui import open_run
            state["home_intent"] = None
            open_run(app, binding=value["binding"])
    started = _task(app, "Bind run " + clean(run_id), lambda: projects.select_run(root, run_id, files=files), selected)
    if home and not started:
        state["home_intent"] = previous_intent
    return started


def _target_job(app):
    if getattr(app, "tab", "") in ("jobs", "history"):
        return getattr(app, "selected_id", None)
    if getattr(app, "tab", "") in ("log", "logs") and getattr(app, "log_job", None):
        return app.log_job
    return getattr(app, "research_job_id", None) or getattr(app, "selected_id", None)


def _attempt_token(job, details):
    """Scheduler identity only; transient state/utilization never resets a view."""
    restart = details.get("Restarts", details.get("RestartCnt"))
    restart = str(restart) if restart is not None else ""
    return (getattr(job, "cluster", "") or details.get("Cluster", ""),
            details.get("SubmitTime") or getattr(job, "submit", ""),
            details.get("StartTime") or getattr(job, "start", ""), restart)


def _backend_token(hub):
    if hub is None:
        return None
    from .project_remote import identity, supported
    return (id(hub), identity(hub.files) if supported(hub.files) else id(hub.files))


def tick(app, snap=None, *, force=False):
    """Schedule bounded automatic project reads; this hook performs no file IO.

    Call once per UI iteration after selection changes. Completion is published
    by ResearchHub.poll_task on the UI thread. A selected manual run wins until
    the user selects a different job; a manual file attachment suppresses
    automatic binding for that job until selection changes.
    """
    state = initialize(app)
    existing_hub = getattr(app, "research", None)
    current_backend = _backend_token(existing_hub)
    if state.get("auto_backend") is not None and current_backend != state["auto_backend"]:
        clear_binding(app)
        state.update(auto_backend=current_backend, auto_pending=False, auto_last=0.0)
    target = _target_job(app)
    binding = state.get("binding")
    if binding and target != binding.get("job_id"):
        # A job change must immediately restore manual paths, even when a slow
        # prior worker has not returned. Its stale callback cannot reattach.
        clear_binding(app)
        if getattr(app, "tab", "") not in ("jobs", "history", "log", "logs"):
            app.research_job_id = target
        binding = None
    if state["auto_suppressed"] is not None:
        if state["auto_suppressed"] == target:
            return False
        state["auto_suppressed"] = None
    if snap is None:
        job, details = app.store.record_context(target)
    else:
        # Explicit snapshots retain their caller's publication, including
        # frozen/report views. Ordinary UI ticks only need one exact record.
        job = next((job for records in (snap.get("jobs", []), snap.get("finished", []),
                                       snap.get("departed_jobs", {}).values())
                    for job in records if job.id == target), None)
        details = snap.get("details", {}).get(target, {}) if target else {}
    workdir = details.get("WorkDir") or getattr(job, "workdir", "")
    attempt_token = _attempt_token(job, details)
    old_attempt = state.get("auto_attempt")
    # A new nonempty observation can disambiguate an old selection. Unknown
    # fields becoming known count as a refresh but do not imply another run.
    if old_attempt is not None and any(old and new and old != new for old, new in zip(old_attempt, attempt_token)):
        if binding:
            clear_binding(app)
            binding = None
        state.update(auto_generation=state["auto_generation"] + 1, auto_pending=False, auto_last=0.0)
    registered = state.get("registered_root") or (state.get("root") if state.get("binding_origin") == "manual" else "")
    roots = tuple(projects.job_project_roots(workdir, registered))
    if binding and state.get("binding_origin") == "manual":
        roots = (binding["project_root"],)
    intent = (target, roots, attempt_token)
    if intent != (state["auto_target"], state["auto_roots"], state.get("auto_attempt")):
        state["auto_generation"] += 1
        state.update(auto_target=target, auto_roots=roots, auto_attempt=attempt_token, auto_pending=False, auto_last=0.0)
    if not roots or state["busy"] or state["auto_pending"]:
        return False
    hub = _hub(app)
    from .project_remote import supported as remote_supported
    if not projects.local_supported(hub.files) and not remote_supported(hub.files):
        state.update(auto_status="unavailable", auto_summary="Automatic project binding requires Raven on the machine that owns the project files")
        return False
    backend = _backend_token(hub)
    if state.get("auto_backend") is not None and state["auto_backend"] != backend:
        clear_binding(app)
        binding = None
        state.update(auto_generation=state["auto_generation"] + 1, auto_pending=False, auto_last=0.0)
    state["auto_backend"] = backend
    from .cadence import file_interval
    interval = file_interval(getattr(hub, "interval", 5.0), getattr(hub, "polling_multiplier", 1), remote=bool(hub.files.remote))
    now = time.monotonic()
    if not force and state["auto_last"] and now - state["auto_last"] < interval:
        return False
    # Share the existing single research reader without queuing behind a render.
    future = getattr(hub, "future", None)
    if getattr(hub, "pending", None) or future is not None and not future.done():
        return False
    token, project_token = state["auto_generation"], state["generation"]
    chosen = copy.deepcopy(binding) if binding else None
    manual = bool(chosen and state.get("binding_origin") == "manual")
    files = hub.files
    cluster = getattr(job, "cluster", "") or details.get("Cluster", "")
    restart_text = attempt_token[3]
    restart_count = int(restart_text) if restart_text.isascii() and restart_text.isdigit() and len(restart_text) <= 9 else None

    def worker():
        if manual:
            found = projects.discover_project(chosen["project_root"], files=files)
            selected = projects.select_run(chosen["project_root"], chosen["run_id"], files=files)
            if any(selected["binding"].get(key) != chosen.get(key) for key in ("job_id", "run_id", "attempt", "experiment_id")):
                return {"status": "changed", "summary": "Selected run changed job identity; its old sources were detached", "selected": None, "projects": [found]}
            # Preserve an explicitly chosen passport when multiple records exist.
            if chosen.get("passport") and not selected["binding"].get("passport"):
                try:
                    if chosen.get("passport_explicit"):
                        selected["binding"]["passport_explicit"] = True
                    passport = projects.read_bound_passport(selected["binding"], chosen["passport"], files=files)
                    selected["binding"]["passport"] = chosen["passport"]
                    selected["passport_record"] = passport
                except (OSError, ValueError):
                    pass
            return {"status": "ready", "summary": found["summary"], "selected": selected, "projects": [found]}
        if target is None:
            found = projects.discover_project(roots[0], files=files)
            return {"status": "ready", "summary": found["summary"], "selected": None, "projects": [found]}
        return projects.discover_job(list(roots), target, files=files, cluster=cluster, restart_count=restart_count)

    def complete(value):
        if (token != state["auto_generation"] or project_token != state["generation"] or _target_job(app) != target
                or getattr(app, "research", None) is not hub or hub.files is not files):
            return
        if _backend_token(hub) != backend:
            state.update(auto_pending=False, auto_last=0.0)
            return
        if snap is None:
            current_job, current_details = app.store.record_context(target)
            if _attempt_token(current_job, current_details) != attempt_token:
                state.update(auto_pending=False, auto_last=0.0)
                return
        state["auto_pending"] = False
        state["auto_last"] = time.monotonic()
        if isinstance(value, Exception):
            state.update(auto_status="error", auto_summary="Project refresh: " + clean(value))
            if selected_binding(app):
                clear_binding(app)
            return
        state.update(auto_status=value["status"], auto_summary=value["summary"])
        selected = value.get("selected")
        found = next((project for project in value.get("projects", []) if selected and project["root"] == selected["binding"]["project_root"]), None)
        if found is None:
            found = next((project for project in value.get("projects", []) if project["root"] == state.get("root")), None)
        if found is None and value.get("projects"):
            found = value["projects"][0]
        if found:
            wanted = (state.get("binding") or {}).get("run_id") or state.get("restore_run_id")
            cursor_id = state["runs"][state["run_cursor"]]["run_id"] if 0 <= state["run_cursor"] < len(state["runs"]) else wanted
            state.update(root=found["root"], runs=found["runs"], warnings=found["warnings"], limited=found["limited"], status=found["status"])
            state["run_cursor"] = next((index for index, run in enumerate(found["runs"]) if run["run_id"] == cursor_id), 0)
        if selected:
            _apply_run(app, selected, automatic=not manual, refresh=bool(state.get("binding")))
        elif state.get("binding"):
            clear_binding(app)

    if not hub.start_task(worker, complete):
        return False
    state["auto_callback"] = complete
    state["auto_pending"] = True
    state["auto_last"] = now
    return True


def settle(app, snap=None, *, timeout=90):
    """Offline --once helper: wait for one scheduled read and publish it."""
    hub = _hub(app)
    hub.poll_task()
    tick(app, snap, force=True)
    pending = getattr(hub, "pending", None)
    if pending and hasattr(pending[0], "result"):
        try:
            pending[0].result(timeout=timeout)
        except FutureTimeoutError:
            cancel_automatic(app)
            clear_binding(app)
            initialize(app).update(auto_status="error", auto_summary="Project discovery timed out; select the run after its reader finishes")
        except Exception:
            # The completion callback publishes a bounded error and restores
            # prior paths; an unavailable report must not crash --once.
            pass
        hub.poll_task()
    return selected_binding(app)


def _outputs(app):
    state = initialize(app)
    settings = _hub(app).settings
    contract, root = settings.get("contract", ""), settings.get("workdir", "")
    if not contract or not root:
        raise ValueError("Select a run with an output contract, or attach :artifacts CONTRACT ROOT first")
    hub = _hub(app)
    app.mode = "project_outputs"

    def complete(value):
        state.update(tree=value, preview=None, collapsed=[], output_cursor=0, output_top=0, filter="", filtering=False)
        app.say("Declared outputs: " + value["summary"])

    return _task(app, "Inspect declared outputs", lambda: projects.artifact_tree(contract, root, files=hub.files), complete)


def _visible_runs(state):
    query = state.get("filter", "").casefold()
    return [run for run in state["runs"] if not query or query in " ".join(str(run.get(key) or "") for key in ("run_id", "experiment_id", "state", "job_id", "name")).casefold()]


def _visible_outputs(state):
    tree = state.get("tree") or {}
    collapsed = state.get("collapsed", [])
    query = state.get("filter", "").casefold()
    return [node for node in tree.get("nodes", []) if (query or not any(node["path"].startswith(parent + "/") for parent in collapsed))
            and (not query or query in node["path"].casefold() or query in node["status"].casefold())]


def _preview(app, node):
    if node.get("directory"):
        state = initialize(app)
        path = node["path"]
        if path in state["collapsed"]:
            state["collapsed"].remove(path)
        else:
            state["collapsed"].append(path)
        return
    state = initialize(app)
    root = state["tree"]["root"]
    hub = _hub(app)

    def complete(value):
        state.update(preview=value, preview_scroll=0, preview_node=node, preview_pages=[(0, 0)], preview_page=0,
                     preview_columns=None, preview_sort=None, preview_column=0, preview_collapsed=[], preview_format=None)
        app.mode = "project_preview"
        app.say(value["summary"])

    _task(app, "Preview " + clean(node["path"]), lambda: artifact_pages.read_page(root, node["specification"], files=hub.files), complete)


def _preview_page(app, direction=0, *, reset=False):
    state = initialize(app)
    node = state.get("preview_node")
    if not node or not state.get("tree"):
        raise ValueError("Open a declared artifact first with :outputs")
    if state["busy"]:
        raise ValueError("An artifact page is already being read")
    preview = state.get("preview") or {}
    pages = [(0, 0)] if reset else list(state["preview_pages"])
    page_index = 0 if reset else state["preview_page"] + direction
    if page_index < 0:
        app.say("First artifact page")
        return False
    if page_index >= len(pages):
        if not preview.get("has_next"):
            app.say("Last artifact page")
            return False
        pages.append((preview["next_offset"], preview["next_row"]))
    offset, row = pages[page_index]
    root = state["tree"]["root"]
    hub = _hub(app)
    columns = None if state["preview_columns"] is None else list(state["preview_columns"])
    sort = state["preview_sort"]
    collapsed = list(state["preview_collapsed"])
    specification = dict(node["specification"])
    if state["preview_format"] is not None:
        specification["format"] = state["preview_format"]
    def failed():
        state["preview_sort"] = preview.get("sort")
        state["preview_columns"] = preview.get("columns")
    def complete(value):
        old_identity = preview.get("identity")
        if not reset and old_identity is not None and value.get("identity") != old_identity:
            app.fail("Artifact changed between pages; reopen it to use a consistent file")
            return
        state.update(preview=value, preview_scroll=0, preview_pages=pages, preview_page=page_index)
        app.say(value["summary"])
    return _task(app, "Read artifact page " + str(page_index + 1),
                 lambda: artifact_pages.read_page(root, specification, files=hub.files, offset=offset,
                                                  row=row, columns=columns, sort=sort, collapsed=collapsed), complete, failed)


def _csv_column(preview, value):
    header = preview.get("header", [])
    if value.isascii() and value.isdigit() and 1 <= int(value) <= len(header):
        return int(value) - 1
    if value in header:
        return header.index(value)
    raise ValueError("Choose an existing CSV header or its 1-based column number")


def _artifact_control(app, rest):
    state = initialize(app)
    preview = state.get("preview") or {}
    if app.mode != "project_preview":
        raise ValueError("Open a declared output in :outputs before using artifact page controls")
    if state["busy"]:
        raise ValueError("An artifact page is already being read")
    if rest in (["next"], ["prev"]):
        _preview_page(app, 1 if rest == ["next"] else -1)
    elif len(rest) == 2 and rest[0] == "column" and preview.get("format") == "csv":
        state["preview_column"] = _csv_column(preview, rest[1])
    elif rest == ["refresh"]:
        _preview_page(app, reset=True)
    elif rest in (["text"], ["structured"]):
        state["preview_format"] = "text" if rest == ["text"] else None
        state["preview_columns"], state["preview_sort"] = None, None
        _preview_page(app, reset=True)
    elif rest and rest[0] == "columns" and len(rest) >= 2:
        if preview.get("format") != "csv":
            raise ValueError("Column selection requires a CSV artifact")
        columns = None if rest[1:] == ["all"] else list(dict.fromkeys(_csv_column(preview, item) for item in rest[1:]))
        state["preview_columns"] = columns
        _preview_page(app, reset=True)
    elif rest and rest[0] == "sort" and len(rest) == 3:
        if preview.get("format") != "csv" or rest[2] not in ("asc", "desc", "off"):
            raise ValueError("artifact sort COLUMN asc|desc|off (CSV global sorting)")
        column = _csv_column(preview, rest[1])
        if rest[2] != "off" and preview.get("size", 0) > artifact_pages.SORT_BYTES:
            raise ValueError("Global CSV sorting supports at most 8 MiB; no partial sort was applied")
        state["preview_sort"] = None if rest[2] == "off" else (column, rest[2])
        _preview_page(app, reset=True)
    elif rest and rest[0] == "json" and len(rest) == 2:
        if preview.get("format") != "json":
            raise ValueError("JSON node controls require a structured JSON artifact")
        path = "" if rest[1] == "/" else rest[1]
        if path in state["preview_collapsed"]:
            state["preview_collapsed"].remove(path)
        else:
            state["preview_collapsed"].append(path)
        nodes, more = json_page(preview["json_value"], state["preview_collapsed"])
        preview.update(nodes=nodes, lines=[item["text"] for item in nodes], row=0, next_row=len(nodes),
                       has_next=more, truncated=more,
                       summary=f"{preview.get('size', 0)} source bytes; JSON nodes 1-{len(nodes)}; expandable tree")
        state.update(preview_scroll=0, preview_pages=[(0, 0)], preview_page=0)
    else:
        raise ValueError("artifact next|prev|refresh|text|structured|columns COLUMN...|sort COLUMN asc|desc|off|json /POINTER")


def run_command(app, args):
    if not args or args[0] not in command_names():
        return False
    command, rest = args[0], list(args[1:])
    state = initialize(app)
    try:
        if command == "project":
            if len(rest) != 1:
                raise ValueError("project PATH")
            app.mode = "project_runs"
            _refresh_runs(app, rest[0])
        elif command == "runs":
            if rest or not state["root"]:
                raise ValueError("runs (choose :project PATH first)")
            app.mode = "project_runs"
            state["filter"], state["filtering"] = "", False
            _refresh_runs(app, state["root"])
        elif command == "run":
            if rest == ["clear"]:
                clear_binding(app, suppress_auto=True)
                app.mode = "main"
                app.say("Run binding cleared; original research and log paths restored")
            elif len(rest) == 2 and rest[0] == "select":
                _select(app, rest[1])
            elif len(rest) == 2 and rest[0] == "home":
                _select(app, rest[1], home=True)
            elif len(rest) == 2 and rest[0] == "passport":
                binding = selected_binding(app)
                if not binding:
                    raise ValueError("Select a run before choosing its passport")
                path = projects.relative_path(rest[1])
                hub = _hub(app)

                def complete(passport):
                    binding["passport"] = os.path.join(binding["run_root"], path)
                    binding["passport_explicit"] = True
                    hub.passport, hub.passport_diff = passport, None
                    hub.configure(passport=binding["passport"])
                    app.tab, app.research_view, app.mode = "research", "passport", "main"
                    app.say("Bound verified passport " + passport["id"])

                _task(app, "Verify run passport", lambda: projects.read_passport(binding["run_root"], path, job_id=binding["job_id"], files=hub.files), complete)
            else:
                raise ValueError("run select RUN_ID | run passport RELATIVE_PATH | run clear")
        elif command in ("outputs", "artifact"):
            if command == "artifact" and len(rest) == 2 and rest[0] == "open":
                if app.mode != "project_outputs":
                    raise ValueError("Open :outputs before selecting a declared artifact")
                node = next((node for node in _visible_outputs(state) if node["path"] == rest[1]), None)
                if node is None:
                    raise ValueError("Choose a currently visible declared artifact path")
                _preview(app, node)
                return True
            if command == "artifact" and rest not in ([], ["browse"]):
                _artifact_control(app, rest)
                return True
            if rest not in ([], ["browse"]):
                raise ValueError(command + " [browse]")
            _outputs(app)
    except (OSError, ValueError, TypeError) as exc:
        app.fail(command + ": " + clean(exc))
    return True


def handle_key(app, key):
    if app.mode not in MODES:
        return False
    state = initialize(app)
    if key == "h" and app.mode == "project_runs" and not state["filtering"] and not state["notices_open"]:
        items = _visible_runs(state)
        if items:
            _select(app, items[min(state["run_cursor"], len(items) - 1)]["run_id"], home=True)
        return True
    if key == "!" and app.mode == "project_runs":
        state["notices_open"] = not state["notices_open"]
        return True
    if state["notices_open"] and app.mode == "project_runs":
        if key == "esc":
            state["notices_open"] = False
        else:
            notices = state["warnings"] + state["run_warnings"]
            delta = {"up": -1, "down": 1, "pgup": -10, "pgdn": 10, "k": -1, "j": 1}.get(key, 0)
            state["notices_scroll"] = max(0, min(max(0, len(notices) - 1), state["notices_scroll"] + delta))
            if key in ("home", "end"):
                state["notices_scroll"] = 0 if key == "home" else max(0, len(notices) - 1)
        return True
    if state["filtering"] and app.mode != "project_preview":
        if key in ("enter", "esc"):
            state["filtering"] = False
            if key == "esc":
                state["filter"] = ""
        elif key in ("backspace", "\b", "\x7f"):
            state["filter"] = state["filter"][:-1]
        elif len(key) == 1 and key.isprintable():
            state["filter"] = (state["filter"] + key)[:128]
        state["run_cursor"], state["output_cursor"] = 0, 0
        return True
    if key == "esc":
        state["home_intent"] = None
        if app.mode == "project_preview":
            app.mode = "project_outputs"
        else:
            app.mode = "main"
        return True
    if key in ("/",) and app.mode != "project_preview":
        state["filtering"] = True
        return True
    if key in ("r",) and app.mode != "project_preview":
        try:
            _refresh_runs(app, state["root"]) if app.mode == "project_runs" else _outputs(app)
        except ValueError as exc:
            app.fail(clean(exc))
        return True
    page = max(1, getattr(app, "height", 24) - 12)
    action = getattr(app, "keymap", {}).get(key, key)
    if app.mode == "project_preview":
        preview = state.get("preview") or {}
        if key in ("[", "]"):
            try:
                _preview_page(app, -1 if key == "[" else 1)
            except (ValueError, OSError) as exc:
                app.fail(clean(exc))
            return True
        if preview.get("format") == "csv" and key in ("left", "right", "enter", "c"):
            header = preview.get("header", [])
            if not header:
                return True
            if key in ("left", "right"):
                state["preview_column"] = (state["preview_column"] + (-1 if key == "left" else 1)) % len(header)
            else:
                column = state["preview_column"]
                try:
                    if key == "enter":
                        sort = state["preview_sort"]
                        direction = "desc" if sort == (column, "asc") else "off" if sort == (column, "desc") else "asc"
                        _artifact_control(app, ["sort", str(column + 1), direction])
                    else:
                        indexes = list(state["preview_columns"] if state["preview_columns"] is not None else range(len(header)))
                        if column in indexes and len(indexes) > 1:
                            indexes.remove(column)
                        elif column not in indexes:
                            indexes.append(column)
                            indexes.sort()
                        state["preview_columns"] = indexes
                        _preview_page(app, reset=True)
                except (ValueError, OSError) as exc:
                    app.fail(clean(exc))
            return True
        if preview.get("format") == "json" and key in ("enter", "space"):
            nodes = preview.get("nodes", [])
            item = nodes[min(state["preview_scroll"], len(nodes) - 1)] if nodes else {}
            if item.get("expandable"):
                _artifact_control(app, ["json", item["node"] or "/"])
            return True
        count = len((state.get("preview") or {}).get("lines", []))
        if preview.get("format") == "csv":
            count = max(0, count - 1)
        cursor_key = "preview_scroll"
    else:
        items = _visible_runs(state) if app.mode == "project_runs" else _visible_outputs(state)
        count = len(items)
        cursor_key = "run_cursor" if app.mode == "project_runs" else "output_cursor"
    delta = {"up": -1, "down": 1, "page_up": -page, "page_down": page, "pgup": -page, "pgdn": page}.get(action)
    if delta is not None:
        state[cursor_key] = max(0, min(max(0, count - 1), state[cursor_key] + delta))
    elif action in ("home", "end"):
        state[cursor_key] = 0 if action == "home" else max(0, count - 1)
    elif key in ("enter", "right", "space") and app.mode != "project_preview" and count:
        item = items[min(state[cursor_key], count - 1)]
        try:
            if app.mode == "project_runs":
                _select(app, item["run_id"])
            else:
                _preview(app, item)
        except (OSError, ValueError) as exc:
            app.fail(clean(exc))
    elif key == "left" and app.mode == "project_preview":
        app.mode = "project_outputs"
    elif key == "left" and app.mode == "project_outputs" and count:
        item = items[min(state[cursor_key], count - 1)]
        parent = item["path"] if item["directory"] else item["path"].rpartition("/")[0]
        if parent and parent not in state["collapsed"]:
            state["collapsed"].append(parent)
            state["output_cursor"] = next((index for index, node in enumerate(_visible_outputs(state)) if node["path"] == parent), 0)
    return True


def _window(state, key, top_key, count, page):
    cursor = max(0, min(state[key], max(0, count - 1)))
    top = min(state[top_key], max(0, count - page))
    top = min(top, cursor)
    if cursor >= top + page:
        top = cursor - page + 1
    state[key], state[top_key] = cursor, top
    return cursor, top


def overlay(views, snap, app, width, height):
    if app.mode not in MODES:
        return None
    state = initialize(app)
    from . import modal_scrollbars as B
    state["control_hits"] = []
    button_hits = []
    g = views.g
    row = lambda text, style="": [(L.cut(clean(text, g.ascii), max(0, width - 8), g.ascii), style)]
    binding = selected_binding(app) or {}
    identity = f"{binding.get('run_id', 'none')} / attempt {binding.get('attempt', '?')} / job {binding.get('job_id') or 'not recorded'}"
    lines = [row(" Project " + (state["root"] or "not selected"), "cyan+bold"), row(" Selected " + identity, "dim")]
    if state["busy"]:
        lines.append(row(" Background read in progress...", "yellow"))
    if app.mode == "project_runs":
        if state["notices_open"]:
            lines.append(row(" All binding and discovery notices | arrows scroll | ! / Esc back", "dim"))
            notices = state["warnings"] + state["run_warnings"]
            start = min(state["notices_scroll"], max(0, len(notices) - 1))
            page = max(1, height - 9)
            context = ("project-notices", state["root"], binding.get("run_id"))
            logical, start = B.window(app, "modal:project-notices", start, len(notices), page, context=context)
            state["notices_scroll"] = logical
            for notice in notices[start:start + page]:
                lines.append(row(" " + notice, "yellow"))
            if not notices:
                lines.append(row(" No binding or discovery notices", "green"))
            rendered = L.box(g, lines, width, height, "project notices")
            return B.boxed(app, "modal:project-notices", rendered, start=3, count=len(notices), page=page,
                           target=logical, painted=start, setter=lambda value: state.update(notices_scroll=value), context=context)
        lines.append(row(" Enter binds | h run home | / filter | ! notices | r refresh | Esc back", "dim"))
        lines.append(row(" Filter: " + state["filter"] + ("_" if state["filtering"] else ""), "cyan"))
        items = _visible_runs(state)
        page = max(1, height - 12)
        cursor, top = _window(state, "run_cursor", "run_top", len(items), page)
        if items:
            label = " [Open selected run home]"
            button_hits.append((len(lines), "control", {"id": "project-run-home", "label": "Open selected run home",
                                "left": 0, "right": min(max(0, width - 8), L.vlen(label)),
                                "action": ("command", "run home " + shlex.quote(items[cursor]["run_id"])), "group": "project_runs"}))
            lines.append(row(label, "cyan+bold"))
        key = "modal:project-runs"
        context = ("project_runs", state["root"], state["filter"])
        logical, top = B.window(app, key, top, len(items), page, context=context, focus=cursor)
        state["run_top"] = logical
        data_start = len(lines)
        setter = lambda value: state.update(run_top=value)
        for index, run in enumerate(items[top:top + page], top):
            marker = (">" if g.ascii else "›") if index == cursor else " "
            shown_state = "ARRAY RECEIPT" if _array_receipt(run) else run["state"]
            text = f" {marker} {run['run_id']}  [{shown_state}]  attempt {run['attempt']}  job {run.get('job_id') or 'unrecorded'}"
            button_hits.append((len(lines), "control", {"id": "project-run:" + run["run_id"], "label": run["run_id"],
                                "left": 0, "right": min(max(0, width - 8), L.vlen(clean(text, g.ascii))),
                                "action": ("command", "run select " + shlex.quote(run["run_id"])), "group": "project_runs"}))
            lines.append(row(text, "rev+bold" if index == cursor else "green" if run["state"] == "COMPLETED" else "yellow" if run["state"] in ("RUNNING", "PENDING") else ""))
        if not items:
            lines.append(row(" " + state["summary"], "dim"))
        notices = state["warnings"] + state["run_warnings"]
        lines.append(row(f" {len(items)} matching runs; {len(notices)} notices" + ("; discovery limited" if state.get("limited") else ""), "yellow" if notices else "dim"))
        if notices:
            lines.append(row(" " + notices[0], "yellow"))
        title = "project / runs"
    elif app.mode == "project_outputs":
        tree = state.get("tree") or {}
        lines.append(row(" Enter expands or previews | Left collapses | / filter | r refresh | Esc back", "dim"))
        lines.append(row(" " + tree.get("summary", "Waiting for declared output inspection"), "yellow" if tree.get("status") != "valid" else "green"))
        lines.append(row(" Filter: " + state["filter"] + ("_" if state["filtering"] else ""), "cyan"))
        items = _visible_outputs(state)
        page = max(1, height - 12)
        cursor, top = _window(state, "output_cursor", "output_top", len(items), page)
        key = "modal:project-outputs"
        context = ("project_outputs", state["root"], binding.get("run_id"), binding.get("attempt"),
                   state["filter"], tuple(state["collapsed"]))
        logical, top = B.window(app, key, top, len(items), page, context=context, focus=cursor)
        state["output_top"] = logical
        data_start = len(lines)
        setter = lambda value: state.update(output_top=value)
        for index, node in enumerate(items[top:top + page], top):
            marker = (">" if g.ascii else "›") if index == cursor else " "
            branch = ("+" if node["path"] in state["collapsed"] else "-") if node["directory"] else ("o" if g.ascii else "◆")
            status = node["status"]
            size = "" if node.get("size") is None else f"  {node['size']} B"
            button_hits.append((len(lines), "control", {"id": "project-output:" + node["path"], "label": node["name"],
                                "left": 0, "right": max(0, width - 8),
                                "action": ("command", "artifact open " + shlex.quote(node["path"])), "group": "project_outputs"}))
            lines.append(row(f" {marker} {'  ' * min(8, node['depth'])}{branch} {node['name']}  {status}{size}",
                             "rev+bold" if index == cursor else "green" if status == "valid" else "red" if status in ("missing", "invalid", "error") else "dim"))
        if not items:
            lines.append(row(" No declared outputs match this filter", "dim"))
        lines.append(row(f" {len(items)} visible nodes / {tree.get('declared', 0)} declared files" + ("; tree display limit reached" if tree.get("limited") else ""), "dim"))
        title = "declared artifacts"
    else:
        preview = state.get("preview") or {}
        lines.append(row(" " + preview.get("path", "") + " | " + preview.get("format", "text"), "bold"))
        lines.append(row(" " + preview.get("summary", ""), "yellow" if preview.get("truncated") or preview.get("status") != "ready" else "dim"))
        lines.append(row(" Arrows / PgUp / PgDn scroll | [ previous page | ] next page | Esc outputs", "dim"))
        if preview.get("format") == "csv":
            header = preview.get("header", [])
            column = min(state["preview_column"], max(0, len(header) - 1))
            name = header[column] if header else "none"
            lines.append(row(f" Column {column + 1}: {name} | Left/Right choose | Enter sort asc/desc/off | c show/hide", "cyan"))
            shown = []
            for index in preview.get("columns", []):
                name = header[index]
                sorting = preview.get("sort")
                if sorting and sorting[0] == index:
                    name += " ^" if sorting[1] == "asc" else " v"
                shown.append((str(index), "[" + name + "]" if index == column else name,
                              ("command", "artifact column " + str(index + 1))))
            from .control_rows import buttons
            controls, hits = buttons(g, max(0, width - 8), shown, selected=str(column),
                                      group="artifact_columns", prefix="artifact-column:", gap="|")
            button_hits = [(y + len(lines), kind, value) for y, kind, value in hits]
            lines.extend(controls)
        elif preview.get("format") == "json":
            lines.append(row(" Enter expands/folds selected JSON node; :artifact json /POINTER", "cyan"))
        page = max(1, height - 11)
        display_lines = preview.get("lines", [])[1:] if preview.get("format") == "csv" else preview.get("lines", [])
        start = min(state["preview_scroll"], max(0, len(display_lines) - 1))
        page = max(1, height - len(lines) - 5)
        key = "modal:project-preview"
        context = ("project_preview", id(preview), width, state["preview_column"])
        logical, top = B.window(app, key, start, len(display_lines), page,
                                context=context, focus=state["preview_scroll"])
        data_start = len(lines)
        items = display_lines
        setter = lambda value: None
        for index, text in enumerate(display_lines[top:top + page], top):
            selected = index == state["preview_scroll"]
            marker = ">" if g.ascii else "›"
            number = preview.get("row", 0) + index + 1
            lines.append(row(f" {marker if selected else ' '} {number:>4}  {text}", "rev+bold" if selected else ""))
        title = "artifact preview"
    rendered = L.box(g, lines, width, height, title)
    from .control_rows import place_hits
    state["control_hits"] = place_hits(button_hits, rendered[1:-1])
    return B.boxed(app, key, rendered, start=data_start, count=len(items), page=page,
                   target=logical, painted=top, setter=setter, context=context)


def handle_mouse(app, y, x, button="left", shift=False):
    if getattr(app, "mode", "main") not in MODES or button != "left":
        return False
    for row, _, value in initialize(app).get("control_hits", []):
        if row == y and value["left"] <= x < value["right"]:
            app.run_command(value["action"][1])
            return True
    return False
