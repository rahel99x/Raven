#!/usr/bin/env python3
"""Profile one pytest command without weakening its selection or assertions.

Run serial and parallel commands separately on a quiet host. Linux /proc RSS
samples sum live processes (shared pages can be counted more than once); this
is not PSS or Python allocations. wait4 CPU/block-I/O includes waited children.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def fingerprint():
    paths = [ROOT / 'pyproject.toml']
    for name in ('raven', 'tests', 'scripts'):
        paths.extend(path for path in (ROOT / name).rglob('*.py') if '__pycache__' not in path.parts)
    paths.extend((ROOT / 'docs/schemas').glob('*.json'))
    records = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}
    return {'sha256': hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(), 'files': records}


def process(pid, *, rss=True):
    try:
        stat = (Path('/proc') / str(pid) / 'stat').read_text()
        fields = stat[stat.rfind(')') + 2:].split()
        memory = 0
        if rss:
            status = (Path('/proc') / str(pid) / 'status').read_text()
            memory = next((int(line.split()[1]) for line in status.splitlines() if line.startswith('VmRSS:')), 0)
        io = None
        if rss:
            try:
                io = {line.split(':', 1)[0]: int(line.split(':', 1)[1]) for line in (Path('/proc') / str(pid) / 'io').read_text().splitlines()}
            except (OSError, ValueError):
                pass
        return (pid, fields[19]), memory, int(fields[1]), io
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
        return None


def sample(pid, expected_start):
    # Some Linux containers omit task/*/children. Build parent links from stat;
    # only read the larger status files for descendants of our exact process.
    children = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        record = process(int(path.name), rss=False)
        if record is not None:
            children.setdefault(record[2], []).append(record[0])
    todo, visited, rows = [(pid, expected_start)], set(), []
    while todo:
        current, expected = todo.pop()
        if current in visited:
            continue
        visited.add(current)
        record = process(current)
        if record is None or record[0][1] != expected:
            continue
        identity, rss, _, io = record
        rows.append((identity, rss, io))
        todo.extend(children.get(current, ()))
    return rows


def junit(path):
    if not path.exists():
        return {'available': False}
    if path.stat().st_size > 32 << 20:
        return {'available': False, 'error': 'JUnit exceeds32MiB'}
    try:
        tree = ET.fromstring(path.read_bytes())
    except ET.ParseError as exc:
        return {'available': False, 'error': str(exc)}
    cases = tree.findall('.//testcase')
    names = sorted((case.get('classname', ''), case.get('name', '')) for case in cases)
    counts = {key: sum(case.find(key) is not None for case in cases) for key in ('failure', 'error', 'skipped')}
    return {'available': True, 'cases': len(cases), **counts,
            'identity_multiset_sha256': hashlib.sha256(json.dumps(names).encode()).hexdigest(),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New report path; log/JUnit/source inventories beside it')
    parser.add_argument('--cadence', type=float, default=.2)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or not .05 <= args.cadence <= 2:
        parser.error('Provide a command and a0.05–2s sampling cadence')
    output = args.output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    log = output.with_suffix('.log'); xml = output.with_suffix('.xml'); source_file = output.with_suffix('.sources.json')
    if any(path.exists() for path in (output, log, xml, source_file)):
        parser.error('Profile paths must be new; existing evidence is never overwritten')
    if any(value.startswith('--junitxml') or value.startswith('--junit-xml') for value in command):
        parser.error('The profiler owns the JUnit path; omit a separate --junitxml argument')
    command += ['--junitxml=' + str(xml)]
    before = fingerprint()
    source_file.write_text(json.dumps(before, indent=2) + '\n')
    started_utc = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    self_before = resource.getrusage(resource.RUSAGE_SELF)
    io_peaks = {key: 0 for key in ("rchar", "wchar", "read_bytes", "write_bytes")}
    io_available = False
    peaks, peak_tree, peak_count, samples = {}, 0, 0, 0
    interrupted = False
    with log.open('xb') as stream:
        child = subprocess.Popen(command, cwd=ROOT, env=os.environ.copy(), stdout=stream,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        initial = process(child.pid)
        expected_start = initial[0][1] if initial else None
        print(json.dumps({'started': started_utc, 'pid': child.pid, 'command': command, 'log': str(log)}), flush=True)
        try:
            while child.poll() is None:
                rows = sample(child.pid, expected_start) if expected_start is not None else []
                for identity, rss, _ in rows:
                    peaks[identity] = max(peaks.get(identity, 0), rss)
                peak_tree = max(peak_tree, sum(rss for _, rss, _ in rows))
                counters = [io for _, _, io in rows if io is not None]
                if counters:
                    io_available = True
                    for key in io_peaks:
                        io_peaks[key] = max(io_peaks[key], sum(io.get(key, 0) for io in counters))
                peak_count = max(peak_count, len(rows)); samples += 1
                time.sleep(args.cadence)
        except KeyboardInterrupt:
            interrupted = True
            record = process(child.pid)
            if record is not None and record[0][1] == expected_start:
                os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                record = process(child.pid)
                if record is not None and record[0][1] == expected_start:
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
        code = child.wait()
    elapsed = time.monotonic() - started
    ended_utc = datetime.now(timezone.utc).isoformat()
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    own = resource.getrusage(resource.RUSAGE_SELF)
    after = fingerprint()
    result = {'schema': 'raven.test-profile/v1', 'started': started_utc, 'ended': ended_utc,
        'command': command, 'cwd': str(ROOT), 'exit_code': code, 'interrupted': interrupted,
        'wall_seconds': elapsed, 'user_cpu_seconds': usage.ru_utime - usage_before.ru_utime,
        'system_cpu_seconds': usage.ru_stime - usage_before.ru_stime,
        'profiler_cpu_seconds': own.ru_utime + own.ru_stime - self_before.ru_utime - self_before.ru_stime,
        'peak_live_tree_io_counters': io_peaks if io_available else None,
        'input_blocks': usage.ru_inblock - usage_before.ru_inblock, 'output_blocks': usage.ru_oublock - usage_before.ru_oublock,
        'peak_aggregate_live_rss_kib': peak_tree, 'peak_individual_sampled_rss_kib': max(peaks.values(), default=0),
        'waited_process_maxrss_kib': usage.ru_maxrss, 'peak_live_process_count': peak_count,
        'observed_processes': len(peaks), 'samples': samples, 'cadence_seconds': args.cadence,
        'process_peaks': [{'pid': key[0], 'start_ticks': key[1], 'rss_kib': value} for key, value in sorted(peaks.items())],
        'junit': junit(xml), 'source_before': before['sha256'], 'source_after': after['sha256'],
        'source_stable': before['sha256'] == after['sha256'], 'rss_sampling_available': bool(peaks),
        'environment': {'python': sys.version, 'platform': sys.platform, 'cpu_count': os.cpu_count(),
                        'pytest_addopts': os.environ.get('PYTEST_ADDOPTS', ''), 'pythonhashseed': os.environ.get('PYTHONHASHSEED')},
        'limits': 'Linux /proc sampling can miss short-lived peaks; summed live RSS includes shared pages, not PSS. CPU and block-I/O use waited child resource counters. /proc I/O fields are peak non-atomic sums of live process counters (including waited descendants), not exact final totals; read_bytes/write_bytes are storage bytes and rchar/wchar include cached I/O. Observed wall time includes up to one sampling interval of exit detection. No tracemalloc instrumentation. Same inherited environment; caller must retain identical extras and selection.'}
    output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: value for key, value in result.items() if key not in {'command', 'process_peaks', 'environment'}}))
    return 130 if interrupted else code if code else 0 if result['source_stable'] and result['junit'].get('available') and result['rss_sampling_available'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
