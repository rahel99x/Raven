"""Completion recovery scheduled on Raven's existing bounded research worker."""
from __future__ import annotations

from datetime import datetime
import threading
import time

from . import auto_export, projects

RECOVERY_INTERVAL = 30.0


def initialize(app):
    state = getattr(app, "auto_export_state", None)
    if not isinstance(state, dict):
        state = {"last": 0., "pending": False, "statuses": [], "error": "",
                 "scope": None, "cancel": None, "generation": 0, "notices": {}, "launches": []}
        app.auto_export_state = state
    return state


def _epoch(value):
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        # Only local filesystem/connection recovery uses this adapter. Slurm's
        # native naive timestamps have the submission host's local timezone.
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _evidence(store, statuses):
    evidence = {}
    for item in statuses:
        identity = item.get("identity")
        if not isinstance(identity, dict) or not identity.get("job_id"):
            continue
        job, details = store.record_context(identity["job_id"])
        if job is None:
            continue
        state = str(getattr(job, "state", "")).split("+", 1)[0].split(" ", 1)[0]
        if state not in auto_export._FINAL:
            continue
        restart = str(details.get("Restarts", details.get("RestartCnt", "")))
        if not restart.isascii() or not restart.isdigit() or len(restart) > 9:
            continue  # No guesses about whether a recycled/requeued ID is ours.
        submit = _epoch(details.get("SubmitTime") or getattr(job, "submit", ""))
        if submit is None:
            continue
        evidence[identity["run_id"]] = {
            "state": state, "job_id": job.id, "cluster": getattr(job, "cluster", "") or details.get("Cluster", ""),
            "attempt": int(restart) + 1, "submit_time": submit, "observed_at": time.time(),
            "source": "retained local scheduler observation"}
    return evidence


def tick(app):
    """Use cached scope in the UI; all filesystem work stays in one worker."""
    state = initialize(app)
    hub = getattr(app, "research", None)
    project_state = getattr(app, "project_state", {}) or {}
    binding = project_state.get("binding") or {}
    root = binding.get("project_root") or project_state.get("root")
    if (not getattr(app, "interactive", False) or not hub or not root
            or not projects.local_supported(getattr(hub, "files", None))):
        if state.get("cancel"):
            state["cancel"].set()
        if state["scope"] is not None or state["pending"]:
            state.update(scope=None, pending=False, last=0., statuses=[], launches=[], error="",
                         generation=state["generation"] + 1, notices={})
        return False
    files = hub.files
    scope = (id(hub), id(files), root)
    if state["scope"] != scope:
        if state.get("cancel"):
            state["cancel"].set()
        # Let explicit project discovery/selection finish before entering the
        # shared worker lane. Opening a project is not an immediate export.
        state.update(scope=scope, last=time.monotonic(), pending=False, statuses=[], launches=[], error="",
                     generation=state["generation"] + 1, notices={})
        return False
    if getattr(app, "mode", "main") != "main" or project_state.get("busy"):
        return False
    if (state["pending"] or getattr(hub, "pending", None) or
            time.monotonic() - state["last"] < RECOVERY_INTERVAL):
        return False
    future = getattr(hub, "future", None)
    if future is not None and not future.done():
        return False
    generation = state["generation"]
    cancel = threading.Event()
    store = getattr(app, "store", None)

    def worker():
        statuses = auto_export.pending_status(root)
        launches = auto_export.launch_candidates(root)
        evidence = _evidence(store, statuses + launches) if store and hasattr(store, "record_context") else {}
        results = auto_export.process_pending(root, deadline_seconds=5., limit=2,
                                             cancel=cancel, terminal_evidence=evidence)
        return {"pending": auto_export.pending_status(root), "launches": auto_export.launch_candidates(root), "results": results}

    def complete(value):
        if generation != state["generation"] or state["scope"] != scope:
            return
        state.update(pending=False, last=time.monotonic())
        if getattr(app, "research", None) is not hub or hub.files is not files:
            return
        if isinstance(value, Exception):
            message = "Automatic export recovery: " + str(value)
            if state.get("error") != message:
                app.say(message, level="warning")
            state["error"] = message
            return
        state.update(statuses=value["pending"], launches=value["launches"], error="")
        for result in value["results"]:
            signature = (result["status"], result.get("reason"), tuple(result.get("outputs", [])))
            if state["notices"].get(result["key"]) == signature:
                continue
            state["notices"][result["key"]] = signature
            while len(state["notices"]) > 128:
                del state["notices"][next(iter(state["notices"]))]
            message = ("Automatic export " + result["status"] + " for " + result["identity"]["run_id"]
                       + ": " + result.get("reason", ""))
            if result.get("outputs"):
                message += "\n" + "\n".join(result["outputs"])
            app.say(message, level="warning" if result["status"] in {"failed", "partial"} else "info")

    if not hub.start_task(worker, complete):
        return False
    state.update(pending=True, last=time.monotonic(), cancel=cancel)
    return True
