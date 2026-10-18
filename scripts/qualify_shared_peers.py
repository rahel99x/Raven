"""Actual multi-UID socket qualification inside a disposable Linux container.

Run as fixture root in an isolated container with an existing `raven` test user.
No host configuration or scheduler mutation is performed. Data is synthetic.
"""
import json
import os
from pathlib import Path
import pwd
import subprocess
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from raven import shared_host_service as H
from raven.services_io import write_json_file


def main():
    if os.getuid() != 0:
        raise SystemExit('Run only as root inside the disposable qualification container')
    user = pwd.getpwnam('raven')
    with tempfile.TemporaryDirectory(prefix='rv-peer-qualification-') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        policy_file = root/'policy.json'
        policy = {'schema': H.POLICY, 'id': 'isolated-peer-qualification', 'socket_gid': user.pw_gid,
                  'interval_seconds': 60, 'max_records': 1000,
                  'principals': {'0': {'user': 'root', 'accounts': [], 'site': False},
                                 str(user.pw_uid): {'user': user.pw_name, 'accounts': [], 'site': False}}}
        def attempt(jid):
            return {'cluster': 'fixture', 'job_id': jid, 'submit_time': 1792279000,
                    'start_time': 1792279060, 'restart_count': 0}
        policy['project_memberships'] = {'ProjectA': [attempt('3')], 'ProjectB': [attempt('4')]}
        write_json_file(policy_file, policy)
        server = H.SharedServer(root/'service', policy_file)
        server.publish_source({'jobs': [{'id': '1', 'user': 'root', 'account': 'admin', 'name': 'root-only', 'command': 'PRIVATE-COMMAND'},
                                       {'id': '2', 'user': user.pw_name, 'account': 'own', 'name': 'user-only'},
                                       {'id': '3', 'user': 'other', 'account': 'LabX', 'name': 'project-a', 'cluster': 'fixture', '_attempt': attempt('3')},
                                       {'id': '4', 'user': 'other', 'account': 'LabX', 'name': 'project-b', 'cluster': 'fixture', '_attempt': attempt('4')}],
                               'finished': [], 'queue_observed_at': time.time(), 'accounting_observed_at': time.time(),
                               'source_errors': {'jobs': '', 'finished': ''}})
        stop = threading.Event()
        def pump():
            while not stop.is_set():
                server.poll(.01)
        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        def read_user():
            command = ['runuser', '-u', user.pw_name, '--', sys.executable, '-m', 'raven.shared_host_service', 'query', str(server.root), '--server-uid', '0']
            return subprocess.run(command, cwd=str(Path(__file__).resolve().parents[1]), capture_output=True, text=True, timeout=5)
        def replace_policy():
            write_json_file(policy_file, policy)
            generation = H.load_policy(policy_file)[1]
            deadline = time.monotonic()+2
            while server.generation != generation and time.monotonic() < deadline:
                time.sleep(.01)
            assert server.generation == generation
        try:
            own = H.SharedClient(server.root, server_uid=0).query()
            assert [row['id'] for row in own['snapshot']['jobs']] == ['1']
            deadline = time.monotonic()+2
            while server.clients and time.monotonic() < deadline:
                time.sleep(.01)
            idle = []
            try:
                for _ in range(4):
                    peer = socket.socket(socket.AF_UNIX)
                    peer.connect(str(server.root/'session.sock'))
                    idle.append(peer)
                deadline = time.monotonic()+2
                while len(server.clients) < 4 and time.monotonic() < deadline:
                    time.sleep(.01)
                assert len(server.clients) == 4
                extra = socket.socket(socket.AF_UNIX)
                extra.settimeout(2)
                extra.connect(str(server.root/'session.sock'))
                idle.append(extra)
                assert extra.recv(1) == b''
                other_under_flood = read_user()
                assert other_under_flood.returncode == 0, other_under_flood.stderr
                assert [row['id'] for row in json.loads(other_under_flood.stdout)['snapshot']['jobs']] == ['2']
            finally:
                for peer in idle:
                    peer.close()
            other = read_user()
            assert other.returncode == 0, other.stderr
            assert [row['id'] for row in json.loads(other.stdout)['snapshot']['jobs']] == ['2']
            assert 'root-only' not in other.stdout and 'PRIVATE-COMMAND' not in other.stdout
            policy['principals'][str(user.pw_uid)]['projects'] = ['ProjectA']
            replace_policy()
            project_a = read_user()
            assert project_a.returncode == 0, project_a.stderr
            assert [row['id'] for row in json.loads(project_a.stdout)['snapshot']['jobs']] == ['2', '3']
            assert 'ProjectB' not in project_a.stdout and 'project-b' not in project_a.stdout
            assert '_attempt' not in project_a.stdout
            policy['project_memberships']['ProjectA'] = []
            replace_policy()
            project_revoked = read_user()
            assert project_revoked.returncode == 0, project_revoked.stderr
            assert [row['id'] for row in json.loads(project_revoked.stdout)['snapshot']['jobs']] == ['2']
            policy['principals'][str(user.pw_uid)]['projects'] = ['ProjectB']
            replace_policy()
            project_b = read_user()
            assert project_b.returncode == 0, project_b.stderr
            assert [row['id'] for row in json.loads(project_b.stdout)['snapshot']['jobs']] == ['2', '4']
            assert 'ProjectA' not in project_b.stdout and 'project-a' not in project_b.stdout
            policy['principals'][str(user.pw_uid)]['accounts'] = ['LabX']
            replace_policy()
            lab = read_user()
            assert lab.returncode == 0, lab.stderr
            assert [row['id'] for row in json.loads(lab.stdout)['snapshot']['jobs']] == ['2', '3', '4']
            del policy['principals'][str(user.pw_uid)]
            write_json_file(policy_file, policy)
            deadline = time.monotonic()+2
            while server.authorized(user.pw_uid) and time.monotonic() < deadline:
                time.sleep(.01)
            revoked = read_user()
            assert revoked.returncode != 0 and 'root-only' not in revoked.stdout
            assert H.SharedClient(server.root, server_uid=0).query()['snapshot']['jobs'][0]['id'] == '1'
            print(json.dumps({'qualification': 'actual Linux SO_PEERCRED across two Unix UIDs in disposable container',
                              'uids': [0, user.pw_uid], 'own_user_isolation': True, 'explicit_lab_grant': True,
                              'revocation': True, 'private_fields_excluded': True, 'fixture_data': 'synthetic scheduler records',
                              'other_uid_reads_during_idle_connection_flood': True,
                              'projects_sharing_lab_account_isolated': True, 'project_membership_revocation': True,
                              'project_principal_grant_replacement': True,
                              'production_site_security_qualified': False}, indent=2))
        finally:
            stop.set()
            thread.join(timeout=2)
            server.close()


if __name__ == '__main__':
    main()
