#!/usr/bin/env python3
"""Measure an actual curses PTY and its owned process tree for five minutes.

Run baseline and Light measurements sequentially after correctness tests. The
same 100-job simulation exercises real parsing, collection, rendering and input;
no Slurm daemon, scheduler mutation, network connection or user shell is needed.
This is a stated local fixture, not cluster qualification or visible-pixel timing.

Example:
  python scripts/benchmark_raven_resources.py --variant light --duration 300 \
      --output /tmp/raven-light-monitor.json
  python scripts/benchmark_raven_resources.py --variant baseline \
      --source-root /path/to/pristine/checkout --output /tmp/raven-baseline.json
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark_source import load_module, package_directory

ROOT = Path(__file__).resolve().parents[1]


def publish(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + '\n')
    os.replace(temporary, path)


def summary(values):
    if not values:
        return {'count': 0}
    ordered = sorted(values)
    return {'count': len(values), 'mean': statistics.fmean(values), 'min': ordered[0],
            'p50': statistics.median(ordered), 'p95': ordered[math.ceil(.95 * len(ordered)) - 1],
            'p99': ordered[math.ceil(.99 * len(ordered)) - 1], 'max': ordered[-1]}


def source_digest(root):
    state = hashlib.sha256()
    for path in sorted(package_directory(root).glob('*.py')):
        state.update(path.name.encode() + b'\0' + path.read_bytes())
    return state.hexdigest()


def fixture_specs(count):
    return [dict(id=str(8000000 + index), name='benchmark-%03d' % index, part='main',
                 submit=-7200-index, start=-600-index if index % 5 else None, dur=86400,
                 cpus=4, gt='', gn=0, nodes=1, mem='4G', limit='1-00:00:00',
                 node='n%03d' % (index % 8), prio=10000-index,
                 reason='Priority' if index % 5 == 0 else 'None') for index in range(count)]


def child(args):
    application = load_module(args.source_root)
    cli = load_module(args.source_root, 'cli')
    slurm = load_module(args.source_root, 'slurm')
    Backend, CommandError, FakeBackend = slurm.Backend, slurm.CommandError, slurm.FakeBackend
    Views = load_module(args.source_root, 'views').Views
    calls, results, observation = Counter(), Counter(), {'frames': 0, 'max_jobs': 0}
    backends = []
    original_init, original_run, original_compose = FakeBackend.__init__, FakeBackend.run, Views.compose

    def fake_init(self, *values, **kwargs):
        original_init(self, *values, **kwargs)
        self.spec = fixture_specs(args.jobs)
        self.others = []
        self.done = [dict(self.done[index % len(self.done)], id=str(7000000 + index),
                          name='prior-%03d' % index) for index in range(25)]
        backends.append(self)

    def fake_run(self, command, *values, **kwargs):
        name = os.path.basename(command[0])
        calls[name] += 1
        try:
            response = original_run(self, command, *values, **kwargs)
        except Exception:
            results['errors'] += 1
            raise
        results['response_bytes'] += len(response[0].encode())
        return response

    def no_external(*values, **kwargs):
        raise CommandError('Benchmark forbids real scheduler commands')

    def compose(self, snap, app, *values, **kwargs):
        observation['frames'] += 1
        observation['max_jobs'] = max(observation['max_jobs'], len(snap.get('jobs', ())))
        rendered = original_compose(self, snap, app, *values, **kwargs)
        if observation['max_jobs'] >= args.jobs and 'ready' not in observation:
            managed = bool(app.cfg.get('raven', {}).get('managed'))
            if managed != (args.variant == 'light'):
                raise RuntimeError('Requested baseline/managed Light variant was not active')
            idle_policy = getattr(app.sampler, 'collection', None)
            preaged = args.mode == 'idle' and bool(idle_policy) and hasattr(app.sampler, '_collection_activity')
            if preaged:
                # Enter the real steady-idle branch without adding a five-minute
                # wait before the separately measured five-minute idle window.
                app.sampler._collection_activity -= float(idle_policy.get('idle_after_seconds', 300)) + 1
            observation.update(ready=time.monotonic(), managed=managed,
                               retained_jobs=len(app.store.jobs), selected=app.selected_id,
                               steady_idle_timer_preaged=preaged,
                               process_id=os.getpid(), version=application.__version__)
            publish(args.ready, observation)
        return rendered

    FakeBackend.__init__, FakeBackend.run, Views.compose = fake_init, fake_run, compose
    Backend.run = Backend.call = no_external
    exit_code = 2
    try:
        common = ['--fake', '--no-state', '--no-plugins', '--no-gpu', '--user', 'benchmark', '--tab', 'jobs']
        if args.variant == 'light':
            light_main = load_module(args.source_root, 'raven_launch').light_main
            exit_code = light_main(['--config', str(args.config), *common])
        else:
            exit_code = cli.main(['--config', str(args.config), *common])
        return exit_code
    except BaseException as exc:
        observation['error_type'] = type(exc).__name__
        raise
    finally:
        observation.update(exit_code=exit_code, commands=dict(calls), responses=dict(results),
                           simulated_only=True, backend_instances=len(backends))
        publish(args.child_report, observation)


def bounded_disk(root):
    pending, count, size, complete = [root], 0, 0, True
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > 4096:
                        return {'files_and_directories': count, 'bytes': size, 'complete': False}
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    else:
                        size += entry.stat(follow_symlinks=False).st_size
        except OSError:
            complete = False
    return {'files_and_directories': count, 'bytes': size, 'complete': complete}


def process_sample(pid, scratch):
    # Parent-only diagnostics, even when the child uses a pristine old checkout.
    process_tree = load_module(ROOT, 'raven_diagnostics').process_tree
    tree = process_tree(pid)
    cpu, io, waited_available = 0., {}, True
    ticks = os.sysconf('SC_CLK_TCK')
    for row in tree['processes']:
        base = Path('/proc') / str(row['pid'])
        try:
            raw = (base / 'stat').read_text()[:8192]
            fields = raw[raw.rfind(')') + 2:].split()
            if int(fields[19]) != row['start_ticks']:
                raise ValueError('PID identity changed')
            # Waited descendants disappear from /proc but remain in cutime/
            # cstime. Sum these with living descendants' own counters once.
            cpu += sum(int(fields[index]) for index in (11, 12, 13, 14)) / ticks
            counters = dict(line.split(':', 1) for line in (base / 'io').read_text().splitlines() if ':' in line)
            io[str(row['pid']) + ':' + str(row['start_ticks'])] = {
                name: int(counters.get(name, 0)) for name in ('read_bytes', 'write_bytes', 'rchar', 'wchar')}
        except (OSError, ValueError, IndexError):
            cpu += row['cpu_seconds']
            waited_available = False
    return {'at': time.monotonic(), 'cpu_inclusive_seconds': cpu, 'cpu_complete': waited_available and tree['complete'],
            'rss_bytes': tree['rss_sum_bytes'], 'pss_bytes': tree['pss_sum_bytes'],
            'pss_complete': tree['pss_complete'], 'processes': len(tree['processes']),
            'threads': tree['threads'], 'descriptors': tree['descriptors'],
            'tree_complete': tree['complete'], 'io': io, 'disk': bounded_disk(scratch)}


def host_metadata():
    result = {'system': platform.platform(), 'python': platform.python_version(),
              'logical_cpus': os.cpu_count(), 'cpu_scope': '100 percent means one logical CPU'}
    for path, key in (('/sys/fs/cgroup/cpu.max', 'cgroup_cpu_max'), ('/sys/fs/cgroup/memory.max', 'cgroup_memory_max')):
        try:
            result[key] = Path(path).read_text()[:128].strip()
        except OSError:
            result[key] = None
    try:
        result['affinity_cpus'] = len(os.sched_getaffinity(0))
    except AttributeError:
        pass
    return result


def run(args):
    import fcntl
    import pty
    import select
    import signal
    import struct
    import subprocess
    import termios
    sys.path.insert(0, str(ROOT))
    if args.output.exists():
        raise ValueError('Benchmark output already exists; choose a new path')
    if not (package_directory(args.source_root) / 'cli.py').is_file():
        raise ValueError('Source root must contain the declared application CLI')
    measured_source = source_digest(args.source_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='raven-resource-') as temporary:
        scratch = Path(temporary)
        config, ready, child_report = (scratch / name for name in ('config.json', 'ready.json', 'child-report.json'))
        if args.variant == 'light':
            settings = {'schema': 'raven.config/v1', 'profile': 'resource-benchmark',
                        'site': {'kind': 'slurm', 'cluster_id': 'simulated'},
                        'storage': {'state_dir': str(scratch / 'state'), 'coordination_dir': str(scratch / 'coordination')}}
        else:
            settings = {'startup_animation': False}
        publish(config, settings)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', args.height, args.width, 0, 0))
        os.set_blocking(master, False)
        command = [args.python, '-S', str(Path(__file__).resolve()), '--child', '--source-root', str(args.source_root),
                   '--variant', args.variant, '--mode', args.mode, '--jobs', str(args.jobs), '--config', str(config),
                   '--ready', str(ready), '--child-report', str(child_report)]
        env = dict(os.environ, TERM='xterm-256color', LC_ALL='C.UTF-8', PYTHONDONTWRITEBYTECODE='1',
                   XDG_CONFIG_HOME=str(scratch / 'config'), XDG_STATE_HOME=str(scratch / 'state'))
        launched = time.monotonic()
        process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, cwd=args.source_root,
                                   env=env, close_fds=True, start_new_session=True)
        os.close(slave)
        output_hash, byte_count, tail = hashlib.sha256(), 0, bytearray()

        def drain(seconds):
            nonlocal byte_count
            deadline = time.monotonic() + max(0, seconds)
            while time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], min(.025, max(0., deadline-time.monotonic())))
                if readable:
                    try:
                        raw = os.read(master, 65536)
                    except (BlockingIOError, OSError):
                        return
                    if not raw:
                        return
                    byte_count += len(raw)
                    output_hash.update(raw)
                    tail.extend(raw[-8192:])
                    del tail[:-8192]

        samples, error, sent = [], '', 0
        try:
            deadline = launched + 90
            while not ready.exists() and time.monotonic() < deadline and process.poll() is None:
                drain(.05)
            if not ready.exists():
                raise RuntimeError('PTY did not render the complete fixture; child exit=' + str(process.poll()))
            readiness = json.loads(ready.read_text())
            if readiness['retained_jobs'] != args.jobs:
                raise RuntimeError('Fixture job retention differs from requested size')
            drain(args.warmup)
            start = time.monotonic()
            previous = process_sample(process.pid, scratch)
            initial_io = previous['io']
            initial_cpu = previous['cpu_inclusive_seconds']
            initial_bytes = byte_count
            next_sample, next_input = start + args.sample_seconds, start + 15
            while time.monotonic() - start < args.duration:
                if process.poll() is not None:
                    raise RuntimeError('PTY application exited before measurement completed')
                now = time.monotonic()
                if args.mode == 'monitor' and now >= next_input:
                    # Occasional real keyboard navigation, fixed viewport;
                    # no pointer flood or hidden render-loop shortcuts.
                    os.write(master, b'\x1b[B' if sent % 2 == 0 else b'\x1b[A')
                    sent += 1
                    next_input += 15
                if now >= next_sample:
                    current = process_sample(process.pid, scratch)
                    elapsed = current['at'] - previous['at']
                    current['cpu_percent'] = max(0., current['cpu_inclusive_seconds'] - previous['cpu_inclusive_seconds']) / elapsed * 100
                    current['elapsed'] = current['at'] - start
                    samples.append(current)
                    previous = current
                    next_sample += args.sample_seconds
                drain(min(.025, max(0., start + args.duration - time.monotonic())))
            final = process_sample(process.pid, scratch)
            elapsed = final['at'] - start
            final['cpu_percent'] = max(0., final['cpu_inclusive_seconds'] - previous['cpu_inclusive_seconds']) / max(.000001, final['at'] - previous['at']) * 100
            final['elapsed'] = elapsed
            samples.append(final)
            average_cpu = max(0., final['cpu_inclusive_seconds'] - initial_cpu) / elapsed * 100
            os.write(master, b'q')
            deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < deadline:
                drain(.05)
            if process.poll() is None or process.returncode != 0:
                raise RuntimeError('PTY did not shut down cleanly after quit')
            child_result = json.loads(child_report.read_text())
            if child_result.get('backend_instances') != 1 or child_result.get('max_jobs') != args.jobs:
                raise RuntimeError('Benchmark did not use exactly one complete simulated fixture')
            if source_digest(args.source_root) != measured_source:
                raise RuntimeError('Measured application sources changed during the benchmark')
            io_totals, io_previous = Counter(), dict(initial_io)
            for sample in samples:
                for identity, counters in sample['io'].items():
                    prior = io_previous.get(identity, counters)
                    io_totals.update({key: max(0, value-prior.get(key, 0)) for key, value in counters.items()})
                    io_previous[identity] = counters
            report = {'schema': 'raven.resource-benchmark/v1', 'variant': args.variant, 'mode': args.mode,
                      'source_root': str(args.source_root), 'source_sha256': measured_source,
                      'host': host_metadata(), 'child_python': args.python, 'terminal': {'width': args.width, 'height': args.height, 'type': 'PTY/xterm-256color'},
                      'duration_seconds': elapsed, 'warmup_seconds': args.warmup,
                      'startup_to_full_fixture_seconds': readiness['ready'] - launched,
                      'fixture': {'jobs': args.jobs, 'running': args.jobs - math.ceil(args.jobs/5), 'history_jobs': 25,
                                  'gpu_jobs': 0, 'ordinary_shells': 0, 'heavy_workers': 0,
                                  'steady_idle_timer_preaged': readiness['steady_idle_timer_preaged'], 'keyboard_events': sent},
                      'cpu_mean_percent_one_core': average_cpu,
                      'summary': {key: summary([sample[key] for sample in samples]) for key in
                                  ('cpu_percent', 'rss_bytes', 'pss_bytes', 'processes', 'threads', 'descriptors')},
                      'io_observed_bytes': dict(io_totals), 'retained_disk_final': final['disk'],
                      'output_bytes_measured': byte_count-initial_bytes, 'output_bytes_total': byte_count,
                      'output_sha256': output_hash.hexdigest(), 'child': child_result,
                      'complete': all(sample['tree_complete'] and sample['cpu_complete'] for sample in samples),
                      'pss_complete': all(sample['pss_complete'] for sample in samples), 'samples': samples,
                      'candidate_targets': {'cpu_percent_one_core': 1 if args.mode == 'idle' else 5, 'rss_bytes': 256 << 20},
                      'qualification': 'Measured local fixture only; candidate targets are not claims of site qualification.',
                      'limitations': ['CPU includes waited descendants; sampling can miss very short orphaned helpers.',
                                      'Process tree capped at 64 processes/128 threads per process; incomplete flags prevent silent qualification.',
                                      'PSS is Linux best-effort; IO counters omit work completed entirely between samples.',
                                      'Idle mode pre-ages the actual idle timer at the first complete frame; no input is injected afterward.',
                                      'No live Slurm, network latency, terminal emulator pixel latency, GPU job or opened shell is represented.']}
            publish(args.output, report)
            return report
        except Exception as exc:
            error = str(exc)
            publish(args.output, {'schema': 'raven.resource-benchmark/v1', 'failed': True, 'error': error,
                                 'variant': args.variant, 'mode': args.mode, 'samples': samples,
                                 'output_tail_escaped': tail.decode('utf-8', 'replace').encode('unicode_escape').decode()})
            raise
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            os.close(master)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--variant', choices=('baseline', 'light'), default='light')
    parser.add_argument('--mode', choices=('idle', 'monitor'), default='monitor')
    parser.add_argument('--duration', type=float, default=300)
    parser.add_argument('--warmup', type=float, default=10)
    parser.add_argument('--sample-seconds', type=float, default=1)
    parser.add_argument('--jobs', type=int, default=100)
    parser.add_argument('--width', type=int, default=160)
    parser.add_argument('--height', type=int, default=48)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--output', type=Path, default=Path('/tmp/raven-resource-benchmark.json'))
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    for name in ('config', 'ready', 'child-report'):
        parser.add_argument('--' + name, type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.source_root, args.output = args.source_root.resolve(), args.output.resolve()
    for key, low, high in (('duration', .25, 3600), ('warmup', 0, 600), ('sample_seconds', .1, 60)):
        value = getattr(args, key)
        if not math.isfinite(value) or not low <= value <= high:
            parser.error(key + ' is outside its bounded measurement range')
    if args.sample_seconds > args.duration or not 1 <= args.jobs <= 10000 or not 40 <= args.width <= 400 or not 15 <= args.height <= 160:
        parser.error('Invalid fixture size, terminal dimensions or sampling interval')
    if args.child:
        return child(args)
    report = run(args)
    print(json.dumps({'output': str(args.output), 'cpu_percent': report['cpu_mean_percent_one_core'],
                      'rss_peak_mib': report['summary']['rss_bytes']['max'] / (1 << 20),
                      'complete': report['complete']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
