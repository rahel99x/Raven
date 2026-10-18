#!/usr/bin/env python3
"""Developer qualification against separately supplied Perfetto/speedscope tools."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from raven.trace_export import convert
from raven.trace_protocol import read_trace
from raven.trace_recorder import PythonCapture, Recorder
from raven.ui_trace import UITrace
from raven.worker_scheduler import WorkerScheduler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--processor', required=True)
    parser.add_argument('--speedscope-schema', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    trace = UITrace()
    app = SimpleNamespace(tab='jobs', mode='main', width=80, height=24)
    trace.measure(app, 'document', lambda: trace.measure(app, 'paint', time.sleep, .025))
    scheduler = WorkerScheduler(workers=1, mode='single')
    scheduler.ui_trace = trace
    try:
        assert scheduler.lane('source').submit(lambda: 17).result(timeout=5) == 17
    finally:
        scheduler.shutdown(wait=True)
    ui_path = output / 'ui.json'
    ui_path.write_text(json.dumps(trace.report()))
    (output / 'journal').mkdir()
    recorder = Recorder(output / 'journal', job_id='qualification', attempt='interop', background_flush=False,
                        coverage={'adapter': 'python-sys-monitoring', 'scope': 'CPython 3.12+ local qualification fixture'})
    capture = PythonCapture(recorder)
    def workload():
        return sum(i * i for i in range(25))
    try:
        capture.start()
        assert workload() == 4900
    finally:
        capture.stop()
        recorder.close()
    snapshot = read_trace(recorder.path)
    assert snapshot['status'] == 'COMPLETE'
    for format in ('perfetto', 'speedscope'):
        (output / (format + '.json')).write_text(json.dumps(convert(snapshot, format)))
    queries = []
    def query(path, sql):
        command = [args.processor, 'query', str(path), sql]
        run = subprocess.run(command, text=True, capture_output=True, timeout=30)
        queries.append(dict(command=command, returncode=run.returncode, stdout=run.stdout, stderr=run.stderr))
        assert run.returncode == 0, run.stderr
        return list(csv.DictReader(io.StringIO(run.stdout)))
    slices = query(ui_path, "SELECT name,dur FROM slice WHERE name IN ('document','paint') ORDER BY name")
    assert len(slices) == 2 and all(int(row['dur']) >= 25_000_000 for row in slices)
    assert int(query(ui_path, 'SELECT count(*) AS count FROM flow')[0]['count']) == 1
    assert query(ui_path, "SELECT name,value FROM stats WHERE severity='error' AND value>0") == []
    assert int(query(output / 'perfetto.json', 'SELECT count(*) AS count FROM slice WHERE dur>0')[0]['count']) > 0
    import jsonschema
    schema_bytes = Path(args.speedscope_schema).read_bytes()
    jsonschema.validate(json.loads((output / 'speedscope.json').read_text()), json.loads(schema_bytes))
    result = dict(schema='raven.trace-interop-qualification/v1', passed=True,
        processor_sha256=hashlib.sha256(Path(args.processor).read_bytes()).hexdigest(),
        speedscope_schema_sha256=hashlib.sha256(schema_bytes).hexdigest(),
        source_sha256={name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in
            ('raven/ui_trace.py', 'raven/trace_export.py', 'raven/trace_protocol.py', 'raven/trace_recorder.py', 'raven/worker_scheduler.py')},
        queries=queries, speedscope_validation='official JSON Schema accepted actual monitoring recording',
        limits='Injected sleep and local captured Python fixture; no remote, GPU, display-latency or overhead qualification.')
    (output / 'qualification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(dict(passed=True, report=str(output / 'qualification.json'))))


if __name__ == '__main__':
    main()
