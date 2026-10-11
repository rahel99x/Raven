"""Production read consolidation: ownership, cancellation, freshness and budgets."""
from concurrent.futures import ThreadPoolExecutor
import copy
import gc
import threading
import time
import weakref

import pytest

from tower import read_acquisition as A
from tower.shared_collection import SharedQueryCache
from tower.worker_scheduler import WorkerScheduler
from test_flagship_platform import SCOPE, caps, policy, query


def client(tmp_path, **bounds):
    return A.for_cache(SharedQueryCache(tmp_path / 'cache', **bounds))


def read(acquisition, loader, *, scope=None, request=None, rules=None, observed=None, **kwargs):
    return acquisition.query(scope or SCOPE, request or query(), loader, rules or policy(),
                             observed or caps(), read_only=True, **kwargs)


def wait_for(predicate):
    until = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < until
        time.sleep(.001)


def test_concurrent_clients_share_one_sqlite_acquisition_and_detach_results(tmp_path, monkeypatch):
    first = client(tmp_path)
    second = client(tmp_path)
    assert first is second
    entered, release = threading.Event(), threading.Event()
    calls = []
    queries = []
    original = first.cache._query_admitted
    def cached(*args, **kwargs):
        queries.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(first.cache, '_query_admitted', cached)
    def load(stop):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return {'records': [{'cpu': 37}]}
    with ThreadPoolExecutor(3) as pool:
        a = pool.submit(read, first, load)
        assert entered.wait(1)
        b = pool.submit(read, second, load)
        c = pool.submit(read, second, load)
        wait_for(lambda: first.stats()['leases'] == 3)
        release.set()
        results = [task.result(2) for task in (a, b, c)]
    assert len(calls) == len(queries) == 1
    results[0]['value']['records'][0]['cpu'] = 999
    assert results[1]['value']['records'][0]['cpu'] == 37
    assert first.stats()['leases'] == first.stats()['entries'] == 0
    # Completed reads consult the host authority, so invalidation in another
    # client/process cannot be hidden behind a stale process-local result.
    assert read(first, load)['cached']
    assert len(calls) == 1 and len(queries) == 2


def test_cancel_owner_keeps_other_subscriber_and_cancelled_owner_gets_no_result(tmp_path):
    acquisition = client(tmp_path)
    entered, release, cancel_owner = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def load(stop):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        assert not stop.is_set()
        return {'ok': True}
    with ThreadPoolExecutor(2) as pool:
        owner = pool.submit(read, acquisition, load, cancel=cancel_owner)
        assert entered.wait(1)
        subscriber = pool.submit(read, acquisition, load)
        wait_for(lambda: acquisition.stats()['leases'] == 2)
        cancel_owner.set()
        release.set()
        with pytest.raises(ValueError, match='cancelled'):
            owner.result(2)
        assert subscriber.result(2)['value'] == {'ok': True}
    assert calls == [1]


def test_all_subscriber_events_cancel_caller_owned_loader(tmp_path):
    acquisition = client(tmp_path)
    entered = threading.Event()
    one, two = threading.Event(), threading.Event()
    def load(stop):
        entered.set()
        assert stop.wait(1), 'Cancellation must reach a loader executing on its subscriber worker'
        return {}
    with ThreadPoolExecutor(2) as pool:
        owner = pool.submit(read, acquisition, load, cancel=one)
        assert entered.wait(1)
        subscriber = pool.submit(read, acquisition, load, cancel=two)
        wait_for(lambda: acquisition.stats()['leases'] == 2)
        one.set()
        two.set()
        for task in (owner, subscriber):
            with pytest.raises(ValueError, match='cancel|changed|expired'):
                task.result(2)
    assert acquisition.stats()['active'] == acquisition.stats()['leases'] == 0


def test_cancel_only_waiter_is_prompt_and_owner_continues(tmp_path):
    acquisition = client(tmp_path)
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    def load(cancel):
        entered.set()
        assert release.wait(2)
        assert not cancel.is_set()
        return {'ok': True}
    with ThreadPoolExecutor(2) as pool:
        owner = pool.submit(read, acquisition, load)
        assert entered.wait(1)
        waiter = pool.submit(read, acquisition, load, cancel=stop)
        wait_for(lambda: acquisition.stats()['leases'] == 2)
        stop.set()
        with pytest.raises(ValueError, match='cancelled'):
            waiter.result(.5)
        release.set()
        assert owner.result(2)['value'] == {'ok': True}


def test_fresh_read_waits_for_older_flight_then_observes_again(tmp_path):
    acquisition = client(tmp_path)
    entered, release, fresh_entered = threading.Event(), threading.Event(), threading.Event()
    calls = []
    q = query()
    q['request']['interval'] = .5
    def old(stop):
        calls.append('old')
        entered.set()
        assert release.wait(2)
        return {'state': 'PENDING'}
    def new(stop):
        calls.append('new')
        return {'state': 'RUNNING'}
    def fresh():
        fresh_entered.set()
        return read(acquisition, new, request=q, fresh=True)
    with ThreadPoolExecutor(2) as pool:
        owner = pool.submit(read, acquisition, old, request=q)
        assert entered.wait(1)
        checked = pool.submit(fresh)
        assert fresh_entered.wait(1)
        release.set()
        assert owner.result(2)['value']['state'] == 'PENDING'
        assert checked.result(2)['value']['state'] == 'RUNNING'
    assert calls == ['old', 'new']
    assert read(acquisition, new, request=q)['cached']


@pytest.mark.parametrize('changed', ['connection', 'generation', 'user', 'cluster', 'query', 'policy', 'capabilities', 'allocation', 'duration', 'output_limit'])
def test_different_read_contracts_never_coalesce(tmp_path, changed):
    acquisition = client(tmp_path)
    original_scope = {**SCOPE, 'user': 'one', 'cluster': 'alpha'}
    scope, q, rules, observed = dict(original_scope), query(), policy(), caps()
    calls = []
    def load(stop):
        calls.append(len(calls))
        return {'value': calls[-1]}
    first = read(acquisition, load, scope=original_scope)
    if changed in scope:
        scope[changed] = 'changed'
    elif changed == 'query':
        q['parameters']['job'] = 'other'
    elif changed == 'policy':
        rules['revision'] = '2'
    elif changed == 'capabilities':
        observed['version'] = '2'
    elif changed == 'allocation':
        q['request']['allocation'] = {'cluster': 'alpha', 'job_id': '1', 'attempt': 'new'}
    elif changed == 'duration':
        q['request']['duration'] = 1.5
    else:
        q['parameters']['output_limit'] = 10
    second = read(acquisition, load, scope=scope, request=q, rules=rules, observed=observed)
    assert first['value'] != second['value']
    assert len(calls) == 2


def test_external_invalidation_is_visible_and_cannot_overlap_old_owner(tmp_path):
    acquisition = client(tmp_path)
    independent = SharedQueryCache(tmp_path / 'cache')
    entered, release, replaced = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def old(stop):
        calls.append('old')
        entered.set()
        assert release.wait(2)
        return {'stale': True}
    def new(stop):
        calls.append('new')
        replaced.set()
        return {'new': True}
    with ThreadPoolExecutor(2) as pool:
        owner = pool.submit(read, acquisition, old)
        assert entered.wait(1)
        independent.invalidate(SCOPE)
        replacement = pool.submit(independent.query, SCOPE, query(), new, policy(), caps(), read_only=True)
        assert not replaced.wait(.06), 'Invalidation must retain the active reservation'
        release.set()
        with pytest.raises(ValueError, match='invalidated'):
            owner.result(2)
        assert replacement.result(2)['value'] == {'new': True}
    assert calls == ['old', 'new']
    independent.invalidate(SCOPE)
    assert read(acquisition, new)['cached'] is False


def test_byte_and_entry_bounds_are_preserved_during_concurrent_reads(tmp_path):
    acquisition = client(tmp_path, capacity=1, max_bytes=20000)
    entered, release = threading.Event(), threading.Event()
    def load(stop):
        entered.set()
        assert release.wait(2)
        return {}
    with ThreadPoolExecutor(1) as pool:
        owner = pool.submit(read, acquisition, load)
        assert entered.wait(1)
        with pytest.raises(ValueError, match='capacity'):
            read(acquisition, load, scope={**SCOPE, 'generation': 'other'})
        release.set()
        owner.result(2)
    q = query()
    q['request']['max_bytes'] = 30000
    with pytest.raises(ValueError, match='byte'):
        read(acquisition, lambda _: {}, request=q)


def test_single_multi_single_transitions_do_not_create_nested_executors(tmp_path, monkeypatch):
    acquisition = client(tmp_path)
    scheduler = WorkerScheduler(workers=2, mode='single')
    lane = scheduler.lane('research')
    original_init = WorkerScheduler.__init__
    def forbidden(*args, **kwargs):
        pytest.fail('Read acquisition allocated an additional worker scheduler')
    monkeypatch.setattr(WorkerScheduler, '__init__', forbidden)
    thread_ids = []
    def collect():
        executing = threading.get_ident()
        def load(stop):
            thread_ids.append(threading.get_ident())
            assert thread_ids[-1] == executing
            return {'thread': executing}
        return read(acquisition, load, fresh=True)
    try:
        for mode in ('single', 'multi', 'single', 'multi', 'single'):
            scheduler.request_mode(mode)
            result = lane.submit(collect).result(2)
            assert result['value']['thread'] == thread_ids[-1]
            assert scheduler.status()['mode'] == mode
        # Ordinary (coalescible) reads also execute within the existing permit.
        acquisition.invalidate(SCOPE)
        assert lane.submit(lambda: read(acquisition, lambda _: {'scheduler': WorkerScheduler.current() is scheduler})).result(2)['value']['scheduler']
    finally:
        monkeypatch.setattr(WorkerScheduler, '__init__', original_init)
        scheduler.shutdown(wait=True)


def test_recursive_read_fails_promptly_instead_of_waiting_for_its_own_future(tmp_path):
    acquisition = client(tmp_path)
    def recursive(stop):
        return read(acquisition, recursive)
    with pytest.raises(ValueError, match='Recursive'):
        read(acquisition, recursive)
    assert acquisition.stats()['active'] == 0


def test_idle_acquisition_clients_and_credentials_are_not_retained(tmp_path):
    acquisition = client(tmp_path)
    reference = weakref.ref(acquisition)
    read(acquisition, lambda _: {'value': 'private'})
    del acquisition
    gc.collect()
    assert reference() is None


@pytest.mark.parametrize('kwargs', [{'read_only': False}, {'read_only': 1}, {'read_only': True, 'fresh': 1}])
def test_actions_and_ambiguous_freshness_cannot_enter_read_acquisition(tmp_path, kwargs):
    acquisition = client(tmp_path)
    with pytest.raises(ValueError):
        acquisition.query(SCOPE, query(), lambda _: pytest.fail('Must not execute'), policy(), caps(), **kwargs)


def telemetry_manifest(tmp_path):
    from test_prometheus_source import manifest
    doc = manifest('https://telemetry.example')
    doc['cache_directory'] = str(tmp_path / 'telemetry')
    doc['policy'] = {'schema': 'tower.site-policy/v1', 'id': 'site', 'revision': '1',
                     'sources': ['site-metrics'], 'operations': ['query-range'],
                     'allocation_required': True, 'allow_intrusive': False,
                     'limits': {'min_interval': .5, 'max_duration': 5, 'max_bytes': 500000,
                                'max_records': 20000, 'max_inflight': 2}}
    return doc


def test_production_telemetry_concurrent_collects_share_acquisition(tmp_path, monkeypatch):
    from tower import prometheus_source as source
    from test_prometheus_source import response
    doc = telemetry_manifest(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []
    acquisition = A.for_cache(SharedQueryCache(doc['cache_directory']))
    def fetch(manifest, credential, stop):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return response()
    monkeypatch.setattr(source, '_fetch', fetch)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(source.collect, doc)
        assert entered.wait(1)
        second = pool.submit(source.collect, doc)
        wait_for(lambda: acquisition.stats()['leases'] == 2)
        release.set()
        assert first.result(2) == second.result(2)
    assert calls == [1]


def test_telemetry_moving_windows_keep_source_rate_floor(tmp_path, monkeypatch):
    from tower import prometheus_source as source
    doc = telemetry_manifest(tmp_path)
    starts = []
    def fetch(manifest, credential, stop):
        starts.append(time.monotonic())
        return {'status': 'success', 'data': {'resultType': 'matrix', 'result': []}}
    monkeypatch.setattr(source, '_fetch', fetch)
    source.collect(doc)
    doc.update(start=11, end=12)
    source.collect(doc)
    assert len(starts) == 2 and starts[1] - starts[0] >= .49


def test_telemetry_credential_rotation_and_anonymous_are_distinct(tmp_path, monkeypatch):
    from tower import prometheus_source as source
    doc = telemetry_manifest(tmp_path)
    credentials = []
    def fetch(manifest, credential, stop):
        credentials.append(credential)
        return {'status': 'success', 'data': {'resultType': 'matrix', 'result': []}}
    monkeypatch.setattr(source, '_fetch', fetch)
    source.collect(doc)
    doc['auth'] = {'kind': 'bearer_env', 'source': 'TOWER_FIXTURE_PRINCIPAL'}
    monkeypatch.setenv('TOWER_FIXTURE_PRINCIPAL', 'anonymous')
    source.collect(doc)
    monkeypatch.setenv('TOWER_FIXTURE_PRINCIPAL', 'second')
    source.collect(doc)
    assert credentials == [None, 'anonymous', 'second']


def test_governed_backends_share_production_read_and_preserve_output_contract(tmp_path):
    from tower.platform_session import GovernedBackend
    from tower.slurm import CommandError
    entered, release = threading.Event(), threading.Event()
    class Backend:
        calls = 0
        def run(self, argv, timeout):
            self.calls += 1
            entered.set()
            assert release.wait(2)
            return '123|RUNNING\n', .001
    backend = Backend()
    one = GovernedBackend(backend, policy(.5), SharedQueryCache(tmp_path / 'slurm'), SCOPE)
    two = GovernedBackend(backend, policy(.5), SharedQueryCache(tmp_path / 'slurm'), SCOPE)
    assert one.acquisition is two.acquisition
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(one.run, ['squeue'])
        assert entered.wait(1)
        b = pool.submit(two.run, ['squeue'])
        wait_for(lambda: one.acquisition.stats()['leases'] == 2)
        release.set()
        assert a.result(2)[0] == b.result(2)[0] == '123|RUNNING\n'
    assert backend.calls == 1
    with pytest.raises(CommandError, match='inspection limit'):
        one.command(['squeue'], timeout=1, limit=3, cancel=threading.Event())
    assert backend.calls == 2
