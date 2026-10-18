import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest

from raven import operations as O, process_profile as P, raven_execution as R
from raven.raven_worker_client import WorkerClient


def identity():
    return dict(site_kind='desktop', host=socket.gethostname(), uid=os.getuid(), pid=os.getpid(),
                verified=True, verification='explicit approved desktop policy')


def params(tmp_path, pid=None):
    return dict(pid=str(pid or os.getpid()), seconds='.2', interval_ms='100', cpu_budget_percent='10', destination=str(tmp_path / 'receipt'))


def test_read_only_owned_process_sampling_has_explicit_limits_and_unknowns():
    result = P.collect(P.inspect(os.getpid(), identity()), dict(seconds=.2, cpu_budget_percent=10), identity())
    assert result['samples'] >= 1 and result['rows']
    assert result['target']['rank'] is None
    assert all(sum(row['state_proportions'].values()) == pytest.approx(1) for row in result['rows'])
    assert all(row['rank'] is None and row['start_ticks'] > 0 for row in result['rows'])
    assert result['coverage']['complete'] is False
    assert any('R combines' in value for value in result['limitations'])
    assert any('scientific-job attribution' in value for value in result['limitations'])


def test_pid_reuse_exit_and_wrong_host_refused():
    target = P.inspect(os.getpid(), identity())
    with pytest.raises(ValueError, match='reused'):
        P.inspect(os.getpid(), identity(), target['start_ticks'] + 1)
    with pytest.raises(ValueError, match='host'):
        P.inspect(os.getpid(), dict(identity(), host='another-host'))
    process = subprocess.Popen([sys.executable, '-c', 'pass']); process.wait(timeout=5)
    with pytest.raises((OSError, ValueError)): P.inspect(process.pid, identity())


def test_review_does_not_sample_and_scope_cannot_be_spoofed(tmp_path, monkeypatch):
    from raven import ops_process_profile
    monkeypatch.setattr(P, 'collect', lambda *_a, **_kw: pytest.fail('review sampled'))
    result = ops_process_profile.run('process-profile', params(tmp_path), O.Context(scope={'site':'test'}, verified_execution=identity()))
    assert result['plan']['payload']['target']['pid'] == os.getpid() and not list(tmp_path.iterdir())
    with pytest.raises(ValueError, match='authenticated'):
        R.execute_operation('process-profile', {'params': params(tmp_path)}, context={'verified_execution': identity()})
    deferred = O.run('process-profile', params(tmp_path), O.Context(cfg={'raven': {'managed': True}}))
    assert deferred['status'] == 'deferred'


def test_cancel_cleans_pinned_fds_without_workload_action():
    cancel = threading.Event(); cancel.set()
    target = P.inspect(os.getpid(), identity())
    before = len(list(Path('/proc/self/fd').iterdir()))
    result = P.collect(target, dict(seconds=10), identity(), cancel=cancel)
    assert result['status'] == 'cancelled' and result['samples'] == 0
    assert len(list(Path('/proc/self/fd').iterdir())) == before


def test_permission_and_missing_counters_are_unknown(monkeypatch):
    real = P._read
    def unavailable(fd, name, *args):
        if name in {'io', 'sched', 'status'}: raise PermissionError('hidepid fixture')
        return real(fd, name, *args)
    monkeypatch.setattr(P, '_read', unavailable)
    result = P.collect(P.inspect(os.getpid(), identity()), dict(seconds=.1), identity())
    assert result['rows']
    assert all('read_bytes' in row['unavailable'] and 'migrations' in row['unavailable'] for row in result['rows'])
    assert all('read_bytes' not in row['counter_deltas'] for row in result['rows'])


def test_task_churn_does_not_alias_or_leak_fds():
    code = 'import threading,time\nfor i in range(30):\n t=threading.Thread(target=lambda:time.sleep(.01));t.start();t.join()\ntime.sleep(.2)'
    process = subprocess.Popen([sys.executable, '-c', code])
    try:
        before = len(list(Path('/proc/self/fd').iterdir()))
        result = P.collect(P.inspect(process.pid, identity()), dict(seconds=.4, cpu_budget_percent=10), identity())
        keys = [(r['pid'], r['tid'], r['start_ticks']) for r in result['rows']]
        assert len(keys) == len(set(keys)) and len(keys) <= P.MAX_TASKS
        assert len(list(Path('/proc/self/fd').iterdir())) == before
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None: process.terminate(); process.wait(timeout=5)


@pytest.mark.parametrize('value', ['nan', 'inf', '-1', '61', True])
def test_invalid_options_are_rejected(value):
    with pytest.raises(ValueError): P.settings(dict(seconds=value))


def test_owned_descendant_is_included_but_unrelated_process_is_not(tmp_path):
    pidfile = tmp_path / 'child.pid'
    code = 'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(2)"]);open(sys.argv[1],"w").write(str(p.pid)); p.wait()'
    parent = subprocess.Popen([sys.executable, '-c', code, str(pidfile)])
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'])
    try:
        deadline = time.monotonic() + 3
        while not pidfile.exists() and time.monotonic() < deadline: time.sleep(.005)
        child = int(pidfile.read_text())
        result = P.collect(P.inspect(parent.pid, identity()), dict(seconds=.2, cpu_budget_percent=10), identity())
        pids = {r['pid'] for r in result['rows']}
        assert parent.pid in pids and child in pids and unrelated.pid not in pids
    finally:
        parent.wait(timeout=5); unrelated.wait(timeout=5)


def test_real_heavy_worker_review_capture_and_private_persistence(tmp_path):
    client = WorkerClient(dict(site_kind='desktop', desktop_explicit=True, context={},
                               resources=dict(memory_mib=512, cpus=1, max_tasks=1, wall_seconds=70)))
    try:
        client.start(); deadline = time.monotonic() + 8
        while not client.ready and time.monotonic() < deadline: client.poll(.01)
        assert client.ready, client.error
        router = R.ExecutionRouter({'managed': True}, lambda: client)
        ctx = O.Context(scope={'site': 'test'}, execution=router, state_dir=str(tmp_path / 'state'))
        review = O.run('process-profile', params(tmp_path), ctx)
        assert review['plan']['payload']['execution'] == client.identity
        result = O.apply('process-profile', review['plan'], ctx)
        output = tmp_path / 'receipt/process-profile.json'
        assert result['data'] == json.loads(output.read_text())
        assert result['data']['target']['pid'] == os.getpid()
        assert result['data']['collector_execution']['pid'] != os.getpid()
        assert output.stat().st_mode & 0o777 == 0o600
        with pytest.raises(ValueError): O.apply('process-profile', review['plan'], ctx)
    finally:
        client.close()


def test_reparented_grandchild_chain_is_rejected_before_counter_commit(monkeypatch):
    from types import SimpleNamespace
    nodes = {10: dict(start_ticks=1, state='S', ppid=99),
             20: dict(start_ticks=2, state='S', ppid=100)}
    monkeypatch.setattr(P, '_stat', lambda fd: nodes[fd])
    monkeypatch.setattr(P.os, 'fstat', lambda fd: SimpleNamespace(st_uid=1000))
    chain = ((100, 10, 1), (200, 20, 2))
    P._verify_chain(chain, 1000)
    nodes[20]['ppid'] = 999
    with pytest.raises(ValueError, match='ancestry'):
        P._verify_chain(chain, 1000)
    nodes[20]['ppid'] = 100; nodes[10]['state'] = 'Z'
    with pytest.raises(ValueError, match='ancestor'):
        P._verify_chain(chain, 1000)


def test_foreign_uid_refused_before_metadata_and_fd_closed(monkeypatch):
    from types import SimpleNamespace
    real = os.fstat
    before = len(list(Path('/proc/self/fd').iterdir()))
    monkeypatch.setattr(P.os, 'fstat', lambda fd: SimpleNamespace(st_uid=os.getuid() + 1))
    with pytest.raises(ValueError, match='not owned'):
        P._open(os.getpid())
    monkeypatch.setattr(P.os, 'fstat', real)
    assert len(list(Path('/proc/self/fd').iterdir())) == before
