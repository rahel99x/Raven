import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from raven import session_contract as C, session_service as S


@pytest.fixture
def directory():
    with tempfile.TemporaryDirectory(prefix='rv-session-') as root:
        yield Path(root)


@pytest.fixture
def server(directory):
    service = S.Server(directory, {'user': 'alice', 'profile': 'lab'})
    service.publish({'jobs': [{'id': '1', 'state': 'RUNNING'}], 'finished': []})
    stop = threading.Event()
    thread = threading.Thread(target=lambda: pump(service, stop), daemon=True)
    thread.start()
    try:
        yield service
    finally:
        stop.set()
        thread.join(timeout=2)
        service.close()


def pump(server, stop):
    while not stop.is_set():
        server.poll(.01)


def test_real_unix_client_and_stable_observation_without_collection(server):
    first = S.Client(server.root)
    one = first.query('hello')
    assert one['snapshot']['jobs'][0]['id'] == '1'
    assert one['capabilities'] == ['snapshot', 'events']
    assert one['mutations'] is False
    for _ in range(10):
        other = S.Client(server.root).query()
        assert other['session'] == one['session']
        assert other['sequence'] == one['sequence']
    assert server.metrics['queries'] == 11
    assert (server.root/S.SOCKET).stat().st_mode & 0o777 == 0o600
    assert server.root.stat().st_mode & 0o777 == 0o700


def test_ownership_refuses_second_service_before_collection(server):
    with pytest.raises(OSError, match='Another Raven writer'):
        S.Server(server.root, {'user': 'alice', 'profile': 'lab'})


def test_restart_preserves_session_cursor_and_marks_observations_stale(directory):
    first = S.Server(directory, {'user': 'alice'})
    first.publish({'jobs': [{'id': '123', 'state': 'RUNNING'}]})
    old = first.observations.query(C.request('hello'))
    first.close()
    second = S.Server(directory, {'user': 'alice'})
    try:
        new = second.observations.query(C.request('events', session=old['session'], epoch=old['epoch'], cursor=old['sequence']))
        assert new['session'] == old['session']
        assert new['epoch'] != old['epoch']
        assert new['snapshot'] == old['snapshot']
        assert new['stale'] is True
        assert 'restarted' in new['gap']['reason']
        second.publish({'jobs': [{'id': '123', 'state': 'COMPLETED'}]})
        assert second.observations.sequence == old['sequence'] + 1
    finally:
        second.close()


def test_stale_socket_recovery_and_unrelated_file_refusal(directory):
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(directory/S.SOCKET))
    (directory/S.SOCKET).chmod(0o600)
    sock.close()
    server = S.Server(directory, {'user': 'alice'})
    server.close()
    (directory/S.SOCKET).write_text('unrelated')
    with pytest.raises(ValueError, match='unrelated'):
        S.Server(directory, {'user': 'alice'})
    assert (directory/S.SOCKET).read_text() == 'unrelated'


def test_scope_change_cannot_restore_other_profile(directory):
    service = S.Server(directory, {'user': 'alice'})
    service.close()
    with pytest.raises(ValueError, match='different connection'):
        S.Server(directory, {'user': 'bob'})


def test_replay_ring_is_bounded_and_gap_is_explicit():
    observations = C.Observations({'user': 'alice'})
    for i in range(100):
        observations.publish({'jobs': [{'id': str(i)}]})
    result = observations.query(C.request('events', cursor=1))
    assert len(result['events']) == C.MAX_EVENTS
    assert result['gap']['through'] == 100-C.MAX_EVENTS
    assert result['snapshot']['jobs'][0]['id'] == '99'


@pytest.mark.parametrize('change', [{'version': 2}, {'op': 'submit'}, {'op': 'cancel'}, {'op': 'allocate'}, {'cursor': -1}, {'cursor': True}, {'extra': 1}])
def test_security_semantics_and_version_skew_fail_closed(change):
    value = C.request('hello')
    value.update(change)
    with pytest.raises(ValueError):
        C.Observations({}).query(value)


def test_fragmented_contract_reuses_shared_decoder():
    value = C.request('hello')
    raw = C.message(value, request=True)
    decoder = C.decoder(request=True)
    for char in raw[:-1]:
        decoder.feed(bytes([char]))
        assert not decoder.take()
    decoder.feed(raw[-1:])
    assert decoder.take() == [value]
    with pytest.raises(ValueError):
        C.decoder(request=True).feed((C.MAX_REQUEST+1).to_bytes(4, 'big'))


def test_unknown_local_peer_is_rejected_before_data(server, monkeypatch):
    # Unit policy injection is separate from actual multi-UID qualification.
    monkeypatch.setattr(S, 'peer_uid', lambda connection: os.getuid()+1)
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(1)
        connection.connect(str(server.root/S.SOCKET))
        assert connection.recv(1) == b''
    assert server.metrics['rejected_peers'] == 1
    assert server.metrics['queries'] == 0


def test_client_timeout_preserves_retained_cursor(server, monkeypatch):
    client = S.Client(server.root)
    client.query()
    identity = (client.session, client.epoch, client.cursor)
    monkeypatch.setattr(client, 'root', server.root/'missing')
    with pytest.raises(OSError):
        client.query()
    assert (client.session, client.epoch, client.cursor) == identity


def test_cli_bridge_uses_same_versioned_contract(server):
    completed = subprocess.run([sys.executable, '-m', 'raven.session_service', 'bridge', str(server.root),
                                '--request', json.dumps(C.request('hello'))], capture_output=True, text=True, timeout=5)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)['session'] == server.observations.session


def test_remote_installed_bootstrap_never_ships_source_or_replays(monkeypatch):
    from raven import operations
    calls = []
    response = C.Observations({}).query(C.request('hello'))
    def command(backend, argv, timeout, limit, cancel):
        calls.append(argv)
        return C.encoded(response).decode()
    monkeypatch.setattr(operations, '_backend_command', command)
    client = S.RemoteClient('/remote path', object())
    assert client.query()['session'] == response['session']
    assert calls[0][:4] == ['python3', '-m', 'raven.session_service', 'bridge']
    assert calls[0][4] == '/remote path'
    assert len(calls) == 1


def test_snapshot_omissions_are_explicit_and_private_configuration_is_absent():
    from raven.model import Store, Job
    store = Store(persist=False)
    store.jobs = [Job(id=str(i), name='fixture', partition='local', state='RUNNING', command='PRIVATE-COMMAND', workdir='PRIVATE-PATH') for i in range(3)]
    value = S.observation(store, max_records=2)
    assert len(value['jobs']) == 2
    assert value['coverage']['jobs'] == {'observed': 3, 'returned': 2, 'omitted': 1}
    assert not {'config', 'logs', 'details', 'hist_cpu'} & value.keys()
    assert 'PRIVATE-' not in json.dumps(value)
    store.close()


def test_oversized_snapshot_does_not_replace_prior_context():
    observations = C.Observations({})
    observations.publish({'jobs': [{'id': '1'}]})
    with pytest.raises(ValueError, match='response budget'):
        observations.publish({'text': 'x'*C.MAX_RESPONSE})
    assert observations.sequence == 1
    assert observations.snapshot == {'jobs': [{'id': '1'}]}


def test_slow_client_aggregate_budget_disconnects_without_blocking_others(server, monkeypatch):
    monkeypatch.setattr(S, 'MAX_TOTAL_QUEUED', 1)
    with pytest.raises((OSError, ValueError)):
        S.Client(server.root).query()
    assert server.metrics['slow_clients'] == 1
    monkeypatch.setattr(S, 'MAX_TOTAL_QUEUED', 8 << 20)
    assert S.Client(server.root).query()['snapshot']['jobs'][0]['id'] == '1'


def test_global_read_quota_does_not_create_collections(server):
    server._rate_tokens = 0
    server._rate_updated = time.monotonic() + 60
    with pytest.raises(ValueError, match='read quota'):
        S.Client(server.root).query()
    assert server.observations.sequence == 1


@pytest.mark.parametrize('change', [{'session': 'garbage'}, {'observed_at': 1e100}, {'snapshot': []}, {'snapshot': {'jobs': [1]}}, {'sequence': True}])
def test_malformed_retained_authoritative_state_fails_before_publication(change):
    observations = C.Observations({'user': 'alice'})
    retained = observations.retained()
    retained.update(change)
    with pytest.raises(ValueError):
        C.Observations({'user': 'alice'}, retained)


@pytest.mark.parametrize('change', [{'epoch': ''}, {'observed_at': 1e100}, {'snapshot': {'jobs': [1]}}, {'mutations': True}, {'events': [{}]}, {'stale': 'false'}])
def test_bad_response_cannot_advance_client_cursor(directory, change):
    client = S.Client(directory)
    original = (client.session, client.epoch, client.cursor)
    response = C.Observations({}).query(C.request('hello'))
    response.update(change)
    with pytest.raises(ValueError):
        client.accept(response)
    assert (client.session, client.epoch, client.cursor) == original


def test_slow_read_never_blocks_ui_or_close():
    entered, release = threading.Event(), threading.Event()
    class SlowClient:
        def query(self):
            entered.set()
            release.wait(2)
            raise ConnectionError('fixture disconnect')
    reader = S.LatestReader(SlowClient())
    try:
        assert entered.wait(1)
        before = time.monotonic()
        assert reader.latest() == (None, '')
        reader.close()
        assert time.monotonic() - before < .1
    finally:
        release.set()
        reader.thread.join(timeout=2)
        reader.close()


def test_invalid_publication_leaves_identity_and_snapshot_unchanged():
    observations = C.Observations({})
    observations.publish({'jobs': [{'id': '1'}]})
    before = observations.retained()
    with pytest.raises(ValueError):
        observations.publish({'jobs': [1]})
    assert observations.retained() == before


def test_real_optional_daemon_demo_restart_and_cleanup(directory):
    command = [sys.executable, '-m', 'raven.session_service', 'serve', str(directory), '--demo']
    sessions = []
    for _ in range(2):
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic()+8
            response = None
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    response = S.Client(directory).query()
                    if response['snapshot'].get('jobs'):
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(.05)
            assert response is not None and response['snapshot'].get('jobs'), process.communicate(timeout=1) if process.poll() is not None else response
            sessions.append(response['session'])
        finally:
            process.terminate()
            output, error = process.communicate(timeout=8)
        assert process.returncode == 0, error
        assert not (directory/S.SOCKET).exists()
    assert sessions[0] == sessions[1]
