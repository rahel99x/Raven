#!/usr/bin/env python3
"""Controlled current-code export/signing comparison; no historical-source claim.

Generated test keys/inputs and imports are outside export timing. Each mode uses
an isolated process and the same 1,000-record input. The legacy-identity mode
changes only the reviewed dev/inode encoding helper, not the rest of Raven.
Run in a coordinated quiet window. No scheduler or remote key service is used.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
IDENTITY = {'cluster': 'fixture', 'job_id': '123_4', 'attempt': '2026-01-01T00:00:00/2026-01-01T00:00:02'}
PRINCIPAL = 'generated-benchmark-key@example.invalid'


def child(root, mode, repetitions):
    from raven import evidence_signing as S, evidence_verify as V, operations as O, ops_platform as OPS
    if mode == 'legacy-identity':
        # Exact earlier helper, for this narrow representation comparator only.
        # Never weakens the worker wire or claims to replay a historical build.
        S._identity = lambda info: [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    source = root/'input.json'
    signed = mode != 'unsigned'
    times, verifies, sizes = [], [], []
    digests = None
    cpu = time.process_time()
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    for index in range(repetitions):
        destination = root/(mode+'-'+str(index))
        params = {'path': str(source), 'format': 'raven-evidence', **IDENTITY,
                  'destination': str(destination), 'export_format': 'workflow-run-ro-crate'}
        if signed:
            params.update(signing_key=str(root/'key'), signing_principal=PRINCIPAL)
        context = O.Context(scope={'cluster': 'fixture', 'user': 'benchmark'})
        began = time.perf_counter()
        review = O.run('research-portable', params, context)
        result = OPS.apply('research-portable', review['plan'], context)
        times.append(time.perf_counter()-began)
        assert result['data']['signed'] is signed
        if signed:
            began = time.perf_counter()
            verified = V.verify(destination, root/'allowed_signers', PRINCIPAL)
            verifies.append(time.perf_counter()-began)
            assert verified['verified'] and not verified['unsigned_extra_files']
        current = {name: hashlib.sha256((destination/name).read_bytes()).hexdigest()
                   for name in ('raven-evidence.json', 'ro-crate-metadata.json')}
        assert digests is None or digests == current
        digests = current
        assert (destination/'raven-evidence.json').read_bytes() == source.read_bytes()
        sizes.append(sum(path.stat().st_size for path in destination.iterdir() if path.is_file()))
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {'mode': mode, 'repetitions': repetitions, 'records': 1000,
            'export_mean_ms': statistics.mean(times)*1000, 'export_p50_ms': statistics.median(times)*1000,
            'verify_mean_ms': statistics.mean(verifies)*1000 if verifies else None,
            'cpu_seconds_including_verification_and_fidelity': time.process_time()-cpu,
            'joined_child_cpu_seconds': after.ru_utime+after.ru_stime-children.ru_utime-children.ru_stime,
            'process_max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'max_individual_child_rss_kib': after.ru_maxrss, 'output_bytes': sizes,
            'data_artifacts_sha256': digests}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--repetitions', type=int, default=5)
    parser.add_argument('--child', choices=('unsigned', 'legacy-identity', 'signed'))
    parser.add_argument('--root', type=Path)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 20:
        parser.error('Choose 1..20 repetitions')
    if args.child:
        if not args.root:
            parser.error('--child requires the generated --root fixture')
        print(json.dumps(child(args.root, args.child, args.repetitions)))
        return
    from raven import evidence as E, evidence_signing as S
    began = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix='rv-sign-cost-') as temporary:
        root = Path(temporary)
        value = E.receipt(IDENTITY, {'id': 'fixture', 'format': 'fixture-json', 'version': '1', 'sha256': 'a'*64},
            [{'id': 'm'+str(index), 'kind': 'metric', 'data': {'name': 'memory', 'value': index, 'unit': 'B'}} for index in range(1000)])
        (root/'input.json').write_bytes(E.canonical(value)+b'\n')
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(root/'key')], check=True, timeout=10)
        (root/'allowed_signers').write_text(PRINCIPAL+' namespaces="'+S.NAMESPACE+'" '+(root/'key.pub').read_text())
        results = []
        for mode in ('unsigned', 'legacy-identity', 'signed'):
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--child', mode, '--root', str(root),
                                        '--repetitions', str(args.repetitions)], check=True, capture_output=True, text=True, timeout=60)
            results.append(json.loads(completed.stdout))
        assert all(row['data_artifacts_sha256'] == results[0]['data_artifacts_sha256'] for row in results)
        assert results[1]['output_bytes'] == results[2]['output_bytes']
    files = ('scripts/benchmark_signed_evidence.py', 'raven/evidence_signing.py', 'raven/evidence_verify.py',
             'raven/ops_platform.py', 'raven/evidence.py', 'raven/evidence_formats.py', 'raven/science_files.py')
    result = {'schema': 'raven.signing-cost/v1', 'started': began, 'completed': datetime.now(timezone.utc).isoformat(),
              'results': results, 'source_hashes': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in files},
              'limits': ['Current-code unsigned and signed comparison; legacy mode changes only the identity helper, not a historical source checkout',
                         'Synthetic 1000-metric fixture and generated test key; no live scheduler, GPU or remote key service',
                         'One sequential run; no universal overhead or statistically significant helper speed claim',
                         'OS process/individual child high-water RSS, not Python allocation or simultaneous process-tree memory',
                         'Imports/fixture/key generation excluded from export time; parent RSS includes imports, child CPU includes verification']}
    if args.output:
        args.output.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
