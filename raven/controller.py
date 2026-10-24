"""The application state and key handling, free of curses so tests can drive it: tabs, cursors, marks, sort and
filter, overlays (confirm, details, help), the log tab, and the actions with their confirmations."""
from __future__ import annotations

import time
import shlex
import math
import os
from typing import Dict, List, Optional, Sequence

from . import clipboard, export, workbench, command_registry
from .job_logs.buffer import LogSession
from .actions import Actions
from .model import Job, Store, secs
from .sampler import Sampler
from .ui.catalog import ANALYTICS_VIEWS, NODES_VIEWS, SORTS, TABS
from .ui.catalog import RESEARCH_VIEWS
from .transitions import CompletionFeedback
from .ui.keys import KEY_LABELS, KEY_LABELS_ASCII

from .palette import THEME_NAMES, canonical_theme
THEMES = list(THEME_NAMES)


class App:
    def __init__(self, store: Store, sampler: Optional[Sampler], actions: Optional[Actions], cfg, user: str, ascii_: bool = False, interactive: bool = True):
        self.store, self.sampler, self.actions, self.cfg, self.user = store, sampler, actions, cfg, user
        self.keymap: Dict[str, str] = cfg.keymap()
        self.labels = KEY_LABELS_ASCII if ascii_ else KEY_LABELS
        self._ascii_cfg = ascii_
        self.config_path = getattr(cfg, "path", "")
        self.interactive = interactive
        persistence_scheduler = getattr(sampler, "worker_scheduler", None)
        if interactive and persistence_scheduler is not None:
            self.persistence_lane = persistence_scheduler.lane("source")
            store.configure_persistence_writer(self.persistence_lane.submit)
        self.tab = "jobs"
        self.cursor: Dict[str, int] = {t: 0 for t, _ in TABS}
        self.top: Dict[str, int] = {t: 0 for t, _ in TABS}
        self.sort: Dict[str, str] = {"jobs": "state", "history": "end", "nodes": "name", "group": "user"}
        self.nodes_view = "mine"
        self.compare_ids: List[str] = []
        self.dep_ids: List[str] = []
        self.reverse: Dict[str, bool] = {}
        self.marks: set = set()
        self.filter, self.filter_edit = "", ""
        self.mode = "main"
        self.confirm: dict = {}
        self.detail_id: Optional[str] = None
        self.scroll = 0
        self.log_lines = int(cfg["log_lines"])
        self.log_job: Optional[str] = None
        self.log_record = None
        self.logs = LogSession(max_bytes=int(cfg["log_max_mb"]) << 20)
        self.gpu = bool(cfg["gpu_sampling"])
        self.bell = bool(cfg["bell"])
        self.message, self.message_t = "", 0.0
        self.ui_publication_revision = 0
        self.command_ok = True
        self.selected_id: Optional[str] = None
        self.visible_ids: List[str] = []
        self.recent_ids: List[str] = []
        self.source_ids: List[str] = []
        self.jobs_selection_options = None
        self.tab_hits: list = []
        self.quit = False
        self.sel_anchor: Optional[int] = None             # line selection (screen rows) for copying
        self.sel_end: int = 0
        self.click_row: Optional[int] = None
        self.last_rows: list = []
        self.palette_edit = ""
        configured_theme = canonical_theme(cfg["theme"])
        self.theme = configured_theme if configured_theme in THEMES else "default"
        self.plugins = None                                # PluginAPI (set by the cli)
        self.switch_profile: Optional[str] = None          # set by :profile <name>: the cli restarts with it
        self.profile_name = getattr(cfg, "profile_name", "")
        self.files = None                                  # the files reader (LocalFiles / RemoteFiles), set by the cli
        self.analytics_view = "job"
        self.analytics_job: Optional[str] = None
        self.research = None
        self.forecast_scope = None
        self.research_view = "experiment"
        self.research_job_id = None
        self.research_scroll = 0
        self.research_rows = 0
        self.research_groups = []
        self.research_array_open = False
        self.research_task_offset = 0
        self.research_array_focus = False
        self.research_result = None
        self.research_data_jobs = []
        days = list(cfg["analytics_days"]) or [1, 2, 7]
        self.days_options = days
        self.days_index = days.index(cfg["history_days"]) if cfg["history_days"] in days else 0
        self.state_dir = store.state_dir if store.persist else None
        self.views_ref = None                              # set by the screen: the Views instance (exports render through it)
        self.width = 120
        self.last_hits: list = []
        self.completion = CompletionFeedback(store.render_snapshot())
        self.last_jobs_ids = None
        self.last_history_ids = None
        self.last_history_options = None
        self.log_selection_expected = False
        self.log_render_token = None
        workbench.initialize(self)
        self.restore(store.load_ui())
        self.labels = KEY_LABELS_ASCII if self.theme == "reader" or ascii_ else KEY_LABELS
        enable_series_background = getattr(sampler, "enable_series_background", None)
        if interactive and callable(enable_series_background):
            enable_series_background()

    # ---- persistence -------------------------------------------------------------------------------
    def restore(self, ui: dict):
        ui = ui if isinstance(ui, dict) else {}
        for k in ("tab", "log_lines", "gpu", "bell"):
            if k in ui and k != "tab":
                setattr(self, k, ui[k])
        if ui.get("tab") in dict(TABS):
            self.tab = ui["tab"]
        if isinstance(ui.get("sort"), dict):
            self.sort.update({k: v for k, v in ui["sort"].items() if v in SORTS.get(k, [])})
        if isinstance(ui.get("reverse"), dict):
            self.reverse.update({k: bool(v) for k, v in ui["reverse"].items() if k in SORTS})
        restored_theme = canonical_theme(ui.get("theme"))
        if restored_theme in THEMES:
            self.theme = restored_theme
        if ui.get("analytics_view") in dict(ANALYTICS_VIEWS):
            self.analytics_view = ui["analytics_view"]
        if ui.get("nodes_view") in dict(NODES_VIEWS):
            self.nodes_view = ui["nodes_view"]
        if ui.get("research_view") in dict(RESEARCH_VIEWS):
            self.research_view = ui["research_view"]
        if isinstance(ui.get("bookmarks"), dict):
            self.logs.bookmarks = {k: sorted(int(i) for i in v) for k, v in ui["bookmarks"].items() if isinstance(v, list)}
        self.logs.wrap = bool(ui.get("log_wrap", False))
        workbench.restore(self, ui.get("workbench", {}))

    def save(self):
        from .pane_drag import committed
        from .ui_persistence import save
        forecast_state = None
        if self.store.persist and self.forecast_scope is not None and self.research and self.research.forecasts:
            forecast_state = dict(version=1, scope=dict(self.forecast_scope), observations=self.research.forecasts.observations())
        with committed(self):
            save(self, dict(tab=self.tab, log_lines=self.log_lines, gpu=self.gpu, bell=self.bell, sort=self.sort, theme=self.theme, analytics_view=self.analytics_view,
                                    nodes_view=self.nodes_view, research_view=self.research_view, bookmarks=self.logs.bookmarks, log_wrap=self.logs.wrap,
                                    forecast_state=forecast_state, workbench=workbench.save(self), reverse=self.reverse))

    def analytics_days_value(self) -> float:
        return float(self.days_options[self.days_index])

    def set_theme(self, name: str):
        name = canonical_theme(name)
        if name not in THEMES:
            self.fail("theme <" + "|".join(THEMES) + ">")
            return
        self.theme = name
        self.labels = KEY_LABELS_ASCII if name == "reader" or self._ascii_cfg else KEY_LABELS
        if self.views_ref is not None and hasattr(self.views_ref, "set_ascii"):
            self.views_ref.set_ascii(True if name == "reader" else self._ascii_cfg)
        self.say(f"theme {name}" + (" (plain text, no colour, no glyphs)" if name == "reader" else ""))

    @property
    def filter(self):
        from .table_tools import filter_text
        return filter_text(self, getattr(self, "tab", "jobs"))

    @filter.setter
    def filter(self, text):
        from .table_tools import set_filter_text
        set_filter_text(self, getattr(self, "tab", "jobs"), text)

    def settings_changed(self, values):
        """Notify the terminal renderer after settings have been applied."""
        self.terminal_settings_generation = getattr(self, "terminal_settings_generation", 0) + 1

    @property
    def follow(self) -> bool:
        return self.logs.following

    # ---- helpers the views call --------------------------------------------------------------------
    def keys_help(self, action: str) -> str:
        keys = self.cfg["keys"].get(action, [])
        return "/".join(self.labels.get(k, k) for k in keys[:2]) if keys else "?"

    def clamp_cursor(self, tab: str, n: int) -> int:
        cur = max(0, min(self.cursor.get(tab, 0), n - 1)) if n else 0
        self.cursor[tab] = cur
        return cur

    def scroll_to(self, tab: str, cursor: Optional[int], visible: int, n: int) -> int:
        """Paint a bounded viewport, following only a cursor in this table.

        Jobs and Recents share one selection cursor but have independent
        viewports. ``None`` preserves the inactive table's logical offset
        and any in-flight wheel target instead of following the other table.
        """
        from . import scrollbars, scrolling
        top = self.top.get(tab, 0)
        state = scrollbars.initialize(self)
        manual = state['manual'].get(tab)
        if cursor is not None and manual and manual['cursor'] != self.cursor.get('jobs' if tab == 'recent' else tab):
            scrollbars.resume(self, tab)
            manual = None
        if manual is not None:
            top = manual['target']
        if manual is None and cursor is not None:
            if cursor < top:
                top = cursor
            if cursor >= top + visible:
                top = cursor - visible + 1
        top = max(0, min(top, max(0, n - visible)))
        self.top[tab] = top
        if getattr(self, 'scrollbar_probe', False):
            return top
        return scrolling.viewport(self, tab, top, n, visible,
                                  context=(self.tab, self.mode, getattr(self, 'width', 0)))

    def say(self, text: str, *, level="info", path="", job="", task=""):
        self.message, self.message_t = text, time.time()
        self.message_level = level
        if hasattr(self, "activity"):
            self.activity.post(text, level, path, job=job, task=task)
            if path:
                self.save()

    def fail(self, text: str):
        """Report a command failure independently of its human-readable wording."""
        self.command_ok = False
        self.say(text, level="error")

    def background_worker_scheduler(self):
        """Supply the existing application service owner to background clients."""
        scheduler = getattr(self, "worker_scheduler", None) or getattr(self.sampler, "worker_scheduler", None)
        if scheduler is not None:
            return scheduler
        if self.research is None:
            from .research import ResearchHub
            self.research = ResearchHub(self.cfg, self.files or self.logs.files)
        return self.research.worker_scheduler

    def tick(self):
        """Housekeeping before each frame: expire the message, tell the sampler which job is selected."""
        clipboard.poll(self)
        scheduler = getattr(self.sampler, "worker_scheduler", None)
        if scheduler is not None:
            dropped = scheduler.status().get("notification_dropped", 0)
            if dropped > getattr(self, "notification_dropped_seen", 0):
                self.notification_dropped_seen = dropped
                self.say(f"{dropped} stale notification hooks skipped while their bounded queue was full; scheduler collection remains available", level="warning")
        if self.message and time.time() - self.message_t > 6:
            self.message = ""
        if self.research:
            pending=getattr(self.research,'pending',None)
            self.research.poll_task()
            if pending is not None and getattr(self.research,'pending',None) is not pending:
                # A command callback can publish pane state without a message
                # or scheduler update. Retained documents must still refresh.
                self.ui_publication_revision += 1
        if self.sampler:
            inspector = self.mode == "analysis" and getattr(self, "analysis_state", {}).get("modal") == "inspect"
            detail_mode = self.mode == "details" or inspector
            jid = (self.detail_id if detail_mode else
                   (self.log_job or self.selected_id) if self.tab == "log" else
                   (self.research_job_id or self.selected_id) if self.tab == "research" else
                   self.selected_id if self.tab in ("jobs", "history", "traces") else None)
            self.sampler.select(jid)
            self.sampler.gpu_sampling = self.gpu
            self.sampler.marks = set(self.marks)
            fin_target = self.detail_id if detail_mode else (
                self.selected_id if self.tab in ("jobs", "history", "traces") and self.mode == "main"
                and (self.tab == "traces" or getattr(self, "job_panel_state", {}).get("mode") != "off") else None)
            self.sampler.select_fin(fin_target if fin_target and not self.store.job(fin_target) else None)
            panel = getattr(self, "job_panel_state", {})
            inline_trace = (self.tab in ("jobs", "history") and self.mode == "main" and panel.get("mode") == "analytics"
                            and panel.get("analytics_view", "job") == "job")
            inline_live = (self.tab in ("jobs", "history") and self.mode == "main"
                           and panel.get("mode", "inspector") == "inspector")
            self.sampler.select_trace(self.selected_id if inline_trace else
                                      self.analytics_job if (self.tab == "analytics" and self.analytics_view == "job") else None)
            demand = getattr(self.sampler, "set_collection_demand", None)
            if demand is not None:
                sources = ("live", "trace") if inline_trace else ("live",) if inline_live or detail_mode else ()
                demand(self.tab, jid if detail_mode else self.analytics_job if self.tab == "analytics" else jid,
                       sources=sources)
        workbench.tick(self)

    @property
    def animations_enabled(self):
        return self.interactive and bool(self.cfg.get("animations", True)) and self.theme != "reader"

    def selected_job(self) -> Optional[Job]:
        return self.store.job(self.selected_id) if self.selected_id else None

    def job_record(self, jid, snap=None):
        """Resolve an exact ID across queue and accounting records, without another-job fallback."""
        if snap is None:
            return self.store.record_context(jid, include_group=True)[0]
        return next((j for records in (snap["jobs"], snap["finished"],
                                      snap.get("departed_jobs", {}).values(), snap.get("group", []))
                     for j in records if j.id == jid), None)

    def log_target(self, snap=None):
        from .project_ui import selected_binding
        binding = selected_binding(self)
        if binding and not binding.get("job_id"):
            return None
        jid = self.log_job or self.selected_id
        record = self.job_record(jid, snap)
        if record is not None and self.log_job:
            self.log_record = record
        return record or (self.log_record if self.log_record and self.log_record.id == jid else None)

    def finished_jobs(self, snap=None, *, recent=False):
        """The same sorted/filtered accounting records drive rendering and every row action."""
        from .table_tools import snapshot, filter_text, history_matches, recent_matches, recent_limit
        snap = snapshot(self, self.store.render_snapshot()) if snap is None else snap
        from .table_query import matches
        if recent:
            limit = recent_limit(self)
            if type(limit) is int and limit > 0:
                # Recents retain accounting order before their own displayed
                # subset is sorted. Stop once that subset is complete instead
                # of filtering an entire history window on each scroll frame.
                active = {job.id for job in snap["jobs"]}
                flt = filter_text(self, "recent").lower()
                fin = []
                for record in snap["finished"]:
                    if not matches(self, "recent", record, snap) or not recent_matches(self, record):
                        continue
                    if record.id in active:
                        continue
                    if flt.startswith("#"):
                        if flt[1:] not in [tag.lower() for tag in snap.get("tags", {}).get(record.id, {}).get("tags", [])]:
                            continue
                    elif flt and not any(flt in value.lower() for value in (record.name, record.id, record.state, record.partition)):
                        continue
                    fin.append(record)
                    if len(fin) == limit:
                        break
                return fin
        fin = list(snap["finished"])
        fin = [record for record in fin if matches(self, "recent" if recent else "history", record, snap)
               and (recent_matches(self, record) if recent else history_matches(self, record))]
        if recent:
            active = {j.id for j in snap["jobs"]}
            fin = [f for f in fin if f.id not in active]
        else:
            from .table_sort import chain, history_value, sort_rows
            cascade = chain(self, "history")
            key = self.sort.get("history", "end")
            keyfn = {"end": lambda f: f.end, "name": lambda f: (f.name, f.end),
                     "state": lambda f: (f.state, f.end), "elapsed": lambda f: secs(f.elapsed) or 0,
                     "cpu_eff": lambda f: -1 if f.cpu_eff is None else f.cpu_eff,
                     "mem_eff": lambda f: -1 if f.mem_eff is None else f.mem_eff}.get(key, lambda f: f.end)
            if cascade is None:
                fin.sort(key=keyfn, reverse=(key == "end") != self.reverse.get("history", False))
            else:
                fin = sort_rows(self, "history", fin, value=lambda record, column: history_value(record, column, snap))
        flt = filter_text(self, "recent" if recent else "history").lower()
        if flt.startswith("#"):
            fin = [f for f in fin if flt[1:] in [t.lower() for t in snap.get("tags", {}).get(f.id, {}).get("tags", [])]]
        elif flt:
            fin = [f for f in fin if any(flt in value.lower() for value in (f.name, f.id, f.state, f.partition))]
        if recent:
            return fin[:recent_limit(self)]
        self.history_all_records = fin
        from raven.jobs.group_domain import project_records
        return project_records(self, snap, fin, tab="history")

    def history_jobs(self, snap=None):
        from .table_query import fingerprint
        from raven.jobs.group_domain import registry
        if snap is None:
            from .table_tools import snapshot
            snap = snapshot(self, self.store.render_snapshot())
        publication = snap.get("_ui_publication_token")
        if publication is None:
            return self.finished_jobs(snap)
        groups = registry(self)
        key = (publication, self.filter, self.sort.get("history", "end"),
               self.reverse.get("history", False), fingerprint(self, "history"),
               tuple(sorted(groups.collapsed)), groups._source_revision,
               self.selected_id if groups.collapsed else None)
        cached = getattr(self, "history_records_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        records = self.finished_jobs(snap)
        self.history_records_cache = key, records
        return records

    def sync_history_selection(self, snap=None):
        fin = self.history_jobs(snap)
        ids = [f.id for f in fin]
        from .table_query import fingerprint
        options = self.filter, self.sort.get("history", "end"), self.reverse.get("history", False), fingerprint(self, "history")
        previous = self.last_history_ids
        if (previous is not None and ids != previous and options == self.last_history_options
                and self.selected_id in ids and self.selected_id in previous
                and self.cursor["history"] == previous.index(self.selected_id)):
            self.cursor["history"] = ids.index(self.selected_id)
        self.last_history_ids, self.last_history_options = ids, options
        from .ui.selection_state import selected
        self.selected_id = selected(self, "history", ids[self.clamp_cursor("history", len(ids))] if ids else None)
        return fin

    def recent_jobs(self, snap=None):
        from .table_tools import snapshot
        snap = snapshot(self, self.store.render_snapshot()) if snap is None else snap
        from . import recent_history
        from raven.jobs import group_domain as job_groups
        state = recent_history.initialize(self)
        state.projector = lambda observation, raw: job_groups.project_records(self, observation, raw, tab="recent")
        recent = state.projector(snap, recent_history.records(self, snap))
        state.loaded = recent
        return recent

    def table_sort_changed(self, table, *, persist=True):
        """Reanchor exact identities before a queued click/command can use them."""
        active = "jobs" if table == "recent" else table
        if active == self.tab and self.views_ref is not None:
            from .table_tools import snapshot
            snap, selected = snapshot(self, self.store.render_snapshot()), self.selected_id
            if active == "jobs":
                self.visible_ids = [row["id"] for row in self.views_ref.job_rows(snap, self, self.actions)]
                self.recent_ids = [record.id for record in self.recent_jobs(snap)]
                ids = self.visible_ids + self.recent_ids
                if self.reveal_sorted_group(selected, snap, ids):
                    self.visible_ids = [row["id"] for row in self.views_ref.job_rows(snap, self, self.actions)]
                    self.recent_ids = [record.id for record in self.recent_jobs(snap)]
                    ids = self.visible_ids + self.recent_ids
                if selected in ids:
                    self.cursor["jobs"] = ids.index(selected)
                from .ui.selection_state import selected as selection_value
                self.selected_id = selection_value(self, "jobs", ids[self.clamp_cursor("jobs", len(ids))] if ids else None)
                self.last_jobs_ids, self.jobs_selection_options = ids, self.jobs_options()
            elif active == "history":
                ids = [record.id for record in self.history_jobs(snap)]
                if self.reveal_sorted_group(selected, snap, ids):
                    ids = [record.id for record in self.history_jobs(snap)]
                if selected in ids:
                    self.cursor["history"] = ids.index(selected)
                self.last_history_ids = ids
                self.last_history_options = None
                self.sync_history_selection(snap)
            elif active == "group":
                self.views_ref.group_tab(snap, self, self.width, None)
                ids = getattr(self, "group_ids", [])
                if self.reveal_sorted_group(selected, snap, ids):
                    self.views_ref.group_tab(snap, self, self.width, None)
                    ids = getattr(self, "group_ids", [])
                if selected in ids:
                    self.cursor["group"] = ids.index(selected)
                self.sync_selection()
            elif active == "sources":
                names = self.ordered_source_ids()
                index = self.clamp_cursor("sources", len(names))
                chosen = names[index] if names else None
                self.views_ref.sources_tab(snap, self, self.width, None)
                if chosen in self.source_ids:
                    self.cursor["sources"] = self.source_ids.index(chosen)
            self.sel_anchor, self.click_row = None, None
        if persist:
            self.save()

    def reveal_sorted_group(self, selected, snap, displayed):
        """A changed group representative must not retarget a queued action."""
        if not selected or selected in displayed or not self.table_state.get("groups"):
            return False
        from raven.jobs.group_domain import registry, fold
        groups = registry(self)
        group = groups.ensure(snap).for_job(selected)
        if group is None or not groups.is_collapsed(group):
            return False
        fold(self, group.id, False)
        self.selected_id = selected
        return True

    def ordered_source_ids(self):
        """Actions use the rendered order, including between frames."""
        known = self.store.health
        names = [name for name in self.source_ids if name in known]
        if "sources" in getattr(self, "table_tools_state", {}).get("resource_ids", {}):
            return names
        return names + [name for name in sorted(known) if name not in names]

    def resolve_log_path(self):
        """Resolve the current stream now, including keys received between frames."""
        if self.tab != "log" or self.logs.browser:
            return ""
        if self.views_ref is not None:
            jid = self.log_job or self.selected_id
            record, details = self.store.record_context(jid, include_group=True)
            snap = {"jobs": [record] if record is not None else [], "finished": []}
            job = self.log_target(snap)
            if job is None:
                from .project_ui import selected_binding, resolve_log_entry
                if selected_binding(self):
                    entry = resolve_log_entry(self)
                    return entry.get("path", "") if entry else ""
                return ""
            return self.views_ref.log_path(self, job, details)[0]
        return self.logs.path

    def prepare_log(self):
        path = self.resolve_log_path()
        if not path:
            self.logs.clear_selection(reset_cursor=True)
            return None
        return self.read_log_buffer(path)

    def read_log_buffer(self, path):
        from .job_logs.workbench import sync_source, observe_buffer
        from .job_logs.tools import observe_buffer as observe_position
        if (self.mode not in ("main", "filter", "palette")
                or self.logs.search and getattr(self, "log_tools_state", {}).get("retained_pending")):
            # The modal owns an exact immutable source snapshot. Refreshing its
            # hidden tail would compete with explicit page/search navigation.
            return self.logs.buffers.get(path)
        sync_source(self, path)
        if self.interactive and getattr(self.logs.files, "remote", False) and self.research is None:
            from .research import ResearchHub
            self.research = ResearchHub(self.cfg, self.logs.files)
        buf = self.logs.buffer(path, worker=self.research, background=self.interactive)
        observe_position(self, buf)
        observe_buffer(self, buf)
        return buf

    def find_log_match(self, buf, backwards=False):
        from .job_logs.tools import find_retained
        result = find_retained(self, buf, backwards=backwards)
        return result[1] if result is not None else self.logs.find_next(buf, backwards=backwards)

    def count_log_matches(self, buf):
        from .job_logs.tools import count_retained
        return count_retained(self, buf) if buf is not None and self.logs.search else 0

    def log_search_message(self, buf):
        if not self.logs.search:
            return "search cleared"
        count = self.count_log_matches(buf)
        state = self.log_tools_state
        if state.get("retained_pending"):
            known = state.get("retained_known_count")
            return (f"Search index updating; {known} matches in the previous snapshot" if known is not None
                    else "Indexing retained log in the background; navigation remains available")
        return f"{count} lines match '{self.logs.search}'"

    def jobs_options(self):
        from .table_query import fingerprint
        return (self.filter, self.sort.get("jobs", "state"), self.reverse.get("jobs", False),
                fingerprint(self, "jobs"), fingerprint(self, "recent"))

    def open_log(self, jid=None):
        selects_job = jid is not None or self.tab != "log"
        self.sync_selection()
        jid = jid or (self.log_job if self.tab == "log" else self.selected_id)
        record = self.job_record(jid)
        if record is None and self.log_record and self.log_record.id == jid:
            record = self.log_record
        if record is None:
            self.fail("no job selected for logs")
            return
        if selects_job:
            from .ui.selection_effects import resume
            resume(self, "log")
        from .navigation_ui import record as remember
        remember(self, "log", force=self.tab == "log" and jid != self.log_job)
        from .project_ui import selected_binding, clear_binding
        binding = selected_binding(self)
        if binding and binding.get("job_id") != jid:
            clear_binding(self)
        if jid != self.log_job:
            from .job_logs.tools import before_source_change
            before_source_change(self)
            self.logs.clear_selection(reset_cursor=True)
            self.log_selection_expected = False
            self.logs.which, self.logs.file_index = "out", 0
            self.logs.path, self.logs.match, self.logs.last_bookmark = "", None, None
            self.logs.entry, self.logs.entries = None, []
            self.logs.browser, self.logs.browse_return = False, False
            self.logs.browser_cursor, self.logs.browser_top, self.logs.file_filter = 0, 0, ""
        self.log_job, self.log_record, self.logs.top = jid, record, None
        self.enter_tab("log")
        from .ui.selection_state import selected
        self.selected_id = selected(self, "log", jid)
        if self.sampler:
            self.sampler.select(jid)

    def log_entries(self):
        flt = self.logs.file_filter.casefold()
        from .job_logs.workbench import visible_entries
        entries = [entry for entry in self.logs.entries if not flt or
                   any(flt in str(entry.get(key, "")).casefold() for key in ("label", "group", "path", "description"))]
        return visible_entries(self, entries)

    def select_log_file(self):
        entries = self.log_entries()
        if not entries:
            self.say("no log file selected")
            return False
        from .ui.selection_effects import resume_lines
        resume_lines(self)
        from .navigation_ui import record
        record(self, "log", force=True)
        self.logs.browser_cursor = max(0, min(self.logs.browser_cursor, len(entries) - 1))
        entry = entries[self.logs.browser_cursor]
        if not self.logs.entry or self.logs.entry["path"] != entry["path"]:
            from .job_logs.tools import before_source_change
            before_source_change(self)
            self.logs.clear_selection(reset_cursor=True)
            self.log_selection_expected = False
            self.logs.path, self.logs.top, self.logs.match, self.logs.last_bookmark = "", None, None, None
        self.logs.entry = dict(entry)
        self.logs.file_index, self.logs.browser, self.logs.browse_return = 0, False, True
        return True

    def target_jobs(self) -> List[Job]:
        """The marked jobs when there are any (visible ones first), else the selected job."""
        if self.marks:
            jobs = [self.store.job(i) for i in self.visible_ids if i in self.marks]
            jobs += [self.store.job(i) for i in sorted(self.marks) if i not in self.visible_ids]
            return [j for j in jobs if j]
        j = self.selected_job()
        return [j] if j else []

    def target_ids(self) -> List[str]:
        """Marked ids, else the selected one (on the History tab the row under the cursor)."""
        if self.marks:
            return sorted(self.marks)
        from .ui.selection_state import cleared
        if cleared(self):
            return []
        if self.tab == "history":
            from .ui.selection_state import selected
            if selected(self, "history", True) is None:
                return []
            ids = [f.id for f in self.history_jobs()]
            return [ids[self.clamp_cursor("history", len(ids))]] if ids else []
        return [self.selected_id] if self.selected_id else []

    def pin_toggle(self, ids: List[str]):
        if not ids:
            self.fail("no job selected or marked")
            return
        states = [self.store.pin(i) for i in ids]
        self.store.event("tag", f"{'pinned' if states[0] else 'unpinned'} {' '.join(ids)}")
        self.say(f"{'pinned' if states[0] else 'unpinned'} {', '.join(ids[:4])}{' ...' if len(ids) > 4 else ''}")

    def chain_ids(self, jid: str) -> List[str]:
        """The job and everything that waits for it, nearest first."""
        from .deps import DepGraph
        return [jid] + DepGraph(self.store.jobs).downstream(jid, include_inferred=False)

    # ---- key handling ------------------------------------------------------------------------------
    def sync_selection(self):
        """The selected job follows the cursor even between two renders (keys can arrive faster than frames)."""
        if self.tab == "jobs":
            if self.views_ref is not None and self.jobs_selection_options != self.jobs_options():
                from .table_tools import snapshot
                snap = snapshot(self, self.store.render_snapshot())
                self.visible_ids = [r["id"] for r in self.views_ref.job_rows(snap, self, self.actions)]
                self.recent_ids = [f.id for f in self.recent_jobs(snap)]
                self.jobs_selection_options = self.jobs_options()
            ids = self.visible_ids + self.recent_ids
            from .ui.selection_state import selected
            self.selected_id = selected(self, "jobs", ids[self.clamp_cursor("jobs", len(ids))] if ids else None)
        elif self.tab == "history":
            self.sync_history_selection()
        elif self.tab in ("log", "analytics", "research"):
            from .ui.selection_state import selected
            field = {"log": "log_job", "analytics": "analytics_job", "research": "research_job_id"}[self.tab]
            self.selected_id = selected(self, self.tab, getattr(self, field, None))
        elif self.tab == "deps":
            browser = getattr(self, "history_browser_state", {})
            view = browser.get("views", {}).get("deps", {})
            scoped = view.get("selected") if view.get("explicit") else None
            if scoped and self.selected_id == scoped:
                return
            cur = self.clamp_cursor("deps", len(self.dep_ids))
            from .ui.selection_state import selected
            self.selected_id = selected(self, "deps", self.dep_ids[cur] if self.dep_ids else None)
        elif self.tab == "group":
            self.selected_id = self.group_selected()

    def handle(self, key: str) -> None:
        """Route a key and release dialog scroll overrides when its owner closes."""
        previous = self.mode
        try:
            self._handle(key)
        finally:
            if self.mode != previous:
                from .scrollbars import resume, cancel
                resume(self)
                cancel(self)

    def _handle(self, key: str) -> None:
        """``key`` is a name: a-z A-Z 0-9 punctuation, or up down pgup pgdn home end tab btab enter esc space backspace."""
        if workbench.route_modal_key(self, key):
            return
        binding_test = self.mode == "bindings_editor" and self.navigation_tools_state.get("test")
        if key == "ctrl-c" and self.mode != "terminal_probe" and not binding_test and key not in self.keymap:
            self.quit = True
            return
        from .startup import handle_key as startup_key
        startup_key(self, key)
        self.sync_selection()
        toolbar_state = getattr(self, "toolbar_state", {})
        if key == "f10" or toolbar_state.get("menu") is not None or toolbar_state.get("panel") or toolbar_state.get("focus") == "rate":
            from .toolbar import handle_key as toolbar_key
            if toolbar_key(self, key):
                return
        palette_contexts = {"analysis", "session_alerts", "terminal_diagnostics", "log_tools_page", "log_tools_results", "log_tools_marks",
                            "project_preview", "export_preview", "locations_picker", "value_peek", "field_explanation", "layout", "columns"}
        list_context = (self.mode == "session_inbox" and not self.session_tools_state.get("filtering")
                        or self.mode in ("activity", "exports") and not self.activity.filtering
                        or self.mode == "table_tools" and self.table_tools_state.get("edit") is None)
        if key == ":" and (self.mode in palette_contexts or list_context):
            from .command_ui import open_palette
            open_palette(self)
            return
        from .text_selection import handle_key as text_key
        if text_key(self, key):
            return
        from .manual_group_ui import handle_key as manual_group_key
        from .ui.selection_state import context as job_list_context
        grouping_key_blocked = key == "u" and job_list_context(self) is None
        if manual_group_key(self, key):
            return
        if (key in ("up", "down", "pgup", "pgdn", "home", "end")
                and not getattr(self, "interaction_state", {}).get("active")
                and not (self.mode == "main" and self.tab == "analytics" and self.analytics_view != "job")):
            from .scrollbars import resume as resume_scroll
            resume_scroll(self)
        if workbench.handle_key(self, key, modal=False):
            return
        if key == ":" and self.mode not in ("main", "filter", "palette", "confirm"):
            from .command_ui import open_palette
            open_palette(self)
            return
        if self.mode == "confirm":
            self.finish_confirm(key in ("y", "Y"))
            return
        if self.mode in ("help", "details"):
            if key in ("esc", "q", "enter") or self.keymap.get(key) in ("help", "details", "quit"):
                self.mode = "main"
            elif self.keymap.get(key) == "down":
                self.scroll += 1
            elif self.keymap.get(key) == "up":
                self.scroll = max(0, self.scroll - 1)
            return
        if self.mode == "filter":
            if key == "enter":
                if self.tab == "log" and self.logs.browser:
                    self.logs.file_filter, self.mode = self.filter_edit, "main"
                    self.logs.browser_cursor, self.logs.browser_top = 0, 0
                    return
                if self.tab == "log":                      # on the Log tab the prompt is the search
                    self.logs.search, self.mode = self.filter_edit, "main"
                    self.logs.match = None
                    buf = self.read_log_buffer(self.logs.path) if self.logs.path else None
                    if self.logs.search:
                        self.find_log_match(buf, backwards=True)
                    self.say(self.log_search_message(buf))
                    return
                self.filter, self.mode = self.filter_edit, "main"
                self.cursor[self.tab] = 0
            elif key == "esc":
                if self.tab == "log" and self.logs.browser:
                    self.logs.file_filter = ""
                elif self.tab == "log":
                    self.logs.search, self.logs.match = "", None
                else:
                    self.filter = ""
                self.filter_edit, self.mode = "", "main"
            elif key == "backspace":
                self.filter_edit = self.filter_edit[:-1]
            elif len(key) == 1:
                self.filter_edit += key
            elif key == "space":
                self.filter_edit += " "
            return
        if self.mode == "palette":
            if key == "enter":
                line, self.palette_edit, self.mode = self.palette_edit, "", "main"
                self.run_command(line)
            elif key == "esc":
                self.palette_edit, self.mode = "", "main"
            elif key == "backspace":
                self.palette_edit = self.palette_edit[:-1]
            elif key == "tab":
                self.palette_edit = self.palette_complete(self.palette_edit)
            elif len(key) == 1:
                self.palette_edit += key
            elif key == "space":
                self.palette_edit += " "
            return
        action = self.keymap.get(key)
        if action is None:
            return
        if self.tab == "log" and self.logs.browser and action in ("follow", "find_next", "find_prev", "bookmark", "bookmark_next"):
            self.say("Open a log with Enter before using file search, follow, or bookmarks")
            return
        if self.sel_anchor is not None and action in ("up", "down", "page_up", "page_down", "home", "end"):
            n = len(self.last_rows) or 1
            from .table_tools import page_size
            page = page_size(self, self.tab)
            step = {"up": -1, "down": 1, "page_up": -page, "page_down": page, "home": -n, "end": n}[action]
            self.sel_end = max(0, min(n - 1, self.sel_end + step))
            return
        n = len(self.visible_ids) if self.tab == "jobs" else None
        if action == "quit":
            if self.filter:
                self.filter = ""
                self.say("filter cleared")
            else:
                self.quit = True
        elif action == "clear":
            if self.tab == "log" and (self.logs.selection_active or self.log_selection_expected):
                self.logs.clear_selection()
                self.log_selection_expected = False
                self.say("selection cancelled")
            elif self.sel_anchor is not None:
                self.sel_anchor = None; self.say("selection cancelled")
            elif self.tab == "log" and (self.logs.browser or self.logs.browse_return):
                self.logs.browser = not self.logs.browser
            elif self.filter:
                self.filter = ""; self.say("filter cleared")
            elif self.marks:
                self.marks.clear()
                self.job_selection_state["mark_tokens"].clear()
                self.say("marks cleared")
        elif action == "help":
            self.mode, self.scroll = "help", 0
        elif action == "refresh":
            self.logs.invalidate_remote()
            if self.sampler:
                self.sampler.refresh_all()
            if self.research:
                self.research.configure()
            if self.logs.catalog:
                self.logs.catalog.configure(files=self.logs.files)
            self.say("sampling every source now")
        elif action in ("up", "down", "page_up", "page_down", "home", "end"):
            self.move(action)
            self.sync_selection()
        elif action == "next_tab":
            self.switch_tab(+1)
        elif action == "prev_tab":
            self.switch_tab(-1)
        elif action.startswith("tab_"):
            self.enter_tab(action[4:])
        elif action in ("view_prev", "view_next"):
            if self.tab == "analytics":
                keys = [k for k, _ in ANALYTICS_VIEWS]
                self.analytics_view = keys[(keys.index(self.analytics_view) + (1 if action == "view_next" else -1)) % len(keys)]
            elif self.tab == "nodes":
                keys = [k for k, _ in NODES_VIEWS]
                self.nodes_view = keys[(keys.index(self.nodes_view) + (1 if action == "view_next" else -1)) % len(keys)]
            elif self.tab == "research":
                keys = [k for k, _ in RESEARCH_VIEWS]
                self.research_view = keys[(keys.index(self.research_view) + (1 if action == "view_next" else -1)) % len(keys)]
                self.research_scroll = 0
        elif action == "steps":
            self.open_details()
        elif action in ("days_more", "days_less"):
            self.set_days(self.days_index + (1 if action == "days_more" else -1))
        elif action == "visual":
            if self.tab == "log" and not self.logs.browser:
                from .ui.selection_effects import resume_lines
                resume_lines(self)
                self.log_selection_expected = self.logs.begin_selection(self.prepare_log())
            else:
                self.start_selection(self.cursor_row())
        elif action == "visual_all":
            if self.tab == "log" and not self.logs.browser:
                from .ui.selection_effects import resume_lines
                resume_lines(self)
                self.log_selection_expected = self.logs.select_all(self.prepare_log())
            else:
                self.sel_anchor, self.sel_end = 0, max(0, len(self.last_rows) - 1)
        elif action == "yank":
            self.yank()
        elif action == "copy_all":
            self.copy_all_log() if self.tab == "log" else self.copy_visible_pane()
        elif action == "export_text":
            self.export("text")
        elif action == "export_csv":
            self.export("csv")
        elif action == "export_json":
            self.export("json")
        elif action == "palette":
            from .command_ui import open_palette
            open_palette(self)
        elif action == "theme":
            self.set_theme(THEMES[(THEMES.index(self.theme) + 1) % len(THEMES)])
        elif action == "mark":
            if self.tab in ("jobs", "deps") and self.selected_job():
                if self.selected_id in self.marks:
                    self.marks.discard(self.selected_id)
                else:
                    self.marks.add(self.selected_id)
                self.move("down")
                self.sync_selection()
        elif action == "pin":
            self.pin_toggle(self.target_ids())
        elif action == "resubmit":
            ids = self.target_ids()
            if ids:
                from .command_ui import open_palette
                open_palette(self, f"resubmit {ids[0]} ")
            else:
                self.say("no job selected")
        elif action == "mark_all":
            if self.tab == "jobs":
                self.marks.update(self.visible_ids)
                from .ui.selection_state import _bind_marks
                _bind_marks(self, self.visible_ids)
                self.say(f"{len(self.marks)} marked")
        elif action == "unmark_all":
            if key == "u":
                if grouping_key_blocked or job_list_context(self) is None:
                    self.say("Focus a job list to ungroup; U clears all job marks")
                    return
            self.marks.clear()
            self.job_selection_state["mark_tokens"].clear()
        elif action == "details":
            if self.tab == "log" and self.logs.browser:
                self.select_log_file()
            elif self.tab == "research" and self.research_view == "arrays":
                from .ui.selection_state import cleared
                if cleared(self, "research"):
                    self.say("Select an array cohort before opening its tasks")
                    return
                self.research_array_open = not self.research_array_open
                self.research_task_offset = 0
            elif self.tab in ("jobs", "group", "deps") and self.selected_id:
                self.open_details()
            elif self.tab == "history":
                ids = self.target_ids()
                if ids:
                    self.analytics_job, self.analytics_view = ids[0], "job"
                    self.enter_tab("analytics")
        elif action in ("cancel", "hold", "requeue", "top"):
            self.start_confirm(action)
        elif action == "log":
            self.open_log()
        elif action == "less":
            if self.tab == "log" and self.logs.browser:
                if not self.select_log_file():
                    return
            self.want_less = True                       # the screen opens less on the selected job
        elif action == "log_files":
            if self.tab != "log":
                self.open_log()
            if self.tab == "log":
                self.logs.clear_selection(reset_cursor=True)
                self.log_selection_expected = False
                self.sel_anchor = None
                self.logs.browser, self.logs.browse_return = True, True
        elif action == "follow":
            buf = self.prepare_log()
            if self.logs.following:
                self.logs.top = buf.clamp_top(max(0, buf.total - self.logs.page), self.logs.page) if buf else 0
                if self.logs.top is None:
                    self.logs.top = 0
                self.say("paused: arrows and PgUp/PgDn scroll, End or f follows again")
            else:
                self.logs.clear_selection(reset_cursor=True)
                self.log_selection_expected = False
                self.logs.top = None
                self.say("following")
        elif action in ("find_next", "find_prev"):
            if self.tab == "log":
                buf = self.read_log_buffer(self.logs.path) if self.logs.path else None
                i = self.find_log_match(buf, backwards=(action == "find_prev"))
                self.say(f"match at line {i + 1}" if i is not None else
                         "Search index is updating; try N or P when indexing finishes" if self.log_tools_state.get("retained_pending") else
                         f"no match for '{self.logs.search}'" if self.logs.search else "no search: / sets one")
        elif action == "wrap":
            self.logs.wrap = not self.logs.wrap
            self.say(f"long lines {'wrapped' if self.logs.wrap else 'cut'}")
        elif action == "stderr":
            if self.tab == "log":
                self.logs.clear_selection(reset_cursor=True)
                self.log_selection_expected = False
                self.logs.entry, self.logs.browser = None, False
                from .job_logs.tools import before_source_change
                before_source_change(self)
                self.logs.which = "err" if self.logs.which == "out" else "out"
                self.logs.file_index, self.logs.top, self.logs.match = 0, None, None
                self.say(f"showing {'stderr' if self.logs.which == 'err' else 'stdout'}")
        elif action == "log_file":
            if self.tab == "log":
                from .project_ui import selected_binding, cycle_log_entry
                if selected_binding(self):
                    entry = cycle_log_entry(self)
                    self.say("Showing " + entry.get("label", entry["path"]) if entry else "This run has no declared log files")
                    return
                jid = self.log_job or self.selected_id
                if self.views_ref:
                    snap = self.store.render_snapshot()
                    job = self.log_target(snap)
                    if job:
                        self.views_ref.log_candidates(self, job, snap["details"].get(jid, {}))
                cands = self.logs.candidates.get(jid, (0, []))[1] if jid else []
                if not cands:
                    self.say("no other files of this job in its log directory (array tasks, steps, gpu-util-<id>.csv)")
                else:
                    self.logs.clear_selection(reset_cursor=True)
                    self.log_selection_expected = False
                    self.logs.entry, self.logs.browser = None, False
                    self.logs.file_index = (self.logs.file_index + 1) % (len(cands) + 1)
                    self.logs.top, self.logs.match = None, None
                    self.say("stdout" if self.logs.file_index == 0 else f"file {self.logs.file_index + 1}/{len(cands) + 1}: {cands[self.logs.file_index - 1]}")
        elif action == "bookmark":
            if self.tab == "log" and self.logs.path:
                buf = self.read_log_buffer(self.logs.path)
                i = self.logs.current_line(buf)
                if i is not None:
                    self.logs.last_bookmark = None
                    on = self.logs.toggle_bookmark(self.logs.path, i)
                    self.say(f"bookmark {'set' if on else 'removed'} at line {i + 1}")
        elif action == "bookmark_next":
            if self.tab == "log" and self.logs.path:
                buf = self.read_log_buffer(self.logs.path)
                i = self.logs.next_bookmark(self.logs.path, self.logs.current_line(buf))
                if i is None:
                    self.say("no bookmarks in this file (m sets one)")
                else:
                    self.logs.match, self.logs.last_bookmark = None, i
                    self.logs.goto(i, buf)
                    self.logs.cursor = i
                    self.logs.top = buf.clamp_top(i, self.logs.page) if buf else 0   # the bookmark at the top of the page
                    if self.logs.top is None:
                        self.logs.top = 0
                    self.say(f"bookmark at line {i + 1}")
        elif action.startswith("replay_"):
            self.replay_control(action[7:])
        elif action == "sort":
            from .table_sort import reset_sort
            reset_sort(self, self.tab)
            opts = SORTS.get(self.tab, ["name"])
            cur = self.sort.get(self.tab, opts[0])
            self.sort[self.tab] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
            self.table_sort_changed(self.tab, persist=False)
            self.say(f"sorted by {self.sort[self.tab]}")
        elif action == "reverse":
            from .table_sort import reset_sort
            reset_sort(self, self.tab)
            self.reverse[self.tab] = not self.reverse.get(self.tab, False)
            self.table_sort_changed(self.tab, persist=False)
        elif action == "filter":
            self.mode, self.filter_edit = "filter", (self.logs.file_filter if self.tab == "log" and self.logs.browser else self.logs.search if self.tab == "log" else self.filter)
        elif action == "gpu_toggle":
            self.gpu = not self.gpu
            self.say(f"GPU sampling {'on' if self.gpu else 'off'}")
        elif action == "bell_toggle":
            self.bell = not self.bell
            self.say(f"bell {'on' if self.bell else 'off'}")
        elif action == "log_lines":
            self.log_lines = min(60, self.log_lines + 4)
        elif action == "log_lines_less":
            self.log_lines = max(0, self.log_lines - 4)
        elif action == "source_toggle":
            if self.tab == "sources":
                names = self.ordered_source_ids()
                cur = self.clamp_cursor("sources", len(names))
                if names:
                    h = self.store.health[names[cur]]
                    h.enabled = not h.enabled
                    h.error = ""
                    self.say(f"{h.name} {'enabled' if h.enabled else 'disabled'}")
        # Cursor movement is session-local. Shared/home filesystem writes on
        # every arrow or wheel tick can stall CARC's terminal input loop.
        if action not in ("up", "down", "page_up", "page_down", "home", "end"):
            self.save()

    def open_details(self):
        """The details overlay of the selected job: scontrol and the steps of a running job, sacct -j of a finished one."""
        from .ui.selection_state import cleared
        if cleared(self):
            self.say("Select a job before opening its inspector")
            return
        if self.tab == "history":
            ids = [f.id for f in self.history_jobs()]
            if not ids:
                return
            jid = ids[self.clamp_cursor("history", len(ids))]
        elif self.tab == "group":
            jid = self.group_selected()
        else:
            jid = self.selected_id
        if jid:
            self.detail_id, self.mode, self.scroll = jid, "details", 0
            if self.sampler:
                self.sampler.select(jid)
                if not self.store.job(jid):
                    self.sampler.select_fin(jid)

    def group_selected(self) -> Optional[str]:
        ids = getattr(self, "group_ids", [])
        from .ui.selection_state import selected
        return selected(self, "group", ids[self.clamp_cursor("group", len(ids))] if ids else None)

    def handle_action(self, action: str):
        from .historical_ui import active as historical_active
        if historical_active(self):
            self.fail("Historical observations are read-only; return Live before changing the session")
            return
        """Run one key action by name (what the palette's wrap / bookmark commands do)."""
        key = next((k for k, a in self.keymap.items() if a == action), None)
        if key:
            self.handle(key)

    def move(self, action: str):
        from .scrollbars import resume as resume_scroll
        if not (self.tab == "analytics" and self.analytics_view != "job"):
            resume_scroll(self)
        from .ui.selection_effects import resume
        self.click_row = None
        scoped = getattr(self, "history_browser_state", {}).get("views", {}).get("deps", {})
        if self.tab == "deps" and scoped.get("explicit"):
            page = max(1, getattr(self, "deps_scope_page", 1))
            count = getattr(self, "deps_scope_count", 0)
            top = getattr(self, "deps_scope_top", 0)
            limit = max(0, count - page)
            self.deps_scope_top = max(0, min(limit, {"up": top - 1, "down": top + 1,
                "page_up": top - page, "page_down": top + page, "home": 0, "end": limit}.get(action, top)))
            return
        if self.tab == "research":
            if action in ("page_up", "page_down", "home", "end"):
                from .ui.selection_state import cleared
                if (self.research_view == "arrays" and self.research_array_open
                        and not cleared(self, "research") and action in ("page_up", "page_down")):
                    self.research_task_offset = max(0, self.research_task_offset + (-24 if action == "page_up" else 24))
                else:
                    self.research_scroll = max(0, {"page_up": self.research_scroll - 12, "page_down": self.research_scroll + 12,
                                                    "home": 0, "end": max(0, self.research_rows - 1)}[action])
                return
            delta = -1 if action == "up" else 1
            if self.research_view in ("experiment", "evidence", "predict", "forecast", "blockers", "tradeoffs"):
                if self.research_data_jobs and self.research_view in ("predict", "forecast", "blockers"):
                    ids = list(dict.fromkeys(r.get("id") or r.get("job_id") for r in self.research_data_jobs if r.get("id") or r.get("job_id")))
                else:
                    ids = list(dict.fromkeys(j.id for j in self.store.jobs + self.store.finished))
                cur = ids.index(self.research_job_id) if self.research_job_id in ids else 0
                if ids:
                    from .ui.research_binding import select_job
                    select_job(self, ids[max(0, min(len(ids) - 1, cur + delta))])
                self.research_scroll = 0
            elif self.research_view == "arrays":
                resume(self)
                self.cursor["research"] = max(0, min(len(self.research_groups) - 1, self.cursor.get("research", 0) + delta))
                self.research_task_offset = 0
                self.research_array_focus = True
            else:
                self.research_scroll = max(0, self.research_scroll + delta)
            return
        if self.tab == "analytics":
            if self.analytics_view != "job":
                from .scrollbars import keyboard_scroll
                keyboard_scroll(self, "analytics:document:" + self.analytics_view, action)
                return
            if self.analytics_view == "job":
                ids = [j.id for j in self.store.jobs if not j.pending]
                seen = set(ids)
                series_jobs = self.store.series_jobs_view if self.interactive else self.store.series_jobs
                for i in series_jobs():
                    if i not in seen:
                        ids.append(i)
                        seen.add(i)
                if ids:
                    resume(self)
                    cur = ids.index(self.analytics_job) if self.analytics_job in ids else 0
                    step = {"up": -1, "down": 1, "page_up": -5, "page_down": 5, "home": -len(ids), "end": len(ids)}[action]
                    self.analytics_job = ids[max(0, min(len(ids) - 1, cur + step))]
            return
        if self.tab == "log":
            from .ui.selection_effects import resume_lines
            resume_lines(self)
            if self.logs.browser:
                n = len(self.log_entries())
                cur = self.logs.browser_cursor
                step = max(1, self.logs.browser_page - 1)
                self.logs.browser_cursor = max(0, min(max(0, n - 1), {"up": cur - 1, "down": cur + 1,
                    "page_up": cur - step, "page_down": cur + step, "home": 0, "end": n - 1}[action]))
                return
            self.logs.move_cursor(action, self.prepare_log())
            return
        tab = self.tab
        if tab in ("jobs", "history", "group", "deps"):
            resume(self)
        elif tab == "sources":
            from .ui.selection_effects import resume_lines
            resume_lines(self)
        n = len(self.history_jobs()) if tab == "history" else {"jobs": len(self.visible_ids) + len(self.recent_ids),
             "sources": len(self.ordered_source_ids()), "group": len(getattr(self, "group_ids", [])), "deps": len(self.dep_ids)}.get(tab, 0)
        from .table_tools import page_size
        page = page_size(self, tab)
        cur = self.cursor.get(tab, 0)
        cur = {"up": cur - 1, "down": cur + 1, "page_up": cur - page, "page_down": cur + page, "home": 0, "end": 10 ** 9}[action]
        self.cursor[tab] = max(0, min(cur, max(0, n - 1)))

    def switch_tab(self, d: int):
        keys = [t for t, _ in TABS]
        self.enter_tab(keys[(keys.index(self.tab) + d) % len(keys)])

    def enter_tab(self, name: str):
        """Switch tabs; entering Analytics from Jobs or History carries the job under the cursor along."""
        from .ui.text_selection_state import clear as clear_text
        clear_text(self)
        from .navigation_ui import record
        record(self, name)
        if name == "analytics" and self.tab == "jobs" and self.selected_id:
            self.analytics_job = self.selected_id
        elif name == "analytics" and self.tab == "history" and self.selected_id:
            ids = [f.id for f in self.history_jobs()]
            if ids:
                self.analytics_job = ids[self.clamp_cursor("history", len(ids))]
        if name == "research" and self.tab in ("jobs", "history"):
            ids = self.target_ids()
            chosen = ids[0] if ids else self.selected_id
            from .project_ui import selected_binding, clear_binding
            binding = selected_binding(self)
            if binding and chosen and binding.get("job_id") != chosen:
                clear_binding(self)
            if chosen:
                self.research_job_id = chosen
        self.tab = name
        self.sel_anchor = None
        self.click_row = None
        self.logs.clear_selection()
        self.log_selection_expected = False
        if name == "history":
            self.completion.acknowledge()

    def set_days(self, index: int):
        index = max(0, min(len(self.days_options) - 1, index))
        if index == self.days_index:
            return
        self.days_index = index
        days = self.analytics_days_value()
        if self.sampler:
            self.sampler.history_days = days
            self.sampler.last_run["finished"] = 0.0
            self.sampler.kick.set()
        self.say(f"history window {days:g} day{'s' if days != 1 else ''} (sacct refreshes)")

    # ---- selection, copy, export -------------------------------------------------------------------
    def cursor_row(self) -> int:
        """The screen row of the selected job (jobs tab) or of the last click, else the first body row."""
        for (y, kind, key) in getattr(self, "last_hits", []):
            if kind in ("job", "recent", "fin") and key == self.selected_id:
                return y
        tab_kind = {"sources": "source", "group": "group", "deps": "dep", "research": "research_array"}.get(self.tab)
        matches = [y for y, kind, _ in self.last_hits if kind == tab_kind]
        if matches:
            return matches[max(0, min(len(matches) - 1, self.cursor.get(self.tab, 0) - self.top.get(self.tab, 0)))]
        if self.click_row is not None:
            return self.click_row
        return min(4, max(0, len(self.last_rows) - 1))

    def start_selection(self, row: int):
        n = len(self.last_rows)
        if n == 0:
            return
        from .ui.selection_effects import resume_lines
        resume_lines(self)
        self.sel_anchor = self.sel_end = max(0, min(n - 1, row))

    def extend_selection(self, row: int):
        if self.sel_anchor is None:
            self.sel_anchor = self.click_row if self.click_row is not None else row
        self.sel_end = max(0, min(len(self.last_rows) - 1, row))

    def selection_text(self) -> str:
        if self.sel_anchor is None:
            return ""
        return export.selection_text(self.last_rows, self.sel_anchor, self.sel_end)

    def yank(self):
        from .text_selection import copy_selection
        if copy_selection(self):
            return
        from raven.jobs.copy import copy_selection as copy_jobs
        if copy_jobs(self):
            return
        from .row_copy import copy_selection as copy_row
        if copy_row(self):
            return
        if self.tab == "log" and not self.logs.browser:
            expected = self.log_selection_expected or self.logs.selection_active
            buf = self.prepare_log()
            if expected and not self.logs.selection_active:
                self.log_selection_expected = False
                self.fail("Log changed or the selected lines left the buffer; select the lines again (Y copies the entire file)")
                return
            if self.logs.selection_all or not self.logs.selection_active:
                self.copy_all_log()
                return
            lo, hi = sorted((self.logs.selection_anchor, self.logs.selection_end))
            size = sum(buf._line_bytes[lo:min(hi + 1, len(buf._line_bytes))])
            if hi >= len(buf.lines):
                size += len(buf._partial_raw)
            if size > 256 << 10 or hi - lo + 1 > 4096:
                self.copy_selected_log(buf)
                return
            raw = self.logs.selection_bytes(buf)
            try:
                text = raw.decode("utf-8")
            except UnicodeError:
                self.copy_selected_log(buf)
                return
        elif self.tab == "log" and self.logs.browser:
            self.fail("Open a log with Enter before copying its contents")
            return
        else:
            text = self.selection_text()
        if not text:
            self.fail("Select a row or text lines before copying; Y copies the focused pane")
            return
        msg = clipboard.copy_for_app(self, text, self.state_dir, **clipboard.options(self))
        self.store.event("copy", msg)
        self.say(msg)
        self.sel_anchor = None
        if self.tab == "log":
            self.logs.clear_selection()
            self.log_selection_expected = False

    def copy_visible_pane(self):
        from .text_selection import copy_visible_pane
        if copy_visible_pane(self):
            return
        self.fail("No current pane is available to copy; wait for the display to refresh")

    def copy_all_log(self):
        path = self.resolve_log_path()
        if not path:
            self.fail("Open a log with Enter before copying its contents" if self.logs.browser else "no log file selected")
            return
        from .job_logs.copy import copy_full_log
        files = self.logs.files
        cb = dict(self.cfg["clipboard"])
        self.start_log_copy(lambda service: copy_full_log(path, self.state_dir, files=files,
            use_osc52=bool(cb.get("osc52", True)), use_tools=bool(cb.get("tools", True)),
            destination=cb.get("destination", "copy"),
            cancel=lambda: service.closed or self.copy_task["cancel"].is_set(),
            progress=lambda done, total: self.activity.progress(self.copy_task, done, total)), path, f"Copying entire log: {path}")

    def copy_selected_log(self, buf):
        """Pin immutable retained line bytes; large/invalid text never blocks the UI."""
        lo, hi = sorted((self.logs.selection_anchor, self.logs.selection_end))
        complete = tuple(buf.raw_lines[lo:min(hi + 1, len(buf.raw_lines))])
        partial = buf._partial_raw if hi >= len(buf.raw_lines) else None
        path = self.logs.path
        def chunks():
            for line in complete:
                yield line
                yield b"\n"
            if partial is not None:
                yield partial
        from .job_logs.copy import copy_log_selection
        cb = dict(self.cfg["clipboard"])
        self.start_log_copy(lambda service: copy_log_selection(chunks(), self.state_dir, source_path=path,
            use_osc52=bool(cb.get("osc52", True)), use_tools=bool(cb.get("tools", True)),
            destination=cb.get("destination", "copy"),
            cancel=lambda: service.closed or self.copy_task["cancel"].is_set(),
            progress=lambda done, total: self.activity.progress(self.copy_task, done, total)), path, f"Copying log lines {lo + 1}-{hi + 1}: {path}")

    def start_log_copy(self, task, path, message):
        if self.research is None:
            from .research import ResearchHub
            self.research = ResearchHub(self.cfg, self.logs.files)
        service = self.research
        selected_range = (self.logs.selection_path, self.logs.selection_anchor, self.logs.selection_end, self.logs.selection_all)
        selected_token = self.logs._buffer_token, self.logs.selection_generation
        def source_context():
            record = self.log_record
            project = getattr(self, "project_state", {})
            project = project if isinstance(project, dict) else {}
            binding = project.get("binding")
            binding = binding if isinstance(binding, dict) else {}
            return (self.logs.path, id(self.logs.files), self.log_job,
                    getattr(record, "id", None), getattr(record, "start", None), getattr(record, "end", None),
                    project.get("root"), *(binding.get(key) for key in
                        ("run_id", "attempt", "project_root", "run_root", "path", "workdir")))
        source = source_context()
        # A completed read may still await publication between frames. Polling
        # never waits, and its publication must not turn pinned old lines into
        # a copy of a newly selected source or invalidated range.
        service.poll_task()
        if getattr(service, "closed", False) or getattr(service, "pending", None):
            self.fail("A background command is still running; retry copy when it finishes")
            return
        current_range = (self.logs.selection_path, self.logs.selection_anchor, self.logs.selection_end, self.logs.selection_all)
        current_token = self.logs._buffer_token, self.logs.selection_generation
        if source_context() != source or (selected_range[0] is not None and
                (current_range != selected_range or current_token != selected_token)):
            self.fail("Log source or selected lines changed; select the source and lines again before copying")
            return
        selected_range = (self.logs.selection_path, self.logs.selection_anchor, self.logs.selection_end, self.logs.selection_all)
        selected_token = self.logs._buffer_token, self.logs.selection_generation
        copy_task = self.activity.start(message, path)
        self.copy_task = copy_task
        def finished(result):
            self.activity.finish(copy_task, "error" if isinstance(result, Exception) else result.get("status", "error"))
            if isinstance(result, Exception):
                self.fail(f"Log copy failed: {result}")
                return
            message = result.get("message", "Log copy finished")
            if result.get("status") not in ("ready", "partial"):
                self.fail(message)
                return
            self.store.event("copy", message)
            self.say(message, level="warning" if result.get("status") == "partial" else "success", path=result.get("export_path", ""))
            current_range = (self.logs.selection_path, self.logs.selection_anchor, self.logs.selection_end, self.logs.selection_all)
            current_token = self.logs._buffer_token, self.logs.selection_generation
            if self.logs.path == path and current_range == selected_range and current_token == selected_token:
                self.logs.clear_selection()
                self.log_selection_expected = False
        accepted = service.start_task(lambda: task(service), finished)
        if accepted:
            self.say(message)
        else:
            self.activity.finish(copy_task, "not started")
            self.fail("A background command is still running; retry copy when it finishes")

    def export(self, kind: str):
        if not self.views_ref:
            self.fail("export needs the screen")
            return
        if kind == "report" and self.interactive:
            self.export_report_background()
            return
        snap = self.store.render_snapshot()
        name = f"{kind}-{self.tab}-{export.stamp()}"
        try:
            if kind == "report":
                from . import report
                path = export.write(self.state_dir, f"report-{export.stamp()}.txt", report.build(snap, self, self.views_ref, self.actions))
            elif kind == "text":
                path = export.write(self.state_dir, name + ".txt", export.tab_text(self.views_ref, snap, self, self.actions, max(120, self.width)))
            elif kind == "csv":
                content = export.tab_csv(self.views_ref, snap, self, self.actions)
                if content is None:
                    self.fail(f"no table to export on the {self.tab} tab (use text)")
                    return
                path = export.write(self.state_dir, name + ".csv", content)
            else:
                ids = sorted(self.marks) or ([self.analytics_job] if self.tab == "analytics" and self.analytics_job else ([self.selected_id] if self.selected_id else []))
                if self.tab == "history" and not self.marks:
                    fins = [f.id for f in self.history_jobs()]
                    if fins:
                        ids = [fins[self.clamp_cursor("history", len(fins))]]
                if not ids:
                    self.fail("no job selected or marked")
                    return
                path = export.write(self.state_dir, name + ".json", export.jobs_json(snap, ids, series_of=self.store.series_of))
        except (OSError, ValueError) as e:
            self.fail(f"export failed: {e}")
            return
        self.store.event("export", f"exported {kind} to {path}")
        self.say(f"exported {path}", level="success", path=path)

    def export_report_background(self):
        from . import report
        if self.research is None:
            from .research import ResearchHub
            self.research = ResearchHub(self.cfg, self.logs.files)
        service = self.research
        service.poll_task()
        if service.closed or service.pending:
            self.fail("A background command is still running; retry report when it finishes")
            return
        try:
            prepared = report.prepare_export(self.store.snapshot(), self, self.views_ref, self.actions)
        except (OSError, ValueError, TypeError) as exc:
            self.fail(f"report snapshot failed: {exc}")
            return
        task = self.activity.start("Exporting complete terminal report")
        state_dir, name = self.state_dir, f"report-{export.stamp()}.txt"
        def finished(value):
            if isinstance(value, Exception):
                cancelled = isinstance(value, report.ReportCancelled)
                self.activity.finish(task, "cancelled" if cancelled else "error")
                self.fail(str(value) if cancelled else f"report export failed: {value}")
                return
            self.activity.finish(task, "ready")
            self.store.event("export", f"exported report to {value}")
            self.say(f"exported {value}", level="success", path=value)
        if service.start_task(lambda: prepared.write(state_dir, name,
                cancel=lambda: service.closed or task["cancel"].is_set()), finished):
            self.say("Exporting report in the background; Ctrl-A shows progress, c cancels")
        else:
            self.activity.finish(task, "not started")
            self.fail("A background command is still running; retry report when it finishes")

    # ---- the command palette -----------------------------------------------------------------------
    COMMANDS = list(command_registry.CORE_NAMES)

    def commands(self) -> List[str]:
        return command_registry.command_names(self, core=self.COMMANDS)

    def palette_hint(self) -> str:
        word = self.palette_edit.split(" ")[0]
        cmds = self.commands()
        if not self.palette_edit:
            return "commands: " + " ".join(cmds) + "   (Tab completes, Enter runs, Esc closes)"
        if word in cmds and " " in self.palette_edit:
            return command_registry.usage(word, self)
        matches = [c for c in cmds if c.startswith(word)]
        return " ".join(matches) if matches else "unknown command"

    def palette_complete(self, text: str) -> str:
        if " " in text:
            return text
        matches = [c for c in self.commands() if c.startswith(text)]
        return matches[0] + " " if len(matches) == 1 else text

    def _ids_arg(self, args: List[str]) -> List[Job]:
        if args == ["marked"]:
            if not self.marks:
                return []
            return self.target_jobs()
        if not args:
            return self.target_jobs() if self.interactive else []
        if args == ["all"]:
            return [self.store.job(i) for i in self.visible_ids if self.store.job(i)]
        if len(args) != len(set(args)):
            return []
        out = []
        for a in args:
            j = self.store.job(a)
            if not j:
                return []
            out.append(j)
        return out

    def run_command(self, line: str):
        self.command_ok = True
        parts = line.strip().split()
        if not parts:
            self.fail("empty command")
            return
        cmd, args = parts[0], parts[1:]
        from .historical_ui import active as historical_active
        if historical_active(self) and cmd != "asof":
            self.fail("Historical observations are read-only; use asof live before other commands")
            return
        cmds = self.commands()
        full = [c for c in cmds if c.startswith(cmd)]
        if cmd not in cmds and len(full) == 1:
            cmd = full[0]
        if not command_registry.command(cmd).raw_tail:
            try:
                args = shlex.split(line.strip())[1:]
            except ValueError as exc:
                self.fail(f"command failed: {exc}")
                return
        try:
            if workbench.run_command(self, [cmd] + args):
                return
        except (ValueError, OSError, TypeError, KeyError) as exc:
            self.fail(f"{cmd} failed: {exc}")
            return
        if self.plugins and cmd in self.plugins.commands:
            fn = self.plugins.commands[cmd][0]
            try:
                msg = fn(self, args)
            except Exception as e:
                self.command_ok = False
                msg = f"{cmd} failed: {type(e).__name__}: {e}"
            if msg:
                self.say(str(msg))
            return
        from .research_commands import execute
        if execute(self, cmd, args):
            return
        if command_registry.execute_core(self, cmd, args, line):
            return
        self.fail(f"unknown command '{cmd}'")

    def _command_cancel(self, args: list[str], line: str, cmd: str) -> None:
        jobs = self._ids_arg(args)
        if not jobs or not self.actions:
            self.fail("no such job" if args else "no job selected or marked")
            return
        ok = [j for j in jobs if self.actions.applicable(cmd, j)[0]]
        if not ok:
            self.fail(self.actions.applicable(cmd, jobs[0])[1])
            return
        self.confirm, self.mode = dict(action=cmd, jobs=ok, action_scope=self._action_scope()), "confirm"

    def _command_filter(self, args: list[str], line: str, cmd: str) -> None:
        value = " ".join(args)
        if self.tab == "log":
            if self.logs.browser:
                self.logs.file_filter = value
                self.logs.browser_cursor, self.logs.browser_top = 0, 0
            else:
                self.logs.search, self.logs.match = value, None
        else:
            self.filter = value
            self.cursor[self.tab] = 0
        self.say(f"filter '{value}'" if value else "filter cleared")

    def _command_sort(self, args: list[str], line: str, cmd: str) -> None:
        opts = SORTS.get(self.tab, ["name"])
        if args and args[0] in opts:
            from .table_sort import reset_sort
            reset_sort(self, self.tab)
            self.sort[self.tab] = args[0]
            self.table_sort_changed(self.tab)
            self.say(f"sorted by {args[0]}")
        else:
            self.fail("sort keys here: " + " ".join(opts))

    def _command_days(self, args: list[str], line: str, cmd: str) -> None:
        try:
            d = float(args[0])
            if not math.isfinite(d) or d <= 0:
                raise ValueError("days must be positive and finite")
        except (IndexError, ValueError):
            self.fail("days <" + "|".join(f"{x:g}" for x in self.days_options) + ">")
            return
        if d not in self.days_options:
            self.days_options.append(d)
            self.days_options.sort()
        self.set_days(self.days_options.index(d))

    def _command_tab(self, args: list[str], line: str, cmd: str) -> None:
        if args and args[0] in dict(TABS):
            self.enter_tab(args[0])
        else:
            self.fail("tab <" + "|".join(t for t, _ in TABS) + ">")

    def _command_view(self, args: list[str], line: str, cmd: str) -> None:
        if len(args) == 1 and args[0] in dict(NODES_VIEWS):
            self.nodes_view = args[0]
            self.enter_tab("nodes")
            return
        if args and args[0] in dict(RESEARCH_VIEWS):
            self.research_view = args[0]
            self.research_scroll = 0
            self.enter_tab("research")
            return
        if args and args[0] in dict(ANALYTICS_VIEWS):
            self.analytics_view = args[0]
            self.enter_tab("analytics")
        else:
            self.fail("view <job|history|timeline>")

    def _command_copy(self, args: list[str], line: str, cmd: str) -> None:
        if args == ["all"]:
            if self.tab == "log":
                self.copy_all_log()
            else:
                self.copy_visible_pane()
        elif len(args) == 2 and all(a.isdigit() and int(a) > 0 for a in args):
            if self.tab == "log" and not self.logs.browser:
                buf = self.prepare_log()
                if buf is None or max(map(int, args)) > buf.total:
                    self.fail("copy line range is outside the loaded log")
                    return
                from .ui.selection_effects import resume_lines
                resume_lines(self)
                self.log_selection_expected = self.logs.begin_selection(buf, int(args[0]) - 1)
                self.logs.selection_end = self.logs.cursor = int(args[1]) - 1
            else:
                from .ui.text_selection_state import clear as clear_text
                clear_text(self)
                self.sel_anchor, self.sel_end = int(args[0]) - 1, int(args[1]) - 1
            self.yank()
        elif not args:
            self.yank()
        else:
            self.fail("copy [all|first last] (positive 1-based lines)")

    def _command_gpu(self, args: list[str], line: str, cmd: str) -> None:
        if args and args not in (["on"], ["off"]):
            self.fail("gpu <on|off>")
            return
        self.gpu = (args[0] == "on") if args else (not self.gpu)
        self.say(f"GPU sampling {'on' if self.gpu else 'off'}")

    def _command_bell(self, args: list[str], line: str, cmd: str) -> None:
        if args and args not in (["on"], ["off"]):
            self.fail("bell <on|off>")
            return
        self.bell = (args[0] == "on") if args else (not self.bell)
        self.say(f"bell {'on' if self.bell else 'off'}")

    def _command_source(self, args: list[str], line: str, cmd: str) -> None:
        if len(args) == 2 and args[0] in self.store.health and args[1] in ("on", "off"):
            h = self.store.health[args[0]]
            h.enabled = args[1] == "on"
            h.error = ""
            self.say(f"{h.name} {'enabled' if h.enabled else 'disabled'}")
        else:
            self.fail("source <" + "|".join(sorted(self.store.health)) + "> <on|off>")

    def _command_theme(self, args: list[str], line: str, cmd: str) -> None:
        if args and canonical_theme(" ".join(args)) in THEMES:
            self.set_theme(" ".join(args))
        else:
            self.fail("theme <" + "|".join(THEMES) + ">")

    def _command_profile(self, args: list[str], line: str, cmd: str) -> None:
        if args and args[0] in self.cfg.profiles():
            if args[0] == self.profile_name:
                self.say(f"already on profile {args[0]}")
            else:
                self.switch_profile, self.quit = args[0], True
        else:
            self.fail("profile <" + "|".join(self.cfg.profiles()) + ">" if self.cfg.profiles() else "no profiles configured ([profiles.<name>] in the config)")

    def _command_compare(self, args: list[str], line: str, cmd: str) -> None:
        if args == ["clear"]:
            self.compare_ids = []
            self.say("comparison cleared")
            return
        elif args and args != ["marked"]:
            self.compare_ids = list(args)
        else:
            self.compare_ids = sorted(self.marks)
        if not self.compare_ids:
            self.fail("compare <ids> or mark jobs first")
            return
        self.analytics_view = "compare"
        self.enter_tab("analytics")

    def _command_tag(self, args: list[str], line: str, cmd: str) -> None:
        ids = [a for a in args if self.store.job(a) or any(f.id == a for f in self.store.finished)]
        tags = [a for a in args if a not in ids]
        ids = ids or self.target_ids()
        if not ids or not tags:
            self.fail(f"{cmd} [ids] <tags...>")
            return
        for i in ids:
            self.store.tag(i, *tags, remove=(cmd == "untag"))
        self.store.event("tag", f"{cmd} {' '.join(ids)}: {' '.join(tags)}")
        self.say(f"{cmd}ged {', '.join(ids[:4])}{' ...' if len(ids) > 4 else ''}: {' '.join('#' + t.lstrip('#') for t in tags)}")

    def _command_pin(self, args: list[str], line: str, cmd: str) -> None:
        self.pin_toggle(args or self.target_ids())

    def _command_note(self, args: list[str], line: str, cmd: str) -> None:
        ids = [a for a in args[:1] if self.store.job(a) or any(f.id == a for f in self.store.finished)]
        text = " ".join(args[1:] if ids else args)
        ids = ids or self.target_ids()[:1]
        if not ids:
            self.fail("note [id] <text>")
            return
        self.store.note(ids[0], text)
        self.say(f"note on {ids[0]}: {text}" if text else f"note on {ids[0]} cleared")

    def _command_resubmit(self, args: list[str], line: str, cmd: str) -> None:
        self.start_resubmit(args)

    def _command_chain(self, args: list[str], line: str, cmd: str) -> None:
        verb = args[0] if args else ""
        if verb not in ("cancel", "hold", "release"):
            self.fail("chain <cancel|hold|release> [id]")
            return
        root = args[1] if len(args) == 2 else self.selected_id if self.interactive and len(args) == 1 else None
        if not root or not self.store.job(root):
            self.fail("chain: no such job")
            return
        jobs = [j for j in (self.store.job(i) for i in self.chain_ids(root)) if j and self.actions and self.actions.applicable(verb, j)[0]]
        if not jobs:
            self.fail(f"nothing in the chain of {root} to {verb}")
            return
        self.confirm, self.mode = dict(action=verb, jobs=jobs, action_scope=self._action_scope()), "confirm"

    def _command_refresh(self, args: list[str], line: str, cmd: str) -> None:
        if self.sampler:
            self.sampler.refresh_all()
        if self.research:
            self.research.configure()
        self.say("sampling every source now")

    def _command_mark(self, args: list[str], line: str, cmd: str) -> None:
        if args == ["all"]:
            identifiers = self.visible_ids
        else:
            identifiers = [a for a in args if self.store.job(a)]
        self.marks.update(identifiers)
        from .ui.selection_state import _bind_marks
        _bind_marks(self, identifiers)
        self.say(f"{len(self.marks)} marked")

    def _command_unmark(self, args: list[str], line: str, cmd: str) -> None:
        if args:
            self.marks.difference_update(args)
        else:
            self.marks.clear()
        from .ui.selection_state import _bind_marks
        _bind_marks(self, ())
        self.say(f"{len(self.marks)} marked")

    def _command_log(self, args: list[str], line: str, cmd: str) -> None:
        if len(args) == 1 and self.job_record(args[0]):
            self.open_log(args[0])
        else:
            self.fail("log <id>")

    def _command_find(self, args: list[str], line: str, cmd: str) -> None:
        if self.tab != "log":
            self.open_log()
            if self.tab != "log":
                return
        if self.logs.browser:
            self.fail("Open a log with Enter before searching its contents")
            return
        self.logs.search, self.logs.match = " ".join(args), None
        buf = self.read_log_buffer(self.logs.path) if self.logs.path else None
        if self.logs.search:
            self.find_log_match(buf, backwards=True)
        self.say(self.log_search_message(buf))

    def _command_help(self, args: list[str], line: str, cmd: str) -> None:
        self.mode, self.scroll = "help", 0

    def _command_quit(self, args: list[str], line: str, cmd: str) -> None:
        self.quit = True

    def replay_control(self, what: str, arg: str = "") -> None:
        rp = getattr(self, "replay", None)
        if rp is None:
            self.fail("not replaying (raven --replay FILE)")
            return
        c = rp.clock
        if what == "pause":
            self.say("paused" if c.toggle_pause() else "playing")
        elif what == "play":
            if c.paused:
                c.toggle_pause()
            self.say("playing")
        elif what == "back":
            c.skip(-60); self.say("60 s back")
        elif what == "fwd":
            c.skip(60); self.say("60 s forward")
        elif what == "slower":
            c.set_speed(max(0.1, c.speed / 2)); self.say(f"speed x{c.speed:g}")
        elif what == "faster":
            c.set_speed(min(1000, c.speed * 2)); self.say(f"speed x{c.speed:g}")
        elif what == "speed":
            try:
                c.set_speed(float(arg))
                self.say(f"speed x{c.speed:g}")
            except ValueError:
                self.fail("replay speed <n>")
        elif what == "seek":
            a = arg.strip()
            try:
                if a.endswith("%"):
                    c.seek(c.t0 + (c.t1 - c.t0) * float(a[:-1]) / 100)
                elif a.startswith(("+", "-")):
                    c.skip(float(a))
                elif ":" in a:
                    day = time.strftime("%Y-%m-%d", time.localtime(c.t0))
                    hhmm = a if a.count(":") == 2 else a + ":00"
                    t = time.mktime(time.strptime(f"{day}T{hhmm}", "%Y-%m-%dT%H:%M:%S"))
                    c.seek(t)
                else:
                    c.seek(c.t0 + float(a))
                self.say(f"at {time.strftime('%H:%M:%S', time.localtime(c.now()))}")
            except ValueError:
                self.fail("replay seek <HH:MM | +60 | -60 | 50% | seconds>")
        else:
            self.fail("replay <pause|play|back|fwd|slower|faster|speed N|seek ...>")
        if self.sampler:
            self.sampler.refresh_all()

    def evaluate(self, text: str) -> str:
        """Evaluate cached data on the shared worker in an interactive session."""
        snapshot, user, marks = self.store.render_snapshot(), self.user, tuple(self.marks)

        def evaluate_snapshot():
            from .expr import Expr, ExprError, cluster_ns, bounded_repr
            try:
                value = Expr(text)(cluster_ns(snapshot, user, marks))
                if isinstance(value, (list, tuple, set)):
                    value = [item.get("id", item) if isinstance(item, dict) else item for item in value]
                output = value if isinstance(value, str) else bounded_repr(value)
                return True, output if len(output) < 300 else output[:297] + "..."
            except ExprError as exc:
                return False, "eval: " + str(exc)

        if self.interactive and self.research is not None:
            # Capture references before admission; the worker never reaches
            # back into mutable selection/marks or publishes directly to UI.
            self.research.poll_task()
            def completed(result):
                if isinstance(result, Exception):
                    self.fail("eval: " + str(result)[:512])
                else:
                    self.command_ok, output = result
                    self.say(output, level="info" if self.command_ok else "error")
            if self.research.start_task(evaluate_snapshot, completed):
                return "Evaluating the cached snapshot..."
            self.command_ok = False
            return "eval: background lane is busy; try again when the current task completes"
        self.command_ok, output = evaluate_snapshot()
        return output

    def advise(self, what: Optional[str]) -> str:
        """The advisor's suggestion for a job id, a job name, or the selected job."""
        from . import advisor
        snap = self.store.render_snapshot()
        if what is None:
            ids = self.target_ids()
            what = ids[0] if ids else None
        if not what:
            self.command_ok = False
            return "advise <id | name>"
        j = self.store.job(what)
        if j and not j.pending:
            from raven.recordings.presentation import _native_series
            a = advisor.advise_running(j, snap["live"].get(j.id), _native_series(self.store.series_of(j.id)), snap["finished"])
            return f"{j.id} {j.name} so far: " + (a.summary(self.views_ref.g.dot if self.views_ref else "|") or "nothing to change yet")
        f = next((f for f in snap["finished"] if f.id == what), None)
        if f:
            a = advisor.advise_finished(f, snap["finished"])
            return f"{f.id} {f.name} ({f.state.lower()}): " + (a.summary(self.views_ref.g.dot if self.views_ref else "|") or "nothing to change") + (f"  -> {a.flags()}" if a.flags() else "")
        name = j.name if j else what
        advs = [a for a in advisor.advise_names(snap["finished"]) if a.name == name]
        if advs:
            a = advs[0]
            return f"{name} ({a.id}): " + (a.summary(self.views_ref.g.dot if self.views_ref else "|") or "nothing to change") + (f"  -> {a.flags()}" if a.flags() else "")
        self.command_ok = False
        return f"no finished run of '{what}' in the window and no such running job"

    def click(self, y: int, x: int, hits: Sequence, button: str = "left", shift: bool = False, alt: bool = False) -> None:
        """Select/group rows, reset graphs, route exports, and clear selections.

        Shift-click extends text selection; other right-clicks clear selections.
        """
        # A pointer report must never replace the frame's published hit map.
        # In particular, a late event can retain rows from a previous tab or
        # an inline Research view after the selected job has changed.
        from .pointer_routing import PointerEvent, begin_gesture, route_capture
        pointer_event = PointerEvent(y, x, button, shift, alt)
        if not pointer_event.valid:
            return
        # Reconcile a retained gesture before a newly opened modal can claim
        # its release. A changed mode, toolbar, tab or geometry cancels the
        # original owner and consumes the stale continuation.
        if route_capture(self, pointer_event):
            return
        from .historical_ui import handle_mouse as historical_mouse
        if historical_mouse(self, y, x, button=button, shift=shift):
            return
        from .terminal.ui import handle_mouse as terminal_mouse
        if terminal_mouse(self, y, x, button=button, shift=shift, alt=alt):
            return
        from .ui.interaction_state import initialize as pointer_state, _current as current_graph, _scope as pointer_scope
        pointer = pointer_state(self)
        published_graph = pointer.get("graph")
        fresh_activation = button in ("press", "left")
        if fresh_activation and (not isinstance(hits, Sequence) or
                                 isinstance(hits, (str, bytes, bytearray)) or
                                 any(not isinstance(hit, (tuple, list)) or len(hit) != 3
                                     for hit in hits)):
            return
        if fresh_activation and not (0 <= x < self.width and
                                     0 <= y < getattr(self, "height", 100000)):
            return
        content_current = True
        tab_target = None
        if fresh_activation and published_graph is not None:
            # A selected row changes the Details identity without moving the
            # global tabs. They remain usable until the next content frame.
            tab_frame_current = (published_graph.scope == pointer_scope(self) and
                                 all(getattr(self, name, None) == expected for name, expected in
                                     zip(("width", "height"), published_graph.observed_geometry)))
            if self.mode == "main" and tab_frame_current:
                tab_target = next((key for ty, left, right, key in self.tab_hits
                                   if y == ty and left <= x < right), None)
            from .ui.interaction_state import hit_token
            published_hit_token = pointer.get("published_hit_token")
            content_current = (current_graph(self) is not None and
                               published_hit_token is not None and
                               hit_token(hits) == published_hit_token)
        if fresh_activation and content_current:
            # Published tokens above bind supplied maps to the real frame;
            # unpublished/headless renderers retain the original row API.
            self.last_hits = list(hits)
        if fresh_activation:
            # A new gesture supersedes every earlier owner's cancelled release.
            # The new owner can claim the press before lower-priority handlers
            # run; their old markers must not steal its eventual release.
            begin_gesture(self)
        if button in ("press", "left"):
            from .scrollbars import commit_selection_gesture
            commit_selection_gesture(self)
        if workbench.route_modal_mouse(self, y, x, button=button, shift=shift,
                                       current=content_current):
            return
        from .job_logs.history_export import active as export_active, handle_mouse as export_mouse
        from .scrollbars import handle_mouse as scrollbar_mouse
        from .column_resize import handle_mouse as column_mouse
        from .group_queue_drag import handle_mouse as queue_mouse
        from raven.jobs.group_drag import handle_mouse as group_drag_mouse
        from raven.jobs.group_menu import active as group_menu_active, handle_mouse as group_menu_mouse
        if group_menu_active(self):
            if scrollbar_mouse(self, y, x, button=button, shift=shift):
                return
            group_menu_mouse(self, y, x, button=button, shift=shift)
            return
        if scrollbar_mouse(self, y, x, button=button, shift=shift):
            return
        if export_active(self):
            export_mouse(self, y, x, button=button, shift=shift)
            return
        if button == "right":
            # Interval controls own reset gestures before generic selection
            # clearing. A reset must preserve marked jobs and selected text.
            from .toolbar import handle_interval_reset as toolbar_reset
            from .metric_live import handle_mouse as live_mouse
            if toolbar_reset(self, y, x):
                return
            if live_mouse(self, y, x, button=button, shift=shift):
                return
            if column_mouse(self, y, x, button=button, shift=shift):
                return
            if group_menu_mouse(self, y, x, button=button, shift=shift):
                return
        from raven.jobs.selection import context_click
        if button == "right":
            from .ui.text_selection_state import clear as clear_text
            clear_text(self)
        if context_click(self, y, x, button=button, shift=shift):
            return
        if button in ("press", "left"):
            # A fresh gesture commits the previous marked range even when its
            # terminal release was lost. Controls can activate before the row
            # selector runs; an old capture must not later restore old marks or
            # consume Escape after a Details button has already gained focus.
            getattr(self, "job_selection_state", {})["capture"] = None
        from .startup import handle_mouse as startup_mouse
        startup_mouse(self, y, x, button=button, shift=shift)
        from .interaction import handle_mouse as pointer_mouse
        pointer_mouse(self, y, x, button="motion", shift=shift)
        from .toolbar import handle_mouse as toolbar_mouse
        if toolbar_mouse(self, y, x, button=button, shift=shift):
            from . import chart_interaction, metric_live
            chart_interaction.cancel(self)
            metric_live.cancel(self)
            return
        if tab_target is not None and not shift:
            self.enter_tab(tab_target)
            return
        if fresh_activation and not content_current:
            # Global controls validate their own published tokens above.
            # Content controls and native row fallbacks require this frame.
            return
        if column_mouse(self, y, x, button=button, shift=shift):
            return
        if queue_mouse(self, y, x, button=button, shift=shift, alt=alt):
            return
        from raven.jobs.selection import pointer_focus as job_pointer_focus
        if fresh_activation:
            job_pointer_focus(self, y, x)
            if group_drag_mouse(self, y, x, button=button, shift=shift, alt=alt):
                return
        from .text_selection import handle_mouse as text_mouse
        if getattr(self, "text_selection_state", {}).get("explicit") and text_mouse(self, y, x, button=button, shift=shift):
            return
        from .ui.selection_state import active as job_capture, advisor_pointer
        from raven.jobs.selection import handle_mouse as select_mouse
        if (job_capture(self) or advisor_pointer(self, y, x)) and select_mouse(self, y, x, button=button, shift=shift):
            return
        from .pane_drag import handle_mouse as pane_mouse
        if pane_mouse(self, y, x, button=button, shift=shift):
            return
        from .history_browser import handle_mouse as history_mouse
        if history_mouse(self, y, x, button=button, shift=shift):
            return
        from .metric_live import handle_mouse as live_mouse
        if live_mouse(self, y, x, button=button, shift=shift):
            return
        from .chart_interaction import handle_mouse as chart_mouse
        if chart_mouse(self, y, x, button=button, shift=shift):
            if button in ("press", "left") and not getattr(self, "text_selection_state", {}).get("explicit"):
                from .ui.text_selection_state import clear as clear_text
                clear_text(self)
            return
        if text_mouse(self, y, x, button=button, shift=shift):
            return
        if button == "press" and not shift and pointer_mouse(self, y, x, button="left"):
            return
        from raven.jobs.selection import handle_mouse as select_mouse
        if button in ("press", "left") and any(row == y and kind in
                ("job", "recent", "fin", "group", "source", "dep", "log_file")
                for row, kind, _ in hits):
            from .scrollbars import resume as resume_scroll
            resume_scroll(self)
        if select_mouse(self, y, x, button=button, shift=shift):
            return
        if button == "press":
            button = "left"
        elif button in ("motion", "drag", "release") and self.mode != "terminal_probe":
            return
        if workbench.handle_mouse(self, y, x, button, shift):
            return
        if button != "left":
            # Motion and release belong to captures, while wheels and other
            # buttons belong to their explicit handlers. None may activate a
            # tab, sort header, row, file, or Research link through fallthrough.
            return
        from .table_tools import handle_click_hit
        if handle_click_hit(self, y, x, hits, button=button, shift=shift):
            return
        if button == "left" and not shift:
            from .table_sort import cycle_sort, valid_header
            for row, kind, payload in hits:
                if row != y or kind != "sort_header" or not valid_header(payload):
                    continue
                table, column, left, right = payload
                if left <= x < right and (table == self.tab or table == "recent" and self.tab == "jobs"):
                    cycle_sort(self, table, column)
                    self.table_sort_changed(table)
                    return
        if self.tab == "log" and not self.logs.browser:
            line = next((int(key) for hy, kind, key in hits if hy == y and kind == "log_line"), None)
            if line is not None:
                buf = self.prepare_log()
                if buf is None or self.log_render_token != (self.logs.path, self.logs._buffer_token):
                    self.logs.clear_selection(reset_cursor=True)
                    self.log_selection_expected = True
                    self.fail("Log changed since that row was drawn; select a line in the current display")
                    return
                from .ui.selection_effects import resume_lines
                resume_lines(self)
                if shift:
                    if not self.logs.selection_active:
                        self.logs.begin_selection(buf)
                    self.logs.selection_end = self.logs.cursor = line
                else:
                    self.logs.clear_selection()
                    self.logs.cursor = line
                    self.logs.begin_selection(buf, line)
                self.log_selection_expected = self.logs.selection_active
                return
        if shift:
            from .text_selection import extend_row_range
            if not extend_row_range(self, y, x):
                self.fail("The selected pane changed; select rows in the current display before copying")
            return
        self.click_row = y
        for (ty, x0, x1, key) in self.tab_hits:
            if y == ty and x0 <= x < x1:
                self.enter_tab(key)
                return
        for (hy, kind, key) in hits:
            if hy == y:
                if kind == "job":
                    if key in self.visible_ids:
                        from .ui.selection_effects import resume
                        resume(self, "jobs")
                        self.cursor["jobs"] = self.visible_ids.index(key)
                elif kind == "recent":
                    if key in self.recent_ids:
                        from .ui.selection_effects import resume
                        resume(self, "jobs")
                        self.cursor["jobs"] = len(self.visible_ids) + self.recent_ids.index(key)
                elif kind == "log_file":
                    ids = [entry["id"] for entry in self.log_entries()]
                    if key in ids:
                        from .ui.selection_effects import resume_lines
                        resume_lines(self)
                        self.logs.browser_cursor = ids.index(key)
                elif kind == "log_group":
                    from .job_logs.workbench import run_command
                    self.log_workbench_state["current_group"] = key
                    run_command(self, ["loggroup", key])
                elif kind == "research_evidence":
                    ids = self.analysis_state.get("evidence_ids", [])
                    if key in ids:
                        from .ui.selection_effects import resume_lines
                        resume_lines(self)
                        self.analysis_state["evidence_cursor"] = ids.index(key)
                        from .analysis_ui import handle_key
                        handle_key(self, "enter")
                elif kind == "research_metric":
                    from .analysis_ui import run_command
                    run_command(self, ["chart", key])
                elif kind == "fin":
                    ids = [f.id for f in self.history_jobs()]
                    if key in ids:
                        from .ui.selection_effects import resume
                        resume(self, "history")
                        self.cursor["history"] = ids.index(key)
                elif kind == "source":
                    names = self.ordered_source_ids()
                    if key in names:
                        from .ui.selection_effects import resume_lines
                        resume_lines(self)
                        self.cursor["sources"] = names.index(key)
                elif kind == "group":
                    ids = getattr(self, "group_ids", [])
                    if key in ids:
                        from .ui.selection_effects import resume
                        resume(self, "group")
                        self.cursor["group"] = ids.index(key)
                elif kind == "dep":
                    if key in self.dep_ids:
                        from .ui.selection_effects import resume
                        resume(self, "deps")
                        self.cursor["deps"] = self.dep_ids.index(key)
                elif kind == "research_array":
                    from .array_disclosure import select
                    try:
                        select(self, key)
                    except ValueError:
                        pass
                self.sync_selection()
                return

    # ---- confirmations -----------------------------------------------------------------------------
    def start_confirm(self, action: str):
        if self.tab not in ("jobs", "deps") or not self.actions:
            return
        if self.tab == "deps" and self.selected_id and not self.marks:
            jobs = [self.store.job(i) for i in self.chain_ids(self.selected_id)]
            jobs = [j for j in jobs if j]
        else:
            jobs = self.target_jobs()
        pend = [j for j in jobs if j.pending]
        if action == "hold" and pend and all(j.held for j in pend):
            action = "release"
        ok = [j for j in jobs if self.actions.applicable(action, j)[0]]
        if not ok:
            why = self.actions.applicable(action, jobs[0])[1] if jobs else "no job selected"
            self.fail(why)
            return
        self.confirm = dict(action=action, jobs=ok, action_scope=self._action_scope())
        self.mode = "confirm"

    def _action_scope(self):
        backend = getattr(getattr(self.actions, "slurm", None), "b", None)
        return (id(self.actions), str(getattr(self.store, "_recording_scope", "")),
                str(getattr(backend, "host", "local")), self.user)

    def _action_task(self, work, done, message):
        """Run explicit scheduler work once; only completion publishes UI state."""
        if not self.interactive:
            try:
                result = work()
            except Exception as exc:
                result = exc
            done(result)
            return True
        from raven.jobs.panels import _worker
        hub = _worker(self, getattr(self, "files", None))
        hub.poll_task()
        from .project_ui import cancel_automatic
        cancel_automatic(self)
        if not hub.start_task(work, done):
            self.fail("A background operation is still running; review the action again after it completes")
            return False
        self.say(message)
        return True

    def start_resubmit(self, args: List[str]):
        """Prepare and validate a clone on the worker; preserve exact argv for review."""
        from . import resubmit
        self.mode, self.confirm = "main", {}
        try:
            over, words, switches = resubmit.parse_override_args(args)
            if len(words) > 1:
                raise ValueError("provide one job id; use --script PATH to replace its script")
            if words == ["marked"]:
                ids = sorted(self.marks)
                if len(ids) != 1:
                    raise ValueError("marked requires exactly one explicitly marked job")
                words = ids
            if not words and not self.interactive:
                raise ValueError("non-interactive resubmit requires an explicit job id")
            jid = words[0] if words else (self.target_ids() or [None])[0]
            if not jid or not self.actions:
                raise ValueError("resubmit needs an exact job id and an active scheduler connection")
        except ValueError as exc:
            self.fail(f"resubmit: {exc}")
            return
        actions, scope = self.actions, self._action_scope()
        token = getattr(self, "resubmit_request", 0) + 1
        self.resubmit_request = token
        def work():
            result = self._prepare_resubmit(jid, over, switches, actions)
            result["action_scope"] = scope
            return result
        def done(result):
            if self._action_scope() != scope or self.resubmit_request != token:
                return
            if isinstance(result, Exception):
                self.fail("resubmit: " + str(result))
            else:
                self.confirm, self.mode = result, "confirm"
        self._action_task(work, done, "Preparing this resubmission in the background")

    def _prepare_resubmit(self, jid, over, switches, actions):
        from . import advisor, resubmit
        snap = self.store.render_snapshot()
        job = self.store.job(jid)
        fin = next((f for f in snap["finished"] if f.id == jid), None)
        if job is None and fin is None:
            raise ValueError(f"no job {jid} in the queue or the history")
        if "advised" in switches:
            from raven.recordings.presentation import _native_series
            adv = advisor.advise_running(job, snap["live"].get(jid), _native_series(self.store.series_of(jid)), snap["finished"]) if job and not job.pending else \
                (advisor.advise_finished(fin, snap["finished"]) if fin else None)
            if adv:
                if adv.mem_suggest:
                    over.setdefault("mem", adv.mem_suggest)
                if adv.cpus_suggest and adv.cpus_suggest != adv.cpus:
                    over.setdefault("cpus", str(adv.cpus_suggest))
                if adv.time_suggest:
                    over.setdefault("time", adv.time_suggest)
        try:
            lookup = getattr(actions.slurm, "record_submit_info", None)
            info = lookup(job or fin) if lookup else actions.slurm.submit_info(jid)
        except Exception as e:                            # sacct without SubmitLine (older Slurm), or unreachable: the record will do
            info = {}
            self.store.event("resubmit", f"sacct submit line unavailable for {jid}: {e}", job_id=jid)
        details = snap["details"].get(jid, {})
        details_lookup = getattr(actions.slurm, "record_details", None)
        if job and (details_lookup or not details):
            try:
                details = details_lookup(job) if details_lookup else actions.slurm.details(jid)
            except Exception:
                details = {}
        try:
            clone = resubmit.build(jid, info, details, job=job, fin=fin, overrides=over)
            has_submission = resubmit.has_submission(clone.argv)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if not has_submission:
            raise ValueError((clone.notes[0] if clone.notes else "nothing to submit") + " (--script PATH)")
        ok, output = actions.slurm.preview_submit(clone.argv, clone.workdir)
        if not ok:
            raise ValueError(f"preview failed: {output or 'sbatch --test-only did not succeed'}")
        from .slurm import parse_test_only
        est, nodes = parse_test_only(output)
        if est:
            from .model import when
            clone.probe = f"sbatch --test-only: would start {when(est)}" + (f" on {nodes}" if nodes else "")
        else:
            clone.probe = "sbatch --test-only: " + (output.strip() or "validated; no start-time projection")
        return dict(action="resubmit", jobs=[job] if job else [], clone=clone)


    def finish_confirm(self, yes: bool):
        from .historical_ui import active as historical_active
        if historical_active(self):
            self.fail("Historical observations cannot apply actions; return Live and prepare a new review")
            return
        self.mode = "main"
        if not yes:
            self.say("kept")
            return
        if self.confirm.get("action") == "submit":
            from .submission import submit
            from .research_commands import submission_done
            plan = self.confirm["plan"]
            directory = self.confirm.get("passport_directory") or os.path.join(plan["workdir"], ".raven", "passports")
            fn = lambda: submit(plan, self.actions.slurm, passport_directory=directory)
            if self.interactive:
                if self.research.start_task(fn, lambda value: submission_done(self, value)):
                    self.say("submitting once in the background")
                else:
                    self.fail("a research operation is still running; confirm submission again after it completes")
            else:
                submission_done(self, fn())
            return
        review = self.confirm
        if review.get("consumed"):
            self.fail("This confirmation was already consumed; select the targets and prepare a new review")
            return
        # Keep the reviewed scope available for inspection after an
        # abort, without allowing a second key/callback to apply it again.
        self.confirm = dict(review, consumed=True)
        actions, store, scope = self.actions, self.store, self._action_scope()
        def owns_review():
            return self.actions is actions and self.store is store and self._action_scope() == scope
        if review.get("action_scope", scope) != scope:
            self.fail("Connection changed after review; inspect the targets and review again")
            return
        if review.get("action") == "resubmit":
            clone = review["clone"]
            def work():
                if not owns_review():
                    raise ValueError("Connection changed before resubmission; prepare a new review")
                return actions.resubmit(clone)
            def done(result):
                if not owns_review():
                    return
                if isinstance(result, Exception):
                    self.fail("Resubmit outcome unknown: " + str(result) + "; inspect scheduler receipts before retrying")
                    return
                ok, out, new_id = result
                self.command_ok = ok
                unknown = out.startswith("Resubmit outcome unknown")
                self.say(f"resubmitted {clone.id} as {new_id}" if ok and new_id else (f"submitted: {out}" if ok else out if unknown else f"resubmit failed: {out}"))
                if ok and new_id:
                    store.tag(new_id, f"from-{clone.id.split('_')[0]}")
                if self.sampler:
                    self.sampler.refresh_all()
            self._action_task(work, done, "Submitting the reviewed clone once in the background")
            return
        action, expected = review.get("action"), review.get("jobs", [])
        if not actions or not expected:
            self.fail("Action is no longer available; select the jobs and review a new confirmation")
            return
        def work():
            if not owns_review():
                raise ValueError("Connection changed before the action; prepare a new review")
            live_jobs, invalid = [], []
            from .model import stamp
            for prior in expected:
                current = store.job(prior.id)
                if current is None:
                    invalid.append(f"{prior.id}: no longer in the active queue")
                    continue
                if not prior.pending and stamp(prior.start) is not None and prior.start != current.start:
                    invalid.append(f"{prior.id}: start time changed")
                    continue
                if stamp(prior.submit) is not None and prior.submit != current.submit:
                    invalid.append(f"{prior.id}: submission identity changed")
                    continue
                applicable, reason = actions.applicable(action, current)
                if not applicable:
                    invalid.append(f"{prior.id}: {reason}")
                else:
                    live_jobs.append(current)
            if invalid:
                raise ValueError("Action aborted: " + "; ".join(invalid[:4]) +
                          (f"; {len(invalid) - 4} more changed targets" if len(invalid) > 4 else "") +
                          "; select the jobs and review a new confirmation")
            return actions.run(action, live_jobs)
        def done(result):
            if not owns_review():
                return
            if isinstance(result, Exception):
                self.fail(str(result))
                return
            ok, out = result
            self.command_ok = ok
            ids = ", ".join(j.id for j in expected[:4]) + (" ..." if len(expected) > 4 else "")
            pending = ""
            if ok:
                from .actions import VERBS
                pending = " / " + VERBS[action][1].upper() + "; awaiting scheduler"
            self.say(f"{action} {ids}: " + ("sent" + pending if ok else f"failed: {out}"))
            if ok:
                self.marks.difference_update(j.id for j in expected)
            if self.sampler:
                self.sampler.refresh_all()
        self._action_task(work, done, f"Applying reviewed {action} in the background")
