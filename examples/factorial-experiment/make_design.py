#!/usr/bin/env python3
"""Print the reproducible local diagnostic design with exact source identity."""
import hashlib
import json
from pathlib import Path
import sys

source = Path(__file__).with_name('reduction.py').resolve()
print(json.dumps({'schema': 'raven.factorial-design/v1', 'id': 'controlled-reduction', 'seed': 47,
 'order': 'blocked', 'repetitions': 4,
 'factors': [{'id': name, 'levels': [{'id': level, 'arguments': ['--' + name, level]} for level in levels]}
             for name, levels in [('order', ['forward', 'paired']), ('method', ['plain', 'accurate'])]],
 'equivalence': {'reference': 1., 'absolute': .01, 'relative': 0., 'semantic': 'sum(1e16,1,-1e16)', 'unit': 'sum'},
 'recipe': {'schema': 'raven.diagnostic-campaign/v1', 'id': 'template', 'kind': 'campaign', 'mode': 'local-diagnostic',
 'command': [str(Path(sys.executable).resolve()), '{file:reduction.py}', '{result}'],
 'files': [{'name': 'reduction.py', 'path': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}],
 'cases': [{'id': 'template', 'arguments': []}],
 'limits': {'max_trials': 16, 'trial_seconds': 2, 'total_seconds': 30, 'cpus': 1, 'memory_mb': 256, 'output_bytes': 8192, 'repeats': 1},
 'oracle': {'exit_code': 0, 'contains': 'MEASURED'}}}, indent=2))
