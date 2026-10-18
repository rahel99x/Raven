"""Run only in a clean prototype environment; emit actual imports and outcomes."""
import json
import importlib.util
import os
from pathlib import Path
import resource
import sys
import tempfile
import time
_bench_allocations = os.environ.get('RAVEN_W42_ALLOCATIONS') == '1'
if _bench_allocations:
    import tracemalloc
    tracemalloc.start()
_bench_cpu_start = time.process_time()
begin = time.perf_counter()
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.slurm import Slurm, FakeBackend
from raven.views import Views
from raven.layout import Glyphs
from raven import launch, launch_runtime, run_home, operations
from raven.logs import LogBuffer
from raven.plugins import load as load_plugins
from raven.raven_worker_client import WorkerClient
from raven.raven_execution import ExecutionRouter
_bench_import_seconds = time.perf_counter() - begin
_bench_import_cpu_seconds = time.process_time() - _bench_cpu_start
_bench_import_count = len([name for name in sys.modules if name == 'raven' or name.startswith('raven.')])
with tempfile.TemporaryDirectory(prefix='raven-distribution-probe-') as temporary:
    folder = Path(temporary)
    config_path = folder / 'config.json'
    config_path.write_text('{"project":"preserve-user-value"}\n')
    original = config_path.read_bytes()
    store = Store(persist=False)
    scheduler = Slurm(FakeBackend('prototype', speed=0), 'prototype')
    store.apply_jobs(scheduler.jobs())
    cfg = Config({'log_lines': 0, 'startup_animation': False})
    app = App(store, None, None, cfg, 'prototype', interactive=True)
    views = Views(Glyphs(True), cfg)
    app.views_ref = views
    rows, _ = views.compose(store.snapshot(), app, 80, 24)
    _bench_first_frame_seconds = time.perf_counter() - begin
    _bench_first_frame_cpu_seconds = time.process_time() - _bench_cpu_start
    _bench_rows = [[list(segment) for segment in row] for row in rows]
    _bench_job_fields = ('id', 'name', 'partition', 'state', 'elapsed', 'limit', 'nodes', 'cpus', 'gpu_type', 'gpus', 'nodelist', 'mem_req', 'reason', 'priority', 'dependency', 'account', 'qos', 'user')
    _bench_jobs = [{field: getattr(job, field) for field in _bench_job_fields} for job in sorted(store.jobs, key=lambda job: job.id)]
    assert rows and store.jobs
    script = folder / 'science.sh'
    script.write_text('#!/bin/sh\nprintf "prototype log\\n"\n')
    env = {'PATH': os.defpath}
    plan = launch.resolve_plan([str(script), '--project', str(folder), '--local', '--no-cpu-memory', '--no-gpu', '--no-trace', '--no-export-on-finish'], environ=env)
    receipt = launch_runtime.start_local_plan(plan, environ=env)
    inventory = Path(receipt['run_dir']) / 'run.json'
    until = time.monotonic() + 10
    while time.monotonic() < until:
        if inventory.exists() and json.loads(inventory.read_text()).get('state') == 'COMPLETED':
            break
        time.sleep(0.02)
    home = run_home.load(run_home.receipt_binding(receipt))
    assert home['inventory']['state'] == 'COMPLETED'
    logs = LogBuffer(receipt['logs']['stdout'], max_bytes=4096)
    logs.refresh()
    assert 'prototype log' in '\n'.join(logs.lines)
    plugin_file = folder / 'compatibility.py'
    plugin_file.write_text('def setup(api):\n    api.command("compatibility", lambda app, args: "ok")\n')
    plugin = load_plugins([str(plugin_file)])
    assert not plugin.errors and plugin.commands['compatibility'][0](None, []) == 'ok'
    assert importlib.util.find_spec('pyte') is None, 'Probe requires optional terminal dependency absence'
    client = WorkerClient({'site_kind': 'desktop', 'desktop_explicit': True, 'context': {}, 'resources': {'memory_mib': 512}})
    try:
        client.start()
        until = time.monotonic() + 8
        while not client.ready and time.monotonic() < until:
            client.poll()
            time.sleep(0.01)
        assert client.ready, client.error
        router = ExecutionRouter({'managed': True}, lambda: client)
        result = router.dispatch('log-search', {'text': 'hello\nprototype log\n', 'needle': 'prototype'})
        assert result['result']['matches'][0]['line'] == 2
    finally:
        client.close()
    assert config_path.read_bytes() == original
    from raven.distribution_profile import profile
    optional = profile()['optional_modules']
    missing = [name for name in optional if name not in sys.modules]
    mode = sys.argv[1] if len(sys.argv) > 1 else 'complete'
    if mode == 'core':
        assert len(missing) == len(optional), set(optional) & set(sys.modules)
        from raven import ops_ui
        ops_ui.restore(app, {'pinned': ['statistics']})
        assert ops_ui.save(app)['pinned'] == ['statistics'], 'Missing extensions must preserve saved favorites'
        try:
            operations.specification('statistics')
        except ValueError as exc:
            assert 'matching experimental Raven lab wheel' in str(exc)
        else:
            raise AssertionError('Core unexpectedly includes the lab adapter')
    elif mode == 'lab':
        from raven.science_stats import compare
        result = compare({'schema': 'raven.statistics/v1', 'design': 'independent', 'unit': 'run', 'control': [{'id': 'a', 'value': 1}, {'id': 'b', 'value': 2}, {'id': 'c', 'value': 3}], 'treatment': [{'id': 'd', 'value': 4}, {'id': 'e', 'value': 5}, {'id': 'f', 'value': 6}]})
        assert result['difference'] == 3
        assert operations.specification('statistics')
    _bench_self = resource.getrusage(resource.RUSAGE_SELF)
    _bench_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    _bench_python_peak = tracemalloc.get_traced_memory()[1] if _bench_allocations else None
    _bench_io = {}
    try:
        for _bench_line in Path('/proc/self/io').read_text().splitlines():
            _bench_key, _bench_value = _bench_line.split(':', 1)
            _bench_io[_bench_key] = int(_bench_value.strip())
    except OSError:
        pass
    print(json.dumps({'schema': 'raven.distribution-workflow-probe/v1', 'mode': mode, 'elapsed_seconds': time.perf_counter() - begin, 'peak_process_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, 'raven_imports': sorted((name for name in sys.modules if name == 'raven' or name.startswith('raven.'))), 'checks': ['80-column fake monitor', 'actual local launch', 'retained logs', 'exact run home', 'installed Heavy child', 'plugin API', 'config preserved'], 'human_study': False, 'live_scheduler': False, 'instrumentation': 'phase markers; separate tracemalloc runs; common complete path', 'phase': {'raven_import_seconds': _bench_import_seconds, 'raven_import_cpu_seconds': _bench_import_cpu_seconds, 'first_frame_seconds': _bench_first_frame_seconds, 'first_frame_cpu_seconds': _bench_first_frame_cpu_seconds, 'workflow_cpu_seconds': time.process_time() - _bench_cpu_start, 'raven_import_count_after_imports': _bench_import_count, 'process_cpu_seconds': _bench_self.ru_utime + _bench_self.ru_stime, 'reaped_children_cpu_seconds': _bench_children.ru_utime + _bench_children.ru_stime, 'largest_reaped_child_peak_rss_kib': _bench_children.ru_maxrss, 'python_peak_allocated_bytes': _bench_python_peak, 'process_io': _bench_io}, 'installed_raven_path': sys.modules['raven'].__file__, 'semantic_fixture': {'jobs': _bench_jobs, 'frame_dimensions': [80, 24], 'frame_row_count': len(rows), 'run_state': home['inventory']['state'], 'log_lines': list(logs.lines), 'worker_result': result['result'], 'config_bytes': original.decode('utf-8'), 'fake_scheduler_calls': scheduler.b.calls}, 'frame_rows': _bench_rows, 'allocation_run': _bench_allocations}))
