"""The tabs and overlays as rows of (text, style) segments.  ``compose`` returns the rows for the whole screen plus
the hit map (row index -> what the mouse would select)."""
from __future__ import annotations

from .ui.table_schema import JOB_COLS as JOB_COLS, JOB_DROP as JOB_DROP, FIN_COLS as FIN_COLS, GROUP_COLS as GROUP_COLS

from raven.recordings.presentation import _series_of as _series_of, _series_publication as _series_publication, prepared_series as prepared_series, normalized_series as normalized_series, _series_jobs as _series_jobs, _native_series as _native_series, _series_notice as _series_notice, _MetricValues as _MetricValues, _metric_observations as _metric_observations, _metric_source_times as _metric_source_times, _metric_edge_values as _metric_edge_values, _metric_edge_signature as _metric_edge_signature, _gpu_value as _gpu_value, _gpu_mean as _gpu_mean, _gpu_record_mean as _gpu_record_mean, _gpu_memory as _gpu_memory, _managed_gpu_mode as _managed_gpu_mode, _gpu_unavailable as _gpu_unavailable, _busy_mean as _busy_mean, _owned_metric_record as _owned_metric_record, _prepare_native_metrics as _prepare_native_metrics, _prepare_trace_metrics as _prepare_trace_metrics, _prepare_retained_process_metrics as _prepare_retained_process_metrics, _retained_status_rows as _retained_status_rows

import os
import re
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Sequence, Tuple

from . import clock
from . import layout as L
from . import charts
from .layout import Column, Glyphs, Row, bar, box, cut, gradient_bar, pad, rule, spark, table, vlen
from . import advisor
from .deps import DepGraph
from .model import Finished, Job, Step, compact, hms, human, secs, short_duration, stamp, when
from .local_files import LocalFiles
from .job_logs.text import display_text
from . import scrollbars as SB
from .ui.catalog import TABS, ANALYTICS_VIEWS, NODES_VIEWS, SORTS as SORTS
from .job_logs.paths import stdout_path

LOG_ERROR = re.compile(r"\b(?:error|fatal|traceback|oom|killed|failed)\b", re.IGNORECASE)
LOG_WARNING = re.compile(r"\b(?:warn(?:ing)?|retry(?:ing)?|timeout)\b", re.IGNORECASE)
LOG_SUCCESS = re.compile(r"\b(?:done|complete(?:d)?|success(?:ful)?)\b", re.IGNORECASE)


















































def _scroll_rule(glyphs, width, title):
    """Keep jump controls in the title strip, outside the table columns."""
    return [("    ", "")] + rule(glyphs, max(0, width - 4), title) if width >= 6 else rule(glyphs, width, title)


def _table_scrollbar(app, key, width, first, page, count, top, *, header=0):
    """Publish a virtual table without changing its selected job or marks."""
    if width < 2 or page <= 0:
        return
    def seek(value):
        app.top[key] = max(0, int(value))
    SB.register(app, key, (first, 0, first + page, width), count, page,
                app.top.get(key, top), top, seek,
                header=(header, 0, width) if width >= 6 else None)



def job_sort_values(job: Job, row: dict, snap: dict, keys=None) -> dict:
    """Sort observations before display rounding, decoration or viewport slicing."""
    requested = set(keys) if keys is not None else {column.key for column in JOB_COLS}
    values = dict(id=job.id, name=job.name, part=job.partition, st=job.state,
                  user=job.user, where=job.nodelist or (job.nodes if job.pending else None),
                  cpus=job.cpus, gpu=job.gpus, prio=job.priority,
                  flags=row.get("flags", ""), tags=row.get("tags", ""), info=row.get("info", ""))
    if "progress" in requested:
        values["progress"] = row.get("_progress_value")
    # A JOBID or name click should not parse timestamps or inspect telemetry for
    # every row. Only active measured columns need their unrounded observations.
    if "time" in requested:
        values["time"] = None if job.pending else job.elapsed_s
    if "left" in requested:
        if job.pending:
            submitted = stamp(job.submit)
            values["left"] = clock.now() - submitted if submitted is not None else None
        else:
            elapsed, limit = job.elapsed_s, job.limit_s
            values["left"] = limit - elapsed if limit is not None and elapsed is not None else None
    if requested & {"cpu%", "eff", "mem%"}:
        live = snap.get("live", {}).get(job.id) if not job.pending else None
        if "cpu%" in requested:
            values["cpu%"] = (live.rate if live.rate is not None else live.avg) if live else None
        if "eff" in requested:
            values["eff"] = live.avg if live else None
        if "mem%" in requested:
            request = job.mem_bytes
            values["mem%"] = live.rss / request if live and live.rss is not None and request else None
    if "gpu%" in requested:
        gpu = (snap.get("gpu", {}).get(job.id) or []) if not job.pending else []
        values["gpu%"] = _gpu_mean(sample.util for sample in gpu)
    return values


def tail_lines(path: str, n: int, max_bytes: int = 131072, files=None) -> List[str]:
    if not path or n <= 0:
        return []
    files = files or LocalFiles()
    try:
        data, size = files.tail(path, max_bytes)
        data = data.decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if size > max_bytes and lines:
        lines = lines[1:]
    return [display_text(line.rstrip("\r")) for line in lines[-n:]]




class Views:
    feedback_options = True

    def __init__(self, glyphs: Glyphs, cfg, files=None, plugins=None):
        self.g, self.cfg = glyphs, cfg
        self.th = cfg["thresholds"]
        self.gpu_types = cfg["gpu_types"]
        self.files = files or LocalFiles()
        self._preview_cache = OrderedDict()
        self._preview_pending = set()
        from .metric_raster import MetricRasterCache
        self._metric_rasters = MetricRasterCache()
        from .native_series_cache import NativeSeriesCache
        self._native_sources = NativeSeriesCache()
        self.history_advice_cache = advisor.HistoryAdviceCache()
        self.plugins = plugins                             # PluginAPI or None: extra tabs and flags
        self.extra_tabs: Dict[str, object] = {}
        for key, title, render in (plugins.tabs if plugins else []):
            if key not in dict(TABS):
                TABS.append((key, title))
            self.extra_tabs[key] = render

    def set_ascii(self, ascii_: bool):
        """Reader mode: plain glyphs (the painter also drops colour)."""
        if ascii_ != self.g.ascii:
            self.g = Glyphs(ascii_)

    def visual_room(self, width: int, height: Optional[int], minimum: int = 18) -> bool:
        """Give dense visual panels room without displacing a selected table row on small screens."""
        return not self.g.ascii and width >= 72 and (height is None or height >= minimum)

    def composition(self, items: Sequence[Tuple[str, float, str]], width: int, title: str = "") -> List[Row]:
        """A measured distribution: counts remain readable when colour is unavailable."""
        if not any(value > 0 for _, value, _ in items):
            return []
        return charts.stacked_bar(self.g, items, width, title=title)

    def metric_curve(self, app, values, width, height, plot_key, *, row=0, column=0,
                     filled=False, running=None, observation_index=None, **options):
        """Render one source-scoped curve and stage its measured cell bounds."""
        if isinstance(values, _MetricValues):
            values = values.materialize()
        if isinstance(options.get("sample_times"), _MetricValues):
            options["sample_times"] = options["sample_times"].materialize()
        from . import chart_interaction, metric_live
        controls = []
        if running is not None and not filled:
            controls, _ = metric_live.controls(self.g, app, plot_key, width, running=running,
                                               row=row, column=column)
        from .metric_sampling_runtime import cadence as polling_cadence
        from .cadence import format_interval, source as polling_source
        companion = filled and isinstance(plot_key, tuple) and plot_key[0] == "resource-area"
        polling = polling_cadence(app, plot_key) if running or companion and metric_live.buffered(app, plot_key) else None
        if polling is not None and polling_source(plot_key) != "trace":
            options["sample_interval"] = polling
        wall = clock.now()
        oldest, newest, previous = _metric_source_times(options.get("sample_times"), wall, observation_index)
        display_end = metric_live.display_end(app, plot_key, now=wall, oldest=oldest,
                                              newest=newest, previous=previous,
                                              poll_interval=polling or options.get("sample_interval"))
        buffered_source = metric_live.buffered(app, plot_key)
        live_window = metric_live.window(app, plot_key, now=wall)
        captured = chart_interaction.captured_bounds(app, plot_key, scale="linear")
        zoom = chart_interaction.bounds(app, plot_key, scale="linear")
        display_window = live_window
        if display_window is None and buffered_source and charts._finite(wall) is not None and not captured and not zoom:
            end = wall if display_end is None else display_end
            original = options.get("times")
            start = original[0] if oldest is not None and original and charts._finite(original[0]) is not None else oldest
            # One known point gets a real one-second coordinate domain. It is
            # still labelled buffering, and no left-hand sample is invented.
            display_window = (start if start is not None and start < end else end - 1., end)
        if display_window:
            options["times"] = display_window
        interval = charts._finite(options.get("sample_interval"))
        indexed = observation_index is not None and interval is not None and interval > 0
        source_values, source_times = values, options.get("sample_times")
        if buffered_source and (zoom or captured) and options.get("sample_times") is not None and not indexed:
            # A captured coordinate system remains fixed while Live advances.
            # Future replay records cannot enter its fitted bounds or source age.
            observations = [(value, timestamp) for value, timestamp in zip(values, options["sample_times"])
                            if charts._finite(timestamp) is not None and timestamp <= wall]
            values = [value for value, _ in observations]
            options["sample_times"] = [timestamp for _, timestamp in observations]
            observation_index = None
        if zoom:
            options.update(times=zoom["x"], lo=zoom["y"][0], hi=zoom["y"][1])
            options["time_units"] = True
            if chart_interaction.autofit(app, plot_key, scale="linear") and options.get("sample_times") is not None:
                if not captured:
                    fit_values, fit_times = values, options["sample_times"]
                    if indexed:
                        fit_values, fit_times, _, _ = observation_index.window(
                            values, zoom["x"], latest=wall if buffered_source else None)
                    options["lo"], options["hi"] = charts.fit_time_bounds(
                        fit_values, fit_times, zoom["x"], zoom["y"], options.get("sample_interval"))
                options["fitted"] = True
        # Publications keep flowing during a drag. Freeze its coordinate
        # system so a new timestamp or resource peak cannot move the pointer's
        # mapping, while painting the latest observations in that system.
        if captured:
            options.update(times=captured["x"], lo=captured["y"][0], hi=captured["y"][1])
        visible_values = None
        newest, newest_known = None, False
        bounds = options.get("times")
        sample_times = options.get("sample_times")
        if bounds and sample_times is not None and (display_window or zoom or captured):
            if interval is not None and interval > 0:
                if indexed:
                    values, sample_times, visible_values, newest = observation_index.window(
                        values, bounds, latest=wall if buffered_source else None)
                else:
                    values, sample_times, visible_values, newest = _metric_observations(
                        values, sample_times, bounds, latest=wall if buffered_source else None)
                options["sample_times"] = sample_times
                newest_known = True
            else:
                if buffered_source:
                    observations = [(value, timestamp) for value, timestamp in zip(values, sample_times)
                                    if charts._finite(timestamp) is not None and timestamp <= wall]
                    values = [value for value, _ in observations]
                    sample_times = options["sample_times"] = [timestamp for _, timestamp in observations]
                visible_values = [charts._finite(value) for value, timestamp in zip(values, sample_times)
                                  if charts._finite(timestamp) is not None and bounds[0] <= timestamp <= bounds[1]]
            if not zoom and not captured and options.get("hi") is None:
                display_values = visible_values
                if display_window:
                    # A short delayed window can lie entirely between two
                    # acquired samples. Their clipped intersections determine
                    # the visible height, without adding synthetic readings to
                    # the header statistics or joining an actual source gap.
                    display_values = list(visible_values) + _metric_edge_values(values, sample_times, bounds, interval)
                options["lo"], options["hi"] = charts._bounds(display_values, options.get("lo", 0.0), None)
        from . import metric_playback
        presented_end = None
        playback = metric_live.playback_status(app, plot_key)
        theme = getattr(getattr(app, "_chart_owner", app), "theme", "")
        if (buffered_source and not live_window and not zoom and not captured and playback is not None
                and display_window and sample_times is not None and interval is not None and interval > 0):
            lo, hi = charts._bounds(visible_values or (), options.get("lo", 0.), options.get("hi"))
            chart_width = max(1, min(charts.MAX_COLUMNS, width - L.vlen(options.get("indent", "   "))
                                     - max(1, options.get("axis_w", 7))))
            raster_x = 1 if self.g.ascii or filled else 2
            raster_y = ((len(charts.LEVELS_ASCII[1:]) if self.g.ascii else len(self.g.spark)) if filled else
                        1 if self.g.ascii else 2 if options.get("curve_style") == "blocks" else 4)
            edge = _metric_edge_signature(source_values, source_times, display_window[1], wall,
                                          interval, lo, hi, min(charts.MAX_HEIGHT, max(0, height)) * raster_y,
                                          observation_index if indexed else None)
            geometry = (width, height, filled, self.g.ascii, theme, lo, hi, display_window[0], interval,
                        options.get("axis_w", 7), options.get("indent", "   "), options.get("title", ""),
                        options.get("unit", ""), options.get("curve_style", "fine"), options.get("envelope", True),
                        playback.delay, playback.interval)
            entry = metric_live.initialize(app)["entries"].get(metric_live.canonical(plot_key), {})
            owner = getattr(app, "_chart_owner", app)
            replay_clock = getattr(getattr(owner, "replay", None), "clock", None)
            force_token = (entry.get("token"), entry.get("generation"), entry.get("rate"),
                           id(replay_clock), getattr(replay_clock, "generation", None))
            presented = metric_playback.present(app, plot_key, playback, now=wall, geometry=geometry,
                x_interval=(display_window[1] - display_window[0]) / (chart_width * raster_x),
                edge_signature=edge, force_token=force_token, oldest=oldest)
            if presented is not None:
                presented_end = presented.end
                # The gate holds only within an unchanged acquired bracket.
                # Source arrays/statistics stay exact; the axis is the domain
                # actually drawn, rather than the later candidate playhead.
                options["times"] = (display_window[0], presented_end)
        else:
            metric_playback.reset_presentation(app, plot_key)
        rows, metadata = self._metric_rasters.render(
            self.g, values, width, height, filled=filled, compact_rows=True,
            theme=theme, **options)
        if buffered_source:
            plotted = metadata.get("x_bounds")
            rect = metadata.get("plot_rect")
            if plotted and rect:
                metric_live.display_resolution(app, plot_key, plotted[1] - plotted[0],
                                               rect[3] - rect[1], following=not (zoom or captured))
        if options.get("title") and visible_values is not None and rows:
            title = charts.chart_model_title(options["title"], metadata)
            rows[0] = charts._header(self.g, visible_values, width, title,
                                     options.get("unit", ""), options.get("indent", "   "))
            if display_window and metadata.get("has_data") and not any(value is not None for value in visible_values):
                rows[0] = L.clip_row([(options.get("indent", "   ") + title, "cyan+bold"),
                                     ("  between acquired samples", "dim")], width)
        chart_interaction.record(app, plot_key, metadata, row=row + len(controls), column=column)
        if controls:
            if not newest_known:
                newest = max((timestamp for timestamp in sample_times or ()
                              if charts._finite(timestamp) is not None), default=None)
            age = compact(max(0.0, wall - newest)) if newest is not None else "unavailable"
            cadence = options.get("sample_interval")
            note = f" Source age {age}; sampling {format_interval(cadence, ascii_=self.g.ascii)}" if cadence else f" Source age {age}"
            if polling is not None and polling_source(plot_key) == "trace":
                note += f"; read every {format_interval(polling, ascii_=self.g.ascii)}"
            if live_window and not metadata.get("has_data"):
                note = " no observations in live window;" + note
            playback = metric_live.playback_label(app, plot_key, ascii_=self.g.ascii, displayed_end=presented_end)
            if playback:
                note = " " + playback + ";" + note
            rows.append(L.clip_row([(note, "dim")], width))
        return controls + rows

    @staticmethod
    def beside(panels: Sequence[List[Row]], widths: Sequence[int], gap: int = 2) -> List[Row]:
        """Join independent panels while retaining every segment's style and display width."""
        out: List[Row] = []
        for y in range(max((len(panel) for panel in panels), default=0)):
            row: Row = []
            for i, (panel, width) in enumerate(zip(panels, widths)):
                cell = L.clip_row(panel[y] if y < len(panel) else [], width)
                row += cell + [(" " * max(0, width - vlen(L.row_text(cell))), "")]
                if i < len(panels) - 1:
                    row.append((" " * gap, ""))
            out.append(row)
        return out

    def resource_cards(self, snap: dict, j: Job, width: int) -> List[Row]:
        """Three solid resource instruments, using only recorded CPU, peak RSS and elapsed time."""
        lv = snap["live"].get(j.id)
        cpu = (lv.rate if lv.rate is not None else lv.avg) if lv else None
        mem = lv.rss / j.mem_bytes if lv and lv.rss is not None and j.mem_bytes else None
        elapsed, limit = j.elapsed_s, j.limit_s
        used_time = elapsed / limit if elapsed is not None and limit else None
        efficiency = f"{100 * lv.avg:.1f}%" if lv and lv.avg is not None else "unknown"
        cpu_value = "waiting for a sample" if cpu is None else f"{100 * cpu:.0f}% {'now' if lv.rate is not None else 'average'}  ·  eff {efficiency}"
        cpu_note = (f"{cpu * j.cpus:.1f}/{j.cpus} cores  " + spark(self.g, snap["hist_cpu"].get(j.id, []), 10)) if cpu is not None else "sstat utilisation unknown"
        mem_value = f"{human(lv.rss)} / {human(j.mem_bytes)}" if lv and lv.rss is not None and j.mem_bytes else (f"{human(lv.rss)} used" if lv and lv.rss is not None else "memory sample unknown")
        mem_note = f"{100 * mem:.1f}% of requested memory" if mem is not None else "memory request unknown" if not j.mem_bytes else f"requested {human(j.mem_bytes)}"
        time_value = f"{j.elapsed or '?'} / {j.limit or '?'}"
        time_note = f"{hms(max(0, limit - elapsed))} remaining" if elapsed is not None and limit else "wall-time limit unknown"
        cards = [("CPU", cpu, cpu_value, cpu_note, "#22d3ee", "#3b82f6"),
                 ("PEAK MEMORY", mem, mem_value, mem_note, "#a78bfa", "#ec4899"),
                 ("WALL TIME", used_time, time_value, time_note, "#34d399", "#fbbf24")]
        cell = max(8, (width - 2 - 4) // 3)
        sizes = [cell, cell, max(8, width - 2 - 4 - cell * 2)]
        panels = []
        for (label, frac, value, note, start, end), size in zip(cards, sizes):
            inner = size - 4
            title = cut(" " + label + " ", size - 2, self.g.ascii)
            top = [("┌" + title + "─" * max(0, size - 2 - vlen(title)) + "┐", "dim")]
            def content(segments):
                segments = L.clip_row(segments, inner)
                return [("│ ", "dim")] + segments + [(" " * max(0, inner - vlen(L.row_text(segments))) + " │", "dim")]
            panel = [top, content([(cut(value, inner), "bold")]),
                     content(gradient_bar(self.g, frac, inner, start, end)),
                     content([(cut(note, inner), "dim")]), [("└" + "─" * (size - 2) + "┘", "dim")]]
            panels.append(panel)
        return [[(" ", "")] + row for row in self.beside(panels, sizes)]

    def node_resource_rows(self, nodes, width: int) -> List[Row]:
        """A categorical resource matrix; columns are measurements, never an implied time axis."""
        label_w = min(16, max(vlen(node.name) for node in nodes))
        cell_w = max(8, (width - label_w - 7) // 3)
        titles = ("CPU ALLOCATED", "LOAD / CORE", "MEMORY USED")
        out = [rule(self.g, width, "node resource matrix · measured percentages"),
               [(" " + " " * label_w + "  ", "")] + [(pad(cut(title, cell_w), cell_w) + ("  " if i < 2 else ""), "cyan+bold") for i, title in enumerate(titles)]]
        for node in nodes[:8]:
            values = (node.alloc / node.cpus if node.cpus else None,
                      node.load / node.cpus if node.cpus and node.load is not None else None,
                      (node.mem_total - node.mem_free) / node.mem_total if node.mem_total and node.mem_free is not None and node.mem_free <= node.mem_total else None)
            row: Row = [(" " + pad(cut(node.name, label_w), label_w) + "  ", "bold")]
            for i, value in enumerate(values):
                frac = max(0.0, min(1.0, value)) if value is not None else 0
                row += gradient_bar(self.g, value, cell_w - 8, "track", "cyan" if frac < .5 else "yellow")
                row.append((f" {100 * value:>4.0f}%  " if value is not None else "    ?   ", "bold" if value is not None else "dim"))
                if i < 2:
                    row.append(("  ", ""))
            out.append(row)
        out.append([("   Allocation is reserved CPU capacity; load is runnable work per core. ? means unobserved.", "dim")])
        return [L.clip_row(row, width) for row in out]

    # ---- shared pieces ----------------------------------------------------------------------------
    def header(self, snap: dict, app, width: int, *, toolbar_width=None, navigation_row=False) -> List[Row]:
        from .notifications import sync as sync_notifications
        sync_notifications(app, snap)
        jobs = snap["jobs"]
        running = [j for j in jobs if not j.pending]
        pending = [j for j in jobs if j.pending]
        tot_cpu, tot_gpu = sum(j.cpus for j in running), sum(j.gpus for j in running)
        ests = [j.est_start for j in pending if j.est_start not in ("N/A", "", "Unknown")]
        inv = snap.get("gpu_inventory", {})
        free = "  ".join(f"{t} {v['free']}/{v['total'] - v['down']}" for t, v in sorted(inv.items()) if t in self.gpu_types) or "n/a"
        sh = "  ".join(f"{s['account']} {s['fairshare']}" for s in snap.get("share", [])[:2]) or "n/a"
        acc = snap.get("account", {})
        age = clock.now() - snap["t_jobs"] if snap.get("t_jobs") else None
        who = app.user + (f"@{app.host_label}" if getattr(app, "host_label", "") else "") + (f" [{app.profile_name}]" if getattr(app, "profile_name", "") else "")
        project_root = getattr(app, "project_state", {}).get("root")
        if project_root:
            project_name = display_text(os.path.basename(str(project_root).rstrip(os.sep)))
            who += " / " + cut(project_name, max(8, min(24, width // 4)), self.g.ascii)
        if getattr(app, "demo", False):
            who += " [DEMO]"
        from .ui.allocation import headline as compute_headline
        coverage_note = ' [partial]' if any(value.get('limited') for value in snap.get('coverage',{}).values()) else ''
        r1: Row = [(f" Raven {compute_headline(app)}{coverage_note} {self.g.dot} {who} {self.g.dot} {time.strftime('%H:%M:%S', time.localtime(clock.now()))}", "bold"),
                   ("   running ", ""), (str(len(running)), "green"), (f" ({tot_cpu} cpus, {tot_gpu} gpus)   pending ", ""), (str(len(pending)), "yellow")]
        rp = getattr(app, "replay", None)
        if rp is not None:
            c = rp.clock
            r1.append((f"   REPLAY {time.strftime('%m-%d %H:%M:%S', time.localtime(c.now()))} {g_speed(c)}" + ("  paused" if c.paused else ("  end" if c.at_end else "")), "magenta"))
        if ests:
            r1.append((f"   next start {when(min(ests))}", ""))
        if app.filter:
            r1.append((f"   filter '{app.filter}'", "magenta"))
        if app.marks:
            r1.append((f"   {len(app.marks)} marked", "magenta"))
        if getattr(app, "freeze_label", ""):
            r1.append(("   " + app.freeze_label, "yellow+bold"))
        if getattr(app, "inbox_unread", 0):
            r1.append((f"   {app.inbox_unread} to review (:inbox)", "cyan"))
        gpu_mode = _managed_gpu_mode(app)
        if gpu_mode is not None:
            r1.append(("   GPU telemetry off" if gpu_mode == "off" else "   GPU existing telemetry", "dim"))
        elif not app.gpu:
            r1.append(("   GPU sampling off", "dim"))
        from .refresh_rate import cadence
        if age is not None and age > 3 * cadence(app, "jobs") + 2:
            r1.append((f"   (data {int(age)}s old)", "yellow"))
        r2: Row = [(f" free GPUs: {free}    fair share: {sh}", "")]
        if acc:
            r2.append((f"    {acc['account']}: {acc['running']} running ({acc['cpus']} cpus, {acc['gpus']} gpus), {acc['pending']} pending", "dim"))
        budget_hint = ""
        bud = snap.get("budget", {})
        if bud and bud.get("limit"):
            bits = []
            for k in ("cpu", "gpu"):
                if bud["limit"].get(k):
                    bits.append(f"{k} {100 * bud['used'].get(k, 0) / bud['limit'][k]:.0f}%")
            if bits:
                budget_hint = f"    allocation {' '.join(bits)}"
                r2.append((budget_hint, "dim"))
        r2 = self.page_metrics(snap, app, width) or r2
        if budget_hint and width - vlen(L.row_text(r2)) >= vlen(budget_hint):
            r2.append((budget_hint, "dim"))
        from .ui.allocation import observation_notice
        notice=observation_notice(app,snap)
        if notice:
            # Preserve header height and all pane coordinates while making an
            # unobserved queue distinct from a successful empty observation.
            r2=[(' '+cut(notice,max(0,width-1),self.g.ascii),'yellow')]
        from .toolbar import render_bar
        # Main destinations live in the persistent sidebar. Keep the tab_bar
        # renderer available for extensions, without a competing horizontal row.
        app.tab_hits = []
        toolbar_width = width if toolbar_width is None else toolbar_width
        rows = [render_bar(self, app, toolbar_width), r1, r2]
        if rp is not None:
            rows.append(self.replay_bar(rp, width))
        if navigation_row:
            # Streamed --once/watch output has no fixed-height side rail. A
            # compact destination row retains the plain-output contract.
            tabs, tab_hits = self.tab_bar(snap, app, width)
            app.tab_hits = [(len(rows), x0, x1, key) for x0, x1, key in tab_hits]
            rows.append(tabs)
        from .notification_ui import status_row
        notification_row = status_row(self, app, width, len(rows))
        if notification_row:
            rows.append(notification_row)
        from .navigation_ui import breadcrumb
        from .table_ui import chips
        trail = breadcrumb(app, width, self.g.ascii, snap=snap)
        if trail:
            rows.append(trail)
        if app.tab in ("jobs", "history", "research", "log"):
            from .project_ui import status_row
            attachment = status_row(app, width, self.g.ascii)
            if attachment:
                rows.append(attachment)
        fields = chips(app, app.tab, width, self.g.ascii)
        app.table_chip_y = len(rows) if fields else None
        if fields:
            rows.append(fields)
        return rows

    def tab_bar(self, snap: dict, app, width: int):
        """Fit a contiguous set of tabs, keeping the current page visible and mouse targets exact."""
        labels = []
        from .toolbar import visible_tabs
        for key, title in visible_tabs(app):
            n = {"jobs": len(snap["jobs"]), "history": len(snap.get("finished", [])), "nodes": len(snap.get("nodes", {})), "group": len(snap.get("group", [])),
                 "sources": sum(1 for h in snap.get("health", {}).values() if h.error)}.get(key)
            label = f" {title}" + (f" {n}" if n else "") + " "
            if key == "history" and app.completion.new_history:
                label = label.rstrip() + f" +{app.completion.new_history} "
            labels.append((key, label))
        active = next((i for i, (key, _) in enumerate(labels) if key == app.tab), 0)
        start = 0
        while start < active and 1 + sum(vlen(t) + 1 for _, t in labels[start:active + 1]) > width - 2:
            start += 1
        x = 1 + (2 if start else 0)
        tabs: Row = [(" < " if start else " ", "dim")]
        hits = []
        for i, (key, label) in enumerate(labels[start:], start):
            reserve = 2 if i < len(labels) - 1 else 0
            if x + vlen(label) > width - reserve and i > active:
                tabs.append((" >", "dim"))
                break
            label = cut(label, max(0, width - x - reserve), self.g.ascii)
            style = "rev+bold" if key == app.tab else "dim"
            if key == "history" and app.animations_enabled and app.completion.flash_on():
                style = "bg:#92400e+fg:#fff7ed+bold"
            elif key == "history" and app.completion.new_history:
                style = "yellow+bold" if key != app.tab else "rev+bold"
            tabs.append((label, style))
            hits.append((x, x + vlen(label), key))
            tabs.append((" ", ""))
            x += vlen(label) + 1
        return L.clip_row(tabs, width), hits

    def trace_workspace_tab(self, snap, app, width, height):
        from .tracing.ui import render_global

        return render_global(self, snap, app, width, height)

    def page_metrics(self, snap: dict, app, width: int) -> Row:
        """The top ribbon answers the most useful questions for the current page, without extra rows."""
        jobs, finished = snap["jobs"], snap.get("finished", [])
        running = [j for j in jobs if not j.pending]
        inv = snap.get("gpu_inventory", {})
        health = list(snap.get("health", {}).values())
        items = []
        if app.tab == "jobs":
            rates = [lv.rate if lv.rate is not None else lv.avg for j in running
                     if (lv := snap.get("live", {}).get(j.id)) is not None]
            rates = [r for r in rates if r is not None]
            flags = sum(bool(self.flags_of(j, snap["live"].get(j.id), snap["gpu"].get(j.id), snap)[0]) for j in running)
            items = [("CPU mean", f"{100 * sum(rates) / len(rates):.0f}%" if rates else "waiting", "cyan"),
                     ("GPU free", str(sum(v["free"] for v in inv.values())) if inv else "unknown", "green"),
                     ("Check jobs", str(flags), "yellow" if flags else "green"),
                     ("Queue", str(sum(j.pending for j in jobs)), "yellow")]
        elif app.tab == "cluster":
            parts = snap.get("partitions", [])
            items = [("Partitions", str(len(parts)), "cyan"),
                     ("GPU free", str(sum(v["free"] for v in inv.values())) if inv else "unknown", "green"),
                     ("GPU used", str(sum(v["used"] for v in inv.values())) if inv else "unknown", "cyan"),
                     ("GPU down", str(sum(v["down"] for v in inv.values())) if inv else "unknown", "yellow")]
        elif app.tab in ("history", "analytics"):
            done = sum(f.state == "COMPLETED" for f in finished)
            eff = [f.cpu_eff for f in finished if f.cpu_eff is not None]
            items = [("Completed", f"{done}/{len(finished)}", "green"),
                     ("CPU eff", f"{100 * sum(eff) / len(eff):.0f}%" if eff else "unknown", "cyan"),
                     ("Core-hours", f"{sum(f.core_hours for f in finished):,.1f}", "cyan"),
                     ("GPU-hours", f"{sum(f.gpu_hours for f in finished):,.1f}", "magenta")]
        elif app.tab == "nodes":
            nodes = list(snap.get("nodes", {}).values())
            total, used = sum(n.cpus for n in nodes), sum(n.alloc for n in nodes)
            cells = list(snap.get("nodemap", {}).values())
            items = [("My nodes", str(len(nodes)), "cyan"), ("Cores busy", f"{used}/{total}", "cyan"),
                     ("Map nodes", str(len(cells)), "green"), ("Down", str(sum(n.down for n in cells)), "yellow")]
        elif app.tab == "group":
            group = snap.get("group", [])
            active = [j for j in group if not j.pending]
            items = [("Users", str(len({j.user for j in group})), "cyan"), ("Running", str(len(active)), "green"),
                     ("Pending", str(len(group) - len(active)), "yellow"), ("CPUs", str(sum(j.cpus for j in active)), "cyan")]
        elif app.tab == "deps":
            dependent = [j for j in jobs if j.dependency and j.dependency not in ("(null)", "N/A")]
            items = [("Dependent", str(len(dependent)), "magenta"), ("Held", str(sum(j.held for j in jobs)), "yellow"),
                     ("Pending", str(sum(j.pending for j in jobs)), "yellow"), ("Running", str(len(running)), "green")]
        elif app.tab == "log":
            jid = app.log_job or app.selected_id
            job = app.log_target(snap)
            items = [("Job", jid or "select one", "cyan"), ("Stream", "files" if app.logs.browser else "following" if app.logs.following else "paused", "green" if app.logs.following else "yellow"),
                     ("State", job.state.lower() if job else "waiting", "cyan"), ("Search", app.logs.search or "off", "magenta")]
        elif app.tab == "sources":
            enabled = [h for h in health if h.enabled]
            good = sum(bool(h.last_ok) and not h.error for h in enabled)
            items = [("Healthy", f"{good}/{len(enabled)}", "green"),
                     ("Errors", str(sum(bool(h.error) for h in enabled)), "red" if any(h.error for h in enabled) else "green"),
                     ("Sampling", str(sum(h.inflight for h in enabled)), "cyan"), ("Disabled", str(len(health) - len(enabled)), "dim")]
        return L.metrics(self.g, items, width)

    def replay_bar(self, rp, width: int) -> Row:
        """The scrub bar of a replay: position in the recording, its span, speed and state."""
        g = self.g
        c = rp.clock
        bar_w = max(10, min(60, width - 70))
        n = int(round(c.frac * bar_w))
        fmt = "%H:%M:%S"
        label = " observed replay " if getattr(rp, "replay_mode", "") == "known-at-time" else " reconstructed replay "
        return [(label, "magenta+bold"), (g.full * n, "magenta"), (g.empty * (bar_w - n), "dim"),
                (f" {time.strftime(fmt, time.localtime(c.now()))} of {time.strftime(fmt, time.localtime(c.t0))}..{time.strftime(fmt, time.localtime(c.t1))}  x{c.speed:g}"
                 + ("  paused" if c.paused else ("  end" if c.at_end else "")) + "   | pause  < > 60 s  { } speed", "dim")]

    def footer(self, app, width: int) -> Row:
        if app.mode == "filter":
            return [(f" filter: {app.filter_edit}", "magenta"), ("  Enter applies, Esc clears", "dim")]
        if app.mode == "palette":
            hint = app.palette_hint()
            return [(f" :{app.palette_edit}", "magenta"), (f"   {hint}" if hint else "", "dim")]
        if app.message:
            if getattr(app, "message_level", "info") in ("warning", "error", "critical", "moderate", "mild"):
                center = getattr(app, "notifications", None)
                if center is not None and any(center.snapshot().counts.values()):
                    marker = "!" if self.g.ascii else "⚠"
                    return [(cut(f" {marker} New notification; open alerts or :notifications for details.",
                                 max(0, width - 1), self.g.ascii), "yellow")]
            else:
                return [(cut(app.message, width - 1, self.g.ascii), "yellow")]
        def k(action):
            label = app.keys_help(action)
            if self.g.ascii:
                for symbol, name in (("↑", "Up"), ("↓", "Down"), ("←", "Left"), ("→", "Right")):
                    label = label.replace(symbol, name)
            return label
        if app.tab == "log" and app.logs.selection_active:
            if app.logs.selection_all:
                return [(f" Entire log selected {self.g.dot} y copies the complete file {self.g.dot} Esc cancel", "yellow")]
            a, b = sorted((app.logs.selection_anchor, app.logs.selection_end))
            return [(f" Selecting log lines {a + 1}-{b + 1} ({b - a + 1}) {self.g.dot} arrows/PgUp/PgDn extend {self.g.dot} y copy {self.g.dot} Esc cancel", "yellow")]
        if app.sel_anchor is not None:
            a, b = sorted((app.sel_anchor, app.sel_end))
            return [(f" selecting rows {a + 1}-{b + 1} ({b - a + 1} lines)   {k('yank')} copy  {k('up')}/{k('down')} extend  {k('visual_all')} all  Esc cancel", "magenta")]
        # Keep help and quit discoverable even when the page has many shortcuts.
        primary = {
            "jobs": f"{k('up')}/{k('down')} select  {k('mark')} mark  {k('details')} details  {k('log')} log  {k('filter')} filter",
            "history": f"{k('up')}/{k('down')} select  {k('log')} log  {k('details')} series  {k('sort')} sort  {k('filter')} filter",
            "log": (f"{k('up')}/{k('down')} files  Enter open  Esc back  {k('filter')} filter" if app.logs.browser else
                    f"{k('up')}/{k('down')} line  {k('visual')} select  {k('yank')} copy  {k('copy_all')} all  {k('log_files')} files" + ("  Esc files" if app.logs.browse_return else "")),
            "sources": f"{k('up')}/{k('down')} select  {k('source_toggle')} toggle  {k('refresh')} refresh",
            "analytics": f"{k('view_prev')}/{k('view_next')} view  {k('up')}/{k('down')} job  {k('days_more')}/{k('days_less')} days",
            "research": f"{k('view_prev')}/{k('view_next')} view  {k('up')}/{k('down')} job/cohort  PgUp/PgDn scroll  Enter tasks",
            "nodes": f"{k('view_prev')}/{k('view_next')} view  {k('sort')} sort  {k('refresh')} refresh",
            "group": f"{k('up')}/{k('down')} select  {k('sort')} sort  {k('filter')} filter",
            "deps": f"{k('up')}/{k('down')} select  {k('details')} details  {k('cancel')} cancel chain  {k('hold')} hold/release",
        }.get(app.tab, f"{k('next_tab')} next tab  {k('refresh')} refresh")
        common = f"{k('help')} help  {k('quit')} quit"
        if width >= 110:
            common = f"{k('palette')} commands  " + common
        available = max(0, width - vlen(common) - 3)
        return [(" " + pad(cut(primary, available, self.g.ascii), available), "dim"),
                ("  " + common, "cyan")]

    # ---- jobs tab ---------------------------------------------------------------------------------
    def flags_of(self, j: Job, lv, g, snap) -> Tuple[str, str]:
        flags, style = [], ""
        if j.pending:
            if j.held:
                flags.append("held"); style = "yellow"
            if j.dependency:
                flags.append("dep")
            return " ".join(flags), style
        old = (j.elapsed_s or 0) > 60 * self.th["warn_after_minutes"]
        if old and lv and lv.avg is not None and lv.avg < self.th["cpu"]:
            flags.append("!cpu"); style = "red"
        if old and lv and lv.rss is not None and j.mem_bytes and lv.rss / j.mem_bytes < self.th["mem"]:
            flags.append("!mem"); style = style or "yellow"
        if old and j.gpus and g:
            key = f"{j.id}:" + getattr(g[0], "device_key", f"{g[0].node}:{g[0].index}")
            mean = snap.get("gpu_mean", {}).get(key)
            if mean is not None and mean / 100 < self.th["gpu"]:
                flags.append("!gpu"); style = "red"
        if j.limit_s and j.elapsed_s and j.limit_s - j.elapsed_s < 600:
            flags.append("ending"); style = style or "yellow"
        return " ".join(flags), style

    def plugin_flags(self, j: Job, snap: dict, app) -> List[Tuple[str, str]]:
        out = []
        for fn, st in (self.plugins.flags if self.plugins else []):
            try:
                text = fn(j, snap, app)
            except Exception:
                continue
            if text:
                out.append((str(text), st))
        return out

    def job_rows(self, snap: dict, app, actions=None, *, shared=False) -> List[dict]:
        from .table_query import matches
        from .table_sort import chain, sort_rows
        live, gpu = snap["live"], snap["gpu"]
        cascade = chain(app, "jobs")
        from raven.jobs.progress import published, observation
        progress_sources = published(app, snap)
        from raven.jobs.row_cache import lookup, remember
        cache_key, cached_rows = lookup(self, snap, app, actions, progress_sources, cascade, shared=shared)
        if cached_rows is not None:
            from .table_ui import group_rows
            return group_rows(app, cached_rows, snap)
        rows = []
        for j in snap["jobs"]:
            if not matches(app, "jobs", j, snap):
                continue
            lv = live.get(j.id)
            g = gpu.get(j.id) or []
            mark = actions.mark(j) if actions else ""
            flags, fstyle = self.flags_of(j, lv, g, snap)
            for text, st in self.plugin_flags(j, snap, app):
                flags = (flags + " " + text).strip()
                fstyle = fstyle or st
            styles = {"flags": fstyle} if fstyle else {}
            if j.pending:
                info = j.reason
                if j.est_start not in ("N/A", "", "Unknown"):
                    info += f" {self.g.dot} est {when(j.est_start)}"
                info += f" {self.g.dot} prio {j.priority}"
                sub = stamp(j.submit)
                left = compact(clock.now() - sub) if sub else ""
                row = dict(job=j, id=j.id, name=j.name, part=j.partition, st="PD", where=f"{j.nodes} node{'s' if j.nodes != 1 else ''}", cpus=j.cpus, gpu=j.gpu_text,
                           time=f"-/{j.limit}", left=f"waited {left}" if left else "", info=info, flags=flags, _style="dim", _styles=styles,
                           **{"cpu%": "", "eff": "", "mem%": "", "gpu%": ""})
            else:
                gutil = _gpu_mean(s.util for s in g)
                shown = (lv.rate if lv.rate is not None else lv.avg) if lv else None
                st = {"RUNNING": "R", "COMPLETING": "CG", "CONFIGURING": "CF", "SUSPENDED": "S"}.get(j.state, j.state[:2])
                info = f"started {when(j.start)}"
                if j.state != "RUNNING":
                    info += f" {self.g.dot} {j.state.lower()}"
                left = compact(j.limit_s - j.elapsed_s) if (j.limit_s and j.elapsed_s is not None) else ""
                row = dict(job=j, id=j.id, name=j.name, part=j.partition, st=st, where=j.nodelist, cpus=j.cpus, gpu=j.gpu_text, time=f"{j.elapsed}/{j.limit}", left=left,
                           info=info, flags=flags, _style="", _styles=styles,
                           **{"cpu%": "" if shown is None else f"{100 * shown:.0f}", "eff": "" if (not lv or lv.avg is None) else f"{100 * lv.avg:.1f}",
                              "mem%": "" if (not lv or lv.rss is None or not j.mem_bytes) else f"{round(100 * lv.rss / j.mem_bytes)}",
                              "gpu%": "" if gutil is None else f"{gutil:.0f}"})
            if getattr(j, "collection_stale", False):
                row["flags"] = (row["flags"] + " STALE").strip()
                row["_styles"]["flags"] = "yellow"
                row["info"] += f" {self.g.dot} retained scheduler data; latest read incomplete"
            if mark:
                row["info"] += f" {self.g.dot} {mark.upper()}"
                row["_style"] = "yellow"
            progress = observation(j, progress_sources)
            row["progress"] = progress.format(self.g.ascii)
            row["_progress_value"] = progress.fraction
            row["_styles"]["progress"] = progress.style
            row["_progress_animation"] = ("hourglass" if progress.basis == "pending" else
                                          "clock" if progress.basis == "time" and j.state in ("RUNNING", "COMPLETING") else "")
            rec = snap.get("tags", {}).get(j.id, {})
            row["tags"] = " ".join(rec.get("tags", []))
            row["pinned"] = bool(rec.get("pinned"))
            if row["pinned"]:
                row["id"] = (self.g.pin if hasattr(self.g, "pin") else "^") + j.id
            if rec.get("note"):
                row["info"] += f" {self.g.dot} {rec['note']}"
            if cascade:
                row["_sort"] = job_sort_values(j, row, snap, (key for key, _ in cascade))
            rows.append(row)
        key, rev = app.sort.get("jobs", "state"), app.reverse.get("jobs", False)
        if cascade is not None:
            rows = sort_rows(app, "jobs", rows)
        else:
            if key == "name":
                rows.sort(key=lambda r: (r["name"], r["id"]), reverse=rev)
            elif key == "id":
                rows.sort(key=lambda r: r["id"], reverse=rev)
            elif key == "time":
                rows.sort(key=lambda r: -(r["job"].elapsed_s or -1), reverse=rev)
            elif key == "priority":
                rows.sort(key=lambda r: -r["job"].priority, reverse=rev)
            else:
                rows.sort(key=lambda r: (r["job"].pending, -r["job"].priority if r["job"].pending else r["job"].start), reverse=rev)
        rows.sort(key=lambda r: not r["pinned"])             # pinned first (a stable sort keeps the order within each group)
        for r in rows:
            r["id"] = r["job"].id                                # the pin glyph goes into the mark column, not the id
        flt = app.filter.lower()
        if flt:
            if flt.startswith("#"):
                rows = [r for r in rows if flt[1:] in r["tags"].lower().split()]
            else:
                rows = [r for r in rows if flt in r["name"].lower() or flt in r["id"].lower() or flt in r["part"].lower() or flt in r["info"].lower() or flt in r["tags"].lower()]
        from .table_ui import group_rows
        remember(self, app, cache_key, rows)
        return group_rows(app, rows, snap)

    def selected_panel(self, snap: dict, j: Optional[Job], width: int, log_lines: int, app) -> List[Row]:
        if j is None:
            return []
        g_ = self.g
        kv = snap["details"].get(j.id, {})
        bw = max(12, min(30, width - 78))
        rows: List[Row] = []
        if j.pending:
            sub = stamp(j.submit)
            waited = short_duration(clock.now() - sub) if sub else "?"
            rows.append([(f" {j.id} ", "cyan"), (j.name, "bold"), (f"   pending on {j.partition} {g_.dot} submitted {when(j.submit)} {g_.dot} waited {waited} {g_.dot} priority {j.priority} {g_.dot} {j.account or ''} {j.qos or ''}", "")])
            est = when(j.est_start) if j.est_start not in ("N/A", "", "Unknown") else "not projected yet"
            asks = f"{j.nodes} node(s), {j.cpus} cpus" + (f", {j.gpu_text}" if j.gpus else "") + f", mem {j.mem_req}, limit {j.limit}"
            rows.append([(f"   reason {j.reason} {g_.dot} projected start {est} {g_.dot} asks {asks}", "")])
            if j.dependency:
                graph = DepGraph(snap["jobs"], {f.id: f"{f.name} {f.state.lower()}" for f in snap["finished"]})
                blocked = graph.blocked_by(j.id)
                rows.append([(f"   waits for {', '.join(blocked) if blocked else j.dependency}", "yellow")])
                down = graph.downstream(j.id)
                if down:
                    rows.append([(f"   and {len(down)} job{'s' if len(down) != 1 else ''} wait for it: " + " ".join(f"{d}({snap_name(snap, d) or '?'})" for d in down[:6]), "dim")])
            if kv.get("Command"):
                rows.append([(f"   command {cut(kv['Command'], width - 12, g_.ascii)}", "dim")])
            rows += self.tag_rows(snap, j.id)
            return rows
        el, lim = j.elapsed_s, j.limit_s
        tfrac = None if el is None or not lim else el / lim
        rows.append([(f" {j.id} ", "cyan"), (j.name, "bold"),
                     (f"   started {when(j.start)} {g_.dot} submitted {when(j.submit)} {g_.dot} {j.partition} {g_.dot} {j.nodelist} {g_.dot} {j.cpus} cpus" + (f" {g_.dot} {j.gpu_text}" if j.gpus else "")
                      + f" {g_.dot} mem {j.mem_req} {g_.dot} {j.account or ''} {j.qos or ''}", "")])
        lv = snap["live"].get(j.id)
        if not g_.ascii and width >= 104:
            rows += self.resource_cards(snap, j, width)
        else:
            b = bar(g_, tfrac, 10)
            left = "?" if (el is None or lim is None) else hms(max(0, lim - el))
            time_pct = "n/a" if tfrac is None else f"{round(100 * tfrac)}%"
            rows.append([("   time  ", ""), b, (f" {time_pct:>4}  {j.elapsed or '?'} of {j.limit or '?'}, {left} left, ends {when(j.end)}", "")])
            hist = snap["hist_cpu"].get(j.id, [])
            if not lv:
                rows.append([("   cpu   ", ""), bar(g_, None, bw), ("  n/a (sstat has no batch step yet)", "dim")])
            elif lv.rate is None and lv.avg is None:
                rows.append([("   cpu   ", ""), bar(g_, None, bw), ("  n/a (waiting for CPU utilisation samples)", "dim")])
            else:
                shown = lv.rate if lv.rate is not None else lv.avg
                tag = "now" if lv.rate is not None else "avg"
                efficiency = "n/a" if lv.avg is None else f"{100 * lv.avg:.1f}%"
                rows.append([("   cpu   ", ""), bar(g_, shown, bw), (f" {int(100 * shown):>3}% {tag}  eff {efficiency:>6}  ", ""), (spark(g_, hist), "cyan"),
                             (f"  {shown * j.cpus:.1f}/{j.cpus} cores, {hms(lv.cpu_time)} cpu time", "")])
            req = j.mem_bytes
            if lv and lv.rss is not None and req:
                rows.append([("   mem   ", ""), bar(g_, lv.rss / req, bw), (f" {round(100 * lv.rss / req):>3}%  {human(lv.rss)} of {human(req)} requested", "")])
            elif lv and lv.rss is not None:
                rows.append([("   mem   ", ""), bar(g_, None, bw), (f"  {human(lv.rss)} used (request unknown)", "")])
        if j.gpus:
            g = snap["gpu"].get(j.id, [])
            if g is None:
                rows.append([("   gpu   ", ""), bar(g_, None, bw), ("  n/a", "dim")])
                rows += _gpu_unavailable(snap, app, j.id, j)
            elif not g:
                if _managed_gpu_mode(app) is not None:
                    rows.append([("   gpu   ", ""), bar(g_, None, bw), ("  existing telemetry", "dim")])
                    rows += _gpu_unavailable(snap, app, j.id, j)
                else:
                    rows.append([("   gpu   ", ""), bar(g_, None, bw), ("  sampling ..." if app.gpu else "  sampling off", "dim")])
            else:
                for x in g:
                    key = f"{j.id}:" + getattr(x, "device_key", f"{x.node}:{x.index}")
                    mean = snap["gpu_mean"].get(key)
                    tag = f"gpu{x.index}" + (f"@{x.node}" if j.nodes > 1 else "")
                    util = _gpu_value(x.util)
                    util_text = f"{util:3.0f}%" if util is not None else "unknown"
                    memory_ratio, memory_text = _gpu_memory(x)
                    rows.append([(f"   {tag:<6}", ""), bar(g_, util / 100 if util is not None else None, bw), (f" {util_text}  mem ", ""), bar(g_, memory_ratio, 8), (f" {memory_text}  ", ""),
                                 (spark(g_, snap["hist_gpu"].get(key, [])), "cyan"), (f"  mean {'?' if mean is None else f'{mean:.0f}%'}  {x.name}", "dim")])
        trace = snap.get("trace", {}).get(j.id)
        if trace:
            rows.append(self.trace_row(trace, j, bw))
        for n, info in snap["nodes"].items():
            if n in j.hosts:
                load = f"{info.load:.1f}" if info.load is not None else "unknown"
                memory = f"{info.mem_free / 1024:.0f} of {info.mem_total / 1024:.0f} GB free" if info.mem_free is not None and info.mem_total else "free memory unknown"
                rows.append([(f"   node {n}: load {load} of {info.cpus} cores ({info.alloc} allocated), {memory}, {info.state}", "dim")])
        steps = [st for st in snap.get("steps", {}).get(j.id, []) if st.name not in ("extern",)]
        if len(steps) > 1 or (steps and steps[0].ntasks > 1):
            for st in steps[:4]:
                rows.append(self.step_row(st, j))
        old = (j.elapsed_s or 0) > 60 * self.th["warn_after_minutes"]
        same = any(f.name == j.name for f in snap["finished"])
        if lv and (old or same):
            adv = advisor.advise_running(j, lv, normalized_series(app, j.id), snap["finished"])
            text = adv.summary(g_.dot)
            if text:
                rows.append([("   advice ", "magenta"), (cut(text, width - 12, g_.ascii), "")])
        down = DepGraph(snap["jobs"]).downstream(j.id)
        if down:
            rows.append([(f"   {len(down)} job{'s' if len(down) != 1 else ''} wait for this one: " + " ".join(f"{d}({snap_name(snap, d) or '?'})" for d in down[:6]), "dim")])
        rows += self.tag_rows(snap, j.id)
        if log_lines > 0:
            path = stdout_path(j, kv, self.files, probe=not app.interactive)
            lines = self.log_preview(app, path, log_lines)
            rows.append([(f"   log {cut(path or '(stdout path not known yet)', width - 10, g_.ascii)}", "magenta")])
            for l in lines:
                rows.append([("     " + cut(l, width - 6, g_.ascii), "")])
            if path and not lines:
                rows.append([("     (empty)", "dim")])
        return rows

    def log_preview(self, app, path, count):
        """Reuse pane previews; shared-filesystem tails stay off the screen."""
        if not path or count <= 0:
            return []
        key = (id(self.files), path, count)
        entry = self._preview_cache.get(key)
        remote = bool(getattr(self.files, "remote", False))
        ttl = 5.0 if remote else .5
        from .cadence import file_interval, multiplier
        ttl = file_interval(ttl, multiplier(app), remote=remote)
        now = time.monotonic()
        if entry and now - entry[0] < ttl:
            self._preview_cache.move_to_end(key)
            return list(entry[1])
        def publish(lines):
            self._preview_pending.discard(key)
            if isinstance(lines, Exception):
                return
            self._preview_cache[key] = (time.monotonic(), tuple(lines))
            self._preview_cache.move_to_end(key)
            while len(self._preview_cache) > 16:
                self._preview_cache.popitem(last=False)
        if app.interactive and (remote or app.research is not None):
            if app.research is None:
                from .research import ResearchHub
                app.research = ResearchHub(app.cfg, self.files)
            if key not in self._preview_pending:
                self._preview_pending.add(key)
                backend = self.files
                if not app.research.start_task(lambda: tail_lines(path, count, files=backend), publish):
                    self._preview_pending.discard(key)
            return list(entry[1]) if entry else ["(waiting for the background log preview)"]
        lines = tail_lines(path, count, files=self.files)
        publish(lines)
        return lines

    def tag_rows(self, snap: dict, jid: str) -> List[Row]:
        rec = snap.get("tags", {}).get(jid)
        if not rec:
            return []
        bits = []
        if rec.get("pinned"):
            bits.append("pinned")
        if rec.get("tags"):
            bits.append("tags " + " ".join("#" + t for t in rec["tags"]))
        if rec.get("note"):
            bits.append("note: " + rec["note"])
        return [[("   " + f" {self.g.dot} ".join(bits), "cyan")]] if bits else []

    def trace_row(self, trace: List[dict], j: Job, bw: int) -> Row:
        """One line from the GPU trace the job writes itself: mean utilisation per GPU index over the trace, the last minute."""
        g_ = self.g
        points = [r for r in trace if isinstance(r, dict) and isinstance(r.get("index"), int)
                  and not isinstance(r["index"], bool) and r["index"] >= 0
                  and isinstance(r.get("t"), (int, float)) and not isinstance(r["t"], bool)
                  and charts._finite(r["t"]) is not None]
        points.sort(key=lambda r: r["t"])
        by: Dict[int, List[float]] = {}
        for r in points:
            by.setdefault(r["index"], []).append(r.get("util"))
        last_t = points[-1]["t"] if points else 0
        last = {r["index"]: _gpu_value(r.get("util")) for r in points if r["t"] >= last_t - 60}
        means = {i: _gpu_mean(values) for i, values in by.items()}
        span = (points[-1]["t"] - points[0]["t"]) / 60 if points else 0
        valid = [value for r in points if (value := _gpu_value(r.get("util"))) is not None]
        idle = sum(value < 10 for value in valid)
        shown = lambda value: f"{value:.0f}%" if value is not None else "unknown"
        parts = "  ".join(f"gpu{i} {shown(means[i])} (now {shown(last.get(i))})" for i in sorted(means))
        mean = _gpu_mean(means.values())
        qualifier = "samples" if len(valid) == len(points) else "valid samples"
        idle_text = f"{100 * idle / len(valid):.0f}% of {qualifier} idle" if valid else "idle share unknown"
        seg: Row = [("   trace ", ""), bar(g_, mean / 100 if mean is not None else None, bw),
                    (f"  {parts}  {g_.dot} {span:.0f} min in the job's own nvidia-smi log, {idle_text}", "dim")]
        return seg

    def step_row(self, st: Step, j: Job) -> Row:
        """One step of a running job: CPU time, peak memory and where, the slowest rank."""
        g_ = self.g
        text = f"   step {st.name or st.id:<8} {st.ntasks} task{'s' if st.ntasks != 1 else ''}"
        if st.cpu_time is not None:
            text += f" {g_.dot} cpu {hms(st.cpu_time)}"
        if st.rss:
            text += f" {g_.dot} peak {human(st.rss)}" + (f" on task {st.rss_task}@{st.rss_node}" if st.ntasks > 1 and st.rss_node else "")
        style = "dim"
        if st.ntasks > 1 and st.min_cpu is not None and st.cpu_time:
            ratio = st.min_cpu / st.cpu_time if st.cpu_time else 1.0
            text += f" {g_.dot} slowest rank {st.min_cpu_task}@{st.min_cpu_node} at {100 * ratio:.0f}% of the mean"
            if ratio < 0.6:
                style = "yellow"
        return [(text, style)]

    def jobs_tab(self, snap: dict, app, actions, width: int, height: Optional[int], *, prepared_rows=None) -> Tuple[List[Row], List[Tuple[int, str, str]]]:
        from .ui.table_schema import columns
        from .table_tools import record_page
        from .table_sort import header_hits
        job_columns = columns(app, "jobs", JOB_COLS)
        self.cfg_tags = snap.get("tags", {})
        rows_d = _table_job_rows(self, snap, app, actions) if prepared_rows is None else prepared_rows
        app.visible_ids = [r["id"] for r in rows_d]
        from . import recent_history
        from .grouping_toolbar import controls as grouping_controls
        grouping_rows, grouping_hits = grouping_controls(app, self.g, width) if height is not None else ([], [])
        native = bool(height is not None and getattr(app, "job_panel_defer_content", False))
        if native:
            height = max(0, getattr(app, "workspace_main_usable_height", height))
        if height is not None:
            grouping_rows = grouping_rows[:max(0, height - 3)]
            grouping_hits = [hit for hit in grouping_hits if hit[0] < len(grouping_rows)]
        queue_base = len(grouping_rows)
        app.jobs_toolbar_height = queue_base
        if native:
            queue_capacity, recent_capacity = recent_history.allocate(app, height, len(rows_d), reserved=queue_base)
        fin = app.recent_jobs(snap)
        if native and not fin:
            queue_capacity, recent_capacity = recent_history.allocate(app, height, len(rows_d), reserved=queue_base, recent_count=0)
        app.recent_ids = [f.id for f in fin]
        app.jobs_selection_options = app.jobs_options()
        n = len(rows_d)
        ids = app.visible_ids + app.recent_ids
        if app.last_jobs_ids is not None and ids != app.last_jobs_ids and app.selected_id in ids:
            app.cursor["jobs"] = ids.index(app.selected_id)
        app.last_jobs_ids = list(ids)
        cur = app.clamp_cursor("jobs", len(ids))
        from .ui.selection_state import selected
        app.selected_id = selected(app, "jobs", ids[cur] if ids else None)
        selection_active = app.selected_id is not None
        recent_focus = bool(fin) and cur >= n
        sel = rows_d[cur]["job"] if n and not recent_focus and selection_active else None
        from raven.jobs.panels import render as render_details
        selected_record = fin[cur - n] if recent_focus and selection_active else sel
        if height is None:
            det = self.selected_panel(snap, sel, width, app.log_lines, app)
            if recent_focus and selection_active:
                det = self.finished_summary(fin[cur - n], width)
            detail_hits = []
        else:
            det, detail_hits = render_details(self, snap, app, selected_record, width,
                                             getattr(app, "job_panel_target_height", height))
            if native:
                det, detail_hits = [], []
        events = [e for e in snap["events"] if not e.get("old")][-4:] or snap["events"][-4:]
        if height is None:
            trows, _ = table(job_columns, rows_d, width, self.g.ascii, droppable=JOB_DROP, cursor=None, marks=(), mark_char=None)
            out = [rule(self.g, width, "jobs")] + trows
            if not rows_d:
                out.append([("   no jobs match the filter; Esc clears it" if app.filter else "   Your queue is clear. New jobs appear here automatically.", "dim")])
            if det:
                out += [rule(self.g, width, "selected")] + det
            out += self.finished_rows(fin, width, "recent", app=app)
            out += self.event_rows(events, width)
            return out, []
        budget = height
        moving = app.completion.moving() if app.animations_enabled and height >= 10 else []
        transit = []
        if moving:
            item = moving[-1]
            transit = [[(" ", "")] + gradient_bar(self.g, item["progress"], min(12, max(1, width // 5)), "#67e8f9", "#a78bfa") +
                       [(f" {item['job']} {item.get('name', '')} {'->' if self.g.ascii else '↓'} Recents", "cyan+bold")]]
        vis = min(n, queue_capacity) if native else min(n, max(1, budget // 3 - 2)) if n else 0
        if not native and getattr(app, "job_panel_source_canvas", False):
            budget = max(budget, len(det) + max(16, height // 3) + len(fin) + 16)
        fin_vis = min(len(fin), recent_capacity) if native else len(fin)
        show_queue = bool(n and queue_capacity) if native else True
        if native:
            events, transit = [], []
        def used():
            return queue_base + ((2 + vis if n else 3) if show_queue else 0) + (len(det) + 1 if det else 0) + \
                   (fin_vis + 2 if fin_vis else 0) + (len(events) + 1 if events else 0) + len(transit)
        while not native and used() > budget:
            if transit:
                transit = []
            elif events:
                events = events[:-1]
            elif fin_vis > (1 if recent_focus else 0):
                fin_vis -= 1
            elif det:
                det = det[:-1]
            elif vis > 1:
                vis -= 1
            elif recent_focus and show_queue:
                show_queue = False
            else:
                break
        if show_queue and n and not native:
            vis = min(n, vis + max(0, budget - used()))
        top = app.scroll_to("jobs", None if recent_focus else cur, max(1, vis), n)
        from raven.jobs.row_cache import copy_visible
        shown = copy_visible(rows_d[top:top + vis])
        app.job_progress_visible = [row["id"] for row in shown]
        app.job_progress_animation = {row["id"]: row.get("_progress_animation", "") for row in shown[:256]}
        marks = {i for i, r in enumerate(shown) if r["id"] in app.marks}
        for r in shown:
            r["_mark"] = self.g.pin if r.get("pinned") else ""
        cells = []
        trows, _ = table(job_columns, shown, max(1, width - 1), self.g.ascii, droppable=JOB_DROP, cursor=None if recent_focus or not selection_active else cur - top, marks=marks, mark_char=self.g.mark,
                         header_cells=cells)
        title = f"jobs {top + 1}-{min(n, top + vis)} of {n}" if n > vis else "jobs"
        out = list(grouping_rows) + ([_scroll_rule(self.g, width, title)] + trows if show_queue else [])
        hits = list(grouping_hits) + (header_hits("jobs", cells, queue_base + 1) +
            [(queue_base + 2 + i, "job", r["id"]) for i, r in enumerate(shown)] if show_queue else [])
        if show_queue:
            self.group_controls(app, out, hits, [r["job"] for r in shown], queue_base + 2, "jobs", width, first_visible=True)
            _table_scrollbar(app, "jobs", width, queue_base + 2, max(1, vis), n, top, header=queue_base)
            if native:
                while len(out) < queue_capacity + 2 + queue_base:
                    out.append([("", "")])
        if show_queue and not rows_d:
            out.append([("   no jobs match the filter; Esc clears it" if app.filter else "   Your queue is clear. New jobs appear here automatically.", "dim")])
        if det:
            detail_base = len(out) + 1
            out += [rule(self.g, width, "selected")] + det
            hits += [(detail_base + y, kind, value) for y, kind, value in detail_hits if y < len(det)]
        out += transit
        if fin_vis:
            recent_cur = cur - n if recent_focus else 0
            fin_top = app.scroll_to("recent", recent_cur, fin_vis, len(fin))
            recent_shown = fin[fin_top:fin_top + fin_vis]
            base = len(out)
            recent_cells = []
            recent_rows = self.aligned_recent_rows(recent_shown, max(1, width - 1), cells,
                recent_cur - fin_top if recent_focus and selection_active else None,
                app=app, header_cells=recent_cells)
            recent_rows[0] = _scroll_rule(self.g, width, "recent")
            from .recents_ui import divider as recent_divider
            recent_rows[0], recent_mode_hits, drag_width = recent_divider(app, self.g, recent_rows[0], width)
            hits += [(base + y, kind, value) for y, kind, value in recent_mode_hits]
            hits += header_hits("recent", recent_cells, base + 1) + [(base + 2 + i, "recent", f.id) for i, f in enumerate(recent_shown)]
            out += recent_rows
            self.group_controls(app, out, hits, recent_shown, base + 2, "recent", width, first_visible=True)
            recent_history.register_scrollbar(app, width, base + 2, fin_vis, fin_top, header=base)
            hits.append((base, "control", {"id": "recent-divider", "label": "Resize Queue and Recents",
                "left": 0, "right": drag_width, "action": ("command", "pane-focus recent:jobs"), "group": "pane-dividers"}))
        if native:
            recent_history.publish(app, fin, recent_capacity, n)
            if not rows_d and not fin:
                out = list(grouping_rows) + [rule(self.g, width, "jobs"), [("   no jobs match the filter; Esc clears it" if app.filter else
                    "   Your queue is clear. New jobs appear here automatically.", "dim")]]
        # Inline Details owns the supporting pane. The inspector and
        # investigation include the selected job's own evidence instead.
        record_page(app, "jobs", len(shown) + (len(recent_shown) if fin_vis else 0))
        return out, hits

    def group_controls(self, app, rows, hits, records, base, tab, width, *, first_visible=False):
        """Attach fold buttons to painted members, preserving representative IDs."""
        from raven.jobs.group_domain import metadata_for_record, summary
        from raven.jobs.group_ui import paint_info, fold_icon, fold_command
        from .pane_drag import _replace
        info = next(((value[2], value[3]) for y, kind, value in hits
                     if y == base - 1 and kind == "sort_header" and value[:2] == (tab, "info")), None)
        seen = {value.get("id") for _, kind, value in hits if kind == "control" and isinstance(value, dict)} if first_visible else set()
        for offset, record in enumerate(records):
            meta = metadata_for_record(app, tab, record.id)
            y = base + offset
            if meta is None or not (meta.header or first_visible) or y >= len(rows) or width < 2:
                continue
            identity = "jobgroup:" + tab + ":" + meta.group.id + (":row:" + str(y) if tab == "deps" else "")
            if identity in seen:
                continue
            seen.add(identity)
            glyph = fold_icon(meta.collapsed, ascii_=self.g.ascii)
            rows[y] = _replace(rows[y], 0, glyph, "cyan+bold", width)
            if info is not None:
                rows[y] = paint_info(rows[y], meta, info, width, ascii_=self.g.ascii)
            hits.append((y, "control", {"id": identity,
                "label": ("Expand " if meta.collapsed else "Collapse ") + meta.group.label + f" / {meta.visible_count} visible / {meta.total_count} observed; representative job " + meta.representative_id + "; " + summary(meta.stats or meta.records) + "; " + meta.group.reason,
                "left": 0, "right": 1, "action": ("command", fold_command(meta.group.id, meta.collapsed)), "group": "job-groups"}))

    # ---- history tab ------------------------------------------------------------------------------
    FIN_COLS = FIN_COLS

    def finished_columns(self, app, context):
        from .ui.table_schema import columns
        from raven.jobs.group_ui import has_collapsed
        cols = columns(app, context, self.FIN_COLS) if app else self.FIN_COLS
        if not app or not has_collapsed(app, context):
            return [column for column in cols if column.key != "info"]
        if not app.table_state.get("order", {}).get(context):
            info = [column for column in cols if column.key == "info"]
            cols = [column for column in cols if column.key != "info"]
            position = next((index + 1 for index, column in enumerate(cols) if column.key == "state"), len(cols))
            cols[position:position] = info
        return cols

    def finished_rows(self, fin: Sequence[Finished], width: int, title: str, cursor: Optional[int] = None, app=None,
                      *, header_cells=None, sort_tab=None) -> List[Row]:
        if not fin:
            return []
        sort_tab = sort_tab or ("recent" if title == "recent" else "history")
        data = [self.finished_dict(f, app=app, context=sort_tab) for f in fin]
        cols = self.finished_columns(app, sort_tab)
        drop = (("nodes", "exit", "start", "gpus", "part", "rss", "tags") if any(row.get("tags") for row in data)
                else ("tags", "nodes", "exit", "start", "gpus", "part", "rss"))
        rows, _ = table(cols, data, width, self.g.ascii, indent="   ", droppable=drop, cursor=cursor,
                        header_cells=header_cells)
        return [rule(self.g, width, title)] + rows

    def aligned_recent_rows(self, fin, width, job_cells, cursor=None, *, app, header_cells=None):
        """Keep Recents under the exact Queue columns, including its gutter.

        Completed records have no live progress, instantaneous utilization,
        or remaining time. Those slots stay empty rather than implying zero.
        Headings retain independent recent sorting while widths are shared.
        """
        from .column_resize import RECENT_ALIASES
        from .ui.table_schema import columns
        definitions = {column.key: column for column in self.FIN_COLS}
        definitions.update({column.key: column for column in columns(app, "recent", self.FIN_COLS)})
        original = {column.key: column for column in JOB_COLS}
        fitted = []
        for key, left, right in job_cells:
            alias = RECENT_ALIASES.get(key, "_blank_" + key)
            source = definitions.get(alias)
            title = source.title if source else ""
            if key == "st" and source and right - left < 5:
                title = source.title.replace("STATE", "ST", 1)
            fitted.append(Column(alias, title, right - left, right - left,
                                 original[key].align if key in original else "<"))
        data = [self.finished_dict(record, app=app, context="recent") for record in fin]
        if (not any(column.key == "tags" for column in fitted)
                and "tags" not in app.table_state.get("hidden", {}).get("jobs", ())):
            # A shared narrow Queue layout can drop TAGS. Keep the current
            # accounting badges visible in NAME, as the History table does,
            # without changing fitted widths or reviving explicitly hidden tags.
            from .research_text import clean
            data = [dict(row, name="[" + clean(row["tags"], self.g.ascii) + "] " + row["name"])
                    if row.get("tags") else row for row in data]
        state_width = next((column.hi for column in fitted if column.key == "state"), 99)
        if state_width < 5:
            abbreviations = {"COMPLETED": "CD", "FAILED": "F", "CANCELLED": "CA", "TIMEOUT": "TO",
                             "OUT_OF_MEMORY": "OOM", "NODE_FAIL": "NF", "PREEMPTED": "PR",
                             "BOOT_FAIL": "BF", "DEADLINE": "DL", "accounting...": "AC"}
            data = [dict(row, state=abbreviations.get(str(row.get("state", "")).partition(" ")[0],
                                                     row.get("state", ""))) for row in data]
        marks = {index for index, record in enumerate(fin) if record.id in app.marks}
        rows, _ = table(fitted, data, width, self.g.ascii, cursor=cursor, marks=marks,
                        mark_char=self.g.mark, header_cells=header_cells)
        return [rule(self.g, width, "recent")] + rows

    def finished_dict(self, f: Finished, *, app=None, context="history") -> dict:
        row = self._finished_dict(f)
        if app is not None:
            from raven.jobs.group_ui import finished_row
            return finished_row(app, context, row)
        return row

    def _finished_dict(self, f: Finished) -> dict:
        if isinstance(f, Job):
            return dict(fin=f, id=f.id, name=f.name, state="accounting...", part=f.partition,
                        elapsed=f.elapsed, cpus=f.cpus, gpus=f.gpus or "", ce="n/a", me="n/a",
                        rss="", start=when(f.start), end="pending", exit="", nodes=f.nodelist,
                        tags=" ".join(self.cfg_tags.get(f.id, {}).get("tags", [])) if hasattr(self, "cfg_tags") else "",
                        _styles={"state": "yellow", "end": "dim"})
        ok = f.state == "COMPLETED"
        style = "green" if ok else ("yellow" if f.state.startswith("CANCEL") else "red")
        ce, me = f.cpu_eff, f.mem_eff
        return dict(fin=f, id=f.id, name=f.name, state=f.state, part=f.partition, elapsed=f.elapsed, cpus=f.cpus, gpus=f.gpus or "", ce="n/a" if ce is None else f"{100 * ce:.0f}%",
                    me="n/a" if me is None else f"{100 * me:.0f}%", rss=human(f.rss) if f.rss else "", start=when(f.start), end=when(f.end), exit=f.exit, nodes=f.nodelist,
                    tags=" ".join(self.cfg_tags.get(f.id, {}).get("tags", [])) if hasattr(self, "cfg_tags") else "",
                    _styles={"state": style, "ce": ("red" if ce is not None and ce < self.th["cpu"] else ""), "me": ("yellow" if me is not None and me < self.th["mem"] else "")})

    def finished_summary(self, record, width):
        from .research_text import clean
        metadata = getattr(self, "cfg_tags", {}).get(record.id, {})
        tags = " ".join(metadata.get("tags", []))
        title = f"{record.id} {record.name}" + (f" / {tags}" if tags else "")
        rows = [[(" " + clean(title, self.g.ascii), "heading+bold")]]
        if metadata.get("note"):
            rows.append([(" Note: " + clean(metadata["note"], self.g.ascii), "dim")])
        if isinstance(record, Job):
            rows.append([(" Awaiting accounting; the final outcome is not known yet", "yellow")])
            rows.append([(" " + clean(f"Last observed {record.state} / {record.partition} / {record.elapsed}", self.g.ascii), "dim")])
        else:
            rows.append([(" " + clean(f"{record.state} / {record.partition} / elapsed {record.elapsed} / exit {record.exit or 'unknown'}", self.g.ascii),
                          "green" if record.state == "COMPLETED" else "yellow")])
            rows.append([(" " + clean(f"{record.cpus} CPUs / {record.gpus} GPUs / {record.nodes} nodes", self.g.ascii), "dim")])
            rows.append([(" Peak task RSS " + (human(record.rss) if record.rss else "unknown"), "dim")])
        rows.append([(" I inspector | l logs | :investigate " + str(record.id), "accent")])
        return [L.clip_row(row, width) for row in rows]

    def history_tab(self, snap: dict, app, width: int, height: Optional[int]) -> Tuple[List[Row], List[Tuple[int, str, str]]]:
        from .table_tools import date_label, record_page
        from .table_sort import chain, describe, header_hits
        self.cfg_tags = snap.get("tags", {})
        fin = app.sync_history_selection(snap)
        fin_columns = self.finished_columns(app, "history")
        key, rev = app.sort.get("history", "end"), app.reverse.get("history", False)
        n = len(fin)
        cur = app.clamp_cursor("history", n)
        from .ui.selection_state import selected
        app.selected_id = selected(app, "history", fin[cur].id if fin else None)
        counts: Dict[str, int] = {}
        observed = getattr(app, "history_all_records", fin)
        for f in observed:
            counts[f.state] = counts.get(f.state, 0) + 1
        core_h = sum(f.core_hours for f in observed)
        gpu_h = sum(f.gpu_hours for f in observed)
        effs = [f.cpu_eff for f in observed if f.cpu_eff is not None]
        interval_label = date_label(app) if getattr(app, "table_tools_state", {}).get("dates") else f"last {app.analytics_days_value():g} days"
        summary: Row = [(f" {interval_label}: {len(observed)} jobs  ", "bold")]
        for st, c in sorted(counts.items(), key=lambda kv: -kv[1]):
            summary.append((f"{st.lower()} {c}  ", "green" if st == "COMPLETED" else ("yellow" if st.startswith("CANCEL") else "red")))
        sort_label = describe(app, "history") if chain(app, "history") is not None else f"sorted by {key}{' (reversed)' if rev else ''}"
        summary.append((f"{self.g.dot} {core_h:.1f} core-hours {self.g.dot} {gpu_h:.1f} gpu-hours" + (f" {self.g.dot} mean cpu eff {100 * sum(effs) / len(effs):.0f}%" if effs else "") + f" {self.g.dot} {sort_label}", "dim"))
        prefix = [L.clip_row(summary, width)]
        if self.visual_room(width, height):
            prefix += self.composition([(state.lower(), count, "green" if state == "COMPLETED" else "yellow" if state.startswith("CANCEL") else "red")
                                        for state, count in sorted(counts.items(), key=lambda pair: -pair[1])], width)
        if height is None:
            rows = self.finished_rows(fin, width, "history", app=app)
            return prefix + rows, []
        from raven.jobs.list_controls import controls as list_controls
        controls, control_hits = list_controls(app, self.g, width, "history")
        control_hits = [(y + len(prefix), kind, value) for y, kind, value in control_hits]
        prefix += controls
        vis = max(1, height - len(prefix) - 2)
        top = app.scroll_to("history", cur, vis, n)
        shown = fin[top:top + vis]
        data = [self.finished_dict(f, app=app, context="history") for f in shown]
        cells = []
        marks = {i for i, record in enumerate(shown) if record.id in app.marks}
        drop = (("nodes", "exit", "start", "gpus", "part", "rss", "tags") if any(row.get("tags") for row in data)
                else ("tags", "nodes", "exit", "start", "gpus", "part", "rss"))
        trows, kept = table(fin_columns, data, max(1, width - 1), self.g.ascii, droppable=drop, cursor=cur - top if app.selected_id else None,
                         marks=marks, mark_char=self.g.mark,
                         header_cells=cells)
        if (any(row.get("tags") for row in data) and any(column.key == "tags" for column in fin_columns)
                and not any(column.key == "tags" for column in kept)):
            # The split pane can omit optional columns. Preserve visible tag
            # badges in the name cell, unless the user explicitly hid TAGS.
            from .research_text import clean
            tagged = [dict(row, name="[" + clean(row["tags"], self.g.ascii) + "] " + row["name"])
                      if row.get("tags") else row for row in data]
            cells.clear()
            trows, _ = table([column for column in kept if column.key != "_mark"], tagged,
                             max(1, width - 1), self.g.ascii,
                             cursor=cur - top if app.selected_id else None, marks=marks,
                             mark_char=self.g.mark, header_cells=cells)
        title = f"history {top + 1}-{min(n, top + vis)} of {n}" if n > vis else "history"
        out = prefix + [_scroll_rule(self.g, width, title)] + trows
        if not fin:
            out.append([("   nothing matches the filter; Esc clears it" if app.filter else "   No completed runs yet. Finished jobs and efficiency appear here.", "dim")])
        hits = control_hits + header_hits("history", cells, len(prefix) + 1) + [(len(prefix) + 2 + i, "fin", f.id) for i, f in enumerate(shown)]
        self.group_controls(app, out, hits, shown, len(prefix) + 2, "history", width, first_visible=True)
        _table_scrollbar(app, "history", width, len(prefix) + 2, vis, n, top, header=len(prefix))
        if fin and app.selected_id and not getattr(app, "job_panel_defer_content", False):
            out += [rule(self.g, width, "selected")] + self.finished_summary(fin[cur], width)
        record_page(app, "history", len(shown))
        return out, hits

    # ---- cluster tab ------------------------------------------------------------------------------
    def cluster_tab(self, snap: dict, app, width: int, height: Optional[int]) -> Tuple[List[Row], List]:
        from .table_query import matches
        from .ui.table_schema import columns
        from .table_tools import filter_text, select_resource
        from .table_sort import header_hits, sort_rows
        parts = list(snap["partitions"])
        want = set(self.cfg["partitions"]) or None
        mine = {j.partition for j in snap["jobs"]}
        if want is None and not self.cfg.get("show_all_partitions", False):
            parts = [p for p in parts if p.gpus or p.name in mine]
        elif want is not None:
            parts = [p for p in parts if p.name in want]
        text = filter_text(app, "cluster").casefold()
        if text:
            parts = [part for part in parts if text in part.name.casefold() or text in part.avail.casefold()]
        parts = [part for part in parts if matches(app, "cluster", part, snap)]
        rows = []
        for p in parts:
            na = p.nodes_aiot.split("/") if p.nodes_aiot else ["", "", "", ""]
            ca = p.cpus_aiot.split("/") if p.cpus_aiot else ["", "", "", ""]
            gp = "  ".join(f"{t} {v['free']}/{v['total'] - v['down']}" + (f" ({v['down']} down)" if v["down"] else "") for t, v in sorted(p.gpus.items()))
            rows.append(dict(name=p.name, avail=p.avail, limit=p.limit, nodes=p.nodes, nidle=na[1] if len(na) > 1 else "", nalloc=na[0], nother=na[2] if len(na) > 2 else "",
                             cidle=ca[1] if len(ca) > 1 else "", calloc=ca[0], gpus=gp, mine=sum(1 for j in snap["jobs"] if j.partition == p.name and not j.pending),
                             minep=sum(1 for j in snap["jobs"] if j.partition == p.name and j.pending),
                             _sort={"limit": secs(p.limit) if secs(p.limit) is not None else p.limit,
                                    "gpus": sum(v.get("free", 0) for v in p.gpus.values())},
                             _styles={"avail": "green" if p.avail == "up" else "red"}))
        cols = [Column("name", "PARTITION", 6, 14), Column("avail", "AVAIL", 4, 6), Column("limit", "LIMIT", 5, 12), Column("nodes", "NODES", 5, 6, ">"),
                Column("nidle", "IDLE", 4, 6, ">"), Column("nalloc", "ALLOC", 5, 6, ">"), Column("nother", "OTHER", 5, 6, ">"), Column("cidle", "CPUS IDLE", 9, 10, ">"),
                Column("calloc", "CPUS ALLOC", 10, 11, ">"), Column("mine", "MY RUN", 6, 6, ">"), Column("minep", "MY PEND", 7, 7, ">"), Column("gpus", "GPUS FREE/UP", 12, 60, flex=True)]
        rows = sort_rows(app, "cluster", rows)
        select_resource(app, "cluster", [row["name"] for row in rows])
        cells = []
        trows, _ = table(columns(app, "cluster", cols), rows, width, self.g.ascii, droppable=("nother", "calloc", "cidle", "limit"), header_cells=cells)
        out = [rule(self.g, width, "partitions")] + trows
        if not rows:
            out.append([("   Waiting for partition data. Open Sources to check sinfo or refresh with r.", "dim")])
        inv = snap["gpu_inventory"]
        if inv:
            out.append(rule(self.g, width, "GPUs cluster-wide (each node once)"))
            for t, v in sorted(inv.items()):
                up = v["total"] - v["down"]
                frac = v["used"] / up if up else 0
                if self.visual_room(width, height, minimum=22):
                    out.append([(f"   {t.upper():<8}", "bold"), (f"{v['total']} installed  ·  {up} available  ·  {v['used']} allocated", "dim")])
                    out += self.composition([("free", v["free"], "green"), ("allocated", v["used"], "cyan"), ("down / drained", v["down"], "red")], width)
                else:
                    out.append([(f"   {t:<6} ", "bold"), bar(self.g, frac, 24), (f"  {v['used']}/{up} in use, {v['free']} free" + (f", {v['down']} down or drained" if v["down"] else ""), "")])
        sh = snap["share"]
        if sh:
            out.append(rule(self.g, width, "fair share"))
            for s in sh[:4]:
                out.append([(f"   {s['account']:<16} fair share {s['fairshare']}  effective usage {s['usage']}", "")])
        acc = snap["account"]
        if acc:
            out.append([(f"   {acc['account']} right now: {acc['running']} running jobs using {acc['cpus']} cpus and {acc['gpus']} gpus, {acc['pending']} pending (everyone in the account)", "dim")])
        out += self.weather_rows(snap, width)
        out += self.budget_rows(snap, width)
        return out, header_hits("cluster", cells, 1) + [(2 + index, "partition_row", row["name"]) for index, row in enumerate(rows)]

    def weather_rows(self, snap: dict, width: int) -> List[Row]:
        """Queue weather: pending work ahead per partition cluster-wide and what sbatch --test-only projects for typical jobs."""
        g = self.g
        ahead, probes = snap.get("pending_ahead", {}), snap.get("weather", [])
        if not ahead and not probes:
            return []
        out: List[Row] = [rule(g, width, "queue weather (pending work ahead of a new job, and when a typical job would start)")]
        parts = [p.name for p in snap["partitions"]] or sorted(ahead)
        shown = [p for p in parts if p in ahead] + [p for p in sorted(ahead) if p not in parts]
        for p in shown[:8]:
            a = ahead[p]
            by = "  ".join(f"{t} {n}" for t, n in sorted(a["by_type"].items()))
            inv = next((x.gpus for x in snap["partitions"] if x.name == p), {})
            free = "  ".join(f"{t} {v['free']} free" for t, v in sorted(inv.items()))
            idle = next((x.nodes_aiot.split("/")[1] for x in snap["partitions"] if x.name == p and x.nodes_aiot.count("/") == 3), "")
            heavy = a["jobs"] >= 20 or (a["gpus"] and not any(v["free"] for v in inv.values()))
            out.append([(f"   {p:<10}", "bold"), (f"{a['jobs']:>4} pending jobs asking {a['cpus']} cpus" + (f", {a['gpus']} gpus ({by})" if a["gpus"] else ""), "yellow" if heavy else ""),
                        (f"   {g.dot} idle nodes {idle}" if idle else "", "dim"), (f"   {g.dot} {free}" if free else "", "dim")])
        now = clock.now()
        for pr in probes:
            what = f"{pr['cpus']} cpus, {pr['mem']}" + (f", {pr['gres']}" if pr["gres"] else "") + f", {pr['limit']}"
            if pr.get("ok") and pr.get("est"):
                t = stamp(pr["est"])
                wait = (t - now) if t else None
                soon = wait is not None and wait < 300
                text = f"would start {'now' if soon else 'in ' + compact(wait) + ' (' + when(pr['est']) + ')' if wait is not None else when(pr['est'])}" + (f" on {pr['nodes']}" if pr.get("nodes") else "")
                style = "green" if soon else ("yellow" if wait is not None and wait > 3600 else "")
            else:
                text, style = f"no projection: {cut(pr.get('text', ''), 70, g.ascii)}", "red"
            out.append([(f"   {pr['partition']:<10}", ""), (f"a job of {what} ", "dim"), (text, style)])
        return out

    def budget_rows(self, snap: dict, width: int) -> List[Row]:
        """The account's allocation: used this month against the limit, the burn rate of the last 7 days, the month-end projection."""
        g = self.g
        b = snap.get("budget", {})
        if not b:
            return []
        out: List[Row] = [rule(g, width, f"allocation of {b['account']} since {b['month']} (sreport; limits from sacctmgr)")]
        now = clock.now()
        day = int(time.strftime("%d", time.localtime(now)))
        import calendar
        days_in_month = calendar.monthrange(int(time.strftime("%Y", time.localtime(now))), int(time.strftime("%m", time.localtime(now))))[1]
        for k, label in (("cpu", "core-hours"), ("gpu", "gpu-hours")):
            used = b.get("used", {}).get(k, 0.0)
            week = b.get("week", {}).get(k, 0.0)
            lim = b.get("limit", {}).get(k)
            rate = week / 7.0
            proj = used + rate * max(0, days_in_month - day)
            seg: Row = [(f"   {label:<11}", "bold")]
            if lim:
                frac = used / lim
                seg += [bar(g, frac, 24), (f"  {used:,.0f} of {lim:,.0f} ({100 * frac:.0f}%)", "")]
                pf = proj / lim
                seg.append((f"  {g.dot} burning {rate:,.0f}/day, month-end projection {proj:,.0f} ({100 * pf:.0f}%)", "red" if pf > 1.0 else ("yellow" if pf > 0.9 else "dim")))
                if rate > 0 and lim > used:
                    seg.append((f"  {g.dot} exhausted in {(lim - used) / rate:.0f} days at this rate", "dim"))
            else:
                seg += [(f"{used:,.0f} used this month", ""), (f"  {g.dot} {rate:,.0f}/day over the last 7 days (no limit set)", "dim")]
            out.append(seg)
        users = b.get("by_user", {})
        if users:
            tops = sorted(users.items(), key=lambda kv: -(kv[1].get("cpu", 0) + 10 * kv[1].get("gpu", 0)))[:6]
            out.append([("   by user   ", "bold"), ("  ".join(f"{u} {v.get('cpu', 0):,.0f} cpu-h / {v.get('gpu', 0):,.0f} gpu-h" for u, v in tops), "dim")])
        return out

    # ---- nodes tab --------------------------------------------------------------------------------
    def nodes_tab(self, snap: dict, app, width: int, height: Optional[int]) -> Tuple[List[Row], List]:
        from .control_rows import buttons
        nav, nav_hits = buttons(self.g, width, [(key, title, ("command", "view " + key)) for key, title in NODES_VIEWS],
                               selected=app.nodes_view, group="nodes_nav", prefix="nodes-view:")
        if app.nodes_view == "map":
            rows = self.node_map(snap, app, width, None if height is None else max(0, height - len(nav)))
            return nav + rows, nav_hits + [(y + len(nav), kind, payload) for y, kind, payload in getattr(app, "node_map_hits", [])]
        rows, hits = self.my_nodes(snap, app, width, None if height is None else max(0, height - len(nav)))
        return nav + rows, nav_hits + [(y + len(nav), k, v) for y, k, v in hits]

    def node_map(self, snap: dict, app, width: int, height: Optional[int]) -> List[Row]:
        """Every node of the cluster as a cell per partition: state glyph, allocated cores, GPUs in use; mine marked."""
        g = self.g
        app.node_map_hits = []
        cells = list(snap.get("nodemap", {}).values())
        if not cells:
            return [rule(g, width, "cluster map"), [("   sinfo -N has not answered yet", "dim")]]
        mine = {h for j in snap["jobs"] if not j.pending for h in j.hosts}
        want = set(self.cfg["partitions"]) or None
        parts: Dict[str, list] = {}
        for c in cells:
            for p in c.partitions:
                if want is None or p in want:
                    parts.setdefault(p, []).append(c)
        order = [p.name for p in snap["partitions"] if p.name in parts] + sorted(p for p in parts if p not in {x.name for x in snap["partitions"]})
        legend: Row = [(" legend  ", "bold"), (g.empty + " idle ", "green"), (g.spark[3] + " mixed ", "yellow"), (g.full + " allocated ", "red"), ("x down/drained ", "magenta"),
                       (f"{g.mark} running one of mine", "cyan"), ("   cell: node  cores used/total  gpus used/total", "dim")]
        out: List[Row] = [legend]
        cell_w = 26
        per_row = max(1, (width - 3) // cell_w)
        for p in order:
            if height is not None and len(out) >= height:
                break
            nodes = sorted(parts[p], key=lambda c: c.name)
            n_idle = sum(1 for c in nodes if c.state.startswith("idle"))
            n_down = sum(1 for c in nodes if c.down)
            n_mine = sum(1 for c in nodes if c.name in mine)
            gp = sum(c.gpus for c in nodes)
            gu = sum(c.gpus_used for c in nodes)
            cores = sum(c.cpus for c in nodes)
            alloc = sum(c.cpus_alloc for c in nodes)
            title = f"{p}: {len(nodes)} node{'s' if len(nodes) != 1 else ''}, {n_idle} idle, {n_down} down/drained, {alloc}/{cores} cores allocated" + (f", {gu}/{gp} gpus in use" if gp else "") + (f", {n_mine} running mine" if n_mine else "")
            out.append(rule(g, width, title))
            for i in range(0, len(nodes), per_row):
                if height is not None and len(out) >= height:
                    break
                row: Row = [("  ", "")]
                capacity: Row = [("  ", "")]
                for c in nodes[i:i + per_row]:
                    if c.down:
                        glyph, style = "x", "magenta"
                    elif c.state.startswith("idle"):
                        glyph, style = g.empty, "green"
                    elif c.state.startswith("alloc") or c.state.startswith("comp"):
                        glyph, style = g.full, "red"
                    else:
                        glyph, style = g.spark[3], "yellow"
                    mark = g.mark if c.name in mine else " "
                    gpu = f" {c.gpus_used}/{c.gpus}g" if c.gpus else ""
                    text = f"{mark}{glyph} {cut(c.name, 9, g.ascii):<9}{c.cpus_alloc:>3}/{c.cpus:<3}{gpu}"
                    left = vlen(L.row_text(row))
                    rendered = pad(text, cell_w)
                    right = min(width, left + vlen(rendered))
                    if left < right:
                        app.node_map_hits.append((len(out), "node_cell", (c.name, left, right)))
                    row.append((rendered, ("cyan" if c.name in mine else style)))
                    if not g.ascii:
                        frac = c.cpus_alloc / c.cpus if c.cpus else None
                        capacity += [("  ", "")] + gradient_bar(g, frac, 12, "#34d399", "#22d3ee")
                        capacity.append((f" {100 * frac:>3.0f}%  " if frac is not None else "   ?   ", "dim"))
                        capacity += gradient_bar(g, c.gpus_used / c.gpus if c.gpus else None, 4, "#a78bfa", "#ec4899") + [(" ", "")]
                out.append(row)
                if not g.ascii and (height is None or len(out) < height):
                    out.append(capacity)
        if height is not None:
            out = out[:height]
        from .table_tools import select_resource
        select_resource(app, "nodes", list(dict.fromkeys(payload[0] for _, _, payload in app.node_map_hits)))
        return out

    def my_nodes(self, snap: dict, app, width: int, height: Optional[int]) -> Tuple[List[Row], List]:
        from .table_query import matches
        from .ui.table_schema import columns
        from .table_tools import filter_text, select_resource
        from .table_sort import chain, header_hits, sort_rows
        nodes = list(snap["nodes"].values())
        text = filter_text(app, "nodes").casefold()
        if text:
            nodes = [node for node in nodes if text in node.name.casefold() or text in node.state.casefold() or text in node.partitions.casefold()]
        nodes = [node for node in nodes if matches(app, "nodes", node, snap)]
        key, rev = app.sort.get("nodes", "name"), app.reverse.get("nodes", False)
        if chain(app, "nodes") is None:
            nodes.sort(key=(lambda n: n.name) if key == "name" else (lambda n: (n.load is None, -(n.load or 0))), reverse=rev)
        by_host: Dict[str, List[Job]] = {}
        for j in snap["jobs"]:
            for h in j.hosts:
                by_host.setdefault(h, []).append(j)
        rows = []
        for nd in nodes:
            jobs = by_host.get(nd.name, [])
            gsamp = []
            for j in jobs:
                for s in snap["gpu"].get(j.id) or []:
                    gsamp.append(s)
            measured_gpu = _gpu_mean(s.util for s in gsamp)
            gutil = f"{measured_gpu:.0f}%" if measured_gpu is not None else ""
            rows.append(dict(name=nd.name, state=nd.state, load=f"{nd.load:.1f}" if nd.load is not None else "n/a", cpus=f"{nd.alloc}/{nd.cpus}", loadpct=f"{100 * nd.load / nd.cpus:.0f}%" if nd.load is not None and nd.cpus else "n/a",
                             mem=f"{(nd.mem_total - nd.mem_free) / 1024:.0f}/{nd.mem_total / 1024:.0f} GB" if nd.mem_free is not None and nd.mem_total and nd.mem_free <= nd.mem_total else "n/a", gres=nd.gres.split("(")[0] if nd.gres and nd.gres != "(null)" else "",
                             gused=nd.gres_used.split("(")[0] if nd.gres_used and nd.gres_used != "(null)" else "", gutil=gutil,
                             _node=nd, _sort={"load": nd.load, "cpus": nd.alloc / nd.cpus if nd.cpus else None,
                                             "loadpct": nd.load / nd.cpus if nd.load is not None and nd.cpus else None,
                                             "mem": (nd.mem_total - nd.mem_free) * 1024 ** 2 if nd.mem_free is not None and nd.mem_total and nd.mem_free <= nd.mem_total else None,
                                             "gutil": measured_gpu},
                             jobs=" ".join(f"{j.id}({j.name})" for j in jobs), _styles={"state": "red" if any(k in nd.state.lower() for k in ("drain", "down", "fail")) else ""}))
        cols = [Column("name", "NODE", 6, 16), Column("state", "STATE", 5, 14), Column("cpus", "ALLOC/CPUS", 10, 10, ">"), Column("load", "LOAD", 4, 7, ">"), Column("loadpct", "LOAD%", 5, 5, ">"),
                Column("mem", "MEM USED", 8, 14, ">"), Column("gres", "GRES", 4, 16), Column("gused", "GRES USED", 9, 16), Column("gutil", "GPU%", 4, 4, ">"), Column("jobs", "MY JOBS", 7, 60, flex=True)]
        rows = sort_rows(app, "nodes", rows)
        select_resource(app, "nodes", [row["name"] for row in rows])
        nodes = [row["_node"] for row in rows]
        cells = []
        trows, _ = table(columns(app, "nodes", cols), rows, width, self.g.ascii, droppable=("gused", "gres", "loadpct"), header_cells=cells)
        out = []
        matrix_hits = []
        if nodes and self.visual_room(width, height, minimum=22):
            out += self.node_resource_rows(nodes, width)
            matrix_hits = [(2 + index, "node_row", node.name) for index, node in enumerate(nodes[:8])]
        header_y = len(out) + 1
        out += [rule(self.g, width, "nodes running your jobs")] + trows
        if not rows:
            out.append([("   no running jobs (or scontrol has not answered yet)", "dim")])
        for nd in nodes:
            jobs = by_host.get(nd.name, [])
            for j in jobs:
                for s in snap["gpu"].get(j.id) or []:
                    if s.node == nd.name or s.node.startswith("task"):
                        key = f"{j.id}:" + getattr(s, "device_key", f"{s.node}:{s.index}")
                        mean = snap["gpu_mean"].get(key)
                        util = _gpu_value(s.util)
                        util_text = f"{util:3.0f}%" if util is not None else "unknown"
                        _, memory_text = _gpu_memory(s)
                        out.append([(f"   {nd.name} gpu{s.index} ", ""), bar(self.g, util / 100 if util is not None else None, 20), (f" {util_text}  mem {memory_text}  ", ""),
                                    (spark(self.g, snap["hist_gpu"].get(key, []), 20), "cyan"), (f"  mean {'?' if mean is None else f'{mean:.0f}%'}  job {j.id}", "dim")])
        return out, matrix_hits + header_hits("nodes", cells, header_y) + [(header_y + 1 + index, "node_row", row["name"]) for index, row in enumerate(rows)]

    # ---- group tab --------------------------------------------------------------------------------
    GROUP_COLS = GROUP_COLS

    def group_tab(self, snap: dict, app, width: int, height: Optional[int]) -> Tuple[List[Row], List]:
        from .ui.selection_state import selected as selection_value
        from .ui.table_schema import columns
        from .table_tools import record_page
        from .table_sort import chain, describe, header_hits, sort_rows
        g = self.g
        jobs = list(snap.get("group", []))
        cascade = chain(app, "group")
        acc = snap.get("account", {})
        out: List[Row] = []
        user_hits = []
        if not jobs:
            app.group_ids, app.selected_id = [], None
            if height is not None:
                from raven.jobs.list_controls import controls as list_controls
                out, user_hits = list_controls(app, g, width, "group")
            out.append(rule(g, width, "group"))
            out.append([("   squeue -A has not answered yet (or the account is unknown: --account, or [account] in the config)" if not acc else "   nobody in the account has jobs", "dim")])
            return out, user_hits
        # per-user summary
        by_user: Dict[str, dict] = {}
        for j in jobs:
            d = by_user.setdefault(j.user or "?", dict(running=0, pending=0, cpus=0, gpus=0, nodes=0))
            if j.pending:
                d["pending"] += 1
            else:
                d["running"] += 1
                d["cpus"] += j.cpus
                d["gpus"] += j.gpus
                d["nodes"] += j.nodes
        tot_cpu = sum(d["cpus"] for d in by_user.values()) or 1
        out.append(rule(g, width, f"{acc.get('account', 'the account')}: {len(by_user)} users, {sum(d['running'] for d in by_user.values())} running, {sum(d['pending'] for d in by_user.values())} pending"))
        ordered_users = sorted(by_user.items(), key=lambda kv: (-kv[1]["cpus"], kv[0]))
        if self.visual_room(width, height):
            mix = [(u, d["cpus"], "cyan" if u == app.user else "magenta" if i % 2 else "blue") for i, (u, d) in enumerate(ordered_users[:6])]
            if len(ordered_users) > 6:
                mix.append(("other users", sum(d["cpus"] for _, d in ordered_users[6:]), "dim"))
            out += self.composition(mix, width)
        user_limit = len(ordered_users) if height is None else max(1, min(6, (height - len(out) - 5) // 2))
        for u, d in ordered_users[:user_limit]:
            style = "cyan" if u == app.user else ""
            meter = [bar(g, d["cpus"] / tot_cpu, 16)] if g.ascii else gradient_bar(g, d["cpus"] / tot_cpu, 16)
            user_hits.append((len(out), "user_drill", u))
            out.append([(f"   {u:<12}", "bold+" + style if style else "bold")] + meter +
                       [(f"  {d['running']:>3} running on {d['cpus']:>5} cpus" + (f", {d['gpus']} gpus" if d["gpus"] else "") + f", {d['nodes']} nodes   {d['pending']} pending", style)])
        if user_limit < len(ordered_users) and (height is None or height - len(out) >= 5):
            out.append([(f"   {len(ordered_users) - user_limit} more users in the account; the job table includes everyone", "dim")])
        # the table
        rows = []
        for j in jobs:
            from .table_query import matches
            if not matches(app, "group", j, snap):
                continue
            st = "PD" if j.pending else {"RUNNING": "R", "COMPLETING": "CG", "CONFIGURING": "CF", "SUSPENDED": "S"}.get(j.state, j.state[:2])
            sub = stamp(j.submit)
            info = (j.reason + (f" {g.dot} waited {compact(clock.now() - sub)}" if sub else "")) if j.pending else f"started {when(j.start) if j.start else ''}".strip()
            rows.append(dict(job=j, user=j.user, id=j.id, name=j.name, part=j.partition, st=st, where=j.nodelist if not j.pending else f"{j.nodes} node{'s' if j.nodes != 1 else ''}",
                             cpus=j.cpus, gpu=j.gpu_text, time=f"{j.elapsed}/{j.limit}" if not j.pending else f"-/{j.limit}", prio=j.priority, info=info,
                             _style="cyan" if j.user == app.user else ("dim" if j.pending else "")))
            if cascade:
                rows[-1]["_sort"] = job_sort_values(j, rows[-1], snap, (key for key, _ in cascade))
        key, rev = app.sort.get("group", "user"), app.reverse.get("group", False)
        keyfn = {"user": lambda r: (r["user"], r["job"].pending, r["id"]), "state": lambda r: (r["job"].pending, r["user"], r["id"]), "name": lambda r: (r["name"], r["id"]),
                 "id": lambda r: r["id"], "time": lambda r: -(r["job"].elapsed_s or -1), "priority": lambda r: -r["job"].priority}[key]
        if cascade is None:
            rows.sort(key=keyfn, reverse=rev)
        else:
            rows = sort_rows(app, "group", rows)
        flt = app.filter.lower()
        if flt:
            rows = [r for r in rows if flt in r["user"].lower() or flt in r["name"].lower() or flt in r["id"].lower() or flt in r["part"].lower()]
        from .table_ui import group_rows
        rows = group_rows(app, rows, snap, tab="group")
        n = len(rows)
        ids = [r["id"] for r in rows]
        previous = getattr(app, "group_ids", [])
        selected = app.selected_id
        if (previous != ids and selected in ids and selected in previous
                and app.cursor.get("group", 0) == previous.index(selected)):
            app.cursor["group"] = ids.index(selected)
        app.group_ids = ids
        cur = app.clamp_cursor("group", n)
        app.selected_id = selection_value(app, "group", app.group_ids[cur] if n else None)
        if height is None:
            trows, _ = table(columns(app, "group", self.GROUP_COLS), rows, width, g.ascii, droppable=("prio", "part", "gpu", "where", "st"))
            return out + [rule(g, width, "jobs")] + trows, []
        out = [L.clip_row(row, width) for row in out]
        from raven.jobs.list_controls import controls as list_controls
        controls, control_hits = list_controls(app, g, width, "group")
        user_hits += [(y + len(out), kind, value) for y, kind, value in control_hits]
        out += controls
        vis = max(1, height - len(out) - 2)
        top = app.scroll_to("group", cur, vis, n)
        shown = rows[top:top + vis]
        cells = []
        trows, _ = table(columns(app, "group", self.GROUP_COLS), shown, max(1, width - 1), g.ascii, droppable=("prio", "part", "gpu", "where", "st"), cursor=cur - top if app.selected_id is not None else None, header_cells=cells)
        title = f"jobs {top + 1}-{min(n, top + vis)} of {n}" if n > vis else "jobs"
        base = len(out) + 1
        sort_label = describe(app, "group") if chain(app, "group") is not None else f"sorted by {key}{' (reversed)' if rev else ''}"
        out += [_scroll_rule(g, width, title + f", {sort_label}")] + trows
        hits = user_hits + header_hits("group", cells, base) + [(base + 1 + i, "group", r["id"]) for i, r in enumerate(shown)]
        self.group_controls(app, out, hits, [r["job"] for r in shown], base + 1, "group", width, first_visible=True)
        _table_scrollbar(app, "group", width, base + 1, vis, n, top, header=base - 1)
        record_page(app, "group", len(shown))
        return out, hits

    # ---- deps tab ---------------------------------------------------------------------------------
    def deps_tab(self, snap: dict, app, width: int, height: Optional[int]) -> Tuple[List[Row], List]:
        """Dependency chains as trees; the cursor selects a job, and the actions apply to it and everything downstream."""
        from .ui.selection_state import selected
        g = self.g
        browser = getattr(app, "history_browser_state", {})
        view = browser.get("views", {}).get("deps", {})
        scoped = view.get("selected") if view.get("explicit") else None
        if scoped:
            record = app.job_record(scoped, snap)
            if record is None:
                app.selected_id = selected(app, "deps", scoped)
                return [rule(g, width, "dependencies / " + scoped), [(" This job is no longer in the current observations. Select another job in history.", "dim")]], []
            if record is not None:
                app.selected_id = selected(app, "deps", scoped)
                out = [rule(g, width, "dependencies / " + scoped)]
                if isinstance(record, Job):
                    graph = DepGraph(snap["jobs"], {f.id: f.name for f in snap["finished"]})
                    upstream, downstream = graph.upstream(scoped), graph.downstream(scoped)
                    out += [[(" " + record.name + " / " + record.state.lower(), "heading+bold")],
                            [(" Dependency: " + (record.dependency or "none reported"), "cyan")],
                            [(f" {len(upstream)} upstream / {len(downstream)} downstream", "dim")]]
                    out += [[(" " + jid, "cyan")] for jid in upstream + downstream]
                else:
                    out += self.finished_summary(record, width)
                    details = snap.get("details", {}).get(scoped, {})
                    dependency = details.get("Dependency")
                    out += [[(" Dependency: " + (str(dependency) if dependency else "not retained in accounting"), "dim")],
                            [(" Historical dependency edges require saved scheduler metadata.", "dim")]]
                if getattr(app, "deps_scope_job", None) != scoped:
                    app.deps_scope_job, app.deps_scope_top = scoped, 0
                app.deps_scope_count = len(out) - 1
                rect = getattr(app, "history_browser_content_rect", None)
                actual_height = min(height, rect.height) if height is not None and rect is not None else height
                app.deps_scope_page = max(1, (actual_height or len(out)) - 1)
                if height is not None:
                    from .scrolling import viewport
                    top = viewport(app, "deps:scope", getattr(app, "deps_scope_top", 0),
                        app.deps_scope_count, app.deps_scope_page, context=(scoped, width, actual_height))
                    out = out[:1] + out[1 + top:1 + top + app.deps_scope_page]
                    out[0] = _scroll_rule(g, width, "dependencies / " + scoped)
                    SB.register(app, "deps:scope", (1, 0, 1 + app.deps_scope_page, width),
                        app.deps_scope_count, app.deps_scope_page, getattr(app, "deps_scope_top", 0), top,
                        lambda value: setattr(app, "deps_scope_top", value),
                        context=(scoped,), header=(0, 0, width) if width >= 6 else None)
                return [L.clip_row(row, max(1, width - 1) if index else width)
                        for index, row in enumerate(out)], []
        names = {f.id: f"{f.name} {f.state.lower()}" for f in snap["finished"]}
        graph = DepGraph(snap["jobs"], names)
        jobs = graph.jobs
        related_ids = graph.related()
        trees = graph.trees()
        from raven.jobs.group_domain import project_records
        records_by_id = {record.id: record for record in list(snap.get("finished", ())) + list(snap.get("departed_jobs", {}).values()) + list(snap["jobs"])}
        records = [records_by_id.get(jid) for tree in trees for _, _, jid in tree]
        others = [j for j in snap["jobs"] if j.id not in related_ids]
        grouped = project_records(app, snap, [record for record in records if record is not None] + others, tab="deps")
        displayed = {record.id for record in grouped}
        out: List[Row] = [rule(g, width, f"dependency chains: {len(graph.edges)} edges among {len(related_ids)} jobs (c cancels a job and everything downstream, h releases a held chain)")]
        if self.visual_room(width, height) and trees:
            related = [jobs[jid] for jid in related_ids if jid in jobs]
            held = sum(j.held for j in related)
            out += self.composition([("running", sum(not j.pending for j in related), "green"),
                                     ("waiting", sum(j.pending for j in related) - held, "yellow"),
                                     ("held", held, "magenta")], width)
        prefix_length = len(out)
        app.dep_ids = []
        if not trees:
            out.append([("   no job depends on another (the Dependency field of squeue is empty for all of yours)", "dim")])
        hits = []
        cur = app.clamp_cursor("deps", sum(record is None or record.id in displayed for record in records))
        k = 0
        for tree in trees:
            for depth, kind, jid in tree:
                if jid in records_by_id and jid not in displayed:
                    continue
                j = jobs.get(jid)
                if j is None:
                    desc, style = names.get(jid, "no longer in the queue"), "dim"
                elif j.pending:
                    desc = f"pending ({j.reason})" + (f" {g.dot} est {when(j.est_start)}" if j.est_start not in ("N/A", "", "Unknown") else "")
                    style = "yellow" if j.held or j.reason == "Dependency" else ""
                else:
                    desc, style = f"{j.state.lower()} {j.elapsed} of {j.limit} on {j.nodelist}", "green" if j.state == "RUNNING" else ""
                name = j.name if j else ""
                max_indent = min(30, max(0, width // 4))
                if depth and 3 * (depth - 1) > max_indent:
                    branch = cut(("..." if g.ascii else "…") + str(depth) + " ", max_indent, g.ascii) + " + "
                else:
                    branch = ("" if depth == 0 else "   " * (depth - 1) + ("  " + (g.box[2] if not g.ascii else "+") + (g.box[4] if not g.ascii else "-") + " "))
                kind_t = (kind + " " + (g.arrow + " ") if kind else "")
                row: Row = [("   " + branch, "dim"), (kind_t, "magenta"), (f"{jid} ", "cyan"), (pad(cut(name, 20, g.ascii), 20), "bold"), ("  " + desc, style)]
                from raven.jobs.group_domain import metadata_for_record
                meta = metadata_for_record(app, "deps", jid)
                if meta is not None and meta.header and meta.collapsed:
                    from raven.jobs.group_ui import summary_row
                    label = f"   {jid} {meta.group.label} / {meta.visible_count} records "
                    row = [(cut(label, width, g.ascii), "accent+bold")] + summary_row(meta.stats or meta.records,
                        max(0, width - L.vlen(label)), ascii_=g.ascii)
                if jid in app.marks:
                    row.insert(0, (" " + g.mark, "magenta"))
                if k == cur and height is not None and selected(app, "deps", True):
                    row = [(t, "rev") for t, _ in row]
                out.append(row)
                hits.append((len(out) - 1, "dep", jid))
                app.dep_ids.append(jid)
                k += 1
            out.append([("", "")])
        app.selected_id = None
        if app.dep_ids and cur < len(app.dep_ids):
            app.selected_id = selected(app, "deps", app.dep_ids[cur])
        if app.selected_id is not None:
            sel = app.selected_id
            down, up = graph.downstream(sel), graph.upstream(sel)
            out.append([(f"   selected {sel}: {len(up)} upstream, {len(down)} downstream" + (f" ({', '.join(down[:8])})" if down else ""), "dim")])
            j = jobs.get(sel)
            if j and j.pending:
                out.append([("   waits for " + ", ".join(graph.blocked_by(sel)), "yellow")])
        if others:
            out.append(rule(g, width, f"{len(others)} independent jobs"))
            for record in others:
                if record.id not in displayed:
                    continue
                from raven.jobs.group_domain import metadata_for_record
                meta = metadata_for_record(app, "deps", record.id)
                label = f"   {record.id} {record.name}"
                row = [(cut(label, width, g.ascii), "dim")]
                if meta is not None and meta.header and meta.collapsed:
                    from raven.jobs.group_ui import summary_row
                    label = f"   {record.id} {meta.group.label} / {meta.visible_count} records "
                    row = [(cut(label, width, g.ascii), "accent+bold")] + summary_row(meta.stats or meta.records,
                        max(0, width - L.vlen(label)), ascii_=g.ascii)
                out.append(row)
                self.group_controls(app, out, hits, [record], len(out) - 1, "deps", width)
        for y, _, jid in list(hits):
            if not isinstance(jid, str):
                continue
            record = records_by_id.get(jid)
            if record is not None:
                self.group_controls(app, out, hits, [record], y, "deps", width)
        if height is not None:
            # Keep the selected dependency visible while retaining its position in the
            # full graph for keyboard actions and chain confirmations.
            visible = max(1, height - prefix_length)
            selected_row = hits[cur][0] - prefix_length if hits else 0
            count = max(0, len(out) - prefix_length)
            top = app.scroll_to("deps", selected_row, visible, count)
            out = out[:prefix_length] + out[prefix_length + top:prefix_length + top + visible]
            out[0] = _scroll_rule(g, width, f"dependency chains: {len(graph.edges)} edges among {len(related_ids)} jobs (c cancels a job and everything downstream, h releases a held chain)")
            out = [row if index < prefix_length else L.clip_row(row, max(1, width - 1))
                   for index, row in enumerate(out)]
            hits = [(y - top, kind, key) for y, kind, key in hits if prefix_length + top <= y < prefix_length + top + visible]
            _table_scrollbar(app, "deps", width, prefix_length, visible, count, top)
        return out, hits

    # ---- log tab ----------------------------------------------------------------------------------
    def log_candidates(self, app, j: Job, kv: dict) -> List[str]:
        """The other files of the job's log directory that carry its id: array tasks, step outputs, the GPU trace."""
        import time as _t
        cache = app.logs.candidates.get(j.id)
        if cache and _t.time() - cache[0] < 30:
            return cache[1]
        from .job_logs.catalog import build_catalog
        streams = (stdout_path(j, kv, self.files), stdout_path(j, kv, self.files, "StdErr"))
        result = build_catalog(j.id, *streams, files=self.files)
        out = sorted(entry["path"] for entry in result["entries"] if entry["path"] not in streams)
        app.logs.candidates[j.id] = (_t.time(), out)
        return out

    def log_path(self, app, j: Job, kv: dict) -> Tuple[str, str]:
        """(the file the Log tab shows, a label): stdout, stderr, or one of the other files."""
        probe = not app.interactive
        if app.logs.entry:
            entry = app.logs.entry
            if entry["path"] == stdout_path(j, kv, self.files, probe=probe):
                app.logs.which = "out"
            elif entry["path"] == stdout_path(j, kv, self.files, "StdErr", probe=probe):
                app.logs.which = "err"
            return entry["path"], entry["label"]
        cands = app.logs.candidates.get(j.id, (0, []))[1]
        if app.logs.file_index > 0 and cands:
            i = (app.logs.file_index - 1) % len(cands)
            return cands[i], f"file {i + 2}/{len(cands) + 1}"
        app.logs.file_index = 0
        if app.logs.which == "err":
            err = stdout_path(j, kv, self.files, "StdErr", probe=probe)
            same = err and err == stdout_path(j, kv, self.files, probe=probe)
            return err, "stderr (the same file as stdout)" if same else "stderr"
        return stdout_path(j, kv, self.files, probe=probe), "stdout"

    def log_manifest_path(self, app, job, kv):
        settings = self.cfg["logs"]
        if not isinstance(settings, dict) or not isinstance(settings.get("manifest_file", ""), str):
            raise ValueError("logs.manifest_file must be a path string")
        path = settings.get("manifest_file", "")
        if not path:
            return ""
        path = path.replace("{job_id}", job.id)
        if os.path.isabs(path):
            return path
        research = app.research.settings if app.research else self.cfg["research"]
        explicit = research.get("workdir", "")
        wd = explicit or kv.get("WorkDir") or getattr(job, "workdir", "")
        if not wd or (self.files.remote and not os.path.isabs(wd)) or (not explicit and not os.path.isabs(wd)):
            raise ValueError("Select --workdir or a known job WorkDir to resolve logs.manifest_file")
        return os.path.abspath(os.path.join(wd, path)) if not self.files.remote else os.path.normpath(os.path.join(wd, path))

    def pager_path(self, snap, app):
        """The pager uses the same selected file as the Log view, including stderr and custom locations."""
        job = app.log_target(snap) if app.tab == "log" else app.job_record(app.selected_id, snap)
        if job is None:
            return app.resolve_log_path() if app.tab == "log" else ""
        details = snap["details"].get(job.id, {})
        return self.log_path(app, job, details)[0] if app.tab == "log" else stdout_path(job, details, self.files)

    def log_browser(self, snap, app, job, kv, width, height):
        from .job_logs.workbench import render_browser
        if job is None:
            from .project_ui import selected_binding, log_entries
            binding = selected_binding(app) or {}
            app.logs.entries = log_entries(app)
            prefix = [rule(self.g, width, f"Run {binding.get('run_id', '?')} log files")]
            return render_browser(self, snap, app, width, height, prefix, [])
        from .job_logs.catalog import LogCatalog
        from .research_text import clean
        if app.logs.catalog is None:
            app.logs.catalog = LogCatalog(self.files)
        from .cadence import multiplier
        app.logs.catalog.polling_multiplier = multiplier(app)
        messages = []
        try:
            manifest = self.log_manifest_path(app, job, kv)
        except ValueError as exc:
            manifest = ""
            messages.append(str(exc))
        result = app.logs.catalog.request(job.id, stdout_path(job, kv, self.files, probe=not app.interactive),
                    stdout_path(job, kv, self.files, "StdErr", probe=not app.interactive), manifest_file=manifest,
                    worker=app.research, wait=height is None)
        previous = app.log_entries()
        selected_id = previous[app.logs.browser_cursor]["id"] if 0 <= app.logs.browser_cursor < len(previous) else None
        grouped = {}
        for entry in result.get("entries", []):
            grouped.setdefault(entry["group"], []).append(entry)
        app.logs.entries = [entry for group in grouped.values() for entry in group]
        entries = app.log_entries()
        if selected_id in [entry["id"] for entry in entries]:
            app.logs.browser_cursor = next(i for i, entry in enumerate(entries) if entry["id"] == selected_id)
        messages += result.get("messages", [])
        app.logs.browser_cursor = max(0, min(app.logs.browser_cursor, max(0, len(entries) - 1)))
        prefix = [rule(self.g, width, clean(f"{job.id} {job.name} {self.g.dot} log files", self.g.ascii)),
                  [(f" {len(entries)} of {len(app.logs.entries)} files {self.g.dot} Enter opens {self.g.dot} Esc returns {self.g.dot} / filters", "dim")]]
        for message in messages[:2]:
            prefix.append([(" " + cut(clean(message, self.g.ascii), max(0, width - 1), self.g.ascii), "yellow")])
        if not entries:
            message = "Waiting for the background log index reader." if result.get("status") == "loading" else "No matching log files. Declare extra locations in logs.json; r refreshes."
            return render_browser(self, snap, app, width, height, prefix + [[(" " + message, "dim")]], [])
        groups = {}
        for index, entry in enumerate(entries):
            groups.setdefault(entry["group"], []).append((index, entry))
        body, hits, selected_row = [], [], 0
        for group, items in groups.items():
            body.append([(" " + clean(group, self.g.ascii), "cyan+bold")])
            for index, entry in items:
                selected = index == app.logs.browser_cursor
                if selected:
                    selected_row = len(body)
                hits.extend([(len(body), "log_file", entry["id"]), (len(body) + 1, "log_file", entry["id"])])
                marker = "> " if self.g.ascii else "▸ "
                body.append([("   " + (marker if selected else "  ") + clean(entry["label"], self.g.ascii), "rev+bold" if selected else "")])
                body.append([("     " + clean(entry["path"], self.g.ascii), "dim")])
        if height is None:
            return render_browser(self, snap, app, width, height, prefix + body, [(y + len(prefix), kind, key) for y, kind, key in hits])
        visible = max(1, height - len(prefix))
        top = app.logs.browser_top
        if selected_row < top:
            top = selected_row
        if selected_row + 1 >= top + visible:
            top = max(0, selected_row + 2 - visible)
        top = max(0, min(top, max(0, len(body) - visible)))
        app.logs.browser_top, app.logs.browser_page = top, max(1, visible // 2)
        return render_browser(self, snap, app, width, height, prefix + body[top:top + visible], [(y - top + len(prefix), kind, key) for y, kind, key in hits if top <= y < top + visible])

    def log_tab(self, snap: dict, app, width: int, height: Optional[int]) -> Tuple[List[Row], List]:
        _jid = app.log_job or app.selected_id
        j = app.log_target(snap)
        from .project_ui import selected_binding, log_entries, resolve_log_entry
        from .job_logs.workbench import apply_citation, display_line as pan_line, status_label
        binding = selected_binding(app) if j is None else None
        if j is None and not binding:
            return [rule(self.g, width, "log"), [("   select a job in Jobs, Recents, or History, then press l", "dim")]], []
        kv = snap["details"].get(j.id, {}) if j else {}
        if app.logs.browser:
            return self.log_browser(snap, app, j, kv, width, height)
        if j is None:
            entries = log_entries(app)
            app.logs.entries = entries
            chosen = resolve_log_entry(app)
            path, label = (chosen["path"], chosen.get("label", "Log")) if chosen else ("", "Log")
        else:
            path, label = self.log_path(app, j, kv)
        g = self.g
        page = max(1, (height - 2) if height else 40)       # the rule and the status line, then the page
        app.logs.page = page
        buf = app.read_log_buffer(path) if path else None
        if buf is not None and not getattr(buf, "loading", False):
            apply_citation(app, buf)
        app.log_render_token = (app.logs.path, app.logs._buffer_token) if buf is not None and not buf.error and not getattr(buf, "loading", False) else None
        cands = app.logs.candidates.get(j.id, (0, []))[1] if j else []
        identity = f"{j.id} {j.name}" if j else f"Run {binding['run_id']} / " + (f"job {binding['job_id']} (accounting unavailable)" if binding.get("job_id") else "job not recorded")
        head = f"{identity} {g.dot} {label}" + (f" {g.dot} o: {len(cands)} other file{'s' if len(cands) != 1 else ''}" if cands else "") + f" {g.dot} {path or 'path not known yet'}"
        out = [_scroll_rule(g, width, cut(head, width - 8, g.ascii)) if height is not None else rule(g, width, cut(head, width - 8, g.ascii))]
        if buf is None:
            app.logs.path, app.logs.match = "", None
            message = kv.get("LogPathError") or ("Log path unavailable for this finished job; checking its recorded scheduler/accounting paths."
                                                 if isinstance(j, Finished) else "Log path not known yet; checking scontrol show job.")
            return out + [[("   " + message, "dim")]], []
        if buf.error:
            return out + [[(f"   {buf.error}", "red")]], []
        if getattr(buf, "loading", False):
            return out + [[("   Reading the selected log in the background.", "dim")]], []
        total = buf.total
        scroll_context = (path, buf.ident, buf.skipped_bytes, width, app.logs.wrap)
        from .scrolling import viewport as scroll_viewport, active as scrolling_active
        target = max(0, total - page) if app.logs.top is None else app.logs.top
        painted_top = scroll_viewport(app, "logs:document", target, total, page,
                                     context=scroll_context,
                                     immediate=app.logs.following or app.logs.selection_active or height is None)
        lines, start = buf.window(None if app.logs.following else painted_top, page)
        search = app.logs.search
        from .job_logs.tools import retained_matches, count_retained
        marks = set(app.logs.bookmarks.get(path, []))
        pos = f"lines {start + 1}-{start + len(lines)} of {total}" if total else "empty file"
        pct = f" ({100 * (start + len(lines)) // total}%)" if total else ""
        status: Row = [(f" {pos}{pct} {g.dot} {buf.size / 1024 ** 2:.1f} MB", "dim"),
                       (f" {g.dot} following", "green") if app.logs.following else (f" {g.dot} paused (End or f follows)", "yellow")]
        if app.logs.wrap:
            status.append((f" {g.dot} wrapped", "dim"))
        if buf.truncated:
            status.append((f" {g.dot} the first {buf.skipped_bytes / 1024 ** 2:.0f} MB are not loaded (log_max_mb)", "yellow"))
        if search:
            count = count_retained(app, buf)
            omitted = app.log_tools_state.get("retained_omitted", 0)
            indexing = app.log_tools_state.get("retained_pending")
            known = app.log_tools_state.get("retained_known_count")
            result_label = ("indexing in background" if known is None else f"updating; previous {known} matches") if indexing else f"{count} lines"
            status.append((f" {g.dot} search '{search}': {result_label} (N / P next / previous)" +
                           (f"; {omitted} oversized regex lines omitted" if omitted else ""), "magenta"))
        if marks:
            status.append((f" {g.dot} {len(marks)} bookmark{'s' if len(marks) != 1 else ''} (' jumps)", "cyan"))
        if not g.ascii and width >= 100 and total:
            status = [(" ", "")] + gradient_bar(g, (start + len(lines)) / total, 12) + [(" ", "")] + status
        extra_status = status_label(app)
        if extra_status:
            # Keep the cause of hidden prefixes and its reset command visible,
            # even when the rest of the status is clipped by a narrow terminal.
            status.insert(0, (f" {extra_status} {g.dot}", "yellow+bold"))
        out.append(status)
        def render_lines(source_lines, first):
            body = []
            line_width = width
            rail_width = 1 if height is not None and width >= 4 else 0
            numbers = not g.ascii and width >= 64
            number_width = max(5, len(str(total))) if numbers else 0
            content_width = max(1, line_width - rail_width - (number_width + 5 if numbers else 2))
            for i, line in enumerate(source_lines):
                idx = first + i
                line = pan_line(app, line)
                display_budget = max(1, content_width * page * 2)
                clipped_prefix = app.logs.wrap and app.logs.following and len(line) > display_budget
                display_line = line[-display_budget:] if clipped_prefix else line[:display_budget]
                selected = app.logs.is_selected(idx)
                if selected:
                    style = "sel"
                elif search and (idx == app.logs.match or retained_matches(app, display_line,
                     source_bytes=buf._line_bytes[idx] if idx < len(buf._line_bytes) else len(buf._partial_raw))):
                    style = "sel" if idx == app.logs.match else "yellow"
                elif LOG_ERROR.search(display_line):
                    style = "red"
                elif LOG_WARNING.search(display_line):
                    style = "yellow"
                elif LOG_SUCCESS.search(display_line):
                    style = "green"
                else:
                    style = ""
                mark = g.mark if idx in marks else " "
                gutter = [(mark, "cyan"), (f"{idx + 1:>{number_width}} │ ", "dim")] if numbers else [(mark, "cyan")]
                chunks = []
                skipped_prefix = False
                if app.logs.wrap:
                    # Rendering a gigantic unbroken line still allocates at most a page.
                    skipped_prefix = clipped_prefix
                    current = display_line
                    while current and len(chunks) < page * 2:
                        chunk = L.truncate(current, content_width)
                        consumed = len(chunk) if chunk else 1
                        chunks.append(chunk or "?")
                        current = current[consumed:]
                    chunks = chunks or [""]
                    if app.logs.following:
                        skipped_prefix = skipped_prefix or len(chunks) > page
                        chunks = chunks[-page:]
                else:
                    chunks = [cut(display_line, content_width, g.ascii)]
                for part, text in enumerate(chunks):
                    continuation = [(" " * (number_width + 1) + " │ ", "dim")] if numbers else [(" ", "cyan")]
                    row = (gutter if part == 0 and not skipped_prefix else continuation) + [(text, style)]
                    row = L.clip_row(row, max(0, line_width - 1 - rail_width))
                    symbol = ("*" if g.ascii else "◆") if selected else ((">" if g.ascii else "›") if idx == app.logs.cursor else " ")
                    if symbol.strip():
                        row += [(" " * max(0, line_width - 1 - vlen(L.row_text(row))), style),
                                (symbol, "fg:#fb923c+bold" if selected else "cyan+bold")]
                    body.append((row, idx))
                    if not app.logs.following and len(body) >= page:
                        return body
                if app.logs.following and len(body) > page:
                    body = body[-page:]
            return body[-page:] if app.logs.following else body[:page]
        body = render_lines(lines, start)
        if (app.logs.cursor is not None and not app.logs.following
                and (not scrolling_active(app) or app.logs.selection_active)
                and SB.manual(app, "logs:document", context=scroll_context) is None
                and not any(idx == app.logs.cursor for _, idx in body)):
            # Long wrapped predecessors must not conceal the keyboard-selected line.
            app.logs.top = app.logs.cursor
            lines, start = buf.window(app.logs.cursor, max(1, min(page, total - app.logs.cursor)))
            body = render_lines(lines, start)
        out += [row for row, _ in body]
        if not lines:
            out.append([("   This log file is empty." if isinstance(j, Finished) else "   No output yet. This view updates as the job writes to its log.", "dim")])
        if height is not None and width >= 4:
            SB.register(app, "logs:document", (2, 0, 2 + page, width - 1), total, page,
                max(0, total - page) if app.logs.top is None else app.logs.top, start,
                lambda value: setattr(app.logs, "top", value), context=scroll_context,
                header=(0, 0, width) if width >= 6 else None)
        return out, [(2 + i, "log_line", str(idx)) for i, (_, idx) in enumerate(body)]

    # ---- sources tab ------------------------------------------------------------------------------
    def sources_tab(self, snap: dict, app, width: int, height: Optional[int]) -> Tuple[List[Row], List]:
        from .table_query import matches
        from .ui.table_schema import columns
        from .table_tools import filter_text, record_page, select_resource
        from .table_sort import chain, header_hits, sort_rows
        hs = list(snap["health"].values())
        text = filter_text(app, "sources").casefold()
        if text:
            hs = [health for health in hs if text in health.name.casefold() or text in health.error.casefold()]
        hs = [health for health in hs if matches(app, "sources", {"name": health.name, "state": "off" if not health.enabled else "error" if health.error else "ok" if health.last_ok else "pending"}, snap)]
        if chain(app, "sources") is None:
            hs.sort(key=lambda h: h.name, reverse=app.reverse.get("sources", False))
        n = len(hs)
        previous = getattr(app, "source_ids", [])
        old_cursor = app.cursor.get("sources", 0)
        chosen = previous[old_cursor] if 0 <= old_cursor < len(previous) else None
        now = clock.now()
        rows = []
        from .refresh_rate import cadence
        from .cadence import format_interval
        for h in hs:
            state = "off" if not h.enabled else ("error" if h.error else ("ok" if h.last_ok else "pending"))
            every = cadence(app, h.name)
            rows.append(dict(name=h.name, state=state, every=format_interval(every, ascii_=self.g.ascii), last=short_duration(now - h.last_ok) + " ago" if h.last_ok else "never",
                             latency=f"{h.latency_ms:.0f} ms" if h.latency_ms else "", calls=h.calls, errors=h.errors, backoff=f"{h.backoff:.0f}s" if h.backoff else "",
                             _sort={"every": every,
                                    "last": now - h.last_ok if h.last_ok else None,
                                    "latency": h.latency_ms if h.calls else None,
                                    "backoff": h.backoff},
                             error=h.error, _styles={"state": {"ok": "green", "error": "red", "off": "dim", "pending": "yellow"}[state]}))
        rows = sort_rows(app, "sources", rows)
        app.source_ids = [row["name"] for row in rows]
        select_resource(app, "sources", app.source_ids)
        # Sources has no selected job ID: its identity is the name under the
        # current cursor in the last rendered order, including intervening keys.
        if previous != app.source_ids and chosen in app.source_ids:
            app.cursor["sources"] = app.source_ids.index(chosen)
        cur = app.clamp_cursor("sources", n)
        cols = [Column("name", "SOURCE", 6, 12), Column("state", "STATE", 5, 7), Column("every", "EVERY", 5, 6, ">"), Column("last", "LAST OK", 7, 12, ">"),
                Column("latency", "LATENCY", 7, 8, ">"), Column("calls", "CALLS", 5, 6, ">"), Column("errors", "ERRORS", 6, 6, ">"), Column("backoff", "BACKOFF", 7, 7, ">"),
                Column("error", "LAST ERROR", 10, 80, flex=True)]
        from .control_rows import buttons
        prefix, prefix_hits = buttons(self.g, width,
            [("telemetry", "Metric sampling", ("command", "telemetry")),
             ("gpu-provider", "GPU source", ("command", "gpuprovider")),
             ("doctor", "Slurm doctor", ("command", "ops slurm-doctor")),
             ("clusters", "Cluster workspace", ("command", "ops clusters")),
             ("storage", "Storage readiness", ("command", "ops storage"))],
            group="source-tools", prefix="source-tools:")
        if height is not None and height < len(prefix) + 3:
            # Keep the selected source visible in a short pane. These tools
            # remain available through the persistent toolbar and commands.
            prefix, prefix_hits = [], []
        if self.visual_room(width, height):
            enabled = [h for h in hs if h.enabled]
            prefix += self.composition([("healthy", sum(bool(h.last_ok) and not h.error for h in enabled), "green"),
                                        ("error", sum(bool(h.error) for h in enabled), "red"),
                                        ("waiting", sum(not h.last_ok and not h.error for h in enabled), "yellow"),
                                        ("off", len(hs) - len(enabled), "dim")], width)
        vis = len(rows) if height is None else max(1, height - len(prefix) - 2)
        top = app.scroll_to("sources", cur, vis, n) if height is not None else 0
        shown = rows[top:top + vis]
        cells = []
        trows, _ = table(columns(app, "sources", cols), shown, max(1, width - 1) if height is not None else width, self.g.ascii, cursor=cur - top if height else None,
                         droppable=("backoff", "calls", "errors", "every", "error"), header_cells=cells)
        title = f"sources {top + 1}-{min(n, top + vis)} of {n}" if n > vis else "sources"
        out = prefix + [_scroll_rule(self.g, width, title + f" ({app.keys_help('source_toggle')} enables / disables the selected one)") if height is not None else rule(self.g, width, title)] + trows
        if not hs:
            out.append([("   Waiting for the first sample. Source health appears here automatically.", "dim")])
        out.append([("", "")])
        out.append([("   jobs: squeue (your jobs)   starts: squeue --start   live: sstat (CPU time, peak memory)   gpu: allocation-scoped vendor tools", "dim")])
        out.append([("   nodes: scontrol show node   partitions: sinfo   finished: sacct   share: sshare   account: squeue -A   details: scontrol show job (the selected job)", "dim")])
        scheduler = getattr(getattr(app, "actions", None), "slurm", None)
        selected_source = app.source_ids[cur] if 0 <= cur < len(app.source_ids) else ""
        reader = getattr(scheduler, "reader_status", {}).get(selected_source, {})
        if reader:
            note = f"   Reader: {reader.get('format', 'unknown')} | {reader.get('parser') or reader.get('reason', '')}"
            out.append([(display_text(note), "dim")])
        accounting = getattr(scheduler, "accounting_diagnostics", {}).get(selected_source, {})
        if accounting:
            out.append([(display_text(f"   Accounting: {accounting['start']} -> {accounting['end']} | {accounting['commands']} commands | {accounting['window']}"), "dim")])
        transport = getattr(scheduler, "b", None)
        connection_note = getattr(transport, "control_diagnostic", "")
        if not connection_note:
            connection_note = getattr(getattr(getattr(app, "files", None), "ssh", None), "control_diagnostic", "")
        if connection_note:
            out.append([(display_text("   Connection: " + str(connection_note)), "yellow")])
        ev = snap["events"][-8:]
        if ev:
            out += self.event_rows(ev, width, limit=8)
        hits = prefix_hits + header_hits("sources", cells, len(prefix) + 1) + [(len(prefix) + 2 + i, "source", r["name"]) for i, r in enumerate(shown)]
        record_page(app, "sources", len(shown))
        if height is not None:
            _table_scrollbar(app, "sources", width, len(prefix) + 2, vis, n, top, header=len(prefix))
        return out, hits

    # ---- analytics tab ----------------------------------------------------------------------------
    def analytics_jobs(self, snap: dict, app) -> List[str]:
        """Jobs the series view can show: running first, then every job with a recorded series."""
        pane_ids = getattr(app, "pane_job_ids", None)
        if pane_ids is not None:
            return list(pane_ids)
        ids = [j.id for j in snap["jobs"] if not j.pending]
        seen = set(ids)
        for i in _series_jobs(app):
            if i not in seen:
                ids.append(i)
                seen.add(i)
        binding = getattr(getattr(app, "sampler", None), "telemetry_binding", None) or {}
        retained_job = binding.get("job_id")
        if retained_job and retained_job not in seen:
            # A selected retained run remains readable after scheduler history
            # expires, or before its first scheduler response has arrived.
            ids.append(retained_job)
            seen.add(retained_job)
        chosen = getattr(app, "analytics_job", None)
        if chosen and chosen not in ids and any(record.id == chosen for record in
                list(snap.get("jobs", ())) + list(snap.get("finished", ())) + list(snap.get("departed_jobs", {}).values())):
            ids.append(chosen)
        return ids

    def analytics_tab(self, snap: dict, app, width: int, height: Optional[int]) -> Tuple[List[Row], List]:
        from . import chart_interaction, analytics_document
        analytics_document.begin_render(app)
        chart_mark = chart_interaction.mark(app)
        scroll_mark = SB.mark(app)
        g = self.g
        view = app.analytics_view
        days = app.analytics_days_value()
        from .control_rows import buttons
        out, hits = buttons(g, width, [(key, title, ("command", "view " + key)) for key, title in ANALYTICS_VIEWS],
                            selected=view, group="analytics_nav", prefix="analytics-view:")
        import shlex
        selected_job = getattr(app, "analytics_job", None)
        diagnostic_command = "telemetry" + (" " + shlex.quote(str(selected_job)) if selected_job else "")
        controls, control_hits = buttons(g, width,
            [("setup", "Set up this view", ("command", "setup analytics " + view)),
             ("connect-data", "Connect data", ("command", "connectdata")),
             ("telemetry", "Metric sampling", ("command", diagnostic_command)),
             ("gpu-provider", "GPU source", ("command", "gpuprovider"))],
            group="analytics-tools", prefix="analytics-tools:")
        hits += [(y + len(out), kind, data) for y, kind, data in control_hits]
        out += controls
        if not getattr(app, "analytics_document_mode", False):
            # The Jobs Details adapter discards this navigation prefix. Keep
            # standalone controls without rebuilding invisible buttons for
            # every live graph frame in an inline document.
            from .ui import operations as ops_ui
            controls, control_hits = ops_ui.contextual_controls(g, width, ("bottlenecks", "statistics", "energy", "regression-investigation", "numerical-stability", "execution-simulation"))
            hits += [(y + len(out), kind, data) for y, kind, data in control_hits]
            out += controls
        app.analytics_nav_rows = len(out)
        out.append([(f" window {days:g} day{'s' if days != 1 else ''}", "dim")])
        avail = None if height is None else max(0, height - len(out))
        from .ui.workspace_state import enabled as workspace_enabled
        document = (view != "job" and height is not None and
                    not getattr(app, "analytics_document_mode", False) and not workspace_enabled(app))
        body_width = max(1, width - 1) if document else width
        body_avail = None if document else avail
        previous_window = getattr(app, "analytics_render_window", None)
        app.analytics_group_hits = []
        app.analytics_group_records = []
        app.analytics_advisor_ids = ()
        app.analytics_advisor_positions = {}
        app.analytics_advisor_page = 0
        if document:
            offsets = getattr(app, "analytics_scroll_offsets", None)
            if not isinstance(offsets, dict):
                offsets = app.analytics_scroll_offsets = {}
            key = "analytics:document:" + view
            context = (view, app.analytics_job if view != "advisor" else None, days, tuple(app.compare_ids), width)
            state = offsets.setdefault(view, {"top": 0, "context": context})
            if state["context"] != context:
                state.update(top=0, context=context)
            app.analytics_render_window = (state["top"], max(0, (avail or 0) - 1))
        try:
            if view == "job":
                body = self.analytics_job(snap, app, width, avail)
            elif view == "history":
                body = self.analytics_history(snap, app, body_width, body_avail, days)
            elif view == "advisor":
                render_advice = getattr(app, "pane_advisor_renderer", None) or self.analytics_advisor
                body = render_advice(snap, app, body_width, body_avail, days)
            elif view == "compare":
                body = self.analytics_compare(snap, app, body_width, body_avail)
            else:
                body = self.analytics_timeline(snap, app, body_width, body_avail, days)
        finally:
            app.analytics_render_window = previous_window
        group_hits = getattr(app, "analytics_group_hits", [])
        group_records = getattr(app, "analytics_group_records", [])
        if document:
            from .scrolling import viewport
            page, count = max(0, (avail or 0) - 1), max(0, len(body) - 1)
            if view == "advisor":
                app.analytics_advisor_page = page
            state["top"] = max(0, min(max(0, count - page), state["top"]))
            painted = viewport(app, key, state["top"], count, page, context=context)
            records = chart_interaction.take_since(app, chart_mark)
            chart_interaction.put_records(app, chart_interaction.map_records(records,
                lambda y: y - painted if 1 + painted <= y < 1 + painted + page else None,
                clip=(1, 0, 1 + page, width)))
            body = body[:1] + body[1 + painted:1 + painted + page]
            group_hits = [(y - painted, kind, value) for y, kind, value in group_hits
                          if 1 + painted <= y < 1 + painted + page]
            group_records = [(y - painted, record) for y, record in group_records
                             if 1 + painted <= y < 1 + painted + page]
            if body and width >= 6:
                body[0] = L.clip_row([("    ", "")] + body[0], width)
            if page and width >= 2:
                SB.register(app, key, (1, 0, 1 + page, width), count, page, state["top"], painted,
                    lambda value: state.__setitem__("top", value), context=context,
                    header=(0, 0, width) if width >= 6 else None)
        if view == "advisor":
            # Apply continuation controls after the actual smooth-scroll
            # viewport is known. Overscan rows must not claim the only button
            # for a group whose first painted member appears below them.
            for y, record in group_records:
                if 0 <= y < len(body):
                    self.group_controls(app, body, group_hits, [record], y,
                                        "analytics:advisor", body_width, first_visible=True)
            hits += [(len(out) + y, "advisor_job", record.id)
                     for y, record in group_records if 0 <= y < len(body)]
        chart_interaction.place_since(app, chart_mark, dy=len(out),
            clip=(len(out), 0, len(out) + len(body), width))
        SB.place_since(app, scroll_mark, dy=len(out),
            clip=(len(out), 0, len(out) + len(body), width))
        hits += [(len(out) + y, kind, value) for y, kind, value in group_hits if 0 <= y < len(body)]
        if view == "advisor":
            import shlex
            hits += [(len(out) + y, "control", {"id": "advisor-job:" + record.id,
                       "label": record.id, "left": 0, "right": body_width,
                       "action": ("command", "advisor-job " + shlex.quote(record.id)),
                       "group": "advisor-jobs", "button": False})
                     for y, record in group_records if 0 <= y < len(body)]
        return out + body, hits

    def analytics_advisor(self, snap: dict, app, width: int, avail: Optional[int], days: float) -> List[Row]:
        """What each job name should ask for, from its completed runs in the window; the running jobs so far."""
        app.analytics_group_hits = []
        app.analytics_group_records = []
        g = self.g
        now = clock.now()
        t_lo = now - days * 86400
        fin = [f for f in snap["finished"] if (stamp(f.end) or now) >= t_lo]
        advices = self.history_advice_cache.names(fin)
        out: List[Row] = [rule(g, width, f"advisor: what the jobs of the last {days:g} day{'s' if days != 1 else ''} should have asked for ({len(advices)} job names)")]
        wasted = sum(a.wasted_core_hours for a in advices)
        out.append([(f"   {wasted:.1f} core-hours spent on idle cores over the window (1 - efficiency, times core-hours); suggestions keep {int(100 * (advisor.MEM_HEADROOM - 1))}% memory headroom and {int(100 * (advisor.TIME_HEADROOM - 1))}% time headroom over the worst run", "dim")])
        rows = []
        for a in advices:
            rows.append(dict(name=a.name, runs=a.id, mem=f"{human(a.mem_peak) if a.mem_peak else '?'} / {human(a.mem_req) if a.mem_req else '?'}", mem_s=a.mem_suggest or (g.dot if a.mem_req else ""),
                             eff=f"{100 * a.cpu_eff:.0f}%" if a.cpu_eff is not None else "", cpus=f"{a.cpus}", cpus_s=str(a.cpus_suggest) if a.cpus_suggest and a.cpus_suggest != a.cpus else g.dot,
                             time=f"{hms(a.elapsed) if a.elapsed else '?'} / {hms(a.limit) if a.limit else '?'}", time_s=a.time_suggest or g.dot, waste=f"{a.wasted_core_hours:.1f}",
                             notes=", ".join(a.notes), flags=a.flags(),
                             _styles={"mem_s": "yellow" if a.mem_suggest else "", "cpus_s": "yellow" if (a.cpus_suggest and a.cpus_suggest != a.cpus) else "", "time_s": "yellow" if a.time_suggest else "",
                                      "notes": "red" if a.notes else ""}))
        cols = [Column("name", "JOB NAME", 8, 24, flex=True), Column("runs", "RUNS", 4, 7), Column("mem", "PEAK / REQ MEM", 14, 20), Column("mem_s", "MEM", 3, 6, ">"), Column("eff", "EFF", 3, 4, ">"),
                Column("cpus", "CPUS", 4, 4, ">"), Column("cpus_s", "CPUS", 4, 4, ">"), Column("time", "LONGEST / LIMIT", 15, 20), Column("time_s", "TIME", 4, 10), Column("waste", "IDLE C-H", 8, 8, ">"),
                Column("notes", "NOTES", 5, 30, flex=True)]
        if rows:
            trows, _ = table(cols, rows, width, g.ascii, droppable=("notes", "waste", "eff"))
            out += trows
            out.append([("   copy a line with v then y; the flags of the first row: " + (rows[0]["flags"] or "nothing to change"), "dim")])
        else:
            out.append([("   nothing finished in the window", "dim")])
        running = [j for j in snap["jobs"] if not j.pending]
        if running:
            from raven.jobs.group_domain import project_records, metadata_for_record
            running = project_records(app, snap, running, tab="analytics:advisor")
            app.analytics_advisor_ids = tuple(j.id for j in running)
            out.append(rule(g, width, "running jobs so far"))
            from raven.jobs.list_controls import controls as list_controls
            controls, control_hits = list_controls(app, g, width, "analytics:advisor")
            app.analytics_group_hits += [(y + len(out), kind, value) for y, kind, value in control_hits]
            out += controls
            app.analytics_advisor_positions = {j.id: len(out) + index for index, j in enumerate(running)}
            by_name = self.history_advice_cache.groups(snap["finished"])
            window = getattr(app, "analytics_render_window", None)
            if isinstance(window, tuple) and len(window) == 2:
                wanted_top, window_page = window
                wanted_top = min(wanted_top, max(0, len(out) + len(running) - 1 - window_page))
                lower, upper = wanted_top + 1 - 4, wanted_top + 1 + window_page + 4
            else:
                lower, upper = 0, float("inf")
            for j in running:
                if not lower <= len(out) < upper:
                    out.append([])
                    continue
                meta = metadata_for_record(app, "analytics:advisor", j.id)
                app.analytics_group_records.append((len(out), j))
                marked = j.id in app.marks
                selected = (getattr(app, "job_selection_state", {}).get("advisor_focus")
                            and app.analytics_job == j.id)
                row_style = "sel+bold" if selected else "warning+bold" if marked else "cyan"
                marker = "*" if marked else " "
                if meta is not None and meta.header and meta.collapsed:
                    from raven.jobs.group_ui import summary_row
                    label = f"  {marker}{j.id} {meta.group.label} / {meta.visible_count} records "
                    out.append([(cut(label, width, g.ascii), row_style)] + summary_row(meta.stats or meta.records,
                        max(0, width - L.vlen(label)), ascii_=g.ascii))
                    self.group_controls(app, out, app.analytics_group_hits, [j], len(out) - 1, "analytics:advisor", width)
                    continue
                lv = snap["live"].get(j.id)
                adv = advisor.advise_running(j, lv, normalized_series(app, j.id), by_name.get(j.name, ()))
                text = adv.summary(g.dot) or "nothing to change yet"
                out.append([(f"  {marker}{j.id} ", row_style), (pad(cut(j.name, 20, g.ascii), 20), "bold"), (" " + cut(text, width - 36, g.ascii), "")])
                if meta is not None and meta.header:
                    self.group_controls(app, out, app.analytics_group_hits, [j], len(out) - 1, "analytics:advisor", width)
        if avail is not None:
            out = out[:avail]
        return out

    def analytics_compare(self, snap: dict, app, width: int, avail: Optional[int]) -> List[Row]:
        """The marked (or :compare) jobs side by side: a table, then each metric aligned on time since the job started."""
        g = self.g
        ids = list(app.compare_ids) or sorted(app.marks)
        out: List[Row] = [rule(g, width, f"compare {len(ids)} jobs (mark jobs with Space, or :compare <ids>)")]
        if len(ids) < 1:
            return out + [[("   mark two or more jobs on the Jobs tab (Space), or :compare 123 456", "dim")]]
        jobs = {j.id: j for j in snap["jobs"]}
        fins = {f.id: f for f in snap["finished"]}
        rows = []
        series = {}
        for i in ids[:6]:
            s = normalized_series(app, i)
            series[i] = s
            live = [x for x in s if x.get("k") == "live"]
            gpu = [x for x in s if x.get("k") == "gpu"]
            cpu = [x.get("cpu") if x.get("cpu") is not None else x.get("eff") for x in live]
            cpu = [c for c in cpu if c is not None]
            rss = [x.get("rss") for x in live if x.get("rss") is not None]
            gu = [value for point in gpu if (value := _gpu_record_mean(point)) is not None]
            j, f = jobs.get(i), fins.get(i)
            name = j.name if j else (f.name if f else "?")
            state = j.state if j else (f.state if f else "?")
            cpus = j.cpus if j else (f.cpus if f else 0)
            req = j.mem_bytes if j else (f.req_mem if f else 0)
            el = j.elapsed_s if j else (secs(f.elapsed) if f else None)
            rows.append(dict(id=i, name=name, state=state, elapsed=hms(el) if el is not None else "?", cpus=cpus,
                             cpu_mean=f"{100 * sum(cpu) / len(cpu):.0f}%" if cpu else "", cpu_max=f"{100 * max(cpu):.0f}%" if cpu else "",
                             mem=human(max(rss)) if rss else (human(f.rss) if f and f.rss else ""), mem_pct=f"{100 * max(rss) / req:.0f}%" if (rss and req) else (f"{100 * f.mem_eff:.0f}%" if f and f.mem_eff is not None else ""),
                             gpu=f"{sum(gu) / len(gu):.0f}%" if gu else "", samples=len(s), coreh=f"{(el or 0) * cpus / 3600:.1f}",
                             _styles={"state": "green" if state in ("COMPLETED", "RUNNING") else "red"}))
        cols = [Column("id", "JOBID", 5, 16), Column("name", "NAME", 6, 24, flex=True), Column("state", "STATE", 5, 13), Column("elapsed", "ELAPSED", 7, 12), Column("cpus", "CPUS", 4, 4, ">"),
                Column("cpu_mean", "CPU MEAN", 8, 8, ">"), Column("cpu_max", "CPU MAX", 7, 7, ">"), Column("mem", "PEAK MEM", 8, 9, ">"), Column("mem_pct", "MEM%", 4, 5, ">"), Column("gpu", "GPU MEAN", 8, 8, ">"),
                Column("coreh", "CORE-H", 6, 7, ">"), Column("samples", "SAMPLES", 7, 7, ">")]
        trows, _ = table(cols, rows, width, g.ascii, droppable=("samples", "coreh", "cpu_max"))
        out += trows
        metrics = [("cpu per core", lambda x: (100 * x["cpu"] if x.get("cpu") is not None else (100 * x["eff"] if x.get("eff") is not None else None)) if x.get("k") == "live" else None, 100.0, "%", "live"),
                   ("memory (GB)", lambda x: (x["rss"] / 1024 ** 3) if (x.get("k") == "live" and x.get("rss") is not None) else None, None, "G", "live"),
                   ("gpu utilisation", lambda x: _gpu_record_mean(x) if x.get("k") == "gpu" else None, 100.0, "%", "gpu")]
        with_data = [i for i in ids[:6] if series.get(i)]
        if not with_data:
            return out + [[("   no recorded series for these jobs (series accumulate while the dashboard runs)", "dim")]]
        span = max((series[i][-1]["t"] - series[i][0]["t"]) for i in with_data) or 1.0
        n_charts = sum(1 for _, fn, _, _, _ in metrics if any(fn(x) is not None for i in with_data for x in series[i]))
        h = 3 if avail is None else max(2, min(6, (avail - len(out) - 3 * n_charts * len(with_data)) // max(1, n_charts * len(with_data))))
        for title, fn, hi, unit, kind in metrics:
            if not any(fn(x) is not None for i in with_data for x in series[i]):
                continue
            out.append(rule(g, width, f"{title}, aligned on each job's first sample ({compact(span)} across)"))
            hi_all = hi
            if hi is None:
                vals = [fn(x) for i in with_data for x in series[i] if fn(x) is not None]
                hi_all = max(vals) * 1.05 if vals else 1.0
            for i in with_data:
                s = series[i]
                t0 = s[0]["t"]
                samples = [x for x in s if x.get("k") == kind]
                values = [fn(x) for x in samples]
                label = f"{i} {cut(rows[[r['id'] for r in rows].index(i)]['name'], 14, g.ascii)}"
                from . import chart_interaction
                identity = chart_interaction.key(app, title, kind, i, scope="resource-compare", attempt=t0)
                out += self.metric_curve(app, values, width, h, identity, row=len(out),
                    hi=hi_all, unit=unit, title=label, times=(0, span),
                    sample_times=[x["t"] - t0 for x in samples], sample_interval=self.cfg["intervals"][kind], elapsed=True)
        if avail is not None:
            out = out[:avail]
        return out

    def analytics_job(self, snap: dict, app, width: int, avail: Optional[int]) -> List[Row]:
        from .ui.selection_state import selected
        g = self.g
        ids = self.analytics_jobs(snap, app)
        if app.analytics_job and app.analytics_job not in ids:
            return [rule(g, width, "job series / " + str(app.analytics_job)), [(" This job is no longer in the current observations. Select another job in history.", "dim")]]
        if not ids:
            return [rule(g, width, "job series"), [("   no running job and no recorded series yet (series accumulate while the dashboard runs)", "dim")]]
        if app.analytics_job not in ids:
            app.analytics_job = selected(app, "analytics", app.selected_id if app.selected_id in ids else ids[0])
        if app.analytics_job is None:
            return [rule(g, width, "job series"), [(" Select a job in the history browser to show its recorded series.", "dim")]]
        jid = app.analytics_job
        pos = ids.index(jid)
        job = next((j for j in snap["jobs"] if j.id == jid), None)
        fin = next((f for f in snap["finished"] if f.id == jid), None)
        binding = getattr(getattr(app, "sampler", None), "telemetry_binding", None) or {}
        binding = binding if binding.get("job_id") == jid else {}
        name = job.name if job else (fin.name if fin else binding.get("run_id", ""))
        state = job.state.lower() if job else (fin.state.lower() if fin else str(binding.get("state", "no longer listed")).lower())
        record = job or fin
        attempt = "|".join(str(getattr(record, name, None) or "") for name in ("submit", "start")) if record else None
        req = job.mem_bytes if job else (fin.req_mem if fin else 0)
        intervals = self.cfg["intervals"]
        # Without scheduler attempt evidence, an old native archive for a
        # reused job ID must not be mixed into the explicitly attached run.
        samples = [] if binding and record is None else _series_publication(app, jid)
        native = self._native_sources.remember(
            ("native", getattr(app.store, "_series_scope", id(app.store)), jid, attempt, req, g.ascii, intervals["live"], intervals["gpu"]), samples,
            lambda owned: _prepare_native_metrics(owned, req, g.ascii, intervals, owned=True))
        trace_samples = snap.get("trace", {}).get(jid, [])
        trace = self._native_sources.remember(("trace", getattr(app.store, "_series_scope", id(app.store)), jid, attempt), trace_samples,
                                             lambda owned: _prepare_trace_metrics(owned, owned=True))
        retained = getattr(getattr(app, "sampler", None), "retained_telemetry", {}).get(jid, {})
        process_rows = retained.get("cpu-memory", [])
        retained_plots = self._native_sources.remember(("retained-process", jid, attempt), process_rows,
                                                      _prepare_retained_process_metrics)
        series_notice = _series_notice(app, jid)
        live_count, gpu_count, span = native["live_count"], native["gpu_count"], native["span"]
        head = [rule(g, width, f"job series {pos + 1}/{len(ids)}"),
                [(f" {jid} ", "cyan"), (name, "bold"), (f"   {state}", ""),
                 (f"   {live_count} cpu samples, {gpu_count} gpu samples" +
                  (f" over {compact(span)}" if span is not None and charts._finite(span) is not None else ""), "dim")]]
        if job:
            head[1].append((f"   {job.partition} {g.dot} {job.nodelist or 'pending'} {g.dot} {job.cpus} cpus" + (f" {g.dot} {job.gpu_text}" if job.gpus else "") + f" {g.dot} {job.elapsed} of {job.limit}", "dim"))
        if series_notice:
            head.append([(" " + series_notice[0], series_notice[1])])
        head.extend(_retained_status_rows(app, jid, retained, width))
        if retained:
            head.append([(" Retained telemetry: local observed scope; export reads the full retained stream, not this chart tail.", "dim")])
            for source in retained.get("index", {}).get("sources", []):
                coverage = source.get("coverage", {})
                note = source.get("reason") or coverage.get("limitation")
                if note:
                    head.append([(" " + source["family"] + ": " + display_text(str(note)), "dim")])
            head.extend([(" " + display_text(str(note)), "dim")] for note in retained.get("warnings", []))
        if not live_count and not gpu_count and not trace_samples and not process_rows:
            if series_notice:
                return head
            return head + _gpu_unavailable(snap, app, jid, record) + [[("   no samples recorded for this job yet", "dim")]]
        charts_ = native["plots"] + trace["plots"] + retained_plots
        if native["keys"] or trace["devices"]:
            head.append([(" GPU rate is sampled device busy %. Efficiency proxy is the mean of valid retained samples; gaps are excluded.", "dim")])
            head.append([(" GPU scope is observed devices only. Throughput, FLOP efficiency, and full-run allocation efficiency are not measured.", "dim")])
            if not native["available"]:
                if "available" not in trace:
                    trace["available"] = trace["availability"]()
                if not trace["available"]:
                    head += _gpu_unavailable(snap, app, jid, record, detected=True)
        elif getattr(record, "gpus", 0) and not (series_notice and series_notice[2] in ("loading", "busy")):
            head += _gpu_unavailable(snap, app, jid, record)
        from . import analytics_document, chart_interaction, metric_live
        native_document = analytics_document.eligible(app, avail)
        source_head = len(head)
        n = len(charts_)
        telemetry = []
        heatmap_start = len(head)
        if not g.ascii and width >= 120 and (avail is None or avail >= 28):
            telemetry = [(title, values) for title, values, hi, unit, _, _, _, _ in charts_ if unit == "%" and len(values) >= 2][:4]
            if telemetry:
                # Reserve exact title/data/legend geometry. Materialize the
                # summary only after both pane types publish their viewport.
                head += [[] for _ in range(len(telemetry) + 2)]
        columns = 2 if not g.ascii and width >= 140 and n >= 2 and (native_document or avail is None or avail >= 20) else 1
        plot_rows = max(1, (n + columns - 1) // columns)
        filled = not native_document and not g.ascii and (avail is None or avail - len(head) >= plot_rows * 10)
        overhead = (7 if filled else 3) + 2 * int(bool(job and job.state == "RUNNING" and width >= 24))
        h = 8 if native_document or avail is None else max(2, min(12, (avail - len(head)) // plot_rows - overhead))
        plot_width = max(1, width - 1) if native_document else width
        cell_width = (plot_width - 2 * (columns - 1)) // columns
        out = list(head)
        record = job or fin
        attempt = "|".join(str(getattr(record, name, None) or "") for name in ("submit", "start")) if record else None
        running = bool(job and job.state == "RUNNING")
        # Title + plot rows + baseline + time axis. Running controls and their
        # source-age footer add two rows; an area companion adds four rows.
        band_rows = h + 3 + 2 * int(running and cell_width >= metric_live.MIN_WIDTH) + 4 * int(filled)
        band_heights = []
        for offset in range(0, n, columns):
            extras = []
            for _, _, _, unit, _, _, _, metric_id in charts_[offset:offset + columns]:
                identity = chart_interaction.key(app, metric_id, unit, jid, scope="resource-series", attempt=attempt)
                extra = int(chart_interaction.bounds(app, identity, scale="linear") is not None)
                if filled:
                    area_key = chart_interaction.key(app, metric_id, unit, jid, scope="resource-area", attempt=attempt)
                    extra += int(chart_interaction.bounds(app, area_key, scale="linear") is not None)
                extras.append(extra)
            band_heights.append(band_rows + max(extras, default=0))
        chart_mark = chart_interaction.mark(app)
        inline_window = getattr(app, "analytics_series_window", None)
        if inline_window and inline_window[0] >= len(head) + sum(band_heights):
            # A device source can disappear while viewing its final card.
            # Reflow clamps the outer scroll only after composition, so paint
            # this changed document once before establishing its new map.
            inline_window = None
        if native_document:
            sticky = min(source_head, max(0, avail - 2))
            count = len(head) - sticky + sum(band_heights)
            painted, page = analytics_document.prepare(app, jid, attempt, width, avail, sticky, count)
        heatmap_end = heatmap_start + len(telemetry) + 2
        heatmap_visible = (not inline_window or
                           inline_window[0] < heatmap_end and inline_window[1] > heatmap_start)
        if native_document:
            heatmap_visible = sticky + painted < heatmap_end and sticky + painted + page > heatmap_start
        if telemetry and heatmap_visible:
            heatmap_key = (width, g.ascii, g.spark, tuple(telemetry))
            heatmaps = native["heatmaps"]
            heatmap = heatmaps.get(heatmap_key)
            if heatmap is None:
                heatmap = charts.heatmap(g, [values.materialize() if isinstance(values, _MetricValues) else values
                                            for _, values in telemetry], width,
                                        labels=[title.split(" · ")[0] for title, _ in telemetry], hi=100, unit="%",
                                        title="telemetry heatmap · each row's observations, oldest to newest")
                from .metric_raster import coalesce_row
                heatmap = [coalesce_row(row) for row in heatmap]
                heatmaps[heatmap_key] = heatmap
                while len(heatmaps) > 4:
                    heatmaps.popitem(last=False)
            else:
                heatmaps.move_to_end(heatmap_key)
            out[heatmap_start:heatmap_end] = [list(row) for row in heatmap]
        for offset in range(0, n, columns):
            if not native_document and avail is not None and len(out) >= avail:
                break
            current_band_rows = band_heights[offset // columns]
            if native_document and (len(out) + current_band_rows <= sticky + painted or len(out) >= sticky + painted + page):
                # Reserve measured document rows without rasterizing or staging
                # controls for cards outside the actual painted viewport.
                out.extend([[] for _ in range(current_band_rows)])
                continue
            if inline_window and (len(out) + current_band_rows <= inline_window[0] or len(out) >= inline_window[1]):
                out.extend([[] for _ in range(current_band_rows)])
                continue
            panels = []
            for position, (title, values, hi, unit, times, sample_times, sample_interval, metric_id) in enumerate(charts_[offset:offset + columns]):
                # Each resource uses its own sampled span; a GPU trace cannot move a CPU time axis.
                if isinstance(values, _MetricValues):
                    values = values.materialize()
                observation_index = None
                if isinstance(sample_times, _MetricValues):
                    observation_index = sample_times.index()
                    sample_times = sample_times.materialize()
                title = title.replace(" · ", " - ") if g.ascii else title
                from . import chart_interaction
                identity = chart_interaction.key(app, metric_id, unit, jid, scope="resource-series", attempt=attempt)
                column = position * (cell_width + 2)
                panel = self.metric_curve(app, values, cell_width, h, identity, row=len(out), column=column,
                    running=bool(job and job.state == "RUNNING"), hi=hi, unit=unit, title=title,
                    times=times, sample_times=sample_times, sample_interval=sample_interval, observation_index=observation_index)
                if filled:
                    area_key = chart_interaction.key(app, metric_id, unit, jid, scope="resource-area", attempt=attempt)
                    area_start = len(out) + len(panel)
                    area_rows = 4 + int(chart_interaction.bounds(app, area_key, scale="linear") is not None)
                    if inline_window and (area_start + area_rows <= inline_window[0] or area_start >= inline_window[1]):
                        # A visible fine curve does not require its companion
                        # area below the viewport to scan the full history.
                        panel += [[] for _ in range(area_rows)]
                    else:
                        panel += self.metric_curve(app, values, cell_width, 2, area_key, row=area_start,
                            column=column, filled=True, hi=hi, unit=unit, times=times,
                            sample_times=sample_times, sample_interval=sample_interval, observation_index=observation_index)
                panels.append(panel)
            out += self.beside(panels, [cell_width] * len(panels)) if columns > 1 else panels[0]
        if native_document:
            return analytics_document.finish(app, out, sticky, painted, page, width, chart_mark, g)
        return out if avail is None else out[:avail]

    def analytics_history(self, snap: dict, app, width: int, avail: Optional[int], days: float) -> List[Row]:
        g = self.g
        now = clock.now()
        t_lo = now - days * 86400
        fin = [f for f in snap["finished"] if (stamp(f.end) or now) >= t_lo]
        out: List[Row] = [rule(g, width, f"history, last {days:g} day{'s' if days != 1 else ''}: {len(fin)} finished jobs")]
        if not fin:
            return out + [[("   nothing finished in the window", "dim")]]
        by_day: Dict[str, Dict[str, float]] = {}
        for f in fin:
            day = (f.end or f.start)[:10] or "?"
            d = by_day.setdefault(day, dict(completed=0, failed=0, other=0, core_h=0.0, gpu_h=0.0))
            key = "completed" if f.state == "COMPLETED" else ("other" if f.state.startswith("CANCEL") else "failed")
            d[key] += 1
            d["core_h"] += f.core_hours
            d["gpu_h"] += f.gpu_hours
        days_sorted = sorted(by_day)
        out.append([("   jobs per day", "bold"), ("   completed ", "dim"), (g.full * 2, "green"), ("  failed / timeout / oom ", "dim"), (g.full * 2, "red"), ("  cancelled ", "dim"), (g.full * 2, "yellow")])
        peak = max(sum(v for k, v in d.items() if k in ("completed", "failed", "other")) for d in by_day.values()) or 1
        bar_w = max(10, width - 36)
        for day in days_sorted:
            d = by_day[day]
            segs: Row = [(f"   {day[5:]}  ", "")]
            total = 0
            for key, style in (("completed", "green"), ("failed", "red"), ("other", "yellow")):
                n = int(round(d[key] / peak * bar_w))
                segs.append((g.full * n, style))
                total += d[key]
            segs.append((f" {int(total)}  ({d['core_h']:.1f} core-h, {d['gpu_h']:.1f} gpu-h)", "dim"))
            out.append(segs)
        out.append(rule(g, width, "core-hours per partition"))
        parts: Dict[str, float] = {}
        for f in fin:
            parts[f.partition or "?"] = parts.get(f.partition or "?", 0.0) + f.core_hours
        out += charts.hbar_rows(g, [(p, v, "cyan") for p, v in sorted(parts.items(), key=lambda kv: -kv[1])[:6]], width, unit="h")
        waits = []
        for f in fin:
            a, b = stamp(f.submit), stamp(f.start)
            if a and b and b >= a:
                waits.append((b - a) / 60)
        if waits:
            out.append(rule(g, width, f"queue wait (minutes), {len(waits)} jobs, median {sorted(waits)[len(waits) // 2]:.0f}"))
            out += charts.histogram(g, waits, [0, 1, 5, 15, 60, 240, 1440], width, fmt=lambda x: f"{x:g}")
        effs = [100 * f.cpu_eff for f in fin if f.cpu_eff is not None]
        if effs:
            out.append(rule(g, width, f"cpu efficiency (%), mean {sum(effs) / len(effs):.0f}"))
            out += charts.histogram(g, effs, [0, 10, 25, 50, 75, 90], width, fmt=lambda x: f"{x:g}")
        if avail is not None:
            out = out[:avail]
        return out

    def analytics_timeline(self, snap: dict, app, width: int, avail: Optional[int], days: float) -> List[Row]:
        g = self.g
        now = clock.now()
        t_lo = now - days * 86400
        items = []
        for f in snap["finished"]:
            end = stamp(f.end)
            if end is not None and end < t_lo:
                continue
            items.append(dict(name=f.name, id=f.id, state=f.state, submit=stamp(f.submit), start=stamp(f.start), end=end))
        for j in snap["jobs"]:
            items.append(dict(name=j.name, id=j.id, state=j.state, submit=stamp(j.submit), start=stamp(j.start) if not j.pending else None, end=None))
        items.sort(key=lambda d: (d["start"] if d["start"] is not None else (d["submit"] or now)))
        out: List[Row] = [rule(g, width, f"timeline, last {days:g} day{'s' if days != 1 else ''}: {len(items)} jobs ({g.dot} queued, {g.full} running)")]
        if not items:
            return out + [[("   nothing in the window", "dim")]]
        earliest = min((d["submit"] or d["start"] or now) for d in items)
        t0 = max(t_lo, earliest - 60)
        t1 = now + max(60.0, (now - t0) * 0.02)
        rows = charts.gantt(g, items, t0, t1, width)
        if avail is not None:
            rows = rows[:avail - 1]
        return out + rows

    # ---- shared -----------------------------------------------------------------------------------
    def event_rows(self, events: Sequence[dict], width: int, limit: int = 4) -> List[Row]:
        ev = list(events)[-limit:]
        if not ev:
            return []
        out = [rule(self.g, width, "events")]
        for e in ev:
            col = {"started": "green", "queued": "blue", "finished": "dim", "left": "dim", "action": ("red" if not e.get("ok", True) else "magenta"), "held": "yellow", "released": "green",
                   "alert": "red+bold", "alert_error": "red", "plugin": "red", "tag": "cyan"}.get(e.get("kind", ""), "dim")
            t = time.strftime("%H:%M:%S", time.localtime(e.get("t", 0)))
            day = time.strftime("%m-%d", time.localtime(e.get("t", 0)))
            if day != time.strftime("%m-%d", time.localtime(clock.now())):
                t = f"{day} {t}"
            out.append([("   " + cut(f"{t}  {e.get('text', '')}", width - 4, self.g.ascii), col)])
        return out

    def compose(self, snap: dict, app, width: int, height: Optional[int], actions=None, *, feedback=True) -> Tuple[List[Row], List[Tuple[int, str, str]]]:
        from .historical_ui import active as historical_active
        if historical_active(app):
            # A read-only historical overlay never reveals later live rows
            # around its border or leaves their hit targets active underneath.
            app.width, app.height = width, height
            rows = [[(" " * max(0, width), "")] for _ in range(max(0, height or 30))]
            app.frame_rows, app.last_rows, app.last_hits, app.tab_hits = rows, [], [], []
            return rows, []
        from raven.jobs.group_domain import frame
        from .table_tools import snapshot
        from raven.jobs.list_controls import mode as list_mode
        contexts = (("jobs", "recent") if app.tab == "jobs" else
                    (app.tab, "history:" + app.tab, "analytics:advisor" if app.tab == "analytics" else app.tab))
        groups_visible = any(list_mode(app, context) == "grouped" for context in contexts)
        if app.tab not in ("jobs", "history", "group", "deps", "log", "analytics", "research") or not groups_visible:
            return self._compose(snap, app, width, height, actions, feedback=feedback)
        with frame(app, snapshot(app, snap)):
            return self._compose(snap, app, width, height, actions, feedback=feedback)

    def _compose(self, snap: dict, app, width: int, height: Optional[int], actions=None, *, feedback=True) -> Tuple[List[Row], List[Tuple[int, str, str]]]:
        width = max(0, width)
        app.width = width
        if height is not None:
            app.height = height
        from .sidebar import width as sidebar_width
        content_x = sidebar_width(app, width) if height is not None else 0
        content_width = max(0, width - content_x)
        app.content_origin_x = content_x
        from .terminal import ui as terminal_ui
        from . import chart_interaction
        terminal_ui.begin_frame(app)
        chart_interaction.begin_frame(app, width, height)
        SB.begin_frame(app, width, height)
        app.completion.update(snap)
        from .session_tools import observe, unread_count
        from .table_tools import snapshot, freeze_status
        observe(app, snap)
        app.inbox_unread = unread_count(app)
        app.freeze_label = freeze_status(app, snap)
        snap = snapshot(app, snap)
        if height is not None and height <= 0:
            from .pane_drag import begin_frame
            begin_frame(app, width, height)
            app.tab_hits, app.last_rows, app.last_hits = [], [], []
            app.frame_rows = []
            from .column_resize import publish as publish_columns
            publish_columns(app, [], [], width, height, ascii_=self.g.ascii)
            from .group_queue_drag import cancel as cancel_queue_drag
            cancel_queue_drag(app)
            if feedback:
                from .interaction import publish
                publish(app, [], [], width, height)
            terminal_ui.finalize_frame(app)
            return [], []
        head = self.header(snap, app, content_width, toolbar_width=width, navigation_row=height is None)
        from .ui.content_geometry import shift_hit, prefix_rows, shift_header_controls
        shift_header_controls(app, content_x)
        # Row zero spans the whole application. The sidebar starts below it;
        # every remaining header and document row shares one content origin.
        head = head[:1] + prefix_rows(head[1:], content_x, content_width)
        terminal_header_hits = []
        if (terminal_ui.enabled(app) and app.tab not in ("jobs", "history")
                and not terminal_ui.showing(app) and (height is None or height - len(head) - 1 >= 8)):
            terminal_row, terminal_controls = terminal_ui.header(app, content_width, x=content_x, y=len(head))
            terminal_header_hits = [shift_hit(y + len(head), kind, value, dx=content_x)
                                    for y, kind, value in terminal_controls]
            head.extend(prefix_rows([terminal_row], content_x, content_width))
            terminal_ui.initialize(app).geometry = (app.width, getattr(app, "height", None))
        app.body_origin = len(head)
        from .pane_drag import begin_frame
        begin_frame(app, width, height)
        body_h = None if height is None else max(0, height - len(head) - 1)
        prepared_jobs = None
        def jobs_renderer(panel_width, panel_height):
            nonlocal prepared_jobs
            if prepared_jobs is None:
                prepared_jobs = _table_job_rows(self, snap, app, actions)
            return self.jobs_tab(snap, app, actions, panel_width, panel_height, prepared_rows=prepared_jobs)
        def default_renderer(panel_width, panel_height):
            fn = {"jobs": lambda: jobs_renderer(panel_width, panel_height), "history": lambda: self.history_tab(snap, app, panel_width, panel_height),
                  "cluster": lambda: self.cluster_tab(snap, app, panel_width, panel_height), "nodes": lambda: self.nodes_tab(snap, app, panel_width, panel_height),
                  "log": lambda: self.log_tab(snap, app, panel_width, panel_height), "sources": lambda: self.sources_tab(snap, app, panel_width, panel_height),
                  "analytics": lambda: self.analytics_tab(snap, app, panel_width, panel_height), "group": lambda: self.group_tab(snap, app, panel_width, panel_height),
                  "deps": lambda: self.deps_tab(snap, app, panel_width, panel_height),
                  "research": lambda: self.research_tab(snap, app, panel_width, panel_height),
                  "traces": lambda: self.trace_workspace_tab(snap, app, panel_width, panel_height)}.get(app.tab)
            return (self.plugin_tab(snap, app, panel_width, panel_height), []) if fn is None else fn()
        from .workspace_layout import render_body
        from .history_browser import wrap_render
        body, hits = wrap_render(self, snap, app, content_width, body_h,
            lambda panel_width, panel_height: render_body(self, snap, app, panel_width, panel_height, actions, default_renderer))
        terminal_ui.finalize_frame(app)
        chart_interaction.place_since(app, 0, dy=len(head), dx=content_x,
            clip=None if height is None else (len(head), content_x, max(len(head), height - 1), width))
        SB.place_since(app, 0, dy=len(head), dx=content_x,
            clip=None if height is None else (len(head), content_x, max(len(head), height - 1), width))
        hits = [shift_hit(y, kind, key, dx=content_x, dy=len(head)) for y, kind, key in hits] + terminal_header_hits
        body = prefix_rows(body, content_x, content_width)
        if height is None:
            return [L.clip_row(r, width) for r in head + body], hits
        rows = [L.clip_row(r, width) for r in head + body]   # every row fits the width, whatever the panel put in it
        while len(rows) < height - 1:
            rows.append([(" " * content_x, "bg:canvas")])
        rows = rows[:height - 1]
        from .sidebar import render as render_sidebar
        for sidebar_y, _, sidebar_row in render_sidebar(self, app, width, height):
            if 0 <= sidebar_y < len(rows):
                # prefix_rows reserves exactly one style segment. Replace it
                # without expanding or scanning every document character.
                rows[sidebar_y] = sidebar_row + rows[sidebar_y][1:]
        # Preserve the public destination hit map for extensions and native
        # navigation adapters; these rectangles now describe sidebar rows.
        app.tab_hits = [(y, left, right, identity.removeprefix("tab:"))
                        for y, left, right, kind, identity in app.sidebar_state["hits"]
                        if kind == "destination" and identity.startswith("tab:")]
        if app.tab == "jobs":
            from . import recent_history, pane_drag
            from .ui.workspace_state import Rect
            recent_state = recent_history.initialize(app)
            divider = next(((y, value) for y, kind, value in hits if kind == "control" and
                            isinstance(value, dict) and value.get("id") == "recent-divider"), None)
            main = getattr(app, "workspace_main_rect", None)
            if divider and main is not None:
                y, value = divider
                left, right = value["left"], value["right"]
                mode_controls = [control for line, kind, control in hits if line == y and kind == "control"
                                 and isinstance(control, dict) and control.get("group") == "job-list-controls"
                                 and str(control.get("id", "")).startswith("joblist:recent:")]
                # Only the visible handle resizes the panes. Mode buttons
                # share its row, while wheel navigation spans all data cells.
                content_right = max([right] + [control["right"] + 6 for control in mode_controls])
                padding = 1 if getattr(app.layout_state, "density", "compact") == "comfortable" and main.width >= 8 and main.height >= 7 else 0
                toolbar_height = getattr(app, "jobs_toolbar_height", 0)
                origin = main.y + 1 + padding + toolbar_height
                extent = max(1, getattr(app, "workspace_main_usable_height", main.height - 1) - 1 - toolbar_height)
                original_ratio, original_manual = recent_state.ratio, recent_state.manual_split
                def resize_recent(percentage):
                    recent_history.resize(app, 100 - percentage)
                    if percentage == 100 - original_ratio:
                        recent_state.manual_split = original_manual
                pane_drag.register(app, "recent:jobs", "horizontal", left, y, max(1, right - left), 1,
                    origin, extent, 100 - recent_state.ratio,
                    resize_recent,
                    minimum=10, maximum=90, label="Resize Queue and Recents")
                recent_state.rect = Rect(left, y + 2, max(0, content_right - left), max(0, main.y + main.height - padding - y - 2))
            else:
                recent_state.rect = None
        from .column_resize import publish as publish_columns
        publish_columns(app, rows, hits, width, height, ascii_=self.g.ascii)
        app.last_rows = [list(row) for row in rows]         # pristine content; copy excludes feedback glyphs
        if app.sel_anchor is not None:
            a, b = sorted((app.sel_anchor, min(app.sel_end, len(rows) - 1)))
            from .terminal.ui import _slice_row
            for y in range(max(0, a), b + 1):
                leading = _slice_row(rows[y], 0, content_x)
                selected = [(t, "sel") for t, _ in
                            L.clip_row(_slice_row(rows[y], content_x, width), max(0, content_width - 1))]
                rows[y] = leading + selected + [(" " * max(0, content_width - 1 - vlen(L.row_text(selected))), "sel"),
                                                ("*" if self.g.ascii else "◆", "fg:#fb923c+bold")]
        hits = [(y, kind, key) for y, kind, key in hits if 0 <= y < max(0, height - 1)]
        app.last_hits = hits
        from .ui.selection_state import publish as publish_selection
        publish_selection(app, rows, hits, width, height, snap=snap)
        from raven.jobs.group_drag import publish as publish_group_drag
        publish_group_drag(app, snap, rows, hits, width, height)
        from .group_queue_drag import publish as publish_queue_drag
        publish_queue_drag(app, rows, hits, width, height, ascii_=self.g.ascii)
        if app.tab == "jobs" and app.animations_enabled and width >= 4:
            from .transitions import paint_completion_trails
            rows = paint_completion_trails(rows, hits, app.completion.moving(),
                main=getattr(app, "workspace_main_rect", None), width=width, body_top=len(head),
                comfortable=getattr(app.layout_state, "density", "compact") == "comfortable",
                ascii_=self.g.ascii)
        app.completion.remember(hits)
        app.tab_hits = [hit for hit in app.tab_hits if hit[0] < height - 1 and hit[1] < hit[2]]
        output = rows + [L.clip_row(self.footer(app, width), width)]
        app.frame_rows = output
        from raven.jobs.progress import publish_animation
        publish_animation(app, output, hits, ascii_=self.g.ascii)
        if not feedback:
            return output, hits
        from .interaction import publish, decorate
        chart_interaction.publish(app, width, height)
        SB.publish(app, width, height)
        from .metric_live import descriptors as live_descriptors
        from .column_resize import descriptors as column_descriptors
        publish(app, output, hits, width, height, extra_controls=live_descriptors(app) + SB.descriptors(app) + tuple(column_descriptors(app)))
        from raven.jobs.group_drag import feedback as group_drag_feedback
        from .group_queue_drag import feedback as queue_drag_feedback
        return queue_drag_feedback(app, group_drag_feedback(app, decorate(app, output))), hits

    def step_lines(self, steps: Sequence[Step], width: int) -> List[Row]:
        """A small table of a job's steps (sstat for a running job, sacct -j for a finished one)."""
        rows = []
        for st in steps:
            rows.append(dict(step=st.name or st.id, state=st.state, tasks=st.ntasks or "", elapsed=st.elapsed, cpu=hms(st.cpu_time) if st.cpu_time is not None else "",
                             rss=human(st.rss) if st.rss else "", where=(f"task {st.rss_task}@{st.rss_node}" if st.rss_node and (st.ntasks or 0) > 1 else st.rss_node or st.nodelist),
                             slow=(f"task {st.min_cpu_task}@{st.min_cpu_node} {hms(st.min_cpu)}" if st.min_cpu is not None and (st.ntasks or 0) > 1 else ""), exit=st.exit,
                             _styles={"slow": "yellow" if (st.min_cpu is not None and st.cpu_time and st.min_cpu / st.cpu_time < 0.6) else ""}))
        cols = [Column("step", "STEP", 4, 10), Column("state", "STATE", 5, 10), Column("tasks", "TASKS", 5, 5, ">"), Column("elapsed", "ELAPSED", 7, 10), Column("cpu", "CPU TIME", 8, 11),
                Column("rss", "PEAK MEM", 8, 9, ">"), Column("where", "PEAK ON", 7, 18), Column("slow", "SLOWEST RANK", 12, 30), Column("exit", "EXIT", 4, 5)]
        trows, _ = table(cols, rows, width - 8, self.g.ascii, indent="   ", droppable=("exit", "state", "elapsed"))
        return [[(" steps", "bold")]] + trows

    def plugin_tab(self, snap: dict, app, width: int, height: Optional[int]) -> List[Row]:
        render = self.extra_tabs.get(app.tab)
        if render is None:
            return [rule(self.g, width, app.tab), [("   no such tab", "dim")]]
        try:
            rows = render(snap, app, width, height)
            return [list(r) for r in rows]
        except Exception as e:                            # a plugin's tab never takes the screen down
            return [rule(self.g, width, app.tab), [(f"   plugin tab failed: {type(e).__name__}: {e}", "red")]]

    # ---- overlays ---------------------------------------------------------------------------------
    def research_tab(self, snap, app, width, height):
        from .research_views import render
        return render(self, snap, app, width, height)

    def overlay(self, snap: dict, app, width: int, height: int, *, feedback=True):
        app.content_overlay_rows, app.toolbar_overlay_rows = [], []
        rows = self._overlay_content(snap, app, width, height)
        from .chart_interaction import publish as publish_charts
        if feedback:
            publish_charts(app, width, height)
            SB.publish(app, width, height, overlays=rows or ())
        if rows is not None and not app.content_overlay_rows and not app.toolbar_overlay_rows:
            app.content_overlay_rows = rows
        if feedback:
            from .interaction import publish, decorate_overlays
            from .metric_live import descriptors as live_descriptors
            from .column_resize import descriptors as column_descriptors
            publish(app, getattr(app, "frame_rows", getattr(app, "last_rows", [])),
                    getattr(app, "last_hits", []), width, height, overlays=rows or (),
                    extra_controls=live_descriptors(app) + SB.descriptors(app) + tuple(column_descriptors(app)))
            scroll_feedback = SB.feedback(app, ascii_=self.g.ascii)
            decorated = decorate_overlays(app, rows) if rows is not None else []
            return scroll_feedback + decorated if scroll_feedback or rows is not None else None
        return rows

    def _overlay_content(self, snap: dict, app, width: int, height: int):
        from .workbench import overlay
        enhanced = overlay(self, snap, app, width, height)
        if enhanced is not None and app.content_overlay_rows:
            return enhanced
        legacy = self._legacy_overlay(snap, app, width, height)
        if legacy is not None:
            app.content_overlay_rows = legacy
            return legacy + app.toolbar_overlay_rows
        return enhanced

    def _legacy_overlay(self, snap: dict, app, width: int, height: int):
        g = self.g
        if app.mode == "confirm" and app.confirm.get("action") == "submit":
            from .research_text import clean
            plan = app.confirm["plan"]
            lines = [[(" Submit this reviewed batch script?", "bold")],
                     [("   workdir: " + clean(plan["workdir"], g.ascii), "dim")]]
            text = clean(plan["command"], g.ascii, limit=131072)
            w = max(1, width - 12)
            for i in range(0, min(len(text), w * 12), w):
                lines.append([("   " + text[i:i + w], "cyan")])
            if len(text) > w * 12:
                lines.append([("   command continues; review exact argv with raven run prepare", "yellow")])
            for issue in plan.get("issues", [])[:6]:
                lines.append([("   " + clean(issue.get("message", ""), g.ascii), "yellow")])
            lines.append([("   y submits once; any other key keeps the prepared plan", "dim")])
            return box(g, lines, width, height, "submit")
        if app.mode == "confirm" and app.confirm.get("action") == "resubmit":
            c = app.confirm["clone"]
            lines = [[(f" Resubmit {c.id} {c.name}?", "bold")], [("", "")], [(f"   from the {c.source}, in {c.workdir or 'the current directory'}:", "dim")]]
            text = c.command()
            w = max(40, width - 12)
            while text:
                lines.append([("   " + text[:w], "cyan")])
                text = text[w:]
            lines.append([("", "")])
            fl = c.flags()
            res = "  ".join(f"{k} {fl[k]}" for k in ("partition", "cpus", "mem", "time", "gres", "nodes", "dependency") if k in fl)
            if res:
                lines.append([("   " + res, "")])
            lines.append([("   " + c.probe, "yellow" if "would start" in c.probe else "red")])
            for n in c.notes:
                lines.append([("   " + n, "yellow")])
            lines += [[("", "")], [("   y submits, any other key keeps it (:resubmit <id> --mem 12G --time 03:00:00 -c 4 --advised changes the flags)", "dim")]]
            return box(g, lines, width, height, "resubmit")
        if app.mode == "confirm":
            jobs = app.confirm["jobs"]
            verb = app.confirm["action"]
            lines: List[Row] = [[(f" {verb.capitalize()} {len(jobs)} job{'s' if len(jobs) != 1 else ''}?", "bold")], [("", "")]]
            for j in jobs[:12]:
                lines.append([(f"   {j.id:<12} {cut(j.name, 24, g.ascii):<24} {'pending' if j.pending else 'running'} on {j.partition}", "")])
            if len(jobs) > 12:
                lines.append([(f"   ... and {len(jobs) - 12} more", "dim")])
            lines += [[("", "")], [("   y confirms, any other key keeps them", "dim")]]
            return box(g, lines, width, height, "confirm")
        if app.mode == "help":
            k = app.keys_help
            keys = [(f"{k('up')} {k('down')} {k('page_up')} {k('page_down')} {k('home')} {k('end')}", "move; on the Log tab: scroll"),
                    (f"{k('next_tab')} {k('prev_tab')} 1-9, 0", "switch tabs; 0 opens Research (Left/Right views, PgUp/PgDn scroll)"),
                    (f"{k('mark')} {k('mark_all')} U", "mark a job, all visible jobs, none: actions apply to the marked jobs, else the selected one"),
                    ("g / u", "in a job list: group marked jobs; ungroup a collapsed group or selected members"),
                    (k("pin"), "pin / unpin the marked or selected jobs (first within batches and between batch ties; :tag, :note and filter #tag go with it)"),
                    (k("resubmit"), "clone and resubmit the selected job: the palette opens with resubmit <id>, add --mem --time -c --gres -p or --advised; sbatch --test-only previews"),
                    (k("details"), "the job's scontrol show job record and its steps (Esc closes); History: the series"),
                    (k("steps"), "details with the steps: sstat per step of a running job (slowest rank, peak memory), sacct -j of a finished one"),
                    (k("cancel"), "cancel (asks first)"), (k("hold"), "hold a pending job, release a held one (asks first)"),
                    (k("requeue"), "requeue a running job (asks first)"), (k("top"), "scontrol top: put a pending job first among your own"),
                    (k("log"), "the selected job's logs from Jobs, Recents or History"), (k("less"), "the displayed log in less; stdout from a job row (q returns)"),
                    (k("log_files"), "Log files: grouped browser across scheduler and declared locations; Enter opens, Esc returns, / filters"),
                    (f"{k('follow')} {k('filter')} {k('find_next')} {k('find_prev')}", "Log tab: pause / follow; search (a regular expression, highlighted); next / previous match"),
                    (f"{k('wrap')} {k('stderr')} {k('log_file')} {k('bookmark')} {k('bookmark_next')}", "Log tab: wrap long lines; stdout / stderr; cycle the job's other files (array tasks, steps, the GPU trace); bookmark the current line; jump to the next bookmark"),
                    (f"{k('replay_pause')} {k('replay_back')} {k('replay_fwd')} {k('replay_slower')} {k('replay_faster')}", "replay (--replay FILE): pause / play; 60 s back / forward; half / double the speed (:replay seek 10:30, :replay seek 50%)"),
                    (f"{k('log_lines')} {k('log_lines_less')}", "more / fewer log lines under the selected job"),
                    (f"{k('sort')} {k('reverse')}", "cycle the sort of the tab; reverse it"), (k("filter"), "filter by name, id, partition or info (Enter applies, Esc clears)"),
                    (k("gpu_toggle"), "GPU sampling on / off"), (k("bell_toggle"), "bell on start on / off"), (k("source_toggle"), "Sources tab: enable / disable the selected source"),
                    (k("refresh"), "sample every source now"),
                    (f"{k('visual')} {k('visual_all')} {k('yank')}", "select screen lines from the cursor row (arrows or Shift-click extend; right-click clears), all lines; copy them"),
                    (f"{k('export_text')} {k('export_csv')} {k('export_json')}", "export the tab as text; its table as CSV; the marked or selected jobs with their series as JSON (:export report: the whole dashboard as a terminal report)"),
                    (k("palette"), "command palette: cancel <ids>, hold, release, requeue, top, filter, sort, days, tab, export, profile, eval, advise, compare, tag, pin, note, chain ..."),
                    (f"{k('view_prev')} {k('view_next')} {k('days_more')} {k('days_less')}", "Analytics: previous / next view (job series, history, timeline, advisor, compare); a longer / shorter window.  Nodes: my nodes / the cluster map"),
                    (f"Deps tab: {k('cancel')} {k('hold')}", "cancel a job and everything that waits for it; hold / release the chain (asks first)"),
                    (k("theme"), "cycle the theme (default, mono, high contrast, cb: colour-blind safe, reader: plain text)"),
                    (k("help"), "this help"), (k("quit"), "quit (Esc closes an overlay or clears the filter)")]
            lines = [[(" keys", "bold")]] + [[(f"   {a:<26}", "cyan"), (b, "")] for a, b in keys]
            lines += [[("", "")], [(" figures", "bold")],
                      [("   CPU% is the rate between two accounting samples (30 s apart); EFF the CPU time over elapsed x cores so far; MEM% the peak", "")],
                      [("   resident set against the request; GPU% the mean utilisation at the last sample.  FLAGS: !cpu !mem !gpu when a job older than", "")],
                      [(f"   {self.th['warn_after_minutes']} min is below {100 * self.th['cpu']:.0f}% / {100 * self.th['mem']:.0f}% / {100 * self.th['gpu']:.0f}%; ending: under 10 min left; held, dep.  History: what seff reports, from sacct.", "")],
                      [("", "")], [(f" config {app.config_path or '(defaults; raven --write-config creates one)'}", "dim")]]
            return box(g, lines, width, height, "help")
        if app.mode == "details":
            jid = app.detail_id
            j = next((x for x in snap["jobs"] if x.id == jid), None)
            fin = next((x for x in snap["finished"] if x.id == jid), None) if j is None else None
            kv = snap["details"].get(jid, {})
            lines = [[(f" {jid} {j.name if j else (fin.name if fin else '')}", "bold")]]
            if fin is not None:
                d = self.finished_dict(fin)
                lines.append([(f"   {fin.state} {g.dot} {fin.partition} {g.dot} {fin.elapsed} on {fin.nodes} node(s), {fin.cpus} cpus" + (f", {fin.gpus} gpus" if fin.gpus else "")
                               + f" {g.dot} cpu eff {d['ce']} {g.dot} mem eff {d['me']} (peak {d['rss'] or '?'}) {g.dot} exit {fin.exit} {g.dot} {when(fin.start)} to {when(fin.end)}", "")])
                if fin.workdir:
                    lines.append([(f"   workdir {fin.workdir}", "dim")])
                steps = snap.get("fin_steps", {}).get(jid)
                if steps is None:
                    lines.append([("   fetching sacct -j ...", "dim")])
                else:
                    lines += self.step_lines(steps, width)
                app.scroll = min(app.scroll, max(0, len(lines) - 2))
                return box(g, lines[:1] + lines[1 + app.scroll:], width, height, "details (Esc closes, up/down scroll)")
            if not kv:
                lines.append([("   fetching scontrol show job ...", "dim")])
            steps = snap.get("steps", {}).get(jid, [])
            if steps:
                lines += self.step_lines(steps, width)
            first = ("JobState", "Reason", "Dependency", "Priority", "RunTime", "TimeLimit", "SubmitTime", "StartTime", "EndTime", "Partition", "NodeList", "NumNodes", "NumCPUs", "TRES",
                     "Account", "QOS", "StdOut", "StdErr", "WorkDir", "Command")
            keys = sorted(kv, key=lambda k: (k not in first, first.index(k) if k in first else 0, k))
            for k in keys:
                lines.append([(f"   {k:<22}", "cyan"), (cut(kv[k], width - 32, g.ascii), "")])
            app.scroll = min(app.scroll, max(0, len(lines) - 2))
            return box(g, lines[:1] + lines[1 + app.scroll:], width, height, "details (Esc closes, up/down scroll)")
        return None


# Keep the original identity even when an embedding overrides Views.job_rows.
_NATIVE_JOB_ROWS = Views.job_rows


def _table_job_rows(views, snap, app, actions):
    """Share native maps while preserving the public override signature."""
    builder = views.job_rows
    if getattr(builder, "__func__", None) is _NATIVE_JOB_ROWS:
        return builder(snap, app, actions, shared=True)
    return builder(snap, app, actions)


def step_lines_of(views, steps, width):
    return views.step_lines(steps, width)


def g_speed(c) -> str:
    return f"x{c.speed:g}" if c.speed != 1 else "x1"


def snap_name(snap: dict, jid: str) -> str:
    for j in snap["jobs"]:
        if j.id == jid:
            return j.name
    for f in snap["finished"]:
        if f.id == jid:
            return f"{f.name} {f.state.lower()}"
    return ""
