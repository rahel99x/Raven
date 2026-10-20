#!/usr/bin/env python3
"""Equivalent isolated analysis fixtures; scheduler/GPU efficacy is not measured."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time


def lineage_document():
    count, groups = 4000, 256
    nodes, actions = [], []
    for i in range(count):
        nodes.extend([{'id': f'a{i}', 'kind': 'artifact', 'dependencies': [], 'complete': True},
                      {'id': f'c{i}', 'kind': 'claim', 'dependencies': [f'a{i}'], 'complete': True}])
    for group in range(groups):
        actions.append({'id': f'check{group:04d}', 'covers': [key for i in range(group, count, groups) for key in (f'a{i}', f'c{i}')],
                        'requires': [], 'cost': 1, 'method': 'declared check'})
    return {'schema': 'raven.scientific-lineage/v1', 'nodes': nodes, 'changed': [f'a{i}' for i in range(count)], 'actions': actions}


def comparison_fixture():
    base = {'name': 'same-work', 'partition': 'local', 'account': 'lab', 'qos': 'normal',
            'cpus': 4, 'nodes': 1, 'gpus': 0, 'mem_bytes': 32 << 30}
    candidates = [dict(base, label=f'walltime-{i}', time_seconds=3600 + i) for i in range(64)]
    history = [dict(base, id=str(i + 1), time_seconds=3600 + i % 64, state='COMPLETED',
                    submit=3 * i, start=3 * i + 10 + i % 3, end=3 * i + 110 + i % 3 + i % 7,
                    runtime_seconds=100 + i % 7, exit='0:0') for i in range(10000)]
    return candidates, history


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def child(source, case, allocations):
    sys.path.insert(0, str(Path(source).resolve()))
    import raven
    assert Path(raven.__file__).resolve().is_relative_to(Path(source).resolve())
    if case == 'lineage':
        from raven.scientific_lineage import analyze
        fixture = lineage_document()
        run = lambda: analyze(fixture)
    elif case == 'tradeoffs':
        from raven import tradeoffs
        tradeoffs.clock.now = lambda: 1e9
        fixture = comparison_fixture()
        run = lambda: tradeoffs.compare(fixture[0], history=fixture[1], queue_history=fixture[1])
    else:
        raise ValueError(case)
    if allocations:
        import tracemalloc
        tracemalloc.start()
    started = time.perf_counter()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    result = run()
    elapsed = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_SELF)
    peak = tracemalloc.get_traced_memory()[1] if allocations else None
    if case == 'lineage':
        assert len(result['proposed_actions']) == 256 and not result['unresolved_claims']
        fidelity = result
    else:
        fidelity = [{'resources': row['resources'], 'metrics': row['metrics'], 'pareto': row['pareto']} for row in result['candidates']]
    return {'wall_seconds': elapsed, 'cpu_seconds': after.ru_utime + after.ru_stime - usage.ru_utime - usage.ru_stime,
            'process_peak_rss_kib': after.ru_maxrss, 'python_allocation_peak_bytes': peak,
            'fixture_sha256': digest(fixture), 'result_sha256': digest(fidelity)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--child', choices=['lineage', 'tradeoffs'])
    parser.add_argument('--allocations', action='store_true')
    args = parser.parse_args()
    if args.child:
        print(json.dumps(child(args.source, args.child, args.allocations)))
        return 0
    if not args.baseline or not args.output:
        parser.error('--baseline and --output are required')
    results = {'schema': 'raven.analysis-review-benchmark/v1', 'limits': [
        'Equivalent synthetic algorithm fixtures, not live scheduler/site qualification.',
        'Linux process peak RSS includes interpreter/imports and fixture; tracemalloc is measured in a separate process.',
        'Run serially without concurrent builds/tests; shared cloud host activity outside this task remains uncontrolled.'], 'cases': {}}
    with tempfile.TemporaryDirectory(prefix='raven-analysis-benchmark-') as directory:
        for case in ('lineage', 'tradeoffs'):
            results['cases'][case] = {}
            for name, source in (('baseline', args.baseline), ('current', args.source)):
                runs = []
                for allocations in (False, True):
                    # Baseline allocation probes are deliberately omitted;
                    # tracing its repeated work exceeds the timing budget.
                    # Compare normal timings, and label allocation data alone.
                    if name == 'baseline' and allocations:
                        continue
                    cmd = [sys.executable, str(Path(__file__).resolve()), '--source', str(source.resolve()), '--child', case]
                    if allocations:
                        cmd.append('--allocations')
                    env = dict(os.environ, PYTHONPATH=str(source.resolve()), PYTHONHASHSEED='0')
                    completed = subprocess.run(cmd, cwd=directory, env=env, text=True, capture_output=True, timeout=120, check=True)
                    runs.append(json.loads(completed.stdout))
                results['cases'][case][name] = {'timing': runs[0], 'allocation_probe': runs[1] if len(runs) > 1 else None}
            baseline, current = (results['cases'][case][name]['timing'] for name in ('baseline', 'current'))
            assert baseline['fixture_sha256'] == current['fixture_sha256']
            results['cases'][case]['equivalent_result'] = baseline['result_sha256'] == current['result_sha256']
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + '\n')
    print(args.output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
