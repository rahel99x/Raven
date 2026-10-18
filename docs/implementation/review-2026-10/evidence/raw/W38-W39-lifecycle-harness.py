#!/usr/bin/env python3
"""Bounded lifecycle/resource qualification; simulated scheduler, actual services.

No historical version, live scheduler, remote host or source-shipping efficacy is
implied. Compare the same actual Store/Sampler with/without optional publication.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
import hashlib
import json
import os
from pathlib import Path
import pwd
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time


def emit(value):
    print(json.dumps(value, sort_keys=True, separators=(',', ':')), flush=True)


def stable(snapshot):
    return {name: snapshot.get(name, []) for name in ('jobs', 'finished')}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def fixture_backend():
    from raven import cli
    original = cli.FakeBackend
    class Fixed(original):
        instances = []
        def __init__(self, user='alex'):
            super().__init__(user, t0=1792279000., speed=0.)
            # The existing demo retains an unbounded debugging command list.
            # Bound fixture-only instrumentation so it cannot imitate a leak.
            self.calls = deque(maxlen=64)
            self.counts = Counter()
            self.day = '2026-10-17'
            self.instances.append(self)
        def run(self, command, timeout=8):
            self.counts[Path(command[0]).name] += 1
            return super().run(command, timeout)
    cli.FakeBackend = Fixed
    return Fixed


def child_sampler(args):
    from raven import session_service as S
    fixed = fixture_backend()
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    original = S.observation
    samples = []
    def observation(store, **kwargs):
        value = original(store, **kwargs)
        snapshot = stable(value)
        if snapshot['jobs'] and snapshot['finished']:
            samples.append(digest(snapshot))
            emit({'kind': 'observation', 'at': time.monotonic(), 'sha256': samples[-1],
                  'jobs': len(snapshot['jobs']), 'finished': len(snapshot['finished'])})
        return value
    S.observation = observation
    started = time.monotonic()
    if args.child == 'daemon':
        S.serve(args.directory, config=args.config, demo=True, cancel=stop)
    else:
        session, _ = S._managed_session(Path(args.directory), args.config, demo=True)
        try:
            session.sampler.start()
            while not stop.is_set():
                observation(session.store)
                stop.wait(1)
        finally:
            session.close()
    counts = Counter()
    for instance in fixed.instances:
        counts.update(instance.counts)
    emit({'kind': 'final', 'mode': args.child, 'wall_seconds': time.monotonic()-started,
          'source_calls': dict(counts), 'observation_hashes': sorted(set(samples)),
          'source_instances': len(fixed.instances), 'threads_at_return': len(threading.enumerate())})


def shared_fixture(uid, login):
    from raven.shared_host_service import POLICY
    attempts = [{'cluster': 'fixture', 'job_id': str(1000+index), 'submit_time': 1792278000,
                 'start_time': 1792278100, 'restart_count': 0} for index in range(64)]
    projects = {name: [attempt for index, attempt in enumerate(attempts) if index % 2 == parity]
                for parity, name in enumerate(('ProjectA', 'ProjectB'))}
    policy = {'schema': POLICY, 'id': 'bounded-lifecycle-fixture', 'socket_gid': os.getgid(),
              'interval_seconds': 5, 'max_records': 1000,
              'principals': {str(uid): {'user': login, 'accounts': [], 'projects': ['ProjectA'], 'site': False}},
              'project_memberships': projects}
    rows = [{'id': item['job_id'], 'name': 'fixture-'+item['job_id'], 'cluster': 'fixture',
             'user': 'another-fixture-principal', 'account': 'LabX', 'state': 'RUNNING',
             '_attempt': item, 'cpus': 2} for item in attempts]
    return policy, rows


def child_shared(args):
    from raven import shared_host_service as H
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    root = Path(args.directory)
    root.parent.mkdir(exist_ok=True)
    policy_path = root.parent/'shared-policy.json'
    policy, rows = shared_fixture(os.getuid(), pwd.getpwuid(os.getuid()).pw_name)
    def save():
        temporary = policy_path.with_suffix('.pending')
        temporary.write_text(json.dumps(policy))
        temporary.chmod(0o600)
        os.replace(temporary, policy_path)
    save()
    server = H.SharedServer(root, policy_path)
    published = changes = 0
    largest_cache = largest_metadata = largest_clients = largest_pending = 0
    next_publish = next_policy = 0.
    started = time.monotonic()
    try:
        while not stop.is_set():
            now = time.monotonic()
            if now >= next_policy:
                policy['principals'][str(os.getuid())]['projects'] = [('ProjectA', 'ProjectB')[changes % 2]]
                save()
                server.reload_policy()
                changes += 1
                next_policy = now + 3
            if now >= next_publish:
                # Source replacement and project-only visibility use the real
                # private publication/cached projection path on every cycle.
                published += 1
                value = {'jobs': [dict(row, elapsed=str(published)) for row in rows], 'finished': [],
                         'queue_observed_at': time.time(), 'accounting_observed_at': time.time(),
                         'source_errors': {'jobs': '', 'finished': ''}}
                server.publish_source(value)
                next_publish = now + .1
            server.poll(.02)
            largest_cache = max(largest_cache, server._cache_bytes)
            largest_metadata = max(largest_metadata, len(server._view_metadata))
            largest_clients = max(largest_clients, len(server.clients))
            largest_pending = max(largest_pending, sum(client['queued'] for client in server.clients.values()))
    finally:
        server.close()
    emit({'kind': 'final', 'mode': 'shared', 'wall_seconds': time.monotonic()-started,
          'publications': published, 'policy_cycles': changes, 'max_view_cache_bytes': largest_cache,
          'max_replay_metadata_entries': largest_metadata, 'max_clients': largest_clients,
          'max_pending_bytes': largest_pending, 'metrics': server.metrics})


def process_children(pid):
    # Some managed kernels omit /proc/PID/task/PID/children. Read PPIDs from
    # the ordinary proc stat contract instead; no signals or adoption occur.
    children = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            values = (entry/'stat').read_text().rsplit(')', 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(values[1]) == pid:
            children.append(entry.name)
    return sorted(children)


def proc_sample(pid):
    root = Path('/proc')/str(pid)
    fields = (root/'stat').read_text().rsplit(')', 1)[1].split()
    status = dict(line.split(':', 1) for line in (root/'status').read_text().splitlines())
    io = dict(line.split(':', 1) for line in (root/'io').read_text().splitlines())
    return {'at': time.monotonic(), 'cpu_seconds': (int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK'),
            'rss_kib': int(status['VmRSS'].split()[0]), 'hwm_kib': int(status['VmHWM'].split()[0]),
            'threads': int(status['Threads']), 'fds': len(list((root/'fd').iterdir())),
            'children': process_children(pid), 'children_probe': 'proc-stat-ppid',
            'read_bytes': int(io['read_bytes']), 'write_bytes': int(io['write_bytes']),
            'rchar': int(io['rchar']), 'wchar': int(io['wchar'])}


def measure(args, mode, directory, config, seconds, *, prior=None):
    from raven import session_service as S, shared_host_service as H
    command = [sys.executable, str(Path(__file__).resolve()), '--repo', str(args.repo), '--child', mode,
               '--directory', str(directory), '--config', str(config)]
    started = time.monotonic()
    log = directory.parent/(mode+'-stderr.txt')
    messages, samples, latencies, hashes, generations = [], [], [], set(), set()
    initial = last_response = ready_sample = None
    read_errors = []
    prior_session = prior.session if prior is not None else None
    queries = failures = explicit_reconnects = 0
    client = prior
    decoder = b''
    with log.open('wb') as stderr:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr, cwd=str(args.repo))
        os.set_blocking(process.stdout.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        next_query = next_sample = time.monotonic()
        first_ready = None
        try:
            while True:
                now = time.monotonic()
                if first_ready is not None and now-first_ready >= seconds:
                    break
                if now-started > seconds+15:
                    raise TimeoutError('Service fixture never became ready')
                if process.poll() is not None:
                    raise RuntimeError('Lifecycle child exited: '+log.read_text()[-4000:])
                if now >= next_sample:
                    samples.append(proc_sample(process.pid))
                    next_sample = now+1
                if mode != 'baseline' and now >= next_query and (directory/S.SOCKET).exists():
                    if client is None:
                        client = H.SharedClient(directory, server_uid=os.getuid(), timeout=1) if mode == 'shared' else S.Client(directory, timeout=1)
                    before = time.monotonic()
                    identity = client.session, client.epoch, client.cursor
                    try:
                        response = client.query()
                    except (OSError, ValueError, RuntimeError) as exc:
                        failures += 1
                        if len(read_errors) < 128:
                            read_errors.append({'at_seconds': time.monotonic()-started, 'type': type(exc).__name__, 'error': str(exc), 'retained_cursor': client.cursor})
                        assert (client.session, client.epoch, client.cursor) == identity
                        # A shared-policy generation changes the authority
                        # session deliberately. Recreate this read-only client.
                        if mode == 'shared':
                            client = None
                            explicit_reconnects += 1
                        elif first_ready is not None:
                            raise
                    else:
                        latencies.append(time.monotonic()-before)
                        queries += 1
                        if response['snapshot'].get('jobs') and (mode == 'shared' or response['snapshot'].get('finished')):
                            if first_ready is None:
                                first_ready = time.monotonic()
                                ready_sample = proc_sample(process.pid)
                                initial = response
                            if last_response is not None and response['session'] == last_response['session'] and response['epoch'] == last_response['epoch']:
                                assert response['sequence'] >= last_response['sequence']
                            last_response = response
                            if mode == 'shared':
                                view = response['snapshot']
                                grant, = view['authorization']['projects']
                                parity = ('ProjectA', 'ProjectB').index(grant)
                                expected = {str(1000+index) for index in range(64) if index % 2 == parity}
                                assert {row['id'] for row in view['jobs']} == expected
                                assert len(view['jobs']) == 32 and not view['finished']
                                assert all(row['projects'] == [grant] and row['name'] == 'fixture-'+row['id'] and '_attempt' not in row for row in view['jobs'])
                                generations.add(view['policy_generation'])
                            else:
                                hashes.add(digest(stable(response['snapshot'])))
                    next_query = now + (.4 if mode == 'shared' else .1)
                for key, _ in selector.select(.02):
                    block = os.read(key.fd, 65536)
                    decoder += block
                    while b'\n' in decoder:
                        line, decoder = decoder.split(b'\n', 1)
                        message = json.loads(line)
                        messages.append(message)
                        if mode == 'baseline' and message['kind'] == 'observation':
                            hashes.add(message['sha256'])
                            if first_ready is None:
                                first_ready = time.monotonic()
                                ready_sample = proc_sample(process.pid)
            final_sample = proc_sample(process.pid)
            process.terminate()
            process.wait(timeout=8)
            assert process.returncode == 0
            # Drain final observations after the finite owned child has joined.
            os.set_blocking(process.stdout.fileno(), True)
            decoder += process.stdout.read()
            messages.extend(json.loads(line) for line in decoder.splitlines() if line)
            assert not Path('/proc', str(process.pid)).exists()
            assert not (directory/S.SOCKET).exists()
        finally:
            selector.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            process.stdout.close()
    final, = [message for message in messages if message['kind'] == 'final']
    measured = [sample for sample in samples if sample['at'] >= first_ready+3]
    assert len(measured) >= 2
    assert all(not sample['children'] for sample in measured)
    rss_growth = measured[-1]['rss_kib']-measured[0]['rss_kib']
    assert max(sample['fds'] for sample in measured)-min(sample['fds'] for sample in measured) <= 4
    assert max(sample['threads'] for sample in measured)-min(sample['threads'] for sample in measured) <= 2
    assert rss_growth <= 4096
    elapsed = final_sample['at']-measured[0]['at']
    result = {'mode': mode, 'child_command': command, 'cold_ready_seconds': first_ready-started,
              'measurement_seconds': elapsed, 'measurement_cpu_seconds': final_sample['cpu_seconds']-measured[0]['cpu_seconds'],
              'rss_first_kib': measured[0]['rss_kib'], 'rss_last_kib': measured[-1]['rss_kib'],
              'rss_growth_kib': rss_growth, 'os_high_water_rss_kib': final_sample['hwm_kib'],
              'fd_min_max': [min(s['fds'] for s in measured), max(s['fds'] for s in measured)],
              'thread_min_max': [min(s['threads'] for s in measured), max(s['threads'] for s in measured)],
              'read_bytes': final_sample['read_bytes']-measured[0]['read_bytes'],
              'write_bytes': final_sample['write_bytes']-measured[0]['write_bytes'],
              'rchar': final_sample['rchar']-measured[0]['rchar'], 'wchar': final_sample['wchar']-measured[0]['wchar'],
              'queries': queries, 'read_failures': failures, 'read_errors': read_errors,
              'explicit_authority_reconnects': explicit_reconnects,
              'cold_ready_process': ready_sample,
              'warmup_seconds': measured[0]['at']-ready_sample['at'],
              'warmup_cpu_seconds': measured[0]['cpu_seconds']-ready_sample['cpu_seconds'],
              'warmup_rss_growth_kib': measured[0]['rss_kib']-ready_sample['rss_kib'],
              'warmup_end_process': measured[0],
              'query_mean_ms': 1000*sum(latencies)/len(latencies) if latencies else None,
              'query_max_ms': 1000*max(latencies) if latencies else None, 'stable_projection_hashes': sorted(hashes),
              'observed_policy_generations': sorted(generations), 'final': final, 'samples': samples,
              'normal_exit': process.returncode, 'owned_process_joined': True, 'owned_socket_removed': True}
    if prior is not None:
        result['restart_same_session'] = initial['session'] == prior_session
        result['restart_gap'] = initial['gap']
        result['restart_new_epoch'] = initial['epoch'] != args.prior_epoch
        assert result['restart_same_session'] and result['restart_new_epoch'] and result['restart_gap']
    return result, client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--child', choices=('baseline', 'daemon', 'shared'))
    parser.add_argument('--directory')
    parser.add_argument('--config')
    args = parser.parse_args()
    sys.path.insert(0, str(args.repo))
    if args.child:
        (child_shared if args.child == 'shared' else child_sampler)(args)
        return
    if not 8 <= args.seconds <= 60 or args.output is None:
        parser.error('Use an output and a bounded 8..60-second case duration')
    from datetime import datetime, timezone
    from raven import session_service as S
    started = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix='rv-soak-') as temporary:
        root = Path(temporary)
        config = root/'config.json'
        config.write_text(json.dumps({'schema': 'raven.config/v1', 'telemetry': {'preset': 'ultra'}}))
        config.chmod(0o600)
        cases = []
        def retain(case):
            cases.append(case)
            args.output.with_suffix('.partial.json').write_text(json.dumps({'schema': 'raven.service-lifecycle-cost/v1', 'status': 'partial', 'started': started, 'cases': cases}, indent=2)+'\n')
        emit({'stage': 'baseline', 'seconds': args.seconds})
        baseline, _ = measure(args, 'baseline', root/'baseline', config, args.seconds)
        retain(baseline)
        emit({'stage': 'daemon', 'seconds': args.seconds})
        daemon, client = measure(args, 'daemon', root/'session', config, args.seconds)
        retain(daemon)
        assert baseline['stable_projection_hashes'] == daemon['stable_projection_hashes'] and len(daemon['stable_projection_hashes']) == 1
        identity = client.session, client.epoch, client.cursor
        try:
            client.query()
        except OSError:
            pass
        else:
            raise AssertionError('Stopped daemon remained reachable')
        assert (client.session, client.epoch, client.cursor) == identity
        args.prior_epoch = client.epoch
        emit({'stage': 'restart', 'seconds': 6})
        restarted, _ = measure(args, 'daemon', root/'session', config, 6, prior=client)
        retain(restarted)
        emit({'stage': 'shared', 'seconds': args.seconds})
        shared, _ = measure(args, 'shared', root/'shared', config, args.seconds)
        retain(shared)
        assert len(shared['observed_policy_generations']) == 2
        assert shared['explicit_authority_reconnects'] >= 2
        assert shared['final']['publications'] >= int(args.seconds*5)
        assert shared['final']['max_view_cache_bytes'] <= 8 << 20
        assert shared['final']['max_pending_bytes'] <= 8 << 20
    files = ['raven/session_service.py', 'raven/session_contract.py', 'raven/shared_host_service.py',
             'raven/cli.py', 'raven/model.py', 'raven/sampler.py', 'raven/slurm.py', 'raven/state_ownership.py']
    result = {'schema': 'raven.service-lifecycle-cost/v1', 'started': started,
              'completed': datetime.now(timezone.utc).isoformat(), 'cases': cases,
              'source_hashes': {name: hashlib.sha256((args.repo/name).read_bytes()).hexdigest() for name in files},
              'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'limits': ['Explicit ultra telemetry fixture; fixed simulated scheduler responses and bounded demo instrumentation.',
                         'Actual current Store/Sampler baseline versus optional daemon; neither a historical release nor a full terminal UI comparator.',
                         'Exact jobs/finished row hashes; clock/health/publication timestamps intentionally retain real time and are not claimed identical.',
                         'Short bounded lifecycle soak; no long-term leak guarantee, live Slurm, SSH efficacy or production-site authorization claim.',
                         'W39 actual same-UID sockets/project-policy churn with synthetic scheduler records; independent two-UID qualification is separate.',
                         'OS process RSS/CPU/fd/thread/I/O samples, not Python allocations or sampled process-tree memory.']}
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    emit({'output': str(args.output), 'started': started, 'completed': result['completed'],
          'cases': [{'mode': case['mode'], 'cpu_seconds': case['measurement_cpu_seconds'],
                     'rss_growth_kib': case['rss_growth_kib'], 'fds': case['fd_min_max'],
                     'threads': case['thread_min_max'], 'queries': case['queries']} for case in cases]})


if __name__ == '__main__':
    main()
