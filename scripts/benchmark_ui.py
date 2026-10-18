#!/usr/bin/env python3
"""Measure Raven UI CPU work with synthetic, published, in-memory data.

No Slurm connection, personal configuration, persistent state, report reader,
or terminal is required. Times exclude terminal transport and are diagnostics,
not pass/fail thresholds. Compare the same options on the same computer.
"""
from __future__ import annotations

import argparse
import builtins
import collections
import contextlib
import datetime
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark_source import load_module


def positive(value):
    number = int(value)
    if not 1 <= number <= 100_000:
        raise argparse.ArgumentTypeError("use an integer from 1 to 100000")
    return number


def summary(values):
    ordered = sorted(values)
    return {"median": statistics.median(values),
            "p95": ordered[min(len(ordered) - 1, math.ceil(.95 * len(ordered)) - 1)],
            "max": ordered[-1]}


class Measurements:
    def __init__(self):
        self.times, self.counts = collections.Counter(), collections.Counter()

    def reset(self):
        self.times.clear()
        self.counts.clear()

    def timed(self, name, callback):
        start = time.perf_counter()
        try:
            return callback()
        finally:
            self.times[name] += time.perf_counter() - start

    def wrap(self, stack, obj, name, *, count=None, phase=None):
        original = getattr(obj, name)

        def measured(*args, **kwargs):
            if count:
                self.counts[count] += 1
            if phase:
                return self.timed(phase, lambda: original(*args, **kwargs))
            return original(*args, **kwargs)

        stack.enter_context(patch.object(obj, name, measured))


def run_case(args, ascii_):
    source = getattr(args, "source_root", Path(__file__).resolve().parents[1])
    advisor, analysis_ui, charts, clock, interaction, L = (
        load_module(source, suffix) for suffix in
        ("advisor", "analysis_ui", "charts", "clock", "interaction", "layout"))
    screen, scrolling, startup, toolbar = (
        load_module(source, suffix) for suffix in
        ("screen", "scrolling", "startup", "toolbar"))
    Config = load_module(source, "config").Config
    App = load_module(source, "controller").App
    model = load_module(source, "model")
    Finished, Job, Live, Store = model.Finished, model.Job, model.Live, model.Store
    ResearchHub = load_module(source, "research").ResearchHub
    slurm_module = load_module(source, "slurm")
    FakeBackend, Slurm = slurm_module.FakeBackend, slurm_module.Slurm
    Views = load_module(source, "views").Views
    initialize_charts = (load_module(source, "chart_interaction").initialize
                         if args.gesture == "chart-hover" else None)

    cfg = Config({"log_lines": 0, "startup_animation": False,
                  "workspace": {"density": "compact", "split": 50}})
    store = Store(persist=False)
    store.apply_jobs([Job(str(i + 1), f"training-{i % 32}", "gpu",
        "PENDING" if i % 4 == 0 else "RUNNING", cpus=8, mem_req="8G",
        elapsed="00:10:00", limit="01:00:00") for i in range(args.jobs)])
    ended = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    store.apply_finished([Finished(str(100000 + i), f"training-{i % 128}",
        "FAILED" if i % 20 == 0 else "COMPLETED", elapsed="00:10:00",
        cpus=8, cpu_time=300, req_mem=8 << 30, rss=1 << 30,
        end=ended, limit="01:00:00") for i in range(args.history)])
    for job in store.jobs:
        if not job.pending:
            store.live[job.id] = Live(rate=.5, avg=.4, rss=1 << 30, cpu_time=300)
    backend = FakeBackend("benchmark", speed=0)
    slurm = Slurm(backend, "benchmark")
    app = App(store, None, None, cfg, "benchmark", ascii_=ascii_, interactive=True)
    if getattr(args, "ui_trace", False):
        app.ui_trace = load_module(source, "ui_trace").UITrace()
    app.headless = False
    views = Views(L.Glyphs(ascii_), cfg)
    app.views_ref = views
    hub = app.research = ResearchHub(cfg, slurm=slurm)
    hub.interval = 86400
    native_series = args.scenario == "analytics" or args.gesture == "chart-hover" and args.scenario == "jobs"
    if native_series or args.scenario == "advisor":
        record = next((job for job in store.jobs if job.state == "RUNNING"), store.jobs[0])
        # A one-job fixture otherwise contains only a pending allocation. The
        # published native graph benchmark needs an actual running identity.
        record.state = "RUNNING"
        app.selected_id = record.id
    if native_series:
        end = clock.now()
        for index in range(args.points):
            store.record(record.id, {"k": "live", "t": end - (args.points - index - 1) * .5,
                "cpu": .5 + .4 * math.sin(index / 20),
                "rss": (1 + .4 * math.sin(index / 30)) * (1 << 30)})
        if args.scenario == "analytics":
            app.tab, app.analytics_view, app.analytics_job = "analytics", "job", record.id
        else:
            app.run_command("jobpanel analytics job")
    elif args.scenario == "advisor":
        app.run_command("jobpanel analytics advisor")
    elif args.scenario == "research":
        app.tab, app.research_view, app.research_job_id = "research", "experiment", "1"
        points = [{"t": i, "value": math.sin(i / 100), "step": i} for i in range(args.points)]
        result = {"status": "ok", "records": args.points, "path": "published metrics snapshot",
                  "series": {f"metric_{i:02d}": points for i in range(args.metrics)}}
        context = hub.context(store.snapshot(), app)
        hub.cache[hub._key(context)] = (time.monotonic(), result)

    width, height = args.width, args.height
    app.width, app.height = width, height
    meter = Measurements()
    curses = SimpleNamespace(A_BOLD=1, A_DIM=2, A_REVERSE=4, A_UNDERLINE=8,
        has_colors=lambda: False, BUTTON1_CLICKED=1, BUTTON1_PRESSED=2,
        BUTTON1_RELEASED=4, BUTTON1_DOUBLE_CLICKED=8, BUTTON3_CLICKED=16,
        BUTTON3_PRESSED=32, BUTTON4_PRESSED=64, BUTTON5_PRESSED=128,
        REPORT_MOUSE_POSITION=256, BUTTON_SHIFT=512)
    palette = screen.CursesPalette(curses, enabled=False)

    def paint(y, x, row, _width=None, _height=None):
        for text, style in row:
            if x >= width or y >= height:
                break
            text = L.cut(text, width - x, True) if L.vlen(text) > width - x else text
            palette.attr(style, app.theme)
            cells = L.vlen(text)
            meter.counts["paint_calls"] += 1
            meter.counts["paint_cells"] += cells
            x += cells

    cached = hasattr(screen, "_FrameCache")
    cache = screen._FrameCache() if cached else None
    window = SimpleNamespace(erase=lambda: meter.counts.update(erase_calls=1))
    painter = screen._DifferentialPainter(window, paint) if cached else None

    def input_event(iteration, effects):
        if args.gesture == "render" or not app.last_hits:
            return
        if args.gesture == "chart-hover" and (app.tab != "jobs" or iteration % 3):
            plots = initialize_charts(app)["plots"]
            target = plots[iteration % len(plots)] if plots else None
            if target is None:
                raise RuntimeError("Chart hover requires a visible published graph; increase width or height")
            x = target.visible.left + iteration % (target.visible.right - target.visible.left)
            y = target.visible.top + iteration % (target.visible.bottom - target.visible.top)
            bits = curses.REPORT_MOUSE_POSITION
            meter.counts["chart_hover_events"] += 1
        elif args.gesture in ("hover", "chart-hover"):
            controls = interaction.controls(app)
            if args.gesture == "chart-hover":
                controls = [value for value in controls if value.group == "job"]
                target = next((value for value in controls if value.label == app.selected_id),
                              controls[0] if controls else None)
            else:
                target = controls[iteration % len(controls)] if controls else None
            if target is None:
                raise RuntimeError("Hover requires a visible control; increase width or height")
            x, y, bits = target.rect.left, target.rect.top, curses.REPORT_MOUSE_POSITION
            meter.counts["control_hover_events"] += 1
        else:
            rect = getattr(app, "job_panel_rect", None)
            if app.tab == "jobs" and rect is not None:
                x, y = rect.x + 2, rect.y + rect.height - 2
            else:
                x, y = 4, min(height - 2, getattr(app, "body_origin", 1) + 5)
            bits = curses.BUTTON5_PRESSED if iteration % 8 < 4 else curses.BUTTON4_PRESSED
        event = ("mouse", (0, x, y, 0, bits))
        effects.record(app, event, curses)
        meter.timed("input", lambda: screen._apply_input(app, event, app.last_hits, curses))

    def legacy_frame():
        # The published 4.3 screen always rebuilt the document after input.
        app.tick()
        snap = store.snapshot()
        scrolling.begin_frame(app)
        rows, hits = views.compose(snap, app, width, height)
        scrolling.finish_frame(app)
        app.last_hits = hits
        welcome = startup.overlay(views, snap, app, width, height) or []
        overlays = views.overlay(snap, app, width, height) or []
        pristine = getattr(app, "frame_rows", rows)
        interaction.publish(app, pristine, hits, width, height, overlays=welcome + overlays)
        rows = interaction.decorate(app, pristine)
        overlays = interaction.decorate_overlays(app, overlays)
        bar = interaction.decorate(app, [toolbar.render_bar(views, app, width)])[0]

        def draw():
            meter.counts["erase_calls"] += 1
            for y, row in enumerate(rows[:height]):
                paint(y, 0, row)
            for y, x, row in welcome + overlays:
                paint(y, x, row)
            paint(0, 0, bar)
            meter.counts["paint_rows"] += height

        meter.timed("paint", draw)

    def frame(iteration):
        meter.reset()

        def update():
            effects = (screen._InputEffects() if hasattr(screen, "_InputEffects") else
                       SimpleNamespace(document=args.gesture not in ("hover", "chart-hover"),
                                       record=lambda *_: None))
            input_event(iteration, effects)
            if cache is None:
                legacy_frame()
                return
            if effects.document or args.gesture == "render":
                cache.dirty = True
            if cache.due(app, width, height):
                meter.timed("document_rebuild", lambda: cache.rebuild(app, views, store, None, width, height))
            rows, overlays, bar = meter.timed("feedback", lambda: cache.feedback(app, views))
            changed = meter.timed("paint", lambda: painter.draw(rows, overlays, width, height, bar=bar))
            meter.counts["paint_rows"] += len(changed)

        meter.timed("frame", update)
        return dict(meter.times), dict(meter.counts)

    io_counts = collections.Counter()

    def forbidden(kind):
        def fail(*_args, **_kwargs):
            io_counts[kind] += 1
            raise RuntimeError(f"Unexpected {kind} during an in-memory UI benchmark")
        return fail

    try:
        # Imports, initial selection, card reflow and palette initialization
        # are outside steady-state measurements. Every source is already fake.
        for iteration in range(3):
            frame(iteration)
        initial_tab = app.tab
        initial_job = app.selected_id
        timings, counts = [], []
        with contextlib.ExitStack() as stack:
            # Editable import finders can locate a new child module even
            # under an older --source-root package. Inspect the actual row
            # builder before choosing its corresponding diagnostic hook.
            if "job_row_cache" in Views.job_rows.__code__.co_names:
                job_row_cache = load_module(source, "job_row_cache")
            else:
                job_row_cache = None
            if job_row_cache is None:
                meter.wrap(stack, views, "plugin_flags", count="prepared_job_rows")
            else:
                remember_rows = job_row_cache.remember

                def prepared_rows(target_views, target_app, key, rows):
                    meter.counts["prepared_job_rows"] += len(rows)
                    return remember_rows(target_views, target_app, key, rows)

                # An instance override of plugin_flags correctly disables the
                # native row cache. Count actual cache misses through its
                # publication function without changing renderer eligibility.
                stack.enter_context(patch.object(job_row_cache, "remember", prepared_rows))
            for obj, name, phase, count in (
                (app, "tick", "tick", "tick_calls"),
                (store, "snapshot", "snapshot", "snapshot_calls"),
                (views, "compose", "compose", "compose_calls"),
                (views, "overlay", "overlay", "overlay_calls"),
                (advisor, "advise_names", None, "advisor_aggregations"),
                (advisor, "advise_running", None, "running_advice"),
                (analysis_ui, "chart_rows", None, "chart_cards"),
                (charts, "braille_chart", None, "chart_rasters"),
                (interaction, "publish", "graph", "graph_publications"),
                (interaction, "decorate", "decorate", None)):
                meter.wrap(stack, obj, name, count=count, phase=phase)
            for obj, name, kind in ((builtins, "open", "open"), (os, "stat", "stat"),
                (os, "scandir", "scandir"), (Path, "open", "path_open"),
                (backend, "run", "scheduler_run"), (backend, "call", "scheduler_call"),
                (hub.pool, "submit", "worker_submit")):
                stack.enter_context(patch.object(obj, name, forbidden(kind)))
            for iteration in range(args.repeats):
                timing, count = frame(iteration)
                if app.tab != initial_tab:
                    raise RuntimeError("Pointer feedback unexpectedly changed the active page")
                if args.scenario == "jobs" and args.gesture == "chart-hover" and app.selected_id != initial_job:
                    raise RuntimeError("Crossing a job and its graphs unexpectedly changed the selected job")
                timings.append(timing)
                counts.append(count)
        phase_keys = set().union(*(item.keys() for item in timings))
        count_keys = set().union(*(item.keys() for item in counts))
        # Include zeros so a hover report exposes avoided document/source work.
        count_keys.update(("tick_calls", "snapshot_calls", "compose_calls", "overlay_calls",
            "prepared_job_rows", "advisor_aggregations", "running_advice", "chart_cards",
            "chart_rasters",
            "graph_publications", "paint_rows", "paint_cells", "paint_calls", "erase_calls"))
        return {"scenario": args.scenario, "gesture": args.gesture, "jobs": args.jobs,
            "history": args.history, "metrics": args.metrics, "points": args.points,
            "width": width, "height": height, "glyphs": "ascii" if ascii_ else "unicode",
            "pipeline": "cached screen" if cached else "4.3 screen", "repeats": args.repeats,
            "pointer_targets": {"graphs": sum(item.get("chart_hover_events", 0) for item in counts),
                                "controls": sum(item.get("control_hover_events", 0) for item in counts)},
            "phases_ms": {name: summary([1000 * item.get(name, 0) for item in timings]) for name in sorted(phase_keys)},
            "counts_per_frame": {name: summary([item.get(name, 0) for item in counts]) for name in sorted(count_keys)},
            "io_attempts": dict(io_counts), "ui_trace": bool(getattr(args, "ui_trace", False)),
            "trace_timeline": app.ui_trace.report().get("timeline") if getattr(args, "ui_trace", False) else None}
    finally:
        hub.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("jobs", "advisor", "analytics", "research"), default="jobs")
    parser.add_argument("--gesture", choices=("hover", "chart-hover", "wheel", "render"), default="hover",
                        help="chart-hover crosses Jobs rows and native Details graphs, or published page graphs")
    parser.add_argument("--jobs", type=positive, default=100)
    parser.add_argument("--history", type=positive, default=100)
    parser.add_argument("--repeats", type=positive, default=20)
    parser.add_argument("--width", type=positive, default=120)
    parser.add_argument("--height", type=positive, default=36)
    parser.add_argument("--glyphs", choices=("unicode", "ascii", "both"), default="unicode")
    parser.add_argument("--metrics", type=positive, default=8)
    parser.add_argument("--points", type=positive, default=1000)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="checkout or extracted archive containing the selected source package")
    parser.add_argument("--label", default="", help="optional commit or comparison label")
    parser.add_argument("--output", type=Path, help="save the full JSON report")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a short report")
    parser.add_argument("--ui-trace", action="store_true", help="include opt-in UI timing/timeline capture overhead; no trace file is written")
    args = parser.parse_args()
    if args.gesture == "chart-hover" and args.scenario == "advisor":
        parser.error("chart-hover supports jobs, analytics, and research; use hover for advisor controls")
    if args.metrics > 64:
        parser.error("--metrics must be at most 64")
    if args.repeats > 1000:
        parser.error("--repeats must be at most 1000")
    if args.width < 40 or args.height < 16:
        parser.error("use at least --width 40 --height 16")
    source = args.source_root.resolve()
    try:
        package = load_module(source)
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        parser.error(str(exc))
    cases = [run_case(args, ascii_) for ascii_ in
             ([False, True] if args.glyphs == "both" else [args.glyphs == "ascii"])]
    report = {"metadata": {"label": args.label, "source_root": str(source), "version": package.__version__,
        "python": sys.version, "platform": platform.platform(), "interactive": True, "headless": False,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "measurement": "CPU with a counting paint sink; excludes terminal transport and wait times",
        "sources": "synthetic memory fixtures; scheduler, filesystem and worker I/O are forbidden during measured frames"},
        "results": cases}
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    if args.json:
        print(encoded, end="")
    else:
        for result in cases:
            frame = result["phases_ms"]["frame"]
            print(f"{result['scenario']} / {result['gesture']} / {result['glyphs']} / {result['pipeline']}: "
                  f"median {frame['median']:.3f} ms, p95 {frame['p95']:.3f} ms")
            median = lambda key: result["counts_per_frame"][key]["median"]
            print(f"  Work per frame (median): {median('compose_calls'):g} compose, "
                  f"{median('prepared_job_rows'):g} job rows, {median('advisor_aggregations'):g} advisor aggregations, "
                  f"{median('chart_cards'):g} chart card requests / {median('chart_rasters'):g} rasters, "
                  f"{median('graph_publications'):g} graph publications, "
                  f"{median('paint_rows'):g} painted rows, {median('paint_cells'):g} cells; "
                  f"I/O attempts {sum(result['io_attempts'].values())}")
        if args.output:
            print(f"Full report: {args.output}")


if __name__ == "__main__":
    main()
