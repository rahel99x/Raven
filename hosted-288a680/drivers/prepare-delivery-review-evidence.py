"""Prepare an append-only final evidence snapshot after the parent releases it.

Preparation requires --ready and a completed input contract; merely creating or
compiling this script does not read/copy in-progress qualification outputs.
No commits, pushes, source edits or execution of archived programs occur here.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import zipfile

REVISION = 'f63d82ecd40c8b3ad4bdb42c5f92a492d9b0cdc0'
LIVE = '003f3a5c515d70b48982d6c856c57a1d6607d613'
PARENT = '3b104ee64d060b5fbbe7d9da2f37345cd63c91cc'
REPO = Path('/workspace/Raven')
EVIDENCE = Path('/workspace/Raven-review-evidence')
PREFIX = 'delivery-f63d82e'
SIX = {'raven/controller.py', 'raven/installation/update_verification.py', 'raven/plugins.py', 'raven/pointer_routing.py', 'raven/tracing/ui.py', 'raven/workbench.py'}
REQUIRED = {'full-log', 'full-xml', 'artifact-completion', 'wheel', 'sdist', 'inventory', 'source-proof', 'flagship', 'installed-smoke', 'quality', 'acyclic', 'schemas', 'compat310', 'compat314', 'environment', 'prototype', 'updates', 'integration', 'graph'}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def digest_map(mapping):
    return sha(json.dumps(mapping, sort_keys=True, separators=(',', ':')).encode())


def git(*args, repo=REPO):
    return subprocess.check_output(['git', *args], cwd=repo)


def stable_read(path):
    path = Path(path)
    assert path.is_file() and not path.is_symlink(), str(path)
    before = path.stat()
    assert before.st_size <= 128 << 20, str(path)
    raw = path.read_bytes()
    after = path.stat()
    fields = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    assert fields(before) == fields(after) and len(raw) == before.st_size, 'Input changed while reading: ' + str(path)
    return raw


def safe_name(name):
    p = PurePosixPath(name)
    assert name and not p.is_absolute() and '..' not in p.parts and '\\' not in name, name


def json_bytes(value):
    return (json.dumps(value, indent=2) + '\n').encode()


def compressed(raw):
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, filename='', mode='wb', mtime=0) as stream:
        stream.write(raw)
    assert gzip.decompress(output.getvalue()) == raw
    return output.getvalue()


def pytest_counts(raw):
    text = raw.decode('utf-8')
    matches = list(re.finditer(r'(?:\d+ (?:passed|skipped|failed|error(?:s)?)(?:, | in |$))', text))
    assert matches, 'Missing completed pytest summary'
    line = text[text.rfind('\n', 0, matches[-1].start()) + 1:].splitlines()[0]
    counts = {kind: int(count) for count, kind in re.findall(r'(\d+) (passed|skipped|failed|errors?)', line)}
    assert counts.get('passed', 0) > 0 and not counts.get('failed', 0) and not counts.get('error', 0) and not counts.get('errors', 0), line
    return counts


def validate_record(record, raw):
    kind = record.get('kind', 'raw')
    if kind == 'junit':
        root = ET.fromstring(raw)
        suites = [root] if root.tag == 'testsuite' else root.findall('testsuite')
        assert suites, 'No completed JUnit suites'
        result = {key: sum(int(suite.get(key, '0')) for suite in suites) for key in ('tests', 'failures', 'errors', 'skipped')}
        assert result['tests'] >= 19000 and result['failures'] == result['errors'] == 0, result
        result['passed'] = result['tests'] - result['skipped']
        return result
    if kind == 'pytest':
        return pytest_counts(raw)
    if kind == 'json':
        document = json.loads(raw)
        for key, expected in record.get('equals', {}).items():
            current = document
            for part in key.split('.'):
                current = current[int(part)] if isinstance(current, list) else current[part]
            assert current == expected, (record['id'], key)
        return {'valid_json': True}
    assert kind == 'raw', kind
    return {'bytes_bound_and_stable_read': True}


PATTERNS = {
    'private-key-block': re.compile(rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----'),
    'github-token': re.compile(rb'\b(?:ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,})\b'),
    'aws-key': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'slack-token': re.compile(rb'\bxox[baprs]-[0-9A-Za-z-]{20,}\b'),
    'openai-token': re.compile(rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{30,}\b'),
    'credential-url': re.compile(rb'https?://[^\s:/@]{2,}:[^\s/@]{8,}@'),
}
PRIVATE_NAMES = re.compile(r'(^|/)(?:id_rsa|id_ed25519|id_ecdsa|credentials|\.env|munge\.key)(?:$|\.)', re.I)


def inspect_payloads(payloads, allowances):
    findings, archives, inspected = [], [], []
    allowed = {(item['name'], item['kind'], item['file_sha256']): item['reason'] for item in allowances}
    assert all(reason.strip() for reason in allowed.values())
    def scan(name, raw, *, depth=0):
        assert depth <= 3
        safe_name(name.split('!', 1)[0])
        inspected.append((name, sha(raw), len(raw)))
        if PRIVATE_NAMES.search(name):
            findings.append({'name': name, 'kind': 'private-filename', 'file_sha256': sha(raw)})
        for kind, pattern in PATTERNS.items():
            if pattern.search(raw):
                findings.append({'name': name, 'kind': kind, 'file_sha256': sha(raw)})
        if name.endswith('.log.gz') or name.endswith('.xml.gz'):
            plain = gzip.decompress(raw)
            assert len(plain) <= 128 << 20
            scan(name[:-3], plain, depth=depth + 1)
        elif zipfile.is_zipfile(io.BytesIO(raw)):
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                assert archive.testzip() is None
                size = 0
                for member in archive.infolist():
                    safe_name(member.filename)
                    assert not (member.external_attr >> 16) & 0o170000 == 0o120000, member.filename
                    size += member.file_size
                    assert member.file_size <= 32 << 20 and size <= 256 << 20
                    if not member.is_dir():
                        scan(name + '!' + member.filename, archive.read(member), depth=depth + 1)
                archives.append({'path': name, 'format': 'zip', 'members': len(archive.infolist()), 'crc': 'passed'})
        elif name.endswith('.tar.gz'):
            with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
                size = 0
                members = archive.getmembers()
                seen = set()
                for member in members:
                    safe_name(member.name)
                    assert member.name not in seen
                    seen.add(member.name)
                    assert member.isfile() or member.isdir(), member.name
                    size += member.size
                    assert member.size <= 32 << 20 and size <= 256 << 20
                    if member.isfile():
                        value = archive.extractfile(member).read()
                        assert len(value) == member.size
                        scan(name + '!' + member.name, value, depth=depth + 1)
                archives.append({'path': name, 'format': 'tar.gz', 'members': len(members), 'safe_paths_no_links_full_read': True})
    for name, raw in payloads.items():
        scan(name, raw)
    unresolved = [item for item in findings if (item['name'], item['kind'], item['file_sha256']) not in allowed]
    assert not unresolved, json.dumps(unresolved, indent=2)
    return {'schema': 'raven.delivery-content-audit/v1', 'objects_inspected': len(inspected), 'content_mapping_sha256': digest_map({name: digest for name, digest, _ in inspected}), 'archives': archives, 'credential_candidates': findings, 'explicit_fixture_allowances': allowances, 'unresolved_candidates': [], 'limits': 'High-confidence credential patterns, filename and complete bounded archive inspection; not exhaustive secret detection. No archived program was executed.'}


def prepare(contract_path):
    contract_raw = stable_read(contract_path)
    contract = json.loads(contract_raw)
    assert contract.get('schema') == 'raven.delivery-evidence-inputs/v1'
    assert contract.get('ready') is True and contract.get('code_revision') == REVISION
    assert contract.get('publication_authorization') == 'parent reviewed completed outputs; local append only'
    assert git('rev-parse', 'HEAD', repo=EVIDENCE).decode().strip() == PARENT
    assert not git('status', '--porcelain', repo=EVIDENCE).strip()
    v1raw = git('show', PARENT + ':artifact-manifest.json', repo=EVIDENCE)
    v2raw = git('show', PARENT + ':artifact-manifest-v2.json', repo=EVIDENCE)
    previous = [json.loads(v1raw), json.loads(v2raw)]
    for manifest in previous:
        for name, item in manifest['files'].items():
            raw = stable_read(EVIDENCE / name)
            assert len(raw) == item['bytes'] and sha(raw) == item['sha256']
    records = contract['records']
    ids = [record['id'] for record in records]
    assert len(ids) == len(set(ids)) and REQUIRED <= set(ids)
    assert all(record.get('status') == 'passed' for record in records if record['id'] in REQUIRED)
    payloads, metadata, results = {}, {}, {}
    originals = {}
    for record in records:
        safe_name(record['relative'])
        assert record['relative'] not in payloads
        if record['source'].startswith('git:'):
            _, revision, path = record['source'].split(':', 2)
            assert revision == REVISION
            raw = git('show', revision + ':' + path)
        else:
            raw = stable_read(record['source'])
        if record.get('sha256'):
            assert sha(raw) == record['sha256'], record['id']
        results[record['id']] = validate_record(record, raw)
        originals[record['id']] = raw
        value = compressed(raw) if record.get('gzip', False) else raw
        name = PREFIX + '/' + record['relative']
        assert name not in payloads
        payloads[name] = value
        metadata[name] = {'source': record['source'], 'role': record['role'], 'source_bytes': len(raw), 'source_sha256': sha(raw), 'encoding': 'gzip, empty filename, mtime0' if record.get('gzip') else 'verbatim', 'record_id': record['id'], 'command': record.get('command'), 'qualification': record.get('qualification', '')}
    junit, summary = results['full-xml'], results['full-log']
    assert junit['passed'] == summary['passed'] and junit['skipped'] == summary.get('skipped', 0)
    completion = json.loads(originals['artifact-completion'])
    assert completion['code_revision'] == REVISION and completion['status'] == 'passed'
    proof = json.loads(originals['source-proof'])
    assert proof['code_revision'] == REVISION and proof['runtime_files'] == 626 and proof['python_files'] == 625
    runtime = proof['runtime_sha256']
    assert len(runtime) == 626 and digest_map(runtime) == proof['mapping_sha256']
    assert sum(name.endswith('.py') for name in runtime) == 625
    assert not git('diff', '--name-only', REVISION, '--', 'raven', 'scripts', 'tests', 'pyproject.toml', 'MANIFEST.in', 'install.sh').strip(), 'Source/tests changed after qualified freeze'
    assert set(proof['changed_runtime_files_after_live_checkpoint']) == SIX
    assert not proof['added_runtime_files_after_live_checkpoint'] and not proof['removed_runtime_files_after_live_checkpoint']
    for name, expected in runtime.items():
        assert sha(git('show', REVISION + ':' + name)) == expected, name
        assert sha(stable_read(REPO / name)) == expected, 'Runtime differs at archive capture: ' + name
    changes = set(git('diff', '--name-only', LIVE, REVISION, '--', 'raven').decode().splitlines())
    assert changes == SIX
    for item in ('wheel', 'sdist'):
        assert sha(originals[item]) == proof[item + '_sha256']
    with zipfile.ZipFile(io.BytesIO(originals['wheel'])) as wheel:
        wheel_runtime = {name: wheel.read(name) for name in wheel.namelist() if name.startswith('raven/')}
        assert set(wheel_runtime) == set(runtime)
        assert all(sha(raw) == runtime[name] for name, raw in wheel_runtime.items())
    with tarfile.open(fileobj=io.BytesIO(originals['sdist']), mode='r:gz') as sdist:
        sdist_runtime = {member.name.split('/', 1)[1]: sdist.extractfile(member).read() for member in sdist.getmembers() if member.isfile() and '/raven/' in member.name and member.name.split('/', 1)[1].startswith('raven/')}
        assert set(sdist_runtime) == set(runtime)
        assert all(sha(raw) == runtime[name] for name, raw in sdist_runtime.items())
    backend = json.loads(git('show', PARENT + ':final-003f3a5/slurm/qualified-backend-manifest.json', repo=EVIDENCE))
    assert len(backend['files']) == proof['qualified_backend_files_unchanged'] == 48
    assert all(runtime[name] == expected for name, expected in backend['files'].items())
    assert digest_map(backend['files']) == proof['qualified_backend_mapping_sha256']
    installed = json.loads(originals['installed-smoke'])
    assert installed['code_revision'] == REVISION and installed['wheel_sha256'] == proof['wheel_sha256'] and installed['status'] == 'qualified-local'
    flagship = json.loads(originals['flagship'])
    assert flagship['status'] == 'qualified-for-recorded-checks' and flagship['production_qualified'] is False
    prototype = json.loads(originals['prototype'])
    assert prototype['source_commit'] == REVISION and prototype['source_sha256'] == runtime
    assert prototype['status'] == 'passed-experimental-extraction' and len(prototype['journeys']) == 6
    assert prototype['mismatched_extension_refused'] and prototype['experimental'] and not prototype['published'] and not prototype['production_rename']
    updates = json.loads(originals['updates'])
    assert updates['passed'] and updates['source_commit'] == REVISION and updates['pipx_backend'] == 'pip'
    assert len(updates['checks']) == 33
    integration = json.loads(originals['integration'])
    assert integration['schema'] == 'raven.review-integration/v1' and integration['code_revision'] == REVISION
    assert integration['status'] == 'qualified-local' and all(check['status'] == 'passed' for check in integration['checks'] if check['required'])
    graph = json.loads(originals['graph'])
    assert not graph['cyclic_components'] and not graph['dynamic_module_expressions'] and not graph['violations']
    assert b'Contracts: 7 kept, 0 broken.' in originals['quality']
    # Inventory package IDs differ by distribution; the exact binary hashes
    # must occur in its complete manifest, independently of descriptive names.
    inventory = json.loads(originals['inventory'])
    assert inventory['source_commit'] == REVISION
    inventory_serialized = json.dumps(inventory)
    assert proof['wheel_sha256'] in inventory_serialized and proof['sdist_sha256'] in inventory_serialized
    audit = inspect_payloads(payloads, contract.get('credential_fixture_allowances', []))
    provenance = {'schema': 'raven.final-delivery-archive-provenance/v1', 'code_revision': REVISION, 'qualified_runtime_mapping_sha256': proof['mapping_sha256'], 'full_regression': junit, 'record_validation': results, 'source_bytes_compared_to_git': 626, 'wheel_runtime_members_compared': len(wheel_runtime), 'sdist_runtime_members_compared': len(sdist_runtime), 'live_slurm_checkpoint': LIVE, 'unchanged_qualified_backend_files': 48, 'six_later_runtime_files': {name: {'live_checkpoint_sha256': sha(git('show', LIVE + ':' + name)), 'delivery_sha256': runtime[name]} for name in sorted(SIX)}, 'whole_delivery_live_slurm_qualified': False, 'hosted_ci_claim': False, 'limits': contract['limits']}
    for relative, value, role in [('input-contract.json', contract, 'Parent completion contract and exact retained commands; not external authorization or efficacy proof'), ('provenance.json', provenance, 'Independent manifest, regression, source/artifact equality and source-scope verification'), ('content-audit.json', audit, 'Bounded complete archive inspection and credential-pattern audit')]:
        name = PREFIX + '/' + relative
        assert name not in payloads
        payloads[name] = json_bytes(value)
        metadata[name] = {'role': role, 'encoding': 'generated JSON, inputs preserved verbatim separately'}
    readme = ('# Final delivery qualification evidence\n\nExact code checkpoint: `' + REVISION + '`. This append preserves completed local regression, source/artifact equality, quality, schema, interpreter/environment, installed-command and experimental prototype evidence. Read `provenance.json`, `input-contract.json` and the individual reports for source identities, actual commands and limits. Full pytest logs and JUnit XML are deterministic gzip files. The wheel and source distribution are the exact tested bytes, not reconstructed binaries.\n\nThe earlier live Slurm matrix tested `' + LIVE + '`. Forty-eight qualified backend files match; six later runtime files are explicitly delimited in provenance. No whole-final-tree live Slurm, physical GPU, production SSH, multinode deployment, human usability, efficacy or hosted CI qualification is implied. Focused checks overlap; counts are not additive. Prior v1/v2 payloads and manifests remain byte-for-byte unchanged.\n').encode()
    payloads[PREFIX + '/README.md'] = readme
    metadata[PREFIX + '/README.md'] = {'role': 'Final completed evidence scope, reading order and qualification limits', 'encoding': 'generated UTF8'}
    manifest = {'schema': 'raven.adversarial-review-artifacts/v3', 'append_only_parent_commit': PARENT, 'unchanged_v1_manifest_sha256': sha(v1raw), 'unchanged_v2_manifest_sha256': sha(v2raw), 'qualified_code_revision': REVISION, 'new_payload_count': len(payloads), 'new_payload_bytes': sum(map(len, payloads.values())), 'files': {name: {'bytes': len(raw), 'sha256': sha(raw), **metadata[name]} for name, raw in sorted(payloads.items())}, 'limits': contract['limits']}
    assert not (EVIDENCE / PREFIX).exists() and not (EVIDENCE / 'artifact-manifest-v3.json').exists()
    # Publish the completed append only after every input and payload validates.
    with tempfile.TemporaryDirectory(prefix='.delivery-prepare-', dir=EVIDENCE) as temporary:
        stage = Path(temporary) / PREFIX
        for name, raw in payloads.items():
            destination = Path(temporary) / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        os.rename(stage, EVIDENCE / PREFIX)
    (EVIDENCE / 'artifact-manifest-v3.json').write_bytes(json_bytes(manifest))
    assert (EVIDENCE / 'artifact-manifest.json').read_bytes() == v1raw
    assert (EVIDENCE / 'artifact-manifest-v2.json').read_bytes() == v2raw
    print(json.dumps({'prepared_only_no_commit_or_push': True, 'parent_commit': PARENT, 'code_revision': REVISION, 'manifest_sha256': sha(json_bytes(manifest)), 'new_payload_count': manifest['new_payload_count'], 'new_payload_bytes': manifest['new_payload_bytes'], 'regression': junit, 'all626source_and_artifact_members_match': True, 'unresolved_credential_candidates': len(audit['unresolved_candidates'])}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--ready', action='store_true', help='Parent has explicitly released completed frozen outputs for snapshot')
    args = parser.parse_args()
    if not args.ready:
        parser.error('Preparation is held until the parent supplies a completed contract and explicitly releases snapshotting')
    prepare(args.inputs)


if __name__ == '__main__':
    main()
