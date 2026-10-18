#!/usr/bin/env python3
"""Bounded as-of seek/layout costs; no pre-existing feature speedup is claimed."""
from __future__ import annotations
import argparse
import gc
import hashlib
import importlib
import json
from pathlib import Path
import platform
import resource
import statistics
import sys
import time
import tracemalloc

from benchmark_source import load_module


def measure(fn, repetitions):
    wall, cpu = [], []
    for _ in range(repetitions):
        start, processor = time.perf_counter(), time.process_time()
        fn()
        wall.append(time.perf_counter()-start); cpu.append(time.process_time()-processor)
    gc.collect(); tracemalloc.start(); fn(); _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
    return dict(median_seconds=statistics.median(wall), p95_seconds=sorted(wall)[max(0,int(len(wall)*.95)-1)],
                median_cpu_seconds=statistics.median(cpu), python_peak_bytes=peak, repetitions=repetitions)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--jobs', type=int, default=1000)
    parser.add_argument('--frames', type=int, default=32)
    parser.add_argument('--repetitions', type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 2000 or not 1 <= args.frames <= 64 or not 1 <= args.repetitions <= 100:
        parser.error('Use 1..2000 jobs, 1..64 frames and 1..100 repetitions')
    H = load_module(args.source_root, 'historical_replay')
    UI = load_module(args.source_root, 'historical_ui')
    timeline = H.Timeline()
    def snapshot(index):
        moment = 1792233000+index*2
        return dict(observed_at=moment, monotonic=100+index*2, source_time=moment-1,
            observer_id='benchmark', source_scope='isolated', jobs=[dict(id=str(n), name='job-'+str(n),
                state='RUNNING' if (n+index)%3 else 'PENDING', submit='2026-10-17T10:00:00', start='2026-10-17T10:00:01', cluster='bench') for n in range(args.jobs)],
            history=[], transitions=[], selected='0', identity=None, attempts={}, source_health={},
            samples=[dict(t=moment-200+n,k='live',cpu=(n%10)/10,rss=(n+1)*(1<<20)) for n in range(200)],
            coverage=dict(jobs_omitted=0, history_omitted=0,samples_omitted=0,series_scope='benchmark selected'))
    publication = snapshot(0)
    capture = measure(lambda:H.prepare(H.encoded(publication)),args.repetitions)
    for index in range(args.frames):timeline.append(H.encoded(snapshot(index)))
    before = H.encoded(timeline.document())
    target = timeline.frames[-1]['observed_at']
    sequence = timeline.frames[-1]['sequence']
    assert timeline.at(target)['sequence'] == sequence
    assert H.encoded(H.Timeline.load(before).document()) == before
    seek = measure(lambda:timeline.at(target),args.repetitions)
    clone = measure(timeline.clone,args.repetitions)
    jobs = measure(lambda:UI._view(timeline,sequence,None,None,'jobs',False,72,True,1),args.repetitions)
    charts = measure(lambda:UI._view(timeline,sequence,None,None,'analytics',False,72,True,1),args.repetitions)
    diff = measure(lambda:UI._view(timeline,sequence,None,None,'jobs',True,72,True,1),args.repetitions)
    result = dict(schema='raven.historical-benchmark/v1', python=platform.python_version(),platform=platform.platform(),
        fixture=dict(jobs=args.jobs,frames_requested=args.frames,frames_retained=len(timeline.frames),samples_per_frame=200,sha256=hashlib.sha256(before).hexdigest()),
        source_hashes={name:hashlib.sha256((Path(args.source_root)/'raven'/name).read_bytes()).hexdigest() for name in ('historical_replay.py','historical_ui.py','charts.py')},
        snapshot_prepare=capture,seek=seek,metadata_clone_on_ui=clone,jobs_prepare=jobs,analytics_prepare=charts,diff_prepare=diff,
        encoded_snapshots_bytes=timeline.bytes,archive_bytes=len(before),snapshot_metadata_bytes=len(before)-timeline.bytes,
        process_max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        limits='Single process fresh fixture; Python peaks are per operation, RSS is whole-process maximum. No scheduler/I/O commands; persistence bytes measured, no disk-throughput claim. This is a new capability, not an old/new speedup.')
    print(json.dumps(result,indent=2,sort_keys=True))

if __name__=='__main__':main()
