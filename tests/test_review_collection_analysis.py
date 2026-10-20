"""Adversarial collection and analysis regressions against independent facts."""
import copy
import json
import math
import os
import random
import stat
import threading
from types import SimpleNamespace

import pytest

from raven import expr, phase_alignment as P, scientific_lineage as L, slurm_json as J
from raven import stream_anomalies as A, tradeoffs
from raven.model import Job
from raven.remote import SshBackend
from raven.slurm import CommandError, ParseDiagnostics, Slurm, parse_gpu_inventory, parse_node_map


def test_auto_json_failure_uses_fresh_validated_text_and_negative_capability():
    from test_slurm_json import Backend
    backend = Backend('malformed')
    reader = Slurm(backend, 'raven')
    assert reader.jobs()[0].id == '1'
    assert reader.reader_status['jobs']['format'] == 'legacy fallback'
    assert 'unavailable' in reader.reader_status['jobs']['reason'].lower()
    before = len(backend.calls)
    assert reader.jobs()[0].id == '1'
    assert len(backend.calls) == before + 1 and '--json' not in backend.calls[-1]


def test_old_slurm_version_is_learned_before_fetching_json():
    from test_slurm_json import Backend, raw
    class Versioned(Backend):
        probe_slurm_version = True
        def run(self, argv, timeout):
            if argv[-1] == '--version':
                self.calls.append(argv)
                return 'slurm 24.05.8\n', 0
            return super().run(argv, timeout)
    backend = Versioned(raw('pending-squeue-json'))
    reader = Slurm(backend, 'raven')
    assert reader.jobs()[0].id == '1'
    assert not any('--json' in argv for argv in backend.calls)
    assert '24.05.8' in reader.reader_status['jobs']['reason']


def test_warnings_allow_departure_and_only_identified_rejects_expire():
    from raven.sampler import Sampler
    sampler = object.__new__(Sampler)
    sampler.slurm = SimpleNamespace(parse_diagnostics={'jobs': {'ambiguous': 1, 'rejected': 0}})
    previous = [Job('1', 'first', 'cpu', 'RUNNING'), Job('2', 'second', 'cpu', 'RUNNING')]
    assert sampler._keep_rejected('jobs', [], previous) == []
    sampler.slurm.parse_diagnostics['jobs'] = {'rejected': 1, 'rejected_ids': ['1']}
    for _ in range(2):
        result = sampler._keep_rejected('jobs', [], previous)
        assert [row.id for row in result] == ['1'] and result[0].collection_stale
    assert sampler._keep_rejected('jobs', [], previous) == []
    assert not previous[0].collection_stale


def test_small_retained_tail_charges_actual_bytes_and_logs_have_separate_budget(tmp_path):
    from test_raven_collection import backend
    from raven.raven_collection import wrap_files
    from raven.remote import LocalFiles
    _, governed = backend(tmp_path, file_bytes_per_minute=1000, log_file_bytes_per_minute=10)
    files = wrap_files(LocalFiles(), governed)
    path = tmp_path / 'gpu.csv'
    path.write_bytes(b'x' * 37)
    for _ in range(20):
        assert len(files.tail(str(path), 1 << 20)[0]) == 37
    assert files.read(str(path), 0, 10) == b'x' * 10
    with pytest.raises(OSError, match='log-bytes'):
        files.read(str(path), 0, 1)
    rates = governed.ledger.report()['rates_last_minute']
    assert rates['file-bytes'] == 740 and rates['log-bytes'] == 10


def test_automatic_cache_squatting_uses_private_random_fallback(tmp_path, monkeypatch):
    from raven import raven_collection as C
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    monkeypatch.setattr(C, '_host', lambda: 'a' * 64)
    occupied = tmp_path / ('raven-collection-' + str(os.getuid()) + '-' + 'a' * 16)
    occupied.mkdir(mode=0o777)
    occupied.chmod(0o777)
    selected = C.cache_directory()
    assert selected != str(occupied) and C.cache_directory() == selected
    assert stat.S_IMODE(os.stat(selected).st_mode) == 0o700
    assert stat.S_IMODE(occupied.stat().st_mode) == 0o777


def test_missing_linux_identity_uses_process_cache_without_weakening_explicit_paths(tmp_path, monkeypatch):
    from raven import shared_collection as S
    from raven.platform_session import wrap_backend
    from test_raven_collection import Recorder
    monkeypatch.setattr(S, '_host', lambda: (_ for _ in ()).throw(ValueError('Shared collection needs Linux process/boot identity')))
    backend = wrap_backend(Recorder(), {'raven': {'collection': {}}}, {'user': 'me', 'cluster': 'test'}, None)
    assert 'process-local' in backend.cache_coordination
    assert backend.run(['squeue', '-u', 'me'])[0] == '1|RUNNING\n'
    unsafe = tmp_path / 'unsafe'; unsafe.mkdir(); unsafe.chmod(0o777)
    with pytest.raises(ValueError):
        wrap_backend(Recorder(), {'raven': {'collection': {}}, 'collection_cache_dir': str(unsafe)}, {'user': 'me'}, None)


def test_ssh_option_boundary_and_failed_reuse_keep_safe_connection(monkeypatch):
    from raven import ssh_control
    monkeypatch.setattr(ssh_control, 'prepare', lambda *a: (_ for _ in ()).throw(OSError('unavailable')))
    backend = SshBackend('login.example.edu', 'student')
    argv = backend.argv(['squeue', '-u', 'student'])
    marker = argv.index('--')
    assert argv[marker + 1] == 'student@login.example.edu'
    assert argv[-1] == 'squeue -u student' and backend.control_diagnostic
    for host in ('-oProxyCommand=bad', 'host;echo bad', 'host user'):
        with pytest.raises(ValueError): SshBackend(host)


def test_delimited_long_gpu_node_never_reports_busy_cards_free():
    name = 'node-' + 'x' * 40
    gres = 'gpu:h100:8(S:0-7),shard:h100:64'
    row = f'{name}|gpu|{gres}|{gres}|drain|64/0/0/64|2063000|2000000\n'
    node = parse_node_map(row)[name]
    assert node.state == 'drain' and node.gpus_used == node.gpus == 8
    inventory, _ = parse_gpu_inventory(row)
    assert inventory['h100']['free'] == 0
    assert not parse_node_map(f'{name} gpu {gres}{gres} drain2063000')


def test_pending_expected_start_has_no_observed_runtime():
    assert J.runtime({'job_state': ['PENDING'], 'start_time': 1, 'time_limit': 240}, 1000) == 0


def test_scheduler_error_preserves_actual_description():
    with pytest.raises(J.SchedulerError, match='Unable to contact slurm controller'):
        J.document(json.dumps({'jobs': [], 'errors': [{'description': 'Unable to contact slurm controller'}]}))


def lineage(count=32):
    nodes, actions = [], []
    for i in range(count):
        nodes += [{'id': f'a{i}', 'kind': 'artifact', 'dependencies': [], 'complete': True},
                  {'id': f'c{i}', 'kind': 'claim', 'dependencies': [f'a{i}'], 'complete': True}]
        actions.append({'id': f'check{i:04d}', 'covers': [f'a{i}', f'c{i}'], 'requires': [], 'cost': 1, 'method': 'declared test'})
    return {'schema': 'raven.scientific-lineage/v1', 'nodes': nodes, 'changed': [f'a{i}' for i in range(count)], 'actions': actions}


def test_lineage_incremental_greedy_covers_exact_claims_and_checks_cancel():
    document = lineage(1024)
    result = L.analyze(document)
    assert len(result['proposed_actions']) == 1024 and not result['unresolved_claims']
    assert result['cost'] == 1024
    event = threading.Event(); event.set()
    with pytest.raises(ValueError, match='cancelled'): L.analyze(document, cancel=event)
    document = lineage(2)
    document['actions'][0]['requires'] = ['a1']
    result = L.analyze(document)
    assert [row['id'] for row in result['proposed_actions']] == ['check0001', 'check0000']


def signal(levels, sizes, seed=1):
    rng = random.Random(seed)
    values = [level + rng.gauss(0, 1) for level, size in zip(levels, sizes) for _ in range(size)]
    return {'schema': 'raven.measurement-series/v1', 'series': [{'identity': {'scope': 'synthetic', 'attempt': 'a', 'job': '1', 'verified': True}, 'metric': 'cpu', 'unit': '%', 'samples': [{'t': i, 'value': value} for i, value in enumerate(values)]}]}


def test_noise_scaled_phase_boundaries_survive_unrelated_large_step():
    capture = signal([0, 100, 102], [1000] * 3)
    result = P.analyze({'schema': 'raven.phase-comparison/v1', 'reference': capture, 'candidate': copy.deepcopy(capture)})
    boundaries = [row['start'] for row in result['reference_segments']]
    assert any(abs(i - 1000) <= 3 for i in boundaries)
    assert any(abs(i - 2000) <= 10 for i in boundaries)


def test_phase_staircase_keeps_all_large_changes():
    capture = signal([10 * i for i in range(13)], [150] * 13)
    result = P.analyze({'schema': 'raven.phase-comparison/v1', 'reference': capture, 'candidate': copy.deepcopy(capture)})
    boundaries = [row['start'] for row in result['reference_segments']]
    assert len(boundaries) == 13
    assert all(any(abs(actual - expected) <= 3 for actual in boundaries) for expected in range(0, 1950, 150))


def test_unequal_phase_lengths_have_dtw_endpoint_path():
    budget = P.Budget()
    left = [[math.sin(i / 20)] for i in range(100)]
    right = [[math.sin(i / 120)] for i in range(600)]
    # Production resampling limits the pair to 128 points on each side.
    result = P._dtw(left, right, list(range(100)), list(range(0, 600, 5)), budget=budget)
    assert result[0] and result[1] is not None


def test_clock_mismatch_is_an_explicit_refusal():
    capture = signal([10], [50])
    other = copy.deepcopy(capture['series'][0]); other['metric'] = 'gpu'
    for row in other['samples']: row['t'] += .5
    capture['series'].append(other)
    with pytest.raises(ValueError, match='clock|cadence'):
        P.analyze({'schema': 'raven.phase-comparison/v1', 'reference': capture, 'candidate': copy.deepcopy(capture)})


def test_sparse_missing_samples_do_not_erase_anomaly_baseline():
    from test_stream_anomalies import document, series
    values = [80.] * 60 + [5.] * 60
    for i in range(0, len(values), 10): values[i] = None
    result = A.analyze(document(series(values)))
    assert any(row['kind'] == 'down' and row['timestamp'] < 80 for row in result['events'])


def test_expression_native_work_and_output_are_bounded():
    for source in ('sum([[0]*10000]*400, [])', 'str({0: [[0]*100000]*3000}.values())', '["x"*500000 for i in range(4000)]'):
        with pytest.raises((expr.ExprError, ValueError)):
            expr.Expr(source)({})
    output = expr.bounded_repr(['x' * 100000] * 10000)
    assert 0 < len(output) <= 1024


def test_comparisons_cancel_without_publishing_partial_frontier():
    event = threading.Event(); event.set()
    with pytest.raises(ValueError, match='cancelled'):
        tradeoffs.compare([{'name': 'work'}], cancel=event)


def test_json_record_validation_quarantines_only_the_bad_job():
    from test_slurm_json import raw
    value = json.loads(raw('pending-squeue-json'))
    bad = copy.deepcopy(value['jobs'][0]); bad['job_id'] = 9001; bad['comment'] = 'bad\x01comment'
    value['jobs'].append(bad)
    bounded = J.document(json.dumps(value))
    report = ParseDiagnostics('jobs')
    jobs = J.queue(bounded, report)
    assert len(jobs) == 1 and jobs[0].id == '1'
    assert report.rejected == 1 and report.rejected_ids == {'9001'}


def test_bounded_accounting_publishes_coverage_without_failing_history(tmp_path):
    from test_raven_collection import backend
    from test_slurm_json import raw
    lines = [row for row in raw('accounting').splitlines() if '.' not in row.split('|', 1)[0]]
    wire = '\n'.join(lines * 4) + '\n'
    native, wrapper = backend(tmp_path, max_response_records=2)
    native.response = wire
    # This check exercises source admission rather than guessing records inside
    # a parsed fixture with repeated scheduler identities.
    result, _ = wrapper.run(['sacct', '-X', '-u', 'raven', '-n', '-P'])
    assert len(result.splitlines()) == 2
    assert wrapper.observation_coverage['partial']
    assert wrapper.observation_coverage['available_records'] == len(lines) * 4


def test_first_selected_headless_detail_uses_existing_bounded_inspection_allowance(monkeypatch):
    from raven import cli, raven_config
    from raven.config import Config
    resolved = raven_config.resolve(site='slurm')
    session = cli.build(cli.parse(['--fake', '--once', '--no-state', '--no-plugins']), Config(raven_config.to_raven_config(resolved)))
    sleeps = []
    monkeypatch.setattr(cli.time, 'sleep', lambda delay: sleeps.append(delay))
    try:
        cli.settle(session)
        assert not sleeps and not session.sampler._collection_deferred
        assert session.store.details
        assert session.sampler.slurm.b.ledger.report()['rates_last_minute']['validation'] >= 1
    finally:
        session.close()


def test_small_holdout_is_not_false_drift_but_large_drift_is_flagged():
    from raven.predict import _fit
    small = _fit([10.] * 16 + [10., 10., 10., 100.], .8, 10)
    assert small['observed_coverage'] == .75 and small['status'] == 'ok'
    large = _fit([10.] * 160 + [100.] * 40, .8, 10)
    assert large['status'] == 'poor_validation'
    assert large['validation_lower_tail_probability'] < .01


def test_uid_identity_ignores_missing_or_spoofed_user_environment(monkeypatch):
    from raven.user_identity import current_user
    import pwd
    expected = pwd.getpwuid(os.getuid()).pw_name
    for value in (None, 'not-the-real-login'):
        if value is None: monkeypatch.delenv('USER', raising=False)
        else: monkeypatch.setenv('USER', value)
        assert current_user() == expected


def test_early_json_failure_is_one_structured_document(monkeypatch, capsys):
    from raven import cli
    def failure(*args, **kwargs):
        print('early adapter failure')
        return 2
    monkeypatch.setattr(cli, '_entry_main', failure)
    assert cli.main(['--json']) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'error' and result['message'] == 'early adapter failure'


def test_interactive_expression_is_deferred_to_existing_worker(monkeypatch):
    from raven.controller import App
    from raven.model import Store
    from raven.config import Config
    from raven import expr
    class Hub:
        def poll_task(self): pass
        def start_task(self, function, completion):
            self.function, self.completion = function, completion
            return True
    store = Store(persist=False)
    app = App(store, None, None, Config(), 'researcher', interactive=True)
    app.research = Hub()
    calls = []
    original = expr.Expr.__call__
    def evaluate(self, ns, **kwargs):
        calls.append(True)
        return original(self, ns, **kwargs)
    monkeypatch.setattr(expr.Expr, '__call__', evaluate)
    assert 'Evaluating' in app.evaluate('1 + 2') and not calls
    app.research.completion(app.research.function())
    assert calls and app.message == '3'


def test_phase_block_bound_retains_significant_blocks_with_explicit_omissions():
    capture = signal([10], [400])
    for index, row in enumerate(capture['series'][0]['samples']):
        if index % 10 == 9: row['value'] = None
        elif index >= 390: row['value'] = 100 if index % 2 else 0
    result = P.analyze({'schema': 'raven.phase-comparison/v1', 'reference': capture, 'candidate': copy.deepcopy(capture)})
    assert result['status'] == 'partial-experimental'
    assert len(result['reference_segments']) == 32
    assert len(result['reference']['omitted_blocks']) == 8
    assert any(row['start'] == 390 for row in result['reference_segments'])


def test_full_three_factor_design_fits_three_way_term_and_refuses_missing_cell():
    from test_experiment_design import design
    from raven import experiment_design as E
    document = design()
    document['factors'].append({'id': 'third', 'levels': [{'id': str(i), 'arguments': ['--third', str(i)]} for i in range(2)]})
    document['recipe']['limits']['max_trials'] = 32
    plan = E.prepare(document)
    observations = []
    rng = random.Random(71)
    for row in plan['schedule']:
        a, b, c = row['coding']
        value = 10 + 1.5 * a + .9 * a * b * c + rng.gauss(0, .01)
        observations.append({'id': row['id'], 'value': value, 'unit': 'sum', 'output_sha256': 'a' * 64,
                             'semantic': document['equivalence']['semantic'], 'controls': row['levels']})
    source = {'schema': E.RESULT_SCHEMA, 'design': document, 'observations': observations}
    result = E.analyze(source)
    effects = {row['term']: row['contrast'] for row in result['fit']['effects']}
    assert effects['order'] == pytest.approx(3., abs=.03)
    assert effects['order:method:third'] == pytest.approx(1.8, abs=.03)
    missing = {row['id'] for row in plan['schedule'] if row['coding'] == [1, 1, 1]}
    source['observations'] = [row for row in observations if row['id'] not in missing]
    result = E.analyze(source)
    assert result['state'] == 'incomplete-design' and not result['fit']['effects']


def test_session_sustained_read_budget_does_not_starve_later_connections():
    from raven.session_service import Server
    server = object.__new__(Server)
    server._rate_tokens, server._rate_updated = 64., 0.
    server.clients = {i: {'uid': os.getuid(), 'tokens': 4., 'rate_updated': 0.} for i in range(9)}
    accepted = [0] * 9
    for step in range(40):
        for index, peer in server.clients.items():
            try:
                server.admit_read(peer, step / 4.)
            except ValueError:
                pass
            else:
                accepted[index] += 1
    assert min(accepted) >= 25, accepted
    assert max(accepted) - min(accepted) <= 4, accepted


def test_idle_session_connections_do_not_reduce_reader_share():
    from raven.session_service import Server
    server = object.__new__(Server)
    server._rate_tokens, server._rate_updated = 64., 0.
    server.clients = {i: {'uid': os.getuid(), 'tokens': 0., 'rate_updated': 0.} for i in range(64)}
    reader = server.clients[63]
    server.admit_read(reader, .25)
    assert reader['tokens'] == 0.


def test_supported_plural_parser_metadata_publishes_status_without_key_error():
    from test_slurm_json import Backend, raw
    value = json.loads(raw('running-squeue-json'))
    value['meta']['plugins'] = value['meta'].pop('plugin')
    reader = Slurm(Backend(json.dumps(value)), 'raven', json_mode='json')
    assert reader.jobs()
    assert reader.reader_status['jobs']['parser'] == 'data_parser/v0.0.45'


def test_session_principals_progress_with_bounded_global_rate():
    from raven.session_service import Server
    class Shared(Server):
        def authorized(self, uid): return True
    server = object.__new__(Shared)
    server._rate_tokens, server._rate_updated = 64., 0.
    users = [0] * 12 + list(range(1, 9))
    server.clients = {index: {'uid': uid, 'tokens': 4., 'rate_updated': 0.} for index, uid in enumerate(users)}
    accepted = {uid: 0 for uid in users}
    for step in range(80):
        for peer in server.clients.values():
            try: server.admit_read(peer, step / 4.)
            except ValueError: pass
            else: accepted[peer['uid']] += 1
    assert min(accepted.values()) >= 30, accepted
    assert sum(accepted.values()) <= 64 + 32 * 19.75


def test_phase_detection_is_invariant_to_an_arbitrary_dc_offset():
    capture = signal([0, 100, 102], [1000] * 3, seed=19)
    shifted = copy.deepcopy(capture)
    for row in shifted['series'][0]['samples']: row['value'] += 10000
    inputs = {'schema': 'raven.phase-comparison/v1', 'reference': shifted, 'candidate': copy.deepcopy(shifted)}
    result = P.analyze(inputs)
    assert any(abs(row['start'] - 2000) <= 10 for row in result['reference_segments'])
    original = P.analyze(dict(inputs, reference=capture, candidate=copy.deepcopy(capture)))
    assert [row['start'] for row in result['reference_segments']] == [row['start'] for row in original['reference_segments']]
