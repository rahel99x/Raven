import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from raven import evidence as E, evidence_signing as S, evidence_verify as V, operations as O, ops_platform as OPS

IDENTITY = {'cluster': 'alpha', 'job_id': '123_4', 'attempt': '2026-01-01T00:00:00/2026-01-01T00:00:02'}
PRINCIPAL = 'researcher@example.edu'


@pytest.mark.parametrize('device,inode', [(2**63+7, 2**64-1), (0, 2**63+1)])
def test_opaque_filesystem_identity_preserves_unsigned_values_on_worker_wire(device, inode):
    from raven import raven_protocol as P
    info = SimpleNamespace(st_dev=device, st_ino=inode, st_size=1024, st_mtime_ns=123, st_ctime_ns=456)
    identity = S._identity(info)
    encoded = P.encode({'key_identity': identity})
    decoder = P.Decoder()
    decoder.feed(encoded)
    assert decoder.take() == [{'key_identity': [str(device), str(inode), 1024, 123, 456]}]
    assert int(identity[0]) == device and int(identity[1]) == inode


@pytest.mark.parametrize('field,value', [('st_dev', -1), ('st_dev', 2**64), ('st_dev', True),
                                       ('st_ino', 0), ('st_ino', 2**64), ('st_ino', '123')])
def test_signing_rejects_invalid_filesystem_identity_atoms(field, value):
    info = dict(st_dev=0, st_ino=1, st_size=1024, st_mtime_ns=123, st_ctime_ns=456)
    info[field] = value
    with pytest.raises(ValueError, match='filesystem identity'):
        S._identity(SimpleNamespace(**info))


@pytest.fixture
def key(tmp_path):
    if not shutil.which('ssh-keygen'):
        pytest.skip('OpenSSH is required for actual cryptographic qualification')
    path = tmp_path / 'key with spaces'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(path)], check=True)
    return path


def evidence():
    return E.receipt(IDENTITY, {'id': 'test', 'format': 'test-json', 'version': '1', 'sha256': 'a' * 64},
                     [{'id': 'rss', 'kind': 'metric', 'data': {'name': 'memory', 'value': 123, 'unit': 'B'}}])


def context(**kwargs):
    return O.Context(scope={'cluster': 'alpha', 'user': 'researcher'}, **kwargs)


def prepare(tmp_path, key=None, **changes):
    source = tmp_path / 'evidence.json'
    source.write_bytes(E.canonical(evidence()))
    params = {'path': str(source), 'format': 'raven-evidence', **IDENTITY,
              'destination': str(tmp_path / 'signed bundle'), 'export_format': 'workflow-run-ro-crate'}
    if key:
        params.update(signing_key=str(key), signing_principal=PRINCIPAL)
    params.update(changes)
    report = O.run('research-portable', params, context())
    return params, report


def allowlist(tmp_path, key, principal=PRINCIPAL):
    path = tmp_path / 'independent allowed signers'
    path.write_text(principal + ' namespaces="' + S.NAMESPACE + '" ' + Path(str(key) + '.pub').read_text())
    return path


def signed(tmp_path, key):
    params, report = prepare(tmp_path, key)
    OPS.apply('research-portable', report['plan'], context())
    return Path(params['destination']), allowlist(tmp_path, key)


def test_full_review_apply_standalone_verification_without_raven(tmp_path, key):
    params, report = prepare(tmp_path, key)
    assert not Path(params['destination']).exists()
    assert 'SHA256:' in '\n'.join(report['warnings']) and 'independent allowlist' in '\n'.join(report['warnings'])
    assert not list(tmp_path.glob('*.sig'))
    applied = OPS.apply('research-portable', report['plan'], context())
    assert applied['data']['signed'] is True
    root = Path(params['destination'])
    trust = allowlist(tmp_path, key)
    output = V.verify(root, trust, PRINCIPAL)
    assert output['verified'] and output['identity'] == IDENTITY
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    folder = Path(__file__).resolve().parents[1] / 'docs/schemas'
    schemas = [json.loads((folder / name).read_text()) for name in ['signed-evidence-manifest.schema.json', 'evidence.schema.json']]
    registry = Registry().with_resources((item['$id'], Resource.from_contents(item)) for item in schemas)
    Draft202012Validator(schemas[0], registry=registry).validate(json.loads((root/S.MANIFEST).read_text()))
    assert not output['scientific_validity_established'] and not output['trusted_timestamp_established']
    # Copy only the stdlib verifier; no import of Raven or site packages.
    isolated = tmp_path / 'trusted verifier.py'
    shutil.copyfile(V.__file__, isolated)
    result = subprocess.run([sys.executable, '-I', '-S', str(isolated), str(root), '--allowed-signers', str(trust), '--principal', PRINCIPAL],
                            capture_output=True, text=True, cwd='/tmp')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['verified']
    private_marker = b'OPENSSH PRIVATE KEY'
    assert all(private_marker not in path.read_bytes() for path in root.iterdir())
    assert not any(str(key).encode() in path.read_bytes() for path in root.iterdir())


@pytest.mark.parametrize('target', ['raven-evidence.json', 'ro-crate-metadata.json', '.raven-receipt.json', 'signed-manifest.json', 'signed-manifest.sig'])
def test_modification_and_substitution_fail(tmp_path, key, target):
    root, trust = signed(tmp_path, key)
    path = root / target
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b'123', b'999') if b'123' in raw else raw + b'changed')
    with pytest.raises((ValueError, OSError)):
        V.verify(root, trust, PRINCIPAL)


def test_wrong_signer_namespace_and_trust_key_fail(tmp_path, key):
    root, trust = signed(tmp_path, key)
    with pytest.raises(ValueError, match='verification failed'):
        V.verify(root, trust, 'someone-else')
    original_signature = (root / S.SIGNATURE).read_bytes()
    manifest = (root / S.MANIFEST).read_bytes()
    wrong = subprocess.run(['ssh-keygen', '-Y', 'sign', '-n', 'unrelated.namespace', '-f', str(key)], input=manifest, capture_output=True, check=True)
    (root / S.SIGNATURE).write_bytes(wrong.stdout)
    with pytest.raises(ValueError, match='verification failed'):
        V.verify(root, trust, PRINCIPAL)
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(tmp_path/'other')], check=True)
    (root / S.SIGNATURE).write_bytes(original_signature)
    trust.write_text(PRINCIPAL + ' ' + (tmp_path/'other.pub').read_text())
    with pytest.raises(ValueError, match='verification failed'):
        V.verify(root, trust, PRINCIPAL)


def test_key_changed_between_review_and_apply_refused(tmp_path, key):
    params, report = prepare(tmp_path, key)
    key.unlink()
    Path(str(key) + '.pub').unlink()
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
    with pytest.raises(ValueError, match='changed after review'):
        OPS.apply('research-portable', report['plan'], context())
    assert not Path(params['destination']).exists()


def test_bundled_key_is_not_an_independent_trust_root(tmp_path, key):
    root, trust = signed(tmp_path, key)
    bundled = root / 'allowed_signers'
    shutil.copyfile(trust, bundled)
    with pytest.raises(ValueError, match='independent'):
        V.verify(root, bundled, PRINCIPAL)
    assert V.verify(root, trust, PRINCIPAL)['unsigned_extra_files'] == ['allowed_signers']


def test_unavailable_tool_does_not_break_unsigned_export(tmp_path, monkeypatch):
    monkeypatch.setattr(S.shutil, 'which', lambda name: None)
    with pytest.raises(ValueError, match='unavailable'):
        S.preview('/not-read', PRINCIPAL)
    params, report = prepare(tmp_path)
    result = OPS.apply('research-portable', report['plan'], context())
    assert result['data']['signed'] is False and not (Path(params['destination'])/S.SIGNATURE).exists()


def test_cancelled_signing_does_not_publish(tmp_path, key):
    params, report = prepare(tmp_path, key)
    cancel = threading.Event(); cancel.set()
    with pytest.raises(ValueError, match='cancel'):
        OPS.apply('research-portable', report['plan'], context(cancel=cancel))
    assert not Path(params['destination']).exists()


def test_symlinked_or_nonprivate_key_refused(tmp_path, key):
    linked = tmp_path / 'key-link'
    linked.symlink_to(key)
    with pytest.raises(OSError):
        S.preview(linked, PRINCIPAL)
    key.chmod(0o644)
    with pytest.raises(ValueError, match='0600'):
        S.preview(key, PRINCIPAL)


def test_allowlist_atomic_replacement_during_verify_is_detected(tmp_path, key, monkeypatch):
    root, trust = signed(tmp_path, key)
    invoke = V.subprocess.run
    def replacement(*args, **kwargs):
        result = invoke(*args, **kwargs)
        new = trust.with_suffix('.new')
        new.write_text('# revoked\n')
        new.replace(trust)
        return result
    monkeypatch.setattr(V.subprocess, 'run', replacement)
    with pytest.raises(ValueError, match='trust changed'):
        V.verify(root, trust, PRINCIPAL)


def test_agent_public_key_signing_path(tmp_path, key, monkeypatch):
    if not shutil.which('ssh-agent') or not shutil.which('ssh-add'):
        pytest.skip('OpenSSH agent tools unavailable')
    import time
    socket = tmp_path / 'agent.sock'
    agent = subprocess.Popen(['ssh-agent', '-D', '-a', str(socket)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if socket.exists():
                break
            time.sleep(.01)
        monkeypatch.setenv('SSH_AUTH_SOCK', str(socket))
        subprocess.run(['ssh-add', str(key)], check=True, capture_output=True)
        params, report = prepare(tmp_path, Path(str(key) + '.pub'))
        assert report['plan']['payload']['signer']['key_source'] == 'ssh-agent via public file'
        OPS.apply('research-portable', report['plan'], context())
        assert V.verify(params['destination'], allowlist(tmp_path, key), PRINCIPAL)['verified']
    finally:
        agent.terminate()
        agent.wait(timeout=5)


from test_operations_sequences import operation_dashboard


def test_operation_palette_keyboard_review_and_apply_are_required(operation_dashboard, tmp_path, key):
    import shlex
    app = operation_dashboard.app
    params, _report = prepare(tmp_path, key)
    app.run_command('ops research-portable')
    app.run_command('ops field signing_principal')
    app.handle('esc')
    assert app.ops_state['editing'] is None
    app.run_command('ops run research-portable ' + ' '.join(name + '=' + shlex.quote(value) for name, value in params.items()))
    assert app.ops_state['result']['status'] == 'ok', app.ops_state['result']
    app.run_command('ops apply')
    assert not Path(params['destination']).exists()
    app.run_command('ops review')
    operation_dashboard.draw(80, 24)
    assert not app.ops_state['review_truncated']
    assert app.ops_state['confirm_digest']
    app.run_command('ops apply')
    assert Path(params['destination']).exists(), app.ops_state['result']
    assert V.verify(params['destination'], allowlist(tmp_path, key), PRINCIPAL)['verified']


def test_atomic_key_replacement_during_sign_cannot_publish(tmp_path, key, monkeypatch):
    params, report = prepare(tmp_path, key)
    invoke = S._invoke
    def replacement(arguments, **kwargs):
        result = invoke(arguments, **kwargs)
        if arguments[:2] == ['-Y', 'sign']:
            moved = key.with_suffix('.old')
            key.rename(moved)
            key.write_bytes(moved.read_bytes())
            key.chmod(0o600)
        return result
    monkeypatch.setattr(S, '_invoke', replacement)
    with pytest.raises(ValueError, match='changed during'):
        OPS.apply('research-portable', report['plan'], context())
    assert not Path(params['destination']).exists()


@pytest.mark.parametrize('fault', ['path', 'oversize', 'signer', 'identity', 'receipt'])
def test_authenticated_manifest_still_requires_safe_consistent_contract(tmp_path, key, fault):
    root, trust = signed(tmp_path, key)
    manifest = json.loads((root/S.MANIFEST).read_text())
    if fault == 'path':
        manifest['files']['../elsewhere'] = manifest['files'].pop('ro-crate-metadata.json')
    elif fault == 'oversize':
        manifest['files']['raven-evidence.json']['size'] = 10**12
    elif fault == 'signer':
        manifest['signer']['fingerprint'] = 'SHA256:' + 'A'*43
    elif fault == 'identity':
        manifest['identity']['attempt'] = 'another-attempt'
    else:
        manifest['publication_receipt']['evidence_digest'] = '0'*64
    raw = E.canonical(manifest) + b'\n'
    result = subprocess.run(['ssh-keygen', '-Y', 'sign', '-n', S.NAMESPACE, '-f', str(key)], input=raw, capture_output=True, check=True)
    (root/S.MANIFEST).write_bytes(raw)
    (root/S.SIGNATURE).write_bytes(result.stdout)
    with pytest.raises(ValueError):
        V.verify(root, trust, PRINCIPAL)


def test_artifact_symlink_is_not_followed(tmp_path, key):
    root, trust = signed(tmp_path, key)
    artifact = root/'raven-evidence.json'
    saved = tmp_path/'outside.json'
    artifact.rename(saved)
    artifact.symlink_to(saved)
    with pytest.raises(OSError):
        V.verify(root, trust, PRINCIPAL)


def test_unbounded_extra_directory_entries_are_rejected(tmp_path, key):
    root, trust = signed(tmp_path, key)
    for index in range(129):
        (root / ('extra-' + str(index))).touch()
    with pytest.raises(ValueError, match='128-entry'):
        V.verify(root, trust, PRINCIPAL)


def test_signing_timeout_does_not_publish(tmp_path, key, monkeypatch):
    params, report = prepare(tmp_path, key)
    invoke = S.subprocess.run
    def timeout(command, **kwargs):
        if '-Y' in command and '-f' in command:
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        return invoke(command, **kwargs)
    monkeypatch.setattr(S.subprocess, 'run', timeout)
    with pytest.raises(ValueError, match='10-second'):
        OPS.apply('research-portable', report['plan'], context())
    assert not Path(params['destination']).exists()


@pytest.mark.parametrize('fault', ['writable', 'symlink'])
def test_independent_trust_requires_safe_ancestors(tmp_path, key, fault):
    root, trust = signed(tmp_path, key)
    parent = tmp_path / 'trust-directory'
    parent.mkdir()
    moved = parent / trust.name
    trust.rename(moved)
    if fault == 'writable':
        parent.chmod(0o777)
    else:
        link = tmp_path / 'trust-link'
        link.symlink_to(parent, target_is_directory=True)
        moved = link / moved.name
    with pytest.raises((ValueError, OSError)):
        V.verify(root, moved, PRINCIPAL)


def test_authenticated_deep_json_returns_a_bounded_diagnostic(tmp_path, key, capsys):
    root, trust = signed(tmp_path, key)
    manifest = json.loads((root/S.MANIFEST).read_text())
    value = None
    for _ in range(80):
        value = [value]
    manifest['identity'] = value
    raw = json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode() + b'\n'
    result = subprocess.run(['ssh-keygen', '-Y', 'sign', '-n', S.NAMESPACE, '-f', str(key)], input=raw, capture_output=True, check=True)
    (root/S.MANIFEST).write_bytes(raw)
    (root/S.SIGNATURE).write_bytes(result.stdout)
    assert V.main([str(root), '--allowed-signers', str(trust), '--principal', PRINCIPAL]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['verified'] is False
    assert 'complexity limit' in result['error']
