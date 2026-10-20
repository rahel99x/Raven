"""One bounded cache for native prepared Jobs rows, before group projection.

Native Store publications use their immutable-row revision. External snapshots
compare current fields to detect callers correcting records in place. Selection,
groups, viewport geometry and animated icons remain owned by the renderers.
Arbitrary action/plugin callbacks keep their original uncached behavior.
"""

from __future__ import annotations

from dataclasses import fields
from collections.abc import Mapping
from types import MappingProxyType
from operator import attrgetter

from . import clock
from .actions import Actions
from .model import GpuSample, Job, Live, compact, stamp

MAX_JOBS = 10000
_JOB_FIELDS = attrgetter(
    *(field.name for field in fields(Job) if field.name != "hosts")
)
_LIVE_FIELDS = attrgetter("avg", "rate", "rss")
_GPU_FIELDS = attrgetter("node", "index", "util", "vendor", "uuid", "bdf", "partition")
_THRESHOLDS = ("cpu", "mem", "gpu", "warn_after_minutes")
_ACTION_MARK = Actions.mark


def _owner(app):
    return getattr(app, "_chart_owner", app)


def _copy_rows(rows):
    """Return independent mutable presentation maps, retaining exact jobs."""
    result = []
    for row in rows:
        value = dict(row)
        for key in ("_styles", "_sort"):
            if isinstance(value.get(key), Mapping):
                value[key] = dict(value[key])
        result.append(value)
    return result


def _freeze_rows(rows):
    """Own immutable native row maps once, while retaining checked job refs."""
    result = []
    for row in rows:
        value = dict(row)
        for key in ("_styles", "_sort"):
            if isinstance(value.get(key), Mapping):
                value[key] = MappingProxyType(dict(value[key]))
        result.append(MappingProxyType(value))
    return tuple(result)


def copy_visible(rows):
    """Only visible native rows need mutable marks and presentation styles."""
    return _copy_rows(rows)


def lookup(views, snap, app, actions, progress_sources, cascade, *, shared=False):
    """Return (complete input key, independent cached rows or None).

    Native table consumers may opt into immutable shared rows and copy only
    their visible slice. External consumers retain independent mutable maps.

    ``progress_sources`` is the already validated output of
    ``job_progress.published`` for this pass. No report, scheduler or file is
    read here. A cache miss keeps the existing row builder unchanged.
    """
    jobs = snap.get("jobs", ())
    if (
        (
            actions is not None
            and (
                type(actions) is not Actions
                or getattr(actions.mark, "__func__", None) is not _ACTION_MARK
            )
        )
        or not isinstance(jobs, (list, tuple))
        or len(jobs) > MAX_JOBS
        or getattr(getattr(views, "plugins", None), "flags", ())
        or type(views).__module__ != "raven.views"
        or type(views).__name__ != "Views"
        or "flags_of" in vars(views)
        or "plugin_flags" in vars(views)
    ):
        return None, None
    publication = snap.get("_ui_publication_token")
    if publication is not None:
        # Native Store mutation methods publish a revision and immutable row
        # replacements. External/plugin snapshots retain full-value checking.
        if cascade and any(key == "left" for key, _ in cascade):
            return None, None
        from .table_ui import fingerprint

        marks = (
            tuple(
                (jid, label, at, clock.now() - at > 120)
                for jid, (label, at) in actions.marks.items()
            )
            if actions is not None
            else ()
        )
        key = (
            "publication",
            publication,
            tuple(progress_sources.items()),
            marks,
            clock.today(),
            fingerprint(app, "jobs"),
            app.sort.get("jobs", "state"),
            app.reverse.get("jobs", False),
            app.filter,
            getattr(app, "theme", None),
            bool(views.g.ascii),
            views.g.dot,
            getattr(views.g, "pin", "^"),
            tuple(views.th.get(name) for name in _THRESHOLDS),
        )
        cached = getattr(_owner(app), "job_rows_cache_state", None)
        if cached is not None and cached[0] is views and cached[1] == key:
            return key, cached[2] if shared else _copy_rows(cached[2])
        return key, None
    live, gpu, tags = snap.get("live", {}), snap.get("gpu", {}), snap.get("tags", {})
    job_inputs, waiting = [], []
    now = None
    for job in jobs:
        if type(job) is not Job:
            return None, None
        if not isinstance(job.hosts, (list, tuple)):
            return None, None
        sample = live.get(job.id)
        if sample is not None and type(sample) is not Live:
            return None, None
        devices = gpu.get(job.id) or ()
        if any(type(device) is not GpuSample for device in devices):
            return None, None
        metadata = tags.get(job.id, {})
        names, note = metadata.get("tags", ()), metadata.get("note", "")
        if not isinstance(names, (list, tuple)) or not isinstance(note, str):
            return None, None
        job_inputs.append(
            (
                id(job),
                _JOB_FIELDS(job),
                tuple(job.hosts),
                _LIVE_FIELDS(sample) if sample is not None else None,
                tuple(_GPU_FIELDS(device) for device in devices),
                tuple(names),
                bool(metadata.get("pinned")),
                note,
                actions.mark(job) if actions is not None else "",
            )
        )
        if job.pending and job.submit and (submitted := stamp(job.submit)) is not None:
            now = clock.now() if now is None else now
            # Waited text changes once per second initially, then at compact
            # minute/hour boundaries. It does not invalidate on every frame.
            waiting.append((job.id, compact(now - submitted)))
    # Pending LEFT sort values are unrounded observations. Do not hide a sort
    # boundary against running jobs behind compact elapsed text.
    if waiting and cascade and any(key == "left" for key, direction in cascade):
        return None, None
    from .table_ui import fingerprint

    key = (
        tuple(job_inputs),
        tuple(snap.get("gpu_mean", {}).items()),
        tuple(progress_sources.items()),
        tuple(waiting),
        clock.today(),
        fingerprint(app, "jobs"),
        app.sort.get("jobs", "state"),
        app.reverse.get("jobs", False),
        app.filter,
        getattr(app, "theme", None),
        bool(views.g.ascii),
        views.g.dot,
        getattr(views.g, "pin", "^"),
        tuple(views.th.get(name) for name in _THRESHOLDS),
    )
    cached = getattr(_owner(app), "job_rows_cache_state", None)
    if cached is not None and cached[0] is views and cached[1] == key:
        return key, cached[2] if shared else _copy_rows(cached[2])
    return key, None


def remember(views, app, key, rows):
    """Keep one prepared pass; never store collapsed/group-decorated rows."""
    owner = _owner(app)
    owner.job_rows_cache_state = (
        None if key is None else (views, key, _freeze_rows(rows))
    )
