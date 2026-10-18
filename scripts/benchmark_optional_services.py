"""Controlled offline service fan-out and optional WASI cost fixtures.

Logical per-user handlers are injected, not authenticated peers. Separate real
SO_PEERCRED qualification establishes peer behavior. No scheduler calls occur.
Run only in a coordinated quiet window; child OS RSS is not Python allocation.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import resource
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fanout(mode, count):
    from raven import shared_host_service as H, session_contract as C
    from raven import slurm_json as J
    parse_queue = J.queue
    # The source fixture represents one observation, including its derived
    # elapsed fields. Freeze only parser observation time, never timing clocks.
    J.queue = lambda value, report: parse_queue(value, report, observed=1792279100)
    corpus = ROOT/'tests/fixtures/slurm/recorded/26.05.5/running-squeue-json.stdout.txt'
    template = json.loads(corpus.read_text())
    first = template['jobs'][0]
    template['jobs'] = [{**first, 'job_id': index+10000, 'user_id': index+20000,
                         'user_name': 'fixture-user-'+str(index)} for index in range(count)]
    queue_raw = json.dumps(template)
    accounting_raw = json.dumps({**template, 'jobs': []})
    class Fixture:
        calls = 0
        def run(self, argv, timeout):
            self.calls += 1
            return (queue_raw if argv[0] == 'squeue' else accounting_raw, '')
    backend = Fixture()
    empty = {'jobs': [], 'finished': [], 'queue_observed_at': 0., 'accounting_observed_at': 0., 'source_errors': {'jobs': '', 'finished': ''}}
    with tempfile.TemporaryDirectory(prefix='rv-cost-') as temporary:
        path = Path(temporary)/'policy.json'
        policy = {'schema': H.POLICY, 'id': 'benchmark', 'socket_gid': os.getgid(), 'interval_seconds': 60, 'max_records': 10000,
                  'principals': {str(os.getuid()): {'user': pwd.getpwuid(os.getuid()).pw_name, 'accounts': [], 'site': False}}}
        path.write_text(json.dumps(policy))
        server = H.SharedServer(Path(temporary)/'service', path) if mode == 'shared' else None
        # Deliberate logical-handler fixture, never a security/authentication
        # qualification. Production policies still require NSS and SO_PEERCRED.
        grants = {str(index+20000): {'user': 'fixture-user-'+str(index), 'accounts': [], 'site': False} for index in range(count)}
        if server:
            server.policy['principals'] = grants
        wall, cpu = time.perf_counter(), time.process_time()
        reads = 0
        fidelity = hashlib.sha256()
        try:
            if server:
                server.publish_source(H.collect(backend, empty, max_records=10000))
            for index in range(count):
                grant = grants[str(index+20000)]
                if server:
                    # Advance the logical admission clock at32reads/s; actual
                    # time-series arrival latency is not measured by this case.
                    client = {'uid': index+20000, 'tokens': 4., 'rate_updated': time.monotonic()}
                    server.admit_read(client, time.monotonic()+index/32)
                    result = server.query(client, C.request('snapshot'))['snapshot']['jobs']
                else:
                    observed = H.collect(backend, empty, max_records=10000)
                    result = H.project(observed['jobs'], grant)
                assert len(result) == 1 and result[0]['user'] == grant['user']
                encoded = C.encoded({'jobs': result})
                reads += len(encoded)
                fidelity.update(encoded)
            result = {'case': mode, 'clients': count, 'fixture': 'logical independent users, recorded structured source cloned per user',
                      'source_reads': backend.calls, 'wall_seconds': time.perf_counter()-wall, 'cpu_seconds': time.process_time()-cpu,
                      'process_max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      'serialized_job_bytes': reads, 'source_input_bytes': len(queue_raw)+len(accounting_raw),
                      'authorized_rows_sha256': fidelity.hexdigest(),
                      'cache_bytes': server._cache_bytes if server else 0, 'live_scheduler': False,
                      'actual_peer_authentication_measured': False, 'arrival_latency_measured': False}
        finally:
            if server:
                server.close()
    return result


def wasi(mode, module_path):
    values = [float((index % 101)-50) for index in range(10000)]
    if mode == 'wasi':
        from raven import wasi_analysis as W
        if not module_path:
            raise ValueError('Pass a precompiled --module fixture; do not inflate parent RSS by importing the SDK')
        binary = Path(module_path).read_bytes()
        manifest = {'schema': 'raven.wasi-analysis/v1', 'module': 'fixture.wasm', 'sha256': hashlib.sha256(binary).hexdigest(), 'abi': W.ABI,
                    'limits': {'memory_bytes': 1 << 20, 'fuel': 1000000, 'wall_seconds': 5, 'output_bytes': 1024}}
    wall, cpu = time.perf_counter(), time.process_time()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    for _ in range(10):
        if mode == 'wasi':
            result = W.execute(manifest, binary, {'schema': 'raven.f64-series/v1', 'values': values})['summary']
        else:
            total = 0.
            for value in values:
                total += value
            result = {'count': len(values), 'sum': total, 'minimum': min(values), 'maximum': max(values)}
        assert result == {'count': 10000, 'sum': -50., 'minimum': -50., 'maximum': 50.}
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {'case': mode, 'samples': len(values), 'repetitions': 10, 'wall_seconds': time.perf_counter()-wall,
            'parent_cpu_seconds': time.process_time()-cpu,
            'joined_child_cpu_seconds': after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,
            'process_max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'max_individual_child_rss_kib': after.ru_maxrss, 'aggregate_tree_rss_measured': False,
            'fidelity': 'identical count/sum/extrema for the same ordered finite series'}


def socket_burst(count):
    from raven import shared_host_service as H
    with tempfile.TemporaryDirectory(prefix='rv-burst-') as temporary:
        path = Path(temporary)/'policy.json'
        value = {'schema': H.POLICY, 'id': 'socket-burst', 'socket_gid': os.getgid(), 'interval_seconds': 60, 'max_records': 1000,
                 'principals': {str(os.getuid()): {'user': pwd.getpwuid(os.getuid()).pw_name, 'accounts': [], 'site': False}}}
        path.write_text(json.dumps(value))
        server = H.SharedServer(Path(temporary)/'service', path)
        stop = threading.Event()
        def pump():
            while not stop.is_set():
                server.poll(.01)
        thread = threading.Thread(target=pump)
        thread.start()
        accepted = refused = 0
        wall, cpu = time.perf_counter(), time.process_time()
        try:
            for _ in range(count):
                try:
                    H.SharedClient(server.root, server_uid=os.getuid()).query()
                    accepted += 1
                except ValueError as exc:
                    assert 'quota' in str(exc), str(exc)
                    refused += 1
        finally:
            stop.set()
            thread.join()
            server.close()
        duration = time.perf_counter()-wall
        assert accepted <= 4+4*duration
        return {'case': 'socket-burst', 'clients': count, 'accepted': accepted, 'quota_refused': refused,
                'same_real_unix_uid': True, 'connections': 'sequential one-shot clients in an immediate burst',
                'source_reads_triggered_by_clients': 0, 'wall_seconds': duration,
                'combined_service_and_driver_cpu_seconds': time.process_time()-cpu,
                'combined_process_max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=['independent', 'shared', 'socket-burst', 'trusted', 'wasi'])
    parser.add_argument('--clients', type=int, choices=[1, 10, 200], default=1)
    parser.add_argument('--output')
    parser.add_argument('--module')
    args = parser.parse_args()
    if args.case:
        print(json.dumps(wasi(args.case, args.module) if args.case in {'trusted', 'wasi'} else socket_burst(args.clients) if args.case == 'socket-burst' else fanout(args.case, args.clients)))
        return
    results = []
    started = datetime.now(timezone.utc).isoformat()
    for count in (1, 10, 200):
        for mode in ('independent', 'shared', 'socket-burst'):
            raw = subprocess.check_output([sys.executable, __file__, '--case', mode, '--clients', str(count)], timeout=20)
            results.append(json.loads(raw))
        assert results[-3]['authorized_rows_sha256'] == results[-2]['authorized_rows_sha256']
    import wasmtime
    with tempfile.TemporaryDirectory(prefix='rv-wasi-cost-') as temporary:
        path = Path(temporary)/'fixture.wasm'
        path.write_bytes(bytes(wasmtime.wat2wasm((ROOT/'examples/wasi/f64-summary.wat').read_text())))
        for mode in ('trusted', 'wasi'):
            results.append(json.loads(subprocess.check_output([sys.executable, __file__, '--case', mode, '--module', str(path)], timeout=20)))
    report = {'schema': 'raven.optional-service-cost/v1', 'python': sys.version, 'results': results,
              'started_at': started, 'finished_at': datetime.now(timezone.utc).isoformat(),
              'source_hashes': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in
                               [Path(__file__).resolve(), ROOT/'raven/shared_host_service.py', ROOT/'raven/session_contract.py',
                                ROOT/'raven/session_service.py', ROOT/'raven/wasi_analysis.py', ROOT/'raven/_wasi_runner.py',
                                ROOT/'examples/wasi/f64-summary.wat']},
              'limits': ['offline captured scheduler fixture', 'logical user handler fan-out, not real site/security/arrival qualification',
                         'OS process high-water RSS, not Python allocation or simultaneous process-tree RSS']}
    raw = json.dumps(report, indent=2)+'\n'
    if args.output:
        Path(args.output).write_text(raw)
    print(raw)


if __name__ == '__main__':
    main()
