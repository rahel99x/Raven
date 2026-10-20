"""Append hosted qualification only after Python CI and main merge are proved.

No snapshot without --ready and a completed parent contract; never commit/push.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import os

SUPPORT = Path(__file__).with_name('prepare-delivery-review-evidence.py')
spec = importlib.util.spec_from_file_location('delivery_archive_support', SUPPORT)
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)
ROOT = Path('/workspace/Raven-review-evidence')
REPO = Path('/workspace/Raven')
PARENT = '3a3288c8bb057cfdf8a050489ed13835e2940708'
HEAD = '954213c4aa3ef40dd3716e556d53fa39e5d23644'
MERGE = 'b3cc407f4a919bc29157c73778469106237096e9'
TREE = '36e9dbbb51c4d0a06fd1db39395ab36294b0133e'
OLD_HEAD, OLD_MERGE, OLD_TREE = HEAD, MERGE, TREE
PREFIX = 'hosted-954213c'


def git(*args, repo=REPO):
    return subprocess.check_output(['git', *args], cwd=repo)


def bundle(paths):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for name, path in sorted(paths.items()):
            S.safe_name(name)
            raw = S.stable_read(path)
            item = tarfile.TarInfo(name)
            item.size = len(raw)
            item.mode = 0o600
            item.mtime = 0
            archive.addfile(item, io.BytesIO(raw))
    return S.compressed(output.getvalue())


def main():
    global HEAD, MERGE, TREE, PREFIX
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--ready', action='store_true')
    args = parser.parse_args()
    assert args.ready, 'Hold until parent releases all Python CI green and final merge proof'
    contract_raw = S.stable_read(args.inputs)
    contract = json.loads(contract_raw)
    assert contract['schema'] == 'raven.hosted-evidence-inputs/v1' and contract['ready'] is True
    assert contract['publication_authorization'] == 'parent reviewed all Python CI green and main merge proof; local append only'
    proof = contract['identity_proof']
    HEAD, MERGE, TREE = proof['pull_request_head'], proof['tested_merge'], proof['head_tree']
    PREFIX = 'hosted-' + HEAD[:7]
    assert len(HEAD) == len(MERGE) == 40 and len(TREE) == 40 and all(c in '0123456789abcdef' for value in (HEAD, MERGE, TREE) for c in value)
    assert git('rev-parse', 'HEAD', repo=ROOT).decode().strip() == PARENT
    assert not git('status', '--porcelain', repo=ROOT).strip()
    assert git('rev-parse', HEAD + '^{tree}').decode().strip() == TREE
    previous = {}
    for name in ['artifact-manifest.json', 'artifact-manifest-v2.json', 'artifact-manifest-v3.json']:
        raw = S.stable_read(ROOT / name)
        assert raw == git('show', PARENT + ':' + name, repo=ROOT)
        previous[name] = S.sha(raw)
        for path, item in json.loads(raw)['files'].items():
            data = S.stable_read(ROOT / path)
            assert len(data) == item['bytes'] and S.sha(data) == item['sha256']
    assert proof['pull_request_head'] == HEAD and proof['tested_merge'] == MERGE
    assert proof['head_tree'] == proof['tested_merge_tree'] == proof['main_tree'] == TREE
    assert proof['event'] == 'pull_request' and proof['base_branch'] == 'main'
    assert git('rev-parse', proof['main_commit'] + '^{tree}').decode().strip() == TREE
    merged = json.loads(S.stable_read(proof['main_merge_receipt']))
    if merged.get('state') == 'MERGED':
        assert merged['headRefOid'] == HEAD and merged['baseRefName'] == 'main' and merged['mergeCommit']['oid'] == proof['main_commit']
    else:
        assert merged['merged'] is True and merged['head']['sha'] == HEAD and merged['base']['ref'] == 'main' and merged['merge_commit_sha'] == proof['main_commit']
    assert git('rev-parse', 'refs/remotes/origin/main^{tree}').decode().strip() == TREE
    subprocess.run(['git', 'merge-base', '--is-ancestor', proof['main_commit'], 'refs/remotes/origin/main'], cwd=REPO, check=True)
    # Prefer independent local object verification. When GitHub's synthetic
    # object is unavailable, retain the exact commit-tree API receipt instead.
    merge_object = subprocess.run(['git', 'cat-file', '-e', MERGE + '^{commit}'], cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if merge_object.returncode == 0:
        assert git('rev-parse', MERGE + '^{tree}').decode().strip() == TREE
    else:
        receipt = json.loads(S.stable_read(proof['tested_merge_commit_api_receipt']))
        assert receipt['sha'] == MERGE and receipt['tree']['sha'] == TREE
    python_ci = json.loads(S.stable_read(contract['python_ci_receipt']))
    assert python_ci['run_id'] == contract['python_run_id'] and python_ci['conclusion'] == 'success'
    assert python_ci['pull_request_head'] == HEAD and python_ci['tested_merge'] == MERGE
    assert python_ci['event'] == 'pull_request'
    python_jobs = python_ci['python_jobs']
    assert len(python_jobs) == 5 and {job['python'] for job in python_jobs} == {'3.10', '3.11', '3.12', '3.13', '3.14'}
    assert all(job['conclusion'] == 'success' and job['status'] == 'completed' for job in python_jobs)
    for job in python_jobs:
        assert job['full_suite']['passed'] > 19000 and job['full_suite'].get('failed', 0) == job['full_suite'].get('errors', 0) == 0
        assert job['full_suite']['source_log_sha256'] and job['url'].startswith('https://github.com/rahel99x/Raven/')
    payloads, metadata = {}, {}
    def add(relative, raw, role, source, *, original=None):
        S.safe_name(relative)
        name = PREFIX + '/' + relative
        assert name not in payloads
        payloads[name] = raw
        metadata[name] = {'role': role, 'source': source}
        if original is not None:
            metadata[name].update(source_bytes=len(original), source_sha256=S.sha(original), encoding='gzip, empty filename, mtime0')
    # Retain complete Python job logs and raw identity receipts automatically,
    # so a compact accepted summary cannot accidentally replace its evidence.
    add('python/receipt.json', S.stable_read(contract['python_ci_receipt']), 'Completed final Python workflow summary; five interpreter invocations overlap', contract['python_ci_receipt'])
    run_path = Path(python_ci['raw_run_receipt'])
    run_raw = S.stable_read(run_path)
    assert S.sha(run_raw) == python_ci['raw_run_receipt_sha256']
    run = json.loads(run_raw)
    assert run['id'] == contract['python_run_id'] and run['head_sha'] == HEAD and run['event'] == 'pull_request'
    assert run['status'] == 'completed' and run['conclusion'] == 'success'
    add('python/run-receipt.json', run_raw, 'Unmodified GitHub completed successful workflow API receipt', str(run_path))
    pages = sorted(run_path.parent.glob('jobs-page-*.json'))
    assert pages
    listed = []
    for page in pages:
        raw = S.stable_read(page)
        value = json.loads(raw)
        listed.extend(value['jobs'])
        add('python/' + page.name, raw, 'Unmodified complete workflow-attempt jobs API page', str(page))
    assert len(listed) == len({job['id'] for job in listed}) == value['total_count']
    for job in python_jobs:
        assert job['id'] in {item['id'] for item in listed}
        full_path = Path(job['full_log'])
        full_raw = S.stable_read(full_path)
        assert len(full_raw) == job['full_suite']['source_log_bytes']
        assert S.sha(full_raw) == job['full_suite']['source_log_sha256']
        assert job['full_suite']['tested_merge'] == MERGE
        prefix = 'python/python-' + job['python']
        add(prefix + '.full.log.gz', S.compressed(full_raw), 'Complete unmodified hosted job log, including every test and subsequent step; gzip is lossless', str(full_path), original=full_raw)
        job_path = Path(job['raw_job_receipt'])
        job_raw = S.stable_read(job_path)
        assert S.sha(job_raw) == job['raw_job_receipt_sha256']
        source_job = json.loads(job_raw)
        assert source_job['id'] == job['id'] and source_job['run_id'] == contract['python_run_id'] and source_job['conclusion'] == 'success'
        assert source_job['status'] == 'completed' and source_job['html_url'] == job['url']
        add(prefix + '.job-receipt.json', job_raw, 'Unmodified complete successful interpreter job API receipt', str(job_path))
        excerpt_path = Path(job['excerpt_receipt'])
        excerpt_raw = S.stable_read(excerpt_path)
        excerpt = json.loads(excerpt_raw)
        assert excerpt['is_complete_log'] is False and excerpt['source_log_sha256'] == S.sha(full_raw)
        assert excerpt['job_id'] == job['id'] and excerpt['url'] == job['url']
        selected = S.stable_read(excerpt['excerpt'])
        assert S.sha(selected) == excerpt['excerpt_sha256']
        lines = full_raw.splitlines(keepends=True)
        assert selected == b''.join(lines[number - 1] for number in excerpt['source_line_numbers'])
        add(prefix + '.excerpt.json', excerpt_raw, 'Explicit subset receipt bound to original complete log, exact selected source lines and job URL', str(excerpt_path))
        add(prefix + '.excerpt.log', selected, 'Selected exact original log rows; not a complete job log', excerpt['excerpt'])
        step = S.stable_read(excerpt['full_suite_step_log'])
        assert S.sha(step) == job['full_suite']['step_log_sha256']
        first, last = job['full_suite']['step_source_lines']
        assert step == b''.join(lines[first - 1:last])
        add(prefix + '.full-suite-step.log.gz', S.compressed(step), 'Complete exact full-regression step within the retained whole-job log; same invocation, not additional tests', excerpt['full_suite_step_log'], original=step)
    for key, expected in [('pull_request_head_commit_api_receipt', HEAD), ('tested_merge_commit_api_receipt', MERGE)]:
        path = Path(python_ci[key])
        raw = S.stable_read(path)
        value = json.loads(raw)
        assert value['sha'] == expected and value['tree']['sha'] == TREE
        add('proofs/' + path.name, raw, 'Exact final head/synthetic-merge commit-tree API receipt; independently checked against local objects where available', str(path))
    add('proofs/main-pr-merge-receipt.json', S.stable_read(proof['main_merge_receipt']), 'Actual merged PR receipt binding final head and main merge commit', proof['main_merge_receipt'])
    for record in contract['records']:
        raw = S.stable_read(record['source'])
        if record.get('sha256'):
            assert S.sha(raw) == record['sha256']
        original = raw
        if record.get('gzip'):
            raw = S.compressed(raw)
        add(record['relative'], raw, record['role'], record['source'], original=original if record.get('gzip') else None)
    slurm_receipt_raw = S.stable_read(contract['slurm_receipt'])
    slurm_receipt = json.loads(slurm_receipt_raw)
    add('slurm/receipt.json', slurm_receipt_raw, 'Completed final hostedSlurm lifecycle identity/artifact receipt', contract['slurm_receipt'])
    assert slurm_receipt['run_id'] == contract['slurm_run_id'] and slurm_receipt['conclusion'] == 'success'
    assert slurm_receipt['pull_request_head_sha'] == HEAD and slurm_receipt['tested_actions_merge_sha'] == MERGE
    assert slurm_receipt['checks_passed'] == slurm_receipt['checks_total'] == 32
    slurm_root = Path(slurm_receipt['artifact_directory'])
    manifest_raw = S.stable_read(slurm_root / 'manifest.json')
    assert S.sha(manifest_raw) == slurm_receipt['manifest_sha256']
    slurm = json.loads(manifest_raw)
    add('slurm/manifest.json', manifest_raw, 'Final hosted32check command/source manifest', str(slurm_root / 'manifest.json'))
    assert slurm['source_revision'] == MERGE and slurm['status'] == 'passed-for-recorded-checks'
    assert len(slurm['checks']) == 32 and all(check['passed'] for check in slurm['checks'])
    selected = {'manifest.json': slurm_root / 'manifest.json'}
    for command in slurm['commands']:
        for channel in ['stdout', 'stderr']:
            item = command[channel]
            S.safe_name(item['file'])
            source = slurm_root / item['file']
            assert S.sha(S.stable_read(source)) == item['sha256']
            selected[item['file']] = source
    for name in ['image-id.txt', 'service.log', 'slurm-services.log', 'system-packages.txt']:
        if (slurm_root / name).is_file():
            selected[name] = slurm_root / name
    for path in sorted((slurm_root / 'daemon-logs').glob('*')):
        assert path.is_file() and not path.is_symlink()
        selected['daemon-logs/' + path.name] = path
    for name, expected in slurm['source_sha256'].items():
        assert S.sha(git('show', HEAD + ':' + name)) == expected, name
    add('slurm/recorded-commands.tar.gz', bundle(selected), 'Exact hosted command stdout/stderr, source/check manifest and owned daemon metadata. Buildx cache and compiler build log excluded.', str(slurm_root))
    install_raw = S.stable_read(contract['install_receipt'])
    install = json.loads(install_raw)
    add('install/receipt.json', install_raw, 'Completed final hosted Bash/private-umask job/artifact identity receipt', contract['install_receipt'])
    assert install['run_id'] == contract['python_run_id'] and install['pull_request_head'] == HEAD
    assert all(job['conclusion'] == 'success' for job in install['jobs'])
    assert install['provenance']['qualification_checkout'] == MERGE
    assert install['provenance']['qualification_tree'] == install['provenance']['pull_request_head_tree'] == TREE
    for item in install['bash']:
        result_path = Path(item['result'])
        raw = S.stable_read(result_path)
        assert S.sha(raw) == item['result_sha256']
        result = json.loads(raw)
        assert result['passed'] and result['version'] == item['version']
        tests = S.stable_read(result_path.parent / 'tests.log')
        assert S.pytest_counts(tests)['passed'] == 41
        label = item['version'].replace('.', '')
        add('install/bash' + label + '-result.json', raw, 'Actual pinned legacy Bash build/test report; Linux/GCC qualification only', str(result_path))
        add('install/bash' + label + '-tests.log', tests, 'Authoritative downloaded hosted artifact test output; empty failed gh-run-view download excluded', str(result_path.parent / 'tests.log'))
    update_path = Path(install['umask002']['result'])
    update_raw = S.stable_read(update_path)
    assert S.sha(update_raw) == install['umask002']['result_sha256']
    update = json.loads(update_raw)
    assert update['passed'] and update['source_commit'] == MERGE and update['pipx_backend'] == 'pip' and len(update['checks']) == 33
    selected = {'result.json': update_path}
    for check in update['checks']:
        assert not check['failure'] and not check['timed_out'] and check['reaped']
        assert check['exit'] == check['expected_exit']
        S.safe_name(check['log'])
        path = update_path.parent / check['log']
        raw = S.stable_read(path)
        assert len(raw) == check['log_bytes'] and S.sha(raw) == check['sha256']
        selected[check['log']] = path
    add('install/umask002-records.tar.gz', bundle(selected), 'Exact actual33managed/pipx lifecycle report and all hash-bound logs, including legitimate empty successful-command logs', str(update_path.parent))
    add('drivers/prepare-hosted-review-evidence.py', S.stable_read(Path(__file__)), 'Archive coordinator; not another qualification run', str(Path(__file__)))
    add('drivers/prepare-delivery-review-evidence.py', S.stable_read(SUPPORT), 'Bounded content audit support used by coordinator', str(SUPPORT))
    summary = {'schema': 'raven.hosted-final-archive/v1', 'pull_request_head': HEAD, 'tested_merge': MERGE, 'tested_tree': TREE, 'main_commit': proof['main_commit'], 'main_tree': TREE, 'event': 'pull_request', 'python_workflow': python_ci, 'historical_failed_ci': contract['historical_failed_ci'], 'historical_attempt_identity': {'head': OLD_HEAD, 'tested_merge': OLD_MERGE, 'tested_tree': OLD_TREE, 'status': 'First matrix failed three fixture assumptions; original failure logs and actual root diagnosis are retained separately from final acceptance'}, 'hosted_slurm_checks': 32, 'hosted_bash_cases_each': 41, 'hosted_installed_update_checks': 33, 'qualification_limits': contract['limits'], 'counts_are_not_additive': True, 'earlier_v1_v2_v3_manifests_preserved': previous, 'unresolved_credential_candidates': 0}
    for name, value, role in [('summary.json', summary, 'Completed hosted job/run identity, precise units and final main merge proof'), ('input-contract.json', contract, 'Parent release contract and original evidence/source mapping')]:
        add(name, S.json_bytes(value), role, 'generated from retained completed records')
    readme = f"""# Hosted CI and final merge evidence

PR head `{HEAD}` and tested synthetic merge `{MERGE}` share tree `{TREE}`. The final main merge is identified in `summary.json`. Five complete successful Python jobs are bound to this final tree. Final source, installed-command, Slurm, Bash, and umask receipts retain this hosted identity.

The first matrix at `954213c` and second workflow at `ffda87d` failed. All five raw job logs and exact outcomes from each attempt remain historical. Test-fixture, readiness, and experimental build-isolation corrections retain their own evidence. The exact historical helper failure cause and remote-log wall-delay attribution remain unproved. These earlier attempts are not final acceptance. Qualification units remain separate; overlapping counts are not additive.

Slurm command recordings preserve exact raw stdout, stderr, and owned daemon metadata. Compiler and Buildx caches are excluded. Complete final hosted Python job logs are losslessly compressed in `python/`. Explicit excerpts preserve exact source line numbers, original complete-log hashes, and job URLs; they remain subsets of those retained full logs. Earlier local full logs and JUnit results remain in v3.

No physical GPU, production SSH, multi-node, heterogeneous site, human usability, or scientific efficacy qualification is implied. This append does not modify the tested source tree.
"""
    add('README.md', readme.encode(), 'Scope and immutable source/merge binding; earlier archive versions remain unchanged', 'generated')
    audit = S.inspect_payloads(payloads, contract.get('credential_fixture_allowances', []))
    audit['scope'] = 'All retained payloads and generated summary/contract/README before this audit; this generated audit contains only paths, hashes, sizes and approved fixture rationales.'
    audit_raw = S.json_bytes(audit)
    S.inspect_payloads({PREFIX + '/content-audit.json': audit_raw}, [])
    add('content-audit.json', audit_raw, 'Complete bounded archive/pattern inspection with exact fixture allowances; generated audit also independently scanned', 'generated from retained completed records')
    manifest = {'schema': 'raven.adversarial-review-artifacts/v4', 'append_only_parent_commit': PARENT, 'prior_manifests': previous, 'pull_request_head': HEAD, 'tested_merge': MERGE, 'tested_tree': TREE, 'main_commit': proof['main_commit'], 'new_payload_count': len(payloads), 'new_payload_bytes': sum(map(len, payloads.values())), 'files': {name: {'bytes': len(raw), 'sha256': S.sha(raw), **metadata[name]} for name, raw in sorted(payloads.items())}, 'limits': contract['limits']}
    assert not (ROOT / PREFIX).exists() and not (ROOT / 'artifact-manifest-v4.json').exists()
    with tempfile.TemporaryDirectory(prefix='.hosted-prepare-', dir=ROOT) as temporary:
        for name, raw in payloads.items():
            path = Path(temporary) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        os.rename(Path(temporary) / PREFIX, ROOT / PREFIX)
    (ROOT / 'artifact-manifest-v4.json').write_bytes(S.json_bytes(manifest))
    print(json.dumps({'prepared_no_commit_or_push': True, 'manifest_sha256': S.sha(S.json_bytes(manifest)), 'payload_count': len(payloads), 'payload_bytes': manifest['new_payload_bytes'], 'head': HEAD, 'tested_tree': TREE, 'main_commit': proof['main_commit'], 'unresolved_credential_candidates': len(audit['unresolved_candidates'])}, indent=2))


if __name__ == '__main__':
    main()
