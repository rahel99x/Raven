#!/usr/bin/env python3
"""Compare identical structural contracts; full JSON Schema stays development-only."""
import json
import hashlib
from pathlib import Path
import resource
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
started = time.perf_counter()
from raven.protocol_contracts import worker_control, worker_control_schema
light_import = time.perf_counter() - started
started = time.perf_counter()
from jsonschema import Draft202012Validator
full_import = time.perf_counter() - started

document = {"op": "cancel", "session_id": "session", "generation": 1, "id": "task"}
validator = Draft202012Validator(worker_control_schema())
result = {"iterations": 5000, "light_import_seconds": light_import, "jsonschema_import_seconds": full_import,
          "contract_sha256": hashlib.sha256((Path(__file__).resolve().parents[1] / "raven/protocol_contracts.py").read_bytes()).hexdigest(),
          "scope": "Worker control envelope only; domain ownership checks are additional and unchanged"}
for name, validate in (("typed", worker_control), ("jsonschema", validator.validate)):
    validate(document)
    times = []
    for _ in range(3):
        started = time.perf_counter()
        for _ in range(result["iterations"]):
            validate(document)
        times.append(time.perf_counter() - started)
    result[name + "_seconds"] = times
result["process_peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps(result, indent=2))
