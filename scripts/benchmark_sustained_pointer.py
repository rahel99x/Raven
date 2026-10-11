#!/usr/bin/env python3
"""Exercise the real curses loop with sustained synthetic SGR pointer traffic.

The fixture supplies CPU, four GPU, and four GPU trace sources, with fresh
observations every 500 ms. It discards terminal text, saves timings only, and
never calls scheduler mutations. Source roots can be extracted git archives.
Run with python -S to keep an editable-package finder out of the comparison.
"""
from __future__ import annotations

import argparse
from collections import Counter
import functools
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import threading
import time


def quantiles(values):
    if not values:
        return {"calls": 0}
    ordered = sorted(values)
    return {"calls": len(values), "p50_ms": statistics.median(ordered),
            "p95_ms": ordered[min(len(ordered)-1, math.ceil(len(ordered)*.95)-1)],
            "p99_ms": ordered[min(len(ordered)-1, math.ceil(len(ordered)*.99)-1)],
            "max_ms": ordered[-1]}


def publish_marker(path, value):
    """Publish complete child metadata before its existence signals readiness."""
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(value))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def configure_live(app, metric_live, delta):
    """Configure eligible current controls; retired entries can remain retained."""
    enabled_deltas = []
    for key, entry in list(metric_live.initialize(app)["entries"].items()):
        if key[1] != app.selected_id or not metric_live.set_enabled(app, key, True):
            continue
        if delta is not None and not metric_live.set_delta(app, key, delta):
            raise ValueError("selected source does not support the requested Live window")
        enabled_deltas.append(entry["delta"])
    if not enabled_deltas:
        raise ValueError("selected fixture job has no eligible Live metric controls")
    return enabled_deltas


def prepare_fixture_job(app, snapshot, compose):
    """Resolve the Jobs cursor before attaching measured histories to its row.

    Setting selected_id alone is insufficient: the first Jobs composition can
    normalize it to the current sorted row. Seed that actual displayed job so
    a nominal 4,000-point benchmark cannot silently measure an empty neighbour.
    """
    app.job_panel_state.update(mode="analytics", analytics_view="job")
    app.tab = "jobs"
    compose()
    selected = next((job for job in snapshot["jobs"]
                     if job.id == app.selected_id and not job.pending), None)
    if selected is None:
        raise ValueError("fixture requires an actually selected running Jobs row")
    app.analytics_job = selected.id
    selected.gpus = 4
    return selected


def input_state(app, reader):
    """Report framing state without recording terminal bytes or pasted text."""
    return {"mode": getattr(app, "mode", None), "tab": getattr(app, "tab", None),
            "quit": getattr(app, "quit", None),
            "reader": ({"escape_length": len(reader.escape), "pasting": reader.pasting,
                         "queued_events": len(reader.queue),
                         "raw_values": len(getattr(reader, "raw_values", ())),
                         "pending_utf8_bytes": len(reader.utf8.getstate()[0]) if hasattr(reader, "utf8") else 0,
                         "discard_mouse": bool(getattr(reader, "discard_mouse", False)),
                         "discard_csi": bool(getattr(reader, "discard_csi", False)),
                         "pending": bool(getattr(reader, "pending", reader.escape or reader.pasting))}
                       if reader is not None else None)}


def switch_plan(initial, count):
    """Exercise the opposite mode and return through actual command input."""
    other = "single" if initial == "multi" else "multi"
    return {count // 3: other, count * 2 // 3: initial}


def switch_input(mode, control, button=None):
    if control == "command":
        return (":workers " + mode + "\r").encode("ascii")
    row, left, right = button
    column = left + (right - left) // 2
    return f"\x1b[<0;{column + 1};{row + 1}M\x1b[<0;{column + 1};{row + 1}m".encode("ascii")


def diagnostic_drain_motion(screen, app, window, curses, hits, *, pending=False,
                            limit=256, seconds=.012):
    """Harness-only experiment: preserve the first deliberate queued event.

    This does not change Tower's screen loop. No capture or mode transition is
    eligible. Press, release, wheel, resize, key, and paste retain their exact
    decoded identity and order in the input reader's existing queue.
    """
    if pending or app.mode != "main":
        return 0
    captures = (("chart_interaction_state", "capture"), ("metric_live_state", "capture"),
                ("text_selection_state", "capture"), ("job_selection_state", "capture"),
                ("scrollbar_state", "capture"), ("pane_drag_state", "capture"),
                ("toolbar_state", "dragging"), ("history_browser_state", "drag"))
    if any((getattr(app, state, {}) or {}).get(field) for state, field in captures):
        return 0
    deadline = time.perf_counter() + seconds
    deliberate = sum(getattr(curses, name, 0) for name in (
        "BUTTON1_CLICKED", "BUTTON1_PRESSED", "BUTTON1_RELEASED", "BUTTON1_DOUBLE_CLICKED",
        "BUTTON1_TRIPLE_CLICKED", "BUTTON3_CLICKED", "BUTTON3_PRESSED", "BUTTON3_RELEASED",
        "BUTTON3_DOUBLE_CLICKED", "BUTTON4_PRESSED", "BUTTON5_PRESSED"))
    window.timeout(0)
    last, count = None, 0
    while count < limit and time.perf_counter() < deadline:
        event = screen._read_input(window, curses)
        if event is None:
            break
        if (event[0] != "mouse" or event[1] is None
                or not screen._motion_report(event[1][4], curses)
                or event[1][4] & deliberate or getattr(event[1], "held", None) is True):
            reader = screen._INPUT_READERS.get(id(window))
            if reader is None:
                raise RuntimeError("decoded event has no owning input reader")
            reader.queue.appendleft(event)
            break
        last = event
        count += 1
    if last is not None:
        screen._apply_input(app, last, hits, curses)
    return count


def worker(args):
    sys.path.insert(0, str(args.source_root))
    source_digest = hashlib.sha256()
    for path in sorted((args.source_root / "tower").glob("*.py")):
        source_digest.update(path.name.encode())
        source_digest.update(path.read_bytes())
    from tower import chart_interaction, clock, metric_live, palette, screen
    from tower.cli import main
    from tower.slurm import Slurm
    from tower.views import Views

    events, inputs, decoded, counts, totals = [], [], [], Counter(), Counter()
    running = threading.Event()
    running.set()
    producer = None
    initialized = False
    app_ref = None
    window_ref = None
    curses_ref = None
    pending_event = None
    rebuilt = False
    observed_deltas = []
    fixture_job = None
    worker_states = []
    control_written = False
    def observe_workers():
        nonlocal control_written
        governor = getattr(app_ref, "worker_scheduler", None)
        if governor is None:
            return
        status = governor.status()
        status = {name: status[name] for name in ("mode", "target", "pending", "limit", "running", "queued")
                  if name in status}
        if not worker_states or status != {name: value for name, value in worker_states[-1].items() if name != "at"}:
            worker_states.append({"at": time.perf_counter(), **status})
        if args.switch_workers and args.switch_control == "toolbar" and not control_written:
            hit = next((hit for hit in getattr(app_ref, "toolbar_state", {}).get("hits", ())
                        if len(hit) >= 5 and hit[3] == "workers"), None)
            if hit is not None:
                publish_marker(args.report.with_suffix(".control.json"), {"button": hit[:3]})
                control_written = True
    import signal
    import traceback
    import gc
    ui_ident, gc_started = threading.get_ident(), {}
    def gc_timing(phase, info):
        if threading.get_ident() != ui_ident:
            return
        generation = info["generation"]
        if phase == "start":
            gc_started[generation] = (time.perf_counter(), time.thread_time())
        elif generation in gc_started:
            before, cpu_before = gc_started.pop(generation)
            duration, cpu = (time.perf_counter()-before)*1000, (time.thread_time()-cpu_before)*1000
            counts["gc"] += 1
            totals["gc"] += cpu
            if len(events) < 30000:
                events.append({"phase": "gc", "start": before, "ms": duration, "cpu_ms": cpu,
                               "generation": generation})
            else:
                counts["timing_events_dropped"] += 1
    gc.callbacks.append(gc_timing)
    def diagnostic(*_):
        reader = screen._INPUT_READERS.get(id(window_ref)) if window_ref is not None else None
        state = input_state(app_ref, reader)
        state.update(source_hash=source_digest.hexdigest(), counts=dict(counts),
                     toolbar_menu=bool(getattr(app_ref, "toolbar_state", {}).get("menu")),
                     thread_stacks={str(ident): [{"file": Path(item.filename).name,
                                    "line": item.lineno, "function": item.name}
                                   for item in traceback.extract_stack(frame)[-24:]]
                                   for ident, frame in sys._current_frames().items()})
        args.report.with_suffix(".diagnostic.json").write_text(json.dumps(state, indent=2) + "\n")
    signal.signal(signal.SIGUSR1, diagnostic)
    original_compose = Views.compose
    original_jobs = Slurm.jobs
    Slurm.jobs = lambda self: original_jobs(self)[:args.jobs]

    def compose(self, snap, app, *positional, **kwargs):
        nonlocal initialized, producer, app_ref, observed_deltas, fixture_job
        app_ref = app
        if not initialized and snap.get("jobs"):
            initialized = True
            selected = prepare_fixture_job(
                app, snap, lambda: original_compose(self, snap, app, *positional, **kwargs))
            fixture_job = selected.id
            store = app.store
            for index in range(args.points):
                stamp = clock.now() - (args.points-index-1)*.5
                store.record(selected.id, {"k": "live", "t": stamp,
                    "cpu": .5+.4*math.sin(index/20), "rss": (1+.4*math.sin(index/30))*(1 << 30)})
                store.record(selected.id, {"k": "gpu", "t": stamp,
                    "gpu": {str(device): [50+40*math.sin(index/20+device)] for device in range(4)}})
            store.trace[selected.id] = [{"t": clock.now()-(args.points-index-1)*60,
                "index": device, "util": 50+40*math.sin(index/20+device)}
                for device in range(4) for index in range(args.points)]

            def fresh():
                index = args.points
                while running.is_set():
                    time.sleep(.5)
                    if not running.is_set():
                        break
                    stamp = clock.now()
                    store.record(selected.id, {"k": "live", "t": stamp,
                        "cpu": .5+.4*math.sin(index/20), "rss": (1+.4*math.sin(index/30))*(1 << 30)})
                    store.record(selected.id, {"k": "gpu", "t": stamp,
                        "gpu": {str(device): [50+40*math.sin(index/20+device)] for device in range(4)}})
                    counts["fresh_publications"] += 1
                    index += 1
            producer = threading.Thread(target=fresh, name="pointer-fixture-source", daemon=True)
            producer.start()
        result = original_compose(self, snap, app, *positional, **kwargs)
        if initialized and args.live:
            observed_deltas = configure_live(app, metric_live, args.delta)
        return result
    Views.compose = compose

    def measured(obj, name, phase, *, input_event=False, read_event=False, rebuild=False):
        original = getattr(obj, name)
        @functools.wraps(original)
        def wrapper(*positional, **kwargs):
            nonlocal window_ref, curses_ref, pending_event, rebuilt
            if (phase == "feedback" and args.prepaint_drain and rebuilt and window_ref is not None
                    and not hasattr(screen, "_drain_prepaint_motion")):
                counts["prepaint_reports"] += diagnostic_drain_motion(
                    screen, positional[1], window_ref, curses_ref, positional[0].hits,
                    pending=pending_event is not None)
                rebuilt = False
            before = time.perf_counter()
            cpu_before = time.thread_time()
            reason = None
            result = None
            if rebuild:
                app = positional[1]
                cache = positional[0]
                reason = [value for value, deadline in (("maintenance", cache.next_maintenance),
                          ("animation", cache.next_animation), ("live", cache.next_live)) if before >= deadline]
                if cache.dirty:
                    reason.append("dirty")
            try:
                result = original(*positional, **kwargs)
                if phase == "document":
                    rebuilt = True
                if phase == "input_read":
                    window_ref, curses_ref = positional[:2]
                if phase == "input_batch":
                    pending_event = result
                if phase in ("feedback", "input_dispatch"):
                    observe_workers()
                return result
            finally:
                after = time.perf_counter()
                cpu = (time.thread_time()-cpu_before)*1000
                counts[phase] += 1
                totals[phase] += cpu
                if phase not in ("palette_attr", "cell_style") and len(events) < 30000:
                    events.append({"phase": phase, "start": before, "ms": (after-before)*1000, "cpu_ms": cpu,
                                   **({"reasons": reason} if rebuild else {})})
                elif phase not in ("palette_attr", "cell_style"):
                    counts["timing_events_dropped"] += 1
                if input_event:
                    app, event = positional[:2]
                    if event[0] == "q":
                        counts["quit_inputs"] += 1
                    if event[0] == "mouse" and event[1] is not None:
                        _, x, y, _, _ = event[1]
                        seq = (y-9)*295+x-9
                        if 0 <= seq < 10325:
                            inputs.append({"seq": seq, "at": before, "tab": app.tab})
                if read_event and result is not None and result[0] == "mouse" and result[1] is not None:
                    _, x, y, _, _ = result[1]
                    seq = (y-9)*295+x-9
                    if 0 <= seq < 10325:
                        decoded.append({"seq": seq, "at": after})
                if read_event and result is not None and result[0] == "q":
                    counts["decoded_quit_inputs"] += 1
        setattr(obj, name, wrapper)

    measured(Views, "compose", "compose")
    measured(Views, "overlay", "overlay")
    measured(screen._FrameCache, "rebuild", "document", rebuild=True)
    measured(screen._FrameCache, "feedback", "feedback")
    measured(screen._DifferentialPainter, "draw", "paint")
    if hasattr(screen, "_drain_prepaint_motion"):
        measured(screen, "_drain_prepaint_motion", "prepaint_input")
    measured(screen, "_apply_input", "input_dispatch", input_event=True)
    measured(screen, "_consume_input_batch", "input_batch")
    measured(screen, "_read_input", "input_read", read_event=True)
    measured(screen.CursesPalette, "attr", "palette_attr")
    measured(palette, "cell_style", "cell_style")
    import curses
    measured(curses, "doupdate", "terminal_flush")
    exit_code = 0
    error_type = None
    error_frames = []
    try:
        arguments = ["--fake", "--no-state", "--config", str(args.config)]
        if args.workers is not None:
            arguments.extend(("--workers", args.workers))
        result = main(arguments)
        exit_code = result or 0
    except BaseException as error:
        import traceback
        exit_code, error_type = 1, type(error).__name__
        error_frames = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                        for frame in traceback.extract_tb(error.__traceback__)[-20:]]
        raise
    finally:
        gc.callbacks.remove(gc_timing)
        running.clear()
        if producer:
            producer.join(1)
        report = {"exit_code": exit_code, "events": events, "inputs": inputs, "decoded": decoded,
                  "error_type": error_type,
                  "error_frames": error_frames,
                  "counts": dict(counts), "cpu_total_ms": dict(totals), "tab": getattr(app_ref, "tab", None),
                  "source_hash": source_digest.hexdigest(),
                  "live_delta": (args.delta if args.delta is not None else metric_live.MAX_DELTA) if args.live else None,
                  "observed_live_deltas": sorted(set(observed_deltas)),
                  "visible_plots": len(chart_interaction.initialize(app_ref)["plots"]) if app_ref else 0}
        report["input_state"] = input_state(app_ref, screen._INPUT_READERS.get(id(window_ref)))
        records = getattr(getattr(app_ref, "store", None), "series", {}).get(fixture_job, ())
        report["fixture"] = {
            "selected": bool(app_ref is not None and app_ref.selected_id == fixture_job),
            "records": dict(Counter(record.get("k") for record in records)),
            "visible_plots": sum(plot.key[1] == fixture_job for plot in
                                 chart_interaction.initialize(app_ref)["plots"]) if app_ref else 0,
        }
        observe_workers()
        report["worker_states"] = worker_states
        args.report.write_text(json.dumps(report))
    return exit_code


def run(args, rate):
    import fcntl
    import pty
    import select
    import struct
    import subprocess
    import termios
    directory = args.output.parent
    stem = args.output.stem + "-" + str(rate)
    report = directory / (stem + "-worker.json")
    control = report.with_suffix(".control.json")
    for artifact in (report, control, report.with_suffix(".diagnostic.json")):
        artifact.unlink(missing_ok=True)
    config = directory / (stem + "-config.json")
    config.write_text(json.dumps({"log_lines": 0, "startup_animation": False,
        "workspace": {"density": "compact", "split": 50}, "mouse": True,
        "series_keep": args.points*2+1000}))
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 52, 320, 0, 0))
    command = [sys.executable, "-S", str(Path(__file__).resolve()), "--worker", "--source-root",
               str(args.source_root), "--config", str(config), "--report", str(report),
               "--points", str(args.points), "--jobs", str(args.jobs)] + (["--live"] if args.live else []) + (
                   ["--prepaint-drain"] if args.prepaint_drain else []) + (
                   ["--delta", str(args.delta)] if args.delta is not None else [])
    if args.workers is not None:
        command.extend(("--workers", args.workers))
    if args.switch_workers:
        command.append("--switch-workers")
        command.extend(("--switch-control", args.switch_control))
    env = dict(os.environ, TERM="xterm-256color", LC_ALL="C.UTF-8")
    process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=env,
                               cwd=args.source_root, close_fds=True)
    os.close(slave)
    discarded = 0
    sent = {}

    def drain(seconds):
        nonlocal discarded
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            ready, _, _ = select.select([master], [], [], min(.01, max(0., deadline-time.perf_counter())))
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return
                if not data:
                    return
                discarded += len(data)
    try:
        drain(1.5)
        button = None
        if args.switch_workers and args.switch_control == "toolbar":
            if not control.is_file():
                raise RuntimeError("the fixture did not publish a workers toolbar button during startup")
            button = json.loads(control.read_text())["button"]
        begin = time.perf_counter()
        count = min(10325, math.ceil(rate*args.seconds))
        changes = switch_plan(args.workers or "multi", count) if args.switch_workers else {}
        for seq in range(count):
            deadline = begin+seq/rate
            drain(max(0., deadline-time.perf_counter()))
            x, y = 10+seq % 295, 10+seq // 295
            sent[seq] = time.perf_counter()
            os.write(master, f"\x1b[<35;{x};{y}M".encode())
            if seq in changes:
                os.write(master, switch_input(changes[seq], args.switch_control, button))
        drain(.25)
        os.write(master, b"q")
        deadline = time.perf_counter()+8
        while process.poll() is None and time.perf_counter() < deadline:
            drain(.05)
        if process.poll() is None:
            import signal
            process.send_signal(signal.SIGUSR1)
            drain(.25)
            raise RuntimeError("PTY fixture did not exit after quit; sanitized diagnostic: "
                               + str(report.with_suffix(".diagnostic.json")))
        process.wait(timeout=1)
        if process.returncode != 0:
            error_type = json.loads(report.read_text()).get("error_type") if report.exists() else None
            raise RuntimeError("PTY fixture exited " + str(process.returncode)
                               + (" (" + error_type + ")" if error_type else ""))
        data = json.loads(report.read_text())
        fixture = data.get("fixture", {})
        if (not fixture.get("selected") or not fixture.get("visible_plots")
                or any(fixture.get("records", {}).get(kind, 0) < args.points for kind in ("live", "gpu"))):
            raise RuntimeError("fixture did not retain the requested source history in its visible selected job")
        if args.switch_workers:
            modes = {state["mode"] for state in data["worker_states"]}
            final = data["worker_states"][-1] if data["worker_states"] else {}
            if modes != {"single", "multi"} or final.get("mode") != (args.workers or "multi") or final.get("pending"):
                raise RuntimeError("actual :workers commands did not complete both requested mode transitions")
        measured = [event for event in data["events"] if begin <= event["start"] <= begin+args.seconds]
        latency = [(item["at"]-sent[item["seq"]])*1000 for item in data["inputs"]
                   if item["seq"] in sent and item["at"] >= sent[item["seq"]]]
        phases = {phase: quantiles([event["ms"] for event in measured if event["phase"] == phase])
                  for phase in sorted({event["phase"] for event in measured})}
        cpu_phases = {phase: quantiles([event["cpu_ms"] for event in measured if event["phase"] == phase])
                      for phase in sorted({event["phase"] for event in measured})}
        documents = [event for event in measured if event["phase"] == "document"]
        paints = [event for event in measured if event["phase"] == "paint"]
        dispatched = [item for item in data["inputs"] if item["seq"] in sent]
        reports = [item for item in data["decoded"] if item["seq"] in sent]
        decoded_latency = [(item["at"]-sent[item["seq"]])*1000 for item in reports]
        oldest_latency, position = [], 0
        for item in dispatched:
            batch = []
            while position < len(reports) and reports[position]["at"] <= item["at"]:
                batch.append(reports[position])
                position += 1
            if batch:
                oldest_latency.append((item["at"]-min(sent[report["seq"]] for report in batch))*1000)
        painted_age, position = [], 0
        previous = None
        for event in paints:
            while position < len(dispatched) and dispatched[position]["at"] <= event["start"]:
                previous = dispatched[position]
                position += 1
            if previous is not None:
                painted_age.append((event["start"]+event["ms"]/1000-sent[previous["seq"]])*1000)
        return {"rate_hz": rate, "seconds": args.seconds, "points": args.points,
                "source_root": str(args.source_root), "live": args.live, "jobs": args.jobs,
                "source_hash": data["source_hash"], "live_delta": data["live_delta"],
                "observed_live_deltas": data["observed_live_deltas"],
                "prepaint_drain": args.prepaint_drain,
                "initial_workers": args.workers, "switch_workers": args.switch_workers,
                "switch_control": args.switch_control if args.switch_workers else None,
                "worker_states": data["worker_states"],
                "sent_reports": len(sent), "dispatched_reports": len(latency),
                "input_latency": quantiles(latency), "decoded_report_latency": quantiles(decoded_latency),
                "oldest_batch_latency": quantiles(oldest_latency), "painted_pointer_age": quantiles(painted_age),
                "phases": phases, "cpu_phases": cpu_phases,
                "paint_gaps": quantiles([(after["start"]-before["start"])*1000
                                          for before, after in zip(paints, paints[1:])]),
                "document_timeline": [{"at_ms": round((event["start"]-begin)*1000, 3),
                                        "ms": round(event["ms"], 3), "cpu_ms": round(event["cpu_ms"], 3),
                                        "reasons": event["reasons"]}
                                       for event in documents],
                "counts": data["counts"], "cpu_total_ms": data["cpu_total_ms"],
                "timing_events_truncated": bool(data["counts"].get("timing_events_dropped")),
                "input_state": data["input_state"],
                "fixture": data["fixture"],
                "visible_plots": data["visible_plots"],
                "unexpected_tabs": sorted({item["tab"] for item in data["inputs"] if item["tab"] != "jobs"}),
                "terminal_bytes_discarded": discarded, "exit_code": process.returncode}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rates", default="50,250,1000")
    parser.add_argument("--seconds", type=float, default=4.)
    parser.add_argument("--points", type=int, default=1000)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--delta", type=float, help="explicit Live window shared by compared versions")
    parser.add_argument("--workers", choices=("single", "multi"), help="initial mode for sources that support --workers")
    parser.add_argument("--switch-workers", action="store_true",
                        help="type :workers commands during pointer traffic, then return to the initial mode")
    parser.add_argument("--switch-control", choices=("command", "toolbar"), default="command",
                        help="use command input or real toolbar press/release events for --switch-workers")
    parser.add_argument("--prepaint-drain", action="store_true",
                        help="harness-only experiment: drain pure motion before painting a rebuilt document")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.source_root = args.source_root.resolve()
    if args.worker:
        return worker(args)
    if args.output is None:
        parser.error("--output is required")
    try:
        rates = [int(rate) for rate in args.rates.split(",")]
    except ValueError:
        parser.error("--rates must be a comma-separated list of integers from 1 to 1000 Hz")
    if not 1 <= len(rates) <= 8 or any(not 1 <= rate <= 1000 for rate in rates):
        parser.error("--rates must contain 1 to 8 rates from 1 to 1000 Hz")
    if len(set(rates)) != len(rates):
        parser.error("--rates must not contain duplicates")
    if not math.isfinite(args.seconds) or not .1 <= args.seconds <= 10:
        parser.error("--seconds must be between 0.1 and 10")
    if not 1 <= args.points <= 10000:
        parser.error("--points must be between 1 and 10000")
    if not 3 <= args.jobs <= 6:
        parser.error("--jobs must be between 3 and 6 for this fake-job fixture")
    if args.delta is not None and (not math.isfinite(args.delta) or not 1 <= args.delta <= 30):
        parser.error("--delta must be between 1 and 30 seconds; use 5 for pre-4.8 comparisons")
    if args.switch_workers and (args.seconds < 1 or any(rate * args.seconds < 6 for rate in rates)):
        parser.error("--switch-workers requires at least one second and six pointer reports per rate")
    if args.switch_control == "toolbar" and not args.switch_workers:
        parser.error("--switch-control toolbar requires --switch-workers")
    if not (args.source_root / "tower" / "__init__.py").is_file():
        parser.error("--source-root must contain a Tower source package")
    if not args.output.parent.is_dir():
        parser.error("the --output parent directory must already exist")
    if args.output.exists() and not args.output.is_file():
        parser.error("--output must name a file")
    results = [run(args, rate) for rate in rates]
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "results": [{key: value for key, value in item.items()
                    if key not in ("document_timeline", "counts", "source_root")} for item in results]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
