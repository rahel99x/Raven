#!/usr/bin/env python3
"""Capture completed hosted installation checks without changing repository files."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

REPO = 'rahel99x/Raven'
EXPECTED_BASH_SHA = {
    '3.2': '26c99025b59e30779300b68adb764f824974d267a4d7cc1b347d14a2393f9fb4',
    '4.2': 'a27a1179ec9c0830c65c6aa5d7dab60f7ce1a2a608618570f96bfa72e95ab3d8',
}


def gh(*args):
    return subprocess.check_output(['gh', *args], text=True, timeout=120)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.run.isdecimal() or not re.fullmatch('[0-9a-f]{40}', args.head):
        parser.error('run must be decimal and head must be a full lowercase commit SHA')
    if args.output.exists():
        parser.error('output must be a new evidence directory')
    run = json.loads(gh('run', 'view', args.run, '--repo', REPO, '--json', 'headSha,jobs,url'))
    assert run['headSha'] == args.head, 'run belongs to a different head'
    expected = {'legacy-bash (3.2)', 'legacy-bash (4.2)', 'private-group-umask'}
    jobs = [job for job in run['jobs'] if job['name'] in expected]
    assert {job['name'] for job in jobs} == expected
    assert all(job['conclusion'] == 'success' for job in jobs), 'assigned jobs not all successful'
    args.output.mkdir(mode=0o700, parents=True)
    (args.output / 'run.json').write_text(json.dumps(run, indent=2) + '\n')
    for artifact, folder in [('legacy-bash-3.2', 'bash32'), ('legacy-bash-4.2', 'bash42'), ('umask002-updates', 'umask002')]:
        gh('run', 'download', args.run, '--repo', REPO, '--name', artifact, '--dir', str(args.output / folder))
    root = args.output / 'umask002'
    result = json.loads((root / 'result.json').read_text())
    assert result['schema'] == 'raven.update-qualification/v1' and result['passed']
    assert result['source_stable'] and result['pipx_backend'] == 'pip'
    assert len(result['checks']) == 33
    for check in result['checks']:
        assert check['exit'] == check['expected_exit'], check['label']
        assert not check['failure'] and not check['timed_out'] and check['reaped'], check['label']
        log = Path(check['log'])
        assert not log.is_absolute() and '..' not in log.parts
        assert digest(root / log) == check['sha256'], check['label']
    merge = json.loads(gh('api', f'repos/{REPO}/git/commits/' + result['source_commit']))
    head = json.loads(gh('api', f'repos/{REPO}/git/commits/' + args.head))
    assert merge['tree']['sha'] == head['tree']['sha'], 'qualification tree differs from PR head'
    summary = {
        'schema': 'raven.hosted-install-review/v1',
        'run_id': int(args.run), 'run_url': run['url'],
        'pull_request_head': args.head, 'status': 'assigned-jobs-passed',
        'jobs': [{key: job[key] for key in ('name', 'databaseId', 'url', 'conclusion', 'completedAt')} for job in jobs],
        'provenance': {'qualification_checkout': result['source_commit'], 'qualification_checkout_kind': 'GitHub pull-request synthetic merge',
                       'qualification_tree': merge['tree']['sha'], 'pull_request_head_tree': head['tree']['sha'], 'exact_tree_match': True},
        'umask002': {'passed': True, 'checks': 33, 'all_check_log_hashes_verified': True, 'pipx_backend': result['pipx_backend'],
                     'result': str(root / 'result.json'), 'result_sha256': digest(root / 'result.json'), 'limits': result['limits']},
        'bash': [], 'capture_driver': str(Path(__file__).resolve()), 'capture_driver_sha256': digest(Path(__file__)),
    }
    for version, folder in [('3.2', 'bash32'), ('4.2', 'bash42')]:
        root = args.output / folder
        report = json.loads((root / 'result.json').read_text())
        assert report['schema'] == 'raven.bash-qualification/v1' and report['passed']
        assert report['sha256'] == EXPECTED_BASH_SHA[version]
        assert all(command['exit'] == 0 and not command['failure'] and not command['timed_out'] for command in report['commands'])
        tests = (root / 'tests.log').read_text().strip().splitlines()[-1]
        assert re.fullmatch(r'41 passed in [0-9.]+s', tests), tests
        summary['bash'].append({'version': version, 'interpreter': report['interpreter'], 'passed': True, 'source_tarball_sha256': report['sha256'],
                                'tests': tests, 'result': str(root / 'result.json'), 'result_sha256': digest(root / 'result.json'), 'limits': report['limits']})
    path = args.output / 'summary.json'
    path.write_text(json.dumps(summary, indent=2) + '\n')
    print(path)
    print('Three hosted jobs passed; 41+41 Bash tests and 33 lifecycle checks. Qualification tree equals PR head.')


if __name__ == '__main__':
    main()
