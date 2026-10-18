import hashlib
import json
from pathlib import Path
import struct
import threading
import time

import pytest

from raven import operations as O, raven_execution as R, wasi_analysis as W
from test_operations_sequences import operation_dashboard


def module(wat=None):
    runtime = pytest.importorskip('wasmtime', reason='install the optional wasi extra for actual runtime qualification')
    return bytes(runtime.wat2wasm(wat or (Path(__file__).parents[1]/'examples/wasi/f64-summary.wat').read_text()))


def manifest(binary, **limits):
    return {'schema': 'raven.wasi-analysis/v1', 'module': 'analysis.wasm', 'sha256': hashlib.sha256(binary).hexdigest(),
            'abi': W.ABI, 'limits': {'memory_bytes': 1 << 20, 'fuel': 1000000, 'wall_seconds': 5, 'output_bytes': 1024, **limits}}


def data(values=None):
    return {'schema': 'raven.f64-series/v1', 'values': values if values is not None else [1.25, -4., 9., .5]}


def prepare(tmp_path):
    binary = module()
    wasm = tmp_path/'module with spaces.wasm'
    wasm.write_bytes(binary)
    declared = manifest(binary)
    declared['module'] = str(wasm)
    descriptor = tmp_path/'manifest.json'
    descriptor.write_text(json.dumps(declared))
    source = tmp_path/'series.json'
    source.write_text(json.dumps(data()))
    return {'manifest': str(descriptor), 'input': str(source), 'action': 'run', 'destination': str(tmp_path/'result')}


@pytest.mark.parametrize('values', [[1., 2., 3.], [-2., -2., -2.], [1e12, 1., -1e12], [0., -0., .5], [2**60], list(range(1000))])
def test_actual_wasi_summary_matches_sequential_trusted_reference(values):
    binary = module()
    result = W.execute(manifest(binary), binary, data(values))
    total = 0.
    for number in values:
        total += number
    expected = struct.pack('<Qddd', len(values), total, min(values), max(values))
    assert result['output_sha256'] == hashlib.sha256(expected).hexdigest()
    assert not result['scientific_validity_verified']
    assert result['preopens'] == [] and not result['network'] and not result['randomness']


def test_review_apply_persistence_and_changed_module_refusal(tmp_path, monkeypatch):
    params = prepare(tmp_path)
    ctx = O.Context(scope={'site': 'fixture'})
    execute = W.execute
    monkeypatch.setattr(W, 'execute', lambda *_args, **_kwargs: pytest.fail('review executed guest'))
    review = O.run('wasi-analysis', params, ctx)
    assert review['plan'] and not Path(params['destination']).exists()
    monkeypatch.setattr(W, 'execute', execute)
    result = O.apply('wasi-analysis', review['plan'], ctx)
    assert json.loads((Path(params['destination'])/'analysis.json').read_text())['summary'] == result['data']['summary']
    assert (Path(params['destination'])/'.raven-receipt.json').is_file()
    with pytest.raises(ValueError, match='new directory'):
        O.apply('wasi-analysis', review['plan'], ctx)
    declared = json.loads(Path(params['manifest']).read_text())
    Path(declared['module']).write_bytes(module('(module (func (export "_start")))'))
    with pytest.raises(ValueError, match='SHA256'):
        O.apply('wasi-analysis', review['plan'], ctx)


@pytest.mark.parametrize('import_name', ['path_open', 'path_readlink', 'fd_prestat_get', 'environ_get', 'sock_open', 'random_get', 'clock_time_get'])
def test_ungranted_files_symlinks_paths_network_environment_clocks_refused(tmp_path, import_name):
    canary = tmp_path/'secret'
    canary.write_text('PRIVATE-CANARY')
    (tmp_path/'symlink').symlink_to(canary)
    # The module never obtains a directory capability. Even an import with
    # paths naming a real canary or symlink fails before its start function.
    binary = module(f'(module (import "wasi_snapshot_preview1" "{import_name}" (func)) (memory (export "memory") 1) (data (i32.const 0) "../secret\\00symlink\\00") (func (export "_start")))')
    with pytest.raises(ValueError, match='Unsupported host import'):
        W.execute(manifest(binary), binary, data())
    assert canary.read_text() == 'PRIVATE-CANARY'


@pytest.mark.parametrize('wat,limits,reason', [
    ('(module (memory (export "memory") 2) (func (export "_start")))', {'memory_bytes': 65536}, 'memory'),
    ('(module (func (export "_start") (loop $again (br $again))))', {'fuel': 1000}, 'fuel'),
    ('(module (import "wasi_snapshot_preview1" "fd_write" (func $w (param i32 i32 i32 i32) (result i32))) (memory (export "memory") 1) (func (export "_start") (i32.store (i32.const 4) (i32.const 64)) (drop (call $w (i32.const 1) (i32.const 0) (i32.const 1) (i32.const 8)))))', {'output_bytes': 32}, 'output budget'),
    ('(module (import "wasi_snapshot_preview1" "fd_read" (func $r (param i32 i32 i32 i32) (result i32))) (memory (export "memory") 1) (func (export "_start") (drop (call $r (i32.const 0) (i32.const 65535) (i32.const 1024) (i32.const 0)))))', {}, 'memory range'),
])
def test_hostile_guest_resource_limits(wat, limits, reason):
    binary = module(wat)
    with pytest.raises(ValueError, match=reason):
        W.execute(manifest(binary, **limits), binary, data())


def expensive_module():
    return module('(module (import "wasi_snapshot_preview1" "fd_write" (func $w (param i32 i32 i32 i32) (result i32))) (memory (export "memory") 1) (func (export "_start") (loop $again (drop (call $w (i32.const 1) (i32.const 0) (i32.const 0) (i32.const 8))) (br $again))))')


def test_cancellation_and_wall_time_are_independent_of_guest_fuel():
    binary = expensive_module()
    stop = threading.Event()
    timer = threading.Timer(.1, stop.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match='cancelled'):
            W.execute(manifest(binary, fuel=100000000), binary, data(), cancel=stop)
    finally:
        timer.cancel()
    assert time.monotonic()-started < 2
    started = time.monotonic()
    with pytest.raises(RuntimeError, match='wall-time'):
        W.execute(manifest(binary, fuel=100000000, wall_seconds=1), binary, data())
    assert time.monotonic()-started < 3


def test_parent_caps_runner_output_independently_of_runtime(tmp_path, monkeypatch):
    binary = module()
    runner = tmp_path/'_wasi_runner.py'
    runner.write_text('import sys\nsys.stdout.buffer.write(b"x" * (2 << 20))\nsys.stdout.flush()\n')
    monkeypatch.setattr(W, '__file__', str(tmp_path/'wasi_analysis.py'))
    with pytest.raises(RuntimeError, match='parent output budget'):
        W.execute(manifest(binary), binary, data())


def test_missing_or_unqualified_runtime_is_actionable(monkeypatch):
    def missing(_name):
        raise W.importlib.metadata.PackageNotFoundError
    monkeypatch.setattr(W.importlib.metadata, 'version', missing)
    with pytest.raises(ValueError, match='wasi extra'):
        W.capability()
    monkeypatch.setattr(W.importlib.metadata, 'version', lambda _: '0.0.0')
    with pytest.raises(ValueError, match='found 0.0.0'):
        W.capability()


@pytest.mark.parametrize('values', [[], [True], [float('nan')], [float('inf')], [10**1000], [2**53+1], [-(2**53+1)], [10**50], ['1'], [1]*65536])
def test_invalid_input_is_rejected_before_guest(values):
    with pytest.raises(ValueError):
        W.input_bytes(data(values))


def test_managed_light_defers_and_actual_owned_worker_executes(tmp_path):
    from raven.raven_worker_client import WorkerClient
    params = prepare(tmp_path)
    # The runner must preserve inherited hard CPU limits when the requested
    # wall-time ceiling equals the worker's CPU ceiling.
    declared = json.loads(Path(params['manifest']).read_text())
    declared['limits']['wall_seconds'] = 30
    Path(params['manifest']).write_text(json.dumps(declared))
    assert O.run('wasi-analysis', params, O.Context(cfg={'raven': {'managed': True}}))['status'] == 'deferred'
    client = WorkerClient({'site_kind': 'desktop', 'desktop_explicit': True, 'context': {},
                          'resources': {'memory_mib': 512, 'cpus': 1, 'max_tasks': 1, 'wall_seconds': 40}})
    try:
        client.start()
        until = time.monotonic()+8
        while not client.ready and time.monotonic() < until:
            client.poll()
            time.sleep(.01)
        assert client.ready, client.error
        ctx = O.Context(scope={'site': 'test'}, execution=R.ExecutionRouter({'managed': True}, lambda: client))
        review = O.run('wasi-analysis', params, ctx)
        assert review['plan']['raven_worker']
        result = O.apply('wasi-analysis', review['plan'], ctx)
        assert result['data']['summary']['count'] == 4
        assert result['execution_receipt']['operation'] == 'wasi-analysis-execute'
        assert Path(params['destination']).is_dir()
    finally:
        client.close()


def test_palette_requires_visible_review_before_execution(operation_dashboard, tmp_path):
    import shlex
    params = prepare(tmp_path)
    app = operation_dashboard.app
    app.run_command('ops wasi-analysis')
    app.run_command('ops run wasi-analysis '+' '.join(key+'='+shlex.quote(value) for key, value in params.items()))
    assert app.ops_state['result']['status'] == 'ok', app.ops_state['result']
    app.run_command('ops apply')
    assert not Path(params['destination']).exists()
    app.run_command('ops review')
    operation_dashboard.draw(80, 24)
    assert not app.ops_state['review_truncated']
    assert app.ops_state['confirm_digest']
    app.run_command('ops apply')
    assert Path(params['destination']).is_dir(), app.ops_state['result']


def test_manifest_matches_published_schema_and_rejects_extra_authority():
    import jsonschema
    value = manifest(module())
    schema = json.loads((Path(__file__).parents[1]/'docs/schemas/wasi-analysis.schema.json').read_text())
    jsonschema.Draft202012Validator(schema).validate(value)
    value['preopens'] = ['/']
    with pytest.raises(ValueError, match='fields'):
        W.manifest(value)
