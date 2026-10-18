"""Explicit OpenSSH signatures for reviewed, portable evidence exports.

Only the public key and fingerprint leave this module. A signature establishes
key possession over these bytes, not scientific truth or a trusted timestamp.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

from . import evidence as E
from .config_trust import directory_fd

NAMESPACE = 'raven.evidence.v1'
SCHEMA = 'raven.signed-evidence-manifest/v1'
MANIFEST = 'signed-manifest.json'
SIGNATURE = 'signed-manifest.sig'
MAX_KEY = 1024 * 1024
MAX_MANIFEST = 256 * 1024


def _tool():
    path = shutil.which('ssh-keygen')
    if not path:
        raise ValueError('OpenSSH ssh-keygen is unavailable; install OpenSSH signing support or export without signing_key.')
    return path


def _invoke(arguments, *, input=None, key_fd=None, timeout=10):
    environment = dict(os.environ, LC_ALL='C', SSH_ASKPASS_REQUIRE='never')
    try:
        result = subprocess.run([_tool(), *arguments], input=input, stdin=subprocess.DEVNULL if input is None else None,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=environment,
                                pass_fds=() if key_fd is None else (key_fd,))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('OpenSSH signing operation failed or exceeded its 10-second limit; no bundle was published.') from exc
    if len(result.stdout) > MAX_MANIFEST or len(result.stderr) > 65536:
        raise ValueError('OpenSSH signing output exceeded its limit.')
    return result


def _identity(info):
    # dev_t/ino_t may be unsigned 64-bit (including imported overlay inodes).
    # These are opaque identity atoms, not worker-protocol measurement numbers.
    if type(info.st_dev) is not int or not 0 <= info.st_dev < 2**64 or type(info.st_ino) is not int or not 0 < info.st_ino < 2**64:
        raise ValueError('Signing key has an invalid bounded filesystem identity')
    return [str(info.st_dev), str(info.st_ino), info.st_size, info.st_mtime_ns, info.st_ctime_ns]


@contextmanager
def _key(path):
    path = Path(path).expanduser().absolute()
    with directory_fd(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_size > MAX_KEY:
                raise ValueError('Signing key must be an owned, regular file without group/world writes, at most 1 MiB.')
            # Only a single OpenSSH public-key line is eligible for readable mode.
            # Private key bytes are neither exported nor included in diagnostics.
            head = os.read(fd, min(info.st_size, 16384))
            public = head.startswith((b'ssh-', b'ecdsa-', b'sk-')) and b'\n' not in head.rstrip(b'\n')
            if not public and info.st_mode & 0o077:
                raise ValueError('Private signing key must have mode 0600; inspect permissions before correcting them.')
            os.lseek(fd, 0, os.SEEK_SET)
            yield fd, info, public, head if public else None
            if _identity(os.fstat(fd)) != _identity(info) or _identity(os.stat(path, follow_symlinks=False)) != _identity(info):
                raise ValueError('Signing key changed during use; review it again.')
        finally:
            os.close(fd)


def _describe(fd, info, public, head, principal):
    if not isinstance(principal, str) or not re.fullmatch(r'[A-Za-z0-9_@.+:/-]{1,256}', principal):
        raise ValueError('signing_principal must be one explicit printable principal without spaces or commas.')
    if public:
        key = b' '.join(head.split()[:2]).decode('ascii')
    else:
        result = _invoke(['-y', '-P', '', '-f', f'/proc/self/fd/{fd}'], key_fd=fd)
        if result.returncode:
            raise ValueError('Cannot derive this signing key. For an encrypted key, load it in ssh-agent and select its public .pub file; no passphrase is collected by Raven.')
        key = b' '.join(result.stdout.split()[:2]).decode('ascii')
    parts = key.split()
    if len(parts) != 2:
        raise ValueError('OpenSSH did not return one public signing key.')
    import base64
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError as exc:
        raise ValueError('Invalid OpenSSH public key.') from exc
    # Fingerprint is OpenSSH's SHA256 of the public key blob, without padding.
    fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')
    checked = _invoke(['-l', '-E', 'sha256', '-f', f'/proc/self/fd/{fd}'], key_fd=fd)
    if checked.returncode or fingerprint.encode() not in checked.stdout.split():
        raise ValueError('OpenSSH did not validate the public signing-key fingerprint.')
    return {'namespace': NAMESPACE, 'principal': principal, 'public_key': key,
            'fingerprint': fingerprint, 'key_identity': _identity(info),
            'key_source': 'ssh-agent via public file' if public else 'private file; not exported'}


def preview(path, principal):
    """Resolve public identity and support without creating any signature."""
    probe = _invoke(['-Y', 'sign', '-n', NAMESPACE])
    if b'missing key' not in probe.stderr:
        raise ValueError('Installed ssh-keygen does not expose the required -Y signing support; unsigned export remains available.')
    with _key(path) as value:
        return _describe(*value, principal)


def sign(path, reviewed, *, files, receipt, identity, cancel=None):
    if cancel is not None and cancel.is_set():
        raise ValueError('Signing cancelled before execution.')
    with _key(path) as value:
        current = _describe(*value, reviewed['principal'])
        if current != reviewed:
            raise ValueError('Signing key changed after review; prepare the export again.')
        public = {name: current[name] for name in ('principal', 'fingerprint', 'public_key')}
        manifest = {'schema': SCHEMA, 'namespace': NAMESPACE, 'identity': identity,
                    'publication_receipt': receipt, 'signer': public,
                    'files': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)} for name, data, _ in files},
                    'claims': {'authenticity': 'possession of the independently trusted signing key',
                               'scientific_validity': False, 'trusted_timestamp': False}}
        data = E.canonical(manifest) + b'\n'
        if len(data) > MAX_MANIFEST:
            raise ValueError('Signature manifest exceeds 256 KiB.')
        result = _invoke(['-Y', 'sign', '-n', NAMESPACE, '-f', f'/proc/self/fd/{value[0]}'], input=data, key_fd=value[0])
        if result.returncode or not result.stdout.startswith(b'-----BEGIN SSH SIGNATURE-----'):
            raise ValueError('OpenSSH could not sign the reviewed manifest; check key/agent availability. No bundle was published.')
    if cancel is not None and cancel.is_set():
        raise ValueError('Signing cancelled; no bundle was published.')
    return manifest, [(MANIFEST, data, False), (SIGNATURE, result.stdout, False)]
