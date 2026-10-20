#!/usr/bin/env python3
"""Verify completed hosted records and fill the parent-released v4 contract.

No source edits, Git changes, commits, pushes or archived-code execution occur.
"""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import zipfile

ROOT = Path('/workspace')
REPO = ROOT / 'Raven'
HEAD = '288a680dd85dde9766fce3c4d8d962b42bd12203'
MERGE = '2ccc55500471314c12631596c09bfda9335efc50'
TREE = 'dbb18bcb152e97f7bc88964e3b33063a5858cc34'
MAIN = '2bfe5795290575de0e1c4390f2166e89897902f6'
PREP = ROOT / 'review-hosted-final-preparation-288a680'
PREP.mkdir(mode=0o700, exist_ok=True)
module_path = ROOT / 'prepare-hosted-review-evidence.py'
spec = importlib.util.spec_from_file_location('hosted_archive_contract_support', module_path)
H = importlib.util.module_from_spec(spec)
spec.loader.exec_module(H)
S = H.S
contract = json.loads(S.stable_read(ROOT / 'review-hosted-evidence-inputs.template.json'))
assert contract['ready'] is False
records = contract['records']
def add(source, relative, role, *, expected=None):
    raw = S.stable_read(source)
    if expected is not None:
        assert S.sha(raw) == expected
    entry = {'source': str(source), 'relative': relative, 'role': role, 'sha256': S.sha(raw)}
    assert not any(item['relative'] == relative for item in records)
    records.append(entry)
    return raw

def read_json(source, expected=None):
    raw = S.stable_read(source)
    if expected is not None:
        assert S.sha(raw) == expected
    return json.loads(raw)

python_path = ROOT / 'review-final-python-ci-288a680/python-ci.json'
python = read_json(python_path)
assert python['conclusion'] == 'success' and python['pull_request_head'] == HEAD
assert python['tested_merge'] == MERGE and python['head_tree'] == python['tested_merge_tree'] == TREE
assert len(python['python_jobs']) == 5 and all(j['conclusion'] == 'success' for j in python['python_jobs'])
main_proof_path = ROOT / 'review-main-delivery-proof.json'
proof = read_json(main_proof_path)
assert proof['main_commit'] == MAIN and proof['qualified_head'] == HEAD and proof['qualified_pr_merge'] == MERGE
assert proof['identical_qualified_tree'] == TREE and proof['all_main_runtime_bytes_match_qualified_wheel_and_sdist'] is True
assert proof['successful_ci_receipt_sha256'] == S.sha(S.stable_read(python_path))
for revision in (HEAD, MERGE, MAIN, 'refs/remotes/origin/main'):
    assert subprocess.check_output(['git', 'rev-parse', revision + '^{tree}'], cwd=REPO, text=True).strip() == TREE

receipt_path = ROOT / 'review-hosted-distributions-288a680-result.json'
distribution = read_json(receipt_path)
assert distribution['run_id'] == python['run_id'] and distribution['head'] == HEAD
assert distribution['job']['conclusion'] == 'success'
assert distribution['provenance']['qualification_checkout'] == MERGE
assert distribution['provenance']['head_tree'] == distribution['provenance']['qualification_tree'] == TREE
zip_raw = S.stable_read(distribution['artifact']['downloaded_zip'])
assert S.sha(zip_raw) == distribution['artifact']['downloaded_zip_sha256']
assert distribution['artifact']['github_digest'] == 'sha256:' + S.sha(zip_raw)
with zipfile.ZipFile(io.BytesIO(zip_raw)) as outer:
    assert outer.testzip() is None
    assert len(outer.namelist()) == len(set(outer.namelist()))
# The raw outer ZIP duplicates the retained wheel/sdist/metadata/results; keep its
# exact GH digest and artifact receipt, not another7.75MB copy of the same bytes.
add(receipt_path, 'distribution/receipt.json', 'Actual final Python3.12 artifact receipt, GH ZIP digest, exact source-byte and lifecycle verification; not a separate whole-workflow qualification')
artifact_root = ROOT / 'review-hosted-distributions-288a680'
for name in ('artifact-metadata.json', 'run-metadata.json', 'release-policy-verification.log'):
    add(artifact_root / name, 'distribution/' + name, 'Unmodified final hosted distribution artifact/run metadata or independently recorded release policy verification')
for name in ('capture-hosted-distribution-evidence.py', 'verify-review-main-delivery.py'):
    add(ROOT / name, 'drivers/' + name, 'Actual artifact/source or main-delivery verifier driver; archived without execution')
assert S.sha(S.stable_read(ROOT / 'capture-hosted-distribution-evidence.py')) == distribution['capture_driver_sha256']
add(main_proof_path, 'proofs/main-delivery.json', 'Independent final PR/main/source-artifact equality verification; main push workflow status is explicitly not inferred')

# One immutable Git archive provides raw comparison bytes for every input,
# including non-Python files; no imports of the delivered application occur.
git_raw = subprocess.check_output(['git', 'archive', '--format=tar', HEAD], cwd=REPO)
git_files = {}
with tarfile.open(fileobj=io.BytesIO(git_raw), mode='r:') as archive:
    for item in archive:
        if item.isfile():
            S.safe_name(item.name)
            git_files[item.name] = archive.extractfile(item).read()
runtime = {name: S.sha(raw) for name, raw in git_files.items() if name.startswith('raven/')}
assert len(runtime) == 626
assert S.digest_map(runtime) == distribution['provenance']['runtime_byte_proof']['reference_inventory_sha256']
inventory_path = Path(distribution['release_inventory']['report'])
inventory = read_json(inventory_path, distribution['release_inventory']['sha256'])
assert inventory['source_commit'] == MERGE
add(inventory_path, 'distribution/release-inventory.json', 'Exact final member-level release inventory; independently checked against Git and artifact members')
for name in ('SHA256SUMS', 'raven.spdx.json'):
    add(inventory_path.parent / name, 'distribution/' + name, 'Exact final hosted checksums or SPDX inventory; checksum equality is not publisher authentication')
archive_runtime_maps = {}
fixture_allowances = []
for artifact in distribution['release_inventory']['artifacts']:
    source = inventory_path.parent / artifact['filename']
    raw = S.stable_read(source)
    assert len(raw) == artifact['bytes'] and S.sha(raw) == artifact['sha256']
    relative = 'distribution/' + artifact['filename']
    add(source, relative, 'Actual final hosted build artifact, read without importing/executing archived programs', expected=artifact['sha256'])
    archive_files = {}
    if source.suffix == '.whl':
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            assert archive.testzip() is None
            assert len(archive.namelist()) == len(set(archive.namelist()))
            for item in archive.infolist():
                S.safe_name(item.filename)
                if not item.is_dir():
                    archive_files[item.filename] = archive.read(item)
        current = {name: S.sha(value) for name, value in archive_files.items() if name.startswith('raven/')}
    else:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            seen = set()
            for item in archive:
                S.safe_name(item.name)
                assert item.name not in seen
                seen.add(item.name)
                assert item.isfile() or item.isdir()
                if item.isfile():
                    archive_files[item.name] = archive.extractfile(item).read()
        current = {'/'.join(name.split('/')[1:]): S.sha(value) for name, value in archive_files.items()
                   if '/'.join(name.split('/')[1:]).startswith('raven/')}
        # Only explicit static tests already committed and manually reviewed as
        # redaction/refusal fixtures may be excepted, by exact path/kind/SHA.
        allowed_tests = {'tests/test_prometheus_source.py', 'tests/test_provenance.py',
                         'tests/test_review_install_trust.py', 'tests/test_update_source.py'}
        for name, value in archive_files.items():
            project_path = '/'.join(name.split('/')[1:])
            if project_path in allowed_tests and S.PATTERNS['credential-url'].search(value):
                assert value == git_files[project_path]
                fixture_allowances.append({'name': 'hosted-' + HEAD[:7] + '/' + relative + '!' + name,
                    'kind': 'credential-url', 'file_sha256': S.sha(value),
                    'reason': 'Manually reviewed committed regression source: static credential URL refusal/redaction fixture targeting example.invalid or loopback127.0.0.1:1, not a live credential. Exact file bytes match Git288; helper-fixture changes do not modify those URL fixtures. Exception is limited to this exact archive member path, SHA256 and detection kind.'})
    assert current == runtime
    archive_runtime_maps[artifact['filename']] = {'files': len(current), 'mapping_sha256': S.digest_map(current)}
    member_inventory = next(item for item in inventory['artifacts'] if item['filename'] == artifact['filename'])
    assert len(member_inventory['members']) == len(archive_files)
    for member in member_inventory['members']:
        value = archive_files[member['path']]
        assert len(value) == member['size'] and S.sha(value) == member['sha256']

prototype = read_json(distribution['prototype']['manifest'], distribution['prototype']['sha256'])
assert prototype['source_commit'] == MERGE and prototype['source_sha256'] == runtime
assert prototype['status'] == 'passed-experimental-extraction' and len(prototype['journeys']) == 6
assert prototype['mismatched_extension_refused'] is True
assert prototype['experimental'] is True and prototype['published'] is False
add(distribution['prototype']['manifest'], 'distribution/prototype-six-journeys.json', 'Actual final hosted experimental prototype manifest, all six installed journeys and mismatch refusal. Per-build stderr logs were not uploaded; product split remains experimental.', expected=distribution['prototype']['sha256'])
normal = read_json(distribution['normal_update']['result'], distribution['normal_update']['sha256'])
assert normal['passed'] and normal['source_commit'] == MERGE and len(normal['checks']) == 33
assert len(normal['source_hashes']) == 2298
for name, expected in normal['source_hashes'].items():
    assert S.sha(git_files[name]) == expected
normal_dir = Path(distribution['normal_update']['result']).parent
selected = {'result.json': normal_dir / 'result.json'}
for check in normal['checks']:
    assert not check['failure'] and not check['timed_out'] and check['reaped'] and check['exit'] == check['expected_exit']
    S.safe_name(check['log'])
    source = normal_dir / check['log']
    raw = S.stable_read(source)
    assert len(raw) == check['log_bytes'] and S.sha(raw) == check['sha256']
    selected[check['log']] = source
bundle_path = PREP / 'ordinary-update-records.tar.gz'
bundle_path.write_bytes(H.bundle(selected))
add(bundle_path, 'distribution/ordinary-update-records.tar.gz', 'Actual33 ordinary managed/pipx update/revert checks,2298 input source hashes and all33 exact logs; separate from private-umask run, overlapping workflows')
for key, relative in (('flagship', 'flagship.json'), ('offline', 'offline.json')):
    item = distribution[key]
    add(item['report'], 'distribution/' + relative, 'Actual final hosted recorded qualification with explicit untested hardware/production/human-study limits', expected=item['sha256'])
source_proof = {'schema': 'raven.hosted-source-archive-verification/v1', 'head': HEAD, 'tested_merge': MERGE,
    'main_commit': MAIN, 'tree': TREE, 'runtime_files': 626, 'runtime_mapping_sha256': S.digest_map(runtime),
    'runtime_sha256': runtime, 'archive_runtime_maps': archive_runtime_maps,
    'ordinary_update_inputs_matched_git': len(normal['source_hashes']),
    'ordinary_update_checks': 33, 'prototype_journeys': 6,
    'raw_outer_zip_retained': False, 'raw_outer_zip_sha256': S.sha(zip_raw),
    'scope': 'Independent raw Git/hosted wheel/sdist/member-inventory/source-hash equality; no archived code executed. This establishes source identity, not hardware, site or scientific efficacy.'}
proof_path = PREP / 'source-byte-verification.json'
proof_path.write_bytes(S.json_bytes(source_proof))
add(proof_path, 'distribution/source-byte-verification.json', source_proof['scope'])
add(Path(__file__), 'drivers/fill-hosted-review-ready-contract.py', 'Parent-released input/source/archive verifier and contract filler; no Git snapshot or publication performed by this driver')

contract.update(ready=True, publication_authorization='parent reviewed all Python CI green and main merge proof; local append only',
    python_run_id=python['run_id'], python_ci_receipt=str(python_path),
    slurm_receipt=str(ROOT / 'review-hosted-real-slurm-39468456727-result.json'), slurm_run_id=39468456727,
    install_receipt=str(ROOT / 'review-hosted-install-288a680/summary.json'))
contract['identity_proof'].update(pull_request_head=HEAD, tested_merge=MERGE, head_tree=TREE,
    tested_merge_tree=TREE, main_tree=TREE, main_commit=MAIN,
    main_merge_receipt=str(ROOT / 'review-main-merged-pr.json'),
    tested_merge_commit_api_receipt=python['tested_merge_commit_api_receipt'])
contract['credential_fixture_allowances'] = fixture_allowances
contract['distribution_identity'] = {'receipt': str(receipt_path), 'sha256': S.sha(S.stable_read(receipt_path)),
    'runtime_files_exact': 626, 'normal_update_checks': 33, 'prototype_journeys': 6,
    'raw_zip_retained': False, 'raw_zip_sha256': S.sha(zip_raw)}
contract['pending_requirements'] = ['Parent skeptical review of prepared v4 manifest/audit before any evidence commit or push.']
contract['expected_candidate']['acceptance'] = True
contract['expected_candidate']['synthetic_merge_status'] = 'Verified completed successful workflow checkout, API receipt and fetched exact Git parent/tree proof.'
output = ROOT / 'review-hosted-evidence-inputs-ready-288a680.json'
output.write_bytes(S.json_bytes(contract))
print(json.dumps({'ready_contract': str(output), 'sha256': S.sha(output.read_bytes()),
    'records': len(records), 'runtime_files_verified': len(runtime), 'normal_update_source_files': len(normal['source_hashes']),
    'credential_fixture_allowances': len(fixture_allowances), 'no_git_edits': True}, indent=2))
