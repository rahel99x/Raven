#!/usr/bin/env python3
"""Equivalent local UI/PTY review fixtures; Linux RSS and CPU, never live Slurm.

Run from the repository root with PYTHONPATH=.; each invocation is isolated.
Examples: python scripts/benchmark_ui_review.py terminal baseline
          python scripts/benchmark_ui_review.py rows current
          python scripts/benchmark_ui_review.py pty current
"""

import argparse
import json
import os
import resource
import subprocess
import sys
import time
import types

p = argparse.ArgumentParser()
p.add_argument("kind", choices=("terminal", "rows", "pty"))
p.add_argument("revision", choices=("baseline", "current"))
a = p.parse_args()
base = "b98742f"


def baseline(name):
    source = subprocess.check_output(
        ["git", "show", base + ":raven/" + name + ".py"], text=True
    )
    module = types.ModuleType("raven._review_baseline_" + name)
    module.__package__ = "raven"
    sys.modules[module.__name__] = module
    exec(compile(source, module.__name__, "exec"), module.__dict__)
    return module


if a.kind == "pty" and a.revision != "current":
    p.error(
        "PTY fixture qualifies the current real broker; use terminal for exact before/after emulator comparison"
    )
if a.kind == "terminal":
    if a.revision == "baseline":
        T = baseline("terminal_emulator").TerminalEngine
    else:
        from raven.terminal_emulator import TerminalEngine as T
    data = b"yes\r\n" * (8 * 1024 * 1024 // 5)
    e = T(24, 80, history_limit=2000)
    start = time.perf_counter()
    cpu = time.process_time()
    e.feed(data)
    snap = e.snapshot()
    result = dict(
        fixture="8 MiB yes CRLF, 24x80, 2000 retained lines",
        bytes=len(data),
        wall_seconds=time.perf_counter() - start,
        cpu_seconds=time.process_time() - cpu,
        retained_lines=snap["history_count"],
        last_lines=snap["lines"][-2:],
        history_start=snap["history_start"],
    )
    result["mib_per_second"] = len(data) / (1 << 20) / result["wall_seconds"]
elif a.kind == "rows":
    from raven.config import Config
    from raven.controller import App
    from raven.model import Job, Store
    from raven.views import Views
    from raven import layout

    if a.revision == "baseline":
        module = baseline("job_row_cache")
        sys.modules["raven.job_row_cache"] = module
    cfg = Config({"animations": False})
    s = Store(persist=False)
    s.jobs = [
        Job(str(i), "experiment-" + str(i), "main", "RUNNING", cpus=4)
        for i in range(10000)
    ]
    app = App(s, None, None, cfg, "test", interactive=False)
    v = Views(layout.Glyphs(False), cfg)
    app.views_ref = v
    snap = s.render_snapshot()
    v.job_rows(snap, app, shared=True)
    start = time.perf_counter()
    cpu = time.process_time()
    for i in range(30):
        app.selected_id = str(i)
        app.marks = {str(i + 1)}
        rows = v.job_rows(snap, app, shared=True)
    result = dict(
        fixture="10000 queued jobs, 30 cursor/mark changes, exact shared native rows",
        iterations=30,
        wall_seconds=time.perf_counter() - start,
        cpu_seconds=time.process_time() - cpu,
        rows=len(rows),
    )
    result["milliseconds_per_input"] = result["wall_seconds"] / 30 * 1000
    if app.research:
        app.research.close()
elif a.kind == "pty":
    from raven.terminal_service import TerminalService

    program = 'import os; data=b"yes\\n"*16384; [os.write(1,data) for _ in range(128)]'
    service = TerminalService()
    sid = service.open([sys.executable, "-c", program], rows=24, cols=80)
    start = time.perf_counter()
    cpu = time.process_time()
    while time.perf_counter() - start < 45:
        service.poll()
        snap = service.snapshot(sid)
        if snap.get("status") == "exited":
            break
        time.sleep(0.001)
    result = dict(
        fixture="Real local PTY, producer8MiB LF becomes10MiB CRLF, 24x80, default history",
        wall_seconds=time.perf_counter() - start,
        ui_cpu_seconds=time.process_time() - cpu,
        broker_parse_seconds=snap.get("parse_seconds"),
        output_bytes=snap.get("output_bytes"),
        status=snap.get("status"),
    )
    result["mib_per_second"] = (
        result["output_bytes"] / (1 << 20) / result["wall_seconds"]
    )
    service.shutdown()
result.update(
    kind=a.kind,
    revision=a.revision,
    baseline_revision=base,
    process_max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    python=sys.version.split()[0],
    pid=os.getpid(),
)
print(json.dumps(result))
