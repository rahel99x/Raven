#!/usr/bin/env python3
"""Inspect completed GitHub distribution artifacts; never execute their contents."""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tarfile
import zipfile

REPO = 'rahel99x/Raven'
ROOT = Path('/workspace/Raven')


def gh_json(*args):
    return json.loads(subprocess.check_output(['gh', *args], text=True, timeout=60))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unique(root, name):
    found = list(root.rglob(name))
    assert len(found) == 1, (name, len(found))
    return found[0]


def git_bytes(revision, path):
    return subprocess.check_output(['git', 'show', revision + ':' + path], cwd=ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--merge', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists() and not args.receipt.exists()
    run = gh_json('run', 'view', args.run, '--repo', REPO, '--json', 'headSha,jobs,url')
    assert run['headSha'] == args.head
    jobs = [job for job in run['jobs'] if job['name'] == 'test (3.12)']
    assert len(jobs) == 1 and jobs[0]['conclusion'] == 'success'
    artifacts = gh_json('api', f'repos/{REPO}/actions/runs/{args.run}/artifacts')['artifacts']
    chosen = [item for item in artifacts if item['name'] == 'distributions-and-demo']
    assert len(chosen) == 1 and not chosen[0]['expired']
    artifact = chosen[0]
    raw_zip = args.output.with_suffix('.zip')
    assert not raw_zip.exists()
    with raw_zip.open('xb') as stream:
        subprocess.run(['gh', 'api', f'repos/{REPO}/actions/artifacts/{artifact["id"]}/zip'], stdout=stream, check=True, timeout=120)
    assert raw_zip.stat().st_size <= 128 << 20
    archive_sha = digest(raw_zip)
    expected_digest = artifact.get('digest')
    if expected_digest:
        assert expected_digest == 'sha256:' + archive_sha, 'GitHub artifact digest mismatch'
    args.output.mkdir(mode=0o700, parents=True)
    (args.output / 'artifact-metadata.json').write_text(json.dumps(artifact, indent=2) + '\n')
    (args.output / 'run-metadata.json').write_text(json.dumps(run, indent=2) + '\n')
    with zipfile.ZipFile(raw_zip) as archive:
        infos = archive.infolist()
        assert len(infos) <= 50000 and sum(item.file_size for item in infos) <= 256 << 20
        for item in infos:
            path = PurePosixPath(item.filename)
            assert not path.is_absolute() and '..' not in path.parts and '\\' not in item.filename
            assert item.file_size <= 64 << 20
            assert not stat.S_ISLNK(item.external_attr >> 16)
        archive.extractall(args.output)
    prototype_path = unique(args.output, 'manifest.json')
    prototype = json.loads(prototype_path.read_text())
    assert prototype['schema'] == 'raven.distribution-prototype/v1'
    assert prototype['source_commit'] == args.merge
    assert prototype['status'] == 'passed-experimental-extraction'
    assert prototype['experimental'] and not prototype['published'] and not prototype['production_rename']
    assert prototype['build_policy'] == {'isolation':'PEP 517 default', 'requirements_source':'pyproject.toml:[build-system]'}
    phases = ['core','lab','core-after-removal','lab-after-reinstall','lab-after-upgrade','lab-after-downgrade']
    assert [item['phase'] for item in prototype['journeys']] == phases
    assert prototype['mismatched_extension_refused']
    assert len(prototype['source_sha256']) == 626
    recipe = ast.parse(git_bytes(args.head, 'scripts/distribution_prototype.py'))
    build_commands = []
    for node in ast.walk(recipe):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'run' and node.args and isinstance(node.args[0], ast.List):
            values = [element.value for element in node.args[0].elts if isinstance(element, ast.Constant)]
            if values[:2] == ['-m','build']:
                assert values in (['-m','build'],['-m','build','--wheel'])
                build_commands.append(values)
    assert len(build_commands) == 2
    update_path = unique(args.output, 'result.json')
    update = json.loads(update_path.read_text())
    assert update['schema'] == 'raven.update-qualification/v1' and update['passed']
    assert update['source_commit'] == args.merge and update['source_stable']
    assert len(update['checks']) == 33
    for check in update['checks']:
        assert check['exit'] == check['expected_exit'] and not check['failure'] and not check['timed_out'] and check['reaped']
        path = Path(check['log'])
        assert not path.is_absolute() and '..' not in path.parts
        assert digest(update_path.parent / path) == check['sha256']
    wanted = {**prototype['source_sha256'], **update['source_hashes']}
    assert all(wanted[key] == value for key, value in prototype['source_sha256'].items())
    actual = {}
    runtime_expected = {}
    proc = subprocess.Popen(['git','archive',args.head],cwd=ROOT,stdout=subprocess.PIPE)
    with tarfile.open(fileobj=proc.stdout,mode='r|') as archive:
        for member in archive:
            if member.isfile() and (member.name in wanted or member.name.startswith('raven/')):
                checksum = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
                if member.name in wanted:
                    actual[member.name] = checksum
                if member.name.startswith('raven/'):
                    assert member.name not in runtime_expected
                    runtime_expected[member.name] = checksum
    assert proc.wait() == 0 and actual == wanted
    assert runtime_expected == prototype['source_sha256'] and len(runtime_expected) == 626
    flagship_path = unique(args.output, 'raven-qualification.json')
    flagship = json.loads(flagship_path.read_text())
    assert flagship['schema'] == 'raven.qualification-report/v1'
    assert flagship['status'] == 'qualified-for-recorded-checks' and not flagship['production_qualified']
    assert all(check['status'] in ('qualified','untested') for check in flagship['checks'])
    assert any(check['id'] == 'installed-wheel' and check['status'] == 'qualified' for check in flagship['checks'])
    offline_path = unique(args.output, 'raven-offline-qualification.json')
    offline = json.loads(offline_path.read_text())
    assert offline['schema'] == 'raven.qualification/v1' and not offline['production_qualified']
    assert offline['checks'] and all(check['status'] == 'passed' for check in offline['checks'])
    inventory_path = unique(args.output, 'release-inventory.json')
    inventory = json.loads(inventory_path.read_text())
    assert inventory['source_commit'] == args.merge
    assert inventory['schema'] == 'raven.release-inventory/v1' and inventory['version'] == '7.0.0'
    archives = [unique(args.output, item['filename']) for item in inventory['artifacts']]
    verifier = ROOT / 'scripts/release_inventory.py'
    assert verifier.read_bytes() == git_bytes(args.head, 'scripts/release_inventory.py')
    command = ['python',str(verifier),'policy',str(inventory_path),'--expected-version','7.0.0','--expected-source',args.merge,*map(str,archives)]
    inspected = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)
    inspection_log = args.output / 'release-policy-verification.log'
    inspection_log.write_text(inspected.stdout + inspected.stderr)
    wheel = next(path for path in archives if path.suffix == '.whl')
    with zipfile.ZipFile(wheel) as archive:
        infos = [item for item in archive.infolist() if item.filename.startswith('raven/') and not item.is_dir()]
        release_sources = {item.filename:hashlib.sha256(archive.read(item)).hexdigest() for item in infos}
        assert len(release_sources) == len(infos), 'Duplicate wheel package members'
    assert release_sources == runtime_expected, 'Final wheel contains omitted, modified or extra package files'
    sdist = next(path for path in archives if path.name.endswith('.tar.gz'))
    sdist_sources = {}
    with tarfile.open(sdist) as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if member.isfile() and len(parts) > 2 and parts[1] == 'raven':
                name = '/'.join(parts[1:])
                assert name not in sdist_sources, 'Duplicate sdist package members'
                sdist_sources[name] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
    assert sdist_sources == runtime_expected, 'Final sdist contains omitted, modified or extra package files'
    runtime_inventory_sha = hashlib.sha256(json.dumps(runtime_expected, sort_keys=True, separators=(',',':')).encode()).hexdigest()
    installed = next(check for check in flagship['checks'] if check['id'] == 'installed-wheel')
    assert installed['evidence']['wheel_sha256'] == digest(wheel)
    head_info = gh_json('api', f'repos/{REPO}/git/commits/{args.head}')
    merge_info = gh_json('api', f'repos/{REPO}/git/commits/{args.merge}')
    assert head_info['tree']['sha'] == merge_info['tree']['sha']
    summary = {'schema':'raven.hosted-distribution-review/v1','run_id':int(args.run),'run_url':run['url'],'head':args.head,'job':{key:jobs[0][key] for key in ('name','databaseId','url','conclusion','completedAt')},'status':'python312-artifacts-verified','whole_workflow_status':'not claimed; parent monitors every matrix job','artifact':{'id':artifact['id'],'name':artifact['name'],'github_digest':expected_digest,'downloaded_zip':str(raw_zip),'downloaded_zip_sha256':archive_sha,'github_digest_verified':bool(expected_digest),'downloaded_zip_bytes':raw_zip.stat().st_size},'provenance':{'qualification_checkout':args.merge,'head_tree':head_info['tree']['sha'],'qualification_tree':merge_info['tree']['sha'],'exact_tree_match':True,'prototype_package_sources_matched':len(prototype['source_sha256']),'normal_update_input_sources_matched':len(update['source_hashes']),'release_package_sources_matched':len(release_sources),'runtime_byte_proof':{'reference_commit':args.head,'reference_scope':'Every tracked raven/ file, without extension filtering','reference_files':len(runtime_expected),'reference_inventory_sha256':runtime_inventory_sha,'wheel_files':len(release_sources),'sdist_files':len(sdist_sources),'wheel_exact_match':True,'sdist_exact_match':True,'extra_runtime_files':0,'duplicate_runtime_members':0,'proof_scope':'New hosted wheel and sdist, read without extraction or execution; compared directly with Git288 bytes'}},'prototype':{'manifest':str(prototype_path),'sha256':digest(prototype_path),'status':prototype['status'],'build_policy':prototype['build_policy'],'build_recipe_default_isolation':True,'build_projects':['full','core','lab','next-core','next-lab'],'per_build_logs_uploaded':False,'journeys':phases,'mismatched_extension_refused':True,'experimental':True,'published':False,'production_rename':False,'limits':prototype['limits']},'normal_update':{'result':str(update_path),'sha256':digest(update_path),'passed':True,'checks':33,'all_log_hashes_verified':True,'limits':update['limits']},'flagship':{'report':str(flagship_path),'sha256':digest(flagship_path),'status':flagship['status'],'qualified_checks':[check['id'] for check in flagship['checks'] if check['status'] == 'qualified'],'untested_checks':[check['id'] for check in flagship['checks'] if check['status'] == 'untested'],'production_qualified':False},'offline':{'report':str(offline_path),'sha256':digest(offline_path),'passed_checks':[check['id'] for check in offline['checks']],'production_qualified':False,'untested':offline['untested']},'release_inventory':{'report':str(inventory_path),'sha256':digest(inventory_path),'source_commit':inventory['source_commit'],'source_commit_verification':inventory['source_commit_verification'],'policy_command':command,'policy_result':'passed; archive integrity and wheel/sdist source equivalence verified','policy_log':str(inspection_log),'artifacts':[{'filename':item['filename'],'sha256':item['sha256'],'bytes':item['size']} for item in inventory['artifacts']]},'capture_driver':str(Path(__file__).resolve()),'capture_driver_sha256':digest(Path(__file__)),'limits':['Artifact checksums establish byte identity, not publisher authentication or an attestation.','Prototype backend policy is checked against the immutable execution recipe and successful manifest; per-build stderr logs were not selected for upload.','Recorded local/offline checks do not establish live GPU, production SSH, multi-node or human-study qualification.','Only this Python3.12 job and its artifacts are evaluated here; no whole-CI success is inferred.']}
    args.receipt.write_text(json.dumps(summary,indent=2)+'\n')
    print(args.receipt)
    print('Python3.12 artifacts verified: six prototype journeys,626 exact package sources,33 update checks,flagship/offline receipts and source-bound release inventory.')


if __name__ == '__main__':
    main()
