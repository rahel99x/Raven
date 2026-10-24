#!/usr/bin/env python3
"""Measure real loopback browser interaction and committed-journal freshness.

This is a local qualification workload, not a portable performance guarantee.
It launches Chromium explicitly, creates private temporary journals/indexes,
and makes no scheduler request or change to a scientific job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_CLIENT = r"""
const {chromium}=require('playwright'),readline=require('node:readline');
(async()=>{const browser=await chromium.launch({executablePath:process.argv[2],headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
try{const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[];page.on('pageerror',error=>errors.push(error.message));
await page.goto(process.argv[3]);await page.waitForFunction(()=>document.getElementById('identity').textContent.includes('job_id'));
await page.locator('#delta').fill('60');await page.locator('#delta').dispatchEvent('change');
await page.locator('#search').fill('fresh_');await page.waitForTimeout(300);
console.log(JSON.stringify({op:'ready'}));
for await(const line of readline.createInterface({input:process.stdin,crlfDelay:Infinity})){const message=JSON.parse(line);
if(message.op==='fresh'){await page.waitForFunction(name=>[...document.querySelectorAll('#event_body button')].some(row=>row.textContent===name),message.name,{timeout:5000});await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));console.log(JSON.stringify({op:'visible',name:message.name}));}
else if(message.op==='inputs'){
await page.locator('#search').fill('');await page.waitForTimeout(250);await page.locator('#fit').click();await page.locator('#event_body button').first().waitFor();
const selection=[],pan=[],keyboard=[];
for(let i=0;i<message.count;i++)selection.push(await page.evaluate(async i=>{const buttons=document.querySelectorAll('#event_body button'),item=buttons[i%buttons.length],start=performance.now();item.click();await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));if(document.getElementById('details').dataset.intervalId!==item.dataset.intervalId)throw Error('Selection did not preserve exact event');return performance.now()-start},i));
await page.locator('#zoom_in').click();
for(let i=0;i<message.count;i++)pan.push(await page.evaluate(async i=>{const start=performance.now();document.getElementById(i%2?'pan_left':'pan_right').click();await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));return performance.now()-start},i));
for(let i=0;i<message.count;i++)keyboard.push(await page.evaluate(async i=>{document.getElementById('timeline').focus();const start=performance.now();document.getElementById('timeline').dispatchEvent(new KeyboardEvent('keydown',{key:i%2?'ArrowLeft':'ArrowRight',bubbles:true}));await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));return performance.now()-start},i));
if(errors.length)throw Error(errors.join('\n'));console.log(JSON.stringify({op:'inputs',selection_ms:selection,pan_ms:pan,keyboard_ms:keyboard}));}
else if(message.op==='close')break;}
}finally{await browser.close()}})().catch(error=>{console.error(error);process.exit(1)});
"""


def _summary(values):
    ordered = sorted(values)
    return {
        "count": len(values),
        "unit": "ms",
        "minimum": ordered[0],
        "p50": ordered[math.ceil(len(ordered) * 0.5) - 1],
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "maximum": ordered[-1],
        "observations": values,
    }


def _fingerprint():
    files = sorted((_ROOT / "raven" / "tracing").glob("workspace_*.py"))
    files += [
        _ROOT / "raven" / "tracing" / name
        for name in ("capture.py", "reader.py", "protocol.py")
    ]
    files.append(Path(__file__))
    result = {
        str(path.relative_to(_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
        if path.is_file()
    }
    return {
        "sha256": hashlib.sha256(
            json.dumps(result, sort_keys=True).encode()
        ).hexdigest(),
        "files": result,
    }


def _receive(process, timeout=10):
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    if not ready:
        raise TimeoutError(
            "Browser qualification did not acknowledge its bounded request"
        )
    value = process.stdout.readline()
    if not value:
        raise RuntimeError(
            "Browser qualification exited before acknowledging its request"
        )
    return json.loads(value)


def _send(process, value):
    process.stdin.write(json.dumps(value) + "\n")
    process.stdin.flush()


def measure(*, observations=20, lanes=16, chromium=None, node=None):
    from raven.tracing.capture import Recorder
    from raven.tracing.workspace_session import WorkspaceSession

    node = node or shutil.which("node")
    chromium = (
        chromium
        or os.environ.get("RAVEN_BROWSER_EXECUTABLE")
        or next(
            (
                shutil.which(name)
                for name in ("chromium", "chromium-browser", "google-chrome")
                if shutil.which(name)
            ),
            None,
        )
    )
    if not node or not chromium:
        raise RuntimeError(
            "Real UX qualification needs Node, the Playwright Node package, and Chromium"
        )
    started = time.monotonic()
    source_before = _fingerprint()
    with tempfile.TemporaryDirectory(prefix="raven-trace-ux-") as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir(mode=0o700)
        client_path = root / "client.cjs"
        client_path.write_text(_CLIENT)
        session, process = None, None
        with Recorder(
            project,
            job_id="ux-qualification",
            attempt="attempt-1",
            cluster="local",
            background_flush=False,
            coverage={
                "adapter": "cooperative",
                "scope": "Synthetic UX qualification, not a scientific application",
            },
        ) as recorder:
            try:
                for lane in range(lanes):
                    for call in range(4):
                        stamp = time.monotonic_ns()
                        frame = "warm_" + str(lane) + "_" + str(call)
                        recorder.emit(
                            "call",
                            {"frame": frame, "function": frame},
                            thread="thread_" + str(lane),
                            mono_ns=stamp,
                        )
                        recorder.emit(
                            "return",
                            {"frame": frame},
                            thread="thread_" + str(lane),
                            mono_ns=stamp + 1,
                        )
                recorder.flush()
                session = WorkspaceSession(
                    [recorder.path], index_path=root / "index.sqlite"
                )
                for _ in range(100):
                    if session.refresh()["caught_up"]:
                        break
                viewer = session.open_browser()
                with (root / "browser-stderr.txt").open("w+") as stderr:
                    process = subprocess.Popen(
                        [node, str(client_path), chromium, viewer.url],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=stderr,
                        text=True,
                        cwd=_ROOT,
                        close_fds=True,
                        start_new_session=True,
                    )
                    try:
                        if _receive(process, 20).get("op") != "ready":
                            raise RuntimeError(
                                "Unexpected browser qualification handshake"
                            )
                        freshness = []
                        for index in range(observations):
                            name = "fresh_" + str(index)
                            stamp = time.monotonic_ns()
                            recorder.emit(
                                "call",
                                {"frame": name, "function": name},
                                thread="thread_" + str(index % lanes),
                                mono_ns=stamp,
                            )
                            recorder.emit(
                                "return",
                                {"frame": name},
                                thread="thread_" + str(index % lanes),
                                mono_ns=stamp + 1,
                            )
                            committed = time.monotonic_ns()
                            recorder.flush()
                            _send(process, {"op": "fresh", "name": name})
                            reply = _receive(process, 8)
                            if reply != {"op": "visible", "name": name}:
                                raise RuntimeError(
                                    "Browser acknowledged a different committed event"
                                )
                            freshness.append(
                                (time.monotonic_ns() - committed) / 1_000_000
                            )
                        _send(process, {"op": "inputs", "count": observations})
                        inputs = _receive(process, 30)
                        if inputs.get("op") != "inputs":
                            raise RuntimeError(
                                "Browser did not return real input measurements"
                            )
                        _send(process, {"op": "close"})
                        process.stdin.close()
                        process.stdin = None
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            raise RuntimeError(
                                "Owned browser runner did not exit during bounded cleanup"
                            ) from None
                        if process.returncode:
                            raise RuntimeError(
                                "Browser qualification exited unsuccessfully"
                            )
                    except Exception:
                        stderr.seek(0)
                        diagnostics = stderr.read(32 << 10).replace(
                            viewer.token, "[redacted session token]"
                        )
                        if diagnostics:
                            print(diagnostics, file=sys.stderr)
                        raise
            finally:
                if process is not None and process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
                if session is not None:
                    session.close()
    summaries = {
        name.removesuffix("_ms"): _summary(values)
        for name, values in inputs.items()
        if name.endswith("_ms")
    }
    summaries["committed_to_visible"] = _summary(freshness)
    return {
        "schema": "raven.trace-ux-measurement/v1",
        "source": {
            "before": source_before,
            "after": _fingerprint(),
            "qualification": "Exact local source bytes; a dirty working tree is not represented as a clean release revision",
        },
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "logical_cpus": os.cpu_count(),
            "load_average": list(os.getloadavg())
            if hasattr(os, "getloadavg")
            else None,
            "node": node,
            "chromium": chromium,
        },
        "fixture": {
            "lanes": lanes,
            "warm_intervals": lanes * 4,
            "new_intervals": observations,
            "index_ingest_seconds": 0.5,
            "browser_catalog_poll_seconds": 1,
            "scheduler_rpc_calls": 0,
            "scheduler_rpc_scope": "This direct local Recorder/Session/viewer path performs no scheduler request",
        },
        "latency": summaries,
        "elapsed_seconds": time.monotonic() - started,
        "goals": {"input_p95_below_ms": 100, "committed_to_visible_p95_below_ms": 2000},
        "qualification": "Real local Chromium, verified journals, owned Session index-ingestion thread and HTTP range service; no live Slurm, GPU or remote qualification",
        "method": {
            "input": "DOM click/keyboard handler dispatch to two browser animation-frame acknowledgements; includes UI update/paint scheduling, excludes OS/terminal/remote transport latency",
            "freshness": "Parent monotonic clock from immediately before Recorder.flush to browser event-list plus two animation-frame acknowledgement; includes flush and pipe round-trip overhead",
            "percentile": "Nearest-rank p95; raw observations retained",
            "scope": "Modest deterministic synthetic workload. Resource/memory costs and application overhead require separate controlled capture/index benchmarks.",
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=int, default=20)
    parser.add_argument("--lanes", type=int, default=16)
    parser.add_argument("--chromium")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not 20 <= args.observations <= 200 or not 1 <= args.lanes <= 128:
        parser.error("Choose 20–200 observations and 1–128 lanes")
    result = measure(
        observations=args.observations, lanes=args.lanes, chromium=args.chromium
    )
    raw = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(raw)
    else:
        print(raw, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
