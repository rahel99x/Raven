"""Portable cache semantics and actual isolated remote bundle transport."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading

import pytest

from raven import project_remote, project_wire, projects, shared_collection as S
from raven.read_acquisition import for_cache
from raven.remote import RemoteFiles, SshBackend
from test_flagship_platform import SCOPE, caps, policy, query
from test_project_remote import IsolatedSSH


def test_wal_thread_local_connections_reuse_without_sharing_threads(tmp_path, monkeypatch):
    original, calls = S.sqlite3.connect, []
    def connect(*args, **kwargs):
        calls.append(threading.get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(S.sqlite3, 'connect', connect)
    cache = S.SharedQueryCache(tmp_path / 'cache')
    with cache._connection() as first:
        assert first.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert first.execute('PRAGMA synchronous').fetchone()[0] == 1
    for _ in range(10):
        with cache._connection() as same:
            assert same is first
    def worker():
        with cache._connection() as other:
            assert other is not first
        with cache._connection() as same:
            assert same is other
        cache.close()
    with ThreadPoolExecutor(1) as pool:
        pool.submit(worker).result(2)
    assert len(calls) == 2
    cache.close()


def test_process_fallback_has_no_linux_identity_dependency_and_coalesces(monkeypatch):
    def unavailable(*args):
        raise AssertionError('portable cache must not read Linux process identity')
    monkeypatch.setattr(S, '_host', unavailable)
    monkeypatch.setattr(S, '_process_start', unavailable)
    cache = S.ProcessQueryCache()
    acquisition = for_cache(cache)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def load(stop):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return {'records': ['portable']}
    def read():
        return acquisition.query(SCOPE, query(), load, policy(), caps(), read_only=True)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(read)
        assert entered.wait(1)
        second = pool.submit(read)
        release.set()
        assert first.result(2)['value'] == second.result(2)['value'] == {'records': ['portable']}
    assert calls == [1]
    acquisition.invalidate(SCOPE)
    assert read()['value'] == {'records': ['portable']}
    assert calls == [1, 1]
    cache.close()


def test_remote_bundle_uploads_once_then_reuses_compiled_private_content(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    project = tmp_path / 'project'
    project.mkdir()
    files = RemoteFiles(IsolatedSSH())
    first = projects.discover_project(str(project), files=files)
    actions = [project_wire.classify(command) for command in files.ssh.commands]
    payload = ''.join(project_remote._payload())
    assert sum(item['upload_bytes'] for item in actions) == len(payload)
    assert all(len(SshBackend('example.invalid').argv(command)[-1].encode()) < 131072
               for command in files.ssh.commands)
    root = Path(files._project_reader_bundle[2])
    digest = hashlib.sha256(payload.encode()).hexdigest()
    packed = root / (digest + '.xz64')
    compiled = next(root.glob(digest + '.*.code'))
    assert packed.stat().st_mode & 0o777 == compiled.stat().st_mode & 0o777 == 0o600
    before = compiled.stat().st_mtime_ns
    count = len(files.ssh.commands)
    second = projects.discover_project(str(project), files=files)
    assert first == second
    assert len(files.ssh.commands) == count + 1
    assert project_wire.classify(files.ssh.commands[-1])['action'] == 'read'
    assert len(SshBackend('example.invalid').argv(files.ssh.commands[-1])[-1].encode()) < 16000
    assert compiled.stat().st_mtime_ns == before
    # A damaged data bundle is repaired from the exact local digest; it never
    # executes stale bytecode from a different or incomplete publication.
    packed.write_text('corrupt')
    assert projects.discover_project(str(project), files=files) == first
    assert hashlib.sha256(packed.read_bytes()).hexdigest() == digest


def test_remote_wire_rejects_unclassified_sources_and_bad_chunks():
    digest = 'a' * 64
    request = json.dumps({'operation': 'discover_project', 'args': ['/tmp'], 'kwargs': {}})
    argv = project_wire.command('read', digest, '', request, 1)
    argv[4] += '\nprint("injected")'
    with pytest.raises(ValueError, match='Unclassified'):
        project_wire.classify(argv)
    with pytest.raises(ValueError, match='chunk'):
        project_wire.command('upload', digest, '', '1:bad', 1)


def test_governed_startup_without_proc_uses_portable_cache(monkeypatch):
    from raven.platform_session import wrap_backend
    from test_raven_collection import Recorder
    monkeypatch.setattr(S, '_host', lambda: (_ for _ in ()).throw(ValueError('Shared collection needs Linux process/boot identity')))
    raw = Recorder()
    governed = wrap_backend(raw, {'raven': {'collection': {}}}, {'user': 'portable'}, None)
    try:
        assert isinstance(governed.cache, S.ProcessQueryCache)
        assert governed.cache_coordination.startswith('process-local')
        assert governed.run(['squeue'])[0] == '1|RUNNING\n'
        assert governed.run(['squeue'])[0] == '1|RUNNING\n'
        assert len(raw.commands) == 1
        assert governed.ledger.report()['rates_last_minute']['controller'] == 1
    finally:
        governed.close()
        governed.cache.close()


def test_automatic_cache_name_squatting_uses_private_random_fallback(tmp_path, monkeypatch):
    from raven import raven_collection as C
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    root = tmp_path / ('raven-collection-' + str(S.os.getuid()) + '-' + S._host()[:16])
    root.mkdir(mode=0o777)
    root.chmod(0o777)
    selected = Path(C.cache_directory())
    assert selected != root and selected.stat().st_mode & 0o777 == 0o700
    assert root.stat().st_mode & 0o777 == 0o777
    assert C.cache_directory() == str(selected)


def test_reader_wire_checks_quoted_remote_command_bytes():
    request = json.dumps({'operation': 'discover_project', 'args': ["'" * 60000], 'kwargs': {}})
    with pytest.raises(ValueError, match='SSH argument budget'):
        project_wire.command('read', 'a' * 64, '', request, 1)
