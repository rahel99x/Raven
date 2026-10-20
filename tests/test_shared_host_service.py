import json
import os
from pathlib import Path
import pwd
import socket
import tempfile
import threading
import time
from concurrent.futures import Future
from queue import Empty, Queue

import pytest

from raven import operation_support, session_contract as C, shared_host_service as H


def configured(uid=None):
    uid = os.getuid() if uid is None else uid
    return {'schema': H.POLICY, 'id': 'test-local-policy', 'socket_gid': os.getgid(), 'interval_seconds': 60, 'max_records': 1000,
            'principals': {str(uid): {'user': pwd.getpwuid(uid).pw_name, 'accounts': [], 'site': False}}}


@pytest.fixture
def fixture():
    with tempfile.TemporaryDirectory(prefix='rv-shared-') as temporary:
        root = Path(temporary)
        path = root/'policy.json'
        path.write_text(json.dumps(configured()))
        server = H.SharedServer(root/'service', path)
        uid = os.getuid()
        own = pwd.getpwuid(uid).pw_name
        server.publish_source({'jobs': [{'id': '1', 'user': own, 'account': 'own', 'state': 'RUNNING', 'name': 'mine', 'command': 'SECRET-COMMAND', 'workdir': 'SECRET-PATH'},
                                       {'id': '2', 'user': 'someone-else', 'account': 'lab', 'name': 'SECRET-JOB', 'state': 'RUNNING'}],
                               'finished': [{'id': '3', 'user': 'someone-else', 'account': 'lab', 'name': 'SECRET-HISTORY'}],
                               'queue_observed_at': time.time(), 'accounting_observed_at': time.time(),
                               'source_errors': {'jobs': '', 'finished': ''}})
        stop = threading.Event()
        commands = Queue()
        def on_owner(function):
            future = Future()
            commands.put((function, future))
            return future.result(timeout=2)
        server.test_on_owner = on_owner
        def pump():
            while not stop.is_set():
                try:
                    function, future = commands.get_nowait()
                except Empty:
                    pass
                else:
                    try:
                        future.set_result(function())
                    except BaseException as exc:
                        future.set_exception(exc)
                server.poll(.01)
        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        try:
            yield server, path
        finally:
            stop.set()
            thread.join(timeout=2)
            server.close()


def test_actual_same_uid_ipc_filters_before_bytes_leave_service(fixture):
    server, _ = fixture
    response = H.SharedClient(server.root, server_uid=os.getuid()).query()
    assert [row['id'] for row in response['snapshot']['jobs']] == ['1']
    assert not response['snapshot']['finished']
    assert 'SECRET-' not in C.encoded(response).decode()
    assert (server.root/'session.sock').stat().st_mode & 0o777 == 0o660
    assert server.root.stat().st_mode & 0o777 == 0o750


def test_explicit_account_and_site_grants_are_distinct(fixture):
    server, path = fixture
    value = configured()
    grant = value['principals'][str(os.getuid())]
    grant['accounts'] = ['lab']
    path.write_text(json.dumps(value))
    server.test_on_owner(server.reload_policy)
    response = H.SharedClient(server.root, server_uid=os.getuid()).query()
    assert [row['id'] for row in response['snapshot']['jobs']] == ['1', '2']
    assert [row['id'] for row in response['snapshot']['finished']] == ['3']
    assert 'SECRET-COMMAND' not in C.encoded(response).decode()
    first_scope = response['scope']
    grant['accounts'], grant['site'] = [], True
    path.write_text(json.dumps(value))
    server.test_on_owner(server.reload_policy)
    response = H.SharedClient(server.root, server_uid=os.getuid()).query()
    assert response['scope'] != first_scope


def test_revocation_discards_authorized_cache_and_pending_connections(fixture):
    server, path = fixture
    client = H.SharedClient(server.root, server_uid=os.getuid())
    original = client.query()
    assert server._views
    value = configured()
    # Keep a valid policy but revoke this UID. Tests injecting a grant do not
    # substitute for the separate actual multi-UID qualification harness.
    other = next(record for record in pwd.getpwall() if record.pw_uid != os.getuid())
    value['principals'] = {str(other.pw_uid): {'user': other.pw_name, 'accounts': [], 'site': False}}
    path.write_text(json.dumps(value))
    server.test_on_owner(server.reload_policy)
    assert not server._views and server._cache_bytes == 0
    with pytest.raises((OSError, ValueError)):
        client.query()
    assert client.session == original['session']


def test_malformed_policy_denies_all_until_valid_replacement(fixture):
    server, path = fixture
    path.write_text('{}')
    server.test_on_owner(server.reload_policy)
    assert server.policy_error
    with pytest.raises((OSError, ValueError)):
        H.SharedClient(server.root, server_uid=os.getuid()).query()
    path.write_text(json.dumps(configured()))
    server.test_on_owner(server.reload_policy)
    assert H.SharedClient(server.root, server_uid=os.getuid()).query()['snapshot']['jobs']


def test_unscoped_ownership_never_falls_back_to_broad_visibility(fixture):
    server, _ = fixture
    previous = server._source
    value = dict(previous, jobs=[{'id': 'unknown', 'name': 'private'}])
    with pytest.raises(ValueError, match='ownership'):
        server.publish_source(value)
    assert server._source is previous


def test_cache_scope_cannot_be_requested_by_the_peer(fixture):
    server, _ = fixture
    value = C.request('snapshot')
    value['uid'] = 0
    with pytest.raises(ValueError, match='fields'):
        server.query({'uid': os.getuid()}, value)


def test_no_raw_scheduler_text_or_mutation_api(fixture):
    server, _ = fixture
    for operation in ('read-file', 'run', 'submit', 'cancel', 'config'):
        value = C.request('snapshot')
        value['op'] = operation
        with pytest.raises(ValueError, match='read-only'):
            server.query({'uid': os.getuid()}, value)


def test_policy_cannot_map_own_uid_to_someone_elses_login():
    value = configured()
    value['principals'][str(os.getuid())]['user'] = 'forged-other-user'
    with pytest.raises(ValueError, match='authenticated'):
        H.policy(value)


def test_clients_require_independently_trusted_service_owner(fixture):
    server, _ = fixture
    with pytest.raises(ValueError, match='ownership|ancestor'):
        H.SharedClient(server.root, server_uid=os.getuid()+1)


def test_uid_quota_survives_connection_replacement(fixture):
    server, _ = fixture
    for _ in range(4):
        H.SharedClient(server.root, server_uid=os.getuid()).query()
    with pytest.raises(ValueError, match='per-user read quota'):
        H.SharedClient(server.root, server_uid=os.getuid()).query()


def test_idle_peer_flood_cannot_consume_other_principals_connection_capacity(fixture):
    server, _ = fixture
    peers = []
    try:
        for _ in range(4):
            peer = socket.socket(socket.AF_UNIX)
            peer.connect(str(server.root/'session.sock'))
            peers.append(peer)
        deadline = time.monotonic()+2
        while len(server.clients) < 4 and time.monotonic() < deadline:
            time.sleep(.005)
        assert len(server.clients) == 4
        fifth = socket.socket(socket.AF_UNIX)
        fifth.settimeout(2)
        fifth.connect(str(server.root/'session.sock'))
        peers.append(fifth)
        assert fifth.recv(1) == b''
        assert len(server.clients) == 4 and len(server.clients) < server.max_clients
        # This focused hook check does not impersonate a kernel UID. The
        # container qualification exercises the second UID's real socket.
        assert server.test_on_owner(lambda: server.admit_connection(os.getuid()+1))
        assert not server.test_on_owner(lambda: server.admit_connection(os.getuid()))
    finally:
        for peer in peers:
            peer.close()
def test_recorded_structured_sources_keep_independent_failures(fixture, monkeypatch):
    server, _ = fixture
    corpus = Path(__file__).parent/'fixtures/slurm/recorded/26.05.5'
    calls = []
    def command(backend, argv, timeout, limit, cancel):
        calls.append((argv, timeout, limit))
        name = 'running-squeue-json' if argv[0] == 'squeue' else 'accounting-json'
        return (corpus/(name+'.stdout.txt')).read_text()
    monkeypatch.setattr(operation_support, '_backend_command', command)
    # Recorded accounting includes ambiguous requeue identities; preserve the
    # previous history and report its failure independently of a valid queue.
    old = server._source
    new = H.collect(object(), old, max_records=1000)
    assert new['jobs'] and not new['source_errors']['jobs']
    assert new['finished'] == old['finished'] and new['source_errors']['finished']
    assert new['queue_observed_at'] >= old['queue_observed_at']
    assert new['accounting_observed_at'] == old['accounting_observed_at']
    assert calls[1][0] == ['sacct', '--json', '--allusers', '-X', '-S', 'now-24hours']
    assert all(timeout == 8 and limit == 4 << 20 for _, timeout, limit in calls)
    assert 'command' not in new['jobs'][0] and 'workdir' not in new['jobs'][0]


def test_unknown_structured_identity_cannot_publish_an_empty_success(fixture, monkeypatch):
    server, _ = fixture
    corpus = Path(__file__).parent/'fixtures/slurm/recorded/26.05.5/running-squeue-json.stdout.txt'
    raw = json.loads(corpus.read_text())
    for row in raw['jobs']:
        row['user_name'] = ''
    monkeypatch.setattr(operation_support, '_backend_command', lambda *args: json.dumps(raw))
    before = server._source
    after = H.collect(object(), before, max_records=1000, accounting=False)
    assert after['jobs'] == before['jobs']
    assert after['source_errors']['jobs']
    assert after['source_errors']['finished'] == before['source_errors']['finished']


def test_recurring_collection_uses_source_admission_with_cancellation(fixture, monkeypatch):
    from raven.platform_session import GovernedBackend
    server, _ = fixture
    backend = object.__new__(GovernedBackend)
    calls = []
    raw = (Path(__file__).parent/'fixtures/slurm/recorded/26.05.5/running-squeue-json.stdout.txt').read_text()
    def query(argv, timeout, limit, cancel, *, fresh):
        calls.append((argv, cancel, fresh))
        return raw
    backend._query = query
    stop = threading.Event()
    after = H.collect(backend, server._source, max_records=1000, accounting=False, cancel=stop)
    assert not after['source_errors']['jobs']
    assert calls == [(['squeue', '--json'], stop, False)]


def attempt(jid='2', **changes):
    return {'cluster': 'test-cluster', 'job_id': jid, 'submit_time': 1792279000,
            'start_time': 1792279060, 'restart_count': 0, **changes}


def project_policy():
    value = configured()
    value['project_memberships'] = {'ProjectA': [attempt()], 'ProjectB': [attempt('4')]}
    value['principals'][str(os.getuid())]['projects'] = ['ProjectA']
    return value


def project_rows():
    return [{'id': jid, 'cluster': 'test-cluster', 'user': 'other', 'account': 'LabX',
             'name': 'project-'+jid, 'state': 'RUNNING', '_attempt': attempt(jid)} for jid in ('2', '4')]


def install_projects(server, path):
    value = project_policy()
    path.write_text(json.dumps(value))
    def install():
        server.reload_policy()
        server.publish_source({**server._source, 'jobs': project_rows(), 'finished': []})
    server.test_on_owner(install)
    return value


def test_project_grant_is_separate_from_shared_billing_account_and_preserves_native_identity(fixture):
    server, path = fixture
    install_projects(server, path)
    response = H.SharedClient(server.root, server_uid=os.getuid()).query()
    assert [row['id'] for row in response['snapshot']['jobs']] == ['2']
    assert response['snapshot']['jobs'][0]['projects'] == ['ProjectA']
    assert response['snapshot']['authorization']['projects'] == ['ProjectA']
    assert response['snapshot']['authorization']['accounts'] == []
    encoded = C.encoded(response).decode()
    assert 'ProjectB' not in encoded and 'project-4' not in encoded and '_attempt' not in encoded
    # Grants are a union: an explicit lab grant deliberately includes both.
    value = project_policy()
    value['principals'][str(os.getuid())]['accounts'] = ['LabX']
    path.write_text(json.dumps(value))
    server.test_on_owner(server.reload_policy)
    assert [row['id'] for row in H.SharedClient(server.root, server_uid=os.getuid()).query()['snapshot']['jobs']] == ['2', '4']


def test_publication_rejects_invalid_attempt_even_for_own_user_and_replaces_private_snapshot(fixture):
    server, path = fixture
    install_projects(server, path)
    source = {**server._source, 'jobs': project_rows()}
    source['jobs'][0]['user'] = pwd.getpwuid(os.getuid()).pw_name
    source['jobs'][0]['_attempt']['job_id'] = '999'
    prior = server._source
    with pytest.raises(ValueError, match='differs'):
        server.test_on_owner(lambda: server.publish_source(source))
    assert server._source is prior
    source['jobs'][0]['_attempt']['job_id'] = '2'
    server.test_on_owner(lambda: server.publish_source(source))
    source['jobs'][0]['_attempt']['job_id'] = '999'
    source['jobs'][1]['user'] = pwd.getpwuid(os.getuid()).pw_name
    response = H.SharedClient(server.root, server_uid=os.getuid()).query()
    assert [row['id'] for row in response['snapshot']['jobs']] == ['2']
    # A new scheduler observation replaces the same job's attempt; old project
    # authorization cannot survive a requeue through a cached response.
    replacement = project_rows()
    replacement[0]['_attempt']['restart_count'] = 1
    server.test_on_owner(lambda: server.publish_source({**server._source, 'jobs': replacement}))
    assert not H.SharedClient(server.root, server_uid=os.getuid()).query()['snapshot']['jobs']


@pytest.mark.parametrize('field,replacement', [('cluster','different'), ('job_id','3'),
                         ('submit_time',1792278999), ('start_time',1792279061), ('restart_count',1)])
def test_project_membership_is_bound_to_every_native_attempt_field(field, replacement):
    rows = project_rows()
    rows[0]['_attempt'][field] = replacement
    if field == 'cluster':
        rows[0]['cluster'] = replacement
    if field == 'job_id':
        rows[0]['id'] = replacement
    value = project_policy()
    assert not H.project(rows, value['principals'][str(os.getuid())], H.membership_index(value))


def test_unknown_attempt_does_not_become_restart_zero_or_inherit_project():
    rows = project_rows()
    rows[0]['_attempt'] = None
    value = project_policy()
    assert not H.project(rows, value['principals'][str(os.getuid())], H.membership_index(value))
    rows[0]['user'] = pwd.getpwuid(os.getuid()).pw_name
    assert [row['id'] for row in H.project(rows, value['principals'][str(os.getuid())], H.membership_index(value))] == ['2']


@pytest.mark.parametrize('change', [lambda x: x['principals'][str(os.getuid())].update(projects=['undeclared']),
    lambda x: x['project_memberships']['ProjectA'][0].pop('restart_count'),
    lambda x: x['project_memberships']['ProjectA'][0].update(restart_count=None),
    lambda x: x['project_memberships']['ProjectA'][0].update(restart_count=True),
    lambda x: x['project_memberships']['ProjectA'][0].update(start_time=0),
    lambda x: x['project_memberships']['ProjectA'][0].update(job_id='2_[1-9]'),
    lambda x: x['project_memberships']['ProjectA'][0].update(job_id='2.batch'),
    lambda x: x['project_memberships']['ProjectA'][0].update(submission_token='unverified'),
    lambda x: x['project_memberships']['ProjectA'].append(attempt()),
    lambda x: x['project_memberships'].update({'../../untrusted': []})])
def test_project_authority_contract_rejects_ambiguous_unknown_and_action_extensions(change):
    value = project_policy()
    change(value)
    with pytest.raises(ValueError):
        H.policy(value)


def test_project_schema_and_runtime_accept_explicit_array_component_bindings():
    import jsonschema
    value = project_policy()
    value['project_memberships']['ProjectA'] = [attempt('2_3'), attempt('4+0')]
    assert H.policy(value) is value
    schema = json.loads((Path(__file__).parents[1]/'docs/schemas/shared-host-policy.schema.json').read_text())
    jsonschema.validate(value, schema)
    jsonschema.validate(configured(), schema)


def test_membership_change_revokes_cached_and_queued_project_bytes(fixture):
    server, path = fixture
    value = install_projects(server, path)
    initial = H.SharedClient(server.root, server_uid=os.getuid()).query()
    assert server._views
    peer = socket.socket(socket.AF_UNIX)
    peer.settimeout(2)
    peer_path = str(server.root/'test-peer.sock')
    peer.bind(peer_path)
    peer.connect(str(server.root/'session.sock'))
    try:
        deadline = time.monotonic()+2
        def accepted():
            return any(connection.getpeername() == peer_path for connection in server.clients)
        while not server.test_on_owner(accepted) and time.monotonic() < deadline:
            time.sleep(.005)
        assert server.test_on_owner(accepted)
        def stage_revoke():
            # The selector owns this entire sequence: construct an authorized
            # pending response, then observe the administrator's revocation.
            connection, pending = next((connection, pending) for connection, pending in server.clients.items()
                                       if connection.getpeername() == peer_path)
            pending['queue'].append(memoryview(C.message(initial)))
            pending['queued'] += len(pending['queue'][-1])
            value['project_memberships']['ProjectA'] = []
            path.write_text(json.dumps(value))
            server.reload_policy()
            assert connection not in server.clients and not server._views
            assert not server._view_metadata and server._cache_bytes == 0
        server.test_on_owner(stage_revoke)
        assert peer.recv(1) == b''
    finally:
        peer.close()
    response = H.SharedClient(server.root, server_uid=os.getuid()).query()
    assert not response['snapshot']['jobs']
    assert response['scope'] != initial['scope']
    assert 'project-2' not in C.encoded(response).decode()


@pytest.mark.parametrize('restart', [None, {'set':False,'infinite':False,'number':0}, 1])
def test_native_queue_project_identity_preserves_missing_restart(fixture, monkeypatch, restart):
    server, _ = fixture
    corpus = Path(__file__).parent/'fixtures/slurm/recorded/26.05.5/running-squeue-json.stdout.txt'
    raw = json.loads(corpus.read_text())
    native = raw['jobs'][0]
    raw['jobs'] = [native]
    native['restart_cnt'] = restart
    monkeypatch.setattr(operation_support, '_backend_command', lambda *args: json.dumps(raw))
    result = H.collect(object(), server._source, max_records=1000, accounting=False)
    assert not result['source_errors']['jobs']
    if restart == 1:
        assert result['jobs'][0]['_attempt']['restart_count'] == 1
    else:
        assert result['jobs'][0]['_attempt'] is None


def test_native_accounting_project_identity_survives_sorting(fixture, monkeypatch):
    from raven import slurm_json as J
    server, _ = fixture
    corpus = Path(__file__).parent/'fixtures/slurm/recorded/26.05.5/accounting-json.stdout.txt'
    raw = json.loads(corpus.read_text())
    native = next(item for item in raw['jobs'] if J.state(item['state']['current']) == 'COMPLETED' and J.integer(item['time'].get('start')))
    first, second = json.loads(json.dumps(native)), json.loads(json.dumps(native))
    for row, jid, end, restart in ((first, 990001, 1792280000, 0), (second, 990002, 1792280001, 1)):
        row['job_id'], row['restart_cnt'] = jid, restart
        row['array'], row['het'] = {}, {}
        row['time']['end'] = end
    raw['jobs'] = [first, second]
    monkeypatch.setattr(operation_support, '_backend_command', lambda *args: json.dumps(raw))
    result = H.collect(object(), server._source, max_records=1000, queue=False)
    assert not result['source_errors']['finished']
    assert [(row['id'], row['_attempt']['job_id'], row['_attempt']['restart_count']) for row in result['finished']] == [
        ('990002', '990002', 1), ('990001', '990001', 0)]


@pytest.mark.parametrize('state', ['PENDING', 'CONFIGURING'])
def test_predicted_queue_start_does_not_authorize_project_attempt(fixture, monkeypatch, state):
    server, _ = fixture
    corpus = Path(__file__).parent/'fixtures/slurm/recorded/26.05.5/running-squeue-json.stdout.txt'
    raw = json.loads(corpus.read_text())
    native = raw['jobs'][0]
    raw['jobs'] = [native]
    native['job_state'] = [state]
    native['start_time'] = 1799999999
    native['restart_cnt'] = 0
    monkeypatch.setattr(operation_support, '_backend_command', lambda *args: json.dumps(raw))
    result = H.collect(object(), server._source, max_records=1000, accounting=False)
    assert not result['source_errors']['jobs']
    assert result['jobs'][0]['state'] == state and result['jobs'][0]['_attempt'] is None


def test_shared_terminal_explains_grants_and_project_labels(fixture):
    from raven import session_service as S
    server, path = fixture
    install_projects(server, path)
    response = H.SharedClient(server.root, server_uid=os.getuid()).query()
    class Window:
        lines = []
        def erase(self): pass
        def getmaxyx(self): return 24, 80
        def addnstr(self, row, col, text, limit): self.lines.append(text[:limit])
        def refresh(self): pass
        def timeout(self, value): pass
        def getch(self): return ord('q')
    class Reader:
        def latest(self): return response, ''
    class Waiter:
        available = False
    window = Window()
    S._terminal_loop(window, Reader(), Waiter())
    output = '\n'.join(window.lines)
    assert 'Visibility: own login' in output and 'projects ProjectA' in output
    assert '[ProjectA]' in output and 'ProjectB' not in output and 'project-4' not in output
