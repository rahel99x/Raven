#!/usr/bin/env python3
"""Standalone signed-evidence verifier: Python stdlib plus OpenSSH only.

Run this file from a trusted installation, not executable code inside an
unverified bundle. An independently supplied allowed-signers file is mandatory.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys

NAMESPACE = 'raven.evidence.v1'
SCHEMA = 'raven.signed-evidence-manifest/v1'
MAX_MANIFEST = 256 * 1024
MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024


@contextmanager
def _trust_parent(path):
    """Pin every trusted ancestor without importing Raven's implementation."""
    system_uid = os.stat('/').st_uid
    owners = {0, os.getuid(), system_uid}
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parent.parts[1:]:
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
            info = os.fstat(fd)
            # A root-owned sticky /tmp is an allowed ancestor. The final
            # allowlist still requires current-user/root ownership and no writes
            # by another user; symlinked ancestors are never followed.
            sticky = info.st_uid in {0, system_uid} and info.st_mode & stat.S_ISVTX
            if info.st_uid not in owners or (info.st_mode & 0o022 and not sticky):
                raise ValueError('Independent signer trust has an untrusted or writable ancestor: ' + component)
        yield fd
    finally:
        os.close(fd)


def _read(directory, name, limit):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', name) or name in ('.', '..'):
        raise ValueError('Manifest files must be simple relative names, without traversal or directories.')
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('Bundle input must be a bounded regular file: ' + name)
        chunks, count = [], 0
        while True:
            block = os.read(fd, min(65536, limit + 1 - count))
            if not block:
                break
            chunks.append(block)
            count += len(block)
            if count > limit:
                raise ValueError('Bundle input grew past its bound: ' + name)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if signature(before) != signature(os.fstat(fd)) or signature(before) != signature(os.stat(name, dir_fd=directory, follow_symlinks=False)):
            raise ValueError('Bundle input changed during verification: ' + name)
        return b''.join(chunks)
    finally:
        os.close(fd)



def _entries(directory):
    names = set()
    with os.scandir(directory) as entries:
        for entry in entries:
            if len(names) >= 128:
                raise ValueError('Bundle directory exceeds its 128-entry inspection limit.')
            names.add(entry.name)
    return names

def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate manifest key: ' + key)
            value[key] = item
        return value
    def constant(value):
        raise ValueError('Nonfinite JSON value: ' + value)
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (RecursionError, UnicodeError) as exc:
        raise ValueError('Invalid or excessively nested bundle JSON.') from exc
    # Bound nesting before reserialization or equality comparisons. Iterators
    # keep traversal memory proportional to depth, including wide metric arrays.
    stack, nodes = [iter((value,))], 0
    while stack:
        item = next(stack[-1], StopIteration)
        if item is StopIteration:
            stack.pop()
            continue
        nodes += 1
        if nodes > 200000 or len(stack) > 64:
            raise ValueError('Bundle JSON exceeds its 64-level/200000-value complexity limit.')
        if isinstance(item, dict):
            stack.append(iter(item.values()))
        elif isinstance(item, list):
            stack.append(iter(item))
    return value


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()
    except RecursionError as exc:
        raise ValueError('Bundle JSON exceeds the canonical serialization depth limit.') from exc


def _signature_public(signature):
    lines = signature.strip().splitlines()
    if not lines or lines[0] != b'-----BEGIN SSH SIGNATURE-----' or lines[-1] != b'-----END SSH SIGNATURE-----':
        raise ValueError('Expected a detached OpenSSH signature.')
    data = base64.b64decode(b''.join(lines[1:-1]), validate=True)
    if data[:10] != b'SSHSIG\x00\x00\x00\x01' or len(data) < 14:
        raise ValueError('Unsupported OpenSSH signature format.')
    size = struct.unpack('>I', data[10:14])[0]
    if not 1 <= size <= 16384 or len(data) < 14 + size:
        raise ValueError('Invalid signature public-key size.')
    return data[14:14 + size]


def verify(bundle, allowed_signers, principal):
    """Authenticate the manifest first, then check its complete declared payload."""
    root = Path(bundle).absolute()
    trust = Path(allowed_signers).absolute()
    if trust.resolve().is_relative_to(root.resolve()):
        raise ValueError('Use an independent allowed-signers file outside the exported bundle; bundled keys are descriptive only.')
    if not isinstance(principal, str) or not re.fullmatch(r'[A-Za-z0-9_@.+:/-]{1,256}', principal):
        raise ValueError('Supply one explicit expected signer principal.')
    tool = shutil.which('ssh-keygen')
    if not tool:
        raise ValueError('OpenSSH ssh-keygen with -Y verify support is required.')
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    trust_fd = None
    trust_parent_context = _trust_parent(trust)
    trust_parent = None
    signature_fd = None
    try:
        if '.raven-incomplete' in _entries(root_fd):
            raise ValueError('Bundle publication is incomplete.')
        trust_parent = trust_parent_context.__enter__()
        trust_fd = os.open(trust.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=trust_parent)
        trust_info = os.fstat(trust_fd)
        if not stat.S_ISREG(trust_info.st_mode) or trust_info.st_uid not in (0, os.getuid()) or trust_info.st_mode & 0o022 or trust_info.st_size > MAX_MANIFEST:
            raise ValueError('Independent allowed-signers must be an owned/root-owned bounded regular file without group/world writes.')
        manifest_raw = _read(root_fd, 'signed-manifest.json', MAX_MANIFEST)
        signature = _read(root_fd, 'signed-manifest.sig', 16384)
        # Pin the exact detached signature supplied to OpenSSH, without relying
        # on a pathname that another writer can replace after inspection.
        import tempfile
        with tempfile.TemporaryFile() as pinned_signature:
            pinned_signature.write(signature)
            pinned_signature.flush()
            signature_fd = pinned_signature.fileno()
            result = subprocess.run([tool, '-Y', 'verify', '-f', f'/proc/self/fd/{trust_fd}', '-I', principal,
                                     '-n', NAMESPACE, '-s', f'/proc/self/fd/{signature_fd}'], input=manifest_raw,
                                    capture_output=True, timeout=10, env=dict(os.environ, LC_ALL='C'),
                                    pass_fds=(trust_fd, signature_fd))
        if result.returncode:
            raise ValueError('Signature verification failed: wrong signer, namespace, trust key, or modified manifest/signature.')
        trust_identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if trust_identity(trust_info) != trust_identity(os.fstat(trust_fd)) or trust_identity(trust_info) != trust_identity(os.stat(trust.name, dir_fd=trust_parent, follow_symlinks=False)):
            raise ValueError('Independent signer trust changed during verification; retry against a stable allowlist.')
        manifest = _json(manifest_raw)
        expected = {'schema', 'namespace', 'identity', 'publication_receipt', 'signer', 'files', 'claims'}
        if not isinstance(manifest, dict) or set(manifest) != expected or manifest['schema'] != SCHEMA or manifest['namespace'] != NAMESPACE:
            raise ValueError('Unsupported signed evidence manifest contract.')
        if manifest_raw != _canonical(manifest) + b'\n':
            raise ValueError('Signed manifest is not in its canonical representation.')
        signer = manifest['signer']
        if not isinstance(signer, dict) or set(signer) != {'principal', 'fingerprint', 'public_key'} or signer['principal'] != principal:
            raise ValueError('Manifest signer does not match the independently requested principal.')
        public = _signature_public(signature)
        expected_fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(public).digest()).decode().rstrip('=')
        if not isinstance(signer['public_key'], str):
            raise ValueError('Expected one descriptive public key.')
        declared = signer['public_key'].split()
        public_type_size = struct.unpack('>I', public[:4])[0] if len(public) >= 4 else 0
        if len(declared) != 2 or declared[0].encode() != public[4:4 + public_type_size] or base64.b64decode(declared[1], validate=True) != public or signer['fingerprint'] != expected_fingerprint:
            raise ValueError('Descriptive signer metadata differs from the actual signing key.')
        files = manifest['files']
        if not isinstance(files, dict) or not 1 <= len(files) <= 64 or 'raven-evidence.json' not in files:
            raise ValueError('Signed bundle must declare its evidence and a bounded file set.')
        total, evidence = 0, None
        for name, record in files.items():
            if not isinstance(record, dict) or set(record) != {'sha256', 'size'} or type(record['size']) is not int or not 0 <= record['size'] <= MAX_FILE or not isinstance(record['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', record['sha256']):
                raise ValueError('Invalid artifact digest or size.')
            total += record['size']
            if total > MAX_TOTAL:
                raise ValueError('Signed payload exceeds 64 MiB.')
            raw = _read(root_fd, name, record['size'])
            if len(raw) != record['size'] or hashlib.sha256(raw).hexdigest() != record['sha256']:
                raise ValueError('Signed artifact changed: ' + name)
            if name == 'raven-evidence.json':
                evidence = _json(raw)
        publication = _json(_read(root_fd, '.raven-receipt.json', MAX_MANIFEST))
        if publication != manifest['publication_receipt']:
            raise ValueError('Publication receipt differs from the signed manifest.')
        if not isinstance(evidence, dict) or not isinstance(publication, dict) or evidence.get('identity') != manifest['identity'] or publication.get('evidence_digest') != hashlib.sha256(_canonical(evidence)).hexdigest():
            raise ValueError('Signed identity/receipt does not bind this evidence document.')
        if manifest['claims'] != {'authenticity': 'possession of the independently trusted signing key', 'scientific_validity': False, 'trusted_timestamp': False}:
            raise ValueError('Unsupported authenticity claims.')
        known = set(files) | {'signed-manifest.json', 'signed-manifest.sig', '.raven-receipt.json'}
        extras = sorted(_entries(root_fd) - known)
        return {'verified': True, 'namespace': NAMESPACE, 'principal': principal, 'fingerprint': expected_fingerprint,
                'identity': manifest['identity'], 'verified_files': sorted(files), 'unsigned_extra_files': extras,
                'scientific_validity_established': False, 'trusted_timestamp_established': False}
    finally:
        if trust_fd is not None:
            os.close(trust_fd)
        if trust_parent is not None:
            trust_parent_context.__exit__(None, None, None)
        os.close(root_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle')
    parser.add_argument('--allowed-signers', required=True, help='Independent trusted allowlist outside the bundle')
    parser.add_argument('--principal', required=True, help='Expected principal, chosen independently of bundle metadata')
    args = parser.parse_args(argv)
    try:
        value = verify(args.bundle, args.allowed_signers, args.principal)
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'verified': False, 'error': str(exc)}))
        return 1
    print(json.dumps(value, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
