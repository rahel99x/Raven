#!/usr/bin/env python3
"""Equivalent bounded stream ingestion with/without experimental detection."""
import hashlib
import json
from pathlib import Path
import resource
import sys
import time
import tracemalloc
from collections import deque
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from raven import stream_anomalies as A

identities = [{"scope": "synthetic", "attempt": str(i), "job": str(i), "verified": True} for i in range(64)]

def run(enabled):
    monitor = A.Monitor()
    retained = {}
    for t in range(48):
        for identity in identities:
            for metric, unit, value in (("cpu", "fraction", .5), ("rss", "B", 104857600)):
                retained.setdefault((identity['attempt'], metric), deque(maxlen=32)).append((t, value))
                if enabled: monitor.update(identity, metric, unit, t, value)
    return monitor

result = {"observations": 6144, "streams": 128, "source_sha256": hashlib.sha256(Path(A.__file__).read_bytes()).hexdigest()}
for enabled in (False, True):
    wall, cpu = [], []
    for _ in range(3):
        w,c=time.perf_counter(),time.process_time(); monitor=run(enabled)
        wall.append(time.perf_counter()-w);cpu.append(time.process_time()-c)
    tracemalloc.start(); monitor=run(enabled);_,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
    result['monitor' if enabled else 'retention_baseline']={'wall_seconds':wall,'cpu_seconds':cpu,'python_peak_bytes':peak,'streams':len(monitor.streams),'retained_detector_points':sum(len(d.points) for d in monitor.streams.values())}
result['process_peak_rss_kib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
result['limits']='Synthetic bounded in-memory ingestion; process RSS includes both variants. No scheduler, filesystem, GPU or UI-paint work.'
print(json.dumps(result,indent=2))
