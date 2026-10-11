#!/usr/bin/env python3
"""Measure the embedded PTY -> cached frame -> differential painter path.

Uses synthetic Jobs records and real finite PTY children. No Slurm commands,
network connections, shell startup files, or physical terminal writes occur.
The paint endpoint is an in-memory cell sink, not an emulator's visible pixels.
Run alone after correctness suites to avoid concurrent-load distortion.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gc
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import threading
import time
from types import SimpleNamespace


PROBE = r'''
import os, select, sys, termios, time, tty
limit=int(sys.argv[1]); duration=float(sys.argv[2]); only_flood=sys.argv[3]=='1'
tty.setraw(0)
def write(data):
    value=memoryview(data)
    while value:
        value=value[os.write(1,value):]
write(b'\x1b[2J\x1b[HREADY\r\n')
started=time.monotonic(); deadline=started+30; sent=0; pending=b''
next_flood=started
while time.monotonic()<deadline:
    now=time.monotonic()
    if sent<limit and now>=next_flood:
        # Change every cell in the bounded block, rather than repeatedly paint
        # identical text and measure only parser throughput.
        sequence=sent//1024
        chunk=(f'frame {sequence:06d} '+chr(65+sequence%26)*52+'\r\n').encode()*16
        chunk=chunk[:1024]
        part=chunk[:min(len(chunk),limit-sent)]
        # Keep the ACK row stable while the visible terminal is flooded.
        write(b'\x1b[3;r\x1b[3;1H'+part)
        sent+=len(part)
        next_flood=started+duration*sent/max(1,limit)
        if sent==limit:
            write(b'\x1b[2;1H\x1b[2KFLOOD-DONE')
            if only_flood: break
    timeout=min(.004,max(0,next_flood-time.monotonic())) if sent<limit else .01
    readable,_,_=select.select([0],[],[],timeout)
    if readable:
        data=os.read(0,4096)
        if not data: break
        pending+=data
        while b'\n' in pending:
            line,pending=pending.split(b'\n',1)
            if line.startswith(b'PING'):
                write(b'\x1b[1;1H\x1b[2KACK'+line[4:])
'''


def summary(values):
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {"count": len(ordered), "p50_ms": round(statistics.median(ordered), 4),
            "p95_ms": round(ordered[min(len(ordered)-1, math.ceil(len(ordered)*.95)-1)], 4),
            "p99_ms": round(ordered[min(len(ordered)-1, math.ceil(len(ordered)*.99)-1)], 4),
            "max_ms": round(ordered[-1], 4)}


def cgroup_cpu():
    try:
        rows = Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines()
        return {key: int(value) for key, value in (row.split() for row in rows)}
    except (OSError, ValueError):
        return {}


def fixture(directory, jobs):
    from tower import layout as L
    from tower.actions import Actions
    from tower.config import Config
    from tower.controller import App
    from tower.model import Job, Store
    from tower.research import ResearchHub
    from tower.sampler import Sampler
    from tower.slurm import FakeBackend, Slurm
    from tower.views import Views
    from tower.worker_scheduler import WorkerScheduler

    cfg = Config()
    for key, value in (("startup_animation", False), ("animations", False),
                       ("log_lines", 0), ("gpu_sampling", False)):
        cfg.set(key, value)
    store = Store(persist=False)
    store.jobs = [Job(str(1000+i), "terminal benchmark " + str(i), "fixture", "RUNNING",
                      elapsed="00:01:00", limit="01:00:00", cpus=4,
                      submit="2026-10-11T10:00:00", start="2026-10-11T10:01:00") for i in range(jobs)]
    slurm = Slurm(FakeBackend(), "benchmark")
    scheduler = WorkerScheduler()
    sampler = Sampler(slurm, store, cfg["intervals"], cfg["gpu_types"],
                      weather=False, budget=False, worker_scheduler=scheduler)
    actions = Actions(slurm, store)
    app = App(store, sampler, actions, cfg, "benchmark", interactive=False)
    app.research = ResearchHub(cfg, slurm=slurm, worker_scheduler=scheduler)
    app.worker_scheduler = scheduler
    app.state_dir = str(directory)
    app.selected_id = store.jobs[0].id
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    return SimpleNamespace(app=app, views=views, store=store, actions=actions,
                           sampler=sampler, scheduler=scheduler)


def scenario(args, name, directory):
    from tower import layout as L, screen, terminal_ui as T
    from tower.terminal_service import TerminalService
    from tower.ui_trace import UITrace
    f = fixture(directory, args.jobs)
    app, views = f.app, f.views
    service = TerminalService()
    state = T.initialize(app)
    state.service = service
    timings = defaultdict(list)
    counts = Counter()
    painted_cells = Counter()
    invalid = []
    output_frames = 0
    last_sequence = -1
    checking_feedback = False
    gc_events, gc_started = [], {}
    diagnostic_events = []
    measurement_started = None
    last_paint_at = None
    owner_thread = threading.get_ident()
    def observe_gc(phase, info):
        if threading.get_ident() != owner_thread:
            return
        generation = info["generation"]
        if phase == "start":
            gc_started[generation] = (time.perf_counter(), time.thread_time())
        elif generation in gc_started:
            wall, cpu = gc_started.pop(generation)
            if len(gc_events) < 256:
                gc_events.append({"at": wall, "generation": generation,
                                  "wall_ms": (time.perf_counter()-wall)*1000,
                                  "cpu_ms": (time.thread_time()-cpu)*1000,
                                  "collected": info.get("collected", 0)})
    gc.callbacks.append(observe_gc)
    cache = screen._FrameCache()
    original_compose = views.compose

    def compose(*values, **kwargs):
        counts["jobs_document_compositions"] += 1
        return original_compose(*values, **kwargs)
    views.compose = compose

    def paint(y, x, row, width, height):
        nonlocal checking_feedback
        amount = sum(L.vlen(text) for text, _style in row)
        painted_cells["all"] += amount
        rect = state.rect
        if checking_feedback and rect is not None:
            if not rect.y <= y < rect.y + rect.height:
                painted_cells["feedback_outside_terminal"] += amount
            else:
                inside = max(0, min(x+amount, rect.x+rect.width)-max(x,rect.x))
                painted_cells["feedback_outside_terminal"] += amount-inside

    painter = screen._DifferentialPainter(SimpleNamespace(erase=lambda: None), paint)

    def frame():
        nonlocal output_frames, last_sequence, checking_feedback, last_paint_at
        started, started_cpu = time.perf_counter(), time.thread_time()
        T.tick(app)
        timings["poll"].append((time.perf_counter()-started)*1000)
        timings["poll_cpu"].append((time.thread_time()-started_cpu)*1000)
        before_due = time.monotonic()
        ordinary_deadline = min(cache.next_maintenance, cache.next_animation, cache.next_live, cache.next_preview)
        ordinary_rebuild = cache.dirty or cache.geometry != (args.width, args.height) or before_due >= ordinary_deadline
        rebuild = cache.due(app, args.width, args.height)
        if rebuild:
            if not ordinary_rebuild:
                counts["document_rebuilds_before_normal_deadline"] += 1
            started, started_cpu = time.perf_counter(), time.thread_time()
            cache.rebuild(app, views, f.store, f.actions, args.width, args.height)
            timings["document"].append((time.perf_counter()-started)*1000)
            timings["document_cpu"].append((time.thread_time()-started_cpu)*1000)
        seq = service.snapshot(state.active).get("seq", -1)
        if seq != last_sequence:
            output_frames += 1
            last_sequence = seq
        before = counts["jobs_document_compositions"]
        started, started_cpu = time.perf_counter(), time.thread_time()
        rows, overlays, bar = cache.feedback(app, views)
        timings["feedback"].append((time.perf_counter()-started)*1000)
        timings["feedback_cpu"].append((time.thread_time()-started_cpu)*1000)
        if counts["jobs_document_compositions"] != before:
            invalid.append("feedback rebuilt the Jobs document")
        checking_feedback = not rebuild
        if not rebuild:
            counts["cached_feedback_frames"] += 1
        started, started_cpu = time.perf_counter(), time.thread_time()
        painter.draw(rows, overlays, args.width, args.height, bar=bar)
        last_paint_at = time.perf_counter()
        timings["differential_paint"].append((time.perf_counter()-started)*1000)
        timings["differential_paint_cpu"].append((time.thread_time()-started_cpu)*1000)
        checking_feedback = False
        counts["frames"] += 1
        return rows

    def visible_text(rows):
        rect = state.body_rect
        if rect is None:
            return ""
        return "\n".join(L.row_text(T._slice_row(row, rect.x, rect.x+rect.width))
                         for row in rows[rect.y:rect.y+rect.height])

    def wait_for(marker, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            loop_started = time.perf_counter()
            rows = frame()
            started, started_cpu = time.perf_counter(), time.thread_time()
            found = marker in visible_text(rows)
            timings["marker_scan"].append((time.perf_counter()-started)*1000)
            timings["marker_scan_cpu"].append((time.thread_time()-started_cpu)*1000)
            if found:
                return True
            current = service.snapshot(state.active)
            if current.get("status") in {"error", "exited"}:
                invalid.append("probe ended before its expected output: " + str(current.get("error")))
                return False
            requested = min(args.poll_ms / 1000, cache.wait_ms() / 1000)
            started, started_cpu = time.perf_counter(), time.thread_time()
            time.sleep(requested)
            ended = time.perf_counter()
            waited_ms = (ended-started)*1000
            timings["readiness_wait"].append(waited_ms)
            timings["readiness_wait_cpu"].append((time.thread_time()-started_cpu)*1000)
            timings["readiness_overshoot"].append(max(0, waited_ms-requested*1000))
            if measurement_started is not None and ended-loop_started >= .060 and len(diagnostic_events) < 256:
                diagnostic_events.append({"at_seconds": loop_started-measurement_started,
                                          "loop_ms": (ended-loop_started)*1000,
                                          "wait_ms": waited_ms,
                                          "requested_wait_ms": requested*1000,
                                          "marker_scan_ms": timings["marker_scan"][-1]})
        return False

    try:
        duration = args.samples * args.interval_ms / 1000 + 1
        visible_budget = args.flood_bytes if name == "visible-flood" else 0
        sid = service.open([sys.executable, "-u", "-c", PROBE, str(visible_budget), str(duration), "0"],
                           label="benchmark echo", rows=30, cols=70, history_lines=100,
                           metadata={"host": "local fixture", "shell": "python"})
        state.active = sid
        state.modes["jobs"] = "terminal"
        state.page = "jobs"
        T._focus(app, True)
        if not wait_for("READY", 8):
            raise RuntimeError("The real PTY fixture did not become ready: " + str(service.snapshot(sid)))
        if state.rect is None or not T.focused(app):
            raise RuntimeError("The Terminal pane was not visible and focused")
        hidden = None
        if name == "hidden-flood":
            hidden = service.open([sys.executable, "-u", "-c", PROBE, str(args.flood_bytes), str(duration), "1"],
                                  label="bounded hidden flood", rows=30, cols=70, history_lines=100)
        # The initial full paint and visible child startup are outside metrics.
        # Hidden child startup intentionally remains in the hidden-flood case:
        # this exercises cold-session startup alongside already focused input.
        for values in timings.values():
            values.clear()
        counts.clear()
        painted_cells.clear()
        output_frames = 0
        gc_events.clear()
        app.ui_trace = UITrace()
        initial_output = service.snapshot(sid).get("output_bytes", 0)
        initial_flood = service.snapshot(hidden or sid) if hidden or visible_budget else {}
        initial_flood_bytes = initial_flood.get("output_bytes", 0)
        initial_parse_seconds = initial_flood.get("parse_seconds", 0)
        started_wall = time.monotonic()
        started_perf = time.perf_counter()
        measurement_started = started_perf
        started_cpu = time.process_time()
        cgroup_before = cgroup_cpu()
        input_samples = []
        for index in range(args.samples):
            target = started_wall + index * args.interval_ms / 1000
            while time.monotonic() < target:
                frame()
                time.sleep(min(args.poll_ms / 1000, max(0, target-time.monotonic())))
            token = f"{index:06d}"
            started = time.perf_counter()
            dispatch_cpu = time.thread_time()
            accepted = T.handle_event(app, {"type": "text", "text": "PING"+token+"\n"})
            timings["input_dispatch"].append((time.perf_counter()-started)*1000)
            timings["input_dispatch_cpu"].append((time.thread_time()-dispatch_cpu)*1000)
            if not accepted or not wait_for("ACK"+token, args.timeout):
                invalid.append("input acknowledgement timed out: " + token)
                break
            timings["semantic_input_to_paint"].append((last_paint_at-started)*1000)
            timings["semantic_input_to_observed_ack"].append((time.perf_counter()-started)*1000)
            input_samples.append({"index": index, "at_seconds": round(started-started_perf, 6),
                                  "latency_ms": round(timings["semantic_input_to_paint"][-1], 4)})
            if app.tab != "jobs" or app.mode != "main":
                invalid.append("terminal input changed Tower's page or mode")
                break
        elapsed = time.monotonic()-started_wall
        cpu = time.process_time()-started_cpu
        cgroup_after = cgroup_cpu()
        snapshot = service.snapshot(sid)
        flood_snapshot = service.snapshot(hidden) if hidden else snapshot if visible_budget else {}
        if painted_cells["feedback_outside_terminal"]:
            invalid.append("cached terminal feedback changed cells outside its pane")
        if counts["document_rebuilds_before_normal_deadline"]:
            invalid.append("terminal workload rebuilt the document before its normal maintenance deadline")
        return {"scenario": name, "elapsed_seconds": round(elapsed, 4),
                "ui_process_cpu_seconds": round(cpu, 4),
                "timings": {key: summary(value) for key, value in timings.items()},
                "counts": dict(counts), "terminal_publications_observed": output_frames,
                "painted_cells": dict(painted_cells),
                "visible_output_bytes": snapshot.get("output_bytes", 0) - initial_output,
                "flood_output_bytes_during_measurement": flood_snapshot.get("output_bytes", 0) - initial_flood_bytes,
                "flood_parse_seconds_during_measurement": flood_snapshot.get("parse_seconds", 0) - initial_parse_seconds,
                "visible_input_pending": snapshot.get("input_pending", 0),
                "terminal_rect": {key: getattr(state.rect, key) for key in ("x", "y", "width", "height")},
                "input_samples": input_samples,
                "slow_loops": diagnostic_events,
                "gc": [{**event, "at_seconds": event["at"]-started_perf} for event in gc_events],
                "document_trace": app.ui_trace.report(),
                "cgroup_cpu_delta": {key: value-cgroup_before.get(key, value) for key, value in cgroup_after.items()},
                "failures": invalid}
    finally:
        gc.callbacks.remove(observe_gc)
        service.shutdown()
        from tower import terminal_fusion
        terminal_fusion.close(app)
        app.research.close()
        f.sampler.shutdown()
        f.scheduler.shutdown(wait=True, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--interval-ms", type=float, default=25)
    parser.add_argument("--poll-ms", type=float, default=16)
    parser.add_argument("--timeout", type=float, default=3)
    parser.add_argument("--flood-bytes", type=int, default=1024*1024)
    parser.add_argument("--jobs", type=int, default=60)
    parser.add_argument("--width", type=int, default=150)
    parser.add_argument("--height", type=int, default=45)
    parser.add_argument("--scenarios", default="idle,hidden-flood,visible-flood")
    args = parser.parse_args()
    if not (5 <= args.samples <= 200 and 1 <= args.poll_ms <= 16 and 10 <= args.interval_ms <= 100
            and .1 <= args.timeout <= 5 and 4096 <= args.flood_bytes <= 4*1024*1024
            and 1 <= args.jobs <= 500 and 100 <= args.width <= 240 and 25 <= args.height <= 80):
        parser.error("Requested workload is outside the bounded benchmark limits")
    names = args.scenarios.split(",")
    if not names or len(names) > 3 or any(name not in {"idle", "hidden-flood", "visible-flood"} for name in names):
        parser.error("Choose idle,hidden-flood,visible-flood")
    sys.path.insert(0, str(args.source_root.resolve()))
    report = {"schema": "tower.terminal-benchmark/v1", "python": sys.version,
              "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "platform": platform.platform(), "workload": {key: str(value) if isinstance(value, Path) else value
                  for key, value in vars(args).items()},
              "limits": ["Real PTY input and broker emulator; synthetic static Jobs records; no live scheduler.",
                         "Latency begins at semantic input dispatch and ends at the differential paint callback.",
                         "In-memory paint sink excludes curses conversion, outer-terminal rendering, network, and monitor latency.",
                         "UI process CPU excludes the isolated broker and child process CPU.",
                         "Cgroup CPU counters include all processes in this container; they do not attribute other workload to Tower.",
                         "Maintenance can rebuild Jobs at its normal interval; cached output feedback must not rebuild it.",
                         "The default 16 ms readiness interval matches the embedded pane's outer-loop idle wait cap.",
                         "Visible child startup is excluded after READY; hidden child startup is included as cold-session pressure.",
                         "Output and parser counters come from published snapshots; hidden snapshots publish less frequently and can lag actual work.",
                         "Finite flood children are closed during cleanup; output bytes report the measured interval, not the entire budget."]}
    with tempfile.TemporaryDirectory(prefix="tower-terminal-benchmark-") as directory:
        report["scenarios"] = [scenario(args, name, Path(directory)/name) for name in names]
    report["passed"] = all(not result["failures"] and result["timings"]["semantic_input_to_paint"].get("count") == args.samples
                           for result in report["scenarios"])
    args.output.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "output": str(args.output),
                      "scenarios": [{"name": result["scenario"], "latency": result["timings"].get("semantic_input_to_paint", {"count": 0}),
                                     "feedback": result["timings"]["feedback"], "failures": result["failures"]}
                                    for result in report["scenarios"]]}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
