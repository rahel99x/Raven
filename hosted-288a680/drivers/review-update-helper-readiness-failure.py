#!/usr/bin/env python3
"""Fault-inject a fixture readiness failure; verify its failure cleanup."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
from unittest.mock import patch

from pytest import MonkeyPatch

root = Path('/workspace/Raven')
path = root / 'tests/test_update_source.py'
spec = importlib.util.spec_from_file_location('review_update_source_failure_fixture', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
real_popen = subprocess.Popen
real_read = Path.read_text
created, helper = [], []
def identity(pid):
    try:
        fields = real_read(Path(f'/proc/{pid}/stat')).split(') ', 1)[1].split()
        return {'pid': pid, 'state': fields[0], 'pgid': int(fields[2]),
                'sid': int(fields[3]), 'birth': fields[19]}
    except (FileNotFoundError, ProcessLookupError):
        return None

def record(*args, **kwargs):
    process = real_popen(*args, **kwargs)
    created.append((process, identity(process.pid)))
    return process

def incomplete_pid(path, *args, **kwargs):
    if path.name == 'helper.pid':
        try:
            content = real_read(path, *args, **kwargs)
        except FileNotFoundError:
            return ''
        if content.strip().isdigit() and not helper:
            helper.append(identity(int(content.strip())))
        return ''  # deliberate failed readiness proof, never a production edit
    return real_read(path, *args, **kwargs)

started = time.monotonic()
with tempfile.TemporaryDirectory(prefix='raven-helper-readiness-failure-') as location:
    tmp = Path(location)
    checkout = module.checkout.__wrapped__(tmp)
    with MonkeyPatch.context() as monkeypatch:
        with patch.object(subprocess, 'Popen', record), patch.object(Path, 'read_text', incomplete_pid):
            try:
                module.test_exited_git_leader_cannot_strand_helper_holding_stdout(checkout, tmp, monkeypatch)
            except AssertionError:
                pass
            else:
                raise AssertionError('Readiness fault was not exercised')
results = [{'before': proof, 'after': identity(process.pid), 'returncode': process.returncode}
           for process, proof in created]
helper_after = identity(helper[0]['pid']) if helper and helper[0] else None
assert all(item['after'] is None or item['after']['state'] == 'Z' for item in results)
assert helper_after is None or helper_after['state'] == 'Z'
report = {'status': 'passed', 'seconds': time.monotonic() - started,
          'failure': 'Forced empty helper PID readiness, expected AssertionError before U.git receives Popen.',
          'children': results, 'helper_before': helper, 'helper_after': helper_after,
          'test_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
          'limits': 'Failure-cleanup probe only; does not identify original hosted failure cause.'}
output = Path('/workspace/review-update-helper-readiness-failure-310.json')
output.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({'status': report['status'], 'seconds': report['seconds'], 'children': len(results)}))
