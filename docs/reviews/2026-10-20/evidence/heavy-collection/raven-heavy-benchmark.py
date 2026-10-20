"""Run from an external driver with PYTHONPATH selecting baseline or delivered source."""
import json
import os
from pathlib import Path
import resource
import statistics
import sys
import time
import tracemalloc

import raven
from raven.raven_worker_client import WorkerClient

configuration = {"site_kind": "desktop", "desktop_explicit": True, "context": {},
                 "resources": {"memory_mib": 512, "cpus": 1, "max_tasks": 1, "wall_seconds": 120}}
document = {"document": {"schema": "raven.statistics/v1", "design": "independent", "unit": "seed",
             "control": [{"id": str(i), "value": i} for i in range(3)],
             "treatment": [{"id": str(i+3), "value": i+3} for i in range(3)]}}
client = WorkerClient(configuration)
try:
    startup = time.monotonic(); client.start()
    while not client.ready and time.monotonic() - startup < 5:
        client.poll(timeout=.1)
    assert client.ready, client.diagnostics
    startup = time.monotonic() - startup
    client.wait(client.submit("statistics", document, {}), 5)
    def cpu():
        fields = Path(f"/proc/{client.process.pid}/stat").read_text().rsplit(")", 1)[1].split()
        return sum(int(fields[index]) for index in (11, 12, 13, 14)) / os.sysconf("SC_CLK_TCK")
    before_cpu = cpu()
    latencies = []
    before = time.monotonic()
    for iteration in range(50):
        start = time.monotonic()
        result = client.wait(client.submit("statistics", document, {"iteration": iteration}), 5)
        assert result["schema"] == "raven.execution-result/v1"
        latencies.append((time.monotonic() - start) * 1000)
    wall = time.monotonic() - before; consumed = cpu() - before_cpu
    worker_peak = next(line for line in Path(f"/proc/{client.process.pid}/status").read_text().splitlines() if line.startswith("VmHWM:"))
    tracemalloc.start()
    for iteration in range(10):
        client.wait(client.submit("statistics", document, {"allocation_probe": iteration}), 5)
    allocation_peak = tracemalloc.get_traced_memory()[1]; tracemalloc.stop()
    report = {"source": raven.__file__, "python": sys.version, "tasks": 50,
              "startup_wall_ms": startup*1000, "task_median_wall_ms": statistics.median(latencies),
              "batch_wall_seconds": wall, "worker_and_children_cpu_seconds": consumed,
              "worker_peak_rss_kib": int(worker_peak.split()[1]),
              "driver_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "driver_python_allocation_peak_bytes": allocation_peak,
              "qualification": "Actual local software tasks; no scheduler, GPU or cross-host qualification. Run phases without competing heavy tests."}
    Path(sys.argv[1]).write_text(json.dumps(report, indent=2)+"\n")
finally:
    client.close()
