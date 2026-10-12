"""One cached, cancellable export workflow for every job/results view.

Input freezes exact job evidence; the shared ResearchHub lane resolves indexes,
reads bounded source metadata and writes bundles. Painting never reads files or
queries Slurm. An open panel never follows a changing job selection.
"""
from __future__ import annotations

import copy
from bisect import bisect_right
import getpass
import math
import os
from pathlib import Path
import shlex
import socket
import threading

from . import layout as L
from .control_rows import place_hits
from .log_text import display_text

FAMILIES = (("application", "Application metrics"), ("gpu", "GPU measurements"),
            ("cpu-memory", "CPU and memory"), ("summary", "Run summary"),
            ("research", "Research reports"), ("trace", "Trace evidence"),
            ("logs", "Logs"), ("artifacts", "Declared artifacts"))
DEFAULT_FAMILIES = tuple(key for key, _ in FAMILIES[:6])
MAX_ATTEMPTS = 128
MAX_CHOICES = 1024


def initialize(app):
    state = getattr(app, "data_export_state", None)
    if not isinstance(state, dict):
        state = dict(generation=0, revision=0, stage="", return_mode="main", cursor=0,
                     top=0, visible=1, follow=True, control_hits=[], painted=None,
                     pending=None, running=False, cancel=None, progress={},
                     frozen=(), attempts=(), discovery=None,
                     plan=None, receipt=None, error="", message="", editing=None,
                     include=list(DEFAULT_FAMILIES), metrics=[], devices=[], sources=[],
                     layout="category", range_policy="full", chart_range=None,
                     start="", end="", destination=dict(mode="project", path="", host=""),
                     destination_blocked=False, settings={}, presets={}, preset="", preset_name="Paper figures",
                     auto=False, future_auto=False, remote=False, cache={})
        app.data_export_state = state
    return state


def command_names():
    return ["exportdata", "launchsetup"]


def active(app):
    return getattr(app, "mode", "") == "data_export"


def _invalidate(state, *, content=True):
    state["revision"] += 1
    if content:
        state["content_revision"] = state.get("content_revision", 0) + 1
    state.update(control_hits=[], painted=None)


def context(app):
    state = initialize(app)
    return (state["generation"], state["revision"], state["stage"], state["top"], state["editing"])


def _frame(app):
    return (getattr(app, "mode", ""), getattr(app, "width", None), getattr(app, "height", None), context(app))


def _feedback(app, message, error=False):
    callback = getattr(app, "fail" if error else "say", None)
    if callable(callback):
        callback(str(message))


def _failure(app, error):
    state = initialize(app)
    state.update(error=str(error), stage=state.get("return_stage", "form"), message="")
    _invalidate(state)
    # App.fail feeds the retained notification center; no renderer truncation.
    _feedback(app, "Export data: " + str(error), True)


def _host(app):
    files = getattr(app, "files", None) or getattr(getattr(app, "research", None), "files", None)
    if getattr(files, "remote", False):
        backend = getattr(files, "b", None) or getattr(files, "backend", None)
        return str(getattr(backend, "host", None) or getattr(files, "host", None) or "remote project host")
    return socket.gethostname()


def _selected_jobs(app):
    """Expand only painted, collapsed groups' already admitted records."""
    from .job_selection import context as selection_context
    from .job_groups import metadata_for_record
    current = selection_context(app)
    chosen = tuple(getattr(app, "marks", ())) if current else ()
    scope = current.get("scope", "") if current else getattr(app, "tab", "")
    if current:
        admitted = set(current.get("ids", ()))
        if chosen and any(jid not in admitted for jid in chosen):
            raise ValueError("Some marked jobs are outside this published list. Clear the marks or select their list before exporting.")
        chosen = chosen or ((current["selected"],) if current.get("selected") else ())
    if not chosen:
        selected = getattr(app, "selected_id", None)
        if getattr(app, "tab", "") == "research":
            selected = getattr(app, "research_job_id", None) or selected
        elif getattr(app, "tab", "") == "analytics":
            selected = getattr(app, "analytics_job", None) or selected
        chosen = (selected,) if selected else ()
    result = []
    for jid in chosen:
        actual_scope = "recent" if scope == "jobs" and jid in getattr(app, "recent_ids", ()) else scope
        meta = metadata_for_record(app, actual_scope, jid)
        members = tuple(record.id for record in meta.records) if meta and meta.header and meta.collapsed else (jid,)
        result.extend(members)
        if len(result) > MAX_ATTEMPTS:
            raise ValueError(f"Select at most {MAX_ATTEMPTS} admitted attempts for one export.")
    return tuple(dict.fromkeys(result))


def capture_selection(app, job_ids=None):
    """Freeze cached bindings plus observed scheduler attempt disambiguators."""
    from . import projects
    project = getattr(app, "project_state", {})
    binding = project.get("binding") or {}
    if getattr(app, "mode", "") == "project_runs" and binding.get("run_id") and job_ids is None:
        return ({"binding": copy.deepcopy(binding), "host": _host(app)},)
    selected = _selected_jobs(app) if job_ids is None else tuple(job_ids)
    if not selected and binding.get("run_id"):
        return ({"binding": copy.deepcopy(binding), "host": _host(app)},)
    if not selected:
        raise ValueError("Select a job or a recorded run first. Use File > Project runs to choose an exact attempt.")
    result = []
    for jid in selected:
        job, details = app.store.record_context(jid)
        if job is None and binding.get("job_id") != jid:
            raise ValueError(f"Job {jid} is no longer published. Select its exact recorded attempt with :runs.")
        evidence = {"job_id": jid, "cluster": str(getattr(job, "cluster", "") or details.get("Cluster", "")),
                    "host": _host(app), "restart": details.get("Restarts", details.get("RestartCnt")),
                    "roots": tuple(projects.job_project_roots(details.get("WorkDir") or getattr(job, "workdir", ""),
                                  project.get("registered_root", ""))),
                    "start": str(getattr(job, "start", "") or details.get("StartTime", "")),
                    "submit": str(details.get("SubmitTime") or getattr(job, "submit", ""))}
        if binding.get("job_id") == jid and binding.get("run_id"):
            evidence["binding"] = copy.deepcopy(binding)
        result.append(evidence)
    return tuple(result)


def _resolve(frozen, cancel):
    from . import projects
    result = []
    for evidence in frozen:
        if cancel.is_set():
            raise InterruptedError("Export discovery cancelled")
        binding = evidence.get("binding")
        if binding:
            selected = projects.select_run(binding["project_root"], binding["run_id"])
            current = selected["binding"]
            if any(key in binding and binding[key] != current.get(key) for key in ("run_id", "job_id", "attempt", "project_id")):
                raise ValueError("The recorded attempt changed identity. Reopen Export data from the intended attempt.")
            observed_restart = evidence.get("restart")
            if str(observed_restart).isdigit():
                launch = selected["run"]["inventory"].get("metadata", {}).get("raven_launch", {})
                if launch.get("restart_count") != int(observed_restart):
                    raise ValueError("The selected job's restart differs from its cached run binding. Select the intended exact run with File > Project runs.")
            if evidence.get("submit"):
                from .model import stamp
                observed_submit = stamp(evidence["submit"])
                recorded_submit = selected["run"]["inventory"].get("submit")
                if (observed_submit is None or not isinstance(recorded_submit, (int, float))
                        or abs(observed_submit-recorded_submit) >= 1):
                    raise ValueError("The selected job's submission differs from its cached run binding. Select the intended exact run with File > Project runs.")
        else:
            candidates = []
            restart = evidence.get("restart")
            restart = int(restart) if str(restart).isascii() and str(restart).isdigit() else None
            for root in evidence["roots"]:
                try:
                    found = projects._indexed_project(root, evidence["job_id"], evidence["cluster"])
                except FileNotFoundError:
                    continue
                for run in (found or {}).get("runs", ()):
                    launch = run["inventory"].get("metadata", {}).get("raven_launch", {})
                    if restart is not None and launch.get("restart_count") != restart:
                        continue
                    from .model import stamp
                    observed_submit = stamp(evidence.get("submit", ""))
                    recorded_submit = run["inventory"].get("submit")
                    if (observed_submit is None or not isinstance(recorded_submit, (int, float))
                            or abs(observed_submit-recorded_submit) >= 1):
                        continue  # Recycled scheduler IDs never pick an unrelated retained run.
                    candidates.append((found["root"], run["run_id"]))
            candidates = list(dict.fromkeys(candidates))
            if len(candidates) != 1:
                raise ValueError(f"Job {evidence['job_id']} has {len(candidates)} matching indexed attempts. Select its exact run using File > Project runs; export never guesses among requeues or recycled IDs.")
            current = projects.select_run(*candidates[0])["binding"]
        item = {"project": current["project_root"], "run_id": current["run_id"], "host": evidence["host"]}
        if item not in result:
            result.append(item)
    return result


class _Cancellation:
    def __init__(self, event, hub):
        self.event, self.hub = event, hub

    def is_set(self):
        return self.event.is_set() or bool(getattr(self.hub, "closed", False))


def _queue(app, label, work, complete):
    state = initialize(app)
    if state["pending"] or state["running"]:
        return False
    state["return_stage"] = "setup" if state["stage"] == "setup" else "form"
    state.update(pending=(work, complete), cancel=threading.Event(), stage="busy", message=label, progress={})
    _invalidate(state)
    tick(app)
    return True


def tick(app):
    state = initialize(app)
    if state["running"]:
        if not active(app) or getattr(app, "research", None) is not state.get("worker_hub"):
            if state["cancel"] is not None:
                state["cancel"].set()
        return
    if not state["pending"]:
        from .auto_export_ui import tick as automatic_tick
        automatic_tick(app)
        return
    if not active(app):
        cancel(app, close=True)
        return
    hub = getattr(app, "research", None)
    if hub is None:
        state["pending"] = None
        _failure(app, "The shared background reader is unavailable. Start an interactive Raven session to export.")
        return
    token, event = state["generation"], state["cancel"]
    work, complete = state["pending"]

    def progress(value):
        if token == state["generation"] and isinstance(value, dict):
            # Publish a whole snapshot. Readers retain its reference; neither
            # thread mutates a published dictionary while the other reads it.
            state["progress"] = {key: value[key] for key in ("records", "bytes", "files") if key in value}

    def published(value):
        if token != state["generation"]:
            if isinstance(value, dict) and value.get("schema") == "raven.automation-settings/v1":
                _feedback(app, "Project preferences were saved before cancellation completed.")
            elif isinstance(value, dict) and (value.get("outputs") or value.get("automatic")):
                _feedback(app, "Export finished before cancellation: " + value.get("status", "saved") + "; " +
                          ", ".join(value.get("outputs", ())) +
                          (" Completion request was saved." if value.get("automatic") else ""),
                          value.get("status") in ("failed", "partial"))
            return
        state.update(running=False, pending=None, cancel=None)
        if getattr(app, "research", None) is not hub:
            if active(app):
                _failure(app, "The connection changed. Reopen Export data on the intended filesystem.")
            return
        if not active(app):
            return
        if isinstance(value, Exception):
            if event.is_set() and isinstance(value, InterruptedError):
                state.update(stage="form", message="Cancelled; selections retained.")
                _invalidate(state)
            else:
                _failure(app, value)
        else:
            complete(value)

    def worker():
        cancellation = _Cancellation(event, hub)
        if cancellation.is_set():
            raise InterruptedError("Export operation cancelled before it started")
        return work(cancellation, progress)

    if hub.start_task(worker, published):
        state.update(pending=None, running=True, worker_hub=hub)


def cancel(app, close=False):
    state = initialize(app)
    if state["cancel"] is not None:
        state["cancel"].set()
    if close:
        state["generation"] += 1
        state.update(pending=None, running=False, cancel=None)
        app.mode = state["return_mode"] if state["return_mode"] != "data_export" else "main"
    elif state["pending"]:
        state.update(pending=None, cancel=None, stage="form", message="Cancelled; selections retained.")
    elif state["running"]:
        state["message"] = "Cancelling; waiting for owned staging cleanup..."
    else:
        state.update(stage="form", receipt=None, message="")
    _invalidate(state)


def _options(state):
    if state.get("destination_blocked"):
        raise ValueError("The saved absolute destination belongs to another host or user. Select a destination on the displayed filesystem before exporting.")
    result = dict(include=list(state["include"]), metrics=list(state["metrics"]), devices=list(state["devices"]),
                  layout=state["layout"], destination=copy.deepcopy(state["destination"]))
    if state["range_policy"] == "chart" and not state["chart_range"]:
        raise ValueError("The saved chart range is unavailable. Choose Full recorded run or explicit bounds.")
    if state["sources"]:
        result["sources"] = list(state["sources"])
    if state["range_policy"] == "chart" and state["chart_range"]:
        result["start"], result["end"] = state["chart_range"]
    elif state["range_policy"] == "explicit":
        for name in ("start", "end"):
            if state[name]:
                result[name] = float(state[name])
    return result


def request(app):
    state = initialize(app)
    return dict(_options(state), attempts=copy.deepcopy(list(state["attempts"])))


def _scope_preferences(options):
    result = copy.deepcopy(options)
    result.pop("attempts", None)
    destination = result.get("destination", {})
    if destination.get("mode") == "absolute":
        destination.update(host=destination.get("host") or socket.gethostname(), user=getpass.getuser())
    else:
        destination["host"] = ""  # Relative destinations follow the job project filesystem.
    return result


def _apply_preferences(state, options):
    if not isinstance(options, dict):
        return
    for key in ("include", "metrics", "devices", "sources"):
        if isinstance(options.get(key), list):
            state[key] = list(options[key])[:MAX_CHOICES]
    if "all" in state["include"]:
        available = (state["discovery"] or {}).get("availability", {})
        state["include"] = list(dict.fromkeys([key for key in DEFAULT_FAMILIES if available.get(key, {}).get("available")] +
                                              [key for key in state["include"] if key != "all"]))
    if options.get("layout") in ("category", "metric"):
        state["layout"] = options["layout"]
    if options.get("range_policy") in ("full", "chart", "explicit"):
        state["range_policy"] = options["range_policy"]
    destination = copy.deepcopy(options.get("destination", {}))
    if destination.get("mode") in ("project", "relative", "absolute"):
        if destination["mode"] == "absolute" and (destination.get("host") != socket.gethostname() or destination.get("user") != getpass.getuser()):
            state["message"] = "Saved absolute destination belongs to another host or user. Choose an explicit destination here."
            state["destination_blocked"] = True
        else:
            state["destination_blocked"] = False
        destination.pop("user", None)
        if destination["mode"] != "absolute":
            destination["host"] = ""
        state["destination"] = destination
    for key in ("start", "end"):
        state[key] = str(options.get(key, ""))


def _chart_plot(app):
    chart = getattr(app, "chart_interaction_state", {})
    key = chart.get("hovered") or chart.get("last_key")
    plot = next((item for item in chart.get("plots", ()) if item.key == key), None)
    return plot


def _chart_range(app):
    plot = _chart_plot(app)
    if plot is None or not plot.key or plot.key[0] not in ("reported-metric", "resource-series", "resource-area"):
        return None
    # These producers explicitly use t / sample_times epoch axes. Scientific
    # scaling and comparison axes have other scopes and cannot become clocks.
    bounds = plot.x_bounds
    return tuple(bounds) if len(bounds) == 2 and all(type(value) in (int, float) and math.isfinite(value) for value in bounds) else None


def open_export(app, *, chart=False):
    if active(app):
        return True
    if getattr(app, "mode", "main") not in ("main", "setup", "analysis", "project_runs"):
        _feedback(app, "Close the current dialog before opening Export data.", True)
        return False
    toolbar = getattr(app, "toolbar_state", {})
    toolbar.update(menu=None, panel=None, focus="", dragging=False)
    try:
        plot = _chart_plot(app) if chart else None
        chart_key = plot.key if plot is not None else ()
        if len(chart_key) >= 7 and chart_key[5] and chart_key[6]:
            frozen = ({"binding": {"project_root": chart_key[5], "run_id": chart_key[6],
                                    "job_id": chart_key[1] or None, "attempt": chart_key[4]}, "host": _host(app)},)
        else:
            frozen = capture_selection(app, (chart_key[1],) if len(chart_key) >= 2 and chart_key[1] else None)
    except (ValueError, TypeError) as exc:
        _feedback(app, exc, True)
        return False
    state = initialize(app)
    state.update(generation=state["generation"]+1, return_mode=getattr(app, "mode", "main"), frozen=frozen,
                 attempts=(), discovery=None, plan=None, receipt=None, error="", message="", cursor=0, top=0,
                 editing=None, pending=None, running=False, include=list(DEFAULT_FAMILIES), metrics=[], devices=[], sources=[],
                 destination=dict(mode="project", path="", host=""), layout="category", range_policy="full",
                 chart_range=_chart_range(app), start="", end="", auto=False, future_auto=False, destination_blocked=False)
    files = getattr(app, "files", None) or getattr(getattr(app, "research", None), "files", None)
    state["remote"] = bool(getattr(files, "remote", False))
    app.mode = "data_export"
    from .scrollbars import cancel as cancel_scroll
    cancel_scroll(app)
    if state["remote"]:
        _failure(app, "This project lives on " + _host(app) + ". Remote source export is not supported by this connection. Run raven export-data --project PROJECT --run RUN_ID on the project host; no local fallback was used.")
        return True

    def work(cancelled, progress):
        from . import data_export, launch_settings
        attempts = _resolve(frozen, cancelled)
        discovery = data_export.discover({"attempts": attempts, "include": [key for key, _ in FAMILIES]})
        settings = launch_settings.load_settings(attempts[0]["project"]) if len({a["project"] for a in attempts}) == 1 else {}
        from .auto_export import read_status
        statuses = [item for attempt in attempts for item in read_status(Path(attempt["project"]) / "runs" / attempt["run_id"])]
        return attempts, discovery, settings, statuses

    def completed(value):
        attempts, discovery, settings, statuses = value
        state.update(attempts=tuple(attempts), discovery=discovery, settings=settings,
                     presets=settings.get("export_presets", {}), stage="form", auto_status=statuses,
                     future_auto=bool(settings.get("launch", {}).get("auto_export")))
        state["include"] = [key for key in DEFAULT_FAMILIES if discovery.get("availability", {}).get(key, {}).get("available")]
        _apply_preferences(state, settings.get("export_preferences", {}))
        if chart and len(chart_key) >= 4:
            metric, source = str(chart_key[2]), str(chart_key[3])
            if metric.startswith("gpu-trace:"):
                state["include"] = ["gpu"]
                state["devices"] = [metric[len("gpu-trace:"):].rsplit(":", 1)[0]]
                state["message"] = "GPU exports retain original utilization observations, including when this chart shows a derived mean."
            elif metric in ("retained-cpu-seconds", "retained-rss"):
                state["include"] = ["cpu-memory"]
            elif chart_key[0] == "reported-metric" and source != "Raven session resource samples":
                state.update(include=["application"], metrics=[metric])
        if chart and state["chart_range"]:
            state["range_policy"] = "chart"
        elif chart:
            state["message"] = "This chart has no recorded wall-clock range. Full recorded run is selected."
        _invalidate(state)

    _queue(app, "Discovering retained sources and exact attempts", work, completed)
    return True


def _plan(app):
    state = initialize(app)
    try:
        captured = request(app)
        if not captured["include"]:
            raise ValueError("Select at least one output family before reviewing export.")
        available = (state["discovery"] or {}).get("availability", {})
        if not any(available.get(family, {}).get("available") for family in captured["include"]):
            raise ValueError("None of the selected outputs is available. Select available sources or record the missing evidence first.")
    except (ValueError, TypeError) as exc:
        _failure(app, exc)
        return

    def work(cancelled, progress):
        from .data_export import plan_export
        return plan_export(captured)

    def completed(value):
        state.update(plan=value, stage="review", error="", cursor=0, top=0, message="")
        _invalidate(state)

    _queue(app, "Preparing destination and source cutoffs", work, completed)


def _execute(app, *, schedule_only=False):
    state = initialize(app)
    plan = copy.deepcopy(state["plan"])
    if not plan or not plan.get("valid"):
        _failure(app, "The export plan is invalid. Edit the selection or destination and review again.")
        return
    automatic = state["auto"] or schedule_only
    attempts = copy.deepcopy(state["attempts"])
    captured = copy.deepcopy(plan["request"])

    def work(cancelled, progress):
        from .data_export import export_data
        from .auto_export import capture
        if automatic:
            statuses = []
            for attempt in attempts:
                if cancelled.is_set():
                    if statuses:
                        return dict(status="cancelled", outputs=[], files=0, attempts=len(statuses), automatic=statuses,
                                    issues=[dict(message="Scheduling stopped after the listed completion requests were saved.")])
                    raise InterruptedError("Completion export scheduling cancelled")
                options = dict(captured, attempts=[{"project": attempt["project"], "run_id": attempt["run_id"]}])
                statuses.append(capture(Path(attempt["project"]) / "runs" / attempt["run_id"], options))
            if schedule_only:
                return dict(status="pending", outputs=[], files=0, attempts=len(attempts), issues=[], automatic=statuses)
        receipt = export_data(plan, cancel=cancelled, progress=progress)
        if automatic:
            receipt["automatic"] = statuses
        return receipt

    def completed(value):
        state.update(receipt=value, stage="receipt", error="", cursor=0, top=0, message="")
        _invalidate(state)
        if value.get("status") in ("partial", "failed"):
            _feedback(app, "Export data " + value["status"] + ": " + "; ".join(_issue(item) for item in value.get("issues", ())), True)
        else:
            _feedback(app, "Export data: " + value.get("status", "finished") + "; " + ", ".join(value.get("outputs", ())))

    _queue(app, "Exporting retained records", work, completed)


def _save(app, preset=False):
    state = initialize(app)
    projects = {item["project"] for item in state["attempts"]}
    if len(projects) != 1:
        _failure(app, "Save project preferences from a selection in one project.")
        return
    try:
        if state["future_auto"] and state["range_policy"] != "full":
            raise ValueError("Future-launch completion exports require Full recorded run; chart or explicit timestamps belong to this attempt. Save a manual preset or select the full range.")
        options = _scope_preferences(_options(state))
    except ValueError as exc:
        _failure(app, exc)
        return
    options["range_policy"] = state["range_policy"]
    name = state["preset_name"].strip()
    if preset and (not name or len(name) > 128):
        _failure(app, "Enter a preset name between 1 and 128 characters.")
        return
    project = next(iter(projects))
    future = state["future_auto"]

    def work(cancelled, progress):
        from .launch_settings import update_settings
        if cancelled.is_set():
            raise InterruptedError("Preference save cancelled")
        changes = {"export_preferences": options}
        if preset:
            changes.update(export_presets={name: options}, default_export_preset=name)
        future_options = copy.deepcopy(options)
        future_options.get("destination", {}).pop("user", None)
        scope = {"host": options["destination"]["host"], "user": options["destination"]["user"]} if future and options["destination"].get("mode") == "absolute" else {}
        changes["launch"] = {"auto_export": future_options if future else False, "auto_export_scope": scope}
        return update_settings(project, changes, replace_sections=("export_preferences",))

    def completed(value):
        state.update(settings=value, presets=value.get("export_presets", {}), stage="form", error="",
                     message="Saved project export preferences" + (" and preset " + name if preset else "") + ". No transient job IDs were saved.")
        _invalidate(state)

    _queue(app, "Saving project export preferences", work, completed)


def _issue(value):
    return str(value.get("message", value.get("reason", value))) if isinstance(value, dict) else str(value)


def _document(app):
    state = initialize(app)
    progress = tuple(state["progress"].items()) if state["stage"] == "busy" else ()
    key = (state.get("content_revision", 0), state["stage"], progress)
    if state.get("document_key") != key:
        state.update(document_key=key, document=_build_document(app))
    return state["document"]


def _build_document(app):
    state = initialize(app)
    if state["stage"] == "setup":
        from .launch_setup_ui import document
        return document(app)
    items = []
    def action(label, key):
        items.append((label, key))
    def text(value):
        items.append((str(value), None))
    action("Close Export data", "close")
    if state["stage"] == "busy":
        action("Cancel background operation", "cancel")
        text(state["message"])
        text(" | ".join(f"{key}: {value:,}" if isinstance(value, int) else f"{key}: {value}" for key, value in state["progress"].items()))
        return items
    if state["stage"] == "receipt":
        receipt = state["receipt"] or {}
        text("Export " + receipt.get("status", "unknown") + f"; {receipt.get('files', 0)} files; {receipt.get('attempts', 0)} attempts")
        if receipt.get("outputs"):
            action("Copy result paths", "copy")
        if receipt.get("automatic"):
            text("Completion requests saved: " + str(len(receipt["automatic"])))
        action("Edit selections / retry", "edit")
        for path in receipt.get("outputs", ()):
            text(path)
        for issue in receipt.get("issues", ()):
            text(_issue(issue))
        return items
    if state["stage"] == "review":
        plan = state["plan"] or {}
        if plan.get("valid"):
            action("Export now (accept available subset if partial)", "export")
            if state["auto"]:
                action("Save completion request only", "schedule")
        action("Back to selection", "edit")
        selected = plan.get("request", {})
        text("Selected families: " + ", ".join(selected.get("include", ())))
        text("Application metrics: " + (", ".join(selected.get("metrics", ())) or "all retained metrics"))
        text("GPU devices: " + (", ".join(selected.get("devices", ())) or "all retained devices"))
        text("UTC epoch bounds: " + str(selected.get("start")) + " to " + str(selected.get("end")) + " (None means unbounded)")
        text("Layout: " + str(selected.get("layout", "category")))
        text("Manifest and README are always included. Running sources are captured at per-file cutoffs; missing measurements remain missing.")
        for destination in plan.get("destinations", ()):
            text("Filesystem: " + str(destination.get("host", "")))
            text("Destination: " + str(destination.get("path", "")))
        for attempt in plan.get("attempts", ()):
            text(f"Run {attempt.get('run_id')} | job {attempt.get('job_id') or 'local'} | attempt {attempt.get('attempt', '?')} | {attempt.get('state', 'unknown')}")
            for source in attempt.get("sources", ()):
                if source.get("available"):
                    text(f"  {source.get('family')}: {source.get('path')} ({source.get('size', '?')} source bytes)")
        for issue in plan.get("issues", ()):
            text(_issue(issue))
        return items
    action("Review export destination", "review")
    if state["error"]:
        text("Issue: " + state["error"])
    if state["message"]:
        text(state["message"])
    if not state["attempts"]:
        action("Retry retained-source discovery", "rediscover")
        return items
    for attempt in state["attempts"]:
        text("Exact run: " + attempt["run_id"] + " | Job's project folder: " + attempt["project"])
    availability = (state["discovery"] or {}).get("availability", {})
    action("Select all available families (logs/artifacts stay explicit)", "all")
    action("Clear family selection", "none")
    for key, label in FAMILIES:
        info = availability.get(key, {})
        marker = "[x]" if key in state["include"] else "[ ]"
        description = "available" if info.get("available") else "unavailable: " + "; ".join(map(str, info.get("reasons", ())))
        action(marker + " " + label + " - " + description, "family:" + key)
        choices = info.get("metrics", ()) if key == "application" else info.get("devices", ()) if key == "gpu" else ()
        selected = state["metrics"] if key == "application" else state["devices"]
        kind = "metric" if key == "application" else "device"
        if choices and key in state["include"]:
            action("  All " + kind + "s" + (" [selected]" if not selected else ""), kind + ":*")
            for value in choices[:MAX_CHOICES]:
                name = str(value.get("id", value.get("name", ""))) if isinstance(value, dict) else str(value)
                action("  " + ("[x]" if not selected or name in selected else "[ ]") + " " + name, kind + ":" + name)
    if "application" in state["include"]:
        action("Choose application metric by exact name", "field:metric_name")
        text("Metric-name hints are bounded, not an exhaustive stream scan. Empty individual selection means all metrics.")
        for name in state["metrics"]:
            action("Remove selected metric: " + name, "metric:" + name)
    for name in state["devices"]:
        action("Remove selected device: " + name, "device:" + name)
    sources = {(item["family"], item["id"]): item for attempt in (state["discovery"] or {}).get("attempts", ())
               for item in attempt.get("sources", ()) if item.get("family") in ("research", "trace", "logs", "artifacts")}
    for (family, identifier), source in list(sources.items())[:MAX_CHOICES]:
        if family in state["include"]:
            action("  " + ("[x] " if not state["sources"] or family + ":" + identifier in state["sources"] else "[ ] ") + family + ": " + identifier,
                   "source:" + family + ":" + identifier)
    if state["sources"]:
        action("All declared reports / traces / logs / artifacts", "source:*")
    action("Time range: " + {"full": "Full recorded run", "chart": "Current chart range", "explicit": "Explicit UTC epoch bounds"}[state["range_policy"]], "range")
    if state["range_policy"] == "chart":
        text("Chart: " + str(state["chart_range"]) if state["chart_range"] else "Chart range unavailable. Choose Full recorded run or explicit bounds.")
    if state["range_policy"] == "explicit":
        action("Start epoch seconds: " + (state["start"] or "unbounded"), "field:start")
        action("End epoch seconds: " + (state["end"] or "unbounded"), "field:end")
    action("Layout: " + ("One file per category" if state["layout"] == "category" else "Separate file for each application metric"), "layout")
    destination = state["destination"]
    labels = {"project": "Job's project folder / exports", "relative": "Custom: relative to job's project folder", "absolute": "Custom: absolute path on this computer"}
    action("Save to: " + labels[destination["mode"]], "destination")
    if destination["mode"] == "absolute" and len({item["project"] for item in state["attempts"]}) > 1:
        action(("[x] " if destination.get("combined") else "[ ] ") + "Combine projects in one bundle at this absolute destination", "combined")
    if destination["mode"] != "project":
        action("Path: " + (destination.get("path") or "[enter destination parent]"), "field:path")
    text("Filesystem: " + (destination.get("host") or _host(app)))
    for attempt in state["attempts"]:
        root = attempt["project"]
        parent = os.path.join(root, "exports") if destination["mode"] == "project" else os.path.join(root, destination.get("path", "")) if destination["mode"] == "relative" else destination.get("path", "")
        text("Destination preview: " + parent + "/<job-and-attempt>/<unique-export-id>/ (final path on review)")
    action(("[x]" if state["auto"] else "[ ]") + " Export when this attempt finishes", "auto")
    action(("[x]" if state["future_auto"] else "[ ]") + " Project default: export future launches on completion", "future-auto")
    text("Completion exports include failed/cancelled runs and partial evidence; killed jobs await a later reconciliation worker.")
    for status in state.get("auto_status", ()):
        text("Automatic export: " + str(status.get("status", "unknown")) + " " + str(status.get("output", status.get("outputs", ""))))
    action("Save project preferences", "save")
    action("Preset name: " + state["preset_name"], "field:preset_name")
    action("Save named preset", "save-preset")
    for name in sorted(state["presets"]):
        action("Load preset: " + name, "preset:" + name)
    return items


def _toggle(values, value):
    return [item for item in values if item != value] if value in values else values + [value]


def _activate(app, action):
    state = initialize(app)
    if action == "close":
        cancel(app, close=True)
        return
    if action == "cancel":
        cancel(app)
        return
    if state["stage"] == "busy":
        return
    if action.startswith("setup:"):
        from .launch_setup_ui import activate
        activate(app, action)
    elif action == "review":
        _plan(app)
    elif action in ("export", "schedule"):
        _execute(app, schedule_only=action == "schedule")
    elif action == "edit":
        state.update(stage="form", top=0, cursor=0, follow=True)
    elif action == "rediscover":
        previous = state["frozen"]
        cancel(app, close=True)
        # Retry the frozen evidence, never the current moving job cursor.
        _reopen_frozen(app, previous)
    elif action in ("all", "none"):
        availability = (state["discovery"] or {}).get("availability", {})
        state["include"] = [key for key in DEFAULT_FAMILIES if availability.get(key, {}).get("available")] if action == "all" else []
    elif action.startswith("family:"):
        state["include"] = _toggle(state["include"], action[7:])
    elif action.startswith(("metric:", "device:")):
        kind, value = action.split(":", 1)
        field = "metrics" if kind == "metric" else "devices"
        if value == "*":
            state[field] = []
        else:
            # From All, choosing one means only that measurement.
            state[field] = _toggle(state[field], value) if state[field] else [value]
    elif action.startswith("source:"):
        value = action[7:]
        state["sources"] = [] if value == "*" else _toggle(state["sources"], value)
    elif action == "range":
        modes = ["full", "chart", "explicit"] if state["chart_range"] else ["full", "explicit"]
        state["range_policy"] = modes[(modes.index(state["range_policy"]) + 1) % len(modes)] if state["range_policy"] in modes else "full"
    elif action == "layout":
        state["layout"] = "metric" if state["layout"] == "category" else "category"
    elif action == "combined":
        if state["destination"]["mode"] == "absolute":
            state["destination"]["combined"] = not state["destination"].get("combined", False)
    elif action == "destination":
        modes = ["project", "relative", "absolute"]
        destination = state["destination"]
        destination["mode"] = modes[(modes.index(destination["mode"]) + 1) % 3]
        destination["host"] = socket.gethostname() if destination["mode"] == "absolute" else ""
        destination["path"] = ""
        state["destination_blocked"] = False
        destination["combined"] = False
    elif action.startswith("field:"):
        key = action[6:]
        state["editing"] = (key, state["destination"].get("path", "") if key == "path" else
                            state.get("setup_fields", {}).get(key[6:], "") if key.startswith("setup:") else state.get(key, ""))
    elif action == "auto":
        state["auto"] = not state["auto"]
    elif action == "future-auto":
        state["future_auto"] = not state["future_auto"]
    elif action in ("save", "save-preset"):
        _save(app, action == "save-preset")
    elif action.startswith("preset:"):
        name = action[7:]
        _apply_preferences(state, state["presets"].get(name))
        state["preset"] = name
        if not state.get("destination_blocked"):
            state["message"] = "Preset loaded. Missing selections stay visible in review; explicitly accept the available subset before writing."
    elif action == "copy":
        paths = "\n".join((state["receipt"] or {}).get("outputs", ()))
        from .clipboard import copy as copy_text
        settings = getattr(app, "cfg", {}).get("clipboard", {})
        previous_receipt = state["receipt"]
        def completed(value):
            state.update(stage="receipt", receipt=previous_receipt)
            _feedback(app, value)
            _invalidate(state)
        _queue(app, "Copying result paths", lambda event, progress: copy_text(paths, getattr(app, "state_dir", None),
               use_osc52=settings.get("osc52", True), use_tools=settings.get("tools", True)), completed)
    _invalidate(state)


def _reopen_frozen(app, frozen):
    """Retry discovery without consulting mutable table selection."""
    state = initialize(app)
    state.update(generation=state["generation"]+1, frozen=frozen, error="")
    app.mode = "data_export"
    if state["remote"]:
        _failure(app, "Remote source export remains unsupported. Run export-data on the project host.")
        return
    def work(event, progress):
        from .data_export import discover
        attempts = _resolve(frozen, event)
        return attempts, discover({"attempts": attempts, "include": [key for key, _ in FAMILIES]})
    def completed(value):
        state.update(attempts=tuple(value[0]), discovery=value[1], stage="form")
        _invalidate(state)
    _queue(app, "Retrying retained-source discovery", work, completed)


def run_command(app, args):
    if args and args[0] == "launchsetup":
        from .launch_setup_ui import open_setup
        open_setup(app)
        return True
    if not args or args[0] != "exportdata":
        return False
    if args[1:] in ([], ["show"], ["chart"]):
        open_export(app, chart=args[1:] == ["chart"])
    elif len(args) == 3 and args[1] == "action" and active(app):
        _activate(app, args[2])
    elif args[1:] == ["close"] and active(app):
        cancel(app, close=True)
    else:
        _feedback(app, "Use exportdata [chart|close].", True)
    return True


def paste(app, text):
    state = initialize(app)
    if not active(app) or state["editing"] is None:
        return False
    key, value = state["editing"]
    if len(value) + len(text) > 4096 or not all(char.isprintable() for char in text):
        _feedback(app, "Enter at most 4096 printable characters; paths are never shell-expanded.", True)
        return True
    state["editing"] = (key, value + text)
    _invalidate(state)
    return True


def handle_key(app, key):
    if not active(app):
        return False
    state = initialize(app)
    if key == "f8" or getattr(app, "interaction_state", {}).get("active") and key not in ("esc", "ctrl-c"):
        from .interaction import handle_key as focus_key
        if focus_key(app, key):
            return True
    if state["editing"] is not None:
        field, value = state["editing"]
        if key == "esc":
            state["editing"] = None
        elif key == "enter":
            if field == "path":
                state["destination"]["path"] = value
            elif field.startswith("setup:"):
                from .launch_setup_ui import apply_field
                try:
                    apply_field(app, field[6:], value)
                except ValueError as exc:
                    state["error"] = str(exc)
                    _feedback(app, exc, True)
            elif field == "metric_name":
                if value and value not in state["metrics"]:
                    state["metrics"].append(value)
            else:
                state[field] = value
            state["editing"] = None
        elif key in ("backspace", "delete"):
            state["editing"] = (field, value[:-1])
        elif key in ("ctrl-u", "ctrl-a"):
            state["editing"] = (field, "")
        elif (key == "space" or len(key) == 1 and key.isprintable()) and len(value) < 4096:
            state["editing"] = (field, value + (" " if key == "space" else key))
        _invalidate(state)
        return True
    choices = [action for _, action in _document(app) if action]
    state["cursor"] = max(0, min(state["cursor"], len(choices)-1))
    if key in ("esc", "q", "ctrl-c"):
        cancel(app, close=True)
    elif key in ("tab", "btab", "up", "down", "left", "right"):
        state["cursor"] = (state["cursor"] + (-1 if key in ("btab", "up", "left") else 1)) % max(1, len(choices))
        state["follow"] = True
        _invalidate(state, content=False)
    elif key in ("enter", "space") and choices:
        _activate(app, choices[state["cursor"]])
    elif key in ("pgup", "pgdn", "home", "end"):
        state["top"] = max(0, {"pgup": state["top"]-state["visible"], "pgdn": state["top"]+state["visible"], "home": 0, "end": 10**9}[key])
        state["follow"] = False
        _invalidate(state, content=False)
    return True


def handle_mouse(app, y, x, button="left", shift=False):
    if not active(app):
        return False
    state = initialize(app)
    if button in ("wheel-up", "wheel-down"):
        state["top"] = max(0, state["top"] + (-3 if button == "wheel-up" else 3))
        state["follow"] = False
        _invalidate(state, content=False)
    elif button in ("left", "press") and state["painted"] == _frame(app) and state["editing"] is None:
        for row, _, item in state["control_hits"]:
            if row == y and item["left"] <= x < item["right"]:
                _activate(app, shlex.split(item["action"][1])[2])
                break
    return True


def overlay(views, snap, app, width, height):
    if not active(app):
        return None
    state = initialize(app)
    from .notification_ui import _wrap
    inner = max(1, width-6)
    choices = _document(app)
    cache_key = (state["document_key"], inner, bool(views.g.ascii), state["editing"])
    cache = state["cache"]
    if cache.get("key") != cache_key:
        # Keep one immutable paragraph plus compact offsets, never millions of
        # copied offscreen strings for long paths/errors in a narrow terminal.
        document, starts, choice_rows, choice, total = [], [], [], 0, 0
        for value, action in choices:
            text = display_text(value)
            if state["editing"] and action == "field:" + state["editing"][0]:
                text += " -> " + state["editing"][1] + "_"
            wrapped = _wrap(text, max(1, inner-2) if action else inner, views.g.ascii)
            starts.append(total)
            document.append((wrapped, action, choice if action else None))
            if action:
                choice_rows.append(total)
                choice += 1
            total += len(wrapped)
        cache.clear()
        cache.update(key=cache_key, document=document, starts=starts, total=total, choice_rows=choice_rows)
    document, starts, total = cache["document"], cache["starts"], cache["total"]
    choice_rows = cache["choice_rows"]
    state["cursor"] = max(0, min(state["cursor"], len(choice_rows)-1))
    selected_row = choice_rows[state["cursor"]] if choice_rows else 0
    capacity = max(1, height-5)
    state["visible"] = capacity
    if state["follow"]:
        if selected_row < state["top"]:
            state["top"] = selected_row
        elif selected_row >= state["top"]+capacity:
            state["top"] = max(0, selected_row-capacity+1)
        state["follow"] = False
    state["top"] = max(0, min(state["top"], max(0, total-capacity)))
    rows, hits = [], []
    position = max(0, bisect_right(starts, state["top"])-1)
    for index in range(state["top"], min(total, state["top"]+capacity)):
        while position+1 < len(starts) and starts[position+1] <= index:
            position += 1
        wrapped, action, choice = document[position]
        text = wrapped.line(index-starts[position])
        style = "text"
        if action:
            selected = choice == state["cursor"]
            text = L.cut(("> " if selected and index == selected_row else "  ") + text, inner, views.g.ascii)
            style = "sel+bold" if selected else "cyan"
            hits.append((index, "control", {"id": f"exportdata:{choice}", "label": wrapped.text, "left": 0,
                         "right": L.vlen(text), "action": ("command", "exportdata action " + shlex.quote(action)), "group": "data-export"}))
        rows.append([(text, style)])
    if height >= 5:
        rows.append([(L.cut("Tab/Arrows: choose | Enter/Space: apply | PgUp/PgDn: scroll | Esc: close", inner, views.g.ascii), "muted")])
    rendered = L.box(views.g, rows, width, height, "Project launch setup" if state["stage"] == "setup" else "Export data", min_width=10)
    state["control_hits"] = place_hits(hits, rendered[1:-2] if height >= 5 else rendered[1:-1], offset=state["top"])
    state["painted"] = _frame(app)
    return rendered
