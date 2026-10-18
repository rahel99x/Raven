"""Qualify installed read-only session IPC over an isolated loopback sshd.

Generated keys, venv and daemon are disposable. This uses simulated Slurm data,
but real wheel installation, Unix credentials, SSH, process death and reconnect.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import socket
import subprocess
import sys
import tempfile
import time
import venv

ROOT = Path(os.environ.get('RAVEN_REVIEW_SOURCE', '/workspace/Raven')).resolve()
sys.path.insert(0, str(ROOT))
from raven import session_contract as C
from raven.session_service import RemoteClient
from raven.remote import SshBackend


def wait_for(predicate, process, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('Qualification child exited before becoming ready')
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(.05)
    raise TimeoutError('Qualification child did not become ready')


def qualify(*, remapped_root=False, wheel_path=None):
    with tempfile.TemporaryDirectory(prefix='rv-session-qual-') as temporary:
        root = Path(temporary)
        env = root/'installed'
        venv.EnvBuilder(with_pip=True).create(env)
        python = env/'bin/python'
        wheel = Path(wheel_path).resolve(strict=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '--no-deps', '--no-index', str(wheel)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for name in ('host', 'client'):
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(root/name)], check=True)
        (root/'authorized').write_bytes((root/'client.pub').read_bytes())
        (root/'authorized').chmod(0o600)
        with socket.socket() as connection:
            connection.bind(('127.0.0.1', 0))
            port = connection.getsockname()[1]
        username = pwd.getpwuid(os.getuid()).pw_name
        config = root/'sshd.conf'
        config.write_text(f'''Port {port}
ListenAddress 127.0.0.1
HostKey {root}/host
PidFile {root}/sshd.pid
AuthorizedKeysFile {root}/authorized
StrictModes {'no' if remapped_root else 'yes'}
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PermitRootLogin no
AllowUsers {username}
LogLevel VERBOSE
''')
        public = (root/'host.pub').read_text().split()
        (root/'known_hosts').write_text(f'[127.0.0.1]:{port} {public[0]} {public[1]}\n')
        options = ['-F', 'none', '-p', str(port), '-i', str(root/'client'), '-o', 'IdentitiesOnly=yes',
                   '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile='+str(root/'known_hosts'), '-o', 'ControlPersist=5']
        backend = SshBackend('127.0.0.1', username, options)
        daemon = server = None
        with (root/'sshd.log').open('wb') as log, (root/'daemon.log').open('wb') as daemon_log:
            try:
                server = subprocess.Popen(['/usr/sbin/sshd', '-D', '-e', '-f', str(config)], stdout=log, stderr=log)
                def connection_ready():
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        return True
                wait_for(connection_ready, server)
                service = root/'session'
                command = [str(python), '-m', 'raven.session_service', 'serve', str(service), '--demo']
                daemon = subprocess.Popen(command, cwd='/tmp', stdout=daemon_log, stderr=daemon_log)
                client = RemoteClient(service, backend, python=str(python))
                first = wait_for(lambda: (value if (value := client.query())['snapshot'].get('jobs') else None), daemon)
                assert first['snapshot']['jobs'] and first['mutations'] is False
                identity = client.session, client.epoch, client.cursor
                # Kill the owned daemon without cleanup: the new owner must
                # reconcile a stale socket under the released kernel lock.
                daemon.kill()
                daemon.wait(timeout=5)
                try:
                    client.query()
                except (OSError, ValueError, RuntimeError):
                    pass
                else:
                    raise AssertionError('Disconnected remote read unexpectedly succeeded')
                assert (client.session, client.epoch, client.cursor) == identity
                daemon = subprocess.Popen(command, cwd='/tmp', stdout=daemon_log, stderr=daemon_log)
                second = wait_for(client.query, daemon)
                assert second['session'] == identity[0] and second['epoch'] != identity[1]
                assert second['gap'] and 'restarted' in second['gap']['reason']
                wrong = C.request('hello')
                wrong['version'] = 999
                ok, diagnostic = backend.call([str(python), '-m', 'raven.session_service', 'bridge', str(service), '--request', C.encoded(wrong).decode()], timeout=5)
                assert not ok and 'version mismatch' in diagnostic
                module, _ = backend.run([str(python), '-c', 'import raven.session_service;print(raven.session_service.__file__)'], timeout=5)
                assert '/site-packages/raven/' in module and '/workspace/' not in module
                daemon.terminate()
                assert daemon.wait(timeout=8) == 0
                assert not (service/'session.sock').exists()
                return {'kind': 'actual installed wheel + loopback SSH + Unix IPC; simulated scheduler only',
                        'wheel': wheel.name, 'wheel_sha256': __import__('hashlib').sha256(wheel.read_bytes()).hexdigest(), 'installed_module': module.strip(), 'client_host_key_check': 'strict exact generated key',
                        'server_strict_modes': not remapped_root, 'same_session_after_crash': True,
                        'new_epoch_and_explicit_gap': True, 'failed_read_preserves_cursor': True,
                        'version_skew_rejected': True, 'normal_shutdown_removes_owned_socket': True,
                        'live_slurm_qualified': False, 'production_site_qualified': False}
            finally:
                if daemon is not None and daemon.poll() is None:
                    daemon.terminate()
                    try:
                        daemon.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
                        daemon.wait()
                if backend.control_path:
                    subprocess.run(['ssh', '-S', backend.control_path, '-O', 'exit', backend.target], capture_output=True, timeout=5)
                if server is not None:
                    server.terminate()
                    server.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--remapped-root-fixture', action='store_true')
    parser.add_argument('--wheel', type=Path, required=True)
    args = parser.parse_args()
    result = qualify(remapped_root=args.remapped_root_fixture, wheel_path=args.wheel)
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
