"""As-of observations keep late evidence, attempts and actions separated."""
import copy
import json
import os
import time
from types import SimpleNamespace

import pytest
from raven import historical_replay as H, historical_ui as UI
from raven import record, workbench
from raven.config import Config
from raven.controller import App
from raven.model import Store, Job, Finished
from raven.worker_scheduler import WorkerScheduler
from raven.views import Views
from raven.layout import Glyphs
from raven.slurm import CommandError


def bind(store, restart=0):
    job = Job('7', 'training', 'gpu', 'RUNNING', submit='2026-10-17T10:00:00', start='2026-10-17T10:00:01', cluster='lab')
    store.apply_jobs([job])
    store.details['7'] = dict(JobId='7', SubmitTime=job.submit, StartTime=job.start, Restarts=str(restart), Cluster='lab')
    return store.capture_series_identity('7')


def snapshot(moment=100, monotonic=None, *, state='RUNNING', observer='first', scope='local'):
    return dict(observed_at=moment, monotonic=moment if monotonic is None else monotonic,
        source_time=moment-1, observer_id=observer, source_scope=scope, jobs=[dict(id='7', state=state, submit='same', start='same', cluster='lab')],
        history=[], selected='7', samples=[], identity=None, attempts={}, transitions=[], source_health={},
        coverage=dict(jobs_omitted=0, history_omitted=0, samples_omitted=0, series_scope='selected memory'))


def test_late_arrivals_never_leak_back_and_results_are_immutable():
    timeline = H.Timeline()
    first = snapshot(); timeline.append(H.encoded(first))
    later = snapshot(110); later['samples'] = [dict(t=90, k='live', rss=123)]
    timeline.append(H.encoded(later))
    assert timeline.at(99)['status'] == 'unknown'
    old = timeline.at(105)
    assert old['snapshot']['samples'] == [] and old['unknown_since'] == 100
    old['snapshot']['jobs'].clear()
    assert timeline.at(105)['snapshot']['jobs']
    assert timeline.at(110)['snapshot']['samples'][0]['t'] == 90


def test_timeline_copies_preserve_values_and_report_isolates_historical_overlay():
    timeline = H.Timeline()
    timeline.append(H.encoded(snapshot()))
    assert timeline == copy.deepcopy(timeline) == timeline.clone()
    copied = timeline.clone()
    copied.evicted += 1
    assert copied != timeline
    copied = timeline.clone()
    copied.frames[0]['sequence'] += 1
    assert copied != timeline and timeline.frames[0]['sequence'] == 1

    from raven import report
    store = Store(persist=False)
    store.jobs = [Job('17', 'captured-live-job', 'cpu', 'RUNNING')]
    app = App(store, None, None, Config(), 'scientist', ascii_=True)
    app.mode = 'historical'
    app.historical_state.update(timeline=timeline)
    before = copy.deepcopy(app.historical_state)
    local = report._ui_copy(app)
    local.historical_state['timeline'].frames[0]['sequence'] = 99
    assert app.historical_state == before
    page = report.build(store.snapshot(), app, Views(Glyphs(True), app.cfg))
    assert 'captured-live-job' in page and '01 / JOBS' in page
    assert app.historical_state == before and app.mode == 'historical'


def test_same_second_requeue_changes_diff_identity_and_never_merges_samples():
    store = Store(persist=False)
    old = bind(store)
    store.record('7', dict(t=1, k='live', cpu=.5), identity=old, _persist=False)
    before = H.validate(H.capture(store, '7'))
    new = bind(store, restart=1)
    after = H.validate(H.capture(store, '7'))
    delta = H.difference(before, after)
    assert old != new and after['samples'] == []
    assert len(delta['appeared']) == len(delta['disappeared']) == 1 and not delta['changed']
    assert before['samples'][0]['cpu'] == .5


def test_duplicate_scheduler_identity_is_explicitly_ambiguous():
    before = snapshot(); after = snapshot(110)
    before['history'] = copy.deepcopy(before['jobs'])
    delta = H.difference(before, after)
    assert len(delta['ambiguous']) == 1
    assert not delta['changed'] and not delta['appeared'] and not delta['disappeared']


def test_clocks_scope_epochs_timezone_and_partial_retention(monkeypatch):
    timeline = H.Timeline()
    timeline.append(H.encoded(snapshot(100, 10)))
    timeline.append(H.encoded(snapshot(90, 11)))
    with pytest.raises(ValueError, match='epoch'): timeline.at(100)
    assert timeline.at(100, clock_epoch=0)['snapshot']['observed_at'] == 100
    assert timeline.at(100, clock_epoch=1)['snapshot']['observed_at'] == 90
    timeline.append(H.encoded(snapshot(120, 12, scope='other-cluster')))
    assert timeline.frames[-1]['clock_epoch'] == 2
    assert H.timestamp('2026-10-17T12:00:00+02:00') == H.timestamp('2026-10-17T10:00:00Z')
    with pytest.raises(ValueError, match='timezone'): H.timestamp('2026-10-17T10:00:00')
    monkeypatch.setattr(H, 'MAX_SNAPSHOTS', 2)
    timeline.append(H.encoded(snapshot(130, 13, scope='other-cluster')))
    assert timeline.evicted == 2 and timeline.at(110, clock_epoch=0)['status'] == 'unknown'
    loaded = H.Timeline.load(H.encoded(timeline.document()))
    assert loaded.document() == timeline.document()


@pytest.mark.parametrize('change', ['observer', 'hash', 'coverage', 'identity', 'clock'])
def test_load_rejects_contradictory_or_malformed_metadata(change):
    timeline = H.Timeline(); timeline.append(H.encoded(snapshot()))
    document = timeline.document(); frame = document['frames'][0]
    if change == 'observer': frame['observer_id'] = 'different'
    elif change == 'hash': frame['sha256'] = '0'*64
    else:
        if change == 'coverage': frame['snapshot']['coverage'] = None
        elif change == 'identity': frame['snapshot']['identity'] = {'job': '7'}
        else: frame['clock_epoch'] = -1
        import hashlib
        frame['sha256'] = hashlib.sha256(H.encoded(frame['snapshot'])).hexdigest()
    with pytest.raises(ValueError): H.Timeline.load(H.encoded(document))


def test_prepare_and_timeline_byte_caps_keep_old_observations(monkeypatch):
    timeline = H.Timeline(); timeline.append(H.encoded(snapshot()))
    previous = timeline.document()
    with pytest.raises(ValueError): timeline.append(b'x'*(H.MAX_SNAPSHOT+1))
    assert timeline.document() == previous
    with pytest.raises(ValueError): H.Timeline.load(b'x'*(H.MAX_ARCHIVE+(64<<10)+1))
    assert not H._finite(10**500)


@pytest.fixture
def app():
    application = App(Store(persist=False), None, None, Config({}), 'researcher', ascii_=True)
    application.worker_scheduler = WorkerScheduler(workers=1, mode='single')
    application.width, application.height = 80, 24
    bind(application.store)
    application.selected_id = '7'; application.cursor['jobs'] = 4; application.top['jobs'] = 3
    yield application
    UI.shutdown(application)
    application.worker_scheduler.shutdown(wait=True, cancel_futures=True)
    application.store.close()


def drain(app):
    deadline = time.monotonic()+5
    state = UI.initialize(app)
    while time.monotonic() < deadline:
        UI.tick(app)
        if state['pending'] is None and state['view_request'] is None:
            return
        time.sleep(.005)
    pytest.fail('historical background work did not settle')


def test_existing_workbench_records_seeks_and_reads_charts_off_paint(app, monkeypatch):
    app.store.record('7', dict(t=time.time()-1, k='live', cpu=.25, rss=4<<20), _persist=False)
    app.run_command('asof record on'); drain(app)
    state = UI.initialize(app)
    assert len(state['timeline'].frames) == 1
    app.run_command('asof record off'); app.run_command('asof'); drain(app)
    assert app.mode == 'historical'
    app._handle('3'); drain(app)
    text = '\n'.join(''.join(chunk for chunk, _ in row) for row in state['rows'])
    assert 'CPU per allocated core' in text and 'Resident memory' in text and 'GPU recording unavailable' in text
    monkeypatch.setattr(H, 'validate', lambda *_: pytest.fail('paint must not decode snapshots'))
    views = Views(Glyphs(True), Config({}))
    assert UI.overlay(views, {}, app, 80, 24)
    assert UI.overlay(views, {}, app, 80, 24)
    assert app.top['jobs'] == 3


def test_historical_mode_consumes_all_mutations_and_restores_exact_selection(app):
    state = UI.initialize(app); state['timeline'].append(H.capture(app.store, '7'))
    app.run_command('asof'); drain(app)
    app.run_command('cancel 7'); assert not app.command_ok
    app.run_command('asof record on'); assert not app.command_ok and not state['recording']
    app.run_command('asof save elsewhere'); assert not app.command_ok
    assert UI.handle_mouse(app, 10, 10, button='left')
    app._handle('k'); app._handle('q')
    assert app.mode == 'main' and app.selected_id == '7'
    assert app.cursor['jobs'] == 4 and app.top['jobs'] == 3
    app.run_command('asof'); drain(app)
    bind(app.store, restart=1)
    app.run_command('asof live')
    assert app.selected_id is None and 'attempt' in app.message


def test_archive_save_load_is_explicit_noclobber_and_background(app, tmp_path):
    state = UI.initialize(app); state['timeline'].append(H.capture(app.store, '7'))
    target = tmp_path/'observations'
    app.run_command('asof save '+str(target)); drain(app)
    assert (target/'timeline.json').exists()
    before = (target/'timeline.json').read_bytes()
    app.run_command('asof save '+str(target)); drain(app)
    assert (target/'timeline.json').read_bytes() == before and state['error']
    app.run_command('asof load '+str(target/'timeline.json')); drain(app)
    assert app.mode == 'historical' and state['result']['status'] == 'observed'


def test_stale_background_view_cannot_replace_newer_navigation(app):
    state = UI.initialize(app)
    state['timeline'].append(H.encoded(snapshot()))
    state['timeline'].append(H.encoded(snapshot(110)))
    app.run_command('asof'); UI.tick(app)
    pending = state['pending']; pending.result(timeout=5)
    app._handle('left')
    UI.tick(app); drain(app)
    assert state['result']['snapshot']['observed_at'] == 100


def test_extreme_calendar_time_and_missing_gpu_do_not_crash_view():
    value = snapshot(1e99); value['samples'] = [dict(t=1, k='live', cpu=.1, rss=100)]
    timeline = H.Timeline(); timeline.append(H.encoded(value))
    _, _, rows, _ = UI._view(timeline, 1, None, None, 'analytics', False, 72, True, 1)
    text = '\n'.join(''.join(s for s, _ in row) for row in rows)
    assert 'outside calendar display range' in text and 'CPU per allocated core' in text


def test_recording_marks_answer_completion_and_replay_refuses_early_answer(tmp_path, monkeypatch):
    now = [10.]
    monkeypatch.setattr(record.time, 'time', lambda: now[0])
    class Backend:
        def run(self, cmd, timeout):
            if cmd == ['other']:
                return 'early answer', 0.
            now[0] = 20.
            return 'late answer', 10.
    path = tmp_path/'commands.jsonl'
    writer = record.RecordingBackend(Backend(), str(path))
    writer.run(['other'])
    writer.run(['query']); writer.close()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[-1]['requested_at'] == 10 and rows[-1]['observed_at'] == 20
    replay = record.ReplayBackend(str(path), paused=True)
    with pytest.raises(CommandError, match='no observation'): replay.run(['query'])
    replay.clock.seek(20); assert replay.run(['query'])[0] == 'late answer'
    assert replay.replay_mode == 'known-at-time'
    assert replay.call(['scancel','7'])[0] is False


def _recorded_commands(path, monkeypatch, times=(100., 110.)):
    now = [90.]
    monkeypatch.setattr(record.time, 'time', lambda: now[0])
    class Backend:
        def run(self, cmd, timeout):
            return 'answer '+cmd[0], .25
    writer = record.RecordingBackend(Backend(), str(path))
    for index, moment in enumerate(times):
        now[0] = moment
        writer.run(['query'+str(index)])
    writer.close()
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_wall_clock_rollback_is_reconstructed_before_timestamp_sort(tmp_path, monkeypatch):
    path = tmp_path/'rollback.jsonl'
    rows = _recorded_commands(path, monkeypatch, (100., 95., 110.))
    assert [row['sequence'] for row in rows] == [0, 1, 2, 3]
    assert len({row['observer_id'] for row in rows}) == 1
    assert [row['monotonic'] for row in rows] == sorted(row['monotonic'] for row in rows)
    replay = record.ReplayBackend(str(path), paused=True)
    assert replay.replay_mode != 'known-at-time'
    assert 'wall clock moved backward' in replay.summary()
    assert [row['t'] for row in replay.entries] == [95., 100., 110.]
    assert replay.run(['query1'])[0] == 'answer query1'  # Retained reconstruction is still readable.
    text = ''.join(text for text, _ in Views(Glyphs(True), Config({})).replay_bar(replay, 80))
    assert 'reconstructed replay' in text


@pytest.mark.parametrize('change', ['observer', 'sequence', 'monotonic', 'request', 'missing-header',
                                    'old-header', 'header-after-answer', 'second-header'])
def test_only_one_consistent_observer_segment_is_known_at_time(tmp_path, monkeypatch, change):
    path = tmp_path/'provenance.jsonl'
    rows = _recorded_commands(path, monkeypatch)
    assert record.ReplayBackend(str(path), paused=True).replay_mode == 'known-at-time'
    if change == 'observer': rows[-1]['observer_id'] = 'f'*32
    elif change == 'sequence': rows[-1]['sequence'] += 1
    elif change == 'monotonic': rows[-1]['monotonic'] = rows[-2]['monotonic']-1
    elif change == 'request': rows[-1]['requested_at'] = rows[-1]['observed_at']+1
    elif change == 'missing-header': rows.pop(0)
    elif change == 'old-header': rows[0]['schema'] = 'raven.session-recording/v2'
    elif change == 'header-after-answer': rows[0], rows[1] = rows[1], rows[0]
    else: rows.insert(-1, dict(rows[0], observer_id='e'*32))
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    replay = record.ReplayBackend(str(path), paused=True)
    assert replay.replay_mode != 'known-at-time'
    assert replay.replay_diagnostics['reconstruction_reasons']
    assert replay.run(['query0'])[0] == 'answer query0'


def test_appended_recording_sessions_are_labelled_reconstructed(tmp_path, monkeypatch):
    path = tmp_path/'appended.jsonl'
    _recorded_commands(path, monkeypatch)
    rows = _recorded_commands(path, monkeypatch, (120., 130.))
    headers = [row for row in rows if row.get('kind') == 'header']
    assert len(headers) == 2 and headers[0]['observer_id'] != headers[1]['observer_id']
    replay = record.ReplayBackend(str(path), paused=True)
    assert 'multiple observer headers' in replay.summary()
    assert replay.replay_mode != 'known-at-time'


@pytest.mark.parametrize('duration', [{}, [], '1', True, None, -1, float('inf'), float('nan')])
def test_invalid_duration_is_omitted_with_visible_reconstruction_diagnostic(tmp_path, monkeypatch, duration):
    path = tmp_path/'duration.jsonl'
    rows = _recorded_commands(path, monkeypatch)
    rows[-1]['dt'] = duration
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    replay = record.ReplayBackend(str(path), paused=True)
    assert replay.run(['query0']) == ('answer query0', .25)
    assert replay.replay_diagnostics['omitted_invalid_records'] == 1
    assert '1 invalid records omitted' in replay.summary()
    assert replay.replay_mode != 'known-at-time'
    with pytest.raises(CommandError, match='not in the recording'):
        replay.run(['query1'])


@pytest.mark.parametrize('invalid', ['['*100000+'0'+']'*100000, '{"t":1,"t":2}', '{',
                                    '{"t":1,"cmd":["query"],"metadata":{"number":1e999}}'])
def test_malformed_json_is_contained_and_does_not_upgrade_incomplete_observations(tmp_path, monkeypatch, invalid):
    path = tmp_path/'malformed.jsonl'
    _recorded_commands(path, monkeypatch)
    with path.open('a') as output: output.write(invalid+'\n')
    replay = record.ReplayBackend(str(path), paused=True)
    assert replay.replay_diagnostics['omitted_invalid_records'] == 1
    assert replay.replay_mode != 'known-at-time'
    assert 'invalid records omitted' in replay.summary()


@pytest.mark.parametrize('compressed', [False, True])
def test_invalid_utf8_has_a_readable_command_error(tmp_path, compressed):
    import gzip
    path = tmp_path/('bad.jsonl.gz' if compressed else 'bad.jsonl')
    path.write_bytes(gzip.compress(b'\xff\n') if compressed else b'\xff\n')
    with pytest.raises(CommandError, match='UTF-8 JSONL'):
        record.ReplayBackend(str(path))


def test_invalid_only_recording_reports_omissions(tmp_path):
    path = tmp_path/'empty.jsonl'; path.write_text('{\n')
    with pytest.raises(CommandError, match='1 invalid records omitted'):
        record.ReplayBackend(str(path))


@pytest.mark.parametrize('user', [1, [], None, 'x'*257, 'name\x00', '\ud800'])
def test_invalid_header_user_cannot_change_replay_scheduler_identity(tmp_path, monkeypatch, user):
    path = tmp_path/'user.jsonl'
    rows = _recorded_commands(path, monkeypatch)
    rows[0]['user'] = user
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    with pytest.raises(CommandError, match='header user'):
        record.ReplayBackend(str(path))


def test_finite_descriptive_extensions_remain_compatible(tmp_path, monkeypatch):
    path = tmp_path/'extension.jsonl'
    rows = _recorded_commands(path, monkeypatch)
    rows[0]['user'] = 'scientist'
    rows[-1]['extension'] = {'notes': ['published separately'], 'cost': 1.5}
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    replay = record.ReplayBackend(str(path), paused=True)
    assert replay.replay_mode == 'known-at-time' and replay.user == 'scientist'


def test_recorder_metadata_cannot_replace_owned_provenance(tmp_path):
    path = tmp_path/'metadata.jsonl'
    writer = record.RecordingBackend(object(), str(path), {'schema': 'forged', 'kind': 'answer',
        'observer_id': 'forged', 'sequence': 99, 'monotonic': -1})
    writer.close()
    header = json.loads(path.read_text())
    assert header['schema'] == 'raven.session-recording/v3' and header['kind'] == 'header'
    assert header['observer_id'] != 'forged' and header['sequence'] == 0 and header['monotonic'] >= 0


def test_legacy_reconstruction_is_visibly_labelled(tmp_path):
    path = tmp_path/'legacy.jsonl'; path.write_text(json.dumps(dict(t=10, cmd=['query'], out='value'))+'\n')
    replay = record.ReplayBackend(str(path), paused=True)
    assert 'reconstructed legacy' in replay.summary()
    text = ''.join(text for text, _ in Views(Glyphs(True), Config({})).replay_bar(replay, 80))
    assert 'reconstructed replay' in text


@pytest.mark.parametrize('change', ['job', 'scope', 'inventory'])
def test_selected_sample_identity_must_agree_with_observation(change):
    store = Store(persist=False); bind(store)
    value = H.validate(H.capture(store, '7'))
    if change == 'job': value['identity']['job'] = '8'
    elif change == 'scope': value['identity']['scope'] = 'another-cluster'
    else: value['attempts']['7']['attempt'] = 'another-attempt'
    with pytest.raises(ValueError): H.validate(H.encoded(value))


def test_unverified_rows_from_different_observers_are_not_claimed_as_one_attempt():
    before = snapshot(); after = snapshot(110, observer='second')
    after['jobs'][0]['state'] = 'COMPLETED'
    result = H.difference(before, after)
    assert not result['changed']
    assert len(result['appeared']) == len(result['disappeared']) == 1


def test_jobs_history_and_mouse_controls_preserve_live_context(app):
    state = UI.initialize(app)
    value = snapshot(); value['history'] = [dict(id='2', state='COMPLETED', name='finished')]
    state['timeline'].append(H.encoded(value))
    app.run_command('asof'); drain(app)
    app._handle('2'); drain(app)
    views = Views(Glyphs(True), Config({}))
    rows, hits = views.compose(app.store.snapshot(), app, 80, 24)
    assert not hits and all(not ''.join(s for s, _ in row).strip() for row in rows)
    overlay = UI.overlay(views, {}, app, 80, 24)
    assert 'finished' in '\n'.join(''.join(s for s, _ in row) for _, _, row in overlay)
    live = next((row, item) for row, _, item in state['controls'] if item['action'] == ('command', 'asof key esc'))
    app.click(live[0], live[1]['left'], [('cancel', '7')])
    assert app.mode == 'main' and app.selected_id == '7' and app.top['jobs'] == 3


def test_return_from_run_home_keeps_exact_existing_binding(app):
    app.mode = 'run_home'
    binding = dict(run_id='run-a', attempt='2', project_root='/project', job_id='88')
    app.run_home_state['binding'] = copy.deepcopy(binding)
    state = UI.initialize(app); state['timeline'].append(H.capture(app.store, '7'))
    app.run_command('asof'); drain(app)
    assert 'run-a' in state['context_label'] and 'attribution' in state['context_label']
    app.run_command('asof live')
    assert app.mode == 'run_home' and app.run_home_state['binding'] == binding
    assert app.selected_id == '7'
