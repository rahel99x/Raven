"""A complete portable dashboard report, rendered entirely as ASCII terminal text.

The interactive Views remain the single source of tables and charts. Rendering
uses isolated UI state so exporting never changes the user's current screen.
"""
from __future__ import annotations

import copy
from collections import OrderedDict
from dataclasses import dataclass
import math
import os
from pathlib import Path
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace
from typing import List

from . import clock, layout
from .controller import KEY_LABELS_ASCII
from .views import ANALYTICS_VIEWS, TABS
from .research import RESEARCH_VIEWS
from .series_archive import SeriesArchive


_TRANSLATE = str.maketrans({"\u00b7": ".", "\u2026": "...", "\u2192": "->", "\u2190": "<-", "\u2013": "-", "\u2014": "--", "\u00d7": "x"})


def ascii_text(value) -> str:
    """Keep printable ASCII; make Unicode and terminal controls visible, harmless text."""
    text = str(value).translate(_TRANSLATE).replace("\t", "    ")
    return "".join(ch if " " <= ch <= "~" else ch.encode("unicode_escape").decode("ascii") for ch in text)


def _render(rows, width: int) -> str:
    clean = [[(ascii_text(text), "") for text, _ in row] for row in rows]
    return layout.to_text(clean, width, color=False)


def _ui_copy(obj):
    """Copy mutable UI containers while retaining services, configuration and store bindings."""
    from .historical_replay import Timeline
    def containers(value):
        if isinstance(value, Timeline):
            return value.clone()
        if isinstance(value, dict):
            cloned = copy.copy(value)
            for key, item in value.items():
                cloned[key] = containers(item)
            return cloned
        if isinstance(value, list):
            return [containers(item) for item in value]
        if isinstance(value, tuple):
            return tuple(containers(item) for item in value)
        if isinstance(value, set):
            return set(value)
        return value
    result = copy.copy(obj)
    for key, value in vars(obj).items():
        if isinstance(value, (dict, list, set)):
            setattr(result, key, containers(value))
    if getattr(obj, "layout_state", None) is not None:
        result.layout_state = copy.deepcopy(obj.layout_state)
    return result


class ReportCancelled(OSError):
    """A report cancelled before its complete file was published."""


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise ReportCancelled("Report export cancelled; no incomplete report was published")


class _ReportStore:
    """Captured scheduler records with a private reader for persisted series."""

    def __init__(self, original, snap, at):
        self._snap, self._at = snap, at
        self._reader = copy.copy(original)
        self._reader.lock = threading.RLock()
        self._reader.series = copy.deepcopy(original.series)
        self._reader._series_loaded = set(original._series_loaded)
        self._reader.jobs = snap["jobs"]
        self._reader.finished = snap["finished"]
        self._reader.departed_jobs = snap.get("departed_jobs", {})
        self._reader._job_attempts = dict(original._job_attempts)
        self._reader.details = copy.deepcopy(snap.get("details", {}))
        self._reader._recording_identities = dict(original._recording_identities)
        self._reader._recording_observations = dict(original._recording_observations)
        self._reader._recording_evidence = dict(original._recording_evidence)
        self._reader._recording_bindings = copy.deepcopy(original._recording_bindings)
        self._reader._state_owner = None
        self._reader._series_archive = SeriesArchive(self._reader)
        self._reader._series_archive.states = copy.deepcopy(original._series_archive.states)
        self._reader._series_archive.limited = set(original._series_archive.limited)
        self.lock, self.series = self._reader.lock, self._reader.series
        self.state_dir, self.persist = original.state_dir, original.persist
        self.alerts = None
        if getattr(original, "alerts", None) is not None:
            count = original.alerts.active_count()
            messages = tuple(original.alerts.active_text())
            self.alerts = SimpleNamespace(active_count=lambda: count, active_text=lambda: list(messages))

    def snapshot(self):
        return self._snap

    def job(self, jid):
        return next((job for job in self._snap["jobs"] if job.id == jid), None)

    def series_jobs(self):
        return self._reader.series_jobs()

    def series_of(self, jid):
        # Disk reads stay on the export worker. Samples recorded after the
        # request cannot enter an otherwise captured scheduler snapshot.
        values = self._reader.series_of(jid)
        def before_request(sample):
            t = sample.get("t")
            return not (type(t) in (int, float) and math.isfinite(t) and t > self._at)
        values = [sample for sample in values if before_request(sample)]
        self._reader.series[jid].clear()
        self._reader.series[jid].extend(values)
        return values


def _research_copy(hub, cancel=None):
    """Reuse readers synchronously inside the already scheduled export task.

    Dispatching ``request(wait=True)`` onto the same single worker deadlocks.
    A private hub facade instead reads directly, retaining separate settings,
    caches, passports and metric-reader state without creating another pool.
    """
    from .research import ResearchHub
    class ReportResearch(ResearchHub):
        def request(self, context, *, wait=False, force=False):
            _check_cancel(self.report_cancel)
            key = self._key(context)
            if key not in self.cache or force:
                try:
                    value = self._read(context)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    if isinstance(exc, ReportCancelled):
                        raise
                    from .research import clean
                    value = {"status": "error", "summary": clean(exc)}
                _check_cancel(self.report_cancel)
                self.cache[key] = (time.monotonic(), value)
            return self.current(context)

        def _tail(self, path, limit):
            _check_cancel(self.report_cancel)
            result = super()._tail(path, limit)
            _check_cancel(self.report_cancel)
            return result

    private = ReportResearch.__new__(ReportResearch)
    with hub.lock:
        private.__dict__.update(vars(hub))
        for key in ("settings", "log_settings", "plan", "passport", "passport_diff", "planning_source", "planning_choices"):
            setattr(private, key, copy.deepcopy(getattr(hub, key, None)))
        observations = hub.forecasts.observations() if hub.forecasts else None
        private.forecasts = SimpleNamespace(observations=lambda: copy.deepcopy(observations)) if observations is not None else None
    private.lock = threading.RLock()
    private.cache, private.reader = OrderedDict(), None
    private.pool, private.future, private.pending = None, None, None
    private.closed, private.report_cancel = False, cancel
    return private


def _files_copy(files, cancelled):
    """Keep backend type/identity while checking cancellation between reads."""
    private = copy.copy(files)
    def guarded(function):
        def call(*args, **kwargs):
            _check_cancel(cancelled)
            value = function(*args, **kwargs)
            _check_cancel(cancelled)
            return value
        return call
    for name in ("stat", "snapshot_stat", "read", "tail", "exists", "listdir"):
        function = getattr(files, name, None)
        if callable(function):
            setattr(private, name, guarded(function))
    # Catalog discovery intentionally uses exact SSH backend helpers. Guard
    # those operations too, instead of allowing cancellation to wait for a
    # whole multi-file directory/catalog inspection.
    if getattr(files, "ssh", None) is not None:
        private.ssh = copy.copy(files.ssh)
        private.ssh.run = guarded(files.ssh.run)
    return private


@dataclass
class PreparedReport:
    """Inputs captured on the UI thread; render and write only on the worker."""

    snap: dict
    app: object
    views: object
    actions: object
    at: float
    title: str = ""
    width: int = 132

    def render(self, *, cancel=None):
        _check_cancel(cancel)
        files = _files_copy(self.app.logs.files, cancel)
        self.app.files = self.app.logs.files = self.views.files = files
        if self.app.research is not None:
            self.app.research.report_cancel = cancel
            self.app.research.files = files
        return build(self.snap, self.app, self.views, self.actions, self.title,
                     self.width, at=self.at, cancel=cancel)

    def write(self, state_dir, name, *, cancel=None):
        """Publish a complete private file atomically; cancellation cleans up."""
        if not isinstance(name, str) or not name or Path(name).name != name or name in (".", ".."):
            raise ValueError("Report export requires a plain file name")
        content = self.render(cancel=cancel)
        _check_cancel(cancel)
        from .export import export_dir
        directory = export_dir(state_dir)
        fd, temporary = tempfile.mkstemp(prefix=".report-", suffix=".tmp", dir=directory)
        destination = os.path.join(directory, name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
                for offset in range(0, len(content), 65536):
                    _check_cancel(cancel)
                    output.write(content[offset:offset + 65536])
                output.flush()
                os.fsync(output.fileno())
            _check_cancel(cancel)
            os.replace(temporary, destination)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return destination


def prepare_export(snap, app, views, actions=None, *, title="", width=132):
    """Capture identities and in-memory observations without file/scheduler I/O.

    Call this before ``ResearchHub.start_task``. The returned ``write`` method
    performs all report file reads and rendering on that existing worker.
    """
    at = clock.now()
    with app.store.lock:
        frozen = copy.deepcopy(snap)
        store = _ReportStore(app.store, frozen, at)
    local, display = _ui_copy(app), _ui_copy(views)
    local.cfg = copy.deepcopy(app.cfg)
    display.cfg = local.cfg
    display.th, display.gpu_types = local.cfg["thresholds"], local.cfg["gpu_types"]
    local.store, local.sampler = store, None
    local.logs = _ui_copy(app.logs)
    local.logs.buffers, local.logs.catalog = {}, None
    local.logs._buffer_token = None
    for field in ("_async_pending", "_async_active"):
        if hasattr(local.logs, field):
            setattr(local.logs, field, None)
    if hasattr(local.logs, "_async_attempts"):
        local.logs._async_attempts = {}
    local.logs.clear_selection(reset_cursor=True)
    local.log_record = copy.deepcopy(app.log_record)
    local.research = _research_copy(app.research) if app.research is not None else None
    if actions is not None:
        actions = _ui_copy(actions)
        actions.store = store
    return PreparedReport(frozen, local, display, actions, at, title, max(60, min(240, int(width))))


def build(snap: dict, app, views, actions=None, title: str = "", width: int = 132, *, at=None, cancel=None) -> str:
    """All dashboard pages, analytics and node subviews, as a bounded-width ASCII report.

    The report includes every snapshot row and available recorded job series;
    log output is the selected log's current 40-line window. Screen filters are
    cleared only on the private copy, ensuring a full report without losing the
    caller's filter, selection, marks, glyph mode, scroll positions or log state.
    """
    _check_cancel(cancel)
    width = max(60, min(240, int(width)))
    local = _ui_copy(app)
    from .transitions import CompletionFeedback
    local.completion = CompletionFeedback(snap)
    local.interactive = False
    # This report describes its explicitly captured scheduler snapshot. A
    # transient historical overlay must not blank all of its pages, nor may
    # clearing that overlay mutate the interactive observation timeline.
    if getattr(local, "mode", None) == "historical":
        local.mode = "main"
    display = _ui_copy(views)
    display.g = layout.Glyphs(True)
    local.labels = KEY_LABELS_ASCII
    local._ascii_cfg = True
    local.views_ref = display
    local.filter = ""
    local.filter_edit = ""
    if hasattr(local, "table_state"):
        local.table_state["facets"] = {}
        local.table_state["hidden"] = {}
        local.table_state["groups"] = False
        local.table_state["collapsed"] = []
    if hasattr(local, "log_workbench_state"):
        local.log_workbench_state.update(view="plain", pan=0, collapsed=[])
    local.log_lines = 0
    local.cursor = {key: 0 for key, _ in TABS}
    local.top = {key: 0 for key, _ in TABS}
    local.logs = _ui_copy(app.logs)
    local.logs.clear_selection(reset_cursor=True)
    local.logs.browser = False
    # A new buffer cache avoids mutating the interactive reader or copying its
    # potentially large retained files. The normal bounded file reader is reused.
    local.logs.buffers = {}
    local.logs._buffer_token = None
    selected_log = app.log_job or app.selected_id
    now = clock.now() if at is None else at
    generated = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(now))
    who = ascii_text(app.user)
    if getattr(app, "host_label", ""):
        who += " @ " + ascii_text(app.host_label)
    border = "=" * width
    out: List[str] = [border, ascii_text(title or "RAVEN | COMPLETE TERMINAL REPORT")[:width], border,
                      f"Snapshot: {generated}"[:width], f"User: {who}"[:width]]
    if snap.get("account", {}).get("account"):
        out.append("Account: " + ascii_text(snap["account"]["account"]))
    if getattr(app, "demo", False):
        out.append("[DEMO] Simulated cluster data. No live cluster connection.")
    out.extend(textwrap.wrap("Ten dashboard pages, ASCII charts, job series, node maps, research evidence and source diagnostics. This is a fixed snapshot; unmeasured values remain unavailable.", width))
    out.extend(textwrap.wrap("All jobs are included regardless of the screen filter. Tables and charts fit this report's width. The Log page contains one 40-line window of the selected log; it is not the whole file.", width))
    if at is not None:
        out.extend(textwrap.wrap("Scheduler records, run identity and in-memory observations were captured when export was requested. Project files were inspected during report creation.", width))
    out.extend(["", "CONTENTS", "  01 Jobs        02 Cluster       03 History       04 Analytics",
                "  05 Nodes       06 Group         07 Dependencies  08 Log",
                "  09 Sources     10 Research      11 Event journal", ""])

    def heading(text: str):
        out.extend(["", border, ascii_text(text)[:width], border])

    def rows(rendered):
        out.append(_render(rendered, width))

    def page(number: int, key: str, label: str):
        _check_cancel(cancel)
        heading(f"{number:02d} / {label.upper()}")
        local.tab = key
        rendered, _ = display.compose(snap, local, width, None, actions)
        rows(rendered)

    page(1, "jobs", "Jobs")
    selected_job = local.selected_id
    for job in snap["jobs"]:
        _check_cancel(cancel)
        if job.id != selected_job:
            rows([layout.rule(display.g, width, f"job detail: {job.id}")])
            rows(display.selected_panel(snap, job, width, 0, local))
    page(2, "cluster", "Cluster")
    page(3, "history", "History")
    if not snap["finished"]:
        out.append("No completed runs in this snapshot. Accounting history appears when available.")

    local.analytics_view = "job"
    page(4, "analytics", "Analytics")
    first_series = local.analytics_job
    for jid in display.analytics_jobs(snap, local):
        _check_cancel(cancel)
        if jid != first_series:
            local.analytics_job = jid
            rows(display.analytics_job(snap, local, width, None))
    for key, label in ANALYTICS_VIEWS:
        _check_cancel(cancel)
        if key != "job":
            rows([layout.rule(display.g, width, f"analytics / {label}")])
            local.analytics_view = key
            rendered, _ = display.analytics_tab(snap, local, width, None)
            rows(rendered)

    local.nodes_view = "mine"
    page(5, "nodes", "Nodes")
    local.nodes_view = "map"
    rendered, _ = display.nodes_tab(snap, local, width, None)
    rows(rendered)
    page(6, "group", "Group")
    page(7, "deps", "Dependencies")
    local.log_job = selected_log or selected_job
    page(8, "log", "Log")
    page(9, "sources", "Sources")
    for health in sorted(snap.get("health", {}).values(), key=lambda h: h.name):
        if health.error:
            out.extend(textwrap.wrap(ascii_text(f"{health.name}: {health.error}"), width,
                                     subsequent_indent="  ", break_long_words=True, break_on_hyphens=False))

    local.research_view = "experiment"
    if getattr(local, "research", None):
        local.research.request(local.research.context(snap, local), wait=True)
    page(10, "research", "Research")
    for key, label in RESEARCH_VIEWS:
        _check_cancel(cancel)
        if key != "experiment":
            local.research_view = key
            local.research_scroll = 0
            rows([layout.rule(display.g, width, f"research / {label}")])
            if getattr(local, "research", None):
                local.research.request(local.research.context(snap, local), wait=True)
            rendered, _ = display.research_tab(snap, local, width, None)
            rows(rendered)

    heading("11 / EVENT JOURNAL")
    events = list(snap.get("events", []))
    if events:
        out.append(f"{len(events)} recorded events in this snapshot, oldest first.")
        for event in events:
            timestamp = time.strftime("%m-%d %H:%M:%S", time.localtime(event.get("t", 0)))
            job = f" job={event['job']}" if event.get("job") else ""
            record = ascii_text(f"{timestamp} [{event.get('kind', 'event')}]{job} {event.get('text', '')}")
            out.extend(textwrap.wrap(record, width, subsequent_indent="  ", break_long_words=True, break_on_hyphens=False))
    else:
        out.append("No activity recorded yet. Job transitions, alerts and actions appear here.")
    _check_cancel(cancel)
    out.extend(["", "-" * width, "End of report | Raven | Plain ASCII, no external resources."])
    # Defensive final bound also covers explanatory text on narrow exports.
    return "\n".join(ascii_text(line)[:width] for block in out for line in block.split("\n")) + "\n"
