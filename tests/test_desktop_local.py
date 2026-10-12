"""Native desktop commands and opt-in state scopes use real temporary files."""
from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tower import cli
from tower.config import Config
from tower.model import Store
from tower.record import RecordingBackend
from tower.remote import LocalFiles, SshBackend
from tower.slurm import Backend


ROOT = Path(__file__).resolve().parents[1]


def test_native_desktop_automatically_links_and_refreshes_actual_project_reports(native_desktop, monkeypatch):
    """Local Slurm subprocess metadata finds the copied producer's exact run."""
    fixture = native_desktop
    shutil.copytree(ROOT / 'examples/project-template', fixture.root, dirs_exist_ok=True)
    spec = importlib.util.spec_from_file_location('desktop_run_reporter', fixture.root / 'reporting.py')
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    run = producer.begin_run(fixture.root, 'native-desktop-7', name='desktop-cpu',
                             script='experiment.py', job_id='7')
    from tower import clock, metric_live
    origin = clock.now()
    producer.write_metric(run, {'native_loss': .75}, step=1, t=origin)
    producer.publish_research(run, 'predict', {'kind': 'tower.planning', 'version': 1,
                                              'history': [], 'job_id': '7'})
    output = fixture.run('--once', '--tab', 'research')
    assert output.returncode == 0, output.stderr
    assert 'native_loss' in output.stdout and 'DEMO' not in output.stdout
    session = fixture.build('--no-state', '--rate', '50')
    try:
        cli.settle(session, 'research')
        from tower import project_ui
        binding = project_ui.selected_binding(session.app)
        assert binding['job_id'] == '7' and binding['run_id'] == run.name
        assert binding['metrics_file'] == str(run / 'metrics.jsonl')
        assert binding['planning_files']['predict'] == str(run / 'reports/predict.json')
        generation = session.app.research.generation
        wall = [origin + .5]
        monkeypatch.setattr(clock, 'now', lambda: wall[0])
        producer.write_metric(run, {'native_loss': .25}, step=2, t=wall[0])
        producer.publish_research(run, 'workflow', {'kind': 'tower.planning', 'version': 1,
                                                   'jobs': [], 'job_id': '7'})
        project_ui.settle(session.app, session.store.snapshot())
        binding = project_ui.selected_binding(session.app)
        assert binding['planning_files']['workflow'] == str(run / 'reports/workflow.json')
        assert session.app.research.generation > generation
        acquired = session.app.research.request(
            session.app.research.context(session.store.snapshot(), session.app), wait=True, force=True)
        # The real file reader publishes the new value immediately. Buffered
        # presentation must not turn its deliberate delay into collection lag.
        assert acquired['latest']['native_loss'] == .25
        assert [point['value'] for point in acquired['series']['native_loss']] == [.75, .25]
        session.views.compose(session.store.snapshot(), session.app, 160, None)
        identity = next(key for key, entry in metric_live.initialize(session.app)['entries'].items()
                        if key[2] == 'native_loss' and entry.get('playback'))
        playback = metric_live.playback_status(session.app, identity)
        assert playback.end == origin
        wall[0] += playback.delay + .5
        text = '\n'.join(''.join(segment for segment, _ in row)
                         for row in session.views.compose(session.store.snapshot(), session.app, 160, None)[0])
        assert 'native_loss' in text and '0.25' in text
        assert metric_live.playback_status(session.app, identity).end == origin + .5
        assert session.sampler.effective_interval('jobs') == .5
        assert session.app.tab == 'research' and session.app.selected_id == '7'
    finally:
        session.close()
    assert {call[0] for call in fixture.calls()} <= {'squeue', 'scontrol', 'sinfo', 'sacct', 'sstat', 'sshare'}


@pytest.mark.parametrize('flags,interval', [
    (('--interval', '5'), 5.0),
    (('--interval', '.5'), .5),
    (('--interval', '60'), 60.0),
    (('--interval', '.01'), .5),
    (('--interval', '5', '--rate', '50'), .5),
    (('--interval', '.5', '--rate', '1'), 5.0),
])
def test_explicit_interval_selects_bounded_polling_and_rate_takes_precedence(native_desktop, flags, interval):
    session = native_desktop.build('--no-state', *flags)
    try:
        assert session.sampler.effective_interval('jobs') == interval
        # --interval controls the queue. A separately configured native source
        # retains its own base instead of inheriting an unrelated slow queue.
        native_interval = 5.0 if flags == ('--interval', '60') else interval
        assert session.sampler.effective_interval('live') == native_interval
        assert session.sampler.effective_interval('gpu') == native_interval
        assert native_desktop.calls() == []
    finally:
        session.close()


CPU_ROW = ('7|desktop-cpu|localcpu|RUNNING|00:00:12|01:00:00|1|2|N/A|fedora-box|1G|'
           '2026-10-07T00:00:00|2026-10-07T00:00:00|None|1|(null)||normal|N/A|/tmp/cpu.sbatch\n')
NATIVE_COMMAND = r'''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
with open(os.environ['TOWER_DESKTOP_CALLS'], 'a') as output:
    output.write(json.dumps([name, *sys.argv[1:]]) + '\n')
scenario = json.loads(pathlib.Path(os.environ['TOWER_DESKTOP_SCENARIO']).read_text())
if name == 'squeue':
    print('' if '--start' in sys.argv else scenario.get('jobs', ''), end='')
    sys.exit(0)
if name == 'sinfo':
    print(scenario.get('node_output', 'fedora-box localcpu (null) (null) idle 0/8/0/8 32768 0\n') if '-N' in sys.argv
          else scenario.get('partition_output', 'localcpu*|up|infinite|1|0/1/0/1|0/8/0/8\n'), end='')
    sys.exit(0)
if name == 'scontrol':
    if 'hostnames' in sys.argv:
        print('fedora-box'); sys.exit(0)
    if 'node' in sys.argv:
        print('NodeName=fedora-box State=IDLE CPUTot=8 CPUAlloc=0 CPULoad=0 RealMemory=32768 '
              'FreeMem=25000 Gres=(null) GresUsed=(null) Partitions=localcpu'); sys.exit(0)
    if 'job' in sys.argv:
        print('JobId=7 JobName=desktop-cpu JobState=RUNNING WorkDir=' + os.environ['TOWER_DESKTOP_ROOT']
              + ' StdOut=' + os.environ['TOWER_DESKTOP_STDOUT']
              + ' StdErr=' + os.environ['TOWER_DESKTOP_STDERR']); sys.exit(0)
if name == 'sstat' and scenario.get('live', True):
    print('7.batch|00:00:04|128M|0|fedora-box|64M|1|00:00:04|0|fedora-box'); sys.exit(0)
if name in ('sacct', 'sstat', 'sshare', 'sreport', 'sacctmgr'):
    print(name + ': accounting or optional telemetry is not configured', file=sys.stderr); sys.exit(1)
print('FORBIDDEN desktop command: ' + name, file=sys.stderr)
sys.exit(90)
'''


@pytest.fixture
def native_desktop(tmp_path, monkeypatch):
    commands = tmp_path / 'bin'
    commands.mkdir()
    for name in ('squeue', 'scontrol', 'sinfo', 'sacct', 'sstat', 'sshare', 'sreport',
                 'sacctmgr', 'sbatch', 'scancel', 'srun', 'ssh', 'nvidia-smi'):
        executable = commands / name
        executable.write_text('#!' + sys.executable + '\n' + NATIVE_COMMAND)
        executable.chmod(0o755)
    calls, scenario = tmp_path / 'calls.jsonl', tmp_path / 'scenario.json'
    calls.write_text('')
    scenario.write_text(json.dumps({'jobs': CPU_ROW, 'live': True}))
    stdout, stderr = tmp_path / 'cpu.out', tmp_path / 'cpu.err'
    stdout.write_bytes(b'FIRST original bytes\r\n' + 'Unicode 界 line\n'.encode() + b'final partial')
    stderr.write_bytes(b'ANOTHER STREAM\n')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'host': 'carc.invalid', 'user': 'carc-owner', 'account': 'CARC_ACCOUNT',
        'partitions': ['gpu'], 'gpu_sampling': True, 'weather': True, 'budget': True,
        'weather_probes': [{'partition': 'gpu', 'gres': 'gpu:a100:1'}],
        'profiles': {'desktop': {'host': '', 'user': '', 'account': '', 'partitions': [],
            'gpu_sampling': False, 'weather': False, 'weather_probes': [], 'budget': False,
            'show_all_partitions': True, 'state_namespace': 'desktop'}}}))
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    for key, value in {'PATH': str(commands) + os.pathsep + os.environ.get('PATH', ''),
        'USER': 'desktop-user', 'TOWER_ACCOUNT': 'CARC_ENV_ACCOUNT', 'CARC_ACCOUNT': 'CARC_ENV_ACCOUNT',
        'XDG_STATE_HOME': str(tmp_path / 'state'), 'XDG_CONFIG_HOME': str(tmp_path / 'personal-config'),
        'XDG_RUNTIME_DIR': str(runtime),
        'TERM': 'xterm', 'TOWER_DESKTOP_CALLS': str(calls), 'TOWER_DESKTOP_SCENARIO': str(scenario),
        'TOWER_DESKTOP_ROOT': str(tmp_path), 'TOWER_DESKTOP_STDOUT': str(stdout),
        'TOWER_DESKTOP_STDERR': str(stderr)}.items():
        monkeypatch.setenv(key, value)
    argv = ['--config', str(config), '--profile', 'desktop', '--no-plugins', '--ascii']
    def run(*flags):
        return subprocess.run([sys.executable, '-m', 'tower', *argv, *flags], cwd=ROOT,
                              capture_output=True, text=True, timeout=20)
    def recorded():
        return [json.loads(row) for row in calls.read_text().splitlines()]
    def build(*flags):
        return cli.build(cli.parse([*argv, '--once', *flags]), Config.load(str(config)).profile('desktop'))
    return SimpleNamespace(root=tmp_path, run=run, calls=recorded, scenario=scenario, stdout=stdout,
                           stderr=stderr, build=build, config=config)


@pytest.mark.parametrize('queue,live', [('empty', True), ('cpu', True), ('cpu', False)])
def test_native_desktop_cpu_and_empty_queue_work_without_optional_services(native_desktop, queue, live):
    fixture = native_desktop
    fixture.scenario.write_text(json.dumps({'jobs': '' if queue == 'empty' else CPU_ROW, 'live': live}))
    result = fixture.run('--json')
    assert result.returncode == 0, result.stderr
    snap = json.loads(result.stdout)
    assert snap['finished'] == [] and snap['departed_jobs'] == {}
    assert snap['job_transitions'] == [] and snap['group'] == [] and snap['account'] == {}
    assert [part['name'] for part in snap['partitions']] == ['localcpu']
    assert snap['gpu_inventory'] == {} and snap['gpu'] == {}
    if queue == 'empty':
        assert snap['jobs'] == [] and snap['nodes'] == {}
    else:
        assert [(job['id'], job['partition'], job['gpus'], job['hosts']) for job in snap['jobs']] == [
            ('7', 'localcpu', 0, ['fedora-box'])]
        assert snap['nodes']['fedora-box']['cpus'] == 8
        assert snap['nodemap']['fedora-box']['partitions'] == ['localcpu']
        assert snap['details']['7']['StdOut'] == str(fixture.stdout)
        if live:
            assert snap['live']['7']['rss'] == 128 * 1024 ** 2
            assert snap['live']['7']['cpu_time'] == 4
        else:
            assert '7' not in snap['live'] and snap['health']['live']['errors'] == 1
    for name in ('finished', 'share'):
        assert snap['health'][name]['errors'] == 1 and snap['health'][name]['backoff'] > 0
    for name in ('gpu', 'account', 'budget', 'weather'):
        assert snap['health'][name]['calls'] == 0
    calls = fixture.calls()
    assert {call[0] for call in calls} <= {'squeue', 'scontrol', 'sinfo', 'sacct', 'sstat', 'sshare'}
    assert all('-A' not in call for call in calls)
    users = [call[call.index('-u') + 1] for call in calls if call[0] == 'squeue' and '-u' in call]
    assert users and set(users) == {'desktop-user'}


def test_native_desktop_reads_exact_real_local_job_log(native_desktop):
    fixture = native_desktop
    result = fixture.run('--once', '--tab', 'log', '--no-state')
    assert result.returncode == 0 and 'FIRST original bytes' in result.stdout
    assert 'Unicode' in result.stdout and 'final partial' in result.stdout
    assert 'ANOTHER STREAM' not in result.stdout
    session = fixture.build('--no-state')
    try:
        cli.settle(session)
        session.app.open_log('7')
        buf = session.app.prepare_log()
        assert type(session.files) is LocalFiles and not session.files.remote
        assert buf.path == str(fixture.stdout)
        assert b'\n'.join([*buf.raw_lines, buf._partial_raw]) == fixture.stdout.read_bytes()
    finally:
        session.close()


def test_native_desktop_doctor_is_passive_with_local_profile(native_desktop):
    result = native_desktop.run('--doctor', '--json')
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence['mode'] == 'local' and evidence['ready']
    assert native_desktop.calls() == []


def test_idle_desktop_cluster_shows_cpu_partition_and_measured_idle_capacity(native_desktop):
    fixture = native_desktop
    fixture.scenario.write_text(json.dumps({'jobs': ''}))
    result = fixture.run('--once', '--tab', 'cluster', '--width', '200')
    assert result.returncode == 0, result.stderr
    row = next(line.split() for line in result.stdout.splitlines() if 'localcpu' in line)
    start = row.index('localcpu')
    assert row[start + 3:start + 8] == ['1', '1', '0', '0', '8']
    assert 'CPUS IDLE' in result.stdout and 'Waiting for partition data' not in result.stdout


def test_legacy_cluster_filter_still_omits_unused_cpu_only_partition(native_desktop):
    fixture = native_desktop
    fixture.scenario.write_text(json.dumps({'jobs': ''}))
    config = json.loads(fixture.config.read_text())
    config['profiles']['desktop']['show_all_partitions'] = False
    fixture.config.write_text(json.dumps(config))
    result = fixture.run('--once', '--tab', 'cluster', '--width', '200')
    assert result.returncode == 0 and 'localcpu' not in result.stdout


def test_explicit_cluster_partition_names_still_limit_show_all(native_desktop):
    fixture = native_desktop
    fixture.scenario.write_text(json.dumps({'jobs': '',
        'partition_output': 'localcpu*|up|infinite|1|0/1/0/1|0/8/0/8\nsecondarycpu|up|infinite|1|0/1/0/1|0/4/0/4\n',
        'node_output': 'fedora-box localcpu (null) (null) idle 0/8/0/8 32768 0\nother-box secondarycpu (null) (null) idle 0/4/0/4 8192 0\n'}))
    config = json.loads(fixture.config.read_text())
    config['profiles']['desktop']['partitions'] = ['secondarycpu']
    fixture.config.write_text(json.dumps(config))
    result = fixture.run('--once', '--tab', 'cluster', '--width', '200')
    assert result.returncode == 0 and 'secondarycpu' in result.stdout
    assert 'localcpu' not in result.stdout


def test_desktop_scope_does_not_restore_or_change_legacy_carc_state(native_desktop):
    fixture = native_desktop
    legacy_root = fixture.root / 'state' / 'tower'
    legacy = Store(str(legacy_root))
    legacy.tag('7', 'CARC_TAG')
    legacy.record('7', {'t': 1, 'k': 'live', 'cpu': .99, 'rss': 123456})
    legacy.event('copy', 'old CARC event')
    legacy.save_ui({'workbench': {'table_ui': {'facets': {'jobs': {'partition': 'gpu'}}}}})
    before = {str(path.relative_to(legacy_root)): path.read_bytes()
              for path in legacy_root.rglob('*') if path.is_file()}
    session = fixture.build()
    try:
        assert fixture.calls() == []
        location = Path(session.store.state_dir)
        assert location.parent == legacy_root / 'profiles' / 'desktop'
        assert len(location.name) == 64 and set(location.name) <= set('0123456789abcdef')
        assert session.app.state_dir == session.store.state_dir
        assert session.store.tags == {} and session.store.series_of('7') == []
        assert list(session.store.events) == [] and not session.app.table_state['facets'].get('jobs')
        assert session.app.research.forecasts.observations() == []
        cli.settle(session)
        assert session.app.visible_ids == ['7']
        session.store.tag('7', 'DESKTOP_TAG')
        session.store.record('7', {'t': 2, 'k': 'live', 'cpu': .25})
        own_samples = sorted(session.store.series_of('7'), key=lambda sample: sample['t'])
        assert Path(session.store._series_path('7')).is_relative_to(location)
    finally:
        session.close()
    assert all((legacy_root / path).read_bytes() == contents for path, contents in before.items())
    restored = fixture.build()
    try:
        assert restored.store.tags['7']['tags'] == ['DESKTOP_TAG']
        assert restored.store.series_of('7') == own_samples
        assert all(sample.get('t') != 1 for sample in own_samples)
    finally:
        restored.close()


@pytest.mark.parametrize('difference', ['user', 'backend', 'host', 'profile', 'ssh_user', 'ssh_options', 'uid', 'local_host'])
def test_namespace_separates_connection_owners_and_backends_without_queries(tmp_path, monkeypatch, difference):
    monkeypatch.setattr(cli, 'state_dir', lambda: str(tmp_path / 'state'))
    monkeypatch.setattr(socket, 'gethostname', lambda: 'desktop-box')
    monkeypatch.setattr(cli.os, 'getuid', lambda: 1000)
    def forbidden(*_args, **_kwargs):
        pytest.fail('scope construction queried a backend')
    monkeypatch.setattr(Backend, 'run', forbidden)
    monkeypatch.setattr(Backend, 'call', forbidden)
    monkeypatch.setattr(SshBackend, 'run', forbidden)
    monkeypatch.setattr(SshBackend, 'call', forbidden)
    cfg = Config({'state_namespace': 'desktop', 'profiles': {'desktop': {}, 'other': {}}}).profile('desktop')
    remote = difference in ('host', 'ssh_user', 'ssh_options')
    first = SshBackend('cluster-a', user='alice', opts=['-p', '2222'], control=False) if remote else Backend()
    first_path = cli.scoped_state_dir(first, cfg, 'alice')
    second, second_cfg, user = first, cfg, 'alice'
    if difference == 'user': user = 'bob'
    elif difference == 'backend': second = SshBackend('desktop-box', control=False)
    elif difference == 'host': second = SshBackend('cluster-b', user='alice', opts=['-p', '2222'], control=False)
    elif difference == 'profile': second_cfg = cfg.profile('other')
    elif difference == 'ssh_user': second = SshBackend('cluster-a', user='bob', opts=['-p', '2222'], control=False)
    elif difference == 'ssh_options': second = SshBackend('cluster-a', user='alice', opts=['-p', '2223'], control=False)
    elif difference == 'uid': monkeypatch.setattr(cli.os, 'getuid', lambda: 1001)
    elif difference == 'local_host': monkeypatch.setattr(socket, 'gethostname', lambda: 'other-desktop')
    second_path = cli.scoped_state_dir(second, second_cfg, user)
    assert first_path != second_path and not (tmp_path / 'state').exists()


def test_recording_scope_unwraps_actual_backend_without_queries(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'state_dir', lambda: str(tmp_path))
    cfg = Config({'state_namespace': 'desktop'})
    backend = Backend()
    wrapped = RecordingBackend.__new__(RecordingBackend)
    wrapped.inner = backend
    assert cli.scoped_state_dir(wrapped, cfg, 'alice') == cli.scoped_state_dir(backend, cfg, 'alice')


def test_scoped_stores_for_other_user_and_remote_backend_do_not_import_local_job_data(native_desktop, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail('constructing scoped sessions queried a remote backend')
    monkeypatch.setattr(SshBackend, 'run', forbidden)
    monkeypatch.setattr(SshBackend, 'call', forbidden)
    sessions = []
    try:
        initial = native_desktop.build('--user', 'alice')
        sessions.append(initial)
        initial.store.tag('7', 'ALICE_LOCAL')
        initial.store.record('7', {'t': 3, 'k': 'live', 'cpu': .75})
        initial.app.table_state['facets']['jobs'] = {'partition': 'localcpu'}
        initial.app.save()
        for flags in (('--user', 'bob'), ('--user', 'alice', '--host', 'cluster.invalid')):
            other = native_desktop.build(*flags)
            sessions.append(other)
            assert other.store.state_dir != initial.store.state_dir
            assert other.app.state_dir == other.store.state_dir
            assert other.store.tags == {} and other.store.series_of('7') == []
            assert not other.app.table_state['facets'].get('jobs')
        same = native_desktop.build('--user', 'alice')
        sessions.append(same)
        assert same.store.state_dir == initial.store.state_dir
        assert same.store.tags['7']['tags'] == ['ALICE_LOCAL']
        assert same.store.series_of('7') == [{'t': 3, 'k': 'live', 'cpu': .75}]
        assert native_desktop.calls() == []
    finally:
        for session in sessions:
            session.close()


@pytest.mark.parametrize('namespace', [None, False, True, 0, [], {}, '.', '..', '../desktop', 'desktop/',
    'desktop\\other', '.desktop', 'desktop bad', 'é', 'desktop\n', 'foo\x00bar', 'x' * 65])
def test_invalid_namespace_is_rejected_before_backend_or_file_writes(tmp_path, monkeypatch, namespace):
    def forbidden(*_args, **_kwargs):
        pytest.fail('invalid namespace reached backend construction or state creation')
    monkeypatch.setattr(cli, 'make_backend', forbidden)
    monkeypatch.setattr(cli, 'state_dir', forbidden)
    cfg = Config({'state_namespace': namespace, 'record': str(tmp_path / 'must-not-record.jsonl')})
    with pytest.raises(ValueError, match='state_namespace'):
        cli.build(cli.parse(['--no-plugins']), cfg)
    assert not list(tmp_path.iterdir())


def test_invalid_namespace_cli_reports_error_without_writing_state(native_desktop):
    fixture = native_desktop
    config = json.loads(fixture.config.read_text())
    config['profiles']['desktop']['state_namespace'] = '../outside'
    fixture.config.write_text(json.dumps(config))
    result = fixture.run('--once')
    assert result.returncode == 1 and 'state_namespace' in result.stderr
    assert 'Traceback' not in result.stderr and fixture.calls() == []
    assert not (fixture.root / 'state').exists()


@pytest.mark.parametrize('namespace', ['', 'desktop', 'a' * 64, '-_AZaz09'])
def test_valid_namespaces_are_pure_and_empty_preserves_legacy_path(tmp_path, monkeypatch, namespace):
    base = str(tmp_path / 'legacy')
    monkeypatch.setattr(cli, 'state_dir', lambda: base)
    cfg = Config({'state_namespace': namespace})
    path = cli.scoped_state_dir(Backend(), cfg, 'alice')
    assert path == base if not namespace else Path(path).parent == Path(base) / 'profiles' / namespace
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('flags', [('--no-state',), ('--fake',)])
def test_namespace_does_not_enable_disabled_persistence(native_desktop, monkeypatch, flags):
    def forbidden(*_args, **_kwargs):
        pytest.fail('disabled persistence resolved a state directory')
    monkeypatch.setattr(cli, 'scoped_state_dir', forbidden)
    session = native_desktop.build(*flags)
    try:
        assert not session.store.persist and session.store.state_dir is None and session.app.state_dir is None
        assert session.app.forecast_scope is None
    finally:
        session.close()
    assert not (native_desktop.root / 'state').exists()
