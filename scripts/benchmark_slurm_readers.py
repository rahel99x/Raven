#!/usr/bin/env python3
"""Compare qualified JSON/legacy readers on the same recorded stable facts.

Run without competing tests: python scripts/benchmark_slurm_readers.py.
This is a CPU/allocation benchmark, not a live scheduler latency measurement.
"""
import copy
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys


def child(mode):
    import resource
    import time
    import tracemalloc
    from raven.slurm import ParseDiagnostics, parse_jobs
    from raven.slurm_json import document, queue
    root = Path(__file__).resolve().parents[1]
    corpus = root / "tests/fixtures/slurm/recorded/26.05.5"
    structured = json.loads((corpus / "pending-squeue-json.stdout.txt").read_text())
    row = (corpus / "pending-squeue.stdout.txt").read_text().strip().split("|")
    rows, jobs = [], []
    for index in range(500):
        item = copy.deepcopy(structured["jobs"][0])
        item["job_id"] = index + 1
        jobs.append(item)
        rows.append("|".join([str(index + 1), *row[1:]]))
    structured["jobs"] = jobs
    text = json.dumps(structured) if mode == "json" else "\n".join(rows)
    read = (lambda: queue(document(text), ParseDiagnostics("jobs"))) if mode == "json" else (lambda: parse_jobs(text))
    for _ in range(3):
        assert len(read()) == 500
    samples = []
    for _ in range(7):
        start = time.process_time()
        result = read()
        samples.append(1000 * (time.process_time() - start))
        assert len(result) == 500
    del result
    tracemalloc.start()
    result = read()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(json.dumps({"mode": mode, "cpu_ms_500_rows": samples, "median_cpu_ms": statistics.median(samples),
                      "peak_python_bytes": peak, "peak_process_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      "fixture_bytes": len(text.encode()), "records": len(result)}))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        child(sys.argv[1])
    else:
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(root))
        samples = []
        for mode in ("legacy", "json", "json", "legacy"):
            result = subprocess.run([sys.executable, __file__, mode], cwd=root, env=env, check=True, capture_output=True, text=True)
            samples.append(json.loads(result.stdout))
        print(json.dumps({"scope": "500 copies of a real recorded pending job with unique IDs; same stable facts; no scheduler/network IO",
                          "memory": "tracemalloc peak and independent process peak RSS; fixture setup included only in RSS",
                          "samples": samples}, indent=2))
