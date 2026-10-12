#!/usr/bin/env python3
"""Measure changing native graphs with published, in-memory observations.

Run with python -S so an editable-package finder cannot select the live checkout
when --source-root is an isolated git archive. Measurements use perf_counter;
UI clocks advance deterministically by 20 ms per pointer frame.

Default cases cross selected Jobs rows and their Details graphs, publish native
CPU/GPU samples every 500 ms, redraw Live windows at 10 Hz, switch selected jobs,
and resize the viewport. The optional scroll-end case clicks the published
Details scrollbar jump buttons. Four static GPU trace sources accompany the
four native GPU devices. Scheduler, filesystem, and worker I/O are forbidden
during measured frames. Times exclude terminal transport and producer latency;
compare identical options on the same computer. Profiled timings include
instrumentation overhead and must not be compared with unprofiled timings.
"""
from __future__ import annotations

import argparse
import builtins
import collections
import contextlib
import cProfile
import io
import json
import math
import os
from pathlib import Path
import platform
import pstats
import statistics
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark_source import load_module

CASES = ("static", "live30", "live1", "switch", "resize", "scroll-end")


def positive(value):
    number = int(value)
    if not 1 <= number <= 100_000:
        raise argparse.ArgumentTypeError("use an integer from 1 to 100000")
    return number


def summary(values):
    ordered = sorted(values)
    return {"median": statistics.median(values), **{
        f"p{percent}": ordered[min(len(ordered)-1, math.ceil(percent/100*len(ordered))-1)]
        for percent in (95, 99)}, "max": ordered[-1]}


class Meter:
    def reset(self):
        self.times, self.counts = collections.Counter(), collections.Counter()

    def timed(self, name, callback):
        start = time.perf_counter()
        try:
            return callback()
        finally:
            self.times[name] += time.perf_counter() - start

    def wrap(self, stack, obj, name, phase, count=None):
        original = getattr(obj, name)
        def measured(*args, **kwargs):
            if count:
                self.counts[count] += 1
            return self.timed(phase, lambda: original(*args, **kwargs))
        stack.enter_context(patch.object(obj, name, measured))


class Clock:
    def __init__(self):
        self.wall, self.mono = 1791500000., 10000.

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


def run_case(args, kind, ascii_):
    source = getattr(args, "source_root", Path(__file__).resolve().parents[1])
    charts, clock, interaction, L, metric_live, screen, chart_interaction = (
        load_module(source, suffix) for suffix in
        ("charts", "clock", "interaction", "layout", "metric_live", "screen", "chart_interaction"))
    Config = load_module(source, "config").Config
    App = load_module(source, "controller").App
    model = load_module(source, "model")
    Finished, Job, Live, Store = model.Finished, model.Job, model.Live, model.Store
    ResearchHub = load_module(source, "research").ResearchHub
    slurm_module = load_module(source, "slurm")
    FakeBackend, Slurm = slurm_module.FakeBackend, slurm_module.Slurm
    Views = load_module(source, "views").Views
    layout_state = (load_module(source, "workspace_layout").initialize
                    if kind == "scroll-end" else None)

    simulated = Clock()
    cfg = Config({"log_lines": 0, "startup_animation": False,
        "workspace": {"density": "compact", "split": 50}})
    store = Store(persist=False, series_keep=args.points*2+1000)
    store.apply_jobs([Job(str(index+1), f"training-{index % 32}", "gpu",
        "PENDING" if index % 4 == 0 else "RUNNING", cpus=8, mem_req="8G",
        elapsed="00:10:00", limit="01:00:00") for index in range(args.jobs)])
    store.apply_finished([Finished(str(100000+index), f"training-{index % 128}",
        "FAILED" if index % 20 == 0 else "COMPLETED", elapsed="00:10:00",
        cpus=8, cpu_time=300, req_mem=8 << 30, rss=1 << 30,
        end="2026-10-09T00:00:00", limit="01:00:00") for index in range(args.history)])
    for job in store.jobs:
        if not job.pending:
            store.live[job.id] = Live(rate=.5, avg=.4, rss=1 << 30, cpu_time=300)
    records = [job for job in store.jobs if not job.pending][:2]
    if len(records) < 2:
        raise RuntimeError("Use at least three jobs")
    for job in records:
        job.gpus = 4
        for index in range(args.points):
            timestamp = simulated.wall - (args.points-index-1)*.5
            store.record(job.id, {"k": "live", "t": timestamp,
                "cpu": .5+.4*math.sin(index/20),
                "rss": (1+.4*math.sin(index/30))*(1 << 30)})
            store.record(job.id, {"k": "gpu", "t": timestamp,
                "gpu": {str(device): [50+40*math.sin(index/20+device)] for device in range(4)}})
        store.trace[job.id] = [{"t": simulated.wall-(args.points-index-1)*60,
            "index": device, "util": 50+40*math.sin(index/20+device)}
            for device in range(4) for index in range(args.points)]

    backend = FakeBackend("benchmark", speed=0)
    app = App(store, None, None, cfg, "benchmark", ascii_=ascii_, interactive=True)
    app.headless = False
    views = Views(L.Glyphs(ascii_), cfg)
    app.views_ref = views
    hub = app.research = ResearchHub(cfg, slurm=Slurm(backend, "benchmark"))
    hub.interval = 86400
    app.selected_id = records[0].id
    app.run_command("jobpanel analytics job")
    width, height = args.width, args.height
    app.width, app.height = width, height
    meter = Meter()
    meter.reset()
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
            text = L.cut(text, width-x, True) if L.vlen(text) > width-x else text
            palette.attr(style, app.theme)
            cells = L.vlen(text)
            meter.counts["paint_calls"] += 1
            meter.counts["paint_cells"] += cells
            x += cells

    cache = screen._FrameCache()
    painter = screen._DifferentialPainter(SimpleNamespace(erase=lambda: None), paint)
    counts_io = collections.Counter()
    geometry_failures = []

    def forbidden(name):
        def fail(*args, **kwargs):
            counts_io[name] += 1
            raise RuntimeError(f"Unexpected {name} during measured fixture-only UI frame")
        return fail

    def enable_visible():
        if kind == "static":
            return
        for key in list(metric_live.initialize(app)["entries"]):
            if key[1] == app.selected_id:
                metric_live.set_delta(app, key, 1. if kind == "live1" else 30.)
                metric_live.set_enabled(app, key, True)

    def redraw():
        meter.timed("document_rebuild", lambda: cache.rebuild(app, views, store, None, width, height))

    def draw_feedback():
        rows, overlays, bar = meter.timed("feedback", lambda: cache.feedback(app, views))
        changed = meter.timed("paint", lambda: painter.draw(rows, overlays, width, height, bar=bar))
        meter.counts["paint_rows"] += len(changed)

    def event(iteration, effects):
        controls = interaction.controls(app)
        if kind == "scroll-end" and iteration % 40 == 0:
            direction = "bottom" if iteration % 80 == 0 else "top"
            target = next((control for control in controls
                if control.id == "scroll:workspace:jobs:details:" + direction), None)
            if target is None:
                raise RuntimeError("End-jump fixture requires the published Details scroll controls")
            x, y, bits, gesture = target.rect.left, target.rect.top, curses.BUTTON1_CLICKED, "scroll_" + direction
        elif kind == "switch" and iteration and iteration % 40 == 0:
            selected = records[1].id if app.selected_id == records[0].id else records[0].id
            target = next((control for control in controls
                if control.group == "job" and control.label == selected), None)
            if target is None:
                raise RuntimeError("Job-switch fixture requires both first two running job rows")
            x, y, bits, gesture = target.rect.left, target.rect.top, curses.BUTTON1_CLICKED, "job_click"
        elif iteration % 3 == 0:
            target = next((control for control in controls
                if control.group == "job" and control.label == app.selected_id), None)
            if target is None:
                raise RuntimeError("No visible selected-job control")
            x, y, bits, gesture = target.rect.left, target.rect.top, curses.REPORT_MOUSE_POSITION, "job_hover"
        else:
            plots = chart_interaction.initialize(app)["plots"]
            if not plots:
                if kind != "scroll-end":
                    raise RuntimeError("No visible graph in Jobs Details")
                meter.counts["missing_visible_graphs"] += 1
                if len(geometry_failures) < 4:
                    geometry_failures.append({"iteration": iteration,
                        "scroll": dict(layout_state(app).scroll),
                        "document_windows": {str(key): value for key, value in
                            app.job_panel_state.get("document_windows", {}).items()},
                        "rows": [L.row_text(row) for row in cache.rows]})
                target = next((control for control in controls
                    if control.group == "job" and control.label == app.selected_id), None)
                if target is None:
                    raise RuntimeError("No visible selected-job control after end jump")
                x, y = target.rect.left, target.rect.top
                bits, gesture = curses.REPORT_MOUSE_POSITION, "blank_graph_hover"
            else:
                target = plots[iteration % len(plots)]
                x = target.visible.left + iteration % max(1, target.visible.right-target.visible.left)
                y = target.visible.top + iteration % max(1, target.visible.bottom-target.visible.top)
                bits, gesture = curses.REPORT_MOUSE_POSITION, "chart_hover"
        item = ("mouse", (0, x, y, 0, bits))
        meter.counts[gesture] += 1
        effects.record(app, item, curses)
        meter.timed("input", lambda: screen._apply_input(app, item, app.last_hits, curses))
        return gesture

    def publish(iteration):
        for job in records:
            store.record(job.id, {"k": "live", "t": simulated.wall,
                "cpu": .5+.4*math.sin(iteration/20),
                "rss": (1+.4*math.sin(iteration/30))*(1 << 30)})
            store.record(job.id, {"k": "gpu", "t": simulated.wall,
                "gpu": {str(device): [50+40*math.sin(iteration/20+device)] for device in range(4)}})

    def frame(iteration):
        nonlocal width, height
        meter.reset()
        simulated.advance(.02)
        publication = iteration % 25 == 0
        if publication:
            publish(iteration)
        resized = kind == "resize" and iteration and iteration % 50 == 0
        if resized:
            width = args.width-20 if width == args.width else args.width
            height = args.height-6 if height == args.height else args.height
        gesture = ""
        reasons = []
        def update():
            nonlocal gesture
            effects = screen._InputEffects()
            gesture = event(iteration, effects)
            if effects.document:
                cache.dirty = True
                reasons.append("input")
            if resized:
                reasons.append("resize")
            if simulated.mono >= cache.next_maintenance:
                reasons.append("maintenance")
            if simulated.mono >= cache.next_live:
                reasons.append("live")
            if cache.due(app, width, height):
                redraw()
                if gesture in ("job_click", "scroll_top", "scroll_bottom"):
                    enable_visible()
            draw_feedback()
            if app.tab != "jobs":
                raise RuntimeError(f"Pointer changed page to {app.tab}")
        meter.timed("frame", update)
        result = {"iteration": iteration, "simulated_seconds": round((iteration+1)*.02, 3),
            "publication": publication, "resized": bool(resized), "gesture": gesture,
            "reasons": reasons, "selected_id": app.selected_id,
            "phases_ms": {key: value*1000 for key, value in meter.times.items()},
            "counts": dict(meter.counts)}
        if kind == "scroll-end":
            result["visible_metrics"] = sorted({plot.key[2]
                for plot in chart_interaction.initialize(app)["plots"]})
            result["details_scroll"] = layout_state(app).scroll.get("jobs:details", 0)
        return result

    try:
        with patch.object(time, "monotonic", lambda: simulated.mono), patch.object(clock, "_source", lambda: simulated.wall):
            # Preload each actual job attempt, native control entry, and paint.
            for record in reversed(records):
                app.selected_id = record.id
                redraw()
                enable_visible()
                redraw()
                draw_feedback()
            samples = []
            profile = cProfile.Profile() if args.profile else None
            with contextlib.ExitStack() as stack:
                for obj, name, phase, count in (
                    (app, "tick", "tick", "tick_calls"),
                    (store, "snapshot", "snapshot", "snapshot_calls"),
                    (views, "compose", "compose", "compose_calls"),
                    (views, "overlay", "overlay", "overlay_calls"),
                    (views._metric_rasters, "render", "raster_cache", "raster_requests"),
                    (charts, "braille_chart", "raster", "chart_rasters"),
                    (charts, "vbar_chart", "raster", "area_rasters"),
                    (interaction, "publish", "graph", "graph_publications")):
                    meter.wrap(stack, obj, name, phase, count)
                # Do not wrap plugin_flags: native job row caches compare its
                # bound implementation identity. Instrumenting it changes work.
                for obj, name, label in (
                    (builtins, "open", "open"), (os, "stat", "stat"),
                    (os, "scandir", "scandir"), (Path, "open", "path_open"),
                    (backend, "run", "scheduler_run"), (backend, "call", "scheduler_call"),
                    (hub.pool, "submit", "worker_submit")):
                    stack.enter_context(patch.object(obj, name, forbidden(label)))
                for iteration in range(args.repeats):
                    if profile is not None and iteration == args.profile_iteration:
                        profile.enable()
                    samples.append(frame(iteration))
                    if profile is not None and iteration == args.profile_iteration:
                        profile.disable()
            if profile is not None:
                prefix = str(args.output.with_suffix("")) + f"-{kind}-{'ascii' if ascii_ else 'unicode'}"
                profile.dump_stats(prefix+".prof")
                text = io.StringIO()
                pstats.Stats(profile, stream=text).strip_dirs().sort_stats("cumulative").print_stats(45)
                pstats.Stats(profile, stream=text).strip_dirs().sort_stats("tottime").print_stats(30)
                Path(prefix+"-profile.txt").write_text(text.getvalue())
            keys = sorted(set().union(*(item["phases_ms"] for item in samples)))
            count_keys = sorted(set().union(*(item["counts"] for item in samples)))
            rebuilds = [item for item in samples if item["counts"].get("compose_calls")]
            return {"case": kind, "glyphs": "ascii" if ascii_ else "unicode",
                "simulated_seconds": args.repeats*.02, "repeats": args.repeats,
                "live_window": None if kind == "static" else 1 if kind == "live1" else 30,
                "visible_plots": len(chart_interaction.initialize(app)["plots"]),
                "native_publications": sum(item["publication"] for item in samples),
                "phases_ms": {key: summary([item["phases_ms"].get(key, 0.) for item in samples]) for key in keys},
                "rebuild_phases_ms": {key: summary([item["phases_ms"].get(key, 0.) for item in rebuilds]) for key in keys} if rebuilds else {},
                "counts_per_frame": {key: summary([item["counts"].get(key, 0) for item in samples]) for key in count_keys},
                "io_attempts": dict(counts_io), "geometry_failures": geometry_failures,
                "worst_frames": sorted(samples, key=lambda item: item["phases_ms"]["frame"], reverse=True)[:12],
                "frames": samples}
    finally:
        hub.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1],
        help="checkout or extracted git archive containing the selected source package")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", default="")
    parser.add_argument("--cases", default="static,live30,live1,switch,resize",
        help="comma-separated cases: " + ",".join(CASES))
    parser.add_argument("--glyphs", choices=("ascii", "unicode", "both"), default="unicode")
    parser.add_argument("--repeats", type=positive, default=200)
    parser.add_argument("--points", type=positive, default=4000)
    parser.add_argument("--jobs", type=positive, default=1000)
    parser.add_argument("--history", type=positive, default=3000)
    parser.add_argument("--width", type=positive, default=320)
    parser.add_argument("--height", type=positive, default=52)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--profile-iteration", type=int, default=4,
        help="profile one frame; frame 4 is the first scheduled Live redraw")
    args = parser.parse_args()
    cases = args.cases.split(",")
    if any(case not in CASES for case in cases):
        parser.error("--cases must contain one or more of: " + ",".join(CASES))
    if args.jobs < 3:
        parser.error("use at least --jobs 3 for two running job attempts")
    if args.width < 80 or args.height < 24:
        parser.error("use at least --width 80 --height 24")
    if args.repeats > 1000:
        parser.error("--repeats must be at most 1000")
    if args.profile and not 0 <= args.profile_iteration < args.repeats:
        parser.error("--profile-iteration must identify a measured frame")
    source = args.source_root.resolve()
    try:
        package = load_module(source)
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        parser.error(str(exc))
    cases = [run_case(args, kind, ascii_) for kind in cases
        for ascii_ in ([False, True] if args.glyphs == "both" else [args.glyphs == "ascii"])]
    report = {"metadata": {"label": args.label, "source_root": str(source),
        "module_path": package.__file__, "version": package.__version__,
        "python": sys.version, "platform": platform.platform(),
        "measurement": "Elapsed UI work with a counting paint sink; deterministic 50 Hz pointer events, 10 Hz Live window redraws, native publication every 500 ms; terminal transport and external producer I/O excluded",
        "timer": "perf_counter; includes operating-system scheduling variation",
        "configuration": {"jobs": args.jobs, "history": args.history,
            "points_per_source_kind": args.points, "native_gpu_devices": 4,
            "trace_devices": 4, "width": args.width, "height": args.height},
        "trace": "Four static trace cards per job, 60 s producer cadence; four GPU devices publish every 500 ms",
        "profiled_iteration": args.profile_iteration if args.profile else None}, "results": cases}
    args.output.write_text(json.dumps(report, indent=2)+"\n")
    for result in cases:
        print(result["case"], result["glyphs"], "frame", result["phases_ms"]["frame"],
              "rebuild", result["rebuild_phases_ms"].get("compose"), "I/O", result["io_attempts"])


if __name__ == "__main__":
    main()
