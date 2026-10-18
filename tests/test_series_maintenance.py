"""Named local maintenance is finite and retains state ownership until reaped."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest
from raven.model import Store
from raven import series_maintenance as M
from raven.series_worker import execute
from raven.state_ownership import StateOwner, StateOwnershipError


def fixture(tmp_path):
    store = Store(str(tmp_path)); directory = tmp_path / 'series'; directory.mkdir(exist_ok=True)
    source = directory / ('v2-37-' + 'a'*64 + '.jsonl'); source.write_text('{"t":1,"cpu":0.5}\n')
    return store, source


def test_real_local_helper_migrates_under_original_owner(tmp_path):
    store, source = fixture(tmp_path)
    with store._persist_io_lock:
        result = M.run(store, {'action': 'migrate', 'source': str(source), 'apply': True})
    assert result['status'] == 'published' and source.exists() and source.with_suffix('.rvb').exists()
    with pytest.raises(StateOwnershipError): StateOwner(tmp_path, strict=True)
    store.close()
    owner = StateOwner(tmp_path, strict=True); owner.close()


def test_shutdown_cancels_reaps_and_cleans_only_its_staging_file(tmp_path, monkeypatch):
    store, source = fixture(tmp_path)
    previous = source.with_suffix('.rvb').with_name(source.with_suffix('.rvb').name + '.stage-older')
    previous.write_text('preserve unrelated staging evidence')
    started = threading.Event(); processes = []; errors = []
    real = subprocess.Popen
    script = "import sys,json,pathlib,time; r=json.load(sys.stdin); p=pathlib.Path(r['source']).with_suffix('.rvb'); pathlib.Path(str(p)+'.stage-'+r['staging_token']).write_text('incomplete'); time.sleep(30)"
    def spawn(argv, **kw):
        child = real([sys.executable, '-c', script], **kw); processes.append(child); started.set(); return child
    monkeypatch.setattr(M.subprocess, 'Popen', spawn)
    def work():
        try:
            with store._persist_io_lock: M.run(store, {'action': 'migrate', 'source': str(source), 'apply': True})
        except Exception as exc: errors.append(exc)
    thread = threading.Thread(target=work); thread.start(); assert started.wait(2)
    deadline = time.monotonic() + 2
    while len(list(source.parent.glob('*.stage-*'))) < 2 and time.monotonic() < deadline: time.sleep(.01)
    assert len(list(source.parent.glob('*.stage-*'))) == 2
    store.close(); thread.join(2)
    assert not thread.is_alive() and processes[0].poll() is not None
    assert errors and 'cancelled' in str(errors[0])
    assert list(source.parent.glob('*.stage-*')) == [previous]
    assert source.read_text() == '{"t":1,"cpu":0.5}\n'
    owner = StateOwner(tmp_path, strict=True); owner.close()


def test_wall_timeout_is_finite_and_reaped(tmp_path, monkeypatch):
    store, source = fixture(tmp_path); real = subprocess.Popen; processes = []
    def spawn(argv, **kw):
        child = real([sys.executable, '-c', 'import time;time.sleep(30)'], **kw); processes.append(child); return child
    monkeypatch.setattr(M.subprocess, 'Popen', spawn); monkeypatch.setattr(M, 'WALL_SECONDS', .15)
    with pytest.raises(ValueError, match='wall budget'):
        with store._persist_io_lock: M.run(store, {'action': 'migrate', 'source': str(source)})
    assert processes[0].poll() is not None
    store.close()


def test_stalled_request_pipe_cannot_delay_wall_timeout(tmp_path, monkeypatch):
    import fcntl
    store, source = fixture(tmp_path); real = subprocess.Popen; processes = []
    def spawn(argv, **kw):
        child = real([sys.executable, '-c', 'import time;time.sleep(30)'], **kw)
        fcntl.fcntl(child.stdin.fileno(), fcntl.F_SETPIPE_SZ, 4096)
        processes.append(child)
        return child
    monkeypatch.setattr(M.subprocess, 'Popen', spawn)
    monkeypatch.setattr(M, 'WALL_SECONDS', .15)
    started = time.monotonic()
    try:
        with pytest.raises(ValueError, match='wall budget'):
            with store._persist_io_lock:
                M.run(store, {'action': 'retention', 'source': str(source),
                              'settings': {'invalid_field': 'x' * 12000}})
        assert time.monotonic() - started < 2
        assert processes[0].poll() is not None
    finally:
        store.close()


def test_closed_or_out_of_scope_requests_never_spawn(tmp_path, monkeypatch):
    store, source = fixture(tmp_path)
    def forbidden(*args, **kw): pytest.fail('must not spawn')
    monkeypatch.setattr(M.subprocess, 'Popen', forbidden)
    with pytest.raises(ValueError, match='owned state'):
        M.run(store, {'action': 'migrate', 'source': str(tmp_path / 'elsewhere.jsonl')})
    store.close()
    with pytest.raises(ValueError, match='cancelled'): M.run(store, {'action': 'migrate', 'source': str(source)})
    with pytest.raises(ValueError, match='Invalid'): execute({'action': 'shell', 'source': str(source)})
