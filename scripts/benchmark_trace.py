#!/usr/bin/env python3
"""Controlled recorder comparison; launch each fixture in a fresh process.

Reports process CPU/wall seconds and peak process RSS (not Python allocations).
No scheduler, GPU, or remote work is performed. Trace storage uses a temporary
local directory; full durability includes fsync at 256 events and clean close.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile
import threading
import time


def workload(case, iterations):
    def call(value):
        return (value * 17 + 3) % 101
    def calls():
        return sum(call(index) for index in range(iterations))
    def generate():
        for index in range(iterations):
            yield call(index)
    async def asynchronous():
        total = 0
        for value in generate():
            total += value
            await asyncio.sleep(0)
        return total
    if case == 'calls':
        return calls()
    if case == 'async-generator':
        return asyncio.run(asynchronous())
    values = []
    threads = [threading.Thread(target=lambda: values.append(calls())) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return sum(values)


def child(args):
    sys.path.insert(0, str(args.source_root.resolve()))
    from raven.trace_recorder import Recorder, PythonCapture
    import raven.trace_protocol as protocol
    with tempfile.TemporaryDirectory(prefix='raven-trace-benchmark-') as directory:
        recorder = None
        if args.adapter != 'disabled':
            recorder = Recorder(directory, background_flush=False, flush_events=256, flush_interval=60,
                max_bytes=256 << 20, coverage={'adapter': 'benchmark', 'scope': 'fixture'})
            capture = PythonCapture(recorder, legacy=args.adapter == 'legacy')
        start, cpu = time.perf_counter(), time.process_time()
        if recorder is not None:
            capture.start()
        try:
            result = workload(args.case, args.iterations)
        finally:
            if recorder is not None:
                capture.stop()
                recorder.close()
        elapsed, used = time.perf_counter() - start, time.process_time() - cpu
        manifest = protocol.read_json(recorder.path) if recorder else {}
        print(json.dumps({'case': args.case, 'adapter': args.adapter, 'wall_seconds': elapsed,
            'cpu_seconds': used, 'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1024 if sys.platform != 'darwin' else 1),
            'journal_bytes': manifest.get('committed_bytes', 0), 'events': manifest.get('committed_seq', 0),
            'dropped': manifest.get('dropped', 0), 'state': manifest.get('state'), 'answer': result,
            'python': sys.version.split()[0], 'source_root': str(args.source_root)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--baseline-root', type=Path)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--iterations', type=int, default=100)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--case', choices=('calls', 'async-generator', 'threads'), default='calls')
    parser.add_argument('--adapter', choices=('disabled', 'legacy', 'monitoring'), default='disabled')
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20 or not 1 <= args.iterations <= 10000:
        parser.error('repeats must be 1–20 and iterations 1–10000')
    if args.child:
        child(args)
        return
    roots = [args.source_root] if args.baseline_root is None else [args.baseline_root, args.source_root]
    samples = []
    adapters = ['disabled', 'legacy'] + (['monitoring'] if hasattr(sys, 'monitoring') else [])
    for repeat in range(args.repeats):
        for case in ('calls', 'async-generator', 'threads'):
            for adapter in adapters:
                for root in roots if repeat % 2 == 0 else list(reversed(roots)):
                    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--child',
                        '--source-root', str(root), '--case', case, '--adapter', adapter,
                        '--iterations', str(args.iterations)], capture_output=True, text=True, timeout=60, check=True)
                    samples.append(dict(json.loads(completed.stdout), repetition=repeat))
    report = {'schema': 'raven.trace-benchmark/v1', 'iterations': args.iterations, 'repeats': args.repeats,
              'scope': 'local temporary filesystem; peak RSS is process high-water, not Python allocations', 'samples': samples}
    raw = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(raw)
    print(raw)


if __name__ == '__main__':
    main()
