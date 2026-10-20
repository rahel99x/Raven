"""Private, configuration-aware OpenSSH multiplexing for read and action clients.

Resolve SSH's own configuration before each command; do not duplicate Host/Match/Include
parser. SSH still owns host-key verification and authentication. No command is
retried here: an ambiguous action must never be replayed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import socket
import tempfile

from .config_trust import directory_fd

_FALLBACK_ROOTS = {}


class MultiplexUnavailable(ValueError):
    """SSH can still run, but its configuration could not safely be scoped."""


def _quote(value):
    if any(c in value for c in '\r\n\0'):
        raise ValueError('SSH configuration path contains a control character.')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _config_options(options):
    """Extract -F, retaining all other command-line options and their ordering."""
    result, config, index = [], None, 0
    while index < len(options):
        item = options[index]
        if item == '-F':
            if index + 1 == len(options):
                raise ValueError('ssh_options -F requires a configuration path.')
            config = options[index + 1]
            index += 2
        elif item.startswith('-F') and len(item) > 2:
            config = item[2:]
            index += 1
        else:
            result.append(item)
            index += 1
    return result, config


def _private_root():
    # A short path avoids sockaddr_un's 108-byte Linux limit even with a long
    # home directory. A fixed uid path is safe only after no-follow/owner checks.
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    base = Path(runtime) if runtime and Path(runtime).is_absolute() else Path(tempfile.gettempdir())
    root = base / ('raven-ssh-' + str(os.getuid()))
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    try:
        with directory_fd(root, private=True):
            pass
        if len(os.fsencode(root)) + 1 + 32 + 17 >= 104:
            raise MultiplexUnavailable("SSH socket root is too long")
    except (OSError, ValueError):
        key = os.getpid()
        root = _FALLBACK_ROOTS.get(key)
        if root is None:
            root = Path(tempfile.mkdtemp(prefix='raven-ssh-', dir='/tmp'))
            _FALLBACK_ROOTS[key] = root
        with directory_fd(root, private=True):
            pass
    return root


def prepare(target, options):
    """Return a scoped argv prefix and diagnostic socket path.

    A temporary config includes the original config(s), then supplies defaults.
    Thus an explicit ControlMaster=no or ControlPath=none remains an opt-out;
    command-line options keep precedence. Custom enabled paths are narrowed to
    Raven's private, effective-configuration scope to avoid accidental reuse.
    """
    options, selected = _config_options(list(options))
    root = _private_root()
    includes = [os.path.expanduser('~/.ssh/config'), '/etc/ssh/ssh_config'] if selected is None else ([] if selected == 'none' else [os.path.abspath(os.path.expanduser(selected))])
    body = ''.join('Include ' + _quote(path) + '\n' for path in includes)
    # ControlPath is absent intentionally; ssh -G reports none when no path was
    # supplied, and explicit none must be distinguished with a sentinel default.
    sentinel = str(root / 'unresolved')
    body += 'Host *\n  ControlMaster auto\n  ControlPersist 600\n  ControlPath ' + _quote(sentinel) + '\n'
    fd, wrapper = tempfile.mkstemp(prefix='config-', dir=root)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(body)
        prefix = ['ssh', *options, '-F', wrapper]
        result = subprocess.run([*prefix, '-G', '--', target], capture_output=True, text=True, timeout=6)
        if result.returncode or len(result.stdout) > 1024 * 1024:
            raise MultiplexUnavailable('OpenSSH could not resolve scoped multiplexing; using separate connections. Check SSH configuration ownership and ssh_options.')
        effective = dict(line.split(' ', 1) for line in result.stdout.splitlines() if ' ' in line)
        enabled = effective.get('controlmaster', 'false') not in ('false', 'no') and effective.get('controlpath', 'none') != 'none'
        # Preserve repeated identity files/forwards and every effective security
        # or proxy option in the digest, not just the last dictionary entry.
        identity = '\n'.join(line for line in result.stdout.splitlines() if not line.startswith('controlpath '))
        # Shared temporary mounts must not make two client hosts unlink or
        # attach to one another's socket names. Boot identity also separates
        # stale sockets left by a previous boot of this client.
        try:
            boot = Path('/proc/sys/kernel/random/boot_id').read_text()[:128].strip()
        except OSError:
            boot = ''
        client = [socket.gethostname(), boot]
        key = hashlib.sha256(json.dumps([client, target, options, identity], ensure_ascii=True).encode()).hexdigest()[:32]
        control_path = str(root / key) if enabled else ''
        if enabled and len(os.fsencode(control_path)) + 17 >= 104:
            raise MultiplexUnavailable('SSH socket path exceeds its platform bound; using a plain connection')
        # Resolve every invocation so edited identity/proxy/security settings
        # select a new socket. Do not feed unknown ssh -G keys back as config.
        # This adds one bounded local ssh process, not an authentication.
        defaults = []
        if enabled:
            defaults = ['-o', 'ControlMaster=' + effective['controlmaster'], '-o', 'ControlPath=' + control_path,
                        '-o', 'ControlPersist=' + effective.get('controlpersist', '600')]
        # For explicit opt-outs, omit generated defaults and preserve the user's
        # original -F exactly. Otherwise OpenSSH still rereads original files.
        config_args = [] if selected is None else ['-F', selected]
        return ['ssh', *defaults, *options, *config_args, *(['-S', control_path] if enabled else [])], control_path
    finally:
        os.unlink(wrapper)
