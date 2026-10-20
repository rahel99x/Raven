"""Published log-source declarations without presentation or file discovery."""

from ..job_logs.paths import stdout_path
from ..local_files import LocalFiles


def entries(app):
    entries = list(getattr(app.logs, "entries", []))
    store = getattr(app, "store", None)
    binding = (getattr(app, "project_state", {}) or {}).get("binding")
    if binding and not entries:
        # Bound declarations are already validated and published. Paired views
        # can use them before the user opens the native catalog, without a read.
        entries = list((getattr(app, "project_state", {}) or {}).get("logs", []))
    if store is not None and hasattr(app, "log_target") and not binding:
        snapshot = getattr(store, "render_snapshot", None) or store.snapshot
        snap = snapshot()
        job = app.log_target(snap)
        if job is not None:
            details = snap.get("details", {}).get(job.id, {})
            for key, field in (("stdout", "StdOut"), ("stderr", "StdErr")):
                declared = details.get(field)
                if (
                    not isinstance(declared, str)
                    or not declared.strip()
                    or declared.strip().casefold()
                    in {"(null)", "n/a", "unknown", "none"}
                ):
                    # Expanding an explicit scheduler path is pure. Guessing a
                    # local fallback probes the filesystem and belongs to the
                    # catalog worker, never to a redraw of cached entries.
                    continue
                path = stdout_path(
                    job, details, (app.files or app.logs.files or LocalFiles()), field
                )
                if path and not any(entry["path"] == path for entry in entries):
                    entries.append(
                        dict(
                            id="scheduler." + key,
                            path=path,
                            label=key,
                            group="Scheduler",
                            source="scheduler",
                        )
                    )
    if app.logs.entry and not any(e["path"] == app.logs.entry["path"] for e in entries):
        entries.insert(0, dict(app.logs.entry))
    return entries
