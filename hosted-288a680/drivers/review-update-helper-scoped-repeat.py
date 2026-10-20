#!/usr/bin/env python3
"""Repeat the actual scoped fixture with temporary real Git repositories."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import tempfile
import time

from pytest import MonkeyPatch

parser = argparse.ArgumentParser()
parser.add_argument('--iterations', type=int, default=20)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
root = Path('/workspace/Raven')
module_path = root / 'tests/test_update_source.py'
spec = importlib.util.spec_from_file_location('review_update_source_fixture', module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
counts = []
started = time.monotonic()
fd_before = len(os.listdir('/proc/self/fd'))
for iteration in range(args.iterations):
    with tempfile.TemporaryDirectory(prefix='raven-git-owned-repeat-') as directory:
        location = Path(directory)
        checkout = module.checkout.__wrapped__(location)
        with MonkeyPatch.context() as monkeypatch:
            module.test_exited_git_leader_cannot_strand_helper_holding_stdout(
                checkout, location, monkeypatch)
        counts.append({'iteration': iteration + 1, 'status': 'passed',
                       'fd_count': len(os.listdir('/proc/self/fd'))})
report = {'python': platform.python_version(), 'iterations': args.iterations,
          'results': counts, 'fd_before': fd_before,
          'fd_after': len(os.listdir('/proc/self/fd')),
          'seconds': time.monotonic() - started,
          'limits': 'Actual local Linux subprocess fixture; not hosted-cause proof or performance benchmark.',
          'sources': {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in (module_path, root / 'raven/installation/update_source.py')}}
args.output.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({k: report[k] for k in ('python', 'iterations', 'fd_before', 'fd_after', 'seconds')}))
assert report['fd_before'] == report['fd_after']
