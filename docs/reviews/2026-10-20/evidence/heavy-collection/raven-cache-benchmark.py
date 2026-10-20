"""Equivalent completed read admissions; run phases serially without other tests."""
from pathlib import Path
import json
import os
import resource
import statistics
import sys
import tempfile
import time
import tracemalloc
import raven
from raven.shared_collection import SharedQueryCache
from raven.raven_collection import Descriptor, Ledger

connections = []
import sqlite3
original = sqlite3.connect
def connect(*args, **kwargs):
    connections.append(1)
    return original(*args, **kwargs)
sqlite3.connect = connect
with tempfile.TemporaryDirectory(prefix='raven-review-cache-') as directory:
    cache = SharedQueryCache(directory)
    ledger = Ledger(cache, {'target': 'local-fixture', 'user': str(os.getuid())}, {'filesystem_metadata_per_minute': 1000, 'local_processes_per_minute': 1000})
    connections.clear()
    times = []
    cpu = time.process_time()
    start = time.monotonic()
    for index in range(100):
        begin = time.monotonic()
        with ledger.admit(Descriptor('fixture-stat', 'filesystem', 'trace'), ['fixture-stat', str(index)]):
            pass
        times.append((time.monotonic()-begin)*1000)
    wall, cpu = time.monotonic()-start, time.process_time()-cpu
    count = len(connections)
    tracemalloc.start()
    for index in range(10):
        with ledger.admit(Descriptor('fixture-stat', 'filesystem', 'trace'), ['allocation', str(index)]):
            pass
    allocation_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    report = {'source': raven.__file__, 'python': sys.version, 'commands': 100, 'wall_seconds': wall,
              'process_cpu_seconds': cpu, 'median_command_ms': statistics.median(times), 'sqlite_connect_calls': count,
              'process_peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              'python_allocation_peak_bytes': allocation_peak,
              'qualification': 'Local completed advisory admissions on /tmp filesystem; no scheduler or remote I/O. Separate SQL admission/publication transactions preserve live reservations.'}
    Path(sys.argv[1]).write_text(json.dumps(report, indent=2)+'\n')
